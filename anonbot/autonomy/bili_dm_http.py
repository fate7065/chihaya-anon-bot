"""Fixed candidate DM endpoints; no redirects, custom URLs or retry loops."""
from http.client import HTTPException
from threading import Lock
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from anonbot.domain import ValidationError
from .bili_comment_protocol import BiliCredentials
from .bili_dm_protocol import DmError, encode_content, user_id, validate_dm
from .jsonio import strict_json


NAV = 'https://api.bilibili.com/x/web-interface/nav'
SEND = 'https://api.vc.bilibili.com/web_im/v1/web_im/send_msg'
HISTORY = 'https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs'
MAX_RESPONSE = 512 * 1024


class DmNoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise DmError('dm_redirect_blocked')


def request_parameters(operation, params, sender_id):
    schemas = {'nav': set(), 'send': {'receiver_id', 'message'}, 'receipt': {'receiver_id'}}
    if not isinstance(operation, str) or operation not in schemas or type(params) is not dict or params.keys() != schemas[operation]:
        raise DmError('invalid_dm_request')
    sender_id = user_id(sender_id)
    if operation == 'nav':
        return {}
    recipient = user_id(params['receiver_id'])
    if recipient == sender_id:
        raise DmError('dm_self_send_not_allowed')
    if operation == 'send':
        payload = validate_dm(params, sender_id)
        return {'msg[sender_uid]': sender_id, 'msg[receiver_id]': recipient,
                'msg[receiver_type]': 1, 'msg[msg_type]': 1, 'msg[msg_status]': 0,
                'msg[dev_id]': str(uuid4()), 'msg[timestamp]': int(time.time()),
                'msg[new_face_version]': 1, 'msg[content]': encode_content(payload['message']),
                'build': 0, 'mobi_app': 'web'}
    return {'talker_id': recipient, 'session_type': 1, 'size': 20,
            'sender_device_id': 1, 'build': 0, 'mobi_app': 'web'}


class DmTransport:
    simulation = False

    def __init__(self, credentials, *, timeout=10):
        if not isinstance(credentials, BiliCredentials):
            raise DmError('invalid_credentials')
        if type(timeout) not in (int, float) or not 1 <= timeout <= 30:
            raise DmError('invalid_dm_timeout')
        self.credentials, self.timeout = credentials, timeout
        self.requests, self.post_used = 0, False
        self._lock = Lock()
        self._opener = build_opener(ProxyHandler({}), DmNoRedirect())

    def request(self, operation, params):
        values = request_parameters(operation, params, self.credentials.mid)
        method, url = ('POST', SEND) if operation == 'send' else ('GET', NAV if operation == 'nav' else HISTORY)
        data = None
        if method == 'POST':
            values.update(csrf=self.credentials.bili_jct, csrf_token=self.credentials.bili_jct)
            data = urlencode(values).encode('ascii')
        elif values:
            url += '?' + urlencode(values)
        request = Request(url, data=data, method=method, headers={
            'Cookie': self.credentials.cookie_header(), 'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://message.bilibili.com/', 'Accept': 'application/json',
            'Accept-Encoding': 'identity', 'Content-Type': 'application/x-www-form-urlencoded',
        })
        with self._lock:
            if self.requests >= 3 or method == 'POST' and self.post_used:
                raise DmError('dm_request_budget_exhausted')
            self.requests += 1
            if method == 'POST':
                self.post_used = True
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200 or response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise DmError('unexpected_dm_http_response')
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise DmError('dm_response_too_large')
            value = strict_json(raw.decode('utf-8-sig'), limit=MAX_RESPONSE)
            if type(value) is not dict or type(value.get('code')) is not int or not -(2**31) <= value['code'] < 2**31:
                raise DmError('invalid_dm_response')
            if value['code'] != 0:
                raise DmError('dm_platform_rejected', value['code'])
            if type(value.get('data')) is not dict:
                raise DmError('invalid_dm_response')
            return value['data']
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise DmError('dm_http_rejected', status) from None
        except (URLError, OSError, TimeoutError, HTTPException):
            raise DmError('dm_network_unavailable') from None
        except (ValidationError, UnicodeError, ValueError, RecursionError):
            raise DmError('invalid_dm_json') from None
