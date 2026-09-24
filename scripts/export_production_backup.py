"""Export an online production backup outside the application volume."""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent.parent


def compose_command() -> list[str]:
    try:
        result = subprocess.run(["docker", "compose", "version"], check=False,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        command = ["docker", "compose"] if result.returncode == 0 else ["docker-compose"]
    except FileNotFoundError:
        command = ["docker-compose"]
    return [*command, "--env-file", str(ROOT / ".env.production"),
            "-f", str(ROOT / "compose.production.yaml")]


def verify_snapshot(path: Path) -> int:
    uri = f"file:{quote(str(path.resolve(strict=True)))}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        result = db.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise sqlite3.DatabaseError(f"복사한 백업의 무결성 검사 실패: {result}")
        return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def export_backup(destination: Path, command: list[str]) -> int:
    destination = destination.resolve(strict=False)
    if destination.exists():
        raise FileExistsError(f"백업 파일이 이미 존재합니다: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"백업 디렉터리가 없습니다: {destination.parent}")

    remote_path = f"/var/lib/camhal/runs-export-{uuid.uuid4().hex}.sqlite3"
    with tempfile.TemporaryDirectory(prefix=".camhal-export-", dir=destination.parent) as tmp:
        local_copy = Path(tmp) / "runs.sqlite3"
        try:
            subprocess.run([*command, "exec", "-T", "camhal", "python", "scripts/backup_runs.py",
                            remote_path], cwd=ROOT, check=True)
            subprocess.run([*command, "cp", f"camhal:{remote_path}", str(local_copy)],
                           cwd=ROOT, check=True)
            rows = verify_snapshot(local_copy)
            os.chmod(local_copy, 0o600)
            with local_copy.open("rb") as completed:
                os.fsync(completed.fileno())
            os.link(local_copy, destination)
            return rows
        finally:
            cleanup_error = None
            try:
                cleanup = subprocess.run([*command, "exec", "-T", "camhal", "rm", "-f", remote_path],
                                         cwd=ROOT, check=False, capture_output=True, text=True)
                if cleanup.returncode:
                    cleanup_error = f"컨테이너 임시 백업을 삭제하지 못했습니다: {remote_path}"
            except OSError as exc:
                cleanup_error = f"컨테이너 임시 백업을 삭제하지 못했습니다: {remote_path} ({exc})"
            if cleanup_error:
                print(f"경고: {cleanup_error}", file=sys.stderr)
                if sys.exc_info()[0] is None:
                    raise RuntimeError(cleanup_error)


def main() -> None:
    parser = argparse.ArgumentParser(description="운영 컨테이너의 실행 기록을 호스트로 백업")
    parser.add_argument("destination", type=Path, help="새 호스트 백업 파일 경로")
    args = parser.parse_args()
    if not (ROOT / ".env.production").is_file():
        parser.error(".env.production 파일이 없습니다.")
    rows = export_backup(args.destination, compose_command())
    print(f"백업 완료: {args.destination} ({rows}건, 복사본 quick_check=ok)")


if __name__ == "__main__":
    main()
