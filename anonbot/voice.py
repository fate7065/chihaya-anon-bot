"""Local Chinese TTS and validated OneBot voice delivery, opt-in only.

Synthesis follows GPT-SoVITS api_v2.py /tts; transport follows OneBot 11 record.
The conversation never controls backend URLs, model weights or reference files.
"""
from dataclasses import dataclass
import hashlib
import io
import json
import math
from pathlib import Path
import re
import urllib.parse
import urllib.request
from uuid import uuid4
import wave

from .gateway import DeliveryError
from .providers import NoRedirect

MAX_AUDIO_BYTES = 4 * 1024 * 1024


class VoiceError(ValueError):
    pass


def inspect_wav(raw: bytes) -> float:
    """Reject compressed, truncated or excessive audio before saving or sending."""
    if not 44 <= len(raw) <= MAX_AUDIO_BYTES:
        raise VoiceError('语音文件大小不合要求')
    try:
        with wave.open(io.BytesIO(raw), 'rb') as audio:
            channels, width, rate, frames, compression, _ = audio.getparams()
            if channels != 1 or width != 2 or compression != 'NONE' or not 8000 <= rate <= 48000:
                raise VoiceError('语音必须为单声道16位PCM WAV')
            seconds = frames / rate
            if not 0.1 <= seconds <= 30:
                raise VoiceError('每段语音须为0.1到30秒')
            if len(audio.readframes(frames)) != frames * width:
                raise VoiceError('语音文件不完整')
            return seconds
    except (wave.Error, EOFError, OSError):
        raise VoiceError('本机语音返回的WAV无法读取') from None


@dataclass(frozen=True)
class VoiceProfile:
    profile_id: str
    endpoint: str
    reference_audio: Path
    output_dir: Path
    prompt_language: str = 'ja'
    prompt_text: str = ''
    timeout: float = 120
    text_language: str = 'zh'

    @classmethod
    def load(cls, path: Path):
        try:
            if path.stat().st_size > 16384:
                raise VoiceError('语音配置文件过大')
            return cls.from_dict(json.loads(path.read_text('utf-8-sig')))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
            raise VoiceError('本地语音配置无法读取') from None

    @classmethod
    def from_dict(cls, data):
        allowed = {'version', 'profile_id', 'endpoint', 'reference_audio', 'output_dir', 'prompt_language', 'prompt_text', 'timeout', 'text_language'}
        if not isinstance(data, dict) or set(data) - allowed or type(data.get('version')) is not int or data['version'] != 1:
            raise VoiceError('语音配置版本或字段不正确')
        try:
            identity = data['profile_id']
            if not isinstance(identity, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', identity):
                raise VoiceError('语音配置名称不正确')
            endpoint = data['endpoint']
            if not isinstance(endpoint, str):
                raise VoiceError('语音服务地址不正确')
            parsed = urllib.parse.urlsplit(endpoint)
            if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1')
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                    or not parsed.port or not 1 <= parsed.port <= 65535
                    or any(c.isspace() or ord(c) < 32 for c in endpoint)):
                raise VoiceError('语音服务只允许本机数字回环HTTP地址和独立端口')
            reference = Path(data['reference_audio'])
            output = Path(data['output_dir'])
            if not reference.is_absolute() or not output.is_absolute():
                raise VoiceError('语音配置中的文件位置必须为绝对路径')
            if not reference.is_file() or not 44 <= reference.stat().st_size <= 20 * 1024 * 1024:
                raise VoiceError('声线参考音频不存在或大小不正确')
            language = data.get('prompt_language', 'ja')
            prompt = data.get('prompt_text', '')
            timeout = data.get('timeout', 120)
            text_language = data.get('text_language', 'zh')
            if text_language not in ('zh', 'ja'):
                raise VoiceError('朗读语言须为中文或日语')
            if language not in ('ja', 'zh') or not isinstance(prompt, str) or len(prompt) > 1000:
                raise VoiceError('参考音频语言或转写不正确')
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 5 <= timeout <= 180:
                raise VoiceError('语音等待时间须为5到180秒')
            return cls(identity, endpoint.rstrip('/'), reference.resolve(), output.resolve(), language, prompt, timeout, text_language)
        except (KeyError, TypeError, OSError):
            raise VoiceError('本地语音配置缺少有效字段') from None
        except ValueError as exc:
            if isinstance(exc, VoiceError):
                raise
            raise VoiceError('本地语音地址或文件位置不正确') from None


