"""Negotiated 8/16 kHz HFP PCM <-> 48 kHz WebRTC and sample-clock recordings."""
import asyncio
import json
from fractions import Fraction
import struct
import time
import wave

from aiortc import AudioStreamTrack, MediaStreamError
from av import AudioFrame, AudioResampler
import numpy as np

from .media import MediaBridge, RATE, BYTES, SILENCE, gain_pcm, put_latest
from .journal import dump

INPUT_FRAMES = 26  # 520 ms: scheduling reserve plus a batched SCO-replacement gap.
OUTPUT_FRAMES = 26  # Hard burst cap for either profile, not a fixed playout delay.


class SkywalkTrack(AudioStreamTrack):
    """SCO is the input clock. Do not race it with a second 20 ms timer."""
    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge
        self.pts = 0

    async def recv(self):
        if self.readyState != 'live':
            raise MediaStreamError
        if self.bridge.ready:
            data = await self.bridge.incoming.get()
            if data is None:
                raise MediaStreamError
            self.bridge.stats['input_delivered_frames'] += 1
            if 'input' in self.bridge.model_wavs:
                self.bridge.model_wavs['input'].writeframesraw(data)
        else:
            # ICE/API setup happens before answering the phone.
            await asyncio.sleep(.02)
            data = SILENCE
        frame = AudioFrame(format='s16', layout='mono', samples=960)
        frame.planes[0].update(data)
        frame.sample_rate = RATE
        frame.pts = self.pts
        frame.time_base = Fraction(1, RATE)
        self.pts += 960
        return frame

    def stop(self):
        super().stop()
        put_latest(self.bridge.incoming, None)  # Wake a recv waiting for stopped SCO.


class PCMResampler:
    def __init__(self, source, target):
        self.source = source
        self.pts = 0
        self.resampler = AudioResampler(format='s16', layout='mono', rate=target)

    def feed(self, pcm):
        frame = AudioFrame(format='s16', layout='mono', samples=len(pcm) // 2)
        frame.planes[0].update(pcm)
        frame.sample_rate = self.source
        frame.pts = self.pts
        frame.time_base = Fraction(1, self.source)
        self.pts += frame.samples
        return b''.join(f.to_ndarray().astype('<i2', copy=False).tobytes()
                        for f in self.resampler.resample(frame))


class TimelineWav:
    def __init__(self, path, rate=8000):
        self.rate = rate
        self.wav = wave.open(str(path), 'wb')
        self.wav.setnchannels(1); self.wav.setsampwidth(2); self.wav.setframerate(rate)
        self.samples = 0
        self.missing = 0

    def write(self, offset, data):
        if offset < self.samples:
            raise ValueError('Audio clock moved backwards')
        gap = offset - self.samples
        if gap > self.rate * 10:
            raise ValueError('Audio clock jumped more than ten seconds')
        if gap:
            self.wav.writeframesraw(bytes(gap * 2)); self.missing += gap
        self.wav.writeframesraw(data)
        self.samples = offset + len(data) // 2

    def close(self):
        self.wav.close()


def mix_recordings(directory):
    """Bounded-memory mono mix of aligned raw HFP tracks for listening."""
    with wave.open(str(directory / 'caller.wav'), 'rb') as a, \
         wave.open(str(directory / 'assistant.wav'), 'rb') as b, \
         wave.open(str(directory / 'conversation.wav'), 'wb') as out:
        rate = a.getframerate()
        if rate != b.getframerate() or a.getnchannels() != 1 or b.getnchannels() != 1 or \
                a.getsampwidth() != 2 or b.getsampwidth() != 2:
            raise ValueError('Incompatible HFP recordings')
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate)
        while True:
            left, right = a.readframes(rate), b.readframes(rate)
            n = max(len(left), len(right))
            if not n:
                break
            x = np.frombuffer(left.ljust(n, b'\0'), '<i2').astype(np.int32)
            y = np.frombuffer(right.ljust(n, b'\0'), '<i2').astype(np.int32)
            out.writeframesraw(np.clip(x + y, -32768, 32767).astype('<i2').tobytes())


