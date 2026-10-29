"""Freeze and recheck a bounded local video draft; no upload transport or credentials."""
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from .common import identity, text, fingerprint
from ..domain import ValidationError


def freeze_media(path,root):
    root=Path(root).resolve();path=Path(path).resolve()
    if not path.is_relative_to(root) or path==root or not path.is_file() or path.suffix.lower()!='.mp4':
        raise ValidationError('媒体须为指定目录内的MP4文件')
    before=path.stat()
    if not 12<=before.st_size<=64*1024*1024:raise ValidationError('首轮媒体大小须为12字节–64MiB')
    digest=sha256();size=0
    with path.open('rb') as stream:
        header=stream.read(12)
        if header[4:8]!=b'ftyp':raise ValidationError('不是支持的MP4封装头')
        digest.update(header);size=len(header)
        while chunk:=stream.read(1024*1024):
            size+=len(chunk)
            if size>64*1024*1024:raise ValidationError('读取时媒体超出上限')
            digest.update(chunk)
    after=path.stat()
    if size!=before.st_size or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise ValidationError('媒体读取期间发生变化')
    return dict(path=str(path),root=str(root),size=size,sha256=digest.hexdigest(),container_header_only=True)


class VideoDrafts:
    def __init__(self,journal):self.journal=journal

    def prepare(self,scope,draft_id,path,root,metadata):
        identity(draft_id)
        if type(metadata) is not dict or metadata.keys()!={'title','description','tags','category','copyright','source'}:
            raise ValidationError('投稿草稿字段无效')
        text(metadata['title'],80)
        if (not isinstance(metadata['description'],str) or len(metadata['description'])>2000
                or type(metadata['category']) is not int or not 1<=metadata['category']<=10000
                or type(metadata['copyright']) is not int or metadata['copyright'] not in (1,2)
                or type(metadata['tags']) is not list or not 1<=len(metadata['tags'])<=10):raise ValidationError('投稿元数据无效')
        for tag in metadata['tags']:text(tag,20)
        if not isinstance(metadata['source'],str) or len(metadata['source'])>500:raise ValidationError('转载来源无效')
        if metadata['copyright']==2:
            parsed=urlsplit(metadata['source'])
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
                raise ValidationError('转载须明确HTTPS来源')
        media=freeze_media(path,root)
        data=dict(media=media,metadata=metadata,origin='synthetic',state='draft',uploaded=False)
        self.journal.append(scope,'video-draft',draft_id,data)
        return {**data,'fingerprint':fingerprint(data)}

    def verify(self,scope,draft_id):
        identity(draft_id)
        draft=next((r['data'] for r in self.journal.events(scope,'video-draft') if r['event_id']==draft_id),None)
        if draft is None:raise ValidationError('当前作用域不存在该草稿')
        if freeze_media(draft['media']['path'],draft['media']['root'])!=draft['media']:
            raise ValidationError('投稿文件已变化')
        return dict(file_unchanged=True,fingerprint=fingerprint(draft),uploaded=False,
                    remaining=['real media decoding','account and upload protocol','platform receipt'])
