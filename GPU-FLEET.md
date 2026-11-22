# ReelDub GPU fleet

The hosted beta runs on one RTX 4070 Ti Super with two render slots. Proxmox hosts the public API, queue coordinator, encrypted media cache and isolated intake across two guests. The Mac is an iOS development machine. A phone only needs the public HTTPS endpoint.

| Component | Runs where | Role |
| --- | --- | --- |
| Public gateway | Cloudflare and the existing Proxmox gateway | Routes ReelDub HTTPS traffic through a restricted SSH forward to the API origin on CT115. |
| Control and media | Proxmox CT115 | Runs the API, private worker coordinator and encrypted GridFS for originals, renders and posters. Owns server credentials and publishes results. |
| Media intake | Proxmox CT114 | Accepts authenticated fetch and inspection requests from CT115 on private TCP 8443. A restricted proxy sends Instagram and CDN requests through the provider VPN. If the VPN goes down, media egress closes. |
| GPU | Linux devbox | Two slots on one 16 GiB card pull jobs through the private coordinator on TCP 8343. Model inference, audio mixing and encoding run on this machine. |
| Durable metadata | Neon PostgreSQL | Holds accounts, entitlements, queue order, job leases and artifact references. Media bytes stay in private storage. |

## From admission to playback

1. ReelDub checks the signed-in account, Reel URL, allowance and request identity. A daily-plan submission reserves its allowance at admission. The first assigned worker's source request triggers preparation on CT115. When Creator quotes are enabled, CT115 stages the media earlier so it can quote the measured duration.
2. CT115 reuses a staged source for the same owner and Reel across output languages and retries. Concurrent requests share a preparation. CT114 fetches a missing original once and CT115 stores the validated bytes. Active jobs retain their source. Reusing cached output refreshes its 24-hour window. Removing a copy from a phone leaves the independently expiring server cache intact.
3. An available slot asks for one compatible job. Interrupted infrastructure attempts have recovery priority, followed by premium and then standard jobs, with FIFO order within each queue. Running jobs keep their slots. One worker downloads the assigned bytes from CT115 and renders the entire output language on its GPU.
4. The worker reports progress and streams the finished MP4 and poster back. The coordinator verifies node, job and current lease before publication. The iPhone gets the finished file from its usual public API regardless of the rendering host.

## Worker health and recovery

- A node sends a health pulse every 15 seconds. A missed pulse makes the node suspect after 35 seconds and offline after 80 seconds. An unhealthy or draining node stops receiving new claims. The watchdog uses the private media database to track active assignments and wakes the durable queue for recovery.
- A failed admission preflight pauses new claims. Existing renders can finish while the worker checks again. Two failed probes at least 30 seconds apart confirm a fault. A GPU, model or runtime failure reported for an active render can trigger quarantine immediately. The first fault starts a 60-second cooldown. Another within ten minutes starts a 120-second cooldown. Returning nodes must pass an authenticated preflight covering model files, scratch space and a real CUDA allocation.
- An interrupted job keeps its job ID and reservation and moves ahead of ordinary work. A healthy, compatible standby with capacity can take it. The original node can resume after it recovers when it is the only node. Three interrupted attempts exhaust the retry budget and release the reservation. Permanent input errors have a separate failure path. A worker whose lease has been replaced cannot publish.
- The admin console shows node status, last contact and per-node GPU measurements. A second physical GPU host has yet to join this installation. Two slots on the devbox increase throughput, and an outage of that host pauses rendering until it recovers.

## Add another GPU host

1. Install the pinned Studio pipeline, local models and worker service. Give each independently scheduled GPU its own application identity. Start a new GPU at one slot and benchmark it. Set `REELDUB_DEVICE_ID` and expose only the intended CUDA device. Give processes sharing a card the same `REELDUB_GPU_LOCK_DIR` and `REELDUB_GPU_PIPELINE_SLOTS`. Give distinct GPUs separate lock directories. [Worker installation and enrollment](deploy/fleet/README.md)
2. Provide private network reachability to CT115. A remote machine can join as one NetBird peer and route multiple local GPUs. Limit its effective peer policies to the coordinator's TCP 8343 endpoint and check overlapping all-to-all rules. Workers initiate connections. The intake provider VPN on CT114 serves Instagram downloads independently. [NetBird access policies](https://docs.netbird.io/manage/access-control)
3. On the management side, run `enroll-node.sh NODE SLOTS` and deliver the node credential directory privately. The worker presents its certificate and bearer. The coordinator proxy checks the certificate subject against the claimed node name. The node receives only its assigned media under a current lease. Keep database and Apple credentials on the control guest. [Certificate renewal and removal](deploy/fleet/README.md)
4. Verify readiness in the admin console. Render two test jobs, check which node handled each, then disconnect one mid-render and verify one reservation, one published result, recovery status and playback. Drain by disabling new assignments in the registry. Check queue wait, completed source minutes per GPU-hour, peak memory and transfer time before raising the slot count.

To rehearse the queue behavior locally, prepare the Docker images listed in the [burst-worker drill](deploy/qa/test_fleet_burst.py), then run `python3 deploy/qa/test_fleet_burst.py` from the repository root. The drill adds a simulated worker while jobs are running, drops the first worker and checks that the original client can retrieve the result with a single allowance charge per submission. Use an actual rental to measure provisioning, media transfer and render time.

## Measured on the first GPU

On November 21, 2026, the RTX 4070 Ti Super rendered a 13.42-second Persian clip and a 13.03-second Russian clip to English with the Best preset, Index 9B translation, and matched Qwen 1.7B voices.

| Run | First completion | Both complete | Peak sampled GPU memory |
| --- | ---: | ---: | ---: |
| Sequential, Persian first | 42.59 s | 86.46 s | 6,450 MiB |
| Two concurrent renders | 62.46 s | 77.67 s | 11,042 MiB |

Two slots gained 11.3% throughput for this pair and delayed the first completion by 19.87 seconds. VRAM was sampled once a second. These are complete warm local renders with existing input files. Download, network transfer, Neon and queue admission are excluded. No second physical GPU was available. Keep two as a ceiling on this profile and start other GPU models at one until measured.
