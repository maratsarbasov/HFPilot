"""Fetch the pinned native build dependency. Never used by the installed app."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BTSTACK = ROOT / '.work/deps/btstack'
REVISION = '9edb5d82ea6ec4466a393415d65030929330acdf'
URL = 'https://github.com/bluekitchen/btstack.git'


def verify(path):
    if not (path / 'src/hci.c').is_file():
        raise SystemExit('Run .venv/bin/python scripts/prepare_native.py before building.')
    revision = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
    changes = subprocess.check_output(['git', '-C', str(path), 'status', '--porcelain'], text=True).strip()
    if revision != REVISION or changes:
        raise SystemExit('BTstack must be at the pinned revision with no local changes; dependency left untouched.')


def main():
    BTSTACK.parent.mkdir(parents=True, exist_ok=True)
    if not BTSTACK.exists():
        with tempfile.TemporaryDirectory(prefix='btstack-', dir=BTSTACK.parent) as temporary:
            checkout = Path(temporary) / 'source'
            subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
            subprocess.run(['git', '-C', str(checkout), 'fetch', '--depth=1', URL, REVISION], check=True)
            subprocess.run(['git', '-C', str(checkout), 'checkout', '-q', '--detach', 'FETCH_HEAD'], check=True)
            verify(checkout)
            checkout.rename(BTSTACK)
    verify(BTSTACK)
    print('BTstack ready:', REVISION)


if __name__ == '__main__':
    main()
