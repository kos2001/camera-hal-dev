"""camhal-sim 백엔드 — 시뮬레이터를 HTTP API 로 노출한다.

    .venv/bin/uvicorn backend.app:app --port 8010 --reload

API (모두 /api 아래):
  GET  /health
  GET  /ready
  GET  /catalog                          플랫폼·워크로드·결함·검사기·분류 한 번에
  POST /run                              {platform, workload, defects[], seed}
  POST /faults/{fid}/run                 {platform?, seed, fixed}
  POST /faults/{fid}/compare             {platform?, seed} → 결함 주입 / 수정 상태 두 실행
  POST /sweep                            {seed, only?} → 검증 매트릭스
  GET  /runs                             최근 실행 목록
  DELETE /runs                            자신의 모든 실행 결과 삭제
  GET  /runs/{rid}                       실행 결과(요약·판정·타임라인·시계열·로그)
  DELETE /runs/{rid}                      자신의 실행 결과 삭제
  GET  /runs/{rid}/events?kind=&offset=&limit=
  GET  /runs/{rid}/issue                 camera-hal-analysis 형식 Jira 이슈 JSON
  GET  /runs/{rid}/report.html           단일 파일 HTML 리포트

web/dist 가 있으면 / 에서 프론트엔드를 함께 서빙한다.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
import zlib
from collections import OrderedDict
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, StringConstraints

from camhal_sim import __version__
from camhal_sim.checkers import CHECKERS
from camhal_sim.faults import all_faults, get_fault
from camhal_sim.platforms import all_platforms
from camhal_sim.report import render_html, sweep_to_json, to_jira_issue
from camhal_sim.runner import RunResult, run_fault, run_scenario, sweep
from camhal_sim.workloads import REGISTRY

from .serialize import fault_dict, platform_dict, run_payload, workload_dict
from .auth import AuthConfig, IdentityMiddleware
from .request_limits import RequestSizeMiddleware

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "web" / "dist"
MAX_RUNS = 1000
MAX_RUNS_PER_OWNER = 200
MIN_FREE_BYTES = 64 * 1024 * 1024
MAX_SEED = 2**31 - 1
DB_PATH = Path(os.environ.get("CAMHAL_DB_PATH", str(ROOT / "data" / "runs.sqlite3")))
APP_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
           "style-src-attr 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
           "font-src 'self'; object-src 'none'; base-uri 'none'; "
           "frame-ancestors 'none'; form-action 'self'")
REPORT_CSP = ("default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; "
              "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")

auth_config = AuthConfig.from_env()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    store.close()


app = FastAPI(title="camhal-sim", version=__version__, lifespan=lifespan)
if auth_config.mode == "local":
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5180", "http://127.0.0.1:5180"],
                       allow_methods=["*"], allow_headers=["*"])
app.add_middleware(RequestSizeMiddleware)
app.add_middleware(IdentityMiddleware, config=auth_config)


@app.exception_handler(sqlite3.Error)
async def storage_error(request: Request, exc: sqlite3.Error) -> JSONResponse:
    code = getattr(exc, "sqlite_errorcode", None)
    logging.getLogger("uvicorn.error").error(json.dumps({
        "request_id": getattr(request.state, "request_id", ""),
        "event": "storage_error", "sqlite_code": code,
    }), exc_info=(type(exc), exc, exc.__traceback__))
    if code == sqlite3.SQLITE_FULL:
        return JSONResponse({"detail": "저장 공간이 부족하여 실행 결과를 저장할 수 없습니다."},
                            status_code=507)
    return JSONResponse({"detail": "저장소를 사용할 수 없습니다. 잠시 후 다시 시도하세요."},
                        status_code=503, headers={"Retry-After": "5"})


@app.middleware("http")
async def response_headers(request: Request, call_next):
    incoming_id = request.headers.get("X-Request-ID", "")
    try:
        request_id = uuid.UUID(incoming_id).hex
    except ValueError:
        request_id = uuid.uuid4().hex
    request.state.request_id = request_id
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if response.headers.get("Content-Type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
            if request.url.path.startswith("/api/runs/") and request.url.path.endswith("/report.html"):
                response.headers["Content-Security-Policy"] = REPORT_CSP
            elif not request.url.path.startswith(("/docs", "/redoc")):
                response.headers["Content-Security-Policy"] = APP_CSP
        return response
    finally:
        route = request.scope.get("route")
        logging.getLogger("uvicorn.error").info(json.dumps({
            "request_id": request_id, "method": request.method,
            "route": route.path if route else "<unmatched>", "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }))


# --------------------------------------------------------------------------- #
# 실행 결과 저장소 (SQLite, 재시작 후에도 조회 가능)
# --------------------------------------------------------------------------- #
class RunStore:
    def __init__(self, cap: int, path: Path, owner_cap: int | None = None):
        if cap < 1 or (owner_cap is not None and not 1 <= owner_cap <= cap):
            raise ValueError("Run retention caps must be positive and owner_cap must not exceed cap")
        self.cap = cap
        self.owner_cap = cap if owner_cap is None else owner_cap
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() and not path.is_file():
            raise ValueError("Run database path must be a file")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        path.chmod(0o600)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA secure_delete=ON")
        self._db.execute("CREATE TABLE IF NOT EXISTS runs (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, events TEXT NOT NULL, "
                         "issue TEXT NOT NULL, report TEXT NOT NULL, summary TEXT, "
                         "owner TEXT NOT NULL DEFAULT 'local')")
        if "summary" not in {col[1] for col in self._db.execute("PRAGMA table_info(runs)")}:
            self._db.execute("ALTER TABLE runs ADD COLUMN summary TEXT")
        if "owner" not in {col[1] for col in self._db.execute("PRAGMA table_info(runs)")}:
            self._db.execute("ALTER TABLE runs ADD COLUMN owner TEXT NOT NULL DEFAULT 'local'")
        self._db.execute("CREATE INDEX IF NOT EXISTS runs_owner_seq ON runs (owner, seq DESC)")
        for rid, raw in self._db.execute("SELECT id, payload FROM runs WHERE summary IS NULL"):
            self._db.execute("UPDATE runs SET summary = ? WHERE id = ?",
                             (json.dumps(self._summary(json.loads(self._unpack(raw)))), rid))
        self._db.commit()
        self._lock = threading.Lock()
        self._closed = False

    @staticmethod
    def _summary(r: dict) -> dict:
        return {"id": r["id"], "platform": r["platform"]["code"],
                "workload": r["workload"]["name"],
                "fault": r["fault"]["id"] if r["fault"] else None,
                "defects": r["defects"], "seed": r["seed"],
                "violations": len(r["violations"])}

    @staticmethod
    def _pack(value: str) -> bytes:
        return b"Z1" + zlib.compress(value.encode("utf-8"))

    @staticmethod
    def _unpack(value: str | bytes) -> str:
        if isinstance(value, str):
            return value
        if value.startswith(b"Z1"):
            return zlib.decompress(value[2:]).decode("utf-8")
        return value.decode("utf-8")

    def put(self, r: RunResult, owner: str = "local") -> str:
        return self.put_many([r], owner)[0]

    def put_many(self, runs: list[RunResult], owner: str = "local") -> list[str]:
        if not runs:
            return []
        if len(runs) > self.owner_cap:
            raise HTTPException(507, "비교 결과를 저장할 기록 공간이 부족합니다.")
        prepared = []
        for r in runs:
            rid = uuid.uuid4().hex
            payload = run_payload(rid, r)
            summary = self._summary(payload)
            summary["created_at"] = datetime.now(timezone.utc).isoformat()
            prepared.append((rid, self._pack(json.dumps(payload)),
                             self._pack(json.dumps([e.to_dict() for e in r.events])),
                             self._pack(json.dumps(to_jira_issue(r))),
                             self._pack("<!doctype html><meta charset=utf-8>" + render_html(r)),
                             json.dumps(summary), owner))
        with self._lock:
            with self._db:
                self._db.executemany("INSERT INTO runs (id, payload, events, issue, report, summary, owner) "
                                     "VALUES (?, ?, ?, ?, ?, ?, ?)", prepared)
                self._db.execute("DELETE FROM runs WHERE owner = ? AND seq NOT IN "
                                 "(SELECT seq FROM runs WHERE owner = ? ORDER BY seq DESC LIMIT ?)",
                                 (owner, owner, self.owner_cap))
                total = self._db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                excess = total - self.cap
                if excess > 0:
                    own_count = self._db.execute("SELECT COUNT(*) FROM runs WHERE owner = ?",
                                                 (owner,)).fetchone()[0]
                    if own_count - len(prepared) < excess:
                        raise HTTPException(507, "전체 기록 저장 한도에 도달했습니다. 기록을 삭제하거나 운영자에게 문의하세요.")
                    self._db.execute("DELETE FROM runs WHERE seq IN "
                                     "(SELECT seq FROM runs WHERE owner = ? ORDER BY seq LIMIT ?)",
                                     (owner, excess))
        return [row[0] for row in prepared]

    def get(self, rid: str, owner: str = "local") -> dict:
        with self._lock:
            row = self._db.execute("SELECT payload, events, issue, report FROM runs "
                                   "WHERE id = ? AND owner = ?", (rid, owner)).fetchone()
        if row is None:
            raise HTTPException(404, f"실행 결과 없음(만료되었을 수 있음): {rid}")
        return {"payload": json.loads(self._unpack(row[0])), "events": json.loads(self._unpack(row[1])),
                "issue": json.loads(self._unpack(row[2])), "report": self._unpack(row[3])}

    def get_field(self, rid: str, field: str, owner: str = "local"):
        if field not in {"payload", "events", "issue", "report"}:
            raise ValueError("Invalid run field")
        with self._lock:
            row = self._db.execute(f"SELECT {field} FROM runs WHERE id = ? AND owner = ?",
                                   (rid, owner)).fetchone()
        if row is None:
            raise HTTPException(404, f"실행 결과 없음(만료되었을 수 있음): {rid}")
        unpacked = self._unpack(row[0])
        return unpacked if field == "report" else json.loads(unpacked)

    def items(self, owner: str = "local", limit: int | None = None, before: int | None = None) -> list[dict]:
        size = self.cap if limit is None else min(limit, self.cap)
        with self._lock:
            if before is None:
                rows = self._db.execute("SELECT seq, summary FROM runs WHERE owner = ? "
                                        "ORDER BY seq DESC LIMIT ?", (owner, size)).fetchall()
            else:
                rows = self._db.execute("SELECT seq, summary FROM runs WHERE owner = ? AND seq < ? "
                                        "ORDER BY seq DESC LIMIT ?", (owner, before, size)).fetchall()
        return [{**json.loads(summary), "cursor": seq} for seq, summary in rows]

    def count(self, owner: str = "local") -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM runs WHERE owner = ?", (owner,)).fetchone()[0]

    def delete(self, rid: str, owner: str = "local") -> None:
        with self._lock:
            with self._db:
                result = self._db.execute("DELETE FROM runs WHERE id = ? AND owner = ?", (rid, owner))
        if result.rowcount == 0:
            raise HTTPException(404, f"실행 결과 없음(만료되었을 수 있음): {rid}")

    def delete_all(self, owner: str = "local") -> int:
        with self._lock:
            with self._db:
                result = self._db.execute("DELETE FROM runs WHERE owner = ?", (owner,))
        return result.rowcount

    def is_ready(self) -> bool:
        try:
            with self._lock:
                self._db.execute("SELECT 1 FROM runs LIMIT 1").fetchone()
                pages = self._db.execute("PRAGMA page_count").fetchone()[0]
                limit = self._db.execute("PRAGMA max_page_count").fetchone()[0]
                reusable = self._db.execute("PRAGMA freelist_count").fetchone()[0]
                if pages >= limit and reusable == 0:
                    return False
                if shutil.disk_usage(self.path.parent).free < MIN_FREE_BYTES:
                    return False
            return True
        except (OSError, sqlite3.Error):
            return False

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True


store = RunStore(MAX_RUNS, DB_PATH, MAX_RUNS_PER_OWNER)
atexit.register(store.close)
_sweep_cache: OrderedDict[tuple, dict] = OrderedDict()
_sweep_lock = threading.Lock()
MAX_SWEEP_CACHE = 16
MAX_ACTIVE_RUNS = 4
MAX_ACTIVE_RUNS_PER_OWNER = 2
_run_slots = threading.BoundedSemaphore(MAX_ACTIVE_RUNS)
_owner_slot_lock = threading.Lock()
_owner_slots: dict[str, int] = {}


@contextmanager
def _execution_slot(owner: str):
    with _owner_slot_lock:
        if _owner_slots.get(owner, 0) >= MAX_ACTIVE_RUNS_PER_OWNER or not _run_slots.acquire(blocking=False):
            raise HTTPException(503, "실행 요청이 많습니다. 잠시 후 다시 시도하세요.",
                                headers={"Retry-After": "1"})
        _owner_slots[owner] = _owner_slots.get(owner, 0) + 1
    try:
        yield
    finally:
        with _owner_slot_lock:
            remaining = _owner_slots[owner] - 1
            if remaining:
                _owner_slots[owner] = remaining
            else:
                del _owner_slots[owner]
            _run_slots.release()


def _known_defects() -> dict[str, str]:
    return {f.id: f.defect for f in all_faults()}


def _save(r: RunResult, owner: str) -> dict:
    return store.get_field(store.put(r, owner), "payload", owner)


def _save_many(runs: list[RunResult], owner: str) -> list[dict]:
    return [store.get_field(rid, "payload", owner) for rid in store.put_many(runs, owner)]


def _guard(fn, *args, **kwargs) -> RunResult:
    try:
        return fn(*args, **kwargs)
    except KeyError as e:
        raise HTTPException(404, str(e.args[0] if e.args else e)) from e


# --------------------------------------------------------------------------- #
# 요청 스키마
# --------------------------------------------------------------------------- #
Name = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class RunRequest(BaseModel):
    platform: Name
    workload: Name
    defects: list[Name] = Field(default_factory=list, max_length=37)
    seed: int = Field(7, ge=0, le=MAX_SEED)


class FaultRunRequest(BaseModel):
    platform: Name | None = None
    seed: int = Field(7, ge=0, le=MAX_SEED)
    fixed: bool = False


class SweepRequest(BaseModel):
    seed: int = Field(7, ge=0, le=MAX_SEED)
    only: list[Name] | None = Field(None, max_length=37)


# --------------------------------------------------------------------------- #
# 엔드포인트
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@app.get("/api/ready")
def ready() -> dict:
    if not store.is_ready():
        raise HTTPException(503, "저장소를 사용할 수 없습니다.")
    return {"ok": True}


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
def run(req: RunRequest, request: Request) -> dict:
    ids = _known_defects()
    defects = [ids.get(d.upper(), d) for d in req.defects]
    unknown = sorted(set(defects) - set(ids.values()))
    if unknown:
        raise HTTPException(422, f"알 수 없는 결함: {unknown}")
    with _execution_slot(request.state.owner):
        return _save(_guard(run_scenario, req.platform, req.workload, defects, req.seed), request.state.owner)


@app.post("/api/faults/{fid}/run")
def run_one_fault(fid: str, req: FaultRunRequest, request: Request) -> dict:
    with _execution_slot(request.state.owner):
        return _save(_guard(run_fault, fid, platform=req.platform, seed=req.seed, fixed=req.fixed), request.state.owner)


@app.post("/api/faults/{fid}/compare")
def compare_fault(fid: str, req: FaultRunRequest, request: Request) -> dict:
    with _execution_slot(request.state.owner):
        faulty = _guard(run_fault, fid, platform=req.platform, seed=req.seed)
        fixed = _guard(run_fault, fid, platform=req.platform, seed=req.seed, fixed=True)
        saved_faulty, saved_fixed = _save_many([faulty, fixed], request.state.owner)
        return {"faulty": saved_faulty, "fixed": saved_fixed}


@app.post("/api/sweep")
def run_sweep(req: SweepRequest, request: Request) -> dict:
    ids = req.only or [f.id for f in all_faults()]
    unknown = sorted(set(ids) - {f.id for f in all_faults()})
    if unknown:
        raise HTTPException(422, f"알 수 없는 결함: {unknown}")
    key = (req.seed, tuple(ids))
    if not _sweep_lock.acquire(blocking=False):
        raise HTTPException(503, "검증 매트릭스를 계산 중입니다. 잠시 후 다시 시도하세요.",
                            headers={"Retry-After": "1"})
    try:
        cached = _sweep_cache.get(key)
        if cached is not None:
            _sweep_cache.move_to_end(key)
            return cached
        with _execution_slot(request.state.owner):
            rows = sweep(seed=req.seed, fault_ids=ids)
        data = sweep_to_json(rows)
        for row, raw in zip(data["rows"], rows):
            row["title"] = raw.fault.title()
            row["severity"] = raw.fault.severity
            row["sim_ms"] = round(raw.faulty.sim_ms, 1)
        data["seed"] = req.seed
        _sweep_cache[key] = data
        while len(_sweep_cache) > MAX_SWEEP_CACHE:
            _sweep_cache.popitem(last=False)
        return data
    finally:
        _sweep_lock.release()


@app.get("/api/runs")
def list_runs(request: Request, limit: int = Query(50, ge=1, le=200),
              before: int | None = Query(None, ge=1)) -> list[dict]:
    return store.items(request.state.owner, limit, before)


@app.delete("/api/runs")
def delete_all_runs(request: Request) -> dict:
    return {"deleted": store.delete_all(request.state.owner)}


@app.get("/api/runs/{rid}")
def get_run(rid: str, request: Request) -> dict:
    return store.get_field(rid, "payload", request.state.owner)


@app.delete("/api/runs/{rid}", status_code=204)
def delete_run(rid: str, request: Request) -> Response:
    store.delete(rid, request.state.owner)
    return Response(status_code=204)


@app.get("/api/runs/{rid}/events")
def get_events(rid: str, request: Request, kind: str | None = Query(None, max_length=64), offset: int = Query(0, ge=0),
               limit: int = Query(200, ge=1, le=2000)) -> dict:
    evs = [e for e in store.get_field(rid, "events", request.state.owner) if kind is None or e["kind"] == kind]
    return {"total": len(evs), "offset": offset, "items": evs[offset:offset + limit]}


@app.get("/api/runs/{rid}/issue")
def get_issue(rid: str, request: Request) -> dict:
    return store.get_field(rid, "issue", request.state.owner)


@app.get("/api/runs/{rid}/report.html", response_class=HTMLResponse)
def get_report(rid: str, request: Request) -> str:
    return store.get_field(rid, "report", request.state.owner)


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
