<img src="ios/Art/reeldub-mark.svg" width="72" height="72" alt="ReelDub app icon">

# ReelDub

Paste an Instagram Reel link and get a dubbed MP4 in your chosen language. ReelDub downloads the video, identifies speakers, translates what they say, voices each line, and places it on the original timeline. The MP4 includes switchable captions in the output language.

**All AI runs on your computer.** Setup downloads model weights. Link inputs contact the video platform. Once setup finishes, local video files can be dubbed offline.

On iPhone, open **Dubs** to download finished videos for offline playback and sharing. **Select** lets you delete several finished or failed dubs together. After the first device confirms its download, the server keeps a copy for 24 hours. Requesting the same Reel again within that window renews it and uses no additional translation credit. Choose **Remove download from this device** to free space offline. The same option works on multiple selections. The app remembers the decision until you choose **Download to this device**. Deleting the library entry revokes access while its private cache expires on schedule.

The Creator review build accepts private MP4/MOV uploads from **Dubs → + → Photos or Files** and Instagram Reel links from the share sheet. Select the output languages, review the server’s duration quote, then confirm. Free includes five single-language dubs of up to 60 seconds a day. Creator adds 120 minutes per billing month and priority. A one-time pack adds 15 minutes. Minutes are metered by the second. Self-hosted processing is unlimited. See [the product guide](PRODUCT.md) for setup and [the hosted database setup](HOSTING.md) for the Neon deployment.


## Use ReelDub on iPhone

On first launch, tap **Continue with Apple** to create an account or sign in to the preconfigured ReelDub Cloud service. The same button handles returning users. In **Use my own server**, you can test your address and connection key for free. The official iOS app unlocks private-server connections for a one-time purchase ($4.99 in the U.S.). On a new phone, sign in to the same App Store account, tap **Restore Purchases**, and enter your server details. The server and CLI are open source.

ReelDub Cloud uses **Best** for new dubs from the app and share sheet. Best translates dialogue in context and matches each speaker’s voice when the output language supports it. Hindi uses a preset voice.

Share an Instagram Reel up to two minutes long to **ReelDub**, or paste its link in the library. Choose the output language and confirm. Follow its place in the queue, the current render stage and elapsed time. Use the speaker button under the player to listen in Silent Mode or mute a dub. Captions start on when available, and ReelDub remembers both playback controls. Shared MP4 files include a selectable subtitle track. If a translation misses the meaning, open **Report an issue** on that dub and include a timestamp.

**Help & FAQ** is available before sign-in and in Account. Signed-in Cloud users can suggest other types of videos from that screen. Their latest answer appears in the administrator’s account detail view.

The library groups jobs under **Processing**, **Queued**, **Ready**, and **Failed**. Swipe left on a ready or failed dub, or use its **•••** menu, then choose **Delete dub** and confirm. This removes the library entry, hosted video, poster, and attached issue report. Copies exported to Photos or Files are separate.

Start your own server with Docker Compose v2:

```bash
git clone https://github.com/arianizadi/local-reeldub.git
cd local-reeldub
./start-local.sh
curl http://127.0.0.1:8300/health
```

Setup generates a private `.env`, starts MongoDB and the API, and downloads the Light worker models on first use. Follow the first download with `docker compose logs -f worker`. Open `ios/ReelDub.xcodeproj` in Xcode and run **ReelDub** on an iPhone simulator. In **Use my own server**, enter `http://127.0.0.1:8300` and your private `SELF_HOST_KEY` from `.env`. That key has unlimited access. ReelDub Cloud uses individual Apple sessions.

A physical phone needs a reachable HTTPS address for the API. TestFlight delivers the app over the internet. A private network such as NetBird can carry API traffic to your home server. For the Studio GPU worker, remote nodes, Apple account setup and private TestFlight distribution, see the [operator and product guide](PRODUCT.md).

