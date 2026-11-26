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

#: 品牌色：钛金属外框 + 深海曜石雷达盘 + 冰青扫频（程序化兜底绘制用）。
C_BEZEL_HI = QColor(148, 163, 184)
C_BEZEL_LO = QColor(39, 50, 68)
C_DIAL = QColor(11, 19, 32)
C_LIGHT = QColor(56, 189, 248)


def draw_logo(size: int = 512) -> QImage:
    """程序化绘制兜底图标：钛金属圆角方框 + 深海曜石雷达扇扫 + 多类高亮目标点。"""
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    s = float(size)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)

    # 外框：圆角方形，冷钛金属渐变
    grad = QLinearGradient(0, 0, s, s)
    grad.setColorAt(0.0, C_BEZEL_HI)
    grad.setColorAt(1.0, C_BEZEL_LO)
    path = QPainterPath()
    path.addRoundedRect(QRectF(s * 0.03, s * 0.03, s * 0.94, s * 0.94),
                        s * 0.22, s * 0.22)
    p.fillPath(path, grad)

    c = QPointF(s / 2, s / 2)
    dial_r = s * 0.375
    p.setPen(QPen(QColor(15, 23, 42), s * 0.018))
    p.setBrush(C_DIAL)
    p.drawEllipse(c, dial_r, dial_r)

    r = dial_r * 0.88
    # 外圈 + 内圈
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(148, 163, 184, 72), s * 0.010))
    p.drawEllipse(c, r, r)
    p.setPen(QPen(QColor(148, 163, 184, 42), s * 0.007))
    p.drawEllipse(c, r * 0.62, r * 0.62)

    # 扫描扇区：以 12 点为基准，顺时针 52°
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(C_LIGHT.red(), C_LIGHT.green(), C_LIGHT.blue(), 86))
    p.drawPie(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), 38 * 16, 52 * 16)

    # 扫描线
    p.setPen(QPen(C_LIGHT, s * 0.018, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(c, QPointF(c.x() + r * 0.94 * 0.788, c.y() - r * 0.94 * 0.616))

    # 目标光点（锁定琥珀金 + 告警珊瑚红 + 冰青回波）
    p.setPen(QPen(QColor(251, 191, 36, 170), s * 0.008))
    p.setBrush(Qt.NoBrush)
    lock_pt = QPointF(c.x() - r * 0.42, c.y() - r * 0.28)
    p.drawEllipse(lock_pt, s * 0.056, s * 0.056)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(251, 191, 36))
    p.drawEllipse(lock_pt, s * 0.032, s * 0.032)
    p.setBrush(QColor(251, 113, 133))
    p.drawEllipse(QPointF(c.x() + r * 0.34, c.y() + r * 0.34), s * 0.028, s * 0.028)
    p.setBrush(QColor(224, 242, 254))
    p.drawEllipse(QPointF(c.x() - r * 0.44, c.y() + r * 0.42), s * 0.023, s * 0.023)

    p.end()
    return img


def _scale_from_master(master_path: Path, size: int) -> QImage:
    """从高分辨率透明底 logo.png 缩放生成指定尺寸，优先用 Pillow Lanczos 保锐度。"""
    try:
        from PIL import Image

        with Image.open(master_path) as pil_im:
            resized = pil_im.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
            raw = resized.tobytes("raw", "RGBA")
            return QImage(raw, size, size, size * 4, QImage.Format_RGBA8888).copy()
    except Exception:
        src = QImage(str(master_path))
        return src.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


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
    logo_path = OUT / "logo.png"
    procedural = "--procedural" in sys.argv[1:]

    if procedural or not logo_path.exists():
        big = draw_logo(512)
        big.save(str(logo_path))
        print(f"已程序化生成 {logo_path}")

    sizes = [16, 24, 32, 48, 64, 128, 256]
    if logo_path.exists() and not procedural:
        ims = [_scale_from_master(logo_path, n) for n in sizes]
    else:
        ims = [draw_logo(n) for n in sizes]
    write_ico(OUT / "app.ico", ims)
    print(f"已生成 {OUT / 'app.ico'}  ({len(ims)} 个尺寸)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
