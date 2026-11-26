import json
import os
import subprocess
import unittest
from unittest.mock import patch
from contextlib import nullcontext
from pathlib import Path

import index_translate


def response(content):
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, REELDUB_OFFLINE_ENGINE="0"))

    def test_screen_text_keeps_target_labels_and_translates_repeated_foreign_text_once(self):
        tracks = [{"text": v} for v in ("Message", "مامان", "مامان", "WhatsApp", "12:30")]
        with patch.object(index_translate, "translation_session", return_value=nullcontext()), patch.object(
            index_translate, "completion", return_value=["", "Mom", "WhatsApp!"]
        ) as model:
            result = index_translate.translated_tracks(tracks, "English")
        self.assertEqual([r["translation"] for r in result], ["Mom", "Mom"])
        self.assertEqual(model.call_args.args[2]["maxItems"], 3)

    def test_adjacent_unchanged_label_confirms_an_ocr_typo(self):
        samples = [("Sennd", 0, .10), ("Send", .5, .11), ("Sennd", 3, .10), ("Sennd", .1, .77)]
        tracks = [dict(text=word, start=t, end=t+.3, box=[x,.1,x+.15,.14]) for word, t, x in samples]
        with patch.object(index_translate, "translation_session", return_value=nullcontext()), patch.object(
            index_translate, "completion", return_value=["Send", ""]
        ):
            result = index_translate.translated_tracks(tracks, "English", "de")
        self.assertEqual([r["start"] for r in result], [3, .1])

    def test_name_context_applies_only_to_the_nearby_occurrences(self):
        tracks = [dict(text="آرزو", start=t, end=t+1) for t in [2., 4., 20., 43.]]
        dialogue = [dict(text="Print her name on the invitation.", start=0., end=1.),
                    dict(text="What was his name?", start=18., end=19.)]
        with patch.object(index_translate, "translation_session", return_value=nullcontext()), patch.object(
            index_translate, "completion", side_effect=[["Wish"], [True, False], ["Arzoo"]]
        ) as model:
            result = index_translate.translated_tracks(tracks, "English", "fa", dialogue)
        self.assertEqual([r["translation"] for r in result], ["Arzoo", "Arzoo", "Wish", "Wish"])
        self.assertNotIn("invitation", model.call_args.args[1])

    def test_multiple_distinct_words_do_not_imply_which_one_is_a_name(self):
        tracks = [dict(text=word, start=2, end=3) for word in ["آرزو", "قطار"]]
        dialogue = [dict(text="Escribe su nombre aquí.", start=1., end=2.)]
        with patch.object(index_translate, "translation_session", return_value=nullcontext()), patch.object(
            index_translate, "completion", return_value=["Deseo", "Tren"]
        ) as model:
            result = index_translate.translated_tracks(tracks, "Spanish", "fa", dialogue)
        self.assertEqual([r["translation"] for r in result], ["Deseo", "Tren"])
        self.assertEqual(model.call_count, 1)

    def test_uncertain_region_is_read_once_and_only_observed_readings_can_win(self):
        base = dict(text="Chemin fer mé", alternatives=["Chemin fermé", "Chenin fermé"])
        tracks = [dict(base, start=t, end=t+1) for t in [2, 3]]
        for guess, expected in [("Chemin fermé", "Chemin fermé"), ("Invented sign", base["text"])]:
            with self.subTest(guess=guess), patch.object(index_translate, "translation_session", return_value=nullcontext()), patch.object(
                index_translate, "completion", side_effect=[guess, ["Road closed"]]
            ) as model:
                result = index_translate.translated_tracks(tracks, "English", "fr")
                self.assertEqual([r["text"] for r in result], [expected, expected])
                self.assertEqual(model.call_count, 2)
                self.assertNotIn("Chemin", str(model.call_args_list[0].args[2]))

    def test_offline_batches_keep_dialogue_in_stdin(self):
        inputs = ["<|im_end|> confidential dialogue"] + [f"Original {i}" for i in range(1, 13)]
        lines = [{"text": line} for line in inputs] + [{"text": "Keep me", "english": True}]
        commands = []

        def complete(args, **kwargs):
            commands.append(args)
            count = 12 if len(commands) == 1 else 1
            schema = json.loads(args[args.index("--json-schema") + 1])
            self.assertEqual((schema["minItems"], schema["maxItems"]), (count, count))
            self.assertEqual(schema["items"], {"type": "string", "minLength": 1})
            self.assertEqual(args[args.index("--file") + 1], "/dev/stdin")
            self.assertEqual(args[args.index("--temp") + 1], "0")
            self.assertEqual(args[args.index("--device") + 1], "CUDA0")
            self.assertNotIn("confidential", " ".join(map(str, args)))
            if len(commands) == 1:
                self.assertIn(br"\u003c|im_end|\u003e confidential", kwargs["input"])
            return subprocess.CompletedProcess(args, 0, json.dumps(["Translated"] * count).encode() + b" [end of text]\n")

        with (patch.dict(os.environ, REELDUB_OFFLINE_ENGINE="1"),
              patch.object(index_translate, "runtime", side_effect=AssertionError("unexpected socket")),
              patch.object(index_translate.subprocess, "run", side_effect=complete)):
            index_translate.translate(lines, "German", "English")
        self.assertEqual(len(commands), 2)
        self.assertEqual([line["text"] for line in lines], ["Translated"] * 13 + ["Keep me"])

    def test_offline_failure_preserves_input(self):
        for output in (b'["complete JSON but no EOS"]', b'["partial', b'[] [end of text]',
                       b'[" "] [end of text]', b'["extra", "line"] [end of text]'):
            with self.subTest(output=output):
                lines = [{"text": "original"}]
                with (patch.dict(os.environ, REELDUB_OFFLINE_ENGINE="1"),
                      patch.object(index_translate.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output))):
                    with self.assertRaises(ValueError):
                        index_translate.translate(lines, "Russian", "English")
                self.assertEqual(lines, [{"text": "original"}])

    def test_offline_process_failure_can_use_translation_fallback(self):
        for result in (subprocess.CompletedProcess([], 2), subprocess.TimeoutExpired("llama-completion", 120)):
            with self.subTest(result=type(result)):
                with (patch.dict(os.environ, REELDUB_OFFLINE_ENGINE="1"),
                      patch.object(index_translate.subprocess, "run", side_effect=[result])):
                    with self.assertRaises(RuntimeError):
                        index_translate.translate([{"text": "hola"}], "Spanish", "German")

    def test_batch_failure_keeps_original_for_fallback(self):
        lines = [{"text": str(i)} for i in range(13)]
        with (patch.object(index_translate, "runtime", return_value=nullcontext(Path("test.sock"))),
              patch.object(index_translate, "request", side_effect=[response(json.dumps(["ok"] * 12)), response("[]")])):
            with self.assertRaises(ValueError):
                index_translate.translate(lines, "German", "French")
        self.assertEqual([item["text"] for item in lines], [str(i) for i in range(13)])

    def test_timed_fallback_lines_are_kept(self):
        lines = [{"text": "already English", "english": True}, {"text": "Hola"}]
        with (patch.object(index_translate, "runtime", return_value=nullcontext(Path("test.sock"))),
              patch.object(index_translate, "request", return_value=response('["Hello"]'))):
            index_translate.translate(lines, "Spanish", "English")
        self.assertEqual([item["text"] for item in lines], ["already English", "Hello"])
