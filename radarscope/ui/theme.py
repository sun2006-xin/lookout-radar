"""配色与全局样式。

原来的三套色板都是"军用荧光"审美：黑底 + 单一色相。
好看，但连着看两小时眼睛很累，而且和现代桌面软件放在一起像两个年代的产物。

现在默认换成**冰川蓝**：中性炭灰底 + 冷蓝主色，指标仍然保持高对比，
但不再是一片绿。经典三套（战术绿/琥珀/冷青）作为可选保留 ——
有人就喜欢那个味道，不该被剥夺。
"""
from __future__ import annotations

import math
import re

import numpy as np

#: 界面字体：中文 Windows 优先 Microsoft YaHei UI（避免假粗体回退与基线抖动）
FONT_UI = '"Microsoft YaHei UI", "Segoe UI", "PingFang SC", "Noto Sans CJK SC", sans-serif'
#: 等宽字体：用于数值读数，保证数字不跳动
FONT_MONO = '"Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace'


def mono_font(pt: int = 8, bold: bool = False):
    """返回等宽+清晰中文回退字体，避免 QFont('Consolas') 回退到锯齿宋体。"""
    from PyQt5.QtGui import QFont

    f = QFont()
    f.setFamilies(["Consolas", "Cascadia Mono", "Microsoft YaHei UI", "PingFang SC"])
    f.setPointSize(int(pt))
    f.setWeight(QFont.Bold if bold else QFont.Normal)
    return f

PALETTES = {
    "daylight": {
        # 浅色主题配套：冷白底 + 钴蓝回波
        "label": "晴空蔚蓝",
        "stops": [(0.0, (248, 250, 252)), (0.18, (191, 219, 254)),
                  (0.50, (59, 130, 246)), (0.78, (29, 78, 216)),
                  (1.0, (15, 23, 42))],
        "grid": (164, 184, 210),
        "grid_dim": (214, 226, 240),
        "text": (30, 58, 95),
        "bg": (248, 250, 252),
        "target": (220, 38, 38),
        "lock": (180, 83, 9),
        "dim": (132, 154, 182),
    },
    "ocean_teal": {
        # 护眼海图青绿底 + 碧翠青瓷回波（与晴空蔚蓝拉开明显视觉差距）
        "label": "海图青瓷",
        "stops": [(0.0, (234, 245, 243)), (0.20, (153, 224, 216)),
                  (0.52, (13, 148, 136)), (0.80, (15, 118, 110)),
                  (1.0, (17, 62, 58))],
        "grid": (122, 178, 172),
        "grid_dim": (196, 224, 220),
        "text": (19, 78, 74),
        "bg": (235, 245, 243),
        "target": (225, 29, 72),
        "lock": (194, 65, 12),
        "dim": (94, 152, 146),
    },
    "ice": {
        # 深邃藏青底 + 冰川天蓝回波
        "label": "冰川星蓝",
        "stops": [(0.0, (10, 18, 34)), (0.20, (24, 64, 118)), (0.50, (56, 152, 248)),
                  (0.78, (125, 211, 252)), (1.0, (240, 249, 255))],
        "grid": (56, 98, 152),
        "grid_dim": (26, 48, 80),
        "text": (164, 214, 255),
        "bg": (10, 18, 34),
        "target": (248, 113, 113),
        "lock": (251, 191, 36),
        "dim": (78, 124, 172),
    },
    "aurora": {
        # 午夜紫靛底 + 紫青极光回波
        "label": "极光紫靛",
        "stops": [(0.0, (22, 18, 44)), (0.22, (76, 58, 148)), (0.52, (139, 92, 246)),
                  (0.80, (56, 189, 248)), (1.0, (236, 254, 255))],
        "grid": (94, 84, 156),
        "grid_dim": (48, 42, 86),
        "text": (204, 196, 255),
        "bg": (22, 18, 44),
        "target": (244, 114, 182),
        "lock": (250, 204, 21),
        "dim": (124, 114, 186),
    },
    "mono": {
        # 中性石墨炭灰底 + 银白单色回波
        "label": "极简素白",
        "stops": [(0.0, (26, 28, 34)), (0.22, (70, 76, 88)), (0.55, (156, 166, 180)),
                  (0.82, (226, 232, 240)), (1.0, (255, 255, 255))],
        "grid": (106, 114, 128),
        "grid_dim": (54, 58, 68),
        "text": (226, 232, 240),
        "bg": (26, 28, 34),
        "target": (251, 146, 60),
        "lock": (250, 204, 21),
        "dim": (132, 140, 154),
    },
    "green": {
        # P7 军用暗绿底 + 经典荧光绿回波
        "label": "战术哑绿",
        "stops": [(0.0, (6, 24, 16)), (0.22, (16, 86, 54)), (0.55, (34, 197, 94)),
                  (0.80, (134, 239, 172)), (1.0, (240, 253, 244))],
        "grid": (34, 132, 86),
        "grid_dim": (16, 64, 42),
        "text": (134, 239, 172),
        "bg": (6, 24, 16),
        "target": (248, 113, 113),
        "lock": (251, 191, 36),
        "dim": (46, 148, 98),
    },
    "amber": {
        # 驾驶舱棕褐微光底 + 暖琥珀金回波
        "label": "夜航琥珀",
        "stops": [(0.0, (26, 17, 8)), (0.22, (92, 52, 12)), (0.55, (245, 158, 11)),
                  (0.80, (253, 224, 71)), (1.0, (254, 249, 195))],
        "grid": (152, 98, 28),
        "grid_dim": (78, 48, 14),
        "text": (253, 224, 108),
        "bg": (26, 17, 8),
        "target": (248, 113, 113),
        "lock": (56, 189, 248),
        "dim": (180, 122, 42),
    },
    "cyan": {
        # 深海声呐碧蓝底 + 亮青绿回波
        "label": "深海冷青",
        "stops": [(0.0, (5, 26, 36)), (0.22, (14, 78, 98)), (0.55, (20, 184, 166)),
                  (0.80, (103, 232, 249)), (1.0, (236, 254, 255))],
        "grid": (30, 124, 146),
        "grid_dim": (15, 62, 78),
        "text": (103, 232, 249),
        "bg": (5, 26, 36),
        "target": (251, 113, 133),
        "lock": (251, 191, 36),
        "dim": (38, 142, 164),
    },
}

