"""GPU worker in an isolated environment for reference-conditioned multilingual speech."""
from pathlib import Path
import json
import os
import sys
from server.progress import report
from server.gpu import stage
from server.languages import LANGUAGES

MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"


def main():
    import torch
    from huggingface_hub import snapshot_download

    if not torch.cuda.is_available():
        raise RuntimeError("Voice matching needs a working CUDA GPU.")
    prepare = sys.argv[1] == "--prepare"
    weights = snapshot_download(MODEL, local_files_only=not prepare)
    if prepare:
        return
    os.environ["HF_HUB_OFFLINE"] = "1"
    from qwen_tts import Qwen3TTSModel
    import soundfile as sf

    folder = Path(sys.argv[1])
    language = LANGUAGES[sys.argv[2] if len(sys.argv) > 2 else "en"].name
    model = Qwen3TTSModel.from_pretrained(weights, device_map="cuda:0",
                                         dtype=torch.bfloat16, attn_implementation="sdpa")
    lines = json.loads((folder / "lines.json").read_text())
    for i, line in enumerate(lines):
        ref, rate = sf.read(folder / f"reference-{i}.wav", dtype="float32")
        # Conditioning on the reference transcript can repeat source words in a cross-language dub.
        prompt = model.create_voice_clone_prompt(ref_audio=(ref, rate), x_vector_only_mode=True)
        waveforms, rate = model.generate_voice_clone(text=line["text"], language=language,
                                                    voice_clone_prompt=prompt)
        sf.write(folder / f"{i}.wav", waveforms[0], rate)
        report(completed=i+1, total=len(lines))


if __name__ == "__main__":
    if sys.argv[1] == "--prepare":
        main()
    else:
        with stage("voice"):
            print("GPU stage: Voicing", flush=True)
            main()
