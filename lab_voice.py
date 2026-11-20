"""Optional GPU voice backends for operator experiments. All references are local files."""

import json
import os
from pathlib import Path
import sys

from server.gpu import stage
from server.languages import LANGUAGES
from server.progress import report


def omni_voice(folder, model, lines, language, batch):
    import soundfile as sf
    import torch
    import yaml
    import vllm_omni
    from transformers import AutoTokenizer
    from vllm_omni import Omni
    from vllm_omni.model_executor.models.qwen3_tts.configuration_qwen3_tts import (
        Qwen3TTSConfig,
    )
    from vllm_omni.model_executor.models.qwen3_tts.qwen3_tts_talker import (
        Qwen3TTSTalkerForConditionalGeneration as Talker,
    )

    cfg_path = (
        Path(vllm_omni.__file__).parent
        / "model_executor/stage_configs/qwen3_tts_batch.yaml"
    )
    config = yaml.safe_load(cfg_path.read_text())
    for i, item in enumerate(config["stage_args"]):
        item["engine_args"].update(
            gpu_memory_utilization=0.57 if i == 0 else 0.20,
            max_num_seqs=4,
            max_num_batched_tokens=1024,
            max_model_len=1024,
        )
        item["runtime"]["max_batch_size"] = 4
    path = folder / "omni.yaml"
    path.write_text(yaml.safe_dump(config))
    tokenizer = AutoTokenizer.from_pretrained(
        model, local_files_only=True, padding_side="left"
    )
    talker = Qwen3TTSConfig.from_pretrained(model, local_files_only=True).talker_config
    engine = Omni(model=model, stage_configs_path=str(path), stage_init_timeout=180)
    try:
        for start in range(0, len(lines), batch):
            prompts = []
            for i in range(start, min(start + batch, len(lines))):
                info = {
                    "task_type": ["Base"],
                    "text": [lines[i]["text"]],
                    "language": [language],
                    "ref_audio": [str(folder / f"reference-{i}.wav")],
                    "x_vector_only_mode": [True],
                    "max_new_tokens": [min(768, max(128, len(lines[i]["text"]) * 4))],
                }
                size = Talker.estimate_prompt_len_from_additional_information(
                    additional_information=info,
                    task_type="Base",
                    tokenize_prompt=lambda t: tokenizer(t, padding=False)["input_ids"],
                    codec_language_id=talker.codec_language_id,
                    spk_is_dialect=talker.spk_is_dialect,
                )
                prompts.append(
                    {"prompt_token_ids": [0] * size, "additional_information": info}
                )
            remaining = set(range(len(prompts)))
            for group in engine.generate(prompts, use_tqdm=False):
                for out in group.request_output:
                    index = int(out.request_id.split("_", 1)[0])
                    remaining.remove(index)
                    mm = out.outputs[0].multimodal_output
                    data, rate = mm["audio"], mm["sr"]
                    data = torch.cat(data, dim=-1) if isinstance(data, list) else data
                    rate = rate[-1] if isinstance(rate, list) else rate
                    sf.write(
                        folder / f"{start + index}.wav",
                        data.float().cpu().numpy().reshape(-1),
                        int(rate),
                    )
            if remaining:
                raise RuntimeError("Incomplete vLLM voice batch.")
            report(completed=start + len(prompts), total=len(lines))
    finally:
        engine.close()


def graph_voice(folder, model, lines, language):
    import soundfile as sf
    from faster_qwen3_tts import FasterQwen3TTS

    engine = FasterQwen3TTS.from_pretrained(model, max_seq_len=2048)
    for i, line in enumerate(lines):
        waveforms, rate = engine.generate_voice_clone(
            line["text"],
            language,
            str(folder / f"reference-{i}.wav"),
            "",
            xvec_only=True,
        )
        sf.write(folder / f"{i}.wav", waveforms[0], rate)
        report(completed=i + 1, total=len(lines))


if __name__ == "__main__":
    os.environ.update(
        HF_HUB_OFFLINE="1",
        VLLM_NO_USAGE_STATS="1",
        VLLM_WORKER_MULTIPROC_METHOD="spawn",
    )
    from huggingface_hub import snapshot_download

    model = snapshot_download("Qwen/Qwen3-TTS-12Hz-1.7B-Base", local_files_only=True)
    folder = Path(sys.argv[1]).resolve()
    lines = json.loads((folder / "lines.json").read_text())
    with stage("exclusive"):
        print("GPU stage: Voicing", flush=True)
        if sys.argv[3] == "omni":
            omni_voice(
                folder,
                model,
                lines,
                LANGUAGES[sys.argv[2]].name,
                int(os.environ.get("REELDUB_TTS_BATCH_SIZE", "2")),
            )
        elif sys.argv[3] == "graph":
            graph_voice(folder, model, lines, LANGUAGES[sys.argv[2]].name)
        else:
            raise ValueError("Unsupported lab runtime.")
