"""Evidence-tagged synthetic relationship events and decaying expression state."""
from .common import identity, moment
from ..domain import ValidationError


class CompanionState:
    def __init__(self,journal):self.journal=journal

    def record(self,scope,event_id,event,at,source_event):
        identity(event_id);identity(source_event);moment(at)
        if event not in ('conversation','positive','negative','pause','resume','consent-on','consent-off','reset'):
            raise ValidationError('未知的状态事件')
        def guard(db):
            rows=self.journal.events(scope,'companion-state',db)
            if rows and rows[-1]['data']['at']>at:raise ValidationError('状态时钟不能倒退')
        return self.journal.append(scope,'companion-state',event_id,
            dict(event=event,at=at,source_event=source_event,origin='synthetic'),guard=guard)

    def snapshot(self,scope,now,db=None):
        moment(now);value=0.;last=0;count=0;paused=False;consent=False;inbound=None
        for row in self.journal.events(scope,'companion-state',db):
            event=row['data'];at=event['at']
            if at>now:raise ValidationError('状态读取时钟不能早于已记录事件')
            value*=2**(-(at-last)/3600);last=at
            kind=event['event']
            if kind in ('positive','negative'):value=max(-1.,min(1.,value+(0.4 if kind=='positive' else -0.4)))
            elif kind=='conversation':count+=1;inbound=at
            elif kind=='pause':paused=True
            elif kind=='resume':paused=False
            elif kind=='consent-on':consent=True
            elif kind=='consent-off':consent=False
            elif kind=='reset':value=0.;count=0
        return dict(origin='synthetic',relationship='ordinary',interaction_count=count,
                    valence=value*2**(-(now-last)/3600),paused=paused,consent=consent,last_inbound=inbound)
