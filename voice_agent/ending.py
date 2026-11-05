"""Execute one model-chosen hangup, with a fixed allowance for a short farewell."""
import json
import time

END_CALL_TOOL = {
    'type': 'function', 'name': 'end_call', 'strict': True,
    'description': ('End the current phone call. Supply a short final reply to be spoken once '
                    'before a fixed hangup delay, or null to disconnect without another reply. '
                    'This commits the hangup; it is not automatically cancelled by new speech.'),
    'parameters': {'type': 'object', 'additionalProperties': False,
        'properties': {
            'reason': {'type': 'string', 'description': 'Why the conversation should end now.'},
            'farewell': {'type': ['string', 'null'], 'description':
                'One very short final reply in the conversation language, including any needed '
                'confirmation and goodbye. Null if already said goodbye or asked to disconnect now.'}},
        'required': ['reason', 'farewell']},
}


class EndCall:
    def __init__(self, journal, reply, begin, *, delay_seconds=5):
        self.journal, self.reply, self.begin = journal, reply, begin
        self.delay_seconds = delay_seconds
        self.active = False
        self.seen, self.delegations = set(), set()
        self.result = self.deadline = None

    def activate(self):
        self.active = True  # Only after the selected call connects.

    def accept(self, item, delegation_id=None, response_id=None, now=None):
        ident = item.get('call_id')
        if not isinstance(ident, str) or not ident or ident in self.seen:
            return
        self.seen.add(ident)
        try:
            if not self.active or delegation_id not in self.delegations:
                raise ValueError('Call is not connected or delegation is unknown')
            args = json.loads(item.get('arguments', ''))
            if (item.get('name') != 'end_call' or not isinstance(args, dict)
                    or set(args) != {'reason', 'farewell'}
                    or not isinstance(args['reason'], str) or not 1 <= len(args['reason'].strip()) <= 1000
                    or (args['farewell'] is not None and (
                        not isinstance(args['farewell'], str) or not 1 <= len(args['farewell'].strip()) <= 240))):
                raise ValueError('end_call requires a reason and a short farewell or null')
        except (ValueError, TypeError) as error:
            self.reply(ident, {'status': 'rejected', 'error': str(error)})
            return
        if self.result is not None:
            self.reply(ident, {'status': 'already_ending'})
            return
        delay = self.delay_seconds if args['farewell'] is not None else 0
        self.result = {'call_id': ident, 'delegation_id': delegation_id, 'response_id': response_id,
                       **args, 'status': 'scheduled', 'delay_seconds': delay, 'timer_start': 'tool_accepted'}
        self.deadline = (time.monotonic() if now is None else now) + delay
        self.journal.event('end-call-requested', **self.result)
        self.begin(self.result)

    def due(self, now=None):
        if not self.active or self.deadline is None:
            return False
        if (time.monotonic() if now is None else now) < self.deadline:
            return False
        if self.result['status'] == 'scheduled':
            self.result.update(status='ready', completion_condition=(
                'timer_elapsed' if self.result['farewell'] is not None else 'no_farewell'))
            self.journal.event('end-call-ready', **self.result)
        return True
