# More than one GPU

The single-host [Compose setup](README.md#quick-start) starts one worker on one NVIDIA GPU. A separate GPU fleet is useful when different machines need to render jobs from the same private queue. Clients use one server URL as hardware comes and goes.

A fleet deployment has a coordinator, a private media store, an isolated downloader and one or more GPU nodes. The coordinator stages an input once. Each idle GPU slot requests a job over an authenticated private connection. The worker receives the source for its lease, generates the dub locally and uploads the result. The coordinator accepts a result only while that lease is current.

Nodes send heartbeats. When a node stops responding, its unfinished job can move to another healthy worker with its attempt fenced and its recovery ahead of new jobs. One healthy physical node can process work while another is offline. See the [worker installation, certificates and recovery guide](deploy/fleet/README.md).

## Capacity

Start with one process per card. Measure elapsed time, total clips per hour and peak VRAM on your own material before increasing concurrency. A previous two-clip trial on a **16 GB RTX 4070 Ti SUPER** completed both clips in 86.46 seconds sequentially and 77.67 seconds with two render slots. The first completion slowed from 42.59 to 62.46 seconds, and peak sampled memory rose from 6,450 MiB to 11,042 MiB. Those timings used a previous configuration and warm local sources. The current Kitten vLLM process has different memory needs.

To add an independent GPU, provision the same weights, a unique node identity and private coordinator credentials. Run a real dub and check that its reported voice engine and output match the requested configuration before relying on its capacity.
