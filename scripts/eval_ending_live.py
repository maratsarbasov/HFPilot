"""Bounded Live-model evaluation with recorded caller audio; never opens a phone.

Uses the configured paid Live/Responses API. Audio is sent only to OpenAI.
Records generated speech and tool intent; this is NOT a Bluetooth/hangup test.
"""
import argparse
import asyncio
from datetime import datetime, timezone
from fractions import Fraction
import json
from pathlib import Path
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiortc import AudioStreamTrack
from av import AudioFrame, AudioResampler
from voice_agent.config import DEFAULT_CONFIG, configuration, load_call_settings
from voice_agent.journal import Journal, dump
from voice_agent.live import LiveSession, OpenAI, api_key
from voice_agent.media import MediaBridge, RATE, SILENCE, BYTES, put_latest
from voice_agent.skywalk_audio import PCMResampler


class ReplayTrack(AudioStreamTrack):
    def __init__(self, pcm, bridge):
        super().__init__()
        self.pcm, self.position, self.pts = pcm, 0, 0
        self.started = None
        self.play = False
        self.bridge = bridge

    async def recv(self):
        if self.started is None:
            self.started = time.monotonic()
        await asyncio.sleep(max(0, self.started + self.pts / RATE - time.monotonic()))
        data = SILENCE
        if self.play and self.position < len(self.pcm):
            data = self.pcm[self.position:self.position + BYTES].ljust(BYTES, b'\0')
            self.position += BYTES
        frame = AudioFrame(format='s16', layout='mono', samples=960)
        frame.planes[0].update(data)
        frame.sample_rate = RATE; frame.pts = self.pts; frame.time_base = Fraction(1, RATE)
        self.pts += 960
        return frame


class EvalBridge(MediaBridge):
    def __init__(self, journal, pcm):
        super().__init__(journal, 1, 1, True)
        self.track.stop(); self.track = ReplayTrack(pcm, self)
        self.outgoing = asyncio.Queue(maxsize=26)
        self.ready = True
        self.wavs = {}
        for name in ('generated', 'played'):
            wav = wave.open(str(journal.directory / (name + '.wav')), 'wb')
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(RATE)
            self.wavs[name] = wav
        self.spawn(self.play_audio())

    async def play_audio(self):
        target = time.monotonic()
        while True:
            await asyncio.sleep(max(0, target - time.monotonic()))
            target = max(target + .02, time.monotonic())
            data = self.outgoing.get_nowait() if not self.outgoing.empty() else SILENCE
            self.wavs['played'].writeframesraw(data)

    async def receive_audio(self, track):
        resampler = AudioResampler(format='s16', layout='mono', rate=RATE)
        pending = bytearray()
        while True:
            frame = await track.recv()
            for output in resampler.resample(frame):
                pending.extend(output.to_ndarray().astype('<i2', copy=False).tobytes())
                while len(pending) >= BYTES:
                    chunk = bytes(pending[:BYTES]); del pending[:BYTES]
                    self.wavs['generated'].writeframesraw(chunk)
                    put_latest(self.outgoing, chunk)


async def run(args):
    agent, assignment, _ = load_call_settings(args.config, args.assignment)
    with wave.open(str(args.source)) as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError('Use a mono PCM16 caller WAV')
        rate = wav.getframerate(); wav.setpos(round(args.start * rate))
        pcm = wav.readframes(round((args.end - args.start) * rate))
    pcm = PCMResampler(rate, RATE).feed(pcm)
    if not pcm:
        raise ValueError('The selected caller audio is empty')
    journal = Journal(args.output)
    dump(journal.directory / 'agent-config.json', agent.data)
    (journal.directory / 'assignment.txt').write_text(assignment)
    bridge = EvalBridge(journal, pcm)
    key = api_key(); result = {'passed': False, 'phone_used': False,
                              'scope': 'Model tool intent and fixed hangup timer only; review transcript/audio for speech quality.'}
    live = None
    try:
        async with OpenAI(key) as api:
            live = LiveSession(api, bridge, journal, agent)
            await live.connect(configuration(agent, assignment, datetime.now(timezone.utc).isoformat(),
                                             outgoing_number=None if args.incoming else '+12025550123'))
            live.greet(outgoing=not args.incoming)
            start = time.monotonic()
            while time.monotonic() - start < args.timeout:
                elapsed = time.monotonic() - start
                if elapsed >= args.answer_after and not bridge.track.play:
                    bridge.track.play = True
                    journal.event('eval-caller-start', elapsed=elapsed)
                    print('Playing recorded caller answer.', flush=True)
                if live.failure or bridge.failure or live.closed.is_set():
                    raise RuntimeError(live.failure or bridge.failure or 'Session closed')
                if live.ending.due():
                    heard = any(f['speaker'] == 'caller' for f in journal.fragments)
                    result.update(passed=args.expect == 'end' and bridge.track.position >= len(bridge.track.pcm) and heard,
                                  outcome='model_requested_hangup', elapsed_seconds=round(elapsed, 3))
                    break
                await asyncio.sleep(.05)
            else:
                result.update(passed=args.expect == 'continue' and bridge.track.position >= len(bridge.track.pcm)
                              and any(f['speaker'] == 'caller' for f in journal.fragments)
                              and live.ending.result is None,
                              outcome='no_end_call_before_timeout', elapsed_seconds=args.timeout)
            result['live'] = await live.close()
    except Exception as error:
        result.update(error=str(error).replace(key, '[redacted]'))
    finally:
        if live is not None:
            await live.peer.close()
        bridge.track.stop()
        for task in bridge.tasks: task.cancel()
        await asyncio.gather(*bridge.tasks, return_exceptions=True)
        for wav in bridge.wavs.values(): wav.close()
        journal.render(); journal.close()
        dump(journal.directory / 'report.json', result)
    print(json.dumps({k:v for k,v in result.items() if k != 'live'}, ensure_ascii=False), flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--assignment', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--end', type=float, required=True)
    parser.add_argument('--answer-after', type=float, default=12)
    parser.add_argument('--incoming', action='store_true')
    parser.add_argument('--timeout', type=float, default=55)
    parser.add_argument('--expect', choices=('end', 'continue'), default='end')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.start < args.end or not 1 <= args.timeout <= 90 or not 0 <= args.answer_after < args.timeout:
        parser.error('Invalid bounds; timeout must be 1–90 seconds')
    raise SystemExit(asyncio.run(run(args)))
