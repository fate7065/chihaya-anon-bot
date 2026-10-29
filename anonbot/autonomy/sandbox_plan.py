"""Frozen, typed plans for simulated accounts only. No permission fields in plans."""
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from ..domain import ValidationError
from .actions import ActionIntent
from .jsonio import fields

BVID = 'BV1bwnF6eEe8'
PACKAGES = ('tv.danmaku.bili', 'com.tencent.mobileqq')
TOOLS = {
    'qq.group.send':('sim-qq','sim-room',('text',),()),
    'bilibili.search':('sim-bili','public-search',('query',),()),
    'bilibili.info':('sim-bili',BVID,(),()),
    'bilibili.subtitles':('sim-bili',BVID,(),()),
    'bilibili.comment':('sim-bili',BVID,('text',),()),
    'bilibili.dm':('sim-bili','sim-user',('text',),()),
    'bilibili.upload':('sim-bili','sim-channel',('title','asset'),()),
    'android.devices':('sim-android','connected-devices',(),()),
    'android.inspect':('sim-android','sim-device',(),()),
    'android.snapshot':('sim-android','sim-device',(),('expected_package',)),
    'android.tap':('sim-android','sim-device',('x','y','expected_package','expected_revision'),()),
    'android.type':('sim-android','sim-device',('text','expected_package','expected_revision'),()),
}


def name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',value):
        raise ValidationError('invalid sandbox identifier')
    return value


def validate_action(tool, account, target, args):
    if not isinstance(tool,str) or tool not in TOOLS:
        raise ValidationError('unsupported sandbox tool')
    expected_account, expected_target, required, optional = TOOLS[tool]
    if account != expected_account or target != expected_target:
        raise ValidationError('only fixed sandbox accounts and targets are allowed')
    fields(args,required,optional)
    for key,limit in (('text',4000),('title',100),('query',100)):
        if key in args:
            value=args[key]
            if not isinstance(value,str) or not value.strip() or len(value)>limit or '\x00' in value:
                raise ValidationError('invalid bounded sandbox text')
    if 'asset' in args and args['asset']!='demo-video':
        raise ValidationError('only built-in synthetic upload asset is allowed')
    if 'expected_package' in args and args['expected_package'] not in PACKAGES:
        raise ValidationError('unsupported sandbox package')
    if 'expected_revision' in args and (type(args['expected_revision']) is not int or not 1<=args['expected_revision']<=10000):
        raise ValidationError('invalid expected screen revision')
    for key,limit in (('x',719),('y',1279)):
        if key in args and (type(args[key]) is not int or not 0<=args[key]<=limit):
            raise ValidationError('tap outside simulated display')


@dataclass(frozen=True,init=False)
class SandboxPlan:
    _json:str

    def __init__(self,document):
        fields(document,('version','title','steps'))
        if type(document['version']) is not int or document['version']!=1:
            raise ValidationError('unsupported sandbox plan version')
        title=document['title']
        if not isinstance(title,str) or not title.strip() or len(title)>100:
            raise ValidationError('invalid plan title')
        steps=document['steps']
        if type(steps) is not list or not 1<=len(steps)<=20:
            raise ValidationError('sandbox plan needs 1–20 steps')
        prior={}
        for step in steps:
            fields(step,('id','tool','account','target','args'))
            identity=name(step['id']);target=step['target']
            if identity in prior:raise ValidationError('duplicate step id')
            if isinstance(target,dict):
                fields(target,('from','field'))
                if (not isinstance(target['from'],str) or prior.get(target['from']) not in ('bilibili.search','bilibili.info')
                        or target['field']!='bvid' or step['tool'] not in ('bilibili.info','bilibili.subtitles','bilibili.comment')):
                    raise ValidationError('only prior video results can bind a BV target')
                target=BVID
            validate_action(step['tool'],step['account'],target,step['args'])
            prior[identity]=step['tool']
        try:
            encoded=json.dumps(document,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
            if len(encoded.encode('utf-8'))>65536:raise ValidationError('sandbox plan too large')
        except (TypeError,ValueError,UnicodeError,RecursionError):
            raise ValidationError('invalid sandbox plan JSON') from None
        object.__setattr__(self,'_json',encoded)

    @property
    def document(self):return json.loads(self._json)

    @property
    def fingerprint(self):return sha256(self._json.encode('utf-8')).hexdigest()

    @property
    def has_writes(self):
        return any(s['tool'] in ('qq.group.send','bilibili.comment','bilibili.dm','bilibili.upload','android.tap','android.type')
                   for s in self.document['steps'])

    def intent(self,index,results,job_id,run):
        if type(index) is not int or not 0<=index<len(self.document['steps']):
            raise ValidationError('invalid step index')
        step=self.document['steps'][index];target=step['target']
        if isinstance(target,dict):
            try:target=results[target['from']]['bvid']
            except (KeyError,TypeError):raise ValidationError('video result binding unavailable') from None
        validate_action(step['tool'],step['account'],target,step['args'])
        digest=sha256(f'{job_id}:{run}:{index}:{self.fingerprint}'.encode()).hexdigest()
        return ActionIntent('agent-'+digest,'sandbox-agent','sandbox:'+job_id,
                            step['tool'],step['account'],target,step['args'])


def demo_plan():
    def step(identity,tool,target,args):
        return {'id':identity,'tool':tool,'account':TOOLS[tool][0],'target':target,'args':args}
    video={'from':'search','field':'bvid'}
    return {'version':1,'title':'完整跨平台模拟任务','steps':[
        step('search','bilibili.search','public-search',{'query':'千早爱音'}),
        step('info','bilibili.info',video,{}),step('subtitles','bilibili.subtitles',video,{}),
        step('comment','bilibili.comment',video,{'text':'本地模拟评论'}),
        step('dm','bilibili.dm','sim-user',{'text':'本地模拟私信'}),
        step('upload','bilibili.upload','sim-channel',{'title':'合成演示','asset':'demo-video'}),
        step('devices','android.devices','connected-devices',{}),
        step('inspect','android.inspect','sim-device',{}),
        step('before','android.snapshot','sim-device',{'expected_package':'tv.danmaku.bili'}),
        step('tap','android.tap','sim-device',{'x':120,'y':240,'expected_package':'tv.danmaku.bili','expected_revision':1}),
        step('type','android.type','sim-device',{'text':'千早爱音','expected_package':'tv.danmaku.bili','expected_revision':2}),
        step('after','android.snapshot','sim-device',{'expected_package':'tv.danmaku.bili'}),
        step('reply','qq.group.send','sim-room',{'text':'模拟任务已处理，可查看本地记录'})]}
