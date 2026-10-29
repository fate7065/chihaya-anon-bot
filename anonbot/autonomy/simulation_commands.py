"""Bounded synthetic-only CLI: virtual time, budgeted models, local mock deliveries."""
from collections import Counter
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from ..domain import ValidationError
from ..providers import MockProvider
from .evaluation import CallTracker, MeteredProvider, ObservedJudge, _current_deepseek_council
from .group import DecisionCouncil, GroupControl, GroupMessage, GroupRecall, GroupReplyLimit, RoomRef, RuleJudge, _number
from .jsonio import fields, strict_json
from .simulation import GroupReplyGenerator, GroupSimulation
from .task_export import _text


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def _simulation_options(value):
    fields(value, (), ('judge_latency_seconds', 'generation_latency_seconds', 'cooldown_recheck', 'reply_limit'))
    options = {'judge_latency_seconds':0, 'generation_latency_seconds':0, 'cooldown_recheck':False,
               'reply_limit':None, **value}
    for name in ('judge_latency_seconds', 'generation_latency_seconds'):
        _number(options[name], name, 0, 30)
    if type(options['cooldown_recheck']) is not bool:
        raise ValidationError('冷却复判开关须为bool')
    if options['reply_limit'] is not None:
        fields(options['reply_limit'], ('max_replies', 'window_seconds'))
        GroupReplyLimit(**options['reply_limit'])
    return options


def _finish_time(at, options):
    active = any(options[name] for name in ('judge_latency_seconds', 'generation_latency_seconds', 'cooldown_recheck'))
    window = (Decimal(5) + 2*Decimal(str(options['judge_latency_seconds'])) +
              2*Decimal(str(options['generation_latency_seconds']))) if active else Decimal(2)
    return float(Decimal(str(at)) + window)


def load_scenario(path):
    with Path(path).open('rb') as source:
        raw = source.read(2*1024*1024+1)
    if len(raw) > 2*1024*1024:
        raise ValidationError('模拟输入超过2MiB')
    value = strict_json(raw.decode('utf-8-sig'))
    fields(value, ('version', 'origin', 'events'), ('simulation',))
    options = _simulation_options(value.get('simulation', {}))
    if type(value['version']) is not int or value['version'] != 1 or value['origin'] != 'synthetic':
        raise ValidationError('模拟入口只接受版本1的synthetic数据')
    events = value['events']
    if type(events) is not list or not 1 <= len(events) <= 100:
        raise ValidationError('模拟需要1–100个事件')
    last, seen, rooms = 0, set(), set()
    messages, withdrawn = {}, {}
    for event in events:
        fields(event, ('at', 'room'), ('message', 'recall', 'control'))
        if sum(name in event for name in ('message', 'recall', 'control')) != 1:
            raise ValidationError('模拟事件须且只能包含message、recall或control之一')
        _number(event['at'], '事件时间', 0, 3598)
        if _finish_time(event['at'], options) > 3600:
            raise ValidationError('事件时间未给延迟和复判预留收尾窗口')
        if event['at'] < last:
            raise ValidationError('模拟事件必须按时间排序')
        last = event['at']
        RoomRef('simulation', 'sim-bot', event['room'])
        rooms.add(event['room'])
        if 'message' in event:
            fields(event['message'], ('event_id', 'sender_id', 'text'), ('mentioned', 'reply_to_bot'))
            item = GroupMessage(**event['message'])
            identity = event['room'], item.event_id
            if identity in withdrawn and withdrawn[identity] != item.sender_id:
                raise ValidationError('先到撤回通知与原消息作者不一致')
            messages[identity] = item
        elif 'recall' in event:
            fields(event['recall'], ('event_id', 'target_event', 'sender_id'))
            item = GroupRecall(**event['recall'])
            identity = event['room'], item.event_id
            if identity in withdrawn:
                raise ValidationError('撤回通知ID与目标消息ID冲突')
            target = event['room'], item.target_event
            if ((target in messages and messages[target].sender_id != item.sender_id)
                    or (target in withdrawn and withdrawn[target] != item.sender_id)
                    or (target in seen and target not in messages)):
                raise ValidationError('撤回目标或原作者不一致')
            withdrawn[target] = item.sender_id
        else:
            fields(event['control'], ('event_id', 'enabled'))
            item = GroupControl(**event['control'])
            identity = event['room'], item.event_id
            if identity in withdrawn:
                raise ValidationError('群控制ID与已撤回消息ID冲突')
        if identity in seen or item.event_id.startswith('sim-reply-'):
            raise ValidationError('模拟事件ID重复或使用保留前缀')
        seen.add(identity)
    if len(rooms) > 10:
        raise ValidationError('模拟群数量超过10')
    json.dumps(value, ensure_ascii=False).encode('utf-8')
    return value, sha256(raw).hexdigest()


