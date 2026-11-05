"""Full-duplex raw PCM bridge, isolated to one Bluetooth phone."""
import asyncio
import contextlib
import json
import subprocess
import time
import uuid
import wave

import numpy as np

from .pipewire_graph import graph, link_name, node_ports, props, run

from .media import MediaBridge, RATE, BYTES, SILENCE, gain_pcm, put_latest


class AudioBridge(MediaBridge):
    def __init__(self, address, journal, input_gain=4, output_gain=.35, record=False):
        super().__init__(journal, input_gain, output_gain, record)
        self.address = address.upper()
        self.name = 'voice_agent_' + uuid.uuid4().hex[:8]
        self.capture = self.player = None
        self.saved = set()
        self.logs = []
        self.wavs = {}
        self.last_route_error = None

    async def start(self):
        for suffix, direction in (('capture', '--record'), ('playback', '--playback')):
            logfile = (self.journal.directory / f'pipewire-{suffix}.log').open('wb')
            self.logs.append(logfile)
            p = await asyncio.create_subprocess_exec(
                'pw-cat', direction, '--raw', '--target', '0', '--rate', str(RATE),
                '--channels', '1', '--channel-map', 'MONO', '--format', 's16',
                '--latency', '20ms', '--properties', json.dumps({
                    'node.name': self.name + '_' + suffix, 'node.dont-reconnect': True,
                    'node.autoconnect': False}), '-',
                stdin=asyncio.subprocess.PIPE if suffix == 'playback' else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if suffix == 'capture' else asyncio.subprocess.DEVNULL,
                stderr=logfile)
            setattr(self, 'capture' if suffix == 'capture' else 'player', p)
        if self.record:
            for name in ('caller', 'assistant'):
                w = wave.open(str(self.journal.directory / f'{name}.wav'), 'wb')
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
                self.wavs[name] = w
        self.spawn(self.capture_audio())
        self.spawn(self.play_audio())
        deadline = time.monotonic() + 12
        while not await asyncio.to_thread(self.route):
            if self.failure or time.monotonic() > deadline:
                raise RuntimeError(self.failure or
                    'SCO audio ports did not become ready. Last routing error: ' +
                    (self.last_route_error or 'ports not available'))
            await asyncio.sleep(.15)
        self.ready = True
        self.journal.event('audio-ready', input_rate=RATE, input_gain=self.input_gain,
                           output_gain=self.output_gain)
        self.spawn(self.maintain_routes())

    def route(self):
        try:
            routed = self._route()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            # Ports can disappear/reconfigure between pw-dump and pw-link.
            # The start/maintenance loops retry from a fresh graph with their
            # existing deadlines. Policy errors (e.g. foreign apps) stay fatal.
            stderr = error.stderr or ''
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors='replace')
            self.last_route_error = f'{error} {stderr.strip()}'.strip()
            self.journal.event('audio-route-retry', command=error.cmd,
                               returncode=getattr(error, 'returncode', None),
                               message=self.last_route_error)
            return False
        if routed:
            self.last_route_error = None
        return routed

    def _route(self):
        objects = graph()
        nodes = {o['id']: o for o in objects if o['type'].endswith(':Node')}
        ports = {o['id']: o for o in objects if o['type'].endswith(':Port')}
        def phone(factory):
            found = [n for n in nodes.values() if props(n).get('factory.name') == factory
                     and str(props(n).get('api.bluez5.address', '')).upper() == self.address]
            if len(found) > 1:
                raise RuntimeError('Ambiguous SCO nodes')
            return found[0] if found else None
        rx, tx = phone('api.bluez5.sco.source'), phone('api.bluez5.sco.sink')
        rec = next((n for n in nodes.values() if props(n).get('node.name') == self.name + '_capture'), None)
        play = next((n for n in nodes.values() if props(n).get('node.name') == self.name + '_playback'), None)
        if not all((rx, tx, rec, play)):
            return False
        # Inspect the whole plan before removing any links. Only this phone's
        # physical ALSA links may be disconnected; other applications are refused.
        remove = []
        pairs = set()
        for obj in objects:
            if not obj['type'].endswith(':Link'):
                continue
            i = obj.get('info', {})
            pairs.add((i['output-port-id'], i['input-port-id']))
            if i['output-node-id'] == rx['id'] or i['input-node-id'] == tx['id']:
                other_id = i['input-node-id'] if i['output-node-id'] == rx['id'] else i['output-node-id']
                if other_id in (rec['id'], play['id']):
                    continue
                other = props(nodes[other_id])
                if other.get('device.api') != 'alsa' and not other.get('node.name', '').startswith('alsa_'):
                    raise RuntimeError('Another application is using the phone audio')
                remove.append((link_name(objects, ports[i['output-port-id']]),
                               link_name(objects, ports[i['input-port-id']])))
        for source, target in remove:
            run('pw-link', '-d', source, target)
            self.saved.add((source, target))
            self.journal.event('audio-isolated', source=source, target=target)
        if remove:
            # Unlinking physical devices can renegotiate the SCO port layout.
            # Never connect using the snapshot from before that change.
            return False
        r = node_ports(objects, rx['id'], 'output')
        t = node_ports(objects, tx['id'], 'input')
        c = node_ports(objects, rec['id'], 'input')
        p = node_ports(objects, play['id'], 'output')
        if not all((r, t, c, p)):
            return False
        wanted = [(r[0]['id'], c[0]['id'])] + [(p[0]['id'], x['id']) for x in t]
        for source, target in wanted:
            if (source, target) not in pairs:
                source_name = link_name(objects, ports[source])
                target_name = link_name(objects, ports[target])
                # Names resolve afresh inside pw-link; numeric IDs can be reused.
                run('pw-link', source_name, target_name)
                self.journal.event('audio-linked', source=source_name, target=target_name)
        return True

    async def maintain_routes(self):
        missing_since = None
        while True:
            await asyncio.sleep(.5)
            # shield the thread so cleanup cannot race a still-running route edit.
            route_task = asyncio.create_task(asyncio.to_thread(self.route))
            try:
                routed = await asyncio.shield(route_task)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await route_task
                raise
            if routed:
                missing_since = None
            else:
                missing_since = missing_since or time.monotonic()
                if time.monotonic() - missing_since > 8:
                    raise RuntimeError('Bluetooth audio was unavailable for 8 seconds. '
                                       'Last routing error: ' +
                                       (self.last_route_error or 'ports not available'))

    async def capture_audio(self):
        while True:
            data = await self.capture.stdout.readexactly(BYTES)
            if 'caller' in self.wavs:
                self.wavs['caller'].writeframesraw(data)
            self.stats['input_bytes'] += len(data)
            self.stats['input_peak'] = max(self.stats['input_peak'],
                int(np.max(np.abs(np.frombuffer(data, '<i2').astype(np.int32)))))
            self.stats['input_dropped_frames'] += put_latest(self.incoming, gain_pcm(data, self.input_gain))

    async def play_audio(self):
        target = time.monotonic()
        while True:
            await asyncio.sleep(max(0, target - time.monotonic()))
            target = max(target + .02, time.monotonic())
            try:
                data = self.outgoing.get_nowait() if self.ready else SILENCE
            except asyncio.QueueEmpty:
                data = SILENCE
            data = gain_pcm(data, self.output_gain)
            if 'assistant' in self.wavs:
                self.wavs['assistant'].writeframesraw(data)
            self.stats['output_bytes'] += len(data)
            self.stats['output_peak'] = max(self.stats['output_peak'],
                int(np.max(np.abs(np.frombuffer(data, '<i2').astype(np.int32)))))
            self.player.stdin.write(data)
            await asyncio.wait_for(self.player.stdin.drain(), 3)

    async def close(self, restore_links=True):
        self.ready = False
        self.track.stop()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for p in (self.capture, self.player):
            if p and p.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    p.terminate()
                try:
                    await asyncio.wait_for(p.wait(), 3)
                except TimeoutError:
                    p.kill(); await p.wait()
        failures, restored, vanished, skipped = [], [], [], []
        try:
            if not restore_links:
                skipped = [list(pair) for pair in self.saved]
            for source, target in self.saved if restore_links else ():
                objects = await asyncio.to_thread(graph)
                names = {link_name(objects, o) for o in objects if o['type'].endswith(':Port')}
                if source not in names or target not in names:
                    vanished.append([source, target]); continue
                try:
                    await asyncio.to_thread(run, 'pw-link', source, target)
                    restored.append([source, target])
                except Exception as e:
                    # Nodes may disappear, or WirePlumber may restore a link,
                    # between the snapshot and pw-link. Verify the new graph.
                    objects = await asyncio.to_thread(graph)
                    ports = {o['id']: o for o in objects if o['type'].endswith(':Port')}
                    names = {link_name(objects, o) for o in ports.values()}
                    pairs = {(link_name(objects, ports[o['info']['output-port-id']]),
                              link_name(objects, ports[o['info']['input-port-id']]))
                             for o in objects if o['type'].endswith(':Link')
                             and o['info']['output-port-id'] in ports
                             and o['info']['input-port-id'] in ports}
                    if source not in names or target not in names:
                        vanished.append([source, target])
                    elif (source, target) in pairs:
                        restored.append([source, target])
                    else:
                        failures.append([source, target, str(e)])
        except Exception as e:
            failures.append(str(e))
        for w in self.wavs.values():
            w.close()
        for f in self.logs:
            f.close()
        result = {'restored': restored, 'vanished': vanished,
                  'skipped_call_ended': skipped, 'failures': failures, **self.stats}
        self.journal.event('audio-closed', **result)
        return result
