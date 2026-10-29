"""Stage installed OpenJTalk data for Windows native ASCII-relative file access.

pyopenjtalk's lazy downloader resets its dictionary path to an absolute path.
Prepare already-installed data before importing the Japanese frontend instead;
this helper never downloads, modifies packages or changes system settings.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil


DICTIONARY_FILES = ('char.bin', 'COPYING', 'left-id.def', 'matrix.bin', 'pos-id.def',
                    'rewrite.def', 'right-id.def', 'sys.dic', 'unk.dic')
MAX_DICTIONARY_BYTES = 160 * 1024 * 1024


def _digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024*1024), b''):
            result.update(chunk)
    return result.hexdigest()


def prepare_dictionary(source: Path, target: Path, working: Path) -> str:
    try:
        source, target, working = source.resolve(), target.resolve(), working.resolve()
        relative = Path(os.path.relpath(target, working)).as_posix()
        if (not target.is_relative_to(working.parent)
                or not re.fullmatch(r'[A-Za-z0-9_./:-]+', relative)):
            raise RuntimeError('日语词典缓存须在本项目运行目录内，且相对路径只含ASCII字符')
        files = [source/name for name in DICTIONARY_FILES]
        if (not all(p.is_file() and not p.is_symlink() and 0 < p.stat().st_size <= MAX_DICTIONARY_BYTES for p in files)
                or sum(p.stat().st_size for p in files) > MAX_DICTIONARY_BYTES):
            raise RuntimeError('已安装日语词典缺失、为空或过大，请先准备声线环境；未自动下载')
        digests = {p.name: _digest(p) for p in files}
        target.mkdir(parents=True, exist_ok=True)
        for path in files:
            cached = target/path.name
            if not cached.resolve().is_relative_to(target):
                raise RuntimeError('日语词典缓存文件超出本项目目录')
            if cached.exists():
                if not cached.is_file() or cached.stat().st_size != path.stat().st_size or _digest(cached) != digests[path.name]:
                    raise RuntimeError('日语词典缓存校验失败，未覆盖现有文件')
            else:
                with path.open('rb') as input_file, cached.open('xb') as output:
                    shutil.copyfileobj(input_file, output, 1024*1024)
                if _digest(cached) != digests[path.name]:
                    raise RuntimeError('日语词典缓存复制校验失败')
        manifest = target/'manifest.json'
        if not manifest.resolve().is_relative_to(target):
            raise RuntimeError('日语词典缓存清单超出本项目目录')
        manifest.write_text(json.dumps(dict(version=1, files=digests), indent=2), 'utf-8')
        return relative
    except OSError:
        raise RuntimeError('日语词典缓存无法读取或保存，未自动下载或修改安装包') from None


def prepare_installed_dictionary(working: Path) -> str:
    package = importlib.util.find_spec('pyopenjtalk')
    if package is None or not package.origin:
        raise RuntimeError('声线环境缺少日语词典组件pyopenjtalk')
    source = Path(package.origin).parent/'open_jtalk_dic_utf_8-1.11'
    return prepare_dictionary(source, working.parent/'cache/openjtalk', working)
