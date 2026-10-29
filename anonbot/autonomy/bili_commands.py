"""Explicit local CLI for read-only tasks, independent of QQ and model APIs."""
from pathlib import Path
from uuid import uuid4

from .actions import ActionIntent, ActionRuntime, Grant, default_registry
from .bili_executor import BiliReadExecutor
from .bilibili import BiliReadError, parse_bvid


def add_bili_parser(commands):
    bili = commands.add_parser('bili-read', help='B站搜索、详情和字幕；默认离线示例')
    operations = bili.add_subparsers(dest='operation', required=True)
    for name, help_text in (('search', '搜索视频'), ('info', '读取视频详情'),
                            ('subtitles', '尝试读取公开字幕')):
        command = operations.add_parser(name, help=help_text)
        if name == 'search':
            command.add_argument('query')
            command.add_argument('--page', type=int, default=1)
            command.add_argument('--limit', type=int, default=5)
        else:
            command.add_argument('video', help='BV号或 www.bilibili.com/video/ 的HTTPS链接')
        if name == 'subtitles':
            command.add_argument('--page', type=int, default=1)
            command.add_argument('--language', help='例如 zh-CN；默认平台返回的首个字幕轨')
        command.add_argument('--live', action='store_true', help='明确允许匿名公开GET请求，无账号写操作')
        command.add_argument('--db', type=Path, default=Path('data/bili-read-actions.sqlite3'))
        command.add_argument('--intent-id', help='重复使用同一ID和参数时返回原记录，不重试')


def bili_command(args, emit):
    try:
        if args.operation == 'search':
            payload = {'query': args.query, 'page': args.page, 'limit': args.limit}
            target = 'public-search'
        else:
            target = parse_bvid(args.video)
            payload = {'bvid': target}
            if args.operation == 'subtitles':
                payload.update(page=args.page, language=args.language)
        intent = ActionIntent(args.intent_id or 'bili-' + uuid4().hex, 'local-cli',
                              'bilibili:public:' + ('live' if args.live else 'fixture'),
                              'bilibili.' + args.operation, 'anonymous', target, payload)
        # The trusted local invocation authorizes only this exact read target.
        # No group messages or model output can create grants via this command.
        with ActionRuntime(args.db, default_registry(), [Grant.for_intent(intent)], []) as runtime:
            before = runtime.submit(intent)
            cached = before.status != 'ready'
            record = runtime.execute(intent.intent_id, BiliReadExecutor(live=args.live))
            emit({'type': 'bili_read', 'cached': cached, **record.to_dict()})
            if record.status not in ('succeeded', 'simulated'):
                return 2
            return 2 if record.data and record.data.get('status') == 'unavailable' else 0
    except BiliReadError as exc:
        emit({'type': 'error', 'reason': exc.code, 'platform_code': exc.platform_code})
        return 1
