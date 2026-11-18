# ReelDub operator and product guide

ReelDub turns an Instagram share into a playable English MP4. The iOS app handles submission, progress, playback and feedback. The server an individual chooses runs the models and stores their jobs. The same API supports one person at home and a shared GPU installation.

## Run the service

1. Install Docker with the Compose v2 plugin. Run `./start-local.sh` from the repository. On Linux with an NVIDIA GPU, working Docker GPU passthrough and sufficient disk space, run `./start-local.sh --studio` for voice matching and dialogue/music/effects separation. The CPU service defaults to the small Whisper model. Studio installs large-v3 and the contextual translation and synthesis models on first boot.
2. Wait for `curl -fsS http://127.0.0.1:8300/health` to return ready. This probes the API and database. `docker compose logs -f worker` shows model preparation and any worker errors. New submissions can queue during setup.
3. Open ReelDub's **Server and account**. The simulator can reach the Mac at `http://127.0.0.1:8300`. For a phone, supply an HTTPS origin with a certificate the phone trusts. With a private VPN, route the phone to the private HTTPS reverse proxy and forward only the API to port 8300. The loopback address on a physical iPhone points to the phone itself.
4. Use Sign in with Apple or the installation's private connection key. In Instagram choose **Share → ReelDub → Translate to English**. If ReelDub is hidden, look under **More**. Reopen ReelDub to follow the job. The Library holds the latest 100 jobs for the current account.

The setup script creates three random secrets in a mode-600 `.env`. Preserve this file with the database volume. The operator key represents a single unlimited account with a shared history. Keep it with the operator. Other users can connect with Apple. `REELDUB_PORT` changes the local API port. The service downloads public media and initial model weights over the network. All speech recognition, translation, speaker matching and synthesis run on the worker.

## Queue, timing and accounts

| Access | Daily allowance | Scheduling |
| --- | ---: | --- |
| Free | 10 | FIFO among free jobs |
| Paid | 50 | Ahead of queued free jobs |
| Admin | Unlimited | Same priority as paid |

Quotas reset at midnight UTC. Each submission gets a client-generated request ID. A retry using the same ID and URL returns the existing job. A unique database slot enforces the daily cap even when two devices submit at the same time. Failed renders release their slot. Priority applies when a worker becomes available. A render already underway finishes normally.

The library shows how many jobs are ahead and how long the submitted job has waited. Once a worker claims it, the app shows the stage and elapsed processing time. Completed jobs retain processing time, queue time and media length. They may differ greatly because clip complexity, GPU, and model configuration affect render speed.

A verified Apple account starts on Free. Set `APPLE_CLIENT_ID` to the main app bundle ID and restart the API if you use your own app identity. To grant unlimited beta access, have the tester sign in once, confirm the Apple subject for that person, then run:

```bash
docker compose exec api python -m server.admin accounts
docker compose exec api python -m server.admin account VERIFIED_APPLE_SUBJECT admin
```

Use `paid` to assign priority access manually. Billing is deferred. For an installation managed through environment configuration, `ADMIN_APPLE_SUB` can identify the verified owner. The installation key is also available for single-user testing immediately.

## Reports and retained data

**Report an issue** includes the canonical Reel URL, issue category and the user's note. The operator can correlate these with the job, its source, attempt, stage, timing and rendered file. A timestamp in a note helps reproduce an error. The user can edit the report. To review:

```bash
docker compose exec api python -m server.admin reports
```

MongoDB stores canonical source links, jobs, notes and account/session metadata. GridFS stores the completed MP4. Temporary downloaded media, speech samples and render logs live in a job directory removed on normal completion or failure. Artifacts and reports persist until the operator removes them. Abruptly terminating a host can leave temporary files or unreferenced GridFS data. Establish retention, cleanup and backups before hosting other people's videos.

## Deployment and scaling

The API receives requests and serves owner-authorized media. Workers atomically claim the highest-priority eligible job from MongoDB. A worker renews a 90-second lease every 15 seconds. An expired job can be reclaimed up to three attempts. Only the current lease holder can publish a result. Workers handle one render per process. Start with one process per GPU to fit model memory.

```bash
# Two independent CPU worker containers on one host
docker compose up -d --scale worker=2
# Or two render processes inside each worker
WORKER_PROCESSES=2 docker compose up -d worker
```

Every worker and API instance must connect to the same `reeldub` database. To use an existing MongoDB installation, provision a dedicated database user with readWrite on that database. Set `REELDUB_MONGO_URI` in the local environments to its private connection string, using TLS between hosts. Avoid sharing root credentials. After generating the local secrets, run `docker compose up -d --no-deps --build api worker` to use the external database alone. On additional GPU hosts with the same URI use `docker compose -f compose.yaml -f compose.gpu.yaml up -d --no-deps --build worker`. Each node needs its own model cache. Shared GridFS lets any API replica serve any completed job.

The private Compose configuration publishes the API on loopback. MongoDB has a private container network and an app-specific credential. Bearer sessions expire after 30 days. The server verifies Apple tokens and a fresh, single-use nonce, stores session-token hashes, and checks job ownership for progress, download and reporting. The iOS app and share extension keep the connection in a shared device-only Keychain group. The service accepts individual Instagram Reel URLs, constrains source length to ten minutes and source size to 250 MiB, and applies a 30-minute render timeout and 500 MiB output limit. For public hosting, add ingress rate/size limits, worker egress and resource controls, storage retention, observability, backups and tested restore procedures. A managed cluster, shopper billing, automatic capacity scaling, and lip movement synthesis are future work.

## iOS builds and private TestFlight

The checked-in Xcode project supports iOS 17 and later. It includes the app, share extension, original SVG artwork, compiled asset catalog inputs and a UI smoke test. To rebuild the project after editing `ios/project.yml`, run `cd ios && xcodegen generate`. For a fork, configure your own signing team, main and extension bundle identifiers, shared Keychain access group and matching Apple audience. Enable Sign in with Apple on the main app ID.

For private testing, archive the Release scheme for a generic iOS device. In Xcode choose the **TestFlight Internal Only** distribution option. The equivalent export settings are in `ios/Config/InternalExport.plist`. Create an internal group in App Store Connect, leave automatic distribution off, select the intended testers and add the processed build to that group. Apple delivers the invitation. On the phone, accept it in TestFlight with the invited Apple Account. The API address can be changed without uploading a new build. [Apple’s internal testing guide](https://developer.apple.com/help/app-store-connect/test-a-beta-version/add-internal-testers/)

## Verification

Run server integration tests in a new disposable Compose project. They require an empty test database and a running API. The test exercises concurrent quotas, priority, owner isolation, idempotency, reports, recovery of an expired lease, and Apple audience, issuer, expiration and nonce checks against a local test signing key.

```bash
REELDUB_PORT=18300 docker compose -p reeldub-qa up -d --build api
curl --retry 30 --retry-all-errors --retry-delay 1 -fsS http://127.0.0.1:18300/health
docker compose -p reeldub-qa exec -T -e REELDUB_TEST_DATABASE=1 api python -m unittest -v server.test_service
docker compose -p reeldub-qa down -v
```

The Xcode UI smoke test connects to the local server, plays a completed render and submits an issue report. Run against a dedicated test installation with at least one completed job. Pass its operator key in `TEST_RUNNER_REELDUB_TEST_KEY` to `xcodebuild test`. Treat XCTest logs and result bundles as private because they can contain text typed into secure fields.
