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

#: 界面字体：优先 Segoe UI（Win10+），中文回落雅黑
FONT_UI = '"Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
#: 等宽字体：用于数值读数，保证数字不跳动
FONT_MONO = '"Cascadia Mono", "Consolas", "JetBrains Mono", monospace'

PALETTES = {
    "daylight": {
        # 浅色主题配套：白底 + 深蓝回波。和深色色板的逻辑相反 ——
        # 这里"数值大"应该更暗更饱和，否则白底上根本看不见。
        "label": "白昼",
        "stops": [(0.0, (250, 251, 253)), (0.18, (214, 228, 244)),
                  (0.50, (108, 160, 214)), (0.78, (32, 92, 160)),
                  (1.0, (12, 46, 96))],
        "grid": (176, 190, 206),
        "grid_dim": (214, 222, 232),
        "text": (60, 86, 116),
        "bg": (247, 249, 252),
        "target": (214, 60, 40),
        "lock": (176, 120, 0),
        "dim": (150, 172, 196),
    },
    "ice": {
        "label": "冰川蓝",
        "stops": [(0.0, (0, 0, 0)), (0.20, (12, 34, 56)), (0.50, (30, 120, 200)),
                  (0.78, (120, 200, 255)), (1.0, (238, 249, 255))],
        "grid": (52, 96, 140),
        "grid_dim": (30, 54, 78),
        "text": (150, 206, 250),
        "bg": (9, 13, 18),
        "target": (255, 120, 96),
        "lock": (255, 208, 96),
        "dim": (70, 120, 160),
    },
    "mono": {
        "label": "素白",
        "stops": [(0.0, (0, 0, 0)), (0.20, (40, 44, 48)), (0.55, (150, 160, 170)),
                  (0.80, (225, 232, 238)), (1.0, (255, 255, 255))],
        "grid": (96, 106, 116),
        "grid_dim": (52, 58, 64),
        "text": (200, 212, 222),
        "bg": (10, 12, 14),
        "target": (255, 138, 96),
        "lock": (255, 214, 120),
        "dim": (110, 122, 132),
    },
    "green": {
        "label": "战术绿",
        "stops": [(0.0, (0, 0, 0)), (0.22, (0, 52, 24)), (0.55, (0, 190, 105)),
                  (0.80, (90, 255, 170)), (1.0, (225, 255, 240))],
        "grid": (0, 105, 68),
        "grid_dim": (0, 62, 42),
        "text": (110, 235, 165),
        "bg": (3, 12, 8),
        "target": (255, 82, 62),
        "lock": (255, 214, 74),
        "dim": (0, 120, 78),
    },
    "amber": {
        "label": "琥珀",
        "stops": [(0.0, (0, 0, 0)), (0.22, (54, 30, 0)), (0.55, (214, 132, 0)),
                  (0.80, (255, 196, 60)), (1.0, (255, 246, 214))],
        "grid": (126, 82, 0),
        "grid_dim": (72, 46, 0),
        "text": (255, 196, 96),
        "bg": (12, 8, 3),
        "target": (255, 86, 70),
        "lock": (150, 240, 255),
        "dim": (150, 100, 20),
    },
    "cyan": {
        "label": "冷青",
        "stops": [(0.0, (0, 0, 0)), (0.22, (0, 34, 52)), (0.55, (0, 150, 200)),
                  (0.80, (80, 220, 255)), (1.0, (232, 250, 255))],
        "grid": (0, 92, 124),
        "grid_dim": (0, 54, 72),
        "text": (128, 220, 255),
        "bg": (3, 9, 14),
        "target": (255, 92, 80),
        "lock": (255, 206, 90),
        "dim": (0, 110, 150),
    },
}

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
        "name": "现代白",
        "bg": "#F3F4F6",        # 页面底：略灰，衬托白卡片
        "surface": "#FFFFFF",   # 卡片/工具栏
        "surface2": "#F7F8FA",
        "input": "#FFFFFF",
        "border": "#E2E5EA",
        "border_hi": "#C6CCD4",
        "text": "#1F2328",
        "muted": "#6B7280",
        "accent": "#1F6FEB",
        "accent_soft": "#E8F0FE",
        "hover": "#EEF1F5",
        "checked_bg": "#DCE9FB",
        "checked_text": "#12448F",
        "grid": "#EDEFF2",
    },
    "dark": {
        "name": "深色",
        "bg": "#0E1217",
        "surface": "#161C24",
        "surface2": "#121820",
        "input": "#131A22",
        "border": "#26313D",
        "border_hi": "#3A4C60",
        "text": "#D2DDE7",
        "muted": "#8B9AA8",
        "accent": "#4FA8FF",
        "accent_soft": "#1B3E68",
        "hover": "#202B37",
        "checked_bg": "#1B3E68",
        "checked_text": "#CFE6FF",
        "grid": "#1B232C",
    },
}
DEFAULT_THEME = "light"

