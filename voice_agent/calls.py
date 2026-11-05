"""One call owns the Bluetooth audio; finalization and summaries run separately."""
import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import time
import uuid

from .journal import Journal, dump
from .live import LiveSession, summarize
from .config import AgentConfig, configuration, load_call_settings
from .caller import unknown_caller
from .recording import enabled as recording_enabled


def emit(event, **data):
    print(json.dumps({'event': event, **data}, ensure_ascii=False), flush=True)


@dataclass
class CompletedCall:
    journal: Journal
    live: LiveSession
    metadata: dict
    exit_code: int
    agent: AgentConfig


def current(listener, ticket):
    return listener is None or listener.current(ticket)


class OutgoingNotConnected(Exception):
    def __init__(self, outcome):
        self.outcome = outcome
        super().__init__(outcome)


async def wait_outgoing(phone, call, seconds, stop, live, journal):
    deadline = time.monotonic() + seconds
    previous = None
    while True:
        calls = phone.read()[1]
        state = calls.get(call)
        if stop.is_set():
            raise OutgoingNotConnected('stopped')
        if live.failure or live.closed.is_set():
            raise RuntimeError(live.failure or 'Live session ended before outgoing answer')
        if calls and (not phone.owns_call(call) or calls != {call: state} or
                      state not in ('dialing', 'alerting', 'active')):
            raise RuntimeError('Another call appeared during dial; leaving it untouched')
        if state is None:
            # HFP cannot reliably distinguish busy, declined and early remote hangup.
            raise OutgoingNotConnected('not_connected')
        observed = 'dial_requested' if phone.state.get('dial_pending') else state
        if observed != previous:
            journal.event('outgoing-state', call=call, state=observed)
            emit('outgoing-state', call=call, state=observed)
            previous = observed
        if state == 'active' and phone.state.get('setup') == 0:
            return
        if time.monotonic() >= deadline:
            raise OutgoingNotConnected('dial_timeout' if phone.state.get('dial_pending') else 'no_answer')
        await asyncio.sleep(.1)


