"""Strict bounded JSON for local replay and configuration boundaries."""
import json
import math

from ..domain import ValidationError


def strict_json(text: str, limit: int = 65536):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result

    def constant(_value):
        raise ValueError('non-finite number')

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError('floating point overflow')
        return parsed

    if not isinstance(text, str) or len(text) > limit:
        raise ValidationError('JSON 输入超过限制')
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant,
                          parse_float=finite_float)
    except (ValueError, RecursionError):
        raise ValidationError('JSON 格式无效、存在重复键或非有限数值') from None


def fields(value, required, optional=()):
    if (not isinstance(value, dict) or not set(required) <= value.keys()
            or not value.keys() <= set(required) | set(optional)):
        raise ValidationError('对象字段缺失或包含不支持的字段')


def read_json(path):
    with open(path, encoding='utf-8-sig') as handle:
        return strict_json(handle.read(65537))
