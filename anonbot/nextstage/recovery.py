"""Verified synthetic SQLite snapshots and explicitly incomplete erasure inventory."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .common import canonical, fingerprint, identity
from ..domain import EventConflict, ValidationError


MEMORY_KINDS=('memory-batch','memory-proposal','memory-review','reflection')


def _inspect(db):
    if db.execute('PRAGMA page_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0]>32*1024*1024:
        raise ValidationError('逻辑数据库超过首轮32MiB上限')
    if db.execute('SELECT version,origin FROM nextstage_meta').fetchall()!=[(1,'synthetic')]:
        raise ValidationError('只备份独立合成日志')
    if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValidationError('数据库完整性检查失败')
    rows=db.execute('SELECT seq,scope,kind,event_id,payload FROM nextstage_events ORDER BY seq LIMIT 20001').fetchall()
    if len(rows)>20000:raise ValidationError('首轮备份记录数超过上限')
    return dict(row_count=len(rows),logical_sha256=fingerprint(rows))


def _readonly(path):
    path=Path(path).resolve()
    if not path.is_file() or not 1<=path.stat().st_size<=32*1024*1024:raise ValidationError('备份数据库大小无效')
    return sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)


def backup_database(source,directory):
    source=Path(source).resolve();directory=Path(directory)
    if directory.exists():raise ValidationError('备份输出须为新目录')
    with closing(_readonly(source)) as original:
        _inspect(original)
        directory.mkdir(parents=True,exist_ok=False);target=directory/'snapshot.sqlite3'
        with closing(sqlite3.connect(target)) as copy:
            original.backup(copy)
            logical=_inspect(copy)
    manifest=dict(schema='nextstage-backup-v1',origin='synthetic',sha256=sha256(target.read_bytes()).hexdigest(),**logical)
    with (directory/'manifest.json').open('x',encoding='utf-8') as stream:stream.write(canonical(manifest)+'\n')
    return dict(verified=True,**manifest)


def restore_database(directory,destination):
    directory=Path(directory);destination=Path(destination)
    if destination.exists():raise ValidationError('恢复目标必须不存在')
    manifest_file=directory/'manifest.json'
    if manifest_file.stat().st_size>4096:raise ValidationError('备份清单超长')
    manifest=json.loads(manifest_file.read_text(encoding='utf-8'));source=directory/'snapshot.sqlite3'
    if type(manifest) is not dict or manifest.get('schema')!='nextstage-backup-v1' or manifest.get('origin')!='synthetic':
        raise ValidationError('备份清单无效')
    with closing(_readonly(source)) as original:
        if sha256(source.read_bytes()).hexdigest()!=manifest.get('sha256'):raise ValidationError('备份文件摘要不符')
        logical=_inspect(original)
        if any(logical[k]!=manifest.get(k) for k in logical):raise ValidationError('备份内容核对失败')
        destination.parent.mkdir(parents=True,exist_ok=True)
        with destination.open('xb'):pass
        with closing(sqlite3.connect(destination)) as copy:
            original.backup(copy)
            if _inspect(copy)!=logical:raise ValidationError('恢复后内容不一致')
    return dict(verified=True,**logical)


def forget_inventory(journal,scope,db=None):
    if db is None:
        with journal.transaction() as connection:return forget_inventory(journal,scope,connection)
    rows=[]
    for kind in MEMORY_KINDS:
        for row in journal.events(scope,kind,db):
            rows.append(dict(kind=kind,event_id=row['event_id'],payload_sha256=fingerprint(row['data'])))
    return dict(scope_sha256=sha256(scope.key.encode()).hexdigest(),records=rows,physical_erasure_complete=False,
                remaining=['SDK evidence and claims','existing history/indexes','companion/proactive state',
                           'annotation packages','backups/exports','SQLite freed pages'])


def erase_candidate_rows(journal,scope,operation_id,preview_fingerprint):
    identity(operation_id)
    with journal.transaction() as db:
        old=next((r['data'] for r in journal.events(scope,'forget-audit',db) if r['event_id']==operation_id),None)
        if old is not None:
            if old['fingerprint']!=preview_fingerprint:raise EventConflict('遗忘操作内容冲突')
            return dict(duplicate=True,**old)
        current=forget_inventory(journal,scope,db)
        if fingerprint(current)!=preview_fingerprint:raise EventConflict('遗忘清单已变化')
        for kind in MEMORY_KINDS:db.execute('DELETE FROM nextstage_events WHERE scope=? AND kind=?',(scope.key,kind))
        receipt=dict(fingerprint=preview_fingerprint,removed_rows=len(current['records']),
                     physical_erasure_complete=False,remaining=current['remaining'])
        journal.append(scope,'forget-audit',operation_id,receipt,db=db)
    return dict(duplicate=False,**receipt)
