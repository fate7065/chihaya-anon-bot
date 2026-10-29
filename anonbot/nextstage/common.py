"""Small bounded synthetic journal shared by candidate features, not production Store."""
from contextlib import contextmanager
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3

from ..domain import EventConflict, Scope, ValidationError


def text(value, limit=1000):
    if not isinstance(value,str) or not value.strip() or len(value)>limit:
        raise ValidationError('文本为空或超长')
    return value


def identity(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value):
        raise ValidationError('标识无效')
    return value


def moment(value):
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=2**53:
        raise ValidationError('时钟须为有限非负秒数')
    return value


def canonical(value):
    try:return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':'))
    except (TypeError,ValueError):raise ValidationError('数据无法规范序列化') from None


def fingerprint(value):return sha256(canonical(value).encode('utf-8')).hexdigest()


class Journal:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.transaction() as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables!={'nextstage_meta','nextstage_events'}:
                raise ValidationError('不是下一阶段独立合成日志')
            if tables:
                if db.execute('SELECT version,origin FROM nextstage_meta').fetchall()!=[(1,'synthetic')]:
                    raise ValidationError('不支持的日志版本或来源')
            else:
                db.execute('CREATE TABLE nextstage_meta(version INTEGER,origin TEXT)')
                db.execute("INSERT INTO nextstage_meta VALUES(1,'synthetic')")
                db.execute('''CREATE TABLE nextstage_events(seq INTEGER PRIMARY KEY,scope TEXT NOT NULL,
                    kind TEXT NOT NULL,event_id TEXT NOT NULL,payload TEXT NOT NULL,
                    UNIQUE(scope,kind,event_id))''')

    @contextmanager
    def transaction(self):
        db=sqlite3.connect(self.path,timeout=10)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                yield db
        finally:db.close()

    def append(self,scope,kind,event_id,data,guard=None,db=None):
        if not isinstance(scope,Scope) or type(data) is not dict:raise ValidationError('作用域或事件数据无效')
        identity(kind);identity(event_id);payload=canonical(data)
        if len(payload)>65536:raise ValidationError('事件数据超出上限')
        if db is None:
            with self.transaction() as connection:
                return self.append(scope,kind,event_id,data,guard=guard,db=connection)
        old=db.execute('SELECT payload FROM nextstage_events WHERE scope=? AND kind=? AND event_id=?',
                       (scope.key,kind,event_id)).fetchone()
        if old is not None:
            if old[0]!=payload:raise EventConflict('同事件携带不同数据')
            return False
        if db.execute('SELECT count(*) FROM nextstage_events WHERE scope=? AND kind=?',(scope.key,kind)).fetchone()[0]>=2000:
            raise ValidationError('当前作用域事件数量已达上限')
        if guard is not None:guard(db)
        db.execute('INSERT INTO nextstage_events(scope,kind,event_id,payload) VALUES(?,?,?,?)',
                   (scope.key,kind,event_id,payload))
        return True

    def events(self,scope,kind,db=None):
        if not isinstance(scope,Scope):raise ValidationError('作用域无效')
        identity(kind)
        if db is None:
            with self.transaction() as connection:return self.events(scope,kind,connection)
        rows=db.execute('SELECT event_id,payload FROM nextstage_events WHERE scope=? AND kind=? ORDER BY seq LIMIT 2001',
                        (scope.key,kind)).fetchall()
        if len(rows)>2000:raise ValidationError('事件数量超过上限')
        return [dict(event_id=row[0],data=json.loads(row[1])) for row in rows]
