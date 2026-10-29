"""Local permission boundary and durable action ledger; no platform APIs."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from types import MappingProxyType
from typing import Protocol

from anonbot.domain import EventConflict, ValidationError


def _identifier(value, name, limit=256):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or value != value.strip() or any(ord(c) < 32 for c in value)):
        raise ValidationError(f'{name} must be a bounded nonempty identifier')
    return value


def _json_value(value, depth=0):
    if depth > 20:
        raise ValidationError('payload nesting exceeds 20 levels')
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for child in value:
            _json_value(child, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            _json_value(child, depth + 1)
        return
    raise ValidationError('payload must contain only finite JSON values')


def _canonical(value):
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(',', ':'), allow_nan=False)
        result.encode('utf-8')
        return result
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValidationError('invalid JSON content') from exc


def _finite_number(value, name):
    if type(value) not in (int, float):
        raise ValidationError(f'{name} must be a finite number')
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValidationError(f'{name} must be a finite number') from exc
    if not math.isfinite(result):
        raise ValidationError(f'{name} must be a finite number')
    return result


@dataclass(frozen=True, init=False)
class ActionIntent:
    intent_id: str
    actor_id: str
    scope_id: str
    capability: str
    account_id: str
    target: str
    _payload_json: str

    def __init__(self, intent_id, actor_id, scope_id, capability, account_id, target, payload):
        for name, value, limit in (
            ('intent_id', intent_id, 128), ('actor_id', actor_id, 128),
            ('scope_id', scope_id, 512), ('capability', capability, 96),
            ('account_id', account_id, 128), ('target', target, 512),
        ):
            object.__setattr__(self, name, _identifier(value, name, limit))
        if type(payload) is not dict:
            raise ValidationError('payload must be a JSON object')
        _json_value(payload)
        encoded = _canonical(payload)
        if len(encoded.encode('utf-8')) > 32768:
            raise ValidationError('payload exceeds 32768 bytes')
        object.__setattr__(self, '_payload_json', encoded)

    @property
    def payload(self):
        """Return a copy so callers cannot mutate the approved content."""
        return json.loads(self._payload_json)

    def to_dict(self):
        return {name: getattr(self, name) for name in (
            'intent_id', 'actor_id', 'scope_id', 'capability', 'account_id', 'target',
        )} | {'payload': self.payload}

    @property
    def canonical_json(self):
        return _canonical(self.to_dict())

    @property
    def fingerprint(self):
        return sha256(self.canonical_json.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class Capability:
    name: str
    effect: str

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r'[a-z][a-z0-9_.]{0,95}', self.name):
            raise ValidationError('invalid capability name')
        if self.effect not in ('read', 'write'):
            raise ValidationError('capability effect must be read or write')


class Registry:
    """An explicit allowlist. Registering a name does not register an executor."""
    def __init__(self, capabilities):
        registered = {}
        for capability in capabilities:
            if not isinstance(capability, Capability) or capability.name in registered:
                raise ValidationError('invalid or duplicate capability')
            registered[capability.name] = capability
        self._registered = MappingProxyType(registered)

    def get(self, name):
        return self._registered.get(name)

    def capabilities(self):
        return tuple(self._registered.values())


def default_registry():
    return Registry(Capability(name, effect) for name, effect in (
        ('qq.group.send', 'write'), ('bilibili.search', 'read'),
        ('bilibili.info', 'read'), ('bilibili.subtitles', 'read'),
        ('bilibili.comment', 'write'), ('bilibili.dm', 'write'),
        ('bilibili.upload', 'write'), ('android.snapshot', 'read'),
        ('android.devices', 'read'), ('android.inspect', 'read'),
        ('android.tap', 'write'), ('android.type', 'write'),
    ))


@dataclass(frozen=True)
class Grant:
    actor_id: str
    scope_id: str
    capability: str
    account_id: str
    target: str

    def __post_init__(self):
        for name in ('actor_id', 'scope_id', 'capability', 'account_id', 'target'):
            _identifier(getattr(self, name), name, 512)

    @classmethod
    def for_intent(cls, intent):
        return cls(*(getattr(intent, name) for name in (
            'actor_id', 'scope_id', 'capability', 'account_id', 'target',
        )))


@dataclass(frozen=True, init=False)
class ExecutionResult:
    status: str
    detail: str
    _data_json: str | None

    def __init__(self, status, detail, data=None):
        if status not in ('simulated', 'succeeded', 'failed', 'unknown'):
            raise ValidationError('executor returned invalid terminal status')
        if not isinstance(detail, str) or len(detail) > 2048:
            raise ValidationError('executor detail must be a safe summary up to 2048 characters')
        encoded = None
        if data is not None:
            if type(data) is not dict:
                raise ValidationError('result data must be a JSON object')
            _json_value(data)
            encoded = _canonical(data)
            if len(encoded.encode('utf-8')) > 262144:
                raise ValidationError('result data exceeds 262144 bytes')
        object.__setattr__(self, 'status', status)
        object.__setattr__(self, 'detail', detail)
        object.__setattr__(self, '_data_json', encoded)

    @property
    def data(self):
        return json.loads(self._data_json) if self._data_json is not None else None


class Executor(Protocol):
    def execute(self, intent: ActionIntent) -> ExecutionResult:
        """Return a sanitized outcome; never retry an uncertain external effect."""
        ...


@dataclass(frozen=True)
class ActionRecord:
    intent: ActionIntent
    fingerprint: str
    status: str
    detail: str
    created_at: float
    expires_at: float
    approved_by: str | None
    _data_json: str | None = None

    @property
    def data(self):
        return json.loads(self._data_json) if self._data_json is not None else None

    def to_dict(self):
        return dict(intent=self.intent.to_dict(), fingerprint=self.fingerprint,
                    status=self.status, detail=self.detail, created_at=self.created_at,
                    expires_at=self.expires_at, approved_by=self.approved_by, data=self.data)


class ActionRuntime:
    """Use one dedicated DB. Local callers are trusted administrators.

    ``approve`` does not authenticate a remote user: the embedding application
    must provide that boundary. Grants and approvers are a configuration snapshot.
    A new configuration must be loaded when local permissions change.
    """
    def __init__(self, path, registry, grants, approver_ids, clock=time.time, approval_ttl=300.0):
        if str(path) == ':memory:':
            raise ValidationError('runtime requires a durable file path')
        if not isinstance(registry, Registry) or not callable(clock):
            raise ValidationError('invalid registry or clock')
        self.path = Path(path)
        self.registry = registry
        self.grants = frozenset(grants)
        self.approver_ids = frozenset(approver_ids)
        if not all(isinstance(grant, Grant) for grant in self.grants):
            raise ValidationError('grants must contain Grant objects')
        for approver in self.approver_ids:
            _identifier(approver, 'approver_id', 128)
        approval_ttl = _finite_number(approval_ttl, 'approval_ttl')
        if not 1 <= approval_ttl <= 86400:
            raise ValidationError('approval_ttl must be 1 to 86400 seconds')
        self.clock = clock
        self.approval_ttl = float(approval_ttl)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _now(self):
        return _finite_number(self.clock(), 'clock')

    def close(self):
        """Connections are opened and closed inside each transaction."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    @contextmanager
    def _transaction(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.execute('COMMIT')
        except BaseException:
            if connection.in_transaction:
                connection.execute('ROLLBACK')
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self._transaction() as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            if tables and tables != {'action_runtime_meta', 'action_intents'}:
                raise ValidationError('database belongs to another component; use a separate action DB')
            if tables:
                version = connection.execute('SELECT version FROM action_runtime_meta').fetchall()
                if len(version) != 1 or version[0][0] not in (1, 2):
                    raise ValidationError('unsupported action database schema')
                columns = {row[1] for row in connection.execute('PRAGMA table_info(action_intents)')}
                expected = {'intent_id', 'fingerprint', 'content', 'effect', 'status',
                            'detail', 'created_at', 'expires_at', 'approved_by'}
                if columns != (expected if version[0][0] == 1 else expected | {'result_json'}):
                    raise ValidationError('unsupported action database columns')
                if version[0][0] == 1:
                    connection.execute('ALTER TABLE action_intents ADD COLUMN result_json TEXT')
                    connection.execute('UPDATE action_runtime_meta SET version=2')
                return
            connection.execute('CREATE TABLE action_runtime_meta(version INTEGER NOT NULL)')
            connection.execute('INSERT INTO action_runtime_meta(version) VALUES (?)', (2,))
            connection.execute('''CREATE TABLE action_intents(
                intent_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, content TEXT NOT NULL,
                effect TEXT, status TEXT NOT NULL, detail TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL, approved_by TEXT,
                result_json TEXT
            )''')

    @staticmethod
    def _record(row):
        return ActionRecord(ActionIntent(**json.loads(row['content'])), row['fingerprint'],
                            row['status'], row['detail'], row['created_at'],
                            row['expires_at'], row['approved_by'], row['result_json'])

    @staticmethod
    def _row(connection, intent_id, required=True):
        _identifier(intent_id, 'intent_id', 128)
        row = connection.execute('SELECT * FROM action_intents WHERE intent_id=?', (intent_id,)).fetchone()
        if row is None and required:
            raise ValidationError('unknown intent_id')
        return row

    def _authorized(self, intent, stored_effect=None):
        capability = self.registry.get(intent.capability)
        return bool(capability and (stored_effect is None or capability.effect == stored_effect)
                    and Grant.for_intent(intent) in self.grants)

    @staticmethod
    def _update(connection, intent_id, status, detail):
        connection.execute('UPDATE action_intents SET status=?, detail=? WHERE intent_id=?',
                           (status, detail, intent_id))

    def submit(self, intent):
        if not isinstance(intent, ActionIntent):
            raise ValidationError('submit requires ActionIntent')
        with self._transaction() as connection:
            existing = self._row(connection, intent.intent_id, required=False)
            if existing is not None:
                if existing['fingerprint'] != intent.fingerprint:
                    raise EventConflict('intent_id already exists with different content')
                return self._record(existing)
            now = self._now()
            capability = self.registry.get(intent.capability)
            effect = capability.effect if capability else None
            if not self._authorized(intent):
                status, detail = 'denied', 'unknown capability or no exact grant'
            elif effect == 'read':
                status, detail = 'ready', 'read action authorized'
            else:
                status, detail = 'pending_approval', 'local approval required for this exact content'
            connection.execute('''INSERT INTO action_intents
                (intent_id,fingerprint,content,effect,status,detail,created_at,expires_at,approved_by)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (intent.intent_id, intent.fingerprint, intent.canonical_json, effect, status,
                 detail, now, now + self.approval_ttl, None))
            return self._record(self._row(connection, intent.intent_id))

    def approve(self, intent_id, approver_id):
        _identifier(approver_id, 'approver_id', 128)
        if approver_id not in self.approver_ids:
            raise ValidationError('approver is not configured as a local administrator')
        with self._transaction() as connection:
            row = self._row(connection, intent_id)
            if row['status'] != 'pending_approval':
                return self._record(row)
            intent = self._record(row).intent
            if self._now() >= row['expires_at']:
                self._update(connection, intent_id, 'expired', 'approval window expired')
            elif not self._authorized(intent, row['effect']):
                self._update(connection, intent_id, 'denied', 'authorization no longer valid')
            else:
                connection.execute('''UPDATE action_intents SET status=?,detail=?,approved_by=?
                    WHERE intent_id=?''', ('ready', 'approved for stored content', approver_id, intent_id))
            return self._record(self._row(connection, intent_id))

    def execute(self, intent_id, executor: Executor):
        with self._transaction() as connection:
            row = self._row(connection, intent_id)
            if row['status'] != 'ready':
                return self._record(row)
            intent = self._record(row).intent
            if self._now() >= row['expires_at']:
                self._update(connection, intent_id, 'expired', 'execution window expired')
            elif (not self._authorized(intent, row['effect']) or
                  (row['effect'] == 'write' and row['approved_by'] not in self.approver_ids)):
                self._update(connection, intent_id, 'denied', 'authorization no longer valid')
            else:
                self._update(connection, intent_id, 'executing', 'execution claimed; result pending')
            claimed = self._row(connection, intent_id)
            if claimed['status'] != 'executing':
                return self._record(claimed)
        # Commit the unique claim before calling an adapter or any external API.
        try:
            result = executor.execute(intent)
            if not isinstance(result, ExecutionResult):
                raise ValidationError('invalid executor result')
        except Exception:
            result = ExecutionResult('unknown', 'executor failed without a verified outcome; do not retry')
        with self._transaction() as connection:
            connection.execute('''UPDATE action_intents SET status=?,detail=?,result_json=?
                WHERE intent_id=? AND status=?''',
                (result.status, result.detail, result._data_json, intent_id, 'executing'))
            return self._record(self._row(connection, intent_id))

    def get(self, intent_id):
        with self._transaction() as connection:
            row = self._row(connection, intent_id, required=False)
            return self._record(row) if row is not None else None

    def recover(self):
        """Only call after ensuring no executor owns this DB (e.g. exclusive startup).

        This explicit administrative operation does not retry or reset any action.
        Constructors deliberately do not perform it while other workers may be live.
        """
        with self._transaction() as connection:
            return connection.execute('''UPDATE action_intents SET status=?,detail=? WHERE status=?''',
                ('unknown', 'interrupted execution; external outcome requires manual verification',
                 'executing')).rowcount
