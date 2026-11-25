"""主窗口：把所有部件接起来。

这一层只做"编排"，不含协议也不含算法。换雷达不动这里；
雷达多出什么能力（RD 图、IFF、威胁分级），界面自动跟着变。
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
from .theme import (DEFAULT_PALETTE, DEFAULT_THEME, PALETTES, THEMES,
                    build_stylesheet)
from .tracktable import TrackTable
from .extra import AisDialog, ControlDialog
from .waterfall import WaterfallWidget
from .winstyle import enable_dark_titlebar, install_dialog_titlebar, make_app_icon

LOG = logging.getLogger("radarscope")

PRESET_ENDPOINTS = [
    "tcp://192.168.4.1:8080",
    "udp://0.0.0.0:9000",
    "serial://COM3?baud=256000",
    "ble://",
]

#: 深色主题下的告警条配色
ALARM_STYLE = {
    "crit": "background:#7A1B14;color:#FFE3DA;border-radius:4px;padding:4px 10px;font-size:13px;",
    "warn": "background:#5E4210;color:#FFEDCC;border-radius:4px;padding:4px 10px;font-size:13px;",
    "info": "background:#1B3324;color:#CFEAD9;border-radius:4px;padding:4px 10px;font-size:13px;",
    # 无告警时的常态：刻意做得很淡，不该抢注意力
    "ok": "background:#11170F;color:#6C8375;border:1px solid #1E2A22;"
          "border-radius:4px;padding:4px 10px;font-size:12px;",
}

#: 只有这一个色板是"白底深回波"，专供浅色主题
LIGHT_PALETTES = {"daylight"}


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
    frameReady = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, driver, parent=None):
        super().__init__(parent)
        self.driver = driver
        self._alive = True

    def stop(self) -> None:
        self._alive = False
        self.wait(2000)

    def run(self) -> None:
        while self._alive:
            try:
                frame = self.driver.read()
            except Exception as exc:                 # 串口拔出、网络断开
                self.failed.emit(f"{type(exc).__name__}: {exc}")
                return
            if frame is not None:
                self.frameReady.emit(frame)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("瞭望 Lookout · 通用雷达显控台")
        self.resize(1340, 880)

        self.settings = QSettings("RadarScope", "Console")
        self.data_dir = paths.data_dir()
        self.store = Store(self.data_dir / "radarscope.db")

        # 插件先加载，驱动注册表里才会有插件带来的雷达
        self.plugins = PluginManager(paths.plugins_dir())
        self.plugins.load_all()

        self.driver = None
        self.reader: ReaderThread | None = None
        self.caps = Capabilities()
        self.tracker = Tracker()
        self.alarm = AlarmEngine()
        self.alarm.save_path = self.data_dir / "rules.json"
        self._load_rules()
        self.archive: FrameArchive | None = None

        self._thresh = 0.30
        self._last_ts = 0.0
        self._last_frame_mono = 0.0
        self._locked_id = -1
        self._frames = 0
        self._fps_t = time.monotonic()
        self._fps = 0.0
        self._table_t = 0.0
        self._reconnect_try = 0
        self._want_connect = False
        self._paused = False
        self._font_scale = 1.0
        self._theme = str(self.settings.value("theme", DEFAULT_THEME))
        # 数据转发（UDP）
        self.forwarder = Forwarder()
        self._cpa: list = []
        self._muted = False
        self._rec_dir: Path | None = None
        self._rec_n = 0
        self._rec_t = 0.0
        # AIS
        self.ais = AisFeed()
        self._ais_ref = (39.9, 116.4)
        # 联网地图（懒创建，只在开启时建）
        self.map_layer: MapLayer | None = None
        self._map_t = 0.0
        # 真运动：本舰累计位移与上一帧时间
        self._own_x = 0.0
        self._own_y = 0.0
        self._tm_t = 0.0

        self._build_ui()
        self._restore()

    # ================================================================== #
    # 界面构建
    # ================================================================== #
    def _build_ui(self) -> None:
        self.ppi = PPIWidget()
        self.ppi.targetPicked.connect(self._on_target_picked)
        self.ascope = AScopeWidget()
        self.rdscope = RDScopeWidget()
        self.waterfall = WaterfallWidget()
        self.table = TrackTable()
        self.log = EventLog()
        self.history = HistoryPanel(self.store)
        self.history.replayRequested.connect(self._start_replay)
        self.sensors_panel = SensorPanel()
        self.sensors_panel.btn_cfg.clicked.connect(self._open_sensor_config)
        self.ppi.zoneDrawn.connect(self._save_zone_rule)
        self.ppi.zoneDeleted.connect(self._delete_last_zone)

        # 报警条常驻、固定高度。
        # 原来是隐藏/显示切换，报警一来就把下面整个画布挤上去 ——
        # 画面每报一次警就抖动一次，非常影响盯着看。常驻 + 换文案就不会重排。
        self.alarm_bar = QLabel("")
        self.alarm_bar.setFixedHeight(26)
        self.alarm_bar.setText("　系统正常")
        self.alarm_bar.setStyleSheet(ALARM_STYLE["ok"])

        right = QWidget()
        right.setMinimumWidth(520)
        right.setMaximumWidth(680)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(8, 0, 0, 0)
        rv.setSpacing(8)
        rv.addWidget(self.ascope)
        rv.addWidget(self.rdscope)
        rv.addWidget(self.waterfall)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.table, "航迹")
        self.tabs.addTab(self.sensors_panel, "传感器")
        self.tabs.addTab(self.log, "事件")
        self.tabs.addTab(self.history, "历史")
        rv.addWidget(self.tabs, 1)

        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(8, 8, 8, 8)
        cv.setSpacing(6)
        cv.addWidget(self.alarm_bar)
        h = QHBoxLayout()
        h.setSpacing(8)
        h.addWidget(self.ppi, 1)
        h.addWidget(right, 0)
        cv.addLayout(h, 1)
        self.setCentralWidget(central)

        # 近距小量程窗：大范围看态势，小窗看细节，可以关掉
        self.ppi2 = PPIWidget()
        self.ppi2.setMinimumSize(260, 260)
        self.dock_near = QDockWidget("近距监视（2 m 量程）", self)
        self.dock_near.setWidget(self.ppi2)
        self.dock_near.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.dock_near)
        self.dock_near.hide()

        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)

        # 一级：数据从哪来（模拟 / 实测 / 融合 / 回放）。
        # 刻意按"来源"分，而不是把所有型号堆一个下拉框里 ——
        # 那样加十种模块之后，找一个设备要在几十条里翻。
        tb.addWidget(QLabel(" 数据源 "))
        self.src_combo = QComboBox()
        for key, text in (("sim", "模拟与演示"), ("device", "实测模块"),
                          ("fusion", "多设备融合"), ("replay", "数据回放")):
            self.src_combo.addItem(text, key)
        self.src_combo.currentIndexChanged.connect(self._on_source_changed)
        tb.addWidget(self.src_combo)

        # 二级：按"测量形态"分组，型号只作为名字显示。
        # 形态才是决定你怎么用它、软件怎么显示它的东西；
        # 型号天天出新，写进逻辑里就等于把软件钉死在某一年。
        tb.addWidget(QLabel("  类型 "))
        self.drv_combo = QComboBox()
        self.drv_combo.setMinimumWidth(220)
        self.drv_combo.currentIndexChanged.connect(self._on_driver_changed)
        tb.addWidget(self.drv_combo)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("旋转扫描", "rotating")
        self.mode_combo.addItem("凝视扇区", "staring")
        tb.addWidget(self.mode_combo)

        tb.addWidget(QLabel("  端点 "))
        self.endpoint_combo = QComboBox()
        self.endpoint_combo.setEditable(True)
        self.endpoint_combo.setMinimumWidth(280)
        self.endpoint_combo.setToolTip(
            "支持：serial://COM3?baud=256000 ｜ tcp://ip:port ｜ udp://0.0.0.0:port\n"
            "ble://地址 ｜ 文件回放：文件路径或 file://路径\n"
            "任何端点都可以加 &record=capture.bin 把原始字节流录下来")
        tb.addWidget(self.endpoint_combo)
        self.btn_link = QPushButton("连接设置")
        self.btn_link.setToolTip("连接设置：枚举串口（自动识别 CH340/CP210x）、"
                                 "TCP/UDP、蓝牙扫描、文件回放")
        self.btn_link.clicked.connect(self._open_link_dialog)
        tb.addWidget(self.btn_link)
        btn_scan = QPushButton("扫描")
        btn_scan.setToolTip("只刷新串口列表并填进端点框")
        btn_scan.clicked.connect(self._refresh_endpoints)
        tb.addWidget(btn_scan)

        self.btn_conn = QPushButton("连接")
        self.btn_conn.setCheckable(True)
        self.btn_conn.clicked.connect(self._toggle_connect)
        tb.addWidget(self.btn_conn)

        # 原本所有控件挤在一行，1366 宽的笔记本上后半截文字会被裁掉。
        # 拆成三行：数据 / 显示 / 功能。每行都留得下字，也不用记快捷键。
        self.addToolBarBreak()
        tb = self._new_toolbar("显示")

        tb.addWidget(QLabel(" 量程 "))
        self.range_spin = QDoubleSpinBox()
        self.range_spin.setRange(0.5, 100000.0)
        self.range_spin.setSingleStep(0.5)
        self.range_spin.setValue(6.0)
        self.range_spin.setSuffix(" m")
        self.range_spin.valueChanged.connect(self._on_range_changed)
        tb.addWidget(self.range_spin)

        self.thr_spin = QDoubleSpinBox()
        self.thr_spin.setRange(0.05, 0.95)
        self.thr_spin.setSingleStep(0.05)
        self.thr_spin.setValue(0.30)
        self.thr_spin.valueChanged.connect(self._on_threshold_changed)
        tb.addWidget(self.thr_spin)

        # 显示增益：对已解析的矩阵做后处理，不涉及设备控制。
        # 真实雷达的 STC/FTC 要在设备侧下发参数，那是另一件事。
        self.gain_spin = QDoubleSpinBox()
        self.gain_spin.setRange(0.2, 4.0)
        self.gain_spin.setSingleStep(0.1)
        self.gain_spin.setValue(1.0)
        self.gain_spin.setDecimals(1)
        self.gain_spin.setToolTip("显示增益：放大弱回波（只影响显示，不改变检测门限）")
        tb.addWidget(self.gain_spin)

        self.pal_combo = QComboBox()
        for key, pal in PALETTES.items():
            self.pal_combo.addItem(pal["label"], key)
        idx = self.pal_combo.findData(self.settings.value("palette", DEFAULT_PALETTE))
        if idx >= 0:
            self.pal_combo.setCurrentIndex(idx)
        self.pal_combo.currentIndexChanged.connect(self._on_palette_changed)
        tb.addWidget(self.pal_combo)

        # 朝向：车载/船载场景下"北在哪"很重要；固定雷达用艏向朝上即可
        self.btn_north = QPushButton("北向朝上")
        self.btn_north.setCheckable(True)
        self.btn_north.setToolTip("勾选后画面按航向旋转，让北始终朝上；不勾选则平台正前朝上")
        self.btn_north.toggled.connect(self._on_orientation_changed)
        tb.addWidget(self.btn_north)

        self.heading_spin = QDoubleSpinBox()
        self.heading_spin.setRange(0.0, 359.9)
        self.heading_spin.setDecimals(0)
        self.heading_spin.setSingleStep(5.0)
        self.heading_spin.setSuffix("°")
        self.heading_spin.setToolTip("平台航向（0=正北，顺时针）。接罗盘/IMU 后可由插件自动更新")
        self.heading_spin.valueChanged.connect(self._on_orientation_changed)
        tb.addWidget(self.heading_spin)

        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().Expanding, spacer.sizePolicy().Preferred)
        tb.addWidget(spacer)

        self.btn_lock = QPushButton("点目标锁定")
        self.btn_lock.setCheckable(True)
        self.btn_lock.setToolTip("开启后直接点击 PPI 上的目标即可锁定；点空白处解除")
        self.btn_lock.toggled.connect(self._on_lock_toggled)
        tb.addWidget(self.btn_lock)

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.0, 30.0)
        self.speed_spin.setSingleStep(0.5)
        self.speed_spin.setDecimals(1)
        self.speed_spin.setValue(0.0)
        self.speed_spin.setSuffix(" m/s")
        self.speed_spin.setToolTip("本舰/本车速度，用于 CPA 计算。0 表示静止")
        self.speed_spin.valueChanged.connect(lambda *_: self._update_cpa_label())
        tb.addWidget(self.speed_spin)

        self.btn_fwd = QPushButton("转发")
        self.btn_fwd.setCheckable(True)
        self.btn_fwd.setToolTip("把融合后的目标用 UDP 广播出去，别的程序可直接订阅")
        self.btn_fwd.toggled.connect(self._toggle_forward)
        tb.addWidget(self.btn_fwd)

        # 注意：这里必须叫 btn_clip。曾经和下面的「录制帧」都叫 btn_rec，
        # 后者把前者覆盖了 —— 点「录像」实际动的是「录制帧」。
        self.btn_clip = QPushButton("录像")
        self.btn_clip.setCheckable(True)
        self.btn_clip.setToolTip("按固定间隔把雷达画面存成 PNG 序列，用于复盘")
        self.btn_clip.toggled.connect(self._toggle_record)
        tb.addWidget(self.btn_clip)

        self.btn_mute = QPushButton("静音")
        self.btn_mute.setCheckable(True)
        self.btn_mute.setToolTip("临时屏蔽告警提示音（界面与事件仍然记录）")
        self.btn_mute.toggled.connect(self._toggle_mute)
        tb.addWidget(self.btn_mute)

        self.btn_zone = QPushButton("画警戒区")
        self.btn_zone.setCheckable(True)
        self.btn_zone.setToolTip("开启后在雷达图上按住左键拖出一个圆，松手即生成警戒区；"
                                 "右键删除最近添加的一个")
        self.btn_zone.toggled.connect(self.ppi.set_zone_mode)
        tb.addWidget(self.btn_zone)

        self.btn_true = QPushButton("真运动")
        self.btn_true.setCheckable(True)
        self.btn_true.setToolTip("关=相对运动（我永远在中心）；"
                                 "开=真运动（回波固定、本舰按航向航速移动）")
        self.btn_true.toggled.connect(self._toggle_true_motion)
        tb.addWidget(self.btn_true)



        self.btn_rec = QPushButton("录制帧")
        self.btn_rec.setCheckable(True)
        self.btn_rec.setToolTip("把强度矩阵写入 data/*.rad，之后可用回放驱动复现")
        self.btn_rec.toggled.connect(self._on_record_toggled)
        tb.addWidget(self.btn_rec)

        self.chk_reconnect = QPushButton("自动重连")
        self.chk_reconnect.setCheckable(True)
        self.chk_reconnect.setChecked(True)
        tb.addWidget(self.chk_reconnect)

        # 收敛工具栏：常用的留在外面，其余收进「更多设置」。
        # 之前三行二十多个控件铺开，一眼找不到重点 —— 工具应该按使用频率分层。
        panel = QWidget()
        form = QFormLayout(panel)
        form.setContentsMargins(14, 12, 14, 12)
        form.setSpacing(9)
        form.addRow("采集模式", self.mode_combo)
        form.addRow("检测门限", self.thr_spin)
        form.addRow("显示增益", self.gain_spin)
        form.addRow("配色方案", self.pal_combo)
        form.addRow("航向", self.heading_spin)
        form.addRow("航速", self.speed_spin)
        opts = QWidget()
        ol = QHBoxLayout(opts)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(6)
        for w in (self.btn_north, self.btn_rec, self.chk_reconnect):
            ol.addWidget(w)
        ol.addStretch(1)
        form.addRow("选项", opts)

        # 原来是"按钮 + 菜单 + QWidgetAction 面板"。Qt 的菜单里放交互控件
        # 必须给焦点，否则点不动 —— 用户反馈"是个摆设"就是这个原因。
        # 直接改成独立对话框，稳定、能点、还能记住位置。
        self._settings_dlg = QDialog(self)
        self._settings_dlg.setWindowTitle("设置")
        self._settings_dlg.resize(430, 400)
        _lay = QVBoxLayout(self._settings_dlg)
        _lay.setContentsMargins(16, 14, 16, 12)
        _lay.setSpacing(10)
        _lay.addWidget(panel)
        _lay.addStretch(1)
        _bb = QDialogButtonBox(QDialogButtonBox.Close)
        _bb.rejected.connect(self._settings_dlg.reject)
        _lay.addWidget(_bb)
        panel.setParent(self._settings_dlg)

        self.btn_settings = QPushButton("设置")
        self.btn_settings.setToolTip("采集模式 / 门限 / 增益 / 配色 / 航向 / 航速 / 录制帧 / 自动重连")
        self.btn_settings.clicked.connect(self._open_settings)
        tb.addWidget(self.btn_settings)

        # 菜单
        m_file = self.menuBar().addMenu("文件(&F)")
        m_file.addAction("导出事件日志…", self._export_log)
        m_file.addAction("导出当前画面…", self._export_screenshot)
        m_file.addAction("打开数据目录", self._open_data_dir)
        m_file.addSeparator()
        m_file.addAction("退出", self.close)
        m_tool = self.menuBar().addMenu("工具(&T)")
        m_tool.addAction("告警规则…", self._open_rules)
        m_tool.addAction("传感器配置…", self._open_sensor_config)
        m_tool.addAction("朝向标定…", self._open_calibration)
        m_tool.addAction("统计分析…", self._open_stats)
        m_tool.addAction("设备控制…", self._open_control)
        m_tool.addAction("AIS 接入…", self._open_ais)
        m_tool.addAction("插件管理…", self._open_plugins)
        m_help = self.menuBar().addMenu("帮助(&H)")
        m_help.addAction("操作说明", self._show_help)
        m_help.addAction("快捷键", self._show_shortcuts)

        # 视图：面板显隐。用户不该被固定布局绑死——投屏要大字、
        # 调试要全信息，两种场景需要不同的界面。
        m_view = self.menuBar().addMenu("视图(&V)")
        for text, w in (("A 显", self.ascope), ("瀑布图", self.waterfall),
                        ("距离-多普勒", self.rdscope), ("传感器页签", self.sensors_panel)):
            act = QAction(text, self)
            act.setCheckable(True)
            act.setChecked(True)
            act.toggled.connect(w.setVisible)
            m_view.addAction(act)
        m_view.addSeparator()
        for text, attr in (("尾迹", "show_trails"), ("告警禁入区", "show_zones"),
                           ("电子方位线 EBL/VRM", "show_ebl")):
            act = QAction(text, self)
            act.setCheckable(True)
            act.setChecked(True)
            setattr(self.ppi, attr, True)
            act.toggled.connect(lambda on, a=attr: (setattr(self.ppi, a, on),
                                                    self.ppi.update()))
            m_view.addAction(act)
        m_view.addAction("清除 EBL", lambda: self.ppi.set_ebl(None))
        m_view.addAction("清除测距读数", self.ppi.clear_probe)

        act_near = QAction("双量程窗口（近距）", self)
        act_near.setCheckable(True)
        act_near.toggled.connect(self._toggle_near)
        m_view.addAction(act_near)
        m_theme = m_view.addMenu("主题")
        grp = QActionGroup(self)
        grp.setExclusive(True)
        for key, th in THEMES.items():
            a = QAction(th["name"], self)
            a.setCheckable(True)
            a.setChecked(key == self._theme)
            a.triggered.connect(lambda _c, k=key: self._apply_theme(k))
            grp.addAction(a)
            m_theme.addAction(a)

        act_map = QAction("联网地图底图", self)
        act_map.setCheckable(True)
        act_map.toggled.connect(self._toggle_map)
        m_view.addAction(act_map)
        act_ais = QAction("显示 AIS 目标", self)
        act_ais.setCheckable(True)
        act_ais.setChecked(True)
        act_ais.toggled.connect(lambda on: (setattr(self.ppi, "show_ais", on),
                                            self.ppi.update()))
        m_view.addAction(act_ais)

        # 快捷键：高频动作不该去点按钮
        QShortcut(QKeySequence("Space"), self, self._toggle_pause)
        QShortcut(QKeySequence("L"), self, self.btn_lock.toggle)
        QShortcut(QKeySequence("Esc"), self, self._unlock_target)
        QShortcut(QKeySequence("+"), self, lambda: self._nudge_range(1))
        QShortcut(QKeySequence("-"), self, lambda: self._nudge_range(-1))
        # 字号：投屏和现场站着看需要更大的字，坐办公室则要更多信息
        QShortcut(QKeySequence("Ctrl+="), self, lambda: self._zoom(1))
        QShortcut(QKeySequence("Ctrl+-"), self, lambda: self._zoom(-1))
        QShortcut(QKeySequence("Ctrl+0"), self, lambda: self._zoom(0, reset=True))

        self.status_left = QLabel("未连接")
        self.status_right = QLabel("-- fps")
        self.status_cpa = QLabel("CPA --")
        self.status_cpa.setStyleSheet("color:#9FB2A6;")
        self.statusBar().addWidget(self.status_left, 1)
        self.statusBar().addPermanentWidget(self.status_cpa)
        self.statusBar().addPermanentWidget(self.status_right)

        self.log.push("info", "RadarScope 启动")
        self._rebuild_driver_combo()
        # 启动时就让色板与主题配对：白底挂深色色板会糊成一片
        cur = self.pal_combo.currentData()
        want = "daylight" if self._theme == "light" else None
        if want and cur not in LIGHT_PALETTES:
            i = self.pal_combo.findData(want)
            if i >= 0:
                self.pal_combo.setCurrentIndex(i)
        self._on_palette_changed()          # 让所有面板启动时配色一致
        self._sync_zones()                  # 界面就绪后把禁入区画上去
        # 窗口标题栏是 Windows 画的，PyQt 的样式表管不到，得单独调 DWM
        self.setWindowIcon(make_app_icon())
        # 对话框标题栏统一跟随主题（新增对话框不用再单独处理）
        self._dlg_filter = install_dialog_titlebar(lambda: self._theme)
        QTimer.singleShot(0, self._apply_native_style)

    # ================================================================== #
    # 设置持久化
    # ================================================================== #
    def _restore(self) -> None:
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        key = self.settings.value("palette", DEFAULT_PALETTE)
        idx = self.pal_combo.findData(key) if isinstance(key, str) else -1
        if idx >= 0:
            self.pal_combo.setCurrentIndex(idx)
        did = self.settings.value("driver", "", type=str)
        cls = available_drivers().get(did) if did else None
        if cls is not None:
            si = self.src_combo.findData(getattr(cls, "source", "device"))
            if si >= 0:
                self.src_combo.blockSignals(True)
                self.src_combo.setCurrentIndex(si)
                self.src_combo.blockSignals(False)
        self._rebuild_driver_combo(keep=did or None)
        self._refresh_endpoints()
        fs = float(self.settings.value("font_scale", 1.0) or 1.0)
        if 0.8 <= fs <= 1.8 and abs(fs - 1.0) > 1e-3:
            self._font_scale = fs
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(build_stylesheet(self._theme, fs))
        # _restore 会把上次的色板选回去，可能和当前主题不搭（白底配深色色板）。
        # 走一遍主题应用，把样式、标题栏、色板配对准。
        self._apply_theme(self._theme)

    def _save(self) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("palette", self.pal_combo.currentData())
        self.settings.setValue("driver", self.drv_combo.currentData())
        self.settings.setValue("endpoint", self.endpoint_combo.currentText())

    # ================================================================== #
    # 端点
    # ================================================================== #
    def _refresh_endpoints(self) -> None:
        cur = self.endpoint_combo.currentText()
        self.endpoint_combo.clear()
        items: list[str] = []
        try:
            from serial.tools import list_ports
            for p in list_ports.comports():
                items.append(f"serial://{p.device}?baud=256000")
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
        try:
            caps = cls().capabilities()
        except Exception:
            caps = Capabilities()
        caps.source = getattr(cls, "source", "device")
        caps.form = getattr(cls, "form", "sector")
        caps.vendor = getattr(cls, "vendor", "")
        self._apply_caps(caps)

    def _on_range_changed(self, v: float) -> None:
        self.caps.range_max = float(v)
        self.ppi.set_capabilities(self.caps)
        self.waterfall.set_range(self.caps.range_max)

    def _on_threshold_changed(self, v: float) -> None:
        self._thresh = float(v)
        self.ascope.set_threshold(self._thresh)

    def _on_palette_changed(self) -> None:
        key = self.pal_combo.currentData()
        self.ppi.set_palette(key)
        self.ascope.set_palette(key)
        self.waterfall.set_palette(key)
        self.rdscope.set_palette(key)

    def _on_orientation_changed(self, *_args) -> None:
        self.ppi.set_orientation(self.btn_north.isChecked(), self.heading_spin.value())

    def _on_lock_toggled(self, on: bool) -> None:
        if not on:
            self._locked_id = -1
            self.table.set_locked(-1)

    def _on_target_picked(self, tid: int) -> None:
        if not self.btn_lock.isChecked():
            return
        self._locked_id = tid
        self.table.set_locked(tid)
        if tid < 0:
            self.log.push("info", "解除目标锁定")
        else:
            self.log.push("ok", f"锁定 T{tid}")
            self.store.record_event("info", "lock", f"锁定 T{tid}", tid=tid)

    # ================================================================== #
    # 能力应用
    # ================================================================== #
    def _apply_caps(self, caps: Capabilities, reset_range: bool = True) -> None:
        self.caps = caps
        if reset_range:
            self.range_spin.blockSignals(True)
            self.range_spin.setDecimals(0 if caps.range_max >= 1000 else 1)
            self.range_spin.setSingleStep(1000.0 if caps.range_max >= 1000 else 0.5)
            self.range_spin.setValue(caps.range_max)
            self.range_spin.blockSignals(False)
            self.caps.range_max = float(self.range_spin.value())
        self.ppi.set_capabilities(self.caps)
        self.ascope.set_capabilities(self.caps)
        self.ascope.set_threshold(self._thresh)
        self.waterfall.set_range(self.caps.range_max)
        self.waterfall.reset()
        self.rdscope.reset()
        # RD 面板只在雷达真的给得出来的时候才占位置
        self.rdscope.setVisible(caps.has_range_doppler)
        self.table.configure(self.caps)
        self.tracker.reset()
        self.alarm.reset()
        self._locked_id = -1
        self.status_left.setText(f"{caps.name} ｜ {caps.summary()} ｜ {caps.notes}")

    # ================================================================== #
    # 连接 / 断开
    # ================================================================== #
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
        caps = drv.capabilities()
        # 分类属性挂在驱动类上，capabilities() 里不一定重复声明，
        # 这里统一补进去：否则界面按 form 判断的列/面板（比如"共视"）会失效
        caps.source = getattr(type(drv), "source", caps.source)
        caps.form = getattr(type(drv), "form", caps.form)
        caps.vendor = getattr(type(drv), "vendor", caps.vendor)
        self._apply_caps(caps)
        self._reconnect_try = 0

        self.store.start_session(did, endpoint, caps)
        if self.btn_rec.isChecked():
            self._open_archive()

        self._last_ts = 0.0
        self._last_frame_mono = time.monotonic()
        self._frames = 0
        self._fps_t = time.monotonic()

        self.reader = ReaderThread(self.driver, self)
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

    def _open_archive(self) -> None:
        name = time.strftime("%Y%m%d-%H%M%S") + ".rad"
        self.archive = FrameArchive(self.data_dir / name)
        self.log.push("info", f"开始录制帧：{name}")

    # ================================================================== #
    # 帧处理
    # ================================================================== #
    def _on_frame(self, frame) -> None:
        if self._paused:
            return                       # 暂停时画面冻住，方便截图或细看
        try:
            f = pipeline.normalize(frame, self.caps, threshold=self._thresh)
        except Exception as exc:
            self._log_level("warn", f"数据解析异常（已跳过该帧）：{exc}")
            return

        now = frame.timestamp or time.monotonic()
        dt = (now - self._last_ts) if self._last_ts else 0.1
        self._last_ts = now
        self._last_frame_mono = time.monotonic()

        period = f.meta.get("scan_period")
        if period:
            self.tracker.max_missed_s = max(2.5, 1.6 * float(period))
            self.table.set_period(float(period))

        tracks = self.tracker.update(f.targets, dt, -1)

        # ---- 锁定 ----
        if self.btn_lock.isChecked() and self._locked_id < 0:
            alive = [t for t in tracks if t.missed <= 1e-3]
            if alive:
                self._locked_id = min(alive, key=lambda t: t.rng).tid
        for t in tracks:
            t.locked = (t.tid == self._locked_id)

        # ---- 告警 ----
        silence = time.monotonic() - self._last_frame_mono
        events = self.alarm.evaluate(tracks, self.caps, silence_s=silence)
        if events:
            self._on_alarm_events(events)

        all_tracks = self.tracker.tracks

        # ---- 显示增益：只放大显示，不动检测门限 ----
        g = float(self.gain_spin.value())
        if abs(g - 1.0) > 1e-3 and f.scan is not None:
            f.scan = np.clip(f.scan * g, 0.0, 1.0)

        # ---- CPA/TCPA：防撞用的最近会遇距离与时间 ----
        self._cpa = cpa.evaluate(all_tracks, float(self.speed_spin.value()),
                                 float(self.heading_spin.value()))
        self._update_cpa_label()

        # ---- 对外转发（按速率限制，内部自己判） ----
        if self.forwarder.enabled:
            self.forwarder.send(f, all_tracks)

        self.ppi.push_frame(f, all_tracks)
        self._maybe_record(now)

        # 真运动：按航向航速积分本舰位移，画面整体反向平移
        if self.ppi.true_motion:
            dt = (now - self._tm_t) if self._tm_t else 0.0
            self._tm_t = now
            if 0.0 < dt < 0.5:
                vx, vy = cpa.own_velocity(float(self.speed_spin.value()),
                                          float(self.heading_spin.value()))
                self._own_x += vx * dt
                self._own_y += vy * dt
                self.ppi.set_origin(self._own_x, self._own_y)

        # 地图：1 秒检查一次级别/中心是否要换，避免每帧都算
        if (self.map_layer is not None and self.map_layer.enabled
                and now - self._map_t > 1.0):
            self._map_t = now
            self.map_layer.update_for(self._ais_ref[0], self._ais_ref[1],
                                      float(self.range_spin.value()),
                                      max(200, self.ppi.width()))

        # AIS 叠加
        if self.ais.running:
            self.ppi.set_ais(self.ais.snapshot(*self._ais_ref))

        # 近距小量程窗
        if self.dock_near.isVisible():
            self.ppi2.push_frame(f, all_tracks)
        self.ascope.push_frame(f)
        self.waterfall.push_frame(f)
        if self.caps.has_range_doppler:
            self.rdscope.push_frame(f)

        # ---- 落库 ----
        if self.store.session_id is not None:
            self.store.record_tracks(time.time(), all_tracks)
        if self.archive is not None and f.has_scan:
            self.archive.append(f)

        # ---- 刷新率 ----
        self._frames += 1
        el = time.monotonic() - self._fps_t
        if el >= 1.0:
            self._fps = self._frames / el
            self._frames = 0
            self._fps_t = time.monotonic()
            # 好帧/坏帧：借 STM32 radar host 的做法 —— 接线接反或波特率不对时，
            # 帧率看着正常但坏帧会一路涨，这是最快的定位线索。
            meta = frame.meta or {}
            txt = f"{self._fps:.1f} fps"
            if "good" in meta or "bad" in meta:
                good, bad = int(meta.get("good", 0)), int(meta.get("bad", 0))
                txt += f" ｜ 好帧 {good} 坏帧 {bad}"
                if bad > good:
                    txt += "  ⚠ 坏帧偏多，检查波特率/接线"
            self.status_right.setText(txt)

        if now - self._table_t > 0.15:
            self._table_t = now
            self.table.set_locked(self._locked_id)
            self.table.refresh(all_tracks)
            sensors = f.meta.get("sensors")
            if not sensors and self.caps.name:
                # 单传感器驱动（相控阵/激光/毫米波）不会回报"传感器组"，
                # 面板就空着，看起来像坏了。这里合成一行本机信息。
                sensors = [{
                    "name": self.caps.name,
                    "bearing": 0.0,
                    "fov": float(self.caps.fov_deg or 360.0),
                    "endpoint": self.endpoint_combo.currentText(),
                    "online": True,
                    "fps": self._fps,
                    "frames": self.caps.__dict__.get("frames", 0),
                    "errors": 0,
                }]
            if sensors:
                self.sensors_panel.update_sensors(sensors, f.meta.get("gaps"))
                online = sum(1 for s in sensors if s.get("online"))
                self.tabs.setTabText(1, f"传感器 {online}/{len(sensors)}")
            meta = f.meta or {}
            bits = []
            if f.source:
                bits.append(f.source)
            if "state" in meta:
                bits.append(f"状态 {meta['state']}")
            if "detect_m" in meta:
                bits.append(f"探测 {meta['detect_m']:.2f} m")
            bits.append(f"目标 {len(all_tracks)}")
            if self.driver is not None:
                bits.append(self.driver.describe())
            self.status_left.setText(" ｜ ".join(bits))

    # ================================================================== #
    # 告警
    # ================================================================== #
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
        table = ALARM_STYLE_LIGHT if self._theme == "light" else ALARM_STYLE
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
        p = self.data_dir / "rules.json"
        if p.exists():
            try:
                user_rules = rules_from_json(p.read_text(encoding="utf-8"))
            except Exception as exc:
                LOG.warning("rules.json 解析失败，改用内置规则：%s", exc)
        merged = merge_rules(DEFAULT_RULES, self.plugins.plugin_rules(), user_rules)
        self.alarm.set_rules(merged)
        self._sync_zones()

    def _user_rules_path(self) -> Path:
        return self.data_dir / "rules.json"

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
        self.log.push("ok", f"已添加警戒区 ({x:.1f}, {y:.1f}) r={r:.1f}m")

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
        zs: list[tuple[float, float, float, str]] = []
        for r in getattr(self.alarm, "rules", []):
            if r.kind != "zone" or not getattr(r, "enabled", True):
                continue
            p = r.params or {}
            try:
                zs.append((float(p.get("cx", 0.0)), float(p.get("cy", 0.0)),
                           float(p.get("r", 1.0)), str(r.level)))
            except (TypeError, ValueError):
                continue
        # _load_rules() 在界面构造之前就会跑一次，那时还没有 ppi
        ppi = getattr(self, "ppi", None)
        if ppi is not None:
            ppi.set_alarm_zones(zs)

    def _open_rules(self) -> None:
        dlg = RulesDialog(self.alarm, self)
        dlg.exec_()
        self._load_rules()          # 编辑器可能已经写盘，重新读一遍保持单一来源
        self._log_level("info", f"告警规则已更新，共 {len(self.alarm.rules)} 条")

    def _open_plugins(self) -> None:
        dlg = PluginsDialog(self.plugins, self)
        dlg.reloaded.connect(self._on_plugins_reloaded)
        dlg.exec_()

    def _on_plugins_reloaded(self) -> None:
        self._rebuild_driver_combo()
        self._load_rules()
        self._log_level("ok", f"插件已重新加载：{len(self.plugins.plugins)} 个")

    def _open_sensor_config(self) -> None:
        dlg = SensorsDialog(self.data_dir / "sensors.json", self)
        dlg.exec_()

    def _open_calibration(self) -> None:
        engine = None
        if self.driver is not None and hasattr(self.driver, "fusion_engine"):
            engine = self.driver.fusion_engine()
        if engine is None:
            QMessageBox.information(
                self, "朝向标定",
                "标定需要「多设备融合」类雷达在运行：\n"
                "  · 多站融合 · 6×60° 全向（可以先拿它跑一遍流程）\n"
                "  · 多站融合 · 真实雷达组")
            return
        dlg = CalibrationWizard(
            engine.specs(), engine.latest_observations,
            self.data_dir / "sensors.json", self)
        dlg.exec_()

    # ================================================================== #
    # 交互：暂停 / 锁定 / 量程 / 回放
    # ================================================================== #
    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        if self._paused:
            self.log.push("info", "画面已暂停（空格继续）")
            self.status_right.setText("已暂停")
        else:
            self.log.push("info", "画面恢复")
            self.status_right.setText("-- fps")

    def _unlock_target(self) -> None:
        self.btn_lock.setChecked(False)
        self._locked_id = -1
        self.table.set_locked(-1)
        self.ppi.clear_probe()

    def _nudge_range(self, direction: int) -> None:
        """加减号调量程。步长跟着当前量程走，小量程时 0.5 m，大量程时 1 km。"""
        big = self.range_spin.value() >= 1000.0
        step = (1000.0 if big else 0.5) * (1 if direction > 0 else -1)
        self.range_spin.setValue(self.range_spin.value() + step)

    # ================================================================== #
    # 对外输出：数据转发 / 录像 / 静音 / CPA
    # ================================================================== #
    def _new_toolbar(self, name: str) -> QToolBar:
        t = QToolBar(name)
        t.setMovable(False)
        t.setFloatable(False)
        self.addToolBar(t)
        return t

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
        self.settings.setValue("fwd_addr", addr.strip())
        self.forwarder.configure(host or "127.0.0.1", int(port), rate=10.0)
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

    def _update_cpa_label(self) -> None:
        w = cpa.worst(self._cpa) if self._cpa else None
        if w is None:
            self.status_cpa.setText("CPA --  ")
            self.status_cpa.setStyleSheet("color:#6C8375;padding-right:10px;")
            return
        self.status_cpa.setText(f"CPA {w.cpa:.1f}m / {w.tcpa:.0f}s  ")
        near = w.cpa < 1.5 and w.tcpa < 30
        self.status_cpa.setStyleSheet(
            "color:#FF8A73;font-weight:bold;padding-right:10px;" if near
            else "color:#9FB2A6;padding-right:10px;")

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

    def _open_settings(self) -> None:
        self._settings_dlg.show()
        self._settings_dlg.raise_()
        self._settings_dlg.activateWindow()

    def _apply_theme(self, mode: str) -> None:
        """切换浅色/深色。样式表、标题栏、雷达色板一起换。"""
        self._theme = mode if mode in THEMES else DEFAULT_THEME
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(self._theme, self._font_scale))
        self.settings.setValue("theme", self._theme)

        # 白底上挂深色色板会糊成一片，反之亦然 —— 主题切换时顺手配对色板
        cur = self.pal_combo.currentData()
        if self._theme == "light" and cur not in LIGHT_PALETTES:
            i = self.pal_combo.findData("daylight")
            if i >= 0:
                self.pal_combo.setCurrentIndex(i)
        elif self._theme == "dark" and cur in LIGHT_PALETTES:
            i = self.pal_combo.findData("ice")
            if i >= 0:
                self.pal_combo.setCurrentIndex(i)

        enable_dark_titlebar(int(self.winId()), self._theme == "dark")
        self._refresh_alarm_bar()
        self.log.push("info", f"主题：{THEMES[self._theme]['name']}")

    def _toggle_map(self, on: bool) -> None:
        """联网地图：按当前量程自动选瓦片级别，拿"本船经纬度"当中心。"""
        if on:
            if self.map_layer is None:
                self.map_layer = MapLayer(paths.data_dir() / "tiles", self)
                self.map_layer.ready.connect(self.ppi.update)
            self.map_layer.enabled = True
            self.ppi.map_layer = self.map_layer
            lat, lon = self._ais_ref
            self.map_layer.request(lat, lon, float(self.range_spin.value()),
                                   max(200, self.ppi.width()))
            self.log.push("ok", f"联网地图已开启（中心 {lat:.5f}, {lon:.5f}）；"
                                "换位置请到「工具→AIS 接入」改经纬度")
        elif self.map_layer is not None:
            self.map_layer.enabled = False
        self.ppi.update()

    def _toggle_true_motion(self, on: bool) -> None:
        self.ppi.true_motion = bool(on)
        if not on:
            self._own_x = self._own_y = 0.0
            self.ppi.set_origin(0.0, 0.0)
        self._tm_t = 0.0
        self.ppi.update()
        self.log.push("info", "真运动显示" if on else "相对运动显示")

    def _toggle_near(self, on: bool) -> None:
        self.dock_near.setVisible(on)
        if on:
            import copy
            caps = copy.copy(self.caps)
            caps.range_max = max(0.5, min(2.0, self.caps.range_max))
            caps.name = f"近距 {caps.range_max:.1f} m"
            self.ppi2.set_capabilities(caps)

    def _open_control(self) -> None:
        drv = self.driver
        supported = bool(drv is not None and getattr(drv, "supports_control", False))
        dlg = ControlDialog(supported, drv.control_state() if drv else {}, self)
        if dlg.exec_() != QDialog.Accepted or drv is None:
            return
        ok = drv.set_control(**dlg.values())
        if ok:
            self.log.push("ok", f"设备参数已下发：{dlg.values()}")
        else:
            self.log.push("warn", "设备拒绝了下发（驱动未实现 set_control）")

    def _open_ais(self) -> None:
        dlg = AisDialog(self.ais.port, self._ais_ref[0], self._ais_ref[1],
                        self.ais.running, self)
        if dlg.exec_() != QDialog.Accepted:
            return
        port, lat, lon, on = dlg.values()
        self._ais_ref = (lat, lon)
        self.settings.setValue("ais_port", port)
        self.settings.setValue("ais_lat", lat)
        self.settings.setValue("ais_lon", lon)
        if on:
            err = self.ais.start(port)
            if err:
                QMessageBox.warning(self, "AIS", err)
                return
            self.log.push("ok", f"AIS 已接入，监听 UDP {port}")
        else:
            self.ais.stop()
            self.ppi.set_ais([])
            self.log.push("info", "AIS 已停止")

    def _apply_native_style(self) -> None:
        if enable_dark_titlebar(int(self.winId()), self._theme == "dark"):
            LOG.debug("深色标题栏已启用")

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        QTimer.singleShot(0, self._apply_native_style)

    def _open_link_dialog(self) -> None:
        dlg = LinkDialog(self.endpoint_combo.currentText().strip(), self)
        if dlg.exec_() == QDialog.Accepted:
            self.endpoint_combo.setCurrentText(dlg.endpoint())
            self._log_level("info", f"端点已设置：{dlg.endpoint()}")

    def _open_stats(self) -> None:
        dlg = StatsDialog(self.store, self.store.session_id, self)
        dlg.exec_()

    def _zoom(self, direction: int, reset: bool = False) -> None:
        """字号缩放（Ctrl+= / Ctrl+- / Ctrl+0）。"""
        scale = 1.0 if reset else max(0.8, min(1.8, self._font_scale + 0.1 * direction))
        if abs(scale - self._font_scale) < 1e-6:
            return
        self._font_scale = scale
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(self._theme, scale))
        self.settings.setValue("font_scale", scale)
        self.status_right.setText(f"字号 {scale:.0%}")

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self, "快捷键",
            "空格      暂停 / 继续画面\n"
            "L          开关键盘锁定（勾选后点 PPI 上的目标即可锁定）\n"
            "Esc        解除锁定、清除测距读数\n"
            "+ / -      增大 / 减小量程\n"
            "左键点空白  显示该处的距离与方位（测距读数）\n"
            "左键点目标  锁定该目标（需先按 L 打开锁定）")

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
    def _log_level(self, level: str, text: str) -> None:
        self.log.push(level, text)
        getattr(LOG, {"crit": "error", "warn": "warning"}.get(level, "info"))(text)

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

    def _show_help(self) -> None:
        QMessageBox.information(
            self, "操作说明",
            "连接\n"
            "  虚拟设备直接点连接；真实设备在「端点」里填：\n"
            "  serial://COM3?baud=256000 ／ tcp://ip:端口 ／ udp://0.0.0.0:端口\n"
            "  HC-05 这类蓝牙模块在系统里是串口，用 serial:// 即可\n\n"
            "交互\n"
            "  勾选「点目标锁定」，直接点 PPI 上的目标就能锁定，点空白解除\n"
            "  「门限」控制虚警和漏报的平衡，高了漏，低了一屏假目标\n\n"
            "记录\n"
            "  勾「录制帧」把强度矩阵存到 data/*.rad，之后选「回放」驱动可复现\n"
            "  航迹和告警自动写入 data/radarscope.db（SQLite）")

    def closeEvent(self, ev) -> None:
        self._save()
        self._want_connect = False
        self._teardown_reader()
        if self.store.session_id is not None:
            self.store.end_session("正常退出")
        else:
            self.store.flush(force=True)
        self.store.close()
        super().closeEvent(ev)
