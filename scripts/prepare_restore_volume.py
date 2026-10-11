"""Validate a snapshot and copy it into a fresh Docker volume for recovery."""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_production_backup import verify_snapshot


COPY_AND_VERIFY = """
import os
import shutil
import sqlite3
from pathlib import Path

directory = Path('/var/lib/camhal')
destination = directory / 'runs.sqlite3'
if any(directory.iterdir()):
    raise RuntimeError('Restore volume must be empty')
os.chown(directory, 10001, 10001)
os.chmod(directory, 0o700)
with open('/restore.sqlite3', 'rb') as source, destination.open('xb') as target:
    shutil.copyfileobj(source, target)
    target.flush()
    os.fsync(target.fileno())
os.chown(destination, 10001, 10001)
os.chmod(destination, 0o600)
with sqlite3.connect('file:/var/lib/camhal/runs.sqlite3?mode=ro', uri=True) as db:
    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise RuntimeError('Restored SQLite file failed quick_check')
    db.execute('SELECT COUNT(*) FROM runs').fetchone()
"""


def prepare_restore_volume(snapshot: Path, image: str = "python:3.12-slim") -> tuple[str, int]:
    if snapshot.is_symlink() or not snapshot.is_file():
        raise ValueError("백업 경로는 일반 파일이어야 합니다.")
    snapshot = snapshot.resolve(strict=True)
    rows = verify_snapshot(snapshot)
    volume = f"camhal-restore-{uuid.uuid4().hex}"
    subprocess.run(["docker", "volume", "create", volume], check=True,
                   stdout=subprocess.DEVNULL)
    try:
        subprocess.run([
            "docker", "run", "--rm", "--user", "0", "--network", "none",
            "-v", f"{volume}:/var/lib/camhal",
            "-v", f"{snapshot}:/restore.sqlite3:ro",
            image, "python", "-c", COPY_AND_VERIFY,
        ], check=True, stdout=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"복원 볼륨 준비 실패 ({volume}); 내용을 확인한 뒤 직접 정리하세요.") from exc
    return volume, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="검증된 백업을 새 Docker 볼륨에 복원")
    parser.add_argument("snapshot", type=Path, help="복원할 SQLite 백업 파일")
    args = parser.parse_args()
    try:
        volume, rows = prepare_restore_volume(args.snapshot)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"복원 준비 실패: {exc}\n")
    print(volume)
    print(f"복원 볼륨 준비 완료: {volume} ({rows}건, 원본 볼륨 보존)", file=sys.stderr)


if __name__ == "__main__":
    main()
