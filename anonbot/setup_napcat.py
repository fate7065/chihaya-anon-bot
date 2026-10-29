"""Prepare a pinned official portable archive on E:, without executing it."""
import hashlib
import json
import secrets
from pathlib import Path, PurePosixPath
import shutil
import urllib.request
import zipfile
from .domain import ValidationError

VERSION = 'v4.18.38'
SHA256 = 'b88007a373a30deefb387e7cf17911752ff900bce22c534b3da6b644f5c6d0ae'
# Official standalone package: Node and QQ native dependencies are bundled.
# Source: https://github.com/NapNeko/NapCatQQ/releases/tag/v4.18.38
URL = f'https://github.com/NapNeko/NapCatQQ/releases/download/{VERSION}/NapCat.Shell.Windows.Node.zip'


def archive_path(root: Path, name: str) -> Path:
    path = PurePosixPath(name.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts or any(':' in part for part in path.parts):
        raise ValidationError('压缩包包含越界路径，已拒绝解压')
    result = root.joinpath(*path.parts).resolve()
    if not result.is_relative_to(root.resolve()): raise ValidationError('压缩包路径越界')
    return result


def prepare(project: Path | None = None):
    project = project or Path(__file__).resolve().parents[2]
    runtime = project / 'runtime'
    output = runtime / ('NapCat-Node-' + VERSION)
    config_root = output / 'napcat'
    marker = config_root / 'anonbot-package.json'
    required = ('node.exe', 'index.js', 'wrapper.node', 'napcat.bat',
                'napcat/napcat.mjs', 'napcat/loadNapCat.js')
    if output.exists():
        if marker.is_file() and json.loads(marker.read_text())['sha256'] == SHA256 and all((output / name).is_file() for name in required):
            return config_root
        raise ValidationError('目标目录已存在且来源不明，保留原文件，请人工检查')
    downloads = runtime / 'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / ('NapCat.Shell.Windows.Node-' + VERSION + '.zip')
    if not archive.exists():
        part = archive.with_suffix('.part')
        with urllib.request.urlopen(urllib.request.Request(URL, headers={'User-Agent': 'AnonBot-Setup'}), timeout=30) as response, part.open('wb') as stream:
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 180 * 1024 * 1024: raise ValidationError('安装包超过预期大小')
                stream.write(chunk)
        part.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise ValidationError('NapCat 安装包 SHA-256 与官方发布摘要不符，保留文件但拒绝运行')
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if len(entries) > 10000 or sum(item.file_size for item in entries) > 600 * 1024 * 1024:
            raise ValidationError('安装包内容大小超出限制')
        for item in entries:
            archive_path(output, item.filename)
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValidationError('安装包包含符号链接，已拒绝解压')
        if not set(required).issubset({item.filename for item in entries if not item.is_dir()}):
            raise ValidationError('独立运行包缺少 Node 或 QQ 入口依赖')
        output.mkdir(parents=True)
        for item in entries:
            destination = archive_path(output, item.filename)
            if item.is_dir(): destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(item) as source, destination.open('xb') as target: shutil.copyfileobj(source, target)
    marker.write_text(json.dumps({'version': VERSION, 'sha256': SHA256, 'source': URL}, indent=2), encoding='utf-8')
    return config_root


def export_config(settings, credentials, output: Path):
    """Explicit local setup action; no existing account config is overwritten."""
    settings.validate()
    token, webhook = credentials.get('gateway_token', ''), credentials.get('webhook_secret', '')
    if not token or len(webhook) < 16 or (settings.private_mode == 'allowlist' and not settings.allowed_users):
        raise ValidationError('请先保存机器人 QQ、白名单及两个接入密钥')
    from urllib.parse import urlsplit
    url = urlsplit(settings.gateway_url)
    if url.hostname != '127.0.0.1': raise ValidationError('自动配置暂只支持 127.0.0.1，其他地址请在 NapCat 中手动配置')
    marker = output / 'anonbot-package.json'
    if not marker.is_file() or json.loads(marker.read_text()).get('sha256') != SHA256:
        raise ValidationError('NapCat 目录缺少本项目下载标识')
    config = output / 'config'
    target = config / ('onebot11_' + str(settings.bot_id) + '.json')
    if target.exists(): raise ValidationError('NapCat 账号配置已存在，保留原配置；请用 WebUI 核对更新')
    data = {'network': {'httpServers': [{'name': 'anonbot-api', 'enable': True, 'host': '127.0.0.1',
        'port': url.port or 80, 'token': token, 'messagePostFormat': 'array', 'debug': False}],
        'httpClients': [{'name': 'anonbot-events', 'enable': True,
            'url': f'http://127.0.0.1:{settings.port}/events', 'token': webhook,
            'messagePostFormat': 'array', 'reportSelfMessage': False, 'debug': False}],
        'websocketServers': [], 'websocketClients': []}, 'musicSignUrl': '', 'enableLocalFile2Url': False, 'parseMultMsg': False}
    config.mkdir(exist_ok=True)
    # 'x' avoids overwriting a user-maintained config even if it appears concurrently.
    with target.open('x', encoding='utf-8') as stream: json.dump(data, stream, ensure_ascii=False, indent=2)
    webui = config / 'webui.json'
    if not webui.exists():
        with webui.open('x', encoding='utf-8') as stream:
            json.dump({'host': '127.0.0.1', 'port': 6099, 'token': secrets.token_urlsafe(32), 'loginRate': 3}, stream, indent=2)
    return target


if __name__ == '__main__':
    try: print(json.dumps({'prepared': str(prepare()), 'executed': False}, ensure_ascii=True))
    except (OSError, ValueError, zipfile.BadZipFile):
        print('NapCat 准备失败；未执行安装包。请检查网络、空间及包摘要。')
        raise SystemExit(2)
