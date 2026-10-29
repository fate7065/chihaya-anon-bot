"""Single-worker group observer. There is no reply generator or sending path."""
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from ..domain import ProviderError, ValidationError, numeric_id
from .group import DecisionCouncil, GroupDecision, ModelJudge, RuleJudge, hard_gate
from .group_ingress import parse_group
from .shadow_store import ShadowStore, finite_time


class QuotaProvider:
    def __init__(self, provider, runtime, role):
        self.provider, self.runtime, self.role = provider, runtime, role

    def generate(self, messages):
        runtime = self.runtime
        if runtime.stop.is_set():
            raise ProviderError('观察器正在停止')
        identity = runtime.store.reserve_call(runtime.active_job, self.role, runtime.clock(),
                                              runtime.calls_per_hour)
        if identity is None:
            runtime.api_limited = True
            raise ProviderError('观察调用预算已用尽或处理权已失效')
        try:
            if runtime.stop.is_set():
                raise ProviderError('观察器正在停止')
            response = self.provider.generate(messages)
        except Exception:
            runtime.store.complete_call(identity, 'failed', {})
            raise ProviderError('观察判断调用失败；详情已隐藏') from None
        runtime.store.complete_call(identity, 'returned',
                                     getattr(self.provider, 'last_response_metadata', {}))
        return response


class ShadowRuntime:
    sender = None

    def __init__(self, path, bot_id, groups=(), *, council=None, port=8766,
                 queue_limit=100, calls_per_hour=60, debounce_seconds=0.8, clock=time.time):
        self.bot_id = numeric_id(bot_id, '机器人QQ')
        if not isinstance(groups, (list, tuple)) or len(groups) > 100:
            raise ValidationError('观察群须为最多100项的列表')
        self.groups = tuple(sorted({numeric_id(g, '观察群') for g in groups}))
        for value, low, high in ((port, 1, 65535), (queue_limit, 1, 1000), (calls_per_hour, 1, 10000)):
            if type(value) is not int or not low <= value <= high:
                raise ValidationError('观察端口、队列或调用预算无效')
        debounce_seconds = finite_time(debounce_seconds)
        if not 0 <= debounce_seconds <= 60 or not callable(clock):
            raise ValidationError('合并等待或时钟无效')
        self.path, self.settings = Path(path), SimpleNamespace(port=port)
        self.clock, self.queue_limit = clock, queue_limit
        self.calls_per_hour, self.debounce_seconds = calls_per_hour, debounce_seconds
        self.stop, self.thread = threading.Event(), None
        self._processing = threading.Lock()
        self.active_job, self.api_limited, self.last_error_code = None, False, None
        base = council if council is not None else DecisionCouncil(
            [RuleJudge(d) for d in ('address', 'relevance', 'timing')])
        if not isinstance(base, DecisionCouncil):
            raise ValidationError('观察器需要DecisionCouncil')
        judges = []
        for judge in base.judges:
            if isinstance(judge, ModelJudge):
                judges.append(ModelJudge(judge.dimension, QuotaProvider(judge.provider, self, judge.dimension),
                                         judge.provider_id))
            elif isinstance(judge, RuleJudge):
                judges.append(judge)
            else:
                raise ValidationError('观察器仅接受可计量的模型判断器或规则判断器')
        self.council = DecisionCouncil(judges, base.threshold, base.cooldown_seconds)
        self.model_enabled = any(isinstance(j, ModelJudge) for j in judges)
        self.store = ShadowStore(self.path)

    def submit(self, payload):
        ingress = parse_group(payload, self.bot_id, self.groups)
        if ingress is None:
            return 'ignored'
        return self.store.submit(ingress, self.clock(), self.queue_limit)

    def process_one(self):
        if self.stop.is_set() or not self._processing.acquire(blocking=False):
            return False
        try:
            job = self.store.claim(self.clock(), self.debounce_seconds)
            if job is None:
                return False
            self.active_job, self.api_limited = job, False
            room = job['snapshot'].room
            if room.account_id != self.bot_id or room.platform != 'qq' or room.room_id not in self.groups:
                decision = GroupDecision('skip', 'scope_no_longer_allowed', ())
            else:
                decision = hard_gate(job['snapshot'], self.council.cooldown_seconds)
            if decision is None:
                if job['reference_status'] == 'unknown':
                    decision = GroupDecision('defer', 'unresolved_reference', ())
                elif job['media_only']:
                    decision = GroupDecision('defer', 'unsupported_media_only', ())
                else:
                    decision = self.council.evaluate(job['snapshot'])
            if self.api_limited:
                decision = GroupDecision(decision.action, 'api_budget_limited', decision.observations)
            if self.stop.is_set():
                decision = GroupDecision('defer', 'observer_stopping', decision.observations)
            self.store.finish(job, decision)
            return True
        finally:
            self.active_job = None
            self._processing.release()

    def recover(self):
        return self.store.recover()

    def status(self):
        return {**self.store.status(self.clock()), 'mode': 'group-shadow',
                'send_enabled': False, 'model_enabled': self.model_enabled,
                'allowed_group_count': len(self.groups), 'worker_error': self.last_error_code}

    def _worker(self):
        while not self.stop.is_set():
            try:
                if not self.process_one():
                    self.stop.wait(0.1)
            except Exception:
                self.last_error_code = 'worker_failed'
                self.stop.set()

    def start(self):
        if self.stop.is_set() or self.thread is not None:
            raise ValidationError('观察器实例不可重复启动')
        self.thread = threading.Thread(target=self._worker, daemon=True, name='group-shadow-worker')
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
