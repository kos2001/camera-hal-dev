"""시나리오 실행기 — (플랫폼, 워크로드, 결함 집합, 시드) → 관찰 사건 + 판정."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .checkers import Violation, run_checkers
from .engine import Simulator
from .faults import FaultSpec, all_faults, get_fault
from .framework import CameraService
from .model import Event, LogLine
from .platforms import PlatformProfile, get_platform
from .workloads import Workload, get_workload


@dataclass
class RunResult:
    platform: PlatformProfile
    workload: Workload
    defects: tuple[str, ...]
    seed: int
    events: list[Event]
    logs: list[LogLine]
    violations: list[Violation]
    sim_ms: float
    wall_ms: float
    fault: FaultSpec | None = None
    counters: dict = field(default_factory=dict)

    @property
    def codes(self) -> set[str]:
        return {v.code for v in self.violations}

    def summary(self) -> dict:
        return {
            "platform": self.platform.code, "workload": self.workload.name, "defects": list(self.defects),
            "fault": self.fault.id if self.fault else None, "seed": self.seed,
            "sim_ms": round(self.sim_ms, 1), "wall_ms": round(self.wall_ms, 1),
            "counters": self.counters, "violations": [v.to_dict() for v in self.violations],
        }


def run_scenario(platform: str | PlatformProfile, workload: str | Workload, defects=(), seed: int = 7,
                 fault: FaultSpec | None = None) -> RunResult:
    prof = platform if isinstance(platform, PlatformProfile) else get_platform(platform)
    wl = workload if isinstance(workload, Workload) else get_workload(workload)
    sim = Simulator(seed=seed, defects=frozenset(defects))
    t0 = time.perf_counter()
    svc = CameraService(sim, prof)
    wl.fn(svc)
    svc.run(50)
    wall = (time.perf_counter() - t0) * 1000
    counters: dict[str, int] = {}
    for e in sim.events:
        counters[e.kind] = counters.get(e.kind, 0) + 1
    return RunResult(prof, wl, tuple(sorted(defects)), seed, sim.events, sim.logs,
                     run_checkers(sim.events, wl.budgets), sim.now / 1e6, wall, fault, counters)


def run_fault(fault_id: str, platform: str | None = None, seed: int = 7, fixed: bool = False,
              workload: str | None = None) -> RunResult:
    f = get_fault(fault_id)
    return run_scenario(platform or f.platform, workload or f.workload,
                        () if fixed else (f.defect,), seed, fault=f)


@dataclass
class SweepRow:
    fault: FaultSpec
    faulty: RunResult
    fixed: RunResult

    @property
    def detected(self) -> bool:
        return set(self.fault.expect) <= self.faulty.codes

    @property
    def clean(self) -> bool:
        return not self.fixed.violations

    @property
    def ok(self) -> bool:
        return self.detected and self.clean


def sweep(seed: int = 7, fault_ids: list[str] | None = None) -> list[SweepRow]:
    faults = [get_fault(i) for i in fault_ids] if fault_ids else list(all_faults())
    return [SweepRow(f, run_fault(f.id, seed=seed), run_fault(f.id, seed=seed, fixed=True)) for f in faults]
