"""事件日志面板：告警、连接、错误、操作全部落在这里。

为什么不只用弹窗：操作员离开座位两分钟回来，弹窗早被关掉了，
但日志还在。正经雷达显控台都有这块区域，而且能导出。
"""
from __future__ import annotations

import time

from PyQt5.QtWidgets import QPlainTextEdit


class EventLog(QPlainTextEdit):
    COLORS = {
        "info": "#8FA396",
        "ok": "#6EEBA5",
        "warn": "#E8A33A",
        "crit": "#FF5A46",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(800)
        self.setStyleSheet(
            "QPlainTextEdit{background:#0A0D0B;color:#9FB2A6;border:1px solid #1F2A22;"
            "font-family:Consolas,'Microsoft YaHei';font-size:11px;}")
        self._lines: list[tuple[float, str, str]] = []

    def push(self, level: str, text: str, ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        self._lines.append((ts, level, text))
        color = self.COLORS.get(level, self.COLORS["info"])
        stamp = time.strftime("%H:%M:%S", time.localtime(ts))
        safe = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        self.appendHtml(
            f'<span style="color:#4E6157">{stamp}</span> '
            f'<span style="color:{color}">{safe}</span>')
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def export(self, path: str) -> int:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                for ts, level, text in self._lines:
                    fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}\t"
                             f"{level}\t{text}\n")
            return len(self._lines)
        except Exception:
            return -1
