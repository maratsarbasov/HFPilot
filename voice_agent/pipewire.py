"""Unsupported legacy Linux HFP backend; imported only on the PipeWire path."""
import asyncio
import time
from .caller import unknown_caller


class PipeWirePhone:
    name = 'pipewire'

    def __init__(self, address):
        from .pipewire_telephony import Probe
        self.address = address
        self.probe = Probe(address)

    def validate(self):
        self.probe.snapshot()

    async def start(self):
        pass  # serve tolerates a phone that is temporarily disconnected.

    def read(self):
        gateway = self.probe.gateway()
        return gateway, self.probe.calls(gateway)

    def caller(self, call):
        # Caller-ID collection is currently implemented on Skywalk only.
        return unknown_caller()

    async def prepare_caller(self, call):
        return self.caller(call)

    async def answer(self, call):
        import dbus
        from .pipewire_telephony import CALL, PROPS, TRANSPORT
        gateway, calls = self.read()
        if calls != {call: 'incoming'}:
            raise RuntimeError('Incoming call changed before answer')
        self.probe.interface(gateway, PROPS).Set(TRANSPORT, 'RejectSCO',
            dbus.Boolean(False, variant_level=1), signature='ssv', timeout=5)
        self.probe.interface(call, CALL).Answer(timeout=8)

    async def hangup(self, call):
        from .pipewire_telephony import CALL
        _, calls = self.read()
        if calls.get(call) not in ('active', 'held', 'dialing', 'alerting'):
            return
        self.probe.interface(call, CALL).Hangup(timeout=5)
        deadline = time.monotonic() + 5
        while self.read()[1].get(call) not in (None, 'disconnected', 'incoming'):
            if time.monotonic() >= deadline:
                raise TimeoutError('HFP hangup was not confirmed')
            await asyncio.sleep(.1)

    def audio(self, journal, input_gain, output_gain, record):
        from .audio import AudioBridge
        return AudioBridge(self.address, journal, input_gain, output_gain, record)

    async def close(self):
        pass
