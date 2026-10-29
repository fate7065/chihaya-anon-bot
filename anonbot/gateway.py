"""OneBot private-message transport and durable, single-worker processing.

Protocol sources: https://github.com/botuniverse/onebot-11/tree/master/communication
https://github.com/botuniverse/onebot-11/blob/master/api/public.md
"""
import copy
from dataclasses import asdict
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from .adapters import parse_onebot
from .domain import EventConflict, MessageEvent, ReplyResult, Scope, ValidationError, numeric_id
from .engine import Engine
from .memory import Store
from .policy import ReplyPolicy
from .providers import NoRedirect
from .settings import Settings


class QueueFull(RuntimeError): pass


class DeliveryError(RuntimeError):
    def __init__(self, state, reason):
        super().__init__(reason)
        self.state = state


CHAT_PAUSE = re.compile(r'(?<=[\u3400-\u9fff])[ \u3000]+(?=[\u3400-\u9fff])')
CHAT_ACKS = frozenset(('好', '嗯', '行', '哦', '有啊', '有的', '还真有', '买过', '会啊', '对啊'))


def _chat_phrases(line):
    phrases = CHAT_PAUSE.split(line)
    if len(phrases) < 3 or sum(map(len, phrases)) < 16:
        return None
    clauses = phrases[1:] if phrases[0] in CHAT_ACKS else phrases
    # Short fragments can be spaced names or part of a single phrase.
    return phrases if all(len(part.strip()) >= 6 for part in clauses) else None


def _tidy_chat_line(line):
    phrases = _chat_phrases(line)
    return '，'.join(phrases) if phrases else line


def split_reply(text: str) -> tuple[str, ...]:
    """Use at most one clear chat pause as a second QQ bubble."""
    body = text.strip()
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    # Formatting inside code, quotations and explanations is not a chat pause.
    if ('`' in body or body.startswith(('{', '['))
            or any(mark in body for mark in ('“', '「', '『', '‘', '"'))
            or re.search(r'(?m)^\s*(?:[-*] |\d+[.)、])', body)
            or (len(lines) > 1 and lines[0].endswith((':', '：')))):
        return (body,)
    if len(lines) == 2 and all(1 <= len(line) <= 100 for line in lines):
        return tuple(_tidy_chat_line(line) for line in lines)
    pauses = list(CHAT_PAUSE.finditer(body))
    phrases = _chat_phrases(body) if len(lines) == 1 else None
    if phrases:
        first, second = phrases[0], '，'.join(phrases[1:])
        if first in CHAT_ACKS and 6 <= len(second) <= 100:
            return first, second
        return ('，'.join(phrases),)
    if len(pauses) == 1:
        match = pauses[0]
        first, second = body[:match.start()].strip(), body[match.end():].strip()
        if first in CHAT_ACKS and 2 <= len(second) <= 100:
            return first, second
        # Short spaced names are ambiguous; prefer an explicit newline there.
        if 2 <= len(first) <= 100 and 2 <= len(second) <= 100 and len(first) + len(second) >= 10:
            return first, second
    return (body,)


def authorized(body: bytes, headers, secret: str) -> bool:
    if not secret: return False
    authorization = headers.get('Authorization', '')
    if authorization.startswith('Bearer ') and hmac.compare_digest(authorization[7:].encode(), secret.encode()):
        return True
    expected = 'sha1=' + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(headers.get('X-Signature', '').encode(), expected.encode())


