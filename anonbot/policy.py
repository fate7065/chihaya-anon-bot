from dataclasses import dataclass
from .domain import MessageEvent, ReplyDecision


@dataclass
class ReplyPolicy:
    quiet: bool = False
    group_all: bool = False

    def decide(self, event: MessageEvent) -> ReplyDecision:
        if event.scope.owner_id == event.scope.bot_id:
            return ReplyDecision(False, 'self_message')
        if self.quiet:
            return ReplyDecision(False, 'quiet')
        if event.scope.visibility == 'private' or event.mentioned or self.group_all:
            return ReplyDecision(True, 'addressed')
        return ReplyDecision(False, 'group_not_mentioned')
