"""Explicit offline candidate preview; no HTTP, QQ transport or RVC imports."""

import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from uuid import uuid4


FRAMEWORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRAMEWORK))
from anonbot.voice import VoiceError, VoiceProfile, inspect_wav
from anonbot.voice_candidate import CandidateConfig, clarify_directional_phones, file_digest, split_voice_phrases


def load_offline_pipeline(config, directory):
    # GPT-SoVITS writes its settings while loading weights; isolate those writes.
    runtime_config = directory / 'backend-runtime.yaml'
    with runtime_config.open('xb') as stream:
        stream.write(config.assets['backend_config'].read_bytes())
    api = config.source_dir / 'api_v2.py'
    arguments = sys.argv
    try:
        sys.argv = [str(api), '-a', '127.0.0.1', '-p', '0', '-c', str(runtime_config)]
        namespace = runpy.run_path(str(api), run_name='anonbot_direct_offline_candidate')
        pipeline = namespace['tts_pipeline']
        # Reference extraction spends RNG; finish it before run() seeds audio sampling.
        pipeline.set_ref_audio(str(config.assets['reference_audio']))
        return pipeline, runtime_config
    finally:
        sys.argv = arguments


def save_report(directory, report):
    pending = directory / 'results.tmp'
    pending.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    pending.replace(directory / 'results.json')
    lines = ['# 直接合成候选：整句预览', '', '本次未启用QQ或启动HTTP服务。', '',
             '已认可样音不代表任意文本均达标；本报告的生成结果需按文本核对听感。', '',
             '状态：' + report['status'], '']
    for item in report['clips']:
        lines += [item['text'], '', f"![整句试听](<{Path(item['audio']).as_posix()}>)", '']
    if report.get('error'):
        lines += ['本次未完成：' + report['error'], '']
    (directory / '试听报告.md').write_text('\n'.join(lines), 'utf-8')


def build_preview_request(config, text, prompt):
    return dict(text=text, text_lang=config.text_language, ref_audio_path=str(config.assets['reference_audio']),
                prompt_text=prompt, prompt_lang='ja',
                text_split_method='cut0' if config.phrase_policy == 'whole-utterance-v1' else 'cut5',
                batch_size=1, seed=42, parallel_infer=False, speed_factor=1.0, top_k=15,
                top_p=1.0, temperature=1.0, use_cuda_graph=False, use_flash_attention=False)


