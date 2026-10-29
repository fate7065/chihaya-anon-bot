"""Operator-only comment workflow; offline by default, no browser credential access."""
from contextlib import closing, nullcontext
from getpass import getpass, GetPassWarning
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
import warnings

from .actions import ActionIntent, ActionRuntime, Grant, default_registry
from .bili_comment import BiliCommentExecutor, prepare_comment
from .bili_comment_fixture import FixtureCommentTransport
from .bili_comment_http import CommentTransport, request_parameters
from .bili_comment_protocol import (BiliCommentError, BiliCredentials, load_credentials,
                                    positive_id, save_credentials, validate_comment)
from .jsonio import read_json
from .workflow_lock import RunnerLock


def add_bili_comment_parser(commands):
    parser = commands.add_parser('bili-comment', help='B站一级评论；默认离线，须本地内容审批')
    operations = parser.add_subparsers(dest='comment_action', required=True)
    configure = operations.add_parser('configure', help='本机隐藏输入独立账号凭据，不联网')
    configure.add_argument('--account', required=True)
    configure.add_argument('--output', type=Path, required=True)
    for name in ('demo', 'prepare', 'submit', 'approve', 'execute', 'status', 'recover'):
        command = operations.add_parser(name)
        command.add_argument('--directory', type=Path, required=True)
        command.add_argument('--live', action='store_true')
        command.add_argument('--account')
        command.add_argument('--credentials', type=Path)
        if name == 'prepare':
            command.add_argument('--video', required=True)
            command.add_argument('--message', required=True)
        if name == 'submit':
            command.add_argument('--proposal', type=Path, required=True)
        if name in ('submit', 'approve', 'execute', 'status'):
            command.add_argument('--intent-id', required=True)
        if name == 'approve':
            command.add_argument('--fingerprint', required=True)


def _context(args):
    if args.live and args.account is None:
        raise BiliCommentError('live_comment_requires_account')
    if not args.live and (args.credentials is not None or args.account not in (None, '42')):
        raise BiliCommentError('fixture_comment_context_mismatch')
    if args.live and args.comment_action == 'demo':
        raise BiliCommentError('comment_demo_is_fixture_only')
    if args.live and args.comment_action in ('prepare', 'execute') and args.credentials is None:
        raise BiliCommentError('live_comment_requires_explicit_credential_file')
    account_id = '42' if args.account is None else args.account
    return {'mode': 'live' if args.live else 'fixture', 'account_id': positive_id(account_id)}


def _bind(directory, context, create):
    path = directory/'context.json'
    if path.exists():
        if read_json(path) != context:
            raise BiliCommentError('comment_task_context_mismatch')
    elif create:
        directory.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as handle:
            json.dump(context, handle, indent=2)
    else:
        raise BiliCommentError('comment_task_missing')


def _transport(args, context):
    if not args.live:
        return FixtureCommentTransport(args.directory/'world.sqlite3')
    credentials = load_credentials(args.credentials)
    if credentials.mid != context['account_id']:
        raise BiliCommentError('credential_account_mismatch')
    return CommentTransport(credentials)


def _intent(ident, payload, context):
    return ActionIntent(ident, 'local-cli', f"bilibili-comment:{context['mode']}:{context['account_id']}",
                        'bilibili.comment', context['account_id'], payload['bvid'], payload)


def _runtime(directory, item=None):
    return ActionRuntime(directory/'actions.sqlite3', default_registry(),
                         [Grant.for_intent(item)] if item else [], ['local-cli'])


def _record(record):
    return {'type': 'bili_comment_action', 'intent_id': record.intent.intent_id,
            'fingerprint': record.fingerprint, 'status': record.status, 'detail': record.detail,
            'proposal': record.intent.payload, 'expires_at': record.expires_at, 'data': record.data}


def _save_proposal(directory, proposal):
    path = directory/('proposal-' + uuid4().hex + '.json')
    with path.open('x', encoding='utf-8') as handle:
        json.dump(proposal, handle, ensure_ascii=False, indent=2)
    return str(path.resolve())


