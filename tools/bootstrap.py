"""Install and run this checkout in its own virtual environment; never reads account secrets."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import venv

ROOT=Path(__file__).resolve().parents[1]


def config_files(root):
    root=Path(root).resolve()
    local=root/'local'
    for candidate in (local,local/'secrets',local/'config'):
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
            raise RuntimeError('Local configuration must stay inside this project')
    local.mkdir(parents=True,exist_ok=True)
    secret_dir=local/'secrets';secret_dir.mkdir(exist_ok=True)
    for name in ('dashboard','webhook','gateway'):
        path=secret_dir/(name+'.key')
        try:
            with path.open('x',encoding='ascii') as stream:stream.write(secrets.token_urlsafe(32)+'\n')
            os.chmod(path,0o600)
        except FileExistsError:pass
    config_dir=local/'config';config_dir.mkdir(exist_ok=True)
    values={
        'release.json':dict(version=1,data_dir='../state',token_file='../secrets/dashboard.key',
            origin='http://127.0.0.1:8788',embedding=None,vision=None),
        'worker.json':dict(version=1,settings=dict(bot_id='10000',allowed_users=[],private_mode='friends',
             base_url='https://api.deepseek.com',model='deepseek-flash',gateway_url='http://127.0.0.1:3000',port=8765),
             data_dir='../state',api_key_file='../secrets/chat.key',gateway_token_file='../secrets/gateway.key',
             webhook_secret_file='../secrets/webhook.key'),
        'voice-options.json':dict(version=1,default_language='ja',profiles={'zh':'voice-zh.json','ja':'voice-ja.json'})}
    for language,port in [('zh',9892),('ja',9893)]:
        values['voice-'+language+'.json']=dict(version=1,profile_id='anon-direct-'+language+'-v1',
            endpoint=f'http://127.0.0.1:{port}',reference_audio=str((Path(root)/'models/reference.wav').resolve()),
            output_dir=str((local/'state/audio'/language).resolve()),prompt_language='ja',
            prompt_text='学校の宿題で作文があったんだけど、ともりん、再提出になっちゃって',
            timeout=180,text_language=language)
    for name,data in values.items():
        try:
            with (config_dir/name).open('x',encoding='utf-8') as stream:json.dump(data,stream,ensure_ascii=False,indent=2)
        except FileExistsError:pass
    return config_dir


def environment(root):
    root=Path(root).resolve();folder=root/'.venv'
    if folder.is_symlink() or not folder.resolve().is_relative_to(root):
        raise RuntimeError('Project environment cannot point outside this directory')
    executable=folder/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if folder.exists() and (not (folder/'pyvenv.cfg').is_file() or not executable.is_file()):
        raise RuntimeError('Existing .venv is not a usable project environment')
    if not folder.exists():
        if importlib.util.find_spec('ensurepip') is None:
            raise RuntimeError('Install the Python venv component (python3-venv on Ubuntu), then run the installer again.')
        venv.EnvBuilder(with_pip=True).create(folder)
    return executable


def main(argv=None):
    parser=argparse.ArgumentParser(description='Install or start Chihaya Anon bot')
    parser.add_argument('action',choices=('install','dashboard','qq','doctor'),nargs='?',default='install')
    parser.add_argument('--review-model',action='store_true')
    parser.add_argument('--voice-options',type=Path)
    args=parser.parse_args(argv)
    try:
        if sys.version_info<(3,11):raise RuntimeError('Python 3.11 or later is required')
        python=environment(ROOT);config=config_files(ROOT)
        if args.action=='install':
            subprocess.run([str(python),'-m','pip','install','--disable-pip-version-check','-r',str(ROOT/'deploy/requirements-server.txt'),str(ROOT)],cwd=ROOT,check=True)
            print('Installed. Start with run.cmd (Windows) or ./run.sh dashboard (Linux).')
            print('Configuration: local/config; credentials: local/secrets')
            return 0
        command=[str(python),'-m','anonbot.release']
        if args.action in ('dashboard','doctor'):
            command += [args.action,'--config',str(config/'release.json')]
            if args.action=='dashboard' and (args.review_model or (config.parent/'secrets/chat.key').is_file()):
                command+=['--review-worker-config',str(config/'worker.json')]
        else:
            command+=['worker','--config',str(config/'worker.json'),'--live-model','--send']
            if args.voice_options:command+=['--voice-options',str(args.voice_options.resolve())]
        return subprocess.run(command,cwd=ROOT).returncode
    except RuntimeError as exc:
        print(str(exc))
        return 2
    except (OSError,subprocess.SubprocessError):
        print('Setup stopped. Check Python 3.11+, the project directory and README configuration steps.')
        return 2


if __name__=='__main__':raise SystemExit(main())
