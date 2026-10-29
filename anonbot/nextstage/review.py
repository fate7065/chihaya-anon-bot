"""Blind synthetic annotation work packages; reviewer declarations are not authentication."""
from .common import fingerprint, identity, text
from ..domain import ValidationError


class ReviewDesk:
    def __init__(self,journal):self.journal=journal

    def prepare(self,scope,batch_id,cases,category):
        identity(batch_id)
        if category not in ('group','memory') or type(cases) is not list or not 1<=len(cases)<=500:
            raise ValidationError('标注批次无效')
        packed=[];seen=set();splits={}
        for case in cases:
            if type(case) is not dict or case.keys()!={'case_id','conversation_id','split','origin','content'}:
                raise ValidationError('盲标注样本字段无效')
            identity(case['case_id']);identity(case['conversation_id']);text(case['content'],4000)
            if case['origin']!='synthetic' or case['split'] not in ('dev','test') or case['case_id'] in seen:
                raise ValidationError('只接受独立ID合成样本及固定切分')
            if case['conversation_id'] in splits and splits[case['conversation_id']]!=case['split']:
                raise ValidationError('同一对话不能横跨开发与保留集')
            splits[case['conversation_id']]=case['split'];seen.add(case['case_id'])
            packed.append({**case,'case_sha256':fingerprint(case)})
        with self.journal.transaction() as db:
            self.journal.append(scope,'review-batch',batch_id,
                dict(category=category,case_ids=[r['case_id'] for r in packed],case_hashes=[r['case_sha256'] for r in packed]),db=db)
            for case in packed:
                key=fingerprint([batch_id,case['case_id']])
                self.journal.append(scope,'review-case',key,dict(batch=batch_id,**case),db=db)
        return dict(batch=batch_id,category=category,cases=packed)

    def export(self,scope,batch_id):
        identity(batch_id)
        with self.journal.transaction() as db:
            batch=next((r['data'] for r in self.journal.events(scope,'review-batch',db) if r['event_id']==batch_id),None)
            if batch is None:raise ValidationError('当前作用域不存在标注批次')
            cases=[r['data'] for r in self.journal.events(scope,'review-case',db) if r['data']['batch']==batch_id]
            labels={r['data']['case_id']:r['data'] for r in self.journal.events(scope,'review-label',db) if r['data']['batch']==batch_id}
        return dict(batch=batch_id,category=batch['category'],cases=cases,labels=labels,
                    unreviewed=len(cases)-len(labels),synthetic_labels=sum(r['review_origin']=='synthetic' for r in labels.values()),
                    human_declared_labels=sum(r['review_origin']=='human_declared' for r in labels.values()),
                    authenticated_human_review=False)

    def label(self,scope,batch_id,case_id,case_sha256,label,reviewer,review_origin):
        identity(batch_id);identity(case_id);text(reviewer,64)
        batch=self.export(scope,batch_id)
        choices=('reply','skip','uncertain') if batch['category']=='group' else ('remember','omit','uncertain')
        case=next((r for r in batch['cases'] if r['case_id']==case_id),None)
        if (case is None or case['case_sha256']!=case_sha256 or label not in choices
                or review_origin not in ('synthetic','human_declared')):raise ValidationError('标签、样本摘要或来源无效')
        return self.journal.append(scope,'review-label',fingerprint([batch_id,case_id]),
            dict(batch=batch_id,case_id=case_id,case_sha256=case_sha256,label=label,reviewer=reviewer,review_origin=review_origin))
