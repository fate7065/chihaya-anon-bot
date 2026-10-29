"""Explicit evidence-bound settlement of pending synthetic memory corrections."""
import re

from .domain import EventConflict, ValidationError
from .memory_candidate import _event_id
from .memory_revision import ManagedCandidateMemory, MemoryRevisionStore


class MemoryRecovery:
    def __init__(self,memory):
        if not isinstance(memory,ManagedCandidateMemory):
            raise ValidationError('恢复接口需要绑定的版本记忆')
        self.memory,self.store,self.namespace=memory,memory.store,memory.namespace

    async def _check(self,event_id,expected):
        try:actual=await self.memory.raw.read_existing(event_id)
        except ValidationError:
            return dict(status='invalid',evidence_sha256=None)
        if actual is None:return dict(status='missing',evidence_sha256=None)
        return dict(status='match' if actual==expected else 'different',
                    evidence_sha256=MemoryRevisionStore._fingerprint(actual))

    async def inspect(self,operation_id):
        _event_id(operation_id)
        before=self.store.operation_snapshot(self.namespace,operation_id)
        checks={};allowed=[]
        if before['state']=='pending':
            old,new=before['target'],before['new']
            if (old is None or old['state']!='active' or new is None or new['state']!='pending'
                    or new['operation_id']!=operation_id or old['event_id']!=before['target_event']
                    or new['event_id']!=before['new_event']):
                raise ValidationError('待核对更正的版本关系不一致')
            checks['target']=await self._check(before['target_event'],self.store.evidence(old))
            checks['new']=await self._check(before['new_event'],self.store.evidence(new))
            after=self.store.operation_snapshot(self.namespace,operation_id)
            if after!=before:raise EventConflict('证据核对期间账本发生变化')
            allowed=['finish','cancel'] if all(v['status']=='match' for v in checks.values()) else ['cancel']
        view=dict(operation=before,evidence_checks=checks,allowed_actions=allowed)
        return {**view,'fingerprint':self.store._fingerprint(view)}

    async def resolve(self,operation_id,action,fingerprint):
        _event_id(operation_id)
        if action not in ('finish','cancel') or not isinstance(fingerprint,str) or not re.fullmatch(r'[0-9a-f]{64}',fingerprint):
            raise ValidationError('恢复动作或核对指纹无效')
        current=self.store.operation_snapshot(self.namespace,operation_id)
        if current['state']!='pending':
            return self.store.resolve_pending(self.namespace,operation_id,action,self.store._fingerprint(current),fingerprint)
        try:view=await self.inspect(operation_id)
        except EventConflict:
            after=self.store.operation_snapshot(self.namespace,operation_id)
            if after['state']!='pending':
                return self.store.resolve_pending(self.namespace,operation_id,action,
                                                  self.store._fingerprint(after),fingerprint)
            raise
        if view['fingerprint']!=fingerprint:raise EventConflict('证据核对结果已改变')
        if action not in view['allowed_actions']:raise ValidationError('证据未精确匹配，不能完成更正')
        return self.store.resolve_pending(self.namespace,operation_id,action,
                                          self.store._fingerprint(view['operation']),fingerprint)
