"""계약 검사기 — 관찰된 사건 목록만으로 HAL 계약 위반/품질 결함을 판정한다.

검사기는 어떤 결함이 주입됐는지 모른다. CTS/ITS·CameraService 로그·앱 계측이
볼 수 있는 정보(콜백, 결과 메타데이터, 프레임 통계, 커널/USB 사건)만 쓴다.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from .model import MS, REQUIRED_STATIC_KEYS, Event, Key


@dataclass
class Violation:
    code: str
    category: str
    severity: str
    title: str
    detail: str
    count: int = 1
    evidence: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"code": self.code, "category": self.category, "severity": self.severity,
                "title": self.title, "detail": self.detail, "count": self.count,
                "evidence": self.evidence}


@dataclass(frozen=True)
class Checker:
    code: str
    category: str
    severity: str
    title: str
    fn: Callable[[list[Event], dict], tuple[int, str, list[Event]] | None]


CHECKERS: list[Checker] = []


def checker(code: str, category: str, severity: str, title: str):
    def deco(fn):
        CHECKERS.append(Checker(code, category, severity, title, fn))
        return fn
    return deco


def run_checkers(events: list[Event], budgets: dict | None = None) -> list[Violation]:
    budgets = budgets or {}
    out = []
    for c in CHECKERS:
        found = c.fn(events, budgets)
        if not found:
            continue
        count, detail, evid = found
        if count <= 0:
            continue
        out.append(Violation(c.code, c.category, c.severity, c.title, detail, count,
                             [e.to_dict() for e in evid[:5]]))
    order = {"Blocker": 0, "Critical": 1, "Major": 2, "Minor": 3}
    out.sort(key=lambda v: (order.get(v.severity, 9), v.code))
    return out


def _of(events: list[Event], *kinds: str) -> list[Event]:
    return [e for e in events if e.kind in kinds]


def _hit(evid: list[Event], detail: str):
    return (len(evid), detail, evid) if evid else None


def _dev_key(e: Event) -> tuple:
    return (e.data.get("camera"), e.data.get("client"), e.data.get("channel"))


# =========================================================================== #
# Stream Config
# =========================================================================== #
@checker("CONFIG_REJECTED_ADVERTISED", "Stream Config", "Critical",
         "광고된(지원한다고 답한) 스트림 조합의 configureStreams 실패")
def _c_config(events, b):
    evid, last_query = [], {}
    for e in _of(events, "config_query", "configure"):
        key = (e.data["camera"], tuple(e.data["streams"]), e.data["fps"])
        if e.kind == "config_query":
            last_query[key] = e.data["supported"]
        elif e.data["status"] != "OK" and last_query.get(key):
            evid.append(e)
    return _hit(evid, "isStreamCombinationSupported=true 였으나 configureStreams 가 "
                      f"{evid[0].data['status'] if evid else ''} 반환")


@checker("CONSUMER_LOCK_FAILED", "Stream Config", "Major",
         "할당 버퍼 usage 가 소비자 요청 비트를 포함하지 않음")
def _c_usage(events, b):
    evid = [e for e in _of(events, "result") if any(x["consumer_lock"] == "failed" for x in e.data["buffers"])]
    if not evid:
        return None
    buf = next(x for x in evid[0].data["buffers"] if x["consumer_lock"] == "failed")
    return _hit(evid, f"요청 usage 0x{buf['requested_usage']:x} ⊄ 할당 usage 0x{buf['usage']:x} → 검은 화면")


# =========================================================================== #
# Buffer Management
# =========================================================================== #
@checker("BUFFER_LEAK", "Buffer Management", "Critical", "세션 종료 시 반환되지 않은 버퍼")
def _c_leak(events, b):
    evid = [e for e in _of(events, "session_closed") if e.data["leaked_buffers"] > 0]
    return _hit(evid, f"미반환 버퍼 {sum(e.data['leaked_buffers'] for e in evid)}개")


@checker("FRAME_CONTENT_CORRUPT", "Buffer Management", "Critical", "유효 페이로드 없는(제로 필) 프레임 전달")
def _c_corrupt(events, b):
    evid = [e for e in _of(events, "result") if e.data["stats"]["checksum"] == 0]
    clients = sorted({str(e.data.get("client")) for e in evid})
    return _hit(evid, f"checksum=0 프레임 {len(evid)}장 (client {', '.join(clients)})")


@checker("FRAME_DROP_RATE", "Buffer Management", "Major", "프레임 유실률 기준 초과")
def _c_drops(events, b):
    limit = b.get("max_drop_ratio")
    if limit is None:
        return None
    drops = _of(events, "frame_drop")
    results = _of(events, "result")
    total = len(drops) + len(results)
    if not total or len(drops) / total <= limit:
        return None
    ratio = len(drops) / total
    reasons = sorted({e.data["reason"] for e in drops})
    return (len(drops), f"유실 {len(drops)}/{total} ({ratio:.1%}, 한계 {limit:.0%}); 사유: {', '.join(reasons)}",
            drops)


# =========================================================================== #
# Request Pipeline
# =========================================================================== #
@checker("REQUEST_TIMEOUT", "Request Pipeline", "Critical", "요청 결과가 제한 시간 내 도착하지 않음")
def _c_timeout(events, b):
    evid = _of(events, "request_timeout")
    return _hit(evid, f"frame {evid[0].data['frame']} 대기 {evid[0].data['waited_ms']}ms 후 세션 강제 종료"
                if evid else "")


@checker("RESULT_AFTER_FLUSH", "Request Pipeline", "Major", "flush 반환 이후 취소된 요청의 결과 도착")
def _c_after_flush(events, b):
    evid, flushes = [], []
    for e in events:
        if e.kind == "flush_returned":
            flushes.append((e.t_ns, e.data["camera"], e.data["last_submitted"]))
        elif e.kind in ("partial_result", "result"):
            for t, cam, last in flushes:
                if e.data["camera"] == cam and e.t_ns > t and e.data["frame"] <= last:
                    evid.append(e)
                    break
    frames = sorted({e.data["frame"] for e in evid})
    return _hit(evid, f"flush 후 도착한 콜백 {len(evid)}건 (frame {frames[:6]})")


@checker("PARTIAL_OUT_OF_ORDER", "Request Pipeline", "Major", "partial result 순서 역전")
def _c_partials(events, b):
    seen: dict[tuple, int] = {}
    evid = []
    for e in _of(events, "partial_result"):
        k = (e.data["camera"], e.data["frame"])
        if e.data["partial"] < seen.get(k, 0):
            evid.append(e)
        seen[k] = max(seen.get(k, 0), e.data["partial"])
    return _hit(evid, f"역전 {len(evid)}건 (예: frame {evid[0].data['frame']} partial "
                      f"{evid[0].data['partial']}/{evid[0].data['total']} 이 늦게 도착)" if evid else "")


@checker("HAL_HANG", "Request Pipeline", "Blocker", "HAL 호출이 반환되지 않음(교착/정지)")
def _c_hang(events, b):
    evid = [e for e in _of(events, "close_returned") if e.data["status"] == "TIMED_OUT"]
    return _hit(evid, f"close() {evid[0].data.get('latency_ms', '?')}ms 무응답" if evid else "")


# =========================================================================== #
# Metadata
# =========================================================================== #
@checker("STATIC_METADATA_MISSING", "Metadata", "Blocker", "CTS 필수 정적 메타데이터 누락")
def _c_static(events, b):
    evid, missing = [], set()
    for e in _of(events, "static_metadata"):
        miss = [k for k in REQUIRED_STATIC_KEYS if k not in e.data["keys"]]
        if miss:
            evid.append(e)
            missing.update(miss)
    return (len(missing), f"누락 {len(missing)}개: {', '.join(sorted(missing)[:4])} …", evid) if evid else None


@checker("MISSING_PHYSICAL_METADATA", "Metadata", "Major", "논리 카메라 결과에 physical camera metadata 없음")
def _c_physical(events, b):
    evid = [e for e in _of(events, "result") if "physical" in e.data and not e.data["physical"]]
    return _hit(evid, f"빈 physical result {len(evid)}건 (앱 getPhysicalCameraResults NPE 위험)")


@checker("RESULT_SETTING_MISMATCH", "Metadata", "Major", "결과 메타데이터가 요청 설정과 다름")
def _c_setting(events, b):
    submitted: dict[tuple, dict] = {}
    evid, keys = [], set()
    for e in events:
        if e.kind == "request_submitted":
            submitted[(e.data["camera"], e.data["frame"])] = e.data["settings"]
        elif e.kind == "result":
            req = submitted.get((e.data["camera"], e.data["frame"]))
            if not req:
                continue
            for k in (Key.JPEG_ORIENTATION, Key.NOISE_REDUCTION_MODE):
                if k in req and e.data["meta"].get(k) != req[k]:
                    evid.append(e)
                    keys.add(f"{k.split('.')[-1]}: 요청 {req[k]} → 결과 {e.data['meta'].get(k)}")
    return _hit(evid, "; ".join(sorted(keys)))


@checker("CONTROL_NOT_APPLIED", "Metadata", "Major", "광고된 제어 값이 결과에만 반영되고 실제 효과 없음")
def _c_control(events, b):
    evid = [e for e in _of(events, "result")
            if e.data["meta"].get(Key.SCENE_MODE, "DISABLED") != "DISABLED" and not e.data["stats"]["effects"]]
    modes = sorted({e.data["meta"][Key.SCENE_MODE] for e in evid})
    return _hit(evid, f"sceneMode {modes} 가 echo 되었으나 영상 특성 변화 없음")


# =========================================================================== #
# 3A Tuning
# =========================================================================== #
@checker("AE_OSCILLATION", "3A Tuning", "Major", "AE 가 수렴하지 않고 밝기가 진동")
def _c_ae(events, b):
    series: dict[tuple, list[Event]] = defaultdict(list)
    for e in _of(events, "result"):
        series[_dev_key(e)].append(e)
    for evs in series.values():
        tail = evs[30:][-90:]
        if len(tail) < 30:
            continue
        luma = [e.data["stats"]["luma"] for e in tail]
        reversals, direction = 0, 0
        for a, c in zip(luma, luma[1:]):
            if abs(c - a) < 0.15 * max(a, 1e-3):
                continue
            d = 1 if c > a else -1
            if direction and d != direction:
                reversals += 1
            direction = d
        converged = sum(e.data["meta"][Key.AE_STATE] == "CONVERGED" for e in tail) / len(tail)
        if reversals >= 6 and converged < 0.5:
            return (reversals, f"최근 {len(tail)}프레임에서 휘도 반전 {reversals}회, "
                               f"휘도 {min(luma):.3f}~{max(luma):.3f}, CONVERGED 비율 {converged:.0%}", tail[:5])
    return None


@checker("AWB_FLASH_MISMATCH", "3A Tuning", "Major", "플래시 발광 프레임의 화이트밸런스 오류")
def _c_awb(events, b):
    evid = [e for e in _of(events, "result")
            if e.data["meta"].get(Key.FLASH_STATE) == "FIRED" and e.data["stats"]["wb_error_k"] > 800]
    return _hit(evid, f"flashState=FIRED 프레임 색온도 오차 {max((e.data['stats']['wb_error_k'] for e in evid), default=0)}K")


@checker("TUNING_RESET", "3A Tuning", "Major", "스트리밍 중 튜닝 파라미터가 기본값으로 바뀜")
def _c_tuning(events, b):
    last: dict[tuple, int] = {}
    evid = []
    for e in _of(events, "result"):
        k = _dev_key(e)
        tid = e.data["meta"].get(Key.TUNING_ID)
        if last.get(k) and tid == 0:
            evid.append(e)
        if tid:
            last[k] = tid
    return _hit(evid, f"tuningId 0x3C21 → 0 인 결과 {len(evid)}건")


# =========================================================================== #
# Sensor Driver
# =========================================================================== #
@checker("OPEN_FAILURE", "Sensor Driver", "Major", "열거된 카메라 열기 실패")
def _c_open(events, b):
    opens = [e for e in _of(events, "open_result") if e.data["listed"]]
    evid = [e for e in opens if e.data["status"] == "NO_DEVICE"]
    return _hit(evid, f"{len(evid)}/{len(opens)}회 실패 (NO_DEVICE)")


@checker("STREAM_STALL", "Sensor Driver", "Major", "스트리밍 중 프레임 공급 중단")
def _c_stall(events, b):
    evid = _of(events, "stream_stall")
    drops = sorted({e.data["reason"] for e in _of(events, "frame_drop")})
    return _hit(evid, f"{evid[0].data['idle_ms']}ms 동안 결과 0장; 드롭 사유: {', '.join(drops) or '-'}"
                if evid else "")


@checker("EXPOSURE_EXCEEDS_FRAME_DURATION", "Sensor Driver", "Minor", "노출 시간이 프레임 길이를 초과")
def _c_exposure(events, b):
    evid = [e for e in _of(events, "result")
            if e.data["meta"][Key.SENSOR_EXPOSURE_TIME] > e.data["meta"][Key.SENSOR_FRAME_DURATION]]
    return _hit(evid, f"exposure {evid[0].data['meta'][Key.SENSOR_EXPOSURE_TIME] / MS:.1f}ms > frameDuration "
                      f"{evid[0].data['meta'][Key.SENSOR_FRAME_DURATION] / MS:.1f}ms" if evid else "")


# =========================================================================== #
# Sync Timing
# =========================================================================== #
@checker("TIMESTAMP_NON_MONOTONIC", "Sync Timing", "Critical", "센서 타임스탬프 역전")
def _c_ts(events, b):
    last: dict[tuple, int] = {}
    evid = []
    for e in sorted(_of(events, "result"), key=lambda x: (_dev_key(x), x.data["frame"])):
        k = _dev_key(e)
        ts = e.data["meta"][Key.SENSOR_TIMESTAMP]
        if k in last and ts <= last[k]:
            evid.append(e)
        last[k] = max(ts, last.get(k, ts))
    return _hit(evid, f"역전 {len(evid)}회 (MediaCodec 입력 드롭 유발)")


@checker("MULTICHANNEL_SKEW", "Sync Timing", "Critical", "멀티채널 프레임 동기 편차 초과")
def _c_skew(events, b):
    by_ch: dict[int, list[int]] = defaultdict(list)
    for e in _of(events, "shutter"):
        by_ch[e.data.get("channel", 0)].append(e.data["timestamp"])
    if len(by_ch) < 2:
        return None
    limit = b.get("max_skew_ms", 2.0) * MS
    ref = sorted(by_ch[min(by_ch)])[5:]
    diffs = sorted(b_ - a_ for a_, b_ in zip(ref, ref[1:]))
    period = diffs[len(diffs) // 2] if diffs else 33 * MS      # 중앙값 — 세션 간 공백 제외
    worst, evid = 0.0, []
    for ch, ts_list in by_ch.items():
        if ch == min(by_ch):
            continue
        for t in sorted(ts_list)[5:]:
            d = (t - ref[0]) % period
            d = min(d, period - d)
            worst = max(worst, d)
    if worst <= limit:
        return None
    evid = [e for e in _of(events, "shutter")][:8]
    return (1, f"채널 간 최대 편차 {worst / MS:.1f}ms (한계 {limit / MS:.1f}ms)", evid)


@checker("AV_SYNC_DRIFT", "Sync Timing", "Major", "프레임 타임스탬프가 기준 시계(boottime)와 어긋남")
def _c_av(events, b):
    limit = b.get("max_av_drift_ms", 40) * MS
    evid = [e for e in _of(events, "shutter") if abs(e.data["boottime_now"] - e.data["timestamp"] - 1 * MS) > limit]
    worst = max((abs(e.data["boottime_now"] - e.data["timestamp"]) for e in evid), default=0)
    return _hit(evid, f"{len(evid)}프레임, 최대 {worst / MS:.0f}ms 어긋남 (녹화 초기 a/v 드리프트)")


# =========================================================================== #
# Performance
# =========================================================================== #
@checker("FIRST_FRAME_LATENCY", "Performance", "Critical", "첫 프레임 지연 요구 초과")
def _c_first(events, b):
    limit = b.get("first_frame_ms")
    if limit is None:
        return None
    evid = [e for e in _of(events, "first_frame")
            if e.data.get("since_trigger_ms", e.data["since_open_ms"]) > limit]
    worst = max((e.data.get("since_trigger_ms", e.data["since_open_ms"]) for e in evid), default=0)
    return _hit(evid, f"최대 {worst:.0f}ms (요구 {limit}ms)")


@checker("FPS_BELOW_TARGET", "Performance", "Major", "스트림 처리량이 목표 fps 미달")
def _c_fps(events, b):
    target = b.get("min_fps")
    if target is None:
        return None
    series: dict[tuple, list[int]] = defaultdict(list)
    for e in _of(events, "result"):
        series[_dev_key(e)].append(e.t_ns)
    for evs in series.values():
        if len(evs) < 30:
            continue
        fps = (len(evs) - 1) / ((evs[-1] - evs[0]) / 1e9)
        if fps < target:
            drops = _of(events, "frame_drop")
            return (1, f"측정 {fps:.1f}fps < 목표 {target:g}fps (드롭 {len(drops)}건)", drops[:5])
    return None


@checker("MEMORY_GROWTH", "Performance", "Critical", "세션 반복 시 provider 메모리 단조 증가")
def _c_mem(events, b):
    idle = [e for e in _of(events, "provider_memory") if e.data["sessions"] == 0]
    if len(idle) < 20:
        return None
    grow = idle[-1].data["rss_mb"] - idle[0].data["rss_mb"]
    if grow < 200:
        return None
    return (1, f"유휴 RSS {idle[0].data['rss_mb']:.0f}MB → {idle[-1].data['rss_mb']:.0f}MB "
               f"({len(idle)}회), pipes alive={idle[-1].data['pipes_alive']}", [idle[0], idle[-1]])


@checker("THERMAL_UNREPORTED", "Performance", "Major", "열 제한으로 화질 저하가 통보 없이 발생")
def _c_thermal(events, b):
    evid = [e for e in _of(events, "result")
            if e.data["stats"]["effective_width_scale"] < 1.0 and Key.THERMAL_THROTTLE not in e.data["meta"]]
    return _hit(evid, f"실효 해상도 저하 프레임 {len(evid)}장, 메타데이터/콜백 통보 없음")


@checker("PROVIDER_DIED", "Performance", "Blocker", "camera provider 프로세스 종료")
def _c_died(events, b):
    evid = _of(events, "provider_died")
    return _hit(evid, f"사유: {evid[0].data['reason']}" if evid else "")


# =========================================================================== #
# HAL Interface
# =========================================================================== #
@checker("DEVICE_STATUS_STALE", "HAL Interface", "Major", "분리된 장치가 목록에 남아 열기 타임아웃")
def _c_stale(events, b):
    hw: dict[str, bool] = {}
    evid = []
    for e in events:
        if e.kind == "hw_usb":
            hw[e.data["camera"]] = e.data["connected"]
        elif e.kind == "open_result" and hw.get(e.data["camera"]) is False and e.data["listed"] \
                and e.data["status"] != "OK":
            evid.append(e)
    return _hit(evid, f"USB 분리 후 NOT_PRESENT 통보 없음 → open({evid[0].data['camera']}) "
                      f"{evid[0].data['status']} {evid[0].data['latency_ms']}ms" if evid else "")


@checker("EXTERNAL_CAMERA_NOT_ENUMERATED", "HAL Interface", "Major", "연결된 외장 카메라가 열거되지 않음")
def _c_enum(events, b):
    evid = []
    pending: dict[str, Event] = {}
    for e in events:
        if e.kind == "hw_usb":
            if e.data["connected"]:
                pending[e.data["camera"]] = e
            else:
                ev = pending.pop(e.data["camera"], None)
                if ev:
                    evid.append(ev)
        elif e.kind == "device_status" and e.data["status"] == "PRESENT":
            pending.pop(e.data["camera"], None)
    evid += list(pending.values())
    return _hit(evid, f"USB 연결된 카메라 {[e.data['camera'] for e in evid]} 에 PRESENT 통보 없음")


@checker("IPC_TIMEOUT", "HAL Interface", "Critical", "HAL IPC 호출 무응답")
def _c_ipc(events, b):
    evid = [e for e in _of(events, "ipc_call") if e.data["status"] == "TIMED_OUT"]
    return _hit(evid, f"{evid[0].data['method']} {evid[0].data['latency_ms']}ms 무응답" if evid else "")


@checker("CONCURRENT_OPEN_FAILED", "HAL Interface", "Major", "광고된 동시 사용 조합 열기 실패")
def _c_concurrent(events, b):
    evid = [e for e in _of(events, "concurrent_attempt") if not e.data["ok"]]
    return _hit(evid, f"실패 조합 {[tuple(e.data['combo']) for e in evid]}")


@checker("TORCH_CALLBACK_MISSING", "HAL Interface", "Major", "카메라 해제 후 토치 가용 통보 없음")
def _c_torch(events, b):
    unavailable: dict[str, bool] = {}
    closed_at: dict[str, Event] = {}
    for e in events:
        if e.kind == "torch_status":
            unavailable[e.data["camera"]] = not e.data["available"]
            if e.data["available"]:
                closed_at.pop(e.data["camera"], None)
        elif e.kind == "close_returned" and unavailable.get(e.data["camera"]):
            closed_at[e.data["camera"]] = e
    evid = list(closed_at.values())
    return _hit(evid, f"camera {[e.data['camera'] for e in evid]} close 후 torch available=true 미수신")


# =========================================================================== #
# Security
# =========================================================================== #
@checker("PRIVACY_INDICATOR_MISSING", "Security", "Blocker", "카메라 사용 중 알림 없이 센서 스트리밍")
def _c_privacy(events, b):
    in_use: dict[str, bool] = {}
    evid = []
    for e in events:
        if e.kind == "camera_in_use":
            in_use[e.data["camera"]] = e.data["on"]
        elif e.kind == "sensor_streaming" and e.data["on"] and not in_use.get(e.data["camera"]):
            evid.append(e)
    return _hit(evid, "in-use 통보 없이 스트리밍 시작 → 프라이버시 인디케이터 미표시")


@checker("SENSOR_ACTIVE_AFTER_CLOSE", "Security", "Critical", "close() 성공 반환 후에도 센서 동작")
def _c_after_close(events, b):
    limit = b.get("max_close_to_stop_ms", 200) * MS
    closed: dict[str, int] = {}
    evid, worst = [], 0
    for e in events:
        if e.kind == "close_returned" and e.data["status"] == "OK":
            closed[e.data["camera"]] = e.t_ns
        elif e.kind == "sensor_streaming" and e.data["on"]:
            closed.pop(e.data["camera"], None)          # 새 세션 시작 — 이전 close 와 짝짓지 않는다
        elif e.kind == "sensor_streaming" and not e.data["on"] and e.data["camera"] in closed:
            lag = e.t_ns - closed.pop(e.data["camera"])
            if lag > limit:
                evid.append(e)
                worst = max(worst, lag)
    return _hit(evid, f"close 반환 후 {worst / MS:.0f}ms 동안 센서 스트리밍 유지")


@checker("UNAUTHENTICATED_FRAME_ACCESS", "Security", "Blocker", "인증 없는 원본 프레임 접근 허용")
def _c_diag(events, b):
    evid = [e for e in _of(events, "diag_access") if e.data["served"] and not e.data["authenticated"]]
    return _hit(evid, f"진단 인터페이스가 {evid[0].data['peer']} 에 프레임 제공" if evid else "")
