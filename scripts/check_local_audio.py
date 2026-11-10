"""Opt-in hardware smoke check for the app's default-device audio bridge.

Requires microphone permission already granted to HFPilot. Plays silence,
discards captured PCM, and never calls a model or writes an audio recording.
Run separately from the offline suite, on a Mac with input and output devices.
"""
import argparse
import json
from pathlib import Path
import subprocess


def check(binary, seconds):
    frames = int(seconds * 48000)
    result = subprocess.run([str(binary), '--test-audio'], input=bytes(frames * 2),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=seconds + 15)
    diagnostics = result.stderr.decode(errors='replace')
    events = []
    for line in diagnostics.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    ready = next((event for event in events if event.get('event') == 'ready'), None)
    if result.returncode or any(event.get('event') == 'error' for event in events):
        raise RuntimeError(f'Native audio exited with {result.returncode}:\n{diagnostics}')
    if not ready or ready.get('capture_rate', 0) <= 0:
        raise RuntimeError(f'Native audio did not report its device formats:\n{diagnostics}')
    if ready['capture_rate'] != ready.get('playback_rate'):
        raise RuntimeError(f'Voice processing sample rates differ: {ready}')
    # Playback queue capacity is 100 ms. Feeding several seconds exercises the
    # actual output callbacks as well as input capture; a stalled device fails.
    captured = len(result.stdout)
    if captured % 2 or captured < (seconds - 1) * 48000 * 2:
        raise RuntimeError(f'Incomplete microphone stream: {captured} PCM bytes')
    return {**ready, 'captured_frames': captured // 2, 'exit_code': result.returncode}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path)
    parser.add_argument('--seconds', type=int, default=3, choices=range(2, 11))
    parser.add_argument('--runs', type=int, default=2, choices=range(1, 6))
    args = parser.parse_args()
    binary = args.app.resolve() / 'Contents/MacOS/HFPilot'
    for index in range(args.runs):
        print(json.dumps({'run': index + 1, **check(binary, args.seconds)}), flush=True)


if __name__ == '__main__':
    main()
