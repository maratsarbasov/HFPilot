"""Separate immutable application code from private, per-user application data."""
import os
from pathlib import Path
import shutil
import sys
import tempfile

FROZEN = bool(getattr(sys, 'frozen', False))
SOURCE_ROOT = Path(__file__).resolve().parents[1]
ROOT = (Path(os.environ.get('HFPILOT_DATA_ROOT',
            Path.home() / 'Library/Application Support/HFPilot')).expanduser().resolve()
        if FROZEN else SOURCE_ROOT)
CONTENTS = Path(sys.executable).resolve().parents[1] if FROZEN else None


def bootstrap():
    if not FROZEN:
        return
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    for relative in ('agent.toml', 'prompts/incoming.txt'):
        target = ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            continue
        contents = (CONTENTS / 'Resources/Defaults' / relative).read_bytes()
        # Publish complete defaults once, including when two helpers start together.
        fd, temporary = tempfile.mkstemp(prefix='.default-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass  # Updates must never replace the owner's instructions.
        finally:
            os.unlink(temporary)


def command(component, *, root=None):
    if FROZEN:
        return [sys.executable, component]
    root = Path(root or ROOT)
    if component == 'server':
        return [str(root / '.venv/bin/python'), '-m', 'voice_agent.mcp_server']
    return [str(root / '.venv/bin/python'), '-m', 'voice_agent.app_control']


def native_helper(name):
    if name == 'tunnel-client':
        return shutil.which(name)
    if FROZEN:
        return str(CONTENTS / 'Helpers' / name)
    if name == 'skywalk-hfp':
        return str(ROOT / '.work/macos-app/skywalk-build/skywalk-hfp')
    return shutil.which(name)
