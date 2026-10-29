"""Loopback-only authenticated task UI. Not a production or multi-user web server."""
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from urllib.parse import urlsplit

from ..domain import ValidationError
from .console_service import ConsoleService
from .jsonio import fields, strict_json


ASSETS = Path(__file__).with_name('console_web')


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, directory, port):
        self.service = ConsoleService(directory)
        self.session = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(('127.0.0.1', port), ConsoleHandler)
        self.origin = f'http://127.0.0.1:{self.server_port}'
        self.host_header = f'127.0.0.1:{self.server_port}'
        self.cookie_name = f'anon_console_{self.server_port}'

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def _equal(left, right):
    return isinstance(left, str) and len(left) <= 256 and secrets.compare_digest(left.encode('utf-8'), right.encode('utf-8'))


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = 'AnonLocalConsole'

    def setup(self):
        super().setup()
        self.connection.settimeout(4)

    def log_message(self, *args):
        pass  # Do not log request payloads, cookies or query strings.

    def _send(self, status, raw, mime='application/json; charset=utf-8', *, cookie=False, download=None):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(raw)))
        if download is not None:
            self.send_header('Content-Disposition', f'attachment; filename="{download}"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', f'{self.server.cookie_name}={self.server.session}; Path=/; HttpOnly; SameSite=Strict')
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, status, value):
        self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))

    def _auth(self):
        try:
            cookies = SimpleCookie()
            raw = self.headers.get('Cookie', '')
            if len(raw) > 2048 or len(self.headers.get_all('Cookie', [])) != 1:
                return False
            cookies.load(raw)
            entry = cookies.get(self.server.cookie_name)
            return entry is not None and _equal(entry.value, self.server.session)
        except CookieError:
            return False

    def _handle(self, post=False):
        if (self.headers.get_all('Host', []) != [self.server.host_header] or
                self.headers.get('Origin') not in (None, self.server.origin)):
            self._json(403, {'error': '只允许本机任务台访问'})
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            self._json(404, {'error': '找不到此入口'}); return
        if not post and path in ('/', '/app.js', '/app.css', '/favicon.ico'):
            if path == '/favicon.ico':
                self._send(204, b''); return
            name, mime = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/app.css': ('app.css', 'text/css; charset=utf-8')}[path]
            self._send(200, (ASSETS / name).read_bytes(), mime, cookie=path == '/')
            return
        if not self._auth():
            self._json(403, {'error': '连接已过期，请刷新任务台'}); return
        if post:
            if self.headers.get('Origin') != self.server.origin or not _equal(self.headers.get('X-Console-CSRF', ''), self.server.csrf):
                self._json(403, {'error': '请从任务台内执行操作'}); return
            lengths = self.headers.get_all('Content-Length', [])
            if (self.headers.get('Transfer-Encoding') or len(lengths) != 1 or
                    not re.fullmatch(r'[0-9]{1,4}', lengths[0]) or not 1 <= int(lengths[0]) <= 4096 or
                    self.headers.get('Content-Type') != 'application/json'):
                self._json(400, {'error': '请求格式或大小无效'}); return
            body = self.rfile.read(int(lengths[0]))
            if len(body) != int(lengths[0]):
                raise ValidationError('请求未完整接收')
            value = strict_json(body.decode('utf-8'))
        service = self.server.service
        if not post and path == '/api/session':
            self._json(200, {'csrf': self.server.csrf, 'features': {'exports': True}}); return
        if path == '/api/tasks':
            if not post:
                self._json(200, {'tasks': service.list_tasks()}); return
            fields(value, ('template', 'query', 'live', 'max_steps'))
            if type(value['max_steps']) is not int or value['max_steps'] not in (1, 10):
                raise ValidationError('请选择运行步数')
            task = service.create_and_start(value['template'], query=value['query'], live=value['live'], max_steps=value['max_steps'])
            self._json(202, task); return
        match = re.fullmatch(r'/api/tasks/([0-9a-f]{32})(?:/(run|stop|image/([0-9])|export/(md|zip)))?', path)
        if not match:
            self._json(404, {'error': '找不到此入口'}); return
        task_id, operation, index, format = match.groups()
        if not post and operation is None:
            self._json(200, service.detail(task_id)); return
        if not post and index is not None:
            self._send(200, service.image(task_id, int(index)), 'image/png'); return
        if not post and format is not None:
            artifact = service.export(task_id, format)
            self._send(200, artifact.content, artifact.content_type, download=artifact.filename); return
        if post and operation in ('run', 'stop'):
            fields(value, ('max_steps',) if operation == 'run' else ())
            task = service.start(task_id, max_steps=value['max_steps']) if operation == 'run' else service.stop(task_id)
            self._json(202, task); return
        self._json(404, {'error': '找不到此入口'})

    def _dispatch(self, post=False):
        try:
            self._handle(post)
        except (ValidationError, ValueError, TypeError, KeyError, OSError, sqlite3.Error):
            try:
                self._json(400, {'error': '操作未完成，请检查输入或任务状态'})
            except OSError:
                pass

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch(True)


def create_console(directory, *, port=8788):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValidationError('本机端口无效')
    return ConsoleServer(directory, port)
