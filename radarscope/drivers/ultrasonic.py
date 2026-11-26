"""超声波 + 舵机 驱动 —— 用最便宜的器件模拟一台旋转扫描雷达。

下位机（Arduino / STM32）每测一次就发一行 ASCII：
    #<角度>,<距离cm>\\n        例如 #90,142
本驱动自己维护一张 (181 方位 x 80 距离) 的持续累积矩阵，
每收到一行就刷新一个方位列，老数据缓慢衰减 —— 屏幕上就是经典的扫描效果。

配套固件见 firmware/hc_sr04_servo.ino
"""
from __future__ import annotations

import re
import time

import numpy as np

from ..core.driver import RadarDriver, register
from ..core.model import Capabilities, RadarFrame
from ..core.transport import endpoint_of, open_transport

_LINE = re.compile(r"[#A]?\s*(\d{1,3}(?:\.\d+)?)\s*[,:]\s*(?:D:)?\s*(\d{1,4}(?:\.\d+)?)")


@register
class UltrasonicDriver(RadarDriver):
    id = "ultrasonic"
    label = "超声波 + 舵机 (HC-SR04)"
    simulated = False
    source = "device"
    form = "sector"
    vendor = "通用"
    default_baud = 115200

    N_AZ = 181
    N_RNG = 80
    RANGE_MAX = 4.0

    def __init__(self) -> None:
        self._tr = None
        self._scan: np.ndarray | None = None
        self._az = np.radians(np.linspace(-90.0, 90.0, self.N_AZ)).astype(np.float32)
        self._r = np.linspace(0.02, self.RANGE_MAX, self.N_RNG).astype(np.float32)
        self._last = 0
        self._endpoint = ""
        self._last_dist = 0.0
        self._prev_angle: float | None = None
        self._t_wrap = 0.0
        self._rev_s = 0.0

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="HC-SR04 + 舵机",
            has_scan=True, has_azimuth=True, has_targets=False,
            has_velocity=False,
            az_fov=180.0, az_resolution=2.0,
            range_min=0.02, range_max=self.RANGE_MAX,
            scan_mode="rotating", scan_rate=20.0,
            tier="sensor",
            transports=("serial", "tcp", "udp", "file"),
            views=("ppi", "ascope", "waterfall", "tracks"),
            notes="180° 机械扫描，无速度信息",
        )

    def open(self, **kwargs) -> None:
        self._endpoint = endpoint_of(kwargs, 115200)
        if not self._endpoint:
            raise RuntimeError("超声波下位机需要指定端点，例如 serial://COM3?baud=115200")
        self._tr = open_transport(self._endpoint, default_baud=115200)
        self._scan = np.zeros((self.N_AZ, self.N_RNG), dtype=np.float32)
        self._t_wrap = time.monotonic()
        self._prev_angle = None

    def close(self) -> None:
        if self._tr is not None:
            try:
                self._tr.close()
            except Exception:
                pass
        self._tr = None

    def read(self) -> RadarFrame | None:
        if self._tr is None or self._scan is None:
            return None
        raw = self._tr.readline()
        if not raw:
            return None
        line = raw.decode("ascii", errors="ignore").strip()
        m = _LINE.search(line)
        if not m:
            return None

        angle = float(m.group(1))
        dist_cm = float(m.group(2))
        az_idx = int(round((angle / 180.0) * (self.N_AZ - 1)))
        az_idx = max(0, min(self.N_AZ - 1, az_idx))

        # 角度回绕就说明转完一圈，顺便把扫描周期测出来
        now = time.monotonic()
        if self._prev_angle is not None and angle < self._prev_angle - 60.0:
            period = now - self._t_wrap
            self._t_wrap = now
            if 0.3 < period < 120.0:
                self._rev_s = period
        self._prev_angle = angle

        self._scan *= 0.96
        if 2.0 < dist_cm < self.RANGE_MAX * 100.0:
            r0 = dist_cm / 100.0
            prof = np.exp(-0.5 * ((self._r - r0) / 0.10) ** 2)
            self._scan[az_idx, :] = np.maximum(self._scan[az_idx, :], 0.9 * prof)
            self._last_dist = r0

        return RadarFrame(
            timestamp=now,
            scan=self._scan.copy(),
            az_axis=self._az,
            r_axis=self._r,
            source=self.label,
            meta={"fresh_az": (az_idx, az_idx + 1), "angle": angle,
                  "dist_m": self._last_dist,
                  "scan_period": self._rev_s or 4.0},
        )

    def describe(self) -> str:
        return f"{self._endpoint} · 180° 扫描 · 上次 {self._last_dist:.2f} m"
