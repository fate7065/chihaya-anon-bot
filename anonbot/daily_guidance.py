"""Conservative own-daily topic cues; no user text is promoted to instructions.

Callers keep explicit correction and story-evidence handling ahead of these cues.
"""
import re
from .knowledge import STORY_QUERY


_BLOCK = re.compile(r'别|不要|不聊|换.{0,10}话题|帮我|我们|咱|和我|跟我|与我|同我|陪我|一起|[“”「」>\n]')
_OWN = re.compile(r'^(?:(?:那|对了|所以)?你(?:自己)?(?:有没有|有过|最近|买|挑|练|会不会)|最近练(?:琴|吉他))')
_DAILY = re.compile(r'买|喝|吃|配饰|衣服|小东西|点心|练琴|吉他|出门|日常|近况|包装|小事|杯子|整理|喜欢')
_FOLLOW = re.compile(r'^(?:那|所以|当初|这回)?(?:具体|到底|后来|怎么发现|怎么解决|'
                     r'说件具体|说说细节|是哪种|是什么|哪种|什么口味|最让你|'
                     r'你后来|你还|你是怎么|还用|还会|喝完|这件事|那个|要是再挑)')
_START_CUE = ('这轮问的是你自己的项目虚构日常：直接讲一件具体小事，说清是什么、哪里让你有反应。'
              '可用一两条短消息，不必压成一句；只说自己的低风险小事，不补用户事实或原作剧情。')
_FOLLOW_CUE = ('对方在追问你刚才讲的同一件项目虚构日常：沿用已有物品、细节和进度，'
               '只补这轮被问到的新信息，不换另一件事，不重复泛泛的形容。'
               '已说没有经历或尚未购买也要保持，不因追问改成已经发生；不补用户事实或原作剧情。')


def _own_request(text):
    return bool(_OWN.search(text) and _DAILY.search(text))


def daily_guidance(text: str, history=()) -> str:
    text = text.strip()
    if len(text) > 160 or _BLOCK.search(text) or STORY_QUERY.search(text.casefold()):
        return ''
    if _own_request(text):
        return _START_CUE
    if '我' in text or not _FOLLOW.search(text):
        return ''
    # An unrelated user turn ends the topic. Older stories cannot revive it.
    users = [row['content'].strip() for row in history[-6:] if row['role'] == 'user']
    for previous in reversed(users):
        if (_BLOCK.search(previous) or STORY_QUERY.search(previous.casefold())
                or len(previous) > 160):
            return ''
        if _own_request(previous):
            return _FOLLOW_CUE
        if '我' in previous or not _FOLLOW.search(previous):
            return ''
    return ''
