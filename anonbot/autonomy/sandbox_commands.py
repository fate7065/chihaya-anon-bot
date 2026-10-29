"""Local synthetic-account workflows, explicit plan approval and finite workers."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import time

from ..domain import ValidationError
from ..persona import system_prompt,response_style
from .autonomous_sandbox import AutonomousSandbox
from .evaluation import CallTracker
from .group import GroupMessage,GroupSnapshot,RoomRef
from .jsonio import read_json
from .sandbox_plan import SandboxPlan,demo_plan
from .sandbox_planner import generate_plan,models
from .task_export import _text


def _write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def _flags(use,allow):
    if type(use) is not bool or type(allow) is not bool or use!=allow:
        raise ValidationError('current DeepSeek and allow-model-calls must be explicitly paired')


def _report(directory,manager):
    jobs=manager.status();world=manager.world.state()
    _write(directory/'jobs.json',jobs);_write(directory/'world.json',world)
    lines=['# 自主任务模拟报告','','全部结果属于本地合成世界，真实平台动作0次。','']
    for job in jobs:
        lines+=['## '+_text(job['plan']['title']),'',
                '状态：'+_text(job['status'])+'；完成周期：'+str(job['run'])+'；进度：'+str(job['next_step']),'',
                '计划与日程审批哈希：'+job['fingerprint'],'']
        for step in job['plan']['steps']:
            lines+=['- '+_text(step['id'])+'：'+_text(step['tool'])+' → '+_text(str(step['target'])),'']
    (directory/'report.md').write_text('\n'.join(lines),encoding='utf-8')


def group_demo(directory,*,approve_simulation=False,use_current_deepseek=False,allow_model_calls=False,max_calls=8):
    _flags(use_current_deepseek,allow_model_calls)
    if type(approve_simulation) is not bool:raise ValidationError('invalid simulation approval flag')
    directory=Path(directory)
    if directory.exists():raise ValidationError('group demo output must be a new directory')
    tracker=CallTracker(max_calls);tracker.strategy='group-task';tracker.case_id='synthetic-group-task'
    council,planner,reply_provider=models(use_current_deepseek,tracker)
    directory.mkdir(parents=True,exist_ok=False)
    request='爱音，在模拟环境搜索千早爱音视频并读取详情，在模拟视频下评论“练习加油”，最后在模拟群报告处理结果'
    snapshot=GroupSnapshot(RoomRef('simulation','sim-bot','sim-room'),1,
                           (GroupMessage('request-1','sim-user',request,mentioned=True),),0)
    manifest={'mode':'api-simulation' if use_current_deepseek else 'offline-simulation',
              'origin':'synthetic','models':tracker.models,'max_calls':max_calls,'external_actions':0,
              'source_sha256':{n:sha256(Path(__file__).with_name(n).read_bytes()).hexdigest()
                  for n in ('sandbox_commands.py','sandbox_planner.py','sandbox_plan.py','sandbox_world.py','autonomous_sandbox.py')}}
    _write(directory/'manifest.json',manifest);_write(directory/'group-input.json',asdict(snapshot))
    result={'complete':False,'external_actions':0,'effects':0,'task_status':'not_created'}
    manager=None
    try:
        decision=council.evaluate(snapshot);result['group_action']=decision.action
        _write(directory/'group-decision.json',asdict(decision))
        if decision.action=='reply':
            if planner is not None:plan=generate_plan(request,planner)
            else:
                preset=demo_plan();preset['steps']=[s for s in preset['steps'] if s['id'] in ('search','info','comment','reply')]
                for step in preset['steps']:
                    if step['id']=='comment':step['args']['text']='练习加油'
                plan=SandboxPlan(preset)
            allowed={'bilibili.search','bilibili.info','bilibili.comment','qq.group.send'}
            if any(s['tool'] not in allowed or (s['tool']=='bilibili.comment' and s['args']['text']!='练习加油')
                   for s in plan.document['steps']):
                raise ValidationError('group proposal exceeds this synthetic request scope')
            if [s['tool'] for s in plan.document['steps']]!=[
                    'bilibili.search','bilibili.info','bilibili.comment','qq.group.send']:
                raise ValidationError('group proposal is incomplete or repeats requested effects')
            _write(directory/'plan.json',plan.document)
            manager=AutonomousSandbox(directory/'tasks');job=manager.submit('group-request',plan)
            if approve_simulation:manager.approve(job['id'],job['fingerprint'])
            manager.tick(0);job=manager.status('group-request');result['task_status']=job['status']
            state=manager.world.state()
            result['effects']=sum(len(state[k]) for k in ('comments','dms','uploads','qq'))+state['device']['revision']-1
            _report(directory,manager)
            if job['status']=='completed':
                messages=[{'role':'system','content':system_prompt()+'\n'+response_style()+
                           '\n只依据以下本地模拟结果回复，不声称真实账号操作。异常和unknown不得说成功。'},
                          {'role':'user','content':json.dumps({'request':request,'simulation':True,
                           'status':job['status'],'results':job['results']},ensure_ascii=False)}]
                reply=reply_provider.generate(messages)
                if not isinstance(reply,str) or not reply.strip() or len(reply)>2000:
                    raise ValidationError('invalid result reply')
                (directory/'result-reply.txt').write_text('[模拟任务] '+reply,encoding='utf-8')
            else:(directory/'result-reply.txt').write_text('[模拟任务] 尚未完成，当前状态：'+job['status'],encoding='utf-8')
        result['complete']=result['task_status']=='completed'
    finally:
        result.update(api_attempts=tracker.attempts,calls=len(tracker.calls),actual_cost=None,model_quality_accepted=False)
        _write(directory/'calls.json',tracker.calls);_write(directory/'summary.json',result)
    return result


def demo(directory):
    directory=Path(directory)
    if directory.exists():raise ValidationError('demo output must be new')
    directory.mkdir(parents=True,exist_ok=False)
    manager=AutonomousSandbox(directory/'tasks');job=manager.submit('all-tools',SandboxPlan(demo_plan()))
    manager.approve('all-tools',job['fingerprint']);manager.tick(0)
    _report(directory,manager)
    return {'complete':manager.status('all-tools')['status']=='completed','external_actions':0,'mode':'offline-simulation'}


def add_sandbox_parser(commands):
    parser=commands.add_parser('sandbox',help='模拟账号自主任务：规划、审批、调度和状态核对')
    subs=parser.add_subparsers(dest='sandbox_command',required=True)
    for command in ('demo','group-demo','submit','approve','pause','resume','status','tick','recover','quiet','work'):
        sub=subs.add_parser(command);sub.add_argument('--directory',type=Path,required=True)
        if command in ('submit','approve','pause','resume'):sub.add_argument('--job',required=True)
        if command=='status':sub.add_argument('--job')
        if command=='submit':
            sub.add_argument('--plan',type=Path,required=True);sub.add_argument('--at',type=float,default=0)
            sub.add_argument('--interval',type=float,default=0);sub.add_argument('--runs',type=int,default=1)
        if command=='approve':sub.add_argument('--fingerprint',required=True)
        if command in ('tick','quiet'):sub.add_argument('--at',type=float,required=True)
        if command=='tick':sub.add_argument('--max-steps',type=int,default=20)
        if command=='work':
            sub.add_argument('--ticks',type=int,default=1);sub.add_argument('--poll',type=float,default=1)
        if command=='group-demo':
            sub.add_argument('--approve-simulation',action='store_true')
            sub.add_argument('--use-current-deepseek',action='store_true');sub.add_argument('--allow-model-calls',action='store_true')
            sub.add_argument('--max-calls',type=int,default=8)
    plan=subs.add_parser('plan');plan.add_argument('--request',required=True);plan.add_argument('--output',type=Path,required=True)
    plan.add_argument('--use-current-deepseek',action='store_true');plan.add_argument('--allow-model-calls',action='store_true')


def sandbox_command(args,emit):
    command=args.sandbox_command
    if command=='plan':
        _flags(args.use_current_deepseek,args.allow_model_calls)
        if not args.use_current_deepseek:raise ValidationError('model task planning requires explicit API opt-in')
        calls_path=args.output.with_suffix('.calls.json')
        if args.output.exists() or calls_path.exists():raise ValidationError('plan outputs already exist')
        tracker=CallTracker(1);tracker.strategy='planning';tracker.case_id='local-request'
        _,planner,_=models(True,tracker)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        try:
            plan=generate_plan(args.request,planner)
            with args.output.open('x',encoding='utf-8') as target:json.dump(plan.document,target,ensure_ascii=False,indent=2)
        finally:
            with calls_path.open('x',encoding='utf-8') as target:json.dump(tracker.calls,target,ensure_ascii=False,indent=2)
        emit({'type':'sandbox_plan','fingerprint':plan.fingerprint,'approval_granted':False});return 0
    if command=='demo':emit({'type':'sandbox_demo',**demo(args.directory)});return 0
    if command=='group-demo':
        result=group_demo(args.directory,approve_simulation=args.approve_simulation,
                          use_current_deepseek=args.use_current_deepseek,allow_model_calls=args.allow_model_calls,max_calls=args.max_calls)
        emit({'type':'sandbox_group_demo',**result});return 0 if result['complete'] else 2
    plan=SandboxPlan(read_json(args.plan)) if command=='submit' else None
    if command=='work':
        from .group import _number
        if type(args.ticks) is not int or not 1<=args.ticks<=100:raise ValidationError('worker ticks must be 1–100')
        _number(args.poll,'poll interval',0.1,60)
    manager=AutonomousSandbox(args.directory)
    if command=='submit':result={'job':manager.submit(args.job,plan,due=args.at,interval=args.interval,max_runs=args.runs)}
    elif command=='approve':result={'job':manager.approve(args.job,args.fingerprint)}
    elif command=='pause':result={'job':manager.pause(args.job)}
    elif command=='resume':result={'job':manager.resume(args.job)}
    elif command=='status':result={'job':manager.status(args.job)}
    elif command=='recover':result={'recovered':manager.recover()}
    elif command=='quiet':manager.set_quiet_until(args.at);result={'quiet_until':args.at}
    elif command=='work':
        result={}
        for index in range(args.ticks):
            result=manager.tick(time.time())
            if index+1<args.ticks:time.sleep(args.poll)
    else:result=manager.tick(args.at,max_steps=args.max_steps)
    emit({'type':'sandbox_'+command,**result});return 0
