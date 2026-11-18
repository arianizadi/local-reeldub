# Local ReelDub

Paste a YouTube video or Instagram Reel link and get an English-dubbed MP4. ReelDub downloads the video, identifies speakers, translates what they say, voices each line, and places it on the original timeline. The MP4 includes switchable English captions.

**All AI runs on your computer.** Setup downloads model weights. Link inputs contact the video platform. Once setup finishes, local video files can be dubbed offline.

## Start here

On macOS, Linux, or Windows with WSL2, run:

```bash
git clone https://github.com/arianizadi/local-reeldub.git
cd local-reeldub
./setup.sh
./dub
```

Paste a **single video link** at the prompt. The finished video appears in `output/`. You can also pass a quoted URL or a file directly:

```bash
./dub 'https://www.youtube.com/watch?v=VIDEO_ID'
./dub 'https://www.instagram.com/reel/SHORTCODE/'
./dub ~/Videos/interview.mp4 -o interview-english.mp4
```

Setup uses an isolated Python 3.12 environment and installs a project-local copy of uv when needed. It can install FFmpeg using an existing Homebrew or apt installation. Other systems need `git`, `curl`, `ffmpeg`, and `ffprobe` on PATH. Wait for the first setup to finish before disconnecting. Subsequent runs reuse the downloaded weights.

## Choose a setup

| | Studio | Light |
|---|---|---|
| Good fit | Linux with a recent NVIDIA GPU. Tested on a 16 GB RTX 4070 Ti SUPER. | Macs, CPU-only computers, and smaller configurations |
| Translation | Whisper transcribes in the original language. Qwen3-4B translates groups of lines together to retain context. | Whisper translates directly into English. |
| Voices | Qwen3-TTS samples the original speakers and synthesizes English in their approximate voices. | Kokoro assigns a stock English voice to each detected speaker group. |
| Background | BandIt v2 separates speech, music, and sound effects. Music and effects are mixed under the dub in stereo. | Stereo center cancellation during speech. Mono recordings lose most ambience during these sections. |
| Setup | `./setup.sh --studio` | `./setup.sh --light` |

Automatic setup selects Studio on Linux when the first NVIDIA GPU reports at least 15,000 MiB of total VRAM. Other configurations use Light. Allow about **25 GB of free disk space for a Studio install**, plus room for package caches and each video's temporary PCM audio. Downloads and package caches can increase peak usage. The model stages run sequentially so the GPU can be reused. The GPU profile uses PyTorch CUDA 12.4 wheels and needs a compatible NVIDIA driver. WSL2 needs GPU passthrough for Studio.

If you want a smaller first download or faster CPU translation:

```bash
./setup.sh --light --model small
./dub
```

The default translation model is `large-v3`. Select `small`, `medium`, or `large-v3` during setup to cache a model and make it the default. Presets affect future runs. You can override individual stages on an installed Studio system:

```bash
./dub interview.mp4 --voice-mode preset
./dub interview.mp4 --translation whisper
./dub interview.mp4 --background stereo
./dub interview.mp4 --language es -o interview-en.mp4
```

## How the cut is made

1. **Fetch and decode.** yt-dlp fetches the video. The Light environment includes Deno and yt-dlp's JavaScript components for YouTube. FFmpeg reads the first audio track and creates a stereo timeline.
2. **Find the speech.** Studio uses the estimated dialogue stem for recognition and speaker analysis. Whisper detects the source language. A segmentation model and speaker embeddings group recurring voices within the video.
3. **Translate in context.** Studio sends the source-language cues, speaker IDs, and available speaking times to the local translation model. It processes long scripts in batches with neighboring dialogue as context.
4. **Build the voices.** Studio selects relatively long, solo passages per speaker and builds a reusable voice prompt. Voice samples stay inside the temporary job directory. The Light profile works with assigned preset voices.
5. **Mix and export.** Each English line starts on the source timeline. Longer lines are sped up with pitch-preserving time compression. During the dubbed lines, Studio uses the estimated music and effects bed. Between lines, ReelDub returns to the original mix. FFmpeg writes H.264 video, AAC audio, and English subtitles to an MP4.

## What to listen for

The easiest material is clear, non-overlapping dialogue. Listen for speaker swaps near interruptions, rushed lines when English takes more syllables, and accent or timbre changes when a speaker has very little isolated speech. Loud crowds, singing, and effects that resemble speech can enter the wrong stem. A finished stereo mix only permits an estimate of its original layers. The video retains its original mouth animation.

**Input and output details**

- ReelDub handles downloadable individual videos and local video files. Platform login requirements, region blocks, and rate limits can prevent a link from downloading. A local copy works through the same dubbing stages.
- If automatic language detection misses a short clip, pass `--language` with the source language code. Whisper uses a primary source language for the video.
- A clip with no detected speech exits with a message. An existing output file is protected. Choose a fresh name with `-o` to render another version.
- AI synthesis and temporary speech samples stay local. Output MP4s, input videos, voice recordings, and model weights are excluded from the source distribution. Use source material and voices you have permission to dub and share.

## Models and credits

- Source recognition and direct translation use [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and [Whisper](https://github.com/openai/whisper).
- Speaker grouping uses [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), a pyannote segmentation model, and WeSpeaker embeddings.
- The local contextual translator is [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507). Voice matching uses the Base model's speaker-embedding mode. [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)
- Preset voices come from [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) through [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx).
- Dialogue, music, and effects separation uses the multilingual [BandIt v2 checkpoint](https://zenodo.org/records/12701995) by Karn Watcharasupat and collaborators, through [bandit-infer](https://github.com/openmirlab/bandit-infer). Its published weights carry CC BY-SA 4.0.
- Downloads and rendering use [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [FFmpeg](https://ffmpeg.org/).

ReelDub's application code is available under the [MIT license](LICENSE). Dependencies and downloaded weights retain their respective licenses.
