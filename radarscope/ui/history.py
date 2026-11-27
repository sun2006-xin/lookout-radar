"""历史会话面板：会话总览 + 选中会话的目标统计与告警事件明细 + 回放/导出/清理。"""
from __future__ import annotations

import csv
import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..core.storage import Store


class HistoryPanel(QWidget):
    """历史会话列表 + 目标与事件明细 + 导出 + 回放 + 清理。"""

    replayRequested = pyqtSignal(str)      # 参数是 .rad 文件路径

    COLS = ["开始时间", "雷达", "端点", "时长", "航迹点", "告警", "目标数", "录帧"]
    TRK_COLS = ["航迹号", "点数", "最近(m)", "最远(m)", "峰值速度", "最高高度", "峰值SNR", "威胁", "分类"]
    EVT_COLS = ["时间", "级别", "规则", "消息", "目标"]

    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.store = store
        self._sessions: list = []

        top = QHBoxLayout()
        top.setSpacing(4)
        top.addWidget(QLabel("历史会话"))
        top.addStretch(1)
        btn_ref = QPushButton("刷新")
        btn_ref.clicked.connect(self.refresh)
        btn_clean = QPushButton("清零库空记录")
        btn_clean.setToolTip("清理没有航迹点也没有告警事件的历史空会话")
        btn_clean.clicked.connect(self._cleanup_empty)
        btn_del = QPushButton("删除")
        btn_del.clicked.connect(self._delete_current)
        btn_exp = QPushButton("导出CSV")
        btn_exp.clicked.connect(self._export_current)
        btn_report = QPushButton("报表")
        btn_report.setToolTip("生成会话汇总报表（HTML 含航迹图 / CSV）")
        btn_report.clicked.connect(self._export_report)
        btn_timeline = QPushButton("时间轴")
        btn_timeline.setToolTip("拖动时间轴重放 SQLite 里记录的历史航迹与告警状态")
        btn_timeline.clicked.connect(self._open_timeline)
        btn_play = QPushButton("原画回放")
        btn_play.setToolTip("用「回放」驱动重放当时的画面（需要录制时开了「录制帧」）")
        btn_play.clicked.connect(self._replay_current)
        for b in (btn_ref, btn_clean, btn_del, btn_exp, btn_report, btn_timeline, btn_play):
            top.addWidget(b)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.currentCellChanged.connect(lambda r, _c, _pr, _pc: self._load_session_details(r))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)

        # 下半区：选中历史会话即可直接查看具体目标聚合与告警明细
        self.detail_tabs = QTabWidget()
        self.tbl_tracks = QTableWidget(0, len(self.TRK_COLS))
        self.tbl_tracks.setHorizontalHeaderLabels(self.TRK_COLS)
        self.tbl_tracks.verticalHeader().setVisible(False)
        self.tbl_tracks.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_tracks.setShowGrid(False)
        self.tbl_tracks.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        self.tbl_events = QTableWidget(0, len(self.EVT_COLS))
        self.tbl_events.setHorizontalHeaderLabels(self.EVT_COLS)
        self.tbl_events.verticalHeader().setVisible(False)
        self.tbl_events.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_events.setShowGrid(False)
        eh = self.tbl_events.horizontalHeader()
        eh.setSectionResizeMode(QHeaderView.ResizeToContents)
        eh.setSectionResizeMode(3, QHeaderView.Stretch)

        self.detail_tabs.addTab(self.tbl_tracks, "选中会话 · 目标汇总")
        self.detail_tabs.addTab(self.tbl_events, "选中会话 · 告警明细")

        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.detail_tabs)
        self.splitter.setSizes([180, 140])

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addLayout(top)
        lay.addWidget(self.splitter, 1)
        self.refresh()

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        try:
            self._sessions = self.store.list_sessions(80)
        except Exception:
            self._sessions = []

        self.table.setRowCount(len(self._sessions))
        for row, s in enumerate(self._sessions):
            try:
                st = self.store.session_stats(s.id)
                has_arc = bool(self.store.archive_of(s.id))
            except Exception:
                st = {"tracks_rows": 0, "events": 0, "max_tid": 0}
                has_arc = False
            started = time.strftime("%m-%d %H:%M:%S", time.localtime(s.started))
            dur = max(0.0, s.duration)
            dur_txt = f"{dur / 60:.1f} 分" if dur >= 60 else f"{dur:.0f} 秒"
            cells = [
                started,
                s.caps.get("name") or s.driver,
                s.endpoint or "虚拟",
                dur_txt,
                str(st.get("tracks_rows", 0)),
                str(st.get("events", 0)),
                str(st.get("max_tid", 0)),
                ".rad" if has_arc else "--",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter if col in (0, 3, 4, 5, 6, 7) else
                                      Qt.AlignLeft | Qt.AlignVCenter)
                self.table.setItem(row, col, item)
        if self._sessions:
            self.table.selectRow(0)
            self._load_session_details(0)
        else:
            self.tbl_tracks.setRowCount(0)
            self.tbl_events.setRowCount(0)

    def _load_session_details(self, row: int) -> None:
        if row < 0 or row >= len(self._sessions):
            self.tbl_tracks.setRowCount(0)
            self.tbl_events.setRowCount(0)
            return
        sid = self._sessions[row].id
        try:
            summaries = self.store.track_summaries(sid)
        except Exception:
            summaries = []
        self.tbl_tracks.setRowCount(len(summaries))
        for r, item in enumerate(summaries):
            vals = [
                f"T{item['tid']}",
                str(item["points"]),
                f"{item['min_rng']:.1f}",
                f"{item['max_rng']:.1f}",
                f"{item['max_speed']:.1f} m/s",
                f"{item['max_z']:.1f} m" if item["max_z"] > 0.05 else "--",
                f"{item['peak_snr']:.2f}",
                {3: "高", 2: "中", 1: "低"}.get(item["max_threat"], "无"),
                item["cls"] or "--",
            ]
            for c, txt in enumerate(vals):
                it = QTableWidgetItem(txt)
                it.setTextAlignment(Qt.AlignCenter)
                self.tbl_tracks.setItem(r, c, it)

        try:
            evts = self.store.events_of(sid, limit=300)
        except Exception:
            evts = []
        self.tbl_events.setRowCount(len(evts))
        for r, (ts, level, kind, rule_id, msg, _active, tid) in enumerate(evts):
            vals = [
                time.strftime("%H:%M:%S", time.localtime(ts)),
                str(level),
                str(rule_id or kind or "--"),
                str(msg),
                f"T{tid}" if tid is not None and tid >= 0 else "--",
            ]
            for c, txt in enumerate(vals):
                it = QTableWidgetItem(txt)
                if c != 3:
                    it.setTextAlignment(Qt.AlignCenter)
                self.tbl_events.setItem(r, c, it)

    # ------------------------------------------------------------------ #
    def _delete_current(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._sessions):
            QMessageBox.information(self, "删除会话", "先在列表里选中一次会话")
            return
        s = self._sessions[row]
        if QMessageBox.question(
                self, "删除历史会话", f"确定永久删除会话 #{s.id} 及其航迹与告警记录？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.store.delete_session(s.id)
        self.refresh()

    def _cleanup_empty(self) -> None:
        n = self.store.cleanup_empty_sessions()
        self.refresh()
        QMessageBox.information(self, "清理完成", f"已清理 {n} 条空会话记录。")

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
            rows = self.store.tracks_of(s.id, include_3d=True)
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        if not rows:
            QMessageBox.information(self, "导出", "这次会话没有航迹数据")
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["时间", "航迹号", "X(m)", "Y(m)", "Z(m)", "Vx", "Vy",
                        "距离(m)", "方位(°)", "仰角(°)", "RCS", "SNR", "威胁", "分类"])
            for r in rows:
                ts, tid, x, y, vx, vy, rng, az, snr, threat, cls, z, el_deg, rcs = r
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                            tid, f"{x:.3f}", f"{y:.3f}", f"{z or 0.0:.3f}", f"{vx:.3f}", f"{vy:.3f}",
                            f"{rng:.3f}", f"{az:.2f}", f"{el_deg or 0.0:.2f}", f"{rcs or 0.0:.2f}",
                            f"{snr:.3f}", threat, cls])
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

    def _open_timeline(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._sessions):
            QMessageBox.information(self, "时间轴回放", "先在列表里选中一次会话")
            return
        from .timeline import TimelineDialog

        s = self._sessions[row]
        TimelineDialog(self.store, s.id, s.caps, self).exec_()

    def _export_report(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._sessions):
            QMessageBox.information(self, "导出报表", "先在列表里选中一次会话")
            return
        from ..core.report import export_session_report

        s = self._sessions[row]
        path, _ = QFileDialog.getSaveFileName(
            self, "导出会话报表", f"report-session-{s.id}.html",
            "HTML 报表 (*.html);;CSV 汇总 (*.csv)")
        if not path:
            return
        out = export_session_report(self.store, s.id, path)
        QMessageBox.information(self, "导出完成", f"报表已生成：\n{out}")

    def showEvent(self, ev) -> None:
        self.refresh()
        super().showEvent(ev)
