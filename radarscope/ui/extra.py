"""两个小配置对话框：设备控制、AIS 接入。

放在一个文件里是因为它们都很短，而且性质相同：把几个参数收进来交给主窗口。
"""
from __future__ import annotations

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
    QVBoxLayout,
)

from ..core.maptiles import PROVIDERS, geolocate_ip


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


class _GeoLocateWorker(QThread):
    finished_ok = pyqtSignal(float, float, str)
    finished_err = pyqtSignal(str)

    def run(self) -> None:
        try:
            lat, lon, desc = geolocate_ip(timeout=4.0)
            self.finished_ok.emit(lat, lon, desc)
        except Exception as exc:
            self.finished_err.emit(str(exc))


class MapLocationDialog(QDialog):
    """联网地图底图与定位中心设置：支持自动 IP 获取与手动输入经纬度。"""

    def __init__(self, lat: float = 39.9, lon: float = 116.4,
                 provider: str = "auto", map_enabled: bool = False,
                 mbtiles_path: str = "", nmea_port: int = 10111,
                 nmea_enabled: bool = False, land_mask: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("联网 / 离线海图与定位设置")
        self.resize(520, 440)
        self._worker: _GeoLocateWorker | None = None

        form = QFormLayout()
        self.cb_provider = QComboBox()
        self.cb_provider.addItem("自动跟随界面主题（浅色/深色）", "auto")
        for key, meta in PROVIDERS.items():
            self.cb_provider.addItem(meta.get("label", key), key)
        pi = self.cb_provider.findData(provider)
        if pi >= 0:
            self.cb_provider.setCurrentIndex(pi)
        form.addRow("底图图源", self.cb_provider)

        self.ed_lat = QLineEdit(f"{float(lat):.6f}")
        self.ed_lat.setPlaceholderText("例如 39.904200（-90 ~ 90）")
        form.addRow("中心纬度", self.ed_lat)

        self.ed_lon = QLineEdit(f"{float(lon):.6f}")
        self.ed_lon.setPlaceholderText("例如 116.407400（-180 ~ 180）")
        form.addRow("中心经度", self.ed_lon)

        geo_row = QHBoxLayout()
        self.btn_auto = QPushButton("自动获取当前位置 (IP)")
        self.btn_auto.clicked.connect(self._start_auto)
        self.lbl_status = QLabel("可手动输入或点击自动获取；会同步为 AIS 本机坐标")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("color:#6B7280;font-size:12px;")
        geo_row.addWidget(self.btn_auto)
        geo_row.addWidget(self.lbl_status, 1)

        self.chk_enable = QCheckBox("确定后立即开启并刷新联网/离线地图底图")
        self.chk_enable.setChecked(bool(map_enabled))
        self.chk_fit_range = QCheckBox("开启地图且当前量程 < 100 m 时，自动切到 500 m 以便看清街道与岸线")
        self.chk_fit_range.setChecked(True)

        # E6：离线 MBTiles、NMEA 本船定位与陆地静止杂波掩膜
        mb_row = QHBoxLayout()
        self.ed_mbtiles = QLineEdit(str(mbtiles_path or ""))
        self.ed_mbtiles.setPlaceholderText("可选离线 .mbtiles 海图/地图数据库路径")
        btn_mb = QPushButton("选择 .mbtiles…")
        btn_mb.clicked.connect(self._pick_mbtiles)
        btn_mb_clr = QPushButton("清除")
        btn_mb_clr.clicked.connect(lambda: self.ed_mbtiles.setText(""))
        mb_row.addWidget(self.ed_mbtiles, 1)
        mb_row.addWidget(btn_mb)
        mb_row.addWidget(btn_mb_clr)
        form.addRow("离线 MBTiles", mb_row)

        nmea_row = QHBoxLayout()
        self.chk_nmea = QCheckBox("监听本船 NMEA UDP ($GGA/$RMC/$HDT)")
        self.chk_nmea.setChecked(bool(nmea_enabled))
        self.sp_nmea_port = QSpinBox()
        self.sp_nmea_port.setRange(1024, 65535)
        self.sp_nmea_port.setValue(int(nmea_port or 10111))
        nmea_row.addWidget(self.chk_nmea, 1)
        nmea_row.addWidget(QLabel("端口"))
        nmea_row.addWidget(self.sp_nmea_port)
        form.addRow("本船定位流", nmea_row)

        self.chk_land_mask = QCheckBox("启用陆地静止杂波掩膜（自动抑制岸线与陆地区域固定回波）")
        self.chk_land_mask.setChecked(bool(land_mask))

        tip = QLabel(
            "说明：\n"
            "1. 联网地图以此处经纬度为中心下载开源栅格瓦片（默认 ArcGIS 世界街道，国内访问稳定）；\n"
            "2. 6~15 m 室内量程在地球上仅占半间屋子，切到 ≥200 m（如 500 m / 2 km）才能看清道路与水系；\n"
            "3. 自动获取通过公网 IP 定位，精度通常在城市/区级，高精度场景可手动填写精确经纬度。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#6B7280;font-size:12px;")

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addLayout(geo_row)
        lay.addWidget(self.chk_enable)
        lay.addWidget(self.chk_fit_range)
        lay.addWidget(self.chk_land_mask)
        lay.addWidget(tip, 1)
        lay.addWidget(self.buttons)

    def _pick_mbtiles(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择离线 MBTiles 海图/地图", "", "MBTiles 数据库 (*.mbtiles);;所有文件 (*.*)"
        )
        if path:
            self.ed_mbtiles.setText(path)

    def _start_auto(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.btn_auto.setEnabled(False)
        self.lbl_status.setStyleSheet("color:#1F6FEB;font-size:12px;")
        self.lbl_status.setText("正在通过网络自动定位，请稍候…")
        self._worker = _GeoLocateWorker(self)
        self._worker.finished_ok.connect(self._on_geo_ok)
        self._worker.finished_err.connect(self._on_geo_err)
        self._worker.start()

    def _on_geo_ok(self, lat: float, lon: float, desc: str) -> None:
        self.btn_auto.setEnabled(True)
        self.ed_lat.setText(f"{lat:.6f}")
        self.ed_lon.setText(f"{lon:.6f}")
        self.lbl_status.setStyleSheet("color:#0F766E;font-size:12px;")
        self.lbl_status.setText(f"已获取：{desc}（{lat:.4f}, {lon:.4f}）")

    def _on_geo_err(self, msg: str) -> None:
        self.btn_auto.setEnabled(True)
        self.lbl_status.setStyleSheet("color:#B42318;font-size:12px;")
        self.lbl_status.setText(msg)

    def _on_accept(self) -> None:
        try:
            lat = float(self.ed_lat.text().strip())
            lon = float(self.ed_lon.text().strip())
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                raise ValueError("纬度需在 -90~90，经度需在 -180~180")
        except ValueError as exc:
            self.lbl_status.setStyleSheet("color:#B42318;font-size:12px;")
            self.lbl_status.setText(f"坐标格式有误：{exc}")
            return
        self.accept()

    def closeEvent(self, ev) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(500)
        super().closeEvent(ev)

    def auto_fit_range(self) -> bool:
        return self.chk_fit_range.isChecked()

    def values(self) -> tuple[float, float, str, bool]:
        try:
            lat = max(-90.0, min(90.0, float(self.ed_lat.text().strip())))
            lon = max(-180.0, min(180.0, float(self.ed_lon.text().strip())))
        except ValueError:
            lat, lon = 39.9, 116.4
        return (
            lat,
            lon,
            str(self.cb_provider.currentData() or "auto"),
            self.chk_enable.isChecked(),
        )

    def e6_options(self) -> dict:
        return {
            "mbtiles_path": self.ed_mbtiles.text().strip(),
            "nmea_enabled": self.chk_nmea.isChecked(),
            "nmea_port": int(self.sp_nmea_port.value()),
            "land_mask": self.chk_land_mask.isChecked(),
        }
