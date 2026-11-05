"""OpenAI Live WebRTC and managed Responses delegation."""
import asyncio
import contextlib
import json
import os

import aiohttp
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from .journal import dump
from .ending import EndCall
from .caller import caller_context
from .config import ROOT, response_settings


def api_key():
    value = os.getenv('OPENAI_API_KEY', '').strip()
    path = ROOT / '.env'
    if not value and path.exists():
        if path.stat().st_mode & 0o077:
            raise RuntimeError('Protect .env with chmod 600 before using it')
        # A tiny literal parser, never source/execute a credentials file.
        for line in path.read_text().splitlines():
            name, sep, data = line.strip().partition('=')
            if sep and name == 'OPENAI_API_KEY':
                value = data.strip().strip('\"\'')
                break
    if not value:
        raise RuntimeError('Set OPENAI_API_KEY in the environment or the project .env (mode 600)')
    return value


class OpenAI:
    def __init__(self, key):
        self.key = key
        self.http = None

    async def __aenter__(self):
        self.http = aiohttp.ClientSession(
            headers={'Authorization': 'Bearer ' + self.key},
            timeout=aiohttp.ClientTimeout(total=90, connect=15))
        return self

    async def __aexit__(self, *_):
        await self.http.close()

    async def request(self, method, path, body=None):
        async with self.http.request(method, 'https://api.openai.com/v1/' + path,
                                     json=body) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                error = data.get('error', {})
                message = str(error.get('message', 'API request failed')).replace(self.key, '[redacted]')
                raise RuntimeError(f'OpenAI HTTP {response.status}: {message}')
            return data


class LiveSession:
    def __init__(self, api, bridge, journal, agent):
        self.api, self.bridge, self.journal = api, bridge, journal
        self.conversation = agent.data['conversation'].copy()
        # OpenAI's public ICE candidates are sufficient; avoid unrelated STUN services.
        self.peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.peer.addTrack(bridge.track)
        self.channel = self.peer.createDataChannel('oai-events')
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.failure = None
        self.session_id = None
        self.final_event = None
        self.latest_usage = None
        self.delegations = []
        self.responses = []
        self.closing = False
        self.response_ids = {}
        self.ending = EndCall(journal, self.tool_result, self.begin_ending, **agent.data['ending'])
        self.caller_revision = 0
        self.backend_instructions = None

        @self.channel.on('message')
        def message(data):
            try:
                self.handle(json.loads(data))
            except Exception as e:
                self.failure = str(e)
                self.journal.event('event-handler-error', message=self.failure)

        @self.channel.on('close')
        def channel_closed():
            if not self.closed.is_set() and not self.closing:
                self.failure = 'Live event channel closed unexpectedly'

        @self.peer.on('track')
        def track_received(track):
            if track.kind == 'audio':
                bridge.spawn(bridge.receive_audio(track))

        @self.peer.on('connectionstatechange')
        def connection_changed():
            self.journal.event('webrtc-state', state=self.peer.connectionState)
            if self.peer.connectionState == 'failed':
                self.failure = 'WebRTC connection failed'

    def handle(self, event):
        self.journal.api(event)
        kind = event.get('type')
        if kind == 'session.started':
            self.session_id = event.get('session', {}).get('id', self.session_id)
            self.started.set()
        elif kind == 'session.closed':
            self.final_event = event
            self.closed.set()
        elif kind == 'session.usage.updated':
            self.latest_usage = event.get('usage')
        elif kind == 'session.delegation.created':
            self.delegations.append(event)
            ident = event['delegation']['id']
            self.ending.delegations.add(ident)
        elif kind == 'error':
            self.failure = json.dumps(event.get('error', {}), ensure_ascii=False)
        elif kind == 'response.event':
            inner = event.get('event', {})
            delegation_id = event.get('delegation_id')
            if inner.get('type') == 'response.created':
                self.response_ids[delegation_id] = inner.get('response', {}).get('id')
            elif inner.get('type') == 'response.output_item.done':
                item = inner.get('item', {})
                if item.get('type') == 'function_call':
                    self.ending.accept(item, delegation_id, self.response_ids.get(delegation_id))
            if inner.get('type') in ('response.completed', 'response.failed', 'response.incomplete'):
                self.responses.append(inner)
                if inner.get('type') != 'response.completed':
                    self.failure = 'Delegated reasoning failed: ' + inner.get('type', '')

    def tool_result(self, call_id, result, *, continue_response=True):
        self.send({'type': 'response.item.create', 'event_id': 'result_' + call_id,
                   'item': {'type': 'function_call_output', 'call_id': call_id,
                            'output': json.dumps(result, ensure_ascii=False)}})
        if continue_response:
            self.send({'type': 'response.create', 'event_id': 'continue_' + call_id})

    def begin_ending(self, request):
        if request['farewell'] is not None:
            self.send({'type': 'session.commentary.append',
                'event_id': 'farewell_' + request['call_id'], 'delegation_id': None,
                'content': request['farewell']})

    def send(self, item):
        if self.channel.readyState != 'open':
            raise RuntimeError('Live event channel is not open')
        self.channel.send(json.dumps(item))
        self.journal.event('command', payload=item)

    async def connect(self, config):
        self.backend_instructions = config['delegation']['responses']['instructions']
        dump(self.journal.directory / 'session-config.json', config)
        await self.peer.setLocalDescription(await self.peer.createOffer())
        result = await self.api.request('POST', 'live/sessions', {
            'session': config,
            'transport': {'type': 'webrtc', 'sdp': self.peer.localDescription.sdp},
        })
        self.session_id = result['session']['id']
        self.journal.event('session-created', session_id=self.session_id)
        await self.peer.setRemoteDescription(RTCSessionDescription(
            sdp=result['transport']['sdp'], type='answer'))
        deadline = asyncio.get_running_loop().time() + 15
        while not self.started.is_set():
            if self.failure:
                raise RuntimeError(self.failure)
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError('Live session did not start within 15 seconds')
            await asyncio.sleep(.05)
        # Responses settings validate asynchronously; keep handling later errors.
        await asyncio.sleep(.3)
        if self.failure:
            raise RuntimeError(self.failure)

    def greet(self, *, outgoing=False):
        self.ending.activate()
        self.send({'type': 'session.commentary.append', 'event_id': 'greeting_1',
                   'delegation_id': None, 'content': self.conversation[
                       'outgoing_greeting' if outgoing else 'incoming_greeting']})

    def update_caller(self, caller):
        self.caller_revision += 1
        context = caller_context(caller, self.caller_revision)
        self.send({'type': 'session.thinking.append',
                   'event_id': f'caller_context_{self.caller_revision}',
                   'delegation_id': None, 'content': context})
        self.send({'type': 'session.update',
                   'event_id': f'caller_backend_{self.caller_revision}',
                   'session': {'delegation': {'type': 'responses', 'responses': {
                       'instructions': self.backend_instructions + '\n\n' + context}}}})

    async def close(self):
        self.closing = True
        self.ending.active = False
        try:
            if self.started.is_set() and not self.closed.is_set() and self.channel.readyState == 'open':
                with contextlib.suppress(Exception):
                    if self.ending.result is not None:
                        result = self.ending.result
                        status = ('hangup_confirmed' if result['status'] == 'hangup_confirmed'
                                  else 'session_closed')
                        self.tool_result(result['call_id'], {'status': status}, continue_response=False)
                    self.send({'type': 'session.close', 'event_id': 'close_1'})
                    await asyncio.wait_for(self.closed.wait(), 15)
        finally:
            await self.peer.close()
        return {'session_id': self.session_id, 'finalized': self.closed.is_set(),
                'final_event': self.final_event, 'latest_usage': self.latest_usage,
                'delegations': len(self.delegations), 'responses': self.responses,
                'end_call': self.ending.result,
                'end_call_attempts': [self.ending.result] if self.ending.result else [],
                'failure': self.failure}


