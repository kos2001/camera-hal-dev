"""CameraDevice + 캡처 세션 — HAL3 요청 파이프라인의 동작 모델.

흐름 (camera3 / ICameraDeviceSession):
  configureStreams → processCaptureRequest(큐) → [센서 SOF 마다]
    요청 선택 → 버퍼 확보 → 노출 래치/타임스탬프 → notify(SHUTTER)
    → ISP 처리 지연 → partial result(1..N) → 최종 결과(+버퍼 반환)
  flush → 진행 중 요청을 ERROR_REQUEST 로 반환
  close → 스트림 정지, 버퍼 회수, 자원 해제

결함 스위치(sim.defect)는 실제 코드 경로 분기로만 동작한다. 프레임워크 쪽
검사기는 결함 이름을 모르고 관찰된 사건만으로 계약 위반을 판정한다.
"""
from __future__ import annotations

import zlib
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..engine import Simulator, _Scheduled
from ..model import (MS, SEC, REQUIRED_STATIC_KEYS, CaptureRequest, ErrorCode, Key,
                     PixelFormat, Status, Stream, Usage)
from ..platforms import PlatformProfile, SensorMode
from .components import (AeController, BufferPool, LockOp, Sensor, ThermalModel, cct_to_gains,
                         detect_deadlock, scene_luma)

if TYPE_CHECKING:
    from .provider import CameraProvider

HI_RES_MP = 64.0          # 이 이상의 BLOB 은 IPE 추가 포트를 쓴다(RDI 경유)
ZSL_RING = 3              # ZSL 선택 후보 프레임 수
SEC_PER_PARTIAL_NS = 2 * MS


@dataclass
class _Inflight:
    req: CaptureRequest
    buffers: list[tuple[BufferPool, int]]
    ring: list[tuple[BufferPool, int]]
    items: list[_Scheduled] = field(default_factory=list)
    t_final_ns: int = 0


@dataclass
class _Session:
    streams: dict[int, Stream]
    params: dict
    pools: dict[int, BufferPool]
    mode: SensorMode
    frame_duration_ns: int
    mode_switched: bool
    prev_exposure_ns: int
    pending: deque = field(default_factory=deque)
    repeating: CaptureRequest | None = None
    inflight: dict[int, _Inflight] = field(default_factory=dict)
    active: bool = False
    hung: bool = False
    sof: _Scheduled | None = None
    frame_idx: int = 0
    started_frames: int = 0
    last_sof_ts: int = 0
    isp_busy_until: int = 0
    last_physical: str | None = None
    thermal: ThermalModel | None = None
    thermal_logged: bool = False
    stall_logged_frame: int = -1
    decode_error_logged: bool = False
    drops: int = 0
    corrupt_payload: bool = False
    in_use_notified: bool = False


