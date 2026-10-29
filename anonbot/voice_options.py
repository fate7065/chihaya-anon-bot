"""Two explicit speech profiles; Chinese uses the accepted whole-utterance recipe."""
from pathlib import Path

from .autonomy.jsonio import strict_json
from .voice import VoiceError, VoiceProfile, LocalSpeech
from .voice_japanese import JapaneseSpeech


def load_voice_options(path, provider_factory):
    path=Path(path).resolve()
    try:
        with path.open('r',encoding='utf-8-sig') as stream:data=strict_json(stream.read(16385),16384)
        if (type(data) is not dict or set(data)!={'version','default_language','profiles'}
                or type(data['version']) is not int or data['version']!=1
                or data['default_language'] not in ('zh','ja')
                or type(data['profiles']) is not dict or set(data['profiles'])!={'zh','ja'}):raise ValueError()
        profiles={}
        for language,value in data['profiles'].items():
            if not isinstance(value,str) or not value or len(value)>2048:raise ValueError()
            profile=VoiceProfile.load(path.parent/value)
            if profile.text_language!=language:raise ValueError()
            profiles[language]=profile
        voices={'zh':LocalSpeech(profiles['zh']),'ja':JapaneseSpeech(profiles['ja'],provider_factory())}
        return voices,data['default_language']
    except (OSError,ValueError,TypeError,KeyError,RecursionError):
        raise VoiceError('中日语音选项配置或参考音不可用，请核对配置文件') from None
