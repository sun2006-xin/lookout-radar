"""主窗口的三个控制器（mixin）与读取线程。

A1：mainwindow 一度长到 1580 行，成了"什么都知道"的上帝对象。
这里按职责把三块拆出来：

    ConnectionController  连接/断开/重连/回放
    ExportController      转发/录像/静音/导出
    AlarmBarController    告警条/规则/警戒区

用 mixin 而不是持有 owner 引用的组合，是为了让 `self.xxx` 的语义保持不变 ——
这些方法操作的就是 MainWindow 自己，拆的是"代码放在哪"，不是"谁负责"。
风险点：循环导入。所以本模块只 import 外部模块与自己的常量，
绝不 `from .mainwindow import ...`。
"""


from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np

from PyQt5.QtCore import QSettings, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QKeySequence
from PyQt5.QtWidgets import (
    QAction, QActionGroup, QApplication, QComboBox, QDialog, QDockWidget,
    QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
    QDialogButtonBox, QMainWindow, QMenu, QMessageBox, QPushButton,
    QShortcut, QTabWidget,
    QToolBar, QToolButton, QVBoxLayout, QWidget, QWidgetAction,
)

from ..core import cpa, pipeline
from ..core.aisfeed import AisFeed
from ..core.forwarder import Forwarder
from ..core.alarm import DEFAULT_RULES, AlarmEngine, merge_rules, rules_from_json
from ..core.driver import available_drivers
from ..core.model import Capabilities
from ..core.plugins import PluginManager
from ..core.storage import FrameArchive, Store
from ..core.tracker import Tracker
from .. import paths
from .ascope import AScopeWidget
from .calibrationwizard import CalibrationWizard
from .eventlog import EventLog
from .history import HistoryPanel
from .linkdialog import LinkDialog
from .maplayer import MapLayer
from .pluginsdialog import PluginsDialog
from .ppi import PPIWidget
from .rdscope import RDScopeWidget
from .rulesdialog import RulesDialog
from .sensorpanel import SensorPanel
from .sensorsdialog import SensorsDialog
from .statsdialog import StatsDialog
from .theme import (DEFAULT_PALETTE, DEFAULT_THEME, LIGHT_PALETTE_KEYS,
                    PALETTES, THEMES, build_stylesheet, is_dark_theme)
from .tracktable import TrackTable
from .extra import AisDialog, ControlDialog
from .waterfall import WaterfallWidget
from .winstyle import enable_dark_titlebar, install_dialog_titlebar, make_app_icon

LOG = logging.getLogger("radarscope")


PRESET_ENDPOINTS = [
    "serial://COM3?baud=256000",
    "ble://",
    "tcp://192.168.4.1:8080",
    "udp://0.0.0.0:9000",
]

ALARM_STYLE = {
    "crit": "background:#7A1B14;color:#FFE3DA;border-radius:4px;padding:4px 10px;font-size:13px;",
    "warn": "background:#5E4210;color:#FFEDCC;border-radius:4px;padding:4px 10px;font-size:13px;",
    "info": "background:#1B3324;color:#CFEAD9;border-radius:4px;padding:4px 10px;font-size:13px;",
    # 无告警时的常态：刻意做得很淡，不该抢注意力
    "ok": "background:#11170F;color:#6C8375;border:1px solid #1E2A22;"
          "border-radius:4px;padding:4px 10px;font-size:12px;",
}

#: 浅色主题配对色板集合（白底深回波）
LIGHT_PALETTES = LIGHT_PALETTE_KEYS


#: 浅色主题下换成"淡底深字"。一条深棕横幅挂在白界面上太突兀了。
ALARM_STYLE_LIGHT = {
    "crit": "background:#FDECEA;color:#B42318;border:1px solid #F6C9C2;"
            "border-radius:6px;padding:4px 10px;font-size:13px;",
    "warn": "background:#FFF6E5;color:#8A5A00;border:1px solid #F3DFB6;"
            "border-radius:6px;padding:4px 10px;font-size:13px;",
    "info": "background:#EAF6EF;color:#1B6B45;border:1px solid #CBE7D8;"
            "border-radius:6px;padding:4px 10px;font-size:13px;",
    "ok": "background:#F3F4F6;color:#6B7280;border:1px solid #E2E5EA;"
          "border-radius:6px;padding:4px 10px;font-size:12px;",
}

#: 二级下拉框的形态分组顺序（从"最简单"到"最复杂"）
#: 分组依据是"测量形态"，不是型号 —— 型号天天出新，写进逻辑就等于把软件钉死在某一年
FORM_ORDER = ["point", "sector", "omni", "tracking", "rd", "fusion"]
FORM_NAMES = {
    "point": "单点测距",
    "sector": "扇区扫描",
    "omni": "全向 360°",
    "tracking": "目标跟踪",
    "rd": "距离-多普勒",
    "fusion": "多设备融合",
}


