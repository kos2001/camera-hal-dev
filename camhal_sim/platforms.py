"""대상 플랫폼 10종의 하드웨어/HAL 특성 프로파일.

식별자·family·버전 prefix 는 참고 저장소(camera-hal-analysis)의 PLATFORM_LINES 와
같다. 수치(ISP 포트 수, 버퍼 풀 크기, 지연 등)는 공개 스택(CamX, ExynosCamera,
mtkcam, rkaiq, ipu6-camera-hal, libcamera, cros-camera, V4L2, EmulatedCamera, EVS)의
구조적 특징을 반영한 **모델 값**이며 실측치가 아니다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "reference_scenarios.json"


@dataclass(frozen=True)
class SensorMode:
    width: int
    height: int
    max_fps: int

    @property
    def frame_duration_ns(self) -> int:
        return int(1e9 / self.max_fps)


@dataclass(frozen=True)
class PlatformProfile:
    code: str
    family: str
    fw_prefix: str
    isp: str                              # hardware | software | usb | v4l2 | virtual
    sensor_modes: tuple[SensorMode, ...]
    max_blob: tuple[int, int]
    isp_ports: int = 4                    # 동시 출력 포트(IFE/pad) 수
    isp_pads: bool = False                # V4L2 subdev pad 단위 포맷 협상(rkisp)
    raw_capable: bool = False
    pool_size: int = 16                   # 스트림당 gralloc 최대 버퍼
    starvation: str = "wait"              # wait(gralloc 대기) | drop(V4L2 큐 드롭)
    queue_depth: int = 6                  # V4L2 REQBUFS 개수(drop 정책)
    partial_results: int = 1
    isp_latency_ms: float = 30.0
    jpeg_ms: float = 120.0
    open_ms: float = 150.0
    fw_load_ms: float = 0.0               # ISP 펌웨어 로드
    tuning_parse_ms: float = 0.0
    sensor_power_ms: float = 60.0
    logical_multicam: bool = False
    external_camera: bool = False
    torch: bool = True
    channels: int = 1                     # EVS 서라운드뷰 채널 수
    camera_ids: tuple[str, ...] = ("0",)
    base_rss_mb: int = 180
    desc_ko: str = ""
    host_platforms: tuple[str, ...] = field(default_factory=tuple)

    def mode_for(self, width: int, height: int, fps: int) -> SensorMode | None:
        """해상도를 덮고 fps 를 만족하는 가장 작은 센서 모드."""
        ok = [m for m in self.sensor_modes if m.width >= width and m.height >= height and m.max_fps >= fps]
        return min(ok, key=lambda m: (m.width * m.height, m.max_fps)) if ok else None


_M = SensorMode

_PROFILES: tuple[PlatformProfile, ...] = (
    PlatformProfile(
        code="QCOM-CamX-Spectra", family="Qualcomm CamX HAL", fw_prefix="CAMX", isp="hardware",
        sensor_modes=(_M(12000, 9000, 10), _M(3840, 2160, 60), _M(1920, 1080, 60)),
        max_blob=(12000, 9000), isp_ports=4, raw_capable=True, pool_size=32, partial_results=2,
        isp_latency_ms=45, jpeg_ms=180, open_ms=250, logical_multicam=True, camera_ids=("0", "1"),
    ),
    PlatformProfile(
        code="EXYNOS-CamHAL3", family="Exynos Camera HAL", fw_prefix="EXHA", isp="hardware",
        sensor_modes=(_M(4000, 3000, 30), _M(3840, 2160, 60), _M(1920, 1080, 60)),
        max_blob=(4000, 3000), isp_ports=4, raw_capable=True, pool_size=16, partial_results=2,
        isp_latency_ms=35, open_ms=220,
    ),
    PlatformProfile(
        code="MTK-Camera3HAL", family="MediaTek Camera HAL", fw_prefix="MTKC", isp="hardware",
        sensor_modes=(_M(4000, 3000, 30), _M(3840, 2160, 30), _M(1920, 1080, 60)),
        max_blob=(4000, 3000), isp_ports=4, pool_size=16, partial_results=2, isp_latency_ms=38,
        external_camera=True, open_ms=200,
    ),
    PlatformProfile(
        code="RK-ISP3-HAL", family="Rockchip Camera HAL", fw_prefix="RKAQ", isp="hardware",
        sensor_modes=(_M(2592, 1944, 30), _M(1920, 1080, 60)),
        max_blob=(2592, 1944), isp_ports=3, isp_pads=True, raw_capable=True, pool_size=12,
        isp_latency_ms=30, open_ms=180, torch=False,
    ),
    PlatformProfile(
        code="IPU6-CameraHAL", family="Intel IPU6 HAL", fw_prefix="IPU6", isp="hardware",
        sensor_modes=(_M(1920, 1080, 30), _M(1280, 720, 60)),
        max_blob=(1920, 1080), isp_ports=3, pool_size=10, isp_latency_ms=25, open_ms=120,
        fw_load_ms=2280, tuning_parse_ms=620,
    ),
    PlatformProfile(
        code="LIBCAM-AndroidHAL", family="libcamera Android HAL", fw_prefix="LCAM", isp="software",
        sensor_modes=(_M(1920, 1080, 30), _M(1280, 720, 60)),
        max_blob=(1920, 1080), isp_ports=2, pool_size=8, isp_latency_ms=12, open_ms=160, torch=False,
    ),
    PlatformProfile(
        code="CROS-CameraHAL", family="ChromeOS Camera HAL", fw_prefix="CROS", isp="usb",
        sensor_modes=(_M(1920, 1080, 30), _M(1280, 720, 30)),
        max_blob=(1920, 1080), isp_ports=1, pool_size=8, isp_latency_ms=18, open_ms=300,
        camera_ids=("0", "1", "2"), torch=False,
    ),
    PlatformProfile(
        code="V4L2-GenericHAL", family="Generic V4L2 HAL", fw_prefix="V4L2", isp="v4l2",
        sensor_modes=(_M(1920, 1080, 15), _M(1280, 720, 60)),
        max_blob=(1920, 1080), isp_ports=1, pool_size=8, starvation="drop", queue_depth=6,
        isp_latency_ms=10, open_ms=140, torch=False,
    ),
    PlatformProfile(
        code="EMU-CameraHAL", family="Emulated Camera HAL", fw_prefix="EMUC", isp="virtual",
        sensor_modes=(_M(1920, 1080, 30), _M(640, 480, 60)),
        max_blob=(1920, 1080), isp_ports=3, pool_size=8, partial_results=3, isp_latency_ms=20,
        open_ms=60,
    ),
    PlatformProfile(
        code="AUTO-CamHAL-EVS", family="Automotive Camera HAL", fw_prefix="EVSC", isp="hardware",
        sensor_modes=(_M(1920, 1080, 30),),
        max_blob=(1920, 1080), isp_ports=4, pool_size=8, isp_latency_ms=22, open_ms=120,
        sensor_power_ms=380, channels=4, camera_ids=("0", "1", "2", "3"), torch=False,
    ),
)


@lru_cache(maxsize=1)
def _reference_platforms() -> dict[str, dict]:
    if not DATA.exists():
        return {}
    return {p["code"]: p for p in json.loads(DATA.read_text(encoding="utf-8"))["platforms"]}


def all_platforms() -> list[PlatformProfile]:
    ref = _reference_platforms()
    out = []
    for p in _PROFILES:
        r = ref.get(p.code, {})
        out.append(PlatformProfile(**{**p.__dict__, "desc_ko": r.get("desc_ko", ""),
                                      "host_platforms": tuple(r.get("host_platforms", ()))}))
    return out


def get_platform(code: str) -> PlatformProfile:
    for p in all_platforms():
        if p.code.lower() == code.lower() or p.fw_prefix.lower() == code.lower():
            return p
    raise KeyError(f"알 수 없는 플랫폼: {code} (list-platforms 로 확인)")


def platforms_of_family(family: str) -> list[PlatformProfile]:
    return [p for p in all_platforms() if p.family == family]
