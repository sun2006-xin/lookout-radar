"""时间轴轨迹回放对话框（ROADMAP C3）。

从 SQLite 读出历史会话，通过滑块拖动到任意秒，
在 PPI 上重建该时刻的航迹位置与截至该时刻的告警状态。
"""
from __future__ import annotations

import time

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QSlider,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QHeaderView,
)

from ..core.model import Capabilities, RadarFrame, Target
from ..core.storage import Store
from .ppi import PPIWidget


class TimelineDialog(QDialog):
    """历史会话时间轴拖动回放。"""

    def __init__(self, store: Store, sid: int, caps_dict: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"时间轴轨迹回放 · 会话 #{sid}")
        self.resize(940, 620)
        self.store = store
        self.sid = sid

        st = store.session_stats(sid)
        self._t0 = float(st.get("first_ts") or 0.0)
        self._t1 = float(st.get("last_ts") or self._t0)
        self._dur = max(0.0, self._t1 - self._t0)
        self._playing = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        rmax = float((caps_dict or {}).get("range_max") or 10.0)
        caps = Capabilities(
            name=str((caps_dict or {}).get("name") or f"会话 #{sid}"),
            has_scan=False, has_azimuth=True, has_targets=True,
            range_max=rmax, az_fov=360.0,
        )
        self.ppi = PPIWidget()
        self.ppi.set_capabilities(caps)

        self.events = QTableWidget(0, 3)
        self.events.setHorizontalHeaderLabels(["时间", "级别", "事件"])
        self.events.verticalHeader().setVisible(False)
        self.events.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.events.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, max(1, int(round(self._dur * 10))))
        self.slider.valueChanged.connect(self._on_scrub)

        self.lbl_time = QLabel("0.0s / 0.0s")
        self.btn_play = QPushButton("播放")
        self.btn_play.clicked.connect(self._toggle_play)
        self.cb_speed = QComboBox()
        for sp in ("1x", "2x", "5x"):
            self.cb_speed.addItem(sp, float(sp[:-1]))

        ctrl = QHBoxLayout()
        ctrl.addWidget(self.btn_play)
        ctrl.addWidget(self.cb_speed)
        ctrl.addWidget(self.slider, 1)
        ctrl.addWidget(self.lbl_time)

        body = QHBoxLayout()
        body.addWidget(self.ppi, 3)
        body.addWidget(self.events, 2)

        lay = QVBoxLayout(self)
        lay.addLayout(body, 1)
        lay.addLayout(ctrl)

        self._render_at(0.0)

    def _toggle_play(self) -> None:
        self._playing = not self._playing
        self.btn_play.setText("暂停" if self._playing else "播放")
        if self._playing:
            self._timer.start(100)
        else:
            self._timer.stop()

    def _tick(self) -> None:
        step = max(1, int(round(float(self.cb_speed.currentData() or 1.0))))
        nxt = self.slider.value() + step
        if nxt >= self.slider.maximum():
            nxt = self.slider.maximum()
            self._toggle_play()
        self.slider.setValue(nxt)

    def _on_scrub(self, val: int) -> None:
        self._render_at(val / 10.0)

    def _render_at(self, offset_s: float) -> None:
        if self._t0 <= 0.0:
            self.lbl_time.setText("无航迹记录")
            return
        cur = min(self._t1, self._t0 + offset_s)
        self.lbl_time.setText(
            f"{time.strftime('%H:%M:%S', time.localtime(cur))}  ({offset_s:.1f}s / {self._dur:.1f}s)"
        )
        rows = self.store.tracks_window(self.sid, cur - 0.35, cur + 0.05)
        latest: dict[int, Target] = {}
        for _ts, tid, x, y, vx, vy, _rng, _az, snr, threat, cls in rows:
            latest[int(tid)] = Target(
                x=float(x), y=float(y), vx=float(vx), vy=float(vy),
                snr=float(snr or 0.8), tid=int(tid),
                threat=int(threat or 0), cls=str(cls or ""),
            )
        tracks = list(latest.values())
        self.ppi.push_frame(RadarFrame(timestamp=cur, targets=tracks), tracks)

        evts = self.store.events_until(self.sid, cur, limit=40)
        self.events.setRowCount(len(evts))
        for r, (ts, level, _kind, _rid, msg, _act, _tid) in enumerate(evts):
            self.events.setItem(r, 0, QTableWidgetItem(time.strftime("%H:%M:%S", time.localtime(ts))))
            self.events.setItem(r, 1, QTableWidgetItem(str(level)))
            self.events.setItem(r, 2, QTableWidgetItem(str(msg)))

    def closeEvent(self, ev) -> None:
        self._timer.stop()
        super().closeEvent(ev)
