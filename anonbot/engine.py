import copy
from .context import build_context
from .domain import MessageEvent, ModelProvider, ProviderError, ReplyResult
from .memory import Store
from .persona import response_style, system_prompt
from .policy import ReplyPolicy
from .knowledge import STORY_QUERY, load_knowledge
from .turn_guidance import fact_guidance, turn_guidance, natural_guidance
from .daily_guidance import daily_guidance


class Engine:
    def __init__(self, store: Store, provider: ModelProvider, policy: ReplyPolicy, context_chars=16000, record_history=True, knowledge=None, related_history=True, experimental_dialogue=False, persona=None):
        if type(experimental_dialogue) is not bool:
            raise ValueError('experimental_dialogue must be a boolean')
        self.store, self.provider, self.policy = store, provider, policy
        self.context_chars, self.record_history = context_chars, record_history
        self.knowledge = knowledge
        self.related_history = related_history
        self.experimental_dialogue = experimental_dialogue
        self.persona = copy.deepcopy(persona)

    def handle(self, event: MessageEvent, retry_failed: bool = False) -> ReplyResult:
        existing = self.store.claim(event, retry_failed)
        if existing is not None:
            return existing
        decision = self.policy.decide(event)
        if not decision.reply:
            result = ReplyResult('skipped', decision.reason)
        else:
            try:
                memories = [item.content for item in self.store.recall(event.scope)]
                history = self.store.history(event.scope)
                evidence = ''
                if event.scope.persona_id == 'anon':
                    kb = self.knowledge if self.knowledge is not None else load_knowledge()
                    evidence, _ = kb.for_dialogue(event.text, history=history)
                related = []
                related_self = []
                if (self.related_history and event.scope.persona_id == 'anon'
                        and not evidence and not STORY_QUERY.search(event.text.casefold())):
                    related = self.store.related_user_lines(event.scope, event.text)
                    related_self = self.store.related_assistant_lines(event.scope, event.text)
                style = response_style() if self.persona is None else response_style(self.persona)
                if event.scope.persona_id == 'anon':
                    cue = turn_guidance(event.text)
                    if self.persona is not None and not cue:
                        cue = natural_guidance(event.text)
                    if self.experimental_dialogue and not cue:
                        cue = fact_guidance(event.text)
                        if not cue and not evidence:
                            cue = daily_guidance(event.text, history)
                    if cue:
                        style += '\n' + cue
                messages = build_context(event, memories, history,
                                         system_prompt() if self.persona is None else system_prompt(self.persona), self.context_chars, response_style=style,
                                         knowledge_text=evidence, past_user_lines=related,
                                         past_assistant_lines=related_self)
                reply = self.provider.generate(messages)
                if not isinstance(reply, str) or not reply.strip() or len(reply) > 8000:
                    raise ProviderError('模型响应为空或过长')
                result = ReplyResult('replied', 'console_only', reply.strip())
            except ProviderError as exc:
                result = ReplyResult('failed', str(exc))
            except Exception:
                result = ReplyResult('failed', '上下文或模型处理失败；内部详情已隐藏，未回退为演示回复')
        self.store.finish(event, result, record_history=self.record_history)
        return result
