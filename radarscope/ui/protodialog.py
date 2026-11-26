"""零代码通用雷达协议配置向导：无需写 Python，直接配置并预览文本 / NMEA / 二进制雷达帧。"""
from __future__ import annotations

from PyQt5.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QVBoxLayout,
)

from .. import paths
from ..core.custom_proto import (
    CustomProtocolSpec, load_custom_specs, preview_sample, save_custom_specs,
)
from ..drivers.custom_generic import reload_custom_drivers

_SAMPLES = {
    "nmea_ttm": "$RATTM,01,1.25,045.0,T,12.5,210.0,T,0.3,4.2,N,VESSEL_A,T,,*46",
    "csv_xy": "$TGT,1.8,12.5,-0.5,-1.2,0.88",
    "csv_polar": "$SCAN,4.25,042.0,0.86",
    "json_lines": '{"x":2.4,"y":8.6,"vx":0.1,"vy":-0.4,"snr":0.88}',
    "binary_fixed": "AA 55 96 00 C8 01 58 00 0D 0A",
    "binary_point": "59 59 5E 01 20 03 00 00 A0",
    "binary_polar": "AA 56 2C 01 E2 FF 55 00 0D 0A",
}

_PRESET_SAMPLES = {
    "polar_scan_ascii": "$SCAN,4.25,042.0,0.86",
    "tf_point_bin": "59 59 5E 01 20 03 00 00 A0",
    "can_udp_77g": "AB CD 00 00 00 40 00 00 20 41 00 00 80 BF 00 00 00 40 0D 0A",
    "modbus_u16_dist": "01 03 02 0D AC B8 1D",
    "json_radar_stream": '{"x":2.4,"y":8.6,"vx":0.1,"vy":-0.4,"snr":0.88}',
    "polar_bin_radar": "AA 56 2C 01 E2 FF 55 00 0D 0A",
}


