"""历史会话面板。

数据早就写进 SQLite 了，但如果界面上看不见，对用户来说等于没有。
这块把"历史记录"变成一个能点、能查、能导出的东西。

设计上刻意简单：只读列表 + 导出 CSV。真正的回放走 replay 驱动，
因为完整的复盘需要强度矩阵，那是另一个文件（.rad）。
"""
from __future__ import annotations

import csv
import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.storage import Store


class HistoryPanel(QWidget):
    """历史会话列表 + 导出 + 回放。

    回放是这个面板存在的真正理由：光能查数据库不够，
    要从"某天下午那次异常"直接回到当时的画面才算闭环。
    """

    replayRequested = pyqtSignal(str)      # 参数是 .rad 文件路径

    COLS = ["开始时间", "雷达", "端点", "时长", "航迹点", "告警", "备注"]

    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.store = store
        self._sessions: list = []

        top = QHBoxLayout()
        top.addWidget(QLabel("历史会话"))
        top.addStretch(1)
        btn_ref = QPushButton("刷新")
        btn_ref.clicked.connect(self.refresh)
        btn_exp = QPushButton("导出该会话航迹 CSV")
        btn_exp.clicked.connect(self._export_current)
        btn_play = QPushButton("回放该会话")
        btn_play.setToolTip("用「回放」驱动重放当时的画面（需要录制时开了「录制帧」）")
        btn_play.clicked.connect(self._replay_current)
        top.addWidget(btn_ref)
        top.addWidget(btn_exp)
        top.addWidget(btn_play)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        self.refresh()

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        try:
            self._sessions = self.store.list_sessions(60)
        except Exception:
            self._sessions = []

        self.table.setRowCount(len(self._sessions))
        for row, s in enumerate(self._sessions):
            try:
                st = self.store.session_stats(s.id)
            except Exception:
                st = {"tracks_rows": 0, "events": 0}
            started = time.strftime("%m-%d %H:%M:%S", time.localtime(s.started))
            dur = s.duration
            dur_txt = f"{dur / 60:.1f} 分" if dur >= 60 else f"{dur:.0f} 秒"
            cells = [started, s.caps.get("name") or s.driver, s.endpoint or "虚拟",
                     dur_txt, str(st.get("tracks_rows", 0)), str(st.get("events", 0)),
                     s.note or ""]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter if col in (1, 3, 4, 5) else
                                      Qt.AlignLeft | Qt.AlignVCenter)
                self.table.setItem(row, col, item)

    # ------------------------------------------------------------------ #
    def _export_current(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._sessions):
            QMessageBox.information(self, "导出", "先在列表里选中一次会话")
            return
        s = self._sessions[row]
        path, _ = QFileDialog.getSaveFileName(
            self, "导出航迹", f"session-{s.id}.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            rows = self.store.tracks_of(s.id)
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        if not rows:
            QMessageBox.information(self, "导出", "这次会话没有航迹数据")
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["时间", "航迹号", "X(m)", "Y(m)", "Vx", "Vy",
                        "距离(m)", "方位(°)", "SNR", "威胁", "分类"])
            for r in rows:
                ts, tid, x, y, vx, vy, rng, az, snr, threat, cls = r
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                            tid, f"{x:.3f}", f"{y:.3f}", f"{vx:.3f}", f"{vy:.3f}",
                            f"{rng:.3f}", f"{az:.2f}", f"{snr:.3f}", threat, cls])
        QMessageBox.information(self, "导出完成", f"已写出 {len(rows)} 行到\n{path}")

    def _replay_current(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._sessions):
            QMessageBox.information(self, "回放", "先在列表里选中一次会话")
            return
        s = self._sessions[row]
        path = self.store.archive_of(s.id)
        if not path:
            QMessageBox.information(
                self, "无法回放",
                "这次会话没有录制强度矩阵，只能导出航迹。\n\n"
                "要能回放，连接时得勾上「录制帧」——矩阵太大不适合进数据库，\n"
                "所以单独存成 .rad 文件。")
            return
        self.replayRequested.emit(path)

    def showEvent(self, ev) -> None:
        self.refresh()
        super().showEvent(ev)
