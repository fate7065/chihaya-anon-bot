"""Fixed local Chinese audition; signal diagnostics never certify voice quality."""
import array
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import statistics
import sys
import time
from uuid import uuid4
import wave

from .voice import LocalSpeech, VoiceError, VoiceProfile, inspect_wav


CASES = (
    ('短应答', '我在呢，刚看到你的消息'),
    ('日常陈述', '今天回家的路上风好大，我的头发都吹乱了'),
    ('自然问句', '你后来买到想喝的那瓶饮料了吗'),
    ('停顿与语气', '欸，等一下，这个颜色是不是更好看'),
    ('轻松回应', '还好你出门前想起来了，不然又得跑一趟'),
    ('安静回应', '嗯，你接着说，我听着呢'),
    ('时间数字', '明天下午三点半见，记得提前十分钟出门'),
    ('价格小数', '这瓶十二块五，两瓶一共二十五块'),
    ('中文人名', '我是千早爱音，今天也有认真练吉他哦'),
    ('中英混合', '你是从MyGO的演出视频认识我们的吗'),
    ('平台名称', '你发到QQ上的那个B站视频，我刚点开'),
    ('较长双句', '你说那家店昨天还摆得满满的，今天就全卖完了？那也太巧了吧，偏偏赶在你想喝的时候'),
)


def signal_stats(raw: bytes):
    seconds = inspect_wav(raw)
    with wave.open(io.BytesIO(raw), 'rb') as audio:
        values = array.array('h', audio.readframes(audio.getnframes()))
    if sys.byteorder != 'little':
        values.byteswap()
    count = len(values)
    return dict(seconds=seconds, peak=max(abs(x) for x in values)/32768,
                rms=round(math.sqrt(sum(x*x for x in values)/count)/32768, 8),
                saturation_ratio=round(sum(x in (-32768, 32767) for x in values)/count, 8),
                low_level_ratio=round(sum(abs(x) <= 32 for x in values)/count, 8),
                silent=not any(values))


def _save_report(directory, document):
    # A complete JSON checkpoint survives interruption between cases.
    pending = directory/'results.tmp'
    pending.write_text(json.dumps(document, ensure_ascii=False, indent=2), 'utf-8')
    pending.replace(directory/'results.json')
    lines = ['# 爱音中文语音试听', '',
             '这是固定测试文本的本地合成，不是实际聊天记录；未发送QQ消息。', '',
             f"生成状态：{document['status']}；已尝试{len(document['cases'])}/{len(CASES)}条。", '',
             '音色、中文准确性、语气自然度和真实QQ播放均待人工验收。',
             'M37/M38逐采样误差门槛未全部通过；本次信号统计不替代或更改该门槛。', '',
             '请逐条听有无漏字、错读、重复、断句突兀或声音不像角色；记录具体时间位置。',
             '低电平包含正常停顿；峰值和饱和比例只是排查线索，不是质量分数。', '']
    for item in document['cases']:
        lines += [f"## {item['id']}. {item['category']}", '', item['text'], '']
        if item['status'] == 'generated':
            lines += [f"![试听](<{Path(item['audio']).as_posix()}>)", '',
                      f"合成{item['elapsed_seconds']:.2f}秒 / 音频{item['signal']['seconds']:.2f}秒", '',
                      f"排查提示：{'、'.join(item['warnings']) or '无信号异常提示'}", '']
        else:
            lines += [f"本条失败：{item['reason']}", '']
        lines += ['- [ ] 中文内容完整且读音正确', '- [ ] 语气和停顿自然',
                  '- [ ] 声音符合预期', '- 试听意见：待填写', '']
    (directory/'试听报告.md').write_text('\n'.join(lines), 'utf-8')


def run_audition(profile: VoiceProfile, output: Path):
    if not output.is_absolute():
        raise VoiceError('试听输出位置必须是绝对路径')
    try:
        directory = output.resolve()/('voice-audition-'+uuid4().hex)
        directory.mkdir(parents=True, exist_ok=False)
        speech = LocalSpeech(replace(profile, output_dir=directory/'audio'))
        document = dict(version=1, profile_id=profile.profile_id,
                        created_at=datetime.now(timezone.utc).isoformat(), status='running',
                        target_language='zh', qq_sent=False, human_review='pending',
                        numerical_gate='not_evaluated', qq_playback='not_tested', cases=[])
        _save_report(directory, document)
        for index, (category, text) in enumerate(CASES, 1):
            item = dict(id=index, category=category, text=text, human_review='pending')
            started = time.perf_counter()
            try:
                clip = speech.synthesize(text)
                path = clip.validate(speech.profile.output_dir)
                signal = signal_stats(path.read_bytes())
                warnings = []
                if signal['silent']:
                    warnings.append('全静音')
                if signal['saturation_ratio'] >= .001:
                    warnings.append('饱和采样较多，请试听是否失真')
                item.update(status='generated', audio=str(path), sha256=clip.sha256,
                            signal=signal, warnings=warnings)
            except VoiceError as exc:
                item.update(status='failed', reason=str(exc))
            item['elapsed_seconds'] = round(time.perf_counter()-started, 4)
            document['cases'].append(item)
            _save_report(directory, document)
        generated = [x for x in document['cases'] if x['status'] == 'generated']
        document['status'] = 'complete' if len(generated) == len(CASES) else 'partial'
        document['generated_count'] = len(generated)
        document['median_synthesis_seconds'] = statistics.median(x['elapsed_seconds'] for x in generated) if generated else None
        _save_report(directory, document)
        return dict(status=document['status'], report=str(directory/'试听报告.md'),
                    manifest=str(directory/'results.json'), generated_count=len(generated),
                    human_review='pending', qq_sent=False)
    except OSError:
        raise VoiceError('本地试听文件无法读取或保存，请检查输出位置；未发送QQ消息') from None
