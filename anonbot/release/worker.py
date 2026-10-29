"""Portable private-message worker using existing durable Runtime and loopback receiver."""
from dataclasses import fields
import os
from pathlib import Path

from ..domain import ValidationError
from ..gateway import Runtime, OneBotClient
from ..providers import CompatibleProvider, DeepSeekProvider, MockProvider
from ..server import serve
from ..settings import Settings
from .config import read_secret, strict_json


def configured_chat_provider(path):
    """Use only the chat endpoint/key explicitly configured for this project."""
    path=Path(path).resolve()
    try:
        with path.open('rb') as stream:raw=stream.read(65537)
        data=strict_json(raw)
        if (len(raw)>65536 or type(data) is not dict or type(data.get('version')) is not int
                or data['version']!=1 or type(data.get('settings')) is not dict
                or set(data['settings'])-{f.name for f in fields(Settings)}
                or not isinstance(data.get('api_key_file'),str) or not data['api_key_file']):raise ValueError()
        settings=Settings(**data['settings']);settings.validate()
        key=read_secret((path.parent/data['api_key_file']).resolve())
        return (DeepSeekProvider(key,settings.model,settings.base_url)
                if settings.base_url.rstrip('/')=='https://api.deepseek.com'
                else CompatibleProvider(settings.base_url,settings.model,key))
    except (OSError,ValueError,TypeError,KeyError,RecursionError):
        raise ValidationError('请配置本项目的聊天 API 地址、模型与独立密钥文件') from None


def prepare_worker(path, *, live_model=False, send=False, voice_options=None):
    if type(live_model) is not bool or type(send) is not bool or (send and not live_model):
        raise ValidationError('实际发送必须同时明确启用模型')
    path = Path(path).resolve()
    try:
        with path.open('rb') as stream:
            raw = stream.read(65537)
        data = strict_json(raw)
        if (len(raw) > 65536 or type(data) is not dict or
                set(data) != {'version', 'settings', 'data_dir', 'api_key_file', 'gateway_token_file', 'webhook_secret_file'} or
                type(data['version']) is not int or data['version'] != 1 or type(data['settings']) is not dict or
                set(data['settings']) - {f.name for f in fields(Settings)}):
            raise ValueError()
        settings = Settings(**data['settings'])
        settings.validate()
        def local(name):
            value = data[name]
            if not isinstance(value, str) or not value or len(value) > 2048:
                raise ValueError()
            return (path.parent / value).resolve()
        directory = local('data_dir')
        if settings.private_mode == 'friends' and not live_model:
            raise ValidationError('模拟后台只支持本地白名单；好友核对需要明确启用真实模型模式')
        if settings.private_mode == 'allowlist' and not settings.allowed_users:
            raise ValidationError('请先设置用于验收的私聊白名单')
        secret = read_secret(local('webhook_secret_file'))
        if len(secret) < 16:
            raise ValidationError('HTTP 上报密钥至少 16 个字符')
        provider = MockProvider()
        if live_model:
            provider=configured_chat_provider(path)
        client = None
        if send or settings.private_mode == 'friends':
            client = OneBotClient(settings.gateway_url, read_secret(local('gateway_token_file')), settings.bot_id)
        sender=client if send else None
        if voice_options is not None:
            if not send or not live_model:raise ValidationError('语音选项需要启用模型和实际发送')
            from ..voice_options import load_voice_options
            from ..reply_modes import ModeRoutingSender
            voices,language=load_voice_options(voice_options,lambda:provider)
            sender=ModeRoutingSender(client,voices[language],directory/'qq.reply-modes.sqlite3',
                                     speech_by_language=voices,default_language=language)
        runtime = Runtime(directory / ('qq.sqlite3' if send else 'qq-dry-run.sqlite3'),
                          settings, provider, sender, friend_source=client)
        return runtime, secret, client
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise ValidationError('可移植后台配置无效或凭据未就绪；请检查配置说明') from None


def run_worker(path, *, live_model=False, send=False, voice_options=None):
    runtime, secret, client = prepare_worker(path, live_model=live_model, send=send,voice_options=voice_options)
    previous = os.environ.get('ANONBOT_ACTIVITY_DB')
    os.environ['ANONBOT_ACTIVITY_DB'] = str(runtime.path.parent / 'activity.sqlite3')
    try:
        if client is not None:
            client.verify_identity()
        serve(runtime, secret)
    finally:
        # prepare_worker never starts a thread; serve closes it after server startup.
        runtime.close()
        if previous is None:
            os.environ.pop('ANONBOT_ACTIVITY_DB', None)
        else:
            os.environ['ANONBOT_ACTIVITY_DB'] = previous