def preview(config, texts, output):
    phrases = [split_voice_phrases(text, policy=config.phrase_policy, language=config.text_language) for text in texts]
    if not output.is_absolute() or not 1 <= len(texts) <= 3:
        raise VoiceError('预览须指定绝对输出目录，每次1到3条')
    profile = VoiceProfile.load(config.assets['profile'])
    prompt = config.assets['reference_text'].read_text('utf-8').strip()
    if (profile.profile_id != config.candidate_id or profile.prompt_language != 'ja'
            or profile.text_language != config.text_language
            or profile.reference_audio != config.assets['reference_audio'] or profile.prompt_text != prompt):
        raise VoiceError('候选客户端配置与参考音配对不一致')
    directory = output / ('voice-direct-' + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    report = dict(status='initializing', candidate_id=config.candidate_id, mode='direct',
                  phrase_policy=config.phrase_policy,
                  text_language=config.text_language,
                  reference_preloaded=False,
                  model_policy='GPT: ' + config.assets['gpt'].name + '; SoVITS: ' + config.assets['sovits'].name,
                  qq_sent=False, http_started=False, rvc_used=False, human_review='pending',
                  assets={k:dict(path=str(v), sha256=file_digest(v)) for k, v in config.assets.items()},
                  clips=[], phoneme_events=[])
    save_report(directory, report)
    original = None
    frontend = None
    try:
        if subprocess.check_output(['git', '-C', str(config.source_dir), 'rev-parse', 'HEAD'], text=True).strip() != config.source_revision:
            raise VoiceError('候选语音源码版本已变化')
        os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                          TORCH_FORCE_WEIGHTS_ONLY_LOAD='1', HF_HUB_DISABLE_TELEMETRY='1')
        for key in ('HF_HOME', 'HF_HUB_CACHE', 'TEMP', 'TMP', 'NUMBA_CACHE_DIR', 'MPLCONFIGDIR', 'TORCH_HOME'):
            folder = directory / 'cache' / key.lower()
            folder.mkdir(parents=True, exist_ok=True)
            os.environ[key] = str(folder)
        sys.path[:0] = [str(config.source_dir), str(config.source_dir / 'GPT_SoVITS')]
        from tools.portable_runtime import activate
        activate(change_cwd=True)
        import numpy as np
        import soundfile as sf
        import torch
        import yaml
        import tools.acceleration as acceleration
        import voice_backend
        import voice_dictionary
        torch.set_num_threads(4)
        voice_backend.install_convolution_policy(acceleration, torch)
        os.environ['OPEN_JTALK_DICT_DIR'] = voice_dictionary.prepare_installed_dictionary(config.source_dir)
        settings = yaml.safe_load(config.assets['backend_config'].read_text('utf-8'))['custom']
        if (settings['device'] != 'cpu' or settings['is_half'] is not False or settings['version'] != 'v2'
                or Path(settings['t2s_weights_path']).resolve() != config.assets['gpt']
                or Path(settings['vits_weights_path']).resolve() != config.assets['sovits']):
            raise VoiceError('候选后端配方与清单不一致')
        pipeline, runtime_config = load_offline_pipeline(config, directory)
        report['reference_preloaded'] = True
        report['runtime_config'] = dict(path=str(runtime_config), sha256=file_digest(runtime_config))
        if (pipeline.configs.device != 'cpu' or pipeline.precision != torch.float32
                or Path(pipeline.configs.configs_path).resolve() != runtime_config.resolve()):
            raise VoiceError('实际载入的精度、设备或配置与候选不一致')
        from text import chinese2
        frontend, original = chinese2, chinese2.g2p

        def guarded(text):
            phones, counts = original(text)
            result, changes = clarify_directional_phones(text, phones, counts)
            report['phoneme_events'].append(dict(text=text, changes=changes))
            return result, counts

        frontend.g2p = guarded
        report['status'] = 'running'
        for number, (text, segments) in enumerate(zip(texts, phrases), 1):
            parts, records = [], []
            for index, segment in enumerate(segments, 1):
                request = build_preview_request(config, segment, prompt)
                results = list(pipeline.run(request))
                if len(results) != 1:
                    raise VoiceError('候选未返回单个完整片段')
                rate, audio = results[0]
                if (rate != 32000 or not isinstance(audio, np.ndarray)
                        or audio.dtype != np.int16 or audio.ndim != 1 or not np.any(audio)):
                    raise VoiceError('候选返回的片段格式或内容无效')
                raw_path = directory / f'S{number:02}-P{index:02}.wav'
                sf.write(raw_path, audio, rate, subtype='PCM_16')
                inspect_wav(raw_path.read_bytes())
                energy = np.array([np.sqrt(np.mean(audio[i:i + 640].astype(float)**2))
                                   for i in range(0, len(audio), 640)])
                active = np.flatnonzero(energy > max(50, min(200, float(energy.max()) * .015)))
                if not len(active):
                    raise VoiceError('候选片段没有有效声音')
                start = max(0, int(active[0]) * 640 - 1920)
                end = min(len(audio), (int(active[-1]) + 1) * 640 + 1920)
                if parts:
                    parts.append(np.zeros(round(rate * config.join_pause_seconds), dtype=np.int16))
                parts.append(audio[start:end])
                records.append(dict(text=segment, audio=str(raw_path), sha256=file_digest(raw_path),
                                    source_range_seconds=[start/rate, end/rate], request=request))
            if sum(len(part) for part in parts) > 32000 * 30:
                raise VoiceError('完整预览超过30秒，请缩短文本')
            joined = directory / f'S{number:02}-full.wav'
            sf.write(joined, np.concatenate(parts), 32000, subtype='PCM_16')
            seconds = inspect_wav(joined.read_bytes())
            report['clips'].append(dict(text=text, audio=str(joined), sha256=file_digest(joined),
                                        seconds=seconds, phrases=records))
            save_report(directory, report)
        report['status'] = 'generated'
    except (OSError, RuntimeError, ValueError, ImportError, subprocess.SubprocessError) as exc:
        report['status'], report['error'] = 'failed', str(exc)
        raise
    finally:
        if original is not None:
            frontend.g2p = original
        save_report(directory, report)
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--text', action='append', required=True)
    args = parser.parse_args()
    try:
        directory = preview(CandidateConfig.load(args.manifest), args.text, args.output)
        print(json.dumps(dict(status='generated', report=str(directory/'试听报告.md'), qq_sent=False), ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, ValueError, ImportError, subprocess.SubprocessError) as exc:
        print(json.dumps(dict(status='error', reason=str(exc), qq_sent=False), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
