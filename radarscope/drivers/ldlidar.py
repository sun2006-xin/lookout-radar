"""LDROBOT LD19 / LD06 / D500 系列旋转式激光雷达。

这一类是"全向 360°"形态的代表：单线激光，一圈 360°，每转一圈出几千个点。
和 24GHz 模块的区别在于——它给的是**每度都有的距离采样**，
所以能直接铺成一张环视图，不需要扇区拼接。

协议（47 字节定长帧，230400 8N1，4800 转/分以内）：

    偏移  长度  含义
    0     1     帧头 0x54
    1     1     点数标志 0x2C（=12 个点）
    2     2     转速 deg/s，小端
    4     2     起始角，0.01°，小端
    6     36    12 × (距离 u16 mm 小端, 置信度 u8)
    42    2     结束角，0.01°，小端
    44    2     时间戳 ms
    46    1     CRC8（多项式 0x4D，初值 0，MSB first，覆盖 0~45 字节）

两个必须处理的坑：

1. **跨 0° 时结束角小于起始角**。一帧里如果正好跨过正前方，
   `end < start` 直接相减会得到负数，整帧 12 个点全错位，
   表现为"每转一圈就在某个方向错一片"。正确做法：
   `diff = (end + 36000 - start) % 36000`。

2. **CRC 必须校验**。这类模块在供电不足或线太长时会持续吐坏帧，
   不校验就会把噪声当成墙，画面上出现一圈随机弧。
"""

from __future__ import annotations

import time

import numpy as np

from ..core.driver import RadarDriver, register
from ..core.model import Capabilities, RadarFrame
from ..core.transport import endpoint_of, open_transport

PACKET_LEN = 47
HEADER = 0x54
POINTS = 12
DEG = 0.01          # 角度单位：0.01 度


def crc8(data: bytes, poly: int = 0x4D, init: int = 0x00) -> int:
    """CRC-8/MAXIM 变体：多项式 0x4D，MSB first，不反射、不异或输出。"""
    crc = init
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def parse_packet(buf: bytes) -> tuple[list[tuple[float, float, int]], dict] | None:
    """解析一帧，返回 ([(角度°, 距离m, 置信度)], 统计信息)。

    单独抽成函数是为了能脱离串口做单元测试 —— 协议解析必须可测，
    否则接上真设备前根本不知道对不对。
    """
    if len(buf) != PACKET_LEN or buf[0] != HEADER or buf[1] != 0x2C:
        return None
    if crc8(buf[:46]) != buf[46]:
        return None                                     # 坏帧，直接丢

    speed = int.from_bytes(buf[2:4], "little")          # deg/s
    start = int.from_bytes(buf[4:6], "little")          # 0.01°
    end = int.from_bytes(buf[42:44], "little")          # 0.01°

    # 关键：跨 0° 时 end < start，必须加一圈再取模
    span = (end + 36000 - start) % 36000
    step = span / (POINTS - 1) if POINTS > 1 else 0.0

    pts: list[tuple[float, float, int]] = []
    for i in range(POINTS):
        off = 6 + i * 3
        dist_mm = int.from_bytes(buf[off:off + 2], "little")
        conf = buf[off + 2]
        ang = (start + step * i) / 100.0                # 度
        pts.append((ang % 360.0, dist_mm / 1000.0, conf))

    return pts, {"speed_dps": speed, "span_deg": span / 100.0}


