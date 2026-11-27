"""ADS-B 航空监视驱动（SBS-1 BaseStation 30003 文本流 & dump1090/readsb aircraft.json）。

将航空器经纬度/气压几何高度按本地参考站投影为 (x, y, z) 米制坐标与地速矢量，
兼容真实 RTL-SDR + dump1090/readsb 接收机与本地回放文件。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass

from ..core.driver import RadarDriver, register
from ..core.model import Capabilities, RadarFrame, Target
from ..core.transport import endpoint_of, open_transport, parse_spec

_FT_TO_M = 0.3048
_KN_TO_MS = 1852.0 / 3600.0
_FPM_TO_MS = 0.00508


@dataclass
class _AcState:
    hex_id: str
    lat: float | None = None
    lon: float | None = None
    alt_m: float = 0.0
    gs_ms: float = 0.0
    track_deg: float = 0.0
    vr_ms: float = 0.0
    callsign: str = ""
    seen: float = 0.0


def _geodetic_to_local(lat: float, lon: float, alt_m: float,
                       ref_lat: float, ref_lon: float) -> tuple[float, float, float]:
    """WGS84 -> ECEF -> ENU 转换（含远距地球曲率垂向修正，x=东, y=北, z=高度）。"""
    from ..core.geodesy import geodetic_to_enu

    return geodetic_to_enu(lat, lon, alt_m, ref_lat, ref_lon, 0.0)


@register
class AdsbDriver(RadarDriver):
    """ADS-B 空管监视驱动（SBS-1 30003 TCP / dump1090 aircraft.json / file）。"""

    id = "adsb"
    label = "ADS-B 航空监视 · SBS-1 / dump1090"
    source = "device"
    form = "omni"
    vendor = "ADS-B / Mode-S"
    default_baud = 115200
    simulated = False

    def __init__(self) -> None:
        self._tr = None
        self._endpoint = ""
        self._ref_lat = 39.9
        self._ref_lon = 116.4
        self._range_max = 100_000.0
        self._buf = ""
        self._ac: dict[str, _AcState] = {}
        self._good = 0
        self._bad = 0

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="ADS-B 空管监视",
            has_scan=False,
            has_azimuth=True,
            has_targets=True,
            has_velocity=True,
            has_elevation=True,
            has_iff=True,
            range_min=100.0,
            range_max=self._range_max,
            az_fov=360.0,
            az_resolution=1.0,
            max_targets=64,
            scan_mode="rotating",
            scan_rate=1.0,
            tier="airborne",
            transports=("tcp", "udp", "file", "serial"),
            views=("ppi", "ascope", "waterfall", "rhi", "tracks"),
            notes="支持 tcp://host:30003?lat=..&lon=..&rmax=100000 接入 dump1090/readsb SBS-1 或 JSON 行流",
        )

    def open(self, **kwargs) -> None:
        self._endpoint = endpoint_of(kwargs, self.default_baud) or "tcp://127.0.0.1:30003"
        try:
            opts = parse_spec(self._endpoint)["options"]
            if "lat" in opts:
                self._ref_lat = float(opts["lat"])
            if "lon" in opts:
                self._ref_lon = float(opts["lon"])
            if "rmax" in opts:
                self._range_max = max(1000.0, float(opts["rmax"]))
        except Exception:
            pass
        self._tr = open_transport(self._endpoint, default_baud=self.default_baud)
        self._buf = ""
        self._ac.clear()
        self._good = 0
        self._bad = 0

    def _ingest_line(self, line: str, now: float) -> bool:
        s = line.strip()
        if not s:
            return False
        if s.startswith("{"):
            try:
                obj = json.loads(s)
            except ValueError:
                self._bad += 1
                return False
            items = obj.get("aircraft") if isinstance(obj, dict) and isinstance(obj.get("aircraft"), list) else [obj]
            hit = False
            for ac in items:
                if not isinstance(ac, dict):
                    continue
                hid = str(ac.get("hex") or ac.get("icao") or "").strip()
                if not hid:
                    continue
                st = self._ac.setdefault(hid, _AcState(hex_id=hid))
                if ac.get("lat") is not None and ac.get("lon") is not None:
                    st.lat = float(ac["lat"])
                    st.lon = float(ac["lon"])
                alt_ft = ac.get("alt_geom") if ac.get("alt_geom") is not None else ac.get("alt_baro")
                if isinstance(alt_ft, (int, float)):
                    st.alt_m = float(alt_ft) * _FT_TO_M
                if ac.get("gs") is not None:
                    st.gs_ms = float(ac["gs"]) * _KN_TO_MS
                if ac.get("track") is not None:
                    st.track_deg = float(ac["track"])
                if ac.get("baro_rate") is not None:
                    st.vr_ms = float(ac["baro_rate"]) * _FPM_TO_MS
                if ac.get("flight"):
                    st.callsign = str(ac["flight"]).strip()
                st.seen = now
                hit = True
            return hit

        if s.startswith("MSG,"):
            f = s.split(",")
            if len(f) < 11:
                self._bad += 1
                return False
            hid = f[4].strip()
            if not hid:
                return False
            st = self._ac.setdefault(hid, _AcState(hex_id=hid))
            st.seen = now
            if f[10].strip():
                st.callsign = f[10].strip()
            if len(f) > 11 and f[11].strip():
                try:
                    st.alt_m = float(f[11]) * _FT_TO_M
                except ValueError:
                    pass
            if len(f) > 12 and f[12].strip():
                try:
                    st.gs_ms = float(f[12]) * _KN_TO_MS
                except ValueError:
                    pass
            if len(f) > 13 and f[13].strip():
                try:
                    st.track_deg = float(f[13])
                except ValueError:
                    pass
            if len(f) > 15 and f[14].strip() and f[15].strip():
                try:
                    st.lat = float(f[14])
                    st.lon = float(f[15])
                except ValueError:
                    pass
            if len(f) > 16 and f[16].strip():
                try:
                    st.vr_ms = float(f[16]) * _FPM_TO_MS
                except ValueError:
                    pass
            return True
        return False

    def read(self) -> RadarFrame | None:
        if self._tr is None:
            return None
        chunk = self._tr.read(4096)
        if not chunk:
            time.sleep(0.02)
            return None
        self._buf += chunk.decode("utf-8", errors="ignore")
        if len(self._buf) > 131072:
            self._buf = self._buf[-65536:]
        if "\n" not in self._buf:
            return None

        lines = self._buf.splitlines(keepends=True)
        if not lines[-1].endswith(("\n", "\r")):
            self._buf = lines.pop()
        else:
            self._buf = ""

        now = time.monotonic()
        any_ok = False
        for ln in lines:
            if self._ingest_line(ln, now):
                self._good += 1
                any_ok = True
        if not any_ok:
            return None

        # 清理 30 秒未更新的过期飞机
        stale = [k for k, v in self._ac.items() if now - v.seen > 30.0]
        for k in stale:
            del self._ac[k]

        targets: list[Target] = []
        for hid, st in self._ac.items():
            if st.lat is None or st.lon is None:
                continue
            x, y, z = _geodetic_to_local(st.lat, st.lon, st.alt_m, self._ref_lat, self._ref_lon)
            r_xy = math.hypot(x, y)
            if r_xy > self._range_max * 1.1:
                continue
            tr_rad = math.radians(st.track_deg)
            vx = st.gs_ms * math.sin(tr_rad)
            vy = st.gs_ms * math.cos(tr_rad)
            el = math.degrees(math.atan2(z, max(1.0, r_xy)))
            targets.append(Target(
                x=x, y=y, vx=vx, vy=vy, z=z, vz=st.vr_ms, el_deg=el,
                snr=0.90, cls=st.callsign or hid, tid=int(hid, 16) % 100000 if all(c in "0123456789abcdefABCDEF" for c in hid) else -1,
            ))

        return RadarFrame(
            timestamp=now,
            targets=targets,
            source=self.label,
            meta={"good_frames": self._good, "bad_frames": self._bad, "aircraft_count": len(targets)},
        )

    def close(self) -> None:
        if self._tr is not None:
            self._tr.close()
            self._tr = None

    def describe(self) -> str:
        return f"{self.label} ({self._endpoint}) 跟踪 {len(self._ac)} 架"