SUMMARY_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'status': {'type': 'string', 'enum': ['completed', 'partially_completed',
                                             'not_completed', 'incomplete']},
        # Keep the existing output contract for saved calls and MCP clients.
        'summary_ru': {'type': 'string', 'description': 'Concise overall report in the language selected by '
            'the report instructions. This legacy field name does not require Russian.'},
        'task_result': {'type': 'string'},
        'answers': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'properties': {'question': {'type': 'string'}, 'answer': {'type': ['string', 'null']}},
            'required': ['question', 'answer']}},
        'owner_next_steps': {'type': 'array', 'items': {'type': 'string'}},
        'uncertainties': {'type': 'array', 'items': {'type': 'string'}},
        'evidence': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'properties': {'start_ms': {'type': 'number'}, 'speaker': {'type': 'string'},
                           'quote': {'type': 'string'}},
            'required': ['start_ms', 'speaker', 'quote']}},
    },
}
SUMMARY_SCHEMA['required'] = list(SUMMARY_SCHEMA['properties'])

async def summarize(api, journal, metadata, agent):
    transcript = journal.render()
    settings = agent.data['summary']
    if not journal.fragments:
        message = ('The call did not connect; no conversation content is available.'
                   if metadata.get('direction') == 'outgoing' and not metadata.get('connected_at')
                   else 'No transcript is available; conversation content could not be established.')
        dump(journal.directory / 'summary.json', {'status': 'incomplete',
            'summary_ru': message, 'task_result': message, 'answers': [],
            'owner_next_steps': [], 'uncertainties': [message], 'evidence': []})
        (journal.directory / 'summary.txt').write_text(message + '\nStatus: ' + metadata.get('outcome', 'unknown') + '\n')
        return
    # The report sees call facts, not a nested copy of the live model's prompts.
    fields = ('direction', 'started_at', 'connected_at', 'finished_at', 'outcome',
              'test_call', 'caller', 'dialed_number', 'error', 'cleanup_ok')
    data = {'call_metadata': {k: metadata[k] for k in fields if k in metadata},
            'assignment': (journal.directory / 'assignment.txt').read_text(), 'transcript': transcript}
    request = {
        **response_settings(settings), 'store': False,
        'instructions': settings['instructions'],
        'input': json.dumps(data, ensure_ascii=False),
        'text': {'format': {'type': 'json_schema', 'name': 'call_summary',
                            'strict': True, 'schema': SUMMARY_SCHEMA}},
    }
    dump(journal.directory / 'summary-request.json', request)
    response = await api.request('POST', 'responses', request)
    dump(journal.directory / 'summary-response.json', response)
    if response.get('status') != 'completed':
        raise RuntimeError('Post-call summary did not complete')
    text = ''.join(part.get('text', '') for item in response.get('output', [])
                   if item.get('type') == 'message' for part in item.get('content', [])
                   if part.get('type') == 'output_text')
    result = json.loads(text)
    dump(journal.directory / 'summary.json', result)
    (journal.directory / 'summary.txt').write_text(
        result['summary_ru'] + '\n\nNext steps:\n' +
        '\n'.join('- ' + step for step in result['owner_next_steps']) + '\n')
