"""Bounded anonymous reads of Bilibili community web endpoints.

These endpoints are not a supported Open Platform contract. Rejections are
reported, never worked around with cookies, WBI fingerprints or retry loops.
All returned text is untrusted external content, not agent instructions.
"""
import html
import json
import re
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


API_ROOT = 'https://api.bilibili.com'
SEARCH_PATH = '/x/web-interface/wbi/search/type'
INFO_PATH = '/x/web-interface/view'
PLAYER_PATH = '/x/player/wbi/v2'
SUBTITLE_HOSTS = frozenset(('aisubtitle.hdslb.com', 'aisubtitle.bilibili.com'))
MAX_BYTES = 2 * 1024 * 1024


class BiliReadError(Exception):
    """Only stable reason codes and numeric platform codes may leave this boundary."""
    def __init__(self, code, platform_code=None):
        super().__init__(code)
        self.code = code
        self.platform_code = platform_code


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise BiliReadError('redirect_blocked')


class PublicTransport:
    def __init__(self, timeout=10, max_requests=8):
        if type(timeout) not in (int, float) or not 1 <= timeout <= 30:
            raise BiliReadError('invalid_timeout')
        if type(max_requests) is not int or not 1 <= max_requests <= 20:
            raise BiliReadError('invalid_request_budget')
        self.timeout = timeout
        self.max_requests = max_requests
        self.requests = 0
        self._lock = Lock()
        self._opener = build_opener(ProxyHandler({}), NoRedirect())

    @staticmethod
    def validate_url(url):
        try:
            if not isinstance(url, str) or len(url) > 4096 or any(ord(c) <= 32 for c in url):
                raise ValueError()
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or parsed.username is not None or
                    parsed.password is not None or parsed.port not in (None, 443) or parsed.fragment):
                raise ValueError()
            if parsed.hostname == 'api.bilibili.com':
                if parsed.path not in (SEARCH_PATH, INFO_PATH, PLAYER_PATH):
                    raise ValueError()
            elif parsed.hostname not in SUBTITLE_HOSTS or not parsed.path.endswith('.json'):
                raise ValueError()
        except (ValueError, TypeError):
            raise BiliReadError('url_not_allowed') from None
        return url

    def get_json(self, url):
        self.validate_url(url)
        from ..activity import network_span
        with network_span('bilibili', 'read', url) as span:
            return self._get_json(url, span)

    def _get_json(self, url, span):
        with self._lock:
            if self.requests >= self.max_requests:
                raise BiliReadError('request_budget_exhausted')
            self.requests += 1
        request = Request(url, headers={
            'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/',
            'Accept': 'application/json', 'Accept-Encoding': 'identity',
        }, method='GET')
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_BYTES + 1)
                span.response(response.getcode() if hasattr(response, 'getcode') else 200, len(raw))
            if len(raw) > MAX_BYTES:
                raise BiliReadError('response_too_large')
            def reject_constant(value):
                raise ValueError('nonfinite JSON')
            return json.loads(raw.decode('utf-8-sig'), parse_constant=reject_constant)
        except HTTPError as exc:
            code = exc.code
            span.response(code, 0)
            exc.close()
            raise BiliReadError('http_rejected', code) from None
        except (URLError, OSError, TimeoutError):
            raise BiliReadError('network_unavailable') from None
        except (ValueError, UnicodeError, RecursionError):
            raise BiliReadError('invalid_json_response') from None


def parse_bvid(value):
    if not isinstance(value, str):
        raise BiliReadError('invalid_bvid')
    if re.fullmatch(r'BV[0-9A-Za-z]{10}', value):
        return value
    try:
        parsed = urlsplit(value)
        match = re.fullmatch(r'/video/(BV[0-9A-Za-z]{10})/?', parsed.path)
        if (parsed.scheme == 'https' and parsed.netloc == 'www.bilibili.com' and match
                and not parsed.fragment and len(value) <= 2048
                and not any(ord(c) <= 32 for c in value)):
            return match[1]
    except ValueError:
        pass
    raise BiliReadError('invalid_bvid')


