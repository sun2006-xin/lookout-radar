"""统一数据模型。

框架只认两种原料：
    1. scan    强度矩阵 I(方位, 距离)  —— 扫描型雷达的原生输出
    2. targets 目标列表 {位置, 速度, snr} —— 跟踪型雷达的原生输出

任何驱动只要填满其中之一即可，缺失的部分由 pipeline 补齐。
这是整个软件"通用"的地基：视图层永远不需要知道下面接的是什么雷达。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import atan2, degrees, hypot
from typing import Any

import numpy as np


@dataclass(slots=True)
class Target:
    """一个目标。坐标以雷达为原点，单位米；x 向右为正，y 向前为正。"""

    x: float = 0.0
    y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    snr: float = 1.0          # 归一化信噪比 0~1，只用于显示强弱
    tid: int = -1             # 航迹号，-1 表示尚未建立航迹
    age: int = 0              # 连续命中帧数
    missed: float = 0.0       # 距上次命中的时间，秒（不是帧数！旋转雷达一圈才照到一次）
    locked: bool = False      # 是否被操作员锁定（STT）
    views: int = 1            # 这个目标被几个站同时看到（融合后才有意义）
    threat: int = 0           # 威胁等级 0无 1低 2中 3高，高端雷达会给出
    cls: str = ""             # 目标分类（敌/我/未知/民航…），支持 IFF 的雷达才有
    rcs: float = 0.0          # 雷达截面积估值，dBsm；0 表示平台不提供
    z: float = 0.0            # 高度 / 海拔（米），支持 3D 俯仰雷达时有效
    el_deg: float = 0.0       # 俯仰角（度），向上为正

    @property
    def rng(self) -> float:
        """斜距，米。"""
        return hypot(self.x, self.y)

    @property
    def az(self) -> float:
        """方位角，弧度；正上方为 0，顺时针为正。"""
        return atan2(self.x, self.y)

    @property
    def az_deg(self) -> float:
        return degrees(self.az)

    @property
    def speed(self) -> float:
        return hypot(self.vx, self.vy)

    @property
    def course_deg(self) -> float:
        """航向角（弧度转度），无速度时返回 0。"""
        if self.speed < 1e-6:
            return 0.0
        return degrees(atan2(self.vx, self.vy))


@dataclass(slots=True)
class RadarFrame:
    """一帧雷达数据。timestamp 用 time.monotonic()，全驱动统一时间基准。"""

    timestamp: float = 0.0
    scan: np.ndarray | None = None        # (n_az, n_rng) float32，取值 0~1
    az_axis: np.ndarray | None = None     # (n_az,)   弧度
    r_axis: np.ndarray | None = None      # (n_rng,)  米
    targets: list[Target] = field(default_factory=list)
    # 距离-多普勒图 (n_doppler, n_range)。只有能提供相干积累的雷达才给，
    # 存在感应模块拿不到，脉冲多普勒 / FMCW 相参雷达才有。
    rd: np.ndarray | None = None
    rd_v_axis: np.ndarray | None = None   # (n_doppler,) 速度轴，m/s，负=远离
    rhi: np.ndarray | None = None         # (n_el, n_rng) 距离-高度/俯仰剖面矩阵
    el_axis: np.ndarray | None = None     # (n_el,) 俯仰角轴，弧度
    source: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def has_scan(self) -> bool:
        return self.scan is not None and self.scan.size > 0

    @property
    def has_targets(self) -> bool:
        return len(self.targets) > 0


@dataclass(slots=True)
class Capabilities:
    """驱动能力声明。

    这是"通用软件"的灵魂：界面根据它自动启用/禁用显示模式，
    而不是把所有控件都摆上去然后灰一片。

    刻意做得比现在的硬件更强：一台几十块的感应模块只填 3 个字段，
    一台相控阵雷达可以把 12 个字段全填满，界面会自动多出 RD 图、
    威胁分级、IFF 分类这些面板——不需要改一行 UI 代码。
    """

    name: str = "unknown"
    # ---- 分类（界面按它分栏，不要按型号分）----
    #: 数据来源：sim 模拟 / device 实测 / replay 回放 / fusion 组合
    source: str = "device"
    #: 测量形态：point 单点测距 / sector 受限扇区 / omni 全向 360 /
    #: tracking 直接给目标 / rd 距离-多普勒 / fusion 多设备
    form: str = "sector"
    #: 厂商或系列名，仅用于展示，不参与任何逻辑分支
    vendor: str = ""
    # ---- 数据能力 ----
    has_scan: bool = False          # 能给出强度矩阵
    has_azimuth: bool = False       # 矩阵带真实方位维（否则只有距离维）
    has_targets: bool = False       # 能直接给出目标
    has_velocity: bool = False      # 目标带速度
    has_range_doppler: bool = False # 能给出距离-多普勒图
    has_elevation: bool = False     # 三维（带俯仰）
    has_iff: bool = False           # 目标带敌我识别
    has_threat: bool = False        # 目标带威胁等级
    # ---- 几何 ----
    az_fov: float = 120.0           # 无方位雷达用来把单波束摊成扇形，单位度
    az_resolution: float = 0.0      # 方位分辨，度；0 表示未知
    range_min: float = 0.15
    range_max: float = 6.0
    max_targets: int = 0
    # ---- 工作方式 ----
    scan_mode: str = "staring"      # staring（凝视，整幅同时更新） | rotating（旋转扫描）
    scan_rate: float = 10.0         # 标称帧率 Hz
    tier: str = "sensor"            # sensor | tracking | aesa，纯提示用
    transports: tuple[str, ...] = ("serial",)
    views: tuple[str, ...] = ("ppi", "ascope", "waterfall", "tracks")
    notes: str = ""

    def supports(self, view: str) -> bool:
        return view in self.views

    def summary(self) -> str:
        parts = []
        if self.has_scan:
            parts.append("矩阵" + ("(方位)" if self.has_azimuth else "(距离)"))
        if self.has_targets:
            parts.append(f"目标≤{self.max_targets or '?'}")
        if self.has_velocity:
            parts.append("速度")
        if self.has_range_doppler:
            parts.append("RD")
        if self.has_elevation:
            parts.append("3D俯仰")
        if self.has_iff:
            parts.append("IFF")
        if self.has_threat:
            parts.append("威胁分级")
        return " · ".join(parts) or "无可显示数据"


#: 协议抽象层 Meta Schema（ROADMAP C4）：
#: 键名 -> (允许的类型, 默认值)。缺少可选标准键时可填默认，类型错误时报错。
META_SCHEMA: dict[str, tuple[type | tuple[type, ...], Any]] = {
    "scan_period": ((int, float), 0.1),
    "mode": (str, "staring"),
    "state": (str, ""),
    "fresh_az": ((tuple, list), None),
    "sensors": (list, None),
    "coverage": (list, None),
    "fusion": (bool, False),
    "fusion_mode": (str, "point"),
    "good": (int, 0),
    "bad": (int, 0),
    "speed_rpm": ((int, float), 0.0),
    "replay": (bool, False),
}


def validate_frame_meta(frame: RadarFrame, fill_defaults: bool = True) -> list[str]:
    """校验 RadarFrame.meta 是否符合统一 schema；可选补全标准默认值。"""
    if frame.meta is None:
        frame.meta = {}
    errors: list[str] = []
    for key, (expected, default) in META_SCHEMA.items():
        if key not in frame.meta:
            if fill_defaults and default is not None:
                frame.meta[key] = default
            continue
        val = frame.meta[key]
        if val is not None and not isinstance(val, expected):
            errors.append(f"meta[{key!r}] 期望 {expected}，实际 {type(val).__name__}")
    return errors
