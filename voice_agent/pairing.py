"""Local menu-app setup over private pipes. No calls, audio, API key or MCP tools."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
import tempfile
import urllib.error
import urllib.request

from .config import ROOT
from .paths import native_helper, FROZEN
from . import phone_profile

ERRORS = {
    'timeout': 'No phone connected. Open Bluetooth settings on your phone and try again.',
    'forget_on_phone': 'On your phone, forget HFPilot, then try connecting again.',
    'pairing_declined': 'Pairing was not confirmed. Try again and confirm on both devices.',
    'connection_failed': 'The phone could not connect. Try again; if it still fails, forget HFPilot on the phone first.',
    'authentication_failed': 'On your phone, forget HFPilot, then try connecting again.',
    'disconnected': 'The phone disconnected before setup finished. Please try again.',
    'phone_busy': 'Finish the call on your phone, then try again.',
    'legacy_pairing': 'This phone requested an unsupported pairing method.',
    'owner_lost': 'The setup window was closed.',
}


def emit(event, **details):
    print(json.dumps({'event': event, **details}), flush=True)


def local_control(command):
    token = (ROOT / '.work/mcp/local-owner-token').read_text().strip()
    request = urllib.request.Request('http://127.0.0.1:8765/local/phone-setup',
        data=json.dumps({'command': command}).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    try:
        with urllib.request.urlopen(request, timeout=65) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 409:
            raise RuntimeError(json.load(error)['error']) from None
        raise RuntimeError('Restart HFPilot to finish installing phone setup support') from None


async def bluetooth(power=None):
    executable = native_helper('blueutil')
    if not executable:
        raise RuntimeError('Bluetooth control is unavailable. Install blueutil and try again.')
    process = await asyncio.create_subprocess_exec(executable, '--power', *([] if power is None else [str(power)]),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 10)
    except BaseException:
        process.kill(); await process.wait()
        raise
    if process.returncode:
        raise RuntimeError('Allow HFPilot in System Settings > Privacy & Security > Bluetooth, then try again.')
    return output.decode().strip()


class Inputs:
    def __init__(self):
        self.cancelled = asyncio.Event()
        self.confirmations = asyncio.Queue()

    async def read(self):
        reader = asyncio.StreamReader(limit=1024)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        try:
            while raw := await reader.readline():
                command = json.loads(raw)
                if command.get('command') == 'cancel':
                    break
                if command.get('command') == 'confirm' and type(command.get('id')) is int:
                    self.confirmations.put_nowait(command['id'])
        finally:
            self.cancelled.set()
            transport.close()


async def pair(binary, staged, inputs, notify=emit):
    """Bounded helper protocol, testable without Bluetooth using an offline peer."""
    owner, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    owner.setblocking(False)
    loop = asyncio.get_running_loop()
    process = None
    tasks = []
    result = None
    name = 'Phone'
    helper_error = None
    try:
        with staged.with_suffix('.log').open('xb') as log:
            process = await asyncio.create_subprocess_exec(str(binary), '--pair', '180', str(child.fileno()), str(staged),
                pass_fds=(child.fileno(),), stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log,
                env={k: os.environ[k] for k in ('PATH', 'TMPDIR', 'LANG') if k in os.environ})
        child.close()
        async def send_controls():
            while True:
                try:
                    ident = await asyncio.wait_for(inputs.confirmations.get(), 2)
                    await loop.sock_sendall(owner, f'confirm {ident}'.encode())
                except TimeoutError:
                    await loop.sock_sendall(owner, b'ping')
        async def receive():
            nonlocal result, name, helper_error
            while True:
                event = json.loads(await loop.sock_recv(owner, 4096))
                kind = event.get('event')
                if kind == 'name':
                    name = phone_profile.display_name(bytes.fromhex(event['utf8_hex']).decode('utf-8', errors='replace'))
                    notify('name', name=name)
                elif kind in ('device', 'confirm'):
                    if not phone_profile.ADDRESS.fullmatch(event.get('address', '')):
                        raise RuntimeError('Invalid phone identity received during setup')
                    notify(kind, **{k: event[k] for k in ('id', 'address', 'code') if k in event})
                elif kind in ('waiting', 'connecting', 'securing'):
                    notify(kind)
                elif kind == 'connected':
                    result = {'address': event['address'], 'name': name}
                    return
                elif kind == 'error':
                    helper_error = ERRORS.get(event.get('reason'), 'Phone setup failed. Please try again.')
                    return
                else:
                    raise RuntimeError('Unknown phone setup response')
        receiver = asyncio.create_task(receive())
        exited = asyncio.create_task(process.wait())
        cancelled = asyncio.create_task(inputs.cancelled.wait())
        controls = asyncio.create_task(send_controls())
        tasks = [receiver, exited, cancelled, controls]
        done, _ = await asyncio.wait(tasks, timeout=185, return_when=asyncio.FIRST_COMPLETED)
        if inputs.cancelled.is_set():
            return None
        if receiver in done:
            await receiver
        elif controls in done:
            await controls
        else:
            # Final socket events can arrive just after the process-exit notification.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(receiver), .2)
            if receiver.done():
                await receiver
        if helper_error:
            raise RuntimeError(helper_error)
        if result is None:
            raise RuntimeError('Bluetooth setup stopped unexpectedly. Please try again.')
        if await asyncio.wait_for(asyncio.shield(exited), 10) != 0:
            raise RuntimeError('Bluetooth setup did not shut down cleanly; the phone was not saved.')
        return None if inputs.cancelled.is_set() else result
    finally:
        if process and process.returncode is None:
            with contextlib.suppress(OSError):
                await loop.sock_sendall(owner, b'cancel')
            try:
                await asyncio.wait_for(process.wait(), 8)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 7)
                except TimeoutError:
                    process.kill(); await process.wait()
                    helper_error = 'Bluetooth setup required forced shutdown. Restart HFPilot before trying again.'
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        owner.close(); child.close()
        if helper_error and process and process.returncode != 0:
            raise RuntimeError(helper_error)


async def run_setup(*, server_running):
    os.umask(0o077)
    directory = phone_profile.DIRECTORY
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = Path(native_helper('skywalk-hfp')) if FROZEN else directory / 'skywalk-build/skywalk-hfp'
    inputs = Inputs()
    input_task = asyncio.create_task(inputs.read())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, inputs.cancelled.set)
    paused = restore = False
    lock = None
    saved = None
    failure = None
    try:
        emit('preparing')
        if server_running:
            await asyncio.to_thread(local_control, 'pause')
            paused = True
        if inputs.cancelled.is_set():
            return
        lock = phone_profile.controller_lock(binary)
        selected = phone_profile.load()
        restore = await bluetooth() == '1'
        if restore:
            await bluetooth(0)
        with tempfile.TemporaryDirectory(prefix='pair-', dir=directory) as temporary:
            staged = Path(temporary) / 'bond.tlv'
            if selected:
                shutil.copyfile(phone_profile.private_file(selected['bond_file']), staged)
                staged.chmod(0o600)
            else:
                staged.touch(mode=0o600)
            try:
                result = await pair(binary, staged, inputs)
            finally:
                # Only stage names, HCI opcodes and status codes; no names,
                # comparison codes, payloads or keys are written by the helper.
                log = staged.with_suffix('.log')
                if log.exists():
                    saved_log = ROOT / '.work/mcp/pairing-helper.log'
                    shutil.copyfile(log, saved_log)
                    saved_log.chmod(0o600)
            if result and not inputs.cancelled.is_set():
                saved = phone_profile.commit(staged, **result)
    except Exception as error:
        failure = str(error)
    finally:
        if restore:
            try:
                await bluetooth(1)
            except Exception as error:
                failure = str(error)
        if lock:
            lock.close()
        if paused:
            try:
                await asyncio.to_thread(local_control, 'resume')
            except Exception:
                failure = 'Phone setup finished, but the assistant could not resume. Turn it off and on again.'
        input_task.cancel()
        await asyncio.gather(input_task, return_exceptions=True)
        if failure:
            emit('error', message=failure)
        elif saved:
            emit('complete', name=saved['name'], address=saved['address'])
        else:
            emit('cancelled')
