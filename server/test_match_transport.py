import fcntl
import json
import os
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import warnings
import wave
import zipfile


RUNNER = Path(__file__).resolve().parents[1] / "deploy/fleet/match-runner.py"


class SpeakerMatchTransportTests(TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        with patch.dict(os.environ, {"REELDUB_KITTEN_MATCH_SPOOL": str(self.root)}):
            self.runner = SimpleNamespace(**runpy.run_path(str(RUNNER)))

    def test_two_cues_round_trip_with_private_fields_removed(self):
        source, model, result = (self.root / name for name in ("source", "model", "result"))
        for folder in (source, model, result):
            folder.mkdir()
        rows = [{"text": "One", "reference_text": "Uno"}, {"text": "Two", "reference_text": "Dos"}]
        for i in range(2):
            with wave.open(str(source / f"reference-{i}.wav"), "wb") as wav:
                wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                wav.writeframes((i + 1).to_bytes(2, "little") * 240)
        request = self.root / "speech.req"
        self.runner.bundle(request, source, [dict(row, source_id="local") for row in rows], True)
        self.assertEqual(self.runner.unpack(request, model), rows)
        self.assertEqual(json.loads((model / "lines.json").read_text()), rows)

        for i in range(2):
            (model / f"{i}.wav").write_bytes((model / f"reference-{i}.wav").read_bytes())
        response = self.root / "speech.done"
        self.runner.bundle(response, model, rows, False)
        self.runner.unpack(response, result, rows)
        self.assertEqual({p.name for p in result.iterdir()}, {"0.wav", "1.wav"})
        for i in range(2):
            self.assertEqual((result / f"{i}.wav").read_bytes(), (source / f"reference-{i}.wav").read_bytes())

    def test_invalid_archive_members_are_rejected_before_writing(self):
        target = self.root / "target"
        target.mkdir()
        script = json.dumps([{"text": "One", "reference_text": "Uno"}]).encode()
        cases = [
            ("duplicate", script, ["reference-0.wav", "reference-0.wav"], "Incomplete speaker audio bundle"),
            ("outside directory", script, ["../reference-0.wav"], "Incomplete speaker audio bundle"),
            ("oversized script", b" " * (1 << 20) + script, ["reference-0.wav"], "Speaker script exceeds"),
        ]
        for label, document, names, message in cases:
            with self.subTest(label=label):
                path = self.root / "speech.req"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("lines.json", document)
                    with warnings.catch_warnings():
                        warnings.filterwarnings("ignore", message="Duplicate name:", category=UserWarning)
                        for name in names:
                            archive.writestr(name, b"")
                with self.assertRaisesRegex(ValueError, message):
                    self.runner.unpack(path, target)
                self.assertEqual(list(target.iterdir()), [])
                self.assertFalse((self.root / "reference-0.wav").exists())

    def test_lease_is_active_only_while_held(self):
        key = "a" * 32
        lease = self.root / (key + ".lease")
        self.assertFalse(self.runner.active(key))
        with lease.open("xb") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            self.assertTrue(self.runner.active(key))
        self.assertTrue(lease.is_file())
        self.assertFalse(self.runner.active(key))
