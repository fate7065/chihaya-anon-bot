"""Strict comment proposals and separate, account-bound encrypted credentials."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

from anonbot.domain import ValidationError
from anonbot.settings import protect, unprotect
from .bilibili import BiliReadError, parse_bvid
from .jsonio import read_json, strict_json


class BiliCommentError(Exception):
    def __init__(self, code, platform_code=None):
        super().__init__(code)
        self.code, self.platform_code = code, platform_code


def positive_id(value):
    if type(value) is int and 1 <= value <= 2**63-1:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,18}', value) and int(value) <= 2**63-1:
        return value
    raise BiliCommentError('invalid_positive_id')


def comment_text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 1000
            or any(ord(c) < 32 or 127 <= ord(c) < 160 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise BiliCommentError('invalid_comment_text')
    return value


def validate_comment(value):
    if type(value) is not dict or value.keys() != {'bvid', 'aid', 'message'}:
        raise BiliCommentError('invalid_comment_fields')
    try:
        bvid = parse_bvid(value['bvid'])
    except BiliReadError:
        raise BiliCommentError('invalid_comment_bvid') from None
    if bvid != value['bvid'] or type(value['aid']) is not int:
        raise BiliCommentError('invalid_comment_target')
    positive_id(value['aid'])
    return {'bvid': bvid, 'aid': value['aid'], 'message': comment_text(value['message'])}


@dataclass(frozen=True, repr=False)
class BiliCredentials:
    mid: str
    sessdata: str
    bili_jct: str

    def __post_init__(self):
        if not isinstance(self.mid, str) or positive_id(self.mid) != self.mid:
            raise BiliCommentError('invalid_account_id')
        if (not isinstance(self.sessdata, str) or not re.fullmatch(r'[A-Za-z0-9%._~+=:/-]{1,4096}', self.sessdata)
                or not isinstance(self.bili_jct, str) or not re.fullmatch(r'[0-9a-fA-F]{32}', self.bili_jct)):
            raise BiliCommentError('invalid_cookie_credentials')

    def cookie_header(self):
        return f'SESSDATA={self.sessdata}; bili_jct={self.bili_jct}; DedeUserID={self.mid}'


def save_credentials(path, credentials):
    if not isinstance(credentials, BiliCredentials):
        raise BiliCommentError('invalid_credentials')
    path = Path(path)
    if path.suffix.lower() != '.dpapi':
        raise BiliCommentError('encrypted_file_extension_required')
    try:
        content = json.dumps({'mid': credentials.mid, 'sessdata': credentials.sessdata,
                              'bili_jct': credentials.bili_jct})
        encrypted = protect(content)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as handle:
            json.dump({'version': 1, 'protected': encrypted}, handle)
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, ValidationError):
        raise BiliCommentError('credential_save_failed') from None


def load_credentials(path):
    try:
        envelope = read_json(path)
        if type(envelope) is not dict or envelope.keys() != {'version', 'protected'} or type(envelope['version']) is not int or envelope['version'] != 1:
            raise BiliCommentError('invalid_credential_file')
        if not isinstance(envelope['protected'], str):
            raise BiliCommentError('invalid_credential_file')
        value = strict_json(unprotect(envelope['protected']), limit=16384)
        if type(value) is not dict or value.keys() != {'mid', 'sessdata', 'bili_jct'}:
            raise BiliCommentError('invalid_credential_file')
        return BiliCredentials(**value)
    except (OSError, ValidationError, ValueError, TypeError):
        raise BiliCommentError('credential_load_failed') from None
