"""多目标跟踪器定量评测基准（OSPA / GOSPA，ROADMAP G2）。

用法：
    .venv\\Scripts\\python.exe tools\\eval_tracker.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from radarscope.core.metrics import evaluate_tracker_sequence
from radarscope.core.model import Target
from radarscope.core.tracker import Tracker


def _build_turn_and_clutter_scenario(steps: int = 80, dt: float = 0.1) -> tuple[list[list[Target]], list[list[Target]]]:
    rng = np.random.default_rng(42)
    truth_seq: list[list[Target]] = []
    det_seq: list[list[Target]] = []
    x1, y1, vx1, vy1 = -12.0, 20.0, 4.0, 1.5
    x2, y2, vx2, vy2 = 15.0, 45.0, -3.5, -1.2
    for k in range(steps):
        # 目标1：中段高机动协同转弯（模拟无人机急转规避）
        if 25 <= k < 55:
            ang = 1.4 * dt
            ca, sa = math.cos(ang), math.sin(ang)
            vx1, vy1 = vx1 * ca - vy1 * sa, vx1 * sa + vy1 * ca
        x1 += vx1 * dt
        y1 += vy1 * dt
        x2 += vx2 * dt
        y2 += vy2 * dt

        truth = [Target(x=x1, y=y1, tid=1), Target(x=x2, y=y2, tid=2)]
        dets = [
            Target(x=x1 + float(rng.normal(0, 0.25)), y=y1 + float(rng.normal(0, 0.25)), snr=0.85),
            Target(x=x2 + float(rng.normal(0, 0.25)), y=y2 + float(rng.normal(0, 0.25)), snr=0.82),
        ]
        if k % 5 == 0:
            dets.append(Target(x=float(rng.uniform(-25, 25)), y=float(rng.uniform(10, 50)), snr=0.35))
        truth_seq.append(truth)
        det_seq.append(dets)
    return truth_seq, det_seq


def main() -> int:
    truth_seq, det_seq = _build_turn_and_clutter_scenario()
    configs = [
        ("alpha_beta", Tracker(gate_m=3.0, filter_mode="alpha_beta")),
        ("kalman3d+gnn", Tracker(gate_m=3.0, filter_mode="kalman3d", assoc_mode="gnn")),
        ("imm3d+gnn", Tracker(gate_m=3.0, filter_mode="imm3d", assoc_mode="gnn")),
        ("imm3d+maha", Tracker(gate_m=3.0, filter_mode="imm3d", assoc_mode="gnn", gating_mode="mahalanobis", maha_gate=3.5)),
    ]
    print("| 跟踪配置 | 平均 GOSPA | 平均 OSPA | 定位误差 | FN | FP | ID 跳变 |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for name, trk in configs:
        res = evaluate_tracker_sequence(truth_seq, det_seq, trk, dt=0.1, c=5.0)
        print(
            f"| {name} | {res['mean_gospa']:.3f} | {res['mean_ospa']:.3f} | "
            f"{res['mean_loc']:.3f} | {res['fn']} | {res['fp']} | {res['id_switches']} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
