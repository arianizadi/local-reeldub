import json
import os
from unittest.mock import patch
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import numpy as np

from render import ff, render
from screen_overlay import ass_literal, display_size, label_columns, layout, screen_filter


class OverlayMaskTests(unittest.TestCase):
    def test_short_single_word_stays_whole_as_its_box_moves(self):
        for box in [(455, 1071, 701, 1265), (491, 1153, 634, 1269)]:
            _, size, rows = layout("Magnolia", box, 720, 1280, (0, 720))
            self.assertEqual(rows, ["Magnolia"])
            self.assertGreater(size, 12)

    def test_simultaneous_chat_columns_fit_long_labels(self):
        tracks = [dict(start=0, end=2, box=(.1,.3,.28,.35)), dict(start=1, end=3, box=(.30,.3,.54,.35)),
                  dict(start=3, end=4, box=(.31,.3,.55,.35))]
        left, right, later = label_columns(tracks, 720)
        self.assertEqual(later, [0, 720])
        for (text, box, column) in [("Your verification code", (72,240,202,280), left),
                                    ("Account balance", (216,240,389,280), right)]:
            area, _, rows = layout(text, box, 720, 800, column)
            self.assertGreaterEqual(area[0], column[0])
            self.assertLessEqual(area[2], column[1])
            self.assertEqual(" ".join(rows), text)

    def test_literal_code_and_punctuation_keep_their_glyphs(self):
        # The joiner is invisible to the reader while preventing an ASS newline command.
        value = r"｛東口｝, Вход № 2 — {A} C:\New"
        self.assertEqual(ass_literal(value), "｛東口｝, Вход № 2 — " + "\\{A\\} C:\\" + "\u2060New")

    def test_only_confirmed_translations_can_create_masks(self):
        sample = dict(text="WhatsApp", translation="WhatsApp", start=0, end=1, box=[.1, .2, .5, .4])
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for fields in [{}, {"translation": ""}, {"translation": "Earth", "end": 0},
                           {"translation": "Earth", "box": [0, -.2, .5, .4]}]:
                self.assertIsNone(screen_filter([sample | fields], folder, 320, 180, "en"))
            # A model's output cannot add subtitle commands or events.
            value = "Hello {\\an7} \\N 👋\nDialogue: 99"
            self.assertTrue(screen_filter([sample | {"translation": value}], folder, 320, 180, "en"))
            encoded = (folder / "screen-labels.ass").read_text()
            self.assertNotIn(r"{\an7} ", encoded)
            self.assertEqual(len([line for line in encoded.splitlines() if line.startswith("Dialogue:")]), 2)


def has_ass():
    return shutil.which("ffmpeg") and " ass " in subprocess.check_output(
        ["ffmpeg", "-hide_banner", "-filters"], text=True, stderr=subprocess.DEVNULL)


