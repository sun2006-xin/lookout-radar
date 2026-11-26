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
def _integral_box_sums(x: np.ndarray, r: int) -> tuple[np.ndarray, np.ndarray]:
    """基于积分图的任意半径窗口求和与计数（O(1) 复杂度）。"""
    h, w = x.shape
    ii = np.zeros((h + 1, w + 1), dtype=np.float64)
    ii[1:, 1:] = x.cumsum(0, dtype=np.float64).cumsum(1)
    y0 = np.clip(np.arange(h) - r, 0, h)
    y1 = np.clip(np.arange(h) + r + 1, 0, h)
    x0 = np.clip(np.arange(w) - r, 0, w)
    x1 = np.clip(np.arange(w) + r + 1, 0, w)
    s = (ii[y1[:, None], x1[None, :]] - ii[y0[:, None], x1[None, :]]
         - ii[y1[:, None], x0[None, :]] + ii[y0[:, None], x0[None, :]])
    cnt = (y1 - y0)[:, None] * (x1 - x0)[None, :]
    return s, cnt.astype(np.float64)


def _cfar_background(x: np.ndarray, guard: int = 2, train: int = 5) -> np.ndarray:
    """二维 CA-CFAR 背景估计：训练区取环形，中间留保护单元。"""
    s_out, c_out = _integral_box_sums(x, train)
    s_in, c_in = _integral_box_sums(x, guard)
    n = c_out - c_in
    bg = np.where(n > 0, (s_out - s_in) / np.maximum(n, 1.0), s_out / np.maximum(c_out, 1.0))
    return bg.astype(np.float32)


def _go_cfar_background(x: np.ndarray, guard: int = 2, train: int = 5) -> np.ndarray:
    """二维 GO-CFAR（选大单元平均）：在距离前后半窗取较大背景，抑制杂波边缘虚警。"""
    h, w = x.shape
    ii = np.zeros((h + 1, w + 1), dtype=np.float64)
    ii[1:, 1:] = x.cumsum(0, dtype=np.float64).cumsum(1)
    y0 = np.clip(np.arange(h) - train, 0, h)
    y1 = np.clip(np.arange(h) + train + 1, 0, h)
    # 前半窗 [j - train, j - guard) 与后半窗 [j + guard + 1, j + train + 1)
    j = np.arange(w)
    xl0, xl1 = np.clip(j - train, 0, w), np.clip(j - guard, 0, w)
    xr0, xr1 = np.clip(j + guard + 1, 0, w), np.clip(j + train + 1, 0, w)

    sl = (ii[y1[:, None], xl1[None, :]] - ii[y0[:, None], xl1[None, :]]
          - ii[y1[:, None], xl0[None, :]] + ii[y0[:, None], xl0[None, :]])
    cl = np.maximum(1.0, (y1 - y0)[:, None] * np.maximum(0, xl1 - xl0)[None, :])
    sr = (ii[y1[:, None], xr1[None, :]] - ii[y0[:, None], xr1[None, :]]
          - ii[y1[:, None], xr0[None, :]] + ii[y0[:, None], xr0[None, :]])
    cr = np.maximum(1.0, (y1 - y0)[:, None] * np.maximum(0, xr1 - xr0)[None, :])

    has_l = (xl1 > xl0)[None, :]
    has_r = (xr1 > xr0)[None, :]
    ml = np.where(has_l, sl / cl, 0.0)
    mr = np.where(has_r, sr / cr, 0.0)
    bg = np.where(has_l & has_r, np.maximum(ml, mr), np.where(has_l, ml, mr))
    return bg.astype(np.float32)


def _os_cfar_background(x: np.ndarray, guard: int = 2, train: int = 5,
                        quantile: float = 0.75) -> np.ndarray:
    """OS-CFAR（有序统计）：沿距离训练窗取分位数，多目标靠得近时不互相抬高背景。"""
    h, w = x.shape
    pad = np.pad(x, ((0, 0), (train, train)), mode="edge")
    offsets = [k for k in range(-train, train + 1) if abs(k) > guard]
    if not offsets:
        return _cfar_background(x, guard, train)
    stack = np.stack([pad[:, train + off:train + off + w] for off in offsets], axis=0)
    kth = max(0, min(len(offsets) - 1, int(round(quantile * (len(offsets) - 1)))))
    return np.partition(stack, kth, axis=0)[kth].astype(np.float32)


