"""Windows 外观适配：深色标题栏 + 程序图标。

PyQt 只管客户区，窗口标题栏是 Windows 画的。深色界面配一条浅色标题栏
非常割裂，所以要走 DWM 的沉浸式深色模式。

坑：DWMWA_USE_IMMERSIVE_DARK_MODE 这个属性号在不同 Windows 版本里不一样。
Win10 1809~1909 是 19，Win10 2004 之后和 Win11 是 20。两个都试一遍最稳。
"""
from __future__ import annotations

import sys

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPen, QPixmap


def enable_dark_titlebar(hwnd: int, dark: bool = True) -> bool:
    """把标题栏切成深色（dark=False 则切回浅色）。失败不影响功能。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):          # 新版本优先，老版本回退
            hr = dwm.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), attr, ctypes.byref(on), ctypes.sizeof(on))
            if hr == 0:
                return True
    except Exception:
        pass
    return False


def make_app_icon() -> QIcon:
    """用代码画一个雷达图标，省得往仓库里塞二进制资源。

    实际上窗口图标、任务栏图标、Alt+Tab 都取它，缺了会显示成一个空白方块，
    在一堆窗口里很难认。
    """
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128):
        pm = QPixmap(size, size)
        pm.fill(QColor(11, 14, 12))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = size / 2.0
        r = size * 0.40

        # 外环
        p.setPen(QPen(QColor(58, 120, 86), max(1.0, size * 0.045)))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(c, c), r, r)
        if size >= 32:
            p.setPen(QPen(QColor(40, 78, 58), 1))
            p.drawEllipse(QPointF(c, c), r * 0.55, r * 0.55)

        # 扫描扇区（右上 45°）
        if size >= 24:
            p.setBrush(QColor(60, 200, 120, 70))
            p.setPen(Qt.NoPen)
            p.drawPie(QRectF(c - r, c - r, 2 * r, 2 * r), 15 * 16, 45 * 16)

        # 扫描线
        p.setPen(QPen(QColor(120, 245, 165), max(1.0, size * 0.05)))
        p.drawLine(QPointF(c, c), QPointF(c + r * 0.92, c - r * 0.42))

        # 两个目标点
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 120, 90))
        p.drawEllipse(QPointF(c - r * 0.42, c - r * 0.30), size * 0.055, size * 0.055)
        p.setBrush(QColor(255, 190, 70))
        p.drawEllipse(QPointF(c + r * 0.20, c + r * 0.52), size * 0.045, size * 0.045)
        p.end()
        icon.addPixmap(pm)
    return icon


def install_dialog_titlebar(theme_getter):
    """让所有对话框的标题栏都跟随当前主题。

    QDialog 是独立原生窗口，主窗口设过一次 DWM 属性不会传下去；
    逐个对话框去改容易漏（新增一个就白一条）。这里在应用级事件过滤器里
    统一处理 —— 任何 QDialog 一显示就跟着上色。
    """
    from PyQt5.QtCore import QEvent, QObject
    from PyQt5.QtWidgets import QApplication, QDialog

    class _Filter(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Show and isinstance(obj, QDialog):
                try:
                    enable_dark_titlebar(int(obj.winId()), theme_getter() == "dark")
                except Exception:
                    pass
            return False

    app = QApplication.instance()
    if app is None:
        return None
    f = _Filter()
    app.installEventFilter(f)
    return f