class ProtoWizardDialog(QDialog):
    """配置通用声明式雷达驱动并支持粘贴样本报文实时测试解析。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("通用协议雷达适配向导（零代码接入）")
        self.resize(680, 620)
        self._path = paths.data_dir() / "custom_drivers.json"
        self._specs: list[CustomProtocolSpec] = load_custom_specs(self._path)

        top = QHBoxLayout()
        top.addWidget(QLabel("选择模板/已配驱动"))
        self.cb_spec = QComboBox()
        for s in self._specs:
            self.cb_spec.addItem(f"{s.label} ({s.id})", s.id)
        self.cb_spec.currentIndexChanged.connect(self._load_selected)
        top.addWidget(self.cb_spec, 1)
        btn_new = QPushButton("新建协议")
        btn_new.clicked.connect(self._new_spec)
        top.addWidget(btn_new)

        form = QFormLayout()
        self.ed_id = QLineEdit()
        self.ed_label = QLineEdit()
        self.cb_mode = QComboBox()
        for k, v in (
            ("nmea_ttm", "航海 NMEA TTM ($RATTM 目标报文)"),
            ("csv_xy", "文本行 · 笛卡尔坐标 (X, Y, Vx, Vy)"),
            ("csv_polar", "文本行 · 极坐标 (距离, 方位°)"),
            ("json_lines", "网络/串口 · JSON 目标流 ({x,y} 或 {targets:[...]})"),
            ("binary_fixed", "二进制 · 坐标结构体 (X, Y / Vx, Vy)"),
            ("binary_polar", "二进制 · 极坐标结构体 (距离, 方位, 强度)"),
            ("binary_point", "二进制 · 单点测距 (距离, 强度)"),
        ):
            self.cb_mode.addItem(v, k)
        self.cb_form = QComboBox()
        for k, v in (
            ("omni", "全向 360°"),
            ("sector", "受限扇区"),
            ("point", "单点测距"),
            ("tracking", "直接输出目标"),
        ):
            self.cb_form.addItem(v, k)

        self.sp_rmax = QDoubleSpinBox()
        self.sp_rmax.setRange(1.0, 50000.0)
        self.sp_rmax.setValue(80.0)
        self.sp_fov = QDoubleSpinBox()
        self.sp_fov.setRange(5.0, 360.0)
        self.sp_fov.setValue(180.0)

        self.ed_prefix = QLineEdit()
        self.ed_delim = QLineEdit(",")
        self.ed_hdr = QLineEdit("AA 55")
        self.ed_tail = QLineEdit("0D 0A")
        self.sp_flen = QSpinBox()
        self.sp_flen.setRange(0, 512)
        self.sp_offset = QSpinBox()
        self.sp_offset.setRange(0, 256)
        self.ed_fmt = QLineEdit("hhh")
        self.sp_scale = QDoubleSpinBox()
        self.sp_scale.setDecimals(5)
        self.sp_scale.setRange(0.00001, 1000.0)
        self.sp_scale.setValue(1.0)
        self.sp_angle_scale = QDoubleSpinBox()
        self.sp_angle_scale.setDecimals(4)
        self.sp_angle_scale.setRange(0.0001, 100.0)
        self.sp_angle_scale.setValue(1.0)
        self.cb_baud = QComboBox()
        self.cb_baud.setEditable(True)
        for b in ("115200", "256000", "230400", "460800", "921600", "57600", "38400", "9600"):
            self.cb_baud.addItem(b, int(b))

        form.addRow("驱动 ID（英文唯一）", self.ed_id)
        form.addRow("界面显示名称", self.ed_label)
        form.addRow("解析格式", self.cb_mode)
        form.addRow("测量形态", self.cb_form)

        row_rng = QHBoxLayout()
        row_rng.addWidget(QLabel("最大量程(m)"))
        row_rng.addWidget(self.sp_rmax)
        row_rng.addWidget(QLabel("视场角(°)"))
        row_rng.addWidget(self.sp_fov)
        row_rng.addWidget(QLabel("默认波特率"))
        row_rng.addWidget(self.cb_baud)
        form.addRow("量程与串口", row_rng)

        row_txt = QHBoxLayout()
        row_txt.addWidget(QLabel("行前缀"))
        row_txt.addWidget(self.ed_prefix)
        row_txt.addWidget(QLabel("分隔符"))
        row_txt.addWidget(self.ed_delim)
        row_txt.addWidget(QLabel("距缩放"))
        row_txt.addWidget(self.sp_scale)
        row_txt.addWidget(QLabel("角缩放"))
        row_txt.addWidget(self.sp_angle_scale)
        form.addRow("文本/通用缩放", row_txt)

        row_bin = QHBoxLayout()
        row_bin.addWidget(QLabel("帧头Hex"))
        row_bin.addWidget(self.ed_hdr)
        row_bin.addWidget(QLabel("帧尾Hex"))
        row_bin.addWidget(self.ed_tail)
        row_bin.addWidget(QLabel("总字节"))
        row_bin.addWidget(self.sp_flen)
        row_bin.addWidget(QLabel("偏移"))
        row_bin.addWidget(self.sp_offset)
        row_bin.addWidget(QLabel("struct"))
        row_bin.addWidget(self.ed_fmt)
        form.addRow("二进制帧参数", row_bin)

        box = QGroupBox("粘贴样本报文在线试解析（不接硬件即可验证格式）")
        bv = QVBoxLayout(box)
        self.ed_sample = QPlainTextEdit()
        self.ed_sample.setMaximumHeight(90)
        self.lbl_preview = QLabel("点击下方「试解析样本」查看解析结果")
        self.lbl_preview.setWordWrap(True)
        btn_test = QPushButton("试解析样本")
        btn_test.clicked.connect(self._test_parse)
        bv.addWidget(self.ed_sample)
        bv.addWidget(btn_test)
        bv.addWidget(self.lbl_preview)

        buttons = QDialogButtonBox()
        btn_save = buttons.addButton("保存并注册到设备列表", QDialogButtonBox.AcceptRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        btn_save.clicked.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(form)
        lay.addWidget(box, 1)
        lay.addWidget(buttons)

        if self._specs:
            self._load_selected()

    def _new_spec(self) -> None:
        self.ed_id.setText(f"custom_{len(self._specs) + 1}")
        self.ed_label.setText("自定义串口/网口雷达")
        self.cb_mode.setCurrentIndex(1)
        self.cb_form.setCurrentIndex(1)
        self.sp_rmax.setValue(30.0)
        self.sp_fov.setValue(120.0)
        self.ed_prefix.setText("$TGT")
        self.ed_delim.setText(",")
        self.sp_scale.setValue(1.0)
        self.ed_sample.setPlainText(_SAMPLES["csv_xy"])

    def _load_selected(self) -> None:
        did = self.cb_spec.currentData()
        spec = next((s for s in self._specs if s.id == did), None)
        if spec is None:
            return
        self.ed_id.setText(spec.id)
        self.ed_label.setText(spec.label)
        mi = self.cb_mode.findData(spec.mode)
        if mi >= 0:
            self.cb_mode.setCurrentIndex(mi)
        fi = self.cb_form.findData(spec.form)
        if fi >= 0:
            self.cb_form.setCurrentIndex(fi)
        self.sp_rmax.setValue(spec.range_max)
        self.sp_fov.setValue(spec.az_fov)
        self.ed_prefix.setText(spec.prefix)
        self.ed_delim.setText(spec.delimiter)
        self.ed_hdr.setText(spec.header_hex)
        self.ed_tail.setText(spec.tail_hex)
        self.sp_flen.setValue(spec.frame_len)
        self.sp_offset.setValue(spec.payload_offset)
        self.ed_fmt.setText(spec.struct_fmt)
        self.sp_scale.setValue(spec.scale)
        self.sp_angle_scale.setValue(getattr(spec, "angle_scale", 1.0))
        self.cb_baud.setCurrentText(str(getattr(spec, "baud_rate", 115200)))
        self.ed_sample.setPlainText(_PRESET_SAMPLES.get(spec.id, _SAMPLES.get(spec.mode, "")))

    def _build_spec(self) -> CustomProtocolSpec:
        try:
            baud = max(1200, int(self.cb_baud.currentText().strip() or 115200))
        except ValueError:
            baud = 115200
        return CustomProtocolSpec(
            id=self.ed_id.text().strip() or "custom_proto",
            label=self.ed_label.text().strip() or "自定义雷达协议",
            mode=str(self.cb_mode.currentData()),
            form=str(self.cb_form.currentData()),
            range_min=0.2,
            range_max=float(self.sp_rmax.value()),
            az_fov=float(self.sp_fov.value()),
            baud_rate=baud,
            prefix=self.ed_prefix.text().strip(),
            delimiter=self.ed_delim.text() or ",",
            header_hex=self.ed_hdr.text().strip(),
            tail_hex=self.ed_tail.text().strip(),
            frame_len=int(self.sp_flen.value()),
            payload_offset=int(self.sp_offset.value()),
            struct_fmt=self.ed_fmt.text().strip() or "hhh",
            scale=float(self.sp_scale.value()),
            angle_scale=float(self.sp_angle_scale.value()),
        )

    def _test_parse(self) -> None:
        try:
            tgts = preview_sample(self._build_spec(), self.ed_sample.toPlainText())
        except Exception as exc:
            self.lbl_preview.setStyleSheet("color:#B42318;font-size:12px;")
            self.lbl_preview.setText(f"解析失败：{exc}")
            return
        if not tgts:
            self.lbl_preview.setStyleSheet("color:#B45309;font-size:12px;")
            self.lbl_preview.setText("未解析出任何目标，请核对前缀、分隔符或帧头/struct 格式。")
            return
        self.lbl_preview.setStyleSheet("color:#0F766E;font-size:12px;")
        summary = "；".join(
            f"(x={t.x:.2f}m, y={t.y:.2f}m, 距离={t.rng:.2f}m, 速度={t.speed:.2f}m/s)"
            for t in tgts[:5]
        )
        self.lbl_preview.setText(f"成功解析 {len(tgts)} 个目标：{summary}")

    def _save_and_accept(self) -> None:
        spec = self._build_spec()
        if not spec.id:
            QMessageBox.warning(self, "通用协议", "请填写英文驱动 ID")
            return
        kept = [s for s in self._specs if s.id != spec.id]
        kept.append(spec)
        save_custom_specs(self._path, kept)
        reload_custom_drivers()
        self.accept()
