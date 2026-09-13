"""ICameraProvider 모델 — 디바이스 열거, 핫플러그, 토치, IPC, 메모리, 진단 인터페이스."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..engine import Simulator
from ..model import MS, SEC, DeviceStatus, Status
from ..platforms import PlatformProfile
from .components import SePolicy
from .device import CameraDevice

if TYPE_CHECKING:
    from ..framework import CameraService

LOG_TAG = {
    "CAMX": "CamX", "EXHA": "ExynosCamera", "MTKC": "mtkcam", "RKAQ": "rkcamhal", "IPU6": "ipu6",
    "LCAM": "libcamera", "CROS": "cros-camera", "V4L2": "v4l2hal", "EMUC": "EmulatedCamera", "EVSC": "evs",
}
LMK_LIMIT_MB = 1900
SESSION_RSS_MB = 40
SESSION_PIPES = 12
USB_BANDWIDTH = {"0": 0.6, "1": 0.55, "2": 0.3}


class CameraProvider:
    def __init__(self, sim: Simulator, profile: PlatformProfile, service: "CameraService"):
        self.sim = sim
        self.profile = profile
        self.service = service
        self.tag = LOG_TAG.get(profile.fw_prefix, "camhal")
        self.env: dict = dict(lux=300.0, ambient_cct=5000.0, flash_cct=5500.0, orientation=0,
                              high_load=False, webcam_omits_dht=False, cold_boot=False)
        self.alive = True
        self.rss_mb = float(profile.base_rss_mb)
        self.pipes_alive = 0
        self.sessions = 0
        self.boottime_offset_ns = 0
        self.aiq_tuning_id = 0x3C21
        self.ipc_epoch = 0
        self.remote_epoch = 0
        self.fw_cached = False
        self.sensors_standby = False
        self.hw_present: set[str] = set()
        self.devices: dict[str, CameraDevice] = {}
        self.clients: dict[str, list[CameraDevice]] = {}
        rules = {("camera.provider", "video_device", p) for p in ("open", "read", "write", "ioctl")}
        if sim.defect("sepolicy_missing_video_device"):
            rules = {r for r in rules if r[2] not in ("read", "write")}
        self.sepolicy = SePolicy(rules)

        for cid in profile.camera_ids:
            self.devices[cid] = CameraDevice(
                sim, self, profile, cid,
                physical_ids=("2", "3") if profile.logical_multicam and cid == "0" else (),
                channel=int(cid) if profile.channels > 1 else 0,
                usb_bandwidth=USB_BANDWIDTH.get(cid, 0.0) if profile.isp == "usb" else 0.0,
            )
            self.hw_present.add(cid)

    # ------------------------------------------------------------------ #
    def initialize(self) -> None:
        """provider 프로세스 시작(부팅 시). 펌웨어 선로딩·센서 대기전원은 여기서."""
        p, sim = self.profile, self.sim
        if p.fw_load_ms and not sim.defect("isp_fw_loaded_per_open"):
            sim.log(self.tag, f"preloading firmware + tuning at provider init "
                              f"({p.fw_load_ms + p.tuning_parse_ms:.0f}ms, cached)", "D")
            sim.advance((p.fw_load_ms + p.tuning_parse_ms) * MS)
            self.fw_cached = True
        if p.channels > 1 and not sim.defect("evs_cold_start_on_gear"):
            sim.log(self.tag, "sensors held in standby power at ignition", "D")
            self.sensors_standby = True
        boot_ms = 800 if p.channels > 1 else 0
        sim.advance(boot_ms * MS)
        for cid in self.devices:
            self.service.on_device_status(cid, DeviceStatus.PRESENT)
        if p.torch:
            for cid in self.devices:
                self.service.on_torch_status(cid, True)

    def camera_ids(self) -> list[str]:
        return list(self.devices)

    def concurrent_camera_ids(self) -> list[tuple[str, str]]:
        """getConcurrentCameraIds — 동시에 열 수 있다고 광고하는 카메라 쌍."""
        ids = sorted(self.devices)
        pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]
        if self.sim.defect("concurrent_combos_unchecked"):
            out = [pr for pr in pairs if pr[0] == "0"]      # 결함: 대역폭 검증 없는 정적 목록
        else:
            out = [pr for pr in pairs
                   if self.devices[pr[0]].usb_bandwidth + self.devices[pr[1]].usb_bandwidth <= 1.0]
        self.sim.log(self.tag, f"static: concurrent combos advertised: {out}", "D")
        return out

    # ------------------------------------------------------------------ #
    # IPC (Binder / Mojo)
    # ------------------------------------------------------------------ #
    def ipc(self, method: str) -> bool:
        if self.remote_epoch != self.ipc_epoch:
            self.sim.log("client", "using cached remote (stale)", "W")
            self.sim.advance(3 * SEC)
            self.sim.log("client", f"{method} no response after 3000ms", "E")
            self.service.on_ipc(method, Status.TIMED_OUT, 3000)
            return False
        self.sim.advance(2 * MS)
        self.service.on_ipc(method, Status.OK, 2)
        return True

    def suspend_resume(self, suspended_s: float) -> None:
        sim = self.sim
        sim.log("power", f"suspend {suspended_s:g}s")
        self.boottime_offset_ns += int(suspended_s * SEC)
        self.ipc_epoch += 1
        sim.log(self.tag, "mojo connection error, pipe closed", "W")
        sim.advance(400 * MS)
        sim.log(self.tag, "service restarted after resume", "I")
        if not sim.defect("ipc_stale_remote_after_resume"):
            sim.advance(150 * MS)
            self.remote_epoch = self.ipc_epoch
            sim.log("client", "disconnect handler: reconnected to camera service", "I")

    # ------------------------------------------------------------------ #
    # open / close
    # ------------------------------------------------------------------ #
    def open(self, camera_id: str, client: str) -> tuple[Status, CameraDevice | None]:
        sim, p = self.sim, self.profile
        if not self.alive:
            return Status.NO_DEVICE, None
        if not self.ipc("openDevice"):
            return Status.TIMED_OUT, None
        dev = self.devices.get(camera_id)
        if dev is None:
            return Status.NO_DEVICE, None
        if camera_id not in self.hw_present:
            sim.log(self.tag, f"open /dev/video{int(camera_id) + 1} blocking (device gone)", "W")
            sim.advance(5 * SEC)
            sim.log("CameraService", f"open({camera_id}) timeout after 5000ms", "E")
            return Status.TIMED_OUT, None

        shared = False
        if dev.client is not None:
            if p.code != "IPU6-CameraHAL":
                return Status.CAMERA_IN_USE, None
            # IPU6 hal_adapter 는 여러 클라이언트를 한 센서에 붙인다.
            dev = CameraDevice(sim, self, p, camera_id)
            dev.sensor = self.devices[camera_id].sensor
            shared = True
        if dev.usb_bandwidth:
            used = sum(d.usb_bandwidth for d in self.devices.values() if d.client)
            if used + dev.usb_bandwidth > 1.0:
                sim.log("allocator", f"bandwidth check failed for camera {camera_id} "
                                     f"(used {used:.2f} + {dev.usb_bandwidth:.2f} > 1.0)", "E")
                return Status.CAMERA_IN_USE, None

        if p.fw_load_ms and not self.fw_cached:
            sim.log(self.tag, f"loading firmware binary 41MB ({p.fw_load_ms:.0f}ms)")
            sim.advance(p.fw_load_ms * MS)
            sim.log(self.tag, f"tuning parse {p.tuning_parse_ms:.0f}ms")
            sim.advance(p.tuning_parse_ms * MS)
        if p.channels > 1 and not self.sensors_standby:
            sim.log(self.tag, f"sensor{camera_id} power {p.sensor_power_ms:.0f}ms + register config 680ms", "D")
            sim.advance((p.sensor_power_ms + 680) * MS)
        if not shared:
            delay = 1.0 if sim.defect("sensor_power_delay_short") else 5.0
            if not dev.sensor.power_on(delay, cold=self.env.get("cold_boot", False)):
                sim.log(self.tag, "openDevice failed, ret=-ENODEV", "E")
                return Status.NO_DEVICE, None
        sim.advance(p.open_ms * MS)
        dev.client = client
        self.clients.setdefault(camera_id, []).append(dev)
        if shared and sim.defect("hal_adapter_shared_pool"):
            dev.shared_pool = True
            sim.log("hal_adapter", f"client {len(self.clients[camera_id])} attached to shared pool id=0", "W")
        if p.torch and camera_id in ("0",):
            self.service.on_torch_status(camera_id, False)
        return Status.OK, dev

    def on_device_closed(self, dev: CameraDevice) -> None:
        dev.client = None
        lst = self.clients.get(dev.id, [])
        if dev in lst:
            lst.remove(dev)
        if self.profile.torch and dev.id == "0" and not lst:
            if self.sim.defect("torch_release_notify_missing"):
                self.sim.log("torch", "camera released by client; no availability notification sent", "W")
            else:
                self.service.on_torch_status(dev.id, True)

    # ------------------------------------------------------------------ #
    # 세션 자원 / 메모리
    # ------------------------------------------------------------------ #
    def on_session_configured(self, dev: CameraDevice) -> None:
        self.sessions += 1
        self.rss_mb += SESSION_RSS_MB
        self.pipes_alive += SESSION_PIPES
        if getattr(dev, "shared_pool", False):
            dev.session.corrupt_payload = True

    def on_session_released(self, dev: CameraDevice) -> None:
        self.sessions -= 1
        if self.sim.defect("pipe_teardown_leak"):
            self.rss_mb -= SESSION_RSS_MB - 9
            self.pipes_alive -= SESSION_PIPES - 1
        else:
            self.rss_mb -= SESSION_RSS_MB
            self.pipes_alive -= SESSION_PIPES
        self.service.on_provider_memory(self.rss_mb, self.pipes_alive, self.sessions)
        if self.sessions == 0 and self.pipes_alive:
            self.sim.log(self.tag, f"session closed, pipes alive={self.pipes_alive} (expected 0)", "W")
        if self.rss_mb > LMK_LIMIT_MB:
            self.sim.log("meminfo", f"camera.provider RSS {self.rss_mb / 1000:.1f}GB", "W")
            self.sim.log("lowmemorykiller", "killing camera.provider adj=0", "F")
            self.abort("lowmemorykiller")

    def abort(self, reason: str) -> None:
        self.alive = False
        for d in list(self.devices.values()) + [x for lst in self.clients.values() for x in lst]:
            if d.session and d.session.sof:
                d.session.sof.cancel()
            if d.session:
                d.session.active = False
        self.service.on_provider_died(reason)

    # ------------------------------------------------------------------ #
    # 외장 카메라 핫플러그
    # ------------------------------------------------------------------ #
    def usb_connect(self, camera_id: str) -> None:
        sim = self.sim
        node = f"video{int(camera_id) + 1}"
        self.hw_present.add(camera_id)
        sim.log("kernel", f"usb 1-2: new high-speed USB device, uvcvideo: /dev/{node}")
        self.service.on_hw_usb(camera_id, True)
        if not self.sepolicy.allowed(sim, "camera.provider", "video_device", node, ("open", "read", "write")):
            sim.log(self.tag, f"open /dev/{node} failed: Permission denied", "E")
            sim.log(self.tag, "external camera enumeration: 0 found", "W")
            return
        self.devices[camera_id] = CameraDevice(sim, self, self.profile, camera_id, external=True)
        self.service.on_device_status(camera_id, DeviceStatus.PRESENT)

    def usb_disconnect(self, camera_id: str) -> None:
        sim = self.sim
        self.hw_present.discard(camera_id)
        sim.log("kernel", "usb 1-2: USB disconnect, device number 3")
        self.service.on_hw_usb(camera_id, False)
        if sim.defect("provider_hotplug_cache_stale"):
            sim.log(self.tag, f"cached id list {sorted(self.devices)} (external={camera_id})", "W")
            return
        self.devices.pop(camera_id, None)
        self.service.on_device_status(camera_id, DeviceStatus.NOT_PRESENT)

    # ------------------------------------------------------------------ #
    # 3A 서버, 방향, 진단, 사용중 알림
    # ------------------------------------------------------------------ #
    def aiq_server_restart(self) -> None:
        sim = self.sim
        sim.log("rkaiq", "server restarted, loading built-in defaults", "W")
        if sim.defect("aiq_tuning_not_resent"):
            self.aiq_tuning_id = 0
            sim.log(self.tag, "tuning file pushed once at boot (not re-sent)", "W")
            return
        # 재시작 epoch 변화를 감지하면 다음 프레임 적용 전에 튜닝을 다시 올린다.
        sim.log(self.tag, "aiq restart detected, tuning 0x3C21 re-pushed", "I")

    def set_orientation(self, degrees: int) -> None:
        self.env["orientation"] = degrees
        self.sim.log("sensorservice", f"device orientation -> {degrees}", "D")

    def diag_request(self, peer: str, authenticated: bool) -> bool:
        sim = self.sim
        if not sim.defect("diag_path_in_production"):
            sim.log("diag", f"request from {peer} rejected: diagnostic interface not built", "I")
            self.service.on_diag(peer, served=False, authenticated=authenticated)
            return False
        sim.log("diag", f"frame dump request from {peer} ({'auth' if authenticated else 'no auth'})", "W")
        sim.log("build", "DIAG_ENABLED=1 in production config", "W")
        sim.log("diag", "1920x1080 NV12 frame served", "W")
        self.service.on_diag(peer, served=True, authenticated=authenticated)
        return True

    def notify_in_use(self, dev: CameraDevice, on: bool) -> None:
        self.service.on_camera_in_use(dev, on)
