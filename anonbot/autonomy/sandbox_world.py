"""Persistent synthetic platforms. This module has no network, ADB or shell client."""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from ..domain import EventConflict, ValidationError
from .actions import ExecutionResult
from .android_png import fixture_png
from .sandbox_plan import BVID, validate_action


class SandboxWorld:
    def __init__(self,directory):
        self.directory=Path(directory).resolve();self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/'world.sqlite3'
        with self.transaction() as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables!={'world_state','world_receipts'}:
                raise ValidationError('use a dedicated sandbox world directory')
            db.execute('CREATE TABLE IF NOT EXISTS world_state(id INTEGER PRIMARY KEY,content TEXT)')
            db.execute('''CREATE TABLE IF NOT EXISTS world_receipts(
                id TEXT PRIMARY KEY,fingerprint TEXT,status TEXT,content TEXT)''')
            initial={'comments':[],'dms':[],'uploads':[],'qq':[],
                     'device':{'package':'tv.danmaku.bili','revision':1,'input':'','tap':None}}
            db.execute('INSERT OR IGNORE INTO world_state VALUES(1,?)',(json.dumps(initial),))

    @contextmanager
    def transaction(self):
        db=sqlite3.connect(self.path,timeout=5,isolation_level=None);db.row_factory=sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE');yield db;db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:db.execute('ROLLBACK')
            raise
        finally:db.close()

    def state(self):
        with self.transaction() as db:return json.loads(db.execute('SELECT content FROM world_state WHERE id=1').fetchone()[0])

    def _image(self,intent,state):
        digest=sha256(json.dumps(state,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        name='artifacts/'+sha256((intent.fingerprint+digest).encode()).hexdigest()+'.png'
        path=self.directory/name;path.parent.mkdir(exist_ok=True);raw=fixture_png()
        if path.exists():
            if path.read_bytes()!=raw:raise ValidationError('synthetic artifact changed')
        else:
            with path.open('xb') as target:target.write(raw)
        return {'image':name,'sha256':sha256(raw).hexdigest(),'width':64,'height':96,
                'state_sha256':digest,'synthetic_image':True,'revision':state['device']['revision']}

    def _apply(self,intent,state):
        tool,args=intent.capability,intent.payload
        if tool=='bilibili.search':return {'bvid':BVID,'items':[{'bvid':BVID,'title':'合成视频，非实时资料'}]}
        if tool=='bilibili.info':return {'bvid':BVID,'title':'合成爱音演示','description':'仅用于本地任务测试'}
        if tool=='bilibili.subtitles':return {'bvid':BVID,'segments':[{'start':0,'end':1,'text':'合成字幕，不是角色原作台词'}]}
        lists={'bilibili.comment':'comments','bilibili.dm':'dms','bilibili.upload':'uploads','qq.group.send':'qq'}
        if tool in lists:
            receipt={'receipt_id':'sim-'+sha256(intent.intent_id.encode()).hexdigest()[:24],
                     'target':intent.target,**args}
            state[lists[tool]].append(receipt)
            return {'receipt':receipt,'verified':state[lists[tool]][-1]==receipt}
        device=state['device']
        if tool=='android.devices':return {'devices':[{'serial':'sim-device','state':'synthetic'}]}
        if tool=='android.inspect':return {'foreground':device['package'],'revision':device['revision'],'width':720,'height':1280}
        if args.get('expected_package',device['package'])!=device['package']:
            raise ValidationError('foreground package changed')
        if tool=='android.snapshot':return self._image(intent,state)
        if args['expected_revision']!=device['revision']:raise ValidationError('screen revision changed')
        if tool=='android.tap':device['tap']=[args['x'],args['y']]
        else:device['input']=args['text']
        device['revision']+=1
        return {'verified':True,'foreground':device['package'],'revision':device['revision'],
                'input':device['input'],'tap':device['tap']}

    def execute(self,intent,*,fault=None):
        if fault not in (None,'before','after'):raise ValidationError('invalid test fault')
        try:
            validate_action(intent.capability,intent.account_id,intent.target,intent.payload)
            if intent.actor_id!='sandbox-agent' or not intent.scope_id.startswith('sandbox:'):
                raise ValidationError('invalid simulation actor')
        except ValidationError:
            return ExecutionResult('failed','sandbox capability or scope rejected',{'simulation':True})
        with self.transaction() as db:
            old=db.execute('SELECT * FROM world_receipts WHERE id=?',(intent.intent_id,)).fetchone()
            if old:
                if old['fingerprint']!=intent.fingerprint:raise EventConflict('sandbox action id conflict')
                return ExecutionResult(old['status'],'cached synthetic receipt',json.loads(old['content']))
            state=json.loads(db.execute('SELECT content FROM world_state WHERE id=1').fetchone()[0])
            try:
                if fault=='before':raise ValidationError('injected failure before effect')
                data={**self._apply(intent,state),'simulation':True,'source':'stateful_sandbox'}
                status='simulated'
                if fault=='after':
                    status='unknown';data={'simulation':True,'effect_may_have_occurred':True,'verified':False}
            except (ValidationError,OSError):
                status,data='failed',{'simulation':True,'verified':False,'reason':'precondition_or_artifact_failed'}
            if status in ('simulated','unknown'):
                db.execute('UPDATE world_state SET content=? WHERE id=1',(json.dumps(state,ensure_ascii=False),))
            encoded=json.dumps(data,ensure_ascii=False)
            db.execute('INSERT INTO world_receipts VALUES(?,?,?,?)',(intent.intent_id,intent.fingerprint,status,encoded))
            row=db.execute('SELECT status,content FROM world_receipts WHERE id=?',(intent.intent_id,)).fetchone()
            return ExecutionResult(row['status'],'local synthetic state and receipt recorded',json.loads(row['content']))
