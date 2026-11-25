"""连接设置对话框。

之前只有一个可编辑的下拉框预置了两个 TCP 地址 —— 等于没做。
真实使用里最常见的恰恰是串口（USB 转 TTL），但用户在界面上根本
看不到"我插的板子是哪个 COM 口"。

这个对话框解决三件事：
    1. 把电脑上的串口**列出来**，并优先选中 USB 转串口芯片
       （CH340 / CP210x / FTDI / Prolific 占了这类模块的绝大多数）
    2. BLE 能扫描（装了 bleak 才可用），没装就明确告诉你怎么装
    3. 生成 `scheme://...` 端点字符串回填给主界面 —— 
       传输层只认这个字符串，所以这里只是"拼字符串的友好界面"

参考：STM32 radar host (Qt/QML) 的做法是自动识别 CH340 并显示好帧/坏帧计数，
这两点都值得抄 —— 前者省掉用户查设备管理器的步骤，后者能一眼看出接线/波特率对不对。
"""
from __future__ import annotations

import time

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

BAUDS = ["256000", "230400", "115200", "921600", "460800", "57600", "38400", "9600"]

#: USB 转串口芯片的 VID（十六进制），用来优先展示"这很可能就是我的板子"
USB_SERIAL_VIDS = {
    "1A86": "CH340/CH341",
    "10C4": "CP210x",
    "0403": "FTDI",
    "067B": "Prolific",
    "1A86&PID_7523": "CH340",
    "2E8A": "树莓派 Pico",
    "0483": "STM32 虚拟串口",
}


def scan_serial_ports() -> list[dict]:
    """枚举串口。返回 [{'device','description','chip','is_usb'}]，USB 的排前面。"""
    try:
        from serial.tools import list_ports
    except Exception:
        return []

    out: list[dict] = []
    for p in list_ports.comports():
        hwid = (p.hwid or "").upper()
        chip = ""
        for vid, name in USB_SERIAL_VIDS.items():
            if vid in hwid:
                chip = name
                break
        out.append({
            "device": p.device,
            "description": p.description or "",
            "chip": chip,
            "is_usb": "USB" in hwid or bool(chip),
        })
    # USB 转串口优先，其次按名字排
    out.sort(key=lambda d: (not d["is_usb"], d["device"]))
    return out


class _BleScan(QThread):
    found = pyqtSignal(str, str)      # 名称, 地址
    done = pyqtSignal(str)            # 错误信息，空串表示正常结束

    def run(self) -> None:
        try:
            import asyncio

            from bleak import BleakScanner
        except Exception as exc:
            self.done.emit(f"未安装 bleak，无法扫描：{exc}")
            return

        async def go() -> None:
            devices = await BleakScanner.discover(timeout=5.0)
            for d in devices:
                self.found.emit(d.name or "(无名)", d.address)

        try:
            asyncio.run(go())
            self.done.emit("")
        except Exception as exc:
            self.done.emit(str(exc))


