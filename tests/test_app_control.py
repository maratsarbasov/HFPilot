import contextlib
import fcntl
import io
import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from voice_agent import app_control as bench
from voice_agent.mcp_connections import saved_origin


class LocalControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.record = self.root / 'bench.json'
        self.data = {'url': 'https://fixture.trycloudflare.com/mcp',
                     'server': {'pid': 101, 'started': 'server-start'},
                     'tunnel': {'pid': 202, 'started': 'tunnel-start'}}
        self.record.write_text(json.dumps(self.data))
        for name, value in (('ROOT', self.root), ('STATE', self.root), ('RECORD', self.record)):
            patcher = patch.object(bench, name, value)
            patcher.start(); self.addCleanup(patcher.stop)

    def main(self, command):
        with patch('sys.argv', ['app_control.py', command]), contextlib.redirect_stdout(io.StringIO()):
            bench.main()

    def test_enable_adopts_running_instance_without_interrupting_call(self):
        with patch.object(bench, 'alive', return_value=True), \
             patch.object(bench, 'runtime_status', return_value={
                 'listening': {'status': 'listening'}, 'hardware_error': None, 'active_call_id': 'active'}), \
             patch.object(bench, 'start_process') as start, patch('os.kill') as kill:
            self.main('enable')
            start.assert_not_called()
            kill.assert_not_called()
            self.assertEqual(json.loads(self.record.read_text()), self.data)

    def test_reenable_ignores_legacy_tunnel_and_failed_start_cleans_only_new_server(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.record.write_text(json.dumps(self.data))
                with patch.object(bench, 'alive', side_effect=lambda r: r.get('pid') == 202), \
                     patch.object(bench, 'start_process', return_value={'pid': 303, 'started': 'new'}) as start, \
                     patch.object(bench, 'wait_ready', side_effect=RuntimeError('No phone') if fail else None), \
                     patch.object(bench, 'stop') as stop:
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, 'No phone'):
                            self.main('enable')
                        stop.assert_called_once_with({'server': {'pid': 303, 'started': 'new'}})
                    else:
                        self.main('enable')
                        stop.assert_not_called()
                    start.assert_called_once()
                    self.assertNotIn('--public-url', start.call_args.args[0])
                    saved = json.loads(self.record.read_text())
                    self.assertEqual(saved, {'server': {'pid': 303, 'started': 'new'}})

    def test_disable_pauses_and_stop_terminates_only_server_despite_legacy_record(self):
        for command, expected in (('disable', []), ('stop', [101])):
            with self.subTest(command=command):
                running = {101, 202}
                def kill(pid, sig):
                    self.assertEqual(sig, signal.SIGTERM)
                    running.remove(pid)
                with patch.object(bench, 'alive', side_effect=lambda r: r.get('pid') in running), \
                     patch('os.kill', side_effect=kill) as killed, patch.object(bench, 'control') as control:
                    self.main(command)
                    self.assertEqual([c.args[0] for c in killed.call_args_list], expected)
                    self.assertIn(202, running)
                    self.assertEqual(json.loads(self.record.read_text()), self.data)
                    if command == 'disable':
                        control.assert_called_once_with('pause')

    def test_enable_starts_local_server_only(self):
        with patch.object(bench, 'alive', return_value=False), \
             patch.object(bench, 'start_process', return_value={'pid': 303, 'started': 'local'}) as start, \
             patch.object(bench, 'wait_ready'):
            self.main('enable')
        self.assertEqual(start.call_count, 1)
        self.assertNotIn('cloudflared', start.call_args.args[0])
        self.assertNotIn('--public-url', start.call_args.args[0])

    def test_local_only_starts_paused_and_leaves_saved_address_for_server_to_load(self):
        (self.root / 'public-url').write_text('https://stable.example\n')
        with patch.object(bench, 'alive', return_value=False), \
             patch.object(bench, 'start_process', return_value={'pid': 303, 'started': 'local'}) as start, \
             patch.object(bench, 'wait_ready'):
            self.main('local-only')
        argv = start.call_args.args[0]
        self.assertIn('--paused', argv)
        self.assertNotIn('--public-url', argv)
        self.assertEqual(saved_origin(self.root), 'https://stable.example')
        with patch.object(bench, 'alive', return_value=True), patch.object(bench, 'start_process') as start:
            self.main('local-only')
            start.assert_not_called()

    def test_first_test_call_starts_a_paused_server_and_keeps_status_readonly(self):
        payload = {'action': 'test_call', 'command': 'start', 'direction': 'outgoing', 'assignment': 'Rehearse'}
        with patch.object(bench, 'alive', return_value=False), \
             patch.object(bench, 'start_server') as start, \
             patch.object(bench, 'request_local', return_value={'call_id': 'test'}) as request:
            self.assertEqual(bench.desktop_request(self.data, payload), {'call_id': 'test'})
            start.assert_called_once_with(self.data, paused=True)
            request.assert_called_once_with('/local/test-call', payload, timeout=5)
        with (self.root / 'control.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            status = {'action': 'test_call', 'command': 'status', 'call_id': 'test'}
            with patch('sys.stdin', io.StringIO(json.dumps(status))), \
                 patch.object(bench, 'desktop_request', return_value={'status': 'running'}) as read:
                self.main('desktop')
                read.assert_called_once_with({}, status)

    def test_voice_preview_does_not_lock_phone_controls_and_refuses_an_active_call(self):
        payload = {'action': 'voice_preview', 'voice': 'marin', 'language': 'Russian'}
        with (self.root / 'control.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                 patch.object(bench, 'desktop_request', return_value={'audio': 'test'}) as request:
                self.main('desktop')
                request.assert_called_once_with(self.data, payload)
        with patch.object(bench, 'alive', return_value=True), \
             patch.object(bench, 'runtime_status', return_value={'active_call_id': 'call'}), \
             patch('voice_agent.voice_preview.run') as generate:
            with self.assertRaisesRegex(ValueError, 'Finish the current call'):
                bench.desktop_request(self.data, payload)
            generate.assert_not_called()

    def test_restart_preserves_pause_and_refuses_calls_or_pending_summaries(self):
        for enabled, call, pending in ((False, None, 0), (True, None, 0),
                                       (True, 'active', 0), (False, None, 1)):
            with self.subTest(enabled=enabled, call=call, pending=pending), \
                 patch.object(bench, 'alive', return_value=True), \
                 patch.object(bench, 'runtime_status', return_value={
                     'enabled': enabled, 'active_call_id': call, 'pending_summaries': pending}), \
                 patch.object(bench, 'stop') as stop, patch.object(bench, 'start_server') as start:
                if call or pending:
                    with self.assertRaisesRegex(RuntimeError, 'Server is busy'):
                        self.main('restart-server')
                    stop.assert_not_called()
                    start.assert_not_called()
                else:
                    self.main('restart-server')
                    stop.assert_called_once_with({'server': self.data['server']})
                    start.assert_called_once_with(self.data, paused=not enabled)

    def test_concurrent_controller_and_reused_pid_cannot_stop_processes(self):
        with (self.root / 'control.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('os.kill') as kill:
                with self.assertRaisesRegex(RuntimeError, 'Another local'):
                    self.main('stop')
                kill.assert_not_called()
        with patch.object(bench, 'stamp', return_value='different-process-start'), patch('os.kill') as kill:
            self.main('stop')
            kill.assert_not_called()

    def test_readonly_desktop_data_remains_available_during_local_control(self):
        with (self.root / 'control.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('sys.stdin', io.StringIO('{"action":"overview"}')), \
                 patch.object(bench, 'desktop_request', return_value={'calls':[]}) as read:
                self.main('desktop')
                read.assert_called_once_with({}, {'action':'overview'})
