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
from .radar_profiles import C_LIGHT, generate_clutter_matrix

# 电子战控制台内部独立可选的「多战场态势环境」，与全局行业场景互相隔离不冲突
EW_ENVIRONMENTS = {
    "ew_air": "空中防空突防（战机编队 + 隐身突防 + 伴飞掩护）",
    "ew_sea": "海面舰艇编队（K 分布强海杂波 + 掠海反舰目标）",
    "ew_urban": "陆地城市峡谷（Weibull 强建筑地杂波 + 低空突防）",
    "ew_convoy": "地面多车集群（多车道交通 + 车载 77G 互扰）",
    "custom": "保留当前雷达目标（纯手动叠加电子干扰）",
}

EW_REGIMES = {
    "ssj": "自卫随行干扰 (SSJ · 单程 R² 掩护本机)",
    "soj": "远距支援干扰 (SOJ · 远端强压制 + 副瓣泄露)",
}

SCENARIOS = {
    "none": "关闭场景自动生成（日常经典雷达）",
    "patrol": "随机出现与巡逻（动态生成/离开）",
    "approach": "高速逼近 / 防撞告警测试",
    "crossing": "多目标交叉会遇（测航迹关联）",
    "marine": "航海避碰演练（航道交叉 / 追越 / 对遇）",
    "automotive": "车载 77G 多车道交通（前车减速 / 邻道切入）",
    "drone": "低空无人机 / 安防周界（悬停 + 突防）",
    "ew": "电子战对抗演练（干扰注入 / 烧穿曲线 / ESM）",
}

