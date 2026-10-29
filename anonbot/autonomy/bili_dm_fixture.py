"""Persistent synthetic 42-to-43 text session, with large message keys."""
from contextlib import closing
from pathlib import Path
import sqlite3

from .bili_dm_http import request_parameters
from .bili_dm_protocol import DmError


class FixtureDmTransport:
    simulation = True

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.requests, self.post_used = 0, False
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, receiver TEXT NOT NULL, content TEXT NOT NULL)')

    def request(self, operation, params):
        values = request_parameters(operation, params, '42')
        if operation != 'nav' and params['receiver_id'] != '43':
            raise DmError('fixture_dm_recipient_not_found')
        if self.requests >= 3 or operation == 'send' and self.post_used:
            raise DmError('dm_request_budget_exhausted')
        self.requests += 1
        if operation == 'nav':
            return {'isLogin': True, 'mid': 42}
        with closing(sqlite3.connect(self.path)) as connection, connection:
            if operation == 'send':
                self.post_used = True
                index = connection.execute('INSERT INTO messages(receiver,content) VALUES (?,?)',
                                           ('43', values['msg[content]'])).lastrowid
                return {'msg_key': str(2**63 + index), 'msg_content': values['msg[content]']}
            rows = connection.execute('SELECT id,content FROM messages WHERE receiver=? ORDER BY id DESC LIMIT 20', ('43',)).fetchall()
            return {'messages': [{'msg_key': str(2**63 + row[0]), 'sender_uid': 42, 'receiver_id': 43,
                                  'receiver_type': 1, 'msg_type': 1, 'msg_status': 0, 'content': row[1]}
                                 for row in rows]}
