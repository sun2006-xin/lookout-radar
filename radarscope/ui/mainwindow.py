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
from PyQt5.QtGui import QColor, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QAction, QActionGroup, QApplication, QComboBox, QDialog, QDockWidget,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QDialogButtonBox, QMainWindow, QMenu, QMessageBox, QPushButton,
    QShortcut, QSizePolicy, QTabWidget,
    QToolBar, QToolButton, QVBoxLayout, QWidget, QWidgetAction,
)

from ..core import cpa, pipeline
from ..core.aisfeed import AisFeed
from ..core.forwarder import Forwarder
from ..core.alarm import DEFAULT_RULES, AlarmEngine, merge_rules, rules_from_json
from ..core.maptiles import resolve_provider
from .. import drivers as _builtin_drivers  # noqa: F401  保证单独实例化 MainWindow 也能注册内置驱动
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
from .controllers import (PRESET_ENDPOINTS, ALARM_STYLE, ALARM_STYLE_LIGHT, LIGHT_PALETTES,
                          ConnectionController, ExportController,
                          AlarmBarController, ReaderThread)
from .statsdialog import StatsDialog
from .theme import (DEFAULT_PALETTE, DEFAULT_THEME, LIGHT_PALETTE_KEYS, PALETTES,
                    THEMES, build_stylesheet, is_dark_theme, theme_palette)
from .tracktable import TrackTable
from .extra import AisDialog, ControlDialog, MapLocationDialog
from .waterfall import WaterfallWidget
from .winstyle import enable_dark_titlebar, install_dialog_titlebar, make_app_icon

LOG = logging.getLogger("radarscope")


def _swatch_icon(bg: QColor, accent: QColor) -> QIcon:
    """生成 14×14 双色小圆标，用于主题/雷达配色下拉框与菜单直观预览。"""
    pm = QPixmap(14, 14)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(QColor(148, 163, 184, 140))
    p.setBrush(bg)
    p.drawRoundedRect(1, 1, 12, 12, 3, 3)
    p.setPen(Qt.NoPen)
    p.setBrush(accent)
    p.drawRoundedRect(7, 2, 5, 10, 2, 2)
    p.end()
    return QIcon(pm)


