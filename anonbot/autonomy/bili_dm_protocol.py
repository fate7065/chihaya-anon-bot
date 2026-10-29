"""Exact recipient, UTF-8 wire-content budget and precision-safe message IDs."""
import json
import re

from .bili_comment_protocol import BiliCommentError as DmError, positive_id


def side_effects():
    return {'may_mark_conversation_read': True}


def user_id(value):
    if not isinstance(value, str):
        raise DmError('dm_uid_must_be_canonical_string')
    return positive_id(value)


def encode_content(text):
    if (not isinstance(text, str) or not text.strip() or len(text) > 2000
            or any((ord(c) < 32 and c != '\n') or 127 <= ord(c) < 160
                   or 0xD800 <= ord(c) <= 0xDFFF for c in text)):
        raise DmError('invalid_dm_text')
    encoded = json.dumps({'content': text}, ensure_ascii=False, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > 2000:
        raise DmError('dm_content_exceeds_2000_bytes')
    return encoded


def validate_dm(value, sender_id=None):
    if type(value) is not dict or value.keys() != {'receiver_id', 'message'}:
        raise DmError('invalid_dm_proposal_fields')
    receiver = user_id(value['receiver_id'])
    if sender_id is not None and user_id(sender_id) == receiver:
        raise DmError('dm_self_send_not_allowed')
    encode_content(value['message'])
    return {'receiver_id': receiver, 'message': value['message']}


def message_key(value):
    if type(value) is int and 1 <= value < 2**64:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,19}', value) and int(value) < 2**64:
        return value
    raise DmError('invalid_dm_message_key')
