"""Validate a vision provider's observation against an existing captured UI frame."""
from hashlib import sha256
import math
import time

from .common import moment
from ..domain import ValidationError
from ..autonomy.android_control import UiFrame
from ..autonomy.android_png import validate_png
from ..autonomy.android_read import AndroidReadError
from ..autonomy.android_ui import select_node


def observe(provider,frame,captured_at,now,*,allow_model_calls=False):
    moment(captured_at);moment(now)
    if (not isinstance(frame,UiFrame) or not 0<=now-captured_at<=30 or type(allow_model_calls) is not bool
            or (getattr(provider,'simulation',False) is not True and not allow_model_calls)):
        raise ValidationError('画面过期或尚未启用模型调用')
    validate_png(frame.png)
    started=time.monotonic()
    inspect_frame=getattr(provider,'inspect_frame',None)
    result=inspect_frame(frame) if callable(inspect_frame) else provider.inspect(frame.png,frame.metadata())
    return visual_proposal(frame,result,captured_at,now+time.monotonic()-started)


def visual_proposal(frame,observation,captured_at,now):
    moment(captured_at);moment(now)
    if not isinstance(frame,UiFrame) or not 0<=now-captured_at<=30:raise ValidationError('画面无效或过期')
    if type(observation) is not dict or observation.keys()!={'frame','target'} or observation['frame']!=frame.metadata():
        raise ValidationError('视觉结果未绑定当前设备画面')
    dimensions=validate_png(frame.png)
    if (sha256(frame.png).hexdigest()!=frame.screen_sha256
            or (dimensions['width'],dimensions['height'])!=(frame.width,frame.height)):
        raise ValidationError('画面内容或尺寸不符')
    target=observation['target']
    if type(target) is not dict or target.keys()!={'resource_id','label','bounds','confidence'}:
        raise ValidationError('视觉目标字段无效')
    confidence=target['confidence'];bounds=target['bounds']
    if (type(confidence) not in (int,float) or not math.isfinite(confidence) or not 0.8<=confidence<=1
            or type(bounds) is not list or len(bounds)!=4 or any(type(v) is not int for v in bounds)):
        raise ValidationError('视觉置信度或坐标无效')
    try:node=select_node(frame.nodes,{'resource_id':target['resource_id']},frame.package)
    except AndroidReadError as exc:raise ValidationError('视觉目标无法唯一关联UI') from exc
    if (not node['enabled'] or not node['clickable'] or node['password'] or node['bounds']!=bounds
            or node['text']!=target['label'] or not 0<=bounds[0]<bounds[2]<=frame.width
            or not 0<=bounds[1]<bounds[3]<=frame.height):raise ValidationError('视觉目标与受控UI不匹配')
    return dict(selector={'resource_id':target['resource_id'],'text':target['label']},
                frame=frame.metadata(),confidence=confidence,candidate_only=True,executed=False,
                requires_postcondition_and_action_approval=True)
