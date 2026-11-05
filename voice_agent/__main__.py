"""Use HFPilot to answer one call, dial one number, or listen for a bounded time window."""
import argparse
import asyncio
import fcntl
import math
import os
from pathlib import Path
import re
import signal
import sys
import time

from .calls import emit, handle_call, Finalizers
from .listener import IncomingCalls
from .phone import create_phone
from .live import OpenAI, api_key
from .config import ROOT, DEFAULT_CONFIG, load_call_settings
from .paths import native_helper


async def wait_call(phone, seconds, stop):
    gateway, calls = phone.read()
    if any(s != 'disconnected' for s in calls.values()):
        raise RuntimeError('Refusing to arm while a call already exists')
    emit('armed', timeout_seconds=seconds)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not stop.is_set():
        gateway, calls = phone.read()
        calls = {p: s for p, s in calls.items() if s != 'disconnected'}
        if len(calls) == 1 and next(iter(calls.values())) == 'incoming':
            return gateway, next(iter(calls))
        if calls:
            raise RuntimeError('Another call appeared; leaving it untouched')
        await asyncio.sleep(.2)
    raise TimeoutError('No incoming call before timeout, or stopped')


async def serve(args, api, phone, stop, expired, finalizers):
    listener = IncomingCalls(phone.read, emit, stop)
    async def expire():
        await asyncio.sleep(args.hours * 3600)
        expired.set()
        stop.set()
        emit('listen-window-expired', hours=args.hours)
    timer = asyncio.create_task(expire())
    calls, failures = 0, 0
    try:
        await listener.start()
        emit('listening', address=args.address, hours=args.hours)
        while not stop.is_set():
            ticket = await listener.next()
            if ticket is None:
                break
            try:
                completed = await handle_call(args, api, phone, ticket.gateway, ticket.path,
                                              stop, expired, listener, ticket)
            except Exception as error:
                # This ticket stays claimed, so an unanswered failed call is not
                # retried repeatedly. A later incoming call gets a new generation.
                failures += 1
                emit('call-setup-error', message=str(error).replace(api.key, '[redacted]'))
                continue
            calls += 1
            finalizers.submit(completed)
            if completed.metadata['audio']['failures']:
                emit('listener-stopped', reason='audio cleanup failed')
                failures += 1
                break
            # No sleep/re-arm gap: the watcher has already observed the next call
            # while Bluetooth cleanup was running. API finalization is background work.
    finally:
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)
        await listener.close()
    emit('listening-finished', calls=calls, setup_failures=failures,
         reason='window_expired' if expired.is_set() else 'stopped')
    return int(bool(failures))


async def execute(args):
    # Fail before opening Bluetooth/API; serve reloads these inputs per call.
    agent, _, _ = load_call_settings(args.config, args.assignment)
    if not args.address:
        raise ValueError('Source CLI requires --address; use HFPilot.app for saved phone setup.')
    stop, expired = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, stop.set)
    key = api_key()
    lockpath = Path(os.getenv('XDG_RUNTIME_DIR', '/tmp')) / ('voice-agent-' + args.address.replace(':', '') + '.lock')
    with lockpath.open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        async with OpenAI(key) as api:
            if args.command == 'preflight':
                create_phone(args).validate()
                for model in agent.models:
                    result = await api.request('GET', 'models/' + model)
                    emit('model-access', model=result['id'])
                return 0
            phone = create_phone(args)
            finalizers = Finalizers(api)
            exit_code = 0
            try:
                await phone.start()
                if args.command == 'serve':
                    exit_code = await serve(args, api, phone, stop, expired, finalizers)
                else:
                    if args.command == 'dial':
                        gateway, call = phone.address, phone.outgoing_call()
                    else:
                        gateway, call = await wait_call(phone, args.wait, stop)
                    completed = await handle_call(args, api, phone, gateway, call, stop, expired)
                    finalizers.submit(completed)
            finally:
                try:
                    await phone.close()
                finally:
                    failed_summaries = await finalizers.drain()
            return int(bool(exit_code or failed_summaries))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['preflight', 'run', 'serve', 'dial'])
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG,
                        help='Agent settings and prompts (default: project agent.toml)')
    parser.add_argument('--backend', choices=['pipewire', 'skywalk'],
                        default='skywalk' if sys.platform == 'darwin' else 'pipewire')
    parser.add_argument('--skywalk-binary', type=Path,
                        default=Path(native_helper('skywalk-hfp')))
    parser.add_argument('--skywalk-codec', choices=('auto', 'cvsd'), default='auto',
                        help='Skywalk: offer mSBC 16 kHz + CVSD 8 kHz (auto), or CVSD only')
    parser.add_argument('--skywalk-buffer-ms', type=int, choices=(200, 420), default=200,
                        help='Skywalk output buffering target in ms (default: 200; 420 for more jitter reserve)')
    parser.add_argument('--bond-file', type=Path, default=ROOT / '.work/macos-hfp/phone-bond.tlv')
    parser.add_argument('--address', help='Paired phone Bluetooth address; the app uses its saved phone profile')
    parser.add_argument('--assignment', type=Path,
                        help='Assignment file; required for dial, otherwise agent.default_assignment')
    parser.add_argument('--number', help='Outgoing number in international format, e.g. +12025550123')
    parser.add_argument('--dial-timeout', type=float, default=60,
                        help='Maximum wait for outgoing answer, in seconds (default: 60)')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/calls')
    parser.add_argument('--wait', type=float, default=180, help='Incoming wait for run, in seconds')
    parser.add_argument('--hours', type=float, default=12, help='Continuous serve window, at most 24 hours')
    parser.add_argument('--max-call', type=float, default=600)
    parser.add_argument('--input-gain', type=float, default=None)
    parser.add_argument('--output-gain', type=float, default=.35)
    parser.add_argument('--record-audio', action=argparse.BooleanOptionalAction, default=None,
                        help='Override the saved recording preference for this process (default: enabled)')
    parser.add_argument('--test-call', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'dial':
        if args.backend != 'skywalk':
            parser.error('dial is currently implemented only for Skywalk on Mac')
        if not args.number or args.assignment is None:
            parser.error('dial requires --number and --assignment')
        from .skywalk import dial_number
        try:
            args.number = dial_number(args.number)
        except ValueError as error:
            parser.error(str(error))
    elif args.number is not None:
        parser.error('--number is only valid with dial')
    if args.address is not None:
        if not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', args.address):
            parser.error('address must be a Bluetooth MAC address, e.g. AA:BB:CC:DD:EE:FF')
        args.address = args.address.upper()
    if args.input_gain is None:
        args.input_gain = 1 if args.backend == 'skywalk' else 4
    if args.backend == 'skywalk' and args.command == 'serve':
        parser.error('Use HFPilot.app for continuous Skywalk operation; serve is a legacy Linux command')
    if not 1 <= args.wait <= 3600 or not 10 <= args.max_call <= 3600:
        parser.error('wait must be 1–3600 seconds; max-call 10–3600')
    if not 5 <= args.dial_timeout <= 300:
        parser.error('dial-timeout must be 5–300 seconds')
    if not math.isfinite(args.hours) or not 0 < args.hours <= 24:
        parser.error('hours must be greater than zero and at most 24')
    if not 0 < args.input_gain <= 32 or not 0 < args.output_gain <= 1:
        parser.error('input-gain must be (0,32]; output-gain (0,1]')
    return args


def main():
    os.umask(0o077)
    args = parse_args()
    try:
        return asyncio.run(execute(args))
    except (Exception, KeyboardInterrupt) as error:
        emit('fatal', message=str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
