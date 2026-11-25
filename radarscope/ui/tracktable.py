"""航迹列表。"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from ..core.model import Target


class TrackTable(QTableWidget):
    """航迹表。列是动态的：雷达有什么能力就显示什么列。

    一台感应模块只会出现"航迹/距离/方位/状态"四列；
    换成有 IFF 和威胁分级的相控阵，会自动多出"分类""威胁"两列。
    这就是能力声明驱动界面的实际收益 —— 不用堆一堆永远空着的列。
    """

    BASE_COLS = ["航迹", "距离", "方位"]

    def __init__(self, parent=None):
        super().__init__(0, 4, parent)
        self._cols = list(self.BASE_COLS) + ["状态"]
        self.setHorizontalHeaderLabels(self._cols)
        self._caps = None
        self._meters = True
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._locked = -1
        self._period = 1.0

    def set_locked(self, tid: int) -> None:
        self._locked = tid

    def set_period(self, period: float) -> None:
        """扫描周期，秒。状态列的判据要用它。"""
        self._period = max(0.3, float(period))

    def _status_of(self, t: Target, locked: bool) -> str:
        if locked:
            return "锁定"
        if t.missed <= 0.3:
            return "跟踪"
        # 旋转雷达一圈才照到一次，两圈之内都算"保持"，不能显示失锁
        if t.missed <= self._period * 1.1:
            return "保持"
        return "失锁"

    def configure(self, caps) -> None:
        """按雷达能力重建列。"""
        self._caps = caps
        self._meters = caps.range_max < 1000.0
        cols = list(self.BASE_COLS)
        if caps.has_velocity:
            cols.append("速度")
        if caps.has_iff:
            cols.append("分类")
        if caps.has_threat:
            cols.append("威胁")
        if caps.form == "fusion":
            cols.append("共视")           # 几个站同时看到它，融合质量的直观指标
        cols.append("状态")
        self._cols = cols
        self.setColumnCount(len(cols))
        self.setHorizontalHeaderLabels(cols)
        self.setRowCount(0)

    def _rng_text(self, m: float) -> str:
        return f"{m:.1f} m" if self._meters else f"{m / 1000.0:.1f} km"

    def refresh(self, tracks: list[Target]) -> None:
        caps = self._caps
        self.setRowCount(len(tracks))
        for row, t in enumerate(sorted(tracks, key=lambda x: x.tid)):
            locked = t.tid == self._locked
            cells = [f"T{t.tid}", self._rng_text(t.rng), f"{t.az_deg:+.1f}°"]
            if caps is None or caps.has_velocity:
                cells.append(f"{t.speed:.2f} m/s" if t.speed >= 0.02 else "--")
            if caps is not None and caps.has_iff:
                cells.append(t.cls or "未知")
            if caps is not None and caps.has_threat:
                cells.append({3: "高", 2: "中", 1: "低"}.get(t.threat, "无"))
            if caps is not None and caps.form == "fusion":
                cells.append(f"{t.views} 站")
            cells.append(self._status_of(t, locked))

            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                if locked:
                    item.setForeground(QColor(255, 214, 74))
                elif caps is not None and caps.has_threat and self._cols[col] == "威胁":
                    item.setForeground(QColor(255, 60, 48) if t.threat >= 3
                                       else QColor(255, 150, 50) if t.threat == 2
                                       else QColor(200, 190, 120) if t.threat == 1
                                       else QColor(150, 160, 150))
                elif self._cols[col] == "状态":
                    item.setForeground(QColor(110, 235, 165) if t.missed <= 0.3
                                       else QColor(150, 160, 150))
                self.setItem(row, col, item)
