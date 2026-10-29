"""Loopback-only bridge to the validated offline voice CLI; no QQ operations."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
from uuid import uuid4

from .voice import VoiceClip, VoiceError, inspect_wav
from .voice_candidate import CandidateConfig, file_digest, split_voice_phrases


class CandidateRenderer:
    """Use a fresh, bounded task per request so the accepted CLI stays authoritative."""
    def __init__(self, manifest, output_dir, *, runner=subprocess.run):
        self.manifest = Path(manifest).resolve(strict=True)
        self.config = CandidateConfig.load(self.manifest)
        self.manifest_sha256 = file_digest(self.manifest)
        self.assets = {name: dict(path=str(path), sha256=file_digest(path))
                       for name, path in self.config.assets.items()}
        if self.config.phrase_policy != 'whole-utterance-v1':
            raise VoiceError('本机接口须使用已验收的整句候选')
        self.python = self.config.source_dir.parent / '.venv' / ('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        self.cli = Path(__file__).resolve().parents[1] / 'tools/voice_candidate_preview.py'
        if not self.python.is_file() or not self.cli.is_file():
            raise VoiceError('本机整句合成环境不完整')
        self.output_dir = Path(output_dir)
        if not self.output_dir.is_absolute():
            raise VoiceError('合成任务目录必须是绝对路径')
        self.runner = runner

    def __call__(self, text):
        text = split_voice_phrases(text, policy='whole-utterance-v1', language=self.config.text_language)[0]
        if file_digest(self.manifest) != self.manifest_sha256:
            raise VoiceError('候选清单已改变，请核对后重新启动接口')
        job = self.output_dir / ('task-' + uuid4().hex)
        job.mkdir(parents=True, exist_ok=False)
        state = dict(status='running', candidate_id=self.config.candidate_id)
        try:
            args = [str(self.python), str(self.cli), '--manifest', str(self.manifest),
                    '--output', str(job), '--text', text]
            env = dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1')
            with (job / 'worker.log').open('xb') as log:
                result = self.runner(args, cwd=self.cli.parents[1], env=env,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=150,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if result.returncode != 0:
                raise VoiceError('独立语音任务未完成')
            reports = list(job.glob('voice-direct-*/results.json'))
            if len(reports) != 1 or reports[0].stat().st_size > 65536:
                raise VoiceError('独立任务结果清单不正确')
            report = json.loads(reports[0].read_text('utf-8'))
            if (not isinstance(report, dict) or report.get('status') != 'generated'
                    or report.get('candidate_id') != self.config.candidate_id
                    or report.get('phrase_policy') != 'whole-utterance-v1'
                    or report.get('text_language', 'zh') != self.config.text_language
                    or report.get('reference_preloaded') is not True
                    or report.get('assets') != self.assets
                    or report.get('qq_sent') is not False or report.get('http_started') is not False
                    or not isinstance(report.get('clips'), list) or len(report['clips']) != 1):
                raise VoiceError('独立任务配方或结果不一致')
            clip = report['clips'][0]
            if (clip['text'] != text or len(clip['phrases']) != 1
                    or clip['phrases'][0]['request']['text_split_method'] != 'cut0'):
                raise VoiceError('独立任务文字或整句方式不一致')
            verified = VoiceClip(Path(clip['audio']), clip['sha256'], 0).validate(job)
            raw = verified.read_bytes()
            inspect_wav(raw)
            state.update(status='generated', sha256=clip['sha256'])
            return raw
        except (OSError, ValueError, KeyError, TypeError, RecursionError, subprocess.SubprocessError):
            state['status'] = 'failed'
            raise VoiceError('本机独立语音任务失败，请检查任务日志；没有自动重试') from None
        finally:
            (job / 'task-result.json').write_text(json.dumps(state, indent=2), 'utf-8')


def create_server(render, *, port, candidate_id, text_language='zh'):
    """Only the service owns paths/models/parameters; callers supply Chinese text."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise VoiceError('本机语音端口不正确')
    if text_language not in ('zh', 'ja'):
        raise VoiceError('本机朗读语言不正确')
    busy = threading.BoundedSemaphore(1)
    compatible = {'text', 'text_lang', 'ref_audio_path', 'prompt_lang', 'prompt_text',
                  'text_split_method', 'batch_size', 'media_type', 'streaming_mode',
                  'seed', 'parallel_infer', 'speed_factor'}

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)
            self.body_read = False

        def log_message(self, *args):
            pass  # Worker task files retain diagnostics; HTTP logs must not copy chat text.

        def reply(self, status, raw, content_type='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(raw)
            except OSError:
                pass  # A disconnected caller never triggers a second synthesis.

        def error(self, status, code, message):
            # Drain bounded, explicitly framed rejected bodies before closing on Windows.
            lengths = self.headers.get_all('Content-Length', [])
            if (self.command == 'POST' and not self.body_read and len(lengths) == 1
                    and len(lengths[0]) <= 5 and lengths[0].isascii()
                    and lengths[0].isdigit() and int(lengths[0]) <= 65536):
                try:
                    self.rfile.read(int(lengths[0]))
                except OSError:
                    pass
                self.body_read = True
            self.reply(status, json.dumps(dict(error=dict(code=code, message=message)),
                                         ensure_ascii=False).encode('utf-8'))

        def local_request(self):
            if (self.headers.get_all('Host') != [f'127.0.0.1:{self.server.server_port}']
                    or self.headers.get_all('Origin')):
                self.error(403, 'local_client_required', '只接受本机客户端')
                return False
            return True

        def do_GET(self):
            if not self.local_request():
                return
            if self.path != '/anonbot/health':
                self.error(404, 'not_found', '接口不存在')
                return
            self.reply(200, json.dumps(dict(service='anonbot-direct-voice', version=1,
                ready=True, model_warm=False, mode='isolated-task-per-request',
                candidate_id=candidate_id, text_language=text_language, pid=os.getpid())).encode('utf-8'))

        def do_POST(self):
            if not self.local_request():
                return
            if self.path != '/tts':
                self.error(404, 'not_found', '接口不存在')
                return
            lengths = self.headers.get_all('Content-Length', [])
            if (self.headers.get('Transfer-Encoding') or len(lengths) != 1
                    or not lengths[0].isascii() or not lengths[0].isdigit()):
                self.error(400, 'invalid_length', '请求长度不正确')
                return
            if len(lengths[0]) > 5:
                self.error(413, 'too_large', '请求过大')
                return
            length = int(lengths[0])
            if length > 8192:
                self.error(413, 'too_large', '请求过大')
                return
            if self.headers.get_content_type() != 'application/json':
                self.error(415, 'json_required', '仅接受JSON')
                return
            try:
                raw = self.rfile.read(length)
                self.body_read = True
                if len(raw) != length:
                    raise VoiceError('Incomplete request')
                body = json.loads(raw.decode('utf-8'))
                if (not isinstance(body, dict) or set(body) - compatible
                        or body.get('text_lang', text_language) != text_language):
                    raise VoiceError('Invalid request fields')
                text = split_voice_phrases(body.get('text'), policy='whole-utterance-v1', language=text_language)[0]
            except (OSError, ValueError, RecursionError):
                self.error(422, 'invalid_text', '请输入80字以内、符合配置语言的短文本')
                return
            if not busy.acquire(blocking=False):
                self.error(409, 'busy', '已有语音任务正在运行，请勿自动重试')
                return
            try:
                raw = render(text)
                inspect_wav(raw)
                self.reply(200, raw, 'audio/wav')
            except (OSError, ValueError):
                self.error(503, 'synthesis_failed', '本机语音任务未完成，没有自动重试')
            finally:
                busy.release()

    class Server(ThreadingHTTPServer):
        # Normal shutdown must wait for the bounded worker instead of orphaning it.
        daemon_threads = False
        allow_reuse_address = False
        request_queue_size = 4

    return Server(('127.0.0.1', port), Handler)
