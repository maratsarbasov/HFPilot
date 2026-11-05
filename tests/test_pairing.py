import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from voice_agent import phone_profile
from voice_agent.pairing import Inputs, pair

ADDRESS = 'AA:BB:CC:DD:EE:FF'


class ProfileTests(unittest.TestCase):
    def test_selection_commit_is_atomic_and_preserves_previous_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            old = root / 'old-stage'; old.write_bytes(b'OLD TEST CREDENTIALS'); old.chmod(0o600)
            before = phone_profile.commit(old, ADDRESS, 'Old Phone', root)
            old = Path(before['bond_file'])
            staged = root / 'staged'; staged.write_bytes(b'NEW TEST CREDENTIALS'); staged.chmod(0o600)
            with patch('voice_agent.phone_profile.atomic_text', side_effect=OSError('Disk error')):
                with self.assertRaises(OSError):
                    phone_profile.commit(staged, ADDRESS, 'New Phone', root)
            self.assertEqual(phone_profile.load(root), before)
            self.assertEqual(old.read_bytes(), b'OLD TEST CREDENTIALS')
            staged.write_bytes(b'NEW TEST CREDENTIALS'); staged.chmod(0o600)
            saved = phone_profile.commit(staged, ADDRESS, 'New\nPhone\u202e', root)
            self.assertEqual(saved['address'], ADDRESS)
            self.assertEqual(saved['name'], 'NewPhone')
            self.assertEqual(Path(saved['bond_file']).stat().st_mode & 0o777, 0o600)
            self.assertEqual(old.read_bytes(), b'OLD TEST CREDENTIALS')
            self.assertEqual(json.loads((root/'phone.json').read_text())['address'], ADDRESS)

    def test_unconfigured_phone_does_not_use_a_developer_device(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root/'phone-bond.tlv').touch(mode=0o600)
            self.assertIsNone(phone_profile.load(root))

    def test_private_store_rejects_symlinks_paths_and_public_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source=root/'source'; source.touch(mode=0o600)
            link=root/('bond-'+'a'*32+'.tlv'); link.symlink_to(source)
            with self.assertRaises(ValueError): phone_profile.private_file(link)
            link.unlink(); link.touch(); link.chmod(0o644)
            with self.assertRaises(ValueError): phone_profile.private_file(link)
            settings=root/'phone.json'
            settings.write_text(json.dumps({'address':ADDRESS, 'bond':'../outside.tlv'})); settings.chmod(0o600)
            with self.assertRaises(ValueError): phone_profile.load(root)


class PairProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_cancel_and_helper_exit_never_publish_partial_pairing(self):
        for mode in ('success', 'cancel', 'exit', 'error'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                root=Path(root)
                helper=root/'helper'
                helper.write_text(f'#!{sys.executable}\n' + '''
import json, os, socket, sys
from pathlib import Path
s=socket.socket(fileno=int(sys.argv[3]))
def send(**value): s.send(json.dumps(value).encode())
send(event='waiting')
send(event='confirm',id=7,address='AA:BB:CC:DD:EE:FF',code='123456')
''' + ("sys.exit(0)\n" if mode=='exit' else '''
while True:
    command=s.recv(128)
    if command==b'cancel': sys.exit(0)
    if command==b'confirm 7': break
''' + ("send(event='error',reason='phone_busy')\n" if mode=='error' else '''
Path(sys.argv[4]).write_bytes(b'FAKE TEST BOND')
send(event='name',utf8_hex='546573742050686f6e65')
send(event='connected',address='AA:BB:CC:DD:EE:FF')
''')))
                helper.chmod(0o700)
                staged=root/'stage.tlv'; staged.touch(mode=0o600)
                inputs=Inputs(); observed=[]
                def event(kind, **details):
                    observed.append(kind)
                    if kind=='confirm':
                        if mode=='cancel': inputs.cancelled.set()
                        else: inputs.confirmations.put_nowait(details['id'])
                if mode in ('exit','error'):
                    with self.assertRaisesRegex(RuntimeError, 'stopped unexpectedly|Finish the call'):
                        await pair(helper,staged,inputs,event)
                else:
                    result=await pair(helper,staged,inputs,event)
                    if mode=='success':
                        self.assertEqual(result,{'address':ADDRESS,'name':'Test Phone'})
                        self.assertEqual(staged.read_bytes(),b'FAKE TEST BOND')
                    else:
                        self.assertIsNone(result)
                        self.assertEqual(staged.read_bytes(),b'')
                self.assertFalse((root/'phone.json').exists())


if __name__ == '__main__':
    unittest.main()
