import argparse
from dataclasses import asdict
import json
from pathlib import Path
import re
import sqlite3
import sys
from uuid import uuid4
from .adapters import parse_onebot
from .domain import MessageEvent, Scope, ValidationError
from .engine import Engine
from .memory import Store
from .policy import ReplyPolicy
from .providers import MockProvider, provider_from_env
from .reply_editor import AuditProfile, AuditedProvider, ReviewedProvider, make_review_provider

ROOT = Path(__file__).resolve().parent.parent
HELP = '/remember 内容 | /recall [关键词] | /forget ID | /quiet on|off | /help | /exit'


def emit(**data):
    print(json.dumps(data, ensure_ascii=False))


def parser():
    root = argparse.ArgumentParser(description='千早爱音机器人：DeepSeek 对话与 QQ 接入')
    commands = root.add_subparsers(dest='command', required=True)
    from .voice_commands import register as register_voice
    register_voice(commands)
    commands.add_parser('doctor', help='只读环境检查，不调用模型')
    commands.add_parser('configure', help='打开本地 DeepSeek / QQ 配置窗口，不联网')
    commands.add_parser('check-model', help='显式发起一次 DeepSeek 连通检查，不使用聊天历史')
    commands.add_parser('check-qq', help='只读取 NapCat 登录号并核对身份，不发消息')
    commands.add_parser('export-napcat', help='将本地 QQ 连接配置写入新建的 NapCat 目录，保留既有配置')
    for name in ('serve', 'status'):
        cmd = commands.add_parser(name, help='启动 QQ 私聊服务' if name == 'serve' else '查看 QQ 服务状态计数')
        cmd.add_argument('--db', type=Path)
        if name == 'serve':
            cmd.add_argument('--live-model', action='store_true')
            cmd.add_argument('--natural-dialogue',action='store_true',help='启用角色自然对话试用，DeepSeek Pro充分核对，每轮一次请求')
            cmd.add_argument('--review-replies', action='store_true', help='每轮草稿后编辑一次，最多两次模型请求')
            cmd.add_argument('--reviewer-profile',choices=('same','pro','audit'),default='same')
            cmd.add_argument('--send', action='store_true', help='启用 QQ 实际发送，须同时指定 --live-model')
            voice_group=cmd.add_mutually_exclusive_group()
            voice_group.add_argument('--voice-profile', type=Path, help='显式改为语音回复，使用本地声线配置')
            voice_group.add_argument('--voice-options', type=Path, help='启用可切换的中文和日语语音配置')
    resolve = commands.add_parser('resolve', help='服务停止后，人工核对一个未决发送，绝不自动重发')
    resolve.add_argument('job_id', type=int)
    resolve.add_argument('--db', type=Path)
    outcome = resolve.add_mutually_exclusive_group(required=True)
    outcome.add_argument('--sent-id', help='人工确认已发送的 message_id')
    outcome.add_argument('--not-sent', action='store_true', help='人工确认未发送，将本意图取消')
    for name in ('demo', 'chat', 'replay'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--db', type=Path, help='显式指定 SQLite 路径，默认位于 framework/data')
        if name != 'demo':
            cmd.add_argument('--bot-id', default='10000')
            cmd.add_argument('--quiet', action='store_true')
            cmd.add_argument('--group-all', action='store_true', help='允许未 @ 的群消息，默认关闭')
        if name == 'chat':
            cmd.add_argument('--user-id', default='20000')
            cmd.add_argument('--group-id')
            cmd.add_argument('--mention', action='store_true')
            cmd.add_argument('--live-model', action='store_true', help='将当前场景文本和记忆发往环境变量指定的模型端点')
            cmd.add_argument('--natural-dialogue',action='store_true',help='启用角色自然对话试用，DeepSeek Pro充分核对，每轮一次请求')
            cmd.add_argument('--review-replies', action='store_true', help='每轮草稿后编辑一次，最多两次模型请求')
            cmd.add_argument('--reviewer-profile',choices=('same','pro','audit'),default='same')
            cmd.add_argument('--message', help='单轮输入；省略则进入交互')
            cmd.add_argument('--event-id', help='单轮稳定事件 ID，供幂等检查')
            cmd.add_argument('--retry-failed', action='store_true', help='显式重试已失败的单轮事件，可能重复计费')
        if name == 'replay':
            cmd.add_argument('file', type=Path)
    return root


def control(text, store, scope, policy):
    if not text.startswith('/'):
        return False
    command, _, value = text.partition(' ')
    value = value.strip()
    if command == '/remember':
        item = store.remember(scope, value)
        emit(status='remembered', memory_id=item.memory_id, scope=asdict(scope))
    elif command == '/recall':
        emit(status='memories', items=[asdict(item) for item in store.recall(scope, value)])
    elif command == '/forget':
        if not re.fullmatch(r'[1-9][0-9]{0,18}', value) or int(value) > 9223372036854775807:
            raise ValidationError('/forget 后须为有效正整数记忆 ID')
        removed = store.forget(scope, int(value))
        emit(status='forgotten' if removed else 'not_found', message='已删除显式记忆' if removed else '当前作用域未找到该记忆')
    elif command == '/quiet' and value in ('on', 'off'):
        policy.quiet = value == 'on'
        emit(status='quiet', enabled=policy.quiet)
    elif command == '/help':
        print(HELP)
    else:
        raise ValidationError('未知命令或参数；' + HELP)
    return True


def conversation_provider(live_model, review_replies=False, reviewer_profile='same', *, natural_dialogue=False):
    if natural_dialogue and (not live_model or review_replies or reviewer_profile!='same'):
        raise ValidationError('--natural-dialogue 需要真实模型，且不能叠加回复编辑')
    if reviewer_profile not in ('same','pro','audit'):
        raise ValidationError('核对配置无效')
    if reviewer_profile != 'same' and not review_replies:
        raise ValidationError('--reviewer-profile 必须同时指定 --review-replies')
    if review_replies and not live_model:
        raise ValidationError('--review-replies 必须同时指定 --live-model')
    provider = provider_from_env(live_model)
    if natural_dialogue:
        from .natural_dialogue import NaturalDialogueProvider
        return NaturalDialogueProvider(provider)
    if not review_replies:
        return provider
    if reviewer_profile=='audit':
        return AuditedProvider(provider,AuditProfile(provider,thinking=True))
    if reviewer_profile=='pro':
        return ReviewedProvider(provider,make_review_provider(provider))
    return ReviewedProvider(provider)


def chat(args, store):
    scope = Scope(args.bot_id, 'anon', args.user_id, 'group:' + args.group_id if args.group_id else 'private')
    if args.event_id and args.message is None:
        raise ValidationError('--event-id 只适用于 --message 单轮输入')
    if args.retry_failed and (not args.event_id or args.message is None):
        raise ValidationError('--retry-failed 需要 --event-id 和 --message')
    policy = ReplyPolicy(args.quiet, args.group_all)
    from .natural_dialogue import load_natural_persona
    persona = load_natural_persona() if args.natural_dialogue else None
    engine = Engine(store, conversation_provider(args.live_model, args.review_replies, args.reviewer_profile, natural_dialogue=args.natural_dialogue), policy, persona=persona)
    if args.review_replies:
        print('回复编辑已启用：每轮最多两次模型请求；只保存最终回复，失败不发送草稿。')
    print('模式：' + ('live-model（仅 Console；当前场景文本、显式记忆及历史将传给配置的模型端点）' if args.live_model
                     else '离线演示／固定模板；仅 Console') + '\n' + HELP)
    while True:
        try:
            text = args.message if args.message is not None else input('你> ')
            if text.strip() == '/exit':
                return 0
            if not text.strip():
                if args.message is not None:
                    raise ValidationError('消息不能为空')
                continue
            if not control(text, store, scope, policy):
                event = MessageEvent(args.event_id or uuid4().hex, scope, text, args.mention)
                result = engine.handle(event, args.retry_failed)
                emit(mode='console-only', event_id=event.event_id, **asdict(result))
                if args.message is not None:
                    return 2 if result.status in ('failed', 'pending') else 0
            if args.message is not None:
                return 0
        except ValidationError as exc:
            emit(status='invalid_input', reason=str(exc))
            if args.message is not None:
                return 2
        except EOFError:
            return 0


def replay(args, store):
    engine = Engine(store, MockProvider(), ReplyPolicy(args.quiet, args.group_all))
    errors = 0
    with args.file.open(encoding='utf-8-sig') as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                if len(line) > 65536:
                    raise ValidationError('单行事件超过 64K 字符')
                event = parse_onebot(json.loads(line), args.bot_id)
                if event is None:
                    emit(mode='dry-run', line=line_no, status='ignored', reason='non_message')
                    continue
                result = engine.handle(event)
                emit(mode='dry-run', line=line_no, event_id=event.event_id, **asdict(result))
                errors += result.status in ('failed', 'pending')
            except (ValueError, RecursionError) as exc:
                reason = str(exc) if isinstance(exc, ValidationError) else 'JSON 解析失败或超出解析限制'
                emit(mode='dry-run', line=line_no, status='invalid_event', reason=reason)
                errors += 1
    return 2 if errors else 0


def main(argv=None):
    args = parser().parse_args(argv)
    if sys.version_info < (3, 11):
        emit(status='error', reason='需要 Python 3.11 或更新版本')
        return 2
    if args.command == 'voice':
        from .voice_commands import run
        return run(args)
    if args.command in ('configure', 'check-model'):
        from .domain import ProviderError
        try:
            if args.command == 'configure':
                from .configure import launch
                launch()
                return 0
            provider_from_env(True).generate([{'role': 'user', 'content': '这是接口连通测试。请仅回复：连接成功'}])
            emit(status='ok', message='模型接口返回有效正文；未发送聊天历史、记忆或 QQ 消息')
            return 0
        except (ValidationError, ProviderError) as exc:
            emit(status='error', reason=str(exc))
            return 2
        except (OSError, RuntimeError, ImportError):
            emit(status='error', reason='配置窗口或网络不可用，请检查 Python Tk 支持和本地配置')
            return 2
    if args.command in ('serve', 'status', 'check-qq', 'resolve', 'export-napcat'):
        from .settings import load_settings
        from .gateway import Runtime, OneBotClient, DeliveryError
        from .domain import ProviderError
        try:
            settings, secrets = load_settings(with_secrets=args.command not in ('status', 'resolve'))
            if args.command == 'export-napcat':
                from .setup_napcat import prepare, export_config
                path = export_config(settings, secrets, prepare())
                emit(status='ok', config=str(path), message='已写入本机 QQ 接入配置；DeepSeek API 密钥未导出')
                return 0
            if args.command == 'check-qq':
                OneBotClient(settings.gateway_url, secrets.get('gateway_token', ''), settings.bot_id).verify_identity()
                emit(status='ok', message='NapCat 已登录且机器人 QQ 身份一致；未发送消息')
                return 0
            if args.command == 'status':
                runtime = Runtime(args.db or ROOT / 'data' / 'qq.sqlite3', settings, MockProvider())
                emit(status='ok', **runtime.status(), jobs=runtime.jobs())
                return 0
            if args.command == 'resolve':
                from .server import InstanceLock
                path = args.db or ROOT / 'data' / 'qq.sqlite3'
                with InstanceLock(path):
                    Runtime(path, settings, MockProvider()).resolve(args.job_id, args.sent_id)
                emit(status='resolved', message='已按本地人工核对结果更新；未自动重发，原先 blocked 消息仍不重放')
                return 0
            if args.send and not args.live_model:
                raise ValidationError('--send 必须同时指定 --live-model，避免发送演示模板')
            if (args.voice_profile or args.voice_options) and not args.send:
                raise ValidationError('--voice-profile 须配合 --live-model --send 显式启用')
            speech = None
            speech_options=None
            default_voice_language=None
            if args.voice_options:
                from .voice_options import load_voice_options
                from .voice import VoiceError
                try:
                    speech_options,default_voice_language=load_voice_options(args.voice_options,lambda:provider_from_env(True))
                    speech=speech_options[default_voice_language]
                except VoiceError as exc:raise ValidationError(str(exc)) from None
            if args.voice_profile:
                from .voice import LocalSpeech, VoiceProfile, VoiceError
                try:
                    profile = VoiceProfile.load(args.voice_profile)
                    if profile.text_language == 'ja':
                        from .voice_japanese import JapaneseSpeech
                        speech = JapaneseSpeech(profile, provider_from_env(True))
                    else:
                        speech = LocalSpeech(profile)
                except VoiceError as exc:
                    raise ValidationError(str(exc)) from None
            if settings.private_mode == 'allowlist' and not settings.allowed_users:
                raise ValidationError('请先配置私聊白名单 QQ 号')
            secret = secrets.get('webhook_secret', '')
            if len(secret) < 16: raise ValidationError('请先在 configure 保存 HTTP 上报密钥')
            from .natural_dialogue import load_natural_persona
            persona = load_natural_persona() if args.natural_dialogue else None
            provider = conversation_provider(args.live_model, args.review_replies, args.reviewer_profile, natural_dialogue=args.natural_dialogue)
            sender = None
            client = None
            if args.send or settings.private_mode == 'friends':
                client = OneBotClient(settings.gateway_url, secrets.get('gateway_token', ''), settings.bot_id)
                client.verify_identity()
            if args.send: sender = client
            if speech is not None:
                from .reply_modes import ModeRoutingSender
                modes = (args.db or ROOT / 'data' / 'qq.sqlite3').with_suffix('.reply-modes.sqlite3')
                sender = ModeRoutingSender(client, speech, modes,speech_by_language=speech_options,default_language=default_voice_language)
            from .server import serve
            runtime = Runtime(args.db or ROOT / 'data' / ('qq.sqlite3' if args.send else 'qq-dry-run.sqlite3'), settings, provider, sender, friend_source=client, persona=persona)
            if args.natural_dialogue:
                print('自然对话试用：DeepSeek Pro充分核对，每轮一次请求；角色感等待QQ用户验收。',flush=True)
            if args.live_model:
                print('真实模型模式：获授权私聊、角色及该作用域内选中上下文将发送给配置的 DeepSeek 端点。')
            serve(runtime, secret)
            return 0
        except (ValidationError, ProviderError, DeliveryError) as exc:
            emit(status='error', reason=str(exc))
            return 2
        except (OSError, sqlite3.Error):
            emit(status='error', reason='QQ 服务启动或本地存储失败，请检查端口、配置与权限')
            return 2
        except KeyboardInterrupt:
            print('服务已停止；未决发送保留状态，不会自动重发。')
            return 130
    if args.command == 'doctor':
        from .settings import DATA
        emit(status='ok', python=sys.version.split()[0], sqlite=sqlite3.sqlite_version,
             default_data=str(ROOT / 'data'), network='未发起请求',
             local_settings=(DATA / 'settings.json').exists(), qq_transport='请使用 serve 启动接入')
        return 0
    mode = ('chat-live' if args.live_model else 'chat-mock') if args.command == 'chat' else args.command
    store = None
    try:
        store = Store(args.db or ROOT / 'data' / (mode + '.sqlite3'))
        if args.command == 'chat':
            return chat(args, store)
        if args.command == 'replay':
            return replay(args, store)
        engine = Engine(store, MockProvider(), ReplyPolicy())
        private, group = Scope('10000', 'anon', '20000'), Scope('10000', 'anon', '20000', 'group:30000')
        events = [MessageEvent('demo-private', private, '今天有点累'),
                  MessageEvent('demo-silent', group, '有人在吗'),
                  MessageEvent('demo-mention', group, '爱音，吉他怎么练', True)]
        errors = 0
        for event in events + [events[-1]]:
            result = engine.handle(event)
            emit(mode='dry-run', event_id=event.event_id, **asdict(result))
            errors += result.status in ('failed', 'pending')
        return 2 if errors else 0
    except ValidationError as exc:
        emit(status='error', reason=str(exc))
        return 2
    except (OSError, sqlite3.Error, UnicodeError):
        emit(status='error', reason='本地文件或数据库不可用，请检查路径、编码、空间和权限')
        return 2
    except KeyboardInterrupt:
        print('\n已退出；若处理中断，该事件保持未决状态，不自动重新调用模型。')
        return 130
    finally:
        if store is not None:
            store.close()
