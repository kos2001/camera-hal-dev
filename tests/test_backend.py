"""백엔드 API 테스트 — `.venv/bin/python -m unittest discover -s tests` (fastapi 없으면 건너뜀)."""
from __future__ import annotations

import sys
import json
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    from backend.app import MAX_ACTIVE_RUNS, RunStore, _run_slots, _sweep_lock, app
    from camhal_sim.runner import run_fault
except ImportError:  # 서버 의존성 미설치 환경
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi 미설치 (pip install -r requirements-server.txt)")
class BackendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)

    def test_catalog(self):
        response = self.c.get("/api/catalog")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        cat = response.json()
        self.assertEqual(len(cat["faults"]), 37)
        self.assertEqual(len(cat["platforms"]), 10)
        self.assertEqual(len(cat["categories"]), 10)
        self.assertTrue(cat["checkers"])

    def test_html_security_policy_keeps_scripts_out_of_reports(self):
        home = self.c.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("script-src 'self'", home.headers["Content-Security-Policy"])
        self.assertIn("img-src 'self' data:", home.headers["Content-Security-Policy"])
        self.assertEqual(home.headers["Cache-Control"], "no-store")
        self.assertIn("camera=()", home.headers["Permissions-Policy"])

        run = self.c.post("/api/faults/F04/run", json={"seed": 7}).json()
        report = self.c.get(f"/api/runs/{run['id']}/report.html")
        self.assertEqual(report.status_code, 200)
        self.assertIn("script-src 'none'", report.headers["Content-Security-Policy"])
        self.assertIn("style-src 'unsafe-inline'", report.headers["Content-Security-Policy"])
        self.assertIn("<style>", report.text)
        self.assertNotIn("Content-Security-Policy", self.c.get("/docs").headers)

    def test_readiness_reflects_storage(self):
        self.assertEqual(self.c.get("/api/health").status_code, 200)
        self.assertEqual(self.c.get("/api/ready").status_code, 200)
        with tempfile.TemporaryDirectory() as tmp:
            unavailable = RunStore(1, Path(tmp) / "runs.sqlite3")
            unavailable.close()
            with patch("backend.app.store", unavailable):
                self.assertEqual(self.c.get("/api/ready").status_code, 503)
                self.assertEqual(self.c.get("/api/health").status_code, 200)

    def test_disk_full_returns_controlled_error_without_partial_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            full = RunStore(10, Path(tmp) / "runs.sqlite3")
            pages = full._db.execute("PRAGMA page_count").fetchone()[0]
            full._db.execute(f"PRAGMA max_page_count={pages}")
            with patch("backend.app.store", full):
                self.assertEqual(self.c.get("/api/ready").status_code, 503)
                with self.assertLogs("uvicorn.error", level="ERROR") as logs:
                    response = self.c.post("/api/faults/F04/run", json={"seed": 7})
                self.assertEqual(response.status_code, 507)
                self.assertIn("저장 공간", response.json()["detail"])
                self.assertEqual(full.count(), 0)
                self.assertIn('"sqlite_code": 13', logs.output[0])
            full.close()

    def test_unavailable_storage_returns_retryable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            unavailable = RunStore(1, Path(tmp) / "runs.sqlite3")
            unavailable.close()
            with patch("backend.app.store", unavailable):
                with self.assertLogs("uvicorn.error", level="ERROR") as logs:
                    response = self.c.get("/api/runs")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["Retry-After"], "5")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(len(response.headers["X-Request-ID"]), 32)
            self.assertEqual(response.json()["detail"], "저장소를 사용할 수 없습니다. 잠시 후 다시 시도하세요.")
            self.assertIn('"event": "storage_error"', logs.output[0])

    def test_compare_storage_is_atomic_on_second_insert_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = RunStore(10, Path(tmp) / "runs.sqlite3")
            db._db.execute("CREATE TRIGGER fail_second_run BEFORE INSERT ON runs "
                           "WHEN (SELECT COUNT(*) FROM runs) = 1 "
                           "BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END")
            with patch("backend.app.store", db):
                with self.assertLogs("uvicorn.error", level="ERROR"):
                    response = self.c.post("/api/faults/F04/compare", json={"seed": 7})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(db.count(), 0)
            db.close()

    def test_request_id_and_safe_access_log(self):
        supplied = "12345678-1234-1234-1234-123456789abc"
        with self.assertLogs("uvicorn.error", level="INFO") as logs:
            response = self.c.get("/api/catalog", headers={
                "X-Request-ID": supplied, "Authorization": "Bearer should-not-appear-in-log"})
        self.assertEqual(response.headers["X-Request-ID"], supplied.replace("-", ""))
        record = json.loads(logs.output[-1].split(":", 2)[2])
        self.assertEqual(record["request_id"], response.headers["X-Request-ID"])
        self.assertEqual(record["route"], "/api/catalog")
        self.assertEqual(record["status"], 200)
        self.assertNotIn("should-not-appear", logs.output[-1])
        invalid = self.c.get("/api/health", headers={"X-Request-ID": "not-a-uuid"})
        self.assertEqual(len(invalid.headers["X-Request-ID"]), 32)

    def test_compare_verdicts(self):
        r = self.c.post("/api/faults/F04/compare", json={"seed": 7})
        self.assertEqual(r.status_code, 200)
        faulty, fixed = r.json()["faulty"], r.json()["fixed"]
        self.assertTrue(faulty["verdict"]["detected"])
        self.assertTrue(fixed["verdict"]["clean"])
        self.assertTrue(faulty["series"]["frames"][0]["points"])
        self.assertTrue(any(l["kind"] == "result" for l in faulty["timeline"]["lanes"]))

    def test_run_and_run_resources(self):
        run = self.c.post("/api/run", json={"platform": "EMUC", "workload": "partial_results_order",
                                            "defects": ["F21"], "seed": 3}).json()
        self.assertIn("PARTIAL_OUT_OF_ORDER", [v["code"] for v in run["violations"]])
        rid = run["id"]
        self.assertEqual(self.c.get(f"/api/runs/{rid}").json()["id"], rid)
        page = self.c.get(f"/api/runs/{rid}/events", params={"kind": "partial_result", "limit": 5}).json()
        self.assertEqual(len(page["items"]), 5)
        self.assertGreater(page["total"], 5)
        self.assertIn("*대상 플랫폼*: EMU-CameraHAL", self.c.get(f"/api/runs/{rid}/issue").json()["description"])
        self.assertIn("<svg", self.c.get(f"/api/runs/{rid}/report.html").text)

    def test_fault_on_other_platform(self):
        run = self.c.post("/api/faults/F15/run", json={"platform": "EMU-CameraHAL", "seed": 7}).json()
        self.assertEqual(run["platform"]["code"], "EMU-CameraHAL")
        self.assertTrue(run["verdict"]["detected"])

    def test_errors(self):
        self.assertEqual(self.c.post("/api/run", json={"platform": "EMUC", "workload": "nope"}).status_code, 404)
        self.assertEqual(self.c.post("/api/run", json={"platform": "NOPE", "workload": "preview"}).status_code, 404)
        self.assertEqual(self.c.post("/api/run", json={"platform": "EMUC", "workload": "preview",
                                                       "defects": ["bogus"]}).status_code, 422)
        self.assertEqual(self.c.post("/api/faults/F99/run", json={}).status_code, 404)
        self.assertEqual(self.c.post("/api/run", json={"platform": "EMUC", "workload": "preview",
                                                       "seed": -1}).status_code, 422)
        self.assertEqual(self.c.get("/api/runs/unknown").status_code, 404)
        self.assertEqual(self.c.get("/api/nope").status_code, 404)

    def test_sweep_subset(self):
        s = self.c.post("/api/sweep", json={"seed": 7, "only": ["F00", "F25"]}).json()
        self.assertEqual((s["ok"], s["total"]), (2, 2))

    def test_run_store_survives_reopen_and_eviction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            first = RunStore(1, path)
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            rid = first.put(run_fault("F04", seed=7))
            for suffix in ("-wal", "-shm"):
                journal = Path(str(path) + suffix)
                if journal.exists():
                    self.assertEqual(journal.stat().st_mode & 0o077, 0)
            self.assertEqual(first.get(rid)["payload"]["id"], rid)
            first.close()

            reopened = RunStore(1, path)
            self.assertEqual(reopened.get(rid)["payload"]["id"], rid)
            self.assertTrue(reopened.get(rid)["events"])
            self.assertIn("<svg", reopened.get(rid)["report"])
            newer = reopened.put(run_fault("F15", seed=7))
            self.assertEqual(reopened.count(), 1)
            self.assertEqual(reopened.items()[0]["id"], newer)
            self.assertIn("created_at", reopened.items()[0])
            self.assertEqual(self.c.get("/api/runs/unknown").status_code, 404)
            reopened.close()

    def test_sweep_input_is_bounded(self):
        self.assertEqual(self.c.post("/api/sweep", json={"only": ["F00"] * 38}).status_code, 422)
        self.assertEqual(self.c.post("/api/sweep", json={"only": ["F99"]}).status_code, 422)

    def test_store_migrates_existing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE runs (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, events TEXT NOT NULL, "
                       "issue TEXT NOT NULL, report TEXT NOT NULL)")
            db.commit()
            db.close()
            migrated = RunStore(2, path)
            rid = migrated.put(run_fault("F04", seed=1))
            self.assertEqual(migrated.items()[0]["id"], rid)
            migrated.close()

    def test_compressed_storage_reads_legacy_text_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            current = RunStore(2, path)
            rid = current.put(run_fault("F04", seed=7))
            self.assertEqual(current._db.execute("SELECT typeof(events) FROM runs WHERE id = ?",
                                                 (rid,)).fetchone()[0], "blob")
            original = current.get(rid)
            stored_size = current._db.execute("SELECT length(events) FROM runs WHERE id = ?",
                                              (rid,)).fetchone()[0]
            self.assertLess(stored_size, len(json.dumps(original["events"]).encode()) // 2)
            with current._db:
                current._db.execute("UPDATE runs SET payload = ?, events = ?, issue = ?, report = ?, "
                                    "summary = NULL WHERE id = ?",
                                    (json.dumps(original["payload"]), json.dumps(original["events"]),
                                     json.dumps(original["issue"]), original["report"], rid))
            current.close()
            reopened = RunStore(2, path)
            self.assertEqual(reopened.get_field(rid, "payload")["id"], rid)
            self.assertEqual(reopened.get_field(rid, "events"), original["events"])
            self.assertEqual(reopened.get_field(rid, "report"), original["report"])
            self.assertEqual(reopened.items()[0]["id"], rid)
            reopened.close()

    def test_execution_capacity_returns_retryable_error(self):
        for _ in range(MAX_ACTIVE_RUNS):
            self.assertTrue(_run_slots.acquire(blocking=False))
        try:
            r = self.c.post("/api/faults/F04/run", json={"seed": 7})
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.headers["Retry-After"], "1")
        finally:
            for _ in range(MAX_ACTIVE_RUNS):
                _run_slots.release()

    def test_sweep_does_not_queue_behind_active_sweep(self):
        self.assertTrue(_sweep_lock.acquire(blocking=False))
        try:
            r = self.c.post("/api/sweep", json={"seed": 9, "only": ["F04"]})
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.headers["Retry-After"], "1")
        finally:
            _sweep_lock.release()

    def test_run_history_cursor_pagination(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = RunStore(10, Path(tmp) / "runs.sqlite3")
            ids = [db.put(run_fault("F04", seed=seed), owner="user-a") for seed in (1, 2, 3)]
            db.put(run_fault("F15", seed=1), owner="user-b")
            first = db.items("user-a", limit=2)
            self.assertEqual([r["id"] for r in first], [ids[2], ids[1]])
            newer = db.put(run_fault("F04", seed=4), owner="user-a")
            second = db.items("user-a", limit=2, before=first[-1]["cursor"])
            self.assertEqual([r["id"] for r in second], [ids[0]])
            self.assertEqual(db.items("user-a", limit=2)[0]["id"], newer)
            for row in first:
                db.delete(row["id"], "user-a")
            self.assertEqual([r["id"] for r in db.items("user-a", limit=2,
                                                        before=first[-1]["cursor"])], [ids[0]])
            db.close()
        for params in ({"limit": 0}, {"limit": 201}, {"before": 0}):
            self.assertEqual(self.c.get("/api/runs", params=params).status_code, 422)
        created = [self.c.post("/api/faults/F04/run", json={"seed": seed}).json()["id"]
                   for seed in (31, 32, 33)]
        page = self.c.get("/api/runs", params={"limit": 2}).json()
        self.assertEqual([r["id"] for r in page], [created[2], created[1]])
        older = self.c.get("/api/runs", params={"limit": 2, "before": page[-1]["cursor"]}).json()
        self.assertEqual(older[0]["id"], created[0])

    def test_user_retention_cannot_evict_other_users_before_global_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = RunStore(5, Path(tmp) / "runs.sqlite3", owner_cap=2)
            other = db.put(run_fault("F04", seed=1), owner="user-b")
            first = db.put(run_fault("F04", seed=2), owner="user-a")
            second = db.put(run_fault("F04", seed=3), owner="user-a")
            third = db.put(run_fault("F04", seed=4), owner="user-a")
            self.assertEqual([item["id"] for item in db.items("user-a")], [third, second])
            self.assertEqual([item["id"] for item in db.items("user-b")], [other])
            with self.assertRaises(Exception) as raised:
                db.get_field(first, "payload", "user-a")
            self.assertEqual(raised.exception.status_code, 404)
            db.close()

    def test_global_retention_only_evicts_requesting_users_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = RunStore(3, Path(tmp) / "runs.sqlite3", owner_cap=2)
            other_ids = [db.put(run_fault("F04", seed=seed), owner="user-b") for seed in (1, 2)]
            older = db.put(run_fault("F04", seed=3), owner="user-a")
            newer = db.put(run_fault("F04", seed=4), owner="user-a")
            self.assertEqual([row["id"] for row in db.items("user-a")], [newer])
            self.assertEqual([row["id"] for row in db.items("user-b")], list(reversed(other_ids)))
            with self.assertRaises(Exception) as raised:
                db.get_field(older, "payload", "user-a")
            self.assertEqual(raised.exception.status_code, 404)

            with patch("backend.app.store", db):
                response = self.c.post("/api/faults/F04/run", json={"seed": 5})
            self.assertEqual(response.status_code, 507)
            self.assertIn("전체 기록 저장 한도", response.json()["detail"])
            self.assertEqual(db.count("local"), 0)
            self.assertEqual(db.count("user-b"), 2)
            self.assertEqual(db.count("user-a"), 1)
            db.close()

    def test_compare_rejects_capacity_without_partial_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = RunStore(1, Path(tmp) / "runs.sqlite3")
            with patch("backend.app.store", db):
                response = self.c.post("/api/faults/F04/compare", json={"seed": 7})
            self.assertEqual(response.status_code, 507)
            self.assertEqual(db.count(), 0)
            db.close()


if __name__ == "__main__":
    unittest.main()
