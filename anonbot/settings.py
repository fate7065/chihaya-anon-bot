"""Local public settings and Windows account-bound DPAPI secrets.

DPAPI: https://learn.microsoft.com/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata
No other application's credential files are searched.
"""
import base64
import ctypes
from dataclasses import asdict, dataclass, fields
import json
import os
from pathlib import Path
import tempfile
from .domain import ValidationError, numeric_id

DATA = Path(__file__).resolve().parent.parent / 'data'


def _crypt(data: bytes, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise ValidationError('本地密钥保存需要 Windows DPAPI；其他系统请用环境变量')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ValidationError('本地密钥无法解密或保存，请在当前 Windows 账号重新配置')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def protect(text: str) -> str:
    return base64.b64encode(_crypt(text.encode('utf-8'))).decode('ascii')


def unprotect(text: str) -> str:
    try:
        return _crypt(base64.b64decode(text, validate=True), True).decode('utf-8')
    except (ValueError, UnicodeError):
        raise ValidationError('本地加密凭据格式损坏，请重新配置') from None


@dataclass(frozen=True)
class Settings:
    model: str = 'deepseek-flash'
    base_url: str = 'https://api.deepseek.com'
    bot_id: str = '10000'
    allowed_users: tuple[str, ...] = ()
    private_mode: str = 'allowlist'
    gateway_url: str = 'http://127.0.0.1:3000'
    port: int = 8765
    queue_limit: int = 100
    calls_per_hour: int = 60
    context_chars: int = 16000

    def validate(self):
        from .providers import CompatibleProvider
        numeric_id(self.bot_id, '机器人 QQ')
        if self.private_mode not in ('friends', 'allowlist'):
            raise ValidationError('私聊范围须为 friends 或 allowlist')
        if not isinstance(self.allowed_users, (list, tuple)) or len(self.allowed_users) > 50:
            raise ValidationError('私聊白名单最多 50 个用户')
        for value in self.allowed_users: numeric_id(value, '白名单 QQ')
        for value, lo, hi in ((self.port, 1, 65535), (self.queue_limit, 1, 1000),
                              (self.calls_per_hour, 1, 1000), (self.context_chars, 2000, 32000)):
            if type(value) is not int or not lo <= value <= hi:
                raise ValidationError('端口、积压、调用上限或上下文预算超出范围')
        if not isinstance(self.model, str) or not 1 <= len(self.model) <= 100:
            raise ValidationError('模型名称长度无效')
        CompatibleProvider(self.base_url, self.model, 'validation-only')
        # Gateway credentials must never leave the local machine.
        from urllib.parse import urlsplit
        url = urlsplit(self.gateway_url)
        if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.path not in ('', '/') or url.query or url.fragment or url.username or url.password:
            raise ValidationError('QQ 接入端须为本机 HTTP 地址，且不含路径、账号或查询参数')
        try: url.port
        except ValueError: raise ValidationError('QQ 接入端端口无效') from None


def _write_json(path: Path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix='.anonbot-', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(content, stream, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def save_settings(settings: Settings, secrets: dict[str, str], root: Path = DATA):
    settings.validate()
    if not isinstance(secrets, dict) or set(secrets) - {'deepseek_api_key', 'gateway_token', 'webhook_secret'}:
        raise ValidationError('凭据名称无效')
    for key, value in secrets.items():
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValidationError('密钥须为无空白的 ASCII 文本')
    encrypted = protect(json.dumps(secrets))
    _write_json(root / 'secrets.dpapi', {'version': 1, 'protected': encrypted})
    _write_json(root / 'settings.json', asdict(settings))


def load_settings(root: Path = DATA, with_secrets=False):
    try:
        public = root / 'settings.json'
        data = json.loads(public.read_text(encoding='utf-8')) if public.exists() else {}
        if not isinstance(data, dict) or set(data) - {field.name for field in fields(Settings)}:
            raise ValidationError('本地配置字段无效')
        if public.exists() and 'private_mode' not in data:
            data['private_mode'] = 'allowlist' if data.get('allowed_users') else 'friends'
        settings = Settings(**data)
        settings.validate()
        secrets = {}
        if with_secrets and (root / 'secrets.dpapi').exists():
            protected = json.loads((root / 'secrets.dpapi').read_text(encoding='utf-8'))
            secrets = json.loads(unprotect(protected['protected']))
            if not isinstance(secrets, dict) or any(not isinstance(v, str) for v in secrets.values()):
                raise ValidationError('本地凭据格式无效')
        return settings, secrets
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        if isinstance(exc, ValidationError): raise
        raise ValidationError('本地配置不可读或格式损坏，请重新配置') from None
