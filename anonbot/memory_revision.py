"""Explicit local revision visibility for the opt-in synthetic memory candidate."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .domain import EventConflict, ValidationError
from .memory_candidate import CandidateMemory, MemoryEvidence, _event_id


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _time(value):
    try:
        if not isinstance(value,str) or len(value)>64:raise ValueError()
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None:raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError,OverflowError):
        raise ValidationError('记忆操作时间须为带时区的ISO时间') from None


class MemoryRevisionStore:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self._tx() as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected={'memory_revision_meta','revision_events','revision_operations'}
            if tables:
                columns={'memory_revision_meta':('version',),
                         'revision_events':('namespace','event_id','evidence_json','state','replacement','operation_id'),
                         'revision_operations':('namespace','operation_id','payload_hash','target','new_event','state','receipt_json')}
                if tables!=expected:
                    raise ValidationError('未知的记忆状态账本结构或版本')
                for name,wanted in columns.items():
                    if tuple(row[1] for row in db.execute(f'PRAGMA table_info({name})'))!=wanted:
                        raise ValidationError('未知的记忆状态账本列')
                if db.execute('SELECT version FROM memory_revision_meta').fetchall()!=[(1,)]:
                    raise ValidationError('未知的记忆状态账本版本')
            else:
                db.execute('CREATE TABLE memory_revision_meta(version INTEGER NOT NULL)')
                db.execute('INSERT INTO memory_revision_meta VALUES (1)')
                db.execute('''CREATE TABLE revision_events(
                    namespace TEXT NOT NULL,event_id TEXT NOT NULL,evidence_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active','pending','superseded','invalidated','failed')),
                    replacement TEXT,operation_id TEXT NOT NULL,PRIMARY KEY(namespace,event_id))''')
                db.execute('''CREATE TABLE revision_operations(
                    namespace TEXT NOT NULL,operation_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
                    target TEXT NOT NULL,new_event TEXT,state TEXT NOT NULL CHECK(state IN ('pending','committed','failed')),
                    receipt_json TEXT,PRIMARY KEY(namespace,operation_id))''')
                db.execute("CREATE UNIQUE INDEX revision_pending ON revision_operations(namespace,target) WHERE state='pending'")

    @contextmanager
    def _tx(self):
        db=sqlite3.connect(self.path,timeout=10)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                yield db
        finally:db.close()

    @staticmethod
    def _row(db,namespace,event_id):
        row=db.execute('''SELECT evidence_json,state,replacement,operation_id FROM revision_events
                          WHERE namespace=? AND event_id=?''',(namespace,event_id)).fetchone()
        if row is None:return None
        return {**json.loads(row[0]),'state':row[1],'replacement':row[2],'operation_id':row[3]}

    def register(self,namespace,evidence):
        payload=_json(evidence)
        with self._tx() as db:
            old=self._row(db,namespace,evidence['event_id'])
            if old is not None:
                if self.evidence(old)!=evidence:raise EventConflict('已登记的原始证据发生冲突')
                return old
            db.execute('INSERT INTO revision_events VALUES (?,?,?,?,?,?)',
                       (namespace,evidence['event_id'],payload,'active',None,''))
            return self._row(db,namespace,evidence['event_id'])

    @staticmethod
    def evidence(row):
        return {key:row[key] for key in ('event_id','text','speaker_id','observed_at','origin','kind')}

    def get(self,namespace,event_id):
        with self._tx() as db:return self._row(db,namespace,event_id)

    def states(self,namespace,event_ids):
        # One snapshot for the complete bounded candidate list.
        if not event_ids:return {}
        placeholders=','.join('?' for _ in event_ids)
        with self._tx() as db:
            rows=db.execute(f'''SELECT event_id,evidence_json,state,replacement,operation_id
                FROM revision_events WHERE namespace=? AND event_id IN ({placeholders})''',
                (namespace,*event_ids)).fetchall()
            return {row[0]:{**json.loads(row[1]),'state':row[2],'replacement':row[3],'operation_id':row[4]} for row in rows}

    @staticmethod
    def _fingerprint(payload):
        return sha256(_json(payload).encode('utf-8')).hexdigest()

    @staticmethod
    def _replay(db,namespace,operation_id,fingerprint):
        old=db.execute('''SELECT payload_hash,state,receipt_json FROM revision_operations
                          WHERE namespace=? AND operation_id=?''',(namespace,operation_id)).fetchone()
        if old is None:return None
        if old[0]!=fingerprint:raise EventConflict('记忆操作ID携带不同内容')
        if old[1]!='committed':raise ValidationError('记忆操作待核对或已失败，禁止自动重复执行')
        return dict(status='duplicate',receipt=json.loads(old[2]))

    def _active(self,db,namespace,target):
        old=self._row(db,namespace,target)
        if old is None or old['state']!='active':raise ValidationError('目标记忆不存在或不是当前有效版本')
        if db.execute("SELECT 1 FROM revision_operations WHERE namespace=? AND target=? AND state='pending'",
                      (namespace,target)).fetchone():
            raise ValidationError('目标已有待提交更正，不能并行修改')
        return old

    def prepare_correction(self,namespace,operation_id,target,evidence):
        fingerprint=self._fingerprint(dict(action='correct',target=target,evidence=evidence))
        with self._tx() as db:
            replay=self._replay(db,namespace,operation_id,fingerprint)
            if replay is not None:return replay
            old=self._active(db,namespace,target)
            if datetime.fromisoformat(evidence['observed_at'])<=datetime.fromisoformat(old['observed_at']):
                raise ValidationError('更正证据时间必须晚于旧证据')
            if ' '.join(old['text'].split())==' '.join(evidence['text'].split()):
                raise ValidationError('更正内容未发生变化')
            if self._row(db,namespace,evidence['event_id']) is not None:
                raise ValidationError('更正需要一个未登记的新事件ID')
            db.execute('INSERT INTO revision_operations VALUES (?,?,?,?,?,?,?)',
                       (namespace,operation_id,fingerprint,target,evidence['event_id'],'pending',None))
            db.execute('INSERT INTO revision_events VALUES (?,?,?,?,?,?)',
                       (namespace,evidence['event_id'],_json(evidence),'pending',None,operation_id))
        return None

    def commit_correction(self,namespace,operation_id):
        with self._tx() as db:
            return self._commit_correction(db,namespace,operation_id)

    def _commit_correction(self,db,namespace,operation_id,resolution=None):
        operation=db.execute('''SELECT target,new_event,state,receipt_json FROM revision_operations
                                WHERE namespace=? AND operation_id=?''',(namespace,operation_id)).fetchone()
        if operation is None:raise ValidationError('更正操作不存在')
        target,new_event,state,receipt=operation
        if state=='committed':return dict(status='duplicate',receipt=json.loads(receipt))
        old=self._row(db,namespace,target);new=self._row(db,namespace,new_event)
        if (state!='pending' or old is None or old['state']!='active' or new is None
                or new['state']!='pending' or new['operation_id']!=operation_id):
            raise ValidationError('更正提交状态不一致')
        receipt=dict(operation_id=operation_id,action='correct',target=target,new_event=new_event,
                     observed_at=new['observed_at'],state='committed')
        if resolution is not None:receipt['resolution']=resolution
        db.execute("UPDATE revision_events SET state='superseded',replacement=? WHERE namespace=? AND event_id=?",
                   (new_event,namespace,target))
        db.execute("UPDATE revision_events SET state='active' WHERE namespace=? AND event_id=?",(namespace,new_event))
        db.execute("UPDATE revision_operations SET state='committed',receipt_json=? WHERE namespace=? AND operation_id=?",
                   (_json(receipt),namespace,operation_id))
        return dict(status='applied',receipt=receipt)

    def _operation_snapshot(self,db,namespace,operation_id):
        row=db.execute('''SELECT payload_hash,target,new_event,state,receipt_json
                          FROM revision_operations WHERE namespace=? AND operation_id=?''',
                          (namespace,operation_id)).fetchone()
        if row is None:raise ValidationError('当前作用域中不存在此记忆操作')
        return dict(namespace=namespace,operation_id=operation_id,payload_hash=row[0],
                    target_event=row[1],new_event=row[2],state=row[3],
                    receipt=json.loads(row[4]) if row[4] is not None else None,
                    target=self._row(db,namespace,row[1]),
                    new=self._row(db,namespace,row[2]) if row[2] is not None else None)

    def operation_snapshot(self,namespace,operation_id):
        with self._tx() as db:return self._operation_snapshot(db,namespace,operation_id)

    def resolve_pending(self,namespace,operation_id,action,expected_snapshot,fingerprint):
        with self._tx() as db:
            snapshot=self._operation_snapshot(db,namespace,operation_id)
            receipt=snapshot['receipt'] or {}
            if snapshot['state']!='pending':
                resolution=receipt.get('resolution',{})
                if not resolution:raise ValidationError('此操作不是待核对更正，不能恢复')
                if resolution.get('action')!=action or resolution.get('fingerprint')!=fingerprint:
                    raise EventConflict('已处置操作不能换动作或核对指纹')
                return dict(status='duplicate',receipt=receipt)
            if self._fingerprint(snapshot)!=expected_snapshot:
                raise EventConflict('账本状态已改变，需重新核对')
            old,new=snapshot['target'],snapshot['new']
            if (old is None or old['state']!='active' or new is None or new['state']!='pending'
                    or new['operation_id']!=operation_id):
                raise ValidationError('待恢复操作的版本状态不一致')
            resolution=dict(action=action,fingerprint=fingerprint,
                            resolved_at=datetime.now(timezone.utc).isoformat(),source='local-explicit')
            if action=='finish':return self._commit_correction(db,namespace,operation_id,resolution)
            if action!='cancel':raise ValidationError('恢复动作须为finish或cancel')
            receipt=dict(operation_id=operation_id,action='correct',target=snapshot['target_event'],
                         new_event=snapshot['new_event'],state='cancelled',resolution=resolution)
            db.execute("UPDATE revision_events SET state='failed' WHERE namespace=? AND event_id=?",
                       (namespace,snapshot['new_event']))
            db.execute("UPDATE revision_operations SET state='failed',receipt_json=? WHERE namespace=? AND operation_id=?",
                       (_json(receipt),namespace,operation_id))
            return dict(status='applied',receipt=receipt)

    def fail_correction(self,namespace,operation_id):
        with self._tx() as db:
            operation=db.execute('''SELECT new_event FROM revision_operations
                WHERE namespace=? AND operation_id=? AND state='pending' ''',(namespace,operation_id)).fetchone()
            if operation is None:return
            db.execute("UPDATE revision_events SET state='failed' WHERE namespace=? AND event_id=? AND state='pending'",
                       (namespace,operation[0]))
            db.execute("UPDATE revision_operations SET state='failed' WHERE namespace=? AND operation_id=?",
                       (namespace,operation_id))

    def invalidate(self,namespace,operation_id,target,at,reason):
        fingerprint=self._fingerprint(dict(action='invalidate',target=target,observed_at=at,reason=reason))
        with self._tx() as db:
            replay=self._replay(db,namespace,operation_id,fingerprint)
            if replay is not None:return replay
            old=self._active(db,namespace,target)
            if datetime.fromisoformat(at)<datetime.fromisoformat(old['observed_at']):
                raise ValidationError('失效时间不能早于证据时间')
            receipt=dict(operation_id=operation_id,action='invalidate',target=target,
                         observed_at=at,reason=reason,state='committed')
            db.execute('INSERT INTO revision_operations VALUES (?,?,?,?,?,?,?)',
                       (namespace,operation_id,fingerprint,target,None,'committed',_json(receipt)))
            db.execute("UPDATE revision_events SET state='invalidated' WHERE namespace=? AND event_id=?",(namespace,target))
            return dict(status='applied',receipt=receipt)


class ManagedCandidateMemory:
    def __init__(self,memory,store):
        if not isinstance(memory,CandidateMemory) or not isinstance(store,MemoryRevisionStore):
            raise ValidationError('需要绑定的候选记忆和状态账本')
        self.raw,self.store,self.namespace=memory,store,memory.namespace

    async def remember(self,evidence):
        result=await self.raw.remember(evidence)
        row=self.store.register(self.namespace,result['evidence'])
        return dict(status=result['status'],evidence=row,active=row['state']=='active')

    async def correct(self,operation_id,target_event,new_evidence):
        _event_id(operation_id);_event_id(target_event)
        if not isinstance(new_evidence,MemoryEvidence):raise ValidationError('更正需要原始合成证据')
        expected_author=self.raw.scope.owner_id if self.raw.kind=='user_statement' else self.raw.scope.bot_id
        if new_evidence.speaker_id!=expected_author or new_evidence.event_id==target_event:
            raise ValidationError('更正作者或新事件ID无效')
        evidence={**asdict(new_evidence),'kind':self.raw.kind}
        replay=self.store.prepare_correction(self.namespace,operation_id,target_event,evidence)
        if replay is not None:return replay
        try:
            await self.raw.remember(new_evidence)
        except Exception:
            self.store.fail_correction(self.namespace,operation_id)
            raise
        # If commit is interrupted, retain pending evidence, never make it visible early.
        return self.store.commit_correction(self.namespace,operation_id)

    def invalidate(self,operation_id,target_event,observed_at,reason):
        _event_id(operation_id);_event_id(target_event)
        at=_time(observed_at)
        if not isinstance(reason,str) or not reason.strip() or len(reason)>200:
            raise ValidationError('失效理由须为1–200字非空文本')
        return self.store.invalidate(self.namespace,operation_id,target_event,at,reason)

    async def get(self,event_id,include_inactive=False):
        _event_id(event_id)
        if type(include_inactive) is not bool:raise ValidationError('审计开关须为bool')
        row=self.store.get(self.namespace,event_id)
        if row is None or (row['state']!='active' and not include_inactive):return None
        if row['state']=='active' and await self.raw.get(event_id)!=self.store.evidence(row):
            raise ValidationError('上游原文与已登记证据不一致')
        return row

    async def search(self,query,limit=5):
        return await self._search(query,limit,self.raw.search)

    async def search_existing(self,query,limit=5):
        return await self._search(query,limit,self.raw.search_existing,follow=True)

    def _replacement(self,row):
        seen=set()
        while row is not None and row['state']=='superseded':
            if row['event_id'] in seen or len(seen)>=10:
                raise ValidationError('记忆更正关系循环或超过10级')
            seen.add(row['event_id'])
            target=row['replacement'];_event_id(target)
            row=self.store.get(self.namespace,target)
            if row is None:raise ValidationError('记忆更正关系指向缺失版本')
        return row,bool(seen)

    async def _search(self,query,limit,search,follow=False):
        if type(limit) is not int or not 1<=limit<=10:raise ValidationError('记忆查询limit须为1–10整数')
        result=await search(query,10)
        rows=self.store.states(self.namespace,[hit['event_id'] for hit in result['hits']])
        active=[];seen=set();followed_count=0
        for hit in result['hits']:
            row=rows.get(hit['event_id'])
            if follow and row is not None and self.store.evidence(row)!=hit:
                raise ValidationError('检索起点原文与已登记证据不一致')
            followed=False
            if follow:row,followed=self._replacement(row)
            if row is None or row['state']!='active':continue
            if follow and row['event_id'] in seen:continue
            actual=await self.raw.read_existing(row['event_id']) if followed else hit
            if self.store.evidence(row)!=actual:raise ValidationError('检索原文与已登记证据不一致')
            seen.add(row['event_id']);followed_count+=int(followed)
            active.append(row)
        return dict(hits=active[:limit],diagnostics={**result['diagnostics'],
                    'revision_filtered_count':len(result['hits'])-len(active),
                    'revision_followed_count':followed_count,'candidate_limit':10})
