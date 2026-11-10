import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from voice_agent import desktop


class DesktopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.context = self.root / 'incoming.txt'
        self.context.write_text('Take a message.')
        for target, value in (('ROOT', self.root), ('load_config', lambda: SimpleNamespace(default_assignment=self.context))):
            change = patch.object(desktop, target, value)
            change.start(); self.addCleanup(change.stop)

    def test_history_handles_old_calls_and_partial_files_without_exposing_other_paths(self):
        call = self.root / 'artifacts/calls/20261103-123000-aabbcc'
        call.mkdir(parents=True)
        (call/'metadata.json').write_text(json.dumps({'caller':None,'outcome':'remote_hung_up','summary_status':'pending'}))
        (call/'summary.json').write_text('{')  # A progressive write is not a broken library.
        (call/'transcript.txt').write_text('Caller: A test message.')
        history = desktop.conversations()
        self.assertEqual(history[0]['title'], 'Unknown caller')
        self.assertIn('being saved', history[0]['summary'])
        self.assertEqual(desktop.call_details(call.name)['transcript'], 'Caller: A test message.')
        with self.assertRaises(ValueError): desktop.call_details('../.env')
        other = self.root/'outside'; other.mkdir()
        (self.root/'artifacts/calls/linked').symlink_to(other)
        with self.assertRaises(ValueError): desktop.call_details('linked')
        self.assertEqual(len(desktop.conversations()),1)

    def test_instruction_edit_detects_conflicts_and_uses_shared_prompt_file(self):
        with patch.object(desktop, 'api_key', return_value='test'), patch.object(desktop, 'load_phone', return_value=None):
            before = desktop.overview()['context']
        self.context.write_text('A newer instruction from Dot.')
        with self.assertRaisesRegex(ValueError,'Context changed'):
            desktop.save_context('An older desktop draft.', before['revision'])
        self.assertEqual(self.context.read_text(),'A newer instruction from Dot.')
        with patch.object(desktop, 'api_key', return_value='test'), patch.object(desktop, 'load_phone', return_value=None):
            current = desktop.overview()['context']
        result = desktop.save_context('Merged owner instructions.',current['revision'])
        self.assertEqual(result['text'],self.context.read_text())

    def test_history_distinguishes_an_active_call_from_a_pending_report(self):
        self.assertIn('Call in progress', desktop.summary_text({}, {'outcome': 'starting', 'summary_status': 'pending'}))
        for phase in ('pending', 'running'):
            self.assertIn('being saved', desktop.summary_text({}, {'outcome': 'stopped', 'summary_status': phase}))
        self.assertIn('No summary', desktop.summary_text({}, {'outcome': 'failed', 'summary_status': 'failed'}))

    def test_api_key_is_private_and_never_returned_by_overview(self):
        key = 'test-only-private-key-value'
        self.assertEqual(desktop.save_key(key),{'saved':True})
        self.assertEqual((self.root/'.env').stat().st_mode & 0o777,0o600)
        with patch('voice_agent.live.ROOT',self.root), patch.dict(os.environ,{},clear=True), \
             patch.object(desktop,'load_phone',return_value=None):
            result = desktop.overview()
        self.assertTrue(result['api_key_configured'])
        self.assertNotIn(key,json.dumps(result))
        (self.root/'.env').unlink()
        (self.root/'.env').symlink_to(self.context)
        with self.assertRaises(ValueError): desktop.save_key(key)
        self.assertEqual(self.context.read_text(),'Take a message.')

    def test_recording_preference_defaults_on_and_survives_reload_without_a_server(self):
        def current():
            with patch.object(desktop, 'api_key', return_value='test'), patch.object(desktop, 'load_phone', return_value=None):
                return desktop.overview()['recording']
        self.assertTrue(current()['enabled'])
        desktop.save_recording(False)
        self.assertFalse(current()['enabled'])
        path = self.root / '.work/mcp/record-audio'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError): desktop.save_recording('false')
        self.assertFalse(current()['enabled'])
        path.write_text('broken')
        self.assertIsNone(current()['enabled'])
        self.assertIsNotNone(current()['error'])
        desktop.save_recording(True)
        self.assertTrue(current()['enabled'])