def _local_noise_std(x: np.ndarray, guard: int = 2, train: int = 5) -> np.ndarray:
    """训练环内的局部噪声标准差，用于按场景信噪比自适应缩放门限。"""
    xf = x.astype(np.float64)
    s1_out, c_out = _integral_box_sums(xf, train)
    s1_in, c_in = _integral_box_sums(xf, guard)
    s2_out, _ = _integral_box_sums(xf * xf, train)
    s2_in, _ = _integral_box_sums(xf * xf, guard)
    n = np.maximum(1.0, c_out - c_in)
    e1 = (s1_out - s1_in) / n
    e2 = (s2_out - s2_in) / n
    var = np.maximum(0.0, e2 - e1 * e1)
    return np.sqrt(var).astype(np.float32)


def detect_targets(
    frame: RadarFrame,
    threshold: float = 0.30,
    floor: float = 0.15,
    max_targets: int = 8,
    guard_az: int = 4,
    guard_rng: int = 3,
    az_indices: tuple[int, int] | None = None,
    cfar_mode: str = "ca",
) -> list[Target]:
    """在强度矩阵上做峰值检测（支持 CA / GO / OS / auto 自适应 CFAR）。

    cfar_mode:
        "ca"   -- 经典单元平均 + 固定门限（默认兼容旧接口）
        "go"   -- 选大平均（GO-CFAR），抑杂波边缘虚警
        "os"   -- 有序统计（OS-CFAR），抗邻近强目标掩蔽
        "auto" -- GO-CFAR + 局部噪声标准差自适应门限（安静更敏、杂波更稳）
    """
    if not frame.has_scan:
        return []

    scan = frame.scan.astype(np.float32, copy=False)
    mode = (cfar_mode or "ca").lower()
    if mode == "os":
        bg = _os_cfar_background(scan)
    elif mode in ("go", "auto"):
        bg = _go_cfar_background(scan)
    else:
        bg = _cfar_background(scan)

    score = scan - bg
    if mode == "auto":
        # 用 guard=3, train=6 避免宽波束目标自身旁瓣抬高局部标准差
        sigma = _local_noise_std(scan, guard=3, train=6)
        eff_thr = np.clip(0.06 + 3.8 * sigma, 0.09, 0.85)
        eff_floor = np.clip(bg + 3.0 * sigma, 0.08, 0.85)
        mask = (score > eff_thr) & (scan > eff_floor)
    else:
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
def normalize(
    frame: RadarFrame,
    caps: Capabilities,
    threshold: float = 0.30,
    cfar_mode: str = "ca",
) -> RadarFrame:
    """把驱动交上来的帧整理成"两种原料都齐"的状态。"""
    if frame.has_scan:
        # 裁掉无效量程，避免界面按 6m 画却只有 3m 数据
        if frame.r_axis is not None and frame.r_axis.size > 1:
            keep = frame.r_axis <= caps.range_max * 1.02
            if keep.any() and not keep.all():
                frame.scan = frame.scan[:, keep]
                if frame.rd is not None and frame.rd.shape[1] == keep.size:
                    frame.rd = frame.rd[:, keep]
                if frame.rhi is not None and frame.rhi.shape[1] == keep.size:
                    frame.rhi = frame.rhi[:, keep]
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
                cfar_mode=cfar_mode,
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


class ClutterMap:
    """静态杂波背景图（EMA 均值学习与静止背景抑制，借鉴 Smart Radar 杂波掩膜）。"""

    def __init__(self, alpha: float = 0.12, gain: float = 0.85, warmup: int = 5) -> None:
        self.alpha = float(alpha)
        self.gain = float(gain)
        self.warmup = int(warmup)
        self.enabled: bool = False
        self._bg: np.ndarray | None = None
        self.frames: int = 0

    def reset(self) -> None:
        self._bg = None
        self.frames = 0

    def apply(self, frame: RadarFrame) -> RadarFrame:
        if not self.enabled or not frame.has_scan or frame.scan is None:
            return frame
        scan = frame.scan.astype(np.float32, copy=False)
        if self._bg is None or self._bg.shape != scan.shape:
            self._bg = scan.copy()
            self.frames = 1
            return frame
        self.frames += 1
        # 先用上一帧学到的静止背景做减法，再用缓慢 EMA 更新背景，避免把刚进入的运动目标也当成背景减掉
        bg_prev = self._bg
        self._bg = (1.0 - self.alpha) * self._bg + self.alpha * scan
        if self.frames >= self.warmup:
            frame.scan = np.clip(scan - self.gain * bg_prev, 0.0, 1.0)
        return frame


