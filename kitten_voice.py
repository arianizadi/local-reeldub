"""Render English narration inside the isolated KittenML environment."""
import json
from pathlib import Path
import sys

import numpy as np
import soundfile as sf

from server.progress import report
from server.upload_voice import KITTEN_SNAPSHOT


def prepare():
    from huggingface_hub import hf_hub_download, snapshot_download
    from server.upload_voice import KITTEN_REVISION

    snapshot_download("KittenML/kitten-tts-2", revision=KITTEN_REVISION,
                      allow_patterns=["config.json", "LICENSE.md", "lm/*.json", "lm/*.txt",
                                      "lm/model-ternary.safetensors", "speaker/*", "voices/*"])
    revision = "749d1c1a46eb10492095d68fbcf55691ccf137cd"
    weights = Path(hf_hub_download("ResembleAI/chatterbox-turbo", filename="s3gen_meanflow.safetensors",
                                   revision=revision))
    # The SDK requests this decoder at main. Our private offline cache must resolve
    # that name to the same immutable revision we just downloaded.
    cache = weights.parents[2]
    (cache / "refs").mkdir(exist_ok=True)
    (cache / "refs/main").write_text(revision)


def render(folder):
    from kittenml import KittenTTS

    texts = json.loads((folder / "lines.json").read_text())
    model = KittenTTS(str(KITTEN_SNAPSHOT), device="cuda", weights="packed", decoder="default")
    for i, text in enumerate(texts):
        audio = model.generate(text, voice="Bella", preset="stable", advanced={"seed": 9261 + i})
        if not len(audio) or not np.isfinite(audio).all():
            raise ValueError("KittenTTS returned an invalid audio segment.")
        sf.write(folder / f"{i}.wav", audio, model.sample_rate)
        report(completed=i + 1, total=len(texts))


if __name__ == "__main__":
    if sys.argv[1:] == ["--prepare"]:
        prepare()
    else:
        render(Path(sys.argv[1]))