def _components(live, tracker):
    if not live:
        return (DecisionCouncil([RuleJudge(d) for d in ('address', 'relevance', 'timing')]),
                GroupReplyGenerator(MockProvider()))
    base = _current_deepseek_council()
    tracker.models = [{'dimension': j.dimension, 'provider_id': j.provider_id,
                       'model_declared': getattr(j.provider, 'model', None)} for j in base.judges]
    council = DecisionCouncil([ObservedJudge(j, tracker) for j in base.judges],
                              base.threshold, base.cooldown_seconds)
    # Reuse the existing strict, bounded provider for a short group utterance.
    provider = MeteredProvider(base.judges[0].provider, tracker, 'reply', 'current-deepseek:reply')
    return council, GroupReplyGenerator(provider)


def _transcript(records, live, options=None):
    lines = ['# 模拟群聊记录', '',
             '模式：' + ('真实模型API＋本地模拟发送' if live else '规则＋固定离线演示（非模型效果）'), '',
             '所有群和人物均为虚构；没有连接或发送到真实QQ。时间为虚拟秒，不是模型实际响应耗时。', '']
    if options:
        lines += [f'人为设置判断耗时：{options["judge_latency_seconds"]}秒；生成耗时：{options["generation_latency_seconds"]}秒；'
                  + ('冷却到期复判一次' if options['cooldown_recheck'] else '不自动复判'), '']
        if options.get('reply_limit') is not None:
            limit = options['reply_limit']
            lines += [f'每个房间独立限制：滚动{limit["window_seconds"]:g}秒内最多{limit["max_replies"]}次模拟回复。', '']
    labels = {'cancelled':'上下文变化取消旧候选', 'stale_decision':'判断时上下文已变化',
              'stale_generation':'生成时上下文已变化', 'generation_failed':'生成失败，未模拟发送',
              'delivery_failed':'模拟执行未确认', 'judgment_started':'开始判断',
              'generation_started':'开始生成', 'recheck_scheduled':'安排一次冷却复判',
              'recheck_cancelled':'上下文变化取消冷却复判', 'recalled':'群消息已撤回',
              'recall_duplicate':'相同目标已撤回', 'discarded_recalled_message':'忽略先前已撤回的迟到消息',
              'room_paused':'暂停回复', 'room_resumed':'恢复回复（等待新消息）',
              'reply_suppressed':'暂停期间仅记录消息', 'control_unchanged':'回复开关未改变',
              'reply_limited':'发言限额已满，本次不回复'}
    for row in records:
        head = f'[{row["at"]:g}s · {_text(row["room"])}] '
        if row['type'] == 'received':
            message = row['message']
            lines += [head+_text(message['sender_id'])+'：'+_text(message['text']), '']
        elif row['type'] == 'decision':
            lines += [head+'判断：'+_text(row['action'])+' / '+_text(row['reason']), '']
            for vote in row['observations']:
                lines += ['- '+_text(vote['dimension'])+'：'+_text(vote['vote'])+
                          '；'+_text(vote['reason']), '']
        elif row['type'] == 'delivery':
            lines += [head+'爱音 → '+_text(row['recipient'])+'（模拟回复 '+_text(row['target_event'])+'）：'+_text(row['text']), '']
        elif row['type'] in labels:
            suffix = f'（到期{row["due"]:g}秒）' if 'due' in row else ''
            if row['type'] in ('recalled', 'recall_duplicate', 'discarded_recalled_message'):
                suffix += '：'+_text(row['target_event'])
            if row['type'] == 'control_unchanged':
                suffix += '（'+('已启用' if row['enabled'] else '已暂停')+'）'
            if row['type'] == 'reply_limited':
                suffix += f'：{_text(row["target_event"])}；已用{row["used"]}/{row["max_replies"]}；'
                suffix += f'最早{row["next_available_at"]:g}秒可释放（不自动补答）'
            lines += [head+labels[row['type']]+suffix, '']
    return '\n'.join(lines)


