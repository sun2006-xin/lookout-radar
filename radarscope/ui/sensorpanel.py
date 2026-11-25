"""传感器状态面板：哪台在线、哪个朝向、多少帧、有没有报错。

多站组网最怕的是"看着有图，其实是 3 台挂了只剩 3 台在撑"。
这块面板就是用来一眼看穿这件事的。
"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


class SensorPanel(QWidget):
    COLS = ["传感器", "朝向", "视场", "端点", "状态", "帧率", "累计帧", "错误"]

    def __init__(self, parent=None):
        super().__init__(parent)
        top = QHBoxLayout()
        top.addWidget(QLabel("本雷达组"))
        top.addStretch(1)
        self.btn_cfg = QPushButton("传感器配置…")
        top.addWidget(self.btn_cfg)
        self.lbl_gaps = QLabel("")
        top.addWidget(self.lbl_gaps)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)

    def update_sensors(self, sensors: list[dict], gaps: list | None = None) -> None:
        self.table.setRowCount(len(sensors))
        for row, s in enumerate(sensors):
            online = bool(s.get("online"))
            cells = [
                str(s.get("name", "")),
                f"{float(s.get('bearing', 0)):.0f}°",
                f"{float(s.get('fov', 0)):.0f}°",
                str(s.get("endpoint", "")),
                "在线" if online else "离线",
                f"{float(s.get('fps', 0)):.1f}",
                str(s.get("frames", 0)),
                str(s.get("errors", 0)) if not s.get("last_err") else
                f"{s.get('errors', 0)} · {s.get('last_err')[:40]}",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter if col != 3 else
                                      Qt.AlignLeft | Qt.AlignVCenter)
                if col == 4:
                    item.setForeground(QColor(110, 235, 165) if online
                                       else QColor(255, 92, 76))
                elif col == 7 and s.get("errors"):
                    item.setForeground(QColor(232, 163, 58))
                self.table.setItem(row, col, item)

        if gaps:
            txt = "盲区：" + "、".join(f"{a:.0f}°~{b:.0f}°" for a, b in gaps[:4])
            self.lbl_gaps.setText(txt)
            self.lbl_gaps.setStyleSheet("color:#FF5C4C;font-size:12px;")
        else:
            self.lbl_gaps.setText("无盲区" if sensors else "")
            self.lbl_gaps.setStyleSheet("color:#6EEBA5;font-size:12px;")
