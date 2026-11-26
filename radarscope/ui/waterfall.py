"""距离-时间瀑布图（按航迹距离折线投影，ROADMAP B1）。

横轴距离、纵轴时间（最新在顶部，向下滚动），每条航迹一条独立折线，背景留空。
相比早期的 `scan.max(axis=0)` 投影，多站融合与多目标场景下不会互相抹宽糊成一片。
"""
from __future__ import annotations

from collections import deque

from PyQt5.QtCore import QPointF
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QSizePolicy, QWidget

from ..core.model import RadarFrame, Target
from .theme import PALETTES, fmt_range, mono_font, nice_step

# 高对比度航迹配色：浅底（daylight）与深底（ice 等）都清晰可辨
_TRACK_COLORS = (
    "#2563EB",  # 蓝
    "#DC2626",  # 红
    "#059669",  # 绿
    "#D97706",  # 琥珀
    "#7C3AED",  # 紫
    "#0891B2",  # 青
    "#DB2777",  # 粉
    "#4F46E5",  # 靛
)


class WaterfallWidget(QWidget):
    ROWS = 220

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._history: deque[dict[int, float]] = deque(maxlen=self.ROWS)
        self._palette_name = "green"
        self._range_max = 6.0

    def set_palette(self, name: str) -> None:
        self._palette_name = name
        self.update()

    def set_range(self, rmax: float) -> None:
        self._range_max = max(0.1, rmax)
        self.update()

    def reset(self) -> None:
        self._history.clear()
        self.update()

    def push_frame(self, frame: RadarFrame, tracks: list[Target] | None = None) -> None:
        """记录本帧航迹的距离快照，供距离-时间折线绘制。"""
        src = tracks if tracks is not None else frame.targets
        row: dict[int, float] = {}
        for idx, t in enumerate(src):
            tid = t.tid if t.tid >= 0 else -(idx + 1)
            row[tid] = float(t.rng)
        self._history.appendleft(row)
        self.update()

    def track_series(self) -> dict[int, list[tuple[int, float]]]:
        """返回每条航迹在滚动窗口内的 (行龄, 距离m) 序列，供绘制与单测使用。"""
        series: dict[int, list[tuple[int, float]]] = {}
        for age, row in enumerate(self._history):
            for tid, rng in row.items():
                series.setdefault(tid, []).append((age, rng))
        return series

    def paintEvent(self, _ev) -> None:
        pal = PALETTES[self._palette_name]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(*pal["bg"]))
        p.setFont(mono_font(8))
        p.setPen(QPen(QColor(*pal["text"])))
        p.drawText(QPointF(8, 12), "瀑布图 / TRACK RANGE-TIME")

        rc = self.rect().adjusted(34, 16, -10, -18)
        if rc.width() < 10 or rc.height() < 10:
            p.end()
            return

        # 竖向距离网格
        step = nice_step(self._range_max, 6)
        r = step
        txt = QColor(*pal["text"])
        while r < self._range_max:
            x = rc.left() + rc.width() * r / self._range_max
            p.setPen(QPen(QColor(*pal["grid_dim"]), 1))
            p.drawLine(QPointF(x, rc.top()), QPointF(x, rc.bottom() + 3))
            p.setPen(QPen(txt))
            p.drawText(QPointF(x - 10, rc.bottom() + 13), fmt_range(r, self._range_max))
            r += step

        p.setPen(QPen(QColor(*pal["grid"]), 1))
        p.drawRect(rc)

        series = self.track_series()
        if not series:
            p.setPen(QPen(QColor(*pal["text"])))
            p.drawText(rc.center() - QPointF(28, 0), "等待航迹…")
            p.end()
            return

        row_h = rc.height() / max(1, self.ROWS - 1)
        p.save()
        p.setClipRect(rc)
        for tid, pts in series.items():
            color = QColor(_TRACK_COLORS[abs(tid) % len(_TRACK_COLORS)])
            p.setPen(QPen(color, 1.8))
            path = QPainterPath()
            prev_age: int | None = None
            for age, rng in pts:
                x = rc.left() + rc.width() * max(0.0, min(1.0, rng / self._range_max))
                y = rc.top() + age * row_h
                if prev_age is None or age - prev_age > 3:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
                prev_age = age
            p.drawPath(path)

            # 在最新位置标出航迹号
            newest_age, newest_rng = pts[0]
            if newest_age <= 2:
                nx = rc.left() + rc.width() * max(0.0, min(1.0, newest_rng / self._range_max))
                ny = rc.top() + newest_age * row_h
                p.setBrush(color)
                p.drawEllipse(QPointF(nx, ny), 3.0, 3.0)
                if tid >= 0:
                    p.drawText(QPointF(min(rc.right() - 22, nx + 5), max(rc.top() + 10, ny + 4)),
                               f"T{tid}")
        p.restore()
        p.end()
