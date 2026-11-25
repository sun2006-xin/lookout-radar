"""模拟器 · 相控阵 / 脉冲多普勒雷达。

存在的意义是回答一个问题：**这套架构会不会只围着几十块的模块转？**

这台虚拟雷达提供的是高端机才有的东西：
    * 电子扫描 360°，无机械惯性
    * 30 个目标同时跟踪（TWS）
    * 距离-多普勒图（相干积累的直接产物，感应模块永远拿不到）
    * 目标 RCS、径向速度、IFF 敌我识别、威胁分级
    * 20 km 量程

把它接进来，界面会自动多出 RD 面板、威胁列、分类列 —— 一行 UI 代码都不改。
这就是"能力声明驱动界面"的价值所在。
"""
from __future__ import annotations

import time
from math import atan2, cos, degrees, exp, hypot, radians, sin

import numpy as np

from ..core.driver import RadarDriver, register
from ..core.model import Capabilities, RadarFrame, Target


class _Air:
    __slots__ = ("x", "y", "vx", "vy", "rcs", "iff")

    def __init__(self, x, y, vx, vy, rcs, iff):
        self.x, self.y, self.vx, self.vy = x, y, vx, vy
        self.rcs, self.iff = rcs, iff


@register
class AesaSimDriver(RadarDriver):
    id = "sim_aesa"
    label = "模拟器 · 相控阵/脉冲多普勒"
    simulated = True
    source = "sim"
    form = "rd"

    N_AZ = 360
    N_RNG = 240
    N_DOP = 64
    RANGE_MAX = 20000.0
    V_MAX = 300.0

    def __init__(self) -> None:
        self._rate = 12.0
        self._rng = np.random.default_rng(20261125)
        self._opened = False
        self._az: np.ndarray | None = None
        self._r: np.ndarray | None = None
        self._v: np.ndarray | None = None
        self._scan: np.ndarray | None = None
        self._targets: list[_Air] = []
        self._t_last = 0.0
        self._mode = "TWS"

    # ------------------------------------------------------------------ #
    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="相控阵 / 脉冲多普勒",
            has_scan=True, has_azimuth=True, has_targets=True,
            has_velocity=True, has_range_doppler=True,
            has_iff=True, has_threat=True,
            az_fov=360.0, az_resolution=1.0,
            range_min=100.0, range_max=self.RANGE_MAX,
            max_targets=30, scan_mode="staring", scan_rate=self._rate,
            tier="aesa", transports=("virtual",),
            views=("ppi", "ascope", "waterfall", "rd", "tracks"),
            notes="电子扫描 · 距离-多普勒 · IFF · 威胁分级",
        )

    # ------------------------------------------------------------------ #
    def open(self, **kwargs) -> None:
        self._mode = kwargs.get("mode") if kwargs.get("mode") in ("TWS", "STT") else "TWS"
        self._az = np.radians(np.linspace(-180.0, 180.0, self.N_AZ, endpoint=False)).astype(np.float32)
        self._r = np.linspace(self.capabilities().range_min, self.RANGE_MAX, self.N_RNG).astype(np.float32)
        self._v = np.linspace(-self.V_MAX, self.V_MAX, self.N_DOP).astype(np.float32)
        self._scan = np.zeros((self.N_AZ, self.N_RNG), dtype=np.float32)

        rng = self._rng
        self._targets = []
        kinds = [("敌", 3), ("未知", 4), ("友", 2)]
        for name, count in kinds:
            for _ in range(count):
                brg = rng.uniform(-np.pi, np.pi)
                dist = rng.uniform(3000.0, 18000.0)
                spd = rng.uniform(40.0, 260.0)
                head = brg + np.pi + rng.normal(0, 0.6)      # 大致朝雷达飞
                self._targets.append(_Air(
                    dist * sin(brg), dist * cos(brg),
                    spd * sin(head), spd * cos(head),
                    float(rng.uniform(0.5, 18.0)), name))

        self._t_last = time.monotonic()
        self._opened = True

    def close(self) -> None:
        self._opened = False

    # ------------------------------------------------------------------ #
    def _threat_of(self, a: _Air) -> int:
        """粗略的威胁评估：越近、接近率越大、越像敌机，等级越高。
        真实系统会用交战时间、爬升率、机动模式等一堆量，这里给个能看的最小实现。"""
        r = hypot(a.x, a.y)
        closing = -((a.x * a.vx + a.y * a.vy) / max(r, 1.0))   # 正=接近
        if a.iff == "敌":
            if r < 4000 or (r < 9000 and closing > 150):
                return 3
            if r < 12000:
                return 2
            return 1
        if a.iff == "未知":
            return 2 if r < 6000 else 1
        return 0

    def _rebuild(self, dt: float) -> None:
        assert self._az is not None and self._r is not None and self._scan is not None
        assert self._v is not None

        # 目标运动
        for a in self._targets:
            a.x += a.vx * dt
            a.y += a.vy * dt
            r = hypot(a.x, a.y)
            if r > self.RANGE_MAX * 1.15 or r < 800.0:
                brg = atan2(a.x, a.y) + np.pi + self._rng.normal(0, 0.8)
                d = self.RANGE_MAX * 0.95
                spd = hypot(a.vx, a.vy)
                a.x, a.y = d * sin(brg), d * cos(brg)
                head = brg + np.pi + self._rng.normal(0, 0.5)
                a.vx, a.vy = spd * sin(head), spd * cos(head)

        # 余晖：电子扫描没有机械拖尾，衰减快一点
        self._scan *= 0.88

        sigma_a = radians(1.2)
        sigma_r = self.RANGE_MAX / self.N_RNG * 0.9

        rd = np.zeros((self.N_DOP, self.N_RNG), dtype=np.float32)
        rd += self._rng.normal(0.01, 0.006, size=rd.shape).astype(np.float32)

        for a in self._targets:
            r = hypot(a.x, a.y)
            if r < 500 or r > self.RANGE_MAX:
                continue
            az = atan2(a.x, a.y)
            vr = (a.x * a.vx + a.y * a.vy) / max(r, 1e-6)      # 径向速度，正=远离
            # RCS 决定回波强度（4 次方根关系，做个压缩）
            amp = float(min(1.0, 0.35 + 0.16 * (a.rcs ** 0.25)))
            amp *= float(max(0.25, 1.0 - r / (self.RANGE_MAX * 1.3)))

            prof_a = np.exp(-0.5 * ((self._az - az) / sigma_a) ** 2)
            prof_r = np.exp(-0.5 * ((self._r - r) / sigma_r) ** 2)
            self._scan = np.maximum(self._scan, amp * np.outer(prof_a, prof_r))

            # 距离-多普勒：目标落在 (径向速度格, 距离格)
            iv = int(np.argmin(np.abs(self._v - vr)))
            ir = int(np.argmin(np.abs(self._r - r)))
            d0, d1 = max(0, iv - 1), min(self.N_DOP, iv + 2)
            r0, r1 = max(0, ir - 1), min(self.N_RNG, ir + 2)
            rd[d0:d1, r0:r1] = np.maximum(rd[d0:d1, r0:r1], amp)

        # 噪声取"逐点取大"，不能写成 scan += noise。
        # 累计式写法在 decay<1 时稳态会收敛到 noise/(1-decay)，底噪被整体抬高。
        self._scan = np.maximum(
            self._scan,
            self._rng.normal(0.008, 0.004, size=self._scan.shape).astype(np.float32))
        self._scan = np.clip(self._scan, 0.0, 1.0)
        self._rd = np.clip(rd, 0.0, 1.0)

    # ------------------------------------------------------------------ #
    def read(self) -> RadarFrame | None:
        if not self._opened:
            return None
        now = time.monotonic()
        if now - self._t_last < 1.0 / self._rate:
            time.sleep(0.004)
            return None
        dt = now - self._t_last
        self._t_last = now
        self._rebuild(dt)

        targets = []
        for a in self._targets:
            r = hypot(a.x, a.y)
            if r < 500 or r > self.RANGE_MAX:
                continue
            targets.append(Target(
                x=a.x, y=a.y, vx=a.vx, vy=a.vy,
                snr=float(min(1.0, 0.4 + a.rcs / 25.0)),
                threat=self._threat_of(a), cls=a.iff, rcs=a.rcs))

        assert self._scan is not None and self._az is not None
        assert self._r is not None and self._v is not None
        return RadarFrame(
            timestamp=now,
            scan=self._scan.copy(),
            az_axis=self._az.copy(),
            r_axis=self._r.copy(),
            targets=targets,
            rd=self._rd.copy(),
            rd_v_axis=self._v.copy(),
            source=self.label,
            meta={"mode": self._mode, "n": len(targets),
                  "scan_period": 1.0 / self._rate * 2},
        )

    def describe(self) -> str:
        return f"相控阵 · {self._mode} · {len(self._targets)} 批目标"