class ReaderThread(QThread):
    """后台采集与信号处理工作线程（ROADMAP C1）：取帧 + CFAR 检测/反投影全部在后台完成，
    主线程只负责轻量的 UI 绘制与状态更新，避免多站融合下主线程卡顿。"""

    frameReady = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, driver, parent=None, caps=None,
                 thresh_getter=None, cfar_getter=None, stall_timeout_s: float = 0.0):
        super().__init__(parent)
        self.driver = driver
        self._caps = caps
        self._thresh_getter = thresh_getter or (lambda: 0.30)
        self._cfar_getter = cfar_getter or (lambda: "auto")
        self._stall_timeout_s = max(0.0, float(stall_timeout_s))
        self._alive = True

    def stop(self) -> None:
        self._alive = False
        self.wait(2000)

    def run(self) -> None:
        last_rx = time.monotonic()
        while self._alive:
            try:
                frame = self.driver.read()
            except Exception as exc:                 # 串口拔出、网络断开
                self.failed.emit(f"{type(exc).__name__}: {exc}")
                return
            if frame is not None:
                last_rx = time.monotonic()
                if self._caps is not None:
                    try:
                        frame = pipeline.normalize(
                            frame,
                            self._caps,
                            threshold=float(self._thresh_getter()),
                            cfar_mode=str(self._cfar_getter()),
                        )
                        frame.meta["_normalized"] = True
                    except Exception as exc:
                        LOG.warning("后台流水线处理异常（跳过帧）：%s", exc)
                        continue
                self.frameReady.emit(frame)
            elif self._stall_timeout_s > 0.0 and (time.monotonic() - last_rx) >= self._stall_timeout_s:
                self.failed.emit(f"设备数据超时（连续 {self._stall_timeout_s:.0f} 秒未收到有效帧）")
                return



