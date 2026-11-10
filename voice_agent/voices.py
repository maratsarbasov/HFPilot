"""Built-in GPT-Live voices and atomic edits to the shared call configuration."""
import copy
import hashlib
import json
from pathlib import Path
import re
import tomllib

from .config import load_config
from .mcp_state import atomic_text
from .paths import CONTENTS, FROZEN, SOURCE_ROOT


def catalog():
    resources = CONTENTS / 'Resources' if FROZEN else SOURCE_ROOT / 'macos/Resources'
    return json.loads((resources / 'Voices/catalog.json').read_text())['voices']


def settings(path):
    path = Path(path)
    try:
        config = load_config(path)
        return {'current': config.data['voice']['voice'], 'model': config.data['voice']['model'],
                'revision': hashlib.sha256(path.read_bytes()).hexdigest(), 'error': None}
    except (OSError, ValueError) as error:
        return {'current': None, 'model': None, 'revision': None, 'error': str(error)}


def save(path, voice, revision):
    if not isinstance(voice, str) or voice not in {item['id'] for item in catalog()}:
        raise ValueError('Choose a voice from the list')
    path = Path(path)
    if path.is_symlink():
        raise ValueError('The agent configuration must be a regular file')
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != revision:
        raise ValueError('The configuration changed elsewhere. Reopen the voice picker and try again.')
    config = load_config(path)
    if config.data['voice']['model'] != 'gpt-live-1':
        raise ValueError('This voice picker is for gpt-live-1. Check the model in Agent configuration.')
    if config.data['voice']['voice'] == voice:
        return settings(path)
    expected = copy.deepcopy(config.data)
    expected['voice']['voice'] = voice
    text = original.decode('utf-8')
    # Keep comments, formatting, and prompts byte-for-byte. A matching line may
    # also occur inside a multiline prompt, so accept only a parsed edit that
    # changes exactly voice.voice and nothing else.
    pattern = r'''(?m)^[ \t]*voice[ \t]*=[ \t]*(?:"[^"\r\n]*"|'[^'\r\n]*')[ \t]*(?:\#[^\r\n]*)?$'''
    for match in re.finditer(pattern, text):
        line = match.group()
        value = re.search(r'''=[ \t]*("[^"\r\n]*"|'[^'\r\n]*')''', line)
        start, end = match.start() + value.start(1), match.start() + value.end(1)
        updated = text[:start] + json.dumps(voice) + text[end:]
        if tomllib.loads(updated) != expected:
            continue
        if path.read_bytes() != original:
            raise ValueError('The configuration changed elsewhere. Reopen the voice picker and try again.')
        atomic_text(path, updated)
        return settings(path)
    raise ValueError('Use a single-line voice setting in Agent configuration, then try again.')
