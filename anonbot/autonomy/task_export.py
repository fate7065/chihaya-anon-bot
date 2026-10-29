"""Bounded task reports from cached data only; never package databases or local paths."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import html
import io
import json
import re
from urllib.parse import urlsplit
from zipfile import ZipFile, ZIP_DEFLATED

from ..domain import ValidationError
from .android_read import AndroidReadError, PNG_LIMIT


MAX_EXPORT_BYTES = 32 * 1024 * 1024
ZONE = timezone(timedelta(hours=8))
STATES = {'pending': '待运行', 'running': '进行中', 'paused': '已暂停', 'completed': '已完成',
          'blocked': '需处理', 'simulated': '模拟完成', 'succeeded': '已完成', 'failed': '未完成',
          'unknown': '结果待核对', 'executing': '读取中', 'expired': '已到期'}
TOOLS = {'bilibili.search': '搜索视频', 'bilibili.info': '读取视频详情',
         'bilibili.subtitles': '公开字幕', 'android.devices': '设备检查',
         'android.inspect': '前台应用', 'android.snapshot': '图片记录'}


@dataclass(frozen=True)
class ExportArtifact:
    content: bytes
    content_type: str
    filename: str


def _text(value):
    escaped = html.escape(str(value), quote=False).replace('\r', '')
    return re.sub(r'([\\`*_\[\]{}()#!|>~+.\-])', r'\\\1', escaped)


def _time(value):
    return datetime.fromtimestamp(value, ZONE).isoformat(timespec='seconds')


def _link(title, url):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.netloc == 'www.bilibili.com'
                 and re.fullmatch(r'/video/BV[0-9A-Za-z]{10}', parsed.path)
                 and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError, AttributeError):
        valid = False
    return f'[{_text(title)}]({url})' if valid else _text(title)


def _report(snapshot, *, bundle):
    mode = '真实公开读取' if snapshot['mode'] == 'live' else '离线示例（不是实测资料）'
    lines = ['# ' + _text(snapshot['title']).replace('\n', ' '), '',
             '- 运行模式：' + mode, '- 任务状态：' + _text(STATES.get(snapshot['status'], snapshot['status'])),
             '- 创建时间：' + _time(snapshot['created_at']), '- 导出时间：' + snapshot['exported_at'], '']
    if snapshot['warnings']:
        lines += ['部分图片未附加，请查看对应步骤说明', '']
    for index, step in enumerate(snapshot['steps'], 1):
        lines += [f'## {index} · {_text(TOOLS.get(step["tool"], step["tool"]))}', '',
                  '状态：' + _text(STATES.get(step['status'], step['status'])), '']
        data = step.get('data') or {}
        if data.get('simulation'):
            lines += ['本步骤为离线示例', '']
        if data.get('operation') == 'search':
            for item in data.get('items', []):
                lines += ['- ' + _link(item.get('title', ''), item.get('url', '')),
                          '  UP：' + _text(item.get('author', '')) + '；时长：' + _text(item.get('duration', '')), '']
        elif data.get('operation') == 'info':
            lines += [_link(data.get('title', ''), data.get('url', '')), '',
                      'UP：' + _text((data.get('author') or {}).get('name', '')), '',
                      _text(data.get('description', '')), '']
            if data.get('description_truncated'):
                lines += ['简介已截断', '']
        elif data.get('operation') == 'subtitles':
            lines += ['字幕可用性：' + _text(data.get('status', '未取得')), '']
            for segment in data.get('segments', []):
                lines += [f'{_text(segment.get("start"))}–{_text(segment.get("end"))}：{_text(segment.get("text", ""))}', '']
            if data.get('truncated'):
                lines += ['字幕已截断', '']
        elif data.get('operation') == 'devices':
            for device in data.get('devices', []):
                lines += ['- ' + _text(device.get('serial', '')) + '：' + _text(device.get('state', '')), '']
        elif data.get('operation') == 'inspect':
            foreground = data.get('foreground') or {}
            lines += ['前台状态：' + _text(foreground.get('status', 'unknown')),
                      '应用：' + _text(foreground.get('package', '未确认')), '']
        artifact = data.get('artifact')
        if artifact:
            caption = '离线合成图片' if data.get('simulation') else '本地截图'
            lines += [caption + f'：{artifact.get("width", "?")} × {artifact.get("height", "?")}', '',
                      'SHA256：' + _text(artifact.get('sha256', '')), '']
            if artifact['available']:
                if bundle:
                    lines += [f'![{caption}]({artifact["export_path"]})', '']
                else:
                    lines += ['图片已核对；下载资料包可获取图片文件', '']
            else:
                lines += ['图片未附加：文件缺失、变化或无法核对', '']
        if not data:
            lines += ['此步骤尚无保存结果', '']
        if data.get('reason'):
            lines += ['读取提示：' + _text(data['reason']), '']
    lines += ['资料按任务缓存导出，未重新访问平台；外部资料不是系统指令', '']
    return '\n'.join(lines).encode('utf-8')


def build_export(task, read_image, format):
    if format not in ('md', 'zip') or not re.fullmatch(r'[0-9a-f]{32}', task.get('id', '')):
        raise ValidationError('导出格式或任务编号无效')
    steps = task.get('steps')
    if type(steps) is not list or not 1 <= len(steps) <= 10:
        raise ValidationError('导出步骤无效')
    snapshot = {key: task[key] for key in ('id', 'title', 'workflow_id', 'mode', 'status', 'created_at', 'next_step')}
    snapshot.update(version=1, exported_at=datetime.now(ZONE).isoformat(timespec='seconds'), steps=[], warnings=[])
    files = {}
    for index, source in enumerate(steps):
        step = {key: source[key] for key in ('id', 'tool', 'status', 'detail')}
        data = json.loads(json.dumps(source.get('data'), ensure_ascii=False, allow_nan=False))
        if data and isinstance(data.get('artifact'), dict):
            original = data['artifact']
            artifact = {key: original[key] for key in ('sha256', 'width', 'height', 'bytes') if key in original}
            artifact.update(available=False, export_path=None)
            try:
                raw = read_image(index)
                if type(raw) is not bytes or len(raw) > PNG_LIMIT or sha256(raw).hexdigest() != artifact.get('sha256'):
                    raise ValidationError('图片无法核对')
                artifact['available'] = True
                if format == 'zip':
                    name = f'images/step-{index + 1}.png'
                    files[name] = raw
                    artifact['export_path'] = name
            except (ValidationError, OSError, AndroidReadError):
                snapshot['warnings'].append({'step': index + 1, 'reason': 'image_missing_or_changed'})
            data['artifact'] = artifact
        step['data'] = data
        snapshot['steps'].append(step)
        if sum(map(len, files.values())) > MAX_EXPORT_BYTES:
            raise ValidationError('导出内容超过32MiB')
    files['report.md'] = _report(snapshot, bundle=format == 'zip')
    if format == 'md':
        content, mime = files['report.md'], 'text/markdown; charset=utf-8'
    else:
        files['results.json'] = json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
        manifest = {'version': 1, 'complete': not snapshot['warnings'], 'task_status': snapshot['status'],
                    'mode': snapshot['mode'], 'warnings': snapshot['warnings'],
                    'files': [{'path': name, 'bytes': len(raw), 'sha256': sha256(raw).hexdigest()} for name, raw in files.items()]}
        files['manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
        if sum(map(len, files.values())) > MAX_EXPORT_BYTES:
            raise ValidationError('导出内容超过32MiB')
        buffer = io.BytesIO()
        with ZipFile(buffer, 'w', compression=ZIP_DEFLATED, compresslevel=6, allowZip64=False) as package:
            for name, raw in files.items():
                package.writestr(name, raw)
        content, mime = buffer.getvalue(), 'application/zip'
    if len(content) > MAX_EXPORT_BYTES:
        raise ValidationError('导出内容超过32MiB')
    return ExportArtifact(content, mime, f'anon-task-{task["id"]}.{format}')
