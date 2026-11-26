"""真实雷达物理参数数字孪生驱动（基于公开器件手册与工程文献数值）。

让无硬件用户在「数据源 → 模拟与演示 → 类型」中即可直接切换不同体制、频段、波束与杂波模型的真实雷达：
  * TI AWR1843 (77 GHz 车载 FMCW)
  * HLK-LD2450 (24 GHz 室内人体跟踪)
  * Furuno FAR-2xx8 (9.41 GHz X 波段航海 ARPA)
  * Ouster OS1-64 (905 nm 64 线 3D 激光雷达)
  * 机载 X 波段相控阵 (AN/APG 级 3D + RD)
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np

from ..core.driver import RadarDriver, register
from ..core.model import Capabilities, RadarFrame, Target
from ..core.radar_profiles import (
    RADAR_PROFILES, RadarProfile, generate_clutter_matrix, radar_snr_db, snr_to_intensity,
)


@dataclass(slots=True)
class _TwinTarget:
    name: str
    x: float
    y: float
    vx: float
    vy: float
    rcs: float
    z: float
    vz: float
    iff: str
    threat: int


class _BaseTwinDriver(RadarDriver):
    """基于 RadarProfile 的声明式物理数字孪生雷达基类。"""

    profile_id: str = ""
    simulated = True
    source = "sim"
    supports_control = True

    def __init__(self) -> None:
        self._p: RadarProfile = RADAR_PROFILES[self.profile_id]
        self._rng = np.random.default_rng(abs(hash(self.profile_id)) % 100_000 + 17)
        self._opened = False
        self._t_last = 0.0
        self._sweep = 0
        self._ctl = {"gain": 1.0, "stc": 0.0, "ftc": 0.0}
        self._az: np.ndarray | None = None
        self._r: np.ndarray | None = None
        self._v: np.ndarray | None = None
        self._el: np.ndarray | None = None
        self._scan: np.ndarray | None = None
        self._rd: np.ndarray | None = None
        self._rhi: np.ndarray | None = None
        self._targets: list[_TwinTarget] = []

    def capabilities(self) -> Capabilities:
        p = self._p
        views = ["ppi", "ascope", "waterfall", "tracks"]
        if p.has_doppler:
            views.append("rd")
        if p.has_elevation:
            views.append("rhi")
        form = "rd" if p.has_doppler and p.tier == "aesa" else ("omni" if p.az_fov_deg >= 359.0 else "sector")
        return Capabilities(
            name=p.label,
            has_scan=True,
            has_azimuth=True,
            has_targets=True,
            has_velocity=p.has_doppler or p.category in ("marine", "lidar", "automotive"),
            has_range_doppler=p.has_doppler,
            has_elevation=p.has_elevation,
            has_iff=p.has_iff,
            has_threat=True,
            az_fov=p.az_fov_deg,
            az_resolution=p.az_res_deg,
            range_min=p.range_min,
            range_max=p.range_max,
            max_targets=24,
            scan_mode=p.scan_mode,
            scan_rate=p.scan_rate_hz,
            tier=p.tier,
            transports=("virtual",),
            views=tuple(views),
            notes=p.reference_doc,
        )

    def open(self, **kwargs) -> None:
        p = self._p
        self._ctl = {"gain": 1.0, "stc": 0.0, "ftc": 0.0}
        if p.az_fov_deg >= 359.0:
            n_az = 240 if p.az_res_deg <= 1.5 else 180
            self._az = np.radians(np.linspace(-180.0, 180.0, n_az, endpoint=False)).astype(np.float32)
            self._step = max(4, n_az // 30) if p.scan_mode == "rotating" else n_az
        else:
            half = p.az_fov_deg * 0.5
            n_az = max(61, int(round(p.az_fov_deg / max(0.8, p.az_res_deg))))
            self._az = np.radians(np.linspace(-half, half, n_az)).astype(np.float32)
            self._step = n_az
        self._r = np.linspace(p.range_min, p.range_max, 180).astype(np.float32)
        self._scan = np.zeros((self._az.size, self._r.size), dtype=np.float32)
        if p.has_doppler:
            self._v = np.linspace(-p.v_max_ms, p.v_max_ms, 64).astype(np.float32)
            self._rd = np.zeros((self._v.size, self._r.size), dtype=np.float32)
        else:
            self._v = None
            self._rd = None
        if p.has_elevation:
            self._el = np.radians(np.linspace(0.0, max(15.0, p.el_fov_deg), 25)).astype(np.float32)
            self._rhi = np.zeros((self._el.size, self._r.size), dtype=np.float32)
        else:
            self._el = None
            self._rhi = None

        self._targets = [_TwinTarget(*t) for t in p.default_targets]
        self._sweep = 0
        self._t_last = time.monotonic()
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def set_control(self, param: str, value: float) -> bool:
        if param not in self._ctl:
            return False
        self._ctl[param] = float(value)
        return True

    def _step_physics(self, dt: float) -> None:
        p = self._p
        assert self._az is not None and self._r is not None and self._scan is not None
        rmax = p.range_max
        rmin = p.range_min
        half_fov = math.radians(p.az_fov_deg * 0.5)

        for t in self._targets:
            t.x += t.vx * dt
            t.y += t.vy * dt
            if p.has_elevation and t.vz:
                t.z += t.vz * dt
                z_cap = max(6.0, rmax * 0.35)
                if t.z < 1.0 or t.z > z_cap:
                    t.vz = -t.vz
                    t.z = max(1.0, min(z_cap, t.z))
            r = math.hypot(t.x, t.y)
            az = math.atan2(t.x, t.y)
            out_sector = (p.az_fov_deg < 359.0) and (abs(az) > half_fov * 0.96 or t.y < rmin * 1.1)
            if r > rmax * 0.96 or r < rmin * 1.2 or out_sector:
                if p.category == "automotive":
                    t.x = float(self._rng.choice((-3.6, 0.0, 3.6)))
                    t.y = rmax * float(self._rng.uniform(0.72, 0.92)) if t.vy <= 0 else rmax * 0.14
                else:
                    ang = float(self._rng.uniform(-half_fov * 0.82, half_fov * 0.82))
                    r_new = rmax * float(self._rng.uniform(0.60, 0.88))
                    t.x = r_new * math.sin(ang)
                    t.y = r_new * math.cos(ang)
                    spd = math.hypot(t.vx, t.vy) or 2.0
                    in_ang = math.atan2(-t.x, -t.y) + float(self._rng.uniform(-0.45, 0.45))
                    t.vx = spd * math.sin(in_ang)
                    t.vy = spd * math.cos(in_ang)

        decay = 0.965 if p.scan_mode == "rotating" else 0.72
        self._scan *= decay

        start = self._sweep
        idx = (np.arange(start, start + self._step) % self._az.size).astype(int)
        self._sweep = (start + self._step) % self._az.size

        clutter = generate_clutter_matrix(p.clutter_model, idx.size, self._r, self._rng)
        sigma_r = max(0.08, (rmax - rmin) / self._r.size * 1.25)
        sigma_a = math.radians(max(1.2, p.az_res_deg * 1.35))
        gain = float(self._ctl.get("gain", 1.0))

        rd = np.zeros_like(self._rd) if self._rd is not None else None
        if rd is not None:
            rd += self._rng.normal(0.01, 0.005, size=rd.shape).astype(np.float32)
            # 地杂波在 RD 图零多普勒线上形成亮带
            if p.clutter_model in ("urban_weibull", "sea_k"):
                iv0 = int(np.argmin(np.abs(self._v)))
                rd[max(0, iv0 - 1) : min(rd.shape[0], iv0 + 2), :] += 0.22 * clutter.mean(axis=0)[None, :]

        rhi = np.zeros_like(self._rhi) if self._rhi is not None else None

        sig = np.zeros((idx.size, self._r.size), dtype=np.float32)
        az_sub = self._az[idx]
        for t in self._targets:
            r = math.hypot(t.x, t.y)
            if r < rmin or r > rmax:
                continue
            snr_db = radar_snr_db(p, r, t.rcs, swerling=1, rng=self._rng)
            amp = min(1.0, snr_to_intensity(snr_db) * gain)
            az = math.atan2(t.x, t.y)
            da = (az_sub - az + math.pi) % (2.0 * math.pi) - math.pi
            pa = np.exp(-0.5 * (da / sigma_a) ** 2).astype(np.float32)
            pr = np.exp(-0.5 * ((self._r - r) / sigma_r) ** 2).astype(np.float32)
            sig = np.maximum(sig, amp * np.outer(pa, pr))

            if rd is not None and self._v is not None:
                vr = (t.x * t.vx + t.y * t.vy) / max(1e-3, r)
                iv = int(np.argmin(np.abs(self._v - vr)))
                ir = int(np.argmin(np.abs(self._r - r)))
                rd[max(0, iv - 1) : min(rd.shape[0], iv + 2), max(0, ir - 1) : min(rd.shape[1], ir + 2)] = np.maximum(
                    rd[max(0, iv - 1) : min(rd.shape[0], iv + 2), max(0, ir - 1) : min(rd.shape[1], ir + 2)],
                    amp,
                )
            if rhi is not None and self._el is not None:
                el = math.atan2(t.z, max(1e-3, r))
                pe = np.exp(-0.5 * ((self._el - el) / math.radians(2.8)) ** 2).astype(np.float32)
                rhi = np.maximum(rhi, amp * np.outer(pe, pr))

        stc = float(self._ctl.get("stc", 0.0))
        if stc > 0.0:
            rn = np.clip((self._r - rmin) / max(1.0, rmax - rmin), 0.05, 1.0)
            clutter *= (rn ** (1.2 * stc))[None, :]

        self._scan[idx] = np.clip(np.maximum(sig, clutter * gain), 0.0, 1.0)
        if rd is not None:
            self._rd = np.clip(rd, 0.0, 1.0)
        if rhi is not None:
            self._rhi = np.clip(rhi, 0.0, 1.0)

    def read(self) -> RadarFrame | None:
        if not self._opened:
            return None
        p = self._p
        now = time.monotonic()
        if now - self._t_last < 1.0 / p.scan_rate_hz:
            time.sleep(0.004)
            return None
        dt = max(0.01, min(0.25, now - self._t_last))
        self._t_last = now
        self._step_physics(dt)

        out_targets: list[Target] = []
        for t in self._targets:
            r = math.hypot(t.x, t.y)
            if r < p.range_min or r > p.range_max:
                continue
            snr_db = radar_snr_db(p, r, t.rcs, swerling=0)
            el_deg = math.degrees(math.atan2(t.z, max(1e-3, r))) if p.has_elevation else 0.0
            out_targets.append(Target(
                x=t.x, y=t.y, vx=t.vx, vy=t.vy,
                snr=snr_to_intensity(snr_db),
                z=t.z, el_deg=el_deg, rcs=t.rcs,
                cls=t.iff, threat=t.threat,
            ))

        assert self._scan is not None and self._az is not None and self._r is not None
        return RadarFrame(
            timestamp=now,
            scan=self._scan.copy(),
            az_axis=self._az.copy(),
            r_axis=self._r.copy(),
            targets=out_targets,
            rd=self._rd.copy() if self._rd is not None else None,
            rd_v_axis=self._v.copy() if self._v is not None else None,
            rhi=self._rhi.copy() if self._rhi is not None else None,
            el_axis=self._el.copy() if self._el is not None else None,
            source=p.label,
            meta={
                "profile_id": p.id,
                "fc_ghz": p.fc_ghz,
                "pt_w": p.pt_w,
                "gt_dbi": p.gt_dbi,
                "bw_mhz": p.bw_mhz,
                "sll_db": p.sll_db,
                "scan_period": 1.0 / p.scan_rate_hz * (2 if p.scan_mode == "staring" else 20),
            },
        )

    def describe(self) -> str:
        p = self._p
        return f"{p.label} · {p.fc_ghz:g} GHz · {p.range_max:g} m"


@register
class TwinAWR1843Driver(_BaseTwinDriver):
    id = "sim_awr1843"
    label = RADAR_PROFILES["sim_awr1843"].label
    form = "rd"
    profile_id = "sim_awr1843"


@register
class TwinLD2450Driver(_BaseTwinDriver):
    id = "sim_ld2450"
    label = RADAR_PROFILES["sim_ld2450"].label
    form = "sector"
    profile_id = "sim_ld2450"


@register
class TwinFurunoMarineDriver(_BaseTwinDriver):
    id = "sim_furuno"
    label = RADAR_PROFILES["sim_furuno"].label
    form = "omni"
    profile_id = "sim_furuno"


@register
class TwinOusterLidarDriver(_BaseTwinDriver):
    id = "sim_ouster"
    label = RADAR_PROFILES["sim_ouster"].label
    form = "omni"
    profile_id = "sim_ouster"


@register
class TwinAirborneAesaDriver(_BaseTwinDriver):
    id = "sim_air_aesa"
    label = RADAR_PROFILES["sim_air_aesa"].label
    form = "rd"
    profile_id = "sim_air_aesa"
