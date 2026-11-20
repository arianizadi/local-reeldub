# ReelDub operator and product guide

ReelDub turns an Instagram share into a playable MP4 in the selected language. The iOS app handles submission, progress, playback and feedback. The server an individual chooses runs the models and stores their jobs. The same API supports one person at home and a shared GPU installation.

Hosted accounts can use a daily allowance. Deployments with Creator minutes enabled also support monthly minutes and one-off packs.

## Run the service

1. Install Docker with the Compose v2 plugin. Run `./start-local.sh` from the repository. On Linux with an NVIDIA GPU, working Docker GPU passthrough and sufficient disk space, run `./start-local.sh --studio` for voice matching and dialogue/music/effects separation. The CPU service defaults to the small Whisper model. Studio installs large-v3 and the contextual translation and synthesis models on first boot.
2. Wait for `curl -fsS http://127.0.0.1:8300/health` to return ready. This probes the API and database. `docker compose logs -f worker` shows model preparation and any worker errors. New submissions can queue during setup.
3. Choose **Use my own server** on the welcome screen or in Account. The simulator can reach the Mac at `http://127.0.0.1:8300`. For a phone, supply an HTTPS origin with a certificate the phone trusts. With a private VPN, route the phone to the private HTTPS reverse proxy and forward only the API to port 8300. The loopback address on a physical iPhone points to the phone itself.
4. Use the installation’s private connection key. ReelDub Cloud uses Sign in with Apple. In Instagram choose **Share → ReelDub**, select a language and confirm. If ReelDub is hidden, look under **More**. Reopen ReelDub to follow the job. The Library holds the latest 100 jobs for the current account.

The setup script creates random database and operator credentials in a mode-600 `.env`. Preserve this file with the database volume. The operator key represents a single unlimited account with a shared history. Keep it with the operator. The private operator API accepts the installation key. The hosted API accepts individual Apple sessions. `REELDUB_PORT` changes the local API port. The service downloads public media and initial model weights over the network. All speech recognition, translation, speaker matching and synthesis run on the worker.

## Queue, timing and accounts

| Access | Daily allowance | Scheduling |
| --- | ---: | --- |
| Free | 5 short dubs | FIFO among free jobs. Pack-funded jobs join the paid lane. |
| Creator | 5 short dubs plus 120 additional minutes per billing month | FIFO among paid jobs |
| Admin | Unlimited | Same priority as paid |

Quotas reset at midnight UTC. Each submission gets a client-generated request ID. A retry using the same ID and URL returns the existing job. A unique database slot enforces the daily cap even when two devices submit at the same time. Failed renders release their slot. Priority applies when a worker becomes available. A render already underway finishes normally.

The library shows how many jobs are ahead and how long the submitted job has waited. Once a worker claims it, the app shows the stage and elapsed processing time. Completed jobs retain processing time, queue time and media length. They may differ greatly because clip complexity, GPU, and model configuration affect render speed.

The distributed app opens on Continue with Apple and connects to the hosted origin configured by REELDUB_HOSTED_HOST in ios/project.yml. A verified Apple account starts on Free. Set `APPLE_CLIENT_ID` to the main app bundle ID and restart the API if you use your own app identity. To grant unlimited beta access, have the tester sign in once, confirm the Apple subject for that person, then run:

```bash
docker compose exec api python -m server.admin accounts
docker compose exec api python -m server.admin account VERIFIED_APPLE_SUBJECT admin
```

Use `paid` to assign priority access manually. The local catalog includes a monthly subscription and consumables. Live billing is disabled during review. For an installation managed through environment configuration, `ADMIN_APPLE_SUB` can identify the verified owner. The hosted service accepts Apple sessions. An operator may set ADMIN_APPLE_EMAIL privately to recognize a verified email claim and persist its Apple subject as admin. Hide My Email or a prior grant without an email claim requires assigning the verified subject manually. The installation key is available on the self-host API.

The Creator review catalog includes a $19.99/month subscription with 120 additional minutes and a $4.99 consumable with 15 nonexpiring minutes. Both Free and Creator get five daily single-language dubs of up to 60 seconds. Studio is a render profile. The iOS catalog and StoreKit tests use local draft IDs. Live purchases require a separately configured and approved store.

