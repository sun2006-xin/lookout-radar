"""真实雷达物理参数库与雷达方程 / 起伏 / 杂波模型（纯 NumPy，零界面依赖）。

参数均依据公开器件手册与雷达工程文献整理，用于在无实体雷达硬件时进行高保真数字孪生仿真：
  1. TI AWR1843 (76~81 GHz 车载 FMCW 毫米波雷达)：~16 dBm EIRP，4 GHz 带宽，10 dB NF
  2. HLK-LD2450 (24~24.25 GHz 人体跟踪毫米波)：±60°×±35°，6 m，1TX/2RX
  3. Furuno FAR-2xx8 (9.41 GHz X 波段航海 ARPA)：25 kW 磁控管，31 dBi 波导裂缝，K 分布海杂波
  4. Ouster OS1-64 (905 nm 64 线 3D 数字激光雷达)：360°×45° FOV，90~120 m @10% 反射率
  5. 机载 X 波段多功能相控阵 AESA (10 GHz AN/APG 级)：12 kW 等效峰值，34 dBi，-32 dB 副瓣，TWS+RD+3D
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

C_LIGHT = 299_792_458.0
K_BOLTZMANN = 1.380649e-23
T0_KELVIN = 290.0


@dataclass(frozen=True, slots=True)
class RadarProfile:
    id: str
    label: str
    category: str             # automotive | marine | lidar | aesa | indoor
    fc_ghz: float             # 载频 GHz（光雷达取 331000 GHz 等效）
    bw_mhz: float             # 接收机/调制带宽 MHz
    pt_w: float               # 发射峰值功率 W
    gt_dbi: float             # 天线增益 dBi
    nf_db: float              # 接收机噪声系数 dB
    sll_db: float             # 第一副瓣电平 dB
    az_fov_deg: float         # 方位视场角（度）
    az_res_deg: float         # 方位角分辨率（度）
    el_fov_deg: float         # 俯仰视场（度，0 表示无俯仰测量）
    range_min: float          # 最小距离 m
    range_max: float          # 典型作用距离 m
    v_max_ms: float           # 最大不模糊速度 m/s
    scan_mode: str            # rotating | staring
    scan_rate_hz: float       # 帧率/扫描刷新率 Hz
    clutter_model: str        # sea_k | urban_weibull | air_thermal | indoor
    has_doppler: bool
    has_elevation: bool
    has_iff: bool
    tier: str                 # sensor | rd | aesa
    reference_doc: str
    # (name, x, y, vx, vy, rcs_m2, z, vz, iff, threat)
    default_targets: tuple[tuple[str, float, float, float, float, float, float, float, str, int], ...]


RADAR_PROFILES: dict[str, RadarProfile] = {
    "sim_awr1843": RadarProfile(
        id="sim_awr1843",
        label="数字孪生 · TI AWR1843 (77G 车载 FMCW)",
        category="automotive",
        fc_ghz=77.0,
        bw_mhz=4000.0,
        pt_w=0.04,             # ~16 dBm EIRP
        gt_dbi=10.0,
        nf_db=10.0,
        sll_db=-20.0,
        az_fov_deg=120.0,
        az_res_deg=1.5,
        el_fov_deg=0.0,
        range_min=0.3,
        range_max=80.0,
        v_max_ms=45.0,
        scan_mode="staring",
        scan_rate_hz=20.0,
        clutter_model="urban_weibull",
        has_doppler=True,
        has_elevation=False,
        has_iff=False,
        tier="rd",
        reference_doc="TI AWR1843 Datasheet (76-81 GHz, 3TX/4RX, NF 10 dB, EIRP ~16 dBm)",
        default_targets=(
            ("前车-SUV", 0.0, 38.0, 0.0, -5.5, 18.0, 0.0, 0.0, "车辆", 2),
            ("左道超车", -3.6, 56.0, 1.3, -11.5, 12.0, 0.0, 0.0, "车辆", 1),
            ("右道货车", 3.8, 28.0, -0.2, 6.8, 45.0, 0.0, 0.0, "重车", 0),
            ("斑马线行人", 2.2, 16.0, -1.2, 0.2, 0.8, 0.0, 0.0, "行人", 2),
        ),
    ),
    "sim_ld2450": RadarProfile(
        id="sim_ld2450",
        label="数字孪生 · HLK-LD2450 (24G 室内测距)",
        category="indoor",
        fc_ghz=24.125,
        bw_mhz=250.0,
        pt_w=0.01,
        gt_dbi=6.0,
        nf_db=8.0,
        sll_db=-16.0,
        az_fov_deg=120.0,
        az_res_deg=4.0,
        el_fov_deg=0.0,
        range_min=0.2,
        range_max=6.0,
        v_max_ms=5.0,
        scan_mode="staring",
        scan_rate_hz=10.0,
        clutter_model="indoor",
        has_doppler=True,
        has_elevation=False,
        has_iff=False,
        tier="sensor",
        reference_doc="HLK-LD2450 Manual (24-24.25 GHz, ±60° Az, 6 m, 256kbps UART)",
        default_targets=(
            ("人员-1", -0.8, 2.6, 0.35, -0.45, 0.7, 0.0, 0.0, "人体", 1),
            ("人员-2", 1.4, 4.1, -0.22, 0.30, 0.65, 0.0, 0.0, "人体", 0),
        ),
    ),
    "sim_furuno": RadarProfile(
        id="sim_furuno",
        label="数字孪生 · Furuno FAR-2xx8 (X波段航海 ARPA)",
        category="marine",
        fc_ghz=9.41,
        bw_mhz=20.0,
        pt_w=25000.0,          # 25 kW 磁控管
        gt_dbi=31.0,           # ~1.2° 波导裂缝阵
        nf_db=5.0,
        sll_db=-26.0,
        az_fov_deg=360.0,
        az_res_deg=1.2,
        el_fov_deg=0.0,
        range_min=20.0,
        range_max=6000.0,
        v_max_ms=25.0,
        scan_mode="rotating",
        scan_rate_hz=12.0,
        clutter_model="sea_k",
        has_doppler=False,
        has_elevation=False,
        has_iff=False,
        tier="sensor",
        reference_doc="Furuno FAR-2xx8 Series (9410 MHz X-Band, 25 kW, 1.2° H-Beam, 24 rpm)",
        default_targets=(
            ("集装箱轮-A", -1850.0, 3200.0, 6.2, -3.4, 4200.0, 0.0, 0.0, "商船", 1),
            ("散货轮-B", 1100.0, 4400.0, -2.5, -7.8, 2600.0, 0.0, 0.0, "商船", 2),
            ("近岸渔船-C", -620.0, 1450.0, 2.2, 3.1, 18.0, 0.0, 0.0, "渔船", 0),
            ("航道浮标", 880.0, 1120.0, 0.0, 0.0, 4.5, 0.0, 0.0, "航标", 0),
        ),
    ),
    "sim_ouster": RadarProfile(
        id="sim_ouster",
        label="数字孪生 · Ouster OS1-64 (64线 3D 激光雷达)",
        category="lidar",
        fc_ghz=331_000.0,      # 905 nm 近红外激光
        bw_mhz=1000.0,
        pt_w=10.0,
        gt_dbi=42.0,
        nf_db=4.0,
        sll_db=-35.0,
        az_fov_deg=360.0,
        az_res_deg=1.0,
        el_fov_deg=45.0,
        range_min=0.5,
        range_max=120.0,
        v_max_ms=40.0,
        scan_mode="rotating",
        scan_rate_hz=10.0,
        clutter_model="urban_weibull",
        has_doppler=False,
        has_elevation=True,
        has_iff=False,
        tier="aesa",
        reference_doc="Ouster OS1-64 Rev7 (64ch, 360°×45° FOV, 90-120 m @10% Lambertian)",
        default_targets=(
            ("巡检四旋翼", 28.0, 62.0, -3.5, -4.2, 0.08, 16.0, 0.45, "UAV", 2),
            ("园区无人车", -18.0, 44.0, 4.0, 1.5, 8.0, 1.4, 0.0, "UGV", 0),
            ("低空穿梭机", 46.0, 88.0, -7.0, -6.5, 0.05, 34.0, -0.8, "UAV", 3),
        ),
    ),
    "sim_air_aesa": RadarProfile(
        id="sim_air_aesa",
        label="数字孪生 · 机载 X 波段 AESA (AN/APG级 3D+RD)",
        category="aesa",
        fc_ghz=10.0,
        bw_mhz=50.0,
        pt_w=12000.0,
        gt_dbi=34.0,
        nf_db=3.5,
        sll_db=-32.0,
        az_fov_deg=120.0,
        az_res_deg=1.8,
        el_fov_deg=45.0,
        range_min=300.0,
        range_max=40000.0,
        v_max_ms=450.0,
        scan_mode="staring",
        scan_rate_hz=15.0,
        clutter_model="air_thermal",
        has_doppler=True,
        has_elevation=True,
        has_iff=True,
        tier="aesa",
        reference_doc="Airborne X-Band AESA (10 GHz, 12 kW TRM, 34 dBi, -32 dB SLL, TWS+RD+3D)",
        default_targets=(
            ("歼击机编队-1", -6800.0, 26000.0, 45.0, -240.0, 4.2, 5200.0, -18.0, "敌", 3),
            ("隐身巡航-2", 3400.0, 19500.0, -25.0, -285.0, 0.012, 1800.0, -6.0, "敌", 3),
            ("伴飞干扰机-SOJ", -11500.0, 31000.0, 30.0, -165.0, 12.0, 7400.0, 0.0, "敌", 2),
            ("护航运输机", 9200.0, 24000.0, -35.0, -135.0, 28.0, 6100.0, 0.0, "友", 0),
        ),
    ),
}


def radar_snr_db(
    profile: RadarProfile,
    range_m: float,
    rcs_m2: float,
    swerling: int = 1,
    rng: np.random.Generator | None = None,
) -> float:
    """按雷达方程与 Swerling 1/3 起伏统计模型计算单帧信噪比 (dB)。"""
    r = max(profile.range_min, float(range_m))
    sig_avg = max(1e-4, float(rcs_m2))
    if rng is not None:
        if swerling == 3:
            sig = sig_avg * float(rng.gamma(2.0, 0.5))
        elif swerling == 1:
            sig = sig_avg * float(rng.exponential(1.0))
        else:
            sig = sig_avg
    else:
        sig = sig_avg

    if profile.category == "lidar":
        # 905 nm 激光测距遵循 1/R² Lambertian 漫反射能量衰减，90-120m @10% 对应约 +12 dB SNR
        r_ref = profile.range_max * 0.85
        ref_val = max(0.02, min(1.0, sig_avg / 10.0))
        snr_lin = (ref_val / 0.10) * ((r_ref / r) ** 2) * 16.0 * (sig / sig_avg)
        return 10.0 * math.log10(max(1e-3, snr_lin))

    lam = C_LIGHT / (max(0.1, profile.fc_ghz) * 1e9)
    gt = 10.0 ** (profile.gt_dbi / 10.0)
    nf = 10.0 ** (profile.nf_db / 10.0)
    loss = 10.0 ** (4.0 / 10.0)  # 4 dB 典型系统损耗
    pr = (profile.pt_w * (gt ** 2) * (lam ** 2) * sig) / (((4.0 * math.pi) ** 3) * (r ** 4) * loss)
    noise = K_BOLTZMANN * T0_KELVIN * (profile.bw_mhz * 1e6) * nf
    # 相干积累 / 脉冲压缩增益（按典型 CPI 16~64 脉冲等效 +15 dB）
    proc_gain = 10.0 ** 1.5
    return 10.0 * math.log10(max(1e-6, (pr * proc_gain) / max(1e-30, noise)))


def snr_to_intensity(snr_db: float) -> float:
    """将物理 SNR(dB) 对数压缩映射到显控归一化回波幅度 [0.06, 1.0]。"""
    return float(max(0.06, min(1.0, (float(snr_db) + 4.0) / 36.0)))


def generate_clutter_matrix(
    clutter_model: str,
    n_az: int,
    r_axis: np.ndarray,
    rng: np.random.Generator,
    scale: float = 1.0,
) -> np.ndarray:
    """生成符合海杂波 K-分布 / 地杂波 Weibull 的物理统计杂波基底矩阵。"""
    n_r = int(r_axis.size)
    if n_az <= 0 or n_r <= 0:
        return np.zeros((max(1, n_az), max(1, n_r)), dtype=np.float32)
    rmax = max(1.0, float(r_axis[-1]))
    rn = np.clip(r_axis / rmax, 0.02, 1.0).astype(np.float32)

    if clutter_model == "sea_k":
        # 海杂波 K 分布：慢变 Gamma 纹理 × 快变 Rayleigh 斑点，近中距离保留海浪纹理与偶发海尖峰
        texture = rng.gamma(shape=1.5, scale=1.0, size=(n_az, n_r)).astype(np.float32)
        speckle = rng.rayleigh(scale=0.034, size=(n_az, n_r)).astype(np.float32)
        decay = (1.0 / (1.0 + (rn * 2.4) ** 1.8))[None, :]
        mat = np.clip(texture * speckle * decay * scale, 0.0, 0.48)
        return mat.astype(np.float32)

    if clutter_model == "urban_weibull":
        # 城市/陆地杂波：Weibull 重尾散射 + 沿方位-距离不均匀衰减
        w_raw = rng.weibull(0.82, size=(n_az, n_r)).astype(np.float32) * 0.026
        decay = (1.0 / (1.0 + (rn * 2.5) ** 1.5))[None, :]
        return np.clip(w_raw * decay * scale, 0.0, 0.45).astype(np.float32)

    if clutter_model == "air_thermal":
        return np.clip(rng.rayleigh(scale=0.012 * scale, size=(n_az, n_r)), 0.0, 0.25).astype(np.float32)

    # indoor
    return np.clip(rng.rayleigh(scale=0.020 * scale, size=(n_az, n_r)), 0.0, 0.35).astype(np.float32)
