"""Opt-in model configuration, separate from the live private-chat settings."""
import math
import os
import re

from ..domain import ValidationError
from ..providers import CompatibleProvider
from .group import DecisionCouncil, ModelJudge, RuleJudge
from .jsonio import fields, read_json


DIMENSIONS = ('address', 'relevance', 'timing')


def number(value, minimum, maximum):
    if (type(value) not in (int, float) or not minimum <= value <= maximum
            or not math.isfinite(value)):
        raise ValidationError('数值配置超出范围')
    return value


def label(value, maximum=128):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or not value.strip() or any(ord(char) < 32 for char in value)):
        raise ValidationError('配置标识必须是有界非空文本')
    return value


class JudgeProvider(CompatibleProvider):
    def __init__(self, base_url, model, api_key, *, timeout_seconds=4):
        super().__init__(base_url, model, api_key)
        self.timeout = number(timeout_seconds, 0.1, 30)
        self.complete_only = True

    def request_body(self, messages):
        return {**super().request_body(messages), 'temperature': 0, 'max_tokens': 160}


def load_council(path=None, *, allow_model_calls=False, environ=None):
    """Construct providers only; evaluate performs calls when explicitly enabled.

    Three roles may share weights. They are not claimed to be independent models.
    timeout_seconds is a socket timeout, not an end-to-end council deadline.
    """
    if type(allow_model_calls) is not bool:
        raise ValidationError('allow_model_calls 必须是布尔值')
    if path is None:
        return DecisionCouncil([RuleJudge(dimension) for dimension in DIMENSIONS])
    config = read_json(path)
    fields(config, ('version', 'judges'), ('threshold', 'cooldown_seconds'))
    if type(config['version']) is not int or config['version'] != 1:
        raise ValidationError('不支持的判断器配置版本')
    threshold = number(config.get('threshold', 0.7), 0.01, 1)
    cooldown = number(config.get('cooldown_seconds', 3), 0, 3600)
    entries = config['judges']
    if not isinstance(entries, list) or len(entries) != 3:
        raise ValidationError('需要且只允许三个判断器')
    seen = set()
    for entry in entries:
        fields(entry, ('dimension', 'type'),
               ('provider_id', 'base_url', 'model', 'key_env', 'timeout_seconds'))
        dimension = label(entry['dimension'])
        kind = label(entry['type'])
        if dimension not in DIMENSIONS or dimension in seen:
            raise ValidationError('判断维度必须唯一且齐全')
        seen.add(dimension)
        if kind == 'rule':
            fields(entry, ('dimension', 'type'))
        elif kind == 'compatible':
            fields(entry, ('dimension', 'type', 'provider_id', 'base_url', 'model', 'key_env'),
                   ('timeout_seconds',))
            if not allow_model_calls:
                raise ValidationError('模型配置需要显式 --allow-model-calls；默认不联网')
            for name in ('provider_id', 'model', 'key_env'):
                label(entry[name])
            label(entry['base_url'], 2048)
            if not re.fullmatch(r'[A-Z_][A-Z0-9_]{0,127}', entry['key_env']):
                raise ValidationError('key_env 必须是环境变量名，不能填写密钥')
            number(entry.get('timeout_seconds', 4), 0.1, 30)
        else:
            raise ValidationError('不支持的判断器类型')
    environment = os.environ if environ is None else environ
    judges = []
    for entry in entries:
        dimension = entry['dimension']
        if entry['type'] == 'rule':
            judges.append(RuleJudge(dimension))
            continue
        key = environment.get(entry['key_env'], '')
        if not isinstance(key, str) or not key:
            raise ValidationError('模型所需环境变量未设置；不会读取已有聊天密钥')
        provider = JudgeProvider(entry['base_url'], entry['model'], key,
                                 timeout_seconds=entry.get('timeout_seconds', 4))
        judges.append(ModelJudge(dimension, provider, entry['provider_id']))
    return DecisionCouncil(judges, threshold=threshold, cooldown_seconds=cooldown)
