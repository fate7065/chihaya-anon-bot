"""Opt-in metadata-only network journal. Never accepts bodies or exception text."""
from contextlib import contextmanager, closing
from contextvars import ContextVar
import ipaddress
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .domain import ValidationError

SERVICES = frozenset(('chat', 'bilibili', 'embedding', 'vision'))
ACTIONS = frozenset(('generate', 'read', 'embed', 'inspect'))
STATUSES = frozenset(('pending', 'succeeded', 'failed'))
_context = ContextVar('anonbot_activity', default=None)


def safe_host(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme not in ('http', 'https') or not host or parsed.username is not None
                or parsed.password is not None or len(host) > 253
                or any(ord(c) <= 32 for c in url)):
            raise ValueError()
        port = parsed.port
        host = host.encode('idna').decode('ascii')
        if ':' in host:
            ipaddress.IPv6Address(host)
            host = '[' + host + ']'
        return host + (':' + str(port) if port is not None else '')
    except (ValueError, TypeError, UnicodeError):
        raise ValidationError('活动目标地址无效') from None


class ActivityLog:
    """Separate, bounded SQLite database. Pending rows remain evidence after a crash."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables != {'activity_meta', 'network_activity'}:
                raise ValidationError('活动库不能使用其他项目或聊天数据库')
            if tables and db.execute('SELECT version FROM activity_meta').fetchall() != [(1,)]:
                raise ValidationError('活动数据库版本不兼容')
            db.execute('CREATE TABLE IF NOT EXISTS activity_meta(version INTEGER NOT NULL)')
            if not tables:
                db.execute('INSERT INTO activity_meta VALUES(1)')
            db.execute('''CREATE TABLE IF NOT EXISTS network_activity(
                id INTEGER PRIMARY KEY, trace_id TEXT NOT NULL, service TEXT NOT NULL,
                action TEXT NOT NULL, host TEXT NOT NULL, mode TEXT NOT NULL,
                started REAL NOT NULL, finished REAL, duration_ms INTEGER,
                status TEXT NOT NULL, http_status INTEGER, bytes_received INTEGER)''')
            db.execute('CREATE INDEX IF NOT EXISTS activity_time ON network_activity(started)')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def begin(self, service, action, url, mode, trace_id=None):
        if service not in SERVICES or action not in ACTIONS or mode not in ('live', 'fixture'):
            raise ValidationError('活动类型无效')
        trace_id = trace_id or uuid4().hex
        if (not isinstance(trace_id, str) or len(trace_id) != 32
                or any(c not in '0123456789abcdef' for c in trace_id)):
            raise ValidationError('活动关联编号无效')
        host = safe_host(url)
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM network_activity').fetchone()[0] >= 100000:
                raise ValidationError('活动记录已满，请先备份并归档')
            row = db.execute('''INSERT INTO network_activity
                (trace_id,service,action,host,mode,started,status) VALUES(?,?,?,?,?,?,?)''',
                (trace_id, service, action, host, mode, time.time(), 'pending'))
            return row.lastrowid

    def finish(self, row_id, status, duration_ms, http_status=None, bytes_received=None):
        if (status not in ('succeeded', 'failed') or type(duration_ms) is not int or duration_ms < 0
                or (http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599))
                or (bytes_received is not None and (type(bytes_received) is not int or bytes_received < 0))):
            raise ValidationError('活动结果无效')
        with closing(self._connect()) as db, db:
            row = db.execute('''UPDATE network_activity SET status=?,finished=?,duration_ms=?,
                http_status=?,bytes_received=? WHERE id=? AND status='pending' ''',
                (status, time.time(), duration_ms, http_status, bytes_received, row_id))
            if row.rowcount != 1:
                raise ValidationError('活动结果已写入或不存在')

    def list(self, *, limit=50, before=None, service=None, status=None, trace_id=None):
        if (type(limit) is not int or not 1 <= limit <= 200
                or (before is not None and (type(before) is not int or not 1 <= before <= 2**63-1))
                or (service is not None and service not in SERVICES)
                or (status is not None and status not in STATUSES)
                or (trace_id is not None and (not isinstance(trace_id, str) or len(trace_id) != 32
                    or any(c not in '0123456789abcdef' for c in trace_id)))):
            raise ValidationError('活动筛选参数无效')
        conditions, values = [], []
        for name, value in [('id', before), ('service', service), ('status', status), ('trace_id', trace_id)]:
            if value is not None:
                conditions.append(name + (' < ?' if name == 'id' else ' = ?'))
                values.append(value)
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute('SELECT * FROM network_activity' + where +
                    ' ORDER BY id DESC LIMIT ?', (*values, limit + 1))]
        more = len(rows) > limit
        rows = rows[:limit]
        return {'items': rows, 'next_before': rows[-1]['id'] if more else None}

    def summary(self):
        now = time.time()
        with closing(self._connect()) as db:
            counts = dict(db.execute('SELECT status,COUNT(*) FROM network_activity GROUP BY status'))
            modes = dict(db.execute('SELECT mode,COUNT(*) FROM network_activity GROUP BY mode'))
            hourly = dict(db.execute('''SELECT CAST(started / 3600 AS INTEGER),COUNT(*)
                FROM network_activity WHERE started>=? GROUP BY 1''', (now - 24 * 3600,)))
        return dict(total=sum(counts.values()), **{s: counts.get(s, 0) for s in STATUSES},
                    live=modes.get('live', 0), fixture=modes.get('fixture', 0),
                    hourly=[{'hour': hour * 3600, 'count': hourly.get(hour, 0)}
                            for hour in range(int(now // 3600) - 23, int(now // 3600) + 1)])


class Span:
    def __init__(self, trace_id):
        self.trace_id = trace_id
        self.http_status = self.bytes_received = None

    def response(self, status, size):
        self.http_status, self.bytes_received = status, size


@contextmanager
def activity_scope(log, mode='live'):
    if not isinstance(log, ActivityLog) or mode not in ('live', 'fixture'):
        raise ValidationError('活动记录作用域无效')
    token = _context.set((log, mode, uuid4().hex))
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def read_activity_scope(*, simulation=False):
    """Bind one high-level read and its HTTP requests without changing legacy defaults."""
    context = _context.get()
    if context is None:
        path = os.environ.get('ANONBOT_ACTIVITY_DB')
        if not path:
            yield None
            return
        context = ActivityLog(path), 'live', uuid4().hex
    log, mode, trace_id = context
    if simulation:
        mode = 'fixture'
    token = _context.set((log, mode, trace_id))
    try:
        yield log, mode, trace_id
    finally:
        _context.reset(token)


@contextmanager
def network_span(service, action, url):
    context = _context.get()
    if context is None:
        path = os.environ.get('ANONBOT_ACTIVITY_DB')
        if not path:
            yield Span(uuid4().hex)
            return
        context = ActivityLog(path), 'live', uuid4().hex
    log, mode, trace_id = context
    row_id = log.begin(service, action, url, mode, trace_id)
    span = Span(trace_id)
    started = time.monotonic()
    status = 'failed'
    try:
        yield span
        status = 'succeeded'
    finally:
        # Failure here intentionally leaves pending evidence; never retry the request.
        log.finish(row_id, status, int((time.monotonic() - started) * 1000),
                   span.http_status, span.bytes_received)
