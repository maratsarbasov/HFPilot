"""App-owned OpenAI tunnel. Private pipes carry settings/status, never credentials.

The vendor client is installed separately. Closing the parent pipe or receiving
SIGTERM stops only our child process group; no phone or MCP mutation is performed.
"""
import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile

import aiohttp

from voice_agent.tunnel_client import access_code, client_arguments, client_environment, installed_client
from .live import api_key
from .paths import ROOT

TUNNEL_ID = re.compile(r'tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}')


def health_state(value):
    """Read actual successful poll evidence; /readyz alone can be a false positive."""
    if value.get('schema_version') != 1:
        return 'error', 'Update tunnel-client with brew upgrade openai/tools/tunnel-client, then reconnect.'
    control = value.get('components', {}).get('control-plane', {})
    details = control.get('details', {})
    if control.get('status') == 'degraded':
        reason = str(control.get('reason_code', '')) + str(details.get('failure_category', ''))
        if details.get('http_status') in (401, 403) or any(word in reason.lower() for word in ('auth', 'permission', 'forbidden', '401', '403')):
            return 'error', 'OpenAI rejected the connection. Check the Tunnel ID and give your saved API key Tunnels Read + Use access.'
        return 'reconnecting', 'Connection unavailable. Check the network, Tunnel ID and API key permissions. Retrying…'
    if value.get('ready') is True and control.get('status') == 'ok' and details.get('last_success'):
        return 'connected', 'Connected to OpenAI. You can close this window.'
    return 'connecting', 'Connecting to OpenAI…'


async def supervise(binary, tunnel_id, env, directory, parent, stopped, emit, *, interval=2):
    """One owned client with bounded cleanup. Inputs are injectable for offline tests."""
    health_file = directory / 'health-url'
    if stopped.is_set() or parent.at_eof():
        stopped.set()
        return
    process = await asyncio.create_subprocess_exec(binary, 'run', *client_arguments(tunnel_id),
        '--health.url-file', str(health_file), '--control-plane.initial-poll-timeout', '5s',
        env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)

    async def parent_closed():
        while await parent.read(1024):
            pass
        stopped.set()

    async def monitor():
        previous = None
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=2)) as http:
            while not stopped.is_set():
                state = ('connecting', 'Starting tunnel-client…')
                try:
                    url = health_file.read_text().strip()
                    if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', url):
                        raise ValueError('Invalid local health address')
                    async with http.get(url + '/health?details=true', allow_redirects=False) as response:
                        raw = await response.content.read(65537)
                        if response.status != 200 or len(raw) > 65536:
                            raise ValueError('Health unavailable')
                        state = health_state(json.loads(raw))
                except (OSError, ValueError, TypeError, AttributeError, aiohttp.ClientError, TimeoutError):
                    if previous:
                        state = ('reconnecting', 'Waiting for tunnel-client to report its connection…')
                if state != previous:
                    emit(*state)
                    previous = state
                await asyncio.sleep(interval)

    tasks = [asyncio.create_task(parent_closed()), asyncio.create_task(monitor()),
             asyncio.create_task(stopped.wait()), asyncio.create_task(process.wait())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if not stopped.is_set():
            emit('error', 'tunnel-client stopped. Check its installation, Tunnel ID and API key permissions, then reconnect.')
    finally:
        # Includes any companion process launched by this client, never an external tunnel.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 4)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        # A companion can outlive a promptly exiting parent or ignore SIGTERM.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run():
    os.umask(0o077)
    previous = None

    def emit(state, message):
        nonlocal previous
        previous = state
        with contextlib.suppress(BrokenPipeError):
            print(json.dumps({'state': state, 'message': message}), flush=True)

    parent = asyncio.StreamReader(limit=8192)
    loop = asyncio.get_running_loop()
    pipe, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(parent), sys.stdin)
    stopped = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        try:
            settings = json.loads(await asyncio.wait_for(parent.readline(), 10))
            tunnel_id = settings['tunnel_id'].strip()
            if not TUNNEL_ID.fullmatch(tunnel_id):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError, TimeoutError):
            emit('error', 'Enter the complete Tunnel ID from OpenAI Platform.')
            return 1
        binary = shutil.which(installed_client())
        if not binary:
            emit('error', 'Install tunnel-client with Homebrew, or choose its installed file.')
            return 1
        try:
            key = api_key()
            if not key or any(not 33 <= ord(c) <= 126 for c in key):
                raise ValueError()
        except (OSError, RuntimeError, ValueError):
            emit('error', 'Add a valid OpenAI API key in HFPilot Settings.')
            return 1
        try:
            code = access_code()
        except ValueError:
            emit('error', 'Start HFPilot’s local server before connecting the tunnel.')
            return 1
        state = ROOT / '.work/mcp'
        with os.fdopen(os.open(state / 'openai-tunnel.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                emit('error', 'Another HFPilot tunnel is still stopping. Wait a moment and reconnect.')
                return 1
            with tempfile.TemporaryDirectory(prefix='openai-tunnel-', dir=state) as directory:
                emit('connecting', 'Starting tunnel-client…')
                await supervise(binary, tunnel_id, client_environment(key, code), Path(directory), parent, stopped, emit)
        if stopped.is_set():
            emit('stopped', 'Disconnected')
        return 0 if previous == 'stopped' else 1
    except (OSError, ValueError):
        emit('error', 'The tunnel could not start. Check the client installation and reconnect.')
        return 1
    finally:
        pipe.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main():
    return asyncio.run(run())


if __name__ == '__main__':
    sys.exit(main())
