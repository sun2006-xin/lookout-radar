"""地图图层：把瓦片拼成一张 QImage，供 PPI 当背景画。

分工：
    core/maptiles.py  纯计算 + 下载 + 缓存（可单测）
    ui/maplayer.py    拼图 + 后台线程（碰 Qt，不测）

关键点：下载必须放后台线程。地图瓦片每次移动视野都要补几块，
网络慢的时候同步下载会把界面卡死 —— 这是所有地图控件的头号事故。
"""
from __future__ import annotations

from collections import OrderedDict
import math

from PyQt5.QtCore import QObject, QPointF, QRectF, Qt, QThread, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPen

from ..core.maptiles import (TILE_PX, TileStore, lonlat_to_tile,
                             meters_per_pixel, tile_span, zoom_for_range)


class _Fetcher(QThread):
    done = pyqtSignal(int, str)

    def __init__(self, store: TileStore, z: int, tiles: list[tuple[int, int]], parent=None):
        super().__init__(parent)
        self.store = store
        self.z = z
        self.tiles = tiles

    def run(self) -> None:
        ok = 0
        for (x, y) in self.tiles:
            if self.isInterruptionRequested():
                break
            if self.store.get(self.z, x, y) is not None:
                ok += 1
        self.done.emit(ok, self.store.last_error)


