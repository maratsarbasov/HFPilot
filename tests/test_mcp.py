import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

from aiohttp.test_utils import TestClient, TestServer

from voice_agent.config import DEFAULT_CONFIG
from voice_agent.mcp_events import CallbackError, Events, PublicResolver, signed_headers, validate_url
from voice_agent.mcp_oauth import OAuth, redirect_origin
from voice_agent.mcp_runtime import Runtime
from voice_agent.mcp_server import MCP, make_app
from voice_agent.mcp_protocol import MODERN, PREFIX
from voice_agent.mcp_state import State


TOKEN = 'test-owner-token-' + 'x' * 48
SECRET = 'whsec_' + base64.b64encode(b's' * 32).decode()


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.005)


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.context = self.root / 'incoming.txt'
        self.context.write_text('Company X: ask Y. Person Z: say A.')
        self.state = State(self.root / 'state', self.context)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def args(self):
        return SimpleNamespace(config=DEFAULT_CONFIG, backend='skywalk', address='00:00:00:00:00:01',
            output=self.root/'calls', max_call=20, dial_timeout=5, input_gain=1, output_gain=.35,
            record_audio=False, test_call=True, manage_bluetooth=False,
            skywalk_binary=self.root/'unused', bond_file=self.root/'unused', skywalk_buffer_ms=200,
            skywalk_codec='auto')

    def subscription(self, direction='outgoing'):
        return {'name': 'call.completed', 'arguments': {'direction': direction},
                'delivery': {'mode': 'webhook', 'url': 'https://callback.example.com/events', 'secret': SECRET}}

    async def test_signed_callbacks_retry_survive_restart_filter_and_unsubscribe(self):
        requests = []
        async def receiver(url, body, headers):
            requests.append((json.loads(body), headers))
            message = headers['webhook-id'] + '.' + headers['webhook-timestamp'] + '.'
            sig = base64.b64encode(hmac.digest(b's'*32, message.encode() + body, 'sha256')).decode()
            self.assertEqual(headers['webhook-signature'], 'v1,' + sig)
            data = json.loads(body)
            return (200, {'challenge': data['challenge']}) if 'challenge' in data else (503, None)
        events = Events(self.state, receiver)
        sub = await events.subscribe(self.subscription())
        self.assertEqual(sub['id'], (await events.subscribe(self.subscription()))['id'])
        self.assertEqual(len(requests), 1)  # Verification cache, not repeated notifications.
        job, _ = self.state.create_job('outgoing', 'Ask Y', number='+12025550123', request_id='one')
        self.state.finish(job['call_id'], {'outcome': 'assistant_hung_up', 'summary': {'summary_ru': 'Asked Y'}})
        incoming, _ = self.state.create_job('incoming', 'Say A')
        self.state.finish(incoming['call_id'], {'outcome': 'caller_hung_up'})
        self.assertEqual(len(self.state.data['outbox']), 1)
        await events.deliver()
        delivery = next(iter(self.state.data['outbox'].values()))
        self.assertEqual(delivery['status'], 'pending')
        self.assertEqual(delivery['attempts'], 1)
        event_id = requests[-1][0]['eventId']
        restarted = State(self.state.root, self.context)
        next(iter(restarted.data['outbox'].values()))['next_at'] = 0
        async def accepted(url, body, headers):
            self.assertEqual(json.loads(body)['eventId'], event_id)
            self.assertEqual(headers['X-MCP-Subscription-Id'], sub['id'])
            return 204, None
        events = Events(restarted, accepted)
        await events.deliver()
        self.assertEqual(next(iter(restarted.data['outbox'].values()))['status'], 'delivered')
        events.unsubscribe(self.subscription())
        events.unsubscribe(self.subscription())
        self.assertFalse(restarted.data['subscriptions'])

    async def test_callback_verification_failure_ssrf_expiry_and_terminal_response(self):
        failed = Events(self.state, AsyncMock(return_value=(200, {'challenge': 'wrong'})))
        with self.assertRaises(CallbackError):
            await failed.subscribe(self.subscription())
        self.assertFalse(self.state.data['subscriptions'])
        for url in ('http://example.com/a', 'https://127.0.0.1/', 'https://[::1]/',
                    'https://169.254.169.254/', 'https://192.168.1.2/', 'https://example.com:444/',
                    'https://user:pass@example.com/', 'https://[::ffff:127.0.0.1]/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)
        with patch.object(asyncio.get_running_loop(), 'getaddrinfo', AsyncMock(return_value=[
            (2, 1, 6, '', ('127.0.0.1', 443))])):
            with self.assertRaises(CallbackError):
                await PublicResolver().resolve('rebinding.example.com', 443)
        async def receiver(url, body, headers):
            data = json.loads(body)
            return (200, {'challenge': data['challenge']}) if 'challenge' in data else (410, None)
        events = Events(self.state, receiver)
        await events.subscribe(self.subscription())
        job, _ = self.state.create_job('outgoing', 'Test', request_id='two')
        self.state.finish(job['call_id'], {'outcome': 'no_answer'})
        await events.deliver()
        self.assertFalse(self.state.data['subscriptions'])
        self.assertEqual(next(iter(self.state.data['outbox'].values()))['status'], 'failed')
        params = self.subscription(); params['ttlMs'] = 1
        await events.subscribe(params)
        await asyncio.sleep(.005)
        job, _ = self.state.create_job('outgoing', 'Test', request_id='three')
        self.state.finish(job['call_id'], {'outcome': 'no_answer'})
        self.assertEqual(len(self.state.data['outbox']), 1)

    async def test_context_cas_snapshots_and_restart_never_redials(self):
        initial = self.state.context()
        job, fresh = self.state.create_job('incoming', initial['text'], context_revision=initial['revision'])
        self.assertTrue(fresh)
        self.state.set_context('Company X: ask Y2. Person Z: say A.', initial['revision'])
        with self.assertRaisesRegex(ValueError, 'Context changed'):
            self.state.set_context('Overwrite', initial['revision'])
        self.assertEqual((self.state.root/'jobs'/job['call_id']/'assignment.txt').read_text(), initial['text'])
        restarted = State(self.state.root, self.context)
        restarted.recover()
        self.assertEqual(restarted.job(job['call_id'])['result']['outcome'], 'interrupted')
        restarted.recover()
        self.assertEqual(len(restarted.data['events']), 1)

    async def test_http_auth_validation_and_public_tool_contract(self):
        runtime = Mock()
        mcp = MCP(self.state, runtime, Events(self.state))
        async with TestClient(TestServer(make_app(mcp, TOKEN, lifecycle=False))) as client:
            body = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}}
            self.assertEqual((await client.post('/mcp', json=body)).status, 401)
            headers = {'Authorization': 'Bearer ' + TOKEN}
            self.assertEqual((await client.post('/mcp', json=body, headers={**headers, 'Origin': 'https://evil.example'})).status, 403)
            response = await client.post('/mcp', json=body, headers=headers)
            self.assertIn('events', (await response.json())['result']['capabilities'])
            body.update(method='tools/list', params={})
            result = await (await client.post('/mcp', json=body, headers=headers)).json()
            names = {t['name'] for t in result['result']['tools']}
            self.assertTrue({'call', 'get_call', 'set_incoming_context'} <= names)
            self.assertTrue({'start_incoming', 'stop_incoming'}.isdisjoint(names))
            body.update(method='tools/call', params={'name': 'call', 'arguments': {
                'number': '+123;ATH', 'assignment': 'Test', 'request_id': 'test'}})
            result = await (await client.post('/mcp', json=body, headers=headers)).json()
            self.assertTrue(result['result']['isError'])
            runtime.start_call.assert_not_called()
            for name, args in (('start_incoming', {'hours': 12}), ('stop_incoming', {})):
                body.update(method='tools/call', params={'name': name, 'arguments': args})
                self.assertTrue((await (await client.post('/mcp', json=body, headers=headers)).json())['result']['isError'])
            runtime.start.assert_not_called()
            runtime.close.assert_not_called()
            response = await client.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'}, headers=headers)
            self.assertEqual(response.status, 202)
            self.assertEqual((await client.get('/mcp', headers=headers)).status, 405)

    async def test_modern_discovery_tools_and_status_without_initialize_preserves_legacy(self):
        runtime = Mock()
        runtime.status.return_value = {'enabled': False, 'active_call_id': None}
        mcp = MCP(self.state, runtime, Events(self.state))
        async with TestClient(TestServer(make_app(mcp, TOKEN, lifecycle=False))) as client:
            for method, params in [('server/discover', {}), ('tools/list', {}),
                                   ('tools/call', {'name': 'get_status', 'arguments': {}})]:
                body = {'jsonrpc': '2.0', 'id': 'discover-regression', 'method': method,
                        'params': {**params, '_meta': {PREFIX + 'protocolVersion': MODERN,
                                                     PREFIX + 'clientCapabilities': {}}}}
                headers = {'Authorization': 'Bearer ' + TOKEN, 'MCP-Protocol-Version': MODERN,
                           'Mcp-Method': method}
                if method == 'tools/call':
                    # Encoded names are also valid mirrored HTTP metadata.
                    headers['Mcp-Name'] = '=?base64?' + base64.b64encode(b'get_status').decode() + '?='
                unauthorized = await client.post('/mcp', json=body, headers={k:v for k,v in headers.items() if k != 'Authorization'})
                self.assertEqual(unauthorized.status, 401)
                response = await client.post('/mcp', json=body, headers=headers)
                self.assertEqual(response.status, 200)
                result = (await response.json())['result']
                self.assertEqual(result['resultType'], 'complete')
                self.assertEqual(result['_meta'][PREFIX + 'serverInfo']['name'], 'HFPilot')
                if method == 'server/discover':
                    self.assertIn(MODERN, result['supportedVersions'])
                    self.assertIn('events', result['capabilities'])
                elif method == 'tools/list':
                    self.assertIn('get_status', {tool['name'] for tool in result['tools']})
                else:
                    self.assertFalse(result['structuredContent']['enabled'])
            for version in ('2025-03-26', '2025-06-18', '2025-11-25'):
                response = await client.post('/mcp', headers={'Authorization': 'Bearer '+TOKEN,
                    'MCP-Protocol-Version': version}, json={'jsonrpc':'2.0','id':1,'method':'initialize',
                    'params': {'protocolVersion':version}})
                result = (await response.json())['result']
                self.assertEqual(result['protocolVersion'], version)
                self.assertNotIn('resultType', result)
        runtime.start_call.assert_not_called()
        runtime.start.assert_not_called()

    async def test_modern_protocol_and_header_errors_prevent_tool_execution(self):
        runtime = Mock()
        mcp = MCP(self.state, runtime, Events(self.state))
        async with TestClient(TestServer(make_app(mcp, TOKEN, lifecycle=False))) as client:
            async def request(version=MODERN, header_version=MODERN, method_header='tools/call',
                              name_header='call', capabilities=None):
                meta = {PREFIX+'protocolVersion': version}
                if capabilities is None: meta[PREFIX+'clientCapabilities'] = {}
                body = {'jsonrpc':'2.0','id':42,'method':'tools/call','params': {'name':'call',
                    'arguments': {'number':'+12025550123','assignment':'Never executed','request_id':'blocked'},
                    '_meta':meta}}
                headers = {'Authorization':'Bearer '+TOKEN, 'MCP-Protocol-Version':header_version,
                           'Mcp-Method':method_header, 'Mcp-Name':name_header}
                response = await client.post('/mcp', json=body, headers=headers)
                self.assertEqual(response.status, 400)
                reply = await response.json()
                self.assertEqual(reply['id'], 42)
                return reply['error']
            for changes in ({'header_version':'2025-06-18'}, {'method_header':'tools/list'},
                            {'name_header':'get_status'}, {'name_header':'=?base64?bad!?='}):
                self.assertEqual((await request(**changes))['code'], -32020)
            self.assertEqual((await request(capabilities=False))['code'], -32602)
            error = await request(version='2099-01-01', header_version='2099-01-01')
            self.assertEqual(error['code'], -32022)
            self.assertIn(MODERN, error['data']['supported'])
        runtime.start_call.assert_not_called()
        self.assertFalse(self.state.data['jobs'])

    async def test_runtime_idempotency_busy_and_summary_does_not_block_next_call(self):
        runtime = Runtime(self.state, self.args())
        runtime.phone = SimpleNamespace(address='PHONE', read=lambda: ('PHONE', {}),
                                        outgoing_call=lambda: '1', close=AsyncMock())
        finish_audio, finish_summary = asyncio.Event(), asyncio.Event()
        api = AsyncMock(); api.__aenter__.return_value = SimpleNamespace(key='test')
        async def handle(args, *a):
            await finish_audio.wait()
            directory = self.root / args.call_id
            directory.mkdir()
            return SimpleNamespace(journal=SimpleNamespace(directory=directory), metadata={
                'audio': {'failures': []}, 'outcome': 'assistant_hung_up', 'summary_status': 'pending'})
        async def finalize(api, completed, slots):
            await finish_summary.wait()
            completed.metadata.update(summary_status='saved', cleanup_ok=True)
            (completed.journal.directory/'summary.json').write_text('{"summary_ru":"Done"}')
        with patch('voice_agent.mcp_runtime.api_key', return_value='test'), \
             patch('voice_agent.mcp_runtime.OpenAI', return_value=api), \
             patch('voice_agent.mcp_runtime.handle_call', side_effect=handle) as handler, \
             patch('voice_agent.mcp_runtime.finalize_call', side_effect=finalize):
            await runtime.start()
            first = runtime.start_call('+12025550123', 'Ask Y', 'request-one')
            self.assertEqual(runtime.start_call('+12025550123', 'Ask Y', 'request-one')['call_id'], first['call_id'])
            with self.assertRaises(ValueError): runtime.start_call('+12025550124', 'Other', 'request-one')
            with self.assertRaises(ValueError): runtime.start_call('+12025550124', 'Other', 'request-two')
            finish_audio.set()
            await eventually(lambda: first['status'] == 'summarizing')
            self.assertIsNone(runtime.active)
            second = runtime.start_call('+12025550124', 'Other', 'request-two')
            await eventually(lambda: second['status'] == 'summarizing')
            self.assertEqual(handler.call_count, 2)
            finish_summary.set()
            await asyncio.gather(*runtime.tasks)
            self.assertTrue(all(j['status'] == 'completed' for j in self.state.data['jobs'].values()))
            await runtime.close()

    async def test_local_application_handles_mixed_calls_until_shutdown_and_restores_bluetooth(self):
        args = self.args(); args.manage_bluetooth = True
        runtime = Runtime(self.state, args)
        calls = {}
        phone = SimpleNamespace(address='PHONE', read=lambda: ('PHONE', dict(calls)),
            validate=Mock(), start=AsyncMock(), close=AsyncMock(), outgoing_call=lambda: 'outgoing')
        api = AsyncMock(); api.__aenter__.return_value = SimpleNamespace(key='test')
        observed = []
        finish_audio, finish_summary = asyncio.Queue(), asyncio.Event()
        async def handle(args, api, phone, gateway, call, stop, *rest):
            calls[call] = 'active'
            observed.append((args.command, args.assignment.read_text(), stop))
            if len(observed) < 3:
                done = {asyncio.create_task(finish_audio.get()), asyncio.create_task(stop.wait())}
                try:
                    await asyncio.wait(done, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in done:
                        task.cancel()
                    await asyncio.gather(*done, return_exceptions=True)
            else:
                await stop.wait()  # Application shutdown stops only the owned call.
            calls.clear()
            directory = self.root / args.call_id; directory.mkdir()
            return SimpleNamespace(journal=SimpleNamespace(directory=directory), metadata={
                'audio': {'failures': []}, 'outcome': 'caller_hung_up', 'summary_status': 'pending'})
        async def finalize(api, completed, slots):
            await finish_summary.wait()
            completed.metadata['summary_status'] = 'saved'
        power = AsyncMock(return_value='1')
        with patch('voice_agent.mcp_runtime.api_key', return_value='test'), \
             patch('voice_agent.mcp_runtime.OpenAI', return_value=api), \
             patch('voice_agent.mcp_runtime.handle_call', side_effect=handle), \
             patch('voice_agent.mcp_runtime.finalize_call', side_effect=finalize), \
             patch('voice_agent.mcp_runtime.create_phone', return_value=phone) as factory, \
             patch.object(runtime, 'bluetooth', power):
            mcp = MCP(self.state, runtime, Events(self.state))
            app = make_app(mcp, TOKEN)
            app.freeze()
            try:
                await app.startup()
                await eventually(lambda: runtime.listening['status'] == 'listening')
                self.assertEqual(runtime.listening, {'status': 'listening'})
                factory.assert_called_once_with(args, continuous=True)
                calls['1'] = 'incoming'
                await eventually(lambda: len(observed) == 1)
                self.assertEqual(observed[0][1], self.state.context()['text'])
                current = self.state.context()
                self.state.set_context('New task for Company X; also Z.', current['revision'])
                finish_audio.put_nowait(True)
                await eventually(lambda: runtime.active is None)
                mcp.tool('call', {'number': '+12025550123', 'assignment': 'Ask outgoing Y', 'request_id': 'mixed'})
                await eventually(lambda: len(observed) == 2)
                self.assertEqual(observed[1][:2], ('dial', 'Ask outgoing Y'))
                finish_audio.put_nowait(True)
                await eventually(lambda: runtime.active is None)
                calls['2'] = 'incoming'
                await eventually(lambda: len(observed) == 3)
                self.assertEqual(observed[2][:2], ('run', 'New task for Company X; also Z.'))
                self.assertEqual(runtime.status()['pending_summaries'], 2)
                phone.start.assert_awaited_once()
                phone.close.assert_not_awaited()
                self.assertFalse(observed[2][2].is_set())
            finally:
                finish_summary.set()
                await app.cleanup()
            self.assertTrue(observed[2][2].is_set())
            self.assertIsNone(runtime.active)
            self.assertEqual(runtime.listening['status'], 'stopped')
            self.assertTrue(all(j['status'] == 'completed' for j in self.state.data['jobs'].values()))
            phone.close.assert_awaited_once()
            self.assertEqual([c.args for c in power.await_args_list], [(), (0,), (1,)])

    async def test_startup_failure_closes_helper_and_restores_bluetooth(self):
        args = self.args(); args.manage_bluetooth = True
        runtime = Runtime(self.state, args)
        phone = SimpleNamespace(validate=Mock(), start=AsyncMock(side_effect=RuntimeError('No phone')),
                                close=AsyncMock())
        power = AsyncMock(return_value='1')
        with patch('voice_agent.mcp_runtime.api_key', return_value='test'), \
             patch('voice_agent.mcp_runtime.create_phone', return_value=phone), \
             patch.object(runtime, 'bluetooth', power):
            with self.assertRaisesRegex(RuntimeError, 'No phone'):
                await runtime.start()
            self.assertEqual(runtime.listening['status'], 'failed')
            self.assertIsNone(runtime.phone)
            self.assertIsNone(runtime.phone_file)
            phone.close.assert_awaited_once()
            self.assertEqual([c.args for c in power.await_args_list], [(), (0,), (1,)])
            await runtime.close()

    async def test_service_reconnects_and_local_pairing_preserves_enabled_state(self):
        runtime = Runtime(self.state, self.args())
        phone = SimpleNamespace(address='PHONE', read=lambda: ('PHONE', {}), validate=Mock(),
            start=AsyncMock(side_effect=[RuntimeError('Out of range'), None, None]), close=AsyncMock())
        with patch('voice_agent.mcp_runtime.api_key', return_value='test'), \
             patch('voice_agent.mcp_runtime.create_phone', return_value=phone), \
             patch('voice_agent.mcp_runtime.RECONNECT_DELAY', .01):
            try:
                await runtime.start_service()
                await eventually(lambda: runtime.listening['status'] == 'listening')
                self.assertEqual(phone.start.await_count, 2)
                self.assertTrue(runtime.status()['bluetooth_connected'])
                runtime.active = 'owned-call'
                with self.assertRaisesRegex(ValueError, 'current call'):
                    await runtime.pause_for_pairing()
                self.assertFalse(runtime.pairing)
                runtime.active = None
                await runtime.pause_for_pairing()
                self.assertEqual(runtime.listening['status'], 'pairing')
                self.assertIsNone(runtime.phone)
                with self.assertRaises(ValueError):
                    runtime.start_call('+12025550123', 'Not allowed during setup', 'blocked')
                await runtime.resume_after_pairing()
                await eventually(lambda: runtime.listening['status'] == 'listening')
                self.assertEqual(phone.start.await_count, 3)
            finally:
                await runtime.close()

    async def test_pause_finishes_owned_call_blocks_admission_and_keeps_summaries_running(self):
        runtime = Runtime(self.state, self.args())
        calls = {}
        phone = SimpleNamespace(address='PHONE', read=lambda: ('PHONE', dict(calls)), validate=Mock(),
            start=AsyncMock(), close=AsyncMock(), outgoing_call=lambda: 'outgoing')
        api = AsyncMock(); api.__aenter__.return_value = SimpleNamespace(key='test')
        audio_done, summary_done, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def handle(args, api, phone, gateway, call, stop, *rest):
            started.set()
            await audio_done.wait()
            self.assertFalse(stop.is_set())
            directory = self.root / args.call_id; directory.mkdir()
            return SimpleNamespace(journal=SimpleNamespace(directory=directory), metadata={
                'audio': {'failures': []}, 'outcome': 'remote_hung_up', 'summary_status': 'pending'})
        async def summarize(api, completed, slots):
            await summary_done.wait()
            completed.metadata['summary_status'] = 'saved'
        with patch('voice_agent.mcp_runtime.api_key', return_value='test'), \
             patch('voice_agent.mcp_runtime.create_phone', return_value=phone), \
             patch('voice_agent.mcp_runtime.OpenAI', return_value=api), \
             patch('voice_agent.mcp_runtime.handle_call', side_effect=handle), \
             patch('voice_agent.mcp_runtime.finalize_call', side_effect=summarize):
            try:
                await runtime.start_service()
                await eventually(lambda: runtime.status()['bluetooth_connected'])
                job = runtime.start_call('+12025550123', 'Take a message', 'pause-test')
                await started.wait()
                await runtime.pause()
                self.assertEqual(runtime.listening['status'], 'pausing')
                self.assertFalse(runtime.stops[job['call_id']].is_set())
                phone.close.assert_not_awaited()
                with self.assertRaisesRegex(ValueError, 'paused'):
                    runtime.start_call('+12025550124', 'Blocked', 'new-call')
                await runtime.resume()  # Keep answering before this call finishes.
                await eventually(lambda: runtime.maintainer is not None)
                self.assertTrue(runtime.enabled)
                phone.start.assert_awaited_once()
                await runtime.pause()
                calls['another'] = 'incoming'
                audio_done.set()
                await eventually(lambda: runtime.listening['status'] == 'paused')
                self.assertIsNone(runtime.phone)
                self.assertEqual(len(self.state.data['jobs']), 1)
                self.assertEqual(job['status'], 'summarizing')
                summary_done.set()
                await eventually(lambda: job['status'] == 'completed')
                await runtime.pause_for_pairing()
                await runtime.resume_after_pairing()
                self.assertFalse(runtime.enabled)
                self.assertEqual(runtime.listening['status'], 'paused')
                self.assertIsNone(runtime.phone)
            finally:
                audio_done.set(); summary_done.set()
                await runtime.close()

    async def test_local_controls_require_direct_owner_access(self):
        runtime = SimpleNamespace(pause=AsyncMock(return_value={'enabled':False}),
            resume=AsyncMock(), reconnect=AsyncMock(), end_active_call=Mock())
        oauth = Mock(); oauth.accepted.return_value = True
        async with TestClient(TestServer(make_app(MCP(self.state, runtime, Events(self.state)), TOKEN,
                lifecycle=False, oauth=oauth, local_token=TOKEN))) as client:
            owner = {'Authorization':'Bearer '+TOKEN, 'Host':'127.0.0.1:8765'}
            for headers in ({**owner,'CF-Connecting-IP':'192.0.2.1'},
                            {**owner,'Authorization':'Bearer oauth-token'}, {**owner,'Origin':'https://example.com'}):
                self.assertEqual((await client.post('/local/control',json={'command':'pause'},headers=headers)).status,403)
            runtime.pause.assert_not_awaited()
            self.assertEqual((await client.post('/local/control',json={'command':'pause'},headers=owner)).status,200)
            runtime.pause.assert_awaited_once()

    async def test_phone_setup_endpoint_rejects_forwarded_and_oauth_requests(self):
        runtime = SimpleNamespace(pause_for_pairing=AsyncMock(), resume_after_pairing=AsyncMock())
        oauth = Mock(); oauth.accepted.return_value = True
        mcp = MCP(self.state, runtime, Events(self.state))
        async with TestClient(TestServer(make_app(mcp, TOKEN, lifecycle=False, oauth=oauth, local_token=TOKEN))) as client:
            owner = {'Authorization': 'Bearer ' + TOKEN, 'Host': '127.0.0.1:8765'}
            for headers in ({**owner, 'CF-Connecting-IP':'192.0.2.1'}, {**owner, 'Origin':'https://example.com'},
                            {**owner, 'Host':'tunnel.example.com'}, {**owner, 'Authorization':'Bearer oauth-token'}):
                response = await client.post('/local/phone-setup', json={'command':'pause'}, headers=headers)
                self.assertEqual(response.status, 403)
            runtime.pause_for_pairing.assert_not_awaited()
            response = await client.post('/local/phone-setup', json={'command':'pause'}, headers=owner)
            self.assertEqual(response.status, 200)
            runtime.pause_for_pairing.assert_awaited_once()

    async def test_suggest_confirm_change_and_restore_address_while_paused(self):
        local = {'Authorization': 'Bearer local-only', 'Host': '127.0.0.1:8765'}
        remote = {'Host': 'macbook.example.ts.net', 'X-Forwarded-Proto': 'https'}
        base = 'https://macbook.example.ts.net'
        runtime = Mock()
        runtime.status.return_value = {'enabled': False}
        def application():
            return make_app(MCP(self.state, runtime, Events(self.state)), TOKEN,
                            local_token='local-only', lifecycle=False)
        metadata = '/.well-known/oauth-authorization-server'
        async with TestClient(TestServer(application())) as client:
            self.assertEqual((await client.get(metadata, headers=remote)).status, 404)
            status = await (await client.post('/local/status', headers=local)).json()
            self.assertEqual(status['connections'], {'suggested_url': base + '/mcp', 'public_url': None})
            for command in ('dismiss', 'confirm', 'save'):
                body = {'command': command, 'url': base + '/mcp'}
                for headers in ({**local, 'X-Forwarded-Proto': 'https'},
                                {**local, 'Authorization': 'Bearer ' + TOKEN}):
                    self.assertEqual((await client.post('/local/connections', headers=headers, json=body)).status, 403)
            self.assertEqual((await client.post('/local/connections', headers=local,
                json={'command':'confirm','url':'https://wrong.example'})).status, 400)
            await client.post('/local/connections', headers=local, json={'command':'dismiss','url':base})
            await client.post('/mcp', headers=remote)
            status = await (await client.post('/local/status', headers=local)).json()
            self.assertIsNone(status['connections']['suggested_url'])
            await client.get(metadata, headers={'Host':'127.0.0.1:8765', 'X-Forwarded-Host':'calls.example.com'})
            base = 'https://calls.example.com'
            reply = await client.post('/local/connections', headers=local, json={'command':'confirm','url':base})
            self.assertEqual((await reply.json())['public_url'], base + '/mcp')
            for bad in ('https://example.com/private', 'http://calls.example.com', 'https://user@bad.com',
                        'https://127.0.0.1', 'https://[::1]suffix', 'https://bad.example:99999'):
                reply = await client.post('/local/connections', headers=local, json={'command':'save','url':bad})
                self.assertEqual(reply.status, 400)
            self.assertEqual((await client.get(metadata, headers=remote)).status, 421)
            result = await client.get(metadata, headers={'Host':'calls.example.com'})
            self.assertEqual((await result.json())['issuer'], base)
            reply = await client.post('/local/connections', headers=local,
                                      json={'command':'save','url':'https://stable.example/mcp'})
            self.assertEqual(reply.status, 200)
            self.assertIn('https://stable.example/', (await client.get('/mcp')).headers['WWW-Authenticate'])
        async with TestClient(TestServer(application())) as restarted:
            data = await (await restarted.get('/.well-known/oauth-protected-resource/mcp')).json()
            self.assertEqual(data['resource'], 'https://stable.example/mcp')
        runtime.start_service.assert_not_called()

    async def test_oauth_pkce_owner_approval_replay_refresh_and_audience(self):
        oauth = OAuth(self.state, 'https://voice.example.com', TOKEN)
        mcp = MCP(self.state, Mock(), Events(self.state))
        async with TestClient(TestServer(make_app(mcp, TOKEN, oauth=oauth, lifecycle=False))) as client:
            response = await client.post('/mcp', json={})
            self.assertIn('resource_metadata=', response.headers['WWW-Authenticate'])
            result = await client.post('/oauth/register', json={'redirect_uris': ['https://chatgpt.com/callback']})
            registered = await result.json()
            verifier = 'v' * 64
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            params = {'client_id': registered['client_id'], 'redirect_uri': 'https://chatgpt.com/callback',
                      'resource': oauth.resource, 'response_type': 'code', 'code_challenge': challenge,
                      'code_challenge_method': 'S256', 'state': 'client-state'}
            response = await client.get('/oauth/authorize', params=params)
            self.assertEqual(response.status, 200)
            # Native browser form submissions under no-referrer send Origin:
            # null. A manually supplied Origin in this HTTP test hid that bug.
            self.assertEqual(response.headers['Referrer-Policy'], 'strict-origin')
            self.assertEqual(response.headers['Content-Security-Policy'],
                "default-src 'none'; form-action 'self' https://chatgpt.com; frame-ancestors 'none'")

            nonce = response.cookies['voice_oauth'].value
            headers = {'Origin': oauth.base, 'Cookie': 'voice_oauth=' + nonce}
            for origin in ('null', 'https://untrusted.example'):
                response = await client.post('/oauth/authorize', headers={**headers, 'Origin': origin},
                    data={'nonce': nonce, 'owner_token': TOKEN}, allow_redirects=False)
                self.assertEqual(response.status, 403)
                self.assertFalse(oauth.data['codes'])
            response = await client.post('/oauth/authorize', headers={'Origin': oauth.base},
                data={'nonce': nonce, 'owner_token': TOKEN}, allow_redirects=False)
            self.assertEqual(response.status, 400)  # Cookie binding is still required.
            self.assertEqual((await client.post('/oauth/authorize', headers=headers, data={
                'nonce': nonce, 'owner_token': 'wrong'})).status, 403)
            response = await client.post('/oauth/authorize', headers=headers,
                data={'nonce': nonce, 'owner_token': TOKEN}, allow_redirects=False)
            self.assertEqual(response.status, 303)
            query = parse_qs(urlsplit(response.headers['Location']).query)
            self.assertEqual(query['state'], ['client-state'])
            token_request = {'grant_type': 'authorization_code', 'client_id': registered['client_id'],
                'code': query['code'][0], 'redirect_uri': params['redirect_uri'],
                'resource': oauth.resource, 'code_verifier': 'wrong'}
            self.assertEqual((await client.post('/oauth/token', data=token_request)).status, 400)
            token_request['code_verifier'] = verifier
            response = await client.post('/oauth/token', data=token_request)
            tokens = await response.json()
            self.assertTrue(oauth.accepted(tokens['access_token']))
            self.assertEqual((await client.post('/oauth/token', data=token_request)).status, 400)
            self.assertNotIn(tokens['access_token'], self.state.path.read_text())
            refresh = {'grant_type': 'refresh_token', 'client_id': registered['client_id'],
                       'refresh_token': tokens['refresh_token'], 'resource': 'https://wrong.example.com/mcp'}
            self.assertEqual((await client.post('/oauth/token', data=refresh)).status, 400)
            refresh['resource'] = oauth.resource
            response = await client.post('/oauth/token', data=refresh)
            new = await response.json()
            self.assertTrue(oauth.accepted(new['access_token']))
            self.assertEqual((await client.post('/oauth/token', data=refresh)).status, 400)
            response = await client.post('/mcp', headers={'Authorization': 'Bearer ' + new['access_token']},
                json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
            self.assertEqual(response.status, 200)


    async def test_listener_reports_lost_phone_and_releases_controller(self):
        runtime = Runtime(self.state, self.args())
        failed = False
        def read():
            if failed:
                raise RuntimeError('Disconnected')
            return 'PHONE', {}
        phone = SimpleNamespace(read=read, close=AsyncMock())
        runtime.phone = phone
        with patch('voice_agent.mcp_runtime.api_key', return_value='test'):
            await runtime.start()
        failed = True
        await asyncio.wait_for(runtime.listen_task, 2)
        self.assertEqual(runtime.listening['status'], 'failed')
        self.assertIsNotNone(runtime.hardware_error)
        with self.assertRaisesRegex(ValueError, 'not ready'):
            runtime.start_call('+12025550123', 'Test', 'after-disconnect')
        phone.close.assert_awaited_once()
        await runtime.close()

    def test_redirect_metadata_cannot_inject_csp_sources_or_directives(self):
        self.assertEqual(redirect_origin('https://chatgpt.com/callback?ignored=yes'), 'https://chatgpt.com')
        self.assertEqual(redirect_origin('http://127.0.0.1:8878/callback'), 'http://127.0.0.1:8878')
        self.assertEqual(redirect_origin('http://[::1]:8878/callback'), 'http://[::1]:8878')
        for uri in ('https://example.com;script-src/', 'https://example.com *.evil.test/',
                    'https://example.com%0aevil.test/', 'https://example.com:bad/',
                    'https://user:pass@example.com/', 'javascript:alert(1)'):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                redirect_origin(uri)


if __name__ == '__main__':
    unittest.main()