class SkywalkAudio(MediaBridge):
    def __init__(self, phone, journal, input_gain, output_gain, record):
        super().__init__(journal, input_gain, output_gain, record)
        self.track.stop()
        self.incoming = asyncio.Queue(maxsize=INPUT_FRAMES)
        self.outgoing = asyncio.Queue(maxsize=OUTPUT_FRAMES)
        self.track = SkywalkTrack(self)
        self.phone = phone
        # Keep unused helper credits reserved. They occupy no PCM queue space,
        # but limit actual in-flight/queued playback to the selected target.
        # The socket protocol and helper's hard capacity remain unchanged.
        self.withheld_credits = phone.playback_frames - phone.playback_target_frames
        self.generation = phone.state['generation']
        # The remote phone chooses the codec when SCO connects, after dial/answer.
        self.rate = self.frame_bytes = self.codec = None
        self.up = self.down = None
        self.input_pending = bytearray()
        self.wavs = {}
        self.model_wavs = {}
        self.offsets = {1: 0, 2: 0}
        self.last_audio = None
        self.epoch = 0
        self.credits = self.sent_frames = 0
        self.credit_changed = asyncio.Event()
        self.output_primed = False
        self.output_started = False
        self.stats.update(input_missing_samples=0, output_missing_samples=0, ipc_output_dropped=0,
                          output_buffer_target_ms=phone.buffer_ms,
                          helper_target_ms=phone.playback_target_frames * 20,
                          output_prefill_ms=phone.output_prefill_frames * 20,
                          input_delivered_frames=0, input_queue_high_water=0,
                          output_rebuffers=0, output_underrun_frames=0,
                          output_startup_silence_frames=0, output_reset_frames=0,
                          output_received_frames=0, output_submitted_frames=0,
                          output_queue_high_water=0, loop_max_lag_ms=0)

    async def start(self):
        # An outgoing bridge is created before ATD so Live can connect first.
        # Bind media to the accepted call only after the remote party answers.
        self.generation = self.phone.state['generation']
        try:
            await self.phone.wait(lambda: self.phone.state.get('sco') == 1, 3)
        except TimeoutError:
            await self.phone.request('sco', self.generation)
            await self.phone.wait(lambda: self.phone.state.get('sco') == 1, 7)
        self.rate = self.phone.state.get('rate')
        self.codec = {1: 'CVSD', 2: 'mSBC'}.get(self.phone.state.get('codec'))
        if (self.codec, self.rate) not in (('CVSD', 8000), ('mSBC', 16000)):
            raise RuntimeError('Unsupported negotiated Skywalk audio format')
        self.frame_bytes = self.rate // 25
        if self.phone.state.get('frame_bytes') != self.frame_bytes:
            raise RuntimeError('Invalid negotiated Skywalk frame size')
        self.up = PCMResampler(self.rate, RATE)
        self.down = PCMResampler(RATE, self.rate)
        self.down.feed(SILENCE)  # Prime only the filter delay with silence.
        if self.record:
            for kind, name in ((1, 'caller'), (2, 'assistant')):
                self.wavs[kind] = TimelineWav(self.journal.directory / (name + '.wav'), self.rate)
            for kind, name in (('input', 'to_model'), ('output', 'from_model')):
                wav = wave.open(str(self.journal.directory / (name + '.wav')), 'wb')
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(RATE)
                self.model_wavs[kind] = wav
        # No PCM is emitted until audio-start; install the receiver first.
        self.spawn(self.capture_audio())
        await self.phone.request('audio-start', self.generation, rate=self.rate)
        self.ready = True
        self.last_audio = time.monotonic()
        self.spawn(self.play_audio())
        self.spawn(self.watch_audio())
        print(json.dumps({'event': 'audio-format', 'backend': 'skywalk', 'codec': self.codec,
                          'phone_rate': self.rate, 'buffer_ms': self.phone.buffer_ms}), flush=True)
        self.journal.event('audio-ready', backend='skywalk', codec=self.codec, phone_rate=self.rate, input_rate=RATE,
                           input_gain=self.input_gain, output_gain=self.output_gain,
                           helper_directory=str(self.phone.directory))

    def feed(self, packet):
        if packet and packet[0] == 3:
            if len(packet) != 9:
                raise ValueError('Malformed Skywalk playback credits')
            epoch, credits = struct.unpack('<II', packet[1:])
            if epoch < self.epoch:
                raise ValueError('Skywalk playback epoch moved backwards')
            if epoch != self.epoch:
                if self.epoch:
                    # Only discard on a replaced SCO link, never periodic pacing.
                    while not self.outgoing.empty():
                        self.outgoing.get_nowait()
                        self.stats['output_reset_frames'] += 1
                    self.output_primed = False
                self.epoch = epoch
                self.credits = self.sent_frames = 0
            if credits < self.credits or credits - self.sent_frames > self.phone.playback_frames:
                raise ValueError('Invalid Skywalk playback credits')
            self.credits = credits
            self.credit_changed.set()
            return
        if len(packet) < 11 or (len(packet) - 9) % 2:
            raise ValueError('Malformed Skywalk audio packet')
        kind, offset = struct.unpack('<BQ', packet[:9])
        data = packet[9:]
        if kind not in self.offsets or offset < self.offsets[kind]:
            raise ValueError('Invalid Skywalk audio clock')
        gap = offset - self.offsets[kind]
        if gap > self.rate * 10:
            raise ValueError('Skywalk audio clock jumped')
        prefix = 'input' if kind == 1 else 'output'
        self.stats[prefix + '_missing_samples'] += gap
        self.stats[prefix + '_bytes'] += len(data)
        self.stats[prefix + '_peak'] = max(self.stats[prefix + '_peak'],
            int(np.max(np.abs(np.frombuffer(data, '<i2').astype(np.int32)))))
        if kind in self.wavs:
            self.wavs[kind].write(offset, data)
        self.offsets[kind] = offset + len(data) // 2
        if kind == 1:
            self.last_audio = time.monotonic()
            # Preserve stream time across dropped datagrams, but don't accumulate
            # seconds of stale input in the WebRTC queue.
            converted = self.up.feed(bytes(gap * 2) + gain_pcm(data, self.input_gain))
            self.input_pending.extend(converted)
            while len(self.input_pending) >= BYTES:
                chunk = bytes(self.input_pending[:BYTES]); del self.input_pending[:BYTES]
                self.stats['input_dropped_frames'] += put_latest(self.incoming, chunk)
                self.stats['input_queue_high_water'] = max(
                    self.stats['input_queue_high_water'], self.incoming.qsize())

    async def capture_audio(self):
        loop = asyncio.get_running_loop()
        while True:
            self.feed(await loop.sock_recv(self.phone.pcm, 1024))
            # A backlog of ready datagrams must not starve the WebRTC sender.
            await asyncio.sleep(0)

    async def play_audio(self):
        while True:
            await self.credit_changed.wait()
            self.credit_changed.clear()
            while self.sent_frames < self.credits - self.withheld_credits:
                epoch = self.epoch
                data, from_model = await self.next_output()
                if epoch != self.epoch:
                    if from_model:
                        self.stats['output_reset_frames'] += 1
                    continue  # The old link's pending credit expired while waiting.
                frame = self.down.feed(gain_pcm(data, self.output_gain))
                if len(frame) != self.frame_bytes:
                    raise RuntimeError('Unexpected Skywalk resampler frame size')
                # Backpressure rather than a silent packet drop; the helper is
                # independently bounded and detects parent/heartbeat failure.
                self.sent_frames += 1
                await asyncio.wait_for(asyncio.get_running_loop().sock_sendall(
                    self.phone.pcm, struct.pack('<I', epoch) + frame), .5)
                self.stats['output_submitted_frames'] += int(from_model)

    async def receive_audio(self, track):
        """Bound decoded bursts; keep evidence before queueing for the SCO clock."""
        resampler = AudioResampler(format='s16', layout='mono', rate=RATE)
        pending = bytearray()
        while True:
            frame = await track.recv()
            for output in resampler.resample(frame):
                if not self.ready:
                    pending.clear()
                    continue
                pending.extend(output.to_ndarray().astype('<i2', copy=False).tobytes())
                while len(pending) >= BYTES:
                    chunk = bytes(pending[:BYTES]); del pending[:BYTES]
                    self.stats['output_received_frames'] += 1
                    if 'output' in self.model_wavs:
                        self.model_wavs['output'].writeframesraw(chunk)
                    self.stats['output_dropped_frames'] += put_latest(self.outgoing, chunk)
                    self.stats['output_queue_high_water'] = max(
                        self.stats['output_queue_high_water'], self.outgoing.qsize())

    async def next_output(self):
        if not self.ready:
            return SILENCE, False
        if not self.output_primed and self.outgoing.qsize() >= self.phone.output_prefill_frames:
            self.output_primed = self.output_started = True
        if self.output_primed:
            try:
                # Wait only while previously submitted PCM still covers SCO.
                # Fixed 40 ms waits inserted unnecessary silence even when the
                # helper had ample audio left. Outstanding credits are empty
                # slots, so subtract them and retain a 40 ms delivery margin.
                buffered = self.phone.playback_frames - (self.credits - self.sent_frames)
                wait = max(.005, buffered * .02 - .04)
                data = await asyncio.wait_for(self.outgoing.get(), wait)
                return data, True
            except TimeoutError:
                self.output_primed = False
                self.stats['output_rebuffers'] += 1
        key = 'output_underrun_frames' if self.output_started else 'output_startup_silence_frames'
        self.stats[key] += 1
        return SILENCE, False

    async def watch_audio(self):
        previous = time.monotonic()
        while True:
            await asyncio.sleep(.05)
            now = time.monotonic()
            self.stats['loop_max_lag_ms'] = max(self.stats['loop_max_lag_ms'], round((now-previous-.05)*1000, 2))
            previous = now
            if self.phone.failure:
                raise RuntimeError(self.phone.failure)
            if time.monotonic() - self.last_audio > 5:
                raise RuntimeError('Skywalk incoming audio stalled for five seconds')

    async def close(self, restore_links=True):
        self.ready = False
        self.track.stop()
        failures = []
        try:
            await self.phone.request('audio-stop', self.generation)
            # Control and media have independent queues. Let the receiver drain
            # to the last helper sample counters before closing its WAV files.
            deadline = time.monotonic() + .2
            while time.monotonic() < deadline and any(
                    self.offsets[k] < self.phone.state.get(field, 0)
                    for k, field in ((1, 'rx_samples'), (2, 'tx_samples'))):
                await asyncio.sleep(.005)
        except Exception as error:
            failures.append(str(error))
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for wav in self.wavs.values():
            wav.close()
        for wav in self.model_wavs.values():
            wav.close()
        if self.record and len(self.wavs) == 2:
            await asyncio.to_thread(mix_recordings, self.journal.directory)
        result = {'failures': failures, **self.stats, 'helper': dict(self.phone.state),
                  'output_queued_frames_at_close': self.outgoing.qsize(),
                  'codec': self.codec, 'recording_rate': self.rate,
                  'helper_directory': str(self.phone.directory)}
        dump(self.journal.directory / 'audio.json', result)
        self.journal.event('audio-closed', **result)
        return result
