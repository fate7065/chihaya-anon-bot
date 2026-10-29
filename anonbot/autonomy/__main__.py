"""Offline framework demo and bounded JSONL group-decision replay."""
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile

from ..domain import ProviderError, ValidationError
from .actions import ActionIntent, ActionRuntime, Grant, default_registry
from .executors import MockExecutor
from .group import GroupMessage, GroupSnapshot, RoomRef
from .judge_config import load_council
from .jsonio import fields, strict_json
from .replay import ReplayStore


def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


def parse_event(value):
    fields(value, ('room', 'message', 'now'), ('last_reply_at', 'enabled', 'quiet'))
    fields(value['room'], ('platform', 'account_id', 'room_id'), ('persona_id',))
    fields(value['message'], ('event_id', 'sender_id', 'text'), ('mentioned', 'reply_to_bot'))
    room = RoomRef(**value['room'])
    message = GroupMessage(**value['message'])
    options = {name: value[name] for name in ('last_reply_at', 'enabled', 'quiet') if name in value}
    # Validate before any room record is claimed or provider is called.
    GroupSnapshot(room, 1, (message,), value['now'], **options)
    return room, message, value['now'], options


def replay(args):
    council = load_council(args.config, allow_model_calls=args.allow_model_calls)
    emit({'type': 'start', 'mode': 'replay',
          'model_calls_allowed': args.allow_model_calls, 'max_events': args.max_events,
          'actions_enabled': False})
    count = 0
    incomplete = False
    limit_reached = False
    with open(args.input, encoding='utf-8-sig') as source, ReplayStore(args.db) as store:
        while True:
            line = source.readline(65538)
            if not line:
                break
            if len(line) > 65536:
                raise ValidationError('回放单行超过长度限制')
            if not line.strip():
                continue
            if count >= args.max_events:
                limit_reached = True
                break
            room, message, now, options = parse_event(strict_json(line))
            result = store.process(room, message, now, council, **options)
            emit({'type': 'decision', **result})
            count += 1
            if result.get('in_progress'):
                incomplete = True
                break
    emit({'type': 'summary', 'processed': count, 'limit_reached': limit_reached,
          'incomplete': incomplete, 'external_actions': 0})
    return 2 if incomplete else 0


def demo():
    """The approver below belongs only to the temporary mock demonstration."""
    emit({'type': 'start', 'mode': 'offline-demo', 'model_calls_allowed': False})
    council = load_council()
    room = RoomRef('qq', 'demo-bot', 'demo-group')
    first = GroupMessage('1', 'alice', '爱音，你觉得这首歌怎么样', mentioned=True)
    fixtures = [(room, first, 100.0, {}),
                (room, GroupMessage('2', 'bob', '小明，明天一起吃饭吧'), 110.0, {}),
                (RoomRef('qq', 'demo-bot', 'other-group'),
                 GroupMessage('3', 'alice', '嗯'), 120.0, {}),
                (room, GroupMessage('4', 'alice', '爱音？', mentioned=True),
                 131.0, {'last_reply_at': 130.0}),
                (room, first, 100.0, {})]
    with tempfile.TemporaryDirectory(prefix='anon-autonomy-') as directory:
        with ReplayStore(Path(directory) / 'replay.sqlite3') as store:
            for reference, message, now, options in fixtures:
                emit({'type': 'decision', **store.process(reference, message, now, council, **options)})
        grant = Grant('anon', room.key, 'qq.group.send', 'demo-bot', 'demo-group')
        with ActionRuntime(Path(directory) / 'actions.sqlite3', default_registry(),
                           [grant], ['local-demo-owner']) as runtime:
            intent = ActionIntent('demo-send', 'anon', room.key, 'qq.group.send',
                                  'demo-bot', 'demo-group', {'text': '这是一条模拟消息'})
            emit({'type': 'action', **runtime.submit(intent).to_dict()})
            runtime.approve(intent.intent_id, 'local-demo-owner')
            for _ in range(2):
                emit({'type': 'action', **runtime.execute(intent.intent_id, MockExecutor()).to_dict()})
            denied = ActionIntent('demo-denied', 'anon', room.key, 'bilibili.comment',
                                  'demo-bili', 'demo-video', {'text': '模拟评论'})
            emit({'type': 'action', **runtime.submit(denied).to_dict()})
    emit({'type': 'summary', 'external_calls': 0, 'external_actions': 0})
    return 0


def event_limit(value):
    count = int(value)
    if not 1 <= count <= 1000:
        raise argparse.ArgumentTypeError('max-events 必须在 1–1000 之间')
    return count


def call_limit(value):
    count = int(value)
    if not 1 <= count <= 3000:
        raise argparse.ArgumentTypeError('max-calls 必须在1–3000之间')
    return count