def run_simulation(input_path, output, *, use_current_deepseek=False, allow_model_calls=False, max_calls=24):
    data, digest = load_scenario(input_path)
    options = _simulation_options(data.get('simulation', {}))
    if type(use_current_deepseek) is not bool or type(allow_model_calls) is not bool or use_current_deepseek != allow_model_calls:
        raise ValidationError('API模拟需要同时明确选择当前DeepSeek与允许模型调用')
    output = Path(output)
    if output.exists():
        raise ValidationError('模拟输出目录已存在，请选择全新目录')
    if type(max_calls) is not int or not 1 <= max_calls <= 300:
        raise ValidationError('模拟API预算须为1–300')
    tracker = CallTracker(max_calls)
    tracker.strategy, tracker.case_id = 'simulation', 'synthetic-sequence'
    council, generator = _components(use_current_deepseek, tracker)
    output.mkdir(parents=True, exist_ok=False)
    _write(output/'input.json', data)
    code = {name: sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('simulation.py', 'simulation_commands.py', 'group.py', 'evaluation.py')}
    _write(output/'manifest.json', {'version':1, 'origin':'synthetic', 'input_sha256':digest,
           'mode':'api-simulation' if use_current_deepseek else 'offline-simulation',
           'external_actions':0, 'max_calls':max_calls, 'models':tracker.models, 'code_sha256':code,
           'simulation':options,
           'independent_model_weights':False, 'reply_reuses_address_model':use_current_deepseek})
    runtime_options = {**options, 'reply_limit':GroupReplyLimit(**options['reply_limit'])
                       if options['reply_limit'] is not None else None}
    sim = GroupSimulation(output, tuple(sorted({e['room'] for e in data['events']})), council, generator, **runtime_options)
    complete = False
    try:
        for event in data['events']:
            if 'message' in event:
                sim.receive(event['room'], GroupMessage(**event['message']), event['at'])
            elif 'recall' in event:
                sim.recall(event['room'], GroupRecall(**event['recall']), event['at'])
            else:
                sim.control(event['room'], GroupControl(**event['control']), event['at'])
        sim.advance(_finish_time(data['events'][-1]['at'], options))
        complete = sim.pending_count() == 0
    finally:
        counts = Counter(r['type'] for r in sim.records)
        summary = {'complete':complete, 'input_events':len(data['events']), 'processed_events':len(sim.seen),
                   'pending_count':sim.pending_count(),
                   'room_states':{name:{'enabled':state.enabled} for name,state in sim.rooms.items()},
                   'reply_limit':options['reply_limit'],
                   'simulated_deliveries':counts['delivery'], 'external_actions':0,
                   'api_attempts':tracker.attempts, 'api_budget':max_calls,
                   'call_status_counts':dict(Counter(c['status'] for c in tracker.calls)),
                   'event_counts':dict(counts), 'model_quality_accepted':False, 'actual_cost':None}
        with (output/'events.jsonl').open('w', encoding='utf-8') as target:
            for row in sim.records:
                target.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
        _write(output/'calls.json', tracker.calls)
        _write(output/'summary.json', summary)
        (output/'transcript.md').write_text(_transcript(sim.records, use_current_deepseek, options), encoding='utf-8')
    return summary


def add_simulation_parser(commands):
    parser = commands.add_parser('simulate-group', help='虚构群聊闭环，只有本地模拟发送')
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--use-current-deepseek', action='store_true')
    parser.add_argument('--allow-model-calls', action='store_true', help='仅将虚构样本发给当前模型API，可能计费')
    parser.add_argument('--max-calls', type=int, default=24)


def simulation_command(args, emit):
    summary = run_simulation(args.input, args.output, use_current_deepseek=args.use_current_deepseek,
                             allow_model_calls=args.allow_model_calls, max_calls=args.max_calls)
    emit({'type':'simulation_summary', **summary})
    return 0 if summary['complete'] else 2
