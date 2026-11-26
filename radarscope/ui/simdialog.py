"""模拟电子站控制台：不用硬件也能向雷达画面动态注入虚拟移动目标与场景。"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from ..core.sim_targets import SCENARIOS, ElectronicStation


class SimStationDialog(QDialog):
    """管理动态仿真场景与手动注入物体（X/Y/Vx/Vy/强度）。"""

    COLS = ["名称", "X (m)", "Y (m)", "Vx (m/s)", "Vy (m/s)", "强度"]

    def __init__(self, station: ElectronicStation, range_max: float = 8.0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("模拟电子站 · 动态目标仿真与注入")
        self.resize(680, 480)
        self.station = station
        self.range_max = max(2.0, float(range_max))

        tip = QLabel(
            "软件内置电子目标站：可直接叠加到当前任意雷达（含模拟/实测硬件），\n"
            "用来测试多目标跟踪、CPA 防撞、告警防区触发与 UDP 转发。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#6B7280;font-size:12px;")

        top = QHBoxLayout()
        top.addWidget(QLabel("自动场景"))
        self.cb_scene = QComboBox()
        for k, label in SCENARIOS.items():
            self.cb_scene.addItem(label, k)
        si = self.cb_scene.findData(self.station.scenario)
        if si >= 0:
            self.cb_scene.setCurrentIndex(si)
        self.cb_scene.currentIndexChanged.connect(self._on_scene)
        top.addWidget(self.cb_scene, 1)

        top.addWidget(QLabel("速度倍率"))
        self.sp_speed = QDoubleSpinBox()
        self.sp_speed.setRange(0.2, 5.0)
        self.sp_speed.setSingleStep(0.2)
        self.sp_speed.setValue(self.station.speed_scale)
        self.sp_speed.valueChanged.connect(lambda v: setattr(self.station, "speed_scale", float(v)))
        top.addWidget(self.sp_speed)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        # 快速手动注入区
        inj = QFormLayout()
        row_xy = QHBoxLayout()
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("目标名（留空自动编号）")
        self.sp_x = QDoubleSpinBox()
        self.sp_x.setRange(-1000.0, 1000.0)
        self.sp_x.setValue(0.8)
        self.sp_y = QDoubleSpinBox()
        self.sp_y.setRange(0.2, 1000.0)
        self.sp_y.setValue(min(3.5, self.range_max * 0.6))
        self.sp_vx = QDoubleSpinBox()
        self.sp_vx.setRange(-50.0, 50.0)
        self.sp_vx.setValue(-0.25)
        self.sp_vy = QDoubleSpinBox()
        self.sp_vy.setRange(-50.0, 50.0)
        self.sp_vy.setValue(-0.20)
        row_xy.addWidget(self.ed_name, 2)
        row_xy.addWidget(QLabel("X"))
        row_xy.addWidget(self.sp_x)
        row_xy.addWidget(QLabel("Y"))
        row_xy.addWidget(self.sp_y)
        row_xy.addWidget(QLabel("Vx"))
        row_xy.addWidget(self.sp_vx)
        row_xy.addWidget(QLabel("Vy"))
        row_xy.addWidget(self.sp_vy)
        inj.addRow("自定义注入", row_xy)

        btns = QHBoxLayout()
        btn_add = QPushButton("注入自定义目标")
        btn_add.clicked.connect(self._inject_custom)
        btn_rand = QPushButton("随机出现一个移动物体")
        btn_rand.clicked.connect(self._spawn_random)
        btn_clear = QPushButton("清空全部")
        btn_clear.clicked.connect(self._clear)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        for b in (btn_add, btn_rand, btn_clear):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(btn_close)

        lay = QVBoxLayout(self)
        lay.addWidget(tip)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        lay.addLayout(inj)
        lay.addLayout(btns)
        self._refresh()

    def _on_scene(self) -> None:
        sc = str(self.cb_scene.currentData() or "none")
        self.station.set_scenario(sc, self.range_max)
        self._refresh()

    def _inject_custom(self) -> None:
        self.station.inject(
            self.sp_x.value(), self.sp_y.value(),
            self.sp_vx.value(), self.sp_vy.value(),
            name=self.ed_name.text().strip(), respawn=False,
        )
        self._refresh()

    def _spawn_random(self) -> None:
        self.station.spawn_random(self.range_max, respawn=True)
        self._refresh()

    def _clear(self) -> None:
        self.station.clear()
        self.cb_scene.blockSignals(True)
        self.cb_scene.setCurrentIndex(0)
        self.cb_scene.blockSignals(False)
        self._refresh()

    def _refresh(self) -> None:
        objs = self.station.objects
        self.table.setRowCount(len(objs))
        for r, o in enumerate(objs):
            vals = [o.name, f"{o.x:.2f}", f"{o.y:.2f}", f"{o.vx:.2f}", f"{o.vy:.2f}", f"{o.amp:.2f}"]
            for c, txt in enumerate(vals):
                it = QTableWidgetItem(txt)
                if c > 0:
                    it.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, c, it)
