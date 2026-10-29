"""Explicit local-only candidate speech service; does not start or send QQ."""
import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

FRAMEWORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRAMEWORK))
from anonbot.voice import VoiceError, VoiceProfile
from anonbot.voice_candidate_service import CandidateRenderer, create_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path,
                        default=FRAMEWORK / 'candidates/voice-direct-v1/candidate.json')
    args = parser.parse_args()
    try:
        # Hash validation and all paths belong to the local manifest, never HTTP input.
        from anonbot.voice_candidate import CandidateConfig
        config = CandidateConfig.load(args.manifest)
        profile = VoiceProfile.load(config.assets['profile'])
        if (profile.profile_id != config.candidate_id or profile.text_language != config.text_language
                or profile.reference_audio != config.assets['reference_audio']
                or profile.prompt_text != config.assets['reference_text'].read_text('utf-8').strip()
                or profile.prompt_language != 'ja' or urlsplit(profile.endpoint).hostname != '127.0.0.1'):
            raise VoiceError('本机候选客户端配置不匹配')
        renderer = CandidateRenderer(args.manifest, profile.output_dir.parent / 'service-jobs')
        with create_server(renderer, port=urlsplit(profile.endpoint).port,
                           candidate_id=config.candidate_id, text_language=config.text_language) as server:
            print(json.dumps(dict(status='ready', endpoint=profile.endpoint,
                model_warm=False, mode='isolated-task-per-request', qq_sent=False)), flush=True)
            server.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps(dict(status='error', reason=str(exc), qq_sent=False)), flush=True)
        return 2


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
