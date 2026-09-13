"""이산 사건 시뮬레이션 커널 — 시계, 사건 큐, 난수, 관찰 기록.

HAL 내부 스레드를 실제로 돌리지 않고, 각 동작을 "언제 일어나는가"로 사건 큐에
올린다. 전달 순서는 (시각, 등록 순서)이므로 같은 시드면 결과가 항상 같다.
"""
from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from typing import Any, Callable

from .model import Event, LogLine


@dataclass(order=True)
class _Scheduled:
    t_ns: int
    seq: int
    fn: Callable[..., Any] = field(compare=False)
    args: tuple = field(compare=False)
    cancelled: bool = field(default=False, compare=False)

    def cancel(self) -> None:
        self.cancelled = True


class Simulator:
    def __init__(self, seed: int = 1, defects: frozenset[str] | set[str] = frozenset()):
        self.now = 0
        self.rng = random.Random(seed)
        self.seed = seed
        self.defects = frozenset(defects)
        self.events: list[Event] = []
        self.logs: list[LogLine] = []
        self._q: list[_Scheduled] = []
        self._seq = 0

    # -- 결함 스위치 -------------------------------------------------------
    def defect(self, name: str) -> bool:
        return name in self.defects

    # -- 사건 큐 -------------------------------------------------------------
    def at(self, t_ns: int, fn: Callable[..., Any], *args: Any) -> _Scheduled:
        item = _Scheduled(max(int(t_ns), self.now), self._seq, fn, args)
        self._seq += 1
        heapq.heappush(self._q, item)
        return item

    def after(self, delay_ns: float, fn: Callable[..., Any], *args: Any) -> _Scheduled:
        return self.at(self.now + int(delay_ns), fn, *args)

    def run_until(self, t_ns: int) -> None:
        while self._q and self._q[0].t_ns <= t_ns:
            item = heapq.heappop(self._q)
            if item.cancelled:
                continue
            self.now = item.t_ns
            item.fn(*item.args)
        self.now = max(self.now, t_ns)

    def advance(self, delay_ns: float) -> None:
        self.run_until(self.now + int(delay_ns))

    def pending(self) -> int:
        return sum(1 for s in self._q if not s.cancelled)

    # -- 관찰 기록 ---------------------------------------------------------
    def emit(self, kind: str, **data: Any) -> Event:
        ev = Event(self.now, kind, data)
        self.events.append(ev)
        return ev

    def log(self, tag: str, msg: str, level: str = "I") -> None:
        self.logs.append(LogLine(self.now, level, tag, msg))

    def events_of(self, *kinds: str) -> list[Event]:
        return [e for e in self.events if e.kind in kinds]
