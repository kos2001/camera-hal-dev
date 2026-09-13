"""결함 카탈로그 — 참고 저장소의 고장 시나리오 37종을 주입 가능한 결함으로 매핑.

각 항목:
  defect    : HAL 모델 안의 코드 경로 분기 이름 (sim.defect)
  workload  : 그 결함을 드러내는 재현 절차
  platform  : 참고 시나리오의 family 에 속한 기본 플랫폼
  expect    : 결함 주입 시 검사기가 내야 하는 위반 코드 (sweep 검증 기준)

증상/근본원인/해결책 텍스트는 data/reference_scenarios.json (import_reference.py 산출).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "reference_scenarios.json"


@dataclass(frozen=True)
class FaultSpec:
    id: str
    defect: str
    workload: str
    platform: str
    expect: tuple[str, ...]
    # 참고 시나리오 텍스트
    category: str = ""
    family: str = ""
    severity: str = ""
    summary: str = ""
    symptom: str = ""
    root_cause: str = ""
    resolution: str = ""
    workaround: str = ""
    repro: tuple[str, ...] = ()
    log_excerpt: str = ""

    def title(self, platform: str | None = None) -> str:
        return self.summary.replace("{chip}", platform or self.platform)


_MAP: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    ("F00", "static_port_table", "three_stream_snapshot", "QCOM-CamX-Spectra", ("CONFIG_REJECTED_ADVERTISED",)),
    ("F01", "zsl_leak_on_cancel", "zsl_burst", "QCOM-CamX-Spectra", ("REQUEST_TIMEOUT", "BUFFER_LEAK")),
    ("F02", "physical_meta_dropped_on_switch", "logical_zoom_switch", "QCOM-CamX-Spectra", ("MISSING_PHYSICAL_METADATA",)),
    ("F03", "ts_from_pipe_completion", "video_60fps", "EXYNOS-CamHAL3", ("TIMESTAMP_NON_MONOTONIC",)),
    ("F04", "ae_no_latency_compensation", "low_light_preview", "EXYNOS-CamHAL3", ("AE_OSCILLATION",)),
    ("F05", "pipe_teardown_leak", "session_reconfigure_loop", "EXYNOS-CamHAL3", ("MEMORY_GROWTH", "PROVIDER_DIED")),
    ("F06", "provider_hotplug_cache_stale", "usb_hotplug", "MTK-Camera3HAL", ("DEVICE_STATUS_STALE",)),
    ("F07", "awb_capture_uses_ambient_gains", "flash_capture", "MTK-Camera3HAL", ("AWB_FLASH_MISMATCH",)),
    ("F08", "sepolicy_missing_video_device", "usb_hotplug", "MTK-Camera3HAL", ("EXTERNAL_CAMERA_NOT_ENUMERATED",)),
    ("F09", "isp_pad_single_path", "raw_plus_yuv", "RK-ISP3-HAL", ("CONFIG_REJECTED_ADVERTISED",)),
    ("F10", "aiq_tuning_not_resent", "aiq_restart_preview", "RK-ISP3-HAL", ("TUNING_RESET",)),
    ("F11", "sensor_power_delay_short", "cold_boot_open", "RK-ISP3-HAL", ("OPEN_FAILURE",)),
    ("F12", "isp_fw_loaded_per_open", "open_to_first_frame", "IPU6-CameraHAL", ("FIRST_FRAME_LATENCY",)),
    ("F13", "hal_adapter_shared_pool", "dual_client", "IPU6-CameraHAL", ("FRAME_CONTENT_CORRUPT",)),
    ("F14", "static_metadata_incomplete", "static_metadata_audit", "LIBCAM-AndroidHAL", ("STATIC_METADATA_MISSING",)),
    ("F15", "flush_returns_before_inflight_drained", "flush_inflight", "LIBCAM-AndroidHAL", ("RESULT_AFTER_FLUSH",)),
    ("F16", "softisp_scalar_debayer", "preview_30fps", "LIBCAM-AndroidHAL", ("FPS_BELOW_TARGET",)),
    ("F17", "ipc_stale_remote_after_resume", "suspend_resume", "CROS-CameraHAL", ("IPC_TIMEOUT",)),
    ("F18", "mjpeg_no_default_huffman", "usb_webcam_mjpeg", "CROS-CameraHAL", ("STREAM_STALL",)),
    ("F19", "v4l2_queue_depth_short", "high_load_preview", "V4L2-GenericHAL", ("FRAME_DROP_RATE",)),
    ("F20", "fps_range_union_advertised", "fps_range_config", "V4L2-GenericHAL", ("CONFIG_REJECTED_ADVERTISED",)),
    ("F21", "partial_results_unsequenced", "partial_results_order", "EMU-CameraHAL", ("PARTIAL_OUT_OF_ORDER",)),
    ("F22", "lock_order_inversion", "stream_stop_race", "EMU-CameraHAL", ("HAL_HANG", "PROVIDER_DIED")),
    ("F23", "evs_cold_start_on_gear", "gear_reverse", "AUTO-CamHAL-EVS", ("FIRST_FRAME_LATENCY",)),
    ("F24", "evs_free_running_channels", "surround_view", "AUTO-CamHAL-EVS", ("MULTICHANNEL_SKEW",)),
    ("F25", "privacy_notify_normal_path_only", "extension_session", "QCOM-CamX-Spectra", ("PRIVACY_INDICATOR_MISSING",)),
    ("F26", "close_async_ack", "app_background", "MTK-Camera3HAL", ("SENSOR_ACTIVE_AFTER_CLOSE",)),
    ("F27", "diag_path_in_production", "diag_probe", "AUTO-CamHAL-EVS", ("UNAUTHENTICATED_FRAME_ACCESS",)),
    ("F28", "orientation_read_at_completion", "rotate_during_capture", "EXYNOS-CamHAL3", ("RESULT_SETTING_MISMATCH",)),
    ("F29", "scene_mode_handlers_missing", "scene_mode_sweep", "LIBCAM-AndroidHAL", ("CONTROL_NOT_APPLIED",)),
    ("F30", "concurrent_combos_unchecked", "concurrent_open", "CROS-CameraHAL", ("CONCURRENT_OPEN_FAILED",)),
    ("F31", "torch_release_notify_missing", "torch_availability", "IPU6-CameraHAL", ("TORCH_CALLBACK_MISSING",)),
    ("F32", "ts_pipeline_ready_clock", "record_start_av", "RK-ISP3-HAL", ("AV_SYNC_DRIFT",)),
    ("F33", "reprocess_uses_session_params", "reprocess_capture", "QCOM-CamX-Spectra", ("RESULT_SETTING_MISMATCH",)),
    ("F34", "thermal_silent_downscale", "long_recording_thermal", "EXYNOS-CamHAL3", ("THERMAL_UNREPORTED",)),
    ("F35", "usage_flags_hal_only", "consumer_usage", "V4L2-GenericHAL", ("CONSUMER_LOCK_FAILED",)),
    ("F36", "mode_switch_stale_exposure", "sensor_mode_switch", "MTK-Camera3HAL", ("EXPOSURE_EXCEEDS_FRAME_DURATION",)),
)


@lru_cache(maxsize=1)
def all_faults() -> tuple[FaultSpec, ...]:
    ref = {}
    if DATA.exists():
        ref = {s["id"]: s for s in json.loads(DATA.read_text(encoding="utf-8"))["scenarios"]}
    out = []
    for fid, defect, wl, plat, expect in _MAP:
        r = ref.get(fid, {})
        out.append(FaultSpec(
            id=fid, defect=defect, workload=wl, platform=plat, expect=expect,
            category=r.get("category", ""), family=r.get("family", ""), severity=r.get("severity", ""),
            summary=r.get("summary", f"[{{chip}}] {defect}"), symptom=r.get("symptom", ""),
            root_cause=r.get("root_cause", ""), resolution=r.get("resolution", ""),
            workaround=r.get("workaround", ""), repro=tuple(r.get("repro", ())),
            log_excerpt=r.get("log_excerpt", ""),
        ))
    return tuple(out)


def get_fault(key: str) -> FaultSpec:
    k = key.strip()
    for f in all_faults():
        if k.upper() == f.id or k == f.defect:
            return f
    raise KeyError(f"알 수 없는 결함: {key} (list-faults 로 확인)")
