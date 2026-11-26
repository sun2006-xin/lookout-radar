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

    COLS = ["插件", "版本", "作者", "驱动", "规则", "校验 (SHA256)", "状态", "说明"]

    def __init__(self, manager: PluginManager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("插件管理")
        self.resize(920, 440)
        self.manager = manager

        # S3：插件是任意代码，必须给出视觉警示，不能只写在文档里
        warn = QLabel("⚠ 插件是任意 Python 代码，加载即在本机执行。"
                      "只加载你自己写的、或经过 SHA256 信任确认的来源。")
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
        hh.setSectionResizeMode(7, QHeaderView.Stretch)

        btn_trust = QPushButton("信任并重载所选")
        btn_trust.clicked.connect(self._trust_selected)
        btn_reload = QPushButton("重新加载")
        btn_reload.clicked.connect(self._reload)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(btn_trust)
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
            sha_txt = (p.sha256[:12] + "…" + (" [已信任]" if p.trusted else " [待确认]")) if p.sha256 else "-"
            cells = [p.name, p.version, p.author,
                     ", ".join(p.drivers) if p.drivers else "-",
                     str(p.rules) if p.rules else "-", sha_txt, p.status,
                     (p.description.splitlines()[0] if p.description else "")]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 6:
                    item.setForeground(QColor(16, 140, 82) if (p.ok and p.trusted)
                                       else QColor(220, 60, 48))
                if not p.ok and col == 7:
                    item.setForeground(QColor(210, 130, 20))
                self.table.setItem(row, col, item)

    def _trust_selected(self) -> None:
        row = self.table.currentRow()
        ps = self.manager.plugins
        if row < 0 or row >= len(ps):
            QMessageBox.information(self, "信任插件", "请先在列表里选中一个插件")
            return
        p = ps[row]
        if QMessageBox.question(
                self, "确认信任插件",
                f"确认信任插件「{p.name}」？\nSHA256: {p.sha256}\n\n"
                "信任后将记录其文件摘要，文件未被修改前可直接加载。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.manager.approve_plugin(p.path)
        self._reload()

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
