# ReelDub Cloud hosting

ReelDub Cloud keeps account, billing and queue metadata on **Neon Postgres Free, AWS Oregon**. The iOS app connects to the HTTPS API. The devbox runs the HTTPS origin, API, isolated media intake, GPU worker and private GridFS cache. A shared Cloudflare gateway on Proxmox forwards the single ReelDub origin to the devbox over SSH. Uploaded originals, rendered videos and poster images stay in the private cache. Self-hosters can use the bundled MongoDB configuration.

## Storage and billing controls

The hosted beta uses 0.25–0.5 CU with five-minute suspension. Neon Free includes 100 CU-hours and 5 GB of public network transfer per project each month, 1 GB of database storage and up to six hours of restore history. Storage carries over between months. Monitor the project totals in Neon. [Neon plans](https://neon.com/docs/introduction/plans)

Workers watch a notification in the private media cache before and after queue admission. An hourly queue check recovers missed notifications and coincides with cleanup. The API instances share a cleanup lease. Short-lived Apple sign-in challenges use the private cache. Account sessions, quotas and purchases stay in Postgres. Use the public `/live` endpoint for uptime monitoring and the private API’s `/health` endpoint for an on-demand database check.

On the devbox the API and workers share a private `REELDUB_WAKE_FILE`. A submission wakes idle workers immediately. Each worker checks the cache once a minute as a fallback, with earlier checks for outstanding admissions or expiring leases. Deployments without this shared Linux file check every five seconds. The foreground iOS library requests only the active dubs every five seconds during rendering or ten seconds while queued. Completion triggers a full library and balance refresh. Idle and background libraries stop polling.

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

The hosted quality profile uses Whisper large-v3, Index-Translate-9B Q5_K_M, Gemma 3 12B as the translation fallback, and Qwen3-TTS 1.7B for matched voices. Hindi uses Kokoro. Each new completed job records the actual models it used. On the Ryzen 9 5900X, using six CPU threads, native transcription of 13.4 seconds of Persian took 11.9 seconds on CPU and 1.1 seconds on the RTX. A 40.0-second Russian clip took 38.7 and 4.3 seconds respectively. These timings exclude model loading and the rest of the render.
