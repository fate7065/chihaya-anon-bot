"""Small, fixed cues for explicit conversation requests; never promote user text."""
import re
from .knowledge import STORY_QUERY


def turn_guidance(text: str) -> str:
    topic_requests = ('挑个话题', '起个话题', '起个话头',
                      '你自己说', '接着说你的看法', '继续说你的看法')
    change_topic = ('说点别的' in text or bool(re.search(r'换.{0,10}话题', text))
                    or ('不聊' in text and '换一个' in text)
                    or ('换个' in text and '不相关' in text and '你来说' in text))
    if change_topic:
        tail = re.search(r'(?:换.{0,10}话题|说点别的|换一个)[，,。:： ]+(.+)', text)
        if tail and tail.group(1).startswith(('我', '今天', '昨天', '刚才', '这件', '那个')):
            return '对方已经给出新话题：只接新内容，不再自己起话题，也别提旧话题。不要补对方未说的情境，例如忘带伞不等于已经下雨。'
    if change_topic or any(phrase in text for phrase in topic_requests):
        change = '彻底换到不相关的主题，别沿着刚才的事件联想，也别用「说到」过渡。' if change_topic else ''
        return ('这轮由你带话题：' + change +
                '挑一个带个人审美或流行兴趣的具体话题，给看法和半句理由，别讲生活技巧。'
                '不要反问或让对方选择；是否聊虚构日常按角色卡边界处理，不编与用户共同线下经历。')
    if any(phrase in text for phrase in ('先别聊这个', '这个先不聊', '先不聊这事', '不想聊这个')):
        tail = re.search(r'(?:先别聊这个|这个先不聊|先不聊这事|不想聊这个)[，,。:： ]+(.+)', text)
        if tail and tail.group(1).startswith(('我', '今天', '昨天', '刚才', '这件', '那个')):
            return '对方已给出新话题：接新内容，不回到刚才那件事，也别插入自己的日常。'
        return '对方暂时不想聊这件事：简短应下，暂不另起话题或谈自己的日常；等对方另提话题。'
    if '别教我' in text or '别给我建议' in text:
        return '对方只想吐槽：停止建议，接住这件具体吐槽即可；不解释，也不争论或无依据承认刚才是否在教。'
    if any(phrase in text for phrase in ('你怎么每次', '你怎么又', '你又在', '你别总', '你不用总', '别教我')):
        return '对方指出你的聊天方式问题：先核对最近回复；确实如此就简单认下并调整，若对方误会则轻轻澄清。不解释为什么这样说，也别反问或保证永远不会再犯。'
    correction = (any(phrase in text for phrase in ('我刚才说成', '刚才说错', '我说错了'))
                  or ('看错' in text and '是' in text)
                  or bool(re.search(r'不是.{1,20}[，, ]是', text)))
    if correction:
        return '这轮是事实更正：只确认新的信息；对方自我更正时不用承认自己听错，确实是你听错才认。不要评价它更好、更值得或更有意义，也不要猜任何人的动机、关系或感受。'
    if any(phrase in text for phrase in ('就一句就行', '一句就行', '给我一句')):
        return '对方只要一句话：只给可以直接使用的最终句子，不解释写法，也不再问要不要改。'
    return ''


def fact_guidance(text: str) -> str:
    """Experimental factual cue; caller controls whether this is enabled."""
    if (re.search(r'忘(?:了)?带伞|没带伞|伞忘(?:在|带)', text)
            and not re.search(r'下雨|淋雨|晴天|暴雨', text)
            and not STORY_QUERY.search(text.casefold())):
        return ('事实边界：忘带伞本身不能证明正在下雨或已经淋雨。只按用户已明确的天气接话；'
                '天气未知时不预设躲雨、淋回家，也不用为了接话追问天气。')
    return ''


def natural_guidance(text: str) -> str:
    """Additional intent cues used only by the explicit natural persona trial."""
    if any(phrase in text for phrase in ('我就吐槽', '就想吐槽', '只是吐槽', '就想抱怨', '只是抱怨')):
        return ('这轮对方明确只想吐槽：留在这件具体事情上，接一下烦恼即可；'
                '不替对方算值不值，不提供解决办法，也不把话题转到自己。')
    return fact_guidance(text)
