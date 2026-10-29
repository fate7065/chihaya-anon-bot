"""Loopback-only authenticated HTTP event receiver; no public web surface.

https://docs.python.org/3.13/library/http.server.html
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
from .domain import EventConflict, ValidationError
from .gateway import QueueFull, authorized


class InstanceLock:
    def __init__(self, database: Path):
        self.path = database.with_suffix('.service.lock')
        self.file = None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.file.write(b'1'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close(); self.file = None
            raise ValidationError('此数据文件已有机器人服务运行，请先停止原服务') from None
        return self
    def __exit__(self, *args):
        if self.file:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


class BodyError(ValidationError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.status = status


def read_body(stream, headers):
    """Decode NapCat's Node HTTP chunk framing without changing signed JSON bytes.

    Source: NapNeko/NapCatQQ v4.18.38 packages/napcat-common/src/request.ts.
    Node's write(data) + end() sends chunked when Content-Length is absent.
    """
    lengths = headers.get_all('Content-Length', [])
    transfers = headers.get_all('Transfer-Encoding', [])
    if transfers:
        if lengths or len(transfers) != 1 or transfers[0].strip().lower() != 'chunked':
            raise BodyError('unsupported_transfer')
        body = bytearray()
        while True:
            line = stream.readline(64)
            if not re.fullmatch(rb'[0-9a-fA-F]{1,8}\r\n', line):
                raise BodyError('invalid_chunk')
            size = int(line, 16)
            if size == 0:
                # This local client sends no extensions or trailer headers.
                if stream.read(2) != b'\r\n': raise BodyError('invalid_trailer')
                if not body: raise BodyError('body_too_large_or_empty', 413)
                return bytes(body)
            if len(body) + size > 65536: raise BodyError('body_too_large', 413)
            chunk = stream.read(size)
            if len(chunk) != size or stream.read(2) != b'\r\n': raise BodyError('incomplete_chunk')
            body.extend(chunk)
    if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,6}', lengths[0]):
        raise BodyError('invalid_length')
    size = int(lengths[0])
    if not 1 <= size <= 65536: raise BodyError('body_too_large', 413)
    raw = stream.read(size)
    if len(raw) != size: raise BodyError('incomplete_body')
    return raw


def make_server(runtime, secret: str, port=None):
    if len(secret) < 16: raise ValidationError('HTTP 上报密钥至少 16 个字符，请在配置窗口生成')
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def setup(self):
            super().setup()
            self.connection.settimeout(5)
        def log_message(self, *args): pass
        def answer(self, code, body):
            raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Connection', 'close')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.close_connection = True
            try: self.wfile.write(raw)
            except (BrokenPipeError, ConnectionError): pass
        def do_GET(self):
            if self.path != '/health': return self.answer(404, {'error': {'code': 'not_found'}})
            try:
                self.answer(200 if not runtime.stop.is_set() else 503,
                            {'status': 'stopped' if runtime.stop.is_set() else 'ok', **runtime.status()})
            except sqlite3.Error: self.answer(503, {'error': {'code': 'storage_unavailable'}})
        def do_POST(self):
            if self.path != '/events': return self.answer(404, {'error': {'code': 'not_found'}})
            if self.headers.get('Origin'):
                return self.answer(400, {'error': {'code': 'unsupported_request'}})
            if self.headers.get_content_type() != 'application/json':
                return self.answer(415, {'error': {'code': 'json_required'}})
            try:
                raw = read_body(self.rfile, self.headers)
                if not authorized(raw, self.headers, secret): return self.answer(401, {'error': {'code': 'unauthorized'}})
                if runtime.stop.is_set(): return self.answer(503, {'error': {'code': 'worker_stopped'}})
                status = runtime.submit(json.loads(raw))
                self.answer(200, {'status': status})
            except BodyError as exc: self.answer(exc.status, {'error': {'code': str(exc)}})
            except EventConflict: self.answer(409, {'error': {'code': 'event_conflict'}})
            except QueueFull: self.answer(503, {'error': {'code': 'queue_full'}})
            except (ValueError, UnicodeError, RecursionError): self.answer(400, {'error': {'code': 'invalid_event'}})
            except (OSError, sqlite3.Error): self.answer(503, {'error': {'code': 'storage_or_connection_unavailable'}})
    return ThreadingHTTPServer(('127.0.0.1', runtime.settings.port if port is None else port), Handler)


def serve(runtime, secret):
    with InstanceLock(runtime.path):
        server = make_server(runtime, secret)
        try:
            runtime.recover()
            runtime.start()
            print(json.dumps({'status': 'listening', 'url': f'http://127.0.0.1:{server.server_port}/events',
                              'send': runtime.sender is not None}, ensure_ascii=False))
            server.serve_forever(poll_interval=0.2)
        finally:
            runtime.close()
            server.server_close()