The hosted owner can open **Account → Admin console** to inspect requests and reports, review allowances, suspend accounts and compare fresh dubs in the render lab. See the [hosted console setup and GPU timings](HOSTING.md#hosted-administrator-console).

## Use the standalone command line

On macOS, Linux, or Windows with WSL2, run:

```bash
git clone https://github.com/arianizadi/local-reeldub.git
cd local-reeldub
./setup.sh
./dub
```

Paste a **single video link** at the prompt. The finished video appears in `output/`. You can also pass a quoted URL or a file directly:

```bash
./dub 'https://www.instagram.com/reel/SHORTCODE/'
./dub ~/Videos/interview.mp4 -o interview-english.mp4
```

Setup uses an isolated Python 3.12 environment and installs a project-local copy of uv when needed. It can install FFmpeg using an existing Homebrew or apt installation. Other systems need `git`, `curl`, `ffmpeg`, and `ffprobe` on PATH. Wait for the first setup to finish before disconnecting. Subsequent runs reuse the downloaded weights.

## Choose a setup

| | Studio | Light |
|---|---|---|
| Good fit | Linux with a recent NVIDIA GPU. Tested on a 16 GB RTX 4070 Ti SUPER. | Macs, CPU-only computers, and smaller configurations |
| Translation | Whisper transcribes in the original language. A 4-bit Gemma 3 12B translates groups of lines together to retain context. | Whisper translates directly into English. |
| Voices | Qwen3-TTS samples the speakers and synthesizes the chosen supported language in their approximate voices. Hindi uses a local preset. | Kokoro assigns a stock English voice to each detected speaker group. |
| Background | BandIt v2 separates speech, music, and sound effects. Music and effects are used continuously under the dub in stereo. | Continuous stereo center cancellation. Mono recordings yield a voice-only dub. |
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

## Output languages

Studio offers matched voices in English, Mandarin Chinese, French, German, Italian, Japanese, Korean, Portuguese, Russian and Spanish. Hindi uses a local preset voice. The Light profile translates to English. The server advertises its output languages to the iPhone app and share extension. Both remember your preferred language. A new language for the same Reel is a new translation, and each language has its own cache entry. Earlier dubs and links sent by older apps default to English.

For the CLI, `--target-language` sets the voice and subtitle language. `--language` optionally overrides source detection. For example:

```bash
./dub interview.mp4 --target-language es
./dub interview.mp4 --language fa --target-language ja
```

[Qwen multilingual voice model](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base) · [Kokoro preset voices](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)

## How the cut is made

1. **Fetch and decode.** yt-dlp fetches the video. Only individual Instagram Reel permalinks are accepted. FFmpeg reads the first audio track and creates a stereo timeline.
2. **Find the speech.** Whisper listens to the full mix, including vocalizations outside ordinary speech. Studio uses the isolated dialogue stem for speaker analysis. Whisper detects the source language. A segmentation model and speaker embeddings group recurring voices within the video.
3. **Translate in context.** Studio sends the original speaker turns to the local translation model. It processes long scripts in batches in scene order.
4. **Build the voices.** Studio samples the original utterance for each line, avoids detected overlap where possible, and extends very short references within the same speaker turn. This helps when the same character changes delivery between scenes. Voice samples stay inside the temporary job directory. The Light profile works with assigned preset voices.
5. **Mix and export.** Each translated line starts on the source timeline. Longer lines are sped up with pitch-preserving time compression. Studio builds one full-length soundtrack from the separated music and effects plus synthesized speech. The same mix runs through pauses and the last frame. Studio checks the background for residual speech. Where voices persist, a second local separator removes estimated vocals and fades back into the original music and effects. A missed vocalization may be absent, and quiet overlapping voices can still leak through. FFmpeg writes H.264 video, AAC audio, and subtitles in the selected language to an MP4.

## What to listen for

The easiest material is clear, non-overlapping dialogue. Listen for speaker swaps near interruptions, rushed lines when the translation takes more syllables, and accent or timbre changes when a speaker has very little isolated speech. Loud crowds, singing, and effects that resemble speech can enter the wrong stem. A finished stereo mix only permits an estimate of its original layers. The video retains its original mouth animation.

**Input and output details**

- ReelDub handles downloadable individual videos and local video files. Platform login requirements, region blocks, and rate limits can prevent a link from downloading. A local copy works through the same dubbing stages.
- If automatic language detection misses a short clip, pass `--language` with the source language code. Whisper uses a primary source language for the video.
- A clip with no detected speech exits with a message. An existing output file is protected. Choose a fresh name with `-o` to render another version.
- AI synthesis and temporary speech samples stay local. Output MP4s, input videos, voice recordings, and model weights are excluded from the source distribution. Use source material and voices you have permission to dub and share.

## Models and credits

- Source recognition and direct translation use [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and [Whisper](https://github.com/openai/whisper).
- Speaker grouping uses [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), a pyannote segmentation model, and WeSpeaker embeddings.
- The local contextual translator is [Gemma 3 12B](https://huggingface.co/google/gemma-3-12b-it), loaded from the [4-bit checkpoint](https://huggingface.co/unsloth/gemma-3-12b-it-bnb-4bit). Voice matching uses the Base model's speaker-embedding mode. [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)
- Preset voices come from [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) through [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx).
- Dialogue, music, and effects separation uses the multilingual [BandIt v2 checkpoint](https://zenodo.org/records/12701995) by Karn Watcharasupat and collaborators, through [bandit-infer](https://github.com/openmirlab/bandit-infer). Its published weights carry CC BY-SA 4.0.
- Studio checks the background for both acoustic speech and words that match the time-aligned original transcript. It uses Hybrid Demucs on flagged intervals and fades the background out through a confirmed source line when speech persists. This also covers slowed voices that outlast their translation. [TorchAudio Hybrid Demucs](https://docs.pytorch.org/audio/2.6/tutorials/hybrid_demucs_tutorial.html)
- On Linux x86-64 with NVIDIA CUDA 12.8, `./setup.sh --studio --index` adds Index-Translate-9B Q5_K_M. Each render runs it locally over a private Unix socket and validates the number of translated lines. Gemma handles failures. The speech and subtitle language options still follow the installed voice models. [Index-Translate model card](https://huggingface.co/IndexTeam/Index-Translate-9B)
- Downloads and rendering use [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [FFmpeg](https://ffmpeg.org/).

ReelDub's application code is available under the [MIT license](LICENSE). Dependencies and downloaded weights retain their respective licenses.
