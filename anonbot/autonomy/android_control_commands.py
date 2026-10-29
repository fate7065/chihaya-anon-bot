"""Local operator boundary for pinned Android actions; fixture is the default."""
import json
from contextlib import nullcontext
from pathlib import Path
import uuid

from .actions import ActionIntent, ActionRuntime, Grant, default_registry
from .android_control import (AdbControlClient, AndroidControlExecutor, FixtureControlClient,
                              device_lock, save_frame)
from .android_read import AndroidReadError, validate_package, validate_serial
from .android_ui import validate_proposal
from .jsonio import read_json
from .workflow_lock import RunnerLock


PACKAGES = ('com.example.demo', 'com.tencent.mobileqq', 'tv.danmaku.bili')


def add_android_control_parser(commands):
    parser = commands.add_parser('android-control', help='受控安卓点击/输入，默认离线')
    actions = parser.add_subparsers(dest='control_action', required=True)
    for name in ('demo', 'preview', 'submit', 'approve', 'execute', 'status', 'recover'):
        command = actions.add_parser(name)
        command.add_argument('--directory', type=Path, required=True)
        command.add_argument('--live', action='store_true')
        command.add_argument('--adb', type=Path)
        command.add_argument('--serial')
        command.add_argument('--package', choices=PACKAGES)
        if name == 'submit':
            command.add_argument('--proposal', type=Path, required=True)
            command.add_argument('--intent-id', required=True)
        if name in ('approve', 'execute', 'status'):
            command.add_argument('--intent-id', required=True)
        if name == 'approve':
            command.add_argument('--fingerprint', required=True)


def _context(args):
    if args.live and (not args.adb or not args.serial or not args.package):
        raise AndroidReadError('live_requires_adb_serial_package')
    if not args.live and (args.adb or args.serial not in (None, 'demo-emulator')
                          or args.package not in (None, 'com.example.demo')):
        raise AndroidReadError('fixture_context_mismatch')
    if args.control_action == 'demo' and args.live:
        raise AndroidReadError('demo_is_fixture_only')
    return {'mode': 'live' if args.live else 'fixture',
            'serial': validate_serial(args.serial or 'demo-emulator'),
            'package': validate_package(args.package or 'com.example.demo')}


def _bind(directory, context, create):
    path = directory / 'context.json'
    if path.is_file():
        if read_json(path) != context:
            raise AndroidReadError('task_context_mismatch')
    elif create:
        directory.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as handle:
            json.dump(context, handle, ensure_ascii=False, indent=2)
    else:
        raise AndroidReadError('task_context_missing')


def _runtime(directory, context):
    scope = 'android-control:{mode}:{serial}:{package}'.format(**context)
    grants = [Grant('local-cli', scope, 'android.' + op, 'adb-local', context['serial'])
              for op in ('tap', 'type')]
    return ActionRuntime(directory/'actions.sqlite3', default_registry(), grants, ['local-cli'])


def _client(args):
    return AdbControlClient(args.adb) if args.live else FixtureControlClient(args.directory/'fixture.sqlite3')


def _intent(ident, payload, context):
    return ActionIntent(ident, 'local-cli', 'android-control:{mode}:{serial}:{package}'.format(**context),
                        'android.' + payload['operation'], 'adb-local', context['serial'], payload)


def _record(record):
    return {'type': 'android_control_action', 'intent_id': record.intent.intent_id,
            'fingerprint': record.fingerprint, 'status': record.status, 'detail': record.detail,
            'data': record.data}


