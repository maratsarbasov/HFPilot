"""Private desktop data shared with the native app through stdin/stdout."""
import json
from pathlib import Path
import re

from .config import ROOT, load_config
from .live import api_key
from .mcp_state import State, atomic_text
from .phone_profile import load as load_phone, display_name
from . import recording, voices


def read_json(path):
    try:
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def summary_text(summary, metadata):
    sections = []
    for field, title in (('summary_ru', ''), ('task_result', 'Result'),
                         ('owner_next_steps', 'Next steps'), ('uncertainties', 'Unresolved')):
        value = summary.get(field)
        if isinstance(value, list):
            value = '\n'.join('• ' + str(item) for item in value)
        if isinstance(value, str) and value.strip():
            sections.append((title + '\n' if title else '') + value.strip())
    if sections:
        return '\n\n'.join(sections)
    if metadata.get('outcome') == 'starting' and not metadata.get('finished_at'):
        return 'Call in progress. Open Transcript to read the conversation so far.'
    if metadata.get('summary_status') in ('pending', 'running'):
        return 'The summary is being saved. You can read the transcript now.'
    return 'No summary is available for this conversation.'


def conversations():
    entries = []
    for directory in sorted((ROOT / 'artifacts/calls').glob('*'), reverse=True):
        if directory.is_symlink() or not directory.is_dir():
            continue
        metadata = read_json(directory / 'metadata.json')
        if not metadata:
            continue
        caller = metadata.get('caller') or {}
        if not isinstance(caller, dict):
            caller = {}
        summary = read_json(directory / 'summary.json')
        title = caller.get('name') or caller.get('number') or metadata.get('dialed_number') or 'Unknown caller'
        if metadata.get('backend') == 'local-test':
            title = 'Test call · ' + metadata.get('direction', 'incoming').capitalize()
        entries.append({'id': directory.name, 'title': display_name(str(title)),
            'direction': metadata.get('direction', 'incoming'),
            'outcome': metadata.get('outcome', 'unknown'),
            'started_at': metadata.get('started_at') or metadata.get('created_at') or directory.name,
            'test_call': metadata.get('test_call', False),
            'summary_status': metadata.get('summary_status', 'unavailable'),
            'summary': summary_text(summary, metadata)})
    return entries


def call_details(ident):
    if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', ident):
        raise ValueError('Invalid conversation')
    directory = ROOT / 'artifacts/calls' / ident
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Conversation is unavailable')
    transcript = directory / 'transcript.txt'
    text = ''
    if transcript.is_file() and not transcript.is_symlink():
        with transcript.open() as source:
            text = source.read(256000)
    recording = directory / 'conversation.wav'
    return {'transcript': text or 'No transcript is available.',
            'recording': str(recording) if recording.is_file() and not recording.is_symlink() else None,
            'directory': str(directory)}


def overview():
    error = None
    context = None
    try:
        config = load_config()
        context = State(ROOT / '.work/mcp', config.default_assignment).context()
    except (ValueError, OSError) as failure:
        error = str(failure)
    try:
        api_key()
        configured = True
    except RuntimeError:
        configured = False
    try:
        phone = load_phone()
    except (ValueError, OSError):
        phone = None
    return {'calls': conversations(), 'context': context, 'configuration_error': error,
            'api_key_configured': configured,
            'voice': voices.settings(ROOT / 'agent.toml'),
            'recording': recording.status(ROOT / '.work/mcp/record-audio'),
            'phone': {'name': phone['name'], 'address': phone['address']} if phone else None}


def save_recording(value):
    return recording.save(value, ROOT / '.work/mcp/record-audio')


def save_voice(value, revision):
    return voices.save(ROOT / 'agent.toml', value, revision)


def save_context(text, revision):
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 32000:
        raise ValueError('Enter instructions between 1 and 32000 characters')
    return State(ROOT / '.work/mcp', load_config().default_assignment).set_context(text, revision)


def save_key(value):
    if not isinstance(value, str) or not 16 <= len(value) <= 4096 or not value.isascii() or any(c.isspace() for c in value):
        raise ValueError('Enter a valid API key without spaces')
    # Credentials arrive over a private pipe and are never returned to the UI.
    path = ROOT / '.env'
    if path.is_symlink():
        raise ValueError('The credentials file must be a regular file')
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if not line.strip().startswith('OPENAI_API_KEY=')]
    atomic_text(path, '\n'.join([*lines, 'OPENAI_API_KEY=' + value]) + '\n')
    return {'saved': True}