class OneBotClient:
    def __init__(self, base_url: str, token: str, bot_id: str):
        Settings(gateway_url=base_url).validate()
        if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValidationError('请先配置 NapCat HTTP Access Token')
        self.base_url, self.token, self.bot_id = base_url.rstrip('/'), token, numeric_id(bot_id, 'bot_id')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, action, parameters):
        request = urllib.request.Request(self.base_url + '/' + action,
                    data=json.dumps(parameters, ensure_ascii=False).encode('utf-8'),
                    headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=10) as response:
                limit = 2000000 if action == 'get_friend_list' else 65536
                raw = response.read(limit + 1)
            if len(raw) > limit: raise ValueError()
            result = json.loads(raw)
            if not isinstance(result, dict) or type(result.get('retcode')) is not int:
                raise ValueError()
            if result.get('status') != 'ok' or result['retcode'] != 0:
                if result.get('status') == 'failed' and result['retcode'] != 0:
                    raise DeliveryError('failed', 'QQ 接入端明确拒绝操作；详情已隐藏')
                raise DeliveryError('unknown', 'QQ 接入端未确认完成，可能异步处理中；未自动重试')
            data = result['data']
            if not isinstance(data, dict) and not (action == 'get_friend_list' and isinstance(data, list)):
                raise ValueError()
            return data
        except DeliveryError: raise
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            raise DeliveryError('unknown', 'QQ 操作回执未知；未自动重试，请核对接入端') from None

    def verify_identity(self):
        data = self.call('get_login_info', {})
        if numeric_id(data.get('user_id'), '登录 QQ') != self.bot_id:
            raise ValidationError('NapCat 登录账号与配置的机器人 QQ 不同，已停止发送')

    def friend_ids(self):
        data = self.call('get_friend_list', {})
        if not isinstance(data, list) or len(data) > 10000 or any(not isinstance(item, dict) for item in data):
            raise DeliveryError('unknown', 'QQ 好友列表格式不兼容，未授权模型处理')
        try:
            return {numeric_id(item['user_id'], '好友 QQ') for item in data}
        except (KeyError, ValidationError):
            raise DeliveryError('unknown', 'QQ 好友列表格式不兼容，未授权模型处理') from None

    def send(self, event: MessageEvent, text: str) -> str:
        if event.scope.scene != 'private': raise ValidationError('当前发送端仅支持私聊')
        self.verify_identity()
        result = self.call('send_private_msg', {'user_id': int(event.scope.owner_id),
                         'message': [{'type': 'text', 'data': {'text': text}}]})
        receipt = result.get('message_id')
        if isinstance(receipt, bool) or not isinstance(receipt, (str, int)) or not re.fullmatch(r'-?[0-9]{1,20}', str(receipt)):
            raise DeliveryError('unknown', 'QQ 未返回有效 message_id；未自动重发')
        return str(receipt)

    def send_record(self, event: MessageEvent, clip, *, audio_root: Path) -> str:
        """Only a verified generated clip can become a OneBot record segment."""
        if event.scope.scene != 'private':
            raise ValidationError('当前发送端仅支持私聊')
        path = clip.validate(audio_root)
        self.verify_identity()
        result = self.call('send_private_msg', {'user_id': int(event.scope.owner_id),
                         'message': [{'type': 'record', 'data': {'file': path.as_uri()}}]})
        receipt = result.get('message_id')
        if isinstance(receipt, bool) or not isinstance(receipt, (str, int)) or not re.fullmatch(r'-?[0-9]{1,20}', str(receipt)):
            raise DeliveryError('unknown', 'QQ 未返回有效语音 message_id；未自动重发')
        return str(receipt)


