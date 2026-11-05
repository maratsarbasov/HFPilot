"""Shared 48 kHz WebRTC frames and bounded media queues."""
import asyncio
from fractions import Fraction
import time

import numpy as np
from aiortc import AudioStreamTrack
from av import AudioFrame, AudioResampler

RATE = 48000
SAMPLES = 960
BYTES = SAMPLES * 2
SILENCE = bytes(BYTES)


def gain_pcm(data, gain):
    if gain == 1:
        return data
    return np.clip(np.frombuffer(data, '<i2').astype(np.float32) * gain,
                   -32768, 32767).astype('<i2').tobytes()


def put_latest(queue, item):
    dropped = 0
    while queue.full():
        queue.get_nowait()
        dropped += 1
    queue.put_nowait(item)
    return dropped


class PhoneTrack(AudioStreamTrack):
    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge
        self.started = None
        self.pts = 0

    async def recv(self):
        if self.started is None:
            self.started = time.monotonic()
        target = self.started + self.pts / RATE
        await asyncio.sleep(max(0, target - time.monotonic()))
        try:
            data = self.bridge.incoming.get_nowait() if self.bridge.ready else SILENCE
        except asyncio.QueueEmpty:
            data = SILENCE
        frame = AudioFrame(format='s16', layout='mono', samples=SAMPLES)
        frame.planes[0].update(data)
        frame.sample_rate = RATE
        frame.pts = self.pts
        frame.time_base = Fraction(1, RATE)
        self.pts += SAMPLES
        return frame


class MediaBridge:
    def __init__(self, journal, input_gain, output_gain, record):
        self.journal = journal
        self.input_gain, self.output_gain, self.record = input_gain, output_gain, record
        self.incoming = asyncio.Queue(maxsize=5)
        self.outgoing = asyncio.Queue(maxsize=5)
        self.track = PhoneTrack(self)
        self.ready = False
        self.failure = None
        self.tasks = []
        self.stats = {'input_bytes': 0, 'output_bytes': 0, 'input_dropped_frames': 0,
                      'output_dropped_frames': 0, 'input_peak': 0, 'output_peak': 0}

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        def done(t):
            if not t.cancelled() and t.exception():
                self.failure = str(t.exception())
                self.journal.event('audio-error', message=self.failure)
        task.add_done_callback(done)
        return task

    async def receive_audio(self, track):
        resampler = AudioResampler(format='s16', layout='mono', rate=RATE)
        pending = bytearray()
        while True:
            frame = await track.recv()
            for output in resampler.resample(frame):
                data = output.to_ndarray().astype('<i2', copy=False).tobytes()
                if not self.ready:
                    pending.clear()
                    continue
                pending.extend(data)
                while len(pending) >= BYTES:
                    chunk = bytes(pending[:BYTES]); del pending[:BYTES]
                    self.stats['output_dropped_frames'] += put_latest(self.outgoing, chunk)