class ConnectionController:
    """见模块说明。方法在运行时由 MainWindow 实例调用。"""

    def _driver_baud(self) -> int:
        did = self.drv_combo.currentData() if hasattr(self, "drv_combo") else None
        cls = available_drivers().get(did) if did else None
        return int(getattr(cls, "default_baud", 256000) or 256000)

    def _refresh_endpoints(self) -> None:
        cur = self.endpoint_combo.currentText()
        self.endpoint_combo.clear()
        items: list[str] = []
        baud = self._driver_baud()
        try:
            from serial.tools import list_ports
            for p in list_ports.comports():
                items.append(f"serial://{p.device}?baud={baud}")
        except Exception:
            pass
        items.extend(PRESET_ENDPOINTS)
        for f in sorted(self.data_dir.glob("*.rad"))[-5:]:
            items.append(str(f))
        self.endpoint_combo.addItems(items)
        saved = self.settings.value("endpoint", "", type=str)
        if cur:
            self.endpoint_combo.setCurrentText(cur)
        elif saved:
            self.endpoint_combo.setCurrentText(saved)
        elif items:
            self.endpoint_combo.setCurrentIndex(0)

    def _on_source_changed(self) -> None:
        self._rebuild_driver_combo()

    def _rebuild_driver_combo(self, keep: str | None = None) -> None:
        """按「来源 → 形态」两级重建二级下拉框。"""
        src = self.src_combo.currentData()
        want = keep or self.drv_combo.currentData()
        self.drv_combo.blockSignals(True)
        self.drv_combo.clear()

        by_form: dict[str, list[tuple[str, type]]] = {}
        for did, cls in available_drivers().items():
            if getattr(cls, "source", "device") != src:
                continue
            by_form.setdefault(getattr(cls, "form", "sector"), []).append((did, cls))

        for form in FORM_ORDER:
            items = by_form.get(form)
            if not items:
                continue
            # 组标题做成不可选项，比单纯加分隔线更容易看懂
            self.drv_combo.addItem(f"── {FORM_NAMES[form]} ──", None)
            item = self.drv_combo.model().item(self.drv_combo.count() - 1)
            if item is not None:
                item.setEnabled(False)
            for did, cls in sorted(items, key=lambda kv: kv[1].label):
                self.drv_combo.addItem(cls.label, did)

        idx = self.drv_combo.findData(want) if want else -1
        if idx < 0 and src == "sim":
            # 模拟源优先默认选中内置「模拟器 · 合成雷达」，而不是 1D 单波束插件示例
            idx = self.drv_combo.findData("sim")
        if idx < 0:
            idx = next((i for i in range(self.drv_combo.count())
                        if self.drv_combo.itemData(i) is not None), 0)
        self.drv_combo.setCurrentIndex(idx)
        self.drv_combo.blockSignals(False)
        self._on_driver_changed()

    def _on_driver_changed(self) -> None:
        did = self.drv_combo.currentData()
        cls = available_drivers().get(did)
        if cls is None:
            return
        sim = bool(getattr(cls, "simulated", False))
        self.endpoint_combo.setEnabled(not sim)
        self.mode_combo.setEnabled(did == "sim")
        src = getattr(cls, "source", "device")
        if sim:
            self.endpoint_combo.setToolTip("模拟数据源无需填写端点")
        else:
            self.endpoint_combo.setToolTip(
                "支持：serial://COM3?baud=256000 ｜ ble://MAC ｜ tcp://ip:port ｜ udp://0.0.0.0:port\n"
                "文件回放：文件路径或 file://路径；任何端点都可以加 &record=capture.bin 录原始流"
            )
            cur = self.endpoint_combo.currentText().strip()
            if src == "replay" and not cur.lower().endswith((".rad", ".bin")):
                rads = sorted(self.data_dir.glob("*.rad"))
                if rads:
                    self.endpoint_combo.setCurrentText(str(rads[-1]))
            elif src == "device" and cur.lower().endswith(".rad"):
                for i in range(self.endpoint_combo.count()):
                    t = self.endpoint_combo.itemText(i)
                    if t.startswith("serial://"):
                        self.endpoint_combo.setCurrentIndex(i)
                        break
        try:
            caps = cls().resolve_capabilities()
        except Exception:
            caps = Capabilities()
        self._apply_caps(caps)
        # 串口波特率跟随当前实测驱动（例如 LD19=230400, LD2450=256000, 超声波=115200）
        want_baud = int(getattr(cls, "default_baud", 256000) or 256000)
        ep_cur = self.endpoint_combo.currentText().strip()
        if ep_cur.startswith("serial://") and "baud=" in ep_cur:
            for known in (115200, 230400, 256000):
                if f"baud={known}" in ep_cur and known != want_baud:
                    self.endpoint_combo.setCurrentText(ep_cur.replace(f"baud={known}", f"baud={want_baud}"))
                    break

    def _toggle_connect(self) -> None:
        if self.reader is not None:
            self._disconnect("手动断开")
            return
        self._want_connect = True
        self._connect()

    def _connect(self, silent: bool = False) -> None:
        if self.reader is not None:
            return
        did = self.drv_combo.currentData()
        cls = available_drivers().get(did)
        if cls is None:
            return
        endpoint = self.endpoint_combo.currentText().strip()
        try:
            drv = cls()
            drv.open(endpoint=endpoint, port=endpoint, path=endpoint,
                     mode=self.mode_combo.currentData())
        except Exception as exc:
            self.driver = None
            self._log_level("crit", f"打开失败：{type(exc).__name__}: {exc}")
            if not silent:
                QMessageBox.critical(
                    self, "打开失败",
                    f"{type(exc).__name__}: {exc}\n\n"
                    "常见原因：\n"
                    "· 串口被别的软件占用 / 没装 CH340 驱动\n"
                    "· 波特率不对（海凌科模块是 256000，不是 115200）\n"
                    "· 模块供电不足（峰值要 200mA 以上）\n"
                    "· TCP/UDP 地址或端口写错，防火墙拦截")
            self.btn_conn.setChecked(False)
            self.btn_conn.setText("连接")
            return

        self.driver = drv
        # 分类属性由驱动基类统一合并，别在这里手工补（容易漏）
        caps = drv.resolve_capabilities()
        self._apply_caps(caps)
        self._reconnect_try = 0

        self.store.start_session(did, endpoint, caps)
        if self.btn_rec.isChecked():
            self._open_archive()

        self._last_ts = 0.0
        self._last_frame_mono = time.monotonic()
        self._frames = 0
        self._fps_t = time.monotonic()

        ep_low = endpoint.lower()
        is_live_hw = (
            not bool(getattr(drv, "simulated", False))
            and getattr(caps, "source", "") == "device"
            and not ep_low.startswith("file://")
            and not ep_low.endswith((".bin", ".rad"))
        )
        self.reader = ReaderThread(
            self.driver, self,
            caps=self.caps,
            thresh_getter=lambda: self._thresh,
            cfar_getter=lambda: getattr(self, "_cfar_mode", "auto"),
            stall_timeout_s=5.0 if is_live_hw else 0.0,
        )
        self.reader.frameReady.connect(self._on_frame)
        self.reader.failed.connect(self._on_failed)
        self.reader.start()

        self.btn_conn.setText("断开")
        self.btn_conn.setChecked(True)
        self.status_left.setText(drv.describe())
        self._log_level("ok", f"已连接 {caps.name} ｜ {endpoint or '虚拟设备'}")
        self.store.record_event("info", "connect", f"连接 {caps.name} {endpoint}")

    def _teardown_reader(self) -> None:
        if self.reader is not None:
            self.reader.stop()
            self.reader = None
        mr = getattr(self, "mission_recorder", None)
        if mr is not None:
            mf = mr.close()
            self.mission_recorder = None
            if hasattr(self, "btn_mission"):
                self.btn_mission.blockSignals(True)
                self.btn_mission.setChecked(False)
                self.btn_mission.setText("任务录制")
                self.btn_mission.blockSignals(False)
            self.log.push("ok", f"任务包已归档：{mf.mission_id}（{mf.frames} 帧）")
        if self.archive is not None:
            self.archive.close()
            if self.store.session_id is not None:
                self.store.attach_archive(self.store.session_id, self.archive)
            self.archive = None
        if self.driver is not None:
            try:
                self.driver.close()
            except Exception:
                pass
            self.driver = None

    def _disconnect(self, reason: str = "") -> None:
        self._want_connect = False
        self._teardown_reader()
        if self.store.session_id is not None:
            self.store.end_session(reason)
        self.btn_conn.setText("连接")
        self.btn_conn.setChecked(False)
        self.status_left.setText("已断开")
        try:
            self.history.refresh()
        except Exception:
            pass
        if reason:
            self._log_level("info", f"已断开：{reason}")

    def _on_failed(self, msg: str) -> None:
        simulated = bool(getattr(self.driver, "simulated", False))
        self._teardown_reader()
        if self.store.session_id is not None:
            self.store.end_session(f"中断：{msg}")
        self._log_level("crit", f"数据中断：{msg}")
        self.btn_conn.setText("连接")
        self.btn_conn.setChecked(False)

        if self.chk_reconnect.isChecked() and not simulated and self._want_connect:
            self._reconnect_try += 1
            delay = min(10.0, 2.0 ** (self._reconnect_try - 1))
            self.status_left.setText(
                f"连接中断，{delay:.0f} 秒后自动重连（第 {self._reconnect_try} 次）")
            QTimer.singleShot(int(delay * 1000), lambda: self._connect(silent=True))
        else:
            self.status_left.setText("已断开")
            QMessageBox.warning(self, "数据中断", msg)

    # ================================================================== #
    # 录制
    # ================================================================== #

    def _open_archive(self) -> None:
        name = time.strftime("%Y%m%d-%H%M%S") + ".rad"
        self.archive = FrameArchive(self.data_dir / name)
        self.log.push("info", f"开始录制帧：{name}")

    # ================================================================== #
    # 帧处理
    # ================================================================== #

    def _start_replay(self, path: str) -> None:
        """从历史面板直接回放某次录制。"""
        p = Path(path)
        if not p.exists():
            QMessageBox.warning(self, "回放", f"录制文件不存在：\n{path}")
            return
        if self.reader is not None:
            self._disconnect("切换到回放")
        si = self.src_combo.findData("replay")
        if si >= 0:
            self.src_combo.setCurrentIndex(si)
        self._rebuild_driver_combo(keep="replay")
        self.endpoint_combo.setCurrentText(str(p))
        self._log_level("info", f"开始回放 {p.name}")
        self._toggle_connect()

    # ================================================================== #



