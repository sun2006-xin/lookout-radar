"""视图坐标换算。

把"雷达本体坐标"换算成"屏幕上该画在哪"，以及反过来——
鼠标点一下要知道对应的距离和方位。这些数学放进 core 是为了能测：
写在 widget 里就只能靠肉眼验证。

约定（全工程统一）：
    方位 az：0° 在正前，顺时针为正
    屏幕：x 向右、y 向下，圆心为 (cx, cy)，半径 R
"""

from __future__ import annotations

from math import atan2, cos, degrees, hypot, radians, sin

HEAD_UP = "head_up"
NORTH_UP = "north_up"


def orbit_angle(az_deg: float, heading_deg: float = 0.0,
                mode: str = HEAD_UP) -> float:
    """目标方位 → 屏幕上应该画的角度（度，0=上，顺时针）。

    · head_up（艏向朝上）：画面跟着车/船转，操作员看"前左前右"最直观
    · north_up（北向朝上）：画面固定，方便和地图对照

    两者的差别只是加不加平台航向。上一版直接画 az，等价于 head_up=0°，
    接上罗盘/IMU 之后 north_up 才有意义。
    """
    if mode == NORTH_UP:
        return (az_deg + heading_deg) % 360.0
    return az_deg % 360.0


def screen_offset(az_deg: float, rng: float, r_min: float, r_max: float,
                  R: float) -> tuple[float, float]:
    """极坐标 → 以圆心为原点的像素偏移 (dx, dy)。"""
    span = max(1e-6, r_max - r_min)
    rr = (rng - r_min) / span * R
    a = radians(az_deg)
    return rr * sin(a), -rr * cos(a)


def point_to_polar(px: float, py: float, cx: float, cy: float, R: float,
                   r_min: float, r_max: float) -> tuple[float, float, bool]:
    """鼠标坐标 → (距离 m, 方位 度, 是否在盘内)。

    点击测距用。超出量程时仍返回方位与截断后的距离，`inside=False`
    让界面可以只显示"盘外点击"而不画点。
    """
    dx = px - cx
    dy = cy - py                       # 屏幕 y 向下，极坐标 y 向上
    dist_px = hypot(dx, dy)
    span = max(1e-6, r_max - r_min)
    rng = r_min + dist_px / max(1e-6, R) * span
    az = degrees(atan2(dx, dy)) % 360.0
    return rng, az, dist_px <= R


def clamp_range(rng: float, r_min: float, r_max: float) -> float:
    return max(r_min, min(r_max, rng))


def fmt_bearing(az_deg: float) -> str:
    """方位文本：做成 000°~359° 的三位定长，列对齐好看。"""
    return f"{int(round(az_deg)) % 360:03d}°"


def fmt_distance(m: float) -> str:
    """距离文本：过 1 km 自动换单位，避免满屏 "12345.6 m"。"""
    if m >= 1000.0:
        return f"{m / 1000.0:.2f} km"
    return f"{m:.2f} m"
