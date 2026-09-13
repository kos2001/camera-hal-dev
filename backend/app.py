"""camhal-sim 백엔드 — 시뮬레이터를 HTTP API 로 노출한다.

    .venv/bin/uvicorn backend.app:app --port 8010 --reload

API (모두 /api 아래):
  GET  /health
  GET  /catalog                          플랫폼·워크로드·결함·검사기·분류 한 번에
  POST /run                              {platform, workload, defects[], seed}
  POST /faults/{fid}/run                 {platform?, seed, fixed}
  POST /faults/{fid}/compare             {platform?, seed} → 결함 주입 / 수정 상태 두 실행
  POST /sweep                            {seed, only?} → 검증 매트릭스
  GET  /runs                             최근 실행 목록
  GET  /runs/{rid}                       실행 결과(요약·판정·타임라인·시계열·로그)
  GET  /runs/{rid}/events?kind=&offset=&limit=
  GET  /runs/{rid}/issue                 camera-hal-analysis 형식 Jira 이슈 JSON
  GET  /runs/{rid}/report.html           단일 파일 HTML 리포트

web/dist 가 있으면 / 에서 프론트엔드를 함께 서빙한다.
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from camhal_sim import __version__
from camhal_sim.checkers import CHECKERS
from camhal_sim.faults import all_faults, get_fault
from camhal_sim.platforms import all_platforms
from camhal_sim.report import render_html, sweep_to_json, to_jira_issue
from camhal_sim.runner import RunResult, run_fault, run_scenario, sweep
from camhal_sim.workloads import REGISTRY

from .serialize import fault_dict, platform_dict, run_payload, workload_dict

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "web" / "dist"
MAX_RUNS = 40
MAX_SEED = 2**31 - 1

app = FastAPI(title="camhal-sim", version=__version__)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5180", "http://127.0.0.1:5180"],
                   allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------------- #
# 실행 결과 저장소 (프로세스 메모리, LRU)
# --------------------------------------------------------------------------- #
class RunStore:
    def __init__(self, cap: int):
        self.cap = cap
        self._runs: OrderedDict[str, RunResult] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, r: RunResult) -> str:
        rid = uuid.uuid4().hex[:12]
        with self._lock:
            self._runs[rid] = r
            while len(self._runs) > self.cap:
                self._runs.popitem(last=False)
        return rid

    def get(self, rid: str) -> RunResult:
        with self._lock:
            r = self._runs.get(rid)
        if r is None:
            raise HTTPException(404, f"실행 결과 없음(만료되었을 수 있음): {rid}")
        return r

    def items(self) -> list[tuple[str, RunResult]]:
        with self._lock:
            return list(reversed(self._runs.items()))


store = RunStore(MAX_RUNS)
_sweep_cache: dict[tuple, dict] = {}


def _known_defects() -> dict[str, str]:
    return {f.id: f.defect for f in all_faults()}


def _save(r: RunResult) -> dict:
    return run_payload(store.put(r), r)


def _guard(fn, *args, **kwargs) -> RunResult:
    try:
        return fn(*args, **kwargs)
    except KeyError as e:
        raise HTTPException(404, str(e.args[0] if e.args else e)) from e


# --------------------------------------------------------------------------- #
# 요청 스키마
# --------------------------------------------------------------------------- #
class RunRequest(BaseModel):
    platform: str
    workload: str
    defects: list[str] = Field(default_factory=list, max_length=37)
    seed: int = Field(7, ge=0, le=MAX_SEED)


class FaultRunRequest(BaseModel):
    platform: str | None = None
    seed: int = Field(7, ge=0, le=MAX_SEED)
    fixed: bool = False


class SweepRequest(BaseModel):
    seed: int = Field(7, ge=0, le=MAX_SEED)
    only: list[str] | None = None


# --------------------------------------------------------------------------- #
# 엔드포인트
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__, "faults": len(all_faults()), "runs_cached": len(store.items())}


@app.get("/api/catalog")
def catalog() -> dict:
    faults = all_faults()
    return {
        "version": __version__,
        "categories": sorted({f.category for f in faults}, key=lambda c: [f.category for f in faults].index(c)),
        "platforms": [platform_dict(p) for p in all_platforms()],
        "workloads": [workload_dict(w) for w in REGISTRY.values()],
        "faults": [fault_dict(f) for f in faults],
        "checkers": [{"code": c.code, "category": c.category, "severity": c.severity, "title": c.title}
                     for c in CHECKERS],
    }


@app.post("/api/run")
def run(req: RunRequest) -> dict:
    ids = _known_defects()
    defects = [ids.get(d.upper(), d) for d in req.defects]
    unknown = sorted(set(defects) - set(ids.values()))
    if unknown:
        raise HTTPException(422, f"알 수 없는 결함: {unknown}")
    return _save(_guard(run_scenario, req.platform, req.workload, defects, req.seed))


@app.post("/api/faults/{fid}/run")
def run_one_fault(fid: str, req: FaultRunRequest) -> dict:
    return _save(_guard(run_fault, fid, platform=req.platform, seed=req.seed, fixed=req.fixed))


@app.post("/api/faults/{fid}/compare")
def compare_fault(fid: str, req: FaultRunRequest) -> dict:
    faulty = _guard(run_fault, fid, platform=req.platform, seed=req.seed)
    fixed = _guard(run_fault, fid, platform=req.platform, seed=req.seed, fixed=True)
    return {"faulty": _save(faulty), "fixed": _save(fixed)}


@app.post("/api/sweep")
def run_sweep(req: SweepRequest) -> dict:
    key = (req.seed, tuple(req.only or ()))
    if key not in _sweep_cache:
        try:
            rows = sweep(seed=req.seed, fault_ids=req.only)
        except KeyError as e:
            raise HTTPException(404, str(e.args[0])) from e
        data = sweep_to_json(rows)
        for row, raw in zip(data["rows"], rows):
            row["title"] = raw.fault.title()
            row["severity"] = raw.fault.severity
            row["sim_ms"] = round(raw.faulty.sim_ms, 1)
        data["seed"] = req.seed
        _sweep_cache[key] = data
    return _sweep_cache[key]


@app.get("/api/runs")
def list_runs() -> list[dict]:
    return [{"id": rid, "platform": r.platform.code, "workload": r.workload.name,
             "fault": r.fault.id if r.fault else None, "defects": list(r.defects), "seed": r.seed,
             "violations": len(r.violations)} for rid, r in store.items()]


@app.get("/api/runs/{rid}")
def get_run(rid: str) -> dict:
    return run_payload(rid, store.get(rid))


@app.get("/api/runs/{rid}/events")
def get_events(rid: str, kind: str | None = None, offset: int = Query(0, ge=0),
               limit: int = Query(200, ge=1, le=2000)) -> dict:
    r = store.get(rid)
    evs = [e for e in r.events if kind is None or e.kind == kind]
    return {"total": len(evs), "offset": offset, "items": [e.to_dict() for e in evs[offset:offset + limit]]}


@app.get("/api/runs/{rid}/issue")
def get_issue(rid: str) -> dict:
    return to_jira_issue(store.get(rid))


@app.get("/api/runs/{rid}/report.html", response_class=HTMLResponse)
def get_report(rid: str) -> str:
    return "<!doctype html><meta charset=utf-8>" + render_html(store.get(rid))


@app.get("/api/faults/{fid}")
def get_fault_detail(fid: str) -> dict:
    try:
        return fault_dict(get_fault(fid))
    except KeyError as e:
        raise HTTPException(404, str(e.args[0])) from e


# --------------------------------------------------------------------------- #
# 프론트엔드 정적 파일 (빌드된 경우)
# --------------------------------------------------------------------------- #
@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "not found")
    if not DIST.exists():
        return HTMLResponse("<p>프론트엔드가 빌드되지 않았습니다. <code>cd web && npm run build</code> "
                            "또는 개발 서버(:5180)를 사용하세요. API 문서: <a href='/docs'>/docs</a></p>")
    target = (DIST / path).resolve()
    if path and target.is_file() and DIST.resolve() in target.parents:
        return FileResponse(target)
    return FileResponse(DIST / "index.html")
