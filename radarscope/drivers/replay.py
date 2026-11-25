"""回放驱动：把录下来的会话重新播一遍。

两种回放，覆盖不同需求：

1. **帧回放**（本文件）：读 FrameArchive 里的强度矩阵，完整复现 PPI 和瀑布图。
   适合复盘、演示、做教程截图。

2. **字流回放**：直接 `file://capture.bin`。因为传输层是独立的，
   录下来的原始串口字节流可以直接喂给原来的驱动，连协议解析都一起复现。
   那条路不用写代码，改个端点就行。
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from ..core.driver import RadarDriver, register
from ..core.model import Capabilities, RadarFrame
from ..core.storage import FrameArchive


@register
class ReplayDriver(RadarDriver):
    id = "replay"
    label = "回放 · 录制会话"
    simulated = False
    source = "replay"
    form = "sector"

    def __init__(self) -> None:
        self._frames: list[tuple[float, np.ndarray, np.ndarray, np.ndarray]] = []
        self._i = 0
        self._t0 = 0.0
        self._play_t0 = 0.0
        self._speed = 1.0
        self._loop = True
        self._path = ""
        self._caps = Capabilities()

    def capabilities(self) -> Capabilities:
        return self._caps

    def open(self, **kwargs) -> None:
        path = kwargs.get("path") or kwargs.get("endpoint") or ""
        if path.startswith("file://"):
            path = path[7:]
        if not path or not Path(path).exists():
            raise RuntimeError(
                "回放需要指定录制文件：open(path='data/xxx.rad')。\n"
                "录制矩阵的开关在界面上是「录制原始帧」。")
        self._path = path
        self._speed = float(kwargs.get("speed") or 1.0)
        self._loop = str(kwargs.get("loop", "1")) not in ("0", "false", "False")

        self._frames = list(FrameArchive.read_frames(path))
        if not self._frames:
            raise RuntimeError("录制文件里没有可回放的帧")

        ts0, scan, az, r = self._frames[0]
        self._caps = Capabilities(
            name=f"回放 · {Path(path).name}",
            has_scan=True, has_azimuth=az.size > 1, has_targets=False,
            az_fov=360.0 if az.size > 2 else 120.0,
            range_min=float(r.min()), range_max=float(r.max()),
            scan_mode="staring", scan_rate=len(self._frames) / max(
                1e-6, self._frames[-1][0] - ts0 + 1e-3),
            tier="sensor", transports=("file",),
            views=("ppi", "ascope", "waterfall", "tracks"),
            notes=f"{self._path} · {len(self._frames)} 帧",
        )
        self._t0 = ts0
        self._play_t0 = time.monotonic()
        self._i = 0

    def close(self) -> None:
        self._frames = []

    def read(self) -> RadarFrame | None:
        if not self._frames:
            return None
        elapsed = (time.monotonic() - self._play_t0) * self._speed
        want_ts = self._t0 + elapsed

        # 追上时间轴：把该放的帧放出来
        while self._i < len(self._frames) and self._frames[self._i][0] <= want_ts:
            ts, scan, az, r = self._frames[self._i]
            self._i += 1
            if self._i >= len(self._frames):
                if self._loop:
                    self._i = 0
                    self._play_t0 = time.monotonic()
                    self._t0 = self._frames[0][0]
                else:
                    raise RuntimeError("回放结束")
            return RadarFrame(
                timestamp=time.monotonic(),
                scan=scan,
                az_axis=az,
                r_axis=r,
                source=self.label,
                meta={"replay": True},
            )
        time.sleep(0.005)
        return None

    def describe(self) -> str:
        return f"回放 {Path(self._path).name} · {self._i}/{len(self._frames)} 帧"
