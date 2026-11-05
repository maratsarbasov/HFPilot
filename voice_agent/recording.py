"""Local recording preference; each call freezes its value before opening audio."""
from pathlib import Path

from .config import ROOT
from .mcp_state import atomic_text


def preference_path():
    return ROOT / '.work/mcp/record-audio'


def enabled(path=None, override=None):
    if type(override) is bool:
        return override
    path = Path(path) if path is not None else preference_path()
    if path.is_symlink():
        raise ValueError('The audio recording preference must not be a symbolic link.')
    try:
        value = path.read_text().strip()
    except FileNotFoundError:
        return True
    if value not in ('true', 'false'):
        raise ValueError('The audio recording preference is invalid. Choose a new value in Settings.')
    return value == 'true'


def status(path=None, override=None):
    try:
        return {'enabled': enabled(path, override), 'overridden': type(override) is bool, 'error': None}
    except (ValueError, OSError) as error:
        return {'enabled': None, 'overridden': False, 'error': str(error)}


def save(value, path=None):
    if type(value) is not bool:
        raise ValueError('Record call audio must be true or false.')
    path = Path(path) if path is not None else preference_path()
    if path.is_symlink():
        raise ValueError('The audio recording preference must not be a symbolic link.')
    atomic_text(path, 'true\n' if value else 'false\n')
    return status(path)