def _demo(args, context, emit):
    args.directory.mkdir(parents=True, exist_ok=False)
    _bind(args.directory, context, True)
    prepared = prepare_comment(_transport(args, context), '42', 'BV1bwnF6eEe8', '练习加油')
    _save_proposal(args.directory, prepared['proposal'])
    item = _intent('demo-comment', prepared['proposal'], context)
    with RunnerLock(args.directory/'control.lock'), _runtime(args.directory, item) as runtime:
        runtime.submit(item)
        runtime.approve(item.intent_id, 'local-cli')  # Fixed synthetic demo only.
        executor = BiliCommentExecutor(_transport(args, context), live=False, account_id='42')
        record = runtime.execute(item.intent_id, executor)
        repeated = runtime.execute(item.intent_id, executor)
        emit(_record(record))
    with closing(sqlite3.connect(args.directory/'world.sqlite3')) as connection:
        count = connection.execute('SELECT count(*) FROM comments').fetchone()[0]
    summary = {'type': 'bili_comment_demo', 'complete': record.status == repeated.status == 'simulated' and count == 1,
               'status': record.status, 'stored_comments': count, 'external_actions': 0, 'model_calls': 0}
    with (args.directory/'summary.json').open('x', encoding='utf-8') as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    emit(summary)
    return 0 if summary['complete'] else 2


def bili_comment_command(args, emit):
    try:
        operation = args.comment_action
        if operation == 'configure':
            mid = positive_id(args.account)
            with warnings.catch_warnings():
                warnings.simplefilter('error', GetPassWarning)
                try:
                    credentials = BiliCredentials(mid, getpass('SESSDATA（隐藏输入）: '), getpass('bili_jct（隐藏输入）: '))
                except (GetPassWarning, EOFError):
                    raise BiliCommentError('hidden_interactive_input_required') from None
            save_credentials(args.output, credentials)
            emit({'type': 'bili_comment_credentials_saved', 'account_id': mid, 'network_requests': 0})
            return 0
        context = _context(args)
        if operation == 'demo':
            return _demo(args, context, emit)
        item = None
        if operation == 'submit':
            item = _intent(args.intent_id, validate_comment(read_json(args.proposal)), context)
        if operation == 'prepare':
            request_parameters('video', {'bvid': args.video})
            from .bili_comment_protocol import comment_text
            comment_text(args.message)
        _bind(args.directory, context, operation in ('prepare', 'submit'))
        if operation == 'prepare':
            prepared = prepare_comment(_transport(args, context), context['account_id'], args.video, args.message)
            path = _save_proposal(args.directory, prepared['proposal'])
            emit({'type': 'bili_comment_prepared', 'proposal_file': path, **prepared})
            return 0
        if operation != 'submit' and not (args.directory/'actions.sqlite3').is_file():
            raise BiliCommentError('comment_ledger_missing')
        guard = nullcontext() if operation == 'status' else RunnerLock(args.directory/'control.lock')
        with guard:
            if operation == 'recover':
                with _runtime(args.directory) as runtime:
                    count = runtime.recover()
                emit({'type': 'bili_comment_recovery', 'marked_unknown': count, 'retried': 0})
                return 0
            if item is None:
                with _runtime(args.directory) as runtime:
                    before = runtime.get(args.intent_id)
                if before is None:
                    raise BiliCommentError('comment_intent_missing')
                item = _intent(before.intent.intent_id, validate_comment(before.intent.payload), context)
                if item.fingerprint != before.fingerprint:
                    raise BiliCommentError('stored_comment_context_mismatch')
            with _runtime(args.directory, item) as runtime:
                record = runtime.submit(item) if operation == 'submit' else runtime.get(item.intent_id)
                if operation == 'approve':
                    if args.fingerprint != record.fingerprint:
                        raise BiliCommentError('comment_approval_fingerprint_mismatch')
                    record = runtime.approve(item.intent_id, 'local-cli')
                elif operation == 'execute' and record.status == 'ready':
                    executor = BiliCommentExecutor(_transport(args, context), live=args.live,
                                                  account_id=context['account_id'])
                    record = runtime.execute(item.intent_id, executor)
                emit(_record(record))
                return 0 if operation != 'execute' or record.status in ('simulated', 'succeeded') else 2
    except BiliCommentError as exc:
        emit({'type': 'bili_comment_error', 'reason': exc.code, 'platform_code': exc.platform_code})
        return 1
