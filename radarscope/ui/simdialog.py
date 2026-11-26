"""模拟电子站控制台：管理动态虚拟目标场景与教学级电子对抗仿真。"""
from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..core.sim_targets import JAMMING_MODES, SCENARIOS, ElectronicStation, compute_ew_metrics


class SimStationDialog(QDialog):
    """管理动态仿真场景、虚拟目标注入与电子对抗教学仿真。"""

    COLS = ["名称", "X (m)", "Y (m)", "Vx (m/s)", "Vy (m/s)", "强度"]
    changed = pyqtSignal()

    def __init__(self, station: ElectronicStation, range_max: float = 8.0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("模拟电子站 · 动态目标与电子对抗教学仿真")
        self.resize(760, 650)
        self.station = station
        self.range_max = max(2.0, float(range_max))

        tip = QLabel("软件内置电子目标站：可直接叠加到任意雷达（含模拟或实测硬件），用于测试跟踪、防区、CPA 与抗干扰能力。")
        tip.setObjectName("hintLabel")
        tip.setWordWrap(True)

        # ---- 区块 1：动态场景与虚拟目标管理 ----
        scene_box = QGroupBox("动态场景与虚拟目标管理")
        sv = QVBoxLayout(scene_box)
        sv.setContentsMargins(12, 16, 12, 10)
        sv.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
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
        self.sp_speed.valueChanged.connect(self._on_speed)
        top.addWidget(self.sp_speed)

        btn_rand = QPushButton("随机出现")
        btn_rand.clicked.connect(self._spawn_random)
        btn_del = QPushButton("删除选中")
        btn_del.clicked.connect(self._remove_selected)
        top.addWidget(btn_rand)
        top.addWidget(btn_del)
        sv.addLayout(top)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setMinimumHeight(148)
        self.table.setMaximumHeight(200)
        sv.addWidget(self.table)

        # 自定义注入：分两行排布，保证名称输入框与坐标框互不挤压
        row_inj_a = QHBoxLayout()
        row_inj_a.setSpacing(8)
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("自定义目标名称（留空自动编号）")
        btn_add = QPushButton("注入自定义目标")
        btn_add.clicked.connect(self._inject_custom)
        row_inj_a.addWidget(QLabel("目标名称"))
        row_inj_a.addWidget(self.ed_name, 1)
        row_inj_a.addWidget(btn_add)

        row_inj_b = QHBoxLayout()
        row_inj_b.setSpacing(8)
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
        for lbl, sp in (("X(m)", self.sp_x), ("Y(m)", self.sp_y), ("Vx(m/s)", self.sp_vx), ("Vy(m/s)", self.sp_vy)):
            row_inj_b.addWidget(QLabel(lbl))
            row_inj_b.addWidget(sp, 1)
        sv.addLayout(row_inj_a)
        sv.addLayout(row_inj_b)

        # ---- 区块 2：电子对抗 / 无线电干扰教学仿真 ----
        ew_box = QGroupBox("电子对抗 / 无线电干扰教学仿真（纯软件信号数学建模，无射频发射）")
        ew_form = QFormLayout(ew_box)
        ew_form.setContentsMargins(12, 16, 12, 10)
        ew_form.setSpacing(9)

        self.cb_jam = QComboBox()
        for k, label in JAMMING_MODES.items():
            self.cb_jam.addItem(label, k)
        ji = self.cb_jam.findData(self.station.jam_mode)
        if ji >= 0:
            self.cb_jam.setCurrentIndex(ji)
        self.cb_jam.currentIndexChanged.connect(self._on_ew)

        ew_row = QHBoxLayout()
        ew_row.setSpacing(8)
        self.sp_jsr = QDoubleSpinBox()
        self.sp_jsr.setRange(0.1, 1.0)
        self.sp_jsr.setSingleStep(0.1)
        self.sp_jsr.setValue(self.station.jam_jsr)
        self.sp_jsr.valueChanged.connect(self._on_jsr)

        self.sp_jaz = QDoubleSpinBox()
        self.sp_jaz.setRange(-180.0, 180.0)
        self.sp_jaz.setSingleStep(10.0)
        self.sp_jaz.setSuffix("°")
        self.sp_jaz.setValue(self.station.jam_az_deg)
        self.sp_jaz.valueChanged.connect(self._on_jaz)

        self.sp_jbw = QDoubleSpinBox()
        self.sp_jbw.setRange(10.0, 120.0)
        self.sp_jbw.setSingleStep(5.0)
        self.sp_jbw.setSuffix("°")
        self.sp_jbw.setValue(self.station.jam_bw_deg)
        self.sp_jbw.valueChanged.connect(self._on_jbw)

        ew_row.addWidget(QLabel("强度"))
        ew_row.addWidget(self.sp_jsr, 1)
        ew_row.addWidget(QLabel("扇区方位"))
        ew_row.addWidget(self.sp_jaz, 1)
        ew_row.addWidget(QLabel("波束宽"))
        ew_row.addWidget(self.sp_jbw, 1)

        eccm_row = QHBoxLayout()
        eccm_row.setSpacing(14)
        self.chk_eccm = QCheckBox("ECCM 模拟频率捷变抗干扰")
        self.chk_eccm.setChecked(self.station.eccm_agility)
        self.chk_eccm.toggled.connect(self._on_eccm)
        self.chk_fc = QCheckBox("模拟火控拦截前置角引导推演 [教学]")
        self.chk_fc.setChecked(self.station.fc_sim)
        self.chk_fc.toggled.connect(self._on_fc)
        eccm_row.addWidget(self.chk_eccm)
        eccm_row.addWidget(self.chk_fc)
        eccm_row.addStretch(1)

        self.lbl_ew_info = QLabel("")
        self.lbl_ew_info.setObjectName("ewMetricBadge")
        ew_tip = QLabel("提示：切换主窗口「设置 → 检测算法 (CA / OS / GO-CFAR)」与「ECCM 捷变」即可在 PPI/RD 上直观对比抗干扰效果。")
        ew_tip.setObjectName("hintLabel")
        ew_tip.setWordWrap(True)

        ew_form.addRow("干扰样式", self.cb_jam)
        ew_form.addRow("干扰参数", ew_row)
        ew_form.addRow("抗干扰与推演", eccm_row)
        ew_form.addRow("烧穿评估", self.lbl_ew_info)
        ew_form.addRow("", ew_tip)

        # ---- 底部操作栏 ----
        btns = QHBoxLayout()
        btn_clear = QPushButton("清空全部仿真")
        btn_clear.clicked.connect(self._clear)
        btn_close = QPushButton("完成")
        btn_close.setObjectName("primaryBtn")
        btn_close.clicked.connect(self.accept)
        btns.addWidget(btn_clear)
        btns.addStretch(1)
        btns.addWidget(btn_close)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(10)
        lay.addWidget(tip)
        lay.addWidget(scene_box)
        lay.addWidget(ew_box)
        lay.addLayout(btns)
        self._refresh()
        self._update_ew_label()

    def _on_speed(self, v: float) -> None:
        self.station.speed_scale = float(v)
        self.changed.emit()

    def _on_jaz(self, v: float) -> None:
        self.station.jam_az_deg = float(v)
        self.changed.emit()

    def _on_jbw(self, v: float) -> None:
        self.station.jam_bw_deg = float(v)
        self.changed.emit()

    def _on_fc(self, on: bool) -> None:
        self.station.fc_sim = bool(on)
        self.changed.emit()

    def _on_jsr(self, v: float) -> None:
        self.station.jam_jsr = float(v)
        self._update_ew_label()
        self.changed.emit()

    def _on_eccm(self, on: bool) -> None:
        self.station.eccm_agility = bool(on)
        self._update_ew_label()
        self.changed.emit()

    def _update_ew_label(self) -> None:
        if self.station.jam_mode == "none":
            self.lbl_ew_info.setText("未注入干扰（正常信号传播）")
            return
        m = compute_ew_metrics(
            self.station.jam_mode, self.station.jam_jsr, self.range_max,
            self.station.jam_az_deg, self.station.jam_bw_deg, self.station.eccm_agility,
        )
        tag = "（ECCM 捷变已抑制干扰）" if m["eccm_agility"] else "（未开 ECCM）"
        self.lbl_ew_info.setText(
            f"JSR = {m['jsr_db']:+.1f} dB ｜ 理论烧穿距离 = {m['burn_through_m']:.2f} m {tag}"
        )

    def _on_ew(self) -> None:
        self.station.jam_mode = str(self.cb_jam.currentData() or "none")
        self.station._jam_t = 0.0
        self._update_ew_label()
        self.changed.emit()

    def _on_scene(self) -> None:
        sc = str(self.cb_scene.currentData() or "none")
        self.station.set_scenario(sc, self.range_max)
        self._refresh()
        self.changed.emit()

    def _inject_custom(self) -> None:
        self.station.inject(
            self.sp_x.value(), self.sp_y.value(),
            self.sp_vx.value(), self.sp_vy.value(),
            name=self.ed_name.text().strip(), respawn=False,
        )
        self.ed_name.clear()
        self._refresh()
        self.changed.emit()

    def _spawn_random(self) -> None:
        self.station.spawn_random(self.range_max, respawn=True)
        self._refresh()
        self.changed.emit()

    def _remove_selected(self) -> None:
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            if 0 <= r < len(self.station.objects):
                self.station.objects.pop(r)
        self._refresh()
        self.changed.emit()

    def _clear(self) -> None:
        self.station.clear()
        self.cb_scene.blockSignals(True)
        self.cb_scene.setCurrentIndex(0)
        self.cb_scene.blockSignals(False)
        self.cb_jam.blockSignals(True)
        self.cb_jam.setCurrentIndex(0)
        self.cb_jam.blockSignals(False)
        self._update_ew_label()
        self._refresh()
        self.changed.emit()

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
