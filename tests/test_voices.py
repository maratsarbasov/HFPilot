import copy
from pathlib import Path
import tempfile
import unittest

import av

from voice_agent import voices
from voice_agent.config import DEFAULT_CONFIG, configuration, load_config


class VoiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'agent.toml'
        self.path.write_bytes(DEFAULT_CONFIG.read_bytes())

    def test_selection_preserves_prompts_and_comments_and_only_changes_future_calls(self):
        text = self.path.read_text().replace('voice = "marin"', 'voice = "marin"  # Keep my comment')
        # A prompt can itself contain a line that looks like a TOML setting.
        text = text.replace('Only the conversation belongs', 'voice = "marin"\nOnly the conversation belongs')
        self.path.write_text(text)
        before = load_config(self.path)
        desired = copy.deepcopy(before.data)
        desired['voice']['voice'] = 'cedar'
        result = voices.save(self.path, 'cedar', voices.settings(self.path)['revision'])
        self.assertEqual(result['current'], 'cedar')
        self.assertEqual(load_config(self.path).data, desired)
        self.assertIn('voice = "cedar"  # Keep my comment', self.path.read_text())
        self.assertIn('voice = "marin"\nOnly the conversation belongs', self.path.read_text())
        for direction in ('incoming', 'outgoing'):
            self.assertEqual(configuration(before, 'A task', 'now', direction=direction)['audio']['output']['voice'], 'marin')
            self.assertEqual(configuration(load_config(self.path), 'A task', 'now', direction=direction)['audio']['output']['voice'], 'cedar')

    def test_invalid_or_stale_changes_cannot_overwrite_configuration(self):
        original = self.path.read_bytes()
        revision = voices.settings(self.path)['revision']
        for value in (None, [], '../cedar', 'not-a-voice'):
            with self.assertRaises(ValueError): voices.save(self.path, value, revision)
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_bytes(original + b'\n# A concurrent edit\n')
        with self.assertRaisesRegex(ValueError, 'changed elsewhere'):
            voices.save(self.path, 'cedar', revision)
        self.assertTrue(self.path.read_bytes().endswith(b'# A concurrent edit\n'))
        actual = self.path.with_name('actual.toml')
        self.path.rename(actual)
        self.path.symlink_to(actual)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            voices.save(self.path, 'cedar', voices.settings(self.path)['revision'])

    def test_other_models_and_broken_configuration_remain_untouched(self):
        self.path.write_text(self.path.read_text().replace('gpt-live-1', 'another-model'))
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'gpt-live-1'):
            voices.save(self.path, 'cedar', voices.settings(self.path)['revision'])
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_text('broken')
        self.assertIsNotNone(voices.settings(self.path)['error'])

    def test_every_listed_voice_has_a_short_english_sample_and_other_languages_are_not_bundled(self):
        catalog = voices.catalog()
        self.assertEqual(len({item['id'] for item in catalog}), len(catalog))
        resources = Path(__file__).resolve().parents[1] / 'macos/Resources/Voices'
        self.assertEqual({p.name for p in resources.iterdir() if p.is_dir()}, {'en'})
        for voice in catalog:
            for language in ('en',):
                with self.subTest(voice=voice['id'], language=language):
                    with av.open(str(resources / language / (voice['id'] + '.m4a'))) as sample:
                        frames = list(sample.decode(audio=0))
                        self.assertTrue(frames)
                        duration = sum(frame.samples / frame.sample_rate for frame in frames)
                        self.assertGreater(duration, 1)
                        self.assertLess(duration, 8.2)
                        self.assertTrue(any(abs(frame.to_ndarray()).max() > .01 for frame in frames))
