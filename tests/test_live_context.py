import json
import unittest
from unittest.mock import Mock

from voice_agent.live import LiveSession


class CallerUpdateTests(unittest.TestCase):
    def test_late_clip_serializes_required_delegation_mode(self):
        # Regression: a real CLCC -> CLIP update omitted this required union
        # discriminator. The API rejected it and the call handler hung up.
        live = LiveSession.__new__(LiveSession)
        live.channel = Mock(readyState='open')
        live.journal = Mock()
        live.caller_revision = 0
        live.backend_instructions = 'Original owner assignment.'
        caller = dict(number='+12025550123', name='\u202aClinic "A"\u202c',
                      number_type=145, source='hfp_clip', status='reported')
        for revision in (1, 2):
            live.update_caller(caller)
            events = [json.loads(call.args[0]) for call in live.channel.send.call_args_list[-2:]]
            thinking, update = events
            self.assertEqual(thinking['event_id'], f'caller_context_{revision}')
            self.assertEqual(thinking['type'], 'session.thinking.append')
            self.assertEqual(update['event_id'], f'caller_backend_{revision}')
            self.assertEqual(update['type'], 'session.update')
            delegation = update['session']['delegation']
            self.assertEqual(delegation['type'], 'responses')
            # Omit unchanged backend settings so the server retains model/tools.
            self.assertEqual(set(delegation['responses']), {'instructions'})
            instructions = delegation['responses']['instructions']
            self.assertTrue(instructions.startswith(live.backend_instructions))
            context=json.loads(thinking['content'])
            self.assertEqual(context, {'caller_id':caller,'caller_id_revision':revision})
            self.assertEqual(instructions.count('"caller_id_revision"'), 1)
            self.assertTrue(instructions.endswith(thinking['content']))
