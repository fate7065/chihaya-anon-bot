"""Local observer commands and explicitly requested annotation exports."""
import json
from pathlib import Path

from ..domain import ValidationError
from .shadow_store import CONTEXT_AGE, ShadowStore


def export_benchmark(store, output, limit=1000):
    candidates = store.export_candidates(limit)
    if not candidates:
        raise ValidationError('暂无可导出的观察快照')
    rooms, users, events, episodes = {}, {}, {}, {}
    lines, size = [], 0
    for index, row in enumerate(candidates, 1):
        snapshot = json.loads(row['snapshot_json'])
        room_key = row['room_key']
        if room_key not in rooms:
            rooms[room_key] = (len(rooms) + 1, snapshot['now'])
        room_number, offset = rooms[room_key]
        original_bot = snapshot['room']['account_id']
        bot_alias = f'bot-{room_number}'
        previous_time, episode = episodes.get(room_key, (row['received'], 1))
        if row['received'] - previous_time > CONTEXT_AGE:
            episode += 1
        episodes[room_key] = (row['received'], episode)
        snapshot['room'] = {'platform': 'qq', 'account_id': bot_alias,
                            'room_id': f'room-{room_number}', 'persona_id': 'anon'}
        snapshot['now'] = snapshot['now'] - offset + 100000
        if snapshot['last_reply_at'] is not None:
            snapshot['last_reply_at'] = snapshot['last_reply_at'] - offset + 100000
        for message in snapshot['messages']:
            user_key = (room_key, message['sender_id'])
            if message['sender_id'] == original_bot:
                message['sender_id'] = bot_alias
            else:
                users.setdefault(user_key, f'user-{len(users) + 1}')
                message['sender_id'] = users[user_key]
            event_key = (room_key, message['event_id'])
            events.setdefault(event_key, f'event-{len(events) + 1}')
            message['event_id'] = events[event_key]
        case = {'case_id': f'shadow-{index:06d}',
                'conversation_id': f'room-{room_number}-episode-{episode}',
                'split': 'dev', 'origin': 'captured', 'review_status': 'unreviewed',
                'expected': 'uncertain', 'snapshot': snapshot}
        line = json.dumps(case, ensure_ascii=False, allow_nan=False) + '\n'
        size += len(line.encode('utf-8'))
        if size > 8 * 1024 * 1024:
            raise ValidationError('导出超过8MiB，请降低limit')
        lines.append(line)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as handle:
        handle.writelines(lines)
    return len(lines)


def shadow_command(args, emit):
    from .shadow_runtime import ShadowRuntime
    if args.command in ('shadow-status', 'shadow-export'):
        if not args.db.is_file():
            raise ValidationError('观察数据库不存在')
        store = ShadowStore(args.db)
        if args.command == 'shadow-status':
            import time
            emit({'type': 'shadow_status', 'send_enabled': False, **store.status(time.time())})
        else:
            count = export_benchmark(store, args.output, args.limit)
            emit({'type': 'shadow_export', 'exported_cases': count,
                  'origin': 'captured', 'review_status': 'unreviewed'})
        return 0
    if args.use_current_deepseek != args.allow_model_calls:
        raise ValidationError('模型观察须同时显式选择当前DeepSeek与API开关')
    if args.allow_model_calls and not args.groups:
        raise ValidationError('API观察需要明确的群范围')
    from ..settings import load_settings
    from ..server import serve
    settings, secrets = load_settings(with_secrets=True)
    secret = secrets.get('webhook_secret', '')
    if not isinstance(secret, str) or len(secret) < 16:
        raise ValidationError('请先配置本项目上报密钥')
    council = None
    if args.use_current_deepseek:
        from .evaluation import DeepSeekJudgeProvider
        from .group import DecisionCouncil, ModelJudge
        key = secrets.get('deepseek_api_key', '')
        if not key:
            raise ValidationError('尚未配置DeepSeek密钥')
        council = DecisionCouncil([ModelJudge(d,
            DeepSeekJudgeProvider(key, settings.model, settings.base_url), 'current-deepseek:' + d)
            for d in ('address', 'relevance', 'timing')])
    runtime = ShadowRuntime(args.db, settings.bot_id, args.groups, council=council,
        port=args.port, queue_limit=args.queue_limit, calls_per_hour=args.calls_per_hour,
        debounce_seconds=args.debounce)
    emit({'type': 'shadow_start', 'allowed_group_count': len(runtime.groups),
          'model_enabled': runtime.model_enabled, 'send_enabled': False})
    serve(runtime, secret)
    return 0


def shadow_demo(emit):
    """Exercise signed local HTTP without reading the project's settings."""
    import hashlib
    import hmac
    import http.client
    import secrets
    import tempfile
    import threading
    from ..server import make_server
    from .shadow_runtime import ShadowRuntime
    with tempfile.TemporaryDirectory(prefix='anon-group-shadow-') as directory:
        root = Path(directory)
        runtime = ShadowRuntime(root / 'shadow.sqlite3', '10000', ('30000',), debounce_seconds=0)
        secret = secrets.token_urlsafe(24)
        server = make_server(runtime, secret, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = {'post_type': 'message', 'message_type': 'group', 'self_id': 10000,
                'group_id': 30000, 'user_id': 20000, 'message_id': 1, 'time': 100,
                'message': [{'type': 'at', 'data': {'qq': '10000'}},
                            {'type': 'text', 'data': {'text': '爱音，在吗'}}]}
        def post(payload):
            raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            signature = 'sha1=' + hmac.new(secret.encode(), raw, hashlib.sha1).hexdigest()
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request('POST', '/events', raw,
                    {'Content-Type': 'application/json', 'X-Signature': signature})
                response = connection.getresponse()
                if response.status != 200:
                    raise ValidationError('本机观察演示上报失败')
                return json.loads(response.read())['status']
            finally:
                connection.close()
        try:
            admitted = [post(base), post({**base, 'message_id': 2, 'user_id': 20001})]
            runtime.process_one()
            quoted = {**base, 'message_id': 3, 'message': [
                {'type': 'reply', 'data': {'id': '999'}}, {'type': 'text', 'data': {'text': '然后呢'}}]}
            admitted.append(post(quoted))
            runtime.process_one()
            admitted.append(post({**base, 'message_id': 4,
                                  'message': [{'type': 'image', 'data': {}}]}))
            runtime.process_one()
            admitted.append(post(base))
            admitted.append(post({**base, 'group_id': 40000}))
            count = export_benchmark(runtime.store, root / 'annotation.jsonl')
            emit({'type': 'shadow_demo', 'ingress_results': admitted,
                  'annotation_candidates': count, 'external_actions': 0, **runtime.status()})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            runtime.close()
    return 0
