"""HAL 하위 구성요소 모델 — 버퍼 풀, 센서, 3A, 열, 락 순서, SELinux."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..engine import Simulator
from ..model import MS
from ..platforms import SensorMode


# --------------------------------------------------------------------------- #
# 버퍼 풀 (gralloc 스트림 버퍼 / V4L2 큐)
# --------------------------------------------------------------------------- #
class BufferPool:
    def __init__(self, sim: Simulator, stream_id: int, size: int, usage: int, tag: str):
        self.sim = sim
        self.stream_id = stream_id
        self.size = size
        self.usage = usage
        self.tag = tag
        self._free = list(range(size))
        self.held: dict[int, str] = {}      # buf_id -> owner
        self.peak = 0

    @property
    def outstanding(self) -> int:
        return len(self.held)

    def acquire(self, owner: str) -> int | None:
        if not self._free:
            return None
        buf = self._free.pop(0)
        self.held[buf] = owner
        self.peak = max(self.peak, self.outstanding)
        return buf

    def release(self, buf: int) -> None:
        if buf in self.held:
            del self.held[buf]
            self._free.append(buf)


# --------------------------------------------------------------------------- #
# 센서: 전원 시퀀스, 모드, 노출 설정의 프레임 지연 적용
# --------------------------------------------------------------------------- #
class Sensor:
    CHIP_ID = 0x3C21
    SETTINGS_DELAY = 2          # 노출/게인 레지스터가 실제 프레임에 반영되기까지 프레임 수

    def __init__(self, sim: Simulator, name: str):
        self.sim = sim
        self.name = name
        self.powered = False
        self.streaming = False
        self.mode: SensorMode | None = None
        self.exposure_ns = 10 * MS
        self.gain = 1.0
        self._pipeline: list[tuple[int, float]] = []

    def power_on(self, delay_ms: float, cold: bool) -> bool:
        """전원 인가 후 delay_ms 대기하고 chip-id 를 읽는다. 안정화 전이면 실패."""
        settle = max(0.2, self.sim.rng.gauss(0.78 if cold else 0.45, 0.14))
        self.sim.log(self.name, f"power on, delay {delay_ms:g}ms, mclk enabled")
        self.sim.advance(delay_ms * MS)
        if delay_ms < settle:
            self.sim.log(self.name, "read chip id failed (-EIO), got 0x0000", "E")
            self.powered = False
            return False
        self.powered = True
        self.sim.log(self.name, f"chip id 0x{self.CHIP_ID:04X} ok (settle {settle:.2f}ms)", "D")
        return True

    def set_mode(self, mode: SensorMode) -> bool:
        switched = self.mode is not None and self.mode != mode
        if switched:
            self.sim.log(self.name, f"mode switch: {self.mode.width}x{self.mode.height}@{self.mode.max_fps}"
                                    f" -> {mode.width}x{mode.height}@{mode.max_fps}")
        self.mode = mode
        return switched

    def queue_settings(self, exposure_ns: int, gain: float) -> None:
        self._pipeline.append((exposure_ns, gain))

    def latch_frame(self) -> tuple[int, float]:
        """SOF 에서 호출. SETTINGS_DELAY 프레임 전에 쓴 값이 이 프레임에 적용된다."""
        if len(self._pipeline) > self.SETTINGS_DELAY:
            self.exposure_ns, self.gain = self._pipeline.pop(0)
        return self.exposure_ns, self.gain

    def reset_pipeline(self) -> None:
        self._pipeline.clear()


# --------------------------------------------------------------------------- #
# 3A: AE (지연 보상 유무가 수렴/진동을 가른다), AWB
# --------------------------------------------------------------------------- #
def scene_luma(lux: float, exposure_ns: int, gain: float) -> float:
    """정규화 휘도(0..1). 18% 회색 기준 선형 모델 + 포화."""
    return min(1.0, 0.18 * (lux / 100.0) * (exposure_ns / (10 * MS)) * gain)


class AeController:
    TARGET = 0.18
    MAX_GAIN = 16.0
    DAMPING = 0.6

    def __init__(self, sim: Simulator):
        self.sim = sim
        self.cmd_exposure = 10 * MS
        self.cmd_gain = 1.0
        self.state = "SEARCHING"
        self.target = self.TARGET

    def update(self, measured: float, applied: tuple[int, float], max_exposure_ns: int,
               min_exposure_ns: int = 100_000) -> tuple[int, float]:
        err = self.target / max(measured, 1e-4)
        self.state = "CONVERGED" if abs(measured / self.target - 1) < 0.08 else "SEARCHING"
        if self.sim.defect("ae_no_latency_compensation"):
            # 결함: 측정값을 만든 설정(2프레임 전)이 아니라 최신 명령값에 전체 비율을 곱한다.
            total = self.cmd_exposure * self.cmd_gain * err
        else:
            total = applied[0] * applied[1] * err ** self.DAMPING
        exposure = int(min(max(total, min_exposure_ns), max_exposure_ns))
        gain = min(max(total / exposure, 1.0), self.MAX_GAIN)
        if exposure >= max_exposure_ns and abs(gain - self.cmd_gain) > 0.5:
            self.sim.log("AE", f"exposure clamped at max {max_exposure_ns / MS:.0f}ms, "
                               f"gain {self.cmd_gain:.1f}->{gain:.1f}", "W")
        self.cmd_exposure, self.cmd_gain = exposure, gain
        return exposure, gain


def cct_to_gains(cct: float) -> tuple[float, float]:
    """색온도 → (R, B) 게인. 5500K 에서 약 (1.9, 1.45)."""
    r = 1.9 * (cct / 5500.0) ** 0.9
    b = 1.45 * (5500.0 / cct) ** 0.75
    return round(r, 3), round(b, 3)


# --------------------------------------------------------------------------- #
# 열 모델
# --------------------------------------------------------------------------- #
@dataclass
class ThermalModel:
    ambient_c: float = 35.0
    rise_c: float = 45.0
    tau_s: float = 60.0
    started_ns: int = 0

    def temp(self, now_ns: int) -> float:
        t = (now_ns - self.started_ns) / 1e9
        return self.ambient_c + self.rise_c * (1 - math.exp(-t / self.tau_s))

    def level(self, now_ns: int) -> int:
        c = self.temp(now_ns)
        return 0 if c < 55 else 1 if c < 62 else 2 if c < 68 else 3


# --------------------------------------------------------------------------- #
# 락 순서 — 두 스레드가 겹칠 때 서로 다른 락을 먼저 잡으면 교착
# --------------------------------------------------------------------------- #
@dataclass
class LockOp:
    thread: str
    func: str
    order: tuple[str, str]
    t_first_ns: int
    hold_ns: int      # 첫 락 획득 후 두 번째 락 요청까지

    def overlaps(self, other: "LockOp") -> bool:
        return (self.t_first_ns <= other.t_first_ns < self.t_first_ns + self.hold_ns
                or other.t_first_ns <= self.t_first_ns < other.t_first_ns + other.hold_ns)


def detect_deadlock(a: LockOp, b: LockOp) -> bool:
    return a.overlaps(b) and a.order[0] != b.order[0] and a.order[0] == b.order[1]


# --------------------------------------------------------------------------- #
# SELinux (camera.provider 도메인)
# --------------------------------------------------------------------------- #
@dataclass
class SePolicy:
    rules: set[tuple[str, str, str]] = field(default_factory=set)

    def allowed(self, sim: Simulator, scontext: str, tclass: str, node: str, perms: tuple[str, ...]) -> bool:
        missing = [p for p in perms if (scontext, tclass, p) not in self.rules]
        if missing:
            sim.log("avc", f"denied {{ {' '.join(missing)} }} for name=\"{node}\" "
                           f"scontext={scontext} tcontext={tclass} permissive=0", "E")
            return False
        return True
