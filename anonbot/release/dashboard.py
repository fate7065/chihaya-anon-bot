"""Read-only ASGI activity viewer. Run one Uvicorn worker behind TLS on servers."""
import asyncio
from collections import deque
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
import json
import re
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

from ..activity import ActivityLog
from ..domain import ValidationError
from ..seen_content import ContentLog, ContentReviewer
from . import VERSION
from .config import ReleaseConfig, strict_json

WEB = Path(__file__).with_name('web')
COOKIE = 'anon_activity_session'
HEADERS = [(b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
           (b'x-frame-options', b'DENY'), (b'referrer-policy', b'no-referrer'),
           (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self'; "
            b"img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")]


class HttpFailure(Exception):
    def __init__(self, status): self.status = status


class Dashboard:
    def __init__(self, config, *, review_provider=None):
        if not isinstance(config, ReleaseConfig):
            raise ValidationError('管理面板配置无效')
        self.config = config
        self.log = ActivityLog(config.data_dir / 'activity.sqlite3')
        self.content = ContentLog(config.data_dir / 'seen-content.sqlite3')
        self.reviewer = ContentReviewer(self.content, review_provider) if review_provider is not None else None
        self.token_hash = hashlib.sha256(config.dashboard_token().encode('ascii')).digest()
        self.sessions = {}
        self.attempts = deque(maxlen=10)
        self.expected_host = urlsplit(config.origin).netloc
        self.secure = config.origin.startswith('https://')
        self.assets = {'/': ('text/html; charset=utf-8', (WEB / 'index.html').read_bytes()),
                       '/app.js': ('text/javascript; charset=utf-8', (WEB / 'app.js').read_bytes()),
                       '/app.css': ('text/css; charset=utf-8', (WEB / 'app.css').read_bytes())}

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                event = await receive()
                if event['type'] == 'lifespan.startup':
                    await send({'type': 'lifespan.startup.complete'})
                elif event['type'] == 'lifespan.shutdown':
                    self.sessions.clear()
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        if scope['type'] != 'http':
            return
        try:
            status, content_type, body, extra = await self.route(scope, receive)
        except HttpFailure as exc:
            status, content_type, body, extra = exc.status, 'application/json', {'error': f'http_{exc.status}'}, []
        except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
            status, content_type, body, extra = 400, 'application/json', {'error': 'invalid_request'}, []
        except (sqlite3.Error, OSError):
            status, content_type, body, extra = 503, 'application/json', {'error': 'storage_unavailable'}, []
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        headers = [*HEADERS, (b'content-type', content_type.encode('ascii')),
                   (b'content-length', str(len(body)).encode('ascii')), *extra]
        if self.secure:
            headers.append((b'strict-transport-security', b'max-age=31536000'))
        await send({'type': 'http.response.start', 'status': status, 'headers': headers})
        await send({'type': 'http.response.body', 'body': body})

    def headers(self, scope):
        headers = {}
        for key, value in scope['headers']:
            key = key.lower().decode('ascii')
            if key in headers:
                raise HttpFailure(400)
            headers[key] = value.decode('latin-1')
        if headers.get('host') != self.expected_host:
            raise HttpFailure(400)
        if 'origin' in headers and headers['origin'] != self.config.origin:
            raise HttpFailure(403)
        return headers

    def session(self, headers):
        now = time.monotonic()
        self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > now}
        try:
            cookie = SimpleCookie()
            cookie.load(headers.get('cookie', ''))
            key = cookie[COOKIE].value
            if not 32 <= len(key) <= 128:
                raise KeyError()
            key = hashlib.sha256(key.encode('ascii')).digest()
            return key, self.sessions[key]
        except (KeyError, CookieError, UnicodeError):
            raise HttpFailure(401) from None

    async def json_body(self, receive, headers):
        if headers.get('content-type', '').split(';')[0] != 'application/json':
            raise HttpFailure(415)
        body = bytearray()
        deadline = time.monotonic() + 5
        while True:
            try:
                event = await asyncio.wait_for(receive(), timeout=max(0.001, deadline - time.monotonic()))
            except TimeoutError:
                raise HttpFailure(408) from None
            if event['type'] != 'http.request':
                raise HttpFailure(400)
            body.extend(event.get('body', b''))
            if len(body) > 4096:
                raise HttpFailure(413)
            if not event.get('more_body', False):
                break
        return strict_json(body)

    def cookie(self, value, age):
        return (f'{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}' +
                ('; Secure' if self.secure else '')).encode('ascii')

    async def route(self, scope, receive):
        path, method = scope['path'], scope['method']
        if path == '/healthz' and method == 'GET':
            return 200, 'application/json', {'service': 'anonbot-activity', 'version': VERSION}, []
        headers = self.headers(scope)
        query = scope.get('query_string', b'')
        if len(query) > 1024:
            raise HttpFailure(414)
        if query and path not in ('/api/activity', '/api/content'):
            raise HttpFailure(400)
        if path in self.assets and method == 'GET':
            content_type, body = self.assets[path]
            return 200, content_type, body, []
        if path == '/api/login' and method == 'POST':
            if headers.get('origin') != self.config.origin:
                raise HttpFailure(403)
            now = time.monotonic()
            if len(self.attempts) >= 10 and now - self.attempts[0] < 60:
                raise HttpFailure(429)
            self.attempts.append(now)
            data = await self.json_body(receive, headers)
            if type(data) is not dict or set(data) != {'token'} or not isinstance(data['token'], str):
                raise HttpFailure(400)
            digest = hashlib.sha256(data['token'].encode('utf-8')).digest()
            if not hmac.compare_digest(digest, self.token_hash):
                raise HttpFailure(401)
            # Remove expired sessions even if no authenticated reads occurred.
            self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > now}
            if len(self.sessions) >= 256:
                raise HttpFailure(429)
            key, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(key.encode('ascii')).digest()] = {'csrf': csrf, 'expires': now + 8 * 3600}
            return 200, 'application/json', {'csrf': csrf}, [(b'set-cookie', self.cookie(key, 8 * 3600))]
        if path == '/api/login':
            raise HttpFailure(404)
        content_detail = re.fullmatch(r'/api/content/([1-9][0-9]{0,15})', path)
        if path not in ('/api/session', '/api/activity', '/api/summary', '/api/status', '/api/logout', '/api/content', '/api/content-review') and not content_detail:
            raise HttpFailure(404)
        key, session = self.session(headers)
        if path == '/api/content-review' and method == 'POST':
            if (headers.get('origin') != self.config.origin or
                    not hmac.compare_digest(headers.get('x-csrf-token', ''), session['csrf'])):
                raise HttpFailure(403)
            data = await self.json_body(receive, headers)
            if type(data) is not dict or set(data) != {'id'} or type(data['id']) is not int:
                raise HttpFailure(400)
            if self.reviewer is None:
                raise HttpFailure(409)
            result = await asyncio.to_thread(self.reviewer.review, data['id'])
            return 200, 'application/json', {'review': result}, []
        if path == '/api/logout' and method == 'POST':
            if (headers.get('origin') != self.config.origin or
                    not hmac.compare_digest(headers.get('x-csrf-token', ''), session['csrf'])):
                raise HttpFailure(403)
            await self.json_body(receive, headers)
            del self.sessions[key]
            return 200, 'application/json', {'logged_out': True}, [(b'set-cookie', self.cookie('', 0))]
        if method != 'GET' or path in ('/api/logout', '/api/content-review'):
            raise HttpFailure(405)
        if path == '/api/session':
            data = {'csrf': session['csrf']}
        elif content_detail:
            data = await asyncio.to_thread(self.content.get, int(content_detail[1]))
        elif path == '/api/content':
            params = parse_qs(query.decode('ascii'), keep_blank_values=True, strict_parsing=True)
            if set(params) - {'limit', 'before', 'kind'} or any(len(v) != 1 for v in params.values()):
                raise HttpFailure(400)
            kwargs = {k: int(v[0]) if k in ('limit', 'before') else v[0] for k, v in params.items()}
            data = await asyncio.to_thread(self.content.list, **kwargs)
        elif path == '/api/activity':
            params = parse_qs(query.decode('ascii'), keep_blank_values=True, strict_parsing=True)
            if set(params) - {'limit', 'before', 'service', 'status', 'trace_id'} or any(len(v) != 1 for v in params.values()):
                raise HttpFailure(400)
            kwargs = {k: int(v[0]) if k in ('limit', 'before') else v[0] for k, v in params.items()}
            data = await asyncio.to_thread(self.log.list, **kwargs)
        elif path == '/api/summary':
            data = await asyncio.to_thread(self.log.summary)
        else:
            data = {'version': VERSION, 'release_ready': False, 'account_actions_enabled': False,
                    'content_review_enabled': self.reviewer is not None,
                    'providers': {role: ('configured_transfer_allowed' if config.allow_transfer else 'configured_transfer_blocked')
                                  if config is not None else 'not_configured'
                                  for role, config in [('embedding', self.config.embedding), ('vision', self.config.vision)]},
                    'coverage': ['chat.generate', 'bilibili.anonymous_read', 'embedding.embed', 'vision.inspect'],
                    'not_covered': ['qq_send', 'tts', 'bilibili_account_writes', 'device_operations', 'other_processes'],
                    'release_blockers': ['provider_acceptance', 'real_platform_acceptance', 'container_server_acceptance',
                                         'human_dialogue_and_voice_acceptance', 'full_planned_feature_acceptance']}
        return 200, 'application/json', data, []