class MapLayer(QObject):
    """一张"以某个经纬度为中心"的地图位图，带上屏幕换算需要的比例。"""

    ready = pyqtSignal()

    def __init__(self, cache_dir, parent=None):
        super().__init__(parent)
        self.store = TileStore(cache_dir)
        self.image: QImage | None = None
        self.mpp = 1.0                    # 米/像素（当前级别、当前纬度）
        self.anchor_px = (0.0, 0.0)       # 中心经纬度落在图上的像素位置
        self.zoom = 15
        self.revision = 0
        self._thread: _Fetcher | None = None
        self._pending_fetch: tuple[int, list[tuple[int, int]]] | None = None
        self._mem_tiles: OrderedDict[tuple[str, int, int, int], QImage] = OrderedDict()
        self._key: tuple | None = None
        self.enabled = False
        self.synthetic_sea = False
        self.status = ""
        self._last_req: tuple | None = None

    @property
    def attribution(self) -> str:
        if self.synthetic_sea and self.store.mbtiles is None:
            return "内置矢量海域海图（零延迟）"
        return self.store.attribution

    def set_provider(self, provider: str) -> None:
        from ..core.maptiles import PROVIDERS

        if provider == "marine_chart":
            if not self.synthetic_sea:
                self.synthetic_sea = True
                self._key = None
                if self.enabled and self._last_req is not None:
                    self.update_for(*self._last_req)
            return
        changed = self.synthetic_sea or (provider in PROVIDERS and provider != self.store.provider)
        self.synthetic_sea = False
        if provider in PROVIDERS:
            self.store.provider = provider
        if changed:
            self._key = None
            if self.enabled and self._last_req is not None:
                self.update_for(*self._last_req)

    def set_mbtiles(self, path: str | None) -> bool:
        ok = self.store.set_mbtiles(path)
        self._key = None
        if self.enabled and self._last_req is not None:
            self.update_for(*self._last_req)
        return ok

    def to_rgb_array(self):
        """将当前拼接底图导出为 (H, W, 3) uint8 数组供陆地杂波掩膜提取岸线。"""
        if self.image is None or self.image.isNull():
            return None
        import numpy as np

        rgb_img = self.image.convertToFormat(QImage.Format_RGB888)
        h, w = rgb_img.height(), rgb_img.width()
        ptr = rgb_img.bits()
        ptr.setsize(rgb_img.byteCount())
        arr = np.frombuffer(ptr, dtype=np.uint8).reshape((h, rgb_img.bytesPerLine()))
        return arr[:, : w * 3].reshape((h, w, 3)).copy()

    # ------------------------------------------------------------------ #
    def update_for(self, lat: float, lon: float, range_m: float,
                   width_px: float) -> None:
        """按当前量程与画面宽度选级别、补瓦片、拼图。"""
        if not self.enabled:
            return
        z = zoom_for_range(lat, range_m, width_px)
        cols = rows = 3
        if self.synthetic_sea and self.store.mbtiles is None:
            key = ("marine_chart", round(lat, 4), round(lon, 4), z, round(float(range_m), 1))
            if key == self._key and self.image is not None:
                return
            self._key = key
            self.zoom = z
            self._compose_marine_chart(lat, lon, z, float(range_m))
            return
        key = (self.store.provider, round(lat, 5), round(lon, 5), z, cols, rows)
        if key == self._key and self.image is not None:
            return
        self._key = key
        self.zoom = z
        x0, y0, x1, y1 = tile_span(lat, lon, z, cols, rows)
        self._compose(lat, lon, z, x0, y0, x1, y1)

    def _compose_marine_chart(self, lat: float, lon: float, z: int, range_m: float) -> None:
        """零延迟离屏生成矢量海图（航道 / 水深等深线 / 岛礁陆地先验，绝不卡顿且无城镇街道）。"""
        sz = 3 * TILE_PX
        canvas = QImage(sz, sz, QImage.Format_ARGB32)
        # 海水底色（B 通道显著高于 R，保证 LandMask 稳定识别海域 vs 岛礁）
        canvas.fill(QColor(210, 232, 248, 215))
        p = QPainter(canvas)
        p.setRenderHint(QPainter.Antialiasing, True)

        mpp = meters_per_pixel(lat, z)
        cx = cy = sz / 2.0
        rmax = max(100.0, float(range_m))
        r_px = rmax / max(1e-6, mpp)

        # 航道分隔线与等深线
        p.setPen(QPen(QColor(156, 198, 232, 165), 1.2, Qt.DashLine))
        p.drawLine(QPointF(cx - sz * 0.45, cy - sz * 0.20), QPointF(cx + sz * 0.45, cy + sz * 0.14))
        p.drawLine(QPointF(cx - sz * 0.45, cy + sz * 0.04), QPointF(cx + sz * 0.45, cy + sz * 0.38))
        for frac in (0.45, 0.85):
            rr = r_px * frac
            p.setPen(QPen(QColor(168, 206, 236, 130), 1.0, Qt.DotLine))
            p.drawEllipse(QPointF(cx, cy), rr, rr)

        # 东侧海岸岛礁轮廓（R > B 触发陆地掩膜识别，演示真海区岸线抑制）
        isle1 = QPainterPath()
        isle1.moveTo(cx + r_px * 0.46, cy - r_px * 0.58)
        isle1.cubicTo(cx + r_px * 0.86, cy - r_px * 0.72, cx + r_px * 0.98, cy - r_px * 0.24, cx + r_px * 0.62, cy - r_px * 0.16)
        isle1.cubicTo(cx + r_px * 0.40, cy - r_px * 0.24, cx + r_px * 0.36, cy - r_px * 0.46, cx + r_px * 0.46, cy - r_px * 0.58)
        p.setBrush(QBrush(QColor(226, 214, 184, 225)))
        p.setPen(QPen(QColor(178, 162, 126, 220), 1.5))
        p.drawPath(isle1)

        isle2 = QPainterPath()
        isle2.addEllipse(QPointF(cx - r_px * 0.62, cy + r_px * 0.48), r_px * 0.18, r_px * 0.12)
        p.drawPath(isle2)

        # 经纬海图方格参考线
        p.setPen(QPen(QColor(145, 188, 222, 95), 1.0))
        step = sz / 4.0
        for i in range(1, 4):
            p.drawLine(QPointF(i * step, 0), QPointF(i * step, sz))
            p.drawLine(QPointF(0, i * step), QPointF(sz, i * step))
        p.end()

        self.image = canvas
        self.mpp = mpp
        self.anchor_px = (cx, cy)
        self.revision += 1
        self.status = f"矢量海图 z{z} · ({lat:.3f}, {lon:.3f})"
        self.ready.emit()

    def _tile_image(self, z: int, x: int, y: int) -> QImage | None:
        key = (self.store.provider if self.store.mbtiles is None else str(self.store.mbtiles.path), z, x, y)
        cached = self._mem_tiles.get(key)
        if cached is not None:
            self._mem_tiles.move_to_end(key)
            return cached
        data: bytes | None = None
        if self.store.mbtiles is not None:
            data = self.store.mbtiles.get_tile(z, x, y)
        if data is None:
            p = self.store.path_of(z, x, y)
            if p.exists():
                try:
                    data = p.read_bytes()
                except OSError:
                    data = None
        if data is None:
            return None
        img = QImage()
        if not img.loadFromData(data):
            return None
        self._mem_tiles[key] = img
        while len(self._mem_tiles) > 48:
            self._mem_tiles.popitem(last=False)
        return img

    def _compose(self, lat: float, lon: float, z: int,
                 x0: int, y0: int, x1: int, y1: int) -> None:
        cols, rows = x1 - x0, y1 - y0
        # 必须用透明底，否则离线或瓦片加载失败时整块画布会变成纯黑盖住白昼界面
        canvas = QImage(cols * TILE_PX, rows * TILE_PX, QImage.Format_ARGB32)
        canvas.fill(Qt.transparent)
        p = QPainter(canvas)
        missing: list[tuple[int, int]] = []
        loaded = 0
        for ix, x in enumerate(range(x0, x1)):
            for iy, y in enumerate(range(y0, y1)):
                img = self._tile_image(z, x, y)
                if img is None:
                    missing.append((x, y))
                    continue
                p.drawImage(ix * TILE_PX, iy * TILE_PX, img)
                loaded += 1
        p.end()

        self.image = canvas if loaded > 0 else None
        self.mpp = meters_per_pixel(lat, z)
        xc, yc = lonlat_to_tile(lat, lon, z)
        self.anchor_px = ((xc - x0) * TILE_PX, (yc - y0) * TILE_PX)
        self.revision += 1
        total = max(0, cols * rows)
        err = f"（失败: {self.store.last_error}）" if (loaded == 0 and self.store.last_error) else ""
        self.status = (f"z{z} · {self.store.provider} · 瓦片 {loaded}/{total}"
                       + (f" · 下载中 {len(missing)}" if missing else "") + err)
        self.ready.emit()

        if missing:
            self._start_fetch(z, missing)

    def _start_fetch(self, z: int, tiles: list[tuple[int, int]]) -> None:
        # 换图源/换量程时不丢新请求：若旧批仍在下载，先打断并排队下一批
        if self._thread is not None and self._thread.isRunning():
            self._pending_fetch = (z, tiles[:24])
            self._thread.requestInterruption()
            return
        self._pending_fetch = None
        self._thread = _Fetcher(self.store, z, tiles[:24], self)
        self._thread.done.connect(lambda n, err, tz=z: self._on_fetched(tz))
        self._thread.start()

    def stop(self) -> None:
        """关闭地图时终止下载线程。"""
        self._pending_fetch = None
        t = self._thread
        if t is not None and t.isRunning():
            t.requestInterruption()
            t.wait(1500)
        self._thread = None

    def _on_fetched(self, z: int) -> None:
        pending = self._pending_fetch
        self._pending_fetch = None
        self._thread = None
        if pending is not None and self.enabled and not self.synthetic_sea:
            pz, ptiles = pending
            self._start_fetch(pz, ptiles)
            return
        # 新瓦片落盘后重拼一次；_key 清掉以保证会真的重算
        self._key = None
        if self._last_req is not None:
            self.update_for(*self._last_req)


    def request(self, lat: float, lon: float, range_m: float, width_px: float) -> None:
        self._last_req = (lat, lon, range_m, width_px)
        self.update_for(lat, lon, range_m, width_px)