class MainWindow(ConnectionController, ExportController,
                 AlarmBarController, QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("瞭望 Lookout · 通用雷达显控台")
        self.resize(1240, 760)

        self.settings = QSettings("RadarScope", "Console")
        self.data_dir = paths.data_dir()
        self.store = Store(self.data_dir / "radarscope.db")

        # 插件先加载，驱动注册表里才会有插件带来的雷达
        self.plugins = PluginManager(
            paths.plugins_dir(),
            trust_path=self.data_dir / "trusted_plugins.json",
        )
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
        self._cfar_mode = str(self.settings.value("cfar_mode", "auto"))
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
        # AIS / 地图中心坐标
        self.ais = AisFeed()
        self._ais_ref = (39.9, 116.4)
        self._map_provider = "auto"
        # 模拟电子站（动态虚拟目标与电子对抗教学叠加）
        from ..core.sim_targets import ElectronicStation
        self.sim_station = ElectronicStation()
        self.clutter_map = pipeline.ClutterMap()
        self.clutter_map.enabled = bool(self.settings.value("clutter_suppress", False, type=bool))
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
        self.ppi.zoneMoved.connect(self._update_zone_geometry)
        self.ppi.zoneEditRequested.connect(self._edit_zone)
        self.ppi.zoneDeleteRequested.connect(self._delete_zone)

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

        ppi_card = QFrame()
        ppi_card.setObjectName("panelCard")
        pl = QVBoxLayout(ppi_card)
        pl.setContentsMargins(6, 6, 6, 6)
        pl.addWidget(self.ppi)

        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(8, 8, 8, 8)
        cv.setSpacing(8)
        cv.addWidget(self.alarm_bar)
        h = QHBoxLayout()
        h.setSpacing(8)
        h.addWidget(ppi_card, 1)
        h.addWidget(right, 0)
        cv.addLayout(h, 1)
        self.setCentralWidget(central)

        # 近距小量程窗：大范围看态势，小窗看细节，放在右侧避免垂直挤压主图
        self.ppi2 = PPIWidget()
        self.ppi2.setMinimumSize(220, 220)
        self.dock_near = QDockWidget("近距监视（2 m 量程）", self)
        self.dock_near.setWidget(self.ppi2)
        self.dock_near.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock_near)
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
        self.btn_conn.setObjectName("primaryBtn")
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
        self.range_spin.setMinimumWidth(96)
        self.range_spin.valueChanged.connect(self._on_range_changed)
        tb.addWidget(self.range_spin)

        # 以下控件统一收进「设置」对话框，不挂到工具栏上，
        # 避免 addWidget() 创建的 QWidgetAction 在 reparent 后留下空白占位并隐藏控件。
        self.thr_spin = QDoubleSpinBox()
        self.thr_spin.setRange(0.05, 0.95)
        self.thr_spin.setSingleStep(0.05)
        self.thr_spin.setValue(0.30)
        self.thr_spin.valueChanged.connect(self._on_threshold_changed)

        # 显示增益：对已解析的矩阵做后处理，不涉及设备控制。
        # 真实雷达的 STC/FTC 要在设备侧下发参数，那是另一件事。
        self.gain_spin = QDoubleSpinBox()
        self.gain_spin.setRange(0.2, 4.0)
        self.gain_spin.setSingleStep(0.1)
        self.gain_spin.setValue(1.0)
        self.gain_spin.setDecimals(1)
        self.gain_spin.setToolTip("显示增益：放大弱回波（只影响显示，不改变检测门限）")

        self.pal_combo = QComboBox()
        for key, pal in PALETTES.items():
            self.pal_combo.addItem(_swatch_icon(QColor(*pal["bg"]), QColor(*pal["stops"][2][1])),
                                   pal["label"], key)
        idx = self.pal_combo.findData(self.settings.value("palette", theme_palette(self._theme)))
        if idx >= 0:
            self.pal_combo.setCurrentIndex(idx)
        self.pal_combo.currentIndexChanged.connect(self._on_palette_changed)

        # 朝向：车载/船载场景下"北在哪"很重要；固定雷达用艏向朝上即可
        self.btn_north = QPushButton("北向朝上")
        self.btn_north.setCheckable(True)
        self.btn_north.setToolTip("勾选后画面按航向旋转，让北始终朝上；不勾选则平台正前朝上")
        self.btn_north.toggled.connect(self._on_orientation_changed)

        self.heading_spin = QDoubleSpinBox()
        self.heading_spin.setRange(0.0, 359.9)
        self.heading_spin.setDecimals(0)
        self.heading_spin.setSingleStep(5.0)
        self.heading_spin.setSuffix("°")
        self.heading_spin.setToolTip("平台航向（0=正北，顺时针）。接罗盘/IMU 后可由插件自动更新")
        self.heading_spin.valueChanged.connect(self._on_orientation_changed)

        # 所有操作按钮紧随「量程」靠左紧凑排列，避免中间留白导致视线跨度过大
        self.btn_scene = QPushButton("场景/电子战")
        self.btn_scene.setCheckable(True)
        self.btn_scene.setToolTip("快速打开模拟电子站 / 军方电子战干扰 / 航海与车载预设场景控制台")
        self.btn_scene.clicked.connect(self._open_sim_station)
        tb.addWidget(self.btn_scene)

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
                                 "右键点圆可删除")
        self.btn_zone.toggled.connect(self._on_zone_mode_toggled)
        self.ppi.zoneModeRequested.connect(self.btn_zone.setChecked)
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

        self.chk_reconnect = QPushButton("自动重连")
        self.chk_reconnect.setCheckable(True)
        self.chk_reconnect.setChecked(True)

        self.btn_clutter = QPushButton("杂波抑制")
        self.btn_clutter.setCheckable(True)
        self.btn_clutter.setChecked(self.clutter_map.enabled)
        self.btn_clutter.setToolTip("学习静态建筑/地形背景杂波并抑制，只突出移动目标（借鉴船用 Smart Radar）")
        self.btn_clutter.toggled.connect(self._toggle_clutter)

        self.theme_combo = QComboBox()
        for key, th in THEMES.items():
            self.theme_combo.addItem(_swatch_icon(QColor(th["bg"]), QColor(th["accent"])),
                                     th["name"], key)
        ti = self.theme_combo.findData(self._theme)
        if ti >= 0:
            self.theme_combo.setCurrentIndex(ti)
        self.theme_combo.currentIndexChanged.connect(
            lambda _i: self._apply_theme(self.theme_combo.currentData(), sync_palette=True)
        )

        self.cfar_combo = QComboBox()
        for key, label in (
            ("auto", "自适应 (GO-CFAR)"),
            ("os", "抗干扰 (OS-CFAR)"),
            ("go", "抑杂波边缘 (GO-CFAR)"),
            ("ca", "固定门限 (CA-CFAR)"),
        ):
            self.cfar_combo.addItem(label, key)
        ci = self.cfar_combo.findData(self._cfar_mode)
        if ci >= 0:
            self.cfar_combo.setCurrentIndex(ci)
        self.cfar_combo.currentIndexChanged.connect(
            lambda _i: self._on_cfar_changed()
        )

        self.lang_combo = QComboBox()
        self.lang_combo.addItem("简体中文", "zh")
        self.lang_combo.addItem("English", "en")
        lang = str(self.settings.value("lang", "zh"))
        li = self.lang_combo.findData(lang)
        if li >= 0:
            self.lang_combo.setCurrentIndex(li)
        self.lang_combo.currentIndexChanged.connect(lambda _i: self._on_lang_changed())

        # 收敛工具栏：常用的留在外面，其余收进「设置」弹窗。
        panel = QWidget()
        form = QFormLayout(panel)
        form.setContentsMargins(14, 12, 14, 12)
        form.setSpacing(9)
        form.addRow("采集模式", self.mode_combo)
        form.addRow("检测算法", self.cfar_combo)
        form.addRow("检测门限", self.thr_spin)
        form.addRow("显示增益", self.gain_spin)
        form.addRow("界面主题", self.theme_combo)
        form.addRow("雷达配色", self.pal_combo)
        form.addRow("界面语言", self.lang_combo)
        form.addRow("航向", self.heading_spin)
        form.addRow("航速", self.speed_spin)
        opts = QWidget()
        ol = QHBoxLayout(opts)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(6)
        for w in (self.btn_north, self.btn_rec, self.btn_clutter, self.chk_reconnect):
            ol.addWidget(w)
        ol.addStretch(1)
        form.addRow("选项", opts)

        # 原来是"按钮 + 菜单 + QWidgetAction 面板"。Qt 的菜单里放交互控件
        # 必须给焦点，否则点不动 —— 用户反馈"是个摆设"就是这个原因。
        # 直接改成独立对话框，稳定、能点、还能记住位置。
        self._settings_dlg = QDialog(self)
        self._settings_dlg.setWindowTitle("设置")
        self._settings_dlg.resize(440, 420)
        _lay = QVBoxLayout(self._settings_dlg)
        _lay.setContentsMargins(16, 14, 16, 12)
        _lay.setSpacing(10)
        _lay.addWidget(panel)
        _lay.addStretch(1)
        _bb = QDialogButtonBox(QDialogButtonBox.Close)
        _bb.rejected.connect(self._settings_dlg.reject)
        _lay.addWidget(_bb)

        # 菜单按职责分区：文件 / 场景(仿真与电子战) / 视图 / 工具(硬件校准与扩展) / 设置 / 帮助
        m_file = self.menuBar().addMenu("文件(&F)")
        self._m_file = m_file
        m_file.addAction("导出事件日志…", self._export_log)
        m_file.addAction("导出当前画面…", self._export_screenshot)
        m_file.addAction("打开数据目录", self._open_data_dir)
        m_file.addSeparator()
        m_file.addAction("退出", self.close)

        m_scene = self.menuBar().addMenu("场景(&N)")
        self._m_scene = m_scene
        sc_grp = QActionGroup(self)
        sc_grp.setExclusive(True)
        self._scene_actions: dict[str, QAction] = {}
        for key, title, rng_hint in (
            ("none", "关闭预设场景（正常雷达显示）", 0.0),
            ("marine", "航海轮船避碰场景（2 km · 商船/货轮会遇）", 2000.0),
            ("automotive", "车载 77G 多车道交通流场景（80 m）", 80.0),
            ("drone", "低空无人机 / 安防突防场景（150 m）", 150.0),
            ("patrol", "随机出现与巡逻（动态生成/离开）", 0.0),
            ("approach", "高速逼近 / 防撞告警演练", 0.0),
            ("crossing", "多目标交叉会遇（航迹关联测试）", 0.0),
        ):
            act = QAction(title, self)
            act.setCheckable(True)
            act.setChecked(key == "none")
            act.triggered.connect(lambda _c=False, k=key, r=rng_hint: self._apply_scenario(k, r))
            sc_grp.addAction(act)
            m_scene.addAction(act)
            self._scene_actions[key] = act
        m_scene.addSeparator()
        m_ew = m_scene.addMenu("军方 / 教学电子战干扰样式")
        ew_grp = QActionGroup(self)
        ew_grp.setExclusive(True)
        self._ew_actions: dict[str, QAction] = {}
        for key, title in (
            ("none", "关闭干扰（正常信号）"),
            ("barrage", "宽带噪声压制（演练烧穿距离）"),
            ("spot", "瞄准扇区压制（指定方位干扰）"),
            ("drfm", "DRFM 多假目标欺骗"),
            ("rgpo", "RGPO 距离波门拖引"),
        ):
            act = QAction(title, self)
            act.setCheckable(True)
            act.setChecked(key == "none")
            act.triggered.connect(lambda _c=False, k=key: self._set_jam_mode(k))
            ew_grp.addAction(act)
            m_ew.addAction(act)
            self._ew_actions[key] = act
        m_scene.addAction("场景与电子战配置控制台…", self._open_sim_station)

        m_tool = self.menuBar().addMenu("工具(&T)")
        self._m_tool = m_tool
        m_tool.addAction("朝向标定…", self._open_calibration)
        m_tool.addAction("统计分析…", self._open_stats)
        m_tool.addAction("设备下行控制 (STC/FTC)…", self._open_control)
        m_tool.addAction("AIS 接入…", self._open_ais)
        m_tool.addAction("通用协议雷达适配向导…", self._open_proto_wizard)
        m_tool.addAction("插件管理…", self._open_plugins)

        # 视图：面板显隐。用户不该被固定布局绑死——投屏要大字、
        # 调试要全信息，两种场景需要不同的界面。
        m_view = self.menuBar().addMenu("视图(&V)")
        self._m_view = m_view
        for text, w in (("A 显", self.ascope), ("瀑布图", self.waterfall),
                        ("距离-多普勒", self.rdscope), ("传感器页签", self.sensors_panel)):
            act = QAction(text, self)
            act.setCheckable(True)
            act.setChecked(True)
            act.toggled.connect(w.setVisible)
            m_view.addAction(act)
        m_view.addSeparator()
        self._view_actions: dict[str, QAction] = {}
        for text, attr, default in (
            ("尾迹", "show_trails", True),
            ("多普勒趋近着色", "doppler_color", True),
            ("告警禁入区", "show_zones", False),
            ("电子方位线 EBL/VRM", "show_ebl", True),
        ):
            val = self.settings.value(attr, default, type=bool)
            act = QAction(text, self)
            act.setCheckable(True)
            act.setChecked(val)
            setattr(self.ppi, attr, val)
            act.toggled.connect(lambda on, a=attr: self._set_ppi_flag(a, on))
            m_view.addAction(act)
            self._view_actions[attr] = act
        m_view.addAction("清除自绘警戒区", self._clear_user_zones)
        m_view.addAction("清除 EBL", lambda: self.ppi.set_ebl(None))
        m_view.addAction("清除测距读数", self.ppi.clear_probe)

        act_near = QAction("双量程窗口（近距）", self)
        act_near.setCheckable(True)
        act_near.toggled.connect(self._toggle_near)
        m_view.addAction(act_near)

        act_map = QAction("联网地图底图", self)
        act_map.setCheckable(True)
        act_map.toggled.connect(self._toggle_map)
        m_view.addAction(act_map)
        self.act_map = act_map
        ais_on = self.settings.value("show_ais", True, type=bool)
        act_ais = QAction("显示 AIS 目标", self)
        act_ais.setCheckable(True)
        act_ais.setChecked(ais_on)
        self.ppi.show_ais = ais_on
        act_ais.toggled.connect(lambda on: self._set_ppi_flag("show_ais", on))
        m_view.addAction(act_ais)
        self._view_actions["show_ais"] = act_ais
        m_view.addSeparator()
        m_theme = m_view.addMenu("界面主题（7 套风格）")
        th_grp = QActionGroup(self)
        th_grp.setExclusive(True)
        self._theme_actions: dict[str, QAction] = {}
        for key, th in THEMES.items():
            act = QAction(th["name"], self)
            act.setCheckable(True)
            act.setChecked(key == self._theme)
            act.triggered.connect(lambda _c=False, k=key: self._apply_theme(k, sync_palette=True))
            th_grp.addAction(act)
            m_theme.addAction(act)
            self._theme_actions[key] = act
        m_pal = m_view.addMenu("雷达配色（8 套色板）")
        pal_grp = QActionGroup(self)
        pal_grp.setExclusive(True)
        self._palette_actions: dict[str, QAction] = {}
        cur_pal = self.pal_combo.currentData()
        for key, pal in PALETTES.items():
            act = QAction(_swatch_icon(QColor(*pal["bg"]), QColor(*pal["stops"][2][1])),
                          pal["label"], self)
            act.setCheckable(True)
            act.setChecked(key == cur_pal)
            act.triggered.connect(lambda _c=False, k=key: self._select_palette(k))
            pal_grp.addAction(act)
            m_pal.addAction(act)
            self._palette_actions[key] = act

        m_set = self.menuBar().addMenu("设置(&S)")
        self._m_set = m_set
        m_set.addAction("显示与主题设置…", self._open_settings)
        m_set.addSeparator()
        m_set.addAction("连接设置…", self._open_link_dialog)
        m_set.addAction("告警规则…", self._open_rules)
        m_set.addAction("传感器配置…", self._open_sensor_config)
        m_set.addAction("联网地图与定位设置…", self._open_map_settings)
        m_help = self.menuBar().addMenu("帮助(&H)")
        self._m_help = m_help
        m_help.addAction("操作说明", self._show_help)
        m_help.addAction("快捷键", self._show_shortcuts)
        m_help.addAction("运行自检…", self._run_selftest)
        m_help.addAction("检查更新…", self._check_updates)

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
        # 允许左侧状态文本随窗口缩放收缩（tooltip 保留全文），防止把右侧 CPA/FPS 或底栏挤出视野
        self.status_left.setMinimumWidth(0)
        self.status_left.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status_right = QLabel("-- fps")
        self.status_link = QLabel("")
        self.status_link.setStyleSheet("color:#8B9AA8;padding-right:12px;")
        self.status_cpa = QLabel("CPA --")
        self.status_cpa.setStyleSheet("color:#9FB2A6;")
        sb = self.statusBar()
        sb.setMinimumHeight(26)
        sb.addWidget(self.status_left, 1)
        sb.addPermanentWidget(self.status_link)
        sb.addPermanentWidget(self.status_cpa)
        sb.addPermanentWidget(self.status_right)

        self.log.push("info", "RadarScope 启动")
        self._rebuild_driver_combo()
        if self.pal_combo.currentData() not in PALETTES:
            i = self.pal_combo.findData(theme_palette(self._theme))
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
        # 防止恢复后的窗口高度/宽度超出桌面可用工作区，避免底部状态栏被任务栏挡住或挤压
        scr = QGuiApplication.primaryScreen()
        if scr is not None and not self.isMaximized():
            avail = scr.availableGeometry()
            w = min(self.width(), max(900, avail.width() - 24))
            h = min(self.height(), max(600, avail.height() - 24))
            self.resize(w, h)
        lat = float(self.settings.value("ais_lat", 39.9) or 39.9)
        lon = float(self.settings.value("ais_lon", 116.4) or 116.4)
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            self._ais_ref = (lat, lon)
        self._map_provider = str(self.settings.value("map_provider", "auto") or "auto")
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
        self.settings.setValue("ais_lat", self._ais_ref[0])
        self.settings.setValue("ais_lon", self._ais_ref[1])
        self.settings.setValue("map_provider", self._map_provider)

    # ================================================================== #
    # 端点
    # ================================================================== #
    def _set_ppi_flag(self, attr: str, on: bool) -> None:
        for w in (self.ppi, self.ppi2):
            setattr(w, attr, bool(on))
            w.update()
        self.settings.setValue(attr, bool(on))
        act = getattr(self, "_view_actions", {}).get(attr)
        if act is not None and act.isChecked() != bool(on):
            act.blockSignals(True)
            act.setChecked(bool(on))
            act.blockSignals(False)

    def _on_zone_mode_toggled(self, on: bool) -> None:
        self.ppi.set_zone_mode(on)
        if on and not self.ppi.show_zones:
            self._set_ppi_flag("show_zones", True)

    def _on_range_changed(self, v: float) -> None:
        self.caps.range_max = float(v)
        self.ppi.set_capabilities(self.caps)
        self.waterfall.set_range(self.caps.range_max)
        if self.dock_near.isVisible():
            self._toggle_near(True)

    def _on_threshold_changed(self, v: float) -> None:
        self._thresh = float(v)
        self.ascope.set_threshold(self._thresh)

    def _on_cfar_changed(self) -> None:
        self._cfar_mode = str(self.cfar_combo.currentData() or "auto")
        self.settings.setValue("cfar_mode", self._cfar_mode)
        self.log.push("info", f"检测算法：{self.cfar_combo.currentText()}")

    def _on_lang_changed(self) -> None:
        from ..core.i18n import set_lang, tr

        lang = set_lang(str(self.lang_combo.currentData() or "zh"))
        self.settings.setValue("lang", lang)
        for m, key in (
            (self._m_file, "文件(&F)"), (self._m_tool, "工具(&T)"),
            (self._m_view, "视图(&V)"), (self._m_set, "设置(&S)"),
            (self._m_help, "帮助(&H)"),
        ):
            m.setTitle(tr(key))
        for btn, key in (
            (self.btn_link, "连接设置"), (self.btn_lock, "点目标锁定"),
            (self.btn_fwd, "转发"), (self.btn_clip, "录像"),
            (self.btn_mute, "静音"), (self.btn_zone, "画警戒区"),
            (self.btn_true, "真运动"),
        ):
            btn.setText(tr(key))
        for idx, key in enumerate(("航迹", "传感器", "事件", "历史")):
            self.tabs.setTabText(idx, tr(key))
        self._settings_dlg.setWindowTitle(tr("设置"))

    def _check_updates(self) -> None:
        from ..version import __version__, check_latest_release

        has_new, msg = check_latest_release(timeout_s=3.0)
        QMessageBox.information(self, f"版本检查（当前 {__version__}）", msg)

    def _select_palette(self, key: str) -> None:
        i = self.pal_combo.findData(key)
        if i >= 0:
            self.pal_combo.setCurrentIndex(i)

    def _on_palette_changed(self) -> None:
        key = self.pal_combo.currentData()
        if key not in PALETTES:
            return
        self.settings.setValue("palette", key)
        if hasattr(self, "_palette_actions") and key in self._palette_actions:
            self._palette_actions[key].setChecked(True)
        self.ppi.set_palette(key)
        self.ppi2.set_palette(key)
        self.ascope.set_palette(key)
        self.waterfall.set_palette(key)
        self.rdscope.set_palette(key)

    def _on_orientation_changed(self, *_args) -> None:
        self.ppi.set_orientation(self.btn_north.isChecked(), self.heading_spin.value())
        self.ppi2.set_orientation(self.btn_north.isChecked(), self.heading_spin.value())

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
        self.clutter_map.reset()
        self._locked_id = -1
        self.status_left.setText(f"{caps.name} ｜ {caps.summary()} ｜ {caps.notes}")

    # ================================================================== #
    # 连接 / 断开
    # ================================================================== #
    def _on_frame(self, frame) -> None:
        if self._paused:
            return                       # 暂停时画面冻住，方便截图或细看
        if frame.meta.get("_normalized"):
            f = frame
        else:
            try:
                f = pipeline.normalize(
                    frame, self.caps, threshold=self._thresh, cfar_mode=self._cfar_mode
                )
            except Exception as exc:
                self._log_level("warn", f"数据解析异常（已跳过该帧）：{exc}")
                return

        now = frame.timestamp or time.monotonic()
        dt = (now - self._last_ts) if self._last_ts else 0.1
        self._last_ts = now
        self._last_frame_mono = time.monotonic()

        if self.clutter_map.enabled and f.has_scan:
            f = self.clutter_map.apply(f)
            if self.clutter_map.frames >= self.clutter_map.warmup:
                f.targets = pipeline.detect_targets(
                    f, threshold=self._thresh,
                    max_targets=max(8, self.caps.max_targets or 8),
                    cfar_mode=self._cfar_mode,
                )

        if self.sim_station.active():
            f = self.sim_station.merge_into_frame(
                f, self.caps, dt, threshold=self._thresh, cfar_mode=self._cfar_mode
            )

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
            self.map_layer.store.prune()
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
        self.waterfall.push_frame(f, all_tracks)
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
            # R1：写库失败必须让用户看见，不能只躺在日志里
            bits = []
            if getattr(self.store, "write_errors", 0):
                bits.append(f"⚠ 存储异常 {self.store.write_errors}")
            if self.forwarder.enabled:
                bits.append(f"转发 {self.forwarder.sent}")
            self.status_link.setText("  ｜  ".join(bits))
            self.status_link.setStyleSheet(
                "color:#B42318;font-weight:bold;padding-right:12px;"
                if getattr(self.store, "write_errors", 0)
                else "color:#6B7280;padding-right:12px;")

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
                    "fov": float(getattr(self.caps, "az_fov", 360.0) or 360.0),
                    "endpoint": self.endpoint_combo.currentText(),
                    "online": True,
                    "fps": self._fps,
                    "frames": self._frames,
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

    def _open_settings(self) -> None:
        self._settings_dlg.show()
        self._settings_dlg.raise_()
        self._settings_dlg.activateWindow()

    def _apply_theme(self, mode: str, sync_palette: bool = False) -> None:
        """切换界面主题。样式表、Windows 标题栏与雷达色板自动协调切换。"""
        self._theme = mode if mode in THEMES else DEFAULT_THEME
        if hasattr(self, "theme_combo"):
            self.theme_combo.blockSignals(True)
            ti = self.theme_combo.findData(self._theme)
            if ti >= 0:
                self.theme_combo.setCurrentIndex(ti)
            self.theme_combo.blockSignals(False)
        if hasattr(self, "_theme_actions") and self._theme in self._theme_actions:
            self._theme_actions[self._theme].setChecked(True)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(self._theme, self._font_scale))
        self.settings.setValue("theme", self._theme)

        # 用户主动切换界面主题（sync_palette=True）时联动该主题的专属雷达配色；
        # 普通启动恢复（sync_palette=False）尊重用户单独挑选并保存的雷达色板，只在无效值时兜底。
        cur = self.pal_combo.currentData()
        dark = is_dark_theme(self._theme)
        want = theme_palette(self._theme)
        if sync_palette or cur not in PALETTES:
            i = self.pal_combo.findData(want)
            if i >= 0:
                self.pal_combo.setCurrentIndex(i)

        enable_dark_titlebar(int(self.winId()), dark)
        if self.map_layer is not None:
            self.map_layer.set_provider(resolve_provider(self._map_provider, "dark" if dark else "light"))
        self._refresh_alarm_bar()
        self.log.push("info", f"主题：{THEMES[self._theme]['name']}")

    def _toggle_map(self, on: bool) -> None:
        """联网地图：按当前量程自动选瓦片级别，拿中心坐标（手动/IP/AIS）当原点。"""
        act = getattr(self, "act_map", None)
        if act is not None and act.isChecked() != bool(on):
            act.blockSignals(True)
            act.setChecked(bool(on))
            act.blockSignals(False)
        if on:
            if self.map_layer is None:
                self.map_layer = MapLayer(paths.data_dir() / "tiles", self)
                self.map_layer.ready.connect(self.ppi.update)
            prov = resolve_provider(self._map_provider, self._theme)
            self.map_layer.set_provider(prov)
            self.map_layer.enabled = True
            self.ppi.map_layer = self.map_layer
            lat, lon = self._ais_ref
            self.map_layer.request(lat, lon, float(self.range_spin.value()),
                                   max(200, self.ppi.width()))
            hint = f"联网地图已开启（中心 {lat:.5f}, {lon:.5f} · 图源 {prov}）"
            if float(self.range_spin.value()) < 30.0:
                hint += "；提示：街道地图仅在 30m 以上室外/海图量程有细节，离线时自动保持透明"
            self.log.push("ok", hint)
        else:
            if self.map_layer is not None:
                self.map_layer.enabled = False
                self.map_layer.stop()
            self.ppi.map_layer = None
            self.ppi2.map_layer = None
            self.log.push("info", "联网地图底图已关闭")
        self.ppi.update()
        self.ppi2.update()

    def _open_map_settings(self) -> None:
        cur_on = bool(self.map_layer is not None and self.map_layer.enabled)
        dlg = MapLocationDialog(
            self._ais_ref[0], self._ais_ref[1], self._map_provider, cur_on, self
        )
        if dlg.exec_() != QDialog.Accepted:
            return
        lat, lon, prov, want_on = dlg.values()
        self._ais_ref = (lat, lon)
        self._map_provider = prov
        self.settings.setValue("ais_lat", lat)
        self.settings.setValue("ais_lon", lon)
        self.settings.setValue("map_provider", prov)
        if want_on and dlg.auto_fit_range() and float(self.range_spin.value()) < 100.0:
            self.range_spin.setValue(500.0)
        if self.map_layer is not None:
            self.map_layer._key = None
        self._toggle_map(want_on)
        if not want_on:
            self.log.push("ok", f"已保存地图与定位中心：{lat:.5f}, {lon:.5f}")

    def _sync_scene_ui(self) -> None:
        sc = self.sim_station.scenario
        jm = self.sim_station.jam_mode
        act_s = getattr(self, "_scene_actions", {}).get(sc)
        if act_s is not None:
            act_s.setChecked(True)
        act_j = getattr(self, "_ew_actions", {}).get(jm)
        if act_j is not None:
            act_j.setChecked(True)
        if hasattr(self, "btn_scene"):
            on = self.sim_station.active()
            self.btn_scene.blockSignals(True)
            self.btn_scene.setChecked(on)
            self.btn_scene.blockSignals(False)
            if jm != "none":
                self.btn_scene.setText(f"干扰:{jm.upper()}")
            elif sc != "none":
                self.btn_scene.setText(f"场景:{sc}")
            elif self.sim_station.objects:
                self.btn_scene.setText(f"目标:{len(self.sim_station.objects)}")
            else:
                self.btn_scene.setText("场景/电子战")

    def _apply_scenario(self, key: str, suggested_range: float = 0.0) -> None:
        if suggested_range > 0.0 and abs(float(self.range_spin.value()) - suggested_range) > 1.0:
            self.range_spin.setValue(suggested_range)
        self.sim_station.set_scenario(key, float(self.range_spin.value()))
        self._sync_scene_ui()
        if key == "none":
            self.log.push("info", "已关闭预设演练场景")
        else:
            self.log.push("ok", f"已开启场景「{key}」，当前目标 {len(self.sim_station.objects)} 个（若未连接请先点「连接」）")

    def _set_jam_mode(self, mode: str) -> None:
        self.sim_station.jam_mode = mode
        self.sim_station._jam_t = 0.0
        self._sync_scene_ui()
        self.log.push("info" if mode == "none" else "warn",
                      f"电子战干扰样式已切换：{mode}")

    def _open_sim_station(self) -> None:
        from .simdialog import SimStationDialog

        dlg = SimStationDialog(self.sim_station, float(self.range_spin.value()), self)
        dlg.exec_()
        self._sync_scene_ui()
        if self.sim_station.active():
            self.log.push("ok", f"模拟电子站：活跃目标 {len(self.sim_station.objects)} 个 · 干扰 {self.sim_station.jam_mode}")

    def _toggle_clutter(self, on: bool) -> None:
        self.clutter_map.enabled = bool(on)
        self.clutter_map.reset()
        self.settings.setValue("clutter_suppress", bool(on))
        self.log.push("info", "静态杂波背景抑制已开启（前 5 帧自动学习环境底噪）" if on else "静态杂波背景抑制已关闭")

    def _open_proto_wizard(self) -> None:
        from .protodialog import ProtoWizardDialog

        dlg = ProtoWizardDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            self._rebuild_driver_combo(keep=dlg.ed_id.text().strip() or None)
            self.log.push("ok", f"已注册/更新通用协议适配器：{dlg.ed_label.text().strip()}")

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
            self.ppi2.set_palette(self.pal_combo.currentData())
            self.ppi2.set_orientation(self.btn_north.isChecked(), self.heading_spin.value())
            self.ppi2.set_capabilities(caps)
            self._sync_zones()

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
        if enable_dark_titlebar(int(self.winId()), is_dark_theme(self._theme)):
            LOG.debug("深色标题栏已启用")

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        QTimer.singleShot(0, self._apply_native_style)

    def _open_link_dialog(self) -> None:
        dlg = LinkDialog(self.endpoint_combo.currentText().strip(), self)
        if dlg.exec_() == QDialog.Accepted:
            self.endpoint_combo.setCurrentText(dlg.endpoint())
            self._log_level("info", f"端点已设置：{dlg.endpoint()}")

    def _run_selftest(self) -> None:
        """帮助菜单里的自检入口：不用开终端，结果直接打到事件日志。"""
        from ..core.selftest import run_core_checks

        ok, msg = run_core_checks(timeout_s=2.5)
        self.log.push("ok" if ok else "warn",
                      ("自检通过：" if ok else "自检失败：") + msg)
        if not ok:
            QMessageBox.warning(self, "自检失败", msg)

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

    def _log_level(self, level: str, text: str) -> None:
        self.log.push(level, text)
        getattr(LOG, {"crit": "error", "warn": "warning"}.get(level, "info"))(text)

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
