"""Validated opt-in voice recipe and pure frontend helpers; no model imports."""

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

from .voice import VoiceError

PHRASE_POLICIES = ('legacy', 'attach-previous-v1', 'whole-utterance-v1')


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class CandidateConfig:
    candidate_id: str
    source_dir: Path
    source_revision: str
    assets: Mapping[str, Path]
    join_pause_seconds: float
    phrase_policy: str = 'legacy'
    text_language: str = 'zh'

    @classmethod
    def load(cls, path):
        allowed = {'version', 'candidate_id', 'mode', 'device', 'precision', 'pronunciation_policy',
                   'source_dir', 'source_revision', 'join_pause_seconds', 'assets', 'provenance', 'phrase_policy', 'text_language'}
        limits = dict(gpt=512 * 1024**2, sovits=512 * 1024**2,
                      reference_audio=20 * 1024**2, reference_text=16384,
                      backend_config=32768, profile=32768)
        try:
            path = Path(path)
            if path.stat().st_size > 32768:
                raise VoiceError('候选清单过大')
            document = json.loads(path.read_text('utf-8-sig'))
            if (not isinstance(document, dict) or set(document) - allowed
                    or type(document.get('version')) is not int or document['version'] != 1
                    or document.get('mode') != 'direct' or document.get('device') != 'cpu'
                    or document.get('precision') != 'float32'
                    or document.get('pronunciation_policy') != 'directional-v1'):
                raise VoiceError('候选版本、模式或字段不正确')
            phrase_policy = document.get('phrase_policy', 'legacy')
            language = document.get('text_language', 'zh')
            if language not in ('zh', 'ja') or (language == 'ja' and phrase_policy != 'whole-utterance-v1'):
                raise VoiceError('候选朗读语言或分句策略不正确')
            if not isinstance(phrase_policy, str) or phrase_policy not in PHRASE_POLICIES:
                raise VoiceError('候选分句策略不正确')
            identity = document['candidate_id']
            revision = document['source_revision']
            if (not isinstance(identity, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', identity)
                    or not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision)):
                raise VoiceError('候选名称或源码版本不正确')
            source = Path(document['source_dir'])
            if not source.is_absolute() or not (source / 'api_v2.py').is_file():
                raise VoiceError('候选语音源码位置不正确')
            pause = document['join_pause_seconds']
            if type(pause) not in (int, float) or not math.isfinite(pause) or not .1 <= pause <= .5:
                raise VoiceError('候选分句停顿不正确')
            assets = document['assets']
            if not isinstance(assets, dict) or set(assets) != set(limits):
                raise VoiceError('候选资产清单不完整')
            resolved = {}
            for name, limit in limits.items():
                asset = assets[name]
                if not isinstance(asset, dict) or set(asset) != {'path', 'sha256'}:
                    raise VoiceError('候选资产字段不正确')
                location, expected = Path(asset['path']), asset['sha256']
                if (not location.is_absolute() or not location.is_file()
                        or not 0 < location.stat().st_size <= limit
                        or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)
                        or file_digest(location) != expected):
                    raise VoiceError('候选资产不存在、过大或完整性校验失败：' + name)
                resolved[name] = location.resolve()
            return cls(identity, source.resolve(), revision, MappingProxyType(resolved), float(pause), phrase_policy, language)
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, KeyError, TypeError):
            raise VoiceError('候选清单无法读取或缺少有效字段') from None


def split_voice_phrases(text, *, policy='legacy', language='zh'):
    if not isinstance(policy, str) or policy not in PHRASE_POLICIES:
        raise VoiceError('候选分句策略不正确')
    if language not in ('zh', 'ja') or (language == 'ja' and policy != 'whole-utterance-v1'):
        raise VoiceError('朗读语言或分句策略不正确')
    pattern = r'[\u3041-\u30ff]' if language == 'ja' else r'[\u3400-\u9fff]'
    if (not isinstance(text, str) or not text.strip() or len(text) > 300
            or not re.search(pattern, text)
            or any(ord(c) < 32 and c not in '\n\t' for c in text)):
        raise VoiceError('预览文字与所选语言不匹配或超出长度范围')
    if policy == 'whole-utterance-v1':
        if len(text.strip()) > 80:
            raise VoiceError('整句候选每条最多80字，请缩短文本后重试')
        return [text.strip()]
    phrases, endings, pending = [], [], ''
    for part in re.split(r'(?<=[，,；;。！？!?])', text.strip()):
        pending += part
        if len(re.findall(r'[\u3400-\u9fff]', pending)) >= 4:
            phrases.append(pending.strip().rstrip('，,；;').strip())
            endings.append(pending.strip()[-1])
            pending = ''
    if pending.strip():
        if phrases:
            phrases[-1] += pending.strip()
        else:
            phrases.append(pending.strip().rstrip('，,；;'))
            endings.append(pending.strip()[-1])
        endings[-1] = pending.strip()[-1]
    if policy == 'attach-previous-v1':
        merged, boundaries = [], []
        for phrase, ending in zip(phrases, endings):
            preceding = len(re.findall(r'[\u3400-\u9fff]', merged[-1])) if merged else 0
            count = len(re.findall(r'[\u3400-\u9fff]', phrase))
            if (merged and boundaries[-1] in ('，', ',') and ending in ('，', ',')
                    and count == 4 and 6 <= preceding <= 20
                    and not re.search(r'[;；。.!?！？]', merged[-1] + phrase)):
                merged[-1] += phrase
                boundaries[-1] = ending
            else:
                merged.append(phrase)
                boundaries.append(ending)
        phrases = merged
    if not phrases or len(phrases) > 8:
        raise VoiceError('预览分句过多，最多8段')
    return phrases


def clarify_directional_phones(text, phones, counts):
    if (len(text) != len(counts)
            or any(type(count) is not int or count <= 0 for count in counts)
            or sum(counts) != len(phones)):
        raise ValueError('Invalid character-to-phone alignment')
    rules = {'楼下': (['x', 'ia5'], ['x', 'ia4']),
             '桌上': (['sh', 'ang5'], ['sh', 'ang4'])}
    output, changes, offset = list(phones), [], 0
    for index, count in enumerate(counts):
        context = text[max(0, index - 1):index + 1]
        if context in rules:
            weak, full = rules[context]
            current = output[offset:offset + count]
            if current not in (weak, full):
                raise ValueError('Unexpected target phonemes: ' + repr(current))
            if current == weak:
                output[offset:offset + count] = full
                changes.append(dict(context=context, index=index, before=current, after=list(full)))
        offset += count
    return output, changes
