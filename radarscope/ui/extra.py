"""两个小配置对话框：设备控制、AIS 接入。

放在一个文件里是因为它们都很短，而且性质相同：把几个参数收进来交给主窗口。
"""
from __future__ import annotations

from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel,
    QLineEdit, QSpinBox, QVBoxLayout,
)


class ControlDialog(QDialog):
    """增益 / STC / FTC 下发。

    关键设计：**如果驱动不支持，就把控件全置灰并写明原因**。
    让用户看得见"这台设备不支持"，比给一个按了没反应的滑块诚实得多。
    """

    def __init__(self, supported: bool, state: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设备控制")
        self.resize(420, 260)
        state = state or {}
        form = QFormLayout()

        self.sp_gain = QDoubleSpinBox()
        self.sp_gain.setRange(0.1, 6.0)
        self.sp_gain.setSingleStep(0.1)
        self.sp_gain.setDecimals(2)
        self.sp_gain.setValue(float(state.get("gain", 1.0)))

        self.sp_stc = QDoubleSpinBox()
        self.sp_stc.setRange(0.0, 1.0)
        self.sp_stc.setSingleStep(0.05)
        self.sp_stc.setValue(float(state.get("stc", 0.0)))

        self.sp_ftc = QDoubleSpinBox()
        self.sp_ftc.setRange(0.0, 0.9)
        self.sp_ftc.setSingleStep(0.05)
        self.sp_ftc.setValue(float(state.get("ftc", 0.0)))

        form.addRow("增益 gain", self.sp_gain)
        form.addRow("STC 近距抑制", self.sp_stc)
        form.addRow("FTC 弱回波抑制", self.sp_ftc)

        self.lbl = QLabel("")
        self.lbl.setWordWrap(True)
        self.lbl.setStyleSheet("color:#8FA396;font-size:12px;")
        if supported:
            self.lbl.setText("这些参数会下发到设备。STC 压制近处强杂波，"
                             "FTC 抹掉低于门限的弱回波（去雨雪/噪声）。")
        else:
            self.lbl.setText("⚠ 当前设备不支持下行控制（协议只读）。\n"
                             "模拟器支持这些参数，可以先在模拟器上看效果；\n"
                             "真雷达需要驱动实现 set_control() 把参数编码成指令发出去。")
            for w in (self.sp_gain, self.sp_stc, self.sp_ftc):
                w.setEnabled(False)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        if not supported:
            self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.lbl, 1)
        lay.addWidget(self.buttons)

    def values(self) -> dict:
        return {"gain": self.sp_gain.value(), "stc": self.sp_stc.value(),
                "ftc": self.sp_ftc.value()}


class AisDialog(QDialog):
    """AIS 接入设置。

    必须填本船经纬度：AIS 报的是绝对经纬度，而雷达画面是"以我为原点"的米制坐标，
    没有参考点就没法把 AIS 船叠到雷达图上。
    """

    def __init__(self, port: int = 10110, lat: float = 39.9, lon: float = 116.4,
                 enabled: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AIS 接入")
        self.resize(440, 300)
        form = QFormLayout()

        self.sp_port = QSpinBox()
        self.sp_port.setRange(1, 65535)
        self.sp_port.setValue(int(port))
        form.addRow("UDP 端口", self.sp_port)

        self.ed_lat = QLineEdit(f"{lat:.6f}")
        form.addRow("本船纬度", self.ed_lat)
        self.ed_lon = QLineEdit(f"{lon:.6f}")
        form.addRow("本船经度", self.ed_lon)

        self.chk = QCheckBox("启用 AIS 接收")
        self.chk.setChecked(bool(enabled))

        tip = QLabel(
            "AIS 接收机一般把 NMEA 语句按 UDP 广播出来（常见端口 10110）。\n"
            "本船经纬度用来把 AIS 的绝对坐标换算成以我为原点的米制坐标；\n"
            "填当前实际位置即可，误差几十米对几公里的显示影响很小。\n\n"
            "支持报文：1/2/3（Class A）与 18/19（Class B）位置报告。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8FA396;font-size:12px;")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.chk)
        lay.addWidget(tip, 1)
        lay.addWidget(buttons)

    def values(self) -> tuple[int, float, float, bool]:
        try:
            lat = float(self.ed_lat.text())
            lon = float(self.ed_lon.text())
        except ValueError:
            lat = lon = 0.0
        return self.sp_port.value(), lat, lon, self.chk.isChecked()
