"""告警规则编辑器。

规则本来就是 dataclass，所以"能不能在界面上改"纯粹是层窗户纸。
这里做三件事：列表、编辑、存盘。参数用 JSON 编辑而不是堆十几个输入框——
因为每种规则类型的参数完全不同，硬做表单会变成一堆空着的框。

改完点「保存并应用」：立刻生效 + 写入 data/rules.json，下次启动自动读回。
"""
from __future__ import annotations

import json

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..core.alarm import PARAM_HINTS, AlarmEngine, AlarmRule

KINDS = ["zone", "poly_zone", "range_lt", "range_gt", "speed_ge", "count_ge", "lost", "offline"]
LEVELS = [("crit", "紧急"), ("warn", "警告"), ("info", "提示")]


class ZoneEditDialog(QDialog):
    """单个圆形警戒区的友好编辑弹窗（名称 / 级别 / 坐标 / 半径 / 启用）。"""

    def __init__(self, rule: AlarmRule, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"编辑警戒区 · {rule.name}")
        self.resize(360, 290)
        p = rule.params or {}

        self.ed_name = QLineEdit(rule.name)
        self.cb_level = QComboBox()
        for key, label in LEVELS:
            self.cb_level.addItem(label, key)
        li = self.cb_level.findData(rule.level)
        if li >= 0:
            self.cb_level.setCurrentIndex(li)

        self.sp_cx = QDoubleSpinBox()
        self.sp_cx.setRange(-50000.0, 50000.0)
        self.sp_cx.setDecimals(2)
        self.sp_cx.setSingleStep(0.5)
        self.sp_cx.setSuffix(" m")
        self.sp_cx.setValue(float(p.get("cx", 0.0)))

        self.sp_cy = QDoubleSpinBox()
        self.sp_cy.setRange(-50000.0, 50000.0)
        self.sp_cy.setDecimals(2)
        self.sp_cy.setSingleStep(0.5)
        self.sp_cy.setSuffix(" m")
        self.sp_cy.setValue(float(p.get("cy", 0.0)))

        self.sp_r = QDoubleSpinBox()
        self.sp_r.setRange(0.2, 50000.0)
        self.sp_r.setDecimals(2)
        self.sp_r.setSingleStep(0.5)
        self.sp_r.setSuffix(" m")
        self.sp_r.setValue(max(0.2, float(p.get("r", 1.5))))

        form = QFormLayout()
        form.addRow("名称", self.ed_name)
        form.addRow("告警级别", self.cb_level)
        form.addRow("中心 X", self.sp_cx)
        form.addRow("中心 Y", self.sp_cy)
        form.addRow("半径 R", self.sp_r)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("保存")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(bb)

    def values(self) -> dict:
        return {
            "name": self.ed_name.text().strip() or "警戒区",
            "level": str(self.cb_level.currentData() or "warn"),
            "cx": round(float(self.sp_cx.value()), 3),
            "cy": round(float(self.sp_cy.value()), 3),
            "r": round(float(self.sp_r.value()), 3),
        }


