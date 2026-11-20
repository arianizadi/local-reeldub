"""GPU worker in an isolated environment for reference-conditioned multilingual speech."""
from pathlib import Path
import json
import os
import sys
from server.progress import report
from server.gpu import stage
from server.languages import LANGUAGES

MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"


def generate_batch(model, folder, lines, indices, language):
    import soundfile as sf

    prompts, texts = [], []
    for i in indices:
        ref, rate = sf.read(folder / f"reference-{i}.wav", dtype="float32")
        # Each line keeps its own speaker reference, even in a multi-speaker batch.
        # A reference transcript can leak source words into a cross-language dub.
        prompts.extend(model.create_voice_clone_prompt(ref_audio=(ref, rate), x_vector_only_mode=True))
        texts.append(lines[i]["text"])
    waveforms, rate = model.generate_voice_clone(text=texts, language=[language] * len(texts),
                                                voice_clone_prompt=prompts)
    if len(waveforms) != len(indices):
        raise RuntimeError("Voice model returned an incomplete batch.")
    for i, waveform in zip(indices, waveforms):
        sf.write(folder / f"{i}.wav", waveform, rate)
    report(completed=indices[-1] + 1, total=len(lines))


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

    folder = Path(sys.argv[1])
    language = LANGUAGES[sys.argv[2] if len(sys.argv) > 2 else "en"].name
    batch_size = int(os.getenv("REELDUB_TTS_BATCH_SIZE", "1"))
    if batch_size not in (1, 2):
        raise ValueError("REELDUB_TTS_BATCH_SIZE must be 1 or 2.")
    model = Qwen3TTSModel.from_pretrained(weights, device_map="cuda:0",
                                         dtype=torch.bfloat16, attn_implementation="sdpa")
    lines = json.loads((folder / "lines.json").read_text())
    start = 0
    while start < len(lines):
        indices = range(start, min(start + batch_size, len(lines)))
        try:
            generate_batch(model, folder, lines, indices, language)
        except torch.cuda.OutOfMemoryError:
            if len(indices) == 1:
                raise
            batch_size = 1
        else:
            start = indices.stop
            continue
        # Retry the same lines after releasing the failed batch's tensors and traceback.
        torch.cuda.empty_cache()


if __name__ == "__main__":
    if sys.argv[1] == "--prepare":
        main()
    else:
        with stage("voice"):
            print("GPU stage: Voicing", flush=True)
            main()
