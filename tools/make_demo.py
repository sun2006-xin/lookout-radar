"""生成 README 用的演示动图（离屏，不弹任何窗口）。

    QT_QPA_PLATFORM=offscreen .venv\\Scripts\\python.exe tools\\make_demo.py

产出 assets/demo.gif：接上模拟器后 PPI 扫描推进、目标出现、拖尾生长的 20 帧循环。
需要 Pillow（仅文档工具依赖，运行本软件不需要）。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PyQt5.QtCore import Qt                                    # noqa: E402
from PyQt5.QtGui import QColor, QImage, QPainter               # noqa: E402
from PyQt5.QtWidgets import QApplication                      # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))          # 从 tools/ 直接跑时项目根不在 path
OUT = ROOT / "assets" / "demo.gif"
FRAMES = 20


def main() -> int:
    try:
        from PIL import Image
    except ImportError:
        print("需要 Pillow：.venv\\Scripts\\pip install pillow")
        return 1

    import io

    from PyQt5.QtCore import QBuffer, QIODevice

    import radarscope.drivers  # noqa: F401
    from radarscope.core.driver import available_drivers
    from radarscope.core.pipeline import detect_targets
    from radarscope.ui.ppi import PPIWidget
    from radarscope.ui.theme import DEFAULT_PALETTE, build_stylesheet

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setStyleSheet(build_stylesheet("light"))

    d = available_drivers()["sim"]()
    d.open(mode="staring")
    caps = d.capabilities()

    w = PPIWidget()
    w.resize(560, 560)
    w.set_capabilities(caps)
    w.set_palette(DEFAULT_PALETTE)
    w.set_orientation(False, 0.0)

    imgs = []
    for k in range(FRAMES):
        f = None
        for _ in range(12):
            f = d.read()
            if f is not None:
                break
            time.sleep(0.01)
        if f is not None:
            tracks = detect_targets(f, threshold=0.30, max_targets=8)
            w.push_frame(f, tracks)
        app.processEvents()

        img = QImage(w.size(), QImage.Format_ARGB32)
        img.fill(QColor(247, 249, 252))
        p = QPainter(img)
        w.render(p)
        p.end()
        qbuf = QBuffer()
        qbuf.open(QIODevice.WriteOnly)
        img.save(qbuf, "PNG")
        im = Image.open(io.BytesIO(bytes(qbuf.data()))).convert("RGB")
        qbuf.close()
        imgs.append(im)
        time.sleep(0.08)
    d.close()

    if not imgs:
        print("没有产生任何帧")
        return 1
    # 统一缩小，控制 README 里 GIF 的体积
    size = (480, 480)
    imgs = [im.resize(size, Image.LANCZOS) for im in imgs]
    pal = imgs[0].quantize(colors=128)
    frames = [im.quantize(palette=pal) for im in imgs]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(str(OUT), save_all=True, append_images=frames[1:],
                   duration=140, loop=0, optimize=True, disposal=2)
    print(f"已生成 {OUT} · {len(frames)} 帧 · {OUT.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
