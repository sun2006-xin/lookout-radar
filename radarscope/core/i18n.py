"""轻量中英文切换（ROADMAP D3）。

保持零外部依赖（不需要 Qt Linguist .qm 编译流程）：常用菜单、按钮、页签、停靠栏、
工作区预设、电子战控制台与状态遥测统一走 tr(key)，切换语言立即刷新界面文本。
"""
from __future__ import annotations

_LANG = "zh"

_EN: dict[str, str] = {
    # 顶栏菜单
    "文件(&F)": "File(&F)",
    "场景(&N)": "Scenarios(&N)",
    "工具(&T)": "Tools(&T)",
    "视图(&V)": "View(&V)",
    "设置(&S)": "Settings(&S)",
    "帮助(&H)": "Help(&H)",
    # 工具栏标签与按钮
    " 数据源 ": " Source ",
    "  类型 ": "  Driver ",
    "  端点 ": "  Endpoint ",
    "连接设置": "Link...",
    "扫描": "Scan",
    "连接": "Connect",
    "断开": "Disconnect",
    " 量程 ": " Range ",
    "场景": "Scene",
    "场景/电子战": "Scene / EW",
    "返回日常雷达": "Normal View",
    "点目标锁定": "Lock Target",
    "转发": "Forward",
    "录像": "Record Video",
    "任务录制": "Mission Rec",
    "停止任务": "Stop Mission",
    "静音": "Mute",
    "画警戒区": "Draw Zone",
    "真运动": "True Motion",
    # 右侧分析与停靠面板
    "航迹": "Tracks",
    "传感器": "Sensors",
    "事件": "Events",
    "历史": "History",
    "近距监视（2 m 量程）": "Close Range (2 m)",
    "雷达波形与航迹台": "Scope & Tracks",
    "电子战与场景控制台": "EW & Scenario Studio",
    "防区与协议快速台": "Zones & Ops",
    # 航迹表列
    "航迹": "Track",
    "距离": "Range",
    "方位": "Azimuth",
    "高度": "Alt",
    "速度": "Speed",
    "敌我": "IFF",
    "威胁": "Threat",
    "状态": "Status",
    "置信": "Conf",
    # 文件与任务子菜单
    "导出事件日志…": "Export Event Log...",
    "导出 ROS / Foxglove (.mcap / JSON)…": "Export ROS / Foxglove (.mcap / JSON)...",
    "导出当前画面…": "Export Screenshot...",
    "一键任务录制": "Record Mission Bundle",
    "任务包回放与参数对比…": "Mission Replay & Compare...",
    "打开数据目录": "Open Data Folder",
    "退出": "Exit",
    # 场景与电子战子菜单
    "模拟雷达扫描模式": "Sim Scan Mode",
    "旋转扫描（360° PPI）": "Rotating Scan (360° PPI)",
    "凝视扇区（高速更新）": "Staring Sector (Fast)",
    "3D 俯仰扫掠（RHI 距离-高度）": "3D Elevation Sweep (RHI)",
    "打开模拟电子站 / 自定义目标与干扰参数…": "Open Electronic Station Dialog...",
    # 视图与布局预设
    "工作区布局预设": "Workspace Presets",
    "极简实测监视": "Minimal Field Monitor",
    "电子战教学对比": "EW & ECCM Studio",
    "多站与协议排障": "Multi-Sensor & Ops",
    "全功能显控台": "Full Operator Console",
    "重置默认布局": "Reset Default Layout",
    "尾迹": "Trails",
    "多普勒趋近着色": "Doppler Approach Color",
    "航迹置信椭圆 (2σ)": "Track Uncertainty Ellipse (2σ)",
    "告警禁入区": "Alarm Zones",
    "电子方位线 EBL/VRM": "EBL / VRM",
    "双量程窗口（近距）": "Dual Range Window",
    "联网地图底图": "Online Map Layer",
    "陆地静止杂波掩膜": "Land Clutter Mask",
    "显示 AIS 目标": "Show AIS Targets",
    "界面主题（7 套风格）": "UI Theme (7 Styles)",
    "雷达配色（8 套色板）": "Radar Palette (8 Palettes)",
    # 工具与设置
    "传感标定旋转模式（画布拖动扇区）": "Canvas Bearing Calib Mode",
    "显示与主题设置…": "Display & Theme...",
    "连接设置…": "Link Settings...",
    "告警规则…": "Alarm Rules...",
    "传感器配置…": "Multi-Sensor Setup...",
    "联网地图与定位设置…": "Map & NMEA Nav Setup...",
    "设置": "Settings",
    # 电子战面板标签
    "自动场景": "Auto Scenario",
    "速度倍率": "Speed Scale",
    "随机出现": "Spawn Random",
    "删除选中": "Remove",
    "注入自定义目标": "Inject Target",
    "干扰样式": "Jamming Mode",
    "清空全部仿真": "Clear All Sim",
    "完成": "Done",
    # 状态栏摘要词条
    "杂波抑制": "ClutterSupp",
    "陆地掩膜": "LandMask",
    "NMEA本船": "NMEA-Nav",
    "任务录制中": "MISSION REC",
    "已暂停": "Paused",
    "未连接": "Disconnected",
    "已断开": "Disconnected",
    "系统正常": "System Normal",
}


def set_lang(lang: str) -> str:
    global _LANG
    _LANG = "en" if str(lang).lower().startswith("en") else "zh"
    return _LANG


def get_lang() -> str:
    return _LANG


def tr(zh_text: str) -> str:
    """按当前语言返回文本；中文或未收录词条原样返回。"""
    if _LANG == "en":
        return _EN.get(zh_text, zh_text)
    return zh_text
