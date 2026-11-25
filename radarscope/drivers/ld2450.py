"""海凌科 LD2450 驱动 —— 真正能出二维坐标的一颗，约定最"雷达"。

帧：AA FF 03 00 | 目标1..3 各 8 字节 | 55 CC，定长 30 字节，10Hz。
每个目标 = X(int16) Y(int16) speed(int16) resolution(uint16)，单位 mm / cm/s。

这里有个反直觉的地方，坑过很多人：
坐标的符号不是补码！bit15 单独表示正负（1=正，0=负），低 15 位才是绝对值。
按 int16 直接转会得到一堆几万毫米的鬼数字。
"""
from __future__ import annotations

import time

from ..core.codec import FrameSpec, StreamDecoder
from ..core.driver import RadarDriver, register
from ..core.model import Capabilities, RadarFrame, Target
from ..core.transport import endpoint_of, open_transport


def _decode_sign_magnitude(raw: int) -> int:
    """bit15 为符号位（1 正 0 负），低 15 位为绝对值。"""
    mag = raw & 0x7FFF
    return mag if (raw & 0x8000) else -mag


@register
class LD2450Driver(RadarDriver):
    id = "ld2450"
    label = "海凌科 LD2450 (三目标跟踪)"
    simulated = False
    source = "device"
    form = "sector"
    vendor = "海凌科"

    def __init__(self) -> None:
        self._tr = None
        self._dec: StreamDecoder | None = None
        self._endpoint = ""

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="LD2450",
            has_scan=False, has_azimuth=True, has_targets=True,
            has_velocity=True,
            az_fov=120.0, az_resolution=2.0, range_min=0.3, range_max=8.0,
            max_targets=3, scan_mode="staring", scan_rate=10.0,
            tier="tracking",
            transports=("serial", "ble", "tcp", "udp", "file"),
            views=("ppi", "ascope", "waterfall", "tracks"),
            notes="直接给出 x/y 坐标与速度，矩阵由框架反投影合成",
        )

    def open(self, **kwargs) -> None:
        self._endpoint = endpoint_of(kwargs, 256000)
        if not self._endpoint:
            raise RuntimeError(
                "LD2450 需要指定端点，例如 serial://COM3?baud=256000 或 tcp://192.168.4.1:8080")
        self._tr = open_transport(self._endpoint, default_baud=256000)
        self._dec = StreamDecoder(
            FrameSpec(header=b"\xaa\xff\x03\x00", tail=b"\x55\xcc", fixed_total=30)
        )

    def close(self) -> None:
        if self._tr is not None:
            try:
                self._tr.close()
            except Exception:
                pass
        self._tr = None

    def read(self) -> RadarFrame | None:
        if self._tr is None or self._dec is None:
            return None
        raw = self._tr.read(64)
        if not raw:
            return None
        frame = None
        for fr in self._dec.feed(raw):
            frame = self._parse(fr) or frame
        return frame

    def _parse(self, fr: bytes) -> RadarFrame | None:
        if len(fr) < 30:
            return None
        targets: list[Target] = []
        for k in range(3):
            off = 4 + k * 8
            xr = int.from_bytes(fr[off:off + 2], "little")
            yr = int.from_bytes(fr[off + 2:off + 4], "little")
            sr = int.from_bytes(fr[off + 4:off + 6], "little")
            if xr == 0 and yr == 0:
                continue                      # 该槽位无目标
            x_mm = _decode_sign_magnitude(xr)
            y_mm = _decode_sign_magnitude(yr)
            v_cms = _decode_sign_magnitude(sr)
            # 速度只给"接近/远离"的径向分量，正值为接近 -> 折算到 -y 方向
            targets.append(
                Target(
                    x=x_mm / 1000.0,
                    y=y_mm / 1000.0,
                    vx=0.0,
                    vy=-v_cms / 100.0,
                    snr=1.0,
                )
            )
        return RadarFrame(
            timestamp=time.monotonic(),
            targets=targets,
            source=self.label,
            meta={"n": len(targets)},
        )

    def describe(self) -> str:
        return f"{self._endpoint} · 三目标跟踪"
