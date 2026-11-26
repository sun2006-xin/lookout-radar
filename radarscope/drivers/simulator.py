"""模拟器驱动：没有硬件也能把整套软件跑起来。

它造的是"真雷达的物理形态"，不是随机数：
  * rotating 模式：180 个方位格 / 360°，每帧只刷新当前扇区 -> 屏幕上有真实扫描拖尾
  * staring 模式：±60° 凝视，整幅同时刷新 -> 像 LD2450 那样
两个模式都会加入底噪和一个静止杂波点，用来验证 CFAR 检测是否可靠。
"""
from __future__ import annotations

import time
from math import atan2, cos, hypot, radians, sin

import numpy as np

from ..core.driver import RadarDriver, register
from ..core.model import Capabilities, RadarFrame, Target


class _Mover:
    __slots__ = ("x", "y", "z", "vx", "vy", "vz", "amp")

    def __init__(self, x, y, vx, vy, amp, z: float = 0.0, vz: float = 0.0):
        self.x, self.y, self.z = x, y, z
        self.vx, self.vy, self.vz = vx, vy, vz
        self.amp = amp


@register
class SimulatorDriver(RadarDriver):
    id = "sim"
    label = "模拟器 · 合成雷达"
    simulated = True
    source = "sim"
    form = "sector"
    supports_control = True

    def __init__(self) -> None:
        self.mode = "rotating"
        self._rate = 20.0
        self._rng = np.random.default_rng(7)
        self._scan: np.ndarray | None = None
        self._az: np.ndarray | None = None
        self._r: np.ndarray | None = None
        self._t_last = 0.0
        self._sweep = 0
        self._movers: list[_Mover] = []
        self._clutter: list[_Mover] = []
        self._opened = False

    # ------------------------------------------------------------------ #
    def capabilities(self) -> Capabilities:
        if self.mode == "rotating":
            return Capabilities(
                name="模拟 · 旋转扫描",
                has_scan=True, has_azimuth=True, has_targets=False,
                az_fov=360.0, az_resolution=2.0, range_min=0.3, range_max=6.0,
                scan_mode="rotating", scan_rate=self._rate,
                tier="sensor", transports=("virtual",),
                views=("ppi", "ascope", "waterfall", "tracks"),
                notes="360° 机械扫描，每帧只刷新一个扇区",
            )
        if self.mode == "rhi_3d":
            return Capabilities(
                name="模拟 · 3D 俯仰扫掠 (RHI)",
                has_scan=True, has_azimuth=True, has_targets=True,
                has_velocity=True, has_elevation=True,
                az_fov=120.0, az_resolution=1.0, range_min=0.2, range_max=8.0,
                scan_mode="staring", scan_rate=self._rate,
                tier="aesa", transports=("virtual",),
                views=("ppi", "ascope", "waterfall", "tracks", "rhi"),
                notes="±60° 方位 × 0°~45° 俯仰三维扫掠（带高度 Z 与 RHI 剖面）",
            )
        return Capabilities(
            name="模拟 · 凝视扇区",
            has_scan=True, has_azimuth=True, has_targets=False,
            az_fov=120.0, az_resolution=1.0, range_min=0.2, range_max=8.0,
            scan_mode="staring", scan_rate=self._rate,
            tier="sensor", transports=("virtual",),
            views=("ppi", "ascope", "waterfall", "tracks"),
            notes="±60° 同时照射，整幅更新",
        )

    # ------------------------------------------------------------------ #
    def open(self, **kwargs) -> None:
        self.mode = kwargs.get("mode", self.mode)
        self._ctl = {"gain": 1.0, "stc": 0.0, "ftc": 0.0}
        caps = self.capabilities()
        if self.mode == "rotating":
            self._az = np.radians(np.linspace(-180.0, 180.0, 180, endpoint=False)).astype(np.float32)
            self._step = 3                      # 每帧扫 3 格 = 6°，一圈 60 帧
        else:
            self._az = np.radians(np.linspace(-60.0, 60.0, 121)).astype(np.float32)
            self._step = self._az.size          # 一次全刷
        self._r = np.linspace(caps.range_min, caps.range_max, 144).astype(np.float32)
        self._scan = np.zeros((self._az.size, self._r.size), dtype=np.float32)

        self._movers = [
            _Mover(1.0, 2.2, 0.28, -0.10, 0.85, z=1.4, vz=0.12),
            _Mover(-1.7, 3.6, 0.12, 0.22, 0.62, z=2.8, vz=-0.08),
            _Mover(2.5, 4.8, -0.32, 0.05, 0.45, z=3.9, vz=0.05),
        ]
        self._el = np.radians(np.linspace(0.0, 45.0, 25)).astype(np.float32) if self.mode == "rhi_3d" else None
        # 静止杂波：验证 CFAR 会不会把墙/椅子当成目标
        self._clutter = [_Mover(0.9, 5.3, 0.0, 0.0, 0.55)]

        self._t_last = time.monotonic()
        self._sweep = 0
        self._opened = True

    def close(self) -> None:
        self._opened = False

    # ------------------------------------------------------------------ #
    def _advance(self, dt: float) -> None:
        # 动态进出与缓慢变向：目标不再只在固定几条线上来回撞墙，而是每隔一段时间自然换航向/重生
        self._age = getattr(self, "_age", 0.0) + dt
        rmax = 5.8 if self.mode == "rotating" else 7.6
        for idx_m, m in enumerate(self._movers):
            m.x += m.vx * dt
            m.y += m.vy * dt
            if self.mode == "rhi_3d":
                m.z += m.vz * dt
                if m.z < 0.4 or m.z > 5.2:
                    m.vz = -m.vz
                    m.z = max(0.4, min(5.2, m.z))
            # 轻微正弦机动，模拟非直线目标运动
            turn = 0.12 * sin(self._age * 0.5 + idx_m * 1.7) * dt
            nvx = m.vx * cos(turn) - m.vy * sin(turn)
            nvy = m.vx * sin(turn) + m.vy * cos(turn)
            m.vx, m.vy = nvx, nvy
            if (m.x ** 2 + m.y ** 2) ** 0.5 > rmax or m.y < (0.4 if self.mode == "staring" else -rmax):
                ang = float(self._rng.uniform(-2.2 if self.mode == "rotating" else -0.9,
                                              2.2 if self.mode == "rotating" else 0.9))
                r_spawn = float(self._rng.uniform(rmax * 0.65, rmax * 0.92))
                m.x = r_spawn * sin(ang)
                m.y = r_spawn * cos(ang)
                spd = float(self._rng.uniform(0.22, 0.55))
                in_ang = float(np.arctan2(-m.x, -m.y)) + float(self._rng.uniform(-0.5, 0.5))
                m.vx, m.vy = spd * sin(in_ang), spd * cos(in_ang)
        assert self._az is not None and self._r is not None and self._scan is not None
        # 老数据缓慢衰减，扫描拖尾就来自这里。
        # 系数要和转速配合：一圈 60 帧，0.985^60≈0.40，
        # 一圈扫完之后整幅画面还剩四成亮度 —— 这就是真实 PPI 的观感。
        # 想更"急性"就调小，比如 0.96 就只剩 120° 拖尾。
        self._scan *= 0.985

        start = self._sweep
        idx = (np.arange(start, start + self._step) % self._az.size).astype(int)
        self._sweep = (start + self._step) % self._az.size

        sigma_r = 0.16
        # 方位展宽跟工作方式有关：
        #   旋转扫描：波束十几度宽，隔 6° 扫一次，目标自然被拉成一段弧
        #   凝视扇区：角度本身能分辨，人就是一个紧凑的点
        # 如果一律用宽波束，CFAR 的训练窗会落在目标自己身上，
        # 背景被抬高，检测器就开始漏报 —— 这是真实雷达调参也会踩的坑。
        sigma_a = radians(6.0 if self.mode == "rotating" else 2.5)
        signal = np.zeros((idx.size, self._r.size), dtype=np.float32)
        for m in self._movers + self._clutter:
            r0 = (m.x ** 2 + m.y ** 2) ** 0.5
            a0 = np.arctan2(m.x, m.y)
            prof_r = np.exp(-0.5 * ((self._r - r0) / sigma_r) ** 2)
            prof_a = np.exp(-0.5 * ((self._az[idx] - a0) / sigma_a) ** 2)
            signal = np.maximum(signal, m.amp * np.outer(prof_a, prof_r))

        # 关键：这一遍必须是"覆盖"，不能是"累加"。
        # 写成 += 噪声 的话，底噪会随帧数累积（0.02/(1-0.93)≈0.29），
        # 结果整个画面都是假峰、检测器全线报警。
        noise = self._rng.normal(0.02, 0.012, size=signal.shape)
        new_vals = np.clip(signal + noise, 0.0, 1.0)
        self._scan[idx, :] = np.maximum(self._scan[idx, :], new_vals)

    # ------------------------------------------------------------------ #
    def set_control(self, **params) -> bool:
        """模拟器支持增益 / STC / FTC，效果和真机同一个物理含义：

        · gain：整体放大（前端增益）
        · stc ：随距离衰减，压制近处强杂波（Sensitivity Time Control）
        · ftc ：低于门限的一律抹掉（Fast Time Constant，去弱回波/雨雪）
        """
        if "gain" in params:
            self._ctl["gain"] = max(0.1, min(6.0, float(params["gain"])))
        if "stc" in params:
            self._ctl["stc"] = max(0.0, min(1.0, float(params["stc"])))
        if "ftc" in params:
            self._ctl["ftc"] = max(0.0, min(0.9, float(params["ftc"])))
        return True

    def control_state(self) -> dict:
        return dict(self._ctl)

    def _apply_control(self, scan):
        g = float(self._ctl.get("gain", 1.0))
        stc = float(self._ctl.get("stc", 0.0))
        ftc = float(self._ctl.get("ftc", 0.0))
        out = scan.copy()
        if stc > 0 and self._r is not None and self._r.size:
            # 归一化到量程末尾：近处衰减最多，远处基本不动
            att = 1.0 / (1.0 + 3.0 * stc * (1.0 - self._r / max(1e-6, self._r[-1])))
            out *= att[None, :]
        if abs(g - 1.0) > 1e-6:
            out *= g
        if ftc > 0:
            out = np.where(out < ftc, 0.0, out)
        return np.clip(out, 0.0, 1.0)

    def read(self) -> RadarFrame | None:
        if not self._opened:
            return None
        now = time.monotonic()
        # 每帧只推进一次物理时间，避免界面拉得越快目标跑得越快
        if now - self._t_last < 1.0 / self._rate:
            time.sleep(0.005)
            return None
        dt = now - self._t_last
        self._t_last = now
        self._advance(dt)

        assert self._scan is not None and self._az is not None and self._r is not None
        start = (self._sweep - self._step) % self._az.size
        stop = start + self._step
        rhi = None
        el_axis = None
        tgts_3d: list[Target] = []
        if self.mode == "rhi_3d" and self._el is not None:
            rhi = np.clip(self._rng.normal(0.02, 0.012, size=(self._el.size, self._r.size)), 0.0, 1.0).astype(np.float32)
            for m in self._movers:
                hr = hypot(m.x, m.y)
                el_rad = atan2(m.z, max(0.2, hr))
                pe = np.exp(-0.5 * ((self._el - el_rad) / radians(4.5)) ** 2).astype(np.float32)
                pr = np.exp(-0.5 * ((self._r - hypot(hr, m.z)) / 0.22) ** 2).astype(np.float32)
                rhi = np.maximum(rhi, m.amp * np.outer(pe, pr))
                tgts_3d.append(Target(
                    x=m.x, y=m.y, vx=m.vx, vy=m.vy, snr=m.amp,
                    z=round(m.z, 2), el_deg=round(float(np.degrees(el_rad)), 1),
                ))
            el_axis = self._el.copy()

        frame = RadarFrame(
            timestamp=now,
            scan=self._apply_control(self._scan),
            az_axis=self._az.copy(),
            r_axis=self._r.copy(),
            targets=tgts_3d,
            rhi=rhi,
            el_axis=el_axis,
            source=self.label,
            meta={
                "fresh_az": (int(start), int(stop)),
                # 一圈要多久。航迹保持时间必须比它长，否则旋转雷达每转一圈
                # 就会把刚建好的航迹删掉一次，屏幕上永远看不到稳定框。
                "scan_period": self._az.size / self._step / self._rate,
            },
        )
        return frame
