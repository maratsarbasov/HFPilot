import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

from aiohttp.test_utils import TestClient, TestServer

from voice_agent.config import DEFAULT_CONFIG
from voice_agent.journal import Journal
from voice_agent.local_audio import LocalAudio, LocalTestPhone
from voice_agent.media import BYTES
from voice_agent.mcp_events import Events
from voice_agent.mcp_runtime import Runtime
from voice_agent.mcp_server import MCP, make_app
from voice_agent.mcp_state import State


class LocalCallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.context = self.root / 'incoming.txt'
        self.context.write_text('Instructions for real incoming calls.')
        self.state = State(self.root / 'state', self.context)
        self.runtime = Runtime(self.state, SimpleNamespace(config=DEFAULT_CONFIG,
            backend='skywalk', address=None, output=self.root / 'calls', max_call=10,
            dial_timeout=5, input_gain=4, output_gain=.35, record_audio=False,
            test_call=False, start_paused=True, manage_bluetooth=True))
        self.phone = patch.object(self.runtime, 'ensure_phone', side_effect=AssertionError('Real phone used'))
        self.phone.start(); self.addCleanup(self.phone.stop)
        for name, value in [('voice_agent.mcp_runtime.api_key', 'test-key'),
                            ('voice_agent.local_audio.audio_command', ['unused'])]:
            change = patch(name, return_value=value)
            change.start(); self.addCleanup(change.stop)
        self.api = AsyncMock()
        self.api.__aenter__.return_value = SimpleNamespace(key='test-key', request=AsyncMock())
        change = patch('voice_agent.mcp_runtime.OpenAI', return_value=self.api)
        change.start(); self.addCleanup(change.stop)

    async def run_call(self, direction, *, cancel=None, audio_error=False):
        bridge = SimpleNamespace(ready=False, failure=None,
            start=AsyncMock(), close=AsyncMock(return_value={'failures': []}))
        live = SimpleNamespace(connect=AsyncMock(), greet=Mock(), update_caller=Mock(),
            closed=asyncio.Event(), failure=None, session_id='test-session',
            ending=SimpleNamespace(due=lambda: True, result={}),
            close=AsyncMock(return_value={'finalized': True}))
        connecting = asyncio.Event()
        async def connect(config):
            live.config = config
            connecting.set()
            if cancel == 'connecting':
                await asyncio.Event().wait()
        live.connect.side_effect = connect
        async def start():
            bridge.ready = True
            if audio_error:
                raise RuntimeError('Microphone permission denied')
            if cancel == 'connected':
                self.runtime.cancel_call(self.runtime.active)
        bridge.start.side_effect = start
        with patch.object(LocalTestPhone, 'audio', return_value=bridge), \
             patch('voice_agent.calls.LiveSession', return_value=live), \
             patch('voice_agent.calls.emit'):
            job = self.runtime.start_test_call(direction, 'A private rehearsal assignment.')
            self.assertTrue(self.runtime.status()['active_test_call'])
            with self.assertRaisesRegex(ValueError, 'current call'):
                self.runtime.start_test_call('incoming', 'Another test')
            if cancel == 'connecting':
                await asyncio.wait_for(connecting.wait(), 1)
                self.runtime.cancel_call(job['call_id'])
            await asyncio.wait_for(asyncio.gather(*self.runtime.tasks), 2)
        self.assertIsNone(self.runtime.active)
        self.assertFalse(self.runtime.enabled)
        self.assertIsNone(self.runtime.phone)
        self.assertIsNone(self.runtime.hardware_error)
        self.assertEqual(self.context.read_text(), 'Instructions for real incoming calls.')
        self.assertEqual(job['status'], 'completed')
        self.assertTrue(job['test_call'])
        directory = Path(job['directory'])
        metadata = json.loads((directory / 'metadata.json').read_text())
        self.assertEqual(metadata['backend'], 'local-test')
        self.assertTrue(metadata['test_call'])
        self.assertEqual(metadata['direction'], direction)
        self.assertEqual((directory / 'assignment.txt').read_text(), 'A private rehearsal assignment.')
        bridge.close.assert_awaited_once()
        live.close.assert_awaited_once()
        return job, metadata, live, bridge

    async def test_both_directions_use_the_real_conversation_flow_without_a_phone(self):
        for direction in ('incoming', 'outgoing'):
            with self.subTest(direction=direction):
                job, metadata, live, _ = await self.run_call(direction)
                self.assertEqual(metadata['outcome'], 'assistant_hung_up')
                self.assertEqual(job['phase'], 'connected')
                self.assertEqual(job['result']['summary_status'], 'saved')
                for instructions in (live.config['instructions'], live.config['delegation']['responses']['instructions']):
                    self.assertIn('A private rehearsal assignment.', instructions)
                    self.assertIn('"direction": "' + direction + '"', instructions)
                if direction == 'outgoing':
                    live.greet.assert_called_once_with(outgoing=True)
                    self.assertIsNone(metadata['dialed_number'])
                    self.assertEqual(metadata['dial_method'], 'local_test')
                else:
                    live.greet.assert_called_once_with()
                self.assertTrue(self.state.data['events']['evt_completed_' + job['call_id']]['data']['test_call'])

    async def test_stop_during_connection_or_audio_start_and_permission_failure_release_the_slot(self):
        for direction in ('incoming', 'outgoing'):
            for cancel in ('connecting', 'connected'):
                with self.subTest(direction=direction, cancel=cancel):
                    _, metadata, live, bridge = await self.run_call(direction, cancel=cancel)
                    self.assertEqual(metadata['outcome'], 'stopped')
                    live.greet.assert_not_called()
                    if cancel == 'connecting':
                        bridge.start.assert_not_awaited()
            _, metadata, live, _ = await self.run_call(direction, audio_error=True)
            self.assertEqual(metadata['outcome'], 'failed')
            self.assertEqual(metadata['error'], 'Microphone permission denied')
            live.greet.assert_not_called()

    async def test_only_local_owner_can_start_or_stop_a_test_and_real_calls_are_excluded(self):
        mcp = MCP(self.state, self.runtime, Events(self.state))
        real, _ = self.state.create_job('outgoing', 'Real task', number='+12025550123')
        owner = {'Authorization': 'Bearer local-secret', 'Host': '127.0.0.1:8765'}
        payload = {'command': 'start', 'direction': 'incoming', 'assignment': 'Rehearse'}
        with patch.object(self.runtime, 'launch') as launch:
            async with TestClient(TestServer(make_app(mcp, 'mcp-secret', local_token='local-secret', lifecycle=False))) as client:
                for headers in ({'Authorization': 'Bearer mcp-secret', 'Host': '127.0.0.1:8765'},
                                {**owner, 'Origin': 'https://example.com'},
                                {**owner, 'X-Forwarded-For': '127.0.0.1'}):
                    response = await client.post('/local/test-call', json=payload, headers=headers)
                    self.assertEqual(response.status, 403)
                launch.assert_not_called()
                response = await client.post('/local/test-call', json=payload, headers=owner)
                self.assertEqual(response.status, 200)
                job = await response.json()
                self.assertTrue(job['test_call'])
                self.assertEqual(job['assignment'], 'Rehearse')
                self.assertIsNone(job['conversation_id'])
                launch.assert_called_once()
                directory = self.root / 'calls' / '20261110-120000-aabbcc'
                directory.mkdir(parents=True)
                self.state.job(job['call_id']).update(status='summarizing', directory=str(directory))
                response = await client.post('/local/test-call', json={'command': 'status', 'call_id': job['call_id']}, headers=owner)
                saved = await response.json()
                self.assertEqual(saved['conversation_id'], directory.name)
                self.assertEqual(saved['assignment'], 'Rehearse')
                self.state.job(job['call_id'])['status'] = 'running'
                for command in ('stop', 'status'):
                    response = await client.post('/local/test-call', json={'command': command, 'call_id': real['call_id']}, headers=owner)
                    self.assertEqual(response.status, 409)
                response = await client.post('/local/test-call', json={'command': 'status', 'call_id': []}, headers=owner)
                self.assertEqual(response.status, 409)
                stop = self.runtime.stops[job['call_id']] = asyncio.Event()
                response = await client.post('/local/test-call', json={'command': 'stop', 'call_id': job['call_id']}, headers=owner)
                self.assertEqual(response.status, 200)
                self.assertTrue(stop.is_set())
        for direction, assignment in (('other', 'text'), ('incoming', ''), ('outgoing', 'x' * 32001)):
            with self.assertRaises(ValueError):
                self.runtime.start_test_call(direction, assignment)


class LocalAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_pcm_duplex_is_bounded_and_recording_follows_the_preference(self):
        # A subprocess stands in for AVAudioEngine; no microphone or API is used.
        script = '''import sys
sys.stderr.write('{"event":"ready"}\\n'); sys.stderr.flush()
while data := sys.stdin.buffer.read(1920):
    sys.stdout.buffer.write(b'\\x10\\x00' * 960); sys.stdout.buffer.flush()
'''
        for record in (False, True):
            with self.subTest(record=record), tempfile.TemporaryDirectory() as temp:
                journal = Journal(Path(temp) / 'call')
                bridge = LocalAudio(journal, record)
                try:
                    with patch('voice_agent.local_audio.audio_command', return_value=[sys.executable, '-u', '-c', script]):
                        await bridge.start()
                    bridge.outgoing.put_nowait(b'\x20\x00' * 960)
                    async with asyncio.timeout(3):
                        while bridge.stats['input_bytes'] < BYTES * 7:
                            await asyncio.sleep(.01)
                    self.assertLessEqual(bridge.incoming.qsize(), 5)
                    self.assertGreater(bridge.stats['input_dropped_frames'], 0)
                    self.assertEqual(bridge.stats['input_peak'], 16)
                    self.assertEqual(bridge.stats['output_peak'], 32)
                finally:
                    result = await bridge.close()
                    journal.close()
                self.assertFalse(result['failures'])
                self.assertEqual(bridge.process.returncode, 0)
                self.assertTrue(all(task.done() for task in bridge.tasks))
                self.assertEqual((journal.directory / 'conversation.wav').exists(), record)
                if record:
                    with wave.open(str(journal.directory / 'conversation.wav')) as audio:
                        self.assertEqual(audio.getframerate(), 48000)
                        self.assertGreater(audio.getnframes(), 0)

    async def test_native_start_error_preserves_the_message_and_closes_resources(self):
        script = '''import sys
sys.stderr.write('{"event":"error","message":"Microphone access denied"}\\n'); sys.stderr.flush()
sys.exit(1)
'''
        with tempfile.TemporaryDirectory() as temp:
            journal = Journal(Path(temp) / 'call')
            bridge = LocalAudio(journal, False)
            with patch('voice_agent.local_audio.audio_command', return_value=[sys.executable, '-u', '-c', script]):
                with self.assertRaisesRegex(RuntimeError, 'Microphone access denied'):
                    await bridge.start()
            result = await bridge.close()
            journal.close()
            self.assertIn('Microphone access denied', result['failures'])
            self.assertFalse(bridge.ready)
            self.assertTrue(all(task.done() for task in bridge.tasks))
