#!/usr/bin/env bash
# Linux Landlock profile for the private network worker and its offline media engine.
set -euo pipefail
mode=${1:?Expected engine or worker}
shift
repo=$(cd "$(dirname "$0")/../.." && pwd)
landrun=${REELDUB_LANDRUN:-$HOME/.local/opt/reeldub-host/landrun}
: "${TMPDIR:?A private worker scratch directory is required}" "${REELDUB_GPU_LOCK_DIR:?A shared GPU lock directory is required}"
[[ -c /dev/nvidiactl && -c /dev/nvidia-uvm ]] || { echo "GPU device nodes are unavailable" >&2; exit 1; }
python3 -c 'import ctypes,platform; assert platform.machine() in ("x86_64","aarch64") and ctypes.CDLL(None).syscall(444,0,0,1)>=6'
args=(--best-effort --rox /usr --ro /etc --ro /sys --rw /proc --rox "$repo" --rw "$TMPDIR" --rw "$REELDUB_GPU_LOCK_DIR"
      --ro /dev/urandom --ro /dev/random --rw /dev/null --rw /dev/zero --rw /dev/shm)
for path in "$repo"/{.tools,.venv,.qwen-venv,.omni-venv,.tts-graph} "$HOME/.cache/bandit-infer" "$HOME/.cache/reeldub/index-translate" "$HOME/.cache/local-reeldub" "$HOME/.cache/torch" "$HOME/.cache/huggingface/hub"; do
  [[ ! -e $path ]] || args+=(--rox "$(realpath "$path")")
done
gpu_paths=(/dev/nvidiactl /dev/nvidia-uvm /dev/nvidia-uvm-tools /dev/nvidia-modeset /dev/nvidia-caps /dev/dri)
# A nested sandbox can open GPU devices but cannot enumerate the host's /dev directory.
for info in /proc/driver/nvidia/gpus/*/information; do
    [[ -r $info ]] || continue
    minor=$(awk '/^Device Minor:/ {print $NF}' "$info")
    [[ ! $minor =~ ^[0-9]+$ ]] || gpu_paths+=("/dev/nvidia$minor")
done
for path in "${gpu_paths[@]}"; do [[ ! -e $path ]] || args+=(--rw "$path"); done
for name in HOME PATH LANG LC_ALL TMPDIR CUDA_VISIBLE_DEVICES REELDUB_LANDRUN LD_LIBRARY_PATH HF_HOME XDG_CACHE_HOME OMP_NUM_THREADS MKL_NUM_THREADS PYTHONUNBUFFERED HF_HUB_OFFLINE REELDUB_GPU_LOCK_DIR REELDUB_TTS_BATCH_SIZE REELDUB_FREE_CPU_ASR REELDUB_METADATA_FILE REELDUB_SERVICE_LIMITS REELDUB_STAGED_INPUT REELDUB_MAX_SECONDS; do
  [[ ! -v $name ]] || args+=(--env "$name")
done
args+=(--env PYTHONDONTWRITEBYTECODE=1 --env "CUDA_CACHE_PATH=$TMPDIR/cuda-cache" --env "TRITON_CACHE_DIR=$TMPDIR/triton" --env "TORCHINDUCTOR_CACHE_DIR=$TMPDIR/inductor" --env "MPLCONFIGDIR=$TMPDIR/matplotlib" --env "NUMBA_CACHE_DIR=$TMPDIR/numba")
seccomp_path="$HOME/.local/opt/reeldub-host/seccomp-package/usr/lib/python3/dist-packages"
case "$mode" in
    engine|preflight)
        if [[ $mode == preflight ]]; then
            set -- "$repo/.qwen-venv/bin/python" -c "import torch; assert torch.ones(1, device='cuda').item() == 1"
        else
            set -- "$repo/dub" "$@"
        fi
        args+=(--env REELDUB_OFFLINE_ENGINE=1)
        export PYTHONPATH="$seccomp_path"
        exec /usr/bin/python3 "$repo/deploy/fleet/engine-network.py" engine "$landrun" "${args[@]}" -- "$@"
        ;;
    worker)
        : "${REELDUB_SERVICE_PYTHON:?}" "${REELDUB_FLEET_CA:?}" "${REELDUB_FLEET_CERT:?}" "${REELDUB_FLEET_KEY:?}" "${REELDUB_FLEET_TOKEN_FILE:?}"
        [[ ! -d $seccomp_path ]] || args+=(--rox "$seccomp_path")
        port=$(python3 -c 'from urllib.parse import urlsplit; import sys; print(urlsplit(sys.argv[1]).port or 443)' "$REELDUB_FLEET_URL")
        args+=(--connect-tcp "$port" --rox "$landrun" --rox "$(dirname "$(dirname "$REELDUB_SERVICE_PYTHON")")"
               --ro "$REELDUB_FLEET_CA" --ro "$REELDUB_FLEET_CERT" --ro "$REELDUB_FLEET_KEY" --ro "$REELDUB_FLEET_TOKEN_FILE"
               --env "REELDUB_ENGINE=$repo/deploy/fleet/isolated-engine.sh")
        for name in REELDUB_FLEET_URL REELDUB_FLEET_CA REELDUB_FLEET_CERT REELDUB_FLEET_KEY REELDUB_FLEET_TOKEN_FILE REELDUB_FLEET_SLOT REELDUB_FLEET_NODE REELDUB_GPU_SCHEDULER REELDUB_GPU_PIPELINE_SLOTS REELDUB_DEVICE_ID REELDUB_LOCAL_GPU_METRICS; do
            args+=(--env "$name")
        done
        export PYTHONPATH="$seccomp_path"
        exec /usr/bin/python3 "$repo/deploy/fleet/engine-network.py" worker "$landrun" "${args[@]}" -- "$REELDUB_SERVICE_PYTHON" -m server.fleet_worker
        ;;
    *) exit 2 ;;
esac
