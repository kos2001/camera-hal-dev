"""Exercise production backup export against a disposable Compose service."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app import RunStore
from scripts.check_production_backup import check_backup
from scripts.export_production_backup import export_backup


def main() -> None:
    parser = argparse.ArgumentParser(description="컨테이너 백업 내보내기 검증")
    parser.add_argument("image", nargs="?", default="camhal-sim:local")
    args = parser.parse_args()
    project = f"camhal-backup-{uuid.uuid4().hex[:8]}"

    with tempfile.TemporaryDirectory(prefix="camhal-backup-smoke-") as tmp:
        root = Path(tmp)
        compose_file = root / "compose.yaml"
        compose_file.write_text("services:\n  camhal:\n    image: " + json.dumps(args.image) +
                                "\n    environment:\n      CAMHAL_ENV: development"
                                "\n      CAMHAL_AUTH_MODE: local" +
                                "\n    volumes:\n      - data:/var/lib/camhal\nvolumes:\n  data: {}\n")
        probe = subprocess.run(["docker", "compose", "version"], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        executable = ["docker", "compose"] if probe.returncode == 0 else ["docker-compose"]
        command = [*executable, "-p", project, "-f", str(compose_file)]
        try:
            subprocess.run([*command, "up", "-d"], check=True, stdout=subprocess.DEVNULL)
            for _ in range(20):
                ready = subprocess.run([*command, "exec", "-T", "camhal", "python", "-c",
                                        'import urllib.request; urllib.request.urlopen('
                                        '"http://127.0.0.1:8010/api/ready", timeout=2)'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Backup smoke service did not become ready")

            subprocess.run([*command, "exec", "-T", "camhal", "python", "-c",
                            'from backend.app import store; from camhal_sim.runner import run_fault; '
                            'store.put(run_fault("F04", seed=7))'], check=True)
            destination = root / f"runs-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}.sqlite3"
            rows = export_backup(destination, command)
            checked, checked_rows = check_backup(root, 24)
            if checked != destination.resolve() or checked_rows != rows:
                raise RuntimeError("Exported backup failed freshness or integrity monitoring")
            restored = RunStore(10, destination)
            try:
                if rows != 1 or restored.count() != 1:
                    raise RuntimeError("Exported backup did not restore the expected run")
            finally:
                restored.close()
            names = subprocess.check_output([*command, "exec", "-T", "camhal", "ls",
                                             "/var/lib/camhal"], text=True)
            if "runs-export-" in names:
                raise RuntimeError("Temporary backup remained in the application volume")
            print("Backup smoke passed: copied snapshot, restored run, volume temporary removed")
        finally:
            subprocess.run([*command, "down", "-v"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
