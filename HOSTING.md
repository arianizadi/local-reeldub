# ReelDub Cloud hosting

ReelDub Cloud keeps account, billing and queue metadata on **Neon Postgres Free, AWS Oregon**. The iOS app connects to the HTTPS API. The devbox runs the HTTPS origin, API, isolated media intake, GPU worker and private GridFS cache. A shared Cloudflare gateway on Proxmox forwards the single ReelDub origin to the devbox over SSH. Uploaded originals, rendered videos and poster images stay in the private cache. Self-hosters can use the bundled MongoDB configuration.

## Storage and billing controls

The hosted beta uses 0.25–0.5 CU with five-minute suspension. Neon Free includes 100 CU-hours and 5 GB of public network transfer per project each month, 1 GB of database storage and up to six hours of restore history. Storage carries over between months. Monitor the project totals in Neon. [Neon plans](https://neon.com/docs/introduction/plans)

Workers watch a notification in the private media cache before and after queue admission. An hourly queue check recovers missed notifications and coincides with cleanup. The API instances share a cleanup lease. Short-lived Apple sign-in challenges use the private cache. Account sessions, quotas and purchases stay in Postgres. Use the public `/live` endpoint for uptime monitoring and the private API’s `/health` endpoint for an on-demand database check.

On the devbox the API and workers share a private `REELDUB_WAKE_FILE`. A submission wakes idle workers immediately. Each worker checks the cache once a minute as a fallback, with earlier checks for outstanding admissions or expiring leases. Deployments without this shared Linux file check every five seconds. The foreground iOS library requests only the active dubs every five seconds during rendering or ten seconds while queued. Completion triggers a full library and balance refresh. Idle and background libraries stop polling.

### Measuring performance

The Studio GPU worker accepts `REELDUB_TTS_BATCH_SIZE=2` to synthesize two lines together. The default is one line. Each line uses its own reference audio. If a pair runs out of CUDA memory the worker retries those lines individually for the rest of the job. Compare voice similarity, timing and peak GPU memory under load before enabling pairs on a hosted worker.

Use the Neon dashboard for billed compute and transfer. For changes to API queries, enable `pg_stat_statements` in a disposable Postgres instance and compare statement calls and shared-buffer reads for the same requests and data. `EXPLAIN (ANALYZE, BUFFERS)` shows whether a running dub needs a queue scan. The integration tests in `server/test_idle.py` also check the connection-checkout budget for authenticated progress and account reads.

On the devbox, use `pidstat` on the worker processes for CPU usage and `nvidia-smi` for GPU load and power. Xcode Instruments’ Network template can measure iPhone request counts and transfer sizes during a render or after the library goes idle.

Set `REELDUB_STORE_STORAGE_LIMIT_BYTES` to the database budget. The free deployment uses 1,000,000,000 bytes. New checkout requires an authenticated, committed purchase intent. Checkout pauses when database usage reaches 80% of the lower of the configured budget and the database provider’s advertised cluster limit, when a write fails, or when billing is disabled. Keep this production project on one branch or account for additional branches in the budget. Verified Apple transactions, refunds and renewals can still be delivered after admission closes. The iOS client finishes transactions after server delivery and retries unfinished purchases. Apple controls renewals and final payment timing.

## Beta tester allowances

With operator database credentials, run `python -m server.admin beta-allowance tester@example.org 50` to reserve a daily allowance. Invite the tester through TestFlight and ask them to choose **Share My Email** with that address when signing in to ReelDub. The first matching verified Apple sign-in claims the allowance. Account settings show daily use and the limit. It resets at midnight UTC and gives standard queue access. Run the command again to update the allowance for that tester.

## Deployment and recovery

1. Provision a dedicated Postgres database. Run `python -m server.migrate_postgres` with `REELDUB_MIGRATION_DSN_FILE` pointing to a private owner connection file. The schema lives in `reeldub`. The API role needs schema usage and CRUD on the metadata tables in `server.postgres.TABLES`. The worker needs schema usage, SELECT and UPDATE(doc) on `reeldub.jobs` only. Uniqueness is enforced by SQL indexes and atomic credit and request-key triggers.
2. Set `REELDUB_API_DSN_FILE` and `REELDUB_WORKER_DSN_FILE` for the Compose secrets in `compose.neon.yaml`. Use the overlay after the hosted and TLS overlays. The local worker container is opt-in via the `local-worker` Compose profile. Remote workers support `REELDUB_POSTGRES_DSN_FILE` or a private systemd environment file. Mount only each service's own credential. TLS verifies the server hostname and CA. The render subprocess receives a restricted environment.
3. Before migration, drain the queue, freeze both APIs and stop the workers. Back up the private Mongo database and restore it on a disposable instance. With the same media connection available, run the migration command with `--metadata`. It checks media references, writes posters into the private cache, imports the metadata in one SQL transaction, and compares a checksum for every table. It stops on unknown collections or different destination records. Apple subjects, session hashes, library identifiers and job IDs retain their original values.
4. Start the API and worker together. Verify admin entitlement, an existing library item, a new render and owner-only playback. After new writes land on Neon, recover from a current Neon backup. The frozen Mongo archive is a pre-cutover recovery point.

Live purchases and public uploads remain disabled on the review deployment.


## Linux devbox deployment

The hosted services bind to loopback. The API and worker have separate Neon roles with verified TLS. MongoDB holds media on a gocryptfs mount. The vault opens using a TPM-sealed credential bound to PCR 7. Keep a separate recovery key offline before changing Secure Boot or replacing the motherboard. Render intermediates use private tmpfs. The user systemd manager has lingering enabled so the stack starts without an interactive login.

On Ubuntu 24.04 x86-64, `deploy/devbox/prepare-runtime.sh` installs the cached service, sandbox and encryption binaries in the operator’s home. The native service installer in `deploy/devbox/install-services.sh` generates the user units after the private vault, credentials, programs and Studio models have been provisioned. The Cloudflare gateway allows the service account to forward only its ReelDub loopback origin. Local Compose remains available for a new self-hosted installation.

Two pipeline slots share this 16 GiB GPU. A priority job keeps its FIFO position among other priority jobs. Free jobs normally run the same models on the GPU. Set `REELDUB_FREE_CPU_OVERFLOW=1` in the worker environment to allow one free job to transcribe on six CPU threads when the GPU lanes are busy. Separation, Studio translation and matched speech still run on the GPU. A running job holds its slot through encoding.

Running jobs report stage milestones and completed voice lines. Estimated finish times use up to seven recent successful renders of comparable length, preferring the same output language. A job with fewer than three comparable samples reports progress without a time estimate. Queue position and time spent waiting are tracked separately.

The Ubuntu 24.04 native intake uses Landlock ABI 6 or later, the distro Python seccomp bindings, TCP DNS and systemd address-family restrictions. Intake can read its packaged code and write its private runtime folder. The kernel denies pathname Unix sockets and datagram sockets. The API, operator endpoint and worker each get their own mode-0700 runtime directory, removed when their service stops. The foreground vault process and its dependent services are supervised by systemd.

New dubs on the hosted Studio worker use the **Best** preset across both queue tiers. Best uses Whisper large-v3, context translation, and Qwen3-TTS 1.7B for matched voices. On the hosted devbox, Index-Translate-9B Q5_K_M handles translation with Gemma 3 12B available as a fallback. Hindi uses Kokoro. Each new completed job records the actual models it used. On the Ryzen 9 5900X, using six CPU threads, native transcription of 13.4 seconds of Persian took 11.9 seconds on CPU and 1.1 seconds on the RTX. A 40.0-second Russian clip took 38.7 and 4.3 seconds respectively. These timings exclude model loading and the rest of the render.

### Hosted administrator console

In the iOS app, open **Dubs → Account → Admin console** from the library menu. The hosted API requires an active Apple session whose subject matches `ADMIN_APPLE_SUB`. Use Apple again to unlock the console. Every dashboard, moderation and experiment request checks the session and a separate, session-bound 15-minute grant. The app holds that grant in memory. A self-host key, beta review key, plan assignment or matching email cannot unlock the console.

Overview groups retained requests by status, language pair and render mode and shows measured cache reuse, queue wait and render time. Cache-hit counting starts with this version. Pull to refresh. Accounts, requests and reports load in pages of 40. Open an account to see its effective allowance and request history. A ban stops hosted access and pending work and records the reason. Account deletion remains available to suspended users.

The render lab queues a fresh test with its own settings. Best uses context translation and a matched voice when supported. Medium keeps context translation and chooses a preset voice where one is available. Fast uses greedy speech decoding and, for English, direct Whisper translation. All three keep source-dialogue cleanup. Paired decoding keeps a separate voice reference for each line. Experiments participate in the regular priority queue and have separate cache identities.

On a native Studio worker, run `./setup-lab.sh graph` to install the optional CUDA Graphs voice engine and set `REELDUB_LAB_RUNTIMES=graph` on the hosted API after testing every eligible worker. For a Compose deployment, bake the same runtime into the worker image or persist its installation at `/app/.tts-graph`. The GPU and model versions affect timing. The app's estimate is calibrated with two English dubs on one RTX 4070 Ti Super. Time spent downloading, waiting in the queue and transferring the result is additional.

Set `REELDUB_LOCAL_GPU_METRICS=1` on a hosted API that shares the worker GPU to include GPU utilization, VRAM, temperature and power in the on-demand overview. The dashboard samples at most once per minute per API process while someone uses it. FP16 dialogue separation is available as a per-experiment toggle. For the alternative vLLM voice backend, use `./setup-lab.sh omni` on each compatible worker and include `omni` in the enabled runtime list. The tested isolated build uses CUDA 12.9.

#### Render lab reference timings

Wall time in seconds from cached local media to a finished English MP4 on a 16 GiB RTX 4070 Ti Super. Each entry is one complete render. The inputs were a 13.42-second Persian clip and a 40.01-second Russian conversation. The voice reference changes per line. Download, queue wait and transfer to the phone are separate.

| Configuration | Persian clip | Russian conversation |
| --- | ---: | ---: |
| Best, matched Qwen 1.7B, paired lines | 42.53 | 88.43 |
| Medium, context translation and preset voice | 30.50 | 67.26 |
| Fast, direct English translation and preset voice | 28.67 | 60.42 |
| Best with CUDA Graphs, one matched line at a time | 42.17 | 83.51 |
| Fast with FP16 dialogue separation | — | 55.26 |
| Best with vLLM-Omni 0.16.0, paired matched voices | 155.18 | — |

A separate warm test of three Spanish lines took 1.61 seconds with CUDA Graphs and 22.99 seconds with vLLM-Omni on this machine. Full renders include engine startup, separation, recognition, translation and encoding. The timing samples describe these inputs. Voice similarity, translation quality and residual dialogue still need listening checks across languages. [CUDA Graphs implementation](https://github.com/andimarafioti/faster-qwen3-tts) · [vLLM-Omni](https://github.com/vllm-project/vllm-omni)
