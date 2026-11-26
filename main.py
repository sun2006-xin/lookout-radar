"""瞭望 Lookout 入口。

用法：
    python main.py                     打开界面，默认虚拟雷达
    python main.py --selftest          无界面跑一遍核心链路

日志写到 data/radarscope.log，界面崩了也能事后查。
"""
from __future__ import annotations

import logging
import sys

from radarscope import paths


def _setup_logging() -> None:
    log_path = paths.data_dir() / "radarscope.log"
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    # 打包成窗口程序后没有控制台，sys.stdout 可能是 None，加进去反而会报错
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )


def _headless_selftest() -> int:
    """`Lookout.exe --selftest`：不创建任何窗口，靠退出码表达成败。"""
    from radarscope.core.selftest import run_core_checks

    log = logging.getLogger("radarscope")
    log.info("开始无界面自检")
    ok, msg = run_core_checks()
    if ok:
        log.info(msg)
        log.info("SELFTEST OK")
        return 0
    log.error("自检失败：%s", msg)
    return 1


def main() -> int:
    _setup_logging()

    if "--selftest" in sys.argv:
        return _headless_selftest()

    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication

    import radarscope.drivers          # noqa: F401  导入即注册所有驱动
    from radarscope.ui.mainwindow import MainWindow

    log = logging.getLogger("radarscope")

    # 高 DPI 必须在 QApplication 创建前设置
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)

    # 任何未捕获异常都不许让窗口直接消失
    def _hook(exc_type, exc, tb):
        log.critical("未捕获异常", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook

    # 启动时就套上用户上次选的主题，否则会先闪一下默认样式再变
    from PyQt5.QtCore import QSettings

    from radarscope.ui.theme import DEFAULT_THEME, build_stylesheet
    from radarscope.ui.winstyle import apply_modern_style, make_app_icon

    apply_modern_style(app)
    app.setWindowIcon(make_app_icon())
    theme = str(QSettings("RadarScope", "Console").value("theme", DEFAULT_THEME))
    app.setStyleSheet(build_stylesheet(theme))
    win = MainWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
