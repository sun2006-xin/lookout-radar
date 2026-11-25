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
    """无界面自检：验证依赖是否完整、核心链路是否通。

    `Lookout.exe --selftest` 会走这里，**不创建任何窗口**。
    存在的理由：打包之后想确认"这个 exe 到底能不能跑"，
    总不能每次都开个窗口看一眼；而且窗口程序没有 stdout，
    所以结果写日志 + 用退出码表示成败，脚本里好判断。
    """
    import logging

    import numpy as np

    log = logging.getLogger("radarscope")
    log.info("开始无界面自检")
    try:
        import radarscope.drivers  # noqa: F401
        from radarscope.core import pipeline
        from radarscope.core.driver import available_drivers
        from radarscope.core.model import Capabilities
        from radarscope.paths import data_dir

        drivers = available_drivers()
        assert drivers, "没有任何驱动被注册"

        # 真正驱动跑一遍：连续取若干帧，确认能出数据、矩阵形状对、数值正常。
        # 注意要给够时间：旋转/凝视模式扫完一个扇区需要几秒，
        # 只看头 0.5 秒会得到"全是噪声"的假结论。
        import time as _t
        d = drivers["sim"]()
        d.open()
        frame = None
        best = 0.0
        deadline = _t.monotonic() + 4.0
        while _t.monotonic() < deadline:
            f = d.read()
            if f is not None:
                frame = f
                best = max(best, float(f.scan.max()))
                if best > 0.3:
                    break
            _t.sleep(0.01)
        d.close()
        assert frame is not None and frame.has_scan, "模拟器没有产出数据"
        assert frame.scan.ndim == 2 and frame.scan.size > 100, "矩阵形状异常"
        assert best > 0.3, f"矩阵里始终没有回波（峰值 {best:.2f}）"
        dets = pipeline.detect_targets(frame, threshold=0.3, max_targets=8)
        assert isinstance(dets, list)

        caps = Capabilities(has_scan=True, has_azimuth=True,
                            range_min=0.2, range_max=8.0)
        synth = pipeline.synthesize_scan(caps, [])
        assert synth.scan.shape == (len(synth.az_axis), len(synth.r_axis))
        assert np.isfinite(frame.scan).all(), "矩阵里有 NaN/Inf"

        log.info("驱动 %d 个 · 帧 %s · 检出 %d 个目标 · 数据目录 %s",
                 len(drivers), frame.scan.shape, len(dets), data_dir())
        log.info("SELFTEST OK")
        return 0
    except Exception as exc:
        import traceback
        log.error("自检失败：%s", exc)
        log.error(traceback.format_exc())
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

    theme = str(QSettings("RadarScope", "Console").value("theme", DEFAULT_THEME))
    app.setStyleSheet(build_stylesheet(theme))
    win = MainWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
