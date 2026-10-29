"""Portable JSON configuration with independent per-provider secret files."""
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from ..domain import ValidationError


def validate_base(url):
    try:
        p = urlsplit(url)
        host = p.hostname
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == 'localhost'
        if (not host or p.username is not None or p.password is not None or p.query or p.fragment
                or p.port == 0 or any(ord(c) <= 32 for c in url)
                or (p.scheme != 'https' and not (p.scheme == 'http' and loopback))):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('API 地址须使用 HTTPS；HTTP 仅限本机，禁止账号和查询参数') from None


def read_secret(path):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(4097)
        value = raw.decode('ascii').strip()
        if not 1 <= len(value) <= 4096 or len(raw) > 4096 or any(not 33 <= ord(c) <= 126 for c in value):
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError):
        raise ValidationError('密钥文件不可用或格式无效；内容与路径已隐藏') from None


@dataclass(frozen=True, repr=False)
class ApiConfig:
    base_url: str
    model: str
    key_file: Path
    allow_transfer: bool = False

    def __post_init__(self):
        validate_base(self.base_url)
        if (not isinstance(self.model, str) or not 1 <= len(self.model) <= 128
                or any(ord(c) < 32 for c in self.model) or type(self.allow_transfer) is not bool
                or not isinstance(self.key_file, Path)):
            raise ValidationError('模型配置字段无效')


@dataclass(frozen=True, repr=False)
class ReleaseConfig:
    data_dir: Path
    token_file: Path
    origin: str
    embedding: ApiConfig | None
    vision: ApiConfig | None

    def dashboard_token(self):
        token = read_secret(self.token_file)
        if len(token) < 32:
            raise ValidationError('管理口令至少需要 32 个随机字符')
        return token


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


def load_config(path):
    path = Path(path).resolve()
    try:
        with path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        data = strict_json(raw)
        if (type(data) is not dict or set(data) != {'version', 'data_dir', 'token_file', 'origin', 'embedding', 'vision'}
                or type(data['version']) is not int or data['version'] != 1):
            raise ValueError()
        def local(value):
            if not isinstance(value, str) or not value or len(value) > 2048:
                raise ValueError()
            return (path.parent / value).resolve()
        origin = data['origin']
        validate_base(origin)
        parsed = urlsplit(origin)
        if parsed.path or origin != f'{parsed.scheme}://{parsed.netloc}':
            raise ValueError()
        def api(value):
            if value is None:
                return None
            if type(value) is not dict or set(value) != {'base_url', 'model', 'key_file', 'allow_transfer'}:
                raise ValueError()
            return ApiConfig(value['base_url'], value['model'], local(value['key_file']), value['allow_transfer'])
        return ReleaseConfig(local(data['data_dir']), local(data['token_file']), origin,
                             api(data['embedding']), api(data['vision']))
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise ValidationError('发布配置无效；请检查字段与版本，敏感值已隐藏') from None


def init_config(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    token = directory / 'dashboard.key'
    with token.open('x', encoding='ascii') as stream:
        stream.write(secrets.token_urlsafe(32) + '\n')
    os.chmod(token, 0o600)
    path = directory / 'release.json'
    data = dict(version=1, data_dir='state', token_file='dashboard.key',
                origin='http://127.0.0.1:8788', embedding=None, vision=None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    return path
