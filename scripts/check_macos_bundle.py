"""Offline relocation test: no phone, model API or external tunnel connections."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def check(app):
    report = {}
    with tempfile.TemporaryDirectory(prefix="HFPilot relocation ' ") as temporary:
        root = Path(temporary).resolve()
        moved = root / 'Moved HFPilot.app'
        shutil.copytree(app, moved, symlinks=True)
        data = root / 'User data'
        env = {'HOME': str(root), 'PATH': '/usr/bin:/bin', 'TMPDIR': str(root),
               'HFPILOT_DATA_ROOT': str(data), 'SSL_CERT_FILE': str(root / 'no-ca.pem'),
               'SSL_CERT_DIR': str(root / 'no-ca-directory')}
        backend = moved / 'Contents/MacOS/HFPilotBackend'

        def run(*args, input=None, extra_env=None):
            return subprocess.run([str(backend), *args], input=input, text=True,
                capture_output=True, cwd=root, env=env | (extra_env or {}), timeout=30, check=True)

        info = plistlib.loads((moved / 'Contents/Info.plist').read_bytes())
        assert 'VoiceAgentProjectRoot' not in info
        assert info['CFBundleIconFile'] == 'AppIcon'
        icon = moved / 'Contents/Resources/AppIcon.icns'
        assert icon.read_bytes()[:4] == b'icns'
        report['application_icon'] = True
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(moved)], check=True)
        report['signature'] = True
        libraries = 0
        for path in moved.rglob('*'):
            assert path.name not in ('.env', 'token', 'local-owner-token', 'tunnel-client')
            assert path.suffix not in ('.tlv', '.wav')
            if path.is_symlink():
                assert path.resolve().is_relative_to(moved), path
                assert path.exists(), path
                continue
            if not path.is_file():
                continue
            with path.open('rb') as stream:
                magic = stream.read(4)
            if magic not in (b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xfe\xed\xfa\xcf'):
                continue
            libraries += 1
            output = subprocess.check_output(['otool', '-L', str(path)], text=True)
            for line in output.splitlines()[1:]:
                dependency = line.strip().split(' (', 1)[0]
                assert dependency.startswith(('@', '/usr/lib/', '/System/Library/')), dependency
        report['self_contained_mach_o_files'] = libraries
        report['bundle_contains_no_user_data_or_tunnel_client'] = True
        licenses = moved / 'Contents/Resources/Licenses'
        for filename in ('Python.txt', 'BTstack.txt', 'Apache-2.0.txt', 'Bluedroid-NOTICE.txt', 'blueutil.txt'):
            assert (licenses / filename).is_file(), filename
        report['native_and_python_license_notices'] = True
        report['runtime'] = json.loads(run('check').stdout)
        assert report['runtime']['packaged']
        assert report['runtime']['native_tls']
        assert Path(report['runtime'].pop('data_directory')) == data
        overview = json.loads(run('bench', 'desktop', input='{"action":"overview"}').stdout)
        assert not overview['api_key_configured'] and overview['phone'] is None
        assert overview['configuration_error'] is None and not overview['calls']
        # Fresh installations must explain missing credentials without crashing
        # the frozen entry point or acquiring Bluetooth. This profile has no key.
        missing_key = subprocess.run([str(backend), 'server', '--port', '0'],
            capture_output=True, text=True, cwd=root, env=env, timeout=15)
        assert missing_key.returncode == 1
        assert missing_key.stderr.strip() == 'Add your OpenAI API key in Settings before turning on the assistant.'
        assert not (data / '.work/macos-hfp').exists()
        report['missing_key_startup_message'] = True
        prompt = data / 'prompts/incoming.txt'
        prompt.write_text('Keep my edited instructions.\n')
        run('check')
        assert prompt.read_text() == 'Keep my edited instructions.\n'
        report['first_launch_and_preserved_instructions'] = True
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        # --paused cannot acquire HFP or answer; use an isolated port and state.
        with (root / 'server.log').open('w+') as log:
            process = subprocess.Popen([str(backend), 'server', '--paused', '--port', str(port)],
                cwd=root, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 20
                token_path = data / '.work/mcp/token'
                def rpc(method, params=None):
                    body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}
                    request = urllib.request.Request(f'http://127.0.0.1:{port}/mcp',
                        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json',
                        'Authorization': 'Bearer ' + token_path.read_text().strip()})
                    with urllib.request.urlopen(request, timeout=2) as response:
                        return json.load(response)['result']
                while True:
                    if process.poll() is not None:
                        log.seek(0)
                        raise AssertionError(log.read())
                    try:
                        rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                            'clientInfo': {'name': 'offline-bundle-check', 'version': '1'}})
                        break
                    except (OSError, ValueError):
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(.1)
                names = {tool['name'] for tool in rpc('tools/list')['tools']}
                assert 'get_status' in names and 'start_incoming' not in names
                status = rpc('tools/call', {'name': 'get_status', 'arguments': {}})['structuredContent']
                assert not status['enabled'] and status['active_call_id'] is None
                assert status['listening']['status'] == 'paused'
                report['paused_mcp_initialize_tools_status'] = True
                # Exercise the separately selected executable, with synthetic secrets only.
                client = root / 'External install/tunnel-client'
                client.parent.mkdir()
                client.write_text('#!/bin/sh\n[ "$1" = doctor ] || exit 9\n'
                    '[ "$CONTROL_PLANE_API_KEY" = synthetic-runtime-key ] || exit 8\n'
                    '[ -n "$HFPILOT_MCP_AUTH" ] || exit 7\nprintf "external-client-ok\\n"\n')
                client.chmod(0o700)
                (data / '.work/mcp/tunnel-client-path').write_text(str(client))
                result = run('tunnel', '--check', '--tunnel-id', 'tunnel_' + 'a' * 32,
                    extra_env={'CONTROL_PLANE_API_KEY': 'synthetic-runtime-key'})
                assert result.stdout.strip() == 'external-client-ok'
                report['separate_client_selection_and_hidden_credentials'] = True
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
                    raise AssertionError('Paused server did not stop cleanly')
            assert process.returncode == 0
        report['clean_shutdown'] = True
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = json.dumps(check(args.app.resolve()), indent=2) + '\n'
    if args.report:
        args.report.write_text(result)
    print(result, end='')
