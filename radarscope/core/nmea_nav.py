"""航海 / 移动平台 NMEA-0183 本船定位与航向解析（ROADMAP E6）。

支持标准报文：
    $GPGGA / $GNGGA ：经纬度定位与定位状态
    $GPRMC / $GNRMC ：经纬度、对地航速 (knots -> m/s) 与航迹向
    $HEHDT / $GPHDT / $GNHDT ：真航向（陀螺罗经 / GNSS 双天线）
    $RARSD ：航海雷达系统光标/量程数据

纯标准库 + 后台 UDP 监听线程，零 Qt 依赖。
"""
from __future__ import annotations

import socket
import threading
import time

from .custom_proto import nmea_checksum_ok

_KN_TO_MS = 1852.0 / 3600.0


def _parse_dm(val: str, hemi: str) -> float | None:
    """将 NMEA ddmm.mmmm / dddmm.mmmm + N/S/E/W 转为十进制度。"""
    s = (val or "").strip()
    h = (hemi or "").strip().upper()
    if not s or "." not in s or h not in ("N", "S", "E", "W"):
        return None
    try:
        dot = s.index(".")
        deg_len = 2 if h in ("N", "S") else 3
        if dot < deg_len + 1:
            return None
        deg = float(s[:deg_len])
        minutes = float(s[deg_len:])
    except ValueError:
        return None
    out = deg + minutes / 60.0
    if h in ("S", "W"):
        out = -out
    return out


class NmeaOwnShip:
    """本船实时位置、航向与航速状态维护器（支持逐行喂入与 UDP 监听）。"""

    def __init__(self) -> None:
        self.port = 10111
        self.running = False
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.lat: float | None = None
        self.lon: float | None = None
        self.heading_deg: float | None = None
        self.speed_ms: float | None = None
        self.fix_quality: int = 0
        self.rsd_range_m: float | None = None
        self.rsd_bearing_deg: float | None = None
        self.received: int = 0
        self.errors: int = 0
        self.updated_ts: float = 0.0
        self._has_hdt: bool = False

    def start(self, port: int | None = None) -> str:
        if self.running:
            return ""
        if port:
            self.port = int(port)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.port))
            s.settimeout(0.5)
        except OSError as exc:
            return f"监听本船 NMEA UDP {self.port} 失败：{exc}"
        self._sock = s
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="nmea-ownship", daemon=True)
        self._thread.start()
        return ""

    def stop(self) -> None:
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _loop(self) -> None:
        buf = b""
        while self.running and self._sock is not None:
            try:
                data, _ = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.feed_line(line.decode("ascii", "ignore"))
            if len(buf) > 8192:
                buf = b""

    def feed_line(self, line: str) -> bool:
        """解析一行 NMEA 定位/航向语句；有效更新返回 True。"""
        s = (line or "").strip()
        if not s or not s.startswith("$") or not nmea_checksum_ok(s):
            if s.startswith("$"):
                self.errors += 1
            return False
        if "*" in s:
            s = s.split("*", 1)[0]
        parts = s.split(",")
        talker_type = parts[0][1:].upper()
        stmt = talker_type[-3:]

        with self._lock:
            if stmt == "GGA" and len(parts) >= 7:
                lat = _parse_dm(parts[2], parts[3])
                lon = _parse_dm(parts[4], parts[5])
                if lat is None or lon is None:
                    return False
                try:
                    q = int(parts[6] or "0")
                except ValueError:
                    q = 0
                if q <= 0:
                    return False
                self.lat, self.lon, self.fix_quality = lat, lon, q
            elif stmt == "RMC" and len(parts) >= 9:
                if (parts[2] or "").upper() != "A":
                    return False
                lat = _parse_dm(parts[3], parts[4])
                lon = _parse_dm(parts[5], parts[6])
                if lat is None or lon is None:
                    return False
                self.lat, self.lon = lat, lon
                try:
                    if parts[7]:
                        self.speed_ms = max(0.0, float(parts[7]) * _KN_TO_MS)
                    if parts[8] and not self._has_hdt:
                        self.heading_deg = float(parts[8]) % 360.0
                except ValueError:
                    pass
            elif stmt == "HDT" and len(parts) >= 2:
                try:
                    hdg = float(parts[1]) % 360.0
                except ValueError:
                    return False
                self.heading_deg = hdg
                self._has_hdt = True
            elif stmt == "RSD" and len(parts) >= 12:
                try:
                    brg = float(parts[10]) % 360.0 if parts[10] else None
                    rng = float(parts[9]) if parts[9] else None
                    unit = (parts[12] if len(parts) > 12 else "N").upper()
                    if rng is not None:
                        scale = {"N": 1852.0, "K": 1000.0, "M": 1.0}.get(unit, 1852.0)
                        self.rsd_range_m = rng * scale
                    if brg is not None:
                        self.rsd_bearing_deg = brg
                except ValueError:
                    return False
            else:
                return False

            self.received += 1
            self.updated_ts = time.monotonic()
            return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "lat": self.lat,
                "lon": self.lon,
                "heading_deg": self.heading_deg,
                "speed_ms": self.speed_ms,
                "fix_quality": self.fix_quality,
                "rsd_range_m": self.rsd_range_m,
                "rsd_bearing_deg": self.rsd_bearing_deg,
                "received": self.received,
                "updated_ts": self.updated_ts,
            }