LIGHT_PALETTE_KEYS = {"daylight", "ocean_teal"}

DEFAULT_PALETTE = "ice"


def color_table(name: str) -> list[int]:
    """生成 QImage Format_Indexed8 用的 256 色表（0xAARRGGBB）。"""
    pal = PALETTES.get(name, PALETTES["green"])
    stops = pal["stops"]
    xs = np.array([s[0] for s in stops]) * 255.0
    table: list[int] = []
    for i in range(256):
        for k in range(len(stops) - 1):
            if xs[k] <= i <= xs[k + 1]:
                t = (i - xs[k]) / max(1e-6, xs[k + 1] - xs[k])
                c0, c1 = stops[k][1], stops[k + 1][1]
                r = int(c0[0] + (c1[0] - c0[0]) * t)
                g = int(c0[1] + (c1[1] - c0[1]) * t)
                b = int(c0[2] + (c1[2] - c0[2]) * t)
                table.append(0xFF000000 | (r << 16) | (g << 8) | b)
                break
        else:
            c = stops[-1][1]
            table.append(0xFF000000 | (c[0] << 16) | (c[1] << 8) | c[2])
    return table


def qcolor(rgb: tuple[int, int, int], alpha: int = 255) -> str:
    return f"rgba({rgb[0]},{rgb[1]},{rgb[2]},{alpha})"


def nice_step(span: float, target_ticks: int = 6) -> float:
    """把坐标轴刻度取成 1/2/5×10^n 这种"整数感"步长。

    不能写死 1.0：量程 20 km 时会画出两万根刻度线，整个面板糊成一片绿色。
    """
    span = max(1e-6, float(span))
    raw = span / max(1, target_ticks)
    mag = 10.0 ** math.floor(math.log10(raw))
    for m in (1.0, 2.0, 5.0, 10.0):
        if raw <= m * mag:
            return m * mag
    return 10.0 * mag


