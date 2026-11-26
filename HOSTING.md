# Hosting ReelDub

The [quick start](README.md#quick-start) runs one GPU worker on a Linux server. Docker publishes the API on loopback. The intake, egress proxy, database and model services have no published ports. The model installer fetches its weights before the GPU worker starts. If the optional Kitten installation fails, the installer reports it in its log and the worker can still process dubs with Qwen. The worker reaches MongoDB and the private downloader through internal networks. The downloader can reach a dedicated outbound proxy. For each HTTP or HTTPS connection, the proxy checks the DNS results and connects to a public IP on port 80 or 443. The Kitten service has its own offline network namespace and receives speaker references through a private volume.

## Connect

On the server, open `http://127.0.0.1:8300`. On another computer, forward a local port through SSH:

```sh
ssh -N -L 8300:127.0.0.1:8300 your-user@your-render-host
```

Open `http://127.0.0.1:8300` on the connecting computer. For phones, use an HTTPS reverse proxy with a certificate the phone trusts or [Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve) on the Docker host. Proxy the site root and all API requests to the loopback address and allow enough time for video uploads. The default shared key opens one library. A PostgreSQL installation can issue individual keys, giving each person a private library on the same server.

## Local PostgreSQL

Use `./start-local.sh --postgres` in a fresh checkout. The installer records the Compose overlay in `.env`, so later `docker compose` commands and `./start-local.sh` continue using that mode. It generates independent database secrets for the API, worker, schema provisioner and access-key administrator. Only the key administrator creates or rotates user keys. The API can look up a key hash and the worker can claim jobs. Neither can administer keys.

The server accepts PostgreSQL connections only over certificate-verified TLS on a private Docker network. Database roles authenticate with SCRAM. No PostgreSQL port is published. A local CA and server identity are initialized on the first run. Role grants and schema setup can be reapplied. The API starts after provisioning succeeds. The terminal command `./access-key list` lists user records. `./access-key create NAME` prints an access key once, `rotate NAME` replaces it and `revoke NAME` disables it. Keep administrative shell access private and deliver each person's key privately. The shared Mongo-mode key cannot access this instance.

In this mode, back up the PostgreSQL database (`pg-data`), its offline certificate authority (`pg-issuer`) and database identity (`pg-tls`), the Mongo media volume and the generated `.env`. Take a consistent stopped-stack snapshot of both databases if restoring renders together with their metadata. The public CA volume (`pg-trust`) is repopulated from the offline certificate authority. Never substitute model caches for a database backup.

For a second instance on a machine with an existing shared-key library, clone into an empty directory and choose a different port, for example `REELDUB_PORT=8302 ./start-local.sh --postgres`. The installer saves that port in the new `.env`. Each new PostgreSQL installation generates a unique Compose project name and private volumes. Keep the project name in its generated `.env` for later restarts and backups. Existing library records remain with their original installation.

## Updates and configuration

Keep `.env` on the server. Setup generates separate MongoDB passwords for the owner, API and worker, plus a connection key in shared-library mode. Re-running setup preserves them.

```sh
git pull --ff-only
./start-local.sh
docker compose ps -a
docker compose logs --tail=60 models worker
```

| Setting | Default | Purpose |
| --- | --- | --- |
| `REELDUB_PORT` | `8300` | Published loopback API port |
| `REELDUB_KITTEN_MATCH` | `1` | Set to `0` before startup for a Qwen-only installation |
| `REELDUB_MAX_SOURCE_SECONDS` | `0` | Optional operator limit on input duration in seconds |
| `WORKER_PROCESSES` | `1` | Increase only after testing simultaneous renders on your GPU |

After changing `.env`, rerun `./start-local.sh`. In shared-library mode, replace `SELF_HOST_KEY` with the output of `openssl rand -hex 32`, recreate the API with `docker compose up -d api`, then connect each client with the new key. Database passwords in an existing Mongo volume require a corresponding MongoDB credential change.

To run MongoDB with TLS on the container networks, follow `server/setup-mongo-tls.sh` and include `compose.tls.yaml` in the Compose file list. To use another GPU server, see the [private worker guide](deploy/fleet/README.md).

## Storage and backups

The default shared-key installation keeps the library, queue, inputs and outputs in `mongo-data`. PostgreSQL mode keeps keys and queue metadata in `pg-data` and media in `mongo-data`. The `model-cache`, `studio-env` and `kitten-env` volumes contain rebuildable models and environments. The `kitten-compile` volume holds reusable CUDA compilation artifacts. `match-spool` is temporary work in progress. `docker compose down` stops the services while preserving named volumes.

For a recoverable single-host backup, wait for active jobs to finish, stop the stack, and take a consistent snapshot of the database volumes listed above for your installation. Keep an encrypted copy of `.env` and any locally managed proxy certificates separately. Restart with `./start-local.sh`. Test recovery by restoring into a separate installation with its own project name and loopback port. Model weights can be reinstalled. Operators with an existing split metadata deployment can use the [off-site backup and restore tooling](deploy/backup/README.md).

## Troubleshooting

| What you see | What to check |
| --- | --- |
| API is up and jobs are queued | `docker compose logs -f models` on first startup. A successful model installer exits with status 0. Then check `docker compose ps worker`. |
| GPU is missing | Run `./start-local.sh --check` on the Linux server. This checks GPU visibility from inside Docker, the Compose file and the host driver. |
| First model install failed | Read the last model installer error. Check free disk, outbound access, GPU driver support and free GPU memory, then rerun setup. A Qwen-only setup can use `REELDUB_KITTEN_MATCH=0`. |
| Phone cannot connect | Open the same HTTPS address in Safari, check the proxy and verify that the phone can reach the private network. A phone’s `localhost` refers to the phone. |
| Video link could not download | Check `docker compose logs intake egress`. Only single public videos supported by the installed yt-dlp extractor can be downloaded. A site may require sign-in, cookies or an IP from a particular region. Share your local copy from Photos or Files in the iPhone app. |

## On-screen translation

Compose installs the Tesseract runtime, its language packs and fonts in the worker image. Model setup also prepares the CPU PaddleOCR detector and Arabic-script recognizer in the `screen-ocr-env` volume. The worker mounts that volume read-only. Rebuild on upgrade with `./start-local.sh` so the worker and model setup use the same version. A native Linux installation can run `./setup-screen-ocr.sh` to prepare the optional Paddle models. The standard recognizer remains available if that installation fails. To override its language list, put installed Tesseract codes such as `REELDUB_OCR_LANGUAGES=eng+fas+fra` in the server’s `.env` and restart the worker.

## Previous hosted deployments

Installing the self-host release does not convert Apple accounts or merge past libraries into a shared operator workspace. Keep a verified backup before modifying a live deployment. The historical account-backed API, StoreKit catalog and hosted Compose overlays were retired. For a clean personal instance, start a separate Compose project with its own generated `.env`, loopback port and volumes, then point the client at the new server.

The existing split-host deployment uses independent GPU, intake, media and metadata services. Its [control setup](deploy/fleet/README.md), [metadata configuration](deploy/metadata/README.md) and [backup tools](deploy/backup/README.md) remain available for operators maintaining that layout.
