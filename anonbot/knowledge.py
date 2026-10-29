"""Reviewed local character evidence. No network requests or chat-memory writes."""
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

def resource_path(name):
    if name not in ('corpus.json','unknown_claims.json'):
        raise ValueError('Unsupported bundled character resource')
    source=Path(__file__).resolve().parents[1]/'knowledge'/'anon'/name
    return source if source.is_file() else Path(sys.prefix)/'share'/'anonbot'/'knowledge'/'anon'/name


DEFAULT_PATH = resource_path('corpus.json')
LOG = logging.getLogger(__name__)
EMPTY = {'version': 1, 'sources': [], 'entries': []}
STORY_QUERY = re.compile(
    r'剧情|原作|动画|番剧|卡面|主线|台词|mygo|crychic|'
    r'高松灯|立希|素世|爽世|乐奈|祥子|そよりん|ともりん|'
    r'灯(?:当时|怎么|为什么|是怎么)|'
    r'(?:你|爱音).{0,10}(?:留学|英国|回国|水族馆|企鹅|学生会长|转学)|'
    r'你是谁|你叫什么|你叫啥|爱音叫什么|怎么称呼你|第[一二三四五六七八九十\d]+[集话]')
FOLLOWUP_QUERY = re.compile(
    r'^(?:为什么|怎么|她|他|后来|当时|原作有|资料|是怎么|'
    r'能据此|可以据此|这能说明|这说明|'
    r'(?:那|这)?片段.{0,8}(?:她|他|称呼|叫法|回应|反应|台词)|'
    r'(?:那|所以)(?:她|他|你|这|个|后来|当时))')


def _text(value, maximum=500, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError('Invalid knowledge text')
    return value


class KnowledgeBase:
    def __init__(self, data):
        if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1:
            raise ValueError('Unsupported knowledge schema')
        sources, entries = data.get('sources'), data.get('entries')
        if not isinstance(sources, list) or len(sources) > 100:
            raise ValueError('Invalid knowledge sources')
        if not isinstance(entries, list) or len(entries) > 500:
            raise ValueError('Invalid knowledge entries')
        self.sources, self.entries = {}, []
        self.available = True
        for source in sources:
            if not isinstance(source, dict): raise ValueError('Invalid source')
            item = {k: _text(source.get(k)) for k in ('id', 'title', 'url', 'kind', 'checked_on')}
            url = urlsplit(item['url'])
            if url.scheme != 'https' or not url.hostname or url.username or url.password:
                raise ValueError('Source needs public HTTPS reference')
            if item['kind'] not in ('official_profile', 'official_synopsis', 'game_mirror', 'unverified'):
                raise ValueError('Unknown source kind')
            if item['id'] in self.sources: raise ValueError('Duplicate source ID')
            self.sources[item['id']] = item
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict): raise ValueError('Invalid entry')
            item = {k: _text(entry.get(k)) for k in
                    ('id', 'source_id', 'status', 'locator', 'scene', 'audience', 'summary', 'analysis', 'limits')}
            if not re.fullmatch(r'[a-z0-9_-]{1,80}', item['id']) or item['id'] in seen:
                raise ValueError('Invalid or duplicate entry ID')
            seen.add(item['id'])
            if item['source_id'] not in self.sources or item['status'] not in ('reviewed', 'pending'):
                raise ValueError('Invalid entry reference or status')
            item['quote'] = _text(entry.get('quote', ''), 80, empty=True)
            keywords = entry.get('keywords')
            if not isinstance(keywords, list) or not 1 <= len(keywords) <= 40:
                raise ValueError('Invalid keywords')
            item['keywords'] = list(dict.fromkeys(_text(k, 40).casefold() for k in keywords))
            priority = entry.get('priority', 0)
            if type(priority) is not int or not 0 <= priority <= 5:
                raise ValueError('Invalid priority')
            item['priority'] = priority
            self.entries.append(item)
        self.fingerprint = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def for_dialogue(self, query: str, budget=2400, history=()):
        """Only inject story evidence for explicit character or plot questions."""
        text = query[:8000].casefold()
        no_plot = any(word in text for word in ('别讲剧情', '不聊剧情', '不要讲剧情'))
        include_plot = not no_plot and bool(STORY_QUERY.search(text))
        if no_plot:
            return '', []
        result = self._retrieve(query, budget) if include_plot else ('', [])
        if result[1] or not FOLLOWUP_QUERY.search(text):
            return result
        # Resolve a short follow-up only across adjacent user turns in this scope.
        # An intervening ordinary topic breaks the chain; never search all history.
        for message in reversed(history[-12:]):
            if message['role'] != 'user':
                continue
            previous = message['content'][:8000].casefold()
            if any(word in previous for word in ('不聊', '别讲', '换个', '说点别的')):
                break
            if STORY_QUERY.search(previous):
                return self._retrieve(previous + '\n' + query, budget)
            if not FOLLOWUP_QUERY.search(previous):
                break
        return result

    def retrieve(self, query: str, budget=2400):
        """Full evidence for local inspection; chat uses for_dialogue instead."""
        return self._retrieve(query, budget)

    def _retrieve(self, query, budget):
        query = query[:8000].casefold()
        ranked = []
        for item in self.entries:
            source = self.sources[item['source_id']]
            if item['status'] != 'reviewed' or source['kind'] == 'unverified': continue
            matches = [word for word in item['keywords'] if word in query]
            if item['id'] == 'identity' and '叫什么' in matches and not re.search(
                    r'你叫什么|你叫啥|爱音叫什么|怎么称呼你', query):
                matches.remove('叫什么')
            if matches:
                score = sum(min(len(word), 8) for word in matches)
                ranked.append((score, item['priority'], item['id'], item))
        ranked.sort(key=lambda row: (-row[0], -row[1], row[2]))
        selected, ids = [], []
        for _, _, _, item in ranked:
            source = self.sources[item['source_id']]
            material = {'id': item['id'], '出处': source['title'], '定位': item['locator'],
                '来源类型': source['kind'], 'url': source['url'],
                '本项目分析': item['analysis'], '适用限制': item['limits'],
                '用途': '剧情资料', '场景': item['scene'], '对象': item['audience'],
                '剧情概述': item['summary'], '日文短引': item['quote']}
            candidate = json.dumps(selected + [material], ensure_ascii=False, separators=(',', ':'))
            if len(candidate) > max(0, min(budget, 2400)): continue
            selected.append(material)
            ids.append(item['id'])
            if len(selected) == 3: break
        return (json.dumps(selected, ensure_ascii=False, separators=(',', ':')), ids) if selected else ('', [])


def load_knowledge(path=DEFAULT_PATH):
    try:
        with Path(path).open('rb') as handle:
            raw = handle.read(1_000_001)
        if len(raw) > 1_000_000: raise ValueError('Knowledge file too large')
        return KnowledgeBase(json.loads(raw.decode('utf-8')))
    except (OSError, ValueError, RecursionError):
        # No source bodies, user messages, paths or credentials in the warning.
        LOG.warning('Character knowledge unavailable or invalid; using ordinary chat context')
        result = KnowledgeBase(EMPTY)
        result.available = False
        return result