_QSS = """
QMainWindow, QWidget { background: @bg@; color: @text@;
    font-family: @font@; font-size: 12px; }
QToolBar { background: @surface@; border: none; spacing: 6px; padding: 5px 8px;
    border-bottom: 1px solid @border@; }
QToolBar::separator { background: @border@; width: 1px; margin: 5px 6px; }
QMenuBar { background: @surface@; color: @muted@; border: none; padding: 2px 6px;
    border-bottom: 1px solid @border@; font-size: 12px; }
QMenuBar::item { background: transparent; padding: 5px 11px; border-radius: 5px; }
QMenuBar::item:selected { background: @hover@; color: @text@; }
QMenu { background: @surface@; color: @text@; border: 1px solid @border@;
    padding: 5px; border-radius: 8px; }
QMenu::item { padding: 6px 26px 6px 18px; border-radius: 5px; }
QMenu::item:selected { background: @accent_soft@; color: @accent@; }
QMenu::separator { height: 1px; background: @border@; margin: 5px 10px; }
QMenu::indicator { width: 12px; height: 12px; }
QLabel { color: @muted@; font-size: 12px; background: transparent; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {
    background: @input@; border: 1px solid @border_hi@; border-radius: 6px;
    padding: 4px 8px; color: @text@; font-size: 12px; min-height: 20px;
    selection-background-color: @accent@; selection-color: #FFFFFF; }
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {
    border-color: @accent@; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView { background: @surface@; color: @text@;
    selection-background-color: @accent_soft@; selection-color: @accent@;
    border: 1px solid @border@; border-radius: 6px; outline: none; }
QPushButton { background: @surface@; border: 1px solid @border_hi@;
    border-radius: 6px; padding: 5px 13px; color: @text@; font-size: 12px; }
QPushButton:hover { background: @hover@; }
QPushButton:pressed { background: @grid@; }
QPushButton:checked { background: @checked_bg@; border-color: @accent@;
    color: @checked_text@; font-weight: 600; }
QPushButton:disabled { color: @muted@; border-color: @border@; background: @surface2@; }
QStatusBar { background: @surface@; color: @muted@; font-size: 12px;
    border-top: 1px solid @border@; min-height: 24px; padding: 2px 6px; }
QStatusBar::item { border: none; }
QHeaderView::section { background: @surface2@; color: @muted@; border: none;
    border-bottom: 1px solid @border@; padding: 5px 8px; font-size: 11px;
    font-weight: 600; }
QTableWidget { background: @surface@; gridline-color: @grid@;
    border: 1px solid @border@; border-radius: 8px; font-size: 12px;
    color: @text@; selection-background-color: @accent_soft@;
    selection-color: @accent@; }
QTableWidget::item { padding: 3px 6px; }
QProgressBar { background: @surface2@; border: 1px solid @border@;
    border-radius: 6px; height: 14px; text-align: center; color: @text@;
    font-size: 11px; }
QProgressBar::chunk { background: @accent@; border-radius: 5px; }
QTextEdit, QPlainTextEdit { background: @surface@; color: @text@;
    border: 1px solid @border@; border-radius: 8px;
    selection-background-color: @accent_soft@; }
QTabWidget::pane { border: 1px solid @border@; background: @surface@;
    border-radius: 8px; top: -1px; }
QTabBar::tab { background: transparent; color: @muted@; padding: 6px 16px;
    border: none; border-bottom: 2px solid transparent; font-size: 12px; }
QTabBar::tab:selected { color: @accent@; border-bottom: 2px solid @accent@; }
QTabBar::tab:hover:!selected { color: @text@; background: @hover@; }
QDockWidget { color: @muted@; }
QDockWidget::title { background: @surface2@; padding: 5px 8px;
    border-top: 1px solid @border@; font-size: 12px; }
QCheckBox { color: @text@; font-size: 12px; spacing: 6px; }
QCheckBox::indicator { width: 14px; height: 14px; border-radius: 4px;
    border: 1px solid @border_hi@; background: @input@; }
QCheckBox::indicator:checked { background: @accent@; border-color: @accent@; }
QToolTip { background: @surface@; color: @text@; border: 1px solid @border_hi@;
    padding: 5px 8px; border-radius: 6px; font-size: 12px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: @border_hi@; border-radius: 5px;
    min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: @border_hi@; border-radius: 5px;
    min-width: 24px; }
"""


def build_stylesheet(mode: str = DEFAULT_THEME, scale: float = 1.0) -> str:
    """生成样式表。scale 用于 Ctrl+= / Ctrl+- 的字号缩放。"""
    t = dict(THEMES.get(mode, THEMES[DEFAULT_THEME]))
    t["font"] = FONT_UI
    qss = _QSS
    for k, v in t.items():
        qss = qss.replace(f"@{k}@", str(v))
    if abs(scale - 1.0) > 1e-3:
        qss = re.sub(r"font-size://s*(//d+)px",
                     lambda m: f"font-size: {max(9, int(round(int(m.group(1)) * scale)))}px",
                     qss)
    return qss


#: 兼容旧调用：默认主题的样式表
STYLESHEET = build_stylesheet(DEFAULT_THEME)


def scaled_stylesheet(scale: float, mode: str = DEFAULT_THEME) -> str:
    """按比例缩放字号（Ctrl+= / Ctrl+-）。"""
    return build_stylesheet(mode, scale)
