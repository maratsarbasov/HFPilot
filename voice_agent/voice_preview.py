"""Short GPT-Live previews with silent input, independent of phone calls."""
import asyncio
import base64
import contextlib
import io
import json
import signal

import av
import numpy as np
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from . import live
from .voices import catalog

GREETING = 'Hello! How can I help you today?'


def validate(voice, language):
    if not isinstance(voice, str) or voice not in {item['id'] for item in catalog()}:
        raise ValueError('Choose a voice from the list')
    if not isinstance(language, str):
        raise ValueError('Enter a language for the preview')
    language = ' '.join(language.split())
    if not 2 <= len(language) <= 80 or not all(c.isalpha() or c in " -'()" for c in language):
        raise ValueError('Enter a language name, for example Spanish or Brazilian Portuguese')
    return language


def encode_sample(chunks):
    samples = np.frombuffer(b''.join(chunks), '<i2')
    voiced = np.flatnonzero(np.abs(samples.astype(np.int32)) > 180)
    if not len(voiced):
        raise RuntimeError('No speech was received. Try this voice again.')
    samples = samples[max(0, voiced[0] - 3600):min(len(samples), voiced[-1] + 6000)]
    seconds = len(samples) / 24000
    if not .5 <= seconds <= 10:
        raise RuntimeError('The preview was incomplete. Try this voice again.')
    buffer = io.BytesIO()
    with av.open(buffer, 'w', format='ipod') as output:
        stream = output.add_stream('aac', rate=24000)
        stream.layout = 'mono'
        stream.bit_rate = 64000
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format='s16', layout='mono')
        frame.sample_rate = 24000
        for packet in stream.encode(frame):
            output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return buffer.getvalue(), round(seconds, 2)


async def generate(voice, language):
    language = validate(voice, language)
    key = live.api_key()
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peer.addTrack(AudioStreamTrack())  # Silence, never the user's microphone.
    channel = peer.createDataChannel('oai-events')
    started, closed = asyncio.Event(), asyncio.Event()
    chunks, fragments, errors, tasks = [], [], [], []
    last_text = 0
    resolved_voice = None

    @peer.on('track')
    def track(track):
        async def receive():
            converter = av.AudioResampler(format='s16', layout='mono', rate=24000)
            size = 0
            while True:
                for frame in converter.resample(await track.recv()):
                    chunk = frame.to_ndarray().astype('<i2').tobytes()
                    size += len(chunk)
                    if size > 24000 * 2 * 35:
                        errors.append('The voice preview exceeded its time limit.')
                        return
                    chunks.append(chunk)
        tasks.append(asyncio.create_task(receive()))

    @channel.on('message')
    def message(raw):
        nonlocal last_text, resolved_voice
        event = json.loads(raw)
        if event['type'] == 'session.started':
            resolved_voice = event['session']['audio']['output']['voice']
            started.set()
        elif event['type'] == 'session.closed':
            closed.set()
        elif event['type'] == 'error':
            errors.append(str(event.get('error', {}).get('message', 'Voice preview failed')).replace(key, '[redacted]'))
            started.set()  # Wake startup on an API rejection.
        elif event['type'] == 'session.output_transcript.delta':
            fragments.append(event['delta'])
            last_text = asyncio.get_running_loop().time()

    try:
        async with asyncio.timeout(30):
            await peer.setLocalDescription(await peer.createOffer())
            config = {'model': 'gpt-live-1', 'store': False, 'delegation': {'type': 'client'},
                      'audio': {'output': {'voice': voice}},
                      'instructions': f'Record one short voice sample in {language}. When prompted, '
                          f'say a natural translation of "{GREETING}" exactly once in {language}. '
                          'Use a natural, friendly phone voice. Then stay silent. '
                          'Do not explain the translation, add anything, or delegate.'}
            async with live.OpenAI(key) as api:
                response = await api.request('POST', 'live/sessions', {'session': config,
                    'transport': {'type': 'webrtc', 'sdp': peer.localDescription.sdp}})
                await peer.setRemoteDescription(RTCSessionDescription(sdp=response['transport']['sdp'], type='answer'))
                await asyncio.wait_for(started.wait(), 15)
                if errors:
                    raise RuntimeError(errors[0])
                if resolved_voice != voice:
                    raise RuntimeError('The requested voice is unavailable. Try another voice.')
                channel.send(json.dumps({'type': 'session.commentary.append', 'delegation_id': None,
                    'event_id': 'sample', 'content': f'Say the greeting now in {language}, then stay silent.'}))
                deadline = asyncio.get_running_loop().time() + 14
                while asyncio.get_running_loop().time() < deadline:
                    if errors:
                        raise RuntimeError(errors[0])
                    if fragments and asyncio.get_running_loop().time() - last_text > 2:
                        break
                    if closed.is_set():
                        raise RuntimeError('The voice preview disconnected. Try again.')
                    await asyncio.sleep(.1)
                if not fragments:
                    raise RuntimeError('No speech was received. Try this voice again.')
    except TimeoutError:
        raise RuntimeError('The voice preview timed out. Check your connection and try again.') from None
    finally:
        try:
            if channel.readyState == 'open' and not closed.is_set():
                with contextlib.suppress(Exception):
                    channel.send(json.dumps({'type': 'session.close'}))
                    await asyncio.wait_for(closed.wait(), 3)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await peer.close()
    data, seconds = encode_sample(chunks)
    return {'voice': voice, 'language': language, 'seconds': seconds,
            'audio': base64.b64encode(data).decode('ascii'), 'format': 'm4a'}


def run(voice, language):
    async def operation():
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(generate(voice, language))
        cancelling = False
        def cancel():
            nonlocal cancelling
            if not cancelling:
                cancelling = True
                task.cancel()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, cancel)
        try:
            return await task
        except asyncio.CancelledError:
            return {'cancelled': True}
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
    return asyncio.run(operation())