def fmt_range(m: float, rmax: float) -> str:
    """量程过千米按公里显示。"""
    if rmax >= 1000.0:
        return f"{m / 1000.0:.1f}km"
    return f"{m:.1f}m"


def scaled_stylesheet(scale: float) -> str:
    """按比例缩放样式表里的字号。

    为什么用正则改字符串，而不是让 Qt 自己缩：QSS 的 `font-size` 是硬写的，
    而 `QWidget{font-size}` 这种通配规则优先级低于具体选择器，改不动。
    所以直接在文本层把 px 值乘一下最可靠，也保持了单一数据源。
    """
    if abs(scale - 1.0) < 1e-3:
        return STYLESHEET

    def repl(m):
        v = max(8, int(round(int(m.group(1)) * scale)))
        return f"font-size: {v}px"

    return re.sub(r"font-size:\s*(\d+)px", repl, STYLESHEET)



# --------------------------------------------------------------------------- #
# 主题：浅色（默认）/ 深色
#
# 用占位符替换而不是 f-string，因为 QSS 里全是 {}，f-string 得把每个括号
# 写成 {{}}，改一处漏一处就炸。占位符最笨也最不会错。
# --------------------------------------------------------------------------- #
THEMES = {
    "light": {
        "name": "现代白 · 晴空",
        "is_light": True,
        "palette": "daylight",
        "bg": "#F1F5F9",
        "surface": "#FFFFFF",
        "surface2": "#F8FAFC",
        "input": "#FFFFFF",
        "border": "#E2E8F0",
        "border_hi": "#CBD5E1",
        "text": "#0F172A",
        "muted": "#64748B",
        "accent": "#2563EB",
        "accent_hover": "#1D4ED8",
        "accent_soft": "#EFF6FF",
        "hover": "#F1F5F9",
        "checked_bg": "#DBEAFE",
        "checked_text": "#1E40AF",
        "grid": "#F1F5F9",
    },
    "nordic": {
        "name": "北欧青瓷（浅色护眼）",
        "is_light": True,
        "palette": "ocean_teal",
        "bg": "#EEF3F4",
        "surface": "#FFFFFF",
        "surface2": "#F5F9F9",
        "input": "#FFFFFF",
        "border": "#D8E4E6",
        "border_hi": "#BDD0D4",
        "text": "#132A2E",
        "muted": "#5A7478",
        "accent": "#0D9488",
        "accent_hover": "#0F766E",
        "accent_soft": "#E6F7F5",
        "hover": "#EAF2F3",
        "checked_bg": "#CCFBF1",
        "checked_text": "#115E59",
        "grid": "#EDF4F5",
    },
    "dark": {
        "name": "黑曜指挥（深邃暗）",
        "is_light": False,
        "palette": "ice",
        "bg": "#090C12",
        "surface": "#111621",
        "surface2": "#0D121B",
        "input": "#0F141F",
        "border": "#1F2937",
        "border_hi": "#334155",
        "text": "#E2E8F0",
        "muted": "#94A3B8",
        "accent": "#38BDF8",
        "accent_hover": "#0EA5E9",
        "accent_soft": "#172E4A",
        "hover": "#1A2232",
        "checked_bg": "#1B3B5A",
        "checked_text": "#BAE6FD",
        "grid": "#18202E",
    },
    "graphite": {
        "name": "极光石墨（工作室灰）",
        "is_light": False,
        "palette": "aurora",
        "bg": "#13161D",
        "surface": "#1B202B",
        "surface2": "#161A24",
        "input": "#171B26",
        "border": "#2A3142",
        "border_hi": "#3E4861",
        "text": "#E5E7EB",
        "muted": "#9CA3AF",
        "accent": "#818CF8",
        "accent_hover": "#6366F1",
        "accent_soft": "#282D54",
        "hover": "#232938",
        "checked_bg": "#312E81",
        "checked_text": "#E0E7FF",
        "grid": "#232937",
    },
    "marine_night": {
        "name": "深海夜航（海图专属）",
        "is_light": False,
        "palette": "cyan",
        "bg": "#06111B",
        "surface": "#0D1E2E",
        "surface2": "#091724",
        "input": "#0B1926",
        "border": "#19334B",
        "border_hi": "#294D6E",
        "text": "#D8EEF5",
        "muted": "#7CA7BE",
        "accent": "#2DD4BF",
        "accent_hover": "#14B8A6",
        "accent_soft": "#103A44",
        "hover": "#14293D",
        "checked_bg": "#134E4A",
        "checked_text": "#CCFBF1",
        "grid": "#132639",
    },
    "tactical": {
        "name": "现代战术绿（暗夜雷达）",
        "is_light": False,
        "palette": "green",
        "bg": "#070D0B",
        "surface": "#101915",
        "surface2": "#0B1310",
        "input": "#0E1713",
        "border": "#1E3129",
        "border_hi": "#2E4A3E",
        "text": "#D1FAE5",
        "muted": "#6EE7B7",
        "accent": "#10B981",
        "accent_hover": "#059669",
        "accent_soft": "#12382B",
        "hover": "#162620",
        "checked_bg": "#064E3B",
        "checked_text": "#A7F3D0",
        "grid": "#15231D",
    },
    "amber_ops": {
        "name": "夜航琥珀（驾驶舱微光）",
        "is_light": False,
        "palette": "amber",
        "bg": "#0E0B08",
        "surface": "#18130E",
        "surface2": "#120E0A",
        "input": "#15100C",
        "border": "#30251A",
        "border_hi": "#4A3928",
        "text": "#FDE68A",
        "muted": "#D97706",
        "accent": "#F59E0B",
        "accent_hover": "#D97706",
        "accent_soft": "#3D2910",
        "hover": "#241B12",
        "checked_bg": "#451A03",
        "checked_text": "#FEF3C7",
        "grid": "#241C14",
    },
}
DEFAULT_THEME = "light"


