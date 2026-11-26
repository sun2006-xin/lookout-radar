"""右侧可停靠「防区、跟踪滤波器与快速标定面板」，减少来回开弹窗的割裂感。"""
from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ..core.alarm import AlarmRule
from ..core.i18n import tr


class ZonesOpsWidget(QWidget):
    """停靠栏操作台：集中管理航迹滤波/起批模式、警戒区实时编辑与画布标定。"""

    trackerConfigChanged = pyqtSignal(str, str, int, int)  # filter_mode, assoc_mode, confirm_hits, confirm_window
    drawZoneRequested = pyqtSignal(bool)
    zoneToggleRequested = pyqtSignal(str, bool)
    zoneEditRequested = pyqtSignal(str)
    zoneDeleteRequested = pyqtSignal(str)
    calibModeToggled = pyqtSignal(bool)
    openFullRulesRequested = pyqtSignal()
    openFullSensorsRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # ---- 1. 航迹关联、3D Kalman/IMM 与 M/N 起批确认 ----
        trk_box = QGroupBox("航迹关联与 3D 滤波引擎")
        form = QFormLayout(trk_box)
        form.setContentsMargins(10, 14, 10, 8)
        form.setSpacing(7)

        self.cb_filter = QComboBox()
        self.cb_filter.addItem("3D Kalman（含 z/vz 与 2σ 椭圆）", "kalman3d")
        self.cb_filter.addItem("IMM 交互多模型（高机动无人机推荐）", "imm3d")
        self.cb_filter.addItem("经典 α-β 滤波（低开销平滑）", "alpha_beta")
        self.cb_filter.currentIndexChanged.connect(self._emit_tracker)

        self.cb_assoc = QComboBox()
        self.cb_assoc.addItem("GNN 匈牙利全局关联（防交叉跳号）", "gnn")
        self.cb_assoc.addItem("贪心最近邻（单目标最快）", "greedy")
        self.cb_assoc.currentIndexChanged.connect(self._emit_tracker)

        self.cb_mn = QComboBox()
        self.cb_mn.addItem("1/1 · 首帧即建轨（瞬时响应）", (1, 1))
        self.cb_mn.addItem("2/3 · 连续 3 帧中 2 帧确认", (2, 3))
        self.cb_mn.addItem("3/5 · 严格 5 帧中 3 帧确认（防虚警）", (3, 5))
        self.cb_mn.currentIndexChanged.connect(self._emit_tracker)

        form.addRow("滤波算法", self.cb_filter)
        form.addRow("航迹关联", self.cb_assoc)
        form.addRow("起批逻辑", self.cb_mn)

        # ---- 2. 警戒区列表（与 PPI 拖拽/顶点编辑双向同步） ----
        zone_box = QGroupBox("警戒防区实时列表（支持 PPI 直接拖移与拖多边形顶点）")
        zv = QVBoxLayout(zone_box)
        zv.setContentsMargins(10, 14, 10, 8)
        zv.setSpacing(6)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.btn_draw = QPushButton(tr("画警戒区"))
        self.btn_draw.setCheckable(True)
        self.btn_draw.toggled.connect(self.drawZoneRequested.emit)
        btn_edit = QPushButton("编辑选中")
        btn_edit.clicked.connect(self._edit_selected)
        btn_del = QPushButton("删除选中")
        btn_del.clicked.connect(self._delete_selected)
        btn_rules = QPushButton("全部规则…")
        btn_rules.clicked.connect(self.openFullRulesRequested.emit)
        for b in (self.btn_draw, btn_edit, btn_del, btn_rules):
            bar.addWidget(b)
        zv.addLayout(bar)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["启用", "防区名称", "形态", "几何参数"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._on_item_changed)
        zv.addWidget(self.table, 1)

        # ---- 3. 画布快速工具（多站扇区标定等） ----
        op_box = QGroupBox("多站与画布交互标定")
        oh = QHBoxLayout(op_box)
        oh.setContentsMargins(10, 14, 10, 8)
        oh.setSpacing(8)
        self.btn_calib = QPushButton("拖动扇区标定偏角")
        self.btn_calib.setCheckable(True)
        self.btn_calib.setToolTip("开启后可直接在 PPI 上拖动多站雷达扇区中心线旋转朝向偏角并写入 sensors.json")
        self.btn_calib.toggled.connect(self.calibModeToggled.emit)
        btn_sensors = QPushButton("传感器阵列配置…")
        btn_sensors.clicked.connect(self.openFullSensorsRequested.emit)
        oh.addWidget(self.btn_calib, 1)
        oh.addWidget(btn_sensors)

        hint = QLabel("提示：PPI 上按住 Shift + 左键可直接点放模拟目标；开启「画警戒区」可拖移圆/调半径或拖拽多边形角点。")
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        lay.addWidget(trk_box)
        lay.addWidget(zone_box, 1)
        lay.addWidget(op_box)
        lay.addWidget(hint)
        self._block_items = False

    def sync_tracker(self, filter_mode: str, assoc_mode: str, confirm_hits: int, confirm_window: int) -> None:
        for cb, val in ((self.cb_filter, filter_mode), (self.cb_assoc, assoc_mode)):
            cb.blockSignals(True)
            idx = cb.findData(val)
            if idx >= 0:
                cb.setCurrentIndex(idx)
            cb.blockSignals(False)
        self.cb_mn.blockSignals(True)
        idx = self.cb_mn.findData((int(confirm_hits), int(confirm_window)))
        if idx < 0 and int(confirm_hits) >= 3:
            idx = 2
        elif idx < 0 and int(confirm_hits) == 2:
            idx = 1
        elif idx < 0:
            idx = 0
        self.cb_mn.setCurrentIndex(idx)
        self.cb_mn.blockSignals(False)

    def _emit_tracker(self, *_args) -> None:
        f_mode = str(self.cb_filter.currentData() or "kalman3d")
        a_mode = str(self.cb_assoc.currentData() or "gnn")
        hits, win = self.cb_mn.currentData() or (1, 1)
        self.trackerConfigChanged.emit(f_mode, a_mode, int(hits), int(win))

    def refresh_zones(self, rules: list[AlarmRule]) -> None:
        zone_rules = [r for r in rules if r.kind in ("zone", "poly_zone")]
        self._block_items = True
        try:
            self.table.setRowCount(len(zone_rules))
            for row, r in enumerate(zone_rules):
                chk_it = QTableWidgetItem()
                chk_it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                chk_it.setCheckState(Qt.Checked if r.enabled else Qt.Unchecked)
                chk_it.setData(Qt.UserRole, str(r.id))
                self.table.setItem(row, 0, chk_it)

                name_it = QTableWidgetItem(r.name)
                name_it.setData(Qt.UserRole, str(r.id))
                kind_txt = "多边形" if r.kind == "poly_zone" else "圆形"
                kind_it = QTableWidgetItem(kind_txt)
                kind_it.setTextAlignment(Qt.AlignCenter)
                if r.kind == "poly_zone":
                    pts = r.params.get("points") or []
                    geo = f"{len(pts)} 顶点"
                else:
                    p = r.params
                    geo = f"({float(p.get('cx', 0)):.1f}, {float(p.get('cy', 0)):.1f}) r={float(p.get('r', 1)):.1f}m"
                geo_it = QTableWidgetItem(geo)

                self.table.setItem(row, 1, name_it)
                self.table.setItem(row, 2, kind_it)
                self.table.setItem(row, 3, geo_it)
        finally:
            self._block_items = False

    def _selected_rule_id(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        it = self.table.item(row, 0)
        return str(it.data(Qt.UserRole) or "") if it else ""

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._block_items or item.column() != 0:
            return
        rid = str(item.data(Qt.UserRole) or "")
        if rid:
            self.zoneToggleRequested.emit(rid, item.checkState() == Qt.Checked)

    def _edit_selected(self) -> None:
        rid = self._selected_rule_id()
        if rid:
            self.zoneEditRequested.emit(rid)

    def _delete_selected(self) -> None:
        rid = self._selected_rule_id()
        if rid:
            self.zoneDeleteRequested.emit(rid)