New Compose installs enable Creator quotes. Set REELDUB_CREATOR_MINUTES=0 to exercise the previous private-beta server protocol. An updated iPhone checks the server capability before offering import and quotes. In Dubs, use **+ → Photos** or **+ → Files**, or paste an Instagram Reel. The server accepts MOV or MP4 with video and audio up to 80 MiB and ten minutes, measures the duration, and shows a per-language quote. The import screen can use the first 60 seconds of a longer source. The private intake service has no database credentials. Sources normally expire after 24 hours and persist while a confirmed render needs them. A confirmed job reserves the quoted time from monthly allowance first and then purchased time. A successful export settles that reservation. A worker failure releases it. Subscription allowances follow the verified Apple renewal period. Manually assigned Creator access uses the UTC calendar month. Self-host and admin accounts have unlimited processing. The two paid paths join the same FIFO priority lane. The [hosting guide](HOSTING.md) describes the database setup and purchase controls.

## Reports and retained data

**Report an issue** includes the canonical Reel URL, issue category and the user's note. The operator can correlate these with the job, its source, attempt, stage, timing and rendered file. A timestamp in a note helps reproduce an error. The user can edit the report. To review:

```bash
docker compose exec api python -m server.admin reports
```

MongoDB stores canonical source links, jobs, notes and account/session metadata. GridFS stores the completed MP4. Temporary downloaded media, speech samples and render logs live in a job directory removed on normal completion or failure. Undelivered renders are available for seven days or until a device confirms a playable download. ReelDub then keeps the server MP4 and poster for 24 hours. A fresh submission of the same link and target language by the same account or a new server playback/download request renews the cache. Status checks, repeated download receipts and offline playback leave the deadline alone. Cache hits use no GPU time or daily quota. A minute-by-minute cleanup removes expired media and playback grants. Deleting from the library immediately hides the job, removes its report and revokes playback links. Only that account can restore the retained video by resubmitting the same link before expiry. Abruptly terminating a host can leave temporary files or unreferenced GridFS data. Establish retention, cleanup and backups before hosting other people's videos.

## Deployment and scaling

The API receives requests and serves owner-authorized media. Workers atomically claim the highest-priority eligible job from MongoDB. A worker renews a 90-second lease every 15 seconds. An expired job can be reclaimed up to three attempts. Only the current lease holder can publish a result. Workers handle one render per process. On the 16 GiB Studio GPU, use two worker processes with REELDUB_GPU_SCHEDULER=1. Two host file locks allow one Gemma translator and one Qwen voice model to overlap. Separation, cleanup and Whisper reserve both lanes. All launchers targeting the same GPU must share REELDUB_GPU_LOCK_DIR; the default lives in the model cache volume. Use a different directory for each physically distinct GPU.

```bash
# Two independent CPU worker containers on one host
docker compose up -d --scale worker=2
# Or two render processes inside each worker
WORKER_PROCESSES=2 docker compose up -d worker
```

Every worker and API instance must connect to the same `reeldub` database. To use an existing MongoDB installation, provision a dedicated database user with readWrite on that database. Set `REELDUB_MONGO_URI` for the API and `REELDUB_WORKER_MONGO_URI` for the separate worker credential. Use verified database TLS. The worker role in `server/worker-role.js` can update jobs and store media. Avoid sharing root credentials. After generating the local secrets, run `docker compose up -d --no-deps --build api worker` to use the external database alone. On additional GPU hosts with the same URI use `docker compose -f compose.yaml -f compose.gpu.yaml up -d --no-deps --build worker`. Each node needs its own model cache. Shared GridFS lets any API replica serve any completed job.

The private Compose configuration publishes the API on loopback. MongoDB has a private container network and an app-specific credential. Bearer sessions expire after 30 days. The server verifies Apple tokens and a fresh, single-use nonce, stores session-token hashes, and checks job ownership for progress, download and reporting. The iOS app and share extension keep the connection in a shared device-only Keychain group. The service accepts individual Instagram Reel URLs, constrains source length to ten minutes and source size to 250 MiB, and applies a 30-minute render timeout and 500 MiB output limit. For public hosting, add ingress rate/size limits, worker egress and resource controls, storage retention, observability, backups and tested restore procedures. A managed cluster, automatic capacity scaling, and lip movement synthesis are future work.

## Hosted endpoint

