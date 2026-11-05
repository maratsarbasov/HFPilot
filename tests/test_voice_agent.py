import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from voice_agent.audio import AudioBridge, BYTES, gain_pcm, put_latest
from voice_agent.journal import Journal, utterances


class EvidenceTests(unittest.TestCase):
    def test_overlap_does_not_split_or_lose_correction(self):
        fragments = [
            dict(speaker='caller', sequence=0, start_ms=0, end_ms=300, delta='Saturday'),
            dict(speaker='assistant', sequence=1, start_ms=200, end_ms=400, delta='Okay.'),
            dict(speaker='caller', sequence=2, start_ms=350, end_ms=800, delta=', no, Sunday.'),
            dict(speaker='caller', sequence=3, start_ms=4000, end_ms=4200, delta='At three.'),
        ]
        turns = utterances(fragments)
        self.assertEqual([t['text'] for t in turns], ['Saturday, no, Sunday.', 'Okay.', 'At three.'])

    def test_partial_transcript_is_durable_and_duplicate_event_not_repeated(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / 'call'
            journal = Journal(directory)
            event = dict(type='session.input_transcript.delta', event_id='a',
                         start_ms=10, end_ms=80, delta=' three')
            journal.api(event); journal.api(event)
            journal.sync()
            lines = (directory / 'transcript.jsonl').read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])['delta'], ' three')
            self.assertIn('caller:  three', journal.render())
            journal.close()


class AudioTests(unittest.TestCase):
    def test_pcm_gain_saturates_instead_of_wrapping(self):
        data = np.array([20000, -20000, 100], dtype='<i2').tobytes()
        self.assertEqual(np.frombuffer(gain_pcm(data, 2), '<i2').tolist(), [32767, -32768, 200])

    def test_queue_discards_stale_audio_instead_of_increasing_latency(self):
        q = asyncio.Queue(maxsize=2)
        put_latest(q, b'a'); put_latest(q, b'b')
        self.assertEqual(put_latest(q, b'c'), 1)
        self.assertEqual([q.get_nowait(), q.get_nowait()], [b'b', b'c'])

    def test_no_links_removed_when_another_app_uses_phone(self):
        def node(i, **p):
            return {'id': i, 'type': 'PipeWire:Interface:Node', 'info': {'props': p}}
        def port(i, node_id, name):
            return {'id': i, 'type': 'PipeWire:Interface:Port',
                    'info': {'props': {'node.id': node_id, 'port.name': name}}}
        def link(i, output, input_, op, ip):
            return {'id': i, 'type': 'PipeWire:Interface:Link', 'info': {
                'output-node-id': output, 'input-node-id': input_,
                'output-port-id': op, 'input-port-id': ip}}
        bridge = AudioBridge('AA:BB', Mock())
        objects = [
            node(1, **{'factory.name':'api.bluez5.sco.source','api.bluez5.address':'AA:BB','node.name':'phone-rx'}),
            node(2, **{'factory.name':'api.bluez5.sco.sink','api.bluez5.address':'AA:BB','node.name':'phone-tx'}),
            node(3, **{'node.name': bridge.name + '_capture'}),
            node(4, **{'node.name': bridge.name + '_playback'}),
            node(5, **{'node.name':'alsa-speaker','device.api':'alsa'}),
            node(6, **{'node.name':'another-app'}),
            port(11, 1, 'out'), port(12, 5, 'in'), port(13, 6, 'in'),
            link(20, 1, 5, 11, 12), link(21, 1, 6, 11, 13),
        ]
        with patch('voice_agent.audio.graph', return_value=objects), patch('voice_agent.audio.run') as command:
            with self.assertRaisesRegex(RuntimeError, 'Another application'):
                bridge.route()
            command.assert_not_called()


class TrackTests(unittest.IsolatedAsyncioTestCase):
    async def test_ended_call_does_not_reconnect_tearing_down_phone_ports(self):
        bridge = AudioBridge('AA:BB', Mock())
        bridge.saved.add(('microphone:out', 'phone:in'))
        with patch('voice_agent.audio.graph') as snapshot, patch('voice_agent.audio.run') as command:
            result = await bridge.close(restore_links=False)
        snapshot.assert_not_called()
        command.assert_not_called()
        self.assertEqual(result['failures'], [])
        self.assertEqual(result['skipped_call_ended'], [['microphone:out', 'phone:in']])
        bridge.track.stop()

    async def test_before_answer_streams_silence_not_stale_phone_audio(self):
        bridge = AudioBridge('AA:BB', Mock())
        put_latest(bridge.incoming, b'\x01\x00' * (BYTES // 2))
        first = await bridge.track.recv()
        self.assertFalse(first.to_ndarray().any())
        bridge.ready = True
        second = await bridge.track.recv()
        self.assertTrue((second.to_ndarray() == 1).all())
        self.assertEqual(second.pts - first.pts, 960)
        bridge.track.stop()


if __name__ == '__main__':
    unittest.main()
