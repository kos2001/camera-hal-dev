"""Camera HAL3 도메인 타입 — AOSP camera.device(AIDL)/camera3.h 의 개념을 축약해 옮겼다.

시뮬레이터 전역에서 쓰는 값 타입만 둔다. 동작(상태 머신)은 hal/ 아래에 있다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, IntFlag


class PixelFormat(IntEnum):
    """android.hardware.graphics.common.PixelFormat 의 카메라 관련 부분."""
    RAW_SENSOR = 0x20
    BLOB = 0x21                # JPEG
    PRIVATE = 0x22             # IMPLEMENTATION_DEFINED
    YUV_420_888 = 0x23
    RAW10 = 0x25
    MJPEG = 0x4D4A5047         # V4L2 전용(외장 USB), HAL3 스트림 포맷은 아니다


class Usage(IntFlag):
    """gralloc BufferUsage 비트 (필요한 것만)."""
    CPU_READ = 0x3
    GPU_TEXTURE = 0x100
    COMPOSER_OVERLAY = 0x800
    VIDEO_ENCODER = 0x10000
    CAMERA_OUTPUT = 0x20000


class Status(IntEnum):
    """HAL 반환 코드(음수 errno 관례)."""
    OK = 0
    PERMISSION_DENIED = -13
    CAMERA_IN_USE = -16
    NO_DEVICE = -19
    ILLEGAL_ARGUMENT = -22
    INTERNAL_ERROR = -38
    TIMED_OUT = -110


class ErrorCode(IntEnum):
    """ICameraDeviceCallback.notify ErrorMsg.errorCode."""
    ERROR_DEVICE = 1
    ERROR_REQUEST = 2
    ERROR_RESULT = 3
    ERROR_BUFFER = 4


class DeviceStatus(IntEnum):
    """ICameraProviderCallback.cameraDeviceStatusChange."""
    NOT_PRESENT = 0
    PRESENT = 1


# 메타데이터 키 — 문자열 태그로 단순화했다.
class Key:
    SENSOR_TIMESTAMP = "android.sensor.timestamp"
    SENSOR_EXPOSURE_TIME = "android.sensor.exposureTime"
    SENSOR_FRAME_DURATION = "android.sensor.frameDuration"
    SENSOR_SENSITIVITY = "android.sensor.sensitivity"
    AE_STATE = "android.control.aeState"
    AE_TARGET_FPS_RANGE = "android.control.aeTargetFpsRange"
    SCENE_MODE = "android.control.sceneMode"
    FLASH_STATE = "android.flash.state"
    FLASH_MODE = "android.flash.mode"
    CC_GAINS = "android.colorCorrection.gains"
    JPEG_ORIENTATION = "android.jpeg.orientation"
    NOISE_REDUCTION_MODE = "android.noiseReduction.mode"
    ZOOM_RATIO = "android.control.zoomRatio"
    LOGICAL_ACTIVE_PHYSICAL_ID = "android.logicalMultiCamera.activePhysicalId"
    # vendor tags
    TUNING_ID = "vendor.camhal.tuningId"
    THERMAL_THROTTLE = "vendor.camhal.thermalThrottle"


# CTS/ITS 가 요구하는 정적 메타데이터 필수 키(발췌).
REQUIRED_STATIC_KEYS: tuple[str, ...] = (
    "android.info.supportedHardwareLevel",
    "android.lens.facing",
    "android.lens.info.availableFocalLengths",
    "android.sensor.info.activeArraySize",
    "android.sensor.info.pixelArraySize",
    "android.sensor.info.physicalSize",
    "android.sensor.info.exposureTimeRange",
    "android.sensor.info.sensitivityRange",
    "android.sensor.info.timestampSource",
    "android.sensor.orientation",
    "android.scaler.availableStreamConfigurations",
    "android.scaler.availableMaxDigitalZoom",
    "android.control.aeAvailableTargetFpsRanges",
    "android.control.aeCompensationRange",
    "android.control.availableSceneModes",
    "android.request.availableCapabilities",
    "android.request.partialResultCount",
    "android.request.pipelineMaxDepth",
    "android.request.maxNumOutputStreams",
    "android.flash.info.available",
    "android.jpeg.availableThumbnailSizes",
)


@dataclass(frozen=True)
class Stream:
    id: int
    width: int
    height: int
    format: PixelFormat
    usage: int = int(Usage.GPU_TEXTURE)
    physical_id: str | None = None

    @property
    def megapixels(self) -> float:
        return self.width * self.height / 1e6

    def label(self) -> str:
        return f"s{self.id}:{self.width}x{self.height} {self.format.name}"


@dataclass
class CaptureRequest:
    frame_number: int
    stream_ids: tuple[int, ...]
    settings: dict = field(default_factory=dict)
    zsl: bool = False            # ZSL 링 버퍼에서 프레임 선택
    reprocess: bool = False      # 입력 스트림 재처리
    storage_slow: bool = False   # 앱 저장 지연 → HAL 내부 취소 경로


@dataclass
class Event:
    """관찰 가능한 사건 한 건 (프레임워크 콜백·커널 로그·계측)."""
    t_ns: int
    kind: str
    data: dict

    def to_dict(self) -> dict:
        return {"t_ms": round(self.t_ns / 1e6, 3), "kind": self.kind, **self.data}


@dataclass
class LogLine:
    t_ns: int
    level: str   # V D I W E F
    tag: str
    msg: str

    def render(self) -> str:
        return f"{self.t_ns / 1e9:10.4f} {self.level} {self.tag}: {self.msg}"


MS = 1_000_000
SEC = 1_000_000_000
