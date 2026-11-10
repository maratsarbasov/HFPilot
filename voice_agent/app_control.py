"""Locally start or stop HFPilot's MCP server and phone assistant."""
import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.request
import urllib.error
import sys

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice_agent.paths import ROOT, FROZEN, command as backend_command
STATE = ROOT / '.work/mcp'
RECORD = STATE / 'bench.json'


def stamp(pid):
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def alive(record):
    return bool(record.get('started') and stamp(record['pid']) == record['started'])


def save(data):
    temporary = RECORD.with_suffix('.tmp')
    temporary.write_text(json.dumps({'server': data['server']}, indent=2) + '\n')
    temporary.replace(RECORD)


def start_process(argv, log):
    with (STATE / log).open('w') as output:
        process = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=output, start_new_session=True)
    return {'pid': process.pid, 'started': stamp(process.pid)}


def request_local(path, body, timeout=65):
    token = (STATE / ('local-owner-token' if path.startswith('/local/') else 'token')).read_text().strip()
    request = urllib.request.Request('http://127.0.0.1:8765' + path,
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as failure:
        try:
            message = json.load(failure).get('error')
        except ValueError:
            message = None
        raise RuntimeError(message if isinstance(message, str) else 'Update HFPilot to finish this operation') from None


def rpc(name, arguments=None):
    result = request_local('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                  'params': {'name': name, 'arguments': arguments or {}}}, timeout=3)['result']
    if result.get('isError'):
        raise ValueError(result['content'][0]['text'])
    return result['structuredContent']


def runtime_status():
    return rpc('get_status')


def control(command):
    return request_local('/local/control', {'command': command})


def wait_ready(data):
    # HCI initialization and the initial HFP connection can take up to 50 seconds.
    deadline = time.monotonic() + 65
    while time.monotonic() < deadline and alive(data['server']):
        try:
            status = runtime_status()
        except (OSError, ValueError, KeyError):
            time.sleep(.2)
            continue
        if status['listening']['status'] == 'listening' and not status['hardware_error']:
            print('Assistant ready; incoming answering is ON until you stop the application.')
            return
        if status['listening']['status'] == 'paused':
            return
        if status['listening']['status'] in ('starting', 'waiting') and not status['hardware_error']:
            print('Assistant enabled; waiting for the phone.')
            return
        raise RuntimeError('Assistant is not listening; inspect .work/mcp/server.log')
    raise RuntimeError('Assistant did not start; inspect .work/mcp/server.log')


def snapshot(data):
    status = {'server': alive(data.get('server', {}))}
    if status['server']:
        try:
            status['assistant'] = runtime_status()
        except (OSError, ValueError, KeyError):
            status['assistant'] = {'status': 'starting_or_unavailable'}
    return status


def start_server(data, *, paused=False):
    command = backend_command('server', root=ROOT) + ['--manage-bluetooth']
    # The server loads its confirmed public address from its own settings.
    if paused:
        command.append('--paused')
    data['server'] = start_process(command, 'server.log')
    save(data)
    try:
        wait_ready(data)
    except BaseException:
        stop({'server': data['server']})
        raise


def enable(data):
    # A menu-bar launch adopts the current instance, including any active call.
    if alive(data.get('server', {})):
        if not runtime_status().get('enabled', True):
            control('resume')
        wait_ready(data)
        return
    start_server(data)


def desktop_request(data, payload):
    from voice_agent import desktop
    action = payload.get('action')
    if action == 'overview':
        return desktop.overview()
    if action == 'test_call':
        if payload.get('command') == 'start' and not alive(data.get('server', {})):
            start_server(data, paused=True)
        return request_local('/local/test-call', payload, timeout=5)
    if action == 'save_recording':
        return desktop.save_recording(payload.get('enabled'))
    if action == 'save_voice':
        return desktop.save_voice(payload.get('voice'), payload.get('revision'))
    if action == 'voice_preview':
        from voice_agent.voice_preview import run
        if alive(data.get('server', {})) and runtime_status()['active_call_id']:
            raise ValueError('Finish the current call before previewing voices')
        return run(payload.get('voice'), payload.get('language'))
    if action == 'connection_address':
        from voice_agent.mcp_connections import save_origin
        command, url = payload.get('command'), payload.get('url')
        if (STATE / 'local-owner-token').exists():
            try:
                return request_local('/local/connections', {'command': command, 'url': url}, timeout=4)
            except urllib.error.URLError as error:
                if getattr(error.reason, 'errno', None) != errno.ECONNREFUSED:
                    raise
        if alive(data.get('server', {})):
            raise ValueError('The local server is updating. Try again when it is ready.')
        if command != 'save':
            raise ValueError('The local server stopped. Retry from your assistant.')
        return {'public_url': save_origin(STATE, url) + '/mcp', 'suggested_url': None}
    if action == 'call_details':
        return desktop.call_details(payload.get('id'))
    if action == 'save_key':
        if alive(data.get('server', {})) and runtime_status()['active_call_id']:
            raise ValueError('Finish the current call before changing the API key')
        return desktop.save_key(payload.get('key'))
    if action == 'save_context':
        arguments = {'text': payload.get('text'), 'expected_revision': payload.get('revision')}
        if alive(data.get('server', {})):
            return rpc('set_incoming_context', arguments)
        return desktop.save_context(arguments['text'], arguments['expected_revision'])
    raise ValueError('Unknown desktop operation')


def desktop_response(data, payload):
    if not FROZEN:
        sys.path.insert(0, str(ROOT))
    try:
        print(json.dumps(desktop_request(data, payload)))
    except Exception as failure:
        print(json.dumps({'error': str(failure)}))
        raise SystemExit(1)


def stop(data):
    # Stop admission/our call and drain summaries before shutting down event egress.
    record = data.get('server', {})
    if not alive(record):
        return
    os.kill(record['pid'], signal.SIGTERM)
    deadline = time.monotonic() + 155
    while alive(record) and time.monotonic() < deadline:
        time.sleep(.2)
    if alive(record):
        raise RuntimeError('Server has not stopped; inspect it before forcing termination')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('start', 'enable', 'disable', 'status', 'stop', 'restart-server', 'pair',
        'local-only', 'reconnect', 'end-call', 'desktop'))
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == 'status':
        data = json.loads(RECORD.read_text()) if RECORD.exists() else {}
        print(json.dumps(snapshot(data), indent=2))
        return
    payload = None
    if args.command == 'desktop':
        try:
            payload = json.loads(sys.stdin.read(262144))
            if not isinstance(payload, dict):
                raise ValueError()
        except ValueError:
            print(json.dumps({'error': 'Invalid desktop request'}))
            raise SystemExit(1)
        # A finite preview owns only its API connection. Do not hold the phone's
        # control lock while generating: calls, shutdown and saves remain usable.
        if payload.get('action') == 'voice_preview':
            data = json.loads(RECORD.read_text()) if RECORD.exists() else {}
            desktop_response(data, payload)
            return
        # Read-only windows stay usable while a control command holds the lock.
        if payload.get('action') in ('overview', 'call_details') or (
                payload.get('action') == 'test_call' and payload.get('command') == 'status'):
            desktop_response({}, payload)
            return
    # Serialize local UI/CLI actions; a second click/process cannot spawn duplicates.
    with (STATE / 'control.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another local start/stop operation is in progress') from None
        data = json.loads(RECORD.read_text()) if RECORD.exists() else {}
        if args.command == 'desktop':
            desktop_response(data, payload)
            return
        if args.command == 'pair':
            import asyncio
            if not FROZEN:
                sys.path.insert(0, str(ROOT))
            from voice_agent.pairing import run_setup
            asyncio.run(run_setup(server_running=alive(data.get('server', {}))))
        elif args.command == 'stop':
            stop(data)
        elif args.command == 'local-only':
            if not alive(data.get('server', {})):
                start_server(data, paused=True)
        elif args.command == 'disable':
            if alive(data.get('server', {})):
                control('pause')
        elif args.command in ('reconnect', 'end-call'):
            if args.command == 'reconnect' and not alive(data.get('server', {})):
                enable(data)
            else:
                control(args.command.replace('-', '_'))
        elif args.command == 'restart-server':
            if not alive(data.get('server', {})):
                raise RuntimeError('A running server is required')
            status = runtime_status()
            if status['active_call_id'] or status['pending_summaries']:
                raise RuntimeError('Server is busy; wait for calls and summaries before restarting')
            stop({'server': data['server']})
            start_server(data, paused=not status.get('enabled', True))
            print('Local MCP server restarted; assistant state preserved.')
        else:
            enable(data)


if __name__ == '__main__':
    main()