@dataclass(frozen=True)
class VoiceClip:
    path: Path
    sha256: str
    seconds: float

    def validate(self, audio_root: Path) -> Path:
        try:
            path = self.path.resolve(strict=True)
            if not path.is_relative_to(audio_root.resolve()) or path.suffix.lower() != '.wav' or not path.is_file():
                raise VoiceError('语音文件超出已配置的生成目录')
            if path.stat().st_size > MAX_AUDIO_BYTES:
                raise VoiceError('语音文件过大')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != self.sha256:
                raise VoiceError('生成语音的完整性校验失败')
            inspect_wav(raw)
            return path
        except OSError:
            raise VoiceError('生成语音不可读取') from None


class LocalSpeech:
    def __init__(self, profile: VoiceProfile):
        self.profile = profile
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def synthesize(self, text: str) -> VoiceClip:
        if self.profile.text_language == 'ja':
            from .voice_candidate import split_voice_phrases
            text = split_voice_phrases(text, policy='whole-utterance-v1', language='ja')[0]
        if (not isinstance(text, str) or not text.strip() or len(text) > 300
                or not re.search(r'[\u3041-\u30ff]' if self.profile.text_language == 'ja' else r'[\u3400-\u9fff]', text)
                or any(ord(c) < 32 and c not in '\n\t' for c in text)):
            raise VoiceError('语音内容须为含中文的1到300字文本，不能含控制字符')
        payload = dict(text=text, text_lang=self.profile.text_language, ref_audio_path=str(self.profile.reference_audio),
                       prompt_lang=self.profile.prompt_language, prompt_text=self.profile.prompt_text,
                       text_split_method='cut5', batch_size=1, media_type='wav', streaming_mode=False,
                       seed=42, parallel_infer=False, speed_factor=1.0)
        request = urllib.request.Request(self.profile.endpoint + '/tts',
                    data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                    headers={'Content-Type': 'application/json', 'Accept': 'audio/wav'})
        try:
            with self.opener.open(request, timeout=self.profile.timeout) as response:
                if response.headers.get('Content-Type', '').split(';')[0].lower() not in ('audio/wav', 'audio/x-wav', 'audio/wave'):
                    raise VoiceError('本机语音没有返回WAV音频')
                raw = response.read(MAX_AUDIO_BYTES + 1)
        except (OSError, ValueError) as exc:
            if isinstance(exc, VoiceError):
                raise
            raise VoiceError('本机语音合成未完成，请检查声线服务；未发送QQ消息') from None
        seconds = inspect_wav(raw)
        digest = hashlib.sha256(raw).hexdigest()
        try:
            self.profile.output_dir.mkdir(parents=True, exist_ok=True)
            path = self.profile.output_dir / (uuid4().hex + '.wav')
            with path.open('xb') as output:
                output.write(raw)
            manifest = dict(version=1, profile_id=self.profile.profile_id, text=text, text_language=self.profile.text_language,
                            reference_language=self.profile.prompt_language, sha256=digest,
                            bytes=len(raw), seconds=seconds, delivery_tracking='qq_outbox_separate')
            with path.with_suffix('.json').open('x', encoding='utf-8') as output:
                json.dump(manifest, output, ensure_ascii=False, indent=2)
            return VoiceClip(path, digest, seconds)
        except OSError:
            raise VoiceError('语音保存失败；未发送QQ消息') from None


class SpeechSender:
    def __init__(self, client, speech: LocalSpeech):
        self.client, self.speech = client, speech
        self.max_model_calls_per_reply = getattr(speech, 'max_model_calls_per_reply', 0)

    def send(self, event, text) -> str:
        try:
            clip = self.speech.synthesize(text)
            clip.validate(self.speech.profile.output_dir)
        except VoiceError:
            raise DeliveryError('failed', '本机语音合成或校验失败，未发送本段QQ消息') from None
        # Do not catch delivery uncertainty or fall back to text; Runtime preserves it.
        return self.client.send_record(event, clip, audio_root=self.speech.profile.output_dir)
