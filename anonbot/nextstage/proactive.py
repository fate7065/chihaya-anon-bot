"""Candidate-only proactive contact policy, with atomic attempt reservations."""
from .common import fingerprint, identity, moment
from ..domain import EventConflict, ValidationError


class ProactiveContact:
    def __init__(self,state):self.state,self.journal=state,state.journal

    def evaluate(self,scope,now,revision,expected_revision,routine,*,channel_capable=False,busy=False,db=None):
        moment(now)
        if (type(revision) is not int or revision<0 or type(expected_revision) is not int or expected_revision<0
                or type(channel_capable) is not bool or type(busy) is not bool):raise ValidationError('主动联系参数无效')
        state=self.state.snapshot(scope,now,db);activity=routine.at(now)
        attempts=[r['data'] for r in self.journal.events(scope,'proactive-claim',db)]
        if any(r['at']>now for r in attempts):raise ValidationError('主动联系时钟倒退')
        minute=(now//60+routine.offset)%1440
        day=(now+routine.offset*60)//86400
        reasons=[]
        if not channel_capable:reasons.append('channel_unverified')
        if not state['consent']:reasons.append('not_enabled')
        if state['paused']:reasons.append('paused')
        if minute<8*60 or minute>=22*60:reasons.append('quiet_hours')
        if busy:reasons.append('reply_in_flight')
        if revision!=expected_revision:reasons.append('stale_revision')
        if not activity['proactive_allowed']:reasons.append('fictional_routine_busy')
        if state['last_inbound'] is not None and now-state['last_inbound']<120:reasons.append('recent_user_message')
        if any((r['at']+routine.offset*60)//86400==day for r in attempts):reasons.append('daily_cap')
        if attempts and now-max(r['at'] for r in attempts)<3600:reasons.append('cooldown')
        binding=dict(scope=scope.key,now=now,revision=revision,expected_revision=expected_revision,
                     channel_capable=channel_capable,busy=busy,state=state,activity=activity,attempts=attempts)
        return dict(eligible=not reasons,reasons=reasons,fingerprint=fingerprint(binding),activity=activity,
                    candidate_only=True,sent=False)

    def claim(self,scope,job_id,preview_fingerprint,now,revision,expected_revision,routine,*,channel_capable=False,busy=False):
        identity(job_id)
        def guard(db):
            current=self.evaluate(scope,now,revision,expected_revision,routine,channel_capable=channel_capable,busy=busy,db=db)
            if current['fingerprint']!=preview_fingerprint:raise EventConflict('主动联系预览已变化')
            if not current['eligible']:raise ValidationError('主动联系被策略阻止')
        created=self.journal.append(scope,'proactive-claim',job_id,
            dict(at=now,revision=revision,fingerprint=preview_fingerprint,origin='synthetic'),guard=guard)
        return dict(claimed=True,duplicate=not created,candidate_only=True,sent=False)
