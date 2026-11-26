"""模拟电子站 / 动态虚拟目标引擎（纯软件仿真注入）。

用途：
  1. 没接真雷达时：提供动态进出、高速逼近、交叉会遇等丰富场景，不再只是固定位置绕圈；
  2. 连着任意雷达（甚至真机）时：可在画面上额外注入虚拟移动目标，直接用来验证
     告警区、CPA 防撞、瀑布图、转发协议与航迹跟踪是否灵敏。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

import numpy as np

from .model import Capabilities, RadarFrame, Target

SCENARIOS = {
    "none": "关闭场景自动生成（仅保留手动注入）",
    "patrol": "随机出现与巡逻（动态生成/离开）",
    "approach": "高速逼近 / 防撞告警测试",
    "crossing": "多目标交叉会遇（测航迹关联）",
}


@dataclass(slots=True)
class SimObject:
    """一个在平面上运动的虚拟电子目标。"""

    id: int
    name: str
    x: float
    y: float
    vx: float
    vy: float
    amp: float = 0.88
    respawn: bool = True


class ElectronicStation:
    """动态虚拟目标管理与帧叠加器。"""

    def __init__(self, seed: int = 2026) -> None:
        self._rng = random.Random(seed)
        self._seq = 0
        self.scenario: str = "none"
        self.objects: list[SimObject] = []
        self.speed_scale: float = 1.0

    def active(self) -> bool:
        return self.scenario != "none" or bool(self.objects)

    def set_scenario(self, scenario: str, range_max: float = 8.0) -> None:
        self.scenario = scenario if scenario in SCENARIOS else "none"
        self.objects.clear()
        rmax = max(2.0, float(range_max))
        if self.scenario == "patrol":
            for _ in range(3):
                self.spawn_random(rmax, respawn=True)
        elif self.scenario == "approach":
            self.inject(-0.4, rmax * 0.85, 0.05, -0.55, amp=0.92, name="逼近目标-1", respawn=True)
            self.inject(rmax * 0.45, rmax * 0.72, -0.30, -0.40, amp=0.82, name="逼近目标-2", respawn=True)
        elif self.scenario == "crossing":
            self.inject(-rmax * 0.55, rmax * 0.45, 0.45, 0.05, amp=0.88, name="横穿-A", respawn=True)
            self.inject(rmax * 0.55, rmax * 0.48, -0.45, -0.04, amp=0.86, name="横穿-B", respawn=True)
            self.inject(0.1, rmax * 0.30, 0.10, 0.25, amp=0.78, name="纵向-C", respawn=True)

    def inject(
        self,
        x: float,
        y: float,
        vx: float,
        vy: float,
        amp: float = 0.88,
        name: str = "",
        respawn: bool = False,
    ) -> SimObject:
        self._seq += 1
        obj = SimObject(
            id=self._seq,
            name=name or f"虚拟-{self._seq}",
            x=float(x),
            y=float(y),
            vx=float(vx),
            vy=float(vy),
            amp=max(0.2, min(1.0, float(amp))),
            respawn=respawn,
        )
        self.objects.append(obj)
        return obj

    def spawn_random(self, range_max: float = 8.0, respawn: bool = True) -> SimObject:
        """从雷达覆盖边缘随机生成一个向场内移动的虚拟物体。"""
        rmax = max(2.0, float(range_max))
        ang = self._rng.uniform(-math.pi * 0.85, math.pi * 0.85)
        r0 = self._rng.uniform(rmax * 0.45, rmax * 0.88)
        x = r0 * math.sin(ang)
        y = max(0.6, abs(r0 * math.cos(ang)))
        spd = self._rng.uniform(0.18, 0.65)
        heading = ang + math.pi + self._rng.uniform(-0.85, 0.85)
        vx = spd * math.sin(heading)
        vy = spd * math.cos(heading)
        amp = self._rng.uniform(0.55, 0.95)
        return self.inject(x, y, vx, vy, amp=amp, respawn=respawn)

    def clear(self) -> None:
        self.scenario = "none"
        self.objects.clear()

    def step(self, dt: float, range_max: float) -> list[Target]:
        """推进所有虚拟目标一个时间步长，边界外自动折返或重生。"""
        if not self.objects:
            return []
        dt = max(0.0, min(0.5, float(dt))) * self.speed_scale
        rmax = max(2.0, float(range_max))
        kept: list[SimObject] = []
        for o in self.objects:
            o.x += o.vx * dt
            o.y += o.vy * dt
            r = math.hypot(o.x, o.y)
            if r > rmax * 0.96 or r < 0.25:
                if o.respawn:
                    # 离开视野后从边缘重新进入，模拟连续来往目标
                    ang = self._rng.uniform(-math.pi * 0.75, math.pi * 0.75)
                    r_new = rmax * self._rng.uniform(0.65, 0.88)
                    o.x = r_new * math.sin(ang)
                    o.y = max(0.6, r_new * math.cos(ang))
                    spd = math.hypot(o.vx, o.vy) or 0.35
                    in_ang = math.atan2(-o.x, -o.y) + self._rng.uniform(-0.55, 0.55)
                    o.vx = spd * math.sin(in_ang)
                    o.vy = spd * math.cos(in_ang)
                    kept.append(o)
                else:
                    # 手动注入的目标碰到边界时折返，方便反复观察
                    o.vx = -o.vx
                    o.vy = -o.vy
                    o.x = max(-rmax * 0.9, min(rmax * 0.9, o.x))
                    o.y = max(0.3, min(rmax * 0.9, o.y))
                    kept.append(o)
            else:
                kept.append(o)
        self.objects = kept
        return [
            Target(x=o.x, y=o.y, vx=o.vx, vy=o.vy, snr=o.amp)
            for o in self.objects
        ]

    def merge_into_frame(self, frame: RadarFrame, caps: Capabilities, dt: float) -> RadarFrame:
        """将活跃虚拟目标叠加到 RadarFrame 的目标列表与强度矩阵中。"""
        v_targets = self.step(dt, caps.range_max)
        if not v_targets:
            return frame

        frame.targets = list(frame.targets) + v_targets
        if frame.has_scan and frame.az_axis is not None and frame.r_axis is not None:
            scan = frame.scan.astype(np.float32, copy=True)
            sigma_r = max(0.10, (caps.range_max - caps.range_min) / max(1, frame.r_axis.size) * 1.2)
            sigma_a = math.radians(3.5)
            for t in v_targets:
                r0 = t.rng
                if r0 < caps.range_min * 0.5 or r0 > caps.range_max * 1.02:
                    continue
                prof_r = np.exp(-0.5 * ((frame.r_axis - r0) / sigma_r) ** 2).astype(np.float32)
                if frame.az_axis.size == 1:
                    scan[0] = np.maximum(scan[0], t.snr * prof_r)
                else:
                    da = (frame.az_axis - t.az + math.pi) % (2 * math.pi) - math.pi
                    prof_a = np.exp(-0.5 * (da / sigma_a) ** 2).astype(np.float32)
                    scan = np.maximum(scan, t.snr * np.outer(prof_a, prof_r))
            frame.scan = np.clip(scan, 0.0, 1.0)
        return frame
