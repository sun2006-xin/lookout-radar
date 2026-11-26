"""A 显（距离剖面）。

战机上 A 显示的是沿天线视线的回波强度，横轴距离、纵轴幅度。
这里取"所有方位上的最大值"，所以不管雷达有没有方位信息，这条曲线都成立。
"""
from __future__ import annotations

import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPainterPath, QPolygonF
from PyQt5.QtWidgets import QSizePolicy, QWidget

from ..core.model import Capabilities, RadarFrame
from ..core.pipeline import range_profile
from .theme import PALETTES, fmt_range, mono_font, nice_step


class AScopeWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._caps = Capabilities()
        self._prof = np.zeros(1, dtype=np.float32)
        self._r = np.zeros(1, dtype=np.float32)
        self._palette_name = "green"
        self._threshold = 0.30

    def set_capabilities(self, caps: Capabilities) -> None:
        self._caps = caps
        self.reset()

    def set_palette(self, name: str) -> None:
        self._palette_name = name
        self.update()

    def set_threshold(self, v: float) -> None:
        self._threshold = v
        self.update()

    def reset(self) -> None:
        self._prof = np.zeros(1, dtype=np.float32)
        self._r = np.zeros(1, dtype=np.float32)
        self.update()

    def push_frame(self, frame: RadarFrame) -> None:
        self._prof = range_profile(frame)
        if frame.r_axis is not None and frame.r_axis.size == self._prof.size:
            self._r = frame.r_axis
        self.update()

    def paintEvent(self, _ev) -> None:
        pal = PALETTES[self._palette_name]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(*pal["bg"]))

        rc = self.rect().adjusted(34, 16, -10, -18)
        if rc.width() < 20 or rc.height() < 20:
            p.end()
            return

        g = QColor(*pal["grid"])
        gd = QColor(*pal["grid_dim"])
        txt = QColor(*pal["text"])

        p.setFont(mono_font(8))
        p.setPen(QPen(txt))
        p.drawText(QPointF(8, 12), "A 显 / RANGE PROFILE")

        # 网格
        p.setPen(QPen(gd, 1, Qt.DotLine))
        for i in range(1, 4):
            y = rc.top() + rc.height() * i / 4
            p.drawLine(QPointF(rc.left(), y), QPointF(rc.right(), y))
        rmax = self._caps.range_max
        rmin = self._caps.range_min
        step = nice_step(max(1e-6, rmax - rmin), 6)
        r = step
        while r < rmax:
            x = rc.left() + rc.width() * (r - rmin) / max(1e-6, rmax - rmin)
            p.drawLine(QPointF(x, rc.top()), QPointF(x, rc.bottom()))
            p.setPen(QPen(txt))
            p.drawText(QPointF(x - 10, rc.bottom() + 13), fmt_range(r, rmax))
            p.setPen(QPen(gd, 1, Qt.DotLine))
            r += step

        # 检测门限线
        p.setPen(QPen(QColor(200, 90, 60, 150), 1, Qt.DashLine))
        ty = rc.bottom() - rc.height() * self._threshold
        p.drawLine(QPointF(rc.left(), ty), QPointF(rc.right(), ty))

        # 曲线（填充 + 描边）
        prof = np.clip(self._prof, 0.0, 1.0)
        n = prof.size
        if n < 2:
            p.end()
            return
        xs = np.linspace(rc.left(), rc.right(), n)
        ys = rc.bottom() - prof * rc.height()
        poly = [QPointF(rc.left(), rc.bottom())]
        poly += [QPointF(float(x), float(y)) for x, y in zip(xs, ys)]
        poly.append(QPointF(rc.right(), rc.bottom()))

        c = pal["stops"][2][1]
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(c[0], c[1], c[2], 68))
        p.drawPolygon(QPolygonF(poly))

        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(*pal["stops"][3][1]), 1.8))
        path = QPainterPath()
        path.moveTo(float(xs[0]), float(ys[0]))
        for x, y in zip(xs[1:], ys[1:]):
            path.lineTo(float(x), float(y))
        p.drawPath(path)

        # 峰值标记
        p.setPen(QPen(QColor(*pal["target"]), 1.2))
        for i in range(1, n - 1):
            if prof[i] > self._threshold and prof[i] >= prof[i - 1] and prof[i] >= prof[i + 1]:
                x, y = float(xs[i]), float(ys[i])
                p.drawLine(QPointF(x, y - 6), QPointF(x, y - 1))
                p.drawLine(QPointF(x, rc.bottom()), QPointF(x, rc.bottom() + 3))
        p.end()
