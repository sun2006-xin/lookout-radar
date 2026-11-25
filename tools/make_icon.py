"""生成品牌图标（PNG + ICO）。

不启动界面，纯离屏绘制：QImage + QPainter 不需要窗口，
跑的时候甚至可以设 QT_QPA_PLATFORM=offscreen 彻底不碰显示器。

    .venv\\Scripts\\python.exe tools\\make_icon.py

产出：
    assets/logo.png       512 透明底，README 用
    assets/app.ico        多尺寸，Windows 快捷方式/窗口图标用
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QPointF, QRectF, Qt          # noqa: E402
from PyQt5.QtGui import (QColor, QGuiApplication, QImage, QLinearGradient,  # noqa: E402
                         QPainter, QPainterPath, QPen)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets"

#: 品牌色：深蓝 → 青。和默认的"冰川蓝"色板同一家族。
C_DEEP = QColor(16, 42, 78)
C_MID = QColor(31, 111, 235)
C_LIGHT = QColor(96, 196, 255)


def draw_logo(size: int = 512) -> QImage:
    """画一枚图标：圆角方块 + 雷达扇扫 + 一只"眼睛"的弧线。

    寓意：软件替代人眼去"看"，而雷达的扫描扇区就是它的瞳孔。
    """
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    s = float(size)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)

    # 底板：圆角方形，深蓝渐变
    grad = QLinearGradient(0, 0, s, s)
    grad.setColorAt(0.0, C_MID)
    grad.setColorAt(1.0, C_DEEP)
    path = QPainterPath()
    path.addRoundedRect(QRectF(s * 0.03, s * 0.03, s * 0.94, s * 0.94),
                        s * 0.22, s * 0.22)
    p.fillPath(path, grad)

    c = QPointF(s / 2, s / 2)
    r = s * 0.34

    # 外圈 + 内圈
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 70), s * 0.012))
    p.drawEllipse(c, r, r)
    p.setPen(QPen(QColor(255, 255, 255, 38), s * 0.008))
    p.drawEllipse(c, r * 0.62, r * 0.62)

    # 扫描扇区：以 12 点为基准，顺时针 52°
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(C_LIGHT.red(), C_LIGHT.green(), C_LIGHT.blue(), 90))
    p.drawPie(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), 38 * 16, 52 * 16)

    # 扫描线
    p.setPen(QPen(C_LIGHT, s * 0.018, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(c, QPointF(c.x() + r * 0.92 * 0.788, c.y() - r * 0.92 * 0.616))

    # 两个目标点
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 138, 96))
    p.drawEllipse(QPointF(c.x() - r * 0.40, c.y() - r * 0.28), s * 0.035, s * 0.035)
    p.setBrush(QColor(255, 214, 120))
    p.drawEllipse(QPointF(c.x() + r * 0.26, c.y() + r * 0.46), s * 0.026, s * 0.026)

    p.end()
    return img


def write_ico(path: Path, images: list[QImage]) -> None:
    """写多尺寸 ICO。每帧直接塞 PNG（Vista 之后都支持）。"""
    import tempfile

    # QImage.save() 不接受内存流，先落临时文件再读回来
    blobs: list[bytes] = []
    with tempfile.TemporaryDirectory() as td:
        for k, im in enumerate(images):
            fp = Path(td) / f"{k}.png"
            im.save(str(fp), "PNG")
            blobs.append(fp.read_bytes())

    head = struct.pack("<HHH", 0, 1, len(blobs))
    offset = len(head) + 16 * len(blobs)
    entries = b""
    for im, data in zip(images, blobs):
        w = 0 if im.width() >= 256 else im.width()
        h = 0 if im.height() >= 256 else im.height()
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(head + entries + b"".join(blobs))


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)   # noqa: F841
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw_logo(512)
    big.save(str(OUT / "logo.png"))

    sizes = [16, 24, 32, 48, 64, 128, 256]
    ims = [draw_logo(n) for n in sizes]
    write_ico(OUT / "app.ico", ims)
    print(f"已生成 {OUT / 'logo.png'}")
    print(f"已生成 {OUT / 'app.ico'}  ({len(ims)} 个尺寸)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
