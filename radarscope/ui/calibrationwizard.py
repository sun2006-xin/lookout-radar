"""标定向导：把"算偏置"这件手工活变成点几下鼠标。

流程（每个点只需要人走过去站好）：
    1. 在地面用卷尺量出几个参考点，把坐标填进表里；
    2. 人站到点上，点「采集」——程序在 2 秒内采几十帧，
       每台雷达取"最强目标的方位角"做圆均值，得到一个观测；
    3. 所有点采完，点「计算」——反推每台的安装偏置，并给出残差；
    4. 点「应用」——偏置写回 sensors.json，重连后生效。

为什么必须人站在已知点上：
    雷达只输出"目标相对我视轴偏了多少度"，这个量本身不含任何绝对信息。
    没有已知参考，安装角度和"世界长什么样"是耦合的，解不出来 ——
    这是标定问题，不是算法不够聪明。
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
)

from ..core.calibration import (
    CalibPoint, calibrate_bearings, apply_offsets, circular_mean_deg,
    norm_deg, residual_report,
)
from ..core.fusion import SensorSpec

DEFAULT_POINTS = [(0.0, 2.0), (1.5, 3.0), (-1.5, 3.0)]


class CalibrationWizard(QDialog):
    def __init__(self, specs: list[SensorSpec], sample_fn, save_path: Path,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("标定向导 · 反推安装朝向")
        self.resize(880, 620)
        self.sample_fn = sample_fn          # () -> dict[传感器名, list[Target]]
        self.save_path = Path(save_path)
        self.specs = [SensorSpec.from_dict(s.to_dict()) for s in specs]
        self.points: list[CalibPoint] = [CalibPoint(x, y) for x, y in DEFAULT_POINTS]

        self._collecting = False
        self._buf: dict[str, list[float]] = {}
        self._need = 0
        self._got = 0
        self._cur_row = -1
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

        # ---- 上：点位表 ----
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["X (m)", "Y (m)", "已采传感器", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)

        row_bar = QHBoxLayout()
        btn_add = QPushButton("添加点")
        btn_add.clicked.connect(self._add_point)
        btn_del = QPushButton("删除点")
        btn_del.clicked.connect(self._del_point)
        for b in (btn_add, btn_del):
            row_bar.addWidget(b)
        row_bar.addStretch(1)

        # ---- 中：采集控制 ----
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.btn_collect = QPushButton("采集当前点（请先站到该点）")
        self.btn_collect.clicked.connect(self._collect_current)
        self.btn_collect_all = QPushButton("全部自动采集")
        self.btn_collect_all.clicked.connect(self._collect_all)

        # ---- 下：结果 ----
        self.out = QTextEdit()
        self.out.setReadOnly(True)
        self.out.setStyleSheet(
            "QTextEdit{background:#0A0D0B;color:#9FB2A6;border:1px solid #1F2A22;"
            "font-family:Consolas,'Microsoft YaHei';font-size:12px;}")

        self.btn_calc = QPushButton("计算偏置")
        self.btn_calc.clicked.connect(self._compute)
        self.btn_apply = QPushButton("应用并保存")
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self._apply)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        for b in (self.btn_calc, self.btn_apply, btn_close):
            bottom.addWidget(b)

        tip = QLabel(
            "步骤：① 用卷尺量出表中各点的真实坐标（以雷达组中心为原点，正前方为 +Y）"
            "　② 人站到点上　③ 点「采集当前点」　④ 全部采完后「计算偏置」→「应用并保存」")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8FA396;font-size:12px;")

        lay = QVBoxLayout(self)
        lay.addWidget(tip)
        lay.addWidget(QLabel("标定点（单位：米，+Y 为正前方）"))
        lay.addWidget(self.table, 1)
        lay.addLayout(row_bar)
        lay.addWidget(self.progress)
        lay.addLayout(self._hrow(self.btn_collect, self.btn_collect_all))
        lay.addWidget(QLabel("计算与验收"))
        lay.addWidget(self.out, 1)
        lay.addLayout(bottom)

        self._offsets: dict[str, float] = {}
        self._fixed: list[SensorSpec] = []
        self._fill_table()

    @staticmethod
    def _hrow(*widgets):
        h = QHBoxLayout()
        for w in widgets:
            h.addWidget(w)
        return h

    # ------------------------------------------------------------------ #
    def _fill_table(self) -> None:
        self.table.setRowCount(len(self.points))
        for r, p in enumerate(self.points):
            for c, v in enumerate([f"{p.x:.2f}", f"{p.y:.2f}",
                                   str(len(p.obs)), "已采" if p.obs else "未采"]):
                it = QTableWidgetItem(v)
                it.setTextAlignment(Qt.AlignCenter)
                if c == 3:
                    it.setForeground(QColor(110, 235, 165) if p.obs
                                     else QColor(150, 160, 150))
                self.table.setItem(r, c, it)

    def _add_point(self) -> None:
        self.points.append(CalibPoint(0.0, 1.0))
        self._fill_table()

    def _del_point(self) -> None:
        r = self.table.currentRow()
        if 0 <= r < len(self.points):
            self.points.pop(r)
            self._fill_table()

    # ------------------------------------------------------------------ #
    def _start(self, row: int) -> None:
        if self.sample_fn is None:
            QMessageBox.warning(self, "没有数据", "当前没有可用的融合雷达组，先连接多站融合数据源")
            return
        self._cur_row = row
        self._buf = {}
        self._got = 0
        self._need = int(2.0 / (self._timer.interval() / 1000.0))   # 约 2 秒
        self.progress.setValue(0)
        self._collecting = True
        self.btn_collect.setEnabled(False)
        self.btn_collect_all.setEnabled(False)
        self._timer.start()

    def _collect_current(self) -> None:
        r = self.table.currentRow()
        if r < 0:
            r = 0 if self.points else -1
        if r < 0:
            return
        self._start(r)

    def _collect_all(self) -> None:
        self._auto_queue = [i for i, p in enumerate(self.points) if not p.obs]
        if not self._auto_queue:
            QMessageBox.information(self, "全部自动采集",
                                    "所有点都已采集过了。要重采请先删除点再添加。")
            return
        self._start(self._auto_queue.pop(0))

    def _tick(self) -> None:
        if not self._collecting:
            return
        try:
            obs = self.sample_fn() or {}
        except Exception:
            obs = {}
        for name, targets in obs.items():
            # 同框多目标时取最强的那个：标定时人通常就是最近最强反射
            best = max(targets, key=lambda t: t.snr)
            self._buf.setdefault(name, []).append(math.degrees(best.az))
        self._got += 1
        self.progress.setValue(int(1000 * min(1.0, self._got / max(1, self._need))))
        if self._got < self._need:
            return

        self._timer.stop()
        self._collecting = False
        self.btn_collect.setEnabled(True)
        self.btn_collect_all.setEnabled(True)

        p = self.points[self._cur_row]
        for name, angles in self._buf.items():
            if len(angles) < max(3, self._need // 4):
                continue                      # 采到的帧太少，这台这次不算
            p.obs[name] = circular_mean_deg(angles)
        self._fill_table()
        self.out.append(f"采集完成：点 ({p.x:.2f}, {p.y:.2f})，"
                        f"参与传感器 {len(p.obs)} 台\n")

        if getattr(self, "_auto_queue", None):
            self._start(self._auto_queue.pop(0))

    # ------------------------------------------------------------------ #
    def _compute(self) -> None:
        usable = [p for p in self.points if len(p.obs) >= 1]
        if not usable:
            QMessageBox.warning(self, "没有数据", "至少要先成功采集一个点")
            return
        off = calibrate_bearings(self.specs, usable)
        fixed = apply_offsets(self.specs, off)
        rep = residual_report(fixed, usable)

        lines = ["=== 安装偏置 ==="]
        for s in self.specs:
            d = off.get(s.name)
            if d is None:
                lines.append(f"  {s.name:>4}  （无观测，跳过）")
            else:
                lines.append(f"  {s.name:>4}  标称 {s.bearing_deg:7.2f}°  "
                             f"偏置 {d:+7.2f}°  ->  校正后 "
                             f"{norm_deg(s.bearing_deg + d):7.2f}°")
        lines.append("")
        lines.append("=== 验收（用已知点反算位置，与真值比） ===")
        for row in rep["rows"]:
            if row["err"] is None:
                lines.append(f"  ({row['x']:+.2f}, {row['y']:+.2f})  无法交会")
            else:
                ex, ey = row["est"]
                lines.append(f"  ({row['x']:+.2f}, {row['y']:+.2f})  ->  "
                             f"({ex:+.2f}, {ey:+.2f})   误差 {row['err'] * 100:.1f} cm")
        lines.append("")
        lines.append(f"RMS = {rep['rms'] * 100:.1f} cm    最大 = {rep['max'] * 100:.1f} cm"
                     f"    ({rep['n']} 个可交会点)")
        if rep["rms"] > 0.5:
            lines.append("提示：残差偏大。检查 ①点的坐标是不是量错了 "
                         "②点是否落在两台雷达的共视区 ③人是否站偏了")
        self.out.setPlainText("\n".join(lines))
        self._offsets, self._fixed = off, fixed
        self.btn_apply.setEnabled(bool(off))

    def _apply(self) -> None:
        if not self._offsets:
            return
        try:
            data = {"sensors": [s.to_dict() for s in self._fixed]}
            self.save_path.parent.mkdir(parents=True, exist_ok=True)
            self.save_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        QMessageBox.information(
            self, "已保存",
            f"偏置已写入：\n{self.save_path}\n\n"
            "重新连接「多站融合」后生效。")
        self.accept()

    def closeEvent(self, ev):
        self._timer.stop()
        super().closeEvent(ev)
