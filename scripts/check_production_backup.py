"""Exit nonzero when the newest scheduled production backup is stale or invalid."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_production_backup import verify_snapshot
from scripts.scheduled_backup import backup_timestamp


def check_backup(directory: Path, max_age_hours: int, now: datetime | None = None) -> tuple[Path, int]:
    if max_age_hours < 1:
        raise ValueError("허용 지연 시간은 1시간 이상이어야 합니다.")
    directory = directory.resolve(strict=True)
    if not directory.is_dir():
        raise NotADirectoryError(directory)
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidates: list[tuple[datetime, Path]] = []
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        created = backup_timestamp(path.name)
        if created is not None:
            candidates.append((created, path))
    if not candidates:
        raise RuntimeError("스케줄 백업 파일을 찾지 못했습니다.")

    created, latest = max(candidates, key=lambda candidate: candidate[0])
    if created > checked_at + timedelta(minutes=5):
        raise RuntimeError(f"백업 파일의 시각이 현재보다 미래입니다: {latest.name}")
    if created < checked_at - timedelta(hours=max_age_hours):
        raise RuntimeError(f"최근 백업이 허용 지연 시간을 넘었습니다: {latest.name}")
    rows = verify_snapshot(latest)
    return latest, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="최신 스케줄 백업의 시각과 SQLite 무결성 확인")
    parser.add_argument("directory", type=Path, help="스케줄 백업 디렉터리")
    parser.add_argument("--max-age-hours", type=int, required=True, help="운영자가 정한 최대 지연 시간 (1 이상)")
    args = parser.parse_args()
    try:
        path, rows = check_backup(args.directory, args.max_age_hours)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"백업 점검 실패: {exc}\n")
    print(f"백업 점검 통과: {path} ({rows}건, quick_check=ok)")


if __name__ == "__main__":
    main()
