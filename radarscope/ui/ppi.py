"""PPI 环视显示器。

性能关键：不做逐点 QPainter 绘制，而是
    1. 预计算 LUT：(方位格, 距离格) -> 画布像素下标   （只在轴变化时算一次）
    2. 每帧一次 numpy 花式赋值把整幅图铺进 float 画布
    3. 整幅转 QImage + 256 色调色板，一次 drawImage 贴上去
    4. 余晖 = 画布按时间常数衰减（不是按帧数，否则不同雷达帧率不同，拖尾长度会不一样）

这套做法的开销基本恒定，20~50 帧/秒无压力。
"""
from __future__ import annotations

import time
from collections import deque
from math import atan2, cos, degrees, exp, hypot, radians, sin

import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPolygonF
from PyQt5.QtWidgets import QSizePolicy, QWidget

from ..core.model import Capabilities, RadarFrame, Target
from ..core.viewmath import fmt_bearing, fmt_distance, point_to_polar
from .theme import DEFAULT_PALETTE, PALETTES, color_table


def _fmt_rng(m: float, rmax: float) -> str:
    """量程过千米就按公里显示，否则 20000m 这种数字在屏幕上会挤成一团。"""
    if rmax >= 1000.0:
        return f"{m / 1000.0:.1f}km"
    return f"{m:.1f}m"


def _dilate3(a: np.ndarray) -> np.ndarray:
    """3x3 最大值滤波（可分离实现，两趟一维）。

    必要性：扫描矩阵只有一万多个格子，画布有十几万像素，一格只点亮一个像素，
    看上去就是一堆散点而不是一块回波。做一次最大值膨胀把格子之间填上，
    亮度不会被拉低（用的是 max 不是平均），同时保留边缘。
    """
    b = a.copy()
    np.maximum(b[1:, :], a[:-1, :], out=b[1:, :])
    np.maximum(b[:-1, :], a[1:, :], out=b[:-1, :])
    c = b.copy()
    np.maximum(c[:, 1:], b[:, :-1], out=c[:, 1:])
    np.maximum(c[:, :-1], b[:, 1:], out=c[:, :-1])
    return c


