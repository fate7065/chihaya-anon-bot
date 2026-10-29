"""Fixed authenticated community endpoints; one POST, no redirects or retries."""
from threading import Lock
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from anonbot.domain import ValidationError
from .bilibili import BiliReadError, parse_bvid
from .bili_comment_protocol import BiliCommentError, BiliCredentials, comment_text, positive_id
from .jsonio import strict_json


ROOT = 'https://api.bilibili.com'
MAX_RESPONSE = 512 * 1024
PATHS = {'nav': '/x/web-interface/nav', 'video': '/x/web-interface/view',
         'comment': '/x/v2/reply/add', 'receipt': '/x/v2/reply/reply'}


class CommentNoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise BiliCommentError('comment_redirect_blocked')


def request_parameters(operation, params):
    schemas = {'nav': set(), 'video': {'bvid'}, 'comment': {'aid', 'message'},
               'receipt': {'aid', 'rpid'}}
    if not isinstance(operation, str) or operation not in schemas or type(params) is not dict or params.keys() != schemas[operation]:
        raise BiliCommentError('invalid_comment_request')
    if operation == 'nav':
        return {}
    if operation == 'video':
        try:
            bvid = parse_bvid(params['bvid'])
        except BiliReadError:
            raise BiliCommentError('invalid_comment_bvid') from None
        if bvid != params['bvid']:
            raise BiliCommentError('invalid_comment_bvid')
        return {'bvid': bvid}
    if type(params['aid']) is not int:
        raise BiliCommentError('invalid_video_aid')
    oid = positive_id(params['aid'])
    if operation == 'comment':
        return {'oid': oid, 'type': 1, 'root': 0, 'parent': 0, 'plat': 1,
                'message': comment_text(params['message'])}
    return {'oid': oid, 'type': 1, 'root': positive_id(params['rpid']), 'ps': 1, 'pn': 1}


class CommentTransport:
    simulation = False

    def __init__(self, credentials, *, timeout=10):
        if not isinstance(credentials, BiliCredentials):
            raise BiliCommentError('invalid_credentials')
        if type(timeout) not in (int, float) or not 1 <= timeout <= 30:
            raise BiliCommentError('invalid_http_timeout')
        self.credentials, self.timeout = credentials, timeout
        self.requests, self.post_used = 0, False
        self._lock = Lock()
        self._opener = build_opener(ProxyHandler({}), CommentNoRedirect())

    def request(self, operation, params):
        values = request_parameters(operation, params)
        method = 'POST' if operation == 'comment' else 'GET'
        data = None
        url = ROOT + PATHS[operation]
        if method == 'POST':
            values['csrf'] = self.credentials.bili_jct
            data = urlencode(values).encode('ascii')
        elif values:
            url += '?' + urlencode(values)
        request = Request(url, data=data, method=method, headers={
            'Cookie': self.credentials.cookie_header(), 'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://www.bilibili.com/', 'Accept': 'application/json',
            'Accept-Encoding': 'identity', 'Content-Type': 'application/x-www-form-urlencoded',
        })
        with self._lock:
            if self.requests >= 4 or (method == 'POST' and self.post_used):
                raise BiliCommentError('comment_request_budget_exhausted')
            self.requests += 1
            if method == 'POST':
                self.post_used = True
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200 or response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise BiliCommentError('unexpected_comment_http_response')
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise BiliCommentError('comment_response_too_large')
            value = strict_json(raw.decode('utf-8-sig'), limit=MAX_RESPONSE)
            if (type(value) is not dict or type(value.get('code')) is not int
                    or not -(2**31) <= value['code'] < 2**31):
                raise BiliCommentError('invalid_comment_response')
            if value['code'] != 0:
                raise BiliCommentError('comment_platform_rejected', value['code'])
            if type(value.get('data')) is not dict:
                raise BiliCommentError('invalid_comment_response')
            return value['data']
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise BiliCommentError('comment_http_rejected', status) from None
        except (URLError, OSError, TimeoutError, HTTPException):
            raise BiliCommentError('comment_network_unavailable') from None
        except (ValidationError, UnicodeError, ValueError, RecursionError):
            raise BiliCommentError('invalid_comment_json') from None
