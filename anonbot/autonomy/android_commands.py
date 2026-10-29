"""Local CLI; offline unless --live and an explicit ADB executable are supplied."""
from pathlib import Path
from uuid import uuid4

from .actions import ActionIntent, ActionRuntime, Grant, default_registry
from .android_executor import AndroidReadExecutor
from .android_read import AdbClient, AndroidReadError, validate_serial


def add_android_parser(commands):
    android = commands.add_parser('android-read', help='安卓设备/当前应用/截图，默认离线')
    operations = android.add_subparsers(dest='operation', required=True)
    for name in ('devices', 'inspect', 'snapshot'):
        command = operations.add_parser(name)
        command.add_argument('--live', action='store_true', help='使用明确指定的本机ADB')
        command.add_argument('--adb', type=Path, help='可信adb.exe的绝对路径；不会自动下载')
        command.add_argument('--db', type=Path, default=Path('data/android-read-actions.sqlite3'))
        command.add_argument('--intent-id', help='相同ID与参数复用缓存，不自动重试')
        if name != 'devices':
            command.add_argument('--serial', help='明确设备序列号；离线默认demo-emulator')
        if name == 'snapshot':
            command.add_argument('--expected-package', help='可选，必须与前后台核对包名一致')
            command.add_argument('--artifacts', type=Path, default=Path('data/android-artifacts'))


def android_command(args, emit):
    try:
        if args.live and args.adb is None:
            raise AndroidReadError('live_requires_explicit_adb_path')
        if args.operation == 'devices':
            payload, target = {}, 'connected-devices'
        else:
            if args.live and not args.serial:
                raise AndroidReadError('live_requires_explicit_serial')
            target = validate_serial(args.serial or 'demo-emulator')
            payload = {'serial': target}
            if args.operation == 'snapshot':
                payload['expected_package'] = args.expected_package
        client = AdbClient(args.adb) if args.live else None
        executor = AndroidReadExecutor(client, live=args.live,
                                       artifact_dir=getattr(args, 'artifacts', 'data/android-artifacts'))
        intent = ActionIntent(args.intent_id or 'android-' + uuid4().hex, 'local-cli',
                              'android:local:' + ('live' if args.live else 'fixture'),
                              'android.' + args.operation, 'adb-local', target, payload)
        with ActionRuntime(args.db, default_registry(), [Grant.for_intent(intent)], []) as runtime:
            before = runtime.submit(intent)
            record = runtime.execute(intent.intent_id, executor)
            emit({'type': 'android_read', 'cached': before.status != 'ready', **record.to_dict()})
            return 0 if record.status in ('succeeded', 'simulated') else 2
    except AndroidReadError as exc:
        emit({'type': 'error', 'reason': str(exc)})
        return 1
