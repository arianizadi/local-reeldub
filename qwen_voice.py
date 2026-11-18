"""GPU worker in an isolated environment for reference-conditioned English speech."""
from pathlib import Path
import json
import os
import sys

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
    model = Qwen3TTSModel.from_pretrained(weights, device_map="cuda:0",
                                         dtype=torch.bfloat16, attn_implementation="sdpa")
    for i, line in enumerate(json.loads((folder / "lines.json").read_text())):
        ref, rate = sf.read(folder / f"reference-{i}.wav", dtype="float32")
        prompt = model.create_voice_clone_prompt(ref_audio=(ref, rate), x_vector_only_mode=True)
        waveforms, rate = model.generate_voice_clone(text=line["text"], language="English",
                                                    voice_clone_prompt=prompt)
        sf.write(folder / f"{i}.wav", waveforms[0], rate)


if __name__ == "__main__":
    main()
