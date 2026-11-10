import asyncio
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import av
import numpy as np

from voice_agent import voice_preview as preview


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_requests_fail_before_credentials_or_network_are_used(self):
        with patch.object(preview.live, 'api_key') as key:
            for voice, language in [('unknown', 'Spanish'), ('marin', ''), ('marin', None),
                                    ('marin', 'x' * 81), ('marin', '../../Russian')]:
                with self.assertRaises(ValueError):
                    await preview.generate(voice, language)
            key.assert_not_called()
        self.assertEqual(preview.validate('marin', ' Brazilian  Portuguese '), 'Brazilian Portuguese')
        self.assertEqual(preview.validate('cedar', 'Русский'), 'Русский')

    async def test_cancelling_an_active_preview_closes_the_api_session_and_peer(self):
        sampling = asyncio.Event()
        events = []
        handlers = {}
        channel = SimpleNamespace(readyState='connecting')
        channel.on = lambda name: lambda callback: handlers.update({name: callback})
        def send(raw):
            event = json.loads(raw)
            events.append(event)
            if event['type'] == 'session.commentary.append':
                sampling.set()
            elif event['type'] == 'session.close':
                handlers['message'](json.dumps({'type': 'session.closed'}))
        channel.send = send
        async def connected(*_):
            channel.readyState = 'open'
            handlers['message'](json.dumps({'type': 'session.started',
                'session': {'audio': {'output': {'voice': 'marin'}}}}))
        peer = SimpleNamespace(on=lambda name: lambda callback: None,
            addTrack=lambda track: None, createDataChannel=lambda name: channel,
            createOffer=AsyncMock(return_value='offer'), setLocalDescription=AsyncMock(),
            localDescription=SimpleNamespace(sdp='offer'), setRemoteDescription=connected, close=AsyncMock())
        api = AsyncMock()
        api.__aenter__.return_value = api
        api.request.return_value = {'transport': {'sdp': 'answer'}}
        with patch.object(preview, 'RTCPeerConnection', return_value=peer), \
             patch.object(preview.live, 'api_key', return_value='test-key'), \
             patch.object(preview.live, 'OpenAI', return_value=api):
            task = asyncio.create_task(preview.generate('marin', 'Dutch'))
            await asyncio.wait_for(sampling.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(events[-1]['type'], 'session.close')
        peer.close.assert_awaited_once()
        api.__aexit__.assert_awaited_once()
        config = api.request.call_args.args[2]['session']
        self.assertEqual(config['audio']['output']['voice'], 'marin')
        self.assertEqual(config['delegation'], {'type': 'client'})
        self.assertIn('Dutch', config['instructions'])
        self.assertNotIn('input', config)
        self.assertFalse(config['store'])

    async def test_connection_failure_still_closes_the_peer(self):
        peer = SimpleNamespace(on=lambda name: lambda callback: None, addTrack=lambda track: None,
            createDataChannel=lambda name: SimpleNamespace(on=lambda name: lambda callback: None, readyState='connecting'),
            createOffer=AsyncMock(side_effect=RuntimeError('Offline')), setLocalDescription=AsyncMock(), close=AsyncMock())
        with patch.object(preview, 'RTCPeerConnection', return_value=peer), \
             patch.object(preview.live, 'api_key', return_value='test-key'):
            with self.assertRaisesRegex(RuntimeError, 'Offline'):
                await preview.generate('marin', 'Spanish')
        peer.close.assert_awaited_once()

    def test_generated_audio_is_bounded_decodable_and_kept_in_memory(self):
        tone = (np.sin(np.arange(48000) * .05) * 12000).astype('<i2').tobytes()
        data, seconds = preview.encode_sample([bytes(12000), tone, bytes(12000)])
        self.assertGreater(seconds, 2)
        self.assertLess(seconds, 3)
        with av.open(io.BytesIO(data)) as output:
            self.assertTrue(list(output.decode(audio=0)))
        for audio in ([bytes(48000)], [tone * 6]):
            with self.assertRaises(RuntimeError): preview.encode_sample(audio)
