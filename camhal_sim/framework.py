"""CameraService(프레임워크) 모델 — HAL 을 구동하고 콜백을 관찰 사건으로 기록한다.

워크로드는 이 클래스의 API(open/configure/submit/repeat/run/flush/close)만 쓴다.
HAL 콜백은 on_* 로 들어와 `sim.emit` 으로 남고, 요청 타임아웃·스트림 정지 감시도
여기서 한다(실제 CameraService 의 watchdog 에 해당).
"""
from __future__ import annotations

from typing import Any

from .engine import Simulator
from .hal.device import CameraDevice
from .hal.provider import CameraProvider
from .model import MS, SEC, CaptureRequest, DeviceStatus, ErrorCode, Key, Status, Stream
from .platforms import PlatformProfile

REQUEST_TIMEOUT_NS = 5 * SEC
STALL_NS = 2 * SEC


class CameraService:
    def __init__(self, sim: Simulator, profile: PlatformProfile):
        self.sim = sim
        self.profile = profile
        self.provider = CameraProvider(sim, profile, self)
        self.device_status: dict[str, DeviceStatus] = {}
        self._outstanding: dict[int, dict[int, int]] = {}     # id(dev) -> frame -> t_submit
        self._timed_out: set[tuple[int, int]] = set()
        self._open_t: dict[int, int] = {}
        self._first_frame_done: set[int] = set()
        self._last_result_t: dict[int, int] = {}
        self._stall_reported: set[int] = set()
        self._flush_t: dict[int, tuple[int, int]] = {}
        self.trigger_t: int | None = None
        self.terminated: set[int] = set()
        self.provider.initialize()

    # ================================================================== #
    # 워크로드용 API
    # ================================================================== #
    def open(self, camera_id: str, client: str = "app") -> CameraDevice | None:
        t0 = self.sim.now
        status, dev = self.provider.open(camera_id, client)
        self.sim.emit("open_result", camera=camera_id, client=client, status=status.name,
                      latency_ms=round((self.sim.now - t0) / MS, 1),
                      listed=self.device_status.get(camera_id) == DeviceStatus.PRESENT)
        if dev is not None:
            self._open_t[id(dev)] = t0
            self._outstanding[id(dev)] = {}
            self.terminated.discard(id(dev))
        return dev

    def static_metadata(self, dev: CameraDevice) -> dict:
        md = dev.static_metadata()
        self.sim.emit("static_metadata", camera=dev.id, keys=sorted(md))
        return md

    def configure(self, dev: CameraDevice | None, streams: list[Stream], params: dict | None = None) -> Status:
        if dev is None:
            return Status.NO_DEVICE
        params = params or {}
        fps = int(params.get(Key.AE_TARGET_FPS_RANGE, (30, 30))[1])
        supported = dev.is_stream_combination_supported(streams, fps)
        self.sim.emit("config_query", camera=dev.id, streams=[s.label() for s in streams], fps=fps,
                      supported=supported)
        status = dev.configure_streams(streams, params)
        self.sim.emit("configure", camera=dev.id, streams=[s.label() for s in streams], fps=fps,
                      status=status.name)
        if status != Status.OK:
            self.sim.log("CameraService", f"configureStreams: {int(status)} ({status.name})", "E")
        return status

    def submit(self, dev: CameraDevice | None, stream_ids: tuple[int, ...], settings: dict | None = None,
               **flags: Any) -> int | None:
        if dev is None or dev.session is None or id(dev) in self.terminated:
            return None
        self._mark_activity(dev)
        req = CaptureRequest(dev.next_frame_number(), tuple(stream_ids), dict(settings or {}), **flags)
        self._record_submit(dev, req, repeating=False)
        dev.process_capture_request(req)
        return req.frame_number

    def repeat(self, dev: CameraDevice | None, stream_ids: tuple[int, ...], settings: dict | None = None) -> None:
        if dev is None or dev.session is None or id(dev) in self.terminated:
            return
        self._mark_activity(dev)
        dev.set_repeating(CaptureRequest(-1, tuple(stream_ids), dict(settings or {})))

    def stop_repeating(self, dev: CameraDevice | None) -> None:
        if dev is not None and dev.session:
            dev.set_repeating(None)

    def run(self, ms: float) -> None:
        end = self.sim.now + int(ms * MS)
        step = 100 * MS
        while self.sim.now < end:
            self.sim.run_until(min(end, self.sim.now + step))
            self._watchdog()

    def flush(self, dev: CameraDevice | None) -> Status:
        if dev is None or dev.session is None:
            return Status.NO_DEVICE
        last = max(self._outstanding.get(id(dev), {}) or [-1])
        status = dev.flush()
        self.sim.emit("flush_returned", camera=dev.id, last_submitted=last,
                      inflight_left=len(dev.session.inflight) if dev.session else 0)
        self._flush_t[id(dev)] = (self.sim.now, last)
        return status

    def close(self, dev: CameraDevice | None) -> Status:
        if dev is None:
            return Status.NO_DEVICE
        if id(dev) in self.terminated and dev.session is None:
            return Status.OK
        t0 = self.sim.now
        status = dev.close()
        if status != Status.OK:
            self.sim.emit("close_returned", camera=dev.id, status=status.name,
                          latency_ms=round((self.sim.now - t0) / MS, 1))
        return status

    def mark_trigger(self, what: str) -> None:
        self.trigger_t = self.sim.now
        self.sim.emit("trigger", what=what)

    # ================================================================== #
    # 감시 (요청 타임아웃, 스트림 정지)
    # ================================================================== #
    def _watchdog(self) -> None:
        now = self.sim.now
        for dev in self._all_devices():
            key = id(dev)
            if key in self.terminated:
                continue
            out = self._outstanding.get(key, {})
            s = dev.session
            streaming = s is not None and s.active and (s.repeating is not None or out)
            last = self._last_result_t.get(key)
            if streaming and last is not None and now - last > STALL_NS and key not in self._stall_reported:
                self._stall_reported.add(key)
                self.sim.log("CameraService", f"Camera {dev.id}: stream stalled, 0 frames in "
                                              f"{(now - last) // MS}ms", "W")
                self.sim.emit("stream_stall", camera=dev.id, idle_ms=(now - last) // MS)
            if out:
                oldest_frame, t_sub = min(out.items(), key=lambda kv: kv[1])
                if now - t_sub > REQUEST_TIMEOUT_NS and (key, oldest_frame) not in self._timed_out:
                    self._timed_out.add((key, oldest_frame))
                    self.sim.log("CameraService", f"Camera {dev.id}: Request timed out, flushing", "E")
                    self.sim.emit("request_timeout", camera=dev.id, frame=oldest_frame,
                                  waited_ms=(now - t_sub) // MS)
                    self.terminated.add(key)
                    if dev.session:
                        dev.flush()
                        dev._stop_session("request timeout")
                    out.clear()

    def _mark_activity(self, dev: CameraDevice) -> None:
        """스트림 요청을 시작한 시점을 정지 감시 기준으로 삼는다(아직 결과가 없을 때)."""
        key = id(dev)
        if key not in self._last_result_t or not (dev.session and dev.session.active):
            self._last_result_t[key] = self.sim.now
            self._stall_reported.discard(key)

    def _all_devices(self) -> list[CameraDevice]:
        devs = list(self.provider.devices.values())
        devs += [d for lst in self.provider.clients.values() for d in lst if d not in devs]
        return devs

    def _record_submit(self, dev: CameraDevice, req: CaptureRequest, repeating: bool) -> None:
        self._outstanding.setdefault(id(dev), {})[req.frame_number] = self.sim.now
        if not repeating:
            self.sim.emit("request_submitted", camera=dev.id, frame=req.frame_number,
                          settings={k: v for k, v in req.settings.items()},
                          zsl=req.zsl, reprocess=req.reprocess)

    def _complete(self, dev: CameraDevice, frame: int) -> None:
        self._outstanding.get(id(dev), {}).pop(frame, None)
        self._last_result_t[id(dev)] = self.sim.now
        self._stall_reported.discard(id(dev))

    # ================================================================== #
    # HAL 콜백
    # ================================================================== #
    def on_device_status(self, camera_id: str, status: DeviceStatus) -> None:
        self.device_status[camera_id] = status
        self.sim.emit("device_status", camera=camera_id, status=status.name)

    def on_torch_status(self, camera_id: str, available: bool) -> None:
        self.sim.emit("torch_status", camera=camera_id, available=available)

    def on_hw_usb(self, camera_id: str, connected: bool) -> None:
        self.sim.emit("hw_usb", camera=camera_id, connected=connected)

    def on_sensor_streaming(self, dev: CameraDevice, on: bool) -> None:
        self.sim.emit("sensor_streaming", camera=dev.id, channel=dev.channel, on=on)

    def on_camera_in_use(self, dev: CameraDevice, on: bool) -> None:
        self.sim.emit("camera_in_use", camera=dev.id, on=on)

    def on_repeating_frame(self, dev: CameraDevice, req: CaptureRequest) -> None:
        self._record_submit(dev, req, repeating=True)

    def on_repeating_dropped(self, dev: CameraDevice, req: CaptureRequest) -> None:
        self._outstanding.get(id(dev), {}).pop(req.frame_number, None)

    def on_frame_drop(self, dev: CameraDevice, reason: str) -> None:
        self.sim.emit("frame_drop", camera=dev.id, reason=reason)

    def on_shutter(self, dev: CameraDevice, frame: int, ts: int) -> None:
        self.sim.emit("shutter", camera=dev.id, channel=dev.channel, frame=frame, timestamp=ts,
                      boottime_now=self.sim.now + self.provider.boottime_offset_ns)

    def on_partial_result(self, dev: CameraDevice, frame: int, index: int, total: int) -> None:
        self.sim.emit("partial_result", camera=dev.id, frame=frame, partial=index, total=total)
        self._check_after_flush(dev, frame, "partial")

    def on_result(self, dev: CameraDevice, frame: int, meta: dict, stats: dict, buffers: list,
                  physical: dict | None) -> None:
        total = dev.profile.partial_results
        self.sim.emit("partial_result", camera=dev.id, frame=frame, partial=total, total=total)
        data = dict(camera=dev.id, channel=dev.channel, client=dev.client, frame=frame, meta=meta,
                    stats=stats, buffers=buffers)
        if physical is not None:
            data["physical"] = physical
        self.sim.emit("result", **data)
        self._check_after_flush(dev, frame, "result")
        key = id(dev)
        if key not in self._first_frame_done:
            self._first_frame_done.add(key)
            since_open = (self.sim.now - self._open_t.get(key, 0)) / MS
            ev = dict(camera=dev.id, since_open_ms=round(since_open, 1))
            if self.trigger_t is not None:
                ev["since_trigger_ms"] = round((self.sim.now - self.trigger_t) / MS, 1)
            self.sim.log(dev.tag, f"first frame at {since_open:.0f}ms after open")
            self.sim.emit("first_frame", **ev)
        self._complete(dev, frame)

    def _check_after_flush(self, dev: CameraDevice, frame: int, what: str) -> None:
        ft = self._flush_t.get(id(dev))
        if ft and frame <= ft[1]:
            self.sim.log("CameraService", f"Unexpected {what} for canceled request {frame}", "E")

    def on_notify_error(self, dev: CameraDevice, frame: int, code: ErrorCode) -> None:
        self.sim.emit("notify_error", camera=dev.id, frame=frame, code=code.name)
        if code in (ErrorCode.ERROR_REQUEST, ErrorCode.ERROR_RESULT):
            self._complete(dev, frame)

    def on_close_returned(self, dev: CameraDevice, status: Status) -> None:
        self.sim.emit("close_returned", camera=dev.id, status=status.name)

    def on_session_closed(self, dev: CameraDevice, leaked: int, reason: str) -> None:
        self.sim.emit("session_closed", camera=dev.id, leaked_buffers=leaked, reason=reason)
        self._first_frame_done.discard(id(dev))

    def on_provider_memory(self, rss_mb: float, pipes: int, sessions: int) -> None:
        self.sim.emit("provider_memory", rss_mb=round(rss_mb, 1), pipes_alive=pipes, sessions=sessions)

    def on_provider_died(self, reason: str) -> None:
        self.sim.emit("provider_died", reason=reason)

    def on_ipc(self, method: str, status: Status, latency_ms: float) -> None:
        self.sim.emit("ipc_call", method=method, status=status.name, latency_ms=latency_ms)

    def on_diag(self, peer: str, served: bool, authenticated: bool) -> None:
        self.sim.emit("diag_access", peer=peer, served=served, authenticated=authenticated)