@register
class LDLidarDriver(RadarDriver):
    id = "ldlidar"
    label = "LDROBOT LD19/LD06 (360° 激光)"
    simulated = False
    source = "device"
    form = "omni"
    vendor = "LDROBOT"
    default_baud = 230400

    N_AZ = 360
    N_RNG = 240
    RANGE_MIN = 0.02
    RANGE_MAX = 12.0

    def __init__(self) -> None:
        self._tr = None
        self._endpoint = ""
        self._buf = bytearray()
        self._acc = np.zeros((self.N_AZ, self.N_RNG), dtype=np.float32)
        self._t_last = 0.0
        self._rate = 12.0
        self._bad = 0
        self._good = 0
        self._speed = 0.0

    # ------------------------------------------------------------------ #
    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="LD19 / LD06",
            source="device", form="omni", vendor="LDROBOT",
            has_scan=True, has_azimuth=True, has_targets=False,
            has_velocity=False,
            az_fov=360.0, az_resolution=360.0 / self.N_AZ,
            range_min=self.RANGE_MIN, range_max=self.RANGE_MAX,
            scan_mode="rotating", scan_rate=self._rate,
            tier="sensor",
            transports=("serial", "tcp", "udp", "file"),
            views=("ppi", "ascope", "waterfall", "tracks"),
            notes="单线激光 360°，无速度信息；玻璃/镜面测距会失真",
        )

    def open(self, **kwargs) -> None:
        self._endpoint = endpoint_of(kwargs, 230400)
        if not self._endpoint:
            raise RuntimeError(
                "LD19/LD06 需要指定端点，例如 serial://COM5?baud=230400\n"
                "注意波特率是 230400，不是 256000；供电必须 5V（3.3V 带不动电机）。")
        self._tr = open_transport(self._endpoint, default_baud=230400)
        self._buf.clear()
        self._acc.fill(0.0)
        self._t_last = 0.0

    def close(self) -> None:
        if self._tr is not None:
            try:
                self._tr.close()
            except Exception:
                pass
        self._tr = None

    # ------------------------------------------------------------------ #
    def _ingest(self, raw: bytes) -> None:
        """按字节流切帧。47 字节定长 + 双字节同步头，碎片/丢字节都能自恢复。"""
        self._buf.extend(raw)
        az_step = self.N_AZ / 360.0
        rng_span = max(1e-6, self.RANGE_MAX - self.RANGE_MIN)
        t = time.monotonic()

        while True:
            # 找同步头
            while len(self._buf) >= 2 and not (self._buf[0] == HEADER
                                               and self._buf[1] == 0x2C):
                del self._buf[0]
            if len(self._buf) < PACKET_LEN:
                return
            pkt = bytes(self._buf[:PACKET_LEN])
            got = parse_packet(pkt)
            if got is None:
                self._bad += 1
                del self._buf[0]                        # 错位，丢一字节重同步
                continue
            del self._buf[:PACKET_LEN]
            self._good += 1

            pts, stat = got
            self._speed = stat["speed_dps"]
            self._acc *= 0.92                            # 余晖：一圈约 0.5 s
            for ang, dist, conf in pts:
                if dist <= 0.0 or conf == 0:
                    continue
                if dist < self.RANGE_MIN or dist > self.RANGE_MAX:
                    continue
                ia = int((ang + 180.0) % 360.0 * az_step) % self.N_AZ
                ir = int((dist - self.RANGE_MIN) / rng_span * (self.N_RNG - 1))
                ir = max(0, min(self.N_RNG - 1, ir))
                v = min(1.0, 0.35 + conf / 255.0 * 0.65)
                if v > self._acc[ia, ir]:
                    self._acc[ia, ir] = v
            self._t_last = t

    # ------------------------------------------------------------------ #
    def read(self) -> RadarFrame | None:
        if self._tr is None:
            return None
        raw = self._tr.read(4096)              # 高波特率下一次可能来好几帧
        if raw:
            self._ingest(raw)

        now = time.monotonic()
        if now - self._t_last > 1.0:
            return None
        if now - getattr(self, "_t_emit", 0.0) < 1.0 / self._rate:
            return None
        self._t_emit = now

        az = np.radians(np.linspace(-180.0, 180.0, self.N_AZ,
                                    endpoint=False)).astype(np.float32)
        r = np.linspace(self.RANGE_MIN, self.RANGE_MAX, self.N_RNG).astype(np.float32)
        return RadarFrame(
            timestamp=now,
            scan=self._acc.copy(),
            az_axis=az,
            r_axis=r,
            source=self.label,
            meta={
                "scan_period": 1.0 / self._rate,
                "speed_rpm": round(self._speed / 6.0, 1),
                "bad": self._bad,
                "good": self._good,
            },
        )

    def describe(self) -> str:
        return (f"{self._endpoint} · {self._speed / 6.0:.0f} rpm · "
                f"好帧 {self._good} / 坏帧 {self._bad}")
