"""Default Mac microphone/speakers for local rehearsals, without a phone."""
import asyncio
import json
import os
import time

import numpy as np

from . import paths
from .caller import unknown_caller
from .journal import dump
from .media import BYTES, RATE, SILENCE, MediaBridge, put_latest
from .skywalk_audio import TimelineWav, mix_recordings


def audio_command():
    binary = (paths.CONTENTS / 'MacOS/HFPilot' if paths.FROZEN else
              paths.ROOT / '.work/macos-app/HFPilot.app/Contents/MacOS/HFPilot')
    if not os.access(binary, os.X_OK):
        raise RuntimeError('Build the macOS application before starting a test call')
    return [str(binary), '--test-audio']


class LocalTestPhone:
    """In-memory call state implementing the existing call lifecycle contract."""
    name = 'local-test'
    address = 'local-audio'

    def __init__(self, direction):
        self.calls = {'test': 'incoming'} if direction == 'incoming' else {}
        self.state = {'setup': 0}

    @staticmethod
    def validate():
        audio_command()

    def read(self):
        return self.address, dict(self.calls)

    def outgoing_call(self):
        return 'test'

    def owns_call(self, call):
        return call in self.calls

    def caller(self, call):
        return unknown_caller()

    async def prepare_caller(self, call):
        return self.caller(call)

    async def answer(self, call):
        self.calls[call] = 'active'

    async def dial(self, number, call):
        # A simulation never passes a number to the Bluetooth transport.
        await self.answer(call)

    async def hangup(self, call):
        self.calls.pop(call, None)

    def audio(self, journal, input_gain, output_gain, record):
        # HFP gain compensation is inappropriate for the Mac's audio devices.
        return LocalAudio(journal, record)


class LocalAudio(MediaBridge):
    def __init__(self, journal, record):
        super().__init__(journal, 1, 1, record)
        self.process = None
        self.wavs = {}
        self.started = asyncio.Event()
        self.origin = None

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(*audio_command(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=BYTES * 10)
        self.spawn(self.control())
        await asyncio.wait_for(self.started.wait(), 10)
        if self.failure:
            raise RuntimeError(self.failure)
        if self.record:
            for side, filename in (('input', 'caller.wav'), ('output', 'assistant.wav')):
                self.wavs[side] = TimelineWav(self.journal.directory / filename, RATE)
        self.origin = time.monotonic()
        self.ready = True
        self.spawn(self.capture())
        self.spawn(self.play())
        self.journal.event('audio-ready', backend='local-test', rate=RATE,
                           input='system-default', output='system-default')

    async def control(self):
        try:
            while line := await self.process.stderr.readline():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # AVFoundation may also write diagnostic lines.
                if not isinstance(event, dict):
                    continue
                if event.get('event') == 'ready':
                    self.started.set()
                elif event.get('event') == 'error':
                    raise RuntimeError(event.get('message', 'Mac audio failed'))
            raise RuntimeError('Mac audio stopped. Check microphone access in System Settings.')
        except Exception as error:
            self.failure = str(error)
            self.started.set()
            raise

    def record_chunk(self, side, data):
        self.stats[side + '_bytes'] += len(data)
        self.stats[side + '_peak'] = max(self.stats[side + '_peak'],
            int(np.max(np.abs(np.frombuffer(data, '<i2').astype(np.int32)))))
        if side in self.wavs:
            wav = self.wavs[side]
            offset = max(wav.samples, int((time.monotonic() - self.origin) * RATE))
            wav.write(offset, data)

    async def capture(self):
        while True:
            try:
                data = await asyncio.wait_for(self.process.stdout.readexactly(BYTES), 5)
            except (asyncio.IncompleteReadError, TimeoutError):
                raise RuntimeError(self.failure or 'The default microphone stopped supplying audio') from None
            self.record_chunk('input', data)
            self.stats['input_dropped_frames'] += put_latest(self.incoming, data)

    async def play(self):
        deadline = time.monotonic()
        while True:
            await asyncio.sleep(max(0, deadline - time.monotonic()))
            try:
                data = self.outgoing.get_nowait()
            except asyncio.QueueEmpty:
                data = SILENCE
            self.process.stdin.write(data)
            await asyncio.wait_for(self.process.stdin.drain(), 2)
            self.record_chunk('output', data)
            deadline = max(deadline + .02, time.monotonic())

    async def close(self, restore_links=True):
        self.ready = False
        self.track.stop()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        failures = []
        if self.process:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
                failures.append('Mac audio did not stop cleanly')
            if self.process.returncode:
                failures.append(self.failure or 'Mac audio exited unexpectedly')
        for wav in self.wavs.values():
            wav.close()
        if len(self.wavs) == 2:
            await asyncio.to_thread(mix_recordings, self.journal.directory)
        result = {**self.stats, 'failures': failures, 'backend': 'local-test', 'recording_rate': RATE}
        dump(self.journal.directory / 'audio.json', result)
        return result
