"""Private, secret-free app preferences shared by Electron and the daemon."""
import json
import os
from pathlib import Path

DEFAULTS = {
    'voiceModel': 'gpt-live-1',
    'inputDevice': '',
    'outputDevice': '',
    'recordSessions': True,
}
MODEL_CHOICES = ('gpt-live-1', 'gpt-realtime-2.1', 'gpt-realtime-2.1-mini')
ENV_KEYS = {'voiceModel': 'ECHOECHO_VOICE_MODEL',
            'inputDevice': 'ECHOECHO_INPUT_DEVICE',
            'outputDevice': 'ECHOECHO_OUTPUT_DEVICE',
            'recordSessions': 'ECHOECHO_RECORD'}


def path():
    return Path(os.environ.get('ECHOECHO_PREFERENCES_FILE',
                               '~/.echoecho/preferences.json')).expanduser()


def load():
    try:
        data = json.loads(path().read_text())
        return validate(data)
    except (OSError, ValueError, TypeError):
        return {}


def validate(data):
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise ValueError('Invalid preference fields')
    for key, value in data.items():
        if key == 'recordSessions':
            if type(value) is not bool:
                raise ValueError('Recording must be on or off')
        elif not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid preference value')
        if key == 'voiceModel' and value not in MODEL_CHOICES:
            raise ValueError('Unsupported voice model')
    return dict(data)


def apply():
    # Saved choices intentionally beat daemon.env; CLI flags are applied later.
    for key, value in load().items():
        os.environ[ENV_KEYS[key]] = ('1' if value else '0') if type(value) is bool else value