def _demo(args, context, emit):
    args.directory.mkdir(parents=True, exist_ok=False)
    _bind(args.directory, context, True)
    client = _client(args)
    statuses = []
    with RunnerLock(args.directory/'control.lock'), _runtime(args.directory, context) as runtime:
        for operation in ('tap', 'type'):
            frame = client.preview(context['serial'], context['package'])
            payload = {'operation': operation, 'screen_sha256': frame.screen_sha256,
                       'ui_sha256': frame.ui_sha256,
                       'selector': {'resource_id': context['package'] + ':id/' + ('next' if operation == 'tap' else 'edit')}}
            if operation == 'tap':
                payload['post_selector'] = {'resource_id': context['package'] + ':id/done'}
            else:
                payload.update(text='爱音 测试', expected_text='爱音 测试', text_mode='adbkeyboard')
            payload = validate_proposal(payload, context['package'])
            with (args.directory / (operation + '-proposal.json')).open('x', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            item = _intent('demo-' + operation, payload, context)
            runtime.submit(item)
            runtime.approve(item.intent_id, 'local-cli')  # Only fixed offline demonstration content.
            executor = AndroidControlExecutor(client, live=False, serial=context['serial'],
                                              package=context['package'], artifact_dir=args.directory/'images')
            record = runtime.execute(item.intent_id, executor)
            statuses.append(record.status)
            emit(_record(record))
    summary = {'type': 'android_control_demo', 'complete': statuses == ['simulated', 'simulated'],
               'statuses': statuses, 'external_actions': 0, 'model_calls': 0}
    with (args.directory/'summary.json').open('x', encoding='utf-8') as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    emit(summary)
    return 0 if summary['complete'] else 2


def android_control_command(args, emit):
    try:
        context = _context(args)
        command = args.control_action
        if command == 'demo':
            return _demo(args, context, emit)
        payload = validate_proposal(read_json(args.proposal), context['package']) if command == 'submit' else None
        # Validate the complete proposal before creating any DB or device client.
        item = _intent(args.intent_id, payload, context) if payload is not None else None
        _bind(args.directory, context, command in ('preview', 'submit'))
        if command == 'preview':
            frame = _client(args).preview(context['serial'], context['package'])
            name = 'preview-' + uuid.uuid4().hex
            metadata = save_frame(frame, args.directory/'images', name)
            path = args.directory/(name + '.json')
            with path.open('x', encoding='utf-8') as handle:
                json.dump(metadata | {'nodes': frame.nodes, 'simulation': not args.live,
                                      'content_trust': 'untrusted_external'}, handle, ensure_ascii=False, indent=2)
            emit({'type': 'android_control_preview', 'preview': str(path.resolve()),
                  'simulation': not args.live, **metadata})
            return 0
        if command != 'submit' and not (args.directory/'actions.sqlite3').is_file():
            raise AndroidReadError('action_ledger_missing')
        # Hold the ledger lock before claim, through transport completion. Recovery
        # must not mark an action unknown in the gap before its device lock is held.
        guard = nullcontext() if command == 'status' else RunnerLock(args.directory/'control.lock')
        with guard, _runtime(args.directory, context) as runtime:
            if command == 'recover':
                with device_lock(context['serial'], args.live):
                    count = runtime.recover()
                emit({'type': 'android_control_recovery', 'marked_unknown': count, 'retried': 0})
                return 0
            record = runtime.submit(item) if item else runtime.get(args.intent_id)
            if record is None:
                raise AndroidReadError('control_intent_missing')
            expected = _intent(record.intent.intent_id, validate_proposal(record.intent.payload, context['package']), context)
            if expected.fingerprint != record.fingerprint:
                raise AndroidReadError('stored_context_mismatch')
            if command == 'approve':
                if args.fingerprint != record.fingerprint:
                    raise AndroidReadError('approval_fingerprint_mismatch')
                record = runtime.approve(args.intent_id, 'local-cli')
            elif command == 'execute' and record.status == 'ready':
                executor = AndroidControlExecutor(_client(args), live=args.live, serial=context['serial'],
                                                  package=context['package'], artifact_dir=args.directory/'images')
                record = runtime.execute(args.intent_id, executor)
            emit(_record(record))
            return 0 if command != 'execute' or record.status in ('simulated', 'succeeded') else 2
    except AndroidReadError as exc:
        emit({'type': 'android_control_error', 'reason': str(exc)})
        return 1
