# Private metadata on Proxmox

The hosted service runs a dedicated MongoDB replica set named `reeldub-meta` in the existing MongoDB guest. It stores ReelDub records in `reeldub_metadata`. The shared MongoDB service and its listeners have separate storage and credentials. The new replica listens only on guest loopback. The control guest forwards its own loopback port 29361 to the replica over SSH and verifies the MongoDB TLS certificate. Clients and GPU nodes continue using their existing endpoints.

## Provision the database

Run `provision.py` as root on the Proxmox host. Set `REELDUB_DB_CT`, `REELDUB_CONTROL_CT`, `REELDUB_DB_IP`, `REELDUB_CONTROL_IP`, and `REELDUB_ENCRYPTED_DATASET` to the two unprivileged guest IDs, their reserved LAN addresses, and the control guest’s encrypted ZFS dataset. The procedure expects the existing ReelDub control-storage unlock service, standard Proxmox UID mapping, MongoDB and OpenSSH in the database guest, and the control service’s Python environment.

The script creates an 8 GiB child dataset with a separately verified mount, an independent MongoDB process and credentials, and a forwarding-only SSH identity. Only the new metadata process requires its encrypted volume marker. The app, queue coordinator and snapshot process use separate MongoDB grants. Keep the ZFS recovery key and TLS issuance material in protected recovery storage. Check certificate dates during normal host maintenance.

## Switch an existing hosted service

Follow [the migration and recovery procedure](../../HOSTING.md). Run an isolated rehearsal first. Drain active renders and stop every metadata writer for the final copy. Keep the collection counts and hashes from `server.migrate_mongo`. Configure the API URI file as `metadata-app.uri`, the loopback operator as `metadata-operator.uri`, and the coordinator as `metadata-fleet.uri`. These names refer to files in `/srv/reeldub-private/config`. All three clients use `metadata-ca.pem`. The public API also needs a persistent private erasure journal on the encrypted control volume.

Before reopening admission, install `install-cutover-fence.sh reeldub_metadata` on the control guest. Confirm the private `/health`, public `/live`, an authenticated library read, an idempotent submission and a completed uncached GPU render. After the first successful off-site restore, retire any authorized legacy copies. Turning off the new service does not make a stale PostgreSQL database safe to serve.

## Backups and host recovery

The [S3 backup service](../backup) runs under systemd on the control guest. The phone, development Mac and GPU worker do not run the timer. The database is a single-node replica set. A guest restart retains journaled writes. Losing the entire Proxmox host requires a restore from a separate failure domain. Use a verified S3 snapshot and replay deletion receipts in staging before promotion. The media cache has its own lifecycle and is excluded from these metadata snapshots.
