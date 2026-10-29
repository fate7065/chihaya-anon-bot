"""Start only this project's isolated, loopback GPT-SoVITS service; never stop QQ."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parent / 'runtime/voice-gptsovits'
sys.path.insert(0, str(ROOT))
from anonbot.providers import NoRedirect
sys.stdout.reconfigure(encoding='utf-8')


def is_ready_health(document):
    return (isinstance(document, dict) and document.get('service') == 'anonbot-voice'
            and type(document.get('version')) is int and document['version'] == 1
            and document.get('warm') is True)


def start():
    python = RUNTIME / '.venv/Scripts/python.exe'
    source = RUNTIME / 'GPT-SoVITS'
    config = RUNTIME / 'anon-zh.yaml'
    adapter = ROOT / 'tools/voice_backend.py'
    if not all(p.is_file() for p in (python, source/'api_v2.py', config, adapter)):
        raise RuntimeError('本地声线环境尚未准备好，请查看中文语音指南')
    try:
        with socket.create_connection(('127.0.0.1', 9882), timeout=1):
            raise RuntimeError('语音端口9882已被占用，请先核对现有服务；未启动第二个实例')
    except (ConnectionRefusedError, TimeoutError):
        pass
    env = os.environ.copy()
    for key in ('HF_HOME', 'TEMP', 'TMP', 'NUMBA_CACHE_DIR', 'MPLCONFIGDIR', 'TORCH_HOME'):
        directory = RUNTIME / 'cache' / key.lower()
        directory.mkdir(parents=True, exist_ok=True)
        env[key] = str(directory)
    env['PYTHONUTF8'] = '1'
    env['PYTHONUNBUFFERED'] = '1'
    env['TORCH_FORCE_WEIGHTS_ONLY_LOAD'] = '1'
    env['HF_HUB_OFFLINE'] = '1'
    env['TRANSFORMERS_OFFLINE'] = '1'
    env['PATH'] = str(source/'runtime') + os.pathsep + env.get('PATH', '')
    log = RUNTIME / ('backend-' + uuid4().hex + '.log')
    with log.open('wb') as output:
        child = subprocess.Popen([str(python), str(adapter), '-a', '127.0.0.1', '-p', '9882', '-c', str(config)],
                                 cwd=source, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    state = dict(pid=child.pid, endpoint='http://127.0.0.1:9882', log=str(log), config=str(config))
    (RUNTIME/'service-state.json').write_text(json.dumps(state, ensure_ascii=False, indent=2), 'utf-8')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError('声线服务启动失败，日志：' + str(log))
        try:
            with opener.open('http://127.0.0.1:9882/anonbot/health', timeout=1) as response:
                raw = response.read(16385)
            if len(raw) > 16384:
                raise ValueError('Health response too large')
            doc = json.loads(raw)
            if is_ready_health(doc):
                print(json.dumps(dict(status='ready', **state), ensure_ascii=False))
                return 0
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    print(json.dumps(dict(status='loading', **state), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(start())
    except (OSError, RuntimeError) as exc:
        print(json.dumps(dict(status='error', reason=str(exc)), ensure_ascii=False))
        raise SystemExit(2)
