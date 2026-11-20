"""Protect full-track replacement across dialogue gaps and the end of a video."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np
import soundfile as sf

from clean_background import leakage_regions, matching_dialogue, suppress_unresolved
from render import mix, RATE


class SoundtrackTest(unittest.TestCase):
    @patch("faster_whisper.vad.get_speech_timestamps")
    def test_cleanup_merges_close_speech_and_respects_track_bounds(self, detect):
        audio = np.zeros(2 * 16000, dtype="float32")
        detect.return_value = [{"start": 0, "end": 12000}, {"start": 15000, "end": 31700}]
        self.assertEqual(leakage_regions(audio), [[0, 2]])
        detect.return_value = [{"start": 1000, "end": 4000}]
        self.assertEqual(leakage_regions(audio), [])

    def test_slow_source_phrase_survives_short_translation(self):
        original = [SimpleNamespace(start=2.5, end=6.8, text="Quel joli citron !")]
        leak = [SimpleNamespace(start=3.1, end=5.1, text="Joli citron."),
                SimpleNamespace(start=20, end=23, text="Quel joli citron !")]
        spans = matching_dialogue(original, leak, 7)
        self.assertEqual(spans, [[2.32, 7]])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "background.wav"
            sound = np.full((7 * 16000, 2), .15, dtype="float32")
            sf.write(path, sound, 16000, subtype="FLOAT")
            suppress_unresolved(path, spans)
            cleaned, _ = sf.read(path, dtype="float32")
            np.testing.assert_array_equal(cleaned[:2 * 16000], sound[:2 * 16000])
            self.assertEqual(float(np.max(np.abs(cleaned[3 * 16000:]))), 0)
        unrelated = [SimpleNamespace(start=2.8, end=6.2, text="Sound of rain")]
        self.assertEqual(matching_dialogue(original, unrelated, 7), [])

    def test_render_uses_only_the_bed_after_the_synthesized_line(self):
        for separated in [False, True]:
            with self.subTest(separated=separated), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                time = np.arange(RATE) / RATE
                source_voice = 0.3 * np.sin(2 * np.pi * 240 * time)
                source = np.column_stack([source_voice, source_voice])
                sf.write(folder / "source.wav", source, RATE)
                bed = np.zeros_like(source)
                if separated:
                    bed[:, 0] = 0.04 * np.sin(2 * np.pi * 660 * time)
                    bed[:, 1] = -bed[:, 0]
                    sf.write(folder / "background.wav", bed, RATE, subtype="FLOAT")
                sf.write(folder / "0.wav", source_voice[: RATE // 4], RATE)
                mix([dict(start=0.02, end=0.32)], folder / "source.wav", folder)
                actual, rate = sf.read(folder / "mix.wav")
                self.assertEqual(rate, RATE)
                self.assertEqual(actual.shape, source.shape)
                self.assertGreater(np.max(np.abs(actual[: RATE // 4])), 0.1)
                np.testing.assert_allclose(actual[RATE // 2:], bed[RATE // 2:], atol=4e-5)


if __name__ == "__main__":
    unittest.main()

