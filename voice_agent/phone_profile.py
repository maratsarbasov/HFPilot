"""Local selected phone and transactional pairing credentials; never model context."""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import unicodedata
import uuid

from .config import ROOT
from .mcp_state import atomic_text
from .paths import FROZEN

DIRECTORY = ROOT / '.work/macos-hfp'
ADDRESS = re.compile(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}')


def private_file(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Phone credentials must be a private file owned by you')
    return path


def display_name(value):
    return ''.join(c for c in value if not unicodedata.category(c).startswith('C')).strip()[:100] or 'Phone'


def load(directory=None):
    directory = Path(directory or DIRECTORY)
    path = directory / 'phone.json'
    if not path.exists():
        return None
    value = json.loads(private_file(path).read_text())
    if not ADDRESS.fullmatch(value.get('address', '')) or not re.fullmatch(r'bond-[a-f0-9]{32}\.tlv', value.get('bond', '')):
        raise ValueError('Invalid saved phone settings')
    return {'address': value['address'], 'name': display_name(value.get('name', 'Phone')),
            'bond_file': str(private_file(directory / value['bond']))}


def commit(staged, address, name, directory=None):
    directory = Path(directory or DIRECTORY)
    if not ADDRESS.fullmatch(address):
        raise ValueError('Invalid phone address')
    staged = private_file(staged)
    if not staged.stat().st_size:
        raise ValueError('Pairing did not save credentials')
    bond = directory / ('bond-' + uuid.uuid4().hex + '.tlv')
    # A single atomic manifest switch selects BOTH the address and its key DB.
    # Old files remain private; cancellation never overwrites the prior pairing.
    staged.replace(bond)
    try:
        with bond.open('rb') as source:
            os.fsync(source.fileno())
        atomic_text(directory / 'phone.json', json.dumps({
            'address': address, 'name': display_name(name), 'bond': bond.name}) + '\n')
    except BaseException:
        bond.unlink(missing_ok=True)
        raise
    return load(directory)


def controller_lock(binary):
    directory = DIRECTORY if FROZEN else Path(binary).parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / 'controller.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    lock = os.fdopen(fd, 'r+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        lock.close()
        raise RuntimeError('Bluetooth is in use by another HFPilot process') from None
    return lock