class ExportController:
    """见模块说明。方法在运行时由 MainWindow 实例调用。"""

    def _on_record_toggled(self, on: bool) -> None:
        if on:
            if self.reader is None:
                self.log.push("warn", "先连接再开始录制")
            else:
                self._open_archive()
        elif self.archive is not None:
            self.archive.close()
            if self.store.session_id is not None:
                self.store.attach_archive(self.store.session_id, self.archive)
            self.log.push("info", f"录制结束：{self.archive.path.name}"
                                  f"（{self.archive.frames} 帧）")
            self.archive = None

    def _toggle_mission_record(self, on: bool) -> None:
        """一键任务录制：同步写入 frames.rad、manifest.json 参数快照并关联 SQLite 会话。"""
        from ..core.i18n import tr
        from ..core.mission import MissionRecorder

        if not on:
            mr = getattr(self, "mission_recorder", None)
            if mr is not None:
                mf = mr.close()
                self.mission_recorder = None
                self.log.push("ok", f"任务包已保存：{mf.mission_id}（{mf.frames} 帧）")
            if hasattr(self, "btn_mission"):
                self.btn_mission.setText(tr("任务录制"))
            if hasattr(self, "_update_status_telemetry"):
                self._update_status_telemetry()
            return

        if self.reader is None and hasattr(self, "_ensure_sim_running"):
            self._ensure_sim_running("任务录制")
        if self.reader is None:
            if hasattr(self, "btn_mission"):
                self.btn_mission.blockSignals(True)
                self.btn_mission.setChecked(False)
                self.btn_mission.blockSignals(False)
            self.log.push("warn", "请先连接雷达或模拟源，再开启一键任务录制")
            return

        snap = {
            "cfar_mode": getattr(self, "_cfar_mode", "auto"),
            "threshold": getattr(self, "_thresh", 0.30),
            "clutter_enabled": bool(self.clutter_map.enabled),
            "land_mask": bool(self.land_mask.enabled),
            "filter_mode": self.tracker.filter_mode,
            "assoc_mode": self.tracker.assoc_mode,
        }
        did = str(self.drv_combo.currentData() or getattr(self.driver, "id", "sim"))
        ep = self.endpoint_combo.currentText().strip()
        self.mission_recorder = MissionRecorder(
            self.data_dir / "missions",
            driver=did, endpoint=ep, caps=self.caps,
            settings_snapshot=snap, session_id=self.store.session_id,
        )
        if hasattr(self, "btn_mission"):
            self.btn_mission.setText(tr("停止任务"))
        if hasattr(self, "_update_status_telemetry"):
            self._update_status_telemetry()
        self.log.push("ok", f"开始录制任务包：{self.mission_recorder.manifest.mission_id}")

    def _open_mission_compare(self) -> None:
        from .missiondialog import MissionCompareDialog

        dlg = MissionCompareDialog(self.data_dir / "missions", self)
        dlg.replayRequested.connect(self._start_replay)
        dlg.exec_()

    def _toggle_forward(self, on: bool) -> None:
        if not on:
            self.forwarder.stop()
            self.log.push("info", "数据转发已停止")
            return
        cur = self.settings.value("fwd_addr", "127.0.0.1:9100", type=str)
        addr, ok = QInputDialog.getText(
            self, "数据转发",
            "接收地址（host:port）。本机调试用 127.0.0.1:9100；\n"
            "发给局域网其他机器可用 255.255.255.255:9100 广播：", text=cur)
        if not ok or not addr.strip():
            self.btn_fwd.setChecked(False)
            return
        host, _, port = addr.strip().rpartition(":")
        if not port.isdigit():
            QMessageBox.warning(self, "地址不对", "格式应为 host:port，例如 127.0.0.1:9100")
            self.btn_fwd.setChecked(False)
            return
        self.forwarder.configure(host or "127.0.0.1", int(port), rate=10.0)
        if self.forwarder.public_target:
            if QMessageBox.question(
                    self, "确认转发到公网",
                    f"目标 {host} 看起来是公网地址。\n"
                    "雷达目标数据会被发送到本机网络之外，确认继续？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No) != QMessageBox.Yes:
                self.forwarder.configure("127.0.0.1", int(port), rate=10.0)
                self.btn_fwd.setChecked(False)
                return
        self.settings.setValue("fwd_addr", addr.strip())
        self.forwarder.start()
        self.log.push("ok", f"数据转发已开启 → {host}:{port}（每帧一个 JSON 包）")

    def _toggle_record(self, on: bool) -> None:
        if not on:
            if self._rec_dir is not None:
                self.log.push("info", f"录像结束，共 {self._rec_n} 帧：{self._rec_dir}")
            self._rec_dir = None
            return
        self._rec_dir = paths.data_dir() / f"rec_{time.strftime('%Y%m%d-%H%M%S')}"
        self._rec_dir.mkdir(parents=True, exist_ok=True)
        self._rec_n = 0
        self._rec_t = 0.0
        self.log.push("ok", f"开始录像（4 帧/秒）→ {self._rec_dir}")

    def _toggle_mute(self, on: bool) -> None:
        self._muted = bool(on)
        self.log.push("info", "告警提示音已静音（事件仍在记录）" if on else "提示音已恢复")

    def _maybe_record(self, now: float) -> None:
        if self._rec_dir is None or (now - self._rec_t) < 0.25:
            return
        self._rec_t = now
        self._rec_n += 1
        try:
            self.ppi.grab().save(str(self._rec_dir / f"f{self._rec_n:05d}.png"))
        except Exception as exc:
            self.log.push("warn", f"录像写盘失败：{exc}")
            self.btn_clip.setChecked(False)

    def _export_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出事件日志", str(self.data_dir / "events.log"),
            "日志文件 (*.log *.txt)")
        if not path:
            return
        n = self.log.export(path)
        if n < 0:
            QMessageBox.warning(self, "导出失败", "写文件时出错")
        else:
            self.log.push("ok", f"已导出 {n} 行到 {path}")

    def _export_screenshot(self) -> None:
        """把整个窗口存成 PNG。

        写报告、发给同事、留档"当时长什么样"都靠它 —— 比口头描述强。
        默认文件名带时间戳，免得存一堆 screenshot(1)(2)。
        """
        default = self.data_dir / (time.strftime("%Y%m%d-%H%M%S") + "-画面.png")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出当前画面", str(default), "PNG 图片 (*.png)")
        if not path:
            return
        try:
            ok = self.grab().save(path)
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        if ok:
            self.log.push("ok", f"画面已导出：{path}")
        else:
            QMessageBox.warning(self, "导出失败", "图片写入失败，检查路径与权限")

    def _open_data_dir(self) -> None:
        from PyQt5.QtGui import QDesktopServices
        from PyQt5.QtCore import QUrl
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir)))

    def _export_ros_log(self) -> None:
        """E8：将当前帧与航迹快照导出为标准 Foxglove .mcap 或 ROS LaserScan/PointCloud2 JSON。"""
        import json as _json
        from ..core.ros_export import to_ros_laserscan, to_ros_pointcloud2, write_mcap_json_log

        frame = getattr(self, "_last_frame_ref", None)
        if frame is None:
            QMessageBox.information(self, "导出 ROS / Foxglove", "请先连接雷达或模拟器采集至少一帧数据后再导出。")
            return
        path, sel = QFileDialog.getSaveFileName(
            self, "导出 ROS / Foxglove 日志",
            str(self.data_dir / "radar_telemetry.mcap"),
            "Foxglove MCAP 日志 (*.mcap);;ROS LaserScan/PointCloud JSON (*.json)",
        )
        if not path:
            return
        p = Path(path)
        tracks = self.tracker.tracks
        if p.suffix.lower() == ".json" or "JSON" in sel:
            payload = {
                "laserscan": to_ros_laserscan(frame, threshold=self._thresh),
                "pointcloud2": to_ros_pointcloud2(frame, tracks=tracks),
            }
            p.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self.log.push("ok", f"已导出 ROS JSON：{p.name}")
        else:
            msg = to_ros_laserscan(frame, threshold=self._thresh)
            n = write_mcap_json_log(p, [msg])
            self.log.push("ok", f"已导出 Foxglove MCAP 日志：{p.name}（{n} 帧）")



