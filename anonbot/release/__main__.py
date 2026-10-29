"""Separate portable entry point. No QQ login or account actions on dashboard startup."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

from ..domain import ValidationError, ProviderError
from ..gateway import DeliveryError
from .config import init_config, load_config


def main(argv=None):
    parser = argparse.ArgumentParser(description='爱音：独立部署与上网活动')
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='新建独立配置和随机管理口令，不覆盖已有目录')
    init.add_argument('--directory', type=Path, required=True)
    for name in ('dashboard', 'doctor'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--config', type=Path, required=True)
        if name == 'dashboard':
            review_group=cmd.add_mutually_exclusive_group()
            review_group.add_argument('--review-model', action='store_true', help='允许登录后的管理员逐条调用已配置聊天 API 生成内容评价')
            review_group.add_argument('--review-worker-config',type=Path,help='从指定的本项目后台配置读取聊天端点与密钥文件')
            cmd.add_argument('--host', choices=('127.0.0.1', '0.0.0.0'), default='127.0.0.1')
            cmd.add_argument('--internal-port', type=int, help='仅反向代理后使用，与公开地址端口分开')
    package = commands.add_parser('bundle')
    package.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    package.add_argument('--output', type=Path, required=True)
    for name in ('backup-activity', 'restore-activity'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--source', type=Path, required=True)
        cmd.add_argument('--destination', type=Path, required=True)
    worker = commands.add_parser('worker', help='可移植QQ后台，默认不调用模型和不发送')
    worker.add_argument('--config', type=Path, required=True)
    worker.add_argument('--live-model', action='store_true')
    worker.add_argument('--send', action='store_true')
    worker.add_argument('--voice-options',type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            path = init_config(args.directory)
            result = {'status': 'initialized', 'config': str(path), 'credentials_printed': False}
        elif args.command == 'bundle':
            from .distribution import build_bundle
            report = build_bundle(args.root, args.output)
            result = {'status': 'bundled', 'file_count': report['file_count'], 'production_accepted': False}
        elif args.command in ('backup-activity', 'restore-activity'):
            from .backup import backup_activity, restore_activity
            function = backup_activity if args.command == 'backup-activity' else restore_activity
            result = {'status': 'verified', **function(args.source, args.destination)}
        elif args.command == 'worker':
            from .worker import run_worker
            run_worker(args.config, live_model=args.live_model, send=args.send,voice_options=args.voice_options)
            return 0
        else:
            config = load_config(args.config)
            config.dashboard_token()
            if args.command == 'doctor':
                result = {'status': 'configuration_valid', 'network_calls': 0, 'production_accepted': False,
                          'providers': {role: ('configured' if getattr(config, role) else 'not_configured')
                                        for role in ('embedding', 'vision')},
                          'platform_tests': 'not_run', 'server_tests': 'not_run'}
            else:
                import uvicorn
                from .dashboard import Dashboard
                origin = urlsplit(config.origin)
                port = args.internal_port or origin.port or (443 if origin.scheme == 'https' else 80)
                if not 1 <= port <= 65535:
                    raise ValidationError('端口无效')
                if args.host == '0.0.0.0' and (origin.scheme != 'https' or args.internal_port is None):
                    raise ValidationError('服务器监听需配置 HTTPS 公开地址和反向代理内部端口')
                from ..providers import provider_from_env
                review_provider = provider_from_env(True) if args.review_model else None
                if args.review_worker_config:
                    from .worker import configured_chat_provider
                    review_provider=configured_chat_provider(args.review_worker_config)
                uvicorn.run(Dashboard(config, review_provider=review_provider), host=args.host, port=port, workers=1,
                            access_log=False, proxy_headers=False, server_header=False,
                            limit_concurrency=64, timeout_keep_alive=5)
                return 0
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValidationError, ProviderError, DeliveryError, OSError, sqlite3.Error, ImportError, ValueError):
        print(json.dumps({'status': 'error', 'message': '操作未完成，请检查配置、独立目录、依赖或校验清单；敏感信息已隐藏'}, ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
