"""多目标追踪定量评估指标：OSPA 与 GOSPA（对标 Stone Soup 学术标准，ROADMAP G2）。

纯 NumPy 实现，利用 tracker.linear_sum_assignment_min 求解最优匹配：
  - ospa_distance():  返回 (总OSPA, 定位分量, 势误差分量)
  - gospa_distance(): alpha=2 下分解为定位误差 + 漏检惩罚(missed) + 虚警惩罚(false)
  - evaluate_tracker_sequence(): 按帧序列评测跟踪器总 GOSPA、定位 RMSE 与 ID 跳变次数
"""
from __future__ import annotations

from math import hypot
from typing import Sequence

import numpy as np

from .model import Target
from .tracker import Tracker, linear_sum_assignment_min


def _pair_dist(a: Target, b: Target) -> float:
    return hypot(float(a.x) - float(b.x), float(a.y) - float(b.y))


def ospa_distance(
    truth: Sequence[Target],
    estimates: Sequence[Target],
    c: float = 10.0,
    p: float = 2.0,
) -> tuple[float, float, float]:
    """计算经典 OSPA 距离，返回 (ospa, loc_part, card_part)。"""
    n, m = len(truth), len(estimates)
    if n == 0 and m == 0:
        return 0.0, 0.0, 0.0
    if n == 0 or m == 0:
        return float(c), 0.0, float(c)

    if n > m:
        truth, estimates = estimates, truth
        n, m = m, n

    cost = np.zeros((n, m), dtype=np.float64)
    for i in range(n):
        for j in range(m):
            cost[i, j] = min(float(c), _pair_dist(truth[i], estimates[j])) ** p

    rows, cols = linear_sum_assignment_min(cost)
    loc_sum = sum(float(cost[r, col]) for r, col in zip(rows, cols))
    card_sum = float(c ** p) * (m - n)
    tot = ((loc_sum + card_sum) / m) ** (1.0 / p)
    loc = (loc_sum / m) ** (1.0 / p)
    card = (card_sum / m) ** (1.0 / p)
    return float(tot), float(loc), float(card)


def gospa_distance(
    truth: Sequence[Target],
    estimates: Sequence[Target],
    c: float = 10.0,
    p: float = 2.0,
    alpha: float = 2.0,
) -> dict[str, float]:
    """计算广义最优子模式分配指标 GOSPA（alpha=2 时完全分解定位/漏检/虚警）。"""
    n, m = len(truth), len(estimates)
    if n == 0 and m == 0:
        return {"gospa": 0.0, "loc": 0.0, "missed": 0.0, "false": 0.0, "tp": 0, "fn": 0, "fp": 0}

    half_cp = (float(c) ** p) / float(alpha)
    if n == 0 or m == 0:
        missed = half_cp * n
        false = half_cp * m
        return {
            "gospa": float((missed + false) ** (1.0 / p)),
            "loc": 0.0,
            "missed": float(missed ** (1.0 / p)),
            "false": float(false ** (1.0 / p)),
            "tp": 0, "fn": n, "fp": m,
        }

    cost = np.zeros((n, m), dtype=np.float64)
    for i in range(n):
        for j in range(m):
            d = _pair_dist(truth[i], estimates[j])
            cost[i, j] = min(float(c) ** p, d ** p)

    rows, cols = linear_sum_assignment_min(cost)
    tp = 0
    loc_p = 0.0
    for r, col in zip(rows, cols):
        if cost[r, col] < float(c) ** p - 1e-12:
            tp += 1
            loc_p += float(cost[r, col])
    fn = n - tp
    fp = m - tp
    missed_p = half_cp * fn
    false_p = half_cp * fp
    total = (loc_p + missed_p + false_p) ** (1.0 / p)
    return {
        "gospa": float(total),
        "loc": float(loc_p ** (1.0 / p)),
        "missed": float(missed_p ** (1.0 / p)),
        "false": float(false_p ** (1.0 / p)),
        "tp": int(tp), "fn": int(fn), "fp": int(fp),
    }


def evaluate_tracker_sequence(
    truth_seq: Sequence[Sequence[Target]],
    det_seq: Sequence[Sequence[Target]],
    tracker: Tracker,
    dt: float = 0.1,
    c: float = 5.0,
) -> dict[str, float]:
    """在带真值标签序列上评估跟踪器平均 GOSPA / OSPA 与航迹 ID 跳变次数。"""
    tracker.reset()
    gospas: list[float] = []
    ospas: list[float] = []
    locs: list[float] = []
    fn_tot = fp_tot = id_sw = 0
    last_tid_for_truth: dict[int, int] = {}

    for truth, dets in zip(truth_seq, det_seq):
        est = [t for t in tracker.update(list(dets), dt) if not t.tentative]
        g = gospa_distance(truth, est, c=c)
        o, _, _ = ospa_distance(truth, est, c=c)
        gospas.append(g["gospa"])
        ospas.append(o)
        locs.append(g["loc"])
        fn_tot += int(g["fn"])
        fp_tot += int(g["fp"])

        if truth and est:
            cost = np.full((len(truth), len(est)), 1e6, dtype=np.float64)
            for i, tr in enumerate(truth):
                for j, es in enumerate(est):
                    d = _pair_dist(tr, es)
                    if d <= c:
                        cost[i, j] = d
            rs, cs = linear_sum_assignment_min(cost)
            for r, col in zip(rs, cs):
                if cost[r, col] < c:
                    key = truth[r].tid if truth[r].tid >= 0 else r
                    tid = est[col].tid
                    if key in last_tid_for_truth and last_tid_for_truth[key] != tid:
                        id_sw += 1
                    last_tid_for_truth[key] = tid

    n = max(1, len(gospas))
    return {
        "mean_gospa": float(sum(gospas) / n),
        "mean_ospa": float(sum(ospas) / n),
        "mean_loc": float(sum(locs) / n),
        "fn": int(fn_tot),
        "fp": int(fp_tot),
        "id_switches": int(id_sw),
    }