def _text(value, limit, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise BiliReadError('unexpected_response_shape')
    # Search titles include highlight tags. Text remains untrusted after cleaning.
    return html.unescape(re.sub(r'<[^>]*>', '', value))[:limit]


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise BiliReadError('unexpected_response_shape')
    return value


def _base(operation, **values):
    return {'operation': operation, 'content_trust': 'untrusted_external', **values}


class BilibiliClient:
    """Inject a transport for offline tests; the live transport only performs GET."""
    def __init__(self, transport):
        self.transport = transport

    def _api(self, path, **params):
        response = self.transport.get_json(API_ROOT + path + '?' + urlencode(params))
        if type(response) is not dict or type(response.get('code')) is not int:
            raise BiliReadError('unexpected_response_shape')
        if response['code'] != 0:
            raise BiliReadError('platform_rejected', response['code'])
        if type(response.get('data')) is not dict:
            raise BiliReadError('unexpected_response_shape')
        return response['data']

    def search(self, query, page=1, limit=5):
        if (not isinstance(query, str) or not query.strip() or len(query) > 100
                or any(ord(c) < 32 for c in query)
                or type(page) is not int or not 1 <= page <= 100
                or type(limit) is not int or not 1 <= limit <= 20):
            raise BiliReadError('invalid_search_arguments')
        data = self._api(SEARCH_PATH, keyword=query, search_type='video', page=page, page_size=limit)
        raw_items = data.get('result')
        if type(raw_items) is not list:
            raise BiliReadError('unexpected_response_shape')
        items = []
        for item in raw_items[:limit]:
            if type(item) is not dict:
                raise BiliReadError('unexpected_response_shape')
            bvid = parse_bvid(item.get('bvid'))
            items.append({'bvid': bvid, 'title': _text(item.get('title'), 300, True),
                          'author': _text(item.get('author'), 100),
                          'duration': _text(item.get('duration'), 32),
                          'url': 'https://www.bilibili.com/video/' + bvid})
        return _base('search', query=query, page=page, items=items,
                     returned=len(items), truncated=len(raw_items) > limit)

    def info(self, bvid):
        bvid = parse_bvid(bvid)
        data = self._api(INFO_PATH, bvid=bvid)
        if data.get('bvid') != bvid or type(data.get('owner')) is not dict:
            raise BiliReadError('unexpected_response_shape')
        raw_pages = data.get('pages')
        if type(raw_pages) is not list or not raw_pages:
            raise BiliReadError('unexpected_response_shape')
        pages = []
        for item in raw_pages[:50]:
            if type(item) is not dict:
                raise BiliReadError('unexpected_response_shape')
            pages.append({'page': _integer(item.get('page'), 1),
                          'cid': _integer(item.get('cid'), 1),
                          'title': _text(item.get('part'), 300),
                          'duration': _integer(item.get('duration'))})
        stat = data.get('stat', {})
        if type(stat) is not dict:
            raise BiliReadError('unexpected_response_shape')
        stats = {key: _integer(stat[key]) for key in ('view', 'like', 'reply', 'favorite') if key in stat}
        description = data.get('desc', '')
        return _base('info', bvid=bvid, title=_text(data.get('title'), 300, True),
                     description=_text(description, 2000), description_truncated=len(description) > 2000,
                     duration=_integer(data.get('duration')),
                     author={'id': _integer(data['owner'].get('mid'), 1),
                             'name': _text(data['owner'].get('name'), 100)},
                     stats=stats, pages=pages, pages_truncated=len(raw_pages) > 50,
                     url='https://www.bilibili.com/video/' + bvid)

    def subtitles(self, bvid, page=1, language=None):
        bvid = parse_bvid(bvid)
        if (type(page) is not int or not 1 <= page <= 50 or
                (language is not None and (not isinstance(language, str) or
                 not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', language)))):
            raise BiliReadError('invalid_subtitle_arguments')
        info = self.info(bvid)
        selected_page = next((item for item in info['pages'] if item['page'] == page), None)
        if selected_page is None:
            raise BiliReadError('page_not_found')
        base = _base('subtitles', bvid=bvid, page=page, url=info['url'])
        try:
            data = self._api(PLAYER_PATH, bvid=bvid, cid=selected_page['cid'])
        except BiliReadError as exc:
            if exc.code not in ('platform_rejected', 'http_rejected'):
                raise
            return {**base, 'status': 'unavailable', 'reason': 'player_access_rejected',
                    'platform_code': exc.platform_code}
        subtitle = data.get('subtitle')
        if type(subtitle) is not dict or type(subtitle.get('subtitles')) is not list:
            raise BiliReadError('unexpected_response_shape')
        tracks = subtitle['subtitles']
        if not tracks:
            return {**base, 'status': 'unavailable', 'reason': 'no_public_tracks_returned'}
        if any(type(track) is not dict or not isinstance(track.get('lan'), str) for track in tracks):
            raise BiliReadError('unexpected_response_shape')
        track = next((track for track in tracks if language is None or track['lan'] == language), None)
        if track is None:
            return {**base, 'status': 'unavailable', 'reason': 'requested_language_not_returned'}
        url = track.get('subtitle_url')
        if isinstance(url, str) and url.startswith('//'):
            url = 'https:' + url
        PublicTransport.validate_url(url)
        # A subtitle location can only be a subtitle CDN, never another API path.
        if urlsplit(url).hostname not in SUBTITLE_HOSTS:
            raise BiliReadError('url_not_allowed')
        response = self.transport.get_json(url)
        if type(response) is not dict or type(response.get('body')) is not list:
            raise BiliReadError('unexpected_response_shape')
        segments, characters = [], 0
        raw = response['body']
        truncated = len(raw) > 500
        for item in raw[:500]:
            if type(item) is not dict:
                raise BiliReadError('unexpected_response_shape')
            start, end, content = item.get('from'), item.get('to'), item.get('content')
            if (type(start) not in (int, float) or type(end) not in (int, float) or
                    not 0 <= start <= end <= 100000000 or
                    not isinstance(content, str)):
                raise BiliReadError('unexpected_response_shape')
            remaining = 16000 - characters
            if len(content) > remaining:
                truncated = True
            text = content[:remaining]
            if text:
                segments.append({'start': start, 'end': end, 'text': text})
            characters += len(text)
            if characters >= 16000:
                truncated = truncated or len(segments) < len(raw)
                break
        if not segments:
            return {**base, 'status': 'unavailable', 'reason': 'empty_subtitle_body'}
        return {**base, 'status': 'available', 'language': _text(track['lan'], 32),
                'segments': segments, 'truncated': truncated}