class CameraDevice:
    def __init__(self, sim: Simulator, provider: "CameraProvider", profile: PlatformProfile,
                 camera_id: str, *, physical_ids: tuple[str, ...] = (), external: bool = False,
                 channel: int = 0, usb_bandwidth: float = 0.0):
        self.sim = sim
        self.provider = provider
        self.profile = profile
        self.id = camera_id
        self.physical_ids = physical_ids
        self.external = external
        self.channel = channel
        self.usb_bandwidth = usb_bandwidth
        self.sensor = Sensor(sim, f"sensor{camera_id}")
        self.ae = AeController(sim)
        self.session: _Session | None = None
        self.client: str | None = None
        self._frame_counter = 0
        self.tag = provider.tag
        self.shared_pool = False       # hal_adapter 가 다른 클라이언트 풀에 붙인 경우

    # ================================================================== #
    # 정적 메타데이터 / 스트림 조합 광고
    # ================================================================== #
    def stream_configurations(self) -> list[tuple[PixelFormat, int, int]]:
        p = self.profile
        out: list[tuple[PixelFormat, int, int]] = []
        for m in p.sensor_modes:
            if m.width * m.height > 20e6:         # 초고해상도 모드는 스틸 전용
                continue
            out += [(PixelFormat.PRIVATE, m.width, m.height), (PixelFormat.YUV_420_888, m.width, m.height),
                    (PixelFormat.BLOB, m.width, m.height)]
        out.append((PixelFormat.BLOB, *p.max_blob))
        if p.raw_capable:
            full = max(p.sensor_modes, key=lambda m: m.width * m.height)
            out.append((PixelFormat.RAW10, full.width, full.height))
        return sorted(set(out))

    def fps_ranges(self) -> dict[tuple[int, int], list[tuple[int, int]]]:
        """해상도별 광고 fps 범위. 결함 시 모든 해상도에 전역 최대 fps 를 광고한다."""
        global_max = max(m.max_fps for m in self.profile.sensor_modes)
        out = {}
        for fmt, w, h in self.stream_configurations():
            if fmt == PixelFormat.BLOB:
                continue
            mode = self.profile.mode_for(w, h, 1)
            top = global_max if self.sim.defect("fps_range_union_advertised") else (mode.max_fps if mode else 15)
            out[(w, h)] = sorted({(min(15, top), top), (top, top)})
        return out

    def static_metadata(self) -> dict[str, Any]:
        p = self.profile
        full = max(p.sensor_modes, key=lambda m: m.width * m.height)
        caps = ["BACKWARD_COMPATIBLE", "BURST_CAPTURE"]
        if p.raw_capable:
            caps.append("RAW")
        if self.physical_ids:
            caps.append("LOGICAL_MULTI_CAMERA")
        md: dict[str, Any] = {
            "android.info.supportedHardwareLevel": "EXTERNAL" if self.external else "FULL",
            "android.lens.facing": "EXTERNAL" if self.external else ("FRONT" if self.id == "1" else "BACK"),
            "android.lens.info.availableFocalLengths": [4.3],
            "android.sensor.info.activeArraySize": [0, 0, full.width, full.height],
            "android.sensor.info.pixelArraySize": [full.width, full.height],
            "android.sensor.info.physicalSize": [6.4, 4.8],
            "android.sensor.info.exposureTimeRange": [100_000, 200 * MS],
            "android.sensor.info.sensitivityRange": [100, 1600],
            "android.sensor.info.timestampSource": "REALTIME",
            "android.sensor.orientation": 90,
            "android.scaler.availableStreamConfigurations":
                [[f.name, w, h] for f, w, h in self.stream_configurations()],
            "android.scaler.availableMaxDigitalZoom": 10.0,
            "android.control.aeAvailableTargetFpsRanges":
                {f"{w}x{h}": r for (w, h), r in self.fps_ranges().items()},
            "android.control.aeCompensationRange": [-12, 12],
            "android.control.availableSceneModes": ["DISABLED", "PORTRAIT", "NIGHT", "SPORTS"],
            "android.request.availableCapabilities": caps,
            "android.request.partialResultCount": p.partial_results,
            "android.request.pipelineMaxDepth": 8,
            "android.request.maxNumOutputStreams": [1, 3, 2],   # RAW, 처리(YUV/PRIV), stall(BLOB)
            "android.flash.info.available": p.torch,
            "android.jpeg.availableThumbnailSizes": [[0, 0], [320, 240]],
        }
        if self.physical_ids:
            md["android.logicalMultiCamera.physicalIds"] = list(self.physical_ids)
        if self.sim.defect("static_metadata_incomplete"):
            # 결함: 파이프라인 핸들러가 채우지 않는 키가 빠진다.
            for k in REQUIRED_STATIC_KEYS[3:20]:
                md.pop(k, None)
        return md

    def _port_need(self, streams: list[Stream]) -> int:
        return len(streams) + sum(1 for s in streams if s.format == PixelFormat.BLOB and s.megapixels > HI_RES_MP)

    def is_stream_combination_supported(self, streams: list[Stream], fps: int = 30) -> bool:
        """isStreamCombinationSupported — 정적 메타데이터 기준 판정(프레임워크가 먼저 묻는다)."""
        advertised = set(self.stream_configurations())
        if any((s.format, s.width, s.height) not in advertised for s in streams):
            return False
        n_raw = sum(s.format in (PixelFormat.RAW10, PixelFormat.RAW_SENSOR) for s in streams)
        n_stall = sum(s.format == PixelFormat.BLOB for s in streams)
        if n_raw > 1 or n_stall > 2 or len(streams) - n_raw - n_stall > 3:
            return False
        if self._port_need(streams) > self.profile.isp_ports:
            return False
        ranges = self.fps_ranges()
        for s in streams:
            if s.format == PixelFormat.BLOB:
                continue
            if s.format in (PixelFormat.RAW10, PixelFormat.RAW_SENSOR):
                if self.profile.mode_for(s.width, s.height, fps) is None:
                    return False
                continue
            if max((hi for _, hi in ranges.get((s.width, s.height), [])), default=0) < fps:
                return False
        return True

    # ================================================================== #
    # configureStreams
    # ================================================================== #
    def configure_streams(self, streams: list[Stream], params: dict | None = None) -> Status:
        params = dict(params or {})
        sim, tag = self.sim, self.tag
        if not self.provider.alive:
            return Status.NO_DEVICE
        if self.session:
            self._stop_session(reason="reconfigure")
        fps = int(params.get(Key.AE_TARGET_FPS_RANGE, (30, 30))[1])

        # 1) ISP 출력 포트 예산
        need = self._port_need(streams)
        avail = self.profile.isp_ports
        if sim.defect("static_port_table"):
            avail = self.profile.isp_ports - 1      # 결함: 정적 표가 RDI 통계 포트를 상시 예약
        if need > avail:
            sim.log(tag, f"[ERROR][CORE] usecase selection failed, no match for {len(streams)} streams", "E")
            sim.log(tag, f"IFE port budget exceeded (req={need}, avail={avail})", "E")
            return Status.ILLEGAL_ARGUMENT

        # 2) V4L2 subdev pad 포맷 협상 (rkisp: pad0=raw bypass, pad2=main path)
        if self.profile.isp_pads:
            pads: dict[int, PixelFormat] = {}
            wants: list[tuple[int, PixelFormat]] = []
            for s in streams:
                if s.format == PixelFormat.BLOB:
                    continue
                is_raw = s.format in (PixelFormat.RAW10, PixelFormat.RAW_SENSOR)
                pad = 0 if (is_raw or sim.defect("isp_pad_single_path")) else 2
                pads[pad] = s.format
                wants.append((pad, s.format))
                fmt = "SRGGB10" if is_raw else "NV12"
                note = " overwrites raw" if pad == 0 and not is_raw and any(
                    w[1] == PixelFormat.RAW10 for w in wants[:-1]) else " ok"
                sim.log("rkisp", f"set_fmt on pad {pad} {fmt}{note}")
            for i, (pad, fmt) in enumerate(wants):
                if pads[pad] != fmt:
                    sim.log(tag, f"negotiate failed for stream {i}, configureStreams -22", "E")
                    return Status.ILLEGAL_ARGUMENT

        # 3) 센서 모드 / 프레임률
        video = [s for s in streams if s.format not in (PixelFormat.BLOB,)]
        big = max(video or streams, key=lambda s: s.width * s.height)
        mode = self.profile.mode_for(big.width, big.height, fps)
        if mode is None:
            if self.profile.isp in ("v4l2", "usb"):
                sim.log("v4l2", f"VIDIOC_S_PARM {fps}fps at {big.width}x{big.height} failed: EINVAL", "E")
            sim.log(tag, f"no sensor mode for {big.width}x{big.height}@{fps}", "E")
            return Status.ILLEGAL_ARGUMENT

        # 4) 버퍼 할당 (usage 협상)
        pools = {}
        for s in streams:
            usage = s.usage | int(Usage.CAMERA_OUTPUT)
            if sim.defect("usage_flags_hal_only"):
                usage = (s.usage & int(Usage.GPU_TEXTURE)) | int(Usage.CAMERA_OUTPUT)
                sim.log(tag, f"stream {s.id}: consumer usage requested 0x{s.usage:x}, "
                             f"allocated with usage 0x{usage & 0xffff:x} (hal only)", "W")
            depth = 3 if sim.defect("v4l2_queue_depth_short") else self.profile.queue_depth
            size = depth if self.profile.starvation == "drop" else self.profile.pool_size
            pools[s.id] = BufferPool(sim, s.id, size, usage, f"{tag}.s{s.id}")
        if self.profile.starvation == "drop":
            sim.log("v4l2", f"VIDIOC_REQBUFS count={depth}", "D")

        prev_exposure = self.sensor.exposure_ns
        switched = self.sensor.set_mode(mode)
        self.sensor.reset_pipeline()
        self.session = _Session(
            streams={s.id: s for s in streams}, params=params, pools=pools, mode=mode,
            frame_duration_ns=int(1e9 / fps), mode_switched=switched, prev_exposure_ns=prev_exposure,
        )
        if params.get("thermal_model"):
            self.session.thermal = ThermalModel(started_ns=sim.now)
        self.provider.on_session_configured(self)
        sim.log(tag, f"configureStreams ok: {', '.join(s.label() for s in streams)} "
                     f"mode {mode.width}x{mode.height}@{fps}", "D")
        return Status.OK

    # ================================================================== #
    # 요청 제출
    # ================================================================== #
    def next_frame_number(self) -> int:
        n = self._frame_counter
        self._frame_counter += 1
        return n

    def process_capture_request(self, req: CaptureRequest) -> Status:
        s = self.session
        if s is None or s.hung or not self.provider.alive:
            return Status.NO_DEVICE
        s.pending.append(req)
        self._ensure_streaming()
        return Status.OK

    def set_repeating(self, template: CaptureRequest | None) -> None:
        if self.session:
            self.session.repeating = template
            if template:
                self._ensure_streaming()

    def _ensure_streaming(self) -> None:
        s = self.session
        if s.active:
            return
        s.active = True
        self.sensor.streaming = True
        path = "extension" if s.params.get("extension") else "normal"
        if path == "extension":
            self.sim.log(self.tag, "session started via extension path")
        if not (path == "extension" and self.sim.defect("privacy_notify_normal_path_only")):
            self.provider.notify_in_use(self, True)
            s.in_use_notified = True
        self.provider.service.on_sensor_streaming(self, True)
        # 멀티채널(EVS) 은 공통 FSYNC 로 위상을 맞춘다. 결함 시 채널별 자유 발진.
        phase = 0
        if self.profile.channels > 1:
            if self.sim.defect("evs_free_running_channels"):
                phase = int(self.sim.rng.uniform(0, s.frame_duration_ns))
            else:
                phase = int(abs(self.sim.rng.gauss(0, 0.25 * MS)))
            base = (self.sim.now // s.frame_duration_ns + 1) * s.frame_duration_ns
            s.sof = self.sim.at(base + phase, self._on_sof)
        else:
            s.sof = self.sim.after(1 * MS, self._on_sof)

    # ================================================================== #
    # 센서 SOF — 프레임 한 장의 처리
    # ================================================================== #
    def _on_sof(self) -> None:
        s = self.session
        if s is None or not s.active or s.hung:
            return
        sim, p, tag = self.sim, self.profile, self.tag
        s.sof = sim.after(s.frame_duration_ns, self._on_sof)
        s.frame_idx += 1
        exp, gain = self.sensor.latch_frame()
        max_exp = s.frame_duration_ns
        if s.mode_switched and s.started_frames == 0 and sim.defect("mode_switch_stale_exposure"):
            exp = s.prev_exposure_ns
            sim.log(tag, f"frame 0: exposure from previous mode ({exp / MS:.0f}ms)", "W")
        else:
            exp = min(exp, max_exp)

        req = s.pending[0] if s.pending else None
        if req is None and s.repeating is None:
            return

        # 소프트웨어 ISP 는 직렬 처리 — 바쁘면 센서 프레임이 덮어써진다.
        if p.isp == "software" and sim.now < s.isp_busy_until:
            self.provider.service.on_frame_drop(self, "isp busy (sensor overrun)")
            return

        # 외장 USB(MJPEG) 디코드
        if p.isp == "usb" and self.provider.env.get("webcam_omits_dht") and s.started_frames >= 1:
            if sim.defect("mjpeg_no_default_huffman"):
                if not s.decode_error_logged:
                    sim.log("usb_hal", f"frame {s.started_frames} decode error: no huffman table", "E")
                    s.decode_error_logged = True
                self.provider.service.on_frame_drop(self, "mjpeg decode error")
                return

        # 출력 버퍼 확보
        if req is None:
            req = self._instantiate_repeating()
            from_repeating = True
        else:
            from_repeating = False
        got: list[tuple[BufferPool, int]] = []
        ring: list[tuple[BufferPool, int]] = []
        starved: BufferPool | None = None
        for sid in req.stream_ids:
            pool = s.pools[sid]
            b = pool.acquire(f"frame{req.frame_number}")
            if b is None:
                starved = pool
                break
            got.append((pool, b))
        if starved is None and req.zsl:
            prev_pool = self._preview_pool()
            for _ in range(ZSL_RING):
                b = prev_pool.acquire("zsl-ring") if prev_pool else None
                if b is None:
                    starved = prev_pool
                    break
                ring.append((prev_pool, b))
        if starved is not None:
            for pool, b in got + ring:
                pool.release(b)
            if p.starvation == "drop":
                if from_repeating:
                    self._forget_frame(req)
                s.drops += 1
                if s.drops == 1 or s.drops % 25 == 0:
                    sim.log("v4l2", "VIDIOC_DQBUF returned EAGAIN", "W")
                    sim.log("v4l2", f"no free buffer, dropping frame (count={s.drops})", "W")
                self.provider.service.on_frame_drop(self, "no free buffer")
                return
            if from_repeating:
                s.pending.appendleft(req)
            if s.stall_logged_frame != req.frame_number:
                sim.log(tag, f"buffer manager: outstanding={starved.outstanding} max={starved.size}, stall", "W")
                s.stall_logged_frame = req.frame_number
            return
        if not from_repeating:
            s.pending.popleft()

        # 타임스탬프 (REALTIME = boottime 기준 노출 시작)
        ts = sim.now + self.provider.boottime_offset_ns
        if sim.defect("ts_pipeline_ready_clock") and s.started_frames < 4:
            ts = sim.now                                        # monotonic 기준
            if s.started_frames == 0:
                sim.log(tag, "frame 0-3: ts from pipeline-ready clock", "D")
        elif sim.defect("ts_pipeline_ready_clock") and s.started_frames == 4:
            sim.log(tag, "frame 4+: ts from sensor exposure clock", "D")
        if sim.defect("ts_from_pipe_completion") and s.last_sof_ts and sim.rng.random() < 0.02:
            back = sim.rng.randint(5, 17) * MS
            sim.log(tag, f"pipe SCP done ts={s.last_sof_ts}", "D")
            ts = s.last_sof_ts - back
            sim.log(tag, f"pipe SCC done ts={ts} (earlier)", "W")
        s.last_sof_ts = sim.now + self.provider.boottime_offset_ns
        s.started_frames += 1

        # 3A
        env = self.provider.env
        scene = req.settings.get(Key.SCENE_MODE, "DISABLED")
        effects: list[str] = []
        handlers = not sim.defect("scene_mode_handlers_missing")
        self.ae.target = AeController.TARGET
        ae_max = max_exp
        if scene == "NIGHT" and handlers:
            self.ae.target, effects = AeController.TARGET * 1.5, ["night-brighten"]
        elif scene == "SPORTS" and handlers:
            ae_max, effects = min(max_exp, 8 * MS), ["sports-short-exposure"]
        elif scene == "PORTRAIT" and handlers:
            effects = ["portrait-bokeh"]
        luma = scene_luma(env["lux"], exp, gain)
        new_exp, new_gain = self.ae.update(luma, (exp, gain), ae_max)
        self.sensor.queue_settings(new_exp, new_gain)

        flash_fired = req.settings.get(Key.FLASH_MODE) == "SINGLE"
        ambient = env["ambient_cct"]
        illum = 0.85 * env["flash_cct"] + 0.15 * ambient if flash_fired else ambient
        if flash_fired and sim.defect("awb_capture_uses_ambient_gains"):
            gains_cct = ambient
            sim.log("AWB", f"precapture flash gains r={cct_to_gains(illum)[0]} b={cct_to_gains(illum)[1]} computed", "D")
            sim.log("AWB", f"capture applied gains r={cct_to_gains(ambient)[0]} b={cct_to_gains(ambient)[1]} (ambient)", "W")
        else:
            gains_cct = illum + sim.rng.gauss(0, 60)

        # SHUTTER
        frame = req.frame_number
        inflight = _Inflight(req, got, ring)
        s.inflight[frame] = inflight
        inflight.items.append(sim.after(1 * MS, self.provider.service.on_shutter, self, frame, ts))

        # 처리 지연
        mp = sum(s.streams[sid].megapixels for sid in req.stream_ids)
        latency = p.isp_latency_ms + 2.0 * mp + sim.rng.gauss(0, 1.5)
        if any(s.streams[sid].format == PixelFormat.BLOB for sid in req.stream_ids):
            latency += p.jpeg_ms
        if p.isp == "software":
            per_mp = (21.4 + 9.8) if sim.defect("softisp_scalar_debayer") else (6.0 + 2.5)
            cost = per_mp * mp / 2.07 + abs(sim.rng.gauss(0, 2.5))
            if sim.defect("softisp_scalar_debayer") and s.started_frames == 1:
                sim.log("softISP", f"debayer 21.4ms + ccm 9.8ms per frame ({mp:.2f}MP)", "D")
            s.isp_busy_until = sim.now + int(cost * MS)
            latency = cost + 2
        t_final = sim.now + int(max(latency, 2) * MS)
        inflight.t_final_ns = t_final

        snapshot = dict(ts=ts, exp=exp, gain=gain, luma=luma, effects=effects, illum=illum,
                        gains_cct=gains_cct, flash=flash_fired, frame_duration=s.frame_duration_ns)
        n = p.partial_results
        for i in range(1, n + 1):
            t_i = t_final - (n - i) * SEC_PER_PARTIAL_NS
            if n > 1 and sim.defect("partial_results_unsequenced"):
                t_i += int(sim.rng.uniform(0, 5.0) * MS)
            if i < n:
                inflight.items.append(sim.at(t_i, self.provider.service.on_partial_result, self, frame, i, n))
            else:
                inflight.items.append(sim.at(t_i, self._complete, frame, snapshot))

    def _instantiate_repeating(self) -> CaptureRequest:
        t = self.session.repeating
        req = CaptureRequest(self.next_frame_number(), t.stream_ids, dict(t.settings))
        self.provider.service.on_repeating_frame(self, req)
        return req

    def _forget_frame(self, req: CaptureRequest) -> None:
        self.provider.service.on_repeating_dropped(self, req)

    def _preview_pool(self) -> BufferPool | None:
        s = self.session
        for sid, st in s.streams.items():
            if st.format == PixelFormat.PRIVATE:
                return s.pools[sid]
        return None

    # ================================================================== #
    # 최종 결과
    # ================================================================== #
    def _complete(self, frame: int, snap: dict) -> None:
        s = self.session
        inf = s.inflight.pop(frame, None) if s else None
        if inf is None:
            # 세션이 정리됐는데 늦게 도착한 결과(flush 가 먼저 반환된 경우)
            return
        sim, p, env, req = self.sim, self.profile, self.provider.env, inf.req
        svc = self.provider.service
        meta: dict[str, Any] = {
            Key.SENSOR_TIMESTAMP: snap["ts"],
            Key.SENSOR_EXPOSURE_TIME: snap["exp"],
            Key.SENSOR_FRAME_DURATION: snap["frame_duration"],
            Key.SENSOR_SENSITIVITY: int(snap["gain"] * 100),
            Key.AE_STATE: self.ae.state,
            Key.SCENE_MODE: req.settings.get(Key.SCENE_MODE, "DISABLED"),
            Key.FLASH_STATE: "FIRED" if snap["flash"] else "READY",
            Key.CC_GAINS: [cct_to_gains(snap["gains_cct"])[0], 1.0, 1.0, cct_to_gains(snap["gains_cct"])[1]],
            Key.TUNING_ID: self.provider.aiq_tuning_id,
        }
        if Key.JPEG_ORIENTATION in req.settings:
            if sim.defect("orientation_read_at_completion"):
                meta[Key.JPEG_ORIENTATION] = env["orientation"]
                if env["orientation"] != req.settings[Key.JPEG_ORIENTATION]:
                    sim.log(self.tag, f"result {frame}: orientation={env['orientation']} (read at completion)", "W")
            else:
                meta[Key.JPEG_ORIENTATION] = req.settings[Key.JPEG_ORIENTATION]
        if Key.NOISE_REDUCTION_MODE in req.settings:
            nr = req.settings[Key.NOISE_REDUCTION_MODE]
            if req.reprocess and sim.defect("reprocess_uses_session_params"):
                nr = s.params.get(Key.NOISE_REDUCTION_MODE, "FAST")
                sim.log("pipeline", f"reprocess req {frame}: overridden with session value {nr}", "W")
            meta[Key.NOISE_REDUCTION_MODE] = nr
        if Key.ZOOM_RATIO in req.settings:
            meta[Key.ZOOM_RATIO] = req.settings[Key.ZOOM_RATIO]

        physical = None
        if self.physical_ids:
            zoom = req.settings.get(Key.ZOOM_RATIO, 1.0)
            active = self.physical_ids[0] if zoom < 2.0 else self.physical_ids[1]
            meta[Key.LOGICAL_ACTIVE_PHYSICAL_ID] = active
            if s.last_physical and s.last_physical != active:
                if sim.defect("physical_meta_dropped_on_switch"):
                    physical = {}
                    sim.log(self.tag, f"physical meta list empty at frame {frame} "
                                      f"(switch {s.last_physical}->{active})", "W")
                else:
                    physical = {s.last_physical: {Key.SENSOR_TIMESTAMP: snap["ts"]}}
            else:
                physical = {active: {Key.SENSOR_TIMESTAMP: snap["ts"]}}
            s.last_physical = active

        width_scale = 1.0
        if s.thermal:
            level = s.thermal.level(sim.now)
            if level >= 3:
                width_scale = 0.5
                if not s.thermal_logged:
                    big = max(s.streams.values(), key=lambda x: x.width)
                    sim.log("thermal", f"level {level}, internal downscale {big.width}->{big.width // 2}", "W")
                    s.thermal_logged = True
                if not sim.defect("thermal_silent_downscale"):
                    meta[Key.THERMAL_THROTTLE] = level

        buffers = []
        for pool, b in inf.buffers:
            st = s.streams[pool.stream_id]
            status = "OK"
            if req.storage_slow and st.format == PixelFormat.BLOB:
                status = "ERROR"
            consumer_ok = (pool.usage & st.usage) == st.usage
            buffers.append({"stream": st.id, "format": st.format.name, "status": status,
                            "usage": pool.usage & 0xffff, "requested_usage": st.usage,
                            "consumer_lock": "ok" if consumer_ok else "failed"})
            pool.held[b] = "consumer"
            hold = self._consumer_hold_ms()
            sim.after(hold * MS, pool.release, b)
        if inf.ring:
            if req.storage_slow and sim.defect("zsl_leak_on_cancel"):
                for pool, b in inf.ring:
                    pool.held[b] = "leaked"
            else:
                for pool, b in inf.ring:
                    pool.release(b)

        seed = zlib.crc32(f"{self.id}:{frame}:{snap['ts']}".encode()) or 1
        checksum = 0 if (s.corrupt_payload and sim.rng.random() < 0.8) else seed
        stats = {
            "luma": round(snap["luma"], 4),
            "wb_error_k": round(abs(snap["illum"] - snap["gains_cct"])),
            "checksum": checksum,
            "effective_width_scale": width_scale,
            "effects": snap["effects"],
        }
        for b in buffers:
            if b["consumer_lock"] == "failed":
                stats["rendered"] = "black"
        svc.on_result(self, frame, meta, stats, buffers, physical)
        if req.storage_slow and any(b["status"] == "ERROR" for b in buffers):
            svc.on_notify_error(self, frame, ErrorCode.ERROR_BUFFER)

    def _consumer_hold_ms(self) -> float:
        env = self.provider.env
        if env.get("high_load") and self.sim.rng.random() < 0.25:
            return self.sim.rng.uniform(45, 95)
        return self.sim.rng.uniform(4, 18)

    # ================================================================== #
    # flush / close
    # ================================================================== #
    def flush(self) -> Status:
        s = self.session
        if s is None:
            return Status.OK
        svc = self.provider.service
        for req in list(s.pending):
            svc.on_notify_error(self, req.frame_number, ErrorCode.ERROR_REQUEST)
        s.pending.clear()
        s.repeating = None
        if self.sim.defect("flush_returns_before_inflight_drained"):
            self.sim.log(self.tag, f"flush() returned, inflight={len(s.inflight)}", "D")
            return Status.OK
        for frame, inf in list(s.inflight.items()):
            self._abort_inflight(frame, inf)
        return Status.OK

    def _abort_inflight(self, frame: int, inf: _Inflight) -> None:
        s = self.session
        for item in inf.items:
            item.cancel()
        for pool, b in inf.buffers + inf.ring:
            pool.release(b)
        s.inflight.pop(frame, None)
        self.provider.service.on_notify_error(self, frame, ErrorCode.ERROR_REQUEST)

    def close(self) -> Status:
        s = self.session
        sim = self.sim
        if s is None:
            self.provider.on_device_closed(self)
            return Status.OK
        if s.hung:
            return Status.TIMED_OUT
        # closeStream 과 결과 방출 스레드의 락 순서
        emitting = [inf for inf in s.inflight.values() if 0 <= inf.t_final_ns - sim.now <= 4 * MS]
        if emitting:
            close_order = ("B", "A") if sim.defect("lock_order_inversion") else ("A", "B")
            op_close = LockOp("thread-12", "closeStream", close_order, sim.now, 3 * MS)
            op_emit = LockOp("thread-7", "emitResult", ("A", "B"), emitting[0].t_final_ns - 4 * MS, 4 * MS)
            if detect_deadlock(op_close, op_emit):
                s.hung = True
                if s.sof:
                    s.sof.cancel()
                for inf in s.inflight.values():
                    for item in inf.items:
                        item.cancel()
                sim.log("thread-12", "waiting for lock A (holds B) in closeStream", "W")
                sim.log("thread-7", "waiting for lock B (holds A) in emitResult", "W")
                sim.advance(30 * SEC)
                sim.log("watchdog", "no progress for 30s", "F")
                self.provider.abort("watchdog: deadlock in closeStream/emitResult")
                return Status.TIMED_OUT
        if sim.defect("close_async_ack"):
            sim.log("framework", "close() returned success at t=0ms", "D")
            sim.after(2400 * MS, self._stop_session, "close")
            self.provider.service.on_close_returned(self, Status.OK)
            sim.after(2400 * MS, self.provider.on_device_closed, self)
            return Status.OK
        self._stop_session("close")
        sim.advance(40 * MS)
        self.provider.service.on_close_returned(self, Status.OK)
        self.provider.on_device_closed(self)
        return Status.OK

    def _stop_session(self, reason: str) -> None:
        s = self.session
        if s is None:
            return
        svc = self.provider.service
        was_active = s.active
        s.active = False
        if s.sof:
            s.sof.cancel()
        for frame, inf in list(s.inflight.items()):
            self._abort_inflight(frame, inf)
        for req in list(s.pending):
            svc.on_notify_error(self, req.frame_number, ErrorCode.ERROR_REQUEST)
        s.pending.clear()
        if was_active:
            self.sensor.streaming = False
            if s.started_frames and self.sim.defect("close_async_ack"):
                self.sim.log(self.sensor.name, f"streaming stopped (reason={reason})", "D")
            svc.on_sensor_streaming(self, False)
            if s.in_use_notified:
                self.provider.notify_in_use(self, False)
        leaked = 0
        for pool in s.pools.values():
            for b, owner in list(pool.held.items()):
                if owner == "leaked":
                    leaked += 1
                else:
                    pool.release(b)
        svc.on_session_closed(self, leaked, reason)
        self.provider.on_session_released(self)
        self.session = None
