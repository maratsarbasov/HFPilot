#!/usr/bin/env python3
"""Exercise the real Skywalk IPC with a simulated SCO clock, without Bluetooth/API."""
import argparse
import asyncio
from fractions import Fraction
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import numpy as np
from av import AudioFrame

ROOT = Path(__file__).resolve().parents[1]


class BurstyTrack:
    def __init__(self, burst_frames=8):
        self.burst_frames = burst_frames
        self.frames = 0
        self.started = time.monotonic()

    async def recv(self):
        if self.frames % self.burst_frames == 0:
            deadline = self.started + (self.frames + self.burst_frames) * .02
            await asyncio.sleep(max(0, deadline - time.monotonic()))
        frame = AudioFrame(format='s16', layout='mono', samples=960)
        t = np.arange(self.frames * 960, (self.frames + 1) * 960)
        frame.planes[0].update((np.sin(t * 2 * np.pi * 500 / 48000) * 8000).astype('<i2').tobytes())
        frame.sample_rate = 48000
        frame.pts = self.frames * 960
        frame.time_base = Fraction(1, 48000)
        self.frames += 1
        return frame


async def exercise(binary, root, seconds=5, stall_ms=120, burst_frames=8, buffer_ms=None, rate=8000):
    from voice_agent.skywalk import SkywalkPhone
    from voice_agent.journal import Journal
    bond = root / 'fake-bond'; bond.touch(mode=0o600)
    # Omit the optional keyword for old source snapshots that predate profiles.
    options = {} if buffer_ms is None else {'buffer_ms': buffer_ms}
    phone = SkywalkPhone('AA:BB:CC:DD:EE:FF', binary, bond, int(seconds)+15, root/'logs', **options)
    journal = Journal(root/'call')
    bridge = reader = None
    try:
        await phone.start()
        if rate == 16000:
            await phone.request('select-msbc')
        await phone.request('ring')
        call = next(iter(phone.read()[1]))
        await phone.answer(call)
        bridge = phone.audio(journal, 1, 1, False)
        await bridge.start()
        source = BurstyTrack(burst_frames)
        bridge.spawn(bridge.receive_audio(source))
        delivered = 0
        async def consume():
            nonlocal delivered
            while True:
                await bridge.track.recv()
                delivered += 1
                await asyncio.sleep(.001)  # Encoding/send scheduling point.
        reader = asyncio.create_task(consume())
        started = time.monotonic()
        stalls = 0
        while time.monotonic() - started < seconds:
            await asyncio.sleep(.45)
            if stall_ms:
                time.sleep(stall_ms / 1000)  # Deliberately block this event loop, not the C helper.
                stalls += 1
        # Allow the final injected pause to drain before checking the counters.
        await asyncio.sleep(.35)
        if bridge.failure:
            raise RuntimeError(bridge.failure)
        await phone.hangup(call)
        reader.cancel(); await asyncio.gather(reader, return_exceptions=True); reader = None
        result = await bridge.close(); bridge = None
        result.update(source_frames=source.frames, consumed_frames=delivered, injected_stalls=stalls,
                      stall_ms=stall_ms, burst_frames=burst_frames,
                      elapsed_seconds=round(time.monotonic()-started, 3))
        result['passed'] = not result['failures'] and not any(result.get(k, 0) for k in (
            'input_dropped_frames', 'output_dropped_frames', 'ipc_output_dropped',
            'input_missing_samples', 'output_missing_samples', 'output_underrun_frames')) and not any(
            result['helper'].get(k, 0) for k in ('tx_dropped_bytes', 'tx_silence_bytes', 'rx_ipc_dropped'))
        return result
    finally:
        if reader:
            reader.cancel(); await asyncio.gather(reader, return_exceptions=True)
        if bridge:
            await bridge.close()
        await phone.close()
        journal.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=5)
    parser.add_argument('--stall-ms', type=float, default=120)
    parser.add_argument('--burst-frames', type=int, default=8)
    parser.add_argument('--buffer-ms', type=int, choices=(200, 420), default=None,
                        help='Override Skywalk buffering; otherwise use the source version default')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--baseline-dir', type=Path, help='Optional private source snapshot for a before/after comparison')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 600 or not 0 <= args.stall_ms <= 2000 or not 1 <= args.burst_frames <= 100:
        parser.error('Invalid bounded stress parameters')
    if args.report.exists():
        parser.error('Report already exists; use a new path')
    sys.path.insert(0, str(ROOT))
    source = ROOT/'macos/Skywalk'
    ipc = source/'agent_ipc.c'
    if args.baseline_dir:
        import voice_agent
        for name in ('media', 'skywalk_audio', 'skywalk'):
            spec = importlib.util.spec_from_file_location('voice_agent.'+name, args.baseline_dir/(name+'.py'))
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module; spec.loader.exec_module(module)
        ipc = args.baseline_dir/'agent_ipc.c'
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); binary = root/'ipc-peer'
        subprocess.run(['cc', '-Wall', '-Wextra', '-I'+str(source),
                        str(ROOT/'tests/fixtures/skywalk_phone.c'), str(ipc),
                        str(source/'caller_id.c'), str(source/'call_state.c'), '-o', str(binary)], check=True)
        report = asyncio.run(exercise(binary, root, args.seconds, args.stall_ms, args.burst_frames, args.buffer_ms))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open('x') as f:
            json.dump(report, f, indent=2); f.write('\n')
        print(json.dumps(report))
        if not report['passed']:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