class LandMask:
    """陆地静止杂波掩膜（ROADMAP E6）：按岸线/地图色彩特征或固定岸界扇区抑制陆地强反射。"""

    def __init__(self, attenuation: float = 0.85, min_land_range_m: float = 15.0) -> None:
        self.enabled: bool = False
        self.attenuation: float = max(0.1, min(1.0, float(attenuation)))
        self.min_land_range_m: float = float(min_land_range_m)
        self.sectors: list[tuple[float, float, float]] = []  # (az0_deg, az1_deg, r_min_m)
        self._grid_mask: np.ndarray | None = None

    def reset(self) -> None:
        self._grid_mask = None

    def set_sectors(self, sectors: list[tuple[float, float, float]]) -> None:
        self.sectors = [(float(a0), float(a1), float(r0)) for a0, a1, r0 in sectors]

    def update_from_map_rgb(
        self,
        rgb: np.ndarray | None,
        mpp: float,
        anchor_px: tuple[float, float],
        az_axis: np.ndarray | None,
        r_axis: np.ndarray | None,
        heading_deg: float = 0.0,
    ) -> None:
        """基于离线/在线海图 RGB 像素水陆颜色先验构建极坐标陆地掩膜。"""
        if rgb is None or rgb.ndim != 3 or mpp <= 1e-6 or az_axis is None or r_axis is None:
            self._grid_mask = None
            return
        h, w, _ = rgb.shape
        ax, ay = float(anchor_px[0]), float(anchor_px[1])
        ang = az_axis[:, None] + radians(heading_deg)
        px = np.rint(ax + np.sin(ang) * (r_axis[None, :] / mpp)).astype(np.int32)
        py = np.rint(ay - np.cos(ang) * (r_axis[None, :] / mpp)).astype(np.int32)
        inside = (px >= 0) & (px < w) & (py >= 0) & (py < h)
        px_c = np.clip(px, 0, w - 1)
        py_c = np.clip(py, 0, h - 1)
        sampled = rgb[py_c, px_c].astype(np.int16)
        r_c, g_c, b_c = sampled[..., 0], sampled[..., 1], sampled[..., 2]
        # 水体在常见海图/深浅底图中通常呈现 B 通道高于 R，或整体低亮度深海色；其余浅灰/米黄/绿色块按陆地抑制
        is_water = (b_c > r_c + 8) | ((r_c + g_c + b_c) < 80)
        far_enough = r_axis[None, :] >= self.min_land_range_m
        self._grid_mask = inside & far_enough & (~is_water)

    def apply(self, frame: RadarFrame) -> RadarFrame:
        if not self.enabled or not frame.has_scan or frame.scan is None:
            return frame
        if frame.az_axis is None or frame.r_axis is None:
            return frame
        scan = frame.scan.astype(np.float32, copy=True)
        mask = np.zeros_like(scan, dtype=bool)
        if self._grid_mask is not None and self._grid_mask.shape == scan.shape:
            mask |= self._grid_mask
        if self.sectors:
            az_deg = np.degrees(frame.az_axis) % 360.0
            for a0, a1, r_min in self.sectors:
                a0m, a1m = a0 % 360.0, a1 % 360.0
                if a0m <= a1m:
                    az_ok = (az_deg >= a0m) & (az_deg <= a1m)
                else:
                    az_ok = (az_deg >= a0m) | (az_deg <= a1m)
                r_ok = frame.r_axis >= r_min
                mask |= az_ok[:, None] & r_ok[None, :]
        if np.any(mask):
            scan[mask] *= (1.0 - self.attenuation)
            frame.scan = scan
        return frame
