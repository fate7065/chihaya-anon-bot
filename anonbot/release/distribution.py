"""Deterministic source bundle with a positive allowlist; never bundles runtime data."""
import hashlib
import json
from pathlib import Path
import zipfile

from ..domain import ValidationError
from . import VERSION

TOP_FILES = {'pyproject.toml', 'start-activity.cmd', '.dockerignore'}
PERSONAS = {'anonbot/persona_anon.json', 'anonbot/persona_anon_natural.json'}


def allowed(relative):
    parts = relative.parts
    if any(p.startswith('.') for p in parts) and relative.as_posix() != '.dockerignore':
        return False
    if relative.as_posix() in TOP_FILES | PERSONAS:
        return True
    if parts[:2]==('knowledge','anon') and relative.name in ('corpus.json','unknown_claims.json'):
        return True
    if parts[0] == 'anonbot':
        return relative.suffix == '.py' or (parts[1:-1] in [('release', 'web'), ('autonomy', 'console_web')]
                                            and relative.suffix in ('.html', '.css', '.js'))
    return (parts[:2] == ('docs', 'release') and relative.suffix == '.md') or (
        parts[0] == 'deploy' and relative.name in {'Dockerfile', 'compose.yaml', 'Caddyfile.example',
                                                 'requirements-server.txt', 'worker.example.json', 'release.example.json'})


def build_bundle(root, destination):
    root, destination = Path(root).resolve(), Path(destination)
    files, content = [], {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if not allowed(relative) or not path.is_file():
            continue
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root.parent):
            raise ValidationError('发行文件不能通过符号链接引入其他目录')
        if not path.resolve().is_relative_to(root) or path.stat().st_size > 2_000_000:
            raise ValidationError('发行文件越界或过大')
        data = path.read_bytes()
        content[relative.as_posix()] = data
        files.append({'path': relative.as_posix(), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    if not files or 'pyproject.toml' not in content:
        raise ValidationError('发行源目录不完整')
    manifest = {'version': VERSION, 'status': 'release_candidate', 'production_accepted': False,
                'file_count': len(files), 'files': files,
                'excluded': ['chat_data', 'secrets', 'voice_weights', 'third_party_runtime', 'private_reference_documents', 'audit_records']}
    content['RELEASE-MANIFEST.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(content.items()):
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return manifest
