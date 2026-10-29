"""Manual workflow CLI, never a scheduler or a channel for model-created permissions."""
from pathlib import Path

from ..domain import ValidationError
from .jsonio import read_json
from .workflow import Workflow
from .workflow_lock import WorkflowBusy
from .workflow_plan import WorkflowPlan
from .workflow_tools import WorkflowTools


def demo_plan():
    return WorkflowPlan({'version': 1, 'workflow_id': 'offline-research', 'steps': [
        {'id': 'find', 'tool': 'bilibili.search', 'args': {'query': '千早爱音', 'limit': 3}},
        {'id': 'detail', 'tool': 'bilibili.info', 'args': {},
         'bindings': {'bvid': {'step': 'find', 'path': ['items', 0, 'bvid']}}},
        {'id': 'devices', 'tool': 'android.devices', 'args': {}},
        {'id': 'shot', 'tool': 'android.snapshot',
         'args': {'serial': 'demo-emulator', 'expected_package': 'com.example.demo'}},
    ]})


def add_workflow_parser(commands):
    parent = commands.add_parser('workflow', help='手动只读多步骤任务、状态、停止与恢复')
    operations = parent.add_subparsers(dest='operation', required=True)
    for name in ('demo', 'run', 'resume', 'status', 'stop'):
        command = operations.add_parser(name)
        command.add_argument('--directory', type=Path, required=name != 'demo', default=Path('data/workflow-demo'))
        if name == 'run':
            command.add_argument('plan', type=Path)
        if name in ('run', 'resume'):
            command.add_argument('--live', action='store_true', help='允许计划内的真实读取；需与已保存模式一致')
            command.add_argument('--adb', type=Path, help='真实安卓读取的可信ADB绝对路径')
        if name in ('demo', 'run', 'resume'):
            command.add_argument('--max-steps', type=int, default=10, help='本次最多执行1–10步')
        if name == 'status':
            command.add_argument('--include-results', action='store_true', help='输出保存的资料与本地图片路径')


def workflow_command(args, emit):
    if args.operation in ('resume', 'status', 'stop') and not (args.directory / 'workflow.sqlite3').is_file():
        raise ValidationError('workflow directory does not exist')
    workflow = Workflow(args.directory)
    if args.operation in ('run', 'demo'):
        plan = demo_plan() if args.operation == 'demo' else WorkflowPlan(read_json(args.plan))
        workflow.submit(plan, live=getattr(args, 'live', False))
    else:
        plan = workflow.plan
    if args.operation == 'stop':
        report = workflow.stop()
    elif args.operation == 'status':
        report = workflow.status(include_results=args.include_results)
    else:
        live = getattr(args, 'live', False)
        if (workflow.status()['mode'] == 'live') != live:
            raise ValidationError('explicit execution mode differs from stored workflow')
        tools = WorkflowTools(workflow.directory, plan, live=live, adb=getattr(args, 'adb', None))
        try:
            report = workflow.run(tools, max_steps=args.max_steps)
        except WorkflowBusy:
            emit({'type': 'workflow', 'status': 'busy', 'reason': 'runner lock is held or unavailable'})
            return 2
    emit({'type': 'workflow', **report})
    return 2 if report['status'] in ('blocked', 'paused', 'running') else 0
