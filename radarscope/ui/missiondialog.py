"""一体化任务包浏览、参数重算对比与一键回放对话框（ROADMAP 任务闭环）。"""
from __future__ import annotations

import time
from pathlib import Path

from PyQt5.QtCore import QPointF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QDialog,
)

from ..core.mission import list_missions, recompute_mission_comparison, resolve_mission_rad


class _CurveChart(QWidget):
    """零依赖双折线对比图：展示基准 vs 对比参数每帧检出稳定航迹数。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(135)
        self._timeline: list[dict] = []

    def set_data(self, timeline: list[dict]) -> None:
        self._timeline = list(timeline or [])
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(15, 22, 32))
        p.setPen(QPen(QColor(46, 58, 76), 1))
        p.drawRect(0, 0, w - 1, h - 1)
        if not self._timeline:
            p.setPen(QColor(145, 160, 175))
            p.drawText(self.rect(), Qt.AlignCenter, "选择任务包并点击「离线重算对比」，查看两套检测/滤波参数逐帧对比曲线")
            return

        pad_l, pad_r, pad_t, pad_b = 34, 14, 18, 22
        pw = max(10, w - pad_l - pad_r)
        ph = max(10, h - pad_t - pad_b)
        ymax = max(1, max(max(pt["base_tracks"], pt["cmp_tracks"]) for pt in self._timeline))

        for g in (0.25, 0.5, 0.75, 1.0):
            yy = pad_t + ph * (1.0 - g)
            p.setPen(QPen(QColor(38, 48, 62), 1, Qt.DashLine))
            p.drawLine(QPointF(pad_l, yy), QPointF(w - pad_r, yy))

        n = len(self._timeline)
        poly_base = QPolygonF()
        poly_cmp = QPolygonF()
        for i, pt in enumerate(self._timeline):
            xx = pad_l + (i / max(1, n - 1)) * pw
            y0 = pad_t + ph * (1.0 - pt["base_tracks"] / ymax)
            y1 = pad_t + ph * (1.0 - pt["cmp_tracks"] / ymax)
            poly_base.append(QPointF(xx, y0))
            poly_cmp.append(QPointF(xx, y1))

        p.setPen(QPen(QColor(148, 163, 184), 1.5, Qt.DashLine))
        p.drawPolyline(poly_base)
        p.setPen(QPen(QColor(56, 189, 248), 2.0))
        p.drawPolyline(poly_cmp)

        p.setPen(QColor(210, 220, 232))
        p.drawText(QPointF(pad_l, 13), f"帧数: {n}   虚线=基准   亮青=对比方案（最大航迹 {ymax}）")


class MissionCompareDialog(QDialog):
    """选择任务包或 .rad 文件，对比不同 CFAR/杂波/滤波参数效果并支持一键回放。"""

    replayRequested = pyqtSignal(str)

    def __init__(self, missions_root: str | Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("任务包统一回放与参数离线重算对比")
        self.resize(780, 560)
        self.missions_root = Path(missions_root)
        self._selected_path: Path | None = None

        top = QHBoxLayout()
        top.addWidget(QLabel("已录任务包"))
        self.cb_mission = QComboBox()
        self.cb_mission.currentIndexChanged.connect(self._on_mission_changed)
        btn_browse = QPushButton("打开目录 / .rad 文件…")
        btn_browse.clicked.connect(self._browse)
        top.addWidget(self.cb_mission, 1)
        top.addWidget(btn_browse)

        self.lbl_meta = QLabel("")
        self.lbl_meta.setObjectName("hintLabel")
        self.lbl_meta.setWordWrap(True)

        # ---- 对比参数配置 ----
        cfg = QGroupBox("参数对照配置（同一批原始扫描矩阵按两组参数重算）")
        cl = QHBoxLayout(cfg)
        cl.setContentsMargins(10, 14, 10, 8)
        cl.setSpacing(8)
        self.cb_base = QComboBox()
        self.cb_cmp = QComboBox()
        for k, name in (("ca", "CA-CFAR (固定)"), ("auto", "GO 自适应 CFAR"), ("os", "OS-CFAR (抗干扰)"), ("go", "GO-CFAR (边缘抑噪)")):
            self.cb_base.addItem(name, k)
            self.cb_cmp.addItem(name, k)
        self.cb_cmp.setCurrentIndex(1)

        self.sp_thr = QDoubleSpinBox()
        self.sp_thr.setRange(0.05, 0.90)
        self.sp_thr.setSingleStep(0.05)
        self.sp_thr.setValue(0.30)

        self.chk_clutter = QCheckBox("对比方案开启杂波抑制")
        self.chk_clutter.setChecked(True)
        self.cb_filter = QComboBox()
        self.cb_filter.addItem("3D Kalman + M/N(3/5)", ("kalman3d", 3, 5))
        self.cb_filter.addItem("IMM 机动模型 + M/N(3/5)", ("imm3d", 3, 5))
        self.cb_filter.addItem("3D Kalman + 瞬时起批", ("kalman3d", 1, 1))

        btn_run = QPushButton("离线重算对比")
        btn_run.setObjectName("primaryBtn")
        btn_run.clicked.connect(self._run_compare)

        cl.addWidget(QLabel("基准:"))
        cl.addWidget(self.cb_base)
        cl.addWidget(QLabel("对比:"))
        cl.addWidget(self.cb_cmp)
        cl.addWidget(QLabel("门限:"))
        cl.addWidget(self.sp_thr)
        cl.addWidget(self.chk_clutter)
        cl.addWidget(self.cb_filter)
        cl.addWidget(btn_run)

        self.table = QTableWidget(2, 6)
        self.table.setHorizontalHeaderLabels(["方案", "CFAR / 杂波", "滤波 / 起批", "总检出点", "峰值航迹", "独立建轨数 / 均SNR"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setMaximumHeight(98)

        self.chart = _CurveChart(self)

        btns = QHBoxLayout()
        self.btn_replay = QPushButton("载入主窗口实时回放")
        self.btn_replay.clicked.connect(self._emit_replay)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        btns.addWidget(self.btn_replay)
        btns.addStretch(1)
        btns.addWidget(btn_close)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        lay.addLayout(top)
        lay.addWidget(self.lbl_meta)
        lay.addWidget(cfg)
        lay.addWidget(self.table)
        lay.addWidget(self.chart, 1)
        lay.addLayout(btns)

        self._reload_list()

    def _reload_list(self) -> None:
        self.cb_mission.blockSignals(True)
        self.cb_mission.clear()
        items = list_missions(self.missions_root)
        for p, m in items:
            ts = time.strftime("%m-%d %H:%M:%S", time.localtime(m.created_at))
            self.cb_mission.addItem(f"{m.mission_id} · {m.driver} · {m.frames}帧 ({ts})", str(p))
        self.cb_mission.blockSignals(False)
        self._on_mission_changed()

    def _on_mission_changed(self) -> None:
        raw = self.cb_mission.currentData()
        if not raw:
            self._selected_path = None
            self.lbl_meta.setText("暂无已录制任务包，可点击主界面工具栏「任务录制」一键生成，或手动打开任意 .rad 录制文件。")
            return
        self.select_path(Path(raw))

    def select_path(self, p: Path) -> None:
        self._selected_path = p
        try:
            rad, mf = resolve_mission_rad(p)
            if mf:
                snap = mf.settings_snapshot
                self.lbl_meta.setText(
                    f"来源: {mf.caps.get('name') or mf.driver} ({mf.endpoint}) ｜ 帧数: {mf.frames} ｜ "
                    f"采集参数: CFAR={snap.get('cfar_mode', 'auto')}, 杂波={snap.get('clutter_enabled', False)}, 文件={rad.name}"
                )
            else:
                self.lbl_meta.setText(f"已选录制文件: {rad}")
        except Exception as exc:
            self.lbl_meta.setText(f"路径解析异常: {exc}")

    def _browse(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "选择任务包 manifest.json 或 .rad 录制文件",
            str(self.missions_root if self.missions_root.exists() else Path.cwd()),
            "任务包或帧归档 (manifest.json *.rad);;所有文件 (*)",
        )
        if p:
            self.select_path(Path(p))

    def _run_compare(self) -> None:
        if self._selected_path is None:
            QMessageBox.information(self, "提示", "请先选择一个任务包或 .rad 录制文件。")
            return
        f_mode, hits, win = self.cb_filter.currentData() or ("kalman3d", 3, 5)
        try:
            res = recompute_mission_comparison(
                self._selected_path,
                baseline_cfar=str(self.cb_base.currentData() or "ca"),
                compare_cfar=str(self.cb_cmp.currentData() or "auto"),
                threshold=float(self.sp_thr.value()),
                baseline_clutter=False,
                compare_clutter=self.chk_clutter.isChecked(),
                confirm_hits=int(hits),
                confirm_window=int(win),
                filter_mode=str(f_mode),
            )
        except Exception as exc:
            QMessageBox.warning(self, "重算失败", str(exc))
            return

        for row, (label, s) in enumerate((("基准方案", res["baseline"]), ("对比方案", res["compare"]))):
            vals = [
                label,
                f"{s['cfar'].upper()} / {'抑噪开' if s['clutter'] else '关'}",
                f"{s['filter']} ({s['confirm']})",
                f"{s['total_dets']} ({s['avg_dets']}/帧)",
                str(s["max_tracks"]),
                f"{s['unique_confirmed']} 批 / SNR {s['mean_snr']:.2f}",
            ]
            for c, txt in enumerate(vals):
                it = QTableWidgetItem(txt)
                it.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, c, it)
        self.chart.set_data(res["timeline"])

    def _emit_replay(self) -> None:
        if self._selected_path is None:
            return
        try:
            rad, _ = resolve_mission_rad(self._selected_path)
        except Exception as exc:
            QMessageBox.warning(self, "无法回放", str(exc))
            return
        self.replayRequested.emit(str(rad))
        self.accept()