class Runtime:
    def __init__(self, path: Path, settings: Settings, provider, sender=None, friend_source=None, persona=None):
        settings.validate()
        self.path, self.settings, self.provider, self.sender = path, settings, provider, sender
        self.allowed_users = {numeric_id(value, '白名单') for value in settings.allowed_users}
        self.stop = threading.Event()
        self.friend_source = friend_source
        self.persona = copy.deepcopy(persona)
        self.model_call_reservation = getattr(provider, 'max_calls_per_reply', 1)
        self.base_model_call_reservation = self.model_call_reservation
        speech_calls = getattr(sender, 'max_model_calls_per_reply', 0)
        if type(speech_calls) is not int or not 0 <= speech_calls <= 3:
            raise ValidationError('语音模型请求预留上界无效')
        self.speech_call_reservation = speech_calls
        if type(self.model_call_reservation) is int:
            self.model_call_reservation += speech_calls
        if type(self.model_call_reservation) is not int or not 1 <= self.model_call_reservation <= 10:
            raise ValidationError('模型请求预留上界无效')
        self.thread = None
        with self.store(): pass

    def store(self):
        store = Store(self.path)
        store.db.executescript('''
            CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL,
                event_json TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS inbox_status ON inbox(status,id);
            CREATE TABLE IF NOT EXISTS outbox (
                event_key TEXT PRIMARY KEY, scope TEXT NOT NULL, text TEXT NOT NULL,
                status TEXT NOT NULL, receipt TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS outbox_parts (
                event_key TEXT NOT NULL, part_index INTEGER NOT NULL, text TEXT NOT NULL,
                status TEXT NOT NULL, receipt TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(event_key, part_index));
            CREATE TABLE IF NOT EXISTS model_calls (id INTEGER PRIMARY KEY, called REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox_controls (event_key TEXT PRIMARY KEY);
            CREATE INDEX IF NOT EXISTS model_calls_called ON model_calls(called);
        ''')
        return store

    def submit(self, payload):
        event = parse_onebot(payload, self.settings.bot_id)
        if event is None or event.scope.scene != 'private' or event.scope.owner_id == event.scope.bot_id:
            return 'ignored'
        if self.settings.private_mode == 'allowlist' and event.scope.owner_id not in self.allowed_users:
            return 'ignored'
        with self.store() as store, store.db:
            store.db.execute('BEGIN IMMEDIATE')
            old = store.db.execute('SELECT payload_hash FROM inbox WHERE event_key=?', (event.key,)).fetchone()
            if old:
                if old['payload_hash'] != event.payload_hash: raise EventConflict('同一事件键内容冲突')
                return 'duplicate'
            queued = store.db.execute("SELECT count(*) FROM inbox WHERE status IN ('queued','processing','generated')").fetchone()[0]
            if queued >= self.settings.queue_limit: raise QueueFull('收件箱积压已达上限')
            store.db.execute('INSERT INTO inbox(event_key,payload_hash,event_json,status,created) VALUES (?,?,?,?,?)',
                (event.key, event.payload_hash, json.dumps(asdict(event), ensure_ascii=False), 'queued', time.time()))
        return 'queued'

    def status(self):
        with self.store() as store:
            return {name: {row[0]: row[1] for row in store.db.execute(f'SELECT status,count(*) FROM {name} GROUP BY status')}
                    for name in ('inbox', 'outbox')}

    def recover(self):
        # Called only after the service owns the single-instance lock.
        with self.store() as store, store.db:
            store.db.execute("UPDATE inbox SET status='unknown',reason='上次处理中断，需人工核对' WHERE status IN ('processing','generated')")
            store.db.execute("UPDATE outbox SET status='unknown' WHERE status IN ('queued','sending')")
            store.db.execute("UPDATE outbox_parts SET status='unknown' WHERE status='sending'")

    def jobs(self):
        with self.store() as store:
            rows = store.db.execute('''SELECT i.id,i.status,i.reason,o.status AS delivery,o.receipt
                                     FROM inbox i LEFT JOIN outbox o ON i.event_key=o.event_key
                                     ORDER BY i.id DESC LIMIT 30''').fetchall()
            jobs = [dict(row) for row in rows]
            for job in jobs:
                job['parts'] = [dict(part) for part in store.db.execute('''
                    SELECT p.part_index,p.status,p.receipt FROM outbox_parts p
                    JOIN inbox i ON i.event_key=p.event_key WHERE i.id=? ORDER BY p.part_index''',
                    (job['id'],))]
            return jobs

    def resolve(self, job_id: int, sent_id: str | None = None):
        """Local operator attestation after checking NapCat; never retries a send."""
        if type(job_id) is not int or not 1 <= job_id <= 9223372036854775807:
            raise ValidationError('无效任务 ID')
        if sent_id is not None and (not isinstance(sent_id, str) or not re.fullmatch(r'-?[0-9]{1,20}', sent_id)):
            raise ValidationError('发送回执须为已人工核对的 message_id')
        with self.store() as store, store.db:
            store.db.execute('BEGIN IMMEDIATE')
            row = store.db.execute('''SELECT i.*,o.text,o.status AS delivery FROM inbox i
                                     LEFT JOIN outbox o ON i.event_key=o.event_key WHERE i.id=?''', (job_id,)).fetchone()
            if not row or row['status'] != 'unknown': raise ValidationError('只能核对 unknown 状态的任务')
            if sent_id is not None and row['delivery'] != 'unknown': raise ValidationError('没有可核对的发送意图，不能标记已发送')
            parts = store.db.execute('''SELECT part_index,text,status,receipt FROM outbox_parts
                                        WHERE event_key=? ORDER BY part_index''', (row['event_key'],)).fetchall()
            if sent_id is not None and parts:
                unresolved = [part for part in parts if part['status'] not in ('sent', 'sent_manual')]
                if len(unresolved) != 1 or unresolved[0]['status'] != 'unknown':
                    raise ValidationError('只能核对唯一回执未知的分条；尚未发送或明确失败的分条不能标记已发送')
                store.db.execute("UPDATE outbox_parts SET status='sent_manual',receipt=? WHERE event_key=? AND part_index=?",
                                 (sent_id, row['event_key'], unresolved[0]['part_index']))
                parts = store.db.execute('''SELECT part_index,text,status,receipt FROM outbox_parts
                                            WHERE event_key=? ORDER BY part_index''', (row['event_key'],)).fetchall()
            elif sent_id is None and parts:
                store.db.execute("UPDATE outbox_parts SET status='cancelled' WHERE event_key=? AND status NOT IN ('sent','sent_manual')",
                                 (row['event_key'],))
            confirmed = [part for part in parts if part['status'] in ('sent', 'sent_manual')]
            if sent_id is not None:
                state = 'sent_manual'
            elif parts and len(confirmed) == len(parts):
                state = 'sent_manual'
            elif confirmed:
                state = 'partial_manual'
            else:
                state = 'cancelled'
            receipt = ','.join(part['receipt'] for part in confirmed) if parts else (sent_id or '')
            store.db.execute('UPDATE inbox SET status=?,reason=? WHERE id=?',
                             (state, '由本地操作者核对；未自动重发', job_id))
            store.db.execute('UPDATE outbox SET status=?,receipt=? WHERE event_key=?',
                             (state, receipt, row['event_key']))
            history_text = '\n'.join(part['text'] for part in confirmed) if parts else (row['text'] if sent_id else '')
            is_control = store.db.execute('SELECT 1 FROM outbox_controls WHERE event_key=?', (row['event_key'],)).fetchone()
            if history_text and not is_control:
                data = json.loads(row['event_json'])
                scope = Scope(**data['scope'])
                store.db.executemany('INSERT INTO history(scope,role,content) VALUES (?,?,?)',
                                    [(scope.key, 'user', data['text']), (scope.key, 'assistant', history_text)])

    def _state(self, store, key, state, reason=''):
        with store.db: store.db.execute('UPDATE inbox SET status=?,reason=? WHERE event_key=?', (state, reason, key))

    def process_one(self):
        with self.store() as store:
            with store.db:
                store.db.execute('BEGIN IMMEDIATE')
                row = store.db.execute("SELECT * FROM inbox WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                if not row or self.stop.is_set(): return False
                data = json.loads(row['event_json'])
                data['scope'] = Scope(**data['scope'])
                event = MessageEvent(**data)
                key = event.key
                blocked = store.db.execute("SELECT 1 FROM outbox WHERE scope=? AND status='unknown'", (event.scope.key,)).fetchone()
                if blocked:
                    store.db.execute("UPDATE inbox SET status='blocked',reason=? WHERE event_key=?", ('同会话有未决发送，需先核对', key))
                    return True
                store.db.execute("UPDATE inbox SET status='processing' WHERE event_key=?", (key,))
            if self.settings.private_mode == 'friends':
                try:
                    if self.friend_source is None: raise DeliveryError('failed', '好友校验端尚未配置')
                    friends = self.friend_source.friend_ids()
                except (DeliveryError, ValidationError):
                    self._state(store, key, 'failed', '好友校验失败；未调用模型或发送消息')
                    return True
                if event.scope.owner_id not in friends:
                    self._state(store, key, 'ignored', '不是已添加好友，未调用模型')
                    return True
            try:
                control = getattr(self.sender, 'control_reply', None)
                command_reply = control(event) if callable(control) else None
                if command_reply is not None and (not isinstance(command_reply, str) or not command_reply.strip()):
                    raise ValidationError('回复方式确认无效')
                reservation = self.model_call_reservation
                extra = getattr(self.sender, 'extra_model_calls', None)
                if command_reply is None and callable(extra):
                    speech_calls = extra(event)
                    if type(speech_calls) is not int or not 0 <= speech_calls <= self.speech_call_reservation:
                        raise ValidationError('回复方式请求预留无效')
                    reservation = self.base_model_call_reservation + speech_calls
            except (sqlite3.Error, ValidationError):
                self._state(store, key, 'failed', '回复方式处理失败，未调用模型或发送消息')
                return True
            if command_reply is None:
                with store.db:
                    store.db.execute('BEGIN IMMEDIATE')
                    count = store.db.execute('SELECT count(*) FROM model_calls WHERE called>?', (time.time() - 3600,)).fetchone()[0]
                    if count + reservation > self.settings.calls_per_hour:
                        store.db.execute("UPDATE inbox SET status='limited',reason=? WHERE event_key=?", ('每小时调用上限已达到，未调用模型', key))
                        return True
                    # Reserve before any paid attempt, including optional speech translation.
                    store.db.executemany('INSERT INTO model_calls(called) VALUES (?)',
                                         [(time.time(),)] * reservation)
                result = Engine(store, self.provider, ReplyPolicy(), self.settings.context_chars, record_history=False, persona=self.persona).handle(event)
            else:
                result = ReplyResult('replied', 'reply_mode_control', command_reply)
            if result.status != 'replied':
                self._state(store, key, 'failed', result.reason)
                return True
            if self.sender is None:
                self._state(store, key, 'dry_run', '已生成，未调用 QQ 发送端')
                return True
            parts = split_reply(result.text)
            sent_text = '\n'.join(parts)
            with store.db:
                store.db.execute('INSERT INTO outbox(event_key,scope,text,status) VALUES (?,?,?,?)',
                                 (key, event.scope.key, sent_text, 'queued'))
                store.db.executemany('INSERT INTO outbox_parts(event_key,part_index,text,status) VALUES (?,?,?,?)',
                                     [(key, index, part, 'queued') for index, part in enumerate(parts)])
                if command_reply is not None:
                    store.db.execute('INSERT INTO outbox_controls(event_key) VALUES (?)', (key,))
                store.db.execute("UPDATE inbox SET status='generated' WHERE event_key=?", (key,))
            if self.stop.is_set():
                with store.db:
                    store.db.execute("UPDATE outbox SET status='cancelled' WHERE event_key=?", (key,))
                    store.db.execute("UPDATE outbox_parts SET status='cancelled' WHERE event_key=?", (key,))
                self._state(store, key, 'cancelled')
                return True
            with store.db: store.db.execute("UPDATE outbox SET status='sending' WHERE event_key=?", (key,))
            receipts = []
            for index, part in enumerate(parts):
                with store.db:
                    store.db.execute("UPDATE outbox_parts SET status='sending' WHERE event_key=? AND part_index=?", (key, index))
                try:
                    receipt = self.sender.send(event, part)
                except DeliveryError as exc:
                    state = 'unknown' if receipts else exc.state
                    with store.db:
                        store.db.execute('UPDATE outbox_parts SET status=? WHERE event_key=? AND part_index=?',
                                         (exc.state, key, index))
                        store.db.execute('UPDATE outbox SET status=? WHERE event_key=?', (state, key))
                    self._state(store, key, state, str(exc))
                    return True
                except Exception:
                    with store.db:
                        store.db.execute("UPDATE outbox_parts SET status='unknown' WHERE event_key=? AND part_index=?", (key, index))
                        store.db.execute("UPDATE outbox SET status='unknown' WHERE event_key=?", (key,))
                    self._state(store, key, 'unknown', '发送适配器异常，未自动重试')
                    return True
                receipts.append(receipt)
                with store.db:
                    store.db.execute("UPDATE outbox_parts SET status='sent',receipt=? WHERE event_key=? AND part_index=?",
                                     (receipt, key, index))
                    store.db.execute("UPDATE outbox SET receipt=? WHERE event_key=?", (','.join(receipts), key))
            with store.db:
                store.db.execute("UPDATE outbox SET status='sent' WHERE event_key=?", (key,))
                store.db.execute("UPDATE inbox SET status='sent' WHERE event_key=?", (key,))
                if command_reply is None:
                    store.db.executemany('INSERT INTO history(scope,role,content) VALUES (?,?,?)',
                                        [(event.scope.key, 'user', event.text), (event.scope.key, 'assistant', sent_text)])
            return True

    def start(self):
        def work():
            while not self.stop.is_set():
                try:
                    if not self.process_one(): self.stop.wait(0.2)
                except Exception:
                    # Leave durable processing/unknown evidence; never silently retry a side effect.
                    self.stop.set()
        self.thread = threading.Thread(target=work, name='anonbot-worker', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread: self.thread.join(timeout=45)
