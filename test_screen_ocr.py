from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import screen_ocr as ocr
from paddle_screen import best_reading, paddle_reader


class ScreenWords(unittest.TestCase):
    def test_models_read_independent_scripts_in_the_same_frame(self):
        image = np.zeros((100, 200), dtype=np.uint8)
        candidates = [
            {"text": "静かな朝の散歩", "box": [.05, .1, .7, .23], "confidence": .99},
            {"text": "Próxima estación: Málaga", "box": [.04, .5, .94, .6], "confidence": .99},
        ]
        tsv = "header\n5\t1\t1\t1\t1\t1\t156\t50\t30\t10\t93\tMalaga\n"
        with patch.object(ocr, "transcribe", return_value=tsv):
            rows = ocr.read_frame(image, "eng+jpn", lambda _: candidates)
        self.assertCountEqual([row.text for row in rows], [item["text"] for item in candidates])

    def test_arabic_head_can_beat_confident_latin_transliteration(self):
        native = {"text": "مثال", "confidence": .95}
        latin = {"text": "JLo", "confidence": .92}
        self.assertEqual(best_reading({"general": latin, "arabic": native}), native)
        self.assertEqual(best_reading({"general": native}), native)
        japanese = {"text": "静かな朝", "confidence": .97}
        self.assertEqual(best_reading({"general": japanese, "arabic": latin}), japanese)

    def test_existing_model_volume_still_exposes_prepared_specialist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for model in ("det", "arabic"):
                folder = root / "models" / model
                folder.mkdir(parents=True)
                for filename in ("inference.yml", "inference.json", "inference.pdiparams"):
                    (folder / filename).touch()
            (root / "bin").mkdir()
            (root / "bin/python").touch()
            with patch("paddle_screen.RUNTIME", root), paddle_reader() as reader:
                self.assertTrue(callable(reader))

    def test_hangul_overlapping_syllables_and_separate_words(self):
        words = [("바", (.10,.2,.35,.3), 95), ("람", (.20,.2,.29,.3), 95),
                 ("이", (.29,.2,.35,.3), 96), ("부는", (.40,.2,.56,.3), 96),
                 ("거리", (.61,.2,.78,.3), 97)]
        self.assertEqual(ocr.word_text(words), "바람이 부는 거리")

    def test_language_selection_is_independent_of_spoken_language(self):
        available = "Languages (4):\neng\nfas\njpn\nrus\n"
        with patch.dict(os.environ, {"REELDUB_OCR_LANGUAGES": ""}), patch("screen_ocr.subprocess.run") as run:
            run.return_value.stdout = available
            self.assertEqual(ocr.ocr_languages("en").split("+"), ["eng", "fas", "rus", "jpn"])
            with patch.dict(os.environ, {"REELDUB_OCR_LANGUAGES": "ukr"}), self.assertRaises(RuntimeError):
                ocr.ocr_languages(None)

    def test_rows_read_in_logical_order_and_keep_distinct_labels(self):
        rows = [ocr.Line("زندگی زیباست", (.35, .61, .69, .65), 92),
                ocr.Line("هنوز امیدوارم", (.32, .66, .68, .70), 90),
                ocr.Line("مامان", (.18, .2, .27, .23), 96)]
        groups = ocr.paragraphs(rows)
        self.assertEqual([item.text for item in groups], ["مامان", "زندگی زیباست هنوز امیدوارم"])
        self.assertEqual(groups[1].box, (.32, .61, .69, .70))

    def test_cut_ends_the_region_at_its_first_new_frame(self):
        rng = np.random.default_rng(11)
        shot = rng.integers(0, 255, (200, 300), dtype=np.uint8)
        empty = 255 - shot
        line = ocr.Line("نمونه", (.2, .2, .4, .24), 96)
        frames = [(0, shot), (.125, shot), (.25, shot), (.375, empty), (.5, empty)]
        with patch.object(ocr, "read_frame", side_effect=[[line], []]) as detect:
            tracks = ocr.track_frames(frames, "fas")
        self.assertEqual(detect.call_count, 2)
        self.assertEqual(tracks[0]["end"], .375)
        self.assertEqual(tracks[0]["box"], list(line.box))

    def test_short_unconfirmed_scene_texture_and_separate_ui_fields(self):
        frame = np.zeros((240, 400), np.uint8)
        noise = ocr.Line("カ イ タ", [.10,.2,.3,.24], 93)
        track = ocr.Track(noise, 0., frame)
        track.samples = 3
        self.assertEqual(track.finish(.375), [])
        rows = [ocr.Line("عنوان", [.2,.2,.29,.23], 96), ocr.Line("متن در اینجا", [.2,.23,.45,.26], 87)]
        self.assertEqual([r.text for r in ocr.paragraphs(rows)], ["عنوان", "متن در اینجا"])

    def test_decorative_glyphs_and_numbers_are_excluded(self):
        tsv = "header\n5\t1\t1\t1\t1\t1\t10\t20\t75\t30\t95\t一一\n"
        tsv += "5\t1\t2\t1\t1\t1\t10\t85\t70\t15\t98\t۱۲۳\n"
        self.assertEqual(ocr.parse_words(tsv, 320, 180), [])

    def test_modern_motion_frame_preserves_latin_and_joins_padded_arabic_rows(self):
        frame = np.full((150, 300), 240, dtype=np.uint8)
        lines = [
            {"text": "Message", "confidence": .98, "box": [.05,.11,.2,.14]},
            {"text": "این یک", "confidence": .99, "box": [.3,.54,.7,.60]},
            {"text": "مثال است", "confidence": .96, "box": [.34,.585,.69,.65]},
        ]
        with patch.object(ocr, "transcribe", side_effect=AssertionError("Unexpected broad OCR")):
            found = ocr.read_frame(frame, "eng+fas", lambda _: lines, broad=False)
            self.assertCountEqual([line.text for line in found], ["Message", "این یک مثال است"])
            self.assertTrue(all(line.verified for line in found))
            self.assertEqual(ocr.read_frame(frame, "eng+fas", lambda _: [], broad=False), [])

    def test_readable_moving_text_uses_the_box_at_each_time(self):
        frame = np.full((200, 300), 180, dtype=np.uint8)
        a, b = [.2,.2,.4,.25], [.25,.2,.45,.25]
        track = ocr.Track(ocr.Line("مثال", a, 98, True), 1., frame)
        line = ocr.Line("مثال", b, 97, True)
        self.assertTrue(track.matches(line))
        track.update(line, frame, 1.125)
        parts = track.finish(1.25)
        self.assertEqual([p["box"] for p in parts], [a, b])
        self.assertEqual([(p["start"], p["end"]) for p in parts], [(1., 1.125), (1.125, 1.25)])

    def test_specialized_reader_failure_falls_back_once(self):
        frame = np.full((120, 200), 240, dtype=np.uint8)
        with patch.object(ocr, "transcribe", return_value="header\n"):
            calls = []
            def failed(gray):
                calls.append(True)
                return None
            self.assertEqual(ocr.read_frame(frame, "fas", failed), [])
            self.assertEqual(len(calls), 1)


