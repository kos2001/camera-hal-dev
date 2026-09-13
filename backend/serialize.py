"""시뮬레이터 객체 → 프론트엔드용 JSON. 큰 사건 목록은 레인/시계열로 요약해 보낸다."""
from __future__ import annotations

from collections import defaultdict

from camhal_sim.faults import FaultSpec
from camhal_sim.model import MS, Key
from camhal_sim.platforms import PlatformProfile
from camhal_sim.runner import RunResult
from camhal_sim.workloads import Workload

MAX_LANE_POINTS = 800
MAX_SERIES_POINTS = 1200
MAX_LOGS = 3000
BAD_KINDS = {"notify_error", "frame_drop", "request_timeout", "stream_stall", "provider_died"}
LANE_ORDER = ("trigger", "open_result", "configure", "sensor_streaming", "camera_in_use", "shutter",
              "partial_result", "result", "notify_error", "frame_drop", "flush_returned", "close_returned",
              "session_closed", "request_timeout", "stream_stall", "device_status", "hw_usb", "torch_status",
              "ipc_call", "provider_memory", "provider_died", "diag_access")


def platform_dict(p: PlatformProfile) -> dict:
    return {
        "code": p.code, "family": p.family, "fw_prefix": p.fw_prefix, "isp": p.isp, "desc": p.desc_ko,
        "sensor_modes": [f"{m.width}x{m.height}@{m.max_fps}" for m in p.sensor_modes],
        "max_blob": f"{p.max_blob[0]}x{p.max_blob[1]}", "isp_ports": p.isp_ports, "pool_size": p.pool_size,
        "partial_results": p.partial_results, "camera_ids": list(p.camera_ids),
        "host_platforms": list(p.host_platforms), "logical_multicam": p.logical_multicam,
        "external_camera": p.external_camera, "channels": p.channels,
    }


def workload_dict(w: Workload) -> dict:
    return {"name": w.name, "title": w.title, "steps": list(w.steps), "budgets": w.budgets}


def fault_dict(f: FaultSpec) -> dict:
    return {
        "id": f.id, "defect": f.defect, "workload": f.workload, "platform": f.platform,
        "expect": list(f.expect), "category": f.category, "family": f.family, "severity": f.severity,
        "title": f.title(), "summary": f.summary, "symptom": f.symptom, "root_cause": f.root_cause,
        "resolution": f.resolution, "workaround": f.workaround, "repro": list(f.repro),
        "log_excerpt": f.log_excerpt,
    }


def _thin(items: list, cap: int) -> list:
    if len(items) <= cap:
        return items
    step = len(items) / cap
    return [items[int(i * step)] for i in range(cap)]


def _timeline(r: RunResult) -> dict:
    by_kind: dict[str, list[float]] = defaultdict(list)
    for e in r.events:
        by_kind[e.kind].append(round(e.t_ns / MS, 2))
    kinds = [k for k in LANE_ORDER if k in by_kind] + sorted(k for k in by_kind if k not in LANE_ORDER)
    return {
        "t_end_ms": round(r.sim_ms, 1),
        "lanes": [{"kind": k, "count": len(by_kind[k]), "bad": k in BAD_KINDS,
                   "points": _thin(by_kind[k], MAX_LANE_POINTS)} for k in kinds],
    }


def _series(r: RunResult) -> dict:
    frames: dict[str, list[dict]] = defaultdict(list)
    last_t: dict[str, float] = {}
    last_ts: dict[str, int] = {}
    for e in r.events:
        if e.kind != "result":
            continue
        d = e.data
        key = f"cam{d['camera']}" + (f"/{d['client']}" if d.get("client") not in (None, "app") else "") \
            + (f"/ch{d['channel']}" if d.get("channel") else "")
        meta, stats = d["meta"], d["stats"]
        t = e.t_ns / MS
        ts = meta[Key.SENSOR_TIMESTAMP]
        frames[key].append({
            "t_ms": round(t, 2),
            "frame": d["frame"],
            "luma": stats["luma"],
            "exposure_ms": round(meta[Key.SENSOR_EXPOSURE_TIME] / MS, 2),
            "frame_duration_ms": round(meta[Key.SENSOR_FRAME_DURATION] / MS, 2),
            "gain": round(meta[Key.SENSOR_SENSITIVITY] / 100, 2),
            "interval_ms": round(t - last_t[key], 2) if key in last_t else None,
            "ts_delta_ms": round((ts - last_ts[key]) / MS, 2) if key in last_ts else None,
            "wb_error_k": stats["wb_error_k"],
        })
        last_t[key], last_ts[key] = t, ts
    memory = [{"t_ms": round(e.t_ns / MS, 1), "rss_mb": e.data["rss_mb"], "pipes": e.data["pipes_alive"]}
              for e in r.events if e.kind == "provider_memory"]
    return {"frames": [{"key": k, "count": len(v), "points": _thin(v, MAX_SERIES_POINTS)} for k, v in frames.items()],
            "memory": _thin(memory, MAX_SERIES_POINTS)}


def run_payload(rid: str, r: RunResult) -> dict:
    verdict = None
    if r.fault:
        missing = sorted(set(r.fault.expect) - r.codes)
        verdict = {"expected": list(r.fault.expect), "injected": bool(r.defects),
                   "detected": not missing, "missing": missing,
                   "clean": not r.violations}
    return {
        "id": rid,
        "platform": platform_dict(r.platform),
        "workload": workload_dict(r.workload),
        "fault": fault_dict(r.fault) if r.fault else None,
        "defects": list(r.defects),
        "seed": r.seed,
        "sim_ms": round(r.sim_ms, 1),
        "wall_ms": round(r.wall_ms, 1),
        "counters": r.counters,
        "verdict": verdict,
        "violations": [v.to_dict() for v in r.violations],
        "timeline": _timeline(r),
        "series": _series(r),
        "logs": [{"t_ms": round(l.t_ns / MS, 1), "level": l.level, "tag": l.tag, "msg": l.msg}
                 for l in r.logs[:MAX_LOGS]],
        "logs_total": len(r.logs),
    }
