"""核心链路自检。

单独放在 core 里，是为了让三个入口共用同一份判断：
    · `Lookout.exe --selftest`（打包后验证，不弹窗）
    · 帮助菜单 →「运行自检」（普通用户点一下就行）
    · CI 里的 tools/check.py

之前逻辑写在 main.py，界面想调用就得 import 一个"入口模块"，
打包后还不一定有 —— 现在没有这个耦合了。
"""
from __future__ import annotations

import time

import numpy as np


def run_core_checks(timeout_s: float = 4.0) -> tuple[bool, str]:
    """跑一遍真实驱动与算法。返回 (是否通过, 一行摘要或错误)。"""
    try:
        import radarscope.drivers  # noqa: F401  导入即注册
        from radarscope.core import pipeline
        from radarscope.core.driver import available_drivers
        from radarscope.core.model import Capabilities
        from radarscope.paths import data_dir

        drivers = available_drivers()
        assert drivers, "没有任何驱动被注册"

        # 给够时间：旋转/凝视扫完一个扇区要几秒，
        # 只看头 0.5 秒会得到"全是噪声"的假结论。
        d = drivers["sim"]()
        d.open()
        frame = None
        best = 0.0
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            f = d.read()
            if f is not None:
                frame = f
                best = max(best, float(f.scan.max()))
                if best > 0.3:
                    break
            time.sleep(0.01)
        d.close()

        assert frame is not None and frame.has_scan, "模拟器没有产出数据"
        assert frame.scan.ndim == 2 and frame.scan.size > 100, "矩阵形状异常"
        assert best > 0.3, f"矩阵里始终没有回波（峰值 {best:.2f}）"
        dets = pipeline.detect_targets(frame, threshold=0.3, max_targets=8)
        assert isinstance(dets, list)

        caps = Capabilities(has_scan=True, has_azimuth=True,
                            range_min=0.2, range_max=8.0)
        synth = pipeline.synthesize_scan(caps, [])
        assert synth.scan.shape == (len(synth.az_axis), len(synth.r_axis))
        assert np.isfinite(frame.scan).all(), "矩阵里有 NaN/Inf"

        return True, (f"驱动 {len(drivers)} 个 · 帧 {frame.scan.shape} · "
                      f"检出 {len(dets)} 个目标 · 数据目录 {data_dir()}")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
