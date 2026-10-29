"""Small immutable contracts shared across adapters and the engine."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
from typing import Protocol


class ValidationError(ValueError):
    pass


class EventConflict(ValidationError):
    pass


class ProviderError(RuntimeError):
    """A sanitized provider failure, safe to show in the console."""


def numeric_id(value: object, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValidationError(f'{name} 必须是数字 ID')
    result = str(value)
    if not re.fullmatch(r'[1-9][0-9]{0,19}', result):
        raise ValidationError(f'{name} 必须是正整数 ID（最多 20 位）')
    return result


@dataclass(frozen=True)
class Scope:
    bot_id: str
    persona_id: str
    owner_id: str
    scene: str = 'private'

    def __post_init__(self):
        object.__setattr__(self, 'bot_id', numeric_id(self.bot_id, 'bot_id'))
        object.__setattr__(self, 'owner_id', numeric_id(self.owner_id, 'owner_id'))
        if not isinstance(self.persona_id, str) or not re.fullmatch(r'[a-z0-9_-]{1,64}', self.persona_id):
            raise ValidationError('persona_id 格式无效')
        if not isinstance(self.scene, str) or not re.fullmatch(r'private|group:[1-9][0-9]{0,19}', self.scene):
            raise ValidationError('scene 必须是 private 或 group:数字ID')

    @property
    def visibility(self) -> str:
        return 'private' if self.scene == 'private' else 'group'

    @property
    def key(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class MessageEvent:
    event_id: str
    scope: Scope
    text: str
    mentioned: bool = False
    source: str = 'console'

    def __post_init__(self):
        if not isinstance(self.scope, Scope):
            raise ValidationError('scope 必须是 Scope')
        if not isinstance(self.event_id, str) or not 1 <= len(self.event_id) <= 128:
            raise ValidationError('event_id 长度必须为 1–128')
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 4000:
            raise ValidationError('消息须为 1–4000 个字符且非空白')
        if not isinstance(self.mentioned, bool) or self.source not in ('console', 'onebot'):
            raise ValidationError('事件来源或 mentioned 无效')

    @property
    def key(self) -> str:
        return json.dumps([self.scope.key, self.source, self.event_id], ensure_ascii=False)

    @property
    def payload_hash(self) -> str:
        payload = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)
        return sha256(payload.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: int
    scope: Scope
    content: str
    created_at: str


@dataclass(frozen=True)
class ReplyDecision:
    reply: bool
    reason: str


@dataclass(frozen=True)
class ReplyResult:
    status: str  # replied, skipped, duplicate, failed, pending
    reason: str
    text: str = ''


class ModelProvider(Protocol):
    def generate(self, messages: list[dict[str, str]]) -> str: ...
