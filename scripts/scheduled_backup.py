"""Export a production snapshot and expire this scheduler's old snapshots."""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_production_backup import compose_command, export_backup


BACKUP_NAME = re.compile(r"^runs-(\d{8}T\d{6}Z)-([0-9a-f]{32})\.sqlite3$")


def backup_timestamp(name: str) -> datetime | None:
    match = BACKUP_NAME.fullmatch(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def prune_backups(directory: Path, retain_days: int, now: datetime) -> int:
    """Remove only expired regular files bearing this scheduler's exact name."""
    if retain_days < 1:
        raise ValueError("보관 기간은 1일 이상이어야 합니다.")
    cutoff = now.astimezone(timezone.utc) - timedelta(days=retain_days)
    removed = 0
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        created = backup_timestamp(path.name)
        if created is None:
            continue
        if created < cutoff:
            path.unlink()
            removed += 1
    return removed


def scheduled_backup(directory: Path, retain_days: int, now: datetime | None = None) -> tuple[Path, int, int]:
    if retain_days < 1:
        raise ValueError("보관 기간은 1일 이상이어야 합니다.")
    directory = directory.resolve(strict=True)
    if not directory.is_dir():
        raise NotADirectoryError(directory)
    if not (ROOT / ".env.production").is_file():
        raise FileNotFoundError(".env.production 파일이 없습니다.")

    lock_path = directory / ".camhal-backup.lock"
    with open(lock_path, "a+b", opener=lambda path, flags: os.open(path, flags, 0o600)) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("다른 백업 작업이 실행 중입니다.") from exc
        created = now or datetime.now(timezone.utc)
        filename = f"runs-{created.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}.sqlite3"
        destination = directory / filename
        rows = export_backup(destination, compose_command())
        removed = prune_backups(directory, retain_days, created)
        return destination, rows, removed


def main() -> None:
    parser = argparse.ArgumentParser(description="운영 실행 기록 백업 및 만료된 스케줄 백업 정리")
    parser.add_argument("directory", type=Path, help="존재하는 호스트 백업 디렉터리")
    parser.add_argument("--retain-days", type=int, required=True, help="운영자가 정한 보관 일수 (1 이상)")
    args = parser.parse_args()
    try:
        destination, rows, removed = scheduled_backup(args.directory, args.retain_days)
    except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"백업 실패: {exc}\n")
    print(f"백업 완료: {destination} ({rows}건, 만료 파일 {removed}개 정리)")


if __name__ == "__main__":
    main()
