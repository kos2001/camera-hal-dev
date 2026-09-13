"""워크로드 — 앱/테스트 하네스가 프레임워크를 통해 HAL 을 사용하는 절차.

각 워크로드는 CameraService API 만 호출한다(HAL 내부를 직접 만지지 않는다).
환경 조건(조도·부하·핫플러그·절전 등)은 provider 의 외부 사건으로 준다.
budgets 는 검사기가 쓰는 요구 기준(예: 첫 프레임 지연 한계)이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .framework import CameraService
from .model import MS, Key, PixelFormat, Status, Stream, Usage

PREVIEW_USAGE = int(Usage.GPU_TEXTURE | Usage.COMPOSER_OVERLAY)
VIDEO_USAGE = int(Usage.VIDEO_ENCODER)


@dataclass(frozen=True)
class Workload:
    name: str
    title: str
    steps: tuple[str, ...]
    fn: Callable[[CameraService], None]
    budgets: dict = field(default_factory=dict)


REGISTRY: dict[str, Workload] = {}


def workload(name: str, title: str, steps: tuple[str, ...], **budgets):
    def deco(fn):
        REGISTRY[name] = Workload(name, title, steps, fn, budgets)
        return fn
    return deco


def get_workload(name: str) -> Workload:
    if name not in REGISTRY:
        raise KeyError(f"알 수 없는 워크로드: {name} (list-workloads 로 확인)")
    return REGISTRY[name]


# --------------------------------------------------------------------------- #
# 공통 헬퍼
# --------------------------------------------------------------------------- #
def _preview_size(svc: CameraService, fps: int = 30) -> tuple[int, int]:
    for w, h in ((1920, 1080), (1280, 720), (640, 480)):
        if svc.profile.mode_for(w, h, fps):
            return w, h
    m = svc.profile.sensor_modes[-1]
    return m.width, m.height


def _preview(svc: CameraService, sid: int = 0, fps: int = 30, usage: int = PREVIEW_USAGE) -> Stream:
    w, h = _preview_size(svc, fps)
    return Stream(sid, w, h, PixelFormat.PRIVATE, usage)


def _fps(fps: int) -> dict:
    return {Key.AE_TARGET_FPS_RANGE: (fps, fps)}


def _stream_preview(svc: CameraService, camera: str = "0", seconds: float = 3.0, fps: int = 30,
                    settings: dict | None = None, params: dict | None = None, client: str = "app"):
    dev = svc.open(camera, client)
    if dev is None:
        return None
    if svc.configure(dev, [_preview(svc, fps=fps)], {**_fps(fps), **(params or {})}) != Status.OK:
        svc.close(dev)
        return None
    svc.repeat(dev, (0,), settings)
    svc.run(seconds * 1000)
    return dev


# --------------------------------------------------------------------------- #
# 기본
# --------------------------------------------------------------------------- #
@workload("preview", "기본 프리뷰 10초", ("카메라 0 열기", "1080p PRIVATE 프리뷰 구성", "반복 요청 10초", "닫기"))
def _w_preview(svc):
    dev = _stream_preview(svc, seconds=10)
    svc.close(dev)


# --------------------------------------------------------------------------- #
# Stream Config
# --------------------------------------------------------------------------- #
@workload("three_stream_snapshot", "프리뷰+비디오+최대 해상도 스냅샷 3스트림 구성",
          ("isStreamCombinationSupported 로 조합 확인", "1080p 프리뷰 + 4K 비디오 + 최대 BLOB 구성",
           "스냅샷 1장 촬영", "닫기"))
def _w_three_stream(svc):
    p = svc.profile
    dev = svc.open("0")
    vw, vh = (3840, 2160) if p.mode_for(3840, 2160, 30) else _preview_size(svc)
    streams = [_preview(svc), Stream(1, vw, vh, PixelFormat.PRIVATE, VIDEO_USAGE),
               Stream(2, *p.max_blob, PixelFormat.BLOB, int(Usage.CPU_READ))]
    if svc.configure(dev, streams, _fps(30)) == Status.OK:
        svc.repeat(dev, (0, 1))
        svc.run(1000)
        svc.submit(dev, (2,))
        svc.run(1500)
    svc.close(dev)


@workload("raw_plus_yuv", "RAW10 + YUV 동시 스트림 구성",
          ("RAW capability 확인", "RAW10 전체 해상도 + 1080p YUV 구성", "1초 스트리밍", "닫기"))
def _w_raw_yuv(svc):
    p = svc.profile
    dev = svc.open("0")
    full = max(p.sensor_modes, key=lambda m: m.width * m.height)
    streams = [Stream(0, full.width, full.height, PixelFormat.RAW10, int(Usage.CPU_READ)),
               Stream(1, *_preview_size(svc), PixelFormat.YUV_420_888, int(Usage.CPU_READ))]
    if svc.configure(dev, streams, _fps(30)) == Status.OK:
        svc.repeat(dev, (0, 1))
        svc.run(1000)
    svc.close(dev)


@workload("fps_range_config", "광고된 최대 fps 로 1080p 스트림 구성",
          ("정적 메타데이터에서 1080p 의 최대 AE target fps 범위 선택", "그 fps 로 구성", "1초 스트리밍"))
def _w_fps_range(svc):
    dev = svc.open("0")
    if dev is None:
        return
    md = svc.static_metadata(dev)
    w, h = 1920, 1080
    ranges = md.get("android.control.aeAvailableTargetFpsRanges", {}).get(f"{w}x{h}", [(30, 30)])
    fps = max(hi for _, hi in ranges)
    streams = [Stream(0, w, h, PixelFormat.PRIVATE, PREVIEW_USAGE)]
    if svc.configure(dev, streams, _fps(fps)) == Status.OK:
        svc.repeat(dev, (0,))
        svc.run(1000)
    svc.close(dev)


@workload("consumer_usage", "GPU+컴포저 소비자 usage 로 프리뷰",
          ("usage=GPU_TEXTURE|COMPOSER_OVERLAY(0x900) 프리뷰 구성", "2초 스트리밍", "소비자 lock 결과 확인"))
def _w_consumer_usage(svc):
    dev = _stream_preview(svc, seconds=2)
    svc.close(dev)


# --------------------------------------------------------------------------- #
# Buffer Management
# --------------------------------------------------------------------------- #
@workload("zsl_burst", "ZSL 연사 30장 (저장 지연)",
          ("프리뷰 + BLOB 구성", "ZSL 활성 상태에서 연사 30장", "저장 지연으로 캡처 취소 경로 진입", "6초 관찰"))
def _w_zsl_burst(svc):
    p = svc.profile
    dev = svc.open("0")
    bw, bh = next(((m.width, m.height) for m in p.sensor_modes if m.width * m.height <= 20e6), p.max_blob)
    if svc.configure(dev, [_preview(svc), Stream(1, bw, bh, PixelFormat.BLOB, int(Usage.CPU_READ))],
                     _fps(30)) != Status.OK:
        return
    svc.repeat(dev, (0,))
    svc.run(1000)
    for _ in range(30):
        svc.submit(dev, (1,), zsl=True, storage_slow=True)
        svc.run(100)
    svc.run(7000)
    svc.close(dev)


@workload("dual_client", "두 앱이 같은 카메라를 동시에 사용",
          ("앱1 카메라 0 열기·프리뷰", "앱2 카메라 0 열기·프리뷰", "2초 동시 스트리밍", "양쪽 프레임 내용 확인"))
def _w_dual_client(svc):
    d1 = _stream_preview(svc, seconds=0.5, client="app1", fps=30)
    d2 = svc.open("0", client="app2")
    if d2 is not None and svc.configure(d2, [_preview(svc)], _fps(30)) == Status.OK:
        svc.repeat(d2, (0,))
    svc.run(2000)
    svc.close(d2)
    svc.close(d1)


@workload("high_load_preview", "고부하(합성 지연) 720p60 프리뷰",
          ("720p60 프리뷰 구성", "디스플레이 합성 지연이 큰 고부하 환경", "10초 스트리밍", "프레임 유실률 측정"),
          max_drop_ratio=0.01)
def _w_high_load(svc):
    svc.provider.env["high_load"] = True
    dev = svc.open("0")
    fps = 60 if svc.profile.mode_for(1280, 720, 60) else 30
    if svc.configure(dev, [Stream(0, 1280, 720, PixelFormat.PRIVATE, PREVIEW_USAGE)], _fps(fps)) == Status.OK:
        svc.repeat(dev, (0,))
        svc.run(10_000)
    svc.close(dev)


# --------------------------------------------------------------------------- #
# Request Pipeline
# --------------------------------------------------------------------------- #
@workload("flush_inflight", "진행 중 요청이 있는 상태에서 flush",
          ("프리뷰 반복 요청", "요청이 파이프라인에 있는 순간 flush", "flush 반환 후 콜백 관찰 (5회 반복)"))
def _w_flush(svc):
    dev = svc.open("0")
    if svc.configure(dev, [_preview(svc)], _fps(30)) != Status.OK:
        return
    for _ in range(5):
        svc.repeat(dev, (0,))
        svc.run(400)
        while not dev.session.inflight:
            svc.sim.advance(1 * MS)
        svc.flush(dev)
        svc.run(300)
    svc.close(dev)


@workload("partial_results_order", "partial result 순서 관찰 (10초)",
          ("partial result 3단계 플랫폼에서 프리뷰", "10초 반복 요청", "프레임별 partial 도착 순서 기록"))
def _w_partials(svc):
    dev = _stream_preview(svc, seconds=10)
    svc.close(dev)


@workload("stream_stop_race", "스트림 중 임의 시점 종료 60회",
          ("프리뷰 시작", "0.3초 + 임의 지연 후 close", "60회 반복", "watchdog 관찰"))
def _w_stop_race(svc):
    for _ in range(60):
        dev = svc.open("0")
        if dev is None:
            break
        if svc.configure(dev, [_preview(svc)], _fps(30)) != Status.OK:
            break
        svc.repeat(dev, (0,))
        svc.run(300 + svc.sim.rng.uniform(0, 34))
        if svc.close(dev) != Status.OK or not svc.provider.alive:
            break
        svc.run(20)


@workload("reprocess_capture", "고화질 재처리 요청",
          ("세션 파라미터 noiseReduction=FAST", "일반 캡처 1장", "noiseReduction=HIGH_QUALITY 재처리 요청", "결과 설정 비교"))
def _w_reprocess(svc):
    dev = svc.open("0")
    blob = Stream(1, *_preview_size(svc), PixelFormat.BLOB, int(Usage.CPU_READ))
    if svc.configure(dev, [_preview(svc), blob], {**_fps(30), Key.NOISE_REDUCTION_MODE: "FAST"}) != Status.OK:
        return
    svc.repeat(dev, (0,), {Key.NOISE_REDUCTION_MODE: "FAST"})
    svc.run(500)
    svc.submit(dev, (1,), {Key.NOISE_REDUCTION_MODE: "FAST"})
    svc.run(500)
    svc.submit(dev, (1,), {Key.NOISE_REDUCTION_MODE: "HIGH_QUALITY"}, reprocess=True)
    svc.run(800)
    svc.close(dev)


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #
@workload("logical_zoom_switch", "논리 멀티카메라 0.6x↔3x 연속 줌",
          ("논리 카메라 프리뷰", "10프레임마다 1.0x↔3.0x 전환 (20회)", "결과의 physical camera metadata 확인"))
def _w_zoom(svc):
    dev = _stream_preview(svc, seconds=0.5)
    if dev is None:
        return
    for i in range(20):
        svc.repeat(dev, (0,), {Key.ZOOM_RATIO: 3.0 if i % 2 else 1.0})
        svc.run(340)
    svc.close(dev)


@workload("static_metadata_audit", "CTS/ITS 정적 메타데이터 점검",
          ("카메라 0 열기", "getCameraCharacteristics", "필수 키 존재 확인"))
def _w_static(svc):
    dev = svc.open("0")
    if dev is not None:
        svc.static_metadata(dev)
    svc.close(dev)


@workload("rotate_during_capture", "촬영 처리 중 기기 회전",
          ("기기 90° 상태에서 jpeg.orientation=90 캡처", "처리 중 기기를 0° 로 회전", "결과/EXIF 방향 비교"))
def _w_rotate(svc):
    svc.provider.set_orientation(90)
    dev = svc.open("0")
    blob = Stream(1, *_preview_size(svc), PixelFormat.BLOB, int(Usage.CPU_READ))
    if svc.configure(dev, [_preview(svc), blob], _fps(30)) != Status.OK:
        return
    svc.repeat(dev, (0,))
    svc.run(500)
    for _ in range(3):
        svc.provider.set_orientation(90)
        svc.submit(dev, (1,), {Key.JPEG_ORIENTATION: 90})
        svc.run(40)
        svc.provider.set_orientation(0)
        svc.run(600)
    svc.close(dev)


@workload("scene_mode_sweep", "광고된 scene mode 순회",
          ("availableSceneModes 조회", "모드별 1.5초 프리뷰", "결과 영상 특성으로 적용 여부 확인"))
def _w_scene(svc):
    svc.provider.env["lux"] = 60
    dev = svc.open("0")
    if dev is None:
        return
    modes = svc.static_metadata(dev).get("android.control.availableSceneModes", ["DISABLED"])
    if svc.configure(dev, [_preview(svc)], _fps(30)) != Status.OK:
        return
    for mode in modes:
        svc.repeat(dev, (0,), {Key.SCENE_MODE: mode})
        svc.run(1500)
    svc.close(dev)


# --------------------------------------------------------------------------- #
# 3A Tuning
# --------------------------------------------------------------------------- #
@workload("low_light_preview", "저조도(8 lux) 프리뷰 AE 수렴",
          ("조도 8 lux", "30fps 프리뷰 8초", "휘도 시계열의 진동 확인"))
def _w_low_light(svc):
    svc.provider.env["lux"] = 8
    dev = _stream_preview(svc, seconds=8)
    svc.close(dev)


@workload("flash_capture", "텅스텐 조명 + 플래시 촬영",
          ("주변광 2800K 저조도", "프리뷰 1초 (AWB 수렴)", "flash.mode=SINGLE 캡처", "색온도 오차 측정"))
def _w_flash(svc):
    svc.provider.env.update(lux=40, ambient_cct=2800)
    dev = svc.open("0")
    blob = Stream(1, *_preview_size(svc), PixelFormat.BLOB, int(Usage.CPU_READ))
    if svc.configure(dev, [_preview(svc), blob], _fps(30)) != Status.OK:
        return
    svc.repeat(dev, (0,))
    svc.run(1000)
    for _ in range(3):
        svc.submit(dev, (1,), {Key.FLASH_MODE: "SINGLE"})
        svc.run(700)
    svc.close(dev)


@workload("aiq_restart_preview", "프리뷰 중 3A 서버 재시작",
          ("프리뷰 2초", "3A(aiq) 서버 크래시 후 재시작", "프리뷰 2초 추가", "결과 tuning id 확인"))
def _w_aiq(svc):
    dev = _stream_preview(svc, seconds=2)
    if dev is None:
        return
    svc.provider.aiq_server_restart()
    svc.run(2000)
    svc.close(dev)


# --------------------------------------------------------------------------- #
# Sensor Driver
# --------------------------------------------------------------------------- #
@workload("cold_boot_open", "콜드 부팅 직후 카메라 열기 60회",
          ("저온 콜드 부팅", "부팅 직후 카메라 0 열기", "닫고 전원 차단", "60회 반복, 실패율 집계"))
def _w_cold_boot(svc):
    svc.provider.env["cold_boot"] = True
    for _ in range(60):
        dev = svc.open("0")
        svc.close(dev)
        svc.run(50)


@workload("usb_webcam_mjpeg", "허프만 테이블 생략 MJPEG 웹캠",
          ("DHT 를 첫 프레임에만 보내는 웹캠 연결", "720p MJPEG 프리뷰", "3초 관찰"))
def _w_mjpeg(svc):
    svc.provider.env["webcam_omits_dht"] = True
    dev = _stream_preview(svc, seconds=3)
    svc.close(dev)


@workload("sensor_mode_switch", "4K30 → 1080p60 센서 모드 전환",
          ("저조도(20 lux) 4K30 프리뷰 2초 (노출 33ms 로 수렴)", "1080p60 으로 재구성", "첫 프레임 노출/프레임 길이 비교"))
def _w_mode_switch(svc):
    svc.provider.env["lux"] = 20
    p = svc.profile
    big = max((m for m in p.sensor_modes if m.width * m.height <= 20e6), key=lambda m: m.width * m.height)
    dev = svc.open("0")
    if svc.configure(dev, [Stream(0, big.width, big.height, PixelFormat.PRIVATE, PREVIEW_USAGE)], _fps(30)) != Status.OK:
        return
    svc.repeat(dev, (0,))
    svc.run(2000)
    fps = 60 if p.mode_for(1920, 1080, 60) else 30
    if svc.configure(dev, [Stream(0, 1920, 1080, PixelFormat.PRIVATE, PREVIEW_USAGE)], _fps(fps)) == Status.OK:
        svc.repeat(dev, (0,))
        svc.run(1000)
    svc.close(dev)


# --------------------------------------------------------------------------- #
# Sync Timing
# --------------------------------------------------------------------------- #
@workload("video_60fps", "1080p60 녹화 20초",
          ("1080p 프리뷰 + 1080p 인코더 스트림 60fps", "20초 녹화", "스트림별 타임스탬프 단조성 확인"))
def _w_video60(svc):
    dev = svc.open("0")
    fps = 60 if svc.profile.mode_for(1920, 1080, 60) else 30
    streams = [_preview(svc, fps=fps), Stream(1, *_preview_size(svc, fps), PixelFormat.PRIVATE, VIDEO_USAGE)]
    if svc.configure(dev, streams, _fps(fps)) == Status.OK:
        svc.repeat(dev, (0, 1))
        svc.run(20_000)
    svc.close(dev)


@workload("surround_view", "서라운드뷰 4채널 동시 스트리밍",
          ("채널 0~3 열기", "채널별 1080p30 구성", "3초 동시 스트리밍", "채널 간 타임스탬프 편차 측정"),
          max_skew_ms=2.0)
def _w_surround(svc):
    devs = []
    for cid in svc.provider.camera_ids():
        dev = svc.open(cid)
        if dev and svc.configure(dev, [_preview(svc)], _fps(30)) == Status.OK:
            devs.append(dev)
    for dev in devs:
        svc.repeat(dev, (0,))
    svc.run(3000)
    for dev in devs:
        svc.close(dev)


@workload("record_start_av", "절전 복귀 이력이 있는 기기에서 녹화 시작",
          ("이전 절전으로 boottime≠monotonic (680ms)", "프리뷰+비디오 녹화 시작", "초기 프레임 타임스탬프와 오디오 시계 비교"),
          max_av_drift_ms=40)
def _w_av(svc):
    svc.provider.boottime_offset_ns += 680 * MS
    dev = svc.open("0")
    streams = [_preview(svc), Stream(1, *_preview_size(svc), PixelFormat.PRIVATE, VIDEO_USAGE)]
    if svc.configure(dev, streams, _fps(30)) == Status.OK:
        svc.repeat(dev, (0, 1))
        svc.run(2000)
    svc.close(dev)


# --------------------------------------------------------------------------- #
# Performance
# --------------------------------------------------------------------------- #
@workload("session_reconfigure_loop", "세션 재구성 220회 반복",
          ("카메라 열기", "프리뷰 구성 → 3프레임 → 재구성 (220회)", "provider RSS 추이 관찰"))
def _w_reconfig(svc):
    dev = svc.open("0")
    for _ in range(220):
        if not svc.provider.alive or dev is None:
            break
        if svc.configure(dev, [_preview(svc)], _fps(30)) != Status.OK:
            break
        for _ in range(3):
            svc.submit(dev, (0,))
        svc.run(150)
    if svc.provider.alive:
        svc.close(dev)


@workload("open_to_first_frame", "화상회의 앱 카메라 열기 → 첫 프레임",
          ("카메라 0 열기", "720p 프리뷰 구성", "첫 프레임까지 지연 측정 (3회)"), first_frame_ms=1000)
def _w_first_frame(svc):
    for _ in range(3):
        dev = svc.open("0")
        if dev and svc.configure(dev, [Stream(0, 1280, 720, PixelFormat.PRIVATE, PREVIEW_USAGE)], _fps(30)) == Status.OK:
            svc.repeat(dev, (0,))
            svc.run(1200)
        svc.close(dev)
        svc.run(500)


@workload("preview_30fps", "1080p30 프리뷰 처리량",
          ("1080p30 프리뷰 구성", "6초 스트리밍", "결과 fps 측정"), min_fps=27.0)
def _w_fps30(svc):
    dev = _stream_preview(svc, seconds=6)
    svc.close(dev)


@workload("gear_reverse", "후진 기어 → 후방 카메라 표시",
          ("후진 기어 입력(t=0)", "후방 카메라 열기·구성·스트리밍", "첫 프레임 표시 시각 측정"), first_frame_ms=1000)
def _w_gear(svc):
    svc.mark_trigger("gear reverse")
    dev = _stream_preview(svc, camera="0", seconds=2.5)
    svc.close(dev)


@workload("long_recording_thermal", "4K 장시간 녹화 (발열)",
          ("4K 비디오 + 1080p 프리뷰", "120초 녹화 (열 모델 활성)", "해상도 저하와 통보 여부 확인"))
def _w_thermal(svc):
    p = svc.profile
    dev = svc.open("0")
    vw, vh = (3840, 2160) if p.mode_for(3840, 2160, 30) else _preview_size(svc)
    streams = [_preview(svc), Stream(1, vw, vh, PixelFormat.PRIVATE, VIDEO_USAGE)]
    if svc.configure(dev, streams, {**_fps(30), "thermal_model": True}) == Status.OK:
        svc.repeat(dev, (0, 1))
        svc.run(120_000)
    svc.close(dev)


# --------------------------------------------------------------------------- #
# HAL Interface
# --------------------------------------------------------------------------- #
@workload("usb_hotplug", "외장 USB 카메라 연결 → 사용 → 분리 → 재열기",
          ("외장 카메라(id 2) 연결", "열기·720p 프리뷰 1초·닫기", "USB 분리", "앱이 목록에 남은 카메라 열기 시도"))
def _w_hotplug(svc):
    svc.provider.usb_connect("2")
    svc.run(200)
    if svc.device_status.get("2") and svc.device_status["2"].name == "PRESENT":
        dev = svc.open("2")
        if dev and svc.configure(dev, [Stream(0, 1280, 720, PixelFormat.PRIVATE, PREVIEW_USAGE)], _fps(30)) == Status.OK:
            svc.repeat(dev, (0,))
            svc.run(1000)
        svc.close(dev)
    svc.provider.usb_disconnect("2")
    svc.run(500)
    if svc.device_status.get("2") and svc.device_status["2"].name == "PRESENT":
        svc.open("2")
    svc.run(200)


@workload("suspend_resume", "절전 복귀 후 카메라 재사용",
          ("프리뷰 1초 후 닫기", "30초 절전 후 복귀(서비스 재시작)", "카메라 다시 열기·프리뷰"))
def _w_suspend(svc):
    dev = _stream_preview(svc, seconds=1)
    svc.close(dev)
    svc.provider.suspend_resume(30)
    dev = _stream_preview(svc, seconds=1)
    svc.close(dev)


@workload("concurrent_open", "광고된 동시 사용 카메라 조합 열기",
          ("concurrent camera id 조합 조회", "조합별로 두 카메라 동시 열기·구성", "0.5초 스트리밍 후 닫기"))
def _w_concurrent(svc):
    combos = svc.provider.concurrent_camera_ids()
    svc.sim.emit("concurrent_combos", combos=[list(c) for c in combos])
    for a, b in combos:
        da = svc.open(a)
        db = svc.open(b)
        ok = all(d is not None and svc.configure(d, [_preview(svc)], _fps(30)) == Status.OK for d in (da, db))
        svc.sim.emit("concurrent_attempt", combo=[a, b], ok=ok)
        if ok:
            svc.repeat(da, (0,))
            svc.repeat(db, (0,))
            svc.run(500)
        svc.close(db)
        svc.close(da)
        svc.run(100)


@workload("torch_availability", "카메라 사용 후 손전등 가용성",
          ("카메라 0 열기 (토치 사용 불가 통보)", "프리뷰 0.5초 후 닫기", "토치 가용 통보 대기 1초"))
def _w_torch(svc):
    dev = _stream_preview(svc, seconds=0.5)
    svc.close(dev)
    svc.run(1000)


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #
@workload("extension_session", "확장(야간) 모드 세션 사용",
          ("카메라 열기", "extension 경로로 세션 구성", "1초 스트리밍", "사용 중 표시등 관찰"))
def _w_extension(svc):
    dev = _stream_preview(svc, seconds=1, params={"extension": "NIGHT"})
    svc.close(dev)


@workload("app_background", "촬영 중 앱 백그라운드 전환",
          ("프리뷰 2초", "홈 버튼 → 앱이 close()", "3초간 센서 동작 관찰"), max_close_to_stop_ms=200)
def _w_background(svc):
    dev = _stream_preview(svc, seconds=2)
    svc.close(dev)
    svc.run(3000)


@workload("diag_probe", "진단 포트 무인증 프레임 요청",
          ("카메라 스트리밍 중", "진단 인터페이스로 인증 없이 프레임 덤프 요청", "응답 확인"))
def _w_diag(svc):
    dev = _stream_preview(svc, seconds=1)
    svc.provider.diag_request("192.168.4.21", authenticated=False)
    svc.run(200)
    svc.close(dev)
