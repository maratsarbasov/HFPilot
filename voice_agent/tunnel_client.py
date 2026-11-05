"""Manually run the official OpenAI tunnel-client against HFPilot's local MCP.

This is never invoked by the app lifecycle. It does not start HFPilot, enable
answering, or call an MCP tool. Credentials are passed via environment references.
"""
import argparse
import getpass
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import warnings


if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice_agent.paths import ROOT, native_helper
MCP_URL = 'http://127.0.0.1:8765/mcp'


def access_code(root=ROOT):
    path = root / '.work/mcp/token'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('The HFPilot access-code file must be owned by you with permissions 600.')
            raw = stream.read(4097)
    except OSError:
        raise ValueError('No readable HFPilot access code. Open HFPilot first, then retry the launcher.') from None
    code = raw.strip()
    if not code or len(raw) > 4096 or any(c < 33 or c > 126 for c in code):
        raise ValueError('The HFPilot access-code file is invalid.')
    return code.decode('ascii')


def client_environment(key, code):
    # Do not inherit tunnel profiles, alternate targets, raw HTTP logging, or
    # managed Cloudflare settings from an unrelated shell configuration.
    keep = {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(CONTROL_PLANE_API_KEY=key, HFPILOT_MCP_AUTH='Bearer ' + code)
    return env


def client_arguments(tunnel_id):
    return ['--control-plane.tunnel-id', tunnel_id,
            '--control-plane.api-key', 'env:CONTROL_PLANE_API_KEY',
            '--control-plane.poll-channel', 'main',
            '--mcp.server-url', MCP_URL,
            '--mcp.extra-headers', 'Authorization: env:HFPILOT_MCP_AUTH',
            '--health.listen-addr', '127.0.0.1:0', '--log.http-raw-unsafe=false']


def installed_client():
    try:
        selected = (ROOT / '.work/mcp/tunnel-client-path').read_text().strip()
        if selected and Path(selected).is_absolute():
            return selected
    except OSError:
        pass
    return native_helper('tunnel-client') or 'tunnel-client'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tunnel-id', help='Complete tunnel ID from OpenAI Platform; prompted if omitted')
    parser.add_argument('--client', default=installed_client(), help='Official tunnel-client executable')
    parser.add_argument('--check', action='store_true', help='Run configuration checks only, without starting the tunnel')
    opts = parser.parse_args()
    binary = shutil.which(opts.client)
    if not binary:
        raise ValueError('Install the official tunnel-client and select it in HFPilot’s Assistant connections, or use --client PATH.')
    code = access_code()
    tunnel_id = (opts.tunnel_id or input('OpenAI tunnel ID: ')).strip()
    if not re.fullmatch(r'tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}', tunnel_id):
        raise ValueError('Enter the complete tunnel ID from OpenAI Platform.')
    key = os.environ.get('CONTROL_PLANE_API_KEY')
    if not key:
        if not sys.stdin.isatty():
            raise ValueError('Run in an interactive terminal for hidden key entry, or supply CONTROL_PLANE_API_KEY securely.')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            try:
                key = getpass.getpass('Tunnel runtime API key (hidden): ')
            except getpass.GetPassWarning:
                raise ValueError('Secure key entry is unavailable. Use a terminal with hidden input.') from None
    key = key.strip()
    if not key or any(not 33 <= ord(c) <= 126 for c in key):
        raise ValueError('A tunnel runtime API key is required; it must contain printable ASCII without whitespace.')
    env = client_environment(key, code)
    args = client_arguments(tunnel_id)
    checked = subprocess.run([binary, 'doctor', *args, '--explain'], env=env)
    if checked.returncode or opts.check:
        return checked.returncode
    print('Starting OpenAI Tunnel in this terminal. Stop it with Ctrl+C. HFPilot is managed separately.', flush=True)
    os.execve(binary, [binary, 'run', *args], env)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print('\nCancelled.', file=sys.stderr)
        sys.exit(130)
