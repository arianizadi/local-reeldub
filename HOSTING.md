# ReelDub Cloud hosting

Phones reach the hosted API through Cloudflare at `reeldub.arianizadi.com`. A restricted SSH forward connects the public gateway to the origin on Proxmox CT115. That guest runs the API, private GPU coordinator, operator endpoint and encrypted media store. Proxmox CT114 fetches public Instagram media through its own restricted VPN connection. The Linux devbox pulls jobs and renders them on its 4070 Ti Super. The Mac is used for iOS development. [GPU fleet architecture, queue order and failover](GPU-FLEET.md)

## Storage and billing controls

Neon PostgreSQL in AWS Oregon stores account identities, session hashes, entitlements, billing records, queue state and references to files. Originals, rendered videos and posters live in encrypted GridFS on CT115. The phone can keep a downloaded dub after its server cache entry expires. Local deletion works offline. [Media lifecycle](GPU-FLEET.md#from-admission-to-playback)

The hosted beta targets 0.25–0.5 CU with five-minute suspension. Use the Neon dashboard to compare project compute, transfer, storage and restore history with the current plan limits. An idle GPU checks for work through CT115. Wake signals, worker health, telemetry, short-lived Apple sign-in challenges and watchdog scheduling use the private media database. Queue admission, active job updates, recovery and periodic reconciliation can wake Neon. Use public `/live` for uptime checks and the private API's `/health` for an on-demand database check. [Neon plans](https://neon.com/docs/introduction/plans)

Set `REELDUB_STORE_STORAGE_LIMIT_BYTES` to the database budget. The free deployment uses 1,000,000,000 bytes. New checkout requires an authenticated, committed purchase intent. Checkout pauses when database usage reaches 80% of the lower of the configured budget and the database provider’s advertised cluster limit, when a write fails, or when billing is disabled. Keep this production project on one branch or account for additional branches in the budget. Verified Apple transactions, refunds and renewals can still be delivered after admission closes. The iOS client finishes transactions after server delivery and retries unfinished purchases. Apple controls renewals and final payment timing.

## Beta tester allowances

With operator database credentials, run `python -m server.admin beta-allowance tester@example.org 50` to reserve a daily allowance. Invite the tester through TestFlight and ask them to choose **Share My Email** with that address when signing in to ReelDub. The first matching verified Apple sign-in claims the allowance. Account settings show daily use and the limit. It resets at midnight UTC and gives standard queue access. Run the command again to update the allowance for that tester.

## Deploy and recover the hosted fleet

1. Configure the two guests, preserving the separate service identities and encrypted data volumes. CT114 accepts private mutual TLS requests from CT115. Its restricted downloader and inspection tools use an allowlisted proxy with VPN-only egress. CT115 serves the app through its public origin and workers through a separate mutual TLS listener. Keep the operator endpoint private. [Intake guest](deploy/proxmox-intake/README.md) · [Control guest and GPU worker](deploy/fleet/README.md) · [Operator relay](deploy/proxmox-operator/README.md)
2. Provision the PostgreSQL schema with `python -m server.migrate_postgres` and the private owner connection in `REELDUB_MIGRATION_DSN_FILE`. Install service-specific credentials on CT115. Protect the database connection with verified TLS. GPU nodes receive a worker certificate, private key and node-scoped bearer. The coordinator transfers only the source and result for the worker's current job lease.
3. For a migration, drain the queue and pause writes. Back up the media database and test restoration separately. When importing historical Mongo metadata, run the migration with `--metadata` against the intended source and destination. The command checks media references, imports metadata transactionally and compares table checksums. Keep the frozen source until old library items, owner-only playback, admin entitlement and a new render have been verified.
4. Verify public API access, private worker authentication, intake egress enforcement, completed playback and guest restart recovery. The former devbox API, direct workers and origin are disabled. Its frozen encrypted archive represents the cutover point. For a later rollback, quiesce the active services and reconcile all newer metadata and media before serving from a restored copy. [Fleet deployment checks](deploy/fleet/README.md)

Hosted purchases and public uploads are feature-gated. Enabling either requires its own deployment configuration.

## Self-hosted Compose and legacy native deployment

The official iOS app offers a free authenticated connection test and a one-time App Store unlock for self-hosting. Your server URL and key go directly to your server. The hosted billing API receives an Apple-signed app proof before purchase and a signed transaction afterward. It commits a checkout intent to PostgreSQL before StoreKit opens the payment sheet. Previously purchased access can be restored through the App Store while the hosted API is offline.

The hosted billing service uses `REELDUB_SELF_HOST_STORE_ENVIRONMENTS=Sandbox` for beta checkout. Production checkout requires enabling `Production` on the billing service and an approved App Store product. This setting is independent of hosted Creator billing. Both use the configured storage budget to pause new purchases when the ledger reaches 80% of the limit. Private server operators do not need Apple billing credentials.

A new self-hosted installation can use Compose with its bundled MongoDB. For PostgreSQL metadata, apply `compose.neon.yaml` after the hosted and TLS overlays. Supply `REELDUB_API_DSN_FILE` to the API and `REELDUB_WORKER_DSN_FILE` to the optional direct worker in the `local-worker` profile. Mount only each service's credential. The direct worker needs schema usage, SELECT and UPDATE(doc) on `reeldub.jobs`, plus access to the local media store. The fleet deployment above provides the alternative for remote GPUs.

For an existing native all-in-one Ubuntu 24.04 x86-64 installation, `deploy/devbox/prepare-runtime.sh` prepares the service and sandbox binaries and `deploy/devbox/install-services.sh` generates user systemd units. The legacy encrypted media vault uses gocryptfs and a TPM-sealed credential bound to PCR 7. Keep a separate recovery key offline, especially before changing Secure Boot or replacing the motherboard. User lingering starts the units after boot. Retain encrypted backups independently of the GPU host.

## Render performance

Two pipeline slots share the hosted 16 GiB card. Both priority and standard jobs use the GPU. The optional direct-worker setting `REELDUB_FREE_CPU_OVERFLOW=1` permits one free job to transcribe on six CPU threads when GPU lanes are busy. Separation, Studio translation and matched speech still use the GPU. The Best preset defaults to paired speech synthesis, with a separate voice reference per line. An out-of-memory error falls back to single-line synthesis.

Jobs report stage milestones and completed voice lines. Finish-time estimates use up to seven recent successful renders of comparable duration and prefer the same output language. Fewer than three comparable samples yields progress without a time estimate. Queue wait is measured separately.

For database tuning, compare `pg_stat_statements` calls and shared-buffer reads on a disposable PostgreSQL instance using the same data and requests. Inspect queue and job queries with `EXPLAIN (ANALYZE, BUFFERS)`. Connection-checkout regression tests live in `server/test_idle.py`. On the GPU host, use `pidstat` for CPU activity and `nvidia-smi` for memory, load and power. Xcode Instruments' Network template measures device traffic while rendering and after the library goes idle.

New dubs on the hosted Studio worker use the **Best** preset across both queue tiers. Best uses Whisper large-v3, context translation, and Qwen3-TTS 1.7B for matched voices. On the hosted devbox, Index-Translate-9B Q5_K_M handles translation with Gemma 3 12B available as a fallback. Hindi uses Kokoro. Each new completed job records the actual models it used. On the Ryzen 9 5900X, using six CPU threads, native transcription of 13.4 seconds of Persian took 11.9 seconds on CPU and 1.1 seconds on the RTX. A 40.0-second Russian clip took 38.7 and 4.3 seconds respectively. These timings exclude model loading and the rest of the render.

### Hosted administrator console

In the iOS app, open **Dubs → Account → Admin console** from the library menu. The hosted API requires an active Apple session whose subject matches `ADMIN_APPLE_SUB`. Use Apple again to unlock the console. Every dashboard, moderation and experiment request checks the session and a separate, session-bound 15-minute grant. The app holds that grant in memory. A self-host key, beta review key, plan assignment or matching email cannot unlock the console.

Overview groups retained requests by status, language pair and render mode and shows measured cache reuse, queue wait and render time. Cache-hit counting starts with this version. Pull to refresh. Accounts, requests and reports load in pages of 40. Open an account to see its effective allowance and request history. A ban stops hosted access and pending work and records the reason. Account deletion remains available to suspended users.

To review the five-dub allowance, open **Admin console → Overview**. Over the last 30 UTC calendar days, it shows how many standard free accounts used a fifth daily slot and how often that happened across days they used at least one slot. Someone who dubs on three different days contributes one account and three active days. Reopening a cached dub uses no slot. Tracking begins with this release.

Priority wait measures the time from entering the paid priority lane to the first worker start. The overview shows the median, 95th percentile and number of measured starts, plus the same figures for arrivals when all compatible configured GPU slots were occupied. An empty sample means there is no observed wait yet. These measurements use the existing admin refresh and do not require background polling.

The render lab queues a fresh test with its own settings. Best uses context translation and a matched voice when supported. Medium keeps context translation and chooses a preset voice where one is available. Fast uses greedy speech decoding and, for English, direct Whisper translation. All three keep source-dialogue cleanup. Paired decoding keeps a separate voice reference for each line. Experiments participate in the regular priority queue and have separate cache identities.

On a native Studio worker, run `./setup-lab.sh graph` to install the optional CUDA Graphs voice engine and set `REELDUB_LAB_RUNTIMES=graph` on the hosted API after testing every eligible worker. For a Compose deployment, bake the same runtime into the worker image or persist its installation at `/app/.tts-graph`. The GPU and model versions affect timing. The app's estimate is calibrated with two English dubs on one RTX 4070 Ti Super. Time spent downloading, waiting in the queue and transferring the result is additional.

Hosted GPU workers report sampled utilization, VRAM, temperature and power to CT115 when local GPU metrics are enabled. The admin overview shows recent samples per node. Samples older than three minutes drop out. For a self-hosted API sharing the same GPU as its worker, `REELDUB_LOCAL_GPU_METRICS=1` also enables on-demand local sampling.

FP16 dialogue separation is available per experiment. To test vLLM-Omni voices, run `./setup-lab.sh omni` on each compatible worker and include `omni` in the enabled runtime list. The tested isolated build uses CUDA 12.9.

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
