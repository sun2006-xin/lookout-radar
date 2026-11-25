"""多站雷达组驱动。

两台实现：

* `multi`      —— 真实雷达组。按 `data/sensors.json` 的配置连 N 台设备，
                  每台有自己的驱动、端点、安装位置和朝向。
* `multi_demo` —— 6×60° 全向演示。一个共享的"世界"里跑目标，
                  6 个虚拟扇区雷达各看 60°，融合后拼成 360°。
                  这正好对应"不用舵机、靠堆模块做无死角"的方案。

两者共用 `core/fusion.py`，所以融合逻辑只写了一遍。
对上层完全透明：pipeline / Tracker / 所有视图拿到的还是一帧普通 RadarFrame。
"""
from __future__ import annotations

import json
import time
from math import atan2, cos, exp, hypot, radians, sin
from pathlib import Path

import numpy as np

from ..core.driver import RadarDriver, get_driver, register
from ..core.fusion import FusionEngine, SensorSpec, global_to_local
from ..core.model import Capabilities, RadarFrame, Target

CONFIG_NAME = "sensors.json"


# --------------------------------------------------------------------------- #
# 演示用：一个共享世界 + N 个只看得见自己扇区的虚拟雷达
# --------------------------------------------------------------------------- #
class _Mover:
    __slots__ = ("x", "y", "vx", "vy", "amp")

    def __init__(self, x, y, vx, vy, amp):
        self.x, self.y, self.vx, self.vy, self.amp = x, y, vx, vy, amp


class _World:
    """所有虚拟雷达共享的同一个世界。目标只更新一次，不能被 N 台各推一遍。"""

    def __init__(self, seed: int = 11) -> None:
        self.rng = np.random.default_rng(seed)
        self.movers = [
            _Mover(1.2, 2.4, 0.30, -0.05, 0.85),
            _Mover(-2.6, 1.4, 0.05, 0.28, 0.70),
            _Mover(-1.0, -3.2, 0.26, 0.10, 0.75),
            _Mover(3.1, -1.8, -0.18, 0.22, 0.60),
            _Mover(0.4, 5.0, -0.22, -0.16, 0.55),
        ]
        self.t = time.monotonic()

    def advance(self) -> None:
        now = time.monotonic()
        dt = min(0.2, now - self.t)
        self.t = now
        for m in self.movers:
            m.x += m.vx * dt
            m.y += m.vy * dt
            if abs(m.x) > 6.0:
                m.vx = -m.vx
            if abs(m.y) > 6.0:
                m.vy = -m.vy


