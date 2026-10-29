"""Only three anonymous read capabilities; no account or write adapter."""
from urllib.parse import parse_qs, urlsplit

from .actions import ExecutionResult
from .bilibili import (
    BiliReadError, BilibiliClient, INFO_PATH, PLAYER_PATH, SEARCH_PATH,
    PublicTransport, parse_bvid,
)


class FixtureTransport:
    """Small synthetic responses. Never contacts the network or claims real content."""
    def get_json(self, url):
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        if parsed.path == SEARCH_PATH:
            data = {'result': [{'bvid': 'BV1bwnF6eEe8', 'title': '离线示例，非实时搜索结果',
                                'author': '示例作者', 'duration': '0:20'}]}
        elif parsed.path == INFO_PATH:
            data = {'bvid': params['bvid'][0], 'title': '离线示例，非真实视频详情',
                    'desc': '用于验证动作接口', 'duration': 20,
                    'owner': {'mid': 1, 'name': '示例作者'}, 'stat': {},
                    'pages': [{'page': 1, 'cid': 1, 'part': '示例', 'duration': 20}]}
        elif parsed.path == PLAYER_PATH:
            data = {'subtitle': {'subtitles': [{'lan': 'zh-CN',
                    'subtitle_url': '//aisubtitle.hdslb.com/demo.json'}]}}
        elif parsed.hostname == 'aisubtitle.hdslb.com' and parsed.path == '/demo.json':
            return {'body': [{'from': 0, 'to': 1, 'content': '离线示例字幕，不是真实台词'}]}
        else:
            raise BiliReadError('fixture_not_found')
        return {'code': 0, 'data': data}


class BiliReadExecutor:
    def __init__(self, client=None, *, live=False):
        if type(live) is not bool:
            raise BiliReadError('invalid_mode')
        self.live = live
        self.client = client if client is not None else BilibiliClient(
            PublicTransport() if live else FixtureTransport())

    @staticmethod
    def _validated_payload(intent):
        operations = {'bilibili.search': ('query', {'query', 'page', 'limit'}),
                      'bilibili.info': ('bvid', {'bvid'}),
                      'bilibili.subtitles': ('bvid', {'bvid', 'page', 'language'})}
        if intent.capability not in operations or intent.account_id != 'anonymous':
            raise BiliReadError('unsupported_read_capability_or_account')
        required, allowed = operations[intent.capability]
        payload = intent.payload
        if required not in payload or set(payload) - allowed:
            raise BiliReadError('invalid_action_payload')
        target = 'public-search' if required == 'query' else parse_bvid(payload['bvid'])
        if intent.target != target:
            raise BiliReadError('target_does_not_match_payload')
        # URL parsing is a convenience at the CLI. Stored action payloads use only BV IDs.
        if required == 'bvid' and payload['bvid'] != target:
            raise BiliReadError('invalid_action_payload')
        return payload

    def execute(self, intent):
        from ..activity import read_activity_scope
        with read_activity_scope(simulation=not self.live) as context:
            result = self._execute(intent)
            if context is not None and intent.capability in ('bilibili.search', 'bilibili.info', 'bilibili.subtitles'):
                from ..seen_content import ContentLog
                log, mode, trace_id = context
                ContentLog(log.path.with_name('seen-content.sqlite3')).capture(result, intent.capability, mode, trace_id)
            return result

    def _execute(self, intent):
        try:
            payload = self._validated_payload(intent)
            if intent.capability == 'bilibili.search':
                data = self.client.search(**payload)
            elif intent.capability == 'bilibili.info':
                data = self.client.info(**payload)
            else:
                data = self.client.subtitles(**payload)
        except BiliReadError as exc:
            return ExecutionResult('failed', 'B站读取未完成', {
                'reason': exc.code, 'platform_code': exc.platform_code,
                'simulation': not self.live,
            })
        data = {**data, 'simulation': not self.live,
                'source': 'bilibili_public_get' if self.live else 'm25a_offline_fixture'}
        detail = '字幕可用性已检查，未取得字幕正文' if data.get('status') == 'unavailable' else 'B站只读任务完成'
        return ExecutionResult('succeeded' if self.live else 'simulated', detail, data)
