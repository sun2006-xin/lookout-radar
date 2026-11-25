"""距离-时间瀑布图（B 显的变体）。

横轴距离、纵轴时间、亮度代表回波强度。这是最能看出"目标在动"的视图：
静止杂波是一条竖直亮线，走动的人是一条斜线。
"""
from __future__ import annotations

import numpy as np
from PyQt5.QtCore import QPointF, QRectF
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen
from PyQt5.QtWidgets import QSizePolicy, QWidget

from ..core.model import RadarFrame
from ..core.pipeline import range_profile
from .theme import PALETTES, color_table, fmt_range, nice_step


class WaterfallWidget(QWidget):
    ROWS = 220

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._buf: np.ndarray | None = None
        self._agc: float | None = None      # 自动增益：缓慢跟随峰值，避免整片饱和
        self._palette_name = "green"
        self._ctable = color_table(self._palette_name)
        self._range_max = 6.0

    def set_palette(self, name: str) -> None:
        self._palette_name = name
        self._ctable = color_table(name)
        self.update()

    def set_range(self, rmax: float) -> None:
        self._range_max = max(0.1, rmax)

    def reset(self) -> None:
        self._buf = None
        self._agc = None
        self.update()

    def push_frame(self, frame: RadarFrame) -> None:
        prof = range_profile(frame)
        if prof.size < 2:
            return
        # 自动增益（AGC）+ 开方压缩。
        # 只用固定 gamma 时，强回波永远顶到 255，整片糊成一块亮色；
        # 按一个缓慢跟随的峰值归一化，弱回波才浮得出来、强回波也留得住层次。
        peak = float(prof.max())
        if peak > 0.02:
            self._agc = peak if self._agc is None else 0.96 * self._agc + 0.04 * peak
        agc = max(0.12, self._agc or 0.12)
        row = (np.clip(prof / agc, 0.0, 1.0) ** 0.75 * 255).astype(np.uint8)
        if self._buf is None or self._buf.shape[1] != row.size:
            self._buf = np.zeros((self.ROWS, row.size), dtype=np.uint8)
        self._buf = np.roll(self._buf, 1, axis=0)
        self._buf[0] = row
        self.update()

    def paintEvent(self, _ev) -> None:
        pal = PALETTES[self._palette_name]
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(*pal["bg"]))
        p.setFont(QFont("Consolas", 8))
        p.setPen(QPen(QColor(*pal["text"])))
        p.drawText(QPointF(8, 12), "瀑布图 / RANGE-TIME")

        rc = self.rect().adjusted(34, 16, -10, -18)
        if self._buf is None or rc.width() < 10 or rc.height() < 10:
            p.end()
            return

        buf = np.ascontiguousarray(self._buf)
        img = QImage(buf.data, buf.shape[1], buf.shape[0], buf.shape[1], QImage.Format_Indexed8)
        img.setColorTable(self._ctable)
        p.drawImage(QRectF(rc), img)

        p.setPen(QPen(QColor(*pal["grid"]), 1))
        p.drawRect(rc)
        step = nice_step(self._range_max, 6)
        r = step
        txt = QColor(*pal["text"])
        while r < self._range_max:
            x = rc.left() + rc.width() * r / self._range_max
            p.setPen(QPen(QColor(*pal["grid_dim"]), 1))
            p.drawLine(QPointF(x, rc.bottom()), QPointF(x, rc.bottom() + 3))
            p.setPen(QPen(txt))
            p.drawText(QPointF(x - 10, rc.bottom() + 13), fmt_range(r, self._range_max))
            r += step
        p.end()
