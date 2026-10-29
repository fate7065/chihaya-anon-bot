"""Isolated, idempotent group replay storage with bounded room snapshots.

Only a short SQLite claim transaction is held during each event admission.
Judgment runs outside it. A room with a processing event is busy; new events are
not admitted until that event completes. A crashed claim is intentionally left
pending: reopening never repeats a potentially paid model request. This replay
ledger has no automatic crash recovery or retention and is not an online queue.
"""
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import threading

from ..domain import EventConflict, ValidationError
from .group import DecisionCouncil, GroupDecision, GroupMessage, GroupSnapshot, RoomRef


_APP = 'anonbot.autonomy.replay'
_VERSION = 1
_POLICY = 'group-council-v1'
_COLUMNS = {
    'replay_meta': ('app', 'version', 'policy'),
    'replay_events': ('room_key', 'event_id', 'payload_hash', 'revision',
                      'message_json', 'input_json', 'status', 'decision_json'),
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


class ReplayStore:
    """Use a dedicated database, not the private conversation or action ledger.

    Completed decisions remain authoritative on replay even if the supplied
    council/configuration has changed. Use a fresh database for model comparisons.
    `room_busy` means a new event was not admitted and can be submitted later.
    `in_progress` on a duplicate means the existing claim must not be rejudged.
    """

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, timeout=5, isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._initialize()
        except BaseException:
            self._db.close()
            raise

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    def _initialize(self):
        try:
            with self._transaction():
                objects = self._db.execute(
                    "SELECT name, type FROM sqlite_master "
                    "WHERE type IN ('table', 'view', 'trigger') "
                    "AND name NOT LIKE 'sqlite_%'").fetchall()
                if not objects:
                    self._db.execute(
                        'CREATE TABLE replay_meta (app TEXT PRIMARY KEY, '
                        'version INTEGER NOT NULL, policy TEXT NOT NULL)')
                    self._db.execute(
                        'INSERT INTO replay_meta VALUES (?, ?, ?)',
                        (_APP, _VERSION, _POLICY))
                    self._db.execute('''CREATE TABLE replay_events (
                        room_key TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK (revision > 0),
                        message_json TEXT NOT NULL,
                        input_json TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('processing', 'completed')),
                        decision_json TEXT,
                        PRIMARY KEY (room_key, event_id),
                        UNIQUE (room_key, revision),
                        CHECK ((status = 'processing' AND decision_json IS NULL) OR
                               (status = 'completed' AND decision_json IS NOT NULL))
                    )''')
                else:
                    expected = {(name, 'table') for name in _COLUMNS}
                    if {(row['name'], row['type']) for row in objects} != expected:
                        raise ValidationError('请选择独立的群聊回放数据库；现有数据库用途不匹配')
                    for table, expected_columns in _COLUMNS.items():
                        # Table names are module constants, never caller input.
                        columns = tuple(row['name'] for row in self._db.execute(
                            f'PRAGMA table_info({table})'))
                        if columns != expected_columns:
                            raise ValidationError('群聊回放数据库结构不兼容')
                    metadata = self._db.execute('SELECT * FROM replay_meta').fetchall()
                    if len(metadata) != 1 or tuple(metadata[0]) != (_APP, _VERSION, _POLICY):
                        raise ValidationError('群聊回放数据库版本或用途不兼容')
        except sqlite3.DatabaseError:
            raise ValidationError('无法读取群聊回放数据库结构') from None

    def _response(self, room, event_id, revision, *, duplicate=False,
                  decision=None, in_progress=False, room_busy=False):
        return {
            'room': asdict(room), 'revision': revision, 'event_id': event_id,
            'duplicate': duplicate, 'decision': decision,
            'in_progress': in_progress, 'room_busy': room_busy,
            'policy_version': _POLICY,
        }

    def _cached(self, room, row):
        return self._response(
            room, row['event_id'], row['revision'], duplicate=True,
            decision=json.loads(row['decision_json']) if row['decision_json'] else None,
            in_progress=row['status'] == 'processing')

    def process(self, room: RoomRef, message: GroupMessage, now: float,
                council: DecisionCouncil, *, enabled=True, quiet=False,
                last_reply_at=None) -> dict:
        # Validate every input before writing a claim or hashing JSON.
        GroupSnapshot(room, 1, (message,), now, last_reply_at, enabled, quiet)
        if not callable(getattr(council, 'evaluate', None)):
            raise ValidationError('council 必须提供 evaluate(snapshot)')
        payload = {
            'room': asdict(room), 'message': asdict(message), 'now': float(now),
            'last_reply_at': float(last_reply_at) if last_reply_at is not None else None,
            'enabled': enabled, 'quiet': quiet,
        }
        payload_json = _json(payload)
        payload_hash = sha256(payload_json.encode('utf-8')).hexdigest()
        with self._transaction():
            existing = self._db.execute(
                'SELECT * FROM replay_events WHERE room_key=? AND event_id=?',
                (room.key, message.event_id)).fetchone()
            if existing is not None:
                if existing['payload_hash'] != payload_hash:
                    raise EventConflict('同一房间的事件 ID 对应不同内容或判断输入')
                return self._cached(room, existing)
            pending = self._db.execute(
                "SELECT revision FROM replay_events WHERE room_key=? "
                "AND status='processing' ORDER BY revision DESC LIMIT 1",
                (room.key,)).fetchone()
            if pending is not None:
                return self._response(room, message.event_id, pending['revision'],
                                      in_progress=True, room_busy=True)
            latest = self._db.execute(
                'SELECT MAX(revision) FROM replay_events WHERE room_key=?',
                (room.key,)).fetchone()[0]
            revision = (latest or 0) + 1
            self._db.execute(
                'INSERT INTO replay_events VALUES (?, ?, ?, ?, ?, ?, ?, NULL)',
                (room.key, message.event_id, payload_hash, revision,
                 _json(asdict(message)), payload_json, 'processing'))
            rows = self._db.execute(
                'SELECT message_json FROM replay_events WHERE room_key=? '
                'ORDER BY revision DESC LIMIT 20', (room.key,)).fetchall()
            messages = []
            character_count = 0
            for row in rows:
                previous = GroupMessage(**json.loads(row['message_json']))
                if character_count + len(previous.text) > 12000:
                    break
                messages.append(previous)
                character_count += len(previous.text)
            snapshot = GroupSnapshot(room, revision, tuple(reversed(messages)),
                                     now, last_reply_at, enabled, quiet)

        try:
            decision = council.evaluate(snapshot)
            if not isinstance(decision, GroupDecision):
                raise ValidationError('判断器未返回 GroupDecision')
            decision_json = _json(asdict(decision))
        except Exception:
            # Never leak provider exception text or silently repeat its request.
            decision_json = _json(asdict(GroupDecision(
                'defer', 'judgment_failed_no_automatic_retry', ())))
        with self._transaction():
            updated = self._db.execute(
                "UPDATE replay_events SET status='completed', decision_json=? "
                "WHERE room_key=? AND event_id=? AND status='processing'",
                (decision_json, room.key, message.event_id))
            if updated.rowcount != 1:
                raise ValidationError('群聊回放记录状态冲突，未重新执行判断')
        return self._response(room, message.event_id, revision,
                              decision=json.loads(decision_json))

    def close(self):
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
