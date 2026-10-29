"""Explicit one-call role dialogue trial, using this app's configured endpoint."""
import json
from pathlib import Path
from .domain import ValidationError
from .persona import load_persona,system_prompt,response_style
from .providers import DeepSeekProvider
from .knowledge_boundary import BoundaryRules

CARD_PATH=Path(__file__).with_name('persona_anon_natural.json')


def load_natural_persona():
    try:
        with CARD_PATH.open('rb') as handle:raw=handle.read(64001)
        if len(raw)>64000:raise ValueError()
        card=json.loads(raw.decode('utf-8'))
        if not isinstance(card,dict):raise ValueError()
        for name in ('official_facts','writing_choices'):
            if not isinstance(card.get(name),list) or not all(isinstance(s,str) for s in card[name]):
                raise ValueError()
        if card['official_facts']!=load_persona()['official_facts']:raise ValueError()
        system_prompt(card);response_style(card)
        return card
    except (OSError,ValueError,KeyError,TypeError,RecursionError):
        raise ValidationError('自然对话角色卡不可用或与官方事实基线不一致，未启用试用') from None


class NaturalDialogueProvider(DeepSeekProvider):
    def __init__(self,source):
        if not isinstance(source,DeepSeekProvider):
            raise ValidationError('自然对话试用仅支持本应用已配置的DeepSeek接口')
        super().__init__(source.api_key,model='deepseek-v4-pro',
                         base_url=source.url.removesuffix('/chat/completions'))
        self.timeout=60
        from .knowledge import resource_path
        self.boundaries=BoundaryRules.load(resource_path('unknown_claims.json'))

    def request_body(self,messages):
        match=self.boundaries.resolve(messages) if len(messages)>1 and messages[-1].get('role')=='user' else None
        augmented=messages
        if match:
            cue='本轮角色资料的已核验范围：'+match.text+'。这表示现有证据不足，不是整部作品没有发生。保留这个未知范围，不补造词原因或内心动机，按角色口吻简单回应，不复述核验过程。'
            augmented=[*messages[:-1],{'role':'system','content':cue},messages[-1]]
        body=super().request_body(augmented)
        body.pop('temperature',None)
        body.update(thinking={'type':'enabled'},reasoning_effort='high',max_tokens=8192)
        return body