def is_dark_theme(mode: str) -> bool:
    return not bool(THEMES.get(mode, THEMES[DEFAULT_THEME]).get("is_light", False))


def theme_palette(mode: str) -> str:
    return str(THEMES.get(mode, THEMES[DEFAULT_THEME]).get("palette", DEFAULT_PALETTE))


_QSS = """
QMainWindow, QDialog { background: @bg@; color: @text@;
    font-family: @font@; font-size: 12px; }
QWidget { color: @text@; font-family: @font@; font-size: 12px; }
QFrame#panelCard { background: @surface@; border: 1px solid @border@; border-radius: 10px; }
QToolBar { background: @surface@; border: none; spacing: 6px; padding: 6px 10px;
    border-bottom: 1px solid @border@; }
QToolBar::separator { background: @border@; width: 1px; margin: 5px 6px; }
QMenuBar { background: @surface@; color: @muted@; border: none; padding: 3px 8px;
    border-bottom: 1px solid @border@; font-size: 12px; }
QMenuBar::item { background: transparent; padding: 5px 12px; border-radius: 6px; font-weight: normal; }
QMenuBar::item:selected { background: @hover@; color: @text@; }
QMenu { background: @surface@; color: @text@; border: 1px solid @border_hi@;
    padding: 6px; border-radius: 10px; }
QMenu::item { padding: 6px 26px 6px 18px; border-radius: 6px; }
QMenu::item:selected { background: @accent_soft@; color: @accent@; }
QMenu::separator { height: 1px; background: @border@; margin: 5px 10px; }
QMenu::indicator { width: 12px; height: 12px; }
QLabel { color: @muted@; font-size: 12px; background: transparent; font-weight: normal; }
QLabel#hintLabel { color: @muted@; font-size: 11px; font-weight: normal; }
QLabel#ewMetricBadge { background: @accent_soft@; color: @accent@;
    border: 1px solid @border_hi@; border-radius: 7px; padding: 6px 10px; font-size: 12px; }
QGroupBox { background: @surface@; border: 1px solid @border@; border-radius: 10px;
    margin-top: 10px; padding: 14px 12px 10px 12px; font-weight: normal; color: @text@; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left;
    padding: 0 6px; left: 10px; color: @accent@; font-weight: bold; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit {
    background: @input@; border: 1px solid @border_hi@; border-radius: 7px;
    padding: 3px 10px; color: @text@; font-size: 12px; min-height: 20px;
    selection-background-color: @accent@; selection-color: #FFFFFF; }
QComboBox { padding-right: 26px; }
QSpinBox, QDoubleSpinBox { padding-right: 26px; }
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover, QLineEdit:hover {
    border-color: @accent@; }
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {
    border: 1.5px solid @accent@; }
QComboBox::drop-down {
    subcontrol-origin: padding; subcontrol-position: top right; width: 22px;
    border: none; border-left: 1px solid @border@;
    border-top-right-radius: 6px; border-bottom-right-radius: 6px;
    background: @surface2@; }
QComboBox::drop-down:hover { background: @accent_soft@; }
QComboBox::drop-down:pressed { background: @checked_bg@; }
QComboBox::down-arrow { image: url(@arr_down@); width: 10px; height: 10px; }
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: padding; subcontrol-position: top right; width: 22px;
    border: none; border-left: 1px solid @border@; border-bottom: 1px solid @border@;
    border-top-right-radius: 6px; background: @surface2@; }
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: padding; subcontrol-position: bottom right; width: 22px;
    border: none; border-left: 1px solid @border@;
    border-bottom-right-radius: 6px; background: @surface2@; }
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
    background: @accent_soft@; }
QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {
    background: @checked_bg@; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url(@arr_up@); width: 10px; height: 8px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url(@arr_down@); width: 10px; height: 8px; }
QComboBox QAbstractItemView { background: @surface@; color: @text@;
    selection-background-color: @accent_soft@; selection-color: @accent@;
    border: 1px solid @border_hi@; border-radius: 8px; padding: 4px; outline: none; }
QPushButton { background: @surface@; border: 1px solid @border_hi@;
    border-radius: 7px; padding: 4px 13px; min-height: 20px; color: @text@;
    font-size: 12px; font-weight: normal; }
QPushButton:hover { background: @hover@; border-color: @accent@; }
QPushButton:pressed { background: @grid@; }
QPushButton:checked { background: @checked_bg@; border-color: @accent@;
    color: @checked_text@; font-weight: normal; }
QPushButton:disabled { color: @muted@; border-color: @border@; background: @surface2@; }
QPushButton#primaryBtn { background: @accent@; border: 1px solid @accent@;
    color: #FFFFFF; font-weight: bold; padding: 4px 18px; }
QPushButton#primaryBtn:hover { background: @accent_hover@; border-color: @accent_hover@; }
QPushButton#primaryBtn:checked { background: #DC2626; border-color: #DC2626; color: #FFFFFF; }
QStatusBar { background: @surface@; color: @muted@; font-size: 12px;
    border-top: 1px solid @border@; min-height: 26px; padding: 2px 8px; }
QStatusBar::item { border: none; }
QHeaderView::section { background: @surface2@; color: @muted@; border: none;
    border-bottom: 1px solid @border@; padding: 6px 8px; font-size: 11px;
    font-weight: normal; }
QTableWidget { background: @surface@; alternate-background-color: @surface2@;
    gridline-color: @grid@; border: 1px solid @border@; border-radius: 10px;
    font-size: 12px; color: @text@; selection-background-color: @accent_soft@;
    selection-color: @accent@; }
QTableWidget::item { padding: 4px 6px; border: none; }
QProgressBar { background: @surface2@; border: 1px solid @border@;
    border-radius: 6px; height: 14px; text-align: center; color: @text@;
    font-size: 11px; }
QProgressBar::chunk { background: @accent@; border-radius: 5px; }
QTextEdit { background: @surface@; color: @text@;
    border: 1px solid @border@; border-radius: 10px;
    selection-background-color: @accent_soft@; }
QTabWidget::pane { border: 1px solid @border@; background: @surface@;
    border-radius: 10px; top: -1px; }
QTabBar::tab { background: transparent; color: @muted@; padding: 7px 16px;
    border: none; border-bottom: 2px solid transparent; font-size: 12px; font-weight: normal; }
QTabBar::tab:selected { color: @accent@; border-bottom: 2px solid @accent@; font-weight: normal; }
QTabBar::tab:hover:!selected { color: @text@; background: @hover@; border-radius: 6px; }
QDockWidget { color: @muted@; }
QDockWidget::title { background: @surface2@; padding: 6px 10px;
    border-top: 1px solid @border@; font-size: 12px; font-weight: normal; }
QCheckBox { color: @text@; font-size: 12px; spacing: 6px; }
QCheckBox::indicator { width: 15px; height: 15px; border-radius: 4px;
    border: 1px solid @border_hi@; background: @input@; }
QCheckBox::indicator:checked { background: @accent@; border-color: @accent@; image: url(@chk_on@); }
QToolTip { background: @surface@; color: @text@; border: 1px solid @border_hi@;
    padding: 6px 10px; border-radius: 8px; font-size: 12px; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: @border_hi@; border-radius: 4px;
    min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 2px; }
QScrollBar::handle:horizontal { background: @border_hi@; border-radius: 4px;
    min-width: 24px; }
"""


