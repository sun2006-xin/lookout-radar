"""标定：软件凭什么知道"雷达朝向哪".

这是多站融合里最容易被跳过、也最容易毁掉整个系统的一步。
必须先把一件事说清楚：

**雷达自己永远不知道自己的绝对方位。**
它只知道"目标相对我视轴偏了多少度"。这个角度是相对量，
要变成平台坐标，必须补三样外部信息：

    1. 安装位置 (sx, sy)   —— 卷尺量的，误差 cm 级，通常够用
    2. 安装朝向 bearing     —— 这个最难，手装必然有几度误差
    3. （可选）平台航向      —— 要从"车头坐标系"变成"世界坐标系"才需要罗盘/IMU

本模块只解决第 2 条：**用已知点标定每台雷达的实际朝向**。

标定原理很简单，但很多人想不到：
    在地上用卷尺量出几个点（比如 (2, 3)、(0, 5)、(-2, 3)），
    把人依次站到每个点上，记录每台雷达报出的角度。
    对某一台雷达来说，"这个点应该在多少度"是可以由几何算出来的，
    和"实际报了多少度"之差，就是它的安装偏置。
    多个点取圆均值，噪声自然被压掉。

一句话：**你不可能从数据里推出安装角度，除非你有已知的参考点。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import atan2, cos, degrees, hypot, radians, sin

import numpy as np

from .fusion import SensorSpec, global_to_local
from .geometry import locate, ray_of      # 交会计算放在 geometry，fusion 也要用


def norm_deg(d: float) -> float:
    """归一化到 (-180, 180]。"""
    d = (d + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d


def circular_mean_deg(values: list[float]) -> float:
    """角度的"平均数"不能直接相加再除。

    +179° 和 -179° 的算术平均是 0°，但它们的真实平均是 180°。
    所以要用单位向量求和再取角度 —— 这个坑在处理罗盘/方位时特别常见。
    """
    if not values:
        return 0.0
    s = sum(sin(radians(v)) for v in values)
    c = sum(cos(radians(v)) for v in values)
    if abs(s) < 1e-12 and abs(c) < 1e-12:
        return 0.0
    return degrees(atan2(s, c))


@dataclass
class CalibPoint:
    """一个标定点：世界坐标已知，各传感器在该点的观测角度已知。"""

    x: float
    y: float
    obs: dict[str, float] = field(default_factory=dict)   # 传感器名 -> 本地方位角(度)
    note: str = ""

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "obs": dict(self.obs), "note": self.note}


def expected_local_deg(spec: SensorSpec, px: float, py: float) -> float:
    """按标称朝向，这个点应该落在传感器的多少度。"""
    lx, ly = global_to_local(px, py, spec)
    return degrees(atan2(lx, ly))


def calibrate_bearings(specs: list[SensorSpec],
                       points: list[CalibPoint]) -> dict[str, float]:
    """用已知点求每台雷达的朝向偏置（度，加到标称 bearing 上）。

    返回 {传感器名: 偏置}。没有观测数据的传感器不返回。
    """
    offsets: dict[str, float] = {}
    for spec in specs:
        deltas: list[float] = []
        for p in points:
            if spec.name not in p.obs:
                continue
            nom = expected_local_deg(spec, p.x, p.y)
            deltas.append(norm_deg(nom - p.obs[spec.name]))
        if deltas:
            offsets[spec.name] = circular_mean_deg(deltas)
    return offsets


def apply_offsets(specs: list[SensorSpec],
                  offsets: dict[str, float]) -> list[SensorSpec]:
    """把偏置写回传感器配置（返回新对象，不改原列表）。"""
    out: list[SensorSpec] = []
    for s in specs:
        d = SensorSpec.from_dict(s.to_dict())
        d.bearing_deg = norm_deg(d.bearing_deg + offsets.get(s.name, 0.0))
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# 定位：直接用已有射线做交会（计算本身在 core/geometry.py）
# --------------------------------------------------------------------------- #
def locate_with(specs: list[SensorSpec], obs: dict[str, float]
                ) -> tuple[float, float] | None:
    """按传感器名 + 观测角度直接定位。"""
    rays = [ray_of(s, obs[s.name]) for s in specs if s.name in obs]
    return locate(rays)


def residual_report(specs: list[SensorSpec], points: list[CalibPoint]
                    ) -> dict:
    """标定质量的验收指标。

    做法很朴素但有效：拿已知点当靶子，用校准后的参数反算位置，
    再和卷尺量出来的真值比 —— 这个误差就是"标定得好不好"的唯一硬指标。
    """
    errs: list[float] = []
    rows: list[dict] = []
    for p in points:
        loc = locate_with(specs, p.obs)
        if loc is None:
            rows.append({"x": p.x, "y": p.y, "est": None, "err": None})
            continue
        e = hypot(loc[0] - p.x, loc[1] - p.y)
        errs.append(e)
        rows.append({"x": p.x, "y": p.y, "est": loc, "err": e})
    rms = float(np.sqrt(np.mean(np.square(errs)))) if errs else 0.0
    return {"rows": rows, "rms": rms, "max": max(errs) if errs else 0.0,
            "n": len(errs)}


def suggest_offsets(specs: list[SensorSpec], points: list[CalibPoint]
                    ) -> tuple[dict[str, float], list[SensorSpec], dict]:
    """一步到位：算偏置 + 应用 + 出验收报告。"""
    off = calibrate_bearings(specs, points)
    fixed = apply_offsets(specs, off)
    return off, fixed, residual_report(fixed, points)


def angle_error_at(range_m: float, bearing_err_deg: float) -> float:
    """朝向误差换算成横向定位误差，用来判断"几度的误差到底能不能接受"。

    这是布站时最该先算的一笔账：
      1° 的朝向误差，在 8 m 处就是 14 cm 的横向偏差。
    """
    return range_m * abs(sin(radians(bearing_err_deg)))
