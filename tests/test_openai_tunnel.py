import contextlib
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from voice_agent import tunnel_client as tunnel


class OpenAITunnelTests(unittest.TestCase):
    def test_access_code_requires_a_private_regular_file_and_valid_header_value(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / '.work/mcp/token'
            path.parent.mkdir(parents=True)
            path.write_text('synthetic-code\n'); path.chmod(0o600)
            self.assertEqual(tunnel.access_code(root), 'synthetic-code')
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'permissions 600'):
                tunnel.access_code(root)
            path.chmod(0o600); path.write_text('code\r\nInjected: value')
            with self.assertRaisesRegex(ValueError, 'invalid'):
                tunnel.access_code(root)
            path.unlink(); path.symlink_to(root / 'unrelated')
            with self.assertRaisesRegex(ValueError, 'No readable'):
                tunnel.access_code(root)

    def test_native_client_gets_secrets_only_in_environment_and_no_inherited_tunnel_overrides(self):
        key, code = 'synthetic-control-key', 'synthetic-local-code'
        output = io.StringIO()
        inherited = {'CONTROL_PLANE_API_KEY': key, 'MCP_SERVER_URL': 'https://wrong.example',
                     'LOG_HTTP_RAW_UNSAFE': 'true', 'TUNNEL_CLIENT_CONFIG': '/unrelated',
                     'CLOUDFLARED_MANAGED': 'true', 'PATH': '/usr/bin'}
        with patch.dict(os.environ, inherited, clear=True), \
             patch('sys.argv', ['openai_tunnel.py', '--tunnel-id', 'tunnel_' + 'a' * 32]), \
             patch.object(tunnel.shutil, 'which', return_value='/official/tunnel-client'), \
             patch.object(tunnel, 'access_code', return_value=code), \
             patch.object(tunnel.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as doctor, \
             patch.object(tunnel.os, 'execve') as run, contextlib.redirect_stdout(output):
            tunnel.main()
        args = run.call_args.args[1]
        env = run.call_args.args[2]
        self.assertNotIn(key, ' '.join(args) + output.getvalue())
        self.assertNotIn(code, ' '.join(args) + output.getvalue())
        self.assertEqual(env['HFPILOT_MCP_AUTH'], 'Bearer ' + code)
        self.assertEqual(env['CONTROL_PLANE_API_KEY'], key)
        self.assertEqual(args[args.index('--mcp.extra-headers') + 1], 'Authorization: env:HFPILOT_MCP_AUTH')
        self.assertEqual(args[args.index('--mcp.server-url') + 1], 'http://127.0.0.1:8765/mcp')
        for name in ('LOG_HTTP_RAW_UNSAFE', 'TUNNEL_CLIENT_CONFIG', 'MCP_SERVER_URL', 'CLOUDFLARED_MANAGED'):
            self.assertNotIn(name, env)
        self.assertEqual(doctor.call_args.kwargs['env'], env)

    def test_failed_doctor_or_check_only_never_starts_a_tunnel(self):
        for check, result in ((False, 1), (True, 0)):
            with self.subTest(check=check), patch.dict(os.environ, {'CONTROL_PLANE_API_KEY': 'synthetic'}, clear=True), \
                 patch('sys.argv', ['openai_tunnel.py', '--tunnel-id', 'tunnel_ab12_' + 'b' * 32] + (['--check'] if check else [])), \
                 patch.object(tunnel.shutil, 'which', return_value='/official/tunnel-client'), \
                 patch.object(tunnel, 'access_code', return_value='synthetic'), \
                 patch.object(tunnel.subprocess, 'run', return_value=SimpleNamespace(returncode=result)), \
                 patch.object(tunnel.os, 'execve') as run:
                self.assertEqual(tunnel.main(), result)
                run.assert_not_called()

    def test_unavailable_hidden_input_never_falls_back_to_echoing_a_key(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch('sys.argv', ['openai_tunnel.py', '--tunnel-id', 'tunnel_' + 'a' * 32]), \
             patch.object(tunnel.shutil, 'which', return_value='/official/tunnel-client'), \
             patch.object(tunnel, 'access_code', return_value='synthetic'), \
             patch.object(tunnel.sys.stdin, 'isatty', return_value=True), \
             patch.object(tunnel.getpass, 'getpass', side_effect=tunnel.getpass.GetPassWarning('no hidden input')), \
             patch.object(tunnel.subprocess, 'run') as doctor:
            with self.assertRaisesRegex(ValueError, 'Secure key entry is unavailable'):
                tunnel.main()
            doctor.assert_not_called()
