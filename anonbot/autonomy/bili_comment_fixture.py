"""Persistent synthetic comment protocol; no cookies or network access."""
from contextlib import closing
from pathlib import Path
import sqlite3

from .bili_comment_http import request_parameters
from .bili_comment_protocol import BiliCommentError


class FixtureCommentTransport:
    simulation = True

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.requests = 0
        self.post_used = False
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS comments (rpid INTEGER PRIMARY KEY AUTOINCREMENT, aid INTEGER NOT NULL, message TEXT NOT NULL)')

    def request(self, operation, params):
        values = request_parameters(operation, params)
        if self.requests >= 4 or operation == 'comment' and self.post_used:
            raise BiliCommentError('comment_request_budget_exhausted')
        self.requests += 1
        if operation == 'nav':
            return {'isLogin': True, 'mid': 42}
        if operation == 'video':
            if values['bvid'] != 'BV1bwnF6eEe8':
                raise BiliCommentError('fixture_video_not_found')
            return {'aid': 123, 'bvid': values['bvid'], 'title': '合成评论测试视频，非真实内容'}
        with closing(sqlite3.connect(self.path)) as connection, connection:
            if operation == 'comment':
                self.post_used = True
                ident = connection.execute('INSERT INTO comments(aid,message) VALUES (?,?)',
                                           (int(values['oid']), values['message'])).lastrowid
                return {'rpid': ident, 'rpid_str': str(ident), 'need_captcha': False}
            row = connection.execute('SELECT rpid,aid,message FROM comments WHERE rpid=?', (int(values['root']),)).fetchone()
            if row is None:
                return {'root': None}
            return {'root': {'rpid': row[0], 'oid': row[1], 'mid': 42, 'type': 1,
                             'root': 0, 'parent': 0, 'content': {'message': row[2]}}}
