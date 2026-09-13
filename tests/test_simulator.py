"""camhal_sim 회귀 테스트 — `python3 -m unittest discover -s tests` (표준 라이브러리만)."""
from __future__ import annotations

import json
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from camhal_sim.checkers import CHECKERS  # noqa: E402
from camhal_sim.cli import main as cli_main  # noqa: E402
from camhal_sim.engine import Simulator  # noqa: E402
from camhal_sim.faults import all_faults, get_fault  # noqa: E402
from camhal_sim.hal.components import (AeController, BufferPool, LockOp, detect_deadlock,  # noqa: E402
                                       scene_luma)
from camhal_sim.model import MS  # noqa: E402
from camhal_sim.platforms import all_platforms  # noqa: E402
from camhal_sim.report import render_html, to_jira_issue  # noqa: E402
from camhal_sim.runner import run_fault, run_scenario, sweep  # noqa: E402
from camhal_sim.workloads import REGISTRY  # noqa: E402


class CatalogTest(unittest.TestCase):
    def test_reference_scenarios_imported(self):
        faults = all_faults()
        self.assertEqual(len(faults), 37)
        self.assertTrue(all(f.root_cause and f.resolution and f.category for f in faults))

    def test_mapping_is_consistent(self):
        codes = {c.code for c in CHECKERS}
        platforms = {p.code for p in all_platforms()}
        defects = [f.defect for f in all_faults()]
        self.assertEqual(len(defects), len(set(defects)), "결함 스위치 이름 중복")
        hal_src = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "camhal_sim" / "hal").glob("*.py"))
        for f in all_faults():
            self.assertIn(f.workload, REGISTRY, f.id)
            self.assertIn(f.platform, platforms, f.id)
            self.assertTrue(set(f.expect) <= codes, f.id)
            self.assertIn(f'"{f.defect}"', hal_src, f"{f.id}: 결함 스위치가 HAL 코드에 없음")

    def test_fault_family_matches_platform(self):
        fam = {p.code: p.family for p in all_platforms()}
        for f in all_faults():
            self.assertEqual(fam[f.platform], f.family, f.id)


class EngineTest(unittest.TestCase):
    def test_order_and_cancel(self):
        sim, seen = Simulator(), []
        sim.at(20, seen.append, "b")
        sim.at(10, seen.append, "a")
        sim.at(20, seen.append, "c")
        sim.at(15, seen.append, "x").cancel()
        sim.run_until(100)
        self.assertEqual(seen, ["a", "b", "c"])
        self.assertEqual(sim.now, 100)


class ComponentTest(unittest.TestCase):
    def _ae_series(self, defects):
        sim = Simulator(defects=defects)
        ae, pipeline, applied, lumas = AeController(sim), [], (10 * MS, 1.0), []
        for _ in range(120):
            luma = scene_luma(8, *applied)
            lumas.append(luma)
            pipeline.append(ae.update(luma, applied, 33 * MS))
            if len(pipeline) > 2:
                applied = pipeline.pop(0)
        return lumas[-40:]

    def test_ae_converges_with_latency_compensation(self):
        tail = self._ae_series(frozenset())
        self.assertTrue(all(abs(v / AeController.TARGET - 1) < 0.08 for v in tail), tail[:5])

    def test_ae_oscillates_without_compensation(self):
        tail = self._ae_series({"ae_no_latency_compensation"})
        self.assertGreater(max(tail) / max(min(tail), 1e-4), 3)

    def test_lock_order_deadlock(self):
        emit = LockOp("t7", "emitResult", ("A", "B"), 0, 4 * MS)
        self.assertTrue(detect_deadlock(LockOp("t12", "closeStream", ("B", "A"), 2 * MS, 3 * MS), emit))
        self.assertFalse(detect_deadlock(LockOp("t12", "closeStream", ("A", "B"), 2 * MS, 3 * MS), emit))
        self.assertFalse(detect_deadlock(LockOp("t12", "closeStream", ("B", "A"), 9 * MS, 3 * MS), emit))

    def test_buffer_pool(self):
        pool = BufferPool(Simulator(), 0, 2, 0x100, "t")
        a, b = pool.acquire("f0"), pool.acquire("f1")
        self.assertIsNone(pool.acquire("f2"))
        pool.release(a)
        self.assertEqual(pool.outstanding, 1)
        self.assertIsNotNone(pool.acquire("f3"))
        self.assertIsNotNone(b)


class FaultInjectionTest(unittest.TestCase):
    """핵심 검증: 결함을 넣으면 기대 위반이 검출되고, 빼면 같은 절차에서 위반 0건."""

    def _check_sweep(self, seed):
        for row in sweep(seed=seed):
            with self.subTest(fault=row.fault.id, seed=seed):
                self.assertTrue(row.detected, f"미검출 {set(row.fault.expect) - row.faulty.codes}")
                self.assertTrue(row.clean, f"수정 상태 위반 {sorted(row.fixed.codes)}")

    def test_sweep_default_seed(self):
        self._check_sweep(7)

    def test_sweep_other_seed(self):
        self._check_sweep(3)

    def test_no_false_positives_across_platforms(self):
        for p in all_platforms():
            for name in REGISTRY:
                with self.subTest(platform=p.code, workload=name):
                    self.assertEqual(run_scenario(p, name, seed=11).violations, [])

    def test_deterministic(self):
        a, b = run_fault("F21", seed=5), run_fault("F21", seed=5)
        self.assertEqual([e.to_dict() for e in a.events], [e.to_dict() for e in b.events])

    def test_fault_on_foreign_platform(self):
        # 같은 결함이 다른 벤더 스택에서도 같은 증상으로 드러나는지(모델 일반성)
        r = run_fault("F04", platform="MTK-Camera3HAL")
        self.assertIn("AE_OSCILLATION", r.codes)
        r = run_fault("F15", platform="EMU-CameraHAL")
        self.assertIn("RESULT_AFTER_FLUSH", r.codes)


class ReportTest(unittest.TestCase):
    def test_jira_issue_markers(self):
        issue = to_jira_issue(run_fault("F03"))
        for marker in ("*대상 플랫폼*: EXYNOS-CamHAL3", "*HAL 버전*: EXHA.", "*고장 분류*: Sync Timing",
                       "*심각도*: Critical", "h2. 증상 (Symptom)", "{code}"):
            self.assertIn(marker, issue["description"])
        self.assertEqual(issue["status"], "Open")
        self.assertTrue(re.match(r"\[EXYNOS-CamHAL3\] ", issue["summary"]))

    def test_html_report(self):
        page = render_html(run_fault("F24"))
        self.assertIn("<title>", page)
        self.assertIn("MULTICHANNEL_SKEW", page)
        self.assertIn("<svg", page)

    def test_cli(self):
        buf = StringIO()
        with redirect_stdout(buf), redirect_stderr(StringIO()):
            self.assertEqual(cli_main(["sweep", "--only", "F00", "F25"]), 0)
            self.assertEqual(cli_main(["run", "--platform", "EMUC", "--workload", "partial_results_order",
                                       "--defect", "F21", "--logs", "0"]), 0)
        self.assertIn("2/2", buf.getvalue())
        self.assertIn("PARTIAL_OUT_OF_ORDER", buf.getvalue())

    def test_json_serializable(self):
        from camhal_sim.report import to_json
        json.dumps(to_json(run_fault("F13")), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
