"""模拟电子站控制台组件与对话框：既可独立悬浮，也可停靠在右侧工作区。"""
from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
    QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.i18n import tr
from ..core.sim_targets import JAMMING_MODES, SCENARIOS, ElectronicStation, compute_ew_metrics


class SimStationWidget(QWidget):
    """可嵌入 Dock 或对话框的动态目标 / 电子对抗实时控制面板。"""

    COLS = ["名称", "X (m)", "Y (m)", "Vx (m/s)", "Vy (m/s)", "强度"]
    changed = pyqtSignal()
    scenarioSelected = pyqtSignal(str)
    doneRequested = pyqtSignal()

    def __init__(self, station: ElectronicStation, range_max: float = 8.0,
                 show_done: bool = False, parent=None):
        super().__init__(parent)
        self.station = station
        self.range_max = max(2.0, float(range_max))

        tip = QLabel("支持在 PPI 上按住 Shift + 左键直接点放目标；调整下方场景或电子战参数实时生效。")
        tip.setObjectName("hintLabel")
        tip.setWordWrap(True)

        # ---- 区块 1：动态场景与虚拟目标管理 ----
        scene_box = QGroupBox("动态场景与虚拟目标管理")
        sv = QVBoxLayout(scene_box)
        sv.setContentsMargins(10, 14, 10, 8)
        sv.setSpacing(7)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.lbl_scene = QLabel(tr("自动场景"))
        top.addWidget(self.lbl_scene)
        self.cb_scene = QComboBox()
        for k, label in SCENARIOS.items():
            self.cb_scene.addItem(label, k)
        si = self.cb_scene.findData(self.station.scenario)
        if si >= 0:
            self.cb_scene.setCurrentIndex(si)
        self.cb_scene.currentIndexChanged.connect(self._on_scene)
        top.addWidget(self.cb_scene, 1)

        self.lbl_speed = QLabel(tr("速度倍率"))
        top.addWidget(self.lbl_speed)
        self.sp_speed = QDoubleSpinBox()
        self.sp_speed.setRange(0.2, 5.0)
        self.sp_speed.setSingleStep(0.2)
        self.sp_speed.setValue(self.station.speed_scale)
        self.sp_speed.valueChanged.connect(self._on_speed)
        top.addWidget(self.sp_speed)

        self.btn_rand = QPushButton(tr("随机出现"))
        self.btn_rand.clicked.connect(self._spawn_random)
        self.btn_del = QPushButton(tr("删除选中"))
        self.btn_del.clicked.connect(self._remove_selected)
        top.addWidget(self.btn_rand)
        top.addWidget(self.btn_del)
        sv.addLayout(top)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setMinimumHeight(130)
        self.table.setMaximumHeight(190)
        sv.addWidget(self.table)

        row_inj_a = QHBoxLayout()
        row_inj_a.setSpacing(6)
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("自定义目标名称（留空自动编号）")
        self.btn_add = QPushButton(tr("注入自定义目标"))
        self.btn_add.clicked.connect(self._inject_custom)
        row_inj_a.addWidget(QLabel("名称"))
        row_inj_a.addWidget(self.ed_name, 1)
        row_inj_a.addWidget(self.btn_add)

        row_inj_b = QHBoxLayout()
        row_inj_b.setSpacing(6)
        self.sp_x = QDoubleSpinBox()
        self.sp_x.setRange(-5000.0, 5000.0)
        self.sp_x.setValue(0.8)
        self.sp_y = QDoubleSpinBox()
        self.sp_y.setRange(-5000.0, 5000.0)
        self.sp_y.setValue(min(3.5, self.range_max * 0.6))
        self.sp_vx = QDoubleSpinBox()
        self.sp_vx.setRange(-100.0, 100.0)
        self.sp_vx.setValue(-0.25)
        self.sp_vy = QDoubleSpinBox()
        self.sp_vy.setRange(-100.0, 100.0)
        self.sp_vy.setValue(-0.20)
        for lbl, sp in (("X", self.sp_x), ("Y", self.sp_y), ("Vx", self.sp_vx), ("Vy", self.sp_vy)):
            row_inj_b.addWidget(QLabel(lbl))
            row_inj_b.addWidget(sp, 1)
        sv.addLayout(row_inj_a)
        sv.addLayout(row_inj_b)

        # ---- 区块 2：电子对抗 / 无线电干扰教学仿真 ----
        ew_box = QGroupBox("电子对抗 / 无线电干扰教学仿真（纯数学建模，无射频发射）")
        ew_form = QFormLayout(ew_box)
        ew_form.setContentsMargins(10, 14, 10, 8)
        ew_form.setSpacing(8)

        self.cb_jam = QComboBox()
        for k, label in JAMMING_MODES.items():
            self.cb_jam.addItem(label, k)
        ji = self.cb_jam.findData(self.station.jam_mode)
        if ji >= 0:
            self.cb_jam.setCurrentIndex(ji)
        self.cb_jam.currentIndexChanged.connect(self._on_ew)

        ew_row = QHBoxLayout()
        ew_row.setSpacing(6)
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
        ew_row.addWidget(QLabel("方位"))
        ew_row.addWidget(self.sp_jaz, 1)
        ew_row.addWidget(QLabel("波宽"))
        ew_row.addWidget(self.sp_jbw, 1)

        eccm_row = QHBoxLayout()
        eccm_row.setSpacing(10)
        self.chk_eccm = QCheckBox("ECCM 频率捷变抗干扰")
        self.chk_eccm.setChecked(self.station.eccm_agility)
        self.chk_eccm.toggled.connect(self._on_eccm)
        self.chk_fc = QCheckBox("模拟火控前置引导 [教学]")
        self.chk_fc.setChecked(self.station.fc_sim)
        self.chk_fc.toggled.connect(self._on_fc)
        eccm_row.addWidget(self.chk_eccm)
        eccm_row.addWidget(self.chk_fc)
        eccm_row.addStretch(1)

        self.lbl_ew_info = QLabel("")
        self.lbl_ew_info.setObjectName("ewMetricBadge")
        ew_tip = QLabel("切换检测算法 (CA/OS/GO-CFAR) 与 ECCM 捷变即可在 PPI/RD 上实时对比烧穿曲线与抗干扰效果。")
        ew_tip.setObjectName("hintLabel")
        ew_tip.setWordWrap(True)

        ew_form.addRow(tr("干扰样式"), self.cb_jam)
        ew_form.addRow("干扰参数", ew_row)
        ew_form.addRow("抗干扰", eccm_row)
        ew_form.addRow("烧穿评估", self.lbl_ew_info)
        ew_form.addRow("", ew_tip)

        btns = QHBoxLayout()
        self.btn_clear = QPushButton(tr("清空全部仿真"))
        self.btn_clear.clicked.connect(self._clear)
        btns.addWidget(self.btn_clear)
        btns.addStretch(1)
        self.btn_done = None
        if show_done:
            self.btn_done = QPushButton(tr("完成"))
            self.btn_done.setObjectName("primaryBtn")
            self.btn_done.clicked.connect(self.doneRequested.emit)
            btns.addWidget(self.btn_done)

        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(8)
        lay.addWidget(tip)
        lay.addWidget(scene_box)
        lay.addWidget(ew_box)
        lay.addLayout(btns)
        lay.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self._refresh()
        self._update_ew_label()

    def sync_from_station(self, range_max: float | None = None) -> None:
        if range_max is not None:
            self.range_max = max(2.0, float(range_max))
        for cb, val in ((self.cb_scene, self.station.scenario), (self.cb_jam, self.station.jam_mode)):
            cb.blockSignals(True)
            idx = cb.findData(val)
            if idx >= 0:
                cb.setCurrentIndex(idx)
            cb.blockSignals(False)
        for sp, val in (
            (self.sp_speed, self.station.speed_scale),
            (self.sp_jsr, self.station.jam_jsr),
            (self.sp_jaz, self.station.jam_az_deg),
            (self.sp_jbw, self.station.jam_bw_deg),
        ):
            sp.blockSignals(True)
            sp.setValue(val)
            sp.blockSignals(False)
        for chk, val in ((self.chk_eccm, self.station.eccm_agility), (self.chk_fc, self.station.fc_sim)):
            chk.blockSignals(True)
            chk.setChecked(val)
            chk.blockSignals(False)
        self._refresh()
        self._update_ew_label()

    def retranslate(self) -> None:
        self.lbl_scene.setText(tr("自动场景"))
        self.lbl_speed.setText(tr("速度倍率"))
        self.btn_rand.setText(tr("随机出现"))
        self.btn_del.setText(tr("删除选中"))
        self.btn_add.setText(tr("注入自定义目标"))
        self.btn_clear.setText(tr("清空全部仿真"))
        if self.btn_done is not None:
            self.btn_done.setText(tr("完成"))

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
        self.scenarioSelected.emit(sc)
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
        self.sync_from_station()
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


class SimStationDialog(QDialog):
    """兼容原有弹窗入口的非模态电子站对话框，内部直接托管 SimStationWidget。"""

    COLS = SimStationWidget.COLS
    changed = pyqtSignal()
    scenarioSelected = pyqtSignal(str)

    def __init__(self, station: ElectronicStation, range_max: float = 8.0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("模拟电子站 · 动态目标与电子对抗教学仿真")
        self.resize(760, 650)
        self.panel = SimStationWidget(station, range_max, show_done=True, parent=self)
        self.panel.changed.connect(self.changed)
        self.panel.scenarioSelected.connect(self.scenarioSelected)
        self.panel.doneRequested.connect(self.accept)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.addWidget(self.panel)

    @property
    def station(self) -> ElectronicStation:
        return self.panel.station

    @property
    def range_max(self) -> float:
        return self.panel.range_max

    @range_max.setter
    def range_max(self, val: float) -> None:
        self.panel.range_max = max(2.0, float(val))

    def __getattr__(self, name: str):
        return getattr(self.panel, name)
