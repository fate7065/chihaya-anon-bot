"""Dedicated group observation ledger; transactions never span model calls."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
from uuid import uuid4

from ..domain import EventConflict, ValidationError
from ..gateway import QueueFull
from .group import GroupMessage, GroupSnapshot, RoomRef
from .group_ingress import IngressEvent


CONTEXT_AGE = 1800


def finite_time(value):
    if type(value) not in (int, float):
        raise ValidationError('时钟须为有限数字')
    try:
        number = float(value)
    except OverflowError:
        raise ValidationError('时钟超出范围') from None
    if not math.isfinite(number):
        raise ValidationError('时钟须为有限数字')
    return number


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class ShadowStore:
    def __init__(self, path):
        if str(path) == ':memory:':
            raise ValidationError('群聊观察需要持久数据库文件')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            objects = {(r[0], r[1]) for r in db.execute(
                "SELECT name,type FROM sqlite_master WHERE type IN ('table','view','trigger') "
                "AND name NOT LIKE 'sqlite_%'")}
            expected = {(name, 'table') for name in ('shadow_meta', 'shadow_events', 'shadow_calls')}
            if objects:
                if objects != expected:
                    raise ValidationError('请选择独立的群聊观察数据库')
                metadata = db.execute('SELECT app,version FROM shadow_meta').fetchall()
                if len(metadata) != 1 or tuple(metadata[0]) != ('anonbot.group-shadow', 1):
                    raise ValidationError('观察数据库版本不兼容')
                return
            db.execute('CREATE TABLE shadow_meta(app TEXT,version INTEGER)')
            db.execute('INSERT INTO shadow_meta VALUES (?,?)', ('anonbot.group-shadow', 1))
            db.execute('''CREATE TABLE shadow_events(
                id INTEGER PRIMARY KEY, room_key TEXT NOT NULL, event_id TEXT NOT NULL,
                sender_id TEXT NOT NULL, payload_hash TEXT NOT NULL, event_json TEXT NOT NULL,
                received REAL NOT NULL, status TEXT NOT NULL, owner TEXT,
                decision_json TEXT, snapshot_json TEXT, stale INTEGER NOT NULL DEFAULT 0,
                UNIQUE(room_key,event_id))''')
            db.execute('CREATE INDEX shadow_room ON shadow_events(room_key,id)')
            db.execute('CREATE INDEX shadow_queue ON shadow_events(status,id)')
            db.execute('''CREATE TABLE shadow_calls(
                id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, role TEXT NOT NULL,
                called REAL NOT NULL, status TEXT NOT NULL,
                prompt_tokens INTEGER, completion_tokens INTEGER)''')
            db.execute('CREATE INDEX shadow_calls_time ON shadow_calls(called)')

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def submit(self, event, now, queue_limit):
        if not isinstance(event, IngressEvent):
            raise ValidationError('观察入库须为IngressEvent')
        now = finite_time(now)
        if type(queue_limit) is not int or not 1 <= queue_limit <= 1000:
            raise ValidationError('队列上限须为1–1000')
        content = canonical(asdict(event))
        fingerprint = sha256(content.encode('utf-8')).hexdigest()
        with self.transaction() as db:
            existing = db.execute('SELECT payload_hash FROM shadow_events WHERE room_key=? AND event_id=?',
                                  (event.room.key, event.message.event_id)).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise EventConflict('同一群事件载荷冲突')
                return 'duplicate'
            pending = db.execute("SELECT count(*) FROM shadow_events WHERE status IN ('queued','processing')").fetchone()[0]
            if pending >= queue_limit:
                raise QueueFull('观察队列已满')
            db.execute('''INSERT INTO shadow_events
                (room_key,event_id,sender_id,payload_hash,event_json,received,status)
                VALUES (?,?,?,?,?,?,?)''', (event.room.key, event.message.event_id,
                event.message.sender_id, fingerprint, content, now, 'queued'))
        return 'queued'

    @staticmethod
    def _message(db, row):
        value = json.loads(row['event_json'])
        message = GroupMessage(**value['message'])
        reference_status = 'none'
        if value['reply_id'] is not None:
            target = db.execute('''SELECT sender_id FROM shadow_events
                WHERE room_key=? AND event_id=? AND id<?''',
                (row['room_key'], value['reply_id'], row['id'])).fetchone()
            reference_status = ('unknown' if target is None else
                'bot' if target[0] == value['room']['account_id'] else 'other')
            message = replace(message, reply_to_bot=reference_status == 'bot')
        return message, reference_status

    def claim(self, now, debounce_seconds):
        now, debounce_seconds = finite_time(now), finite_time(debounce_seconds)
        if not 0 <= debounce_seconds <= 60:
            raise ValidationError('合并等待须为0–60秒')
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM shadow_events WHERE status='processing' LIMIT 1").fetchone():
                return None
            groups = db.execute('''SELECT room_key,MIN(id) AS oldest,MAX(id) AS focus,MIN(received) AS first_received
                FROM shadow_events WHERE status='queued' GROUP BY room_key ORDER BY oldest''').fetchall()
            for group in groups:
                row = db.execute('SELECT * FROM shadow_events WHERE id=?', (group['focus'],)).fetchone()
                if (now - row['received'] < debounce_seconds
                        and now - group['first_received'] < max(2, debounce_seconds)):
                    continue
                value = json.loads(row['event_json'])
                room = RoomRef(**value['room'])
                rows = db.execute('''SELECT * FROM shadow_events
                    WHERE room_key=? AND id<=? AND received>=? ORDER BY id DESC LIMIT 20''',
                    (room.key, row['id'], row['received'] - CONTEXT_AGE)).fetchall()
                messages, used = [], 0
                for previous in rows:
                    message, _ = self._message(db, previous)
                    if used + len(message.text) > 12000:
                        break
                    messages.append(message)
                    used += len(message.text)
                _, reference_status = self._message(db, row)
                own = db.execute('''SELECT event_json FROM shadow_events
                    WHERE room_key=? AND sender_id=? AND id<? ORDER BY id DESC LIMIT 1''',
                    (room.key, room.account_id, row['id'])).fetchone()
                snapshot = GroupSnapshot(room, row['id'], tuple(reversed(messages)), value['event_time'],
                    json.loads(own[0])['event_time'] if own else None)
                owner = uuid4().hex
                db.execute("UPDATE shadow_events SET status='superseded' WHERE room_key=? AND status='queued' AND id<?",
                           (room.key, row['id']))
                db.execute("UPDATE shadow_events SET status='processing',owner=?,snapshot_json=? WHERE id=?",
                           (owner, canonical(asdict(snapshot)), row['id']))
                return {'id': row['id'], 'owner': owner, 'room_key': room.key,
                        'event_id': row['event_id'], 'snapshot': snapshot,
                        'reference_status': reference_status, 'media_only': value['media_only']}
        return None

    def finish(self, job, decision):
        with self.transaction() as db:
            current = db.execute("SELECT room_key FROM shadow_events WHERE id=? AND owner=? AND status='processing'",
                                 (job['id'], job['owner'])).fetchone()
            if current is None:
                return None
            stale = bool(db.execute('SELECT 1 FROM shadow_events WHERE room_key=? AND id>? LIMIT 1',
                                    (current['room_key'], job['id'])).fetchone())
            db.execute("UPDATE shadow_events SET status='observed',decision_json=?,stale=? WHERE id=?",
                       (canonical(asdict(decision)), int(stale), job['id']))
        return stale

    def reserve_call(self, job, role, now, limit):
        now = finite_time(now)
        if role not in ('address', 'relevance', 'timing') or type(limit) is not int or not 1 <= limit <= 10000:
            raise ValidationError('调用预算或维度无效')
        with self.transaction() as db:
            current = db.execute("SELECT 1 FROM shadow_events WHERE id=? AND owner=? AND status='processing'",
                                 (job['id'], job['owner'])).fetchone()
            if current is None:
                return None
            used = db.execute('SELECT count(*) FROM shadow_calls WHERE called>?', (now - 3600,)).fetchone()[0]
            if used >= limit:
                return None
            return db.execute('INSERT INTO shadow_calls(job_id,role,called,status) VALUES (?,?,?,?)',
                              (job['id'], role, now, 'attempting')).lastrowid

    def complete_call(self, identity, status, metadata):
        if status not in ('returned', 'failed'):
            raise ValidationError('调用状态无效')
        tokens = []
        for name in ('prompt_tokens', 'completion_tokens'):
            value = metadata.get(name) if isinstance(metadata, dict) else None
            tokens.append(value if type(value) is int and 0 <= value <= 10000000 else None)
        with self.transaction() as db:
            db.execute("UPDATE shadow_calls SET status=?,prompt_tokens=?,completion_tokens=? WHERE id=? AND status='attempting'",
                       (status, *tokens, identity))

    def recover(self):
        """Only call under the observer's exclusive instance lock."""
        with self.transaction() as db:
            changed = db.execute("UPDATE shadow_events SET status='unknown' WHERE status='processing'").rowcount
            db.execute("UPDATE shadow_calls SET status='unknown' WHERE status='attempting'")
        return changed

    def status(self, now):
        now = finite_time(now)
        with self.transaction() as db:
            return {'events': {r[0]: r[1] for r in db.execute('SELECT status,count(*) FROM shadow_events GROUP BY status')},
                    'stale_observations': db.execute('SELECT count(*) FROM shadow_events WHERE stale=1').fetchone()[0],
                    'api_attempts_1h': db.execute('SELECT count(*) FROM shadow_calls WHERE called>?',
                                                (now - 3600,)).fetchone()[0]}

    def export_candidates(self, limit=1000):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValidationError('导出上限须为1–1000')
        with self.transaction() as db:
            return [dict(r) for r in db.execute('''SELECT id,room_key,received,snapshot_json,stale
                FROM shadow_events WHERE status='observed' ORDER BY id LIMIT ?''', (limit,))]
