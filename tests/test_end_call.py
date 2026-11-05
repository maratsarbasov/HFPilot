import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock

from voice_agent.ending import EndCall
from voice_agent.live import LiveSession


def tool(ident='c1', farewell='Thank you. Goodbye.', **overrides):
    return {'name': 'end_call', 'type': 'function_call', 'call_id': ident,
            'arguments': json.dumps(dict(reason='Task complete', farewell=farewell, **overrides))}


class EndingTests(unittest.TestCase):
    def setUp(self):
        self.reply, self.begin = Mock(), Mock()
        self.ending = EndCall(Mock(), self.reply, self.begin)
        self.ending.activate()
        self.ending.delegations.add('d1')

    def test_short_farewell_uses_fixed_delay_without_audio_or_text_events(self):
        self.ending.accept(tool(), 'd1', now=10)
        self.assertFalse(self.ending.due(now=14.99))
        self.assertTrue(self.ending.due(now=15))
        self.assertEqual(self.ending.result['completion_condition'], 'timer_elapsed')
        self.begin.assert_called_once()
        self.reply.assert_not_called()  # Scheduling is not a confirmed hangup.

    def test_null_farewell_is_immediate(self):
        self.ending.accept(tool(farewell=None), 'd1', now=10)
        self.assertTrue(self.ending.due(now=10))
        self.assertEqual(self.ending.result['completion_condition'], 'no_farewell')

    def test_duplicate_delivery_and_second_tool_cannot_repeat_or_delay_farewell(self):
        self.ending.accept(tool(), 'd1', now=10)
        self.ending.accept(tool(), 'd1', now=12)
        self.ending.accept(tool('c2'), 'd1', now=13)
        self.begin.assert_called_once()
        self.assertTrue(self.ending.due(now=15))
        self.reply.assert_called_once_with('c2', {'status': 'already_ending'})

    def test_invalid_arguments_or_unowned_session_do_not_schedule_hangup(self):
        cases = [tool(farewell=''), tool(farewell=123), tool(farewell='x'*241),
                 tool(extra='unexpected'), {'name': 'end_call', 'arguments': 'not JSON'},
                 {'name': 'other', 'arguments': tool()['arguments']},
                 {'name': 'end_call', 'arguments': json.dumps({'reason': '', 'farewell': None})}]
        for i, item in enumerate(cases):
            self.ending.accept({**item, 'call_id': str(i)}, 'd1', now=10)
        self.ending.accept(tool('unknown'), 'unknown', now=10)
        self.ending.active = False
        self.ending.accept(tool('disconnected'), 'd1', now=10)
        self.begin.assert_not_called()
        self.assertIsNone(self.ending.result)
        self.assertEqual(self.reply.call_count, len(cases)+2)
        self.assertTrue(all(c.args[1]['status'] == 'rejected' for c in self.reply.call_args_list))

    def test_remote_disconnect_disables_pending_hangup(self):
        self.assertFalse(self.ending.due(now=100))
        self.ending.accept(tool(), 'd1', now=10)
        self.ending.active = False
        self.assertFalse(self.ending.due(now=100))


class LiveEndingTests(unittest.IsolatedAsyncioTestCase):
    def make_live(self):
        live = LiveSession.__new__(LiveSession)
        live.channel = Mock(readyState='open'); live.journal = Mock()
        live.response_ids = {}; live.delegations = []; live.responses = []
        live.ending = EndCall(live.journal, live.tool_result, live.begin_ending)
        live.ending.activate()
        live.handle({'type': 'session.delegation.created', 'offset_ms': 1000, 'delegation': {'id': 'd1'}})
        return live

    def commands(self, live):
        return [json.loads(c.args[0]) for c in live.channel.send.call_args_list]

    async def test_only_function_call_schedules_one_farewell_transcripts_are_recorded_only(self):
        live = self.make_live()
        for speaker in ('input', 'output'):
            live.handle({'type': f'session.{speaker}_transcript.delta', 'start_ms': 0,
                         'end_ms': 2000, 'delta': 'Goodbye'})
        self.assertIsNone(live.ending.result)
        event = {'type': 'response.event', 'delegation_id': 'd1',
                 'event': {'type': 'response.output_item.done', 'item': tool()}}
        live.handle(event); live.handle(event)
        deadline = live.ending.deadline
        # No local stale-caption or interruption logic, even after the timer is armed.
        live.handle({'type': 'session.input_transcript.delta', 'start_ms': 2000,
                     'end_ms': 3000, 'delta': 'Wait, one more thing'})
        self.assertEqual(live.ending.deadline, deadline)
        self.assertEqual(self.commands(live), [{'type': 'session.commentary.append',
            'event_id': 'farewell_c1', 'delegation_id': None, 'content': 'Thank you. Goodbye.'}])
        self.assertTrue(live.ending.due(now=deadline))

    async def test_null_sends_no_speech_and_close_reports_actual_result_without_continuing(self):
        for status in ('hangup_confirmed', 'scheduled'):
            with self.subTest(status=status):
                live = self.make_live()
                live.ending.accept(tool(farewell=None), 'd1')
                self.assertEqual(self.commands(live), [])
                live.ending.result['status'] = status
                live.started = asyncio.Event(); live.started.set()
                live.closed = asyncio.Event()
                live.peer = Mock(close=AsyncMock())
                live.session_id = 'session'; live.final_event = live.latest_usage = live.failure = None
                live.channel.send.side_effect = lambda data: live.closed.set() if json.loads(data)['type'] == 'session.close' else None
                result = await live.close()
                commands = self.commands(live)
                expected = status if status == 'hangup_confirmed' else 'session_closed'
                self.assertEqual(json.loads(commands[0]['item']['output']), {'status': expected})
                self.assertEqual(commands[1]['type'], 'session.close')
                self.assertEqual(len(commands), 2)
                self.assertTrue(result['finalized'])
                self.assertFalse(live.ending.due(now=1e20))


if __name__ == '__main__':
    unittest.main()