@unittest.skipUnless(all(shutil.which(tool) for tool in ("ffmpeg", "pango-view", "tesseract")),
                     "Requires a local font renderer, FFmpeg and Tesseract")
class PersianFixture(unittest.TestCase):
    def test_real_persian_text_on_silent_rotated_video(self):
        if "fas" not in ocr.ocr_languages("en").split("+"):
            self.skipTest("Requires Persian Tesseract data")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["pango-view", "--no-display", "--font=Sans 32", "--pixels",
                            "--margin=40", "--background=white", "--foreground=black",
                            "--text=سلام دنیا امروز یک روز بسیار زیبا است", "-o", str(root / "text.png")], check=True)
            # Store the pixels sideways and rotate using MP4's display matrix;
            # the viewer and the coordinates returned by OCR should be upright.
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-loop", "1", "-i", str(root / "text.png"),
                            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2,transpose=1", "-t", "0.75",
                            "-pix_fmt", "yuv420p", "-c:v", "libx264", str(root / "sideways.mp4")], check=True)
            tools = subprocess.run(["ffmpeg", "-hide_banner", "-h", "full"], capture_output=True).stdout
            rotate = ["-display_rotation:v:0", "90"] if b"-display_rotation" in tools else []
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", *rotate, "-i", str(root / "sideways.mp4"),
                            "-c", "copy", "-metadata:s:v:0", "rotate=90", str(root / "rotated.mp4")], check=True)
            tracks = ocr.track_frames(ocr.sampled_frames(root / "rotated.mp4", True),
                                      ocr.ocr_languages("en"))
            observed = [track for track in tracks if "سلام" in track["text"] and "دنیا" in track["text"]]
            self.assertTrue(observed, tracks)
            left, top, right, bottom = observed[0]["box"]
            self.assertGreater(right-left, bottom-top)
