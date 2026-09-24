"""Exercise production backup export against a disposable Compose service."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.check_production_backup import check_backup
from scripts.export_production_backup import export_backup
from scripts.prepare_restore_volume import prepare_restore_volume


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
                                "\n    volumes:\n      - camhal-data:/var/lib/camhal\nvolumes:\n  camhal-data: {}\n")
        probe = subprocess.run(["docker", "compose", "version"], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        executable = ["docker", "compose"] if probe.returncode == 0 else ["docker-compose"]
        command = [*executable, "-p", project, "-f", str(compose_file)]
        restored_command = None
        restore_env = None
        restore_volume = None
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

            run_id = subprocess.check_output(
                [*command, "exec", "-T", "camhal", "python", "-c",
                 'from backend.app import store; from camhal_sim.runner import run_fault; '
                 'print(store.put(run_fault("F04", seed=7)))'], text=True).strip()
            destination = root / f"runs-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}.sqlite3"
            rows = export_backup(destination, command)
            checked, checked_rows = check_backup(root, 24)
            if checked != destination.resolve() or checked_rows != rows:
                raise RuntimeError("Exported backup failed freshness or integrity monitoring")
            if rows != 1:
                raise RuntimeError("Exported backup did not contain the expected run")
            names = subprocess.check_output([*command, "exec", "-T", "camhal", "ls",
                                             "/var/lib/camhal"], text=True)
            if "runs-export-" in names:
                raise RuntimeError("Temporary backup remained in the application volume")

            restore_volume, copied_rows = prepare_restore_volume(destination)
            if copied_rows != rows:
                raise RuntimeError("Restored volume row count differs from the snapshot")
            restored_command = [*executable, "-p", f"{project}-restored", "-f", str(compose_file),
                                "-f", str(Path(__file__).resolve().parent.parent /
                                           "deploy" / "restore-volume.override.yaml")]
            restore_env = {**os.environ, "CAMHAL_RESTORE_VOLUME": restore_volume}
            subprocess.run([*restored_command, "up", "-d"], check=True, stdout=subprocess.DEVNULL,
                           env=restore_env)
            probe_run = (
                'import json, urllib.request; base="http://127.0.0.1:8010"; '
                'assert json.load(urllib.request.urlopen(base+"/api/ready", timeout=2))["ok"]; '
                f'assert json.load(urllib.request.urlopen(base+"/api/runs/{run_id}", timeout=2))["id"] == "{run_id}"')
            for _ in range(20):
                result = subprocess.run([*restored_command, "exec", "-T", "camhal", "python", "-c", probe_run],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        env=restore_env)
                if result.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Restored service could not read the backed-up run")
            print("Backup smoke passed: exported snapshot, restored service read the run, volume temporary removed")
        finally:
            if restored_command is not None:
                subprocess.run([*restored_command, "down"], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=restore_env)
            if restore_volume is not None:
                subprocess.run(["docker", "volume", "rm", restore_volume], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run([*command, "down", "-v"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
