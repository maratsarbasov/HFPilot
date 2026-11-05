"""D-Bus access for the unsupported legacy PipeWire backend."""
import json
import dbus
from dbus.mainloop.glib import DBusGMainLoop

SERVICE = 'org.pipewire.Telephony'
ROOT = '/org/pipewire/Telephony'
OM = 'org.freedesktop.DBus.ObjectManager'
PROPS = 'org.freedesktop.DBus.Properties'
AG = SERVICE + '.AudioGateway1'
CALL = SERVICE + '.Call1'
TRANSPORT = SERVICE + '.AudioGatewayTransport1'



class Probe:
    def __init__(self, address):
        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SessionBus()
        self.address = address.upper()

    def interface(self, path, interface):
        return dbus.Interface(self.bus.get_object(SERVICE, path, introspect=False), interface)

    def objects(self, path=ROOT):
        return self.interface(path, OM).GetManagedObjects(timeout=5)

    def gateway(self):
        matches = [str(p) for p, i in self.objects().items()
                   if str(i.get(AG, {}).get('Address', '')).upper() == self.address]
        if len(matches) != 1:
            raise RuntimeError('Exactly one connected HFP gateway for this address is required')
        return matches[0]

    def calls(self, gateway):
        return {str(p): str(i[CALL].get('State', ''))
                for p, i in self.objects(gateway).items() if CALL in i}

    def snapshot(self):
        gateway = self.gateway()
        transport = self.interface(gateway, PROPS).GetAll(TRANSPORT, timeout=5)
        print(json.dumps(dict(event='status', gateway=gateway, calls=self.calls(gateway),
             transport={str(k): (bool(v) if isinstance(v, dbus.Boolean) else
                                int(v) if isinstance(v, dbus.Byte) else str(v))
                        for k, v in transport.items()})))