@unittest.skipUnless(has_ass(), "Requires FFmpeg with libass, as installed on the render host")
class OverlayMuxTests(unittest.TestCase):
    def probe(self, media, *fields):
        return json.loads(subprocess.check_output(["ffprobe", "-v", "error", *fields, "-of", "json", media]))

    def pts(self, video):
        result = self.probe(video, "-select_streams", "v:0", "-show_entries", "frame=best_effort_timestamp_time")
        return [float(f["best_effort_timestamp_time"]) for f in result["frames"]]

    def test_light_sign_completely_replaces_original_ink(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "source.mp4"
            ff("-f", "lavfi", "-i", "color=c=white:s=320x180:r=4:d=1",
               "-vf", "drawgrid=w=9:h=180:t=2:color=black", "-c:v", "libx264", source)
            render(source, [], folder, target="ja", screen_tracks=[dict(
                text="ایستگاه", translation="駅", box=(.04,.15,.94,.83), start=0, end=1, background_luma=245)])
            def first_frame(file):
                raw = ff("-i", file, "-frames:v", "1", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1",
                         stdout=subprocess.PIPE).stdout
                return np.frombuffer(raw, np.uint8).reshape(180, 320).astype(float)
            base, repaired = first_frame(source), first_frame(folder / "finished.mp4")
            # Linear contrast between original black and white strokes in a repaired area,
            # well away from the new glyph. Limited-range white masks leave ~6–8% ink.
            old, new = base[52:110, 35:95], repaired[52:110, 35:95]
            residual = (new[old > 200].mean() - new[old < 30].mean()) / 255
            self.assertLess(abs(residual), .025)
            self.assertLess(np.abs(base[:14] - repaired[:14]).mean(), 3)

    def test_rotated_vfr_silent_input_and_timed_local_mask(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            # Deliberately irregular PTS, plus display rotation stored as container metadata.
            timing = "settb=1/1000,setpts=if(eq(N\\,0)\\,0\\,if(eq(N\\,1)\\,140\\,if(eq(N\\,2)\\,431\\,900)))"
            ff("-f", "lavfi", "-i", "color=c=white:s=160x96:r=4:d=1",
               "-vf", timing, "-fps_mode", "vfr", "-enc_time_base:v", "1:1000",
               "-c:v", "libx264", "-bf", "0", folder / "flat.mp4")
            source = folder / "rotated.mp4"
            help_text = subprocess.check_output(["ffmpeg", "-h", "full"], stderr=subprocess.DEVNULL, text=True)
            if "-display_rotation" in help_text:
                ff("-display_rotation:v:0", "90", "-i", folder / "flat.mp4", "-c", "copy", source)
            else:
                ff("-i", folder / "flat.mp4", "-c", "copy", "-metadata:s:v:0", "rotate=90", source)
            self.assertEqual(display_size(source, []), (96, 160))
            track = dict(text="فارسی", translation="Go", box=(.2, .15, .7, .42), start=.11, end=.46, background_luma=250)
            render(source, [], folder, screen_tracks=[track])
            result = folder / "finished.mp4"
            self.assertEqual(display_size(result, []), (96, 160))
            self.assertEqual(self.pts(result), self.pts(source))
            types = [s["codec_type"] for s in self.probe(result, "-show_streams")["streams"]]
            self.assertEqual(types, ["video"])
            raw = ff("-i", result, "-map", "0:v:0", "-fps_mode", "passthrough",
                     "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1", stdout=subprocess.PIPE).stdout
            frames = np.frombuffer(raw, np.uint8).reshape(4, 160, 96)
            self.assertTrue(np.all(frames[[0, 3]].mean(axis=(1, 2)) > 245))
            self.assertTrue(np.all((frames[1:3, 28:62, 24:64] < 100).sum(axis=(1, 2)) > 12))
            self.assertTrue(np.all(frames[:, 100:, :].mean(axis=(1, 2)) > 245))
            with patch.dict(os.environ, {"REELDUB_SERVICE_LIMITS": "1"}):
                render(source, [], folder, screen_tracks=[track])
            streams = self.probe(result, "-show_streams")["streams"]
            self.assertEqual([s["codec_name"] for s in streams], ["h264", "aac"])
            self.assertEqual(self.pts(result), self.pts(source))
            original_duration = float(self.probe(source, "-show_format")["format"]["duration"])
            for stream in streams:
                self.assertAlmostEqual(float(stream["duration"]), original_duration, delta=.025)
            pcm = ff("-i", result, "-map", "0:a:0", "-f", "f32le", "pipe:1", stdout=subprocess.PIPE).stdout
            self.assertEqual(np.max(np.abs(np.frombuffer(pcm, np.float32))), 0)

    def test_text_only_keeps_original_audio_and_dub_keeps_switchable_subtitles(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "source.mp4"
            ff("-f", "lavfi", "-i", "color=c=white:s=128x80:r=5:d=1", "-f", "lavfi", "-i",
               "sine=frequency=720:duration=1", "-c:v", "libx264", "-c:a", "aac", source)
            tracks = [dict(text="خوش آمدید", translation="Welcome", start=0, end=.7, box=[.1,.2,.8,.6])]
            render(source, [], folder, screen_tracks=tracks)
            output = folder / "finished.mp4"
            self.assertEqual([s["codec_type"] for s in self.probe(output, "-show_streams")["streams"]],
                             ["video","audio"])
            pcm = ff("-i", output, "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "f32le", "pipe:1",
                     stdout=subprocess.PIPE).stdout
            samples = np.frombuffer(pcm, np.float32)
            spectrum = np.abs(np.fft.rfft(samples[:8000]))
            self.assertAlmostEqual(int(np.argmax(spectrum)), 720, delta=2)
            ff("-f", "lavfi", "-i", "sine=frequency=400:duration=1", folder / "mix.wav")
            render(source, [{"start": .1, "spoken_end": .8, "text": "Hello"}], folder,
                   voice_label="Matched speaker", screen_tracks=tracks)
            streams = self.probe(output, "-show_streams")["streams"]
            self.assertEqual([s["codec_type"] for s in streams], ["video", "audio", "subtitle"])
            self.assertEqual(streams[-1]["codec_name"], "mov_text")


if __name__ == "__main__":
    unittest.main()
