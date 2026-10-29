"""Strict OneBot v11 JSON array input parser; there is no QQ connection."""
import re
from .domain import MessageEvent, Scope, ValidationError, numeric_id


def parse_onebot(payload: object, self_id: str, persona_id: str = 'anon') -> MessageEvent | None:
    if not isinstance(payload, dict):
        raise ValidationError('事件必须是 JSON 对象')
    post_type = payload.get('post_type')
    if post_type in ('notice', 'request', 'meta_event'):
        return None
    if post_type != 'message':
        raise ValidationError('未知或缺少 post_type')
    bot_id = numeric_id(payload.get('self_id'), 'self_id')
    if bot_id != numeric_id(self_id, 'configured self_id'):
        raise ValidationError('事件 self_id 与配置不一致')
    user_id = numeric_id(payload.get('user_id'), 'user_id')
    message_id = payload.get('message_id')
    if isinstance(message_id, bool) or not isinstance(message_id, (int, str)) or not re.fullmatch(r'-?[0-9]+', str(message_id)):
        raise ValidationError('message_id 必须是整数或其字符串形式')
    kind = payload.get('message_type')
    if kind == 'private':
        scene = 'private'
    elif kind == 'group':
        scene = 'group:' + numeric_id(payload.get('group_id'), 'group_id')
    else:
        raise ValidationError('仅支持 private/group 消息')
    segments = payload.get('message')
    if not isinstance(segments, list) or not 1 <= len(segments) <= 100:
        raise ValidationError('仅支持 OneBot message 数组（1–100 段），不支持 CQ 字符串')
    parts, mentioned = [], False
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get('data'), dict):
            raise ValidationError('消息段格式无效')
        data = segment['data']
        if segment.get('type') == 'text':
            text = data.get('text')
            if not isinstance(text, str):
                raise ValidationError('text 段必须包含字符串 text')
            parts.append(text)
        elif segment.get('type') == 'at':
            target = data.get('qq')
            if target != 'all':
                target = numeric_id(target, 'at.qq')
            mentioned = mentioned or target == bot_id
        else:
            raise ValidationError('当前骨架仅支持 text/at 段；图片、语音等尚未实现')
    return MessageEvent(str(message_id), Scope(bot_id, persona_id, user_id, scene),
                        ''.join(parts).strip(), mentioned, 'onebot')
