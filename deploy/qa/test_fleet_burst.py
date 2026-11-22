"""Demonstrate a rented worker joining an active queue, taking over, and delivering.

From the repo root, prepare these local Docker images once:
    docker pull postgres:18-alpine
    docker pull mongo:8.0
    docker build --target api -t reeldub-fleet-qa .
Then, including offline:
    python3 deploy/qa/test_fleet_burst.py

Use --image to select an existing local ReelDub API image with server requirements.
The customer API and worker coordinator run on loopback inside the test container.
This exercises scheduling and delivery using synthetic media, without running AI.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", default="reeldub-fleet-qa", help="Locally prepared API image")
    args = parser.parse_args()
    endpoint = os.getenv("DOCKER_HOST") or subprocess.check_output(
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], text=True).strip()
    if not endpoint.startswith(("unix://", "npipe://")):
        parser.exit(2, "Select a local Docker socket for this disposable drill.\n")
    # Use the validated endpoint even if another command changes the default context.
    docker = ["docker", "--host", endpoint, "compose"]
    root = Path(__file__).resolve().parents[2]
    spec = {
        "networks": {"default": {"internal": True}},
        "services": {
            "pg": {"image": "postgres:18-alpine", "tmpfs": ["/var/lib/postgresql"],
                   "environment": {"POSTGRES_HOST_AUTH_METHOD": "trust"},
                   "healthcheck": {"test": ["CMD", "pg_isready", "-U", "postgres"], "interval": "1s", "retries": 30}},
            "mongo": {"image": "mongo:8.0", "tmpfs": ["/data/db"],
                      "healthcheck": {"test": ["CMD", "mongosh", "--quiet", "--eval", "db.adminCommand('ping').ok"],
                                      "interval": "1s", "retries": 30}},
            "qa": {
                "image": args.image, "working_dir": "/app",
                "volumes": [{"type": "bind", "source": str(root / "server"), "target": "/app/server", "read_only": True}],
                "environment": {"REELDUB_TEST_DATABASE": "1", "REELDUB_POSTGRES_DSN": "postgresql://postgres@pg/postgres",
                                "MONGO_URI": "mongodb://mongo:27017/reeldub", "PYTHONDONTWRITEBYTECODE": "1"},
                "depends_on": {name: {"condition": "service_healthy"} for name in ("pg", "mongo")},
                "command": ["python", "-m", "unittest", "-v",
                            "server.test_fleet.FleetIntegration.test_burst_worker_joins_and_recovers_existing_customers_job"],
            },
        },
    }
    with tempfile.TemporaryDirectory(prefix="reeldub-burst-") as tmp:
        spec_path = Path(tmp) / "compose.json"
        spec_path.write_text(json.dumps(spec))
        compose = [*docker, "--project-name", "reeldub-burst-" + uuid4().hex[:12], "--file", str(spec_path)]
        try:
            subprocess.run([*compose, "up", "--pull", "never", "--no-build", "--attach", "qa",
                            "--exit-code-from", "qa"], check=True)
        finally:
            subprocess.run([*compose, "down", "--volumes"], check=True)
    print("Verified live worker enrollment, simultaneous claims, failover to the queue front, "
          "delivery to the original client, and one allowance slot per submission.")


if __name__ == "__main__":
    main()
