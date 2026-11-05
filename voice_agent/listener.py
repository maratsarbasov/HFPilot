"""Continuous incoming-call observation, independent of call finalization."""
import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class IncomingCall:
    gateway: str
    path: str
    generation: int


class IncomingCalls:
    def __init__(self, read, emit, stop, interval=.2):
        self.read, self.emit, self.stop = read, emit, stop
        self.interval = interval
        self.present = {}
        self.states = {}
        self.blocked = set()
        self.claimed = set()
        self.candidate = None
        self.changed = asyncio.Event()
        self.initial = True
        self.generation = 0
        self.unavailable = False
        self.task = None

    def poll(self):
        try:
            gateway, calls = self.read()
        except Exception as error:
            # Keep the listener alive across a disconnected phone/daemon restart.
            if not self.unavailable:
                self.emit('listener-unavailable', error=str(error))
            self.unavailable = True
            self.initial = False
            self.candidate = None
            self.changed.set()
            return
        if self.unavailable:
            self.emit('listener-reconnected', gateway=gateway)
        self.unavailable = False
        calls = {p: s for p, s in calls.items() if s != 'disconnected'}
        present = {}
        for path, state in calls.items():
            key = (gateway, path)
            ticket = self.present.get(key)
            # HFP can reuse a call path. A fresh ringing state is a new call.
            if ticket is None or (state == 'incoming' and self.states.get(key) != 'incoming'):
                self.generation += 1
                ticket = IncomingCall(gateway, path, self.generation)
            present[key] = ticket
            if self.initial or len(calls) != 1:
                self.blocked.add(ticket)  # Existing calls and call waiting stay untouched.
        if self.initial and calls:
            self.emit('listener-existing-calls-ignored', calls=calls)
        self.initial = False
        self.present = present
        self.states = {(gateway, p): s for p, s in calls.items()}
        current = set(present.values())
        self.blocked.intersection_update(current)
        self.claimed.intersection_update(current)
        self.candidate = next((ticket for key, ticket in present.items()
            if self.states[key] == 'incoming' and ticket not in self.blocked
            and ticket not in self.claimed), None)
        self.changed.set()

    def current(self, ticket):
        return self.present.get((ticket.gateway, ticket.path)) == ticket

    async def start(self):
        self.poll()
        self.task = asyncio.create_task(self.watch())

    async def watch(self):
        try:
            while not self.stop.is_set():
                try:
                    await asyncio.wait_for(self.stop.wait(), self.interval)
                except TimeoutError:
                    self.poll()
        finally:
            self.changed.set()

    async def next(self):
        while not self.stop.is_set():
            ticket = self.candidate
            if ticket and ticket not in self.claimed:
                self.claimed.add(ticket)
                self.candidate = None
                return ticket
            self.changed.clear()
            await self.changed.wait()
        return None

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
