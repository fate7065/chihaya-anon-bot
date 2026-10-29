"""SQLite persistence with exact scope filtering and atomic event claims."""
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from .domain import EventConflict, MemoryRecord, MessageEvent, ReplyResult, Scope, ValidationError


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL, content TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS memories_scope ON memories(scope, id);
            CREATE TABLE IF NOT EXISTS events (
                event_key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS history_scope ON history(scope, id);
        ''')

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def remember(self, scope: Scope, content: str) -> MemoryRecord:
        if not isinstance(content, str) or not content.strip() or len(content) > 1000:
            raise ValidationError('记忆须为 1–1000 个字符且非空白')
        created = datetime.now(timezone.utc).isoformat()
        with self.db:
            cursor = self.db.execute('INSERT INTO memories(scope,content,created_at) VALUES (?,?,?)',
                                     (scope.key, content.strip(), created))
        return MemoryRecord(cursor.lastrowid, scope, content.strip(), created)

    def recall(self, scope: Scope, query: str = '') -> list[MemoryRecord]:
        rows = self.db.execute('''SELECT * FROM memories WHERE scope=? AND instr(content,?)>0
                                  ORDER BY id DESC LIMIT 20''', (scope.key, query)).fetchall()
        return [MemoryRecord(row['id'], scope, row['content'], row['created_at']) for row in rows]

    def forget(self, scope: Scope, memory_id: int) -> bool:
        with self.db:
            cursor = self.db.execute('DELETE FROM memories WHERE id=? AND scope=?', (memory_id, scope.key))
        return cursor.rowcount == 1

    def history(self, scope: Scope) -> list[dict[str, str]]:
        rows = self.db.execute('SELECT role,content FROM history WHERE scope=? ORDER BY id DESC LIMIT 12',
                               (scope.key,)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def related_user_lines(self, scope: Scope, query: str) -> list[str]:
        """Find older verbatim user messages only when the user asks about the past."""
        if not re.search(r'记得|之前|上次|那次|以前|说过|叫什么', query):
            return []
        def grams(value):
            chunks = re.findall(r'[\u4e00-\u9fff]{2,}', value[:4000])
            return {chunk[i:i + width] for chunk in chunks for width in (2, 3)
                    for i in range(len(chunk) - width + 1)}
        stop = {'什么', '我说', '说过', '之前', '上次', '那次', '以前', '记得', '我的',
                '你还', '还记', '得我', '我家', '那个', '这个', '怎么', '过我'}
        keys = grams(query) - stop
        if not keys:
            return []
        rows = self.db.execute('''SELECT content FROM history WHERE scope=? AND role='user'
                                  ORDER BY id DESC LIMIT 200 OFFSET 6''', (scope.key,)).fetchall()
        ranked = []
        for index, row in enumerate(rows):
            content = row['content']
            score = sum(len(key) - 1 for key in keys & grams(content))
            if score:
                ranked.append((score, -index, content[:200]))
        ranked.sort(reverse=True)
        # Select for relevance, present chronologically so corrections stay newer.
        selected = sorted(ranked[:2], key=lambda item: item[1])
        return list(dict.fromkeys(item[2] for item in selected))

    def related_assistant_lines(self, scope: Scope, query: str) -> list[str]:
        """Recall older character-roleplay replies for a direct follow-up, not as canon."""
        if not re.search(r'之前|上次|说过|提过|后来|什么|哪|怎么样|吗|呢|[?？]', query):
            return []
        def grams(value):
            chunks = re.findall(r'[\u4e00-\u9fff]{2,}', value[:4000])
            return {chunk[i:i + 2] for chunk in chunks for i in range(len(chunk) - 1)}
        stop = {'之前', '上次', '说过', '提过', '后来', '还在', '了吗', '怎么',
                '怎么样', '你之', '前说', '的发', '挑好', '选好', '什么',
                '今天', '最近', '现在', '那个', '这个', '东西', '觉得',
                '自己', '时候', '还是', '就是', '好看', '有点', '颜色'}
        keys = grams(query) - stop
        if not keys:
            return []
        rows = self.db.execute('''SELECT content FROM history WHERE scope=? AND role='assistant'
                                  ORDER BY id DESC LIMIT 200 OFFSET 6''', (scope.key,)).fetchall()
        ranked = []
        for index, row in enumerate(rows):
            content = row['content']
            score = len(keys & grams(content))
            if score:
                ranked.append((score, -index, content[:200]))
        ranked.sort(reverse=True)
        selected = sorted(ranked[:2], key=lambda item: item[1])
        return list(dict.fromkeys(item[2] for item in selected))

    def claim(self, event: MessageEvent, retry_failed: bool = False) -> ReplyResult | None:
        with self.db:
            cursor = self.db.execute('''INSERT OR IGNORE INTO events(event_key,payload_hash,status)
                                       VALUES (?,?,'processing')''', (event.key, event.payload_hash))
            if cursor.rowcount:
                return None
            row = self.db.execute('SELECT * FROM events WHERE event_key=?', (event.key,)).fetchone()
            if row['payload_hash'] != event.payload_hash:
                raise EventConflict('同一事件键携带不同内容，已拒绝')
            if row['status'] == 'failed' and retry_failed:
                self.db.execute("UPDATE events SET status='processing', reason='', text='' WHERE event_key=?",
                                (event.key,))
                return None
            if row['status'] == 'processing':
                return ReplyResult('pending', '已有未决处理记录，需人工核对，未重新调用模型')
            if row['status'] == 'failed':
                return ReplyResult('failed', row['reason'])
            return ReplyResult('duplicate', row['status'] + ':' + row['reason'], row['text'])

    def finish(self, event: MessageEvent, result: ReplyResult, record_history=True):
        if result.status not in ('replied', 'skipped', 'failed'):
            raise ValidationError('不能保存非终态结果')
        with self.db:
            self.db.execute('UPDATE events SET status=?,reason=?,text=? WHERE event_key=?',
                            (result.status, result.reason, result.text, event.key))
            if result.status == 'replied' and record_history:
                self.db.executemany('INSERT INTO history(scope,role,content) VALUES (?,?,?)',
                                    [(event.scope.key, 'user', event.text),
                                     (event.scope.key, 'assistant', result.text)])
