"""One account-bound text DM, verified by exact message key in its session."""
from anonbot.domain import ValidationError
from .actions import ExecutionResult
from .bili_dm_protocol import DmError, encode_content, message_key, side_effects, user_id, validate_dm
from .jsonio import strict_json


def _login(transport, account_id):
    login = transport.request('nav', {})
    if login.get('isLogin') is not True or str(login.get('mid')) != account_id or type(login.get('mid')) not in (int, str):
        raise DmError('dm_account_mismatch')


def prepare_dm(transport, account_id, recipient, message):
    account_id = user_id(account_id)
    payload = validate_dm({'receiver_id': recipient, 'message': message}, account_id)
    _login(transport, account_id)
    return {'proposal': payload, 'account_id': account_id, 'simulation': transport.simulation,
            'side_effects': side_effects()}


def _decode_content(raw):
    try:
        # A server may serialize Chinese as \u escapes. Bound that representation
        # separately, then enforce the original UTF-8 budget on decoded content.
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > 12000:
            raise DmError('invalid_dm_receipt_content')
        value = strict_json(raw, limit=12000)
        if type(value) is not dict or value.keys() != {'content'} or not isinstance(value['content'], str):
            raise DmError('invalid_dm_receipt_content')
        encode_content(value['content'])
        return value['content']
    except (ValidationError, UnicodeError):
        raise DmError('invalid_dm_receipt_content') from None


def verify_dm_receipt(data, key, account_id, recipient, message):
    messages = data.get('messages')
    if not isinstance(messages, list) or len(messages) > 20:
        raise DmError('dm_receipt_missing')
    matches = []
    for item in messages:
        if type(item) is not dict:
            raise DmError('invalid_dm_history_shape')
        try:
            item_key = message_key(item.get('msg_key'))
        except DmError:
            continue  # Never coerce floats or parse unrelated message content.
        if item_key == key:
            matches.append(item)
    if len(matches) != 1:
        raise DmError('dm_receipt_missing_or_ambiguous')
    item = matches[0]
    if (type(item.get('sender_uid')) not in (int, str) or str(item['sender_uid']) != account_id
            or type(item.get('receiver_id')) not in (int, str) or str(item['receiver_id']) != recipient
            or any(type(item.get(name)) is not int or item[name] != expected
                   for name, expected in (('receiver_type', 1), ('msg_type', 1), ('msg_status', 0)))
            or item.get('sys_cancel', False) is not False):
        raise DmError('dm_receipt_identity_or_status_mismatch')
    if _decode_content(item.get('content')) != message:
        raise DmError('dm_receipt_text_mismatch')


class DmExecutor:
    def __init__(self, transport, *, live, account_id, recipient):
        if type(live) is not bool or transport.simulation is not (not live):
            raise DmError('dm_transport_mode_mismatch')
        self.transport, self.live = transport, live
        self.account_id, self.recipient = user_id(account_id), user_id(recipient)
        if self.account_id == self.recipient:
            raise DmError('dm_self_send_not_allowed')

    def execute(self, intent):
        result = {'simulation': not self.live, 'write_attempted': False, 'post_accepted': False,
                  'receipt_verified': False, 'recipient_read_verified': False,
                  'recipient_delivery_verified': False, 'side_effects': side_effects(),
                  'verification_scope': 'authenticated_session_readback'}
        try:
            mode = 'live' if self.live else 'fixture'
            if (intent.actor_id != 'local-cli' or intent.scope_id != f'bilibili-dm:{mode}:{self.account_id}:{self.recipient}'
                    or intent.capability != 'bilibili.dm' or intent.account_id != self.account_id
                    or intent.target != self.recipient):
                raise DmError('dm_scope_mismatch')
            payload = validate_dm(intent.payload, self.account_id)
            if payload['receiver_id'] != self.recipient:
                raise DmError('dm_recipient_mismatch')
            _login(self.transport, self.account_id)
            result.update(account_id=self.account_id, receiver_id=self.recipient)
            result['write_attempted'] = True
            posted = self.transport.request('send', payload)
            key = message_key(posted.get('msg_key'))
            result['msg_key'] = key
            if 'msg_content' in posted and _decode_content(posted['msg_content']) != payload['message']:
                raise DmError('dm_post_content_mismatch')
            result['post_accepted'] = True
            receipt = self.transport.request('receipt', {'receiver_id': self.recipient})
            verify_dm_receipt(receipt, key, self.account_id, self.recipient, payload['message'])
            result['receipt_verified'] = True
            return ExecutionResult('succeeded' if self.live else 'simulated', 'dm_session_readback_verified', result)
        except DmError as exc:
            result.update(reason=exc.code, platform_code=exc.platform_code)
            return ExecutionResult('unknown' if result['write_attempted'] else 'failed',
                                   'dm_requires_manual_check' if result['write_attempted'] else 'dm_preflight_failed', result)
