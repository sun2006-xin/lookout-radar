"""数据补全流水线 —— 通用性的核心。

驱动只给你一半的数据，这里负责把另一半推出来：
    有矩阵没目标 -> 跑 CA-CFAR 峰值检测，得到目标列表
    有目标没矩阵 -> 按点扩散函数反投影，合成强度矩阵

补完之后，任何雷达在这套框架里都能同时喂饱 PPI / B 显 / A 显 / 航迹表。
"""
from __future__ import annotations

from math import degrees, radians

import numpy as np

from .model import Capabilities, RadarFrame, Target


# --------------------------------------------------------------------------- #
# 检测：强度矩阵 -> 目标列表
# --------------------------------------------------------------------------- #
def _cfar_background(x: np.ndarray, guard: int = 2, train: int = 5) -> np.ndarray:
    """二维 CA-CFAR 背景估计：训练区取环形，中间留保护单元。

    为什么必须留保护单元：一个点目标在方位上会被波束展宽到好几个格，
    如果直接拿 3x3 邻域求均值，目标自己的能量会进背景，把检测门限抬上去，
    结果就是"看着有回波却报不出目标"。真实雷达的 CFAR 板也是这么做的。

    用积分图实现，计算量与窗口大小无关。
    """
    h, w = x.shape
    ii = np.zeros((h + 1, w + 1), dtype=np.float64)
    ii[1:, 1:] = x.cumsum(0, dtype=np.float64).cumsum(1)

    def box(r: int) -> tuple[np.ndarray, np.ndarray]:
        y0 = np.clip(np.arange(h) - r, 0, h)
        y1 = np.clip(np.arange(h) + r + 1, 0, h)
        x0 = np.clip(np.arange(w) - r, 0, w)
        x1 = np.clip(np.arange(w) + r + 1, 0, w)
        s = (ii[y1[:, None], x1[None, :]] - ii[y0[:, None], x1[None, :]]
             - ii[y1[:, None], x0[None, :]] + ii[y0[:, None], x0[None, :]])
        cnt = (y1 - y0)[:, None] * (x1 - x0)[None, :]
        return s, cnt.astype(np.float64)

    s_out, c_out = box(train)
    s_in, c_in = box(guard)
    n = c_out - c_in
    bg = np.where(n > 0, (s_out - s_in) / np.maximum(n, 1.0), s_out / np.maximum(c_out, 1.0))
    return bg.astype(np.float32)


def detect_targets(
    frame: RadarFrame,
    threshold: float = 0.30,
    floor: float = 0.15,
    max_targets: int = 8,
    guard_az: int = 4,
    guard_rng: int = 3,
    az_indices: tuple[int, int] | None = None,
) -> list[Target]:
    """在强度矩阵上做峰值检测。

    threshold 是"高出局部背景多少"才算目标，不是绝对门限——
    这样近处强反射不会把远处弱目标压掉。

    az_indices 给旋转扫描雷达用：只在刚刷新的扇区里找目标。
    否则扫描拖尾（上一圈残留的高能量）会被当成一堆假目标。
    """
    if not frame.has_scan:
        return []

    scan = frame.scan.astype(np.float32, copy=False)
    bg = _cfar_background(scan)
    score = scan - bg

    mask = (score > threshold) & (scan > floor)
    if frame.r_axis is not None:
        mask &= frame.r_axis[None, :] >= 0.0
    if az_indices is not None:
        sel = np.zeros(mask.shape[0], dtype=bool)
        a0, a1 = az_indices
        idx = np.arange(a0, a1) % mask.shape[0]
        sel[idx] = True
        mask &= sel[:, None]
    if not mask.any():
        return []

    flat = np.where(mask, score, -np.inf)

    # 只取最强的若干候选，而不是把一万多个格子全排序再逐个试。
    # 原来直接 argsort 整个矩阵，NMS 循环在噪声边缘上要走八千多次
    # （每次还要做一次切片判断），单帧 14 ms 全耗在这里。
    # 由于 max_targets 通常 ≤ 16，取 12 倍候选足够，剩下的由抑制逻辑处理。
    k = int(min(flat.size, max(32, max_targets * 12)))
    flat1 = flat.ravel()                    # argpartition 默认沿最后一维，必须先展平
    cand = np.argpartition(-flat1, k - 1)[:k]
    order = cand[np.argsort(-flat1[cand])]

    n_rng = scan.shape[1]
    taken = np.zeros_like(mask, dtype=bool)
    out: list[Target] = []
    for k in order:
        if len(out) >= max_targets:
            break
        i, j = divmod(int(k), n_rng)
        if not np.isfinite(flat[i, j]) or taken[i, j]:
            continue
        az = float(frame.az_axis[i]) if frame.az_axis is not None else 0.0
        rng = float(frame.r_axis[j]) if frame.r_axis is not None else 0.0
        out.append(
            Target(
                x=rng * np.sin(az),
                y=rng * np.cos(az),
                snr=float(min(1.0, scan[i, j])),
            )
        )
        i0, i1 = max(0, i - guard_az), min(mask.shape[0], i + guard_az + 1)
        j0, j1 = max(0, j - guard_rng), min(n_rng, j + guard_rng + 1)
        taken[i0:i1, j0:j1] = True
    return out


