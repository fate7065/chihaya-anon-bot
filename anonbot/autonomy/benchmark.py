"""Labeled group snapshots and metrics; no provider or platform interaction."""
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import math

from ..domain import ValidationError
from .group import GroupMessage, GroupSnapshot, RoomRef
from .jsonio import fields, strict_json


LABELS = ('reply', 'skip', 'uncertain')
ACTIONS = ('reply', 'skip', 'defer')


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    conversation_id: str
    split: str
    origin: str
    review_status: str
    expected: str
    snapshot: GroupSnapshot


@dataclass(frozen=True)
class BenchmarkDataset:
    cases: tuple[BenchmarkCase, ...]
    sha256: str
    profile: dict


def _identity(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or value != value.strip() or any(ord(c) < 32 for c in value)):
        raise ValidationError('评测标识须为有界非空文本')
    return value


def _case(value):
    names = ('case_id', 'conversation_id', 'split', 'origin', 'review_status', 'expected', 'snapshot')
    fields(value, names)
    for key in ('case_id', 'conversation_id'):
        _identity(value[key])
    for key, choices in (('split', ('dev', 'test')), ('origin', ('synthetic', 'human', 'captured')),
                         ('review_status', ('unreviewed', 'human_reviewed')), ('expected', LABELS)):
        if value[key] not in choices:
            raise ValidationError('评测枚举值无效')
    state = value['snapshot']
    fields(state, ('room', 'revision', 'messages', 'now'),
           ('last_reply_at', 'enabled', 'quiet'))
    fields(state['room'], ('platform', 'account_id', 'room_id'), ('persona_id',))
    if not isinstance(state['messages'], list) or not 1 <= len(state['messages']) <= 20:
        raise ValidationError('评测快照需要1–20条消息')
    messages = []
    for message in state['messages']:
        fields(message, ('event_id', 'sender_id', 'text'), ('mentioned', 'reply_to_bot'))
        messages.append(GroupMessage(**message))
    snapshot = GroupSnapshot(RoomRef(**state['room']), state['revision'], tuple(messages),
                             state['now'], state.get('last_reply_at'),
                             state.get('enabled', True), state.get('quiet', False))
    return BenchmarkCase(*(value[name] for name in names[:-1]), snapshot)


def dataset_profile(cases):
    reviewed = [case for case in cases if case.origin == 'human'
                and case.review_status == 'human_reviewed']
    conversations = {case.conversation_id for case in reviewed}
    return {
        'cases': len(cases), 'conversations': len({c.conversation_id for c in cases}),
        'split_counts': dict(Counter(c.split for c in cases)),
        'origin_counts': dict(Counter(c.origin for c in cases)),
        'human_reviewed_cases': len(reviewed),
        'human_reviewed_conversations': len(conversations),
        'minimum_reviewed_coverage': (len(conversations) >= 100
            and {c.split for c in reviewed} == {'dev', 'test'}
            and {'reply', 'skip'} <= {c.expected for c in reviewed}),
        'provenance_is_author_declared': True,
    }


def load_benchmark(path):
    with open(path, 'rb') as handle:
        raw = handle.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValidationError('评测文件超过8MiB')
    try:
        lines = raw.decode('utf-8-sig').split('\n')
    except UnicodeError:
        raise ValidationError('评测文件须为UTF-8') from None
    cases, identities, conversations = [], set(), {}
    for line in lines:
        if not line.strip():
            continue
        if len(cases) >= 1000:
            raise ValidationError('评测文件超过1000条')
        case = _case(strict_json(line))
        if case.case_id in identities:
            raise ValidationError('case_id重复')
        identities.add(case.case_id)
        scope = (case.split, case.snapshot.room.key)
        if case.conversation_id in conversations and conversations[case.conversation_id] != scope:
            raise ValidationError('同一conversation不能跨数据分区或房间')
        conversations[case.conversation_id] = scope
        cases.append(case)
    if not cases:
        raise ValidationError('评测文件为空')
    return BenchmarkDataset(tuple(cases), sha256(raw).hexdigest(), dataset_profile(cases))


def summarize(rows):
    """Score certain labels only for precision; uncertain labels stay separate."""
    confusion = {gold: {action: 0 for action in ACTIONS} for gold in LABELS}
    latencies = []
    for row in rows:
        gold, action, elapsed = row['expected'], row['action'], row['elapsed_ms']
        if (gold not in LABELS or action not in ACTIONS or type(elapsed) not in (int, float)
                or elapsed < 0 or not math.isfinite(elapsed)):
            raise ValidationError('评测结果或耗时无效')
        confusion[gold][action] += 1
        latencies.append(elapsed)
    ratio = lambda numerator, denominator: numerator / denominator if denominator else None
    latencies.sort()
    percentile = lambda fraction: latencies[math.ceil(len(latencies) * fraction) - 1] if latencies else None
    skip_count, reply_count = sum(confusion['skip'].values()), sum(confusion['reply'].values())
    return {
        'cases': len(rows), 'confusion': confusion,
        'uncertain_labels': sum(confusion['uncertain'].values()),
        'false_interruption_rate': ratio(confusion['skip']['reply'], skip_count),
        'miss_rate': ratio(confusion['reply']['skip'] + confusion['reply']['defer'], reply_count),
        'reply_precision': ratio(confusion['reply']['reply'],
                                 confusion['reply']['reply'] + confusion['skip']['reply']),
        'abstention_rate': ratio(sum(c['defer'] for c in confusion.values()), len(rows)),
        'latency_ms': {'p50': percentile(0.5), 'p95': percentile(0.95)},
    }
