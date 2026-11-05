import asyncio
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np

from voice_agent.journal import Journal
from voice_agent.skywalk import SkywalkPhone, CommandRejected
from voice_agent.skywalk_audio import PCMResampler, TimelineWav, mix_recordings, INPUT_FRAMES
from scripts.stress_skywalk import exercise

ROOT = Path(__file__).resolve().parents[1]


class RecordingTests(unittest.TestCase):
    def test_dropped_packets_keep_timeline_and_mix_saturates(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            a, b = TimelineWav(directory/'caller.wav'), TimelineWav(directory/'assistant.wav')
            a.write(0, struct.pack('<hh', 20000, 20000))
            a.write(4, struct.pack('<h', 1000))
            b.write(0, struct.pack('<hhh', 20000, 20000, 100))
            with self.assertRaises(ValueError):
                a.write(1, b'\0\0')
            a.close(); b.close(); mix_recordings(directory)
            with wave.open(str(directory/'conversation.wav')) as f:
                values = np.frombuffer(f.readframes(20), '<i2').tolist()
            self.assertEqual(values, [32767, 32767, 100, 0, 1000])
            self.assertEqual(a.missing, 2)

    def test_resampler_is_continuous_across_uneven_sco_packets(self):
        pcm = (np.sin(np.arange(8000)*2*np.pi*600/8000)*10000).astype('<i2').tobytes()
        whole = PCMResampler(8000, 48000).feed(pcm)
        converter = PCMResampler(8000, 48000)
        split = b''.join(converter.feed(pcm[i:i+60]) for i in range(0, len(pcm), 60))
        self.assertEqual(whole, split)


@unittest.skipUnless(shutil.which('cc'), 'C compiler required for real IPC test')
class HelperTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name)/'ipc-test'
        source = ROOT/'macos/Skywalk'
        subprocess.run(['cc', '-Wall', '-Wextra', '-I'+str(source),
                        str(ROOT/'tests/fixtures/skywalk_phone.c'),
                        str(source/'agent_ipc.c'), str(source/'caller_id.c'), str(source/'call_state.c'),
                        '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    async def test_outgoing_ipc_audio_binds_after_dial_and_cancel_before_answer(self):
        for stage in ('pending', 'alerting', 'active', 'incoming-race'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as root:
                root=Path(root); bond=root/'bond.tlv'; bond.touch(mode=0o600)
                phone=SkywalkPhone('AA:BB:CC:DD:EE:FF',self.binary,bond,15,root/'logs')
                journal=Journal(root/'call'); bridge=None
                try:
                    await phone.start()
                    call=phone.outgoing_call()
                    bridge=phone.audio(journal,1,1,False)  # Created before dial, as in real lifecycle.
                    await phone.dial('+12025550123',call)
                    self.assertEqual(phone.read()[1],{call:'dialing'})
                    self.assertTrue(phone.owns_call(call))
                    with self.assertRaises(CommandRejected):
                        await phone.request('dial',int(call)+1,number='+12025550123')
                    if stage in ('alerting','active'):
                        await phone.request('dial-progress',int(call))
                        await phone.request('dial-alert',int(call))
                        self.assertEqual(phone.read()[1],{call:'alerting'})
                    if stage=='active':
                        await phone.request('dial-answer',int(call))
                        await bridge.start()
                        self.assertEqual(bridge.generation,int(call))
                        self.assertEqual(phone.state['streaming'],1)
                    if stage=='incoming-race':
                        await phone.request('ring')
                        self.assertFalse(phone.owns_call(call))
                        await phone.hangup(call)
                        self.assertEqual(list(phone.read()[1].values()),['incoming'])
                    else:
                        await phone.hangup(call)
                        self.assertEqual(phone.read()[1],{})
                    audio=await bridge.close(); bridge=None
                    self.assertEqual(audio['failures'],[])
                finally:
                    if bridge: await bridge.close()
                    await phone.close()
                    journal.close()

    async def test_caller_clip_roundtrip_malformed_packet_and_generation_isolation(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); bond=root/'bond.tlv'; bond.touch(mode=0o600)
            phone=SkywalkPhone('AA:BB:CC:DD:EE:FF',self.binary,bond,10,root/'logs')
            try:
                await phone.start()
                await phone.request('ring')
                call=next(iter(phone.read()[1]))
                self.assertEqual((await phone.prepare_caller(call,.02))['status'],'unknown')
                await phone.request('caller',int(call))
                caller=await phone.prepare_caller(call,.02)
                self.assertEqual(caller['number'],'+12025550123')
                self.assertEqual(caller['name'],'Clinic "A"\\Desk\nТест')
                self.assertEqual(caller['number_type'],145)
                self.assertEqual(caller['source'],'hfp_clip')
                with self.assertRaisesRegex(RuntimeError,'rejected caller-invalid'):
                    await phone.request('caller-invalid',int(call))
                self.assertEqual(phone.caller(call),caller)
                await phone.answer(call)
                await phone.hangup(call)
                await phone.request('ring')
                next_call=next(iter(phone.read()[1]))
                self.assertIsNone(phone.caller(call))
                self.assertEqual(phone.caller(next_call)['status'],'unknown')
                await phone.request('caller-empty',int(next_call))
                withheld=await phone.prepare_caller(next_call,.02)
                self.assertEqual(withheld['status'],'unavailable')
                self.assertIsNone(withheld['number'])
                self.assertIsNone(withheld['name'])
                await phone.answer(next_call)
                await phone.hangup(next_call)
                await phone.request('ring-early')
                await phone.request('caller',int(next_call))
                # Early CLIP is not visible on the old generation.
                self.assertEqual(phone.caller(next_call),withheld)
                await phone.request('ring')
                early_call=next(iter(phone.read()[1]))
                self.assertEqual(phone.caller(early_call),caller)
                # A call-waiting notification must not overwrite active caller ID.
                await phone.answer(early_call)
                with self.assertRaisesRegex(RuntimeError,'rejected caller-empty'):
                    await phone.request('caller-empty',int(early_call))
                self.assertEqual(phone.caller(early_call),caller)
                await phone.hangup(early_call)
                await phone.request('ring')
                queried=await phone.prepare_caller(next(iter(phone.read()[1])))
                self.assertEqual(queried['source'],'hfp_clcc')
                self.assertEqual(queried['number'],'+12025550199')
                self.assertIsNone(queried['name'])
            finally:
                await phone.close()

    async def test_optional_caller_query_is_bounded_and_cannot_hide_transport_failure(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); bond=root/'bond.tlv'; bond.touch(mode=0o600)
            phone=SkywalkPhone('AA:BB:CC:DD:EE:FF',self.binary,bond,10,root/'logs')
            async def query(command, generation):
                if behavior == 'rejected':
                    raise CommandRejected('Unsupported CLCC')
                if behavior == 'lost-ack':
                    await asyncio.Future()
                phone.failure = 'Test control channel failure'
                raise RuntimeError(phone.failure)
            try:
                await phone.start()
                await phone.request('ring')
                call=next(iter(phone.read()[1]))
                with patch.object(phone,'request',side_effect=query) as request:
                    for behavior in ('rejected','lost-ack'):
                        caller=await asyncio.wait_for(phone.prepare_caller(call,.4),.8)
                        self.assertEqual(caller['status'],'unknown')
                    self.assertEqual(request.await_count,2)
                    behavior='transport'
                    with self.assertRaisesRegex(RuntimeError,'control channel failure'):
                        await phone.prepare_caller(call,.4)
                    phone.failure=None  # Restore the deliberately injected test failure.
                await phone.answer(call)
                self.assertEqual(phone.read()[1],{call:'active'})
                await phone.hangup(call)
            finally:
                await phone.close()

    async def test_decoder_bursts_and_scheduler_stalls_do_not_drop_audio(self):
        # Real C socket protocol + both PCM directions. The old independent
        # timers/five-frame queues lose speech under exactly this schedule.
        self.loop = asyncio.get_running_loop()
        self.loop.slow_callback_duration = .4  # The 260 ms pause below is intentional.
        with tempfile.TemporaryDirectory() as root:
            r = await exercise(self.binary, Path(root), seconds=3, stall_ms=260, buffer_ms=420)
        for key in ('input_dropped_frames', 'output_dropped_frames', 'ipc_output_dropped',
                    'input_missing_samples', 'output_missing_samples', 'output_underrun_frames'):
            self.assertEqual(r[key], 0, (key, r))
        for key in ('tx_dropped_bytes', 'tx_silence_bytes', 'rx_ipc_dropped'):
            self.assertEqual(r['helper'][key], 0, (key, r))
        self.assertEqual(r['output_received_frames'], r['source_frames'])
        self.assertEqual(r['output_received_frames'],
                         r['output_submitted_frames'] + r['output_queued_frames_at_close'])
        self.assertGreater(r['consumed_frames'], 100)

    async def test_200ms_target_bounds_actual_helper_audio_and_preserves_frames(self):
        asyncio.get_running_loop().slow_callback_duration = .3
        for rate in (8000, 16000):
            with self.subTest(rate=rate), tempfile.TemporaryDirectory() as root:
                r = await exercise(self.binary, Path(root), seconds=3, stall_ms=100, buffer_ms=200, rate=rate)
            self.assertTrue(r['passed'], r)
            self.assertEqual(r['output_received_frames'],
                             r['output_submitted_frames'] + r['output_queued_frames_at_close'])
            self.assertEqual(r['recording_rate'], rate)
            self.assertGreater(r['helper']['tx_queue_high_water_bytes'], 0)
            self.assertLessEqual(r['helper']['tx_queue_high_water_bytes'], 8 * rate // 25)

    async def test_stall_beyond_budget_is_counted_and_stream_recovers(self):
        asyncio.get_running_loop().slow_callback_duration = .6
        with tempfile.TemporaryDirectory() as root:
            r = await exercise(self.binary, Path(root), seconds=2, stall_ms=400)
        self.assertGreater(r['helper']['tx_silence_bytes'], 0)
        self.assertEqual(r['helper']['tx_dropped_bytes'], 0)
        self.assertEqual(r['failures'], [])
        self.assertGreater(r['output_peak'], 7000)
        self.assertLessEqual(r['input_queue_high_water'], INPUT_FRAMES)

    async def test_streaming_both_ways_and_confirmed_hangup(self):
        for rate in (8000, 16000):
            with self.subTest(rate=rate):
                await self.streaming_both_ways(rate)

    async def streaming_both_ways(self, rate):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bond = root/'bond.tlv'; bond.touch(mode=0o600)
            phone = SkywalkPhone('AA:BB:CC:DD:EE:FF', self.binary, bond, 15, root/'logs')
            journal = Journal(root/'call')
            bridge = None
            try:
                await phone.start()
                self.assertEqual(phone.read()[1], {})
                await phone.request('ring')
                call = next(iter(phone.read()[1]))
                with self.assertRaisesRegex(RuntimeError, 'rejected answer'):
                    await phone.request('answer', int(call)+1)
                bridge = phone.audio(journal, 1, 1, True)
                if rate == 16000:
                    await phone.request('select-msbc')
                await phone.answer(call)
                with self.assertRaises(CommandRejected):
                    await phone.request('audio-start', int(call), rate=24000-rate)
                await bridge.start()
                self.assertEqual(bridge.rate, rate)
                frequency = 5000 if rate == 16000 else 600
                tone = (np.sin(np.arange(960)*2*np.pi*frequency/48000)*10000).astype('<i2').tobytes()
                for _ in range(35):
                    await bridge.outgoing.put(tone)
                    await asyncio.sleep(.02)
                self.assertIsNone(bridge.failure)
                self.assertGreater(bridge.stats['input_peak'], 8000)
                self.assertGreater(bridge.stats['output_peak'], 8000)
                self.assertLessEqual(bridge.incoming.qsize(), INPUT_FRAMES)
                before = bridge.stats['input_bytes']
                await phone.request('sco-off')
                await asyncio.sleep(.08)
                await phone.request('sco-on')
                await asyncio.sleep(.08)
                self.assertGreater(bridge.stats['input_bytes'], before)
                self.assertEqual(phone.state['streaming'], 1)
                await phone.hangup(call)
                self.assertEqual(phone.read()[1], {})
                # iOS can advance call indicators before Python closes media.
                # Stopping an already inactive old stream must be idempotent.
                await phone.request('ring')
                result = await bridge.close(); bridge = None
                self.assertEqual(result['failures'], [])
                self.assertLessEqual(result['helper']['tx_queue_high_water_bytes'],
                                     phone.playback_target_frames * rate // 25)
                self.assertEqual(phone.state['accepted'], 0)
                self.assertEqual(phone.state['setup'], 1)
                self.assertTrue((journal.directory/'conversation.wav').is_file())
                for filename in ('caller.wav', 'assistant.wav', 'conversation.wav'):
                    with wave.open(str(journal.directory/filename)) as wav:
                        self.assertEqual(wav.getframerate(), rate)
                        self.assertGreater(wav.getnframes(), rate * .5)
                        values = np.frombuffer(wav.readframes(wav.getnframes()), '<i2')
                        spectrum = np.abs(np.fft.rfft(values))
                        peak = np.argmax(spectrum)*rate/len(values)
                        self.assertAlmostEqual(peak, frequency, delta=3)
                self.assertGreater(result['input_missing_samples'], 0)
            finally:
                if bridge:
                    await bridge.close()
                await phone.close()
                journal.close()
            self.assertEqual(phone.process.returncode, 0)

    async def test_cvsd_override_and_midstream_codec_change_fail_explicitly(self):
        for mode in ('auto', 'cvsd'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                root=Path(root); bond=root/'bond.tlv'; bond.touch(mode=0o600)
                phone=SkywalkPhone('AA:BB:CC:DD:EE:FF',self.binary,bond,10,root/'logs',codec=mode)
                journal=Journal(root/'call'); bridge=None
                try:
                    await phone.start()
                    if mode=='cvsd':
                        with self.assertRaises(CommandRejected):
                            await phone.request('select-msbc')
                    else:
                        await phone.request('select-msbc')
                    await phone.request('ring')
                    call=next(iter(phone.read()[1]))
                    await phone.answer(call)
                    bridge=phone.audio(journal,1,1,False)
                    await bridge.start()
                    if mode=='cvsd':
                        self.assertEqual(bridge.rate,8000)
                        await phone.hangup(call)
                    else:
                        await phone.request('sco-off')
                        await phone.request('select-cvsd')
                        with self.assertRaisesRegex(RuntimeError,'codec failed or changed|exited'):
                            await phone.request('sco-on')
                            await phone.wait(lambda:False,2)
                        self.assertIsNotNone(phone.failure)
                finally:
                    if bridge: await bridge.close()
                    await phone.close()
                    journal.close()

    async def test_helper_exit_fails_pending_wait_without_hanging(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); bond=root/'bond.tlv'; bond.touch(mode=0o600)
            phone=SkywalkPhone('AA:BB:CC:DD:EE:FF',self.binary,bond,10,root/'logs')
            try:
                await phone.start()
                phone.process.kill()
                with self.assertRaises(RuntimeError):
                    await phone.wait(lambda: False, 2)
            finally:
                await phone.close()
