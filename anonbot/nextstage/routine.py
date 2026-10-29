"""Trusted fictional daily blocks; no assertion about real-world activity."""
from .common import moment, text
from ..domain import ValidationError


class Routine:
    def __init__(self,blocks,offset_minutes=480):
        if type(blocks) is not list or len(blocks)>24 or type(offset_minutes) is not int or not -720<=offset_minutes<=840:
            raise ValidationError('日程数量或时区偏移无效')
        self.offset=offset_minutes;self.blocks=[];occupied=[]
        for block in blocks:
            if type(block) is not dict or block.keys()!={'start_minute','end_minute','activity','proactive_allowed'}:
                raise ValidationError('日程字段无效')
            start,end=block['start_minute'],block['end_minute']
            if (type(start) is not int or type(end) is not int or not 0<=start<1440 or not 0<=end<=1440
                    or start==end or type(block['proactive_allowed']) is not bool):raise ValidationError('日程时刻无效')
            text(block['activity'],100)
            spans=[(start,end)] if start<end else [(start,1440),(0,end)]
            for a,b in spans:
                if any(max(a,c)<min(b,d) for c,d in occupied):raise ValidationError('日程重叠')
                occupied.append((a,b))
            self.blocks.append((dict(block),spans))

    def at(self,now):
        moment(now);minute=(now//60+self.offset)%1440
        for block,spans in self.blocks:
            if any(a<=minute<b for a,b in spans):
                return dict(activity=block['activity'],proactive_allowed=block['proactive_allowed'],fictional=True)
        return dict(activity='未安排',proactive_allowed=True,fictional=True)
