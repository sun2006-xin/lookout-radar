"""二维几何：方位线、交会定位。

单独抽出来是因为有两个地方要用：
    · `fusion`  重叠区把多台的方位线交会（真融合）
    · `calibration` 标定时用同样的交会做残差验收

放在一起也避免了 fusion ↔ calibration 循环 import。
"""

from __future__ import annotations

from math import radians, sin, cos

import numpy as np


def ray_of(spec, local_deg: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """把一次观测变成世界坐标系里的射线：(起点, 单位方向)。

    spec 需要有 `x` / `y` / `bearing_deg`；传感器报告的角度是相对视轴的，
    加上安装朝向才是世界方位。
    """
    b = radians(spec.bearing_deg)
    a = radians(local_deg)
    return (spec.x, spec.y), (sin(a + b), cos(a + b))


def locate(
    rays: list[tuple[tuple[float, float], tuple[float, float]]],
    weights: list[float] | None = None,
) -> tuple[float, float] | None:
    """多条射线的加权最小二乘交点（未传 weights 时退化为等权）。

    不直接"两条解方程"的原因：
      · 两条线夹角很小时交点会飞到天外（病态），必须能识别出来
      · 三条以上时加权最小二乘能压低近距离/高 SNR 站之外的噪声站影响

    做法：最小化"加权垂距平方和"，解 2×2 正规方程，闭式无迭代。
    射线近乎平行（行列式趋零）时返回 None —— 宁可判无效，不给一个假点。
    """
    if len(rays) < 2:
        return None
    A = np.zeros((2, 2), dtype=np.float64)
    b = np.zeros(2, dtype=np.float64)
    used = 0
    for idx, (o, u) in enumerate(rays):
        ux, uy = u
        n = float(np.hypot(ux, uy))
        if n < 1e-9:
            continue
        w = max(1e-3, float(weights[idx])) if weights is not None and idx < len(weights) else 1.0
        ux, uy = ux / n, uy / n
        P = w * np.array([[1.0 - ux * ux, -ux * uy],
                          [-ux * uy, 1.0 - uy * uy]])
        A += P
        b += P @ np.array([o[0], o[1]], dtype=np.float64)
        used += 1
    if used < 2:
        return None
    if abs(float(np.linalg.det(A))) < 1e-9:
        return None
    p = np.linalg.solve(A, b)
    return float(p[0]), float(p[1])