# 场景预设 <-> 界面形态 / 扫描模式 / 检测滤波参数 自动协同映射
# "none" 即恢复日常经典雷达显示：关闭专属停靠台、隐藏多余工具，回归纯净监控视图。
SCENARIO_PROFILES: dict[str, dict] = {
    "none": {
        "range_m": 0.0,
        "sim_mode": None,
        "cfar_mode": None,
        "clutter_enabled": False,
        "land_mask": False,
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
        "map_provider": "marine_chart",
        "marine_lat": 30.085,
        "marine_lon": 122.420,
        "enable_ais": True,
        "gate_m": 120.0,
        "filter_mode": "kalman3d",
        "confirm_hits": 2,
        "confirm_window": 4,
        "docks": ("zones",),
        "show_cov": True,
        "banner": "航海海图避碰模式 · 已展开电子海图 / 陆地掩膜 / GO-CFAR 与防区操作台",
    },
    "automotive": {
        "range_m": 80.0,
        "sim_mode": "staring",
        "cfar_mode": "os",
        "clutter_enabled": False,
        "land_mask": False,
        "enable_map": False,
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
        "land_mask": False,
        "enable_map": False,
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
        "land_mask": False,
        "enable_map": False,
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
        "land_mask": False,
        "enable_map": False,
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
        "land_mask": False,
        "enable_map": False,
        "gate_m": 1.5,
        "filter_mode": "kalman3d",
        "confirm_hits": 1,
        "confirm_window": 1,
        "docks": (),
        "show_cov": False,
        "banner": "动态巡逻仿真模式",
    },
    "ew": {
        "range_m": 20000.0,
        "sim_driver": "sim_aesa",
        "cfar_mode": "go",
        "clutter_enabled": False,
        "land_mask": False,
        "enable_map": False,
        "gate_m": 800.0,
        "filter_mode": "kalman3d",
        "confirm_hits": 2,
        "confirm_window": 4,
        "default_jam": "chaff",
        "docks": ("ew",),
        "show_cov": False,
        "banner": "电子战对抗场景 · 已展开电子战控制台 · 可对比 CA / OS / GO-CFAR 与烧穿曲线",
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
    jam_regime: str = "ssj",
    eccm_slc: bool = False,
    pt_w: float = 12000.0,
    gt_dbi: float = 34.0,
    fc_ghz: float = 10.0,
    target_rcs: float = 3.5,
) -> dict:
    """按雷达方程与干信比模型计算真实烧穿距离 R_BT、JSR(dB) 与 ECCM 抑制效果。"""
    raw_jsr = max(0.05, min(1.0, float(jam_jsr)))
    # 瞄准干扰(spot)因功率集中至窄带，同等功率下 JSR 高于宽带阻塞(barrage)
    focus_gain = 1.65 if jam_mode == "spot" else (1.25 if jam_mode == "isrj" else 1.0)
    # 频率捷变重点克制瞄准与间歇采样转发，对宽带/箔条也有部分波形分集收益
    if eccm_agility:
        agil_factor = 0.18 if jam_mode in ("spot", "isrj") else 0.35
    else:
        agil_factor = 1.0
    # 副瓣对消(SLC)重点抑制远距支援(SOJ)旁瓣干扰
    slc_factor = 0.42 if (eccm_slc and jam_regime == "soj") else (0.78 if eccm_slc else 1.0)
    eff_jsr = raw_jsr * focus_gain * agil_factor * slc_factor

    # 结合真实雷达频段/发射功率/天线增益与目标 RCS 计算物理 JSR(dB) 基准与烧穿距离 R_BT
    rmax = max(1.0, float(range_max))
    lam = C_LIGHT / (max(0.5, float(fc_ghz)) * 1e9)
    gt = 10.0 ** (float(gt_dbi) / 10.0)
    sig = max(0.01, float(target_rcs))
    # 等效干扰机有效辐射功率 ERP_j 随归一化强度 0.05~1.0 在 [25W, 4kW] 映射，保证各量程均有可读教学烧穿圈
    pj_gj = max(10.0, float(pt_w) * 0.08 * (eff_jsr / 0.25))
    phys_bt = math.sqrt((max(0.01, float(pt_w)) * (gt ** 2) * (lam ** 2) * sig) / (4.0 * math.pi * pj_gj))
    # 归一化教学烧穿圈同时严格随 eff_jsr 单调反比变化（确保 ECCM 开启后烧穿距离严格增大）
    bt_norm = rmax * min(0.92, 0.78 / (1.0 + 2.4 * eff_jsr))
    bt = bt_norm if phys_bt <= 1.0 or phys_bt > rmax * 3.0 else (0.65 * bt_norm + 0.35 * min(rmax * 0.92, phys_bt))
    jsr_db = round(20.0 * math.log10(max(0.02, eff_jsr) / 0.25), 1)
    return {
        "jam_mode": jam_mode,
        "jam_regime": jam_regime,
        "jsr_eff": round(eff_jsr, 3),
        "jsr_db": jsr_db,
        "burn_through_m": round(max(0.2, bt), 2),
        "jam_az_deg": float(jam_az_deg),
        "jam_bw_deg": float(jam_bw_deg),
        "eccm_agility": bool(eccm_agility),
        "eccm_slc": bool(eccm_slc),
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
    """一个在空间中运动的虚拟电子目标（含可选 3D 高度与垂直速度）。"""

    id: int
    name: str
    x: float
    y: float
    vx: float
    vy: float
    amp: float = 0.88
    respawn: bool = True
    z: float = 0.0
    vz: float = 0.0
    cls: str = ""
    threat: int = 0
    rcs: float = 1.0


class ElectronicStation:
    """动态虚拟目标管理与帧叠加器。"""

    def __init__(self, seed: int = 2026) -> None:
        self._rng = random.Random(seed)
        self._np_rng = np.random.default_rng(seed)
        self._seq = 0
        self.scenario: str = "none"
        self.ew_env: str = "ew_air"
        self.objects: list[SimObject] = []
        self.speed_scale: float = 1.0
        # 教学级电子对抗（纯软件波形/矩阵数学建模，无实际射频发射）
        self.jam_mode: str = "none"
        self.jam_regime: str = "ssj"     # ssj 自卫随行 | soj 远距支援（含副瓣泄露）
        self.jam_jsr: float = 0.55       # 干扰压制/假目标相对强度 0.1~1.0
        self.jam_az_deg: float = 15.0    # 扇区干扰中心方位（度）
        self.jam_bw_deg: float = 36.0    # 扇区干扰波束宽度（度）
        self.eccm_agility: bool = False  # 模拟频率捷变/PRF 参差抗干扰教学开关
        self.eccm_slc: bool = False      # 模拟副瓣对消 (SLC) 抑制 SOJ 旁瓣泄露
        self.fc_sim: bool = False        # 模拟火控前置角引导推演开关（纯软件教学）
        self._jam_t: float = 0.0
        self._last_radar_meta: dict = {}

    def active(self) -> bool:
        return self.scenario != "none" or bool(self.objects) or self.jam_mode != "none" or self.fc_sim

    def set_ew_environment(self, env_key: str, range_max: float = 20000.0) -> None:
        """切换电子战专项场景内部的多战场态势（空中/海面/城市/多车），与外部全局场景隔离。"""
        self.ew_env = env_key if env_key in EW_ENVIRONMENTS else "custom"
        if self.ew_env == "custom":
            return
        self.objects.clear()
        rmax = max(10.0, float(range_max))
        if self.ew_env == "ew_air":
            v = max(6.0, rmax * 0.012)
            self.inject(-rmax * 0.22, rmax * 0.68, v * 0.25, -v, amp=0.90, name="突防战机-1", respawn=True,
                        z=max(12.0, rmax * 0.16), vz=-0.5, cls="敌", threat=3, rcs=3.5)
            self.inject(rmax * 0.16, rmax * 0.52, -v * 0.12, -v * 1.15, amp=0.72, name="隐身突防-2", respawn=True,
                        z=max(8.0, rmax * 0.08), vz=0.0, cls="敌", threat=3, rcs=0.05)
            self.inject(rmax * 0.32, rmax * 0.80, -v * 0.45, -v * 0.55, amp=0.94, name="伴飞干扰机-SOJ", respawn=True,
                        z=max(18.0, rmax * 0.24), vz=0.0, cls="敌", threat=2, rcs=10.0)
        elif self.ew_env == "ew_sea":
            v = max(1.5, min(35.0, rmax * 0.004))
            self.inject(-rmax * 0.30, rmax * 0.56, v * 0.7, -v * 0.4, amp=0.93, name="护卫舰-A", respawn=True,
                        cls="敌", threat=2, rcs=1200.0)
            self.inject(rmax * 0.14, rmax * 0.72, -v * 0.25, -v * 2.1, amp=0.80, name="掠海反舰-B", respawn=True,
                        cls="敌", threat=3, rcs=0.8)
            self.inject(-rmax * 0.12, rmax * 0.32, v * 0.3, v * 0.5, amp=0.84, name="商船-友方", respawn=True,
                        cls="友", threat=0, rcs=600.0)
        elif self.ew_env == "ew_urban":
            v = max(1.0, min(20.0, rmax * 0.006))
            self.inject(rmax * 0.20, rmax * 0.44, -v * 0.65, -v * 0.7, amp=0.78, name="城峡无人机-1", respawn=True,
                        z=max(12.0, rmax * 0.14), vz=0.4, cls="敌", threat=3, rcs=0.08)
            self.inject(-rmax * 0.24, rmax * 0.58, v * 0.85, -v * 0.2, amp=0.82, name="低空渗透-2", respawn=True,
                        z=max(8.0, rmax * 0.10), vz=-0.3, cls="未知", threat=2, rcs=0.25)
        elif self.ew_env == "ew_convoy":
            v = max(1.5, min(18.0, rmax * 0.015))
            self.inject(0.0, rmax * 0.48, 0.0, -v * 0.6, amp=0.92, name="主道头车", respawn=True,
                        cls="未知", threat=2, rcs=18.0)
            self.inject(-max(3.6, rmax * 0.08), rmax * 0.66, v * 0.25, -v * 1.1, amp=0.88, name="左道机动车", respawn=True,
                        cls="敌", threat=2, rcs=12.0)
            self.inject(max(3.6, rmax * 0.08), rmax * 0.34, -0.2, v * 0.7, amp=0.86, name="右道伴随车", respawn=True,
                        cls="未知", threat=1, rcs=24.0)

    def set_scenario(self, scenario: str, range_max: float = 8.0) -> None:
        self.scenario = scenario if scenario in SCENARIOS else "none"
        self.objects.clear()
        rmax = max(2.0, float(range_max))
        if self.scenario == "patrol":
            for _ in range(3):
                self.spawn_random(rmax, respawn=True)
        elif self.scenario == "approach":
            v = max(0.55, rmax * 0.08)
            self.inject(-0.05 * rmax, rmax * 0.84, 0.05 * v, -v, amp=0.92, name="逼近目标-1", respawn=True, threat=2)
            self.inject(rmax * 0.42, rmax * 0.72, -0.55 * v, -0.70 * v, amp=0.84, name="逼近目标-2", respawn=True, threat=1)
        elif self.scenario == "crossing":
            v = max(0.45, rmax * 0.07)
            self.inject(-rmax * 0.55, rmax * 0.48, v, 0.05 * v, amp=0.88, name="横穿-A", respawn=True)
            self.inject(rmax * 0.55, rmax * 0.50, -v, -0.04 * v, amp=0.86, name="横穿-B", respawn=True)
            self.inject(0.08 * rmax, rmax * 0.28, 0.15 * v, 0.55 * v, amp=0.80, name="纵向-C", respawn=True)
        elif self.scenario == "marine":
            # 真实近海避碰速度 3~8 m/s (约 6~15 节)
            self.inject(-rmax * 0.42, rmax * 0.65, 5.2, -2.1, amp=0.90, name="商船-右舷避让", respawn=True, cls="友")
            self.inject(0.22 * rmax, rmax * 0.78, -1.2, -6.8, amp=0.88, name="货轮-对遇", respawn=True, cls="未知", threat=1)
            self.inject(-0.16 * rmax, rmax * 0.34, 0.6, 2.8, amp=0.78, name="渔船-慢速前航", respawn=True, cls="友")
        elif self.scenario == "automotive":
            # 真实城市/快速路 77G 前向车流 (y = 20~65m, 速度 5~14 m/s)
            self.inject(0.0, rmax * 0.58, 0.0, -4.5, amp=0.92, name="本道前车", respawn=True, threat=2)
            self.inject(-3.6, rmax * 0.76, 1.4, -9.5, amp=0.85, name="左道切入", respawn=True, threat=1)
            self.inject(3.7, rmax * 0.36, -0.3, 6.2, amp=0.82, name="右道超车", respawn=True)
        elif self.scenario == "drone":
            # 真实低空 3D 无人机（仅 2 批高价值目标，含真实高度 z 与爬升/俯冲 vz）
            self.inject(rmax * 0.44, rmax * 0.74, -5.8, -6.2, amp=0.84, name="突防低飞-1", respawn=True,
                        z=18.0, vz=-0.65, cls="敌", threat=3, rcs=0.06)
            self.inject(-rmax * 0.28, rmax * 0.50, 1.8, 1.2, amp=0.76, name="巡检悬停-2", respawn=True,
                        z=32.0, vz=0.35, cls="未知", threat=1, rcs=0.08)
        elif self.scenario == "ew":
            self.set_ew_environment(self.ew_env, rmax)

    def inject(
        self,
        x: float,
        y: float,
        vx: float,
        vy: float,
        amp: float = 0.88,
        name: str = "",
        respawn: bool = False,
        z: float = 0.0,
        vz: float = 0.0,
        cls: str = "",
        threat: int = 0,
        rcs: float = 1.0,
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
            z=float(z),
            vz=float(vz),
            cls=str(cls),
            threat=int(threat),
            rcs=max(0.01, float(rcs)),
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
            if o.vz:
                o.z += o.vz * dt
                if o.z < 4.0 or o.z > 65.0:
                    o.vz = -o.vz
                    o.z = max(4.0, min(65.0, o.z))
            r = math.hypot(o.x, o.y)
            max_r = rmax * 0.96
            out_of_fov = (self.scenario == "automotive" and (o.y < 3.0 or abs(o.x) > rmax * 0.55))
            if r > max_r or r < 0.25 or out_of_fov:
                if self.scenario == "automotive" and o.respawn:
                    # 车载场景：车辆保持在前方 ±8m 车道内有序循环出现，不跑到船用 360° 后方
                    o.x = float(self._rng.choice((-3.6, 0.0, 3.6)))
                    o.y = rmax * self._rng.uniform(0.75, 0.92) if o.vy <= 0 else rmax * 0.15
                    kept.append(o)
                elif o.respawn:
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
                    o.vx = -o.vx
                    o.vy = -o.vy
                    o.x = max(-rmax * 0.9, min(rmax * 0.9, o.x))
                    o.y = max(0.3, min(rmax * 0.9, o.y))
                    kept.append(o)
            else:
                kept.append(o)
        self.objects = kept
        out_t: list[Target] = []
        for o in self.objects:
            rr = math.hypot(o.x, o.y)
            el = math.degrees(math.atan2(o.z, max(1e-3, rr))) if o.z else 0.0
            out_t.append(Target(
                x=o.x, y=o.y, vx=o.vx, vy=o.vy, snr=o.amp,
                z=o.z, el_deg=el, cls=o.cls, threat=o.threat,
            ))
        return out_t

    def ew_metrics(self, range_max: float) -> dict:
        meta = getattr(self, "_last_radar_meta", None) or {}
        rcs = max([float(getattr(o, "rcs", 1.0)) for o in self.objects], default=3.5)
        return compute_ew_metrics(
            self.jam_mode, self.jam_jsr, range_max,
            self.jam_az_deg, self.jam_bw_deg, self.eccm_agility,
            jam_regime=self.jam_regime,
            eccm_slc=self.eccm_slc,
            pt_w=float(meta.get("pt_w", 12000.0)),
            gt_dbi=float(meta.get("gt_dbi", 34.0)),
            fc_ghz=float(meta.get("fc_ghz", 10.0)),
            target_rcs=rcs,
        )

    def _apply_jamming(
        self,
        frame: RadarFrame,
        caps: Capabilities,
        dt: float,
    ) -> None:
        """在软件层向扫描/RD矩阵注入符合雷达方程与物理规律的电子对抗波形。"""
        if self.jam_mode == "none" or not frame.has_scan or frame.scan is None:
            return
        self._jam_t += max(0.01, min(0.5, float(dt)))
        scan = frame.scan.astype(np.float32, copy=True)
        n_az, n_r = scan.shape
        raw_jsr = max(0.1, min(1.0, float(self.jam_jsr)))
        agil = 0.22 if (self.eccm_agility and self.jam_mode in ("spot", "isrj")) else (0.35 if self.eccm_agility else 1.0)
        slc = 0.45 if (self.eccm_slc and self.jam_regime == "soj") else 1.0
        jsr = raw_jsr * agil * slc

        if self.jam_mode == "barrage":
            noise = self._np_rng.uniform(0.0, jsr * 0.72, size=scan.shape).astype(np.float32)
            scan = np.maximum(scan, noise)
        elif self.jam_mode == "spot" and frame.az_axis is not None:
            az_c = math.radians(self.jam_az_deg)
            bw = math.radians(max(8.0, self.jam_bw_deg))
            da = (frame.az_axis - az_c + math.pi) % (2 * math.pi) - math.pi
            mask_a = np.exp(-0.5 * (da / (bw * 0.42)) ** 2).astype(np.float32)[:, None]
            # SOJ 远距支援干扰在不开启 SLC 副瓣对消时会通过天线旁瓣渗漏到邻近方位
            if self.jam_regime == "soj" and not self.eccm_slc:
                sidelobe = 0.28 * np.abs(np.cos(da * 4.0))[:, None].astype(np.float32)
                mask_a = np.maximum(mask_a, sidelobe)
            strobe = self._np_rng.uniform(0.32, 1.0, size=scan.shape).astype(np.float32)
            scan = np.maximum(scan, min(1.0, jsr * 1.15) * mask_a * strobe)
        elif self.jam_mode == "chaff" and frame.az_axis is not None and frame.r_axis is not None:
            # 箔条云随风缓慢漂移并扩散，RD 图中在低多普勒(风速)处展宽
            az_drift = self.jam_az_deg + math.sin(self._jam_t * 0.15) * 4.0
            az_c = math.radians(az_drift)
            spread = 1.0 + min(0.85, self._jam_t * 0.08)
            bw = math.radians(max(14.0, self.jam_bw_deg * 0.85 * spread))
            da = (frame.az_axis - az_c + math.pi) % (2 * math.pi) - math.pi
            prof_a = np.exp(-0.5 * (da / (bw * 0.5)) ** 2).astype(np.float32)
            r_center = caps.range_min + (caps.range_max - caps.range_min) * min(0.75, 0.46 + 0.01 * self._jam_t)
            r_sigma = max(0.35, (caps.range_max - caps.range_min) * 0.11 * spread)
            prof_r = np.exp(-0.5 * ((frame.r_axis - r_center) / r_sigma) ** 2).astype(np.float32)
            raw_sp = self._np_rng.uniform(0.76, 1.0, size=scan.shape).astype(np.float32)
            cloud = (0.45 + 0.50 * jsr) * np.outer(prof_a, prof_r) * raw_sp
            cloud = 0.5 * cloud + 0.25 * np.roll(cloud, 1, axis=0) + 0.25 * np.roll(cloud, 1, axis=1)
            scan = np.maximum(scan, cloud)
            if frame.rd is not None and frame.rd_v_axis is not None:
                rd = frame.rd.astype(np.float32, copy=True)
                vmax = max(1.0, float(np.max(np.abs(frame.rd_v_axis))))
                pv = np.exp(-0.5 * ((frame.rd_v_axis - 3.0) / max(1.5, vmax * 0.07)) ** 2).astype(np.float32)
                rd = np.maximum(rd, (0.35 + 0.45 * jsr) * np.outer(pv, prof_r))
                frame.rd = np.clip(rd, 0.0, 1.0)
        elif self.jam_mode in ("drfm", "rgpo", "isrj") and frame.az_axis is not None and frame.r_axis is not None:
            anchors = frame.targets[:2] if frame.targets else [
                Target(x=0.0, y=max(caps.range_min + 0.5, caps.range_max * 0.35), snr=0.85)
            ]
            sigma_r = max(0.10, (caps.range_max - caps.range_min) / max(1, n_r) * 1.1)
            sigma_a = math.radians(3.2)
            span_r = max(0.5, caps.range_max - caps.range_min)
            rd = frame.rd.astype(np.float32, copy=True) if (frame.rd is not None and frame.rd_v_axis is not None) else None
            for t in anchors:
                da = (frame.az_axis - t.az + math.pi) % (2 * math.pi) - math.pi
                prof_a = np.exp(-0.5 * (da / sigma_a) ** 2).astype(np.float32)
                if self.jam_mode == "drfm":
                    step_r = span_r * 0.14
                    offsets = (step_r, 2.0 * step_r, -step_r)
                    amp_j = 0.55 + 0.40 * jsr
                elif self.jam_mode == "rgpo":
                    # 真实 RGPO：先捕俘波门(1.5s)，随后按 ΔR = 0.5*a*t² 抛物线向外加速拖引，循环重置
                    cyc = self._jam_t % 6.5
                    pull = 0.0 if cyc < 1.2 else (((cyc - 1.2) / 5.3) ** 2) * (span_r * 0.52)
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
                        if rd is not None and frame.rd_v_axis is not None:
                            vr = (t.x * t.vx + t.y * t.vy) / max(1e-3, t.rng)
                            vmax = max(1.0, float(np.max(np.abs(frame.rd_v_axis))))
                            pv = np.exp(-0.5 * ((frame.rd_v_axis - vr) / max(1.0, vmax * 0.06)) ** 2).astype(np.float32)
                            rd = np.maximum(rd, amp_j * np.outer(pv, prof_r))
            if rd is not None:
                frame.rd = np.clip(rd, 0.0, 1.0)
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

    @staticmethod
    def _stamp_targets(
        scan: np.ndarray,
        az_axis: np.ndarray,
        r_axis: np.ndarray,
        caps: Capabilities,
        targets: list[Target],
    ) -> np.ndarray:
        sigma_r = max(0.10, (caps.range_max - caps.range_min) / max(1, r_axis.size) * 1.2)
        sigma_a = math.radians(3.5)
        for t in targets:
            r0 = t.rng
            if r0 < caps.range_min * 0.5 or r0 > caps.range_max * 1.02:
                continue
            prof_r = np.exp(-0.5 * ((r_axis - r0) / sigma_r) ** 2).astype(np.float32)
            if az_axis.size == 1:
                scan[0] = np.maximum(scan[0], t.snr * prof_r)
            else:
                da = (az_axis - t.az + math.pi) % (2 * math.pi) - math.pi
                prof_a = np.exp(-0.5 * (da / sigma_a) ** 2).astype(np.float32)
                scan = np.maximum(scan, t.snr * np.outer(prof_a, prof_r))
        return scan

    def merge_into_frame(
        self,
        frame: RadarFrame,
        caps: Capabilities,
        dt: float,
        threshold: float = 0.30,
        cfar_mode: str = "ca",
    ) -> RadarFrame:
        """将活跃虚拟目标与电子对抗波形叠加到 RadarFrame，并在干扰开启时重跑 CFAR 检出。"""
        if frame.meta:
            self._last_radar_meta = dict(frame.meta)
        v_targets = self.step(dt, caps.range_max)
        # 专有仿真场景下替代模拟器泛用室内杂点，还原贴合实战的目标数量与空间杂波特征
        ew_exclusive = (
            caps.source == "sim"
            and self.scenario == "ew"
            and self.ew_env != "custom"
            and bool(v_targets)
        )
        exclusive_sim = (
            caps.source == "sim"
            and (self.scenario in ("marine", "automotive", "drone", "crossing", "approach") or ew_exclusive)
            and bool(v_targets)
        )
        if exclusive_sim and frame.az_axis is not None and frame.r_axis is not None:
            frame.targets = list(v_targets)
            frame.r_axis = np.linspace(caps.range_min, caps.range_max, frame.r_axis.size, dtype=np.float32)
            base_shape = frame.scan.shape if frame.scan is not None else (frame.az_axis.size, frame.r_axis.size)
            clutter_kind = (
                {"ew_sea": "sea_k", "ew_urban": "urban_weibull", "ew_convoy": "urban_weibull"}.get(self.ew_env, "air_thermal")
                if self.scenario == "ew"
                else ("sea_k" if self.scenario == "marine" else ("urban_weibull" if self.scenario == "drone" else "air_thermal"))
            )
            scan = generate_clutter_matrix(clutter_kind, base_shape[0], frame.r_axis, self._np_rng, scale=0.65)
            scan = self._stamp_targets(scan, frame.az_axis, frame.r_axis, caps, v_targets)
            frame.scan = np.clip(scan, 0.0, 1.0)
            if frame.rd is not None and frame.rd_v_axis is not None:
                rd = np.zeros((frame.rd_v_axis.size, frame.r_axis.size), dtype=np.float32)
                rd += self._np_rng.normal(0.01, 0.005, size=rd.shape).astype(np.float32)
                if clutter_kind in ("sea_k", "urban_weibull"):
                    iv0 = int(np.argmin(np.abs(frame.rd_v_axis)))
                    rd[max(0, iv0 - 1) : min(rd.shape[0], iv0 + 2), :] = np.maximum(
                        rd[max(0, iv0 - 1) : min(rd.shape[0], iv0 + 2), :],
                        0.28 * scan.mean(axis=0)[None, :],
                    )
                vmax = max(1.0, float(np.max(np.abs(frame.rd_v_axis))))
                for vt in v_targets:
                    vr = (vt.x * vt.vx + vt.y * vt.vy) / max(1e-3, vt.rng)
                    iv = int(np.argmin(np.abs(frame.rd_v_axis - vr)))
                    ir = int(np.argmin(np.abs(frame.r_axis - vt.rng)))
                    rd[max(0, iv - 1) : min(rd.shape[0], iv + 2), max(0, ir - 1) : min(rd.shape[1], ir + 2)] = np.maximum(
                        rd[max(0, iv - 1) : min(rd.shape[0], iv + 2), max(0, ir - 1) : min(rd.shape[1], ir + 2)],
                        vt.snr,
                    )
                frame.rd = np.clip(rd, 0.0, 1.0)
            if frame.rhi is not None and frame.el_axis is not None:
                rhi = np.zeros((frame.el_axis.size, frame.r_axis.size), dtype=np.float32)
                for vt in v_targets:
                    el_r = math.radians(vt.el_deg)
                    pe = np.exp(-0.5 * ((frame.el_axis - el_r) / math.radians(3.5)) ** 2).astype(np.float32)
                    pr = np.exp(-0.5 * ((frame.r_axis - vt.rng) / max(0.5, caps.range_max * 0.02)) ** 2).astype(np.float32)
                    rhi = np.maximum(rhi, vt.snr * np.outer(pe, pr))
                frame.rhi = np.clip(rhi, 0.0, 1.0)
        elif v_targets:
            frame.targets = list(frame.targets) + v_targets
            if frame.has_scan and frame.az_axis is not None and frame.r_axis is not None:
                scan = frame.scan.astype(np.float32, copy=True)
                scan = self._stamp_targets(scan, frame.az_axis, frame.r_axis, caps, v_targets)
                frame.scan = np.clip(scan, 0.0, 1.0)

        if self.jam_mode != "none":
            if frame.has_scan:
                self._apply_jamming(frame, caps, dt)
                # 干扰开启时按当前 CFAR 算法重跑检测（限制峰值数量防止单云碎裂爆量）
                det_cap = max(4, min(10, (caps.max_targets or 6)))
                frame.targets = pipeline.detect_targets(
                    frame,
                    threshold=threshold,
                    max_targets=det_cap,
                    cfar_mode=cfar_mode,
                )
            frame.meta["ew"] = self.ew_metrics(caps.range_max)
        if self.fc_sim and frame.targets:
            pri = min(frame.targets, key=lambda t: t.rng)
            sol = compute_fire_control_solution(pri)
            if sol is not None:
                frame.meta["fc_solution"] = sol
        return frame
