"""轻量中英文切换（ROADMAP D3）。

保持零外部依赖（不需要 Qt Linguist .qm 编译流程）：常用菜单、按钮、页签与设置项
统一走 tr(key)，切换语言立即刷新界面文本。
"""
from __future__ import annotations

_LANG = "zh"

_EN: dict[str, str] = {
    "文件(&F)": "File(&F)",
    "工具(&T)": "Tools(&T)",
    "视图(&V)": "View(&V)",
    "设置(&S)": "Settings(&S)",
    "帮助(&H)": "Help(&H)",
    "数据源 ": "Source ",
    "  类型 ": "  Type ",
    "  端点 ": "  Endpoint ",
    "连接设置": "Link...",
    "扫描": "Scan",
    "连接": "Connect",
    "断开": "Disconnect",
    " 量程 ": " Range ",
    "点目标锁定": "Lock Target",
    "转发": "Forward",
    "录像": "Record Video",
    "静音": "Mute",
    "画警戒区": "Draw Zone",
    "真运动": "True Motion",
    "航迹": "Tracks",
    "传感器": "Sensors",
    "事件": "Events",
    "历史": "History",
    "显示与主题设置…": "Display & Theme...",
    "连接设置…": "Link Settings...",
    "告警规则…": "Alarm Rules...",
    "传感器配置…": "Multi-Sensor Setup...",
    "设置": "Settings",
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
