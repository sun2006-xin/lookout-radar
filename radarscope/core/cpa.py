"""CPA / TCPA：最近会遇距离与最近会遇时间。

船用雷达避碰的核心指标，车载/机器人避障同样能用：
    · CPA  = 如果我保持现在的速度和方向，最近会离对方多远
    · TCPA = 多久之后到达那个最近点（负值表示已经过了）

推导（相对运动）：
    相对位置 r = p_target - p_own
    相对速度 v = v_target - v_own
    相遇时间 t* = -(r·v) / |v|²        （让 |r + v t| 取极小时）
    最近距离 CPA = |r + v t*|
    t* < 0 说明正在远离，此时 CPA 取当前距离

单位统一用米和秒。本舰速度由航向（度，0=北，顺时针）与航速（m/s）给出。
"""
from __future__ import annotations

from dataclasses import dataclass
from math import cos, hypot, radians, sin

from .model import Target


@dataclass
class CpaResult:
    tid: int
    cpa: float          # 米
    tcpa: float         # 秒；<=0 表示正在远离
    range_now: float    # 当前距离，米

    @property
    def danger(self) -> bool:
        return self.tcpa > 0


def own_velocity(speed: float, heading_deg: float) -> tuple[float, float]:
    """航速 + 航向 → 世界坐标速度向量（x 向右，y 向前，与雷达一致）。"""
    a = radians(heading_deg)
    return speed * sin(a), speed * cos(a)


def evaluate(tracks: list[Target], speed: float, heading_deg: float,
             horizon: float = 60.0) -> list[CpaResult]:
    """对每个航迹算 CPA/TCPA。horizon 之外的不算，避免满屏无意义的远目标。"""
    ovx, ovy = own_velocity(speed, heading_deg)
    out: list[CpaResult] = []
    for t in tracks:
        rx, ry = t.x, t.y                      # 本舰在原点
        vx, vy = t.vx - ovx, t.vy - ovy
        vv = vx * vx + vy * vy
        rng_now = hypot(rx, ry)
        if vv < 1e-9:
            # 相对静止：不会靠近，CPA 就是当前距离
            out.append(CpaResult(t.tid, rng_now, 0.0, rng_now))
            continue
        t_star = -(rx * vx + ry * vy) / vv
        if t_star <= 0:
            out.append(CpaResult(t.tid, rng_now, t_star, rng_now))
            continue
        if t_star > horizon:
            continue
        cx, cy = rx + vx * t_star, ry + vy * t_star
        out.append(CpaResult(t.tid, hypot(cx, cy), t_star, rng_now))
    out.sort(key=lambda r: (r.tcpa <= 0, r.cpa))
    return out


def worst(results: list[CpaResult]) -> CpaResult | None:
    """最危险的那个：正在接近的目标里 CPA 最小的。"""
    approaching = [r for r in results if r.tcpa > 0]
    if not approaching:
        return None
    return min(approaching, key=lambda r: r.cpa)