class AlarmBarController:
    """见模块说明。方法在运行时由 MainWindow 实例调用。"""

    def _on_alarm_events(self, events) -> None:
        worst = "info"
        for ev in events:
            if ev.active:
                self.log.push(ev.level, f"[告警] {ev.name}：{ev.message}")
                self.store.record_event(ev.level, "alarm", ev.message, ev.rule_id, True, ev.tid)
                if ev.level_rank > 2:
                    worst = "crit"
                elif ev.level_rank == 1 and worst != "crit":
                    worst = "warn"
            else:
                self.log.push("info", f"[恢复] {ev.name}")
                self.store.record_event("info", "alarm-clear", ev.message, ev.rule_id, False, ev.tid)
        if worst == "crit" and not self._muted:
            from PyQt5.QtWidgets import QApplication
            QApplication.beep()
        self._refresh_alarm_bar()

    def _alarm_style(self, level: str) -> str:
        table = ALARM_STYLE if is_dark_theme(self._theme) else ALARM_STYLE_LIGHT
        return table.get(level, table["warn"])

    def _refresh_alarm_bar(self) -> None:
        act = self.alarm.active_events()
        if not act:
            self.alarm_bar.setStyleSheet(self._alarm_style("ok"))
            self.alarm_bar.setText("　系统正常")
            return
        worst = max(act, key=lambda e: e.level_rank)
        self.alarm_bar.setStyleSheet(self._alarm_style(worst.level))
        self.alarm_bar.setText("　!　" + "　｜　".join(e.name for e in act))
        self.alarm_bar.setVisible(True)

    # ================================================================== #
    # 杂项
    # ================================================================== #
    # 规则 / 插件 / 传感器配置
    # ================================================================== #

    def _load_rules(self) -> None:
        """规则优先级：内置默认 < 插件 < 用户手动编辑。"""
        user_rules = []
        p = self._user_rules_path()
        if p.exists():
            try:
                user_rules = rules_from_json(p.read_text(encoding="utf-8"))
            except Exception as exc:
                LOG.warning("rules.json 解析失败，改用内置规则：%s", exc)
        merged = merge_rules(DEFAULT_RULES, self.plugins.plugin_rules(), user_rules)
        self.alarm.set_rules(merged)
        self._sync_zones()

    def _user_rules_path(self) -> Path:
        return getattr(self.alarm, "save_path", None) or (self.data_dir / "rules.json")

    def _save_zone_rule(self, x: float, y: float, r: float) -> None:
        """把屏上画出来的圈存成一条 zone 规则并立即生效。

        这是"普通人能用"的关键一步：不用去翻 JSON，画一个圈就生效。
        存进 rules.json 后 _load_rules() 重新合并，界面和检测同时更新。
        """
        import json

        p = self._user_rules_path()
        data = {"rules": []}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or "rules" not in data:
                    data = {"rules": []}
            except Exception:
                data = {"rules": []}
        n = sum(1 for r in data["rules"] if str(r.get("kind")) == "zone") + 1
        data["rules"].append({
            "id": f"zone_user_{int(time.time())}",
            "name": f"警戒区 {n}",
            "kind": "zone",
            "level": "warn",
            "enabled": True,
            "params": {"cx": round(x, 3), "cy": round(y, 3), "r": round(r, 3)},
        })
        try:
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self._load_rules()
        if hasattr(self, "_set_ppi_flag"):
            self._set_ppi_flag("show_zones", True)
        self.log.push("ok", f"已添加警戒区 ({x:.1f}, {y:.1f}) r={r:.1f}m")

    def _clear_user_zones(self) -> None:
        """清空所有界面自绘的警戒区（zone_user_*），保留其他规则。"""
        import json

        p = self._user_rules_path()
        if not p.exists():
            self.log.push("info", "没有自绘警戒区可清除")
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        rules = data.get("rules", [])
        kept = [r for r in rules if not str(r.get("id", "")).startswith("zone_user_")]
        if len(kept) == len(rules):
            self.log.push("info", "没有自绘警戒区可清除")
            return
        data["rules"] = kept
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._load_rules()
        self.log.push("ok", f"已清除 {len(rules) - len(kept)} 个自绘警戒区")

    def _read_user_rules_doc(self) -> dict:
        import json

        p = self._user_rules_path()
        if not p.exists():
            return {"rules": []}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("rules"), list):
                return data
        except Exception:
            pass
        return {"rules": []}

    def _write_user_rules_doc(self, data: dict) -> None:
        import json

        p = self._user_rules_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _delete_zone(self, rule_id: str) -> None:
        """删除或停用用户点中的那个警戒区（内置与自绘均可单个删除）。"""
        cur_rule = next((r for r in getattr(self.alarm, "rules", [])
                         if str(r.id) == rule_id), None)
        name = cur_rule.name if cur_rule else rule_id
        if QMessageBox.question(
                self, "删除警戒区", f"确定删除「{name}」？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return

        data = self._read_user_rules_doc()
        rules = data["rules"]
        builtin_ids = {r.id for r in DEFAULT_RULES} | {r.id for r in self.plugins.plugin_rules()}

        if rule_id in builtin_ids:
            # 内置/插件规则用 enabled=False 覆盖写入 rules.json，避免 merge_rules 重建复活
            kept = [r for r in rules if str(r.get("id")) != rule_id]
            entry = cur_rule.to_dict() if cur_rule else {
                "id": rule_id, "name": name, "kind": "zone", "params": {"cx": 0, "cy": 3, "r": 1.5}
            }
            entry["enabled"] = False
            kept.append(entry)
            data["rules"] = kept
        else:
            data["rules"] = [r for r in rules if str(r.get("id")) != rule_id]

        self._write_user_rules_doc(data)
        self._load_rules()
        self.log.push("info", f"已删除警戒区「{name}」")

    def _upsert_user_rule(self, rule_id: str, mutator) -> AlarmRule | None:
        """公共辅助：按 rule_id 读取、编辑或覆写 rules.json 后重加载规则并同步画面。"""
        cur_rule = next((x for x in getattr(self.alarm, "rules", []) if str(x.id) == rule_id), None)
        if cur_rule is None:
            return None
        data = self._read_user_rules_doc()
        rules = data["rules"]
        entry = None
        for item in rules:
            if str(item.get("id")) == rule_id:
                entry = dict(item)
                break
        if entry is None:
            entry = cur_rule.to_dict()
        mutator(entry, cur_rule)
        data["rules"] = [r for r in rules if str(r.get("id")) != rule_id] + [entry]
        self._write_user_rules_doc(data)
        self._load_rules()
        return cur_rule

    def _update_zone_geometry(self, rule_id: str, cx: float, cy: float, r: float) -> None:
        """在 PPI 上拖拽移动或调整半径后，将新位置/半径写回 rules.json。"""
        def _mut(entry: dict, _cur) -> None:
            entry["params"] = {"cx": round(cx, 3), "cy": round(cy, 3), "r": round(max(0.2, r), 3)}
            entry["enabled"] = True

        cur = self._upsert_user_rule(rule_id, _mut)
        if cur is not None:
            self.log.push("info", f"已更新「{cur.name}」({cx:.1f}, {cy:.1f}) r={r:.1f}m")

    def _update_poly_zone_geometry(self, rule_id: str, points: list) -> None:
        """在 PPI 上直接拖拽多边形顶点后写回 rules.json。"""
        clean_pts = [[round(float(x), 3), round(float(y), 3)] for x, y in points]
        if len(clean_pts) < 3:
            return

        def _mut(entry: dict, _cur) -> None:
            entry["params"] = {"points": clean_pts}
            entry["enabled"] = True

        cur = self._upsert_user_rule(rule_id, _mut)
        if cur is not None:
            self.log.push("info", f"已更新多边形防区「{cur.name}」({len(clean_pts)} 顶点)")

    def _toggle_zone_enabled(self, rule_id: str, enabled: bool) -> None:
        def _mut(entry: dict, _cur) -> None:
            entry["enabled"] = bool(enabled)

        cur = self._upsert_user_rule(rule_id, _mut)
        if cur is not None:
            if enabled and hasattr(self, "_set_ppi_flag"):
                self._set_ppi_flag("show_zones", True)
            self.log.push("info", f"{'启用' if enabled else '停用'}防区「{cur.name}」")

    def _edit_zone(self, rule_id: str) -> None:
        """打开单个警戒区的友好参数编辑弹窗（名称 / 级别 / 坐标 / 半径）。"""
        from .rulesdialog import ZoneEditDialog

        cur_rule = next((x for x in getattr(self.alarm, "rules", []) if str(x.id) == rule_id), None)
        if cur_rule is None:
            return
        dlg = ZoneEditDialog(cur_rule, self)
        if dlg.exec_() != QDialog.Accepted:
            return
        vals = dlg.values()
        data = self._read_user_rules_doc()
        rules = data["rules"]
        entry = cur_rule.to_dict()
        entry["name"] = vals["name"]
        entry["level"] = vals["level"]
        entry["params"] = {"cx": vals["cx"], "cy": vals["cy"], "r": vals["r"]}
        entry["enabled"] = True
        data["rules"] = [r for r in rules if str(r.get("id")) != rule_id] + [entry]
        self._write_user_rules_doc(data)
        self._load_rules()
        self.log.push("ok", f"已保存警戒区「{vals['name']}」")

    def _delete_last_zone(self) -> None:
        """删掉最后一个自绘的警戒区（id 以 zone_user_ 开头）。

        原来右键一下就删，手一抖警戒区就没了 —— 删除必须二次确认。
        """
        if QMessageBox.question(
                self, "删除警戒区",
                "确定删除最近添加的那个警戒区？\n（内置规则不受影响）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        import json

        p = self._user_rules_path()
        if not p.exists():
            self.log.push("info", "没有自绘的警戒区可删")
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        rules = data.get("rules", [])
        idx = [i for i, r in enumerate(rules) if str(r.get("id", "")).startswith("zone_user_")]
        if not idx:
            self.log.push("info", "没有自绘的警戒区可删")
            return
        rules.pop(idx[-1])
        data["rules"] = rules
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._load_rules()
        self.log.push("info", "已删除最近添加的警戒区")

    def _sync_zones(self) -> None:
        """把 zone 类规则同步到 PPI 上画出来。

        规则里写了"圆心 (0,3) 半径 1.5"却看不见它 —— 圈住了什么东西只能靠想象。
        画出来才算把配置和画面接上。
        """
        zs: list[tuple] = []
        polys: list[tuple] = []
        for r in getattr(self.alarm, "rules", []):
            if not getattr(r, "enabled", True):
                continue
            p = r.params or {}
            if r.kind == "zone":
                try:
                    zs.append((float(p.get("cx", 0.0)), float(p.get("cy", 0.0)),
                               float(p.get("r", 1.0)), str(r.level), str(r.id)))
                except (TypeError, ValueError):
                    continue
            elif r.kind == "poly_zone":
                pts = [(float(pt[0]), float(pt[1])) for pt in (p.get("points") or []) if len(pt) >= 2]
                if len(pts) >= 3:
                    polys.append((pts, str(r.level), str(r.id)))
        # _load_rules() 在界面构造之前就会跑一次，那时还没有 ppi
        for name in ("ppi", "ppi2"):
            w = getattr(self, name, None)
            if w is not None:
                w.set_alarm_zones(zs, polys)
        zp = getattr(self, "zones_panel", None)
        if zp is not None:
            zp.refresh_zones(list(getattr(self.alarm, "rules", [])))

    def _open_rules(self) -> None:
        dlg = RulesDialog(self.alarm, self)
        dlg.exec_()
        self._load_rules()          # 编辑器可能已经写盘，重新读一遍保持单一来源
        self._log_level("info", f"告警规则已更新，共 {len(self.alarm.rules)} 条")
