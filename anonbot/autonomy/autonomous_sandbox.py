"""Durable bounded scheduler for the synthetic world; no live executor injection."""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from ..domain import EventConflict,ValidationError
from .actions import ActionRuntime,Grant,default_registry
from .group import _number
from .sandbox_plan import SandboxPlan,name
from .sandbox_world import SandboxWorld
from .workflow_lock import RunnerLock


class AutonomousSandbox:
    def __init__(self,directory,*,actions_per_hour=60):
        if type(actions_per_hour) is not int or not 1<=actions_per_hour<=1000:
            raise ValidationError('hourly action budget must be 1–1000')
        self.directory=Path(directory).resolve();self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/'scheduler.sqlite3';self.actions_per_hour=actions_per_hour
        with self.transaction() as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables!={'sandbox_meta','sandbox_jobs','sandbox_quota'}:
                raise ValidationError('use a dedicated scheduler database')
            db.execute('CREATE TABLE IF NOT EXISTS sandbox_meta(id INTEGER PRIMARY KEY,last_tick REAL,quiet_until REAL)')
            db.execute('INSERT OR IGNORE INTO sandbox_meta VALUES(1,-1,0)')
            db.execute('''CREATE TABLE IF NOT EXISTS sandbox_jobs(id TEXT PRIMARY KEY,fingerprint TEXT,
                plan TEXT,status TEXT,approved INTEGER,due REAL,interval REAL,max_runs INTEGER,
                run INTEGER,next_step INTEGER,results TEXT,paused INTEGER,detail TEXT)''')
            db.execute('CREATE TABLE IF NOT EXISTS sandbox_quota(id TEXT PRIMARY KEY,created REAL)')
            self.now=max(0,db.execute('SELECT last_tick FROM sandbox_meta').fetchone()[0])
        self.world=SandboxWorld(self.directory/'world')

    @contextmanager
    def transaction(self):
        db=sqlite3.connect(self.path,timeout=5,isolation_level=None);db.row_factory=sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE');yield db;db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:db.execute('ROLLBACK')
            raise
        finally:db.close()

    def status(self,identity=None):
        with self.transaction() as db:
            if identity is None:rows=db.execute('SELECT * FROM sandbox_jobs ORDER BY due,id').fetchall()
            else:
                name(identity);rows=db.execute('SELECT * FROM sandbox_jobs WHERE id=?',(identity,)).fetchall()
                if not rows:raise ValidationError('unknown sandbox task')
        result=[]
        for row in rows:
            value=dict(row);value['plan']=json.loads(value['plan']);value['results']=json.loads(value['results'])
            result.append(value)
        return result[0] if identity is not None else result

    def submit(self,identity,plan,*,due=0,interval=0,max_runs=1):
        name(identity)
        if not isinstance(plan,SandboxPlan):raise ValidationError('validated sandbox plan required')
        _number(due,'due',0,1e12);_number(interval,'interval',0,86400)
        if (type(max_runs) is not int or not 1<=max_runs<=20 or
                (max_runs>1 and interval<60) or (max_runs==1 and interval!=0)):
            raise ValidationError('invalid bounded recurrence')
        digest=sha256((plan._json+json.dumps([due,interval,max_runs])).encode()).hexdigest()
        with self.transaction() as db:
            old=db.execute('SELECT fingerprint FROM sandbox_jobs WHERE id=?',(identity,)).fetchone()
            if old:
                if old[0]!=digest:raise EventConflict('task id conflicts with existing content or schedule')
            else:
                if db.execute('SELECT COUNT(*) FROM sandbox_jobs').fetchone()[0]>=100:
                    raise ValidationError('sandbox task limit reached')
                approved=int(not plan.has_writes)
                db.execute('INSERT INTO sandbox_jobs VALUES(?,?,?,?,?,?,?,?,0,0,?,0,?)',
                    (identity,digest,plan._json,'waiting' if approved else 'awaiting_approval',approved,
                     due,interval,max_runs,'{}','submitted'))
        return self.status(identity)

    def approve(self,identity,fingerprint):
        job=self.status(identity)
        if fingerprint!=job['fingerprint']:raise ValidationError('approval does not match frozen plan and schedule')
        with self.transaction() as db:
            db.execute("""UPDATE sandbox_jobs SET approved=1,status=CASE WHEN paused=1 THEN 'paused' ELSE 'waiting' END,
                detail='plan_approved' WHERE id=? AND status IN ('awaiting_approval','paused')""",(identity,))
        return self.status(identity)

    def pause(self,identity):
        self.status(identity)
        with self.transaction() as db:
            db.execute("""UPDATE sandbox_jobs SET paused=1,status=CASE WHEN status='running' THEN status ELSE 'paused' END,
                detail='stop_requested' WHERE id=? AND status NOT IN ('completed','blocked')""",(identity,))
        return self.status(identity)

    def resume(self,identity):
        if self.status(identity)['status']!='paused':raise ValidationError('only a paused task may resume')
        with self.transaction() as db:
            db.execute("""UPDATE sandbox_jobs SET paused=0,status=CASE WHEN approved=1 THEN 'waiting' ELSE 'awaiting_approval' END,
                detail='resumed' WHERE id=? AND status='paused'""",(identity,))
        return self.status(identity)

    def set_quiet_until(self,at):
        _number(at,'quiet_until',0,1e12)
        with self.transaction() as db:db.execute('UPDATE sandbox_meta SET quiet_until=?',(at,))

    def _block(self,identity,reason):
        with self.transaction() as db:db.execute("UPDATE sandbox_jobs SET status='blocked',detail=? WHERE id=?",(reason,identity))

    def _runtime(self,intent=None):
        grants=[] if intent is None else [Grant.for_intent(intent)]
        return ActionRuntime(self.directory/'actions.sqlite3',default_registry(),grants,
                             ['sandbox-owner'],clock=lambda:self.now)

    def _step(self,job,faults):
        plan=SandboxPlan(job['plan']);index=job['next_step'];step=plan.document['steps'][index]
        try:intent=plan.intent(index,job['results'],job['id'],job['run'])
        except ValidationError:
            self._block(job['id'],'binding_unavailable');return True
        runtime=self._runtime(intent);record=runtime.get(intent.intent_id)
        if record is None or record.status in ('ready','pending_approval'):
            with self.transaction() as db:
                current=db.execute('SELECT status,paused,approved FROM sandbox_jobs WHERE id=?',(job['id'],)).fetchone()
                if current['status']!='waiting' or current['paused'] or not current['approved']:
                    return False
                reserved=db.execute('SELECT 1 FROM sandbox_quota WHERE id=?',(intent.intent_id,)).fetchone()
                count=db.execute('SELECT COUNT(*) FROM sandbox_quota WHERE created>?',(self.now-3600,)).fetchone()[0]
                if not reserved and count>=self.actions_per_hour:
                    db.execute("UPDATE sandbox_jobs SET detail='hourly_quota' WHERE id=?",(job['id'],));return False
                db.execute('INSERT OR IGNORE INTO sandbox_quota VALUES(?,?)',(intent.intent_id,self.now))
                db.execute("UPDATE sandbox_jobs SET status='running',detail='action_started' WHERE id=?",(job['id'],))
            record=runtime.submit(intent)
            if record.status=='pending_approval':record=runtime.approve(intent.intent_id,'sandbox-owner')
            executor=SimpleNamespace(execute=lambda action:self.world.execute(action,fault=faults.get(step['id'])))
            record=runtime.execute(intent.intent_id,executor)
        if record.status!='simulated':
            self._block(job['id'],'action_'+record.status);return True
        results={**job['results'],step['id']:record.data};index+=1
        status='waiting';run=job['run'];due=job['due']
        if index==len(plan.document['steps']):
            run+=1
            if run>=job['max_runs']:status='completed'
            else:
                due=max(self.now,job['due'])+job['interval'];index=0;results={}
        with self.transaction() as db:
            paused=db.execute('SELECT paused FROM sandbox_jobs WHERE id=?',(job['id'],)).fetchone()[0]
            if paused and status!='completed':status='paused'
            db.execute('UPDATE sandbox_jobs SET status=?,run=?,next_step=?,results=?,due=?,detail=? WHERE id=?',
                       (status,run,index,json.dumps(results,ensure_ascii=False),due,'step_confirmed',job['id']))
        return True

    def tick(self,at,*,max_steps=20,faults=None,should_stop=None):
        _number(at,'scheduler time',0,1e12)
        if type(max_steps) is not int or not 1<=max_steps<=100:
            raise ValidationError('tick budget must be 1–100')
        if faults is not None and (type(faults) is not dict or any(v not in ('before','after') for v in faults.values())):
            raise ValidationError('invalid test fault map')
        if should_stop is not None and not callable(should_stop):raise ValidationError('invalid stop callback')
        with RunnerLock(self.directory/'scheduler.lock'):
            with self.transaction() as db:
                meta=db.execute('SELECT * FROM sandbox_meta').fetchone()
                if at<meta['last_tick']:raise ValidationError('scheduler time must not go backwards')
                if db.execute("SELECT 1 FROM sandbox_jobs WHERE status='running'").fetchone():
                    raise ValidationError('interrupted task requires exclusive recovery')
                self.now=at;db.execute('UPDATE sandbox_meta SET last_tick=?',(at,))
                if at<meta['quiet_until']:return {'steps':0,'quiet':True,'external_actions':0}
            count=0
            while count<max_steps:
                with self.transaction() as db:
                    if at<db.execute('SELECT quiet_until FROM sandbox_meta').fetchone()[0]:
                        return {'steps':count,'quiet':True,'external_actions':0}
                jobs=[j for j in self.status() if j['status']=='waiting' and not j['paused'] and j['due']<=at]
                if not jobs:break
                job=jobs[0]
                if should_stop is not None and should_stop():self.pause(job['id']);break
                if not self._step(job,faults or {}):break
                count+=1
            return {'steps':count,'quiet':False,'external_actions':0}

    def recover(self):
        with RunnerLock(self.directory/'scheduler.lock'):
            if (self.directory/'actions.sqlite3').exists():self._runtime().recover()
            with self.transaction() as db:
                return db.execute("UPDATE sandbox_jobs SET status='blocked',detail='interrupted_requires_review' WHERE status='running'").rowcount
