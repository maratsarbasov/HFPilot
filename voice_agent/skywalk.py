"""A supervised Skywalk HFP helper and its private, bounded IPC channels."""
import asyncio
import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
import re
import socket
import time
import uuid

from .journal import dump
from .caller import unknown_caller
from .phone_profile import controller_lock
from .paths import ROOT


class CommandRejected(RuntimeError):
    """A helper command was refused while the control channel remains usable."""


def dial_number(value):
    """Only explicit international phone numbers, never AT commands or USSD."""
    if not re.fullmatch(r'\+[1-9][0-9]{6,14}', value):
        raise ValueError('number must be + followed by 7–15 digits, without spaces')
    return value


class SkywalkPhone:
    name = 'skywalk'

    def __init__(self, address, binary, bond_file, timeout, log_root=None, *, buffer_ms=200, codec='auto'):
        self.address = address
        self.binary, self.bond_file = Path(binary), Path(bond_file)
        self.timeout = int(timeout)
        if buffer_ms not in (200, 420):
            raise ValueError('Skywalk output buffering must be 200 or 420 ms')
        self.buffer_ms = buffer_ms
        if codec not in ('auto', 'cvsd'):
            raise ValueError('Skywalk codec must be auto or cvsd')
        self.codec = codec
        self.output_prefill_frames = 2 if buffer_ms == 200 else 5
        self.playback_target_frames = buffer_ms // 20 - self.output_prefill_frames
        self.log_root = Path(log_root) if log_root else ROOT / 'artifacts/skywalk'
        self.process = self.control = self.pcm = self.log = None
        self.state = {}
        self.failure = None
        self.tasks = []
        self.pending = {}
        self.serial = 0
        self.stopping = False
        self.ready = asyncio.Event()
        self.stopped = asyncio.Event()
        self.changed = asyncio.Event()
        self.directory = None
        self.playback_frames = 0
        self.supports_dial = False
        self.controller_owner = None

    def validate(self):
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise RuntimeError('Build skywalk-hfp first; see README.md#building-from-source')
        # An agent run must reuse an explicitly prepared pairing, never create one silently.
        if self.bond_file.is_symlink() or not self.bond_file.is_file():
            raise RuntimeError('An existing private Skywalk pairing database is required')
        info = self.bond_file.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('Skywalk pairing database must be owned by you with mode 600')
        if not 0 <= self.timeout <= 86400:
            raise ValueError('Skywalk helper timeout must be 0 (application lifetime) or 1–86400 seconds')

    def claim_controller(self):
        if self.controller_owner is None:
            self.controller_owner = controller_lock(self.binary)

    async def start(self):
        self.validate()
        self.claim_controller()
        self.directory = self.log_root / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        self.directory.mkdir(parents=True, mode=0o700)
        self.log = (self.directory / 'helper.log').open('xb')
        self.control, child_control = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.pcm, child_pcm = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        for sock in (self.control, child_control, self.pcm, child_pcm):
            sock.setblocking(False)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(self.binary), '--agent', str(self.timeout), self.address,
                str(child_control.fileno()), str(child_pcm.fileno()), str(self.bond_file), 'v3', self.codec,
                pass_fds=(child_control.fileno(), child_pcm.fileno()),
                stdin=asyncio.subprocess.DEVNULL, stdout=self.log, stderr=self.log,
                env={k: os.environ[k] for k in ('PATH', 'TMPDIR', 'LANG') if k in os.environ})
        finally:
            child_control.close(); child_pcm.close()
        self.tasks = [asyncio.create_task(self._events()), asyncio.create_task(self._watch_process())]
        await self.wait(lambda: self.ready.is_set(), 10)
        self.tasks.append(asyncio.create_task(self._heartbeat()))
        # Controller initialization runs asynchronously. Retry no mutating command;
        # wait until HCI has finished initialization before the one connect request.
        await self.wait(lambda: self.state.get('working') == 1, 15)
        await self.request('connect')
        await self.wait(lambda: self.state.get('slc') and
                        all(self.state.get(k, -1) >= 0 for k in ('call', 'setup', 'held')), 25)
        self.read()

    async def _events(self):
        loop = asyncio.get_running_loop()
        try:
            while True:
                raw = await loop.sock_recv(self.control, 4096)
                if not raw:
                    raise RuntimeError('Skywalk control channel closed')
                event = json.loads(raw)
                if event['event'] == 'ready':
                    if event.get('protocol') != 3 or event.get('rates') != [8000, 16000]:
                        raise RuntimeError('Unsupported Skywalk IPC format; rebuild skywalk-hfp for v3')
                    self.playback_frames = event.get('playback_frames', 0)
                    self.supports_dial = event.get('dial') is True
                    if not isinstance(self.playback_frames, int) or not 1 <= self.playback_frames <= 50:
                        raise RuntimeError('Invalid Skywalk playback window')
                    if self.playback_frames < self.playback_target_frames:
                        raise RuntimeError('Skywalk helper window is too small for selected buffering; rebuild it')
                    self.ready.set()
                elif event['event'] == 'state':
                    self.state.update(event)
                elif event['event'] == 'audio-error':
                    # Preserve the control reader so bounded helper cleanup can
                    # still confirm idle and deliver its final counters.
                    self.failure = 'Skywalk audio: ' + event['message']
                elif event['event'] == 'working':
                    self.state['working'] = 1
                elif event['event'] == 'ack':
                    future = self.pending.get(event['id'])
                    if future and not future.done():
                        future.set_result(event['status'])
                elif event['event'] == 'stopped':
                    self.stopped.set()
                    return
                self.changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failure = 'Skywalk event channel: ' + str(error)
        finally:
            self.changed.set()

    async def _watch_process(self):
        code = await self.process.wait()
        if not self.stopping and not self.failure:
            self.failure = f'Skywalk helper exited ({code}); see {self.directory / "helper.log"}. Native Bluetooth must be OFF.'
        for future in self.pending.values():
            if not future.done():
                future.set_exception(RuntimeError(self.failure or 'Skywalk helper stopped'))
        self.changed.set()

    async def _heartbeat(self):
        try:
            while True:
                await asyncio.sleep(2)
                await self.request('status')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self.stopping:
                self.failure = 'Skywalk heartbeat: ' + str(error)
                self.changed.set()

    async def wait(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while not predicate():
            if self.failure:
                raise RuntimeError(self.failure)
            self.changed.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Skywalk state transition timed out')
            await asyncio.wait_for(self.changed.wait(), remaining)
        if self.failure:
            raise RuntimeError(self.failure)

    async def request(self, command, generation=0, *, number=None, rate=None):
        if number is not None:
            if command != 'dial':
                raise ValueError('Only dial accepts a number')
            dial_number(number)
        if rate is not None and (command != 'audio-start' or rate not in (8000, 16000)):
            raise ValueError('Only audio-start accepts a negotiated PCM rate')
        if self.process is None or self.process.returncode is not None:
            raise RuntimeError(self.failure or 'Skywalk helper is not running')
        self.serial += 1
        ident = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            payload = f'{ident} {command} {generation}'.encode('ascii')
            if number is not None:
                payload += b' ' + number.encode('ascii')
            if rate is not None:
                payload += f' {rate}'.encode('ascii')
            await asyncio.wait_for(asyncio.get_running_loop().sock_sendall(self.control, payload), 2)
            status = await asyncio.wait_for(future, 5)
            if status:
                raise CommandRejected(f'Skywalk rejected {command}: {status}')
        finally:
            self.pending.pop(ident, None)

    def read(self):
        if self.failure:
            raise RuntimeError(self.failure)
        s = self.state
        if not s.get('slc'):
            raise RuntimeError('Skywalk phone disconnected')
        if any(s.get(k, -1) < 0 for k in ('call', 'setup', 'held')):
            raise RuntimeError('Skywalk call indicators are not ready')
        call = str(s['generation'])
        calls = {}
        if s['call']:
            calls[call] = 'held' if s['held'] else 'active'
            if s['setup'] == 1:
                calls[call + '/waiting'] = 'incoming'  # Prevent taking over call waiting.
        elif s['setup']:
            calls[call] = {1: 'incoming', 2: 'dialing', 3: 'alerting'}.get(s['setup'], 'unknown')
        elif s['held']:
            calls[call] = 'held'
        elif s.get('dial_pending'):
            calls[call] = 'dialing'  # Requested; remote callsetup may not yet have arrived.
        return self.address, calls

    def outgoing_call(self):
        if not self.supports_dial:
            raise RuntimeError('Rebuild skywalk-hfp: this helper does not support dial')
        if self.read()[1]:
            raise RuntimeError('Refusing to dial while another call exists')
        return str(self.state['generation'] + 1)

    def owns_call(self, call):
        return str(self.state.get('generation')) == call and bool(self.state.get('accepted'))

    async def dial(self, number, call):
        dial_number(number)
        if self.outgoing_call() != call:
            raise RuntimeError('Phone call state changed before dial; leaving it untouched')
        await self.request('dial', int(call), number=number)

    async def answer(self, call):
        if self.read()[1] != {call: 'incoming'}:
            raise RuntimeError('Incoming call changed before answer')
        await self.request('answer', int(call))

    def caller(self, call):
        s = self.state
        if str(s.get('generation')) != call or not s.get('slc'):
            return None  # Never attach another generation's identity to this call.
        result = unknown_caller()
        if not s.get('caller_received'):
            return result
        def decode(field, limit):
            value = s.get(field, '')
            if not isinstance(value, str) or len(value) > limit * 2:
                raise ValueError('Invalid Skywalk caller field')
            return bytes.fromhex(value).decode('utf-8', errors='replace') or None
        result.update(number=decode('caller_number_hex', 127),
                      name=decode('caller_name_hex', 255),
                      number_type=s.get('caller_type'),
                      source={1: 'hfp_clip', 2: 'hfp_clcc'}.get(s.get('caller_source', 1)))
        result['status'] = 'reported' if result['number'] or result['name'] else 'unavailable'
        return result

    async def prepare_caller(self, call, timeout=1.5):
        # Prefer the unsolicited CLIP (includes optional name), then one read-only
        # current-call query. Caller-ID absence must not block answering forever.
        deadline = time.monotonic() + timeout
        def done():
            return self.read()[1] != {call: 'incoming'} or bool(self.caller(call).get('source'))
        try:
            await self.wait(done, min(.35, timeout))
        except TimeoutError:
            if self.read()[1] == {call: 'incoming'} and time.monotonic() < deadline:
                try:
                    await asyncio.wait_for(self.request('caller-query', int(call)),
                                           max(.01, deadline - time.monotonic()))
                    await self.wait(done, max(.01, deadline - time.monotonic()))
                except (TimeoutError, CommandRejected):
                    # Optional identity lookup must not prevent answering on
                    # phones without CLCC support. Transport failure still fails.
                    self.read()
        return self.caller(call) or unknown_caller()

    async def hangup(self, call):
        if self.read()[1].get(call) in (None, 'incoming') or not self.owns_call(call):
            return
        await self.request('hangup', int(call))
        await self.wait(lambda: self.read()[1].get(call) in (None, 'incoming'), 5)

    def audio(self, journal, input_gain, output_gain, record):
        from .skywalk_audio import SkywalkAudio
        return SkywalkAudio(self, journal, input_gain, output_gain, record)

    async def close(self, *, release_controller=True):
        self.stopping = True
        error = None
        try:
            if self.process and self.process.returncode is None:
                with contextlib.suppress(Exception):
                    await self.request('quit')
                try:
                    await asyncio.wait_for(self.process.wait(), 8)
                except TimeoutError:
                    self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), 7)
                    except TimeoutError:
                        self.process.kill()
                        await self.process.wait()
                    error = 'Skywalk helper required forced shutdown; inspect phone call state'
            if self.process and self.process.returncode == 0:
                # Process exit and final control datagrams have separate wakeups.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stopped.wait(), .5)
            if self.state.get('accepted') and any(self.state.get(k) for k in ('call', 'setup', 'held', 'dial_pending')):
                error = 'Skywalk shutdown did not confirm idle call indicators'
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.directory:
                dump(self.directory / 'shutdown.json', {'state': self.state, 'failure': self.failure,
                     'cleanup_error': error, 'returncode': self.process.returncode if self.process else None})
            for resource in (self.control, self.pcm, self.log):
                if resource:
                    resource.close()
            if release_controller and self.controller_owner:
                self.controller_owner.close()
        if error:
            raise RuntimeError(error)
