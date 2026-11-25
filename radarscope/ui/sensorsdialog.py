"""传感器配置对话框：给多站融合排布子雷达。

这是"6 个 60° 模块拼 360°"真正落地的地方——朝向摆错 10°，
拼缝就会出现一条看不见的缝，而屏幕上看上去"有个洞但不知道原因"。
所以这里把朝向、视场、安装位置都显式列出来。
"""
from __future__ import annotations

import json
from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..core.fusion import SensorSpec

COLS = ["名称", "驱动", "端点", "X(m)", "Y(m)", "朝向(°)", "视场(°)", "量程(m)", "启用"]


def default_specs(n: int = 6, fov: float = 60.0, driver: str = "ld2450") -> list[SensorSpec]:
    return [
        SensorSpec(name=f"S{i + 1}", driver=driver,
                   endpoint=f"serial://COM{i + 3}?baud=256000",
                   bearing_deg=i * (360.0 / n), fov_deg=fov, range_max=8.0)
        for i in range(n)
    ]


class SensorsDialog(QDialog):
    saved = pyqtSignal()

    def __init__(self, path: str | Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("传感器配置")
        self.resize(940, 460)
        self.path = Path(path)

        tip = QLabel(
            "每一行是一台子雷达。朝向 0° 表示正前方，顺时针为正；视场填模块的实际方位覆盖"
            "（LD2450 约 120°，单扇区 60° 模块填 60）。\n"
            "保存后选「多站融合 · 真实雷达组」并重连即可生效。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8FA396;font-size:12px;")

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)

        btn_add = QPushButton("添加")
        btn_add.clicked.connect(lambda: self._add_row(SensorSpec(name="S?", fov_deg=60.0)))
        btn_del = QPushButton("删除选中")
        btn_del.clicked.connect(self._del_row)
        btn_def = QPushButton("6×60° 默认布站")
        btn_def.clicked.connect(self._fill_default)
        btn_save = QPushButton("保存")
        btn_save.clicked.connect(self._save)
        btn_close = QPushButton("取消")
        btn_close.clicked.connect(self.reject)

        bar = QHBoxLayout()
        for b in (btn_add, btn_del, btn_def):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(btn_save)
        bar.addWidget(btn_close)

        lay = QVBoxLayout(self)
        lay.addWidget(tip)
        lay.addWidget(self.table, 1)
        lay.addLayout(bar)

        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        specs: list[SensorSpec] = []
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                items = data.get("sensors", data) if isinstance(data, dict) else data
                specs = [SensorSpec.from_dict(d) for d in items]
            except Exception:
                specs = []
        if not specs:
            specs = default_specs()
        for s in specs:
            self._add_row(s)

    def _add_row(self, spec: SensorSpec) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        vals = [spec.name, spec.driver, spec.endpoint, f"{spec.x:.2f}", f"{spec.y:.2f}",
                f"{spec.bearing_deg:.1f}", f"{spec.fov_deg:.1f}",
                f"{spec.range_max:.1f}", "是" if spec.enabled else "否"]
        for c, v in enumerate(vals):
            item = QTableWidgetItem(v)
            item.setTextAlignment(Qt.AlignCenter if c > 2 else Qt.AlignLeft | Qt.AlignVCenter)
            self.table.setItem(r, c, item)

    def _del_row(self) -> None:
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def _fill_default(self) -> None:
        self.table.setRowCount(0)
        for s in default_specs():
            self._add_row(s)

    # ------------------------------------------------------------------ #
    def _collect(self) -> list[SensorSpec]:
        out: list[SensorSpec] = []
        for r in range(self.table.rowCount()):
            def cell(c: int, default: str = "") -> str:
                it = self.table.item(r, c)
                return it.text().strip() if it else default

            try:
                out.append(SensorSpec(
                    name=cell(0, f"S{r + 1}"),
                    driver=cell(1, "ld2450"),
                    endpoint=cell(2),
                    x=float(cell(3, "0") or 0),
                    y=float(cell(4, "0") or 0),
                    bearing_deg=float(cell(5, "0") or 0),
                    fov_deg=float(cell(6, "120") or 120),
                    range_max=float(cell(7, "8") or 8),
                    enabled=cell(8, "是") in ("是", "1", "true", "True", ""),
                ))
            except ValueError:
                QMessageBox.warning(self, "格式错误", f"第 {r + 1} 行有非数字内容，已跳过")
        return out

    def _save(self) -> None:
        specs = self._collect()
        if not specs:
            QMessageBox.warning(self, "保存", "至少要有一台子雷达")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"sensors": [s.to_dict() for s in specs]}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        total = sum(s.fov_deg for s in specs if s.enabled)
        msg = f"已保存 {len(specs)} 台，视场合计 {total:.0f}°"
        if total < 360:
            msg += "\n注意：视场合计不足 360°，会有盲区（界面上会用红弧标出来）"
        QMessageBox.information(self, "保存成功", msg)
        self.saved.emit()
        self.accept()
