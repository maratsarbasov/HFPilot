import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from voice_agent.calls import handle_call
from voice_agent.live import summarize, SUMMARY_SCHEMA
from voice_agent.config import DEFAULT_CONFIG, load_config
from voice_agent.journal import Journal
from voice_agent.skywalk import dial_number
from voice_agent import recording

ROOT = Path(__file__).resolve().parents[1]


class OutgoingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, root, behavior, *, record_audio=False, recording_path=None, change_recording=None):
        assignment=root/'assignment.txt'; assignment.write_text('Ask the person for a test phrase.')
        args=SimpleNamespace(config=DEFAULT_CONFIG, assignment=assignment, output=root/'calls', address='PHONE',
            command='dial', number='+12025550123', dial_timeout=0, test_call=True,
            record_audio=record_audio, recording_preference_path=recording_path,
            input_gain=1, output_gain=1, max_call=10)
        stop=asyncio.Event(); calls={}; order=[]; owned=False
        bridge=SimpleNamespace(ready=False, failure=None, close=AsyncMock(return_value={'failures':[]}))
        async def audio_start():
            self.assertEqual(calls,{'1':'active'})
            order.append('audio'); bridge.ready=True
            if change_recording is not None:
                recording.save(change_recording, recording_path)
        bridge.start=audio_start
        async def dial(number, call):
            nonlocal owned
            if calls: raise RuntimeError('Another call exists')
            self.assertEqual(number,args.number)
            order.append('dial'); owned=True; calls[call]='dialing'
            phone.state.update(setup=0,dial_pending=1)
            if behavior=='lost-ack': raise TimeoutError('Lost dial acknowledgement')
            if behavior=='cancel': stop.set()
            elif behavior=='declined': calls.clear(); owned=False; phone.state['dial_pending']=0
            elif behavior=='success': calls[call]='active'; phone.state['dial_pending']=0
            elif behavior=='no-answer': calls[call]='alerting'; phone.state.update(setup=3,dial_pending=0)
        async def hangup(call):
            nonlocal owned
            self.assertTrue(owned)
            order.append('hangup'); calls.clear(); owned=False
        phone=SimpleNamespace(name='skywalk', state={}, read=lambda:('PHONE',dict(calls)),
            owns_call=lambda call:owned, dial=dial, answer=AsyncMock(), hangup=hangup,
            audio=Mock(return_value=bridge), prepare_caller=AsyncMock())
        async def connect(config):
            order.append('live'); live.config=config
            if behavior=='api-failure': raise RuntimeError('API unavailable')
            if behavior=='incoming': calls['1']='incoming'
        def greet(*, outgoing):
            self.assertTrue(outgoing); self.assertTrue(bridge.ready); order.append('greet')
        live=SimpleNamespace(connect=connect, greet=greet, tick=Mock(), failure=None, closed=asyncio.Event(),
            ending=SimpleNamespace(due=lambda:True,result={}))
        with patch('voice_agent.calls.LiveSession',return_value=live), patch('voice_agent.calls.emit'):
            completed=await handle_call(args,SimpleNamespace(key='test'),phone,'PHONE','1',stop,asyncio.Event())
        completed.journal.close()
        self.assertEqual(phone.audio.call_args.args[-1], completed.metadata['record_audio'])
        phone.answer.assert_not_awaited(); phone.prepare_caller.assert_not_awaited()
        return completed,order,calls

    async def test_recording_changes_apply_to_next_outgoing_call_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'record-audio'
            for expected, change in ((True, False), (False, True), (True, None)):
                completed, _, _ = await self.run_case(root, 'success', record_audio=None,
                                                     recording_path=path, change_recording=change)
                self.assertEqual(completed.metadata['record_audio'], expected)
            # Explicit CLI overrides are separate from the saved application preference.
            completed, _, _ = await self.run_case(root, 'success', record_audio=False, recording_path=path)
            self.assertFalse(completed.metadata['record_audio'])
            self.assertTrue(recording.enabled(path))

    async def test_prepare_then_dial_then_answered_audio_and_model_hangup(self):
        with tempfile.TemporaryDirectory() as root:
            result,order,state=await self.run_case(Path(root),'success')
            self.assertEqual(order,['live','dial','audio','greet','hangup'])
            self.assertEqual(state,{})
            self.assertEqual(result.metadata['outcome'],'assistant_hung_up')
            self.assertEqual(result.metadata['direction'],'outgoing')
            self.assertEqual(result.metadata['dialed_number'],'+12025550123')
            self.assertIsNone(result.metadata['caller']['number'])
            self.assertEqual(result.exit_code,0)
            for prompt in (result.live.config['instructions'],
                           result.live.config['delegation']['responses']['instructions']):
                self.assertIn('"direction": "outgoing"',prompt)
                self.assertIn('Ask the person for a test phrase.',prompt)
                self.assertIn('+12025550123',prompt)

    async def test_pre_answer_failures_never_greet_and_cancel_only_our_dial(self):
        cases = {
            'api-failure': ('failed',['live']),
            'incoming': ('failed',['live']),
            'lost-ack': ('failed',['live','dial','hangup']),
            'cancel': ('stopped',['live','dial','hangup']),
            'no-answer': ('no_answer',['live','dial','hangup']),
            'dial-timeout': ('dial_timeout',['live','dial','hangup']),
            'declined': ('not_connected',['live','dial']),
        }
        for behavior,(outcome,expected) in cases.items():
            with self.subTest(behavior=behavior), tempfile.TemporaryDirectory() as root:
                result,order,state=await self.run_case(Path(root),behavior)
                self.assertEqual(order,expected)
                self.assertEqual(result.metadata['outcome'],outcome)
                self.assertNotIn('connected_at',result.metadata)
                self.assertEqual(state,{'1':'incoming'} if behavior=='incoming' else {})

    async def test_outgoing_summary_reports_assignment_result_and_no_answer_without_api(self):
        with tempfile.TemporaryDirectory() as root:
            journal=Journal(Path(root)/'call')
            (journal.directory/'assignment.txt').write_text('Ask for a test phrase.')
            api=SimpleNamespace(request=AsyncMock())
            metadata={'direction':'outgoing','outcome':'no_answer'}
            agent=load_config()
            await summarize(api,journal,metadata,agent)
            api.request.assert_not_awaited()
            self.assertIn('no_answer',(journal.directory/'summary.txt').read_text())
            journal.api(dict(type='session.input_transcript.delta',event_id='1',
                start_ms=0,end_ms=100,delta='Testing.'))
            api.request.return_value={'status':'completed','output':[{'type':'message','content':[
                {'type':'output_text','text':json.dumps({'summary_ru':'Тест завершён.','owner_next_steps':[]})}]}]}
            metadata.update(connected_at='now',outcome='remote_hung_up')
            await summarize(api,journal,metadata,agent)
            body=api.request.call_args.args[2]
            self.assertEqual(body['text']['format']['schema'],SUMMARY_SCHEMA)
            self.assertEqual(json.loads(body['input'])['assignment'],'Ask for a test phrase.')
            self.assertIn('Тест завершён',(journal.directory/'summary.txt').read_text())
            journal.close()


class DialStateTests(unittest.TestCase):
    def test_number_validation_rejects_at_injection_and_non_numbers(self):
        self.assertEqual(dial_number('+12025550123'),'+12025550123')
        for number in ('12025550123','+01234567','+123','+12025550123;ATH',
                       '+12025550123\rATD123;', '+１２３４５６７８', '+1234567890123456'):
            with self.subTest(number=number), self.assertRaises(ValueError): dial_number(number)

    @unittest.skipUnless(shutil.which('cc'),'C compiler required')
    def test_real_c_ownership_state_machine(self):
        with tempfile.TemporaryDirectory() as root:
            source=ROOT/'macos/Skywalk'; binary=Path(root)/'state-test'
            subprocess.run(['cc','-Wall','-Wextra','-Werror','-I'+str(source),
                str(ROOT/'tests/fixtures/outgoing_state.c'),str(source/'call_state.c'),
                str(source/'caller_id.c'),'-o',str(binary)],check=True)
            subprocess.run([str(binary)],check=True)
