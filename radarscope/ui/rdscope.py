"""距离-多普勒图（RD 图）。

这是脉冲多普勒 / 相参雷达的核心显示：横轴距离，纵轴径向速度。
静止杂波全部堆在零多普勒线上形成一条亮带，动目标偏离中线——
一看就知道"这是人还是电风扇"，也是 MTI 滤波效果的直观检验。

存在感应模块给不出这张图，因为没有相位历史；只有做了相干积累的雷达才有。
"""
from __future__ import annotations

import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen
from PyQt5.QtWidgets import QSizePolicy, QWidget

from ..core.model import RadarFrame
from .theme import PALETTES, color_table


class RDScopeWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._rd: np.ndarray | None = None
        self._v: np.ndarray | None = None
        self._r: np.ndarray | None = None
        self._palette_name = "green"
        self._ctable = color_table(self._palette_name)

    def set_palette(self, name: str) -> None:
        self._palette_name = name
        self._ctable = color_table(name)
        self.update()

    def reset(self) -> None:
        self._rd = None
        self.update()

    def push_frame(self, frame: RadarFrame) -> None:
        if frame.rd is None:
            return
        self._rd = frame.rd
        self._v = frame.rd_v_axis
        self._r = frame.r_axis
        self.update()

    def paintEvent(self, _ev) -> None:
        pal = PALETTES[self._palette_name]
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(*pal["bg"]))
        p.setFont(QFont("Consolas", 8))
        p.setPen(QPen(QColor(*pal["text"])))
        p.drawText(QPointF(8, 12), "RD 图 / RANGE-DOPPLER")

        rc = self.rect().adjusted(38, 16, -10, -20)
        if self._rd is None or rc.width() < 10 or rc.height() < 10:
            p.setPen(QPen(QColor(*pal["grid_dim"])))
            p.drawText(rc, 0x0084, "该雷达不提供距离-多普勒数据")
            p.end()
            return

        # 数组第 0 行是 -V_MAX，但雷达图上习惯"速度向上为正"，
        # 所以显示前上下翻转一次，坐标标注才对得上。
        arr = np.ascontiguousarray((np.clip(self._rd, 0.0, 1.0) * 255).astype(np.uint8)[::-1])
        h, w = arr.shape
        img = QImage(arr.data, w, h, w, QImage.Format_Indexed8)
        img.setColorTable(self._ctable)
        p.drawImage(QRectF(rc), img)

        p.setPen(QPen(QColor(*pal["grid"]), 1))
        p.drawRect(rc)

        # 零多普勒线：静止杂波都在这条线上
        mid = rc.top() + rc.height() / 2.0
        p.setPen(QPen(QColor(200, 90, 60, 150), 1, Qt.DashLine))
        p.drawLine(QPointF(rc.left(), mid), QPointF(rc.right(), mid))

        txt = QColor(*pal["text"])
        p.setPen(QPen(txt))
        if self._v is not None and self._v.size:
            vmax = float(np.max(np.abs(self._v)))
            p.drawText(QPointF(4, rc.top() + 8), f"+{vmax:.0f}")
            p.drawText(QPointF(4, rc.bottom()), f"-{vmax:.0f}")
            p.drawText(QPointF(4, mid + 4), "0")
        if self._r is not None and self._r.size:
            rmax = float(self._r[-1])
            unit, scale = ("km", 1000.0) if rmax >= 1000 else ("m", 1.0)
            p.drawText(QPointF(rc.left() - 6, rc.bottom() + 14), "0")
            p.drawText(QPointF(rc.right() - 26, rc.bottom() + 14), f"{rmax / scale:.0f} {unit}")
        p.drawText(QPointF(rc.right() - 70, rc.top() + 12), "速度 m/s")
        p.end()
