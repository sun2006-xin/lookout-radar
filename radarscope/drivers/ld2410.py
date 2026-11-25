"""海凌科 LD2410 / LD2410B / LD2410C 驱动。

这颗是"存在感应"雷达，没有方位信息，只能给出 9 个距离门的能量。
本驱动做两件事：
  1. 上电后发配置命令打开工程模式（否则只有 4 个标量，没有门能量）
  2. 把 9 门能量归一化成 (1, 9) 的矩阵，剩下的交给界面的扇形展开

传输方式完全交给 transport 层：USB 转串口、蓝牙 SPP、WiFi 透传都能接，
驱动本身不认识 COM 口，只认识"能读写字节的东西"。
"""
from __future__ import annotations

import time

import numpy as np

from ..core.codec import FrameSpec, StreamDecoder, build_command
from ..core.driver import RadarDriver, register
from ..core.model import Capabilities, RadarFrame
from ..core.transport import endpoint_of, open_transport

_H = b"\xf4\xf3\xf2\xf1"          # 上行数据帧头
_T = b"\xf8\xf7\xf6\xf5"          # 上行数据帧尾
_CMD_H = b"\xfd\xfc\xfb\xfa"      # 下行命令帧头
_CMD_T = b"\x04\x03\x02\x01"      # 下行命令帧尾

_STATE_TEXT = {0: "无目标", 1: "运动", 2: "静止", 3: "运动+静止"}


@register
class LD2410Driver(RadarDriver):
    id = "ld2410"
    label = "海凌科 LD2410 (存在感应)"
    simulated = False
    source = "device"
    form = "point"
    vendor = "海凌科"

    GATES = 9

    def __init__(self) -> None:
        self._tr = None
        self._dec: StreamDecoder | None = None
        self._cmd_dec: StreamDecoder | None = None
        self._resolution = 0.75          # 每个距离门代表的米数
        self._engineering = False
        self._last_state = "未连接"
        self._endpoint = ""

    # ------------------------------------------------------------------ #
    def capabilities(self) -> Capabilities:
        rmax = self._resolution * (self.GATES + 1)
        return Capabilities(
            name="LD2410",
            has_scan=True, has_azimuth=False, has_targets=False,
            has_velocity=False,
            az_fov=120.0,                # 单波束，界面按 ±60° 摊成扇形
            range_min=self._resolution,
            range_max=rmax,
            scan_mode="staring", scan_rate=10.0,
            tier="sensor",
            transports=("serial", "ble", "tcp", "udp", "file"),
            views=("ppi", "ascope", "waterfall", "tracks"),
            notes="无方位信息，显示为扇形能量带",
        )

    # ------------------------------------------------------------------ #
    def _cmd(self, word: int, payload: bytes, wait_ack: bool = True) -> bool:
        if self._tr is None:
            return False
        self._tr.write(build_command(_CMD_H, word.to_bytes(2, "little") + payload, _CMD_T))
        if not wait_ack:
            return True
        # 命令 ACK：FD FC FB FA | len | word | status | 04 03 02 01
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            raw = self._tr.read(64)
            if not raw:
                continue
            for fr in self._cmd_dec.feed(raw):
                if fr[6:8] == word.to_bytes(2, "little"):
                    return fr[8:10] == b"\x00\x00"
        return False

    def open(self, **kwargs) -> None:
        self._endpoint = endpoint_of(kwargs, 256000)
        if not self._endpoint:
            raise RuntimeError(
                "LD2410 需要指定端点。例如：\n"
                "  serial://COM3?baud=256000      USB 转串口\n"
                "  serial://COM4?baud=256000      HC-05 蓝牙模块（系统里是串口）\n"
                "  tcp://192.168.4.1:8080         ESP32 WiFi 透传"
            )
        self._tr = open_transport(self._endpoint, default_baud=256000)
        self._dec = StreamDecoder(FrameSpec(header=_H, tail=_T))
        self._cmd_dec = StreamDecoder(
            FrameSpec(header=_CMD_H, tail=_CMD_T)
        )
        time.sleep(0.1)
        try:
            self._tr.read(4096)                     # 清掉上电残留
        except Exception:
            pass

        # 1) 使能配置
        if not self._cmd(0x00FF, b"\x01\x00"):
            self.close()
            raise RuntimeError("LD2410 使能配置失败：检查端点、波特率（必须 256000）、模块供电")
        # 2) 查询距离分辨率（0.75m 或 0.2m）
        self._cmd(0x00AB, b"", wait_ack=False)
        # 3) 打开工程模式，拿到 9 门能量
        self._engineering = self._cmd(0x0062, b"")
        # 4) 结束配置
        self._cmd(0x00FE, b"")
        self._last_state = "已连接·工程模式" if self._engineering else "已连接·仅基本信息"

    def close(self) -> None:
        if self._tr is not None:
            try:
                self._cmd(0x0063, b"", wait_ack=False)   # 关工程模式
                self._cmd(0x00FE, b"", wait_ack=False)
            except Exception:
                pass
            try:
                self._tr.close()
            except Exception:
                pass
        self._tr = None

    # ------------------------------------------------------------------ #
    def read(self) -> RadarFrame | None:
        if self._tr is None or self._dec is None:
            return None
        raw = self._tr.read(128)
        if not raw:
            return None

        frame = None
        for fr in self._dec.feed(raw):
            frame = self._parse(fr) or frame
        return frame

    def _parse(self, fr: bytes) -> RadarFrame | None:
        if len(fr) < 17 or fr[7] != 0xAA:
            return None
        ftype = fr[6]
        state = fr[8]
        moving_cm = int.from_bytes(fr[9:11], "little")
        moving_e = fr[11]
        static_cm = int.from_bytes(fr[12:14], "little")
        static_e = fr[14]
        detect_cm = int.from_bytes(fr[15:17], "little")

        gates = np.zeros(self.GATES, dtype=np.float32)
        if ftype == 0x01 and self._engineering and len(fr) >= 37:
            # 17: 最大运动门, 18: 最大静止门, 19..27 运动门能量, 28..36 静止门能量
            motion = np.frombuffer(fr[19:28], dtype=np.uint8).astype(np.float32)
            static = np.frombuffer(fr[28:37], dtype=np.uint8).astype(np.float32)
            gates = np.maximum(motion, static) / 100.0
        else:
            # 没开工程模式：只有 4 个标量，摊到对应距离门上，够用但不精确
            gi = int(moving_cm / 100.0 / self._resolution)
            if 0 <= gi < self.GATES:
                gates[gi] = max(gates[gi], moving_e / 100.0)
            gi = int(static_cm / 100.0 / self._resolution)
            if 0 <= gi < self.GATES:
                gates[gi] = max(gates[gi], static_e / 100.0)

        r_axis = (np.arange(self.GATES, dtype=np.float32) + 1.0) * self._resolution
        self._last_state = _STATE_TEXT.get(state, "未知")
        return RadarFrame(
            timestamp=time.monotonic(),
            scan=gates.reshape(1, -1),
            az_axis=np.zeros(1, dtype=np.float32),
            r_axis=r_axis,
            targets=[],
            source=self.label,
            meta={
                "state": self._last_state,
                "moving_m": moving_cm / 100.0,
                "static_m": static_cm / 100.0,
                "detect_m": detect_cm / 100.0,
                "engineering": self._engineering,
                "fresh_az": (0, 1),
            },
        )

    def describe(self) -> str:
        return f"{self._endpoint} · {self._last_state}"
