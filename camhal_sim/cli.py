"""camhal-sim CLI.

  camhal-sim list-platforms | list-workloads | list-faults | list-checkers
  camhal-sim run   --platform QCOM-CamX-Spectra --workload zsl_burst [--defect zsl_leak_on_cancel]
  camhal-sim fault F01 [--fixed] [--platform ...] [--html out.html] [--json out.json] [--issue out.json]
  camhal-sim sweep [--seed 7] [--json sweep.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .checkers import CHECKERS
from .faults import all_faults
from .platforms import all_platforms
from .report import render_html, render_sweep_text, render_text, sweep_to_json, to_jira_issue, to_json
from .runner import RunResult, run_fault, run_scenario, sweep
from .workloads import REGISTRY


def _write(path: str | None, text: str) -> None:
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")
        print(f"-> {path}", file=sys.stderr)


def _outputs(r: RunResult, args) -> None:
    print(render_text(r, log_lines=args.logs))
    _write(args.html, render_html(r))
    _write(args.json, json.dumps(to_json(r, include_events=True), ensure_ascii=False, indent=1))
    _write(args.issue, json.dumps(to_jira_issue(r), ensure_ascii=False, indent=1))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="camhal-sim", description="Camera HAL3 이산 사건 시뮬레이터")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-platforms", help="플랫폼 프로파일")
    sub.add_parser("list-workloads", help="워크로드(재현 절차)")
    lf = sub.add_parser("list-faults", help="결함 카탈로그(참고 시나리오 37종)")
    lf.add_argument("--category")
    sub.add_parser("list-checkers", help="계약 검사기")

    def add_out(p):
        p.add_argument("--seed", type=int, default=7)
        p.add_argument("--logs", type=int, default=20, help="출력할 W/E/F 로그 줄 수")
        p.add_argument("--html", help="HTML 타임라인 리포트 경로")
        p.add_argument("--json", help="사건/로그 포함 JSON 경로")
        p.add_argument("--issue", help="camera-hal-analysis 형식 Jira 이슈 JSON 경로")

    rp = sub.add_parser("run", help="플랫폼×워크로드 실행 (결함 선택 주입)")
    rp.add_argument("--platform", required=True)
    rp.add_argument("--workload", required=True)
    rp.add_argument("--defect", action="append", default=[], help="결함 스위치 또는 결함 ID (반복 가능)")
    add_out(rp)

    fp = sub.add_parser("fault", help="결함 시나리오 재현")
    fp.add_argument("fault_id")
    fp.add_argument("--fixed", action="store_true", help="결함 없이(수정 상태) 같은 절차 실행")
    fp.add_argument("--platform", help="다른 플랫폼에서 같은 결함 시험")
    add_out(fp)

    sp = sub.add_parser("sweep", help="전 결함: 주입 시 검출 + 수정 시 무위반 검증")
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--only", nargs="*")
    sp.add_argument("--json")

    args = ap.parse_args(argv)

    if args.cmd == "list-platforms":
        for p in all_platforms():
            modes = ", ".join(f"{m.width}x{m.height}@{m.max_fps}" for m in p.sensor_modes)
            print(f"{p.code:<18} {p.family:<24} isp={p.isp:<8} ports={p.isp_ports} pool={p.pool_size} "
                  f"partials={p.partial_results} cams={','.join(p.camera_ids)}\n{'':<18} {modes}  — {p.desc_ko}")
    elif args.cmd == "list-workloads":
        for w in REGISTRY.values():
            b = f"  budgets={w.budgets}" if w.budgets else ""
            print(f"{w.name:<26} {w.title}{b}")
    elif args.cmd == "list-faults":
        for f in all_faults():
            if args.category and f.category.lower() != args.category.lower():
                continue
            print(f"{f.id} {f.category:<18} {f.severity:<9} {f.defect:<38} {f.workload:<24} "
                  f"expect={','.join(f.expect)}\n     {f.title()}")
    elif args.cmd == "list-checkers":
        for c in CHECKERS:
            print(f"{c.code:<32} {c.category:<18} {c.severity:<9} {c.title}")
    elif args.cmd == "run":
        faults = {f.id: f.defect for f in all_faults()}
        defects = [faults.get(d.upper(), d) for d in args.defect]
        known = {f.defect for f in all_faults()}
        unknown = [d for d in defects if d not in known]
        if unknown:
            ap.error(f"알 수 없는 결함: {unknown}")
        _outputs(run_scenario(args.platform, args.workload, defects, args.seed), args)
    elif args.cmd == "fault":
        _outputs(run_fault(args.fault_id, platform=args.platform, seed=args.seed, fixed=args.fixed), args)
    elif args.cmd == "sweep":
        rows = sweep(seed=args.seed, fault_ids=args.only)
        print(render_sweep_text(rows))
        _write(args.json, json.dumps(sweep_to_json(rows), ensure_ascii=False, indent=1))
        return 0 if all(r.ok for r in rows) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
