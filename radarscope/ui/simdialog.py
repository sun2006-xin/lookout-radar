"""模拟电子站控制台：不用硬件也能向雷达画面动态注入虚拟移动目标与场景。"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..core.sim_targets import JAMMING_MODES, SCENARIOS, ElectronicStation, compute_ew_metrics


class SimStationDialog(QDialog):
    """管理动态仿真场景与手动注入物体（X/Y/Vx/Vy/强度）。"""

    COLS = ["名称", "X (m)", "Y (m)", "Vx (m/s)", "Vy (m/s)", "强度"]

    def __init__(self, station: ElectronicStation, range_max: float = 8.0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("模拟电子站 · 动态目标与电子对抗教学仿真")
        self.resize(700, 560)
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

        ew_box = QGroupBox("电子对抗 / 无线电干扰教学仿真（纯软件信号数学建模，无射频发射）")
        ew_form = QFormLayout(ew_box)
        self.cb_jam = QComboBox()
        for k, label in JAMMING_MODES.items():
            self.cb_jam.addItem(label, k)
        ji = self.cb_jam.findData(self.station.jam_mode)
        if ji >= 0:
            self.cb_jam.setCurrentIndex(ji)
        self.cb_jam.currentIndexChanged.connect(self._on_ew)

        ew_row = QHBoxLayout()
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
        self.sp_jaz.valueChanged.connect(lambda v: setattr(self.station, "jam_az_deg", float(v)))

        self.sp_jbw = QDoubleSpinBox()
        self.sp_jbw.setRange(10.0, 120.0)
        self.sp_jbw.setSingleStep(5.0)
        self.sp_jbw.setSuffix("°")
        self.sp_jbw.setValue(self.station.jam_bw_deg)
        self.sp_jbw.valueChanged.connect(lambda v: setattr(self.station, "jam_bw_deg", float(v)))

        ew_row.addWidget(QLabel("干扰强度"))
        ew_row.addWidget(self.sp_jsr)
        ew_row.addWidget(QLabel("扇区方位"))
        ew_row.addWidget(self.sp_jaz)
        ew_row.addWidget(QLabel("波束宽"))
        ew_row.addWidget(self.sp_jbw)

        eccm_row = QHBoxLayout()
        self.chk_eccm = QCheckBox("ECCM 模拟频率捷变抗干扰")
        self.chk_eccm.setChecked(self.station.eccm_agility)
        self.chk_eccm.toggled.connect(self._on_eccm)
        self.chk_fc = QCheckBox("模拟火控拦截前置角引导推演 [教学]")
        self.chk_fc.setChecked(self.station.fc_sim)
        self.chk_fc.toggled.connect(lambda on: setattr(self.station, "fc_sim", bool(on)))
        eccm_row.addWidget(self.chk_eccm)
        eccm_row.addWidget(self.chk_fc)
        eccm_row.addStretch(1)

        self.lbl_ew_info = QLabel("")
        self.lbl_ew_info.setStyleSheet("color:#0F766E;font-size:12px;")

        ew_form.addRow("干扰样式", self.cb_jam)
        ew_form.addRow("干扰参数", ew_row)
        ew_form.addRow("抗干扰与推演", eccm_row)
        ew_form.addRow("烧穿评估", self.lbl_ew_info)
        ew_tip = QLabel(
            "教学用法：开启干扰后，切换主窗口「设置 → 检测算法 (CA / OS / GO-CFAR)」与「ECCM 捷变抗干扰」，\n"
            "可实时观察 PPI 上的烧穿距离圈、ESM 干扰方位弧段与 RD 多普勒 VGPO 拖引效果。"
        )
        ew_tip.setWordWrap(True)
        ew_tip.setStyleSheet("color:#6B7280;font-size:11px;")
        ew_form.addRow(ew_tip)
        lay.addWidget(ew_box)
        lay.addLayout(btns)
        self._refresh()
        self._update_ew_label()

    def _on_jsr(self, v: float) -> None:
        self.station.jam_jsr = float(v)
        self._update_ew_label()

    def _on_eccm(self, on: bool) -> None:
        self.station.eccm_agility = bool(on)
        self._update_ew_label()

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
        self.cb_jam.blockSignals(True)
        self.cb_jam.setCurrentIndex(0)
        self.cb_jam.blockSignals(False)
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
