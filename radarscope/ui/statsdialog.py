"""统计分析与事件检索。

M6 的目标：从"实时看"走到"事后查"。三块内容：

    概览     —— 这次跑多久、多少航迹点、多少告警
    告警统计 —— 哪条规则报得最多（用来调门限，别一上来就乱告警）
    事件检索 —— 按级别/规则过滤，找出"那天下午那几次"

数据全部走 SQL 聚合，不把百万行拉回内存再算 —— 这是能不能查得动大库的关键。
"""
from __future__ import annotations

import time

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..core.storage import Store
from .theme import mono_font

LEVEL_TEXT = {"crit": "紧急", "warn": "警告", "info": "提示"}
LEVEL_COLOR = {"crit": QColor(255, 92, 76), "warn": QColor(232, 163, 58),
               "info": QColor(140, 160, 150)}


class ActivityChart(QWidget):
    """目标活动量随时间的柱状图。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self._data: list[tuple[int, int]] = []
        self._bucket = 60

    def set_data(self, data: list[tuple[int, int]], bucket_s: int = 60) -> None:
        self._data = data
        self._bucket = bucket_s
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        bg = self.palette().window().color()
        dark = bg.lightness() < 128
        p.fillRect(self.rect(), QColor(10, 13, 11) if dark else QColor("#F8FAFC"))
        rc = self.rect().adjusted(36, 12, -12, -20)
        p.setFont(mono_font(8))
        p.setPen(QPen(QColor(90, 108, 98) if dark else QColor("#64748B")))
        p.drawText(rc.left() - 30, rc.top() + 8, "点数")
        if not self._data:
            p.setPen(QPen(QColor(120, 130, 124)))
            p.drawText(rc, Qt.AlignCenter, "这次会话没有航迹数据")
            p.end()
            return

        if not self._data or len(self._data) < 2:
            # 只有一个桶时画柱子会变成"整块色块"，看起来像渲染坏了。
            # 这种情况直接说明白，比画一个误导性的图好。
            p.setPen(QPen(QColor(120, 130, 124)))
            p.drawText(rc, Qt.AlignCenter,
                       "数据太少（不足 2 个时间桶），换个更长的会话看看")
            p.end()
            return

        n = len(self._data)
        peak = max(1, max(c for _, c in self._data))
        # 柱宽设上限，桶很少时不至于画成一片色块
        bw = min(rc.width() / n, 48.0)
        x_start = rc.left() + (rc.width() - bw * n) / 2.0
        p.setPen(Qt.NoPen)
        for i, (b, c) in enumerate(self._data):
            h = rc.height() * c / peak
            x = x_start + i * bw
            col = QColor(60, 150, 100) if c < peak * 0.8 else QColor(210, 150, 50)
            p.setBrush(col)
            p.drawRect(int(x), int(rc.bottom() - h), max(1, int(bw) - 2), int(h))

        p.setPen(QPen(QColor(70, 84, 76)))
        p.drawLine(rc.bottomLeft(), rc.bottomRight())
        p.setPen(QPen(QColor(140, 155, 146)))
        unit = "分" if self._bucket == 60 else f"{self._bucket}秒"
        p.drawText(rc.left(), rc.bottom() + 14, f"0 {unit}")
        p.drawText(rc.right() - 60, rc.bottom() + 14,
                   f"{n} {unit} · 峰值 {peak}")
        p.end()


class StatsDialog(QDialog):
    def __init__(self, store: Store, sid: int | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("统计分析与事件检索")
        self.resize(900, 620)
        self.store = store
        self._sessions = store.list_sessions(60)
        self._sid = sid if sid is not None else (
            self._sessions[0].id if self._sessions else None)

        top = QHBoxLayout()
        top.addWidget(QLabel("会话"))
        self.cb_session = QComboBox()
        for s in self._sessions:
            ts = time.strftime("%m-%d %H:%M", time.localtime(s.started))
            self.cb_session.addItem(f"#{s.id} {ts}  {s.caps.get('name') or s.driver}", s.id)
        idx = self.cb_session.findData(self._sid)
        if idx >= 0:
            self.cb_session.setCurrentIndex(idx)
        self.cb_session.currentIndexChanged.connect(self._on_session)
        top.addWidget(self.cb_session, 1)
        btn_ref = QPushButton("刷新")
        btn_ref.clicked.connect(self._reload)
        top.addWidget(btn_ref)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview(), "概览")
        self.tabs.addTab(self._build_alarm(), "告警统计")
        self.tabs.addTab(self._build_events(), "事件检索")

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.tabs, 1)
        self._reload()

    # ------------------------------------------------------------------ #
    def _build_overview(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.lbl_kpi = QLabel("")
        self.lbl_kpi.setStyleSheet("font-size:13px;font-weight:600;")
        self.chart = ActivityChart()
        self.tbl_tracks = QTableWidget(0, 7)
        self.tbl_tracks.setHorizontalHeaderLabels(["航迹号", "点数", "最近(m)", "最远(m)", "最大速度", "峰值SNR", "最高威胁"])
        self.tbl_tracks.verticalHeader().setVisible(False)
        self.tbl_tracks.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_tracks.setShowGrid(False)
        self.tbl_tracks.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_tracks.setMaximumHeight(175)
        v.addWidget(self.lbl_kpi)
        v.addWidget(self.chart, 1)
        v.addWidget(QLabel("目标统计摘要（按航迹号 SQL 聚合）："))
        v.addWidget(self.tbl_tracks)
        return w

    def _build_alarm(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.tbl_alarm = QTableWidget(0, 3)
        self.tbl_alarm.setHorizontalHeaderLabels(["规则", "次数", "最近消息"])
        self.tbl_alarm.verticalHeader().setVisible(False)
        self.tbl_alarm.setShowGrid(False)
        self.tbl_alarm.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_alarm.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl_alarm.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        v.addWidget(QLabel("按规则统计报警次数 —— 某条规则老是第一，说明门限该放松了"))
        v.addWidget(self.tbl_alarm, 1)
        return w

    def _build_events(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("级别"))
        self.cb_level = QComboBox()
        self.cb_level.addItem("全部", None)
        for k, text in LEVEL_TEXT.items():
            self.cb_level.addItem(text, k)
        self.cb_level.currentIndexChanged.connect(self._load_events)
        bar.addWidget(self.cb_level)

        bar.addWidget(QLabel("规则"))
        self.cb_rule = QComboBox()
        self.cb_rule.addItem("全部", None)
        for r in self.store.distinct_rule_ids():
            self.cb_rule.addItem(r, r)
        self.cb_rule.currentIndexChanged.connect(self._load_events)
        bar.addWidget(self.cb_rule)
        bar.addStretch(1)
        v.addLayout(bar)

        self.tbl_event = QTableWidget(0, 5)
        self.tbl_event.setHorizontalHeaderLabels(["时间", "级别", "规则", "消息", "航迹"])
        self.tbl_event.verticalHeader().setVisible(False)
        self.tbl_event.setShowGrid(False)
        self.tbl_event.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.tbl_event.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        v.addWidget(self.tbl_event, 1)
        return w

    # ------------------------------------------------------------------ #
    def _on_session(self) -> None:
        self._sid = self.cb_session.currentData()
        self._reload()

    def _reload(self) -> None:
        if self._sid is None:
            self.lbl_kpi.setText("还没有任何会话记录")
            return
        st = self.store.session_stats(self._sid)
        first, last = st.get("first_ts"), st.get("last_ts")
        dur = (last - first) if (first and last) else 0.0
        self.lbl_kpi.setText(
            f"时长 {dur / 60:.1f} 分钟 ｜ 航迹点 {st.get('tracks_rows', 0)} "
            f"｜ 事件 {st.get('events', 0)} ｜ 最大航迹号 {st.get('max_tid', 0)}")

        # 桶宽自适应：短会话按 10s，长会话按分钟，避免柱子过多或过少
        bucket = 10 if dur <= 300 else 60 if dur <= 7200 else 300
        self.chart.set_data(self.store.tracks_per_bucket(self._sid, bucket), bucket)

        summaries = self.store.track_summaries(self._sid)
        self.tbl_tracks.setRowCount(len(summaries))
        for r, item in enumerate(summaries):
            vals = [
                f"T{item['tid']}",
                str(item["points"]),
                f"{item['min_rng']:.1f}",
                f"{item['max_rng']:.1f}",
                f"{item['max_speed']:.1f} m/s",
                f"{item['peak_snr']:.2f}",
                {3: "高", 2: "中", 1: "低"}.get(item["max_threat"], "无"),
            ]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                it.setTextAlignment(Qt.AlignCenter)
                self.tbl_tracks.setItem(r, c, it)

        rows = self.store.alarm_summary(self._sid)
        self.tbl_alarm.setRowCount(len(rows))
        for r, (rule, msg, cnt) in enumerate(rows):
            for c, val in enumerate([rule or "(未归类)", str(cnt), msg]):
                item = QTableWidgetItem(val)
                if c == 1:
                    item.setTextAlignment(Qt.AlignCenter)
                    if cnt >= 5:
                        item.setForeground(QColor(217, 119, 6))
                self.tbl_alarm.setItem(r, c, item)

        self._load_events()

    def _load_events(self) -> None:
        if self._sid is None:
            return
        rows = self.store.query_events(
            sid=self._sid,
            level=self.cb_level.currentData(),
            rule_id=self.cb_rule.currentData(),
            limit=800)
        self.tbl_event.setRowCount(len(rows))
        for r, (ts, level, kind, rule, msg, _active, tid, _sid) in enumerate(rows):
            cells = [time.strftime("%m-%d %H:%M:%S", time.localtime(ts)),
                     LEVEL_TEXT.get(level, level), rule or kind or "-", msg,
                     f"T{tid}" if tid and tid >= 0 else "-"]
            for c, val in enumerate(cells):
                item = QTableWidgetItem(str(val))
                if c == 1:
                    item.setForeground(LEVEL_COLOR.get(level, QColor(160, 170, 162)))
                self.tbl_event.setItem(r, c, item)
