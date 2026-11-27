"""距离-时间瀑布图（按航迹距离折线投影，ROADMAP B1）。

横轴距离、纵轴时间（最新在顶部，向下滚动），每条航迹一条独立折线，背景留空。
相比早期的 `scan.max(axis=0)` 投影，多站融合与多目标场景下不会互相抹宽糊成一片。
"""
from __future__ import annotations

from collections import deque

import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QMenu, QSizePolicy, QWidget

from ..core.model import RadarFrame, Target
from .theme import PALETTES, color_table, fmt_range, mono_font, nice_step

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
        self._raw_rows: deque[np.ndarray] = deque(maxlen=self.ROWS)
        self._palette_name = "green"
        self._ctable = color_table(self._palette_name)
        self._range_max = 6.0
        self.mode = "tracks"  # "tracks"（航迹距离折线） | "raw"（原始回波强度瀑布图）

    def set_palette(self, name: str) -> None:
        self._palette_name = name
        if name in PALETTES:
            self._ctable = color_table(name)
        self.update()

    def set_mode(self, mode: str) -> None:
        if mode in ("tracks", "raw") and mode != self.mode:
            self.mode = mode
            self.update()

    def set_range(self, rmax: float) -> None:
        self._range_max = max(0.1, rmax)
        self.update()

    def reset(self) -> None:
        self._history.clear()
        self._raw_rows.clear()
        self.update()

    def push_frame(self, frame: RadarFrame, tracks: list[Target] | None = None) -> None:
        """同时记录航迹距离快照与原始回波最大能量剖面，支持双模式实时切换。"""
        src = tracks if tracks is not None else frame.targets
        row: dict[int, float] = {}
        for idx, t in enumerate(src):
            tid = t.tid if t.tid >= 0 else -(idx + 1)
            row[tid] = float(t.rng)
        self._history.appendleft(row)
        if frame.has_scan and frame.scan is not None and frame.scan.size > 0:
            prof = np.max(frame.scan, axis=0).astype(np.float32, copy=True)
            self._raw_rows.appendleft(prof)
        self.update()

    def contextMenuEvent(self, ev) -> None:
        menu = QMenu(self)
        act_t = menu.addAction("航迹距离折线模式 (B1)")
        act_r = menu.addAction("原始回波强度瀑布模式 (H1)")
        act_t.setCheckable(True)
        act_r.setCheckable(True)
        act_t.setChecked(self.mode == "tracks")
        act_r.setChecked(self.mode == "raw")
        chosen = menu.exec_(ev.globalPos())
        if chosen == act_t:
            self.set_mode("tracks")
        elif chosen == act_r:
            self.set_mode("raw")

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
        title = "瀑布图 / RAW ECHO WATERFALL" if self.mode == "raw" else "瀑布图 / TRACK RANGE-TIME"
        p.drawText(QPointF(8, 12), title)

        rc = self.rect().adjusted(34, 16, -10, -18)
        if rc.width() < 10 or rc.height() < 10:
            p.end()
            return

        if self.mode == "raw" and self._raw_rows:
            cols = int(self._raw_rows[0].size)
            mat = np.zeros((len(self._raw_rows), cols), dtype=np.float32)
            for ri, prof in enumerate(self._raw_rows):
                if prof.size == cols:
                    mat[ri] = prof
                elif prof.size > 1:
                    mat[ri] = np.interp(np.linspace(0.0, 1.0, cols), np.linspace(0.0, 1.0, prof.size), prof)
            arr = np.ascontiguousarray((np.clip(mat, 0.0, 1.0) * 255).astype(np.uint8))
            img = QImage(arr.data, cols, arr.shape[0], cols, QImage.Format_Indexed8)
            img.setColorTable(self._ctable)
            p.drawImage(QRectF(rc.left(), rc.top(), rc.width(), rc.height() * (arr.shape[0] / self.ROWS)), img)

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
            if self.mode != "raw" or not self._raw_rows:
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