class RuleEditDialog(QDialog):
    def __init__(self, rule: AlarmRule | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑告警规则")
        self.resize(560, 460)

        self.ed_id = QLineEdit(rule.id if rule else "")
        self.ed_name = QLineEdit(rule.name if rule else "")
        self.cb_kind = QComboBox()
        self.cb_kind.addItems(KINDS)
        self.cb_level = QComboBox()
        for key, label in LEVELS:
            self.cb_level.addItem(label, key)
        self.sp_hold = QDoubleSpinBox()
        self.sp_hold.setRange(0.0, 60.0)
        self.sp_hold.setSingleStep(0.1)
        self.sp_cool = QDoubleSpinBox()
        self.sp_cool.setRange(0.0, 600.0)
        self.sp_cool.setSingleStep(1.0)
        self.sp_hyst = QDoubleSpinBox()
        self.sp_hyst.setRange(0.0, 0.9)
        self.sp_hyst.setSingleStep(0.05)
        self.ed_params = QPlainTextEdit()
        self.ed_params.setStyleSheet(
            "QPlainTextEdit{font-family:Consolas;font-size:12px;"
            "background:#101610;color:#CFE0D5;border:1px solid #2A362C;}")
        self.lbl_hint = QLabel("")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color:#8FA396;font-size:11px;")

        if rule:
            self.ed_id.setText(rule.id)
            self.ed_id.setReadOnly(True)
            self.ed_name.setText(rule.name)
            self.cb_kind.setCurrentText(rule.kind)
            i = self.cb_level.findData(rule.level)
            if i >= 0:
                self.cb_level.setCurrentIndex(i)
            self.sp_hold.setValue(rule.hold)
            self.sp_cool.setValue(rule.cooldown)
            self.sp_hyst.setValue(rule.hysteresis)
            self.ed_params.setPlainText(json.dumps(rule.params, ensure_ascii=False))
        else:
            self.sp_hold.setValue(1.0)
            self.sp_cool.setValue(8.0)
            self.sp_hyst.setValue(0.15)
            self.ed_params.setPlainText('{"m": 1.0}')
        self.cb_kind.currentTextChanged.connect(self._on_kind)
        self._on_kind(self.cb_kind.currentText())

        form = QFormLayout()
        form.addRow("规则 ID", self.ed_id)
        form.addRow("名称", self.ed_name)
        form.addRow("类型", self.cb_kind)
        form.addRow("级别", self.cb_level)
        form.addRow("去抖(秒)", self.sp_hold)
        form.addRow("冷却(秒)", self.sp_cool)
        form.addRow("滞回(0~0.9)", self.sp_hyst)
        form.addRow("参数(JSON)", self.ed_params)
        form.addRow("", self.lbl_hint)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("确定")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(bb)

    def _on_kind(self, kind: str) -> None:
        self.lbl_hint.setText("示例：" + PARAM_HINTS.get(kind, "{}"))

    def _ok(self) -> None:
        rid = self.ed_id.text().strip()
        if not rid:
            QMessageBox.warning(self, "参数错误", "规则 ID 不能为空")
            return
        try:
            params = json.loads(self.ed_params.toPlainText() or "{}")
            if not isinstance(params, dict):
                raise ValueError("参数必须是 JSON 对象")
        except Exception as exc:
            QMessageBox.warning(self, "参数错误", f"参数不是合法 JSON：{exc}")
            return
        if not self.ed_name.text().strip():
            QMessageBox.warning(self, "参数错误", "名称不能为空")
            return
        self.result_rule = AlarmRule(
            id=rid, name=self.ed_name.text().strip(),
            kind=self.cb_kind.currentText(), params=params,
            level=self.cb_level.currentData(),
            hold=self.sp_hold.value(), cooldown=self.sp_cool.value(),
            hysteresis=self.sp_hyst.value(), enabled=True)
        self.accept()


class RulesDialog(QDialog):
    COLS = ["ID", "名称", "类型", "级别", "去抖", "冷却", "滞回", "启用", "参数"]

    def __init__(self, engine: AlarmEngine, parent=None):
        super().__init__(parent)
        self.setWindowTitle("告警规则")
        self.resize(900, 480)
        self.engine = engine
        self.rules: list[AlarmRule] = list(engine.rules)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(8, QHeaderView.Stretch)

        btn_add = QPushButton("新增")
        btn_add.clicked.connect(self._add)
        btn_edit = QPushButton("编辑")
        btn_edit.clicked.connect(self._edit)
        btn_del = QPushButton("删除")
        btn_del.clicked.connect(self._del)
        btn_def = QPushButton("恢复内置")
        btn_def.clicked.connect(self._restore)
        btn_save = QPushButton("保存并应用")
        btn_save.clicked.connect(self._save)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.reject)

        bar = QHBoxLayout()
        for b in (btn_add, btn_edit, btn_del, btn_def):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(btn_save)
        bar.addWidget(btn_close)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("规则改动会在点「保存并应用」后立刻生效，并写入 data/rules.json"))
        lay.addWidget(self.table, 1)
        lay.addLayout(bar)
        self._fill()

    # ------------------------------------------------------------------ #
    def _fill(self) -> None:
        self.table.setRowCount(len(self.rules))
        for row, r in enumerate(self.rules):
            cells = [r.id, r.name, r.kind, r.level, f"{r.hold:.1f}",
                     f"{r.cooldown:.0f}", f"{r.hysteresis:.2f}",
                     "是" if r.enabled else "否",
                     json.dumps(r.params, ensure_ascii=False)]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 3:
                    item.setForeground(QColor(255, 92, 76) if r.level == "crit"
                                       else QColor(232, 163, 58) if r.level == "warn"
                                       else QColor(140, 160, 150))
                if not r.enabled:
                    item.setForeground(QColor(110, 120, 115))
                self.table.setItem(row, col, item)

    def _current(self) -> int:
        return self.table.currentRow()

    def _add(self) -> None:
        dlg = RuleEditDialog(None, self)
        if dlg.exec_() == QDialog.Accepted:
            r = dlg.result_rule
            if any(x.id == r.id for x in self.rules):
                QMessageBox.warning(self, "重复", f"规则 ID '{r.id}' 已存在")
                return
            self.rules.append(r)
            self._fill()

    def _edit(self) -> None:
        i = self._current()
        if i < 0 or i >= len(self.rules):
            return
        dlg = RuleEditDialog(self.rules[i], self)
        if dlg.exec_() == QDialog.Accepted:
            self.rules[i] = dlg.result_rule
            self._fill()

    def _del(self) -> None:
        from ..core.alarm import DEFAULT_RULES

        i = self._current()
        if i < 0 or i >= len(self.rules):
            return
        r = self.rules[i]
        builtin_ids = {x.id for x in DEFAULT_RULES}
        if r.id in builtin_ids:
            # 内置规则若直接从列表 pop，merge_rules 会重新补回；改为置 enabled=False 持久停用
            r.enabled = False
            self._fill()
            QMessageBox.information(self, "已停用", f"内置规则「{r.name}」已设为停用，点「保存并应用」后生效")
        else:
            self.rules.pop(i)
            self._fill()

    def _restore(self) -> None:
        from ..core.alarm import DEFAULT_RULES
        self.rules = list(DEFAULT_RULES)
        self._fill()

    def _save(self) -> None:
        try:
            self.engine.set_rules(self.rules)
            path = self.engine.save_path if hasattr(self.engine, "save_path") else None
            if path:
                from ..core.alarm import rules_to_json
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(rules_to_json(self.rules))
            QMessageBox.information(self, "已应用", f"{len(self.rules)} 条规则已生效")
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
