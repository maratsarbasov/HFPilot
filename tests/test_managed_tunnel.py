import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from voice_agent import live, tunnel
from voice_agent import tunnel_client as openai_tunnel

ID = 'tunnel_' + 'a' * 32

# An external executable, with a real loopback health endpoint and no cloud/phone.
CLIENT = '''import json, os, signal, sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
directory = Path(sys.argv[sys.argv.index('--health.url-file') + 1]).parent
(directory / 'pid').write_text(str(os.getpid()))
assert 'synthetic-shared-key' not in ' '.join(sys.argv)
assert 'synthetic-local-code' not in ' '.join(sys.argv)
assert os.environ['CONTROL_PLANE_API_KEY'] == 'synthetic-shared-key'
assert os.environ['HFPILOT_MCP_AUTH'] == 'Bearer synthetic-local-code'
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        value = json.loads((directory / 'snapshot').read_text())
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps(value).encode())
    def log_message(self, *args): pass
server = HTTPServer(('127.0.0.1', 0), Handler)
(directory / 'health-url').write_text('http://127.0.0.1:' + str(server.server_port))
if (directory / 'ignore-term').exists(): signal.signal(signal.SIGTERM, signal.SIG_IGN)
server.serve_forever()
'''


def snapshot(status='ok', **details):
    return {'schema_version': 1, 'ready': True, 'components': {
        'control-plane': {'status': status, 'details': details}}}


class ManagedTunnelTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_evidence_recovery_and_parent_eof_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            binary = directory / 'tunnel-client'
            binary.write_text('#!' + sys.executable + '\n' + CLIENT); binary.chmod(0o700)
            health = directory / 'snapshot'
            health.write_text(json.dumps(snapshot()))  # Ready without a successful poll.
            parent = asyncio.StreamReader()
            stopped = asyncio.Event()
            events = []
            task = asyncio.create_task(tunnel.supervise(str(binary), ID,
                openai_tunnel.client_environment('synthetic-shared-key', 'synthetic-local-code'),
                directory, parent, stopped, lambda *event: events.append(event), interval=.02))

            async def wait_for(predicate):
                async with asyncio.timeout(5):
                    while not predicate(): await asyncio.sleep(.02)

            try:
                await wait_for(lambda: (directory / 'health-url').exists())
                await asyncio.sleep(.1)
                self.assertNotIn('connected', [event[0] for event in events])
                health.write_text(json.dumps(snapshot(last_success='2026-11-05T10:00:00Z')))
                await wait_for(lambda: events[-1][0] == 'connected')
                health.write_text(json.dumps(snapshot('degraded', failure_category='http_error', http_status=403)))
                await wait_for(lambda: events[-1][0] == 'error')
                self.assertIn('Read + Use', events[-1][1])
                health.write_text(json.dumps(snapshot('degraded', failure_category='network_error')))
                await wait_for(lambda: events[-1][0] == 'reconnecting')
                health.write_text(json.dumps(snapshot(last_success='2026-11-05T10:01:00Z')))
                await wait_for(lambda: events[-1][0] == 'connected')
                parent.feed_eof()  # App closes/crashes, not a window lifetime event.
                await asyncio.wait_for(task, 6)
                pid = int((directory / 'pid').read_text())
                with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
                self.assertTrue(stopped.is_set())
                self.assertNotIn('synthetic-', str(events))
            finally:
                stopped.set()
                await asyncio.wait_for(task, 6)

    async def test_stop_is_bounded_when_vendor_ignores_term(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            binary = directory / 'tunnel-client'
            binary.write_text('#!' + sys.executable + '\n' + CLIENT); binary.chmod(0o700)
            (directory / 'ignore-term').touch()
            (directory / 'snapshot').write_text(json.dumps(snapshot()))
            parent = asyncio.StreamReader(); stopped = asyncio.Event()
            task = asyncio.create_task(tunnel.supervise(str(binary), ID,
                openai_tunnel.client_environment('synthetic-shared-key', 'synthetic-local-code'),
                directory, parent, stopped, lambda *_: None, interval=.02))
            try:
                async with asyncio.timeout(5):
                    while not (directory / 'health-url').exists(): await asyncio.sleep(.02)
            finally:
                stopped.set()
                await asyncio.wait_for(task, 6)
            with self.assertRaises(ProcessLookupError): os.kill(int((directory / 'pid').read_text()), 0)

    async def test_runner_reuses_saved_voice_key_without_copying_or_disclosing_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); state = root / '.work/mcp'; state.mkdir(parents=True)
            key = root / '.env'; key.write_text('OPENAI_API_KEY=synthetic-shared-key\n'); key.chmod(0o600)
            code = state / 'token'; code.write_text('synthetic-local-code'); code.chmod(0o600)
            reader, writer = os.pipe()
            output = io.StringIO()
            async def fake_supervise(binary, tunnel_id, env, directory, parent, stopped, emit):
                self.assertEqual(env['CONTROL_PLANE_API_KEY'], 'synthetic-shared-key')
                self.assertEqual(env['HFPILOT_MCP_AUTH'], 'Bearer synthetic-local-code')
                self.assertEqual(tunnel_id, ID)
                stopped.set()
            try:
                os.write(writer, (json.dumps({'tunnel_id': ID}) + '\n').encode())
                with os.fdopen(reader) as stdin, patch.object(sys, 'stdin', stdin), \
                     patch.object(tunnel, 'ROOT', root), patch.object(live, 'ROOT', root), \
                     patch.object(tunnel, 'access_code', lambda: openai_tunnel.access_code(root)), \
                     patch.object(tunnel.shutil, 'which', return_value='/fake/tunnel-client'), \
                     patch.object(tunnel, 'supervise', side_effect=fake_supervise), \
                     patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
                    self.assertEqual(await tunnel.run(), 0)
            finally: os.close(writer)
            self.assertNotIn('synthetic-', output.getvalue())
            self.assertEqual(sorted(p.name for p in state.iterdir()), ['openai-tunnel.lock', 'token'])