class LinkDialog(QDialog):
    """返回一个端点字符串，例如 serial://COM5?baud=256000。"""

    def __init__(self, endpoint: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("连接设置")
        self.resize(560, 420)
        self._endpoint = endpoint
        self._ble_thread: _BleScan | None = None

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_serial(), "串口 / USB")
        self.tabs.addTab(self._build_net(), "网络 TCP/UDP")
        self.tabs.addTab(self._build_ble(), "蓝牙 BLE")
        self.tabs.addTab(self._build_file(), "文件回放")

        self.preview = QLineEdit()
        self.preview.setReadOnly(True)
        self.preview.setStyleSheet("font-family:Consolas;color:#9BF5BE;")

        btn_test = QPushButton("测试连接")
        btn_test.setToolTip("尝试打开并读取约 1 秒，看能不能收到字节")
        btn_test.clicked.connect(self._test)
        btn_ok = QPushButton("确定")
        btn_ok.clicked.connect(self._accept)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        for b in (btn_test, btn_ok, btn_cancel):
            bottom.addWidget(b)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("端点（传输层只认这个字符串，其余都是帮你拼它的界面）："))
        lay.addWidget(self.preview)
        lay.addWidget(self.tabs, 1)
        lay.addLayout(bottom)

        self._parse_initial(endpoint)
        for w in (self.cb_port, self.cb_baud, self.ed_host, self.sp_port,
                  self.ed_file, self.ed_mac):
            sig = getattr(w, "currentTextChanged", None) or getattr(w, "textChanged", None)
            if sig is not None:
                sig.connect(self._refresh_preview)
            elif hasattr(w, "valueChanged"):
                w.valueChanged.connect(self._refresh_preview)
        self.tabs.currentChanged.connect(self._refresh_preview)
        self._refresh_preview()

    # ------------------------------------------------------------------ #
    def _build_serial(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        row = QHBoxLayout()
        self.cb_port = QComboBox()
        self.cb_port.setMinimumWidth(240)
        row.addWidget(self.cb_port, 1)
        btn = QPushButton("刷新")
        btn.clicked.connect(self._refresh_ports)
        row.addWidget(btn)
        form.addRow("端口", row)

        self.cb_baud = QComboBox()
        self.cb_baud.setEditable(True)
        self.cb_baud.addItems(BAUDS)
        self.cb_baud.setCurrentText("256000")
        form.addRow("波特率", self.cb_baud)

        self.lbl_serial_hint = QLabel("")
        self.lbl_serial_hint.setWordWrap(True)
        self.lbl_serial_hint.setStyleSheet("color:#8FA396;font-size:12px;")
        form.addRow("", self.lbl_serial_hint)
        self._refresh_ports()
        return w

    def _refresh_ports(self) -> None:
        cur = self.cb_port.currentData() or ""
        ports = scan_serial_ports()
        self.cb_port.clear()
        if not ports:
            self.cb_port.addItem("（没有发现串口）", "")
            self.lbl_serial_hint.setText(
                "没找到串口。检查：① USB 线是不是数据线（有些只有供电）"
                "② 是否装了 CH340/CP210x 驱动 ③ 设备管理器里有没有未知设备")
            return
        usb_hint = ""
        for p in ports:
            label = f"{p['device']} — {p['description']}"
            if p["chip"]:
                label += f"  [{p['chip']}]"
                if not usb_hint:
                    usb_hint = f"优先选中了 {p['device']}（识别为 {p['chip']}）"
            self.cb_port.addItem(label, p["device"])
        idx = self.cb_port.findData(cur) if cur else -1
        self.cb_port.setCurrentIndex(idx if idx >= 0 else 0)
        if not usb_hint:
            usb_hint = "没识别出常见的 USB 转串口芯片，可能是板载虚拟串口，按设备名选即可"
        self.lbl_serial_hint.setText(usb_hint)

    def _build_net(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.ed_host = QLineEdit("192.168.4.1")
        form.addRow("地址 / 监听地址", self.ed_host)
        self.sp_port = QSpinBox()
        self.sp_port.setRange(1, 65535)
        self.sp_port.setValue(8080)
        form.addRow("端口", self.sp_port)
        self.cb_net = QComboBox()
        self.cb_net.addItem("TCP 客户端（连雷达）", "tcp")
        self.cb_net.addItem("UDP 监听（雷达主动推）", "udp")
        self.cb_net.currentIndexChanged.connect(self._refresh_preview)
        form.addRow("方式", self.cb_net)
        tip = QLabel("UDP 监听时地址填 0.0.0.0 表示接收本机所有网卡的数据。\n"
                     "ESP32 做热点时通常是 192.168.4.1:8080。")
        tip.setStyleSheet("color:#8FA396;font-size:12px;")
        tip.setWordWrap(True)
        form.addRow("", tip)
        return w

    def _build_ble(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        self.btn_scan = QPushButton("扫描附近的 BLE 设备")
        self.btn_scan.clicked.connect(self._scan_ble)
        row.addWidget(self.btn_scan)
        self.lbl_ble = QLabel("")
        self.lbl_ble.setStyleSheet("color:#8FA396;font-size:12px;")
        row.addWidget(self.lbl_ble, 1)
        v.addLayout(row)

        self.lst_ble = QListWidget()
        self.lst_ble.itemClicked.connect(self._pick_ble)
        v.addWidget(self.lst_ble, 1)

        form = QFormLayout()
        self.ed_mac = QLineEdit()
        self.ed_mac.setPlaceholderText("AA:BB:CC:DD:EE:FF（也可手动填）")
        form.addRow("设备地址", self.ed_mac)
        v.addLayout(form)
        v.addWidget(QLabel("提示：经典蓝牙模块（HC-05/06）不走 BLE，"
                           "它们配对后在系统里是一个串口，请用「串口」页。"))
        return w

    def _scan_ble(self) -> None:
        if self._ble_thread is not None and self._ble_thread.isRunning():
            return
        self.lst_ble.clear()
        self.lbl_ble.setText("扫描中…（约 5 秒）")
        self.btn_scan.setEnabled(False)
        self._ble_thread = _BleScan(self)
        self._ble_thread.found.connect(self._add_ble_item)
        self._ble_thread.done.connect(self._ble_done)
        self._ble_thread.start()

    def _add_ble_item(self, name: str, addr: str) -> None:
        it = QListWidgetItem(f"{name}   {addr}")
        it.setData(Qt.UserRole, addr)
        self.lst_ble.addItem(it)

    def _ble_done(self, err: str) -> None:
        self.btn_scan.setEnabled(True)
        if err:
            self.lbl_ble.setText(err)
            self.lbl_ble.setStyleSheet("color:#E8A33A;font-size:12px;")
        else:
            n = self.lst_ble.count()
            self.lbl_ble.setText(f"发现 {n} 个设备" if n else "没扫到设备，确认蓝牙已打开")

    def _pick_ble(self, item: QListWidgetItem) -> None:
        self.ed_mac.setText(item.data(Qt.UserRole) or "")
        self._refresh_preview()

    def _build_file(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        row = QHBoxLayout()
        self.ed_file = QLineEdit()
        row.addWidget(self.ed_file, 1)
        btn = QPushButton("浏览…")
        btn.clicked.connect(self._pick_file)
        row.addWidget(btn)
        form.addRow("录制文件", row)
        tip = QLabel("回放 .rad 文件（连接时勾「录制帧」才会生成）。\n"
                     "也可以选任意原始字节流文件，配合对应的驱动做协议调试。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8FA396;font-size:12px;")
        form.addRow("", tip)
        return w

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择文件", "", "录制/数据 (*.rad *.bin *.dat);;所有文件 (*.*)")
        if path:
            self.ed_file.setText(path)
            self._refresh_preview()

    # ------------------------------------------------------------------ #
    def _parse_initial(self, ep: str) -> None:
        """把已有端点回填到对应页签，方便微调而不是重填。"""
        ep = (ep or "").strip()
        if not ep:
            self.tabs.setCurrentIndex(0)
            return
        scheme = ep.split("://", 1)[0].lower() if "://" in ep else ""
        if scheme in ("serial", "ble", "tcp", "udp", "file"):
            idx = {"serial": 0, "tcp": 1, "udp": 1, "ble": 2, "file": 3}[scheme]
            self.tabs.setCurrentIndex(idx)
        if scheme == "serial":
            body = ep.split("://", 1)[1]
            dev = body.split("?", 1)[0]
            i = self.cb_port.findData(dev)
            if i < 0 and dev:
                # 板子没插着很常见，但配置不能因此丢掉 —— 保留并标注清楚
                self.cb_port.addItem(f"{dev} — 当前配置（本次未检测到）", dev)
                i = self.cb_port.count() - 1
            if i >= 0:
                self.cb_port.setCurrentIndex(i)
            if "baud=" in body:
                self.cb_baud.setCurrentText(body.split("baud=")[1].split("&")[0])
        elif scheme in ("tcp", "udp"):
            body = ep.split("://", 1)[1]
            host, _, port = body.partition(":")
            self.ed_host.setText(host)
            if port.isdigit():
                self.sp_port.setValue(int(port))
            self.cb_net.setCurrentIndex(0 if scheme == "tcp" else 1)
        elif scheme == "ble":
            self.ed_mac.setText(ep.split("://", 1)[1])
        elif scheme == "file":
            self.ed_file.setText(ep.split("://", 1)[1])

    def _current_endpoint(self) -> str:
        page = self.tabs.currentIndex()
        if page == 0:
            dev = self.cb_port.currentData()
            if not dev:
                return ""
            baud = self.cb_baud.currentText().strip() or "256000"
            return f"serial://{dev}?baud={baud}"
        if page == 1:
            scheme = self.cb_net.currentData()
            return f"{scheme}://{self.ed_host.text().strip()}:{self.sp_port.value()}"
        if page == 2:
            mac = self.ed_mac.text().strip()
            return f"ble://{mac}" if mac else ""
        return f"file://{self.ed_file.text().strip()}" if self.ed_file.text().strip() else ""

    def _refresh_preview(self, *_args) -> None:
        ep = self._current_endpoint()
        self.preview.setText(ep or "（尚未选择）")

    # ------------------------------------------------------------------ #
    def _test(self) -> None:
        ep = self._current_endpoint()
        if not ep:
            QMessageBox.information(self, "测试", "先选一个端口或填地址")
            return
        from ..core.transport import TransportError, open_transport
        tr = None
        try:
            tr = open_transport(ep, default_baud=256000)
            got = b""
            deadline = time.monotonic() + 1.2
            while time.monotonic() < deadline and len(got) < 64:
                chunk = tr.read(256)
                if chunk:
                    got += chunk
                else:
                    time.sleep(0.02)
        except TransportError as exc:
            QMessageBox.warning(self, "连接失败", f"{ep}\n\n{exc}")
            return
        except Exception as exc:
            QMessageBox.warning(self, "连接失败", f"{ep}\n\n{type(exc).__name__}: {exc}")
            return
        finally:
            if tr is not None:
                try:
                    tr.close()
                except Exception:
                    pass

        if got:
            head = " ".join(f"{b:02X}" for b in got[:16])
            QMessageBox.information(
                self, "连接正常",
                f"收到 {len(got)} 字节。前 16 字节：\n{head}")
        else:
            QMessageBox.information(
                self, "连上了但没数据",
                "端口能打开，但 1 秒内没收到任何字节。常见原因：\n"
                "· 波特率不对（这类模块常见 256000 / 230400 / 115200）\n"
                "· 板子没上电，或 TX/RX 接反\n"
                "· 需要先发指令才会开始吐数据")

    def _accept(self) -> None:
        ep = self._current_endpoint()
        if not ep:
            QMessageBox.warning(self, "未选择", "还没选端口或填地址")
            return
        self._endpoint = ep
        self.accept()

    def endpoint(self) -> str:
        return self._endpoint
