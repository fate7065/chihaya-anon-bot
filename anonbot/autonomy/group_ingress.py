"""OneBot group ingress; media and quoted content are never downloaded."""
from dataclasses import dataclass
import re

from ..domain import ValidationError, numeric_id
from .group import GroupMessage, RoomRef


@dataclass(frozen=True)
class IngressEvent:
    room: RoomRef
    message: GroupMessage
    event_time: int
    reply_id: str | None = None
    media_only: bool = False


def message_id(value):
    if (type(value) not in (str, int)
            or not re.fullmatch(r'-?[0-9]{1,20}', str(value))):
        raise ValidationError('消息ID须为有界整数')
    return str(value)


def parse_group(payload, bot_id, allowed_groups, persona_id='anon'):
    if not isinstance(payload, dict):
        raise ValidationError('事件须为JSON对象')
    if payload.get('post_type') not in ('message', 'message_sent') or payload.get('message_type') != 'group':
        return None
    bot_id = numeric_id(bot_id, '配置机器人')
    if numeric_id(payload.get('self_id'), 'self_id') != bot_id:
        raise ValidationError('机器人账号不匹配')
    group_id = numeric_id(payload.get('group_id'), 'group_id')
    if group_id not in allowed_groups:
        return None
    sender = numeric_id(payload.get('user_id'), 'user_id')
    identity = message_id(payload.get('message_id'))
    timestamp = payload.get('time')
    if type(timestamp) is not int or not 0 <= timestamp <= 9223372036854775807:
        raise ValidationError('事件时间须为非负int64')
    segments = payload.get('message')
    if not isinstance(segments, list) or not 1 <= len(segments) <= 100:
        raise ValidationError('群消息须为1–100段数组')
    parts, text_parts, mentioned, reply, media = [], [], False, None, False
    placeholders = {'image': '[图片]', 'record': '[语音]', 'video': '[视频]',
                    'face': '[表情]', 'file': '[文件]'}
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get('data'), dict):
            raise ValidationError('群消息段格式无效')
        kind, data = segment.get('type'), segment['data']
        if kind == 'text':
            text = data.get('text')
            if not isinstance(text, str):
                raise ValidationError('text内容须为字符串')
            parts.append(text)
            text_parts.append(text)
        elif kind == 'at':
            target = data.get('qq')
            if target == 'all':
                parts.append('[提及全体]')
            elif numeric_id(target, 'at.qq') == bot_id:
                mentioned = True
            else:
                parts.append('[提及其他群成员]')
        elif kind == 'reply':
            if reply is not None:
                raise ValidationError('不支持多个引用目标')
            reply = message_id(data.get('id'))
        else:
            if not isinstance(kind, str):
                raise ValidationError('消息段类型须为文本')
            parts.append(placeholders.get(kind, '[未解析消息]'))
            media = True
    text = ''.join(parts).strip() or '[仅提及或引用]'
    event = IngressEvent(RoomRef('qq', bot_id, group_id, persona_id),
                         GroupMessage(identity, sender, text, mentioned), timestamp,
                         reply, media and not ''.join(text_parts).strip())
    # Reject lone JSON-surrogate escapes before they reach a model or SQLite.
    try:
        text.encode('utf-8')
    except UnicodeError:
        raise ValidationError('消息文字含无效Unicode') from None
    return event
