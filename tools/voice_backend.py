"""Project-local entry point for GPT-SoVITS with dynamic-length convolution policy.

Upstream Accel/PyTorch/__init__.py enables cuDNN benchmarking lazily. With
different utterance lengths, that can dominate inference. Reset only that
flag after the acceleration factory, preserving the chosen backend and
upstream precision settings. Third-party sources and weights stay unchanged.

https://docs.pytorch.org/docs/2.11/backends.html#torch.backends.cudnn.benchmark
"""
from functools import wraps
import logging
import math
import os
from pathlib import Path
import runpy
import sys
import time


def install_convolution_policy(acceleration, torch_module):
    torch_module.backends.cudnn.benchmark = False
    original = acceleration.create_acceleration
    if getattr(original, '_anonbot_convolution_policy', False):
        return

    @wraps(original)
    def create_acceleration(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            # The factory can import Accel and overwrite the flag, even on error.
            torch_module.backends.cudnn.benchmark = False
            logging.getLogger('anonbot.voice').info('Dynamic speech convolution policy: cudnn.benchmark=False')

    create_acceleration._anonbot_convolution_policy = True
    acceleration.create_acceleration = create_acceleration


def warm_up(pipeline, reference: Path):
    """Exercise real Chinese synthesis once; never send, play or store a message."""
    request = dict(text='我在呢，今天想聊点什么呀', text_lang='zh', ref_audio_path=str(reference),
                   prompt_lang='ja', prompt_text='', text_split_method='cut5', batch_size=1,
                   seed=42, parallel_infer=False, speed_factor=1.0)
    started = time.perf_counter()
    iterator = iter(pipeline.run(request))
    end = object()
    try:
        item = next(iterator, end)
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise RuntimeError('语音预热未返回完整音频')
        rate, audio = item
        if type(rate) is not int or not 8000 <= rate <= 48000:
            raise RuntimeError('语音预热采样率不正确')
        count = getattr(audio, 'size', 0)
        if getattr(audio, 'ndim', None) != 1 or not rate * 0.1 <= count <= rate * 30:
            raise RuntimeError('语音预热音频为空或长度异常')
        audible = False
        for value in audio:
            if not math.isfinite(float(value)):
                raise RuntimeError('语音预热音频含无效数值')
            audible = audible or value != 0
        if not audible:
            raise RuntimeError('语音预热只生成静音')
        if next(iterator, end) is not end:
            raise RuntimeError('语音预热返回多段音频，未完成就绪检查')
        return dict(samples=int(count), sample_rate=rate, audio_seconds=count/rate,
                    elapsed_seconds=round(time.perf_counter()-started, 4))
    finally:
        close = getattr(iterator, 'close', None)
        if callable(close):
            close()


def serve_ready(api, reference: Path):
    if api.get('host') != '127.0.0.1':
        raise RuntimeError('本项目语音服务只允许绑定127.0.0.1')
    logging.getLogger('anonbot.voice').info('Voice startup: warming fixed Chinese text before binding HTTP')
    warmup = warm_up(api['tts_pipeline'], reference)

    def health():
        return dict(service='anonbot-voice', version=1, warm=True, warmup=warmup, pid=os.getpid())

    api['APP'].add_api_route('/anonbot/health', health, methods=['GET'])
    logging.getLogger('anonbot.voice').info('Voice startup: warmup complete, starting loopback HTTP')
    api['uvicorn'].run(app=api['APP'], host=api['host'], port=api['port'], workers=1)


def main():
    from voice_dictionary import prepare_installed_dictionary
    source = Path(__file__).resolve().parents[2] / 'runtime/voice-gptsovits/GPT-SoVITS'
    os.environ['TORCH_FORCE_WEIGHTS_ONLY_LOAD'] = '1'
    sys.path.insert(0, str(source))
    from tools.portable_runtime import activate
    activate(change_cwd=True)
    # Source: pyopenjtalk 0.4 __init__.py/_extract_dic; prepare before frontend import.
    os.environ['OPEN_JTALK_DICT_DIR'] = prepare_installed_dictionary(source)
    import torch
    import tools.acceleration as acceleration
    install_convolution_policy(acceleration, torch)
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('anonbot.voice').info('Local voice adapter enabled; cuDNN remains enabled, benchmarking disabled')
    api = source / 'api_v2.py'
    sys.argv[0] = str(api)
    # Import the original app without its __main__ listener, so cold initialization
    # cannot be confused with a ready-to-speak service.
    namespace = runpy.run_path(str(api), run_name='anonbot_voice_api')
    reference = source.parents[1] / 'voice-assets/chihaya-anon-v2/anon-reference.wav'
    serve_ready(namespace, reference)


if __name__ == '__main__':
    main()
