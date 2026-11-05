"""Application-owned Bluetooth and continuous answering; MCP submits call jobs."""
import asyncio
import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace

from .calls import handle_call, finalize_call
from .config import load_call_settings
from .listener import IncomingCalls
from .live import OpenAI, api_key
from .phone import create_phone
from .skywalk import dial_number
from .mcp_state import utcnow
from . import phone_profile, recording
from .paths import native_helper

RECONNECT_DELAY = 5


class Runtime:
    def __init__(self, state, args):
        self.state, self.args = state, args
        self.args.recording_preference_path = state.root / 'record-audio'
        self.phone = self.phone_file = None
        self.restore_bluetooth = False
        self.connection_lock = asyncio.Lock()
        self.summary_slots = asyncio.Semaphore(2)
        self.active = None
        self.tasks = set()
        self.stops = {}
        self.released = {}
        self.listen_stop = asyncio.Event()
        self.listen_task = None
        self.listening = {'status': 'stopped'}
        self.closing = False
        self.hardware_error = None
        self.cleanup_failed = False
        self.connection_error = None
        self.maintainer = self.pairing_lease = None
        self.pairing = False
        self.enabled = not getattr(args, 'start_paused', False)

    def status(self):
        return {'active_call_id': self.active, 'listening': dict(self.listening),
                'enabled': self.enabled,
                'bluetooth_connected': self.phone is not None and self.listening['status'] in ('listening', 'pausing'),
                'phone_name': getattr(self.args, 'phone_name', 'iPhone'),
                'connection_error': self.connection_error,
                'hardware_error': self.hardware_error,
                'cleanup_failed': self.cleanup_failed,
                'recording': recording.status(self.args.recording_preference_path, self.args.record_audio),
                'pending_summaries': sum(j['status'] == 'summarizing' for j in self.state.data['jobs'].values())}

    async def start_service(self):
        if not self.enabled:
            self.listening = {'status': 'paused'}
            return
        # Fail configuration errors before starting; a missing phone is recoverable.
        load_call_settings(self.args.config, self.state.context_path)
        api_key()
        if not self.maintainer or self.maintainer.done():
            self.listening = {'status': 'listening' if self.listen_task and not self.listen_task.done() else 'starting'}
            self.maintainer = asyncio.create_task(self.maintain_connection())

    async def pause(self):
        if self.pairing or self.closing:
            raise ValueError('Wait for phone setup or shutdown to finish')
        self.enabled = False  # Stop admission before awaiting anything.
        self.listening = {'status': 'pausing'}
        if self.maintainer:
            self.maintainer.cancel()
            await asyncio.gather(self.maintainer, return_exceptions=True)
        if self.active:
            return self.status()
        self.listen_stop.set()
        if self.listen_task:
            await asyncio.gather(self.listen_task, return_exceptions=True)
        await self.release_phone()
        self.listening = {'status': 'failed' if self.cleanup_failed else 'paused'}
        return self.status()

    async def resume(self):
        if self.pairing or self.closing:
            raise ValueError('Wait for phone setup or shutdown to finish')
        if self.cleanup_failed:
            raise ValueError('Bluetooth could not be released. Restart the assistant after checking the phone.')
        load_call_settings(self.args.config, self.state.context_path)
        api_key()
        if not self.enabled and self.listen_task and not self.listen_task.done() and not self.active:
            await asyncio.gather(self.listen_task, return_exceptions=True)
        self.enabled = True
        await self.start_service()
        return self.status()

    async def reconnect(self):
        if self.active:
            raise ValueError('Finish the current call before reconnecting the phone')
        await self.pause()
        return await self.resume()

    def end_active_call(self):
        if self.active:
            self.cancel_call(self.active)
        return self.status()

    async def maintain_connection(self):
        while self.enabled and not self.closing and not self.pairing:
            try:
                if getattr(self.args, 'use_saved_phone', False):
                    selected = phone_profile.load()
                    if not selected:
                        raise RuntimeError('Connect a phone in HFPilot Settings before turning on the assistant.')
                    self.args.address = selected['address']
                    self.args.bond_file = Path(selected['bond_file'])
                    self.args.phone_name = selected['name']
                if not self.cleanup_failed:
                    self.hardware_error = None
                await self.start()
                self.connection_error = None
                await asyncio.shield(self.listen_task)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.connection_error = str(error)
            if self.closing or self.pairing or not self.enabled:
                return
            if self.cleanup_failed:
                self.listening = {'status': 'failed'}
                return
            if self.hardware_error:
                self.connection_error = self.hardware_error
                self.hardware_error = None
            self.listening = {'status': 'waiting'}
            await asyncio.sleep(RECONNECT_DELAY)

    async def pause_for_pairing(self):
        if self.closing or self.pairing:
            raise ValueError('Phone setup is already running or the application is stopping')
        if self.active or self.status()['pending_summaries']:
            raise ValueError('Wait for the current call and its summary to finish')
        self.pairing = True  # Admission closes before the first await.
        self.listen_stop.set()
        if self.maintainer:
            self.maintainer.cancel()
            await asyncio.gather(self.maintainer, return_exceptions=True)
        if self.listen_task:
            await asyncio.gather(self.listen_task, return_exceptions=True)
        await self.release_phone()
        if self.cleanup_failed:
            self.pairing = False
            raise ValueError('Bluetooth did not shut down cleanly; inspect the application log')
        self.listening = {'status': 'pairing'}
        async def expire():
            await asyncio.sleep(300)
            await self.resume_after_pairing()
        self.pairing_lease = asyncio.create_task(expire())

    async def resume_after_pairing(self):
        if self.pairing_lease and self.pairing_lease is not asyncio.current_task():
            self.pairing_lease.cancel()
        self.pairing_lease = None
        self.pairing = False
        if not self.closing:
            await self.start_service()

    async def bluetooth(self, power=None):
        binary = native_helper('blueutil')
        if not binary:
            raise RuntimeError('Install blueutil or run without --manage-bluetooth and turn native Bluetooth off manually')
        command = [binary, '--power'] + ([] if power is None else [str(power)])
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 10)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise RuntimeError(f'blueutil could not change/read native Bluetooth power (exit {process.returncode}); '
                               'check Bluetooth permission for the launching application in System Settings')
        return output.decode().strip()

    async def ensure_phone(self):
        async with self.connection_lock:
            if not self.args.address:
                raise RuntimeError('No phone selected. Connect a phone in HFPilot Settings.')
            if self.hardware_error:
                raise RuntimeError('Bluetooth cleanup needs inspection; restart the MCP server after checking the phone')
            if self.phone is not None:
                self.phone.read()
                return self.phone
            lockpath = Path(os.getenv('XDG_RUNTIME_DIR', '/tmp')) / (
                'voice-agent-' + self.args.address.replace(':', '') + '.lock')
            lock = lockpath.open('a')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                lock.close()
                raise RuntimeError('The phone is already owned by another HFPilot process') from None
            self.phone_file = lock
            # The application owns the helper until shutdown. Its parent/heartbeat
            # watchdogs still apply, independently of each call's timeout.
            args = self.args
            self.phone = create_phone(args, continuous=True)
            try:
                self.phone.validate()
                if hasattr(self.phone, 'claim_controller'):
                    self.phone.claim_controller()
                if args.backend == 'skywalk' and args.manage_bluetooth:
                    self.restore_bluetooth = await self.bluetooth() == '1'
                    if self.restore_bluetooth:
                        await self.bluetooth(0)
                await self.phone.start()
            except BaseException:
                await self._close_phone()
                raise
            return self.phone

    async def _close_phone(self):
        controller_owner = getattr(self.phone, 'controller_owner', None)
        try:
            if self.phone:
                if controller_owner:
                    await self.phone.close(release_controller=False)
                else:
                    await self.phone.close()
        except Exception:
            self.cleanup_failed = True
            self.hardware_error = 'Bluetooth cleanup failed; inspect actual phone state before another call'
        finally:
            self.phone = None
            try:
                if self.restore_bluetooth:
                    await self.bluetooth(1)
            except Exception:
                self.cleanup_failed = True
                self.hardware_error = 'Native Bluetooth restoration failed; restore it manually'
            finally:
                self.restore_bluetooth = False
                if controller_owner:
                    controller_owner.close()
                if self.phone_file:
                    self.phone_file.close()
                    self.phone_file = None

    async def release_phone(self):
        async with self.connection_lock:
            if self.active is None and (not self.enabled or self.listen_stop.is_set() or self.listening['status'] in ('stopped', 'failed')):
                await self._close_phone()

    def start_call(self, number, assignment, request_id):
        if self.closing or self.pairing:
            raise ValueError('Server is shutting down')
        if not self.enabled:
            raise ValueError('The assistant is paused. Turn it on in HFPilot before placing a call.')
        number = dial_number(number)
        # Resolve retries before the busy check: a lost MCP response must not redial.
        prior = next((j for j in self.state.data['jobs'].values() if j.get('request_id') == request_id), None)
        if prior:
            return self.state.create_job('outgoing', assignment, number=number, request_id=request_id)[0]
        if self.args.backend != 'skywalk':
            raise ValueError('Outgoing calls are currently implemented only by the Mac Skywalk backend')
        if self.active:
            raise ValueError('A call is already in progress; no call was queued or dialed')
        if not self.listen_task or self.listen_task.done() or self.hardware_error:
            raise ValueError('Assistant is not ready; check the locally running application')
        load_call_settings(self.args.config, self.state.context_path)
        job, _ = self.state.create_job('outgoing', assignment, number=number, request_id=request_id)
        self.launch(job)
        return job

    def launch(self, job, listener=None, ticket=None):
        self.active = job['call_id']
        self.released[job['call_id']] = asyncio.Event()
        stop = self.stops[job['call_id']] = asyncio.Event()
        task = asyncio.create_task(self.run_call(job, stop, listener, ticket))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def run_call(self, job, stop, listener=None, ticket=None):
        ident = job['call_id']
        result = None
        key = None
        try:
            key = api_key()
            phone = await self.ensure_phone()
            if stop.is_set():
                raise RuntimeError('Call cancelled before setup')
            args = SimpleNamespace(**vars(self.args))
            args.command = 'dial' if job['direction'] == 'outgoing' else 'run'
            args.number = job['number']
            args.assignment = self.state.root / 'jobs' / ident / 'assignment.txt'
            args.call_id = ident
            args.context_revision = job['context_revision']
            if ticket is None:
                gateway, call = phone.address, phone.outgoing_call()
            else:
                gateway, call = ticket.gateway, ticket.path
            job.update(status='running', started_at=utcnow())
            self.state.publish('call.started', {k: job[k] for k in ('call_id', 'direction', 'number')},
                               'evt_started_' + ident)
            async with OpenAI(key) as api:
                completed = await handle_call(args, api, phone, gateway, call, stop,
                                              asyncio.Event(), listener, ticket)
                job.update(status='summarizing', directory=str(completed.journal.directory))
                self.state.save()
                if completed.metadata.get('audio', {}).get('failures'):
                    self.hardware_error = 'Call audio cleanup failed; inspect Bluetooth before continuing'
                    self.listen_stop.set()
                self.active = None
                if not self.enabled:
                    self.listen_stop.set()
                await self.release_phone()
                self.released[ident].set()
                await finalize_call(api, completed, self.summary_slots)
                metadata = completed.metadata
                summary_path = completed.journal.directory / 'summary.json'
                summary = json.loads(summary_path.read_text()) if summary_path.exists() else None
                # Keep webhook payload bounded; the read tool exposes the complete artifacts.
                if summary is not None and len(json.dumps(summary).encode()) > 65536:
                    summary = {'summary_ru': str(summary.get('summary_ru', ''))[:8000], 'truncated': True}
                result = {k: metadata.get(k) for k in ('outcome', 'summary_status', 'caller', 'cleanup_ok')}
                result.update(summary=summary, error=metadata.get('error') or metadata.get('summary_error'))
        except asyncio.CancelledError:
            result = {'outcome': 'interrupted', 'summary_status': 'unavailable', 'summary': None,
                      'error': 'Call worker interrupted; inspect the phone before retrying'}
            raise
        except Exception as error:
            message = str(error).replace(key, '[redacted]') if key else str(error)
            result = {'outcome': 'failed', 'summary_status': 'unavailable', 'summary': None,
                      'error': message[:2000], 'error_type': type(error).__name__}
        finally:
            if self.active == ident:
                self.active = None
            if not self.enabled:
                self.listen_stop.set()
            self.stops.pop(ident, None)
            await self.release_phone()
            self.released.pop(ident).set()
            if result is not None:
                if self.hardware_error:
                    result.update(cleanup_ok=False, error=self.hardware_error)
                self.state.finish(ident, result)

    async def start(self):
        """Called once by local application startup, never by an MCP tool."""
        if self.closing:
            raise ValueError('Server is shutting down')
        if self.listen_task and not self.listen_task.done():
            return self.status()
        load_call_settings(self.args.config, self.state.context_path)
        api_key()  # Fail before taking the controller if the key is missing.
        self.listen_stop = asyncio.Event()
        self.listening = {'status': 'starting'}
        listener = None
        try:
            phone = await self.ensure_phone()
            def observed(event, **details):
                if event == 'listener-unavailable':
                    self.hardware_error = 'Bluetooth became unavailable; inspect the phone before restarting the server'
                    self.listen_stop.set()
            listener = IncomingCalls(phone.read, observed, self.listen_stop)
            await listener.start()
            if listener.unavailable:
                raise RuntimeError('Bluetooth became unavailable during startup')
        except BaseException:
            self.listening['status'] = 'failed'
            self.listen_stop.set()
            if listener:
                await listener.close()
            await self.release_phone()
            raise
        self.listening['status'] = 'listening'
        self.listen_task = asyncio.create_task(self.listen(listener))
        print(json.dumps({'event': 'incoming-listening', 'control': 'local_process'}), flush=True)
        return self.status()

    async def listen(self, listener):
        try:
            while self.enabled and not self.listen_stop.is_set():
                ticket = await listener.next()
                if ticket is None or self.listen_stop.is_set() or not self.enabled:
                    break
                if self.active:
                    continue  # No takeover or queueing of call waiting.
                context = self.state.context()
                job, _ = self.state.create_job('incoming', context['text'], context_revision=context['revision'])
                self.launch(job, listener, ticket)
                await self.released[job['call_id']].wait()
                if self.hardware_error:
                    break
            self.listening['status'] = 'failed' if self.hardware_error else 'stopped'
        except asyncio.CancelledError:
            self.listening['status'] = 'stopped'
            raise
        except Exception:
            self.listening['status'] = 'failed'
            self.hardware_error = self.hardware_error or 'Incoming listener failed; inspect the local application'
        finally:
            self.listen_stop.set()
            await listener.close()
            await self.release_phone()
            if not self.enabled and not self.closing and not self.cleanup_failed:
                self.listening = {'status': 'paused'}
            print(json.dumps({'event': 'incoming-stopped', 'status': self.listening['status'],
                              'hardware_error': self.hardware_error}), flush=True)

    def cancel_call(self, ident):
        job = self.state.job(ident)
        if ident in self.stops and job['status'] in ('queued', 'running'):
            self.stops[ident].set()
        return job

    async def close(self):
        self.closing = True
        for task in (self.maintainer, self.pairing_lease):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.listen_stop.set()
        if self.listening['status'] not in ('stopped', 'failed'):
            self.listening['status'] = 'stopping'
        for stop in self.stops.values():
            stop.set()
        pending = set(self.tasks)
        if self.listen_task:
            pending.add(self.listen_task)
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=130)
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        async with self.connection_lock:
            await self._close_phone()
