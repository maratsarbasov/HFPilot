"""Durable conversation evidence; no scheduling rules or task database."""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def dump(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def utterances(fragments):
    # Group each speaker independently: full duplex includes overlapping speech.
    turns = []
    for speaker in ('caller', 'assistant'):
        current = None
        for f in sorted((x for x in fragments if x['speaker'] == speaker),
                        key=lambda x: (x['start_ms'], x['sequence'])):
            if current is None or f['start_ms'] - current['end_ms'] > 1500:
                current = {'speaker': speaker, 'start_ms': f['start_ms'],
                           'end_ms': f['end_ms'], 'text': ''}
                turns.append(current)
            current['text'] += f['delta']  # Do not strip spaces or repeated words.
            current['end_ms'] = max(current['end_ms'], f['end_ms'])
    return sorted(turns, key=lambda x: x['start_ms'])


class Journal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.events = (self.directory / 'events.jsonl').open('x', buffering=1)
        self.transcripts = (self.directory / 'transcript.jsonl').open('x', buffering=1)
        self.fragments = []
        self.seen = set()
        self.last_sync = time.monotonic()

    def event(self, kind, **data):
        item = {'at': datetime.now(timezone.utc).isoformat(), 'event': kind, **data}
        self.events.write(json.dumps(item, ensure_ascii=False) + '\n')
        if time.monotonic() - self.last_sync > 1:
            self.sync()

    def api(self, event):
        event_id = event.get('event_id')
        if event_id and event_id in self.seen:
            return
        if event_id:
            self.seen.add(event_id)
        self.event('api', payload=event)
        kind = event.get('type')
        if kind in ('session.input_transcript.delta', 'session.output_transcript.delta'):
            item = {'speaker': 'caller' if kind == 'session.input_transcript.delta' else 'assistant',
                    'sequence': len(self.fragments), 'start_ms': event['start_ms'],
                    'end_ms': event['end_ms'], 'delta': event['delta'], 'event_id': event_id}
            self.fragments.append(item)
            self.transcripts.write(json.dumps(item, ensure_ascii=False) + '\n')

    def render(self):
        turns = utterances(self.fragments)
        dump(self.directory / 'transcript.json', turns)
        text = '\n'.join(f"[{x['start_ms']/1000:.2f}–{x['end_ms']/1000:.2f}] "
                         f"{x['speaker']}: {x['text']}" for x in turns)
        (self.directory / 'transcript.txt').write_text(text + '\n')
        return text

    def sync(self):
        for f in (self.events, self.transcripts):
            f.flush()
            os.fsync(f.fileno())
        self.last_sync = time.monotonic()

    def close(self):
        self.sync()
        self.events.close()
        self.transcripts.close()
