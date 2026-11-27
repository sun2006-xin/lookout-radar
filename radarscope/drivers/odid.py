"""Open Drone ID (ASTM F3411) & MAVLink 无人机远程识别接入驱动（ROADMAP G1）。

支持两种主流帧格式：
  1. 标准 25 字节 ASTM F3411 Location (type=0x1) / Basic ID (type=0x0) 二进制帧
  2. JSON 行流（如 dump3411 / MAVLink OPEN_DRONE_ID_LOCATION / ADSB_VEHICLE 桥接转发）
"""
from __future__ import annotations

import json
import math
import struct
import time

from ..core.driver import RadarDriver, register
from ..core.model import Capabilities, RadarFrame, Target
from ..core.transport import endpoint_of, open_transport, parse_spec


def _geodetic_xy(lat: float, lon: float, ref_lat: float, ref_lon: float) -> tuple[float, float]:
    from ..core.geodesy import geodetic_to_enu

    e, n, _u = geodetic_to_enu(lat, lon, 0.0, ref_lat, ref_lon, 0.0)
    return e, n


def parse_odid_frame(pkt: bytes, ref_lat: float = 39.9, ref_lon: float = 116.4) -> Target | None:
    """解析 25 字节 ASTM F3411 Location Message（type=1）。"""
    if len(pkt) < 25:
        return None
    msg_type = (pkt[0] >> 4) & 0x0F
    if msg_type != 1:
        return None
    # Byte 1: status/flags; 2: direction (deg/2 if EW=1 or +180); 3: speed*0.25; 4: vert_speed*0.5
    flags = pkt[1]
    speed_mult = 0.75 if (flags & 0x01) else 0.25
    ew_bit = (flags >> 1) & 0x01
    trk_deg = float(pkt[2]) + (180.0 if ew_bit else 0.0)
    speed_ms = float(pkt[3]) * speed_mult if pkt[3] != 255 else 0.0
    v_rate = struct.unpack_from("<b", pkt, 4)[0] * 0.5
    lat_i, lon_i = struct.unpack_from("<2i", pkt, 5)
    lat = lat_i * 1e-7
    lon = lon_i * 1e-7
    if abs(lat) > 90.0 or abs(lon) > 180.0 or (lat == 0.0 and lon == 0.0):
        return None
    _alt_baro, alt_geo, height_agl = struct.unpack_from("<3H", pkt, 13)
    z_m = (height_agl * 0.5 - 1000.0) if height_agl != 0xFFFF else (alt_geo * 0.5 - 1000.0 if alt_geo != 0xFFFF else 0.0)
    x, y = _geodetic_xy(lat, lon, ref_lat, ref_lon)
    r_xy = math.hypot(x, y)
    tr_rad = math.radians(trk_deg)
    el = math.degrees(math.atan2(z_m, max(1.0, r_xy)))
    return Target(
        x=x, y=y, vx=speed_ms * math.sin(tr_rad), vy=speed_ms * math.cos(tr_rad),
        z=max(0.0, z_m), vz=float(v_rate), el_deg=el,
        snr=0.92, threat=1, cls="UAV",
    )


@register
class OpenDroneIdDriver(RadarDriver):
    """Open Drone ID / MAVLink 无人机 Remote ID 接入驱动。"""

    id = "odid"
    label = "Open Drone ID · 无人机远程识别"
    source = "device"
    form = "omni"
    vendor = "ASTM F3411 / MAVLink"
    default_baud = 115200
    simulated = False

    def __init__(self) -> None:
        self._tr = None
        self._endpoint = ""
        self._ref_lat = 39.9
        self._ref_lon = 116.4
        self._range_max = 5000.0
        self._good = 0
        self._bad = 0

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="Open Drone ID (Remote ID)",
            has_scan=False,
            has_azimuth=True,
            has_targets=True,
            has_velocity=True,
            has_elevation=True,
            has_threat=True,
            range_min=1.0,
            range_max=self._range_max,
            az_fov=360.0,
            az_resolution=1.5,
            max_targets=32,
            scan_mode="rotating",
            scan_rate=2.0,
            tier="drone",
            transports=("udp", "tcp", "serial", "file"),
            views=("ppi", "ascope", "waterfall", "rhi", "tracks"),
            notes="支持 ASTM F3411 25B 二进制 Location 报文与 MAVLink/ODID JSON 行流（udp://0.0.0.0:14550）",
        )

    def open(self, **kwargs) -> None:
        self._endpoint = endpoint_of(kwargs, self.default_baud) or "udp://0.0.0.0:14550"
        try:
            opts = parse_spec(self._endpoint)["options"]
            if "lat" in opts:
                self._ref_lat = float(opts["lat"])
            if "lon" in opts:
                self._ref_lon = float(opts["lon"])
            if "rmax" in opts:
                self._range_max = max(50.0, float(opts["rmax"]))
        except Exception:
            pass
        self._tr = open_transport(self._endpoint, default_baud=self.default_baud)
        self._good = 0
        self._bad = 0

    def read(self) -> RadarFrame | None:
        if self._tr is None:
            return None
        chunk = self._tr.read(4096)
        if not chunk:
            time.sleep(0.02)
            return None

        now = time.monotonic()
        targets: list[Target] = []
        s = chunk.lstrip()
        if s.startswith(b"{"):
            for ln in chunk.decode("utf-8", errors="ignore").splitlines():
                ln = ln.strip()
                if not ln.startswith("{"):
                    continue
                try:
                    obj = json.loads(ln)
                except ValueError:
                    self._bad += 1
                    continue
                lat = obj.get("lat")
                lon = obj.get("lon")
                if lat is None or lon is None:
                    continue
                alt = float(obj.get("alt") or obj.get("height") or obj.get("z") or 0.0)
                x, y = _geodetic_xy(float(lat), float(lon), self._ref_lat, self._ref_lon)
                vx = float(obj.get("vx") or 0.0)
                vy = float(obj.get("vy") or 0.0)
                r_xy = math.hypot(x, y)
                if r_xy <= self._range_max * 1.2:
                    targets.append(Target(
                        x=x, y=y, vx=vx, vy=vy, z=alt,
                        el_deg=math.degrees(math.atan2(alt, max(1.0, r_xy))),
                        snr=0.92, threat=1,
                        cls=str(obj.get("uav_id") or obj.get("id") or "UAV"),
                    ))
        elif len(chunk) >= 25:
            for off in range(0, len(chunk) - 24, 25):
                t = parse_odid_frame(chunk[off:off + 25], self._ref_lat, self._ref_lon)
                if t is not None and t.rng <= self._range_max * 1.2:
                    targets.append(t)

        if not targets:
            return None
        self._good += 1
        return RadarFrame(
            timestamp=now,
            targets=targets,
            source=self.label,
            meta={"good_frames": self._good, "bad_frames": self._bad, "odid": True},
        )

    def close(self) -> None:
        if self._tr is not None:
            self._tr.close()
            self._tr = None

    def describe(self) -> str:
        return f"{self.label} ({self._endpoint}) 好帧 {self._good}"