def benchmark_command(args):
    from .benchmark import load_benchmark
    dataset = load_benchmark(args.input)
    if args.command == 'validate-benchmark':
        emit({'type': 'benchmark_validation', 'dataset_sha256': dataset.sha256,
              'dataset_profile': dataset.profile})
        return 0
    from .evaluation import run_benchmark
    report = run_benchmark(dataset, args.output, strategies=tuple(args.strategies),
                           config=args.config, allow_model_calls=args.allow_model_calls,
                           max_calls=args.max_calls, use_current_deepseek=args.use_current_deepseek)
    emit({'type': 'benchmark_summary', 'run_id': report['run_id'],
          'complete': report['complete'], 'api_attempts': report['api_attempts'],
          'budget_blocked': report['budget_blocked'],
          'model_quality_accepted': report['model_quality_accepted'],
          'call_status_counts': report['call_status_counts']})
    return 0 if report['complete'] else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description='爱音下一阶段框架：默认离线，无账号操作')
    commands = parser.add_subparsers(dest='command', required=True)
    from .bili_commands import add_bili_parser
    add_bili_parser(commands)
    from .bili_comment_commands import add_bili_comment_parser
    add_bili_comment_parser(commands)
    from .bili_dm_commands import add_bili_dm_parser
    add_bili_dm_parser(commands)
    from .android_commands import add_android_parser
    add_android_parser(commands)
    from .android_control_commands import add_android_control_parser
    add_android_control_parser(commands)
    from .workflow_commands import add_workflow_parser
    add_workflow_parser(commands)
    from .console_commands import add_console_parser
    add_console_parser(commands)
    from .simulation_commands import add_simulation_parser
    add_simulation_parser(commands)
    from .sandbox_commands import add_sandbox_parser
    add_sandbox_parser(commands)
    commands.add_parser('demo', help='临时数据库中的离线群决策与模拟动作演示')
    command = commands.add_parser('replay', help='回放 JSONL，仅判定不发消息')
    command.add_argument('input', type=Path)
    command.add_argument('--db', type=Path, default=Path('data/autonomy-replay.sqlite3'))
    command.add_argument('--config', type=Path)
    command.add_argument('--allow-model-calls', action='store_true',
                         help='允许向配置的模型发送本文件的群消息上下文，可能产生费用')
    command.add_argument('--max-events', type=event_limit, default=100)
    validate = commands.add_parser('validate-benchmark', help='验证标注集、来源和数据分区')
    validate.add_argument('input', type=Path)
    evaluate = commands.add_parser('evaluate-benchmark', help='同集评测规则/单模型/三个判断器')
    evaluate.add_argument('input', type=Path)
    evaluate.add_argument('--output', type=Path, required=True, help='全新报告目录，拒绝覆盖')
    evaluate.add_argument('--strategies', nargs='+', choices=('rule', 'single', 'council'), default=['rule'])
    evaluate.add_argument('--config', type=Path)
    evaluate.add_argument('--use-current-deepseek', action='store_true',
                          help='显式复用本项目已保存的DeepSeek配置，不读取QQ历史')
    evaluate.add_argument('--allow-model-calls', action='store_true',
                          help='允许将评测上下文发给模型API，可能产生费用')
    evaluate.add_argument('--max-calls', type=call_limit, default=300)
    commands.add_parser('shadow-demo', help='临时数据库和签名本机HTTP的群聊旁路演示')
    observe = commands.add_parser('observe-groups', help='只观察与记录群判断，不发送消息')
    observe.add_argument('--db', type=Path, default=Path('data/group-shadow.sqlite3'))
    observe.add_argument('--groups', nargs='*', default=[], help='明确的观察群，默认空')
    observe.add_argument('--port', type=int, default=8766)
    observe.add_argument('--queue-limit', type=int, default=100)
    observe.add_argument('--calls-per-hour', type=int, default=60)
    observe.add_argument('--debounce', type=float, default=0.8)
    observe.add_argument('--use-current-deepseek', action='store_true')
    observe.add_argument('--allow-model-calls', action='store_true')
    status = commands.add_parser('shadow-status', help='查看本机观察计数，不显示消息正文')
    status.add_argument('--db', type=Path, default=Path('data/group-shadow.sqlite3'))
    export = commands.add_parser('shadow-export', help='导出标识替换后的待人工标注快照')
    export.add_argument('--db', type=Path, default=Path('data/group-shadow.sqlite3'))
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--limit', type=int, default=1000)
    args = parser.parse_args(argv)
    try:
        if args.command == 'bili-dm':
            from .bili_dm_commands import bili_dm_command
            return bili_dm_command(args, emit)
        if args.command == 'bili-comment':
            from .bili_comment_commands import bili_comment_command
            return bili_comment_command(args, emit)
        if args.command == 'android-control':
            from .android_control_commands import android_control_command
            return android_control_command(args, emit)
        if args.command == 'sandbox':
            from .sandbox_commands import sandbox_command
            return sandbox_command(args, emit)
        if args.command == 'simulate-group':
            from .simulation_commands import simulation_command
            return simulation_command(args, emit)
        if args.command == 'console':
            from .console_commands import console_command
            return console_command(args, emit)
        if args.command == 'workflow':
            from .workflow_commands import workflow_command
            return workflow_command(args, emit)
        if args.command == 'android-read':
            from .android_commands import android_command
            return android_command(args, emit)
        if args.command == 'bili-read':
            from .bili_commands import bili_command
            return bili_command(args, emit)
        if args.command == 'demo':
            return demo()
        if args.command == 'replay':
            return replay(args)
        if args.command in ('observe-groups', 'shadow-status', 'shadow-export', 'shadow-demo'):
            from .shadow_commands import shadow_command, shadow_demo
            return shadow_demo(emit) if args.command == 'shadow-demo' else shadow_command(args, emit)
        return benchmark_command(args)
    except (ValidationError, ProviderError, OSError, sqlite3.Error, UnicodeError, TypeError, ValueError):
        emit({'type': 'error', 'reason': '输入、配置或本地存储无效；详情未回显，请按文档检查'})
        return 1
    except KeyboardInterrupt:
        emit({'type': 'stopped', 'reason': '已停止；中断记录不自动重试'})
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
