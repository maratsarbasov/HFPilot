"""Use the actual mSBC implementation, not a mocked or upsampled PCM path."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('cmake') and (ROOT/'.work/deps/btstack/src/hci.c').is_file(),
                     'Pinned BTstack checkout and CMake required; see Skywalk build instructions')
class CodecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.build = Path(cls.tmp.name)
        subprocess.run(['cmake', '-S', str(ROOT/'macos/Skywalk'),
                        '-B', str(cls.build),
                        '-DCMAKE_C_FLAGS=-fsanitize=address -fno-omit-frame-pointer'],
                       check=True, capture_output=True)
        subprocess.run(['cmake', '--build', str(cls.build), '--target', 'skywalk-codec-test', '-j', '4'],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def roundtrip(self, chunk, fault):
        pcm = self.build/'decoded.pcm'
        run = subprocess.run([str(self.build/'skywalk-codec-test'), str(chunk), str(fault), str(pcm)],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        stats = json.loads(run.stdout.strip().splitlines()[-1])
        return stats, np.fromfile(pcm, '<i2').astype(float)

    def test_real_5khz_signal_survives_msbc_with_fragmented_hci_packets(self):
        for chunk in (24, 30, 60, 120, 255):
            with self.subTest(chunk=chunk):
                stats, samples = self.roundtrip(chunk, 0)
                self.assertEqual(stats, dict(rate=16000, good=160, concealed=0, bad_packets=0))
                self.assertEqual(len(samples), 160*120)
                # This tone cannot pass through an 8 kHz PCM bottleneck.
                signal = samples[2000:18000]
                spectrum = np.abs(np.fft.rfft(signal))**2
                peak = np.argmax(spectrum)*16000/len(signal)
                self.assertAlmostEqual(peak, 5000, delta=2)
                self.assertGreater(spectrum[int(5000*len(signal)/16000)]/spectrum.sum(), .9)
                self.assertGreater(np.sqrt(np.mean(signal**2)), 5000)

    def test_corrupt_or_missing_fragments_recover_without_stalling_decoder(self):
        for chunk, fault in ((30, 1), (30, 2), (30, 3), (60, 3), (120, 3), (30, 4)):
            with self.subTest(chunk=chunk, fault=fault):
                stats, samples = self.roundtrip(chunk, fault)
                self.assertGreaterEqual(stats['good'], 157)
                self.assertLess(stats['good'], 160)
                self.assertGreater(stats['concealed'], 0)
                self.assertEqual(len(samples), 160*120)
                self.assertGreater(np.sqrt(np.mean(samples[-1200:]**2)), 5000)
