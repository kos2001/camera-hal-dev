"""백엔드 API 테스트 — `.venv/bin/python -m unittest discover -s tests` (fastapi 없으면 건너뜀)."""
from __future__ import annotations

import sys
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    from backend.app import app
except ImportError:  # 서버 의존성 미설치 환경
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi 미설치 (pip install -r requirements-server.txt)")
class BackendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)

    def test_catalog(self):
        cat = self.c.get("/api/catalog").json()
        self.assertEqual(len(cat["faults"]), 37)
        self.assertEqual(len(cat["platforms"]), 10)
        self.assertEqual(len(cat["categories"]), 10)
        self.assertTrue(cat["checkers"])

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


if __name__ == "__main__":
    unittest.main()
