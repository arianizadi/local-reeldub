"""Localize a short scene at a time using its source-language dialogue."""
import json
from pathlib import Path
import re
import sys

MODEL = "unsloth/gemma-3-12b-it-bnb-4bit"


def main():
    from huggingface_hub import snapshot_download
    prepare = sys.argv[1] == "--prepare"
    weights = snapshot_download(MODEL, local_files_only=not prepare)
    if prepare:
        return
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from transformers.models.whisper.tokenization_whisper import LANGUAGES
    import torch

    tokenizer = AutoTokenizer.from_pretrained(weights)
    model = AutoModelForCausalLM.from_pretrained(weights, dtype=torch.bfloat16, device_map="cuda:0")
    path = Path(sys.argv[1]) / "lines.json"
    lines = json.loads(path.read_text())
    lang = LANGUAGES.get(sys.argv[2], sys.argv[2]).title()

    def translate(batch):
        numbered = "\n".join(f"{i}. {line['text']}" for i, line in enumerate(batch, 1))
        prompt = (f"Translate this {lang} dialogue into natural spoken English. It was transcribed by ear; "
                  "resolve homophones using the whole conversation. Preserve the literal objects, slang, "
                  "vulgarity, negation and joke. Return one numbered English line per original line, "
                  "in order, with no commentary.\n\n" + numbered)
        ids = tokenizer.apply_chat_template([{"role": "user", "content": prompt}], tokenize=True,
                                            add_generation_prompt=True, return_tensors="pt").to(model.device)
        with torch.inference_mode():
            out = model.generate(ids, max_new_tokens=120 * len(batch) + 64, do_sample=False)
        response = tokenizer.decode(out[0, ids.shape[-1]:], skip_special_tokens=True)
        matches = re.findall(r"^\s*(\d+)[.)]\s*(\S[^\n]*)", response, flags=re.MULTILINE)
        if [int(n) for n, _ in matches] == list(range(1, len(batch) + 1)):
            for line, (_, text) in zip(batch, matches):
                line["text"] = text.strip()
        elif len(batch) == 1 and response.strip() and "\n" not in response.strip():
            batch[0]["text"] = response.strip()
        elif len(batch) > 1:
            pivot = len(batch) // 2
            translate(batch[:pivot])
            translate(batch[pivot:])
        else:
            raise ValueError("The model returned an incomplete translation. Try --translation whisper.")

    pending = [line for line in lines if not line.get("english")]
    for start in range(0, len(pending), 12):
        translate(pending[start:start + 12])
    path.write_text(json.dumps(lines, ensure_ascii=False))


if __name__ == "__main__":
    main()

