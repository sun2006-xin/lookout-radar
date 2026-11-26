"""无界面自检：验证"矩阵 -> 检测 -> 航迹"整条链路。

不依赖 PyQt5，可以直接在命令行跑：
    python selftest.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np

import radarscope.drivers  # noqa: F401
from radarscope.core import pipeline
from radarscope.core.driver import available_drivers
from radarscope.core.model import validate_frame_meta
from radarscope.core.tracker import Tracker


def run(mode: str = "staring", frames: int = 60) -> None:
    cls = available_drivers()["sim"]
    drv = cls()
    drv.open(mode=mode)
    caps = drv.capabilities()
    tracker = Tracker()
    last_ts = 0.0

    det_counts, track_counts, ids = [], [], set()
    t0 = time.perf_counter()
    for _ in range(frames):
        frame = None
        for _ in range(200):                 # 模拟器按帧率出数，这里等一等
            frame = drv.read()
            if frame is not None:
                break
            time.sleep(0.002)
        if frame is None:
            continue
        errs = validate_frame_meta(frame)
        assert not errs, f"{mode} meta schema 违规: {errs}"
        f = pipeline.normalize(frame, caps)
        dt = (frame.timestamp - last_ts) if last_ts else 0.1
        last_ts = frame.timestamp
        tracks = tracker.update(f.targets, dt, -1)
        det_counts.append(len(f.targets))
        track_counts.append(len(tracks))
        ids.update(t.tid for t in tracks)

    el = time.perf_counter() - t0
    drv.close()
    total = int(np.sum(det_counts))
    print(f"[{mode}] 帧数={len(det_counts)} 耗时={el:.2f}s 累计检测={total} "
          f"平均航迹={np.mean(track_counts):.1f} 航迹号={sorted(ids)}")
    assert det_counts, "没有产生任何帧"
    # 旋转扫描每帧只照到一个扇区，所以"每帧检测数"本来就接近 0，
    # 真正该看的是"整段时间内有没有检出、航迹能不能维持住"。
    assert total >= 2, "检测器一个目标都没找到"
    assert np.mean(track_counts) >= 1.0, "航迹没建立"


if __name__ == "__main__":
    run("staring")
    run("rotating")
    print("SELFTEST OK")
