"""Private durable MCP jobs and delivery state; assignments remain prompt files."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

def utcnow():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class State:
    def __init__(self, root, context_path):
        self.root, self.context_path = Path(root), Path(context_path)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / 'state.json'
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            'jobs': {}, 'subscriptions': {}, 'events': {}, 'outbox': {}}

    def save(self):
        atomic_text(self.path, canonical(self.data) + '\n')

    def context(self):
        text = self.context_path.read_text()
        return {'text': text, 'revision': hashlib.sha256(text.encode()).hexdigest()}

    def set_context(self, text, expected_revision):
        if self.context()['revision'] != expected_revision:
            raise ValueError('Context changed; read it again and merge the latest text before updating')
        atomic_text(self.context_path, text)
        return self.context()

    def job(self, ident):
        try:
            return self.data['jobs'][ident]
        except KeyError:
            raise ValueError('Unknown call_id') from None

    def create_job(self, direction, assignment, *, number=None, request_id=None, context_revision=None):
        fingerprint = hashlib.sha256(canonical([number, assignment]).encode()).hexdigest()
        if request_id:
            for job in self.data['jobs'].values():
                if job.get('request_id') == request_id:
                    if job['fingerprint'] != fingerprint:
                        raise ValueError('request_id already belongs to a different call; do not reuse it')
                    return job, False
        ident = uuid.uuid4().hex
        directory = self.root / 'jobs' / ident
        directory.mkdir(parents=True, mode=0o700)
        atomic_text(directory / 'assignment.txt', assignment)
        job = {'call_id': ident, 'direction': direction, 'number': number,
               'request_id': request_id, 'fingerprint': fingerprint,
               'context_revision': context_revision, 'status': 'queued',
               'created_at': utcnow(), 'directory': None, 'result': None}
        self.data['jobs'][ident] = job
        self.save()
        return job, True

    def publish(self, name, data, event_id):
        if event_id in self.data['events']:
            return
        event = {'eventId': event_id, 'name': name, 'timestamp': utcnow(),
                 'data': data, 'cursor': None}
        self.data['events'][event_id] = event
        now = datetime.now(timezone.utc).timestamp()
        for sub in self.data['subscriptions'].values():
            if sub['name'] != name or sub['expires_at'] <= now:
                continue
            if any(data.get(k) != v for k, v in sub['arguments'].items()):
                continue
            self.data['outbox'][sub['id'] + ':' + event_id] = {
                'subscription_id': sub['id'], 'event_id': event_id,
                'attempts': 0, 'next_at': now, 'status': 'pending'}
        self.save()

    def finish(self, ident, result):
        job = self.job(ident)
        job.update(status='completed', finished_at=utcnow(), result=result)
        # Completion and its delivery entries are committed in the same state write.
        payload = {k: job[k] for k in ('call_id', 'direction', 'number', 'context_revision')}
        payload['result'] = result
        self.publish('call.completed', payload, 'evt_completed_' + ident)

    def recover(self):
        # Never redial after a crash: the remote side may already have answered.
        for job in list(self.data['jobs'].values()):
            if job['status'] != 'completed':
                self.finish(job['call_id'], {'outcome': 'interrupted',
                    'summary_status': 'unavailable',
                    'error': 'Server restarted. Call state is uncertain; no automatic redial.',
                    'summary': None})
