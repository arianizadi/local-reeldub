import json
import unittest
from unittest.mock import patch
from contextlib import nullcontext
from pathlib import Path

import index_translate


def response(content):
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}


class TranslationTests(unittest.TestCase):
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
