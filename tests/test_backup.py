"""Online backup and restore checks."""
from __future__ import annotations

import sqlite3
import shutil
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.app import RunStore
from camhal_sim.runner import run_fault
from scripts.backup_runs import backup
from scripts.check_production_backup import check_backup
from scripts.export_production_backup import export_backup
from scripts.prepare_restore_volume import prepare_restore_volume
from scripts.scheduled_backup import prune_backups, scheduled_backup


class BackupTest(unittest.TestCase):
    def test_restore_prepare_rejects_invalid_snapshot_before_creating_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            invalid = Path(tmp) / "invalid.sqlite3"
            invalid.write_bytes(b"not a backup")
            with patch("scripts.prepare_restore_volume.subprocess.run") as docker:
                with self.assertRaises(sqlite3.DatabaseError):
                    prepare_restore_volume(invalid)
                docker.assert_not_called()

    def test_backup_monitor_checks_latest_snapshot_and_rejects_stale_or_corrupt(self):
        now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "runs.sqlite3"
            store = RunStore(10, source)
            store.put(run_fault("F04", seed=7))
            recent = directory / ("runs-20260924T110000Z-" + "a" * 32 + ".sqlite3")
            backup(source, recent)
            self.assertEqual(check_backup(directory, 2, now), (recent.resolve(), 1))
            with self.assertRaisesRegex(RuntimeError, "허용 지연"):
                check_backup(directory, 1, datetime(2026, 9, 24, 13, 1, tzinfo=timezone.utc))

            corrupt = directory / ("runs-20260924T120000Z-" + "b" * 32 + ".sqlite3")
            corrupt.write_bytes(b"not a SQLite database")
            with self.assertRaises(sqlite3.DatabaseError):
                check_backup(directory, 2, now)
            corrupt.unlink()
            future = directory / ("runs-20260924T130000Z-" + "c" * 32 + ".sqlite3")
            future.symlink_to(recent)
            self.assertEqual(check_backup(directory, 2, now), (recent.resolve(), 1))
            future.unlink()
            future.write_bytes(b"not checked because the timestamp is invalid")
            with self.assertRaisesRegex(RuntimeError, "미래"):
                check_backup(directory, 2, now)
            store.close()

    def test_backup_monitor_requires_a_scheduled_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "runs-20260924.sqlite3").write_bytes(b"manual")
            with self.assertRaisesRegex(RuntimeError, "찾지 못했습니다"):
                check_backup(directory, 24)
            with self.assertRaises(ValueError):
                check_backup(directory, 0)

    def test_scheduled_backup_prunes_only_expired_owned_files_after_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            old = directory / ("runs-20260101T000000Z-" + "a" * 32 + ".sqlite3")
            recent = directory / ("runs-20260923T000000Z-" + "b" * 32 + ".sqlite3")
            manual = directory / "runs-20260101.sqlite3"
            link = directory / ("runs-20260101T000000Z-" + "c" * 32 + ".sqlite3")
            for path in (old, recent, manual):
                path.write_bytes(b"test")
            link.symlink_to(manual)
            now = datetime(2026, 9, 24, tzinfo=timezone.utc)

            def fake_export(destination, _command):
                destination.write_bytes(b"snapshot")
                return 3

            with patch("scripts.scheduled_backup.export_backup", side_effect=fake_export), \
                 patch("scripts.scheduled_backup.compose_command", return_value=["docker-compose"]), \
                 patch("scripts.scheduled_backup.ROOT", directory):
                (directory / ".env.production").write_text("test")
                destination, rows, removed = scheduled_backup(directory, 7, now)
            self.assertEqual((rows, removed), (3, 1))
            self.assertTrue(destination.is_file())
            self.assertFalse(old.exists())
            for path in (recent, manual, link):
                self.assertTrue(path.is_symlink() or path.is_file())
            self.assertEqual(prune_backups(directory, 7, now), 0)

    def test_scheduled_backup_does_not_prune_after_failed_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            old = directory / ("runs-20260101T000000Z-" + "a" * 32 + ".sqlite3")
            old.write_bytes(b"test")
            (directory / ".env.production").write_text("test")
            with patch("scripts.scheduled_backup.export_backup", side_effect=OSError("copy failed")), \
                 patch("scripts.scheduled_backup.compose_command", return_value=["docker-compose"]), \
                 patch("scripts.scheduled_backup.ROOT", directory):
                with self.assertRaisesRegex(OSError, "copy failed"):
                    scheduled_backup(directory, 7, datetime(2026, 9, 24, tzinfo=timezone.utc))
            self.assertTrue(old.exists())

    def test_export_verifies_host_copy_and_removes_volume_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "runs.sqlite3"
            snapshot = Path(tmp) / "snapshot.sqlite3"
            exported = Path(tmp) / "exported.sqlite3"
            store = RunStore(10, source)
            rid = store.put(run_fault("F04", seed=7), owner="user-a")
            backup(source, snapshot)
            calls = []

            def fake_run(command, **_kwargs):
                calls.append(command)
                if "cp" in command:
                    shutil.copyfile(snapshot, command[-1])
                return subprocess.CompletedProcess(command, 0)

            with patch("scripts.export_production_backup.subprocess.run", side_effect=fake_run):
                self.assertEqual(export_backup(exported, ["docker", "compose"]), 1)
                with self.assertRaises(FileExistsError):
                    export_backup(exported, ["docker", "compose"])
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[0][2:7], ["exec", "-T", "camhal", "python", "scripts/backup_runs.py"])
            self.assertEqual(calls[-1][2:7], ["exec", "-T", "camhal", "rm", "-f"])
            self.assertEqual(exported.stat().st_mode & 0o777, 0o600)
            restored = RunStore(10, exported)
            self.assertEqual(restored.get(rid, "user-a")["payload"]["id"], rid)
            restored.close()
            store.close()

    def test_failed_export_does_not_publish_backup_and_cleans_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "failed.sqlite3"
            calls = []

            def fake_run(command, **_kwargs):
                calls.append(command)
                if "cp" in command:
                    raise subprocess.CalledProcessError(1, command)
                return subprocess.CompletedProcess(command, 0)

            with patch("scripts.export_production_backup.subprocess.run", side_effect=fake_run):
                with self.assertRaises(subprocess.CalledProcessError):
                    export_backup(destination, ["docker", "compose"])
            self.assertFalse(destination.exists())
            self.assertEqual(calls[-1][2:7], ["exec", "-T", "camhal", "rm", "-f"])

    def test_export_reports_volume_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "runs.sqlite3"
            snapshot = Path(tmp) / "snapshot.sqlite3"
            destination = Path(tmp) / "exported.sqlite3"
            store = RunStore(1, source)
            backup(source, snapshot)

            def fake_run(command, **_kwargs):
                if "cp" in command:
                    shutil.copyfile(snapshot, command[-1])
                return subprocess.CompletedProcess(command, 1 if "rm" in command else 0)

            with patch("scripts.export_production_backup.subprocess.run", side_effect=fake_run):
                with patch("sys.stderr"):
                    with self.assertRaisesRegex(RuntimeError, "임시 백업"):
                        export_backup(destination, ["docker", "compose"])
            self.assertTrue(destination.exists())
            store.close()

    def test_backup_restores_completed_snapshot_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "runs.sqlite3"
            destination = Path(tmp) / "backup.sqlite3"
            after_deletion = Path(tmp) / "backup-after-delete.sqlite3"
            store = RunStore(10, source)
            self.assertEqual(store._db.execute("PRAGMA secure_delete").fetchone()[0], 1)
            original = store.put(run_fault("F04", seed=7), owner="user-a")

            self.assertEqual(backup(source, destination), 1)
            newer = store.put(run_fault("F15", seed=7), owner="user-a")
            restored = RunStore(10, destination)
            self.assertEqual(restored.get(original, "user-a")["payload"]["id"], original)
            self.assertEqual(restored.count("user-a"), 1)
            self.assertEqual(restored.items("user-a")[0]["id"], original)
            self.assertEqual(store.get(newer, "user-a")["payload"]["id"], newer)
            self.assertEqual(store.delete_all("user-a"), 2)
            self.assertEqual(backup(source, after_deletion), 0)
            with closing(sqlite3.connect(after_deletion)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
            with closing(sqlite3.connect(destination)) as db:
                self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
            with self.assertRaises(FileExistsError):
                backup(source, destination)
            restored.close()
            store.close()
