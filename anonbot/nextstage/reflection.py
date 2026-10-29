"""Extractive reflection proposals; invalid when a supporting fact expires or changes."""
from .common import identity, moment, text, fingerprint
from ..domain import ValidationError


class ReflectionNotes:
    def __init__(self,inbox):self.inbox,self.journal=inbox,inbox.journal

    def propose(self,scope,note_id,fragments,now):
        identity(note_id);moment(now)
        if type(fragments) not in (list,tuple) or not 1<=len(fragments)<=5:raise ValidationError('摘要来源数量无效')
        active={r['candidate_id']:r for r in self.inbox.active(scope,now)}
        references=[];parts=[]
        for pair in fragments:
            if type(pair) not in (tuple,list) or len(pair)!=2:raise ValidationError('摘要引用格式无效')
            source,quote=pair;identity(source);text(quote)
            if source not in active or quote not in active[source]['quote']:raise ValidationError('摘要包含无依据内容')
            references.append(dict(candidate=source,fingerprint=fingerprint(active[source])))
            parts.append(quote)
        summary='；'.join(parts);text(summary)
        data=dict(origin='synthetic',kind='extractive_reflection',state='proposed',at=now,summary=summary,references=references)
        self.journal.append(scope,'reflection',note_id,data)
        return data

    def read(self,scope,note_id,now):
        identity(note_id);moment(now)
        note=next((r['data'] for r in self.journal.events(scope,'reflection') if r['event_id']==note_id),None)
        if note is None:raise ValidationError('摘要不存在')
        active={r['candidate_id']:r for r in self.inbox.active(scope,now)}
        valid=note['at']<=now and all(ref['candidate'] in active and fingerprint(active[ref['candidate']])==ref['fingerprint'] for ref in note['references'])
        return dict(valid=True,**note) if valid else dict(valid=False,reason='source_changed_or_expired')