# --------------------------------------------------------------------------- #
# 合成：目标列表 -> 强度矩阵
# --------------------------------------------------------------------------- #
def synthesize_scan(
    caps: Capabilities,
    targets: list[Target],
    n_az: int = 121,
    n_rng: int = 72,
) -> RadarFrame:
    """把点目标反投影成一张强度图，让只有目标列表的雷达也能用 PPI。"""
    fov = radians(caps.az_fov)
    if caps.has_azimuth:
        az_axis = np.linspace(-fov / 2, fov / 2, n_az, dtype=np.float32)
    else:
        az_axis = np.zeros(1, dtype=np.float32)
    r_axis = np.linspace(caps.range_min, caps.range_max, n_rng, dtype=np.float32)

    scan = np.zeros((az_axis.size, n_rng), dtype=np.float32)
    sigma_r = max(0.08, (caps.range_max - caps.range_min) / n_rng * 0.8)
    sigma_az = radians(3.0)

    for t in targets:
        r0 = t.rng
        if not (caps.range_min * 0.5 <= r0 <= caps.range_max * 1.05):
            continue
        prof_r = np.exp(-0.5 * ((r_axis - r0) / sigma_r) ** 2)
        amp = float(max(0.25, min(1.0, t.snr)))
        if az_axis.size == 1:
            scan[0] = np.maximum(scan[0], amp * prof_r)
        else:
            prof_a = np.exp(-0.5 * ((az_axis - t.az) / sigma_az) ** 2)
            scan = np.maximum(scan, amp * np.outer(prof_a, prof_r))

    return RadarFrame(
        timestamp=0.0,
        scan=scan,
        az_axis=az_axis,
        r_axis=r_axis,
        targets=list(targets),
    )


# --------------------------------------------------------------------------- #
# 入口：一次调用补全所有缺失
# --------------------------------------------------------------------------- #
def normalize(frame: RadarFrame, caps: Capabilities, threshold: float = 0.30) -> RadarFrame:
    """把驱动交上来的帧整理成"两种原料都齐"的状态。"""
    if frame.has_scan:
        # 裁掉无效量程，避免界面按 6m 画却只有 3m 数据
        if frame.r_axis is not None and frame.r_axis.size > 1:
            keep = frame.r_axis <= caps.range_max * 1.02
            if keep.any() and not keep.all():
                frame.scan = frame.scan[:, keep]
                frame.r_axis = frame.r_axis[keep]
        if frame.az_axis is None:
            frame.az_axis = np.zeros(frame.scan.shape[0], dtype=np.float32)
        if not frame.has_targets:
            fresh = frame.meta.get("fresh_az")
            frame.targets = detect_targets(
                frame,
                threshold=threshold,
                max_targets=max(1, caps.max_targets or 8),
                az_indices=fresh if isinstance(fresh, tuple) else None,
            )
        return frame

    if frame.has_targets:
        synth = synthesize_scan(caps, frame.targets)
        synth.timestamp = frame.timestamp
        synth.source = frame.source
        synth.meta = frame.meta
        return synth

    # 什么都没有：给一张空图，界面不至于崩
    return synthesize_scan(caps, [])


def range_profile(frame: RadarFrame) -> np.ndarray:
    """沿方位取最大值，得到一条距离剖面（A 显 / 瀑布图用）。"""
    if not frame.has_scan:
        return np.zeros(1, dtype=np.float32)
    return frame.scan.max(axis=0)