class PPIWidget(QWidget):
    RES = 512          # 内部画布分辨率，与外框解耦

    #: 点中某个目标时抛出航迹号；点在空白处抛 -1（表示解除锁定）
    targetPicked = pyqtSignal(int)
    zoneDrawn = pyqtSignal(float, float, float)   # 圆心 x, y, 半径（米）
    zoneDeleted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(380, 380)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAutoFillBackground(False)

        self._caps = Capabilities()
        self._palette_name = DEFAULT_PALETTE
        self._ctable = self._ctable_for(self._palette_name)

        self._radius_px = self.RES * 0.47
        self._cflat = np.zeros(self.RES * self.RES, dtype=np.float32)
        self._lut = np.zeros((1, 1), dtype=np.int32)
        self._valid = np.zeros((1, 1), dtype=bool)
        self._expand = np.zeros(1, dtype=np.int64)
        self._az_eff = np.zeros(1, dtype=np.float32)
        self._r_axis: np.ndarray | None = None
        self._n_rng = 0

        self._tracks: list[Target] = []
        #: 尾迹：每个航迹号最近若干个位置。船用/岸基雷达都有，用于判断运动趋势
        self._trails: dict[int, deque] = {}
        self.trail_len = 36
        self.show_trails = True
        #: 告警禁入区 (cx, cy, r, level)，与 alarm 规则里的 zone 同源
        self._zones: list[tuple[float, float, float, str]] = []
        self.show_zones = True
        #: 电子方位线 (方位°, 距离m)：经典雷达的 EBL/VRM
        self._ebl: tuple[float, float] | None = None
        self.show_ebl = True
        #: 画禁入区模式：按下拖拽出一个圆
        self.zone_mode = False
        self._zone_drag: tuple[float, float, float, float] | None = None
        #: 真运动显示时本舰已行驶的距离（米），画面按它整体平移
        self.origin = (0.0, 0.0)
        self.true_motion = False
        #: AIS 目标（本地米制坐标 + 名称）
        self.map_layer = None      # 由主窗口注入，需提供 image/mpp/anchor_px
        self._ais: list[tuple[float, float, str, float]] = []
        self.show_ais = True
        self._sweep_az: float | None = None
        self._coverage: list[dict] | None = None
        self._gaps: list[tuple[float, float]] | None = None
        #: 点击测距的读数：(屏幕x, 屏幕y, 距离m, 方位度)
        self._probe: tuple[float, float, float, float] | None = None
        self._rot = 0.0             # 朝向旋转（弧度），北向朝上时 = 航向
        self._rot_deg = 0.0
        self._heading_deg = 0.0   # 平台航向，画地图时要用
        self._north_up = False
        self._t_prev = 0.0
        self.tau = 1.1              # 余晖时间常数，秒

    # ------------------------------------------------------------------ #
    def set_capabilities(self, caps: Capabilities) -> None:
        self._caps = caps
        self._r_axis = None         # 强制重建 LUT
        self.reset()

    def set_palette(self, name: str) -> None:
        if name not in PALETTES:
            return
        self._palette_name = name
        self._ctable = self._ctable_for(name)
        self.update()

    @staticmethod
    def _ctable_for(name: str) -> list[int]:
        """取调色板，把**低电平做成透明**。

        两个作用：
        1. 叠加地图底图时，底图能从"没有回波"的地方透出来；
            否则整块光栅是不透明的，地图永远被盖住。
        2. 顺手去掉了北向旋转时露出的方形边角（透明的角不显眼）。
        阈值以下全透明，等于给显示加了一道很低的门限 —— 真实雷达的
        STC 在显示端也是这么干的。
        """
        tbl = color_table(name)
        for i in range(26):
            tbl[i] = 0x00000000
        return tbl

    def reset(self) -> None:
        self._cflat[:] = 0.0
        self._tracks = []
        self._sweep_az = None
        self._t_prev = 0.0
        self.update()

    # ------------------------------------------------------------------ #
    def push_frame(self, frame: RadarFrame, tracks: list[Target]) -> None:
        if not frame.has_scan or frame.az_axis is None or frame.r_axis is None:
            return
        if (self._r_axis is None
                or self._r_axis.shape != frame.r_axis.shape
                or not np.array_equal(self._r_axis, frame.r_axis)
                or self._az_eff.shape[0] != self._predict_az_eff_size(frame.az_axis)):
            self._build_lut(frame.az_axis, frame.r_axis)

        now = frame.timestamp or time.monotonic()
        dt = (now - self._t_prev) if self._t_prev else 0.05
        self._t_prev = now
        self._cflat *= exp(-max(1e-3, min(dt, 1.0)) / self.tau)

        try:
            vals = frame.scan[self._expand, :]
        except IndexError:
            return
        if vals.shape != self._valid.shape:
            return
        self._cflat[self._lut[self._valid]] = vals[self._valid]

        fresh = frame.meta.get("fresh_az")
        if isinstance(fresh, tuple) and len(self._az_eff) > 1:
            a0, a1 = fresh
            idx = int((a0 + (a1 - a0) / 2) % len(self._az_eff))
            self._sweep_az = float(self._az_eff[idx])
        else:
            self._sweep_az = None

        self._coverage = frame.meta.get("coverage")
        self._gaps = frame.meta.get("gaps")

        self._tracks = tracks
        self._update_trails(tracks)
        self.update()

    # ------------------------------------------------------------------ #
    def _predict_az_eff_size(self, az_axis: np.ndarray) -> int:
        if az_axis.size == 1 and self._caps.az_fov > 0:
            return 61
        return az_axis.size

    def _build_lut(self, az_axis: np.ndarray, r_axis: np.ndarray) -> None:
        n_az = az_axis.size
        if n_az == 1 and self._caps.az_fov > 0:
            span = radians(min(359.0, self._caps.az_fov))
            az_eff = np.linspace(-span / 2, span / 2, 61, dtype=np.float32)
            expand = np.zeros(61, dtype=np.int64)
        else:
            az_eff = az_axis.astype(np.float32)
            expand = np.arange(n_az, dtype=np.int64)

        rmin, rmax = self._caps.range_min, self._caps.range_max
        r_px = (r_axis.astype(np.float32) - rmin) / max(1e-6, rmax - rmin) * self._radius_px
        px = self.RES / 2 + np.outer(np.sin(az_eff), r_px)
        py = self.RES / 2 - np.outer(np.cos(az_eff), r_px)
        ix = np.clip(px, 0, self.RES - 1).astype(np.int32)
        iy = np.clip(py, 0, self.RES - 1).astype(np.int32)

        self._lut = iy * self.RES + ix
        self._valid = np.broadcast_to(
            (r_px > 0.5)[None, :] & (r_px <= self._radius_px)[None, :], px.shape
        ).copy()
        self._expand = expand
        self._az_eff = az_eff
        self._r_axis = r_axis.copy()
        self._n_rng = r_axis.size

    # ------------------------------------------------------------------ #
    def paintEvent(self, _ev) -> None:
        pal = PALETTES[self._palette_name]
        w, h = self.width(), self.height()
        side = min(w, h)
        ox, oy = (w - side) / 2.0, (h - side) / 2.0
        cx, cy, R = ox + side / 2.0, oy + side / 2.0, side * 0.47

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(*pal["bg"]))

        # 减一点点底噪再映射颜色，目标更"跳"，也更接近真实显控的 STC 效果
        arr = (np.clip((self._cflat - 0.015) / 0.985, 0.0, 1.0) * 255).astype(np.uint8)
        arr = arr.reshape(self.RES, self.RES)
        # 两趟 3x3 最大值 = 等效 5x5，把方位和距离两个方向的取样缝都补上，
        # 否则距离向只有 72 个格子铺到 240 像素半径上，会画成一条条虚线。
        arr = _dilate3(_dilate3(arr))
        img = QImage(arr.data, self.RES, self.RES, self.RES, QImage.Format_Indexed8)
        img.setColorTable(self._ctable)
        # 光栅按"圆的直径"铺，而不是按整个方框铺。
        # 原来方框边长是 2R/0.94，最外圈回波会比距离环还大出一圈，
        # 看起来就像"圆画歪了"。
        # 真运动：画面整体按本舰位移反向平移，本舰标记自己跑。
        # 网格保持不动 —— 真运动显示里，网格代表"屏幕空间"，运动的是回波。
        offpx = offpy = 0.0
        if self.true_motion:
            rmax = max(1e-6, self._caps.range_max)
            offpx = self.origin[0] / rmax * R
            offpy = -self.origin[1] / rmax * R

        box = QRectF(cx - R, cy - R, 2.0 * R, 2.0 * R)
        # 裁剪 = 屏幕可视圆 ∩ 天线覆盖圆。
        # 只取前者会在真运动时露出"数据被切掉"的直角；
        # 只取后者会画到控件外面。取交集得到的"透镜"形状才是物理上正确的：
        # 天线只覆盖它自己那一圈，屏幕只显示它这一块。
        screen_clip = QPainterPath()
        screen_clip.addEllipse(QPointF(cx, cy), R, R)
        if offpx or offpy:
            ant_clip = QPainterPath()
            ant_clip.addEllipse(QPointF(cx - offpx, cy - offpy), R, R)
            clip = screen_clip.intersected(ant_clip)
        else:
            clip = screen_clip

        p.save()
        if offpx or offpy:
            p.translate(-offpx, -offpy)
        self._draw_map(p, cx, cy, R, pal)
        p.setClipPath(clip)
        if self._rot:
            p.translate(cx, cy)
            p.rotate(self._rot_deg)
            p.translate(-cx, -cy)
        p.drawImage(box, img)
        p.restore()

        # 天线覆盖圈：告诉用户"为什么数据到这儿就没了"
        if self.true_motion:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(*pal["grid"]), 1, Qt.DotLine))
            p.drawEllipse(QPointF(cx - offpx, cy - offpy), R, R)

        self._draw_fov(p, cx, cy, R, pal)
        self._draw_grid(p, cx, cy, R, pal)
        if self._north_up:
            p.setFont(QFont("Consolas", 10, QFont.Bold))
            p.setPen(QPen(QColor(*pal["text"])))
            p.drawText(QPointF(cx - 4, cy - R * 1.02), "N")
        self._draw_coverage(p, cx, cy, R, pal)

        # 这些是"挂在雷达上的东西"，真运动时要跟着平移
        p.save()
        if offpx or offpy:
            p.translate(-offpx, -offpy)
        self._draw_zones(p, cx, cy, R, pal)
        self._draw_sweep(p, cx, cy, R, pal)
        self._draw_trails(p, cx, cy, R, pal)
        self._draw_targets(p, cx, cy, R, pal)
        self._draw_ais(p, cx, cy, R, pal)
        self._draw_zone_drag(p, pal)
        p.restore()

        if self.true_motion:
            self._draw_own_ship(p, QPointF(cx - offpx, cy - offpy), pal)

        self._draw_ebl(p, cx, cy, R, pal)
        self._draw_probe(p, pal)

        p.setPen(QPen(QColor(0, 0, 0, 0)))
        p.end()

    # ------------------------------------------------------------------ #
    def set_alarm_zones(self, zones: list[tuple[float, float, float, str]]) -> None:
        """告警规则里的禁入区，画到屏上。

        规则表里设了"圆心 (0,3) 半径 1.5"却看不见它 —— 那个区到底圈住了什么，
        用眼睛判断不了。把规则画出来，才算把"配置"和"画面"接上了。
        """
        self._zones = list(zones or [])
        self.update()

    def set_ebl(self, az_deg: float | None, rng: float = 0.0) -> None:
        self._ebl = None if az_deg is None else (float(az_deg), float(rng))
        self.clear_probe()
        self.update()

    def _update_trails(self, tracks: list[Target]) -> None:
        if not self.show_trails:
            return
        alive: set[int] = set()
        for t in tracks:
            if t.tid < 0:
                continue
            alive.add(t.tid)
            dq = self._trails.get(t.tid)
            if dq is None:
                dq = deque(maxlen=self.trail_len)
                self._trails[t.tid] = dq
            # 位置没动就不追加，避免静止目标把尾巴堆在一处
            if not dq or abs(dq[-1][0] - t.x) > 1e-3 or abs(dq[-1][1] - t.y) > 1e-3:
                dq.append((t.x, t.y))
        for tid in [k for k in self._trails if k not in alive]:
            del self._trails[tid]

    def _draw_trails(self, p: QPainter, cx, cy, R, pal) -> None:
        if not self.show_trails or not self._trails:
            return
        rmax = max(1e-6, self._caps.range_max)
        base = QColor(*pal["dim"]) if "dim" in pal else QColor(90, 120, 104)
        for tid, pts in self._trails.items():
            if len(pts) < 2:
                continue
            poly = QPolygonF()
            for (x, y) in pts:
                r = hypot(x, y)
                if r > rmax:
                    continue
                poly.append(self._pt(cx, cy, r / rmax * R, atan2(x, y)))
            if poly.size() < 2:
                continue
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(base.red(), base.green(), base.blue(), 90), 1.2))
            p.drawPolyline(poly)
            # 最近几段再描一遍，做出"越旧越淡"的效果
            if poly.size() >= 8:
                recent = QPolygonF([poly.at(i) for i in range(poly.size() - 8, poly.size())])
                p.setPen(QPen(QColor(base.red(), base.green(), base.blue(), 170), 1.6))
                p.drawPolyline(recent)

    def _draw_zones(self, p: QPainter, cx, cy, R, pal) -> None:
        if not self.show_zones or not self._zones:
            return
        rmax = max(1e-6, self._caps.range_max)
        colors = {"crit": QColor(240, 70, 55, 180), "warn": QColor(232, 163, 58, 160),
                  "info": QColor(120, 190, 150, 140)}
        p.setBrush(Qt.NoBrush)
        p.setFont(QFont("Consolas", 8))
        for (x, y, r, level) in self._zones:
            rr = hypot(x, y)
            if rr > rmax:
                continue
            ctr = self._pt(cx, cy, rr / rmax * R, atan2(x, y))
            rad = max(3.0, r / rmax * R)
            col = colors.get(level, colors["warn"])
            p.setPen(QPen(col, 1.2, Qt.DashLine))
            p.drawEllipse(ctr, rad, rad)
            # 圆心画个小十字，让人看清"区"是被圈在哪
            p.setPen(QPen(col, 1))
            p.drawLine(QPointF(ctr.x() - 3, ctr.y()), QPointF(ctr.x() + 3, ctr.y()))
            p.drawLine(QPointF(ctr.x(), ctr.y() - 3), QPointF(ctr.x(), ctr.y() + 3))

    def _draw_ebl(self, p: QPainter, cx, cy, R, pal) -> None:
        """EBL（电子方位线）+ VRM（可变距离圈）。

        真实雷达上的标配：拿它量"那个回波在多少度、多远"。
        我们已经有网格，但量一个具体位置还是得有根可动的线。
        """
        if not self.show_ebl or self._ebl is None:
            return
        az, rng = self._ebl
        col = QColor(150, 200, 255, 190)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(col, 1, Qt.DashLine))
        p.drawLine(QPointF(cx, cy), self._pt(cx, cy, R, radians(az)))

        rmax = max(1e-6, self._caps.range_max)
        if rng > 0:
            ring = max(2.0, min(rng, rmax) / rmax * R)
            p.setPen(QPen(QColor(150, 200, 255, 130), 1, Qt.DotLine))
            p.drawEllipse(QPointF(cx, cy), ring, ring)

        p.setFont(QFont("Consolas", 9))
        txt = f"EBL {az:05.1f}°  VRM {rng:.2f} m"
        tw = p.fontMetrics().horizontalAdvance(txt)
        p.setPen(QPen(col))
        p.drawText(QPointF(max(4.0, cx + R * 0.30 - tw / 2), cy + R * 0.52 + 12), txt)

    # ------------------------------------------------------------------ #
    def _draw_coverage(self, p: QPainter, cx, cy, R, pal) -> None:
        """多站融合时画扇区边界、传感器编号和盲区。

        "6 个 60° 拼 360°"这件事，光说没用，得能一眼看出哪块是哪个模块管的、
        哪里还有洞。盲区用红色弧段标出来，这是验收"无死角"的唯一依据。
        """
        if not self._coverage:
            return

        f = QFont("Consolas", 8)
        p.setFont(f)
        fm = p.fontMetrics()
        dim = QColor(*pal["grid_dim"])

        for i, s in enumerate(self._coverage):
            bearing = float(s.get("bearing", 0.0))
            fov = float(s.get("fov", 120.0))
            online = bool(s.get("online", True))
            col = dim if online else QColor(120, 60, 55, 170)

            for a_deg in (bearing - fov / 2.0, bearing + fov / 2.0):
                p.setPen(QPen(col, 1, Qt.DotLine))
                p.drawLine(QPointF(cx, cy), self._pt(cx, cy, R, radians(a_deg)))

            lab = str(s.get("name", f"S{i + 1}"))
            tw = fm.horizontalAdvance(lab)
            p.setPen(QPen(QColor(*pal["text"]) if online else QColor(170, 100, 90)))
            lp = self._pt(cx, cy, R * 0.90, radians(bearing))
            p.drawText(QPointF(lp.x() - tw / 2, lp.y() + 3), lab)

        # 盲区：外圈红色弧段
        if not self._gaps:
            return
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(240, 60, 48, 210), 4))
        rect = QRectF(cx - R * 0.985, cy - R * 0.985, R * 1.97, R * 1.97)
        for a0, a1 in self._gaps:
            span = (a1 - a0) % 360.0
            if span <= 0.2:
                continue
            # Qt 的角从 3 点钟起算、逆时针为正；本工程的方位角从 12 点起算、顺时针为正。
            # 北向朝上时整幅图转了 _rot_deg，弧段也要跟着转。
            p.drawArc(rect, int((90.0 - a1 - self._rot_deg) * 16), int(span * 16))

    # ------------------------------------------------------------------ #
    def _draw_fov(self, p: QPainter, cx, cy, R, pal) -> None:
        if len(self._az_eff) <= 1:
            return
        a_lo = float(self._az_eff[0])
        a_hi = float(self._az_eff[-1])
        if abs(a_hi - a_lo) > radians(350):
            return
        poly = [QPointF(cx, cy)]
        for k in range(49):
            a = a_lo + (a_hi - a_lo) * k / 48.0
            poly.append(self._pt(cx, cy, R, a))
        c = pal["grid_dim"]
        p.setBrush(QColor(c[0], c[1], c[2], 40))
        p.setPen(QPen(QColor(pal["grid"][0], pal["grid"][1], pal["grid"][2], 120), 1))
        p.drawPolygon(QPolygonF(poly))
        p.setBrush(Qt.NoBrush)

    def _draw_grid(self, p: QPainter, cx, cy, R, pal) -> None:
        g = QColor(*pal["grid"])
        gd = QColor(*pal["grid_dim"])
        p.setFont(QFont("Consolas", 9))

        # 距离环
        for i in range(1, 5):
            rr = R * i / 4.0
            p.setPen(QPen(gd, 1, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), rr, rr)
            if i % 2 == 0:
                rng = self._caps.range_min + (self._caps.range_max - self._caps.range_min) * i / 4.0
                p.setPen(QPen(g))
                p.drawText(QPointF(cx + rr * 0.72, cy - rr * 0.72),
                           _fmt_rng(rng, self._caps.range_max))

        # 十字
        p.setPen(QPen(gd, 1))
        p.drawLine(QPointF(cx - R, cy), QPointF(cx + R, cy))
        p.drawLine(QPointF(cx, cy - R), QPointF(cx, cy + R))

        # 方位刻度
        if len(self._az_eff) <= 1:
            return
        a_lo = degrees(float(self._az_eff[0]))
        a_hi = degrees(float(self._az_eff[-1]))
        full = abs(a_hi - a_lo) > 350
        span = 360.0 if full else (a_hi - a_lo)
        step = 30.0 if full else 15.0
        n = int(span / step) + 1
        for k in range(n):
            ang_deg = (a_lo + step * k) if not full else (-180.0 + step * k)
            a = radians(ang_deg)
            p.setPen(QPen(g, 1))
            p.drawLine(self._pt(cx, cy, R * 0.94, a), self._pt(cx, cy, R, a))
            if k % 2 == 0:
                # 刻度数字要跟着转：北向朝上时"000"应该出现在北边
                lab = f"{(ang_deg + self._rot_deg + 360) % 360:03.0f}"
                p.setPen(QPen(QColor(*pal["text"])))
                tw = p.fontMetrics().horizontalAdvance(lab)
                lp = self._pt(cx, cy, R * 0.845, a)
                p.drawText(QPointF(lp.x() - tw / 2, lp.y() + 4), lab)

    def _draw_sweep(self, p: QPainter, cx, cy, R, pal) -> None:
        if self._sweep_az is None or self._caps.scan_mode != "rotating":
            return
        c = pal["stops"][-1][1]
        p.setPen(QPen(QColor(c[0], c[1], c[2], 190), 1.4))
        p.drawLine(QPointF(cx, cy), self._pt(cx, cy, R, self._sweep_az))

    def _draw_targets(self, p: QPainter, cx, cy, R, pal) -> None:
        if not self._tracks:
            return
        caps = self._caps
        tc = QColor(*pal["target"])
        lc = QColor(*pal["lock"])
        f = QFont("Consolas", 9)
        p.setFont(f)
        fm = p.fontMetrics()
        span = max(1e-6, caps.range_max - caps.range_min)
        placed: list[tuple[float, float, float, float]] = []

        for t in self._tracks:
            r = (t.rng - caps.range_min) / span * R
            if r < 0 or r > R * 1.04:
                continue
            a = t.az
            pt = self._pt(cx, cy, r, a)
            x, y = pt.x(), pt.y()

            # 颜色：锁定 > 威胁等级 > 默认。威胁分级来自雷达本身，没有就退回默认色
            if t.locked:
                col = lc
            elif t.threat >= 3:
                col = QColor(255, 52, 40)
            elif t.threat == 2:
                col = QColor(255, 138, 40)
            elif t.threat == 1:
                col = QColor(232, 176, 66)
            else:
                col = tc

            if t.locked:
                h = 7.0
                p.setPen(QPen(col, 1.4))
                for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                    x0, y0 = x + sx * h, y + sy * h
                    p.drawLine(QPointF(x0, y0 - sy * 5), QPointF(x0, y0))
                    p.drawLine(QPointF(x0, y0), QPointF(x0 - sx * 5, y0))
                p.drawLine(QPointF(cx, cy), QPointF(x, y))

            p.setPen(QPen(col, 1.3))
            p.setBrush(Qt.NoBrush)
            # 符号按 IFF 分类走：敌三角、友圆、未知菱形，和真实显控的约定一致
            if t.cls == "敌":
                p.drawPolygon(QPolygonF([QPointF(x, y - 5.2), QPointF(x + 5.2, y + 3.6),
                                         QPointF(x - 5.2, y + 3.6)]))
            elif t.cls == "友":
                p.drawEllipse(QPointF(x, y), 4.4, 4.4)
            elif t.cls == "未知":
                p.drawPolygon(QPolygonF([QPointF(x, y - 5.4), QPointF(x + 5.4, y),
                                         QPointF(x, y + 5.4), QPointF(x - 5.4, y)]))
            else:
                p.drawPolygon(QPolygonF([QPointF(x, y - 4.5), QPointF(x + 4.5, y + 3.5),
                                         QPointF(x - 4.5, y + 3.5)]))

            lab = f"T{t.tid} {_fmt_rng(t.rng, caps.range_max)}"
            if caps.has_velocity and t.speed >= 0.05:
                lab += f" {t.speed:.0f}m/s"
            tw = fm.horizontalAdvance(lab)
            tx = x + 9 if x < cx + R * 0.6 else x - 9 - tw
            ty = y - 9 if y > cy else y + 16

            # 标签避让：多目标挤在一起时标签会糊成一团，往下错开直到不重叠。
            # 真实显控台也有这个问题，标准做法就是错位 + 引线。
            for _ in range(6):
                rect = (tx - 2, ty - 10, tw + 4, 13)
                clash = any(
                    not (rect[0] + rect[2] < r[0] or r[0] + r[2] < rect[0]
                         or rect[1] + rect[3] < r[1] or r[1] + r[3] < rect[1])
                    for r in placed)
                if not clash:
                    break
                ty += 12
            placed.append((tx - 2, ty - 10, tw + 4, 13))

            if abs(ty - (y - 9 if y > cy else y + 16)) > 1.0:
                p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 110), 1, Qt.DotLine))
                p.drawLine(QPointF(x + (5 if tx > x else -5), y),
                           QPointF(tx + (0 if tx > x else tw), ty - 3))

            p.setPen(QPen(QColor(0, 0, 0, 170)))
            p.drawText(QPointF(tx + 1, ty + 1), lab)
            p.setPen(QPen(col))
            p.drawText(QPointF(tx, ty), lab)

    # ------------------------------------------------------------------ #
    def _geometry(self) -> tuple[float, float, float]:
        w, h = self.width(), self.height()
        side = min(w, h)
        ox, oy = (w - side) / 2.0, (h - side) / 2.0
        return ox + side / 2.0, oy + side / 2.0, side * 0.47

    # ------------------------------------------------------------------ #
    # 北向 / 艏向
    # ------------------------------------------------------------------ #
    def set_orientation(self, north_up: bool, heading_deg: float = 0.0) -> None:
        """切换"艏向朝上 / 北向朝上"。

        实现只在一处：把所有方位加一个固定旋转量 `_rot`。
        图像用 QPainter 旋转，矢量元素统一走 `_pt()` —— 漏一处就会错位，
        所以下面所有绘制函数都不再直接写 sin/cos。
        """
        rot = (heading_deg % 360.0) if north_up else 0.0
        self._north_up = bool(north_up)
        self._heading_deg = float(heading_deg) % 360.0
        new_rot = radians(rot)
        if abs(new_rot - self._rot) > 1e-9:
            self._rot = new_rot
            self._rot_deg = rot
            self.update()

    def _pt(self, cx: float, cy: float, r: float, az_rad: float) -> QPointF:
        """极坐标 → 屏幕点，自动叠加朝向旋转。"""
        a = az_rad + self._rot
        return QPointF(cx + r * sin(a), cy - r * cos(a))

    def pick_target(self, pos) -> int:
        """屏幕坐标 -> 最近的目标航迹号，超出 14 像素算没点中。"""
        cx, cy, R = self._geometry()
        caps = self._caps
        span = max(1e-6, caps.range_max - caps.range_min)
        best: tuple[float, int] | None = None
        for t in self._tracks:
            r = (t.rng - caps.range_min) / span * R
            px = cx + r * sin(t.az)
            py = cy - r * cos(t.az)
            d = ((pos.x() - px) ** 2 + (pos.y() - py) ** 2) ** 0.5
            if d <= 14.0 and (best is None or d < best[0]):
                best = (d, t.tid)
        return best[1] if best else -1

    def mousePressEvent(self, ev) -> None:
        if self.zone_mode:
            # 画区模式：左键起拖、右键删
            if ev.button() == Qt.LeftButton:
                pos = ev.pos()
                self._zone_drag = (pos.x(), pos.y(), pos.x(), pos.y())
                self.update()
                return
            if ev.button() == Qt.RightButton:
                self.zoneDeleted.emit()
                return
        if ev.button() == Qt.LeftButton:
            tid = self.pick_target(ev.pos())
            if tid >= 0:
                # 点到目标：锁定，读数层清掉
                self._probe = None
                self.targetPicked.emit(tid)
            else:
                # 点到空白：不解除锁定，只给一个"这里有多远、多少度"的读数
                cx, cy, R = self._geometry()
                rng, az, inside = point_to_polar(
                    ev.pos().x(), ev.pos().y(), cx, cy, R,
                    self._caps.range_min, self._caps.range_max)
                # 北向朝上时屏幕上量到的是"世界方位"，减回航向才是本体方位
                az = (az - self._rot_deg) % 360.0
                self._probe = (ev.pos().x(), ev.pos().y(), rng, az) if inside else None
                self.update()
        elif ev.button() == Qt.RightButton:
            # 右键放 EBL/VRM（右键点在盘外则清除）
            self._place_ebl(ev.pos())
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:
        if self.zone_mode and self._zone_drag is not None:
            sx, sy = self._zone_drag[0], self._zone_drag[1]
            self._zone_drag = (sx, sy, ev.pos().x(), ev.pos().y())
            self.update()
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:
        if (self.zone_mode and self._zone_drag is not None
                and ev.button() == Qt.LeftButton):
            sx, sy, ex, ey = self._zone_drag
            self._zone_drag = None
            self.update()
            c = self._screen_to_body(QPointF(sx, sy))
            _, _, R = self._geometry()
            span = max(1e-6, self._caps.range_max - self._caps.range_min)
            rad_m = (((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5) / max(1e-6, R) * span
            if c is not None and rad_m >= 0.2:
                self.zoneDrawn.emit(c[0], c[1], rad_m)
            return
        super().mouseReleaseEvent(ev)

    def clear_probe(self) -> None:
        self._probe = None
        self.update()

    def _place_ebl(self, pos) -> None:
        """右键放 EBL/VRM：量这个方向的方位，和这个圈的距离。"""
        cx, cy, R = self._geometry()
        rng, az, inside = point_to_polar(
            pos.x(), pos.y(), cx, cy, R, self._caps.range_min, self._caps.range_max)
        if not inside:
            self.set_ebl(None)
            return
        self.set_ebl((az - self._rot_deg) % 360.0, rng)

    # ------------------------------------------------------------------ #
    # 外部数据与显示模式
    # ------------------------------------------------------------------ #
    def set_ais(self, items) -> None:
        """AIS 目标：[(x, y, 名称, 航向度)]，本地米制坐标。"""
        self._ais = list(items or [])
        self.update()

    def set_origin(self, x: float, y: float) -> None:
        """真运动时本舰已行驶的位移（米）。"""
        if abs(x - self.origin[0]) > 1e-3 or abs(y - self.origin[1]) > 1e-3:
            self.origin = (x, y)
            self.update()

    def set_zone_mode(self, on: bool) -> None:
        """画禁入区模式：左键拖出圆，右键删最近一个。"""
        self.zone_mode = bool(on)
        self._zone_drag = None
        self.setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)
        self.update()

    def _screen_to_body(self, pt: QPointF) -> tuple[float, float] | None:
        cx, cy, R = self._geometry()
        rng, az, inside = point_to_polar(pt.x(), pt.y(), cx, cy, R,
                                         self._caps.range_min, self._caps.range_max)
        if not inside:
            return None
        az = (az - self._rot_deg) % 360.0
        return rng * sin(radians(az)), rng * cos(radians(az))

    def _draw_ais(self, p: QPainter, cx, cy, R, pal) -> None:
        if not self.show_ais or not self._ais:
            return
        rmax = max(1e-6, self._caps.range_max)
        col = QColor(110, 190, 255, 235)
        p.setFont(QFont("Consolas", 8))
        for item in self._ais:
            x, y, name, cog = item[0], item[1], item[2], item[3]
            r = hypot(x, y)
            if r > rmax:
                continue
            pt = self._pt(cx, cy, r / rmax * R, atan2(x, y))
            a = radians(cog + self._rot_deg)
            # 三角形指向航向，和雷达目标（圆点）区分开
            s = 7.5
            tri = QPolygonF([
                QPointF(pt.x() + s * sin(a), pt.y() - s * cos(a)),
                QPointF(pt.x() + s * sin(a + 2.5), pt.y() - s * cos(a + 2.5)),
                QPointF(pt.x() + s * sin(a - 2.5), pt.y() - s * cos(a - 2.5)),
            ])
            p.setBrush(QColor(110, 190, 255, 55))
            p.setPen(QPen(col, 1.2))
            p.drawPolygon(tri)
            p.setBrush(Qt.NoBrush)
            if name:
                p.setPen(QPen(col))
                p.drawText(QPointF(pt.x() + 7, pt.y() - 5), name)

    def _draw_zone_drag(self, p: QPainter, pal) -> None:
        if self._zone_drag is None:
            return
        sx, sy, ex, ey = self._zone_drag
        rad = ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5
        c = QColor(255, 214, 110, 210)
        p.setBrush(QColor(255, 214, 110, 28))
        p.setPen(QPen(c, 1.4, Qt.DashLine))
        p.drawEllipse(QPointF(sx, sy), rad, rad)
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(sx, sy), QPointF(ex, ey))

    def _draw_own_ship(self, p: QPainter, pt: QPointF, pal) -> None:
        """真运动下本舰会移动，必须画出来，否则不知道自己在哪。"""
        c = QColor(240, 246, 240, 225)
        p.setBrush(QColor(240, 246, 240, 45))
        p.setPen(QPen(c, 1.4))
        p.drawPolygon(QPolygonF([
            QPointF(pt.x(), pt.y() - 7),
            QPointF(pt.x() - 5, pt.y() + 5),
            QPointF(pt.x() + 5, pt.y() + 5),
        ]))
        p.setBrush(Qt.NoBrush)

    def _draw_map(self, p: QPainter, cx, cy, R, pal) -> None:
        """把地图瓦片当背景铺在回波下面。

        坐标系对齐方式：地图是"北朝上"的，而画布可能已经按航向转过。
        所以这里要再反向转 -heading —— 艏向模式下净旋转是 -heading（船头朝上），
        北向模式下净旋转是 0（正北朝上）。漏掉这一步地图就会和回波错开。
        """
        m = self.map_layer
        if m is None or getattr(m, "image", None) is None:
            return
        img = m.image
        mpp = float(getattr(m, "mpp", 0.0))
        if mpp <= 0:
            return
        rmax = max(1e-6, self._caps.range_max)
        scale = R / rmax                        # 屏幕像素 / 米
        w = img.width() * mpp * scale
        h = img.height() * mpp * scale
        ax, ay = getattr(m, "anchor_px", (img.width() / 2, img.height() / 2))

        p.save()
        p.translate(cx, cy)
        p.rotate(-self._heading_deg)
        p.translate(-cx, -cy)
        p.setOpacity(0.9)
        p.drawImage(QRectF(cx - ax * mpp * scale, cy - ay * mpp * scale, w, h), img)
        p.setOpacity(1.0)
        p.restore()

        # 图源归属：用别人的瓦片必须标注，这不是可选项
        attr = getattr(m, "attribution", "")
        if attr:
            p.setFont(QFont("Consolas", 7))
            p.setPen(QPen(QColor(150, 160, 170, 190)))
            p.drawText(QPointF(cx - R * 0.70, cy + R * 0.99), attr)

    def _draw_probe(self, p: QPainter, pal) -> None:
        """点击测距的十字与读数，就地画，不弹窗。

        做成"点一下看到数字、再点一下就换地方"的形式，比弹对话框快得多——
        操作员量距离是高频动作，不能每次都走一次弹窗。
        """
        if self._probe is None:
            return
        x, y, rng, az = self._probe
        col = QColor(*pal["text"])
        p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 130), 1, Qt.DashLine))
        p.drawLine(QPointF(x - 9, y), QPointF(x + 9, y))
        p.drawLine(QPointF(x, y - 9), QPointF(x, y + 9))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(x, y), 12, 12)

        txt = f"{fmt_distance(rng)}  {fmt_bearing(az)}"
        p.setFont(QFont("Consolas", 9))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(txt)
        bx = min(max(4.0, x + 14), self.width() - tw - 12)
        by = y - 12
        p.fillRect(QRectF(bx - 3, by - 11, tw + 8, 15), QColor(0, 0, 0, 170))
        p.setPen(QPen(col))
        p.drawText(QPointF(bx, by), txt)