class _SectorSim:
    """一台只覆盖自己扇区的虚拟雷达。鸭子类型，只需要 read()/close()/capabilities()。"""

    def __init__(self, spec: SensorSpec, world: _World, rate: float = 20.0,
                 n_az: int = 61, n_rng: int = 120) -> None:
        self.spec = spec
        self.world = world
        self.rate = rate
        self.n_az = n_az
        self.n_rng = n_rng
        half = radians(spec.fov_deg / 2.0)
        self._az = np.linspace(-half, half, n_az).astype(np.float32)
        self._r = np.linspace(0.2, spec.range_max, n_rng).astype(np.float32)
        self._scan = np.zeros((n_az, n_rng), dtype=np.float32)
        self._t_last = 0.0
        self._rng = np.random.default_rng(abs(hash(spec.name)) % (2 ** 31))

    def capabilities(self) -> Capabilities:
        return Capabilities(name=self.spec.name, has_scan=True, has_azimuth=True,
                            has_targets=True, has_velocity=True,
                            az_fov=self.spec.fov_deg, range_max=self.spec.range_max,
                            tier="sensor")

    def read(self) -> RadarFrame | None:
        now = time.monotonic()
        if now - self._t_last < 1.0 / self.rate:
            time.sleep(0.002)
            return None
        self._t_last = now
        self.world.advance()

        sigma_r = 0.16
        sigma_a = radians(4.0)
        half_fov = self.spec.fov_deg / 2.0 + 6.0

        signal = np.zeros_like(self._scan)
        for m in self.world.movers:
            lx, ly = global_to_local(m.x, m.y, self.spec)
            r = hypot(lx, ly)
            if r < 0.2 or r > self.spec.range_max:
                continue
            a = atan2(lx, ly)
            if abs(np.degrees(a)) > half_fov:
                continue                        # 不在我的扇区里，看不见
            prof_r = np.exp(-0.5 * ((self._r - r) / sigma_r) ** 2)
            prof_a = np.exp(-0.5 * ((self._az - a) / sigma_a) ** 2)
            signal = np.maximum(signal, m.amp * np.outer(prof_a, prof_r))

        # 噪声必须"覆盖"而不是"累加"。
        # 写成 scan += noise 的话，稳态会收敛到 noise/(1-decay)：
        # 0.015 / 0.1 = 0.15，整个画面抬高一层绿底，瀑布图直接糊成一片。
        noise = self._rng.normal(0.015, 0.008, size=self._scan.shape).astype(np.float32)
        self._scan = np.maximum(self._scan * 0.90,
                                np.clip(signal + noise, 0.0, 1.0))

        # 只有本扇区内的目标才作为量测上报 —— 这是扇区雷达的真实行为
        targets: list[Target] = []
        for m in self.world.movers:
            lx, ly = global_to_local(m.x, m.y, self.spec)
            r = hypot(lx, ly)
            if not (0.2 <= r <= self.spec.range_max):
                continue
            if abs(np.degrees(atan2(lx, ly))) > half_fov:
                continue
            targets.append(Target(x=lx, y=ly, vx=m.vx, vy=m.vy, snr=m.amp))

        return RadarFrame(
            timestamp=now,
            scan=self._scan.copy(),
            az_axis=self._az,
            r_axis=self._r,
            targets=targets,
            source=self.spec.name,
            meta={"sensor": self.spec.name},
        )

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return f"{self.spec.name} @{self.spec.bearing_deg:.0f}°"


# --------------------------------------------------------------------------- #
# 演示驱动：6 × 60° = 360°
# --------------------------------------------------------------------------- #
@register
class MultiSectorDemoDriver(RadarDriver):
    id = "multi_demo"
    label = "多站融合 · 6×60° 全向"
    simulated = True
    source = "sim"
    form = "fusion"

    def __init__(self) -> None:
        self._engine: FusionEngine | None = None
        self._n = 6
        self._fov = 60.0
        self._range = 8.0

    def capabilities(self) -> Capabilities:
        if self._engine is None:
            return Capabilities(name="多站融合", has_scan=True, has_azimuth=True,
                                az_fov=360.0, range_max=self._range, tier="fusion",
                                notes="6 台 60° 扇区雷达拼接")
        return self._engine.capabilities()

    def open(self, **kwargs) -> None:
        self._n = int(kwargs.get("n") or 6)
        self._fov = float(kwargs.get("fov") or 60.0)
        self._range = float(kwargs.get("range") or 8.0)

        self._engine = FusionEngine(n_az=360, n_rng=200,
                                    range_min=0.2, range_max=self._range, rate=20.0)
        world = _World()
        # 传感器装在一个小圆周上而不是同一点：
        #   · 真实布站不可能全叠在一处
        #   · 基线为 0 时方位线交会是病态的，融合只能退化成"取坐标平均"
        r_mount = float(kwargs.get("mount_radius") or 0.45)
        # 视场比扇区间距大一点，相邻扇区留出共视区，才能做交会
        fov = self._fov + (360.0 / self._n) * 0.25
        for i in range(self._n):
            bearing = i * (360.0 / self._n)
            spec = SensorSpec(
                name=f"S{i + 1}",
                x=r_mount * sin(radians(bearing)),
                y=r_mount * cos(radians(bearing)),
                bearing_deg=bearing,
                fov_deg=fov,
                range_max=self._range,
            )
            self._engine.attach(spec, _SectorSim(spec, world))

    def read(self) -> RadarFrame | None:
        if self._engine is None:
            return None
        f = self._engine.poll()
        if f is not None:
            f.meta["gaps"] = self._engine.gaps()
            f.meta["scan_period"] = 2.0
        return f

    def close(self) -> None:
        if self._engine is not None:
            self._engine.detach_all()
            self._engine = None

    def describe(self) -> str:
        return f"{self._n} × {self._fov:.0f}° 全向融合"

    def fusion_engine(self) -> FusionEngine | None:
        """标定向导要用：拿到融合内核才能读各台的原始观测。"""
        return self._engine


