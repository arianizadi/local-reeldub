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
from voices import write_references


class SoundtrackTest(unittest.TestCase):
    def test_subtitle_byline_stays_out_of_generated_dialogue(self):
        from reeldub import subtitle_credit, timed_lines
        for credit in ("Subtitles by the Amara.org community.", "Closed captioning provided by the team.",
                       "Sottotitoli creati dalla comunità Amara.org", "Altyazı M.K."):
            self.assertTrue(subtitle_credit(credit), credit)
        actual = ["Amara said hello.", "We used Amara.org to translate the subtitles.", "Subtitles by volunteers are helpful.",
                  "I watch movies with subtitles.", "Thanks for watching!"]
        self.assertTrue(all(not subtitle_credit(text) for text in actual))
        cues = [dict(start=0, end=2.6, text="Subtitles by the Amara.org community.", speaker=0),
                dict(start=3.2, end=6.5, text=actual[0], speaker=0)]
        self.assertEqual(timed_lines(cues, 10), cues[1:])

    def test_credit_in_a_mixed_segment_keeps_real_word_times(self):
        from faster_whisper.transcribe import Segment, Word
        from reeldub import spoken_segments
        words = [Word(i, i+1, w, .9) for i, w in enumerate(
            (" Subtitles", " by", " the", " Amara.org", " community.", " The", " train", " left."))]
        seg = Segment(0,0,0,8,"".join(w.word for w in words),[],0.,0.,0.,words,0.)
        restored, = spoken_segments([seg])
        self.assertEqual((restored.text, restored.start, restored.end), ("The train left.", 5, 8))

    def test_direct_translation_needs_native_or_independent_speaker_support(self):
        from reeldub import grounded_drafts
        intro = dict(start=0, end=3.3)
        speech = dict(start=3.3, end=6.5)
        quiet_reply = dict(start=7., end=7.35)
        turns = [SimpleNamespace(start=3.05,end=6.42), SimpleNamespace(start=3.1,end=3.3),
                 SimpleNamespace(start=7.,end=7.32)]
        self.assertEqual(grounded_drafts([intro, speech, quiet_reply], [speech], turns),
                         [(speech, False), (quiet_reply, True)])

    def test_kitten_reference_gap_transcribes_the_selected_pcm_window(self):
        from reeldub import transcribe_reference
        from unittest.mock import Mock

        speech = np.arange(16000, dtype=np.float32)
        model = Mock()
        model.transcribe.return_value = ([SimpleNamespace(text=" test")], {})
        self.assertEqual(transcribe_reference(model, speech, (.25, .5), "fa", 1), "test")
        call = model.transcribe.call_args
        self.assertTrue(np.shares_memory(call.args[0], speech))
        np.testing.assert_array_equal(call.args[0], speech[4000:8000])
        self.assertEqual(call.kwargs["language"], "fa")

    def test_matching_needs_isolated_speech_from_the_attributed_speaker(self):
        line = {"start": 0, "end": 1, "speaker": 0}
        speaker = SimpleNamespace(start=0, end=1, speaker=0)
        overlap = SimpleNamespace(start=0, end=.9, speaker=1)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "source.wav"
            sf.write(source, np.zeros(RATE), RATE)
            for turns in ([], [overlap], [speaker, overlap]):
                with self.subTest(turns=turns), self.assertRaisesRegex(ValueError, "could not isolate"):
                    write_references([line], turns, source, folder, require_isolated=True)
                self.assertFalse((folder / "reference-0.wav").exists())
            write_references([line], [speaker], source, folder, require_isolated=True)
            self.assertEqual(sf.info(folder / "reference-0.wav").frames, RATE)

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