def _ensure_ui_icons(mode: str, color_hex: str) -> dict[str, str]:
    """用 QImage 离屏生成 2x 抗锯齿矢量箭头与对勾 PNG 供 QSS url() 使用（无需提前建 QApplication）。"""
    try:
        from PyQt5.QtCore import QPointF, Qt
        from PyQt5.QtGui import QColor, QImage, QPainter, QPen
        from .. import paths

        icon_dir = paths.data_dir() / "ui_icons"
        icon_dir.mkdir(parents=True, exist_ok=True)
        tag = "".join(ch for ch in f"{mode}_{color_hex}" if ch.isalnum() or ch in ("_", "-"))
        p_up = icon_dir / f"{tag}_up.png"
        p_dn = icon_dir / f"{tag}_dn.png"
        p_chk = icon_dir / "chk_on.png"

        def _draw_chevron(path, up: bool) -> None:
            if path.exists():
                return
            img = QImage(24, 24, QImage.Format_ARGB32_Premultiplied)
            img.fill(0)
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor(color_hex), 3.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            if up:
                p.drawLine(QPointF(6.0, 15.0), QPointF(12.0, 9.0))
                p.drawLine(QPointF(12.0, 9.0), QPointF(18.0, 15.0))
            else:
                p.drawLine(QPointF(6.0, 9.0), QPointF(12.0, 15.0))
                p.drawLine(QPointF(12.0, 15.0), QPointF(18.0, 9.0))
            p.end()
            img.save(str(path), "PNG")

        _draw_chevron(p_up, True)
        _draw_chevron(p_dn, False)
        if not p_chk.exists():
            img = QImage(24, 24, QImage.Format_ARGB32_Premultiplied)
            img.fill(0)
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor("#FFFFFF"), 3.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawLine(QPointF(5.5, 12.5), QPointF(10.2, 17.2))
            p.drawLine(QPointF(10.2, 17.2), QPointF(18.5, 7.2))
            p.end()
            img.save(str(p_chk), "PNG")
        return {
            "arr_up": p_up.as_posix(),
            "arr_down": p_dn.as_posix(),
            "chk_on": p_chk.as_posix(),
        }
    except Exception:
        return {"arr_up": "", "arr_down": "", "chk_on": ""}


def build_stylesheet(mode: str = DEFAULT_THEME, scale: float = 1.0) -> str:
    """生成样式表。scale 用于 Ctrl+= / Ctrl+- 的字号缩放。"""
    key = mode if mode in THEMES else DEFAULT_THEME
    t = dict(THEMES[key])
    t["font"] = FONT_UI
    t.update(_ensure_ui_icons(key, str(t.get("muted", "#64748B"))))
    qss = _QSS
    for k, v in t.items():
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            qss = qss.replace(f"@{k}@", str(v))
    if abs(scale - 1.0) > 1e-3:
        qss = re.sub(r"font-size:\s*(\d+)px",
                     lambda m: f"font-size: {max(9, int(round(int(m.group(1)) * scale)))}px",
                     qss)
    return qss


#: 兼容旧调用：默认主题的样式表
STYLESHEET = build_stylesheet(DEFAULT_THEME)


def scaled_stylesheet(scale: float, mode: str = DEFAULT_THEME) -> str:
    """按比例缩放字号（Ctrl+= / Ctrl+-）。"""
    return build_stylesheet(mode, scale)