async def handle_call(args, api, phone, gateway, call, stop, expired, listener=None, ticket=None):
    outgoing = args.command == 'dial'
    agent, assignment, assignment_path = load_call_settings(args.config, args.assignment)
    record_audio = recording_enabled(getattr(args, 'recording_preference_path', None), args.record_audio)
    now = datetime.now(agent.timezone)
    directory = args.output / (now.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    journal = Journal(directory)
    (directory / 'assignment.txt').write_text(assignment)
    dump(directory / 'agent-config.json', agent.data)
    metadata = {'started_at': now.isoformat(), 'address': args.address,
                'gateway': gateway, 'call_path': call, 'backend': phone.name,
                'mode': 'one-outgoing-call' if outgoing else (
                    'continuous' if args.command == 'serve' else 'one-incoming-call'),
                'direction': 'outgoing' if outgoing else 'incoming',
                'voice_model': agent.data['voice']['model'],
                'reasoning_model': agent.data['delegation']['model'],
                'summary_model': agent.data['summary']['model'],
                'config_path': str(agent.path), 'assignment_path': str(assignment_path),
                'outcome': 'starting', 'test_call': args.test_call,
                'summary_status': 'pending', 'caller': unknown_caller(), 'record_audio': record_audio}
    if getattr(args, 'call_id', None):
        metadata.update(mcp_call_id=args.call_id, context_revision=args.context_revision)
    if outgoing:
        metadata.update(dialed_number=args.number, dial_timeout_seconds=args.dial_timeout,
                        dial_method='hfp_atd', calling_app=None)
    dump(directory / 'metadata.json', metadata)
    emit('call-starting', directory=str(directory), call=call)
    bridge = phone.audio(journal, args.input_gain, args.output_gain, record_audio)
    live = LiveSession(api, bridge, journal, agent)
    answered, ended, restore_links = False, False, False
    exit_code = 1
    def refresh_caller():
        if outgoing:
            return
        if not current(listener, ticket) or phone.read()[1].get(call) not in ('incoming', 'active'):
            return
        caller = phone.caller(call)
        if caller is not None and caller != metadata['caller']:
            metadata['caller'] = caller
            journal.event('caller-updated', caller=caller)
            dump(directory / 'metadata.json', metadata)
            emit('caller-updated', directory=str(directory), call=call, caller=caller)
            live.update_caller(caller)
    try:
        if outgoing:
            if stop.is_set():
                raise OutgoingNotConnected('stopped')
            config = configuration(agent, assignment, now.isoformat(),
                                   outgoing_number=args.number)
            await asyncio.wait_for(live.connect(config), 25)
            if stop.is_set():
                raise OutgoingNotConnected('stopped')
            # Both Python and C check idle state before the single ATD request.
            answered = True  # Lost command ACK still requires cancellation of our generation.
            journal.event('dial-requested', call=call, number=args.number)
            emit('dial-requested', directory=str(directory), call=call, number=args.number)
            await phone.dial(args.number, call)
            await wait_outgoing(phone, call, args.dial_timeout, stop, live, journal)
        else:
            caller = await phone.prepare_caller(call)
            metadata.update(caller=caller, caller_at_start=caller.copy())
            journal.event('caller-initial', caller=caller)
            dump(directory / 'metadata.json', metadata)
            emit('caller-initial', directory=str(directory), call=call, caller=caller)
            config = configuration(agent, assignment, now.isoformat(), caller)
            if stop.is_set() or not current(listener, ticket) or phone.read()[1] != {call: 'incoming'}:
                raise RuntimeError('Incoming call changed while collecting caller ID; leaving it untouched')
            await asyncio.wait_for(live.connect(config), 25)
            calls = phone.read()[1]
            if stop.is_set() or not current(listener, ticket) or calls != {call: 'incoming'}:
                raise RuntimeError('Incoming call changed before answer; leaving it untouched')
            refresh_caller()
            answered = True  # An uncertain Answer result also needs cleanup.
            await phone.answer(call)
            journal.event('answer-sent', call=call)
            deadline = time.monotonic() + 10
            while phone.read()[1].get(call) != 'active':
                if time.monotonic() > deadline or stop.is_set() or not current(listener, ticket):
                    raise RuntimeError('Call did not become active')
                await asyncio.sleep(.1)
        metadata['connected_at'] = datetime.now(agent.timezone).isoformat()
        await bridge.start()
        refresh_caller()
        if outgoing:
            live.greet(outgoing=True)
        else:
            live.greet()
        emit('conversation-ready', directory=str(directory))
        journal.event('conversation-ready')
        deadline = time.monotonic() + args.max_call
        while True:
            calls = phone.read()[1]
            state = calls.get(call)
            if state in (None, 'disconnected') or not current(listener, ticket):
                ended = True
                metadata['outcome'] = 'remote_hung_up' if outgoing else 'caller_hung_up'
                exit_code = 0
                break
            if stop.is_set():
                metadata['outcome'] = 'window_expired' if expired.is_set() else 'stopped'
                exit_code = 0
                break
            if time.monotonic() >= deadline:
                metadata['outcome'] = 'time_limit'
                break
            if state != 'active' or any(p != call and s != 'disconnected' for p, s in calls.items()):
                raise RuntimeError('Call held or another call appeared; ending only our call')
            if outgoing and not phone.owns_call(call):
                raise RuntimeError('Outgoing call ownership changed')
            refresh_caller()
            if live.failure or bridge.failure or live.closed.is_set():
                raise RuntimeError(live.failure or bridge.failure or 'Live session ended')
            if live.ending.due():
                metadata['outcome'] = 'assistant_hung_up'
                exit_code = 0
                break
            await asyncio.sleep(.2)
    except OutgoingNotConnected as error:
        metadata['outcome'] = error.outcome
        journal.event('outgoing-not-connected', outcome=error.outcome)
        emit('outgoing-not-connected', directory=str(directory), outcome=error.outcome)
        exit_code = 0 if error.outcome == 'stopped' else 1
    except Exception as error:
        metadata.update(outcome='failed', error=str(error).replace(api.key, '[redacted]'))
        journal.event('call-error', message=metadata['error'])
        emit('call-error', message=metadata['error'])
    finally:
        bridge.ready = False
        if answered and not ended and current(listener, ticket):
            try:
                # Never hang up a new incoming call reusing the previous HFP path.
                state = phone.read()[1].get(call)
                if state in ('active', 'held', 'dialing', 'alerting') and (
                        not outgoing or phone.owns_call(call)):
                    restore_links = True
                    await phone.hangup(call)
                    restore_links = False  # The phone ports are now tearing down.
                    journal.event('hangup-confirmed', call=call)
                    if metadata['outcome'] == 'assistant_hung_up':
                        live.ending.result['status'] = 'hangup_confirmed'
            except Exception as error:
                message = str(error).replace(api.key, '[redacted]')
                journal.event('hangup-error', message=message)
                metadata.update(outcome='failed', error='Hangup failed: ' + message)
                exit_code = 1
        try:
            metadata['audio'] = await bridge.close(restore_links=restore_links)
        except Exception as error:
            metadata['audio'] = {'failures': [str(error).replace(api.key, '[redacted]')]}
            exit_code = 1
        metadata['finished_at'] = datetime.now(agent.timezone).isoformat()
        # Durable evidence is saved before handing this call to the background task.
        journal.render()
        journal.sync()
        dump(directory / 'metadata.json', metadata)
    return CompletedCall(journal, live, metadata, exit_code, agent)


async def finalize_call(api, completed, summary_slots):
    journal, live, metadata = completed.journal, completed.live, completed.metadata
    directory = journal.directory
    journal_closed = False
    try:
        try:
            metadata['live'] = await asyncio.wait_for(live.close(), 25)
        except Exception as error:
            metadata['live'] = {'session_id': live.session_id, 'finalized': False,
                                'failure': str(error).replace(api.key, '[redacted]')}
        metadata['cleanup_ok'] = (not metadata['audio']['failures'] and metadata['live']['finalized'])
        journal.render()
        dump(directory / 'metadata.json', metadata)
        journal.close()
        journal_closed = True
        # API finalization must not queue behind slow summaries.
        async with summary_slots:
            metadata['summary_status'] = 'running'
            dump(directory / 'metadata.json', metadata)
            await asyncio.wait_for(summarize(api, journal, metadata, completed.agent), 95)
        metadata['summary_status'] = 'saved'
    except asyncio.CancelledError:
        metadata['summary_status'] = 'interrupted'
        metadata['summary_error'] = 'Shutdown deadline reached; transcript preserved'
        raise
    except Exception as error:
        metadata['summary_status'] = 'failed'
        metadata['summary_error'] = str(error).replace(api.key, '[redacted]')
        emit('summary-error', directory=str(directory), message=metadata['summary_error'])
    finally:
        try:
            if not journal_closed:
                journal.render()
                journal.close()
            if metadata['summary_status'] in ('failed', 'interrupted'):
                dump(directory / 'summary-error.json', {
                    'error': metadata['summary_error'], 'transcript_preserved': True})
            dump(directory / 'metadata.json', metadata)
        except Exception as error:
            emit('persistence-error', directory=str(directory),
                 message=str(error).replace(api.key, '[redacted]'))
            completed.exit_code = 1
    emit('finished', directory=str(directory), outcome=metadata['outcome'],
         summary_status=metadata['summary_status'],
         delegations=metadata.get('live', {}).get('delegations', 0))
    return int(bool(completed.exit_code or not metadata.get('cleanup_ok')
                    or metadata['summary_status'] != 'saved'))


class Finalizers:
    def __init__(self, api):
        self.api = api
        self.slots = asyncio.Semaphore(2)
        self.tasks = set()
        self.failures = 0

    def submit(self, completed):
        task = asyncio.create_task(finalize_call(self.api, completed, self.slots))
        self.tasks.add(task)
        def done(t):
            self.tasks.discard(t)
            if t.cancelled():
                self.failures += 1
            elif t.exception():
                self.failures += 1
                emit('finalization-error', message=str(t.exception()).replace(self.api.key, '[redacted]'))
            elif t.result():
                self.failures += 1
        task.add_done_callback(done)
        return task

    async def drain(self):
        if self.tasks:
            emit('summaries-draining', pending=len(self.tasks))
            _, pending = await asyncio.wait(self.tasks, timeout=120)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)  # Deliver completion callbacks before returning the count.
        return self.failures
