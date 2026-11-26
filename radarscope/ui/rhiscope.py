"""3D 俯仰 RHI 显示控件（Distance-Height Indicator，ROADMAP E7）。

横轴为水平/斜距，纵轴为高度 Z（支持 0°~45° 仰角扇区波束网格与高度分层过滤）。
仅在接入声明 `Capabilities.has_elevation == True` 的三维激光/4D 毫米波雷达时展开。
"""
from __future__ import annotations

from math import cos, radians, sin

import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen
from PyQt5.QtWidgets import QSizePolicy, QWidget

from ..core.model import Capabilities, RadarFrame, Target
from .theme import DEFAULT_PALETTE, PALETTES, color_table, fmt_range, mono_font, nice_step


class RHIScopeWidget(QWidget):
    """距离-高度（RHI）剖面显示器，支持高度分层滤除与 3D 航迹投影。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(155)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._caps = Capabilities()
        self._rhi: np.ndarray | None = None
        self._el_axis: np.ndarray | None = None
        self._r_axis: np.ndarray | None = None
        self._tracks: list[Target] = []
        self._palette_name = DEFAULT_PALETTE
        self._ctable = self._ctable_for(DEFAULT_PALETTE)
        self.z_max: float = 6.0
        self.z_filter_min: float = -2.0
        self.z_filter_max: float = 100.0

    @staticmethod
    def _ctable_for(name: str) -> list[int]:
        tbl = color_table(name)
        for i in range(20):
            tbl[i] = 0x00000000
        return tbl

    def set_palette(self, name: str) -> None:
        if name not in PALETTES:
            return
        self._palette_name = name
        self._ctable = self._ctable_for(name)
        self.update()

    def set_capabilities(self, caps: Capabilities) -> None:
        self._caps = caps
        self.z_max = max(2.0, round(caps.range_max * 0.75, 1))
        self.reset()

    def set_height_filter(self, z_min: float, z_max: float) -> None:
        self.z_filter_min = float(z_min)
        self.z_filter_max = float(z_max)
        self.update()

    def reset(self) -> None:
        self._rhi = None
        self._tracks = []
        self.update()

    def push_frame(self, frame: RadarFrame, tracks: list[Target] | None = None) -> None:
        self._rhi = frame.rhi
        self._el_axis = frame.el_axis
        self._r_axis = frame.r_axis
        self._tracks = list(tracks if tracks is not None else frame.targets)
        self.update()

    def paintEvent(self, _ev) -> None:
        pal = PALETTES[self._palette_name]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(*pal["bg"]))
        p.setFont(mono_font(8))
        p.setPen(QPen(QColor(*pal["text"])))
        p.drawText(QPointF(8, 12), "3D 俯仰 RHI / DISTANCE-HEIGHT")

        rc = self.rect().adjusted(38, 18, -10, -20)
        if rc.width() < 20 or rc.height() < 20:
            p.end()
            return

        rmax = max(0.5, self._caps.range_max)
        zmax = max(1.0, self.z_max)

        # 绘制高度/距离网格与仰角引导射线
        p.setPen(QPen(QColor(*pal["grid_dim"]), 1, Qt.DotLine))
        for deg in (15, 30, 45):
            r_end = min(rmax, zmax / max(1e-6, sin(radians(deg))))
            x_end = rc.left() + rc.width() * min(1.0, (r_end * cos(radians(deg))) / rmax)
            z_end = min(zmax, r_end * sin(radians(deg)))
            y_end = rc.bottom() - rc.height() * (z_end / zmax)
            p.drawLine(QPointF(rc.left(), rc.bottom()), QPointF(x_end, y_end))

        step_r = nice_step(rmax, 5)
        r = step_r
        while r < rmax:
            x = rc.left() + rc.width() * (r / rmax)
            p.setPen(QPen(QColor(*pal["grid_dim"]), 1, Qt.DotLine))
            p.drawLine(QPointF(x, rc.top()), QPointF(x, rc.bottom()))
            p.setPen(QPen(QColor(*pal["text"])))
            p.drawText(QPointF(x - 10, rc.bottom() + 13), fmt_range(r, rmax))
            r += step_r

        for zi in range(1, 4):
            zz = zmax * zi / 3.0
            y = rc.bottom() - rc.height() * (zz / zmax)
            p.setPen(QPen(QColor(*pal["grid_dim"]), 1, Qt.DotLine))
            p.drawLine(QPointF(rc.left(), y), QPointF(rc.right(), y))
            p.setPen(QPen(QColor(*pal["text"])))
            p.drawText(QPointF(4, y + 4), f"{zz:.1f}m")

        # 俯仰×距离热图投影（若设备提供了 rhi 矩阵）
        if self._rhi is not None and self._rhi.size > 0:
            arr = np.ascontiguousarray((np.clip(self._rhi, 0.0, 1.0) * 255).astype(np.uint8)[::-1])
            h, w = arr.shape
            img = QImage(arr.data, w, h, w, QImage.Format_Indexed8)
            img.setColorTable(self._ctable)
            p.setOpacity(0.82)
            p.drawImage(QRectF(rc), img)
            p.setOpacity(1.0)

        p.setPen(QPen(QColor(*pal["grid"]), 1))
        p.drawRect(rc)

        # 绘制三维航迹点 (rng, z)
        p.save()
        p.setClipRect(rc)
        for t in self._tracks:
            zz = float(getattr(t, "z", 0.0))
            if zz < self.z_filter_min or zz > self.z_filter_max:
                continue
            x = rc.left() + rc.width() * min(1.0, max(0.0, t.rng / rmax))
            y = rc.bottom() - rc.height() * min(1.0, max(0.0, zz / zmax))
            col = QColor(*pal["lock"]) if t.locked else QColor(*pal["target"])
            p.setPen(QPen(col, 1.5))
            p.setBrush(col)
            p.drawEllipse(QPointF(x, y), 3.6, 3.6)
            lab = f"T{t.tid} z={zz:.1f}m" if t.tid >= 0 else f"z={zz:.1f}m"
            p.drawText(QPointF(min(rc.right() - 64, x + 6), max(rc.top() + 11, y - 3)), lab)
        p.restore()
        p.end()