# --------------------------------------------------------------------------- #
# 真实组网驱动
# --------------------------------------------------------------------------- #
@register
class MultiRadarDriver(RadarDriver):
    id = "multi"
    label = "多站融合 · 真实雷达组"
    simulated = False
    source = "fusion"
    form = "fusion"

    def __init__(self) -> None:
        self._engine: FusionEngine | None = None
        self._specs: list[SensorSpec] = []

    def capabilities(self) -> Capabilities:
        if self._engine is None:
            return Capabilities(name="多站融合", has_scan=True, has_azimuth=True,
                                az_fov=360.0, tier="fusion")
        return self._engine.capabilities()

    # ------------------------------------------------------------------ #
    @staticmethod
    def load_config(path: str | Path) -> list[SensorSpec]:
        p = Path(path)
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("sensors", data) if isinstance(data, dict) else data
        return [SensorSpec.from_dict(d) for d in items]

    def open(self, **kwargs) -> None:
        path = kwargs.get("path") or kwargs.get("endpoint") or ""
        if not path or not Path(path).exists():
            from .. import paths
            base = paths.data_dir() / CONFIG_NAME
            path = str(base if base.exists() else "")
        self._specs = self.load_config(path) if path else []
        if not self._specs:
            raise RuntimeError(
                "多站融合需要配置子雷达：\n"
                f"在 data/{CONFIG_NAME} 里写 [{{\"name\":\"S1\",\"driver\":\"ld2450\","
                "\"endpoint\":\"serial://COM3?baud=256000\",\"bearing_deg\":0,"
                "\"fov_deg\":120}}…]\n"
                "也可以用界面上的「传感器配置」按钮生成。")

        rmax = max(s.range_max for s in self._specs)
        self._engine = FusionEngine(n_az=360, n_rng=200,
                                    range_min=0.2, range_max=rmax, rate=20.0)

        opened = 0
        for spec in self._specs:
            if not spec.enabled:
                continue
            try:
                dev = get_driver(spec.driver)()
                dev.open(endpoint=spec.endpoint, port=spec.endpoint)
            except Exception:
                # 单台起不来不能拖垮整组，留个占位让界面显示离线
                dev = _DeadSensor(spec.endpoint)
            self._engine.attach(spec, dev)
            opened += 1
        if opened == 0:
            raise RuntimeError("没有一台子雷达到位")

    def read(self) -> RadarFrame | None:
        if self._engine is None:
            return None
        f = self._engine.poll()
        if f is not None:
            f.meta["gaps"] = self._engine.gaps()
            f.meta["scan_period"] = 2.0
        return f

    def close(self) -> None:
        if self._engine is not None:
            self._engine.detach_all()
            self._engine = None

    def describe(self) -> str:
        n = len(self._specs)
        return f"{n} 台子雷达组网"

    def fusion_engine(self) -> FusionEngine | None:
        return self._engine


class _DeadSensor:
    """打不开的子雷达占位，避免一台掉线导致整组不显示。"""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def read(self):
        return None

    def close(self) -> None:
        pass

    def capabilities(self) -> Capabilities:
        return Capabilities()

    def describe(self) -> str:
        return f"离线({self.endpoint})"
