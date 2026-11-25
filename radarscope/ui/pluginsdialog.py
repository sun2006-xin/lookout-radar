"""插件管理对话框。"""
from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..core.plugins import PluginManager


class PluginsDialog(QDialog):
    reloaded = pyqtSignal()

    COLS = ["插件", "版本", "作者", "驱动", "规则", "状态", "说明"]

    def __init__(self, manager: PluginManager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("插件管理")
        self.resize(860, 420)
        self.manager = manager

        # S3：插件是任意代码，必须给出视觉警示，不能只写在文档里
        warn = QLabel("⚠ 插件是任意 Python 代码，加载即在本机执行。"
                      "只加载你自己写的、或你信任的来源。")
        warn.setWordWrap(True)
        warn.setStyleSheet(
            "background:#FFF6E5;color:#8A5A00;border:1px solid #F3DFB6;"
            "border-radius:6px;padding:6px 10px;font-size:12px;")

        tip = QLabel(
            f"插件目录：{manager.plugin_dir}\n"
            "往这个目录丢一个 .py（或一个包目录），点「重新加载」即可生效。\n"
            "插件可以注册：新雷达驱动、告警规则包。加载失败不会影响主程序运行。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8FA396;font-size:12px;")

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(6, QHeaderView.Stretch)

        btn_reload = QPushButton("重新加载")
        btn_reload.clicked.connect(self._reload)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(btn_reload)
        bottom.addWidget(btn_close)

        lay = QVBoxLayout(self)
        lay.addWidget(warn)
        lay.addWidget(tip)
        lay.addWidget(self.table, 1)
        lay.addLayout(bottom)
        self._fill()

    def _fill(self) -> None:
        ps = self.manager.plugins
        self.table.setRowCount(len(ps))
        for row, p in enumerate(ps):
            cells = [p.name, p.version, p.author,
                     ", ".join(p.drivers) if p.drivers else "-",
                     str(p.rules) if p.rules else "-", p.status,
                     (p.description.splitlines()[0] if p.description else "")]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 5:
                    item.setForeground(QColor(110, 235, 165) if p.ok
                                       else QColor(255, 92, 76))
                if not p.ok and col == 6:
                    item.setForeground(QColor(232, 163, 58))
                self.table.setItem(row, col, item)

    def _reload(self) -> None:
        before = set(self.manager.driver_ids())
        self.manager.reload()
        after = set(self.manager.driver_ids())
        added = after - before
        self._fill()
        self.reloaded.emit()
        msg = f"已加载 {len(self.manager.plugins)} 个插件"
        if added:
            msg += f"，新增驱动：{', '.join(sorted(added))}"
        QMessageBox.information(self, "重新加载", msg)
