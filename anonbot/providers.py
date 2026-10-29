"""No external calls unless the CLI explicitly selects the live provider."""
import ipaddress
import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from .domain import ProviderError, ValidationError


class MockProvider:
    def generate(self, messages: list[dict[str, str]]) -> str:
        text = messages[-1]['content']
        if '累' in text:
            response = '今天消耗这么大啊。先缓一会儿，想吐槽的话我听着。'
        elif '吉他' in text:
            response = '说到吉他，练顺一小段也挺有成就感的。你刚才卡在哪儿啦？'
        else:
            response = '嗯，我看到你说的啦。这个话题可以接着聊。'
        return '[离线演示／固定模板，非模型人格效果] ' + response


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CompatibleProvider:
    def __init__(self, base_url: str, model: str, api_key: str):
        try:
            url = urllib.parse.urlsplit(base_url)
            host = url.hostname
            _ = url.port  # Validate malformed ports without displaying the URL.
            if any(c.isspace() for c in base_url) or not host or url.username or url.password or url.query or url.fragment:
                raise ValueError()
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host == 'localhost'
            if url.scheme != 'https' and not (url.scheme == 'http' and loopback):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValidationError('模型地址须为 HTTPS；HTTP 仅允许回环地址，且不得含账号、查询参数或片段') from None
        if not model or not api_key or any(ord(c) < 32 for c in model) or any(not 33 <= ord(c) <= 126 for c in api_key):
            raise ValidationError('模型名称和密钥必须非空且不能含控制字符')
        self.url = base_url.rstrip('/') + '/chat/completions'
        self.model, self.api_key = model, api_key
        self.complete_only = False
        self.timeout = 30
        self.last_response_metadata = {}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request_body(self, messages):
        return {'model': self.model, 'messages': messages, 'stream': False,
                'temperature': 0.9, 'max_tokens': 500}

    def generate(self, messages: list[dict[str, str]]) -> str:
        from .activity import network_span
        with network_span('chat', 'generate', self.url) as span:
            return self._generate(messages, span)

    def _generate(self, messages, span):
        self.last_response_metadata = {}
        payload = json.dumps(self.request_body(messages), ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(self.url, data=payload,
                    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.api_key})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
                span.response(response.getcode() if hasattr(response, 'getcode') else 200, len(raw))
                if len(raw) > 2_000_000:
                    raise ProviderError('模型响应超过大小限制')
            data = json.loads(raw.decode('utf-8'))
            if not isinstance(data, dict) or not isinstance(data.get('choices'), list) or not data['choices'] or not isinstance(data['choices'][0], dict):
                raise ProviderError('模型响应结构不兼容；响应正文已隐藏')
            finish = data['choices'][0].get('finish_reason')
            self.last_response_metadata['finish_reason'] = (finish if finish in (
                'stop', 'length', 'tool_calls', 'content_filter', 'insufficient_system_resource') else 'other')
            usage = data.get('usage')
            if isinstance(usage, dict):
                for name in ('completion_tokens', 'prompt_tokens'):
                    value = usage.get(name)
                    if type(value) is int and 0 <= value <= 10_000_000:
                        self.last_response_metadata[name] = value
            if data['choices'][0].get('finish_reason') not in (('stop',) if self.complete_only else (None, 'stop')):
                raise ProviderError('模型输出未完整结束，本次不发送半截回复')
            if not isinstance(data['choices'][0].get('message'), dict):
                raise ProviderError('模型响应缺少有效 message；响应正文已隐藏')
            text = data['choices'][0]['message']['content']
            if not isinstance(text, str) or not text.strip() or len(text) > 8000:
                raise ProviderError('模型返回的文本为空或过长')
            return text.strip()
        except urllib.error.HTTPError as exc:
            span.response(exc.code, 0)
            exc.close()
            raise ProviderError(f'模型 HTTP 错误（{exc.code}）；响应正文已隐藏') from None
        except (OSError, urllib.error.URLError, TimeoutError, http.client.HTTPException):
            raise ProviderError('模型网络请求失败；地址、密钥和底层错误已隐藏，可能已产生服务费用') from None
        except (ValueError, KeyError, IndexError, TypeError, RecursionError):
            raise ProviderError('模型响应格式不兼容；响应正文已隐藏') from None


class DeepSeekProvider(CompatibleProvider):
    """Official Chat Completions; never expose reasoning_content to QQ.

    https://api-docs.deepseek.com/api/create-chat-completion
    """
    def __init__(self, api_key: str, model='deepseek-flash', base_url='https://api.deepseek.com'):
        super().__init__(base_url, model, api_key)
        self.complete_only = True

    def request_body(self, messages):
        return {**super().request_body(messages), 'thinking': {'type': 'disabled'}}


def provider_from_env(live_model: bool):
    if not live_model:
        return MockProvider()
    names = ('ANONBOT_API_BASE_URL', 'ANONBOT_MODEL', 'ANONBOT_API_KEY')
    values = [os.environ.get(name, '') for name in names]
    if any(values):
        if not all(values): raise ValidationError('兼容模型环境配置不完整')
        return CompatibleProvider(*values)
    key = os.environ.get('DEEPSEEK_API_KEY', '')
    if key:
        return DeepSeekProvider(key, os.environ.get('DEEPSEEK_MODEL', 'deepseek-flash'))
    from .settings import load_settings
    settings, secrets = load_settings(with_secrets=True)
    key = secrets.get('deepseek_api_key', '')
    if not key:
        raise ValidationError('请先运行 configure，在本地填写 DeepSeek API 密钥')
    return DeepSeekProvider(key, settings.model, settings.base_url)
