import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import json

from voice_agent.listener import IncomingCalls
from voice_agent.calls import CompletedCall, Finalizers, finalize_call
from voice_agent.journal import Journal
from voice_agent.__main__ import serve


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = {}
        self.stop = asyncio.Event()
        self.listener = IncomingCalls(lambda: ('gateway', dict(self.state)), Mock(), self.stop, .001)

    async def test_initial_call_is_ignored_but_next_call_on_same_path_is_received(self):
        self.state['call1'] = 'incoming'
        self.listener.poll()
        self.assertIsNone(self.listener.candidate)
        self.state.clear(); self.listener.poll()
        self.state['call1'] = 'incoming'; self.listener.poll()
        ticket = await self.listener.next()
        self.assertEqual(ticket.path, 'call1')

    async def test_next_incoming_is_retained_while_previous_call_is_finalizing(self):
        self.listener.poll()
        self.state['call1'] = 'incoming'; self.listener.poll()
        first = await self.listener.next()
        self.state['call1'] = 'active'; self.listener.poll()
        # A new call can reuse call1, even if no idle poll occurred in between.
        self.state['call1'] = 'incoming'; self.listener.poll()
        self.assertFalse(self.listener.current(first))
        second = await self.listener.next()
        self.assertNotEqual(first.generation, second.generation)
        self.listener.poll()
        self.assertIsNone(self.listener.candidate)  # No repeated handling of a claimed call.

    async def test_waiting_call_is_not_taken_over(self):
        self.listener.poll()
        self.state.update(call1='active', call2='incoming'); self.listener.poll()
        del self.state['call1']; self.listener.poll()
        self.assertIsNone(self.listener.candidate)

    async def test_reconnect_and_stop_without_restarting_listener(self):
        self.listener.read = Mock(side_effect=RuntimeError('phone disconnected'))
        await self.listener.start()
        self.listener.read = lambda: ('gateway', dict(self.state))
        self.state['call1'] = 'incoming'
        ticket = await asyncio.wait_for(self.listener.next(), 1)
        self.assertEqual(ticket.path, 'call1')
        waiter = asyncio.create_task(self.listener.next())
        self.stop.set()
        self.assertIsNone(await asyncio.wait_for(waiter, 1))
        await self.listener.close()


class FinalizationTests(unittest.IsolatedAsyncioTestCase):
    def completed(self, root, name):
        journal = Journal(Path(root) / name)
        journal.api({'type': 'session.input_transcript.delta', 'event_id': name,
                     'start_ms': 0, 'end_ms': 100, 'delta': 'Test conversation.'})
        live = SimpleNamespace(session_id=name, close=AsyncMock(return_value={
            'finalized': True, 'delegations': 0}))
        from voice_agent.config import load_config
        return CompletedCall(journal, live, {'audio': {'failures': []},
            'outcome': 'caller_hung_up', 'summary_status': 'pending'}, 0, load_config())

    async def test_second_call_is_handled_while_first_summary_is_still_running(self):
        with tempfile.TemporaryDirectory() as root:
            api = SimpleNamespace(key='test-key')
            finalizers = Finalizers(api)
            stop, expired, summary_started, release_summary = (asyncio.Event() for _ in range(4))
            state, handled, completed = {}, [], []
            phone = SimpleNamespace(read=lambda: ('gateway', dict(state)))
            args = SimpleNamespace(hours=12, address='TEST')
            async def summary(*_):
                summary_started.set()
                await release_summary.wait()
            async def handle(*_, **__):
                number = len(handled) + 1
                handled.append(number)
                state['call1'] = 'active'
                await asyncio.sleep(.01)
                result = self.completed(root, str(number))
                completed.append(result)
                state.clear()
                if number == 2:
                    stop.set()
                return result
            async def callers():
                await asyncio.sleep(.01)
                state['call1'] = 'incoming'
                await summary_started.wait()
                state['call1'] = 'incoming'
            def listener(read, emit, stop):
                return IncomingCalls(read, emit, stop, .001)
            with patch('voice_agent.calls.summarize', summary), patch('voice_agent.calls.emit'), \
                 patch('voice_agent.__main__.emit'), patch('voice_agent.__main__.handle_call', handle), \
                 patch('voice_agent.__main__.IncomingCalls', listener):
                caller = asyncio.create_task(callers())
                code = await asyncio.wait_for(serve(args, api, phone, stop, expired, finalizers), 1)
                self.assertEqual(code, 0)
                self.assertEqual(handled, [1, 2])
                self.assertFalse(release_summary.is_set())
                release_summary.set()
                self.assertEqual(await finalizers.drain(), 0)
                await caller
            for call in completed:
                meta = json.loads((call.journal.directory / 'metadata.json').read_text())
                self.assertEqual(meta['summary_status'], 'saved')

    async def test_summary_failure_and_shutdown_preserve_transcript_and_status(self):
        with tempfile.TemporaryDirectory() as root:
            api = SimpleNamespace(key='test-key')
            first = self.completed(root, 'failed')
            with patch('voice_agent.calls.summarize', AsyncMock(side_effect=RuntimeError('API failed'))), \
                 patch('voice_agent.calls.emit'):
                code = await finalize_call(api, first, asyncio.Semaphore(2))
            self.assertEqual(code, 1)
            self.assertEqual(first.metadata['summary_status'], 'failed')
            self.assertIn('Test conversation.', (first.journal.directory / 'transcript.txt').read_text())
            second = self.completed(root, 'interrupted')
            entered = asyncio.Event()
            async def blocked(*_):
                entered.set()
                await asyncio.Event().wait()
            with patch('voice_agent.calls.summarize', blocked):
                task = asyncio.create_task(finalize_call(api, second, asyncio.Semaphore(2)))
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            meta = json.loads((second.journal.directory / 'metadata.json').read_text())
            self.assertEqual(meta['summary_status'], 'interrupted')
            self.assertTrue((second.journal.directory / 'transcript.jsonl').exists())

    async def test_window_expiry_wakes_idle_listener(self):
        args = SimpleNamespace(hours=.000005, address='TEST')
        stop, expired = asyncio.Event(), asyncio.Event()
        phone = SimpleNamespace(read=lambda: ('gateway', {}))
        with patch('voice_agent.__main__.emit'):
            self.assertEqual(await asyncio.wait_for(serve(args, Mock(), phone, stop, expired, Mock()), 1), 0)
        self.assertTrue(expired.is_set())
        self.assertTrue(stop.is_set())


if __name__ == '__main__':
    unittest.main()
