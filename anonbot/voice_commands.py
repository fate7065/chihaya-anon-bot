"""Voice preview commands never send QQ messages or read model/account secrets."""
import json
from pathlib import Path

from .voice import LocalSpeech, VoiceError, VoiceProfile


def register(commands):
    voice = commands.add_parser('voice', help='本机语音配置检查与试听，不发QQ消息')
    actions = voice.add_subparsers(dest='voice_action', required=True)
    for name in ('status', 'synthesize', 'audition'):
        command = actions.add_parser(name)
        command.add_argument('--profile', type=Path, required=True)
        if name == 'synthesize':
            command.add_argument('--text', required=True)
        elif name == 'audition':
            command.add_argument('--output', type=Path, required=True)


def run(args):
    try:
        profile = VoiceProfile.load(args.profile)
        if args.voice_action == 'status':
            result = dict(status='configured', profile=profile.profile_id, output_language=profile.text_language,
                          endpoint=profile.endpoint, message='只检查本地配置，未确认语音服务运行或音色效果')
        elif args.voice_action == 'audition':
            if profile.text_language != 'zh':
                raise VoiceError('集中试听集为中文；日语请用synthesize并提供日语台词')
            from .voice_audition import run_audition
            result = run_audition(profile, args.output)
        else:
            clip = LocalSpeech(profile).synthesize(args.text)
            result = dict(status='synthesized', audio=str(clip.path), sha256=clip.sha256,
                          seconds=clip.seconds, output_language=profile.text_language, qq_sent=False)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result['status'] == 'partial' else 0
    except VoiceError as exc:
        print(json.dumps(dict(status='error', reason=str(exc), qq_sent=False), ensure_ascii=False))
        return 2
