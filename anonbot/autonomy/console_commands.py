"""Start the local management UI; credentials stay in server memory."""
from pathlib import Path


def add_console_parser(commands):
    command = commands.add_parser('console', help='打开本地任务台')
    command.add_argument('--directory', type=Path, default=Path('data/console'))
    command.add_argument('--port', type=int, default=8788)
    command.add_argument('--open-browser', action='store_true')


def console_command(args, emit):
    from .console_http import create_console
    with create_console(args.directory, port=args.port) as server:
        emit({'type': 'local_console', 'url': server.origin, 'external_binding': False})
        if args.open_browser:
            import webbrowser
            webbrowser.open(server.origin)
        try:
            server.serve_forever(poll_interval=.2)
        finally:
            server.service.close()
    return 0
