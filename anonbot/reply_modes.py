"""Explicit private-chat preferences and durable per-event delivery routes."""
from contextlib import closing
from pathlib import Path
import sqlite3

from .domain import Scope, ValidationError
from .gateway import DeliveryError
from .voice import SpeechSender


def reply_mode_command(text):
    commands = {'/文字': ('set', 'text'), '/语音': ('set', 'voice'),
                '/回复模式': ('query', None), '/回复模式 默认': ('reset', None),
                '/中文语音': ('language','zh'), '/日语语音': ('language','ja'),
                '/语音 中文': ('language','zh'), '/语音 日语': ('language','ja'),
                '/语音语言': ('language-query',None)}
    return commands.get(text.strip()) if isinstance(text, str) else None


class ReplyModes:
    def __init__(self, path, default='voice'):
        if default not in ('text', 'voice'):
            raise ValidationError('默认回复方式不正确')
        self.path, self.default = Path(path), default
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS reply_mode_preferences (
                    scope TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('text','voice')));
                CREATE TABLE IF NOT EXISTS reply_mode_routes (
                    event_key TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('text','voice')));
                CREATE TABLE IF NOT EXISTS voice_language_preferences (
                    scope TEXT PRIMARY KEY, language TEXT NOT NULL CHECK(language IN ('zh','ja')));
                CREATE TABLE IF NOT EXISTS voice_language_routes (
                    event_key TEXT PRIMARY KEY, language TEXT NOT NULL CHECK(language IN ('zh','ja')));
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def scope_key(self, scope):
        if not isinstance(scope, Scope) or scope.scene != 'private':
            raise ValidationError('回复方式设置只支持当前私聊')
        return scope.key

    def read(self, db, key):
        row = db.execute('SELECT mode FROM reply_mode_preferences WHERE scope=?', (key,)).fetchone()
        value = row[0] if row else self.default
        if value not in ('text', 'voice'):
            raise ValidationError('回复方式数据不正确')
        return value

    def get(self, scope):
        key = self.scope_key(scope)
        with closing(self.connect()) as db:
            return self.read(db, key)

    def set(self, scope, mode):
        key = self.scope_key(scope)
        if mode is not None and mode not in ('text', 'voice'):
            raise ValidationError('回复方式不正确')
        with closing(self.connect()) as db, db:
            if mode is None:
                db.execute('DELETE FROM reply_mode_preferences WHERE scope=?', (key,))
            else:
                db.execute('INSERT INTO reply_mode_preferences(scope,mode) VALUES (?,?) '
                           'ON CONFLICT(scope) DO UPDATE SET mode=excluded.mode', (key, mode))

    def for_event(self, event):
        key = self.scope_key(event.scope)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT mode FROM reply_mode_routes WHERE event_key=?', (event.key,)).fetchone()
            if row:
                if row[0] not in ('text', 'voice'):
                    raise ValidationError('事件回复方式不正确')
                return row[0]
            mode = self.read(db, key)
            db.execute('INSERT INTO reply_mode_routes(event_key,mode) VALUES (?,?)', (event.key, mode))
            return mode

    def language(self, scope, default):
        key=self.scope_key(scope)
        with closing(self.connect()) as db:
            row=db.execute('SELECT language FROM voice_language_preferences WHERE scope=?',(key,)).fetchone()
        value=row[0] if row else default
        if value not in ('zh','ja'):raise ValidationError('语音语言数据不正确')
        return value

    def set_language(self, scope, language):
        key=self.scope_key(scope)
        if language not in ('zh','ja'):raise ValidationError('语音语言只支持中文和日语')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT INTO reply_mode_preferences VALUES(?,'voice') ON CONFLICT(scope) DO UPDATE SET mode='voice'",(key,))
            db.execute('INSERT INTO voice_language_preferences VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET language=excluded.language',(key,language))

    def route(self, event, default_language):
        key=self.scope_key(event.scope)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT mode FROM reply_mode_routes WHERE event_key=?',(event.key,)).fetchone()
            fixed=db.execute('SELECT language FROM voice_language_routes WHERE event_key=?',(event.key,)).fetchone()
            mode=old[0] if old else self.read(db,key)
            # An existing legacy event was reserved using the original primary voice.
            if fixed:language=fixed[0]
            elif old:language=default_language
            else:
                preference=db.execute('SELECT language FROM voice_language_preferences WHERE scope=?',(key,)).fetchone()
                language=preference[0] if preference else default_language
            if mode not in ('text','voice') or language not in ('zh','ja'):raise ValidationError('回复路由数据不正确')
            if not old:db.execute('INSERT INTO reply_mode_routes VALUES(?,?)',(event.key,mode))
            if not fixed:db.execute('INSERT INTO voice_language_routes VALUES(?,?)',(event.key,language))
            return mode,language


class ModeRoutingSender:
    def __init__(self, client, speech, path, *, default='voice', speech_by_language=None, default_language=None):
        self.client = client
        self.speech_sender = SpeechSender(client, speech)
        self.multilingual=speech_by_language is not None
        language=getattr(speech.profile,'text_language','ja')
        self.default_language=default_language or language
        options=speech_by_language if self.multilingual else {language:speech}
        if (type(options) is not dict or not options or set(options)-{'zh','ja'}
                or self.default_language not in options):raise ValidationError('语音选项配置不正确')
        self.speech_senders={key:SpeechSender(client,value) for key,value in options.items()}
        self.max_model_calls_per_reply = max(s.max_model_calls_per_reply for s in self.speech_senders.values())
        self.modes = ReplyModes(path, default)

    def control_reply(self, event):
        command = reply_mode_command(event.text)
        if command is None:
            return None
        action, mode = command
        if action=='language':
            if mode not in self.speech_senders:return '当前没有配置这种语音语言'
            self.modes.set_language(event.scope,mode)
            return '已经切换为'+('中文' if mode=='zh' else '日语')+'语音回复'
        if action=='language-query':
            return '当前语音语言为'+('中文' if self.modes.language(event.scope,self.default_language)=='zh' else '日语')
        if action == 'set':
            self.modes.set(event.scope, mode)
        elif action == 'reset':
            self.modes.set(event.scope, None)
        mode = self.modes.get(event.scope)
        label = '文字' if mode == 'text' else '语音'
        if self.multilingual and mode=='voice':label=('中文' if self.modes.language(event.scope,self.default_language)=='zh' else '日语')+'语音'
        prefix = {'set': '已经切换为', 'query': '当前使用', 'reset': '已恢复默认为'}[action]
        return prefix + label + '回复'

    def extra_model_calls(self, event):
        if reply_mode_command(event.text) is not None:
            return 0
        mode,language=self.modes.route(event,self.default_language)
        if mode=='text':return 0
        if language not in self.speech_senders:raise ValidationError('该事件的语音语言当前不可用')
        return self.speech_senders[language].max_model_calls_per_reply

    def send(self, event, text):
        try:
            command = reply_mode_command(event.text)
            mode,language = ('text',self.default_language) if command is not None else self.modes.route(event,self.default_language)
            if mode=='voice' and language not in self.speech_senders:raise ValidationError('事件语音语言当前不可用')
        except (sqlite3.Error, ValidationError):
            raise DeliveryError('failed', '回复方式读取失败，未发送本段消息') from None
        if mode == 'text':
            return self.client.send(event, text)
        return self.speech_senders[language].send(event, text)
