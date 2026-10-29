"""Local approval boundary for one exact Bilibili recipient; fixture by default."""
from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .actions import ActionIntent, ActionRuntime, Grant, default_registry
from .bili_comment_protocol import load_credentials
from .bili_dm import DmExecutor, prepare_dm
from .bili_dm_fixture import FixtureDmTransport
from .bili_dm_http import DmTransport
from .bili_dm_protocol import DmError, side_effects, user_id, validate_dm
from .jsonio import read_json
from .workflow_lock import RunnerLock


def add_bili_dm_parser(commands):
    parser = commands.add_parser('bili-dm', help='指定收件人的文字私信，默认离线，须本地审批')
    operations = parser.add_subparsers(dest='dm_action', required=True)
    for name in ('demo', 'prepare', 'submit', 'approve', 'execute', 'status', 'recover'):
        command = operations.add_parser(name)
        command.add_argument('--directory', type=Path, required=True)
        command.add_argument('--live', action='store_true')
        command.add_argument('--account')
        command.add_argument('--recipient')
        command.add_argument('--credentials', type=Path)
        if name == 'prepare':
            command.add_argument('--message', required=True)
        if name == 'submit':
            command.add_argument('--proposal', type=Path, required=True)
        if name in ('submit', 'approve', 'execute', 'status'):
            command.add_argument('--intent-id', required=True)
        if name == 'approve':
            command.add_argument('--fingerprint', required=True)


def _context(args):
    if args.live and (args.account is None or args.recipient is None):
        raise DmError('live_dm_requires_account_and_recipient')
    if not args.live and (args.credentials is not None or args.account not in (None, '42') or args.recipient not in (None, '43')):
        raise DmError('fixture_dm_context_mismatch')
    if args.live and args.dm_action == 'demo':
        raise DmError('dm_demo_is_fixture_only')
    account = user_id('42' if args.account is None else args.account)
    recipient = user_id('43' if args.recipient is None else args.recipient)
    if account == recipient:
        raise DmError('dm_self_send_not_allowed')
    if args.live and args.dm_action in ('prepare', 'execute') and args.credentials is None:
        raise DmError('live_dm_requires_explicit_credentials')
    return {'mode': 'live' if args.live else 'fixture', 'account_id': account, 'recipient': recipient}


def _bind(directory, context, create):
    path = directory/'context.json'
    if path.exists():
        if read_json(path) != context:
            raise DmError('dm_task_context_mismatch')
    elif create:
        directory.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as handle:
            json.dump(context, handle, indent=2)
    else:
        raise DmError('dm_task_missing')


def _scope(context):
    return 'bilibili-dm:{mode}:{account_id}:{recipient}'.format(**context)


def _intent(ident, payload, context):
    payload = validate_dm(payload, context['account_id'])
    if payload['receiver_id'] != context['recipient']:
        raise DmError('dm_proposal_recipient_mismatch')
    return ActionIntent(ident, 'local-cli', _scope(context), 'bilibili.dm',
                        context['account_id'], context['recipient'], payload)


def _runtime(directory, context):
    grant = Grant('local-cli', _scope(context), 'bilibili.dm', context['account_id'], context['recipient'])
    return ActionRuntime(directory/'actions.sqlite3', default_registry(), [grant], ['local-cli'])


def _transport(args, context):
    if not args.live:
        return FixtureDmTransport(args.directory/'world.sqlite3')
    credentials = load_credentials(args.credentials)
    if credentials.mid != context['account_id']:
        raise DmError('dm_credential_account_mismatch')
    return DmTransport(credentials)


def _record(record):
    return {'type': 'bili_dm_action', 'intent_id': record.intent.intent_id,
            'fingerprint': record.fingerprint, 'status': record.status, 'detail': record.detail,
            'proposal': record.intent.payload, 'expires_at': record.expires_at,
            'side_effects': side_effects(), 'data': record.data}


def _save_proposal(directory, payload):
    path = directory/('proposal-' + uuid4().hex + '.json')
    with path.open('x', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return str(path.resolve())


def _demo(args, context, emit):
    args.directory.mkdir(parents=True, exist_ok=False)
    _bind(args.directory, context, True)
    prepared = prepare_dm(_transport(args, context), '42', '43', '你好，演示私信')
    _save_proposal(args.directory, prepared['proposal'])
    item = _intent('demo-dm', prepared['proposal'], context)
    with RunnerLock(args.directory/'control.lock'), _runtime(args.directory, context) as runtime:
        runtime.submit(item)
        runtime.approve(item.intent_id, 'local-cli')  # Only fixed synthetic content.
        executor = DmExecutor(_transport(args, context), live=False, account_id='42', recipient='43')
        record = runtime.execute(item.intent_id, executor)
        repeated = runtime.execute(item.intent_id, executor)
        emit(_record(record))
    with closing(sqlite3.connect(args.directory/'world.sqlite3')) as connection:
        count = connection.execute('SELECT count(*) FROM messages').fetchone()[0]
    summary = {'type': 'bili_dm_demo', 'complete': record.status == repeated.status == 'simulated' and count == 1,
               'status': record.status, 'stored_messages': count, 'external_actions': 0,
               'model_calls': 0, 'side_effects': side_effects()}
    with (args.directory/'summary.json').open('x', encoding='utf-8') as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    emit(summary)
    return 0 if summary['complete'] else 2


def bili_dm_command(args, emit):
    try:
        context = _context(args)
        operation = args.dm_action
        if operation == 'demo':
            return _demo(args, context, emit)
        item = _intent(args.intent_id, read_json(args.proposal), context) if operation == 'submit' else None
        if operation == 'prepare':
            validate_dm({'receiver_id': context['recipient'], 'message': args.message}, context['account_id'])
        _bind(args.directory, context, operation in ('prepare', 'submit'))
        if operation == 'prepare':
            prepared = prepare_dm(_transport(args, context), context['account_id'], context['recipient'], args.message)
            emit({'type': 'bili_dm_prepared', 'proposal_file': _save_proposal(args.directory, prepared['proposal']), **prepared})
            return 0
        if operation != 'submit' and not (args.directory/'actions.sqlite3').is_file():
            raise DmError('dm_ledger_missing')
        guard = nullcontext() if operation == 'status' else RunnerLock(args.directory/'control.lock')
        with guard, _runtime(args.directory, context) as runtime:
            if operation == 'recover':
                emit({'type': 'bili_dm_recovery', 'marked_unknown': runtime.recover(), 'retried': 0})
                return 0
            record = runtime.submit(item) if item else runtime.get(args.intent_id)
            if record is None:
                raise DmError('dm_intent_missing')
            expected = _intent(record.intent.intent_id, record.intent.payload, context)
            if expected.fingerprint != record.fingerprint:
                raise DmError('stored_dm_context_mismatch')
            if operation == 'approve':
                if args.fingerprint != record.fingerprint:
                    raise DmError('dm_approval_fingerprint_mismatch')
                record = runtime.approve(args.intent_id, 'local-cli')
            elif operation == 'execute' and record.status == 'ready':
                executor = DmExecutor(_transport(args, context), live=args.live,
                                      account_id=context['account_id'], recipient=context['recipient'])
                record = runtime.execute(args.intent_id, executor)
            emit(_record(record))
            return 0 if operation != 'execute' or record.status in ('simulated', 'succeeded') else 2
    except DmError as exc:
        emit({'type': 'bili_dm_error', 'reason': exc.code, 'platform_code': exc.platform_code})
        return 1
