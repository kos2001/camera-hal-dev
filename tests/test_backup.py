"""Online backup and restore checks."""
from __future__ import annotations

import sqlite3
import shutil
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend.app import RunStore
from camhal_sim.runner import run_fault
from scripts.backup_runs import backup
from scripts.export_production_backup import export_backup


class BackupTest(unittest.TestCase):
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
