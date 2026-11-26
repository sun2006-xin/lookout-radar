"""模拟电子站 / 动态虚拟目标引擎（纯软件仿真注入）。

用途：
  1. 没接真雷达时：提供动态进出、高速逼近、交叉会遇等丰富场景，不再只是固定位置绕圈；
  2. 连着任意雷达（甚至真机）时：可在画面上额外注入虚拟移动目标，直接用来验证
     告警区、CPA 防撞、瀑布图、转发协议与航迹跟踪是否灵敏。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

import numpy as np

from . import pipeline
from .model import Capabilities, RadarFrame, Target

SCENARIOS = {
    "none": "关闭场景自动生成（仅保留手动注入）",
    "patrol": "随机出现与巡逻（动态生成/离开）",
    "approach": "高速逼近 / 防撞告警测试",
    "crossing": "多目标交叉会遇（测航迹关联）",
    "marine": "航海避碰演练（航道交叉 / 追越 / 对遇）",
    "automotive": "车载 77G 多车道交通（前车减速 / 邻道切入）",
    "drone": "低空无人机 / 安防周界（悬停 + 突防）",
}

# 场景预设 <-> 界面形态 / 扫描模式 / 检测滤波参数 自动协同映射
# "none" 即恢复日常经典雷达显示：关闭专属停靠台、隐藏多余工具，回归纯净监控视图。
SCENARIO_PROFILES: dict[str, dict] = {
    "none": {
        "range_m": 0.0,
        "sim_mode": None,
        "cfar_mode": None,
        "clutter_enabled": False,
        "docks": (),
        "show_zones": False,
        "show_cov": False,
        "enable_map": False,
        "enable_ais": False,
        "banner": "",
    },
    "marine": {
        "range_m": 2000.0,
        "sim_mode": "rotating",
        "cfar_mode": "go",
        "clutter_enabled": True,
        "land_mask": True,
        "enable_map": True,
        "enable_ais": True,
        "gate_m": 120.0,
        "filter_mode": "kalman3d",
        "confirm_hits": 2,
        "confirm_window": 4,
        "docks": ("zones",),
        "show_cov": True,
        "banner": "航海海图避碰模式 · 已展开海图底图 / 陆地掩膜 / AIS / GO-CFAR 与防区操作台",
    },
    "automotive": {
        "range_m": 80.0,
        "sim_mode": "staring",
        "cfar_mode": "os",
        "clutter_enabled": False,
        "gate_m": 4.5,
        "filter_mode": "kalman3d",
        "confirm_hits": 2,
        "confirm_window": 3,
        "docks": (),
        "show_cov": True,
        "banner": "车载 77G 前向扇区模式 · 已切换 80m 凝视视场 / OS-CFAR 与轨迹置信椭圆",
    },
    "drone": {
        "range_m": 150.0,
        "sim_mode": "rhi_3d",
        "cfar_mode": "auto",
        "clutter_enabled": True,
        "gate_m": 6.5,
        "filter_mode": "imm3d",
        "confirm_hits": 3,
        "confirm_window": 5,
        "docks": ("zones",),
        "show_zones": True,
        "show_cov": True,
        "banner": "低空无人机 3D 防护模式 · 已展开 RHI 俯仰剖面 / IMM3D 机动滤波 / 3/5 起批与周界防区",
    },
    "crossing": {
        "range_m": 0.0,
        "sim_mode": "rotating",
        "cfar_mode": "auto",
        "clutter_enabled": False,
        "gate_m": 1.6,
        "filter_mode": "kalman3d",
        "confirm_hits": 2,
        "confirm_window": 3,
        "docks": ("zones",),
        "show_cov": True,
        "banner": "多目标交叉关联模式 · 已激活 GNN 匈牙利匹配 / 2σ 置信椭圆与防区滤波台",
    },
    "approach": {
        "range_m": 0.0,
        "cfar_mode": "os",
        "clutter_enabled": False,
        "gate_m": 2.0,
        "filter_mode": "imm3d",
        "confirm_hits": 2,
        "confirm_window": 3,
        "docks": ("zones",),
        "show_cov": True,
        "banner": "高速逼近告警演练 · 已激活 OS-CFAR / IMM3D 滤波与防区台",
    },
    "patrol": {
        "range_m": 0.0,
        "cfar_mode": "auto",
        "clutter_enabled": False,
        "gate_m": 1.5,
        "filter_mode": "kalman3d",
        "confirm_hits": 1,
        "confirm_window": 1,
        "docks": (),
        "show_cov": False,
        "banner": "动态巡逻仿真模式",
    },
}


def recommended_profile(scenario: str) -> dict | None:
    """返回预设场景对应的推荐检测/滤波/确认参数；未定义则返回 None。"""
    p = SCENARIO_PROFILES.get(scenario)
    return dict(p) if p is not None else None

JAMMING_MODES = {
    "none": "关闭干扰（正常回波信号）",
    "barrage": "宽带噪声压制（演练全向底噪抬升与烧穿）",
    "spot": "瞄准扇区压制（指定方位强干扰掩蔽目标）",
    "drfm": "DRFM 多假目标欺骗（同方位注入周期假回波）",
    "rgpo": "RGPO 距离波门拖引（假回波缓慢向外漂移）",
    "vgpo": "VGPO 速度波门拖引（RD 多普勒维速度牵引欺骗）",
    "chaff": "箔条云（Chaff）面状散射（高强度弥散面杂波演练）",
    "isrj": "ISRJ 间歇采样转发干扰（主目标前后等距梳状假峰）",
}


def compute_ew_metrics(
    jam_mode: str,
    jam_jsr: float,
    range_max: float,
    jam_az_deg: float,
    jam_bw_deg: float,
    eccm_agility: bool = False,
) -> dict:
    """计算教学级电子战压制信干比 JSR(dB) 与烧穿距离 R_bt。"""
    raw_jsr = max(0.05, min(1.0, float(jam_jsr)))
    eff_jsr = raw_jsr * (0.35 if eccm_agility else 1.0)
    jsr_db = round(20.0 * math.log10(max(0.05, eff_jsr) / 0.25), 1)
    rmax = max(1.0, float(range_max))
    bt = rmax * min(0.92, 0.78 / (1.0 + 2.4 * eff_jsr))
    return {
        "jam_mode": jam_mode,
        "jsr_eff": round(eff_jsr, 3),
        "jsr_db": jsr_db,
        "burn_through_m": round(max(0.2, bt), 2),
        "jam_az_deg": float(jam_az_deg),
        "jam_bw_deg": float(jam_bw_deg),
        "eccm_agility": bool(eccm_agility),
    }


def compute_fire_control_solution(
    target: Target,
    interceptor_speed: float = 0.0,
) -> dict | None:
    """计算教学级模拟拦截前置点、提前角与预计交会时间（纯数学推演）。"""
    if target is None or target.rng <= 1e-4:
        return None
    v_int = max(0.5, float(interceptor_speed) if interceptor_speed > 0 else max(1.5, target.speed * 1.8))
    tti = min(30.0, target.rng / v_int)
    ix = target.x + target.vx * tti * 0.65
    iy = target.y + target.vy * tti * 0.65
    az_tgt = math.degrees(math.atan2(target.x, target.y))
    az_lead = math.degrees(math.atan2(ix, iy))
    lead_deg = (az_lead - az_tgt + 180.0) % 360.0 - 180.0
    return {
        "tid": int(target.tid),
        "tx": float(target.x),
        "ty": float(target.y),
        "ix": float(ix),
        "iy": float(iy),
        "lead_deg": round(lead_deg, 1),
        "tti_s": round(tti, 1),
        "simulated_only": True,
    }


@dataclass(slots=True)
class SimObject:
    """一个在平面上运动的虚拟电子目标。"""

    id: int
    name: str
    x: float
    y: float
    vx: float
    vy: float
    amp: float = 0.88
    respawn: bool = True


class ElectronicStation:
    """动态虚拟目标管理与帧叠加器。"""

    def __init__(self, seed: int = 2026) -> None:
        self._rng = random.Random(seed)
        self._np_rng = np.random.default_rng(seed)
        self._seq = 0
        self.scenario: str = "none"
        self.objects: list[SimObject] = []
        self.speed_scale: float = 1.0
        # 教学级电子对抗（纯软件波形/矩阵数学建模，无实际射频发射）
        self.jam_mode: str = "none"
        self.jam_jsr: float = 0.55       # 干扰压制/假目标相对强度 0.1~1.0
        self.jam_az_deg: float = 15.0    # 扇区干扰中心方位（度）
        self.jam_bw_deg: float = 36.0    # 扇区干扰波束宽度（度）
        self.eccm_agility: bool = False  # 模拟频率捷变/PRF 参差抗干扰教学开关
        self.fc_sim: bool = False        # 模拟火控前置角引导推演开关（纯软件教学）
        self._jam_t: float = 0.0

    def active(self) -> bool:
        return self.scenario != "none" or bool(self.objects) or self.jam_mode != "none" or self.fc_sim

    def set_scenario(self, scenario: str, range_max: float = 8.0) -> None:
        self.scenario = scenario if scenario in SCENARIOS else "none"
        self.objects.clear()
        rmax = max(2.0, float(range_max))
        if self.scenario == "patrol":
            for _ in range(3):
                self.spawn_random(rmax, respawn=True)
        elif self.scenario == "approach":
            self.inject(-0.4, rmax * 0.85, 0.05, -0.55, amp=0.92, name="逼近目标-1", respawn=True)
            self.inject(rmax * 0.45, rmax * 0.72, -0.30, -0.40, amp=0.82, name="逼近目标-2", respawn=True)
        elif self.scenario == "crossing":
            self.inject(-rmax * 0.55, rmax * 0.45, 0.45, 0.05, amp=0.88, name="横穿-A", respawn=True)
            self.inject(rmax * 0.55, rmax * 0.48, -0.45, -0.04, amp=0.86, name="横穿-B", respawn=True)
            self.inject(0.1, rmax * 0.30, 0.10, 0.25, amp=0.78, name="纵向-C", respawn=True)
        elif self.scenario == "marine":
            spd = max(0.35, rmax * 0.03)
            self.inject(-rmax * 0.45, rmax * 0.68, spd * 1.1, -spd * 0.45, amp=0.90, name="商船-右舷避让", respawn=True)
            self.inject(0.25 * rmax, rmax * 0.82, -spd * 0.15, -spd * 1.25, amp=0.88, name="货轮-对遇", respawn=True)
            self.inject(-0.18 * rmax, rmax * 0.32, 0.05 * spd, spd * 0.65, amp=0.76, name="渔船-慢速前航", respawn=True)
        elif self.scenario == "automotive":
            v = max(0.45, rmax * 0.05)
            self.inject(0.0, rmax * 0.62, 0.0, -v * 0.45, amp=0.92, name="前车-减速", respawn=True)
            self.inject(-rmax * 0.22, rmax * 0.78, v * 0.25, -v * 0.85, amp=0.84, name="左道切入", respawn=True)
            self.inject(rmax * 0.26, rmax * 0.35, -v * 0.05, v * 0.55, amp=0.80, name="右道超车", respawn=True)
        elif self.scenario == "drone":
            v = max(0.30, rmax * 0.06)
            self.inject(rmax * 0.48, rmax * 0.76, -v * 0.85, -v * 0.90, amp=0.82, name="突防机-1", respawn=True)
            self.inject(-rmax * 0.32, rmax * 0.52, v * 0.20, v * 0.08, amp=0.74, name="悬停巡检-2", respawn=True)

    def inject(
        self,
        x: float,
        y: float,
        vx: float,
        vy: float,
        amp: float = 0.88,
        name: str = "",
        respawn: bool = False,
    ) -> SimObject:
        self._seq += 1
        obj = SimObject(
            id=self._seq,
            name=name or f"虚拟-{self._seq}",
            x=float(x),
            y=float(y),
            vx=float(vx),
            vy=float(vy),
            amp=max(0.2, min(1.0, float(amp))),
            respawn=respawn,
        )
        self.objects.append(obj)
        return obj

    def spawn_random(self, range_max: float = 8.0, respawn: bool = True) -> SimObject:
        """从雷达覆盖边缘随机生成一个向场内移动的虚拟物体。"""
        rmax = max(2.0, float(range_max))
        ang = self._rng.uniform(-math.pi * 0.85, math.pi * 0.85)
        r0 = self._rng.uniform(rmax * 0.45, rmax * 0.88)
        x = r0 * math.sin(ang)
        y = max(0.6, abs(r0 * math.cos(ang)))
        spd = self._rng.uniform(0.18, 0.65)
        heading = ang + math.pi + self._rng.uniform(-0.85, 0.85)
        vx = spd * math.sin(heading)
        vy = spd * math.cos(heading)
        amp = self._rng.uniform(0.55, 0.95)
        return self.inject(x, y, vx, vy, amp=amp, respawn=respawn)

    def clear(self) -> None:
        self.scenario = "none"
        self.objects.clear()
        self.jam_mode = "none"
        self._jam_t = 0.0

    def step(self, dt: float, range_max: float) -> list[Target]:
        """推进所有虚拟目标一个时间步长，边界外自动折返或重生。"""
        if not self.objects:
            return []
        dt = max(0.0, min(0.5, float(dt))) * self.speed_scale
        rmax = max(2.0, float(range_max))
        kept: list[SimObject] = []
        for o in self.objects:
            o.x += o.vx * dt
            o.y += o.vy * dt
            r = math.hypot(o.x, o.y)
            if r > rmax * 0.96 or r < 0.25:
                if o.respawn:
                    # 离开视野后从边缘重新进入，模拟连续来往目标
                    ang = self._rng.uniform(-math.pi * 0.75, math.pi * 0.75)
                    r_new = rmax * self._rng.uniform(0.65, 0.88)
                    o.x = r_new * math.sin(ang)
                    o.y = max(0.6, r_new * math.cos(ang))
                    spd = math.hypot(o.vx, o.vy) or 0.35
                    in_ang = math.atan2(-o.x, -o.y) + self._rng.uniform(-0.55, 0.55)
                    o.vx = spd * math.sin(in_ang)
                    o.vy = spd * math.cos(in_ang)
                    kept.append(o)
                else:
                    # 手动注入的目标碰到边界时折返，方便反复观察
                    o.vx = -o.vx
                    o.vy = -o.vy
                    o.x = max(-rmax * 0.9, min(rmax * 0.9, o.x))
                    o.y = max(0.3, min(rmax * 0.9, o.y))
                    kept.append(o)
            else:
                kept.append(o)
        self.objects = kept
        return [
            Target(x=o.x, y=o.y, vx=o.vx, vy=o.vy, snr=o.amp)
            for o in self.objects
        ]

    def ew_metrics(self, range_max: float) -> dict:
        return compute_ew_metrics(
            self.jam_mode, self.jam_jsr, range_max,
            self.jam_az_deg, self.jam_bw_deg, self.eccm_agility,
        )

    def _apply_jamming(
        self,
        frame: RadarFrame,
        caps: Capabilities,
        dt: float,
    ) -> None:
        """在软件层向扫描/RD矩阵注入教学级电子对抗（压制/DRFM/RGPO/VGPO/箔条/ISRJ）波形。"""
        if self.jam_mode == "none" or not frame.has_scan or frame.scan is None:
            return
        self._jam_t += max(0.01, min(0.5, float(dt)))
        scan = frame.scan.astype(np.float32, copy=True)
        n_az, n_r = scan.shape
        raw_jsr = max(0.1, min(1.0, float(self.jam_jsr)))
        jsr = raw_jsr * (0.35 if self.eccm_agility else 1.0)

        if self.jam_mode == "barrage":
            noise = self._np_rng.uniform(0.0, jsr * 0.72, size=scan.shape).astype(np.float32)
            scan = np.maximum(scan, noise)
        elif self.jam_mode == "spot" and frame.az_axis is not None:
            az_c = math.radians(self.jam_az_deg)
            bw = math.radians(max(8.0, self.jam_bw_deg))
            da = (frame.az_axis - az_c + math.pi) % (2 * math.pi) - math.pi
            mask_a = np.exp(-0.5 * (da / (bw * 0.45)) ** 2).astype(np.float32)[:, None]
            strobe = self._np_rng.uniform(0.25, 1.0, size=scan.shape).astype(np.float32)
            scan = np.maximum(scan, jsr * mask_a * strobe)
        elif self.jam_mode == "chaff" and frame.az_axis is not None and frame.r_axis is not None:
            az_c = math.radians(self.jam_az_deg)
            spread = 1.0 + min(0.8, self._jam_t * 0.08)
            bw = math.radians(max(14.0, self.jam_bw_deg * 0.85 * spread))
            da = (frame.az_axis - az_c + math.pi) % (2 * math.pi) - math.pi
            prof_a = np.exp(-0.5 * (da / (bw * 0.5)) ** 2).astype(np.float32)
            r_center = caps.range_min + (caps.range_max - caps.range_min) * 0.48
            r_sigma = max(0.35, (caps.range_max - caps.range_min) * 0.11 * spread)
            prof_r = np.exp(-0.5 * ((frame.r_axis - r_center) / r_sigma) ** 2).astype(np.float32)
            speckle = self._np_rng.uniform(0.45, 1.0, size=scan.shape).astype(np.float32)
            scan = np.maximum(scan, (0.45 + 0.50 * jsr) * np.outer(prof_a, prof_r) * speckle)
        elif self.jam_mode in ("drfm", "rgpo", "isrj") and frame.az_axis is not None and frame.r_axis is not None:
            anchors = frame.targets[:2] if frame.targets else [
                Target(x=0.0, y=max(caps.range_min + 0.5, caps.range_max * 0.35), snr=0.85)
            ]
            sigma_r = max(0.10, (caps.range_max - caps.range_min) / max(1, n_r) * 1.1)
            sigma_a = math.radians(3.2)
            span_r = max(0.5, caps.range_max - caps.range_min)
            for t in anchors:
                da = (frame.az_axis - t.az + math.pi) % (2 * math.pi) - math.pi
                prof_a = np.exp(-0.5 * (da / sigma_a) ** 2).astype(np.float32)
                if self.jam_mode == "drfm":
                    step_r = span_r * 0.14
                    offsets = (step_r, 2.0 * step_r, -step_r)
                    amp_j = 0.55 + 0.40 * jsr
                elif self.jam_mode == "rgpo":
                    pull = (self._jam_t * 0.35 * span_r * 0.55) % (span_r * 0.55)
                    offsets = (pull,)
                    amp_j = 0.65 + 0.32 * jsr
                else:
                    comb = max(0.25, span_r * 0.08)
                    offsets = (-2.0 * comb, -comb, comb, 2.0 * comb)
                    amp_j = 0.48 + 0.42 * jsr
                for off in offsets:
                    rf = t.rng + off
                    if caps.range_min * 0.8 <= rf <= caps.range_max * 0.98:
                        prof_r = np.exp(-0.5 * ((frame.r_axis - rf) / sigma_r) ** 2).astype(np.float32)
                        scan = np.maximum(scan, amp_j * np.outer(prof_a, prof_r))
        elif self.jam_mode == "vgpo":
            # VGPO 在多普勒维注入随时间牵引的假速度峰；无 RD 时伴随微弱距离维脉冲抖动
            if frame.rd is not None and frame.rd_v_axis is not None and frame.r_axis is not None:
                rd = frame.rd.astype(np.float32, copy=True)
                vmax = max(1.0, float(np.max(np.abs(frame.rd_v_axis))))
                r0 = frame.targets[0].rng if frame.targets else caps.range_max * 0.4
                v0 = frame.targets[0].speed if frame.targets else 0.0
                v_pull = ((v0 + self._jam_t * 1.4) % (1.8 * vmax)) - 0.9 * vmax
                sig_v = max(0.5, vmax * 0.10)
                sig_r = max(0.15, (caps.range_max - caps.range_min) * 0.04)
                pv = np.exp(-0.5 * ((frame.rd_v_axis - v_pull) / sig_v) ** 2).astype(np.float32)
                pr = np.exp(-0.5 * ((frame.r_axis - r0) / sig_r) ** 2).astype(np.float32)
                rd = np.maximum(rd, (0.55 + 0.40 * jsr) * np.outer(pv, pr))
                frame.rd = np.clip(rd, 0.0, 1.0)
            elif frame.r_axis is not None and frame.az_axis is not None:
                r0 = frame.targets[0].rng if frame.targets else caps.range_max * 0.4
                az0 = frame.targets[0].az if frame.targets else 0.0
                da = (frame.az_axis - az0 + math.pi) % (2 * math.pi) - math.pi
                prof_a = np.exp(-0.5 * (da / math.radians(3.5)) ** 2).astype(np.float32)
                jit = math.sin(self._jam_t * 3.5) * max(0.15, caps.range_max * 0.03)
                prof_r = np.exp(-0.5 * ((frame.r_axis - (r0 + jit)) / 0.25) ** 2).astype(np.float32)
                scan = np.maximum(scan, (0.50 + 0.40 * jsr) * np.outer(prof_a, prof_r))
        frame.scan = np.clip(scan, 0.0, 1.0)

    def merge_into_frame(
        self,
        frame: RadarFrame,
        caps: Capabilities,
        dt: float,
        threshold: float = 0.30,
        cfar_mode: str = "ca",
    ) -> RadarFrame:
        """将活跃虚拟目标与电子对抗波形叠加到 RadarFrame，并在干扰开启时重跑 CFAR 检出。"""
        v_targets = self.step(dt, caps.range_max)
        if v_targets:
            frame.targets = list(frame.targets) + v_targets
            if frame.has_scan and frame.az_axis is not None and frame.r_axis is not None:
                scan = frame.scan.astype(np.float32, copy=True)
                sigma_r = max(0.10, (caps.range_max - caps.range_min) / max(1, frame.r_axis.size) * 1.2)
                sigma_a = math.radians(3.5)
                for t in v_targets:
                    r0 = t.rng
                    if r0 < caps.range_min * 0.5 or r0 > caps.range_max * 1.02:
                        continue
                    prof_r = np.exp(-0.5 * ((frame.r_axis - r0) / sigma_r) ** 2).astype(np.float32)
                    if frame.az_axis.size == 1:
                        scan[0] = np.maximum(scan[0], t.snr * prof_r)
                    else:
                        da = (frame.az_axis - t.az + math.pi) % (2 * math.pi) - math.pi
                        prof_a = np.exp(-0.5 * (da / sigma_a) ** 2).astype(np.float32)
                        scan = np.maximum(scan, t.snr * np.outer(prof_a, prof_r))
                frame.scan = np.clip(scan, 0.0, 1.0)

        if self.jam_mode != "none":
            if frame.has_scan:
                self._apply_jamming(frame, caps, dt)
                # 干扰开启时按当前 CFAR 算法重跑检测，以直观对比 CA / OS / GO 抗干扰能力
                frame.targets = pipeline.detect_targets(
                    frame,
                    threshold=threshold,
                    max_targets=max(8, caps.max_targets or 8),
                    cfar_mode=cfar_mode,
                )
            frame.meta["ew"] = self.ew_metrics(caps.range_max)
        if self.fc_sim and frame.targets:
            pri = min(frame.targets, key=lambda t: t.rng)
            sol = compute_fire_control_solution(pri)
            if sol is not None:
                frame.meta["fc_solution"] = sol
        return frame
