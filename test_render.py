"""Protect full-track replacement across dialogue gaps and the end of a video."""
from pathlib import Path
import tempfile
import unittest

import numpy as np
import soundfile as sf

from render import mix, RATE


class SoundtrackTest(unittest.TestCase):
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