For the hosted deployment, add `compose.hosted.yaml` to the base Compose file and start `hosted-api ingress`. The ingress listens on loopback port 8301 by default. Point your HTTPS tunnel at this listener on the origin host. The hosted API shares the job database, disables the installation master key, rate-limits sign-in and enforces a request body limit. Keep the origin private and only trust the Cloudflare-supplied client IP through that tunnel. Source repositories for forks can override the hosted hostname in the Xcode project settings before building.

The player requests an owner-authorized, expiring playback URL and streams HTTP byte ranges. While the app is open, it saves completed MP4s one at a time in an account-specific device library. Playback and sharing use the saved file offline. The player can stream while the download is pending. The app retries interrupted transfers. If a second device missed the cache window, open the dub and tap **Translate again**. In Dubs, tap **Select**, pick finished or failed entries, then tap **Delete** and confirm. Deletion needs a connection. Other devices remove their saved copy when they next sync. New jobs keep a small still image from the rendered video and available source title/author; older jobs can display submission dates. Reports and poster frames require the owning account.

## iOS builds and private TestFlight

The checked-in Xcode project supports iOS 17 and later. It includes the app, share extension, original SVG artwork, compiled asset catalog inputs and a UI smoke test. To rebuild the project after editing `ios/project.yml`, run `cd ios && xcodegen generate`. For a fork, configure your own signing team, main and extension bundle identifiers, shared Keychain access group and matching Apple audience. Enable Sign in with Apple on the main app ID.

To run on your iPhone, put `DEVELOPMENT_TEAM = YOUR_TEAM_ID` in `ios/Config/LocalSigning.xcconfig`. Xcode applies this local setting to both the app and the share extension. Open `ios/ReelDub.xcodeproj`, select the **ReelDub** scheme and your physical iPhone, then press **⌘R**.

For private testing, archive the Release scheme for a generic iOS device. In Xcode choose the **TestFlight Internal Only** distribution option. The equivalent export settings are in `ios/Config/InternalExport.plist`. Create an internal group in App Store Connect, leave automatic distribution off, select the intended testers and add the processed build to that group. Apple delivers the invitation. On the phone, accept it in TestFlight with the invited Apple Account. The API address can be changed without uploading a new build. [Apple’s internal testing guide](https://developer.apple.com/help/app-store-connect/test-a-beta-version/add-internal-testers/)

## Verification

Run server integration tests in a new disposable Compose project. They require an empty test database and a hosted-mode API. The test exercises concurrent quotas, priority, owner isolation, idempotency, reports, recovery of an expired lease, and Apple audience, issuer, expiration and nonce checks against a local test signing key.

```bash
REELDUB_PUBLIC_HOST=127.0.0.1 REELDUB_HOSTED_PORT=18300 docker compose -p reeldub-qa -f compose.yaml -f compose.hosted.yaml up -d --build ingress
curl --retry 30 --retry-all-errors --retry-delay 1 -fsS http://127.0.0.1:18300/health
docker compose -p reeldub-qa -f compose.yaml -f compose.hosted.yaml exec -T -e REELDUB_TEST_DATABASE=1 hosted-api python -m unittest -v server.test_service server.test_store server.test_access
docker compose -p reeldub-qa -f compose.yaml -f compose.hosted.yaml down -v
```

The Xcode UI smoke test connects to the local server, plays a completed render and submits an issue report. Run against a dedicated test installation with at least one completed job. Pass its operator key in `TEST_RUNNER_REELDUB_TEST_KEY` to `xcodebuild test`. Treat XCTest logs and result bundles as private because they can contain text typed into secure fields.

The GPU Compose profile advertises Studio languages automatically. For a hosted API connected to an external Studio worker, set `REELDUB_OUTPUT_PROFILE=studio` in the API environment. Light workers claim English jobs, and Studio workers can claim every supported target. The default portable Light installation advertises English.

For a TLS-protected Compose database, create a new private certificate directory with `server/setup-mongo-tls.sh`, set `REELDUB_TLS_DIR` to its absolute path, and add `-f compose.tls.yaml` after the base Compose file. When also using the hosted overlay, append `-f compose.hosted-tls.yaml`. Back up the private CA securely and renew the server certificate before expiry. The GPU worker needs the public CA file and its separate worker credential.
