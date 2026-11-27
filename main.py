"""瞭望 Lookout 入口。

用法：
    python main.py                     打开界面，默认虚拟雷达
    python main.py --selftest          无界面跑一遍核心链路

日志写到 data/radarscope.log，界面崩了也能事后查。
"""
from __future__ import annotations

import logging
import logging.handlers
import sys

from radarscope import paths


def _setup_logging() -> None:
    log_path = paths.data_dir() / "radarscope.log"
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
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


def _headless_daemon(argv: list[str]) -> int:
    """`Lookout --daemon`：纯无界面后台采集、追踪、转发与落库服务（ROADMAP I1）。"""
    import argparse
    import time
    import radarscope.drivers  # noqa: F401
    from radarscope.core.runtime import RadarEngineRuntime

    p = argparse.ArgumentParser(prog="Lookout --daemon")
    p.add_argument("--daemon", action="store_true")
    p.add_argument("--driver", default="sim")
    p.add_argument("--endpoint", default="")
    p.add_argument("--forward", default="")
    p.add_argument("--duration", type=float, default=0.0, help="运行秒数（0 表示持续运行直到 Ctrl+C）")
    p.add_argument("--no-db", action="store_true")
    args, _ = p.parse_known_args(argv)

    rt = RadarEngineRuntime(
        driver_id=args.driver,
        endpoint=args.endpoint,
        db_path=None if args.no_db else (paths.data_dir() / "radarscope.db"),
        forward_target=args.forward or None,
    )
    rt.open()
    rt.start()
    log = logging.getLogger("radarscope")
    log.info("无头守护引擎已启动：driver=%s endpoint=%s", args.driver, args.endpoint or "virtual")
    t0 = time.monotonic()
    try:
        while args.duration <= 0.0 or (time.monotonic() - t0) < args.duration:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        rt.close()
    log.info("无头守护引擎已退出：累计帧=%d", rt.frames)
    return 0


def main() -> int:
    _setup_logging()

    if "--selftest" in sys.argv:
        return _headless_selftest()
    if "--daemon" in sys.argv:
        return _headless_daemon(sys.argv[1:])

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
