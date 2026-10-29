"""Provider-injected vector ranking over reviewed synthetic candidates only."""
import math

from .common import text, moment
from ..domain import EventConflict, ValidationError


class SemanticRecall:
    def __init__(self,inbox,embed):self.inbox,self.embed=inbox,embed

    async def search(self,scope,query,now,limit=5,threshold=0.25,*,allow_model_calls=False):
        text(query);moment(now)
        if (type(limit) is not int or not 1<=limit<=10 or type(threshold) not in (int,float)
                or not math.isfinite(threshold) or not 0<=threshold<=1):raise ValidationError('检索参数无效')
        active=self.inbox.active(scope,now);items=active[:32]
        if not items:return dict(hits=[],candidate_count=0,omitted_count=0,embedding_calls=0)
        if type(allow_model_calls) is not bool or (getattr(self.embed,'simulation',False) is not True and not allow_model_calls):
            raise ValidationError('非合成向量提供方须显式启用调用')
        vectors=await self.embed([query]+[r['quote'] for r in items])
        if type(vectors) is not list or len(vectors)!=len(items)+1:raise ValidationError('向量数量不符')
        dimension=None;unit=[]
        for vector in vectors:
            if (type(vector) is not list or not 1<=len(vector)<=4096
                    or any(type(x) not in (float,int) or not math.isfinite(x) for x in vector)):
                raise ValidationError('无效向量')
            if dimension is None:dimension=len(vector)
            if len(vector)!=dimension:raise ValidationError('向量维度不一致')
            norm=math.hypot(*vector)
            if not math.isfinite(norm) or norm==0:raise ValidationError('零或溢出向量')
            unit.append([x/norm for x in vector])
        hits=[]
        for row,vector in zip(items,unit[1:]):
            score=sum(x*y for x,y in zip(unit[0],vector))
            if score>=threshold:hits.append({**row,'score':score})
        current={r['candidate_id']:r for r in self.inbox.active(scope,now)}
        if any(current.get(row['candidate_id'])!=row for row in items):
            raise EventConflict('向量生成期间记忆发生变化')
        hits.sort(key=lambda r:r['score'],reverse=True)
        return dict(hits=hits[:limit],candidate_count=len(items),omitted_count=len(active)-len(items),embedding_calls=1)
