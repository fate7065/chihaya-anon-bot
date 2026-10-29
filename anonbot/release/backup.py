"""Consistent backup/restore of the isolated activity DB, never of chat history."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from ..activity import ActivityLog
from ..domain import ValidationError
from .config import strict_json

MAX_SIZE = 128 * 1024 * 1024


def digest(path):
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_SIZE:
        raise ValidationError('活动备份不存在或超过大小限制')
    result = hashlib.sha256()
    with path.open('rb') as stream:
        total = 0
        for chunk in iter(lambda: stream.read(65536), b''):
            total += len(chunk)
            if total > MAX_SIZE:
                raise ValidationError('活动备份超过大小限制')
            result.update(chunk)
    return result.hexdigest()


def inspect(path):
    digest(path)
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if (tables != {'activity_meta', 'network_activity'} or
                db.execute('SELECT version FROM activity_meta').fetchall() != [(1,)] or
                db.execute('PRAGMA quick_check').fetchall() != [('ok',)]):
            raise ValidationError('备份不是受支持的完整活动库')
        if db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0] > MAX_SIZE:
            raise ValidationError('活动备份逻辑大小超过限制')
        return db.execute('SELECT COUNT(*) FROM network_activity').fetchone()[0]


def backup_activity(source, directory):
    source, directory = Path(source), Path(directory)
    inspect(source)
    directory.mkdir(parents=True, exist_ok=False)
    target = directory / 'activity.sqlite3'
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
    count = inspect(target)
    manifest = {'version': 1, 'file': 'activity.sqlite3', 'sha256': digest(target), 'rows': count}
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', 'utf-8')
    return manifest


def restore_activity(directory, destination):
    directory, destination = Path(directory), Path(destination)
    with (directory / 'manifest.json').open('rb') as stream:
        raw = stream.read(4097)
    try:
        manifest = strict_json(raw)
        if (len(raw) > 4096 or type(manifest) is not dict
                or set(manifest) != {'version', 'file', 'sha256', 'rows'}
                or type(manifest['version']) is not int or manifest['version'] != 1
                or manifest['file'] != 'activity.sqlite3'
                or type(manifest['rows']) is not int or not 0 <= manifest['rows'] <= 100000):
            raise ValueError()
        source = directory / 'activity.sqlite3'
        # The copied bytes are hashed again; a changed source cannot become accepted output.
        if digest(source) != manifest['sha256'] or inspect(source) != manifest['rows']:
            raise ValueError()
    except (ValueError, KeyError, TypeError, RecursionError, sqlite3.Error):
        raise ValidationError('活动备份清单、内容或校验和无效') from None
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as output, source.open('rb') as stream:
        copied = hashlib.sha256()
        total = 0
        for chunk in iter(lambda: stream.read(65536), b''):
            total += len(chunk)
            if total > MAX_SIZE:
                raise ValidationError('恢复输入超过大小限制；目标为未验收文件')
            output.write(chunk)
            copied.update(chunk)
    if copied.hexdigest() != manifest['sha256'] or inspect(destination) != manifest['rows']:
        raise ValidationError('恢复结果未通过校验；目标为未验收文件，请勿启用')
    return manifest
