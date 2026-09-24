"""Create and validate an online SQLite backup of camhal-sim runs."""
from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.parse import quote


def backup(source: Path, destination: Path) -> int:
    source = source.resolve(strict=True)
    destination = destination.resolve(strict=False)
    if source == destination:
        raise ValueError("백업 대상은 원본 DB와 달라야 합니다.")
    if destination.exists():
        raise FileExistsError(f"백업 파일이 이미 존재합니다: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"백업 디렉터리가 없습니다: {destination.parent}")

    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temporary = Path(temp_name)
    try:
        uri = f"file:{quote(str(source))}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as src, closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst)
            result = dst.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise sqlite3.DatabaseError(f"백업 무결성 검사 실패: {result}")
            rows = dst.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        # 같은 디렉터리 내 하드 링크는 파일을 덮어쓰지 않고 완성본만 공개한다.
        os.link(temporary, destination)
        return rows
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="실행 기록 SQLite DB의 온라인 백업")
    parser.add_argument("destination", type=Path, help="새 백업 파일 경로 (기존 파일은 덮어쓰지 않음)")
    parser.add_argument("--db", type=Path,
                        default=Path(os.getenv("CAMHAL_DB_PATH", "data/runs.sqlite3")),
                        help="원본 DB 경로")
    args = parser.parse_args()
    rows = backup(args.db, args.destination)
    print(f"백업 완료: {args.destination} ({rows}건, quick_check=ok)")


if __name__ == "__main__":
    main()
