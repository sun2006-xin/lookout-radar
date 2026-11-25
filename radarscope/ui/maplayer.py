"""地图图层：把瓦片拼成一张 QImage，供 PPI 当背景画。

分工：
    core/maptiles.py  纯计算 + 下载 + 缓存（可单测）
    ui/maplayer.py    拼图 + 后台线程（碰 Qt，不测）

关键点：下载必须放后台线程。地图瓦片每次移动视野都要补几块，
网络慢的时候同步下载会把界面卡死 —— 这是所有地图控件的头号事故。
"""
from __future__ import annotations

import io

from PyQt5.QtCore import QObject, QRectF, Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter

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
        self._thread: _Fetcher | None = None
        self._key: tuple | None = None
        self.enabled = False
        self.status = ""
        self._last_req: tuple | None = None

    @property
    def attribution(self) -> str:
        return self.store.attribution

    # ------------------------------------------------------------------ #
    def update_for(self, lat: float, lon: float, range_m: float,
                   width_px: float) -> None:
        """按当前量程与画面宽度选级别、补瓦片、拼图。"""
        if not self.enabled:
            return
        z = zoom_for_range(lat, range_m, width_px)
        cols = rows = 3
        key = (round(lat, 5), round(lon, 5), z, cols, rows)
        if key == self._key and self.image is not None:
            return
        self._key = key
        self.zoom = z
        x0, y0, x1, y1 = tile_span(lat, lon, z, cols, rows)
        self._compose(lat, lon, z, x0, y0, x1, y1)

    def _compose(self, lat: float, lon: float, z: int,
                 x0: int, y0: int, x1: int, y1: int) -> None:
        cols, rows = x1 - x0, y1 - y0
        canvas = QImage(cols * TILE_PX, rows * TILE_PX, QImage.Format_RGB32)
        canvas.fill(QColor(12, 16, 20))
        p = QPainter(canvas)
        missing: list[tuple[int, int]] = []
        for ix, x in enumerate(range(x0, x1)):
            for iy, y in enumerate(range(y0, y1)):
                data = self.store.get(z, x, y) if self.store.path_of(z, x, y).exists() else None
                if data is None:
                    missing.append((x, y))
                    continue
                img = QImage()
                if img.loadFromData(data):
                    p.drawImage(ix * TILE_PX, iy * TILE_PX, img)
        p.end()

        self.image = canvas
        self.mpp = meters_per_pixel(lat, z)
        xc, yc = lonlat_to_tile(lat, lon, z)
        self.anchor_px = ((xc - x0) * TILE_PX, (yc - y0) * TILE_PX)
        self.status = (f"z{z} · {self.store.describe()}"
                       + (f" · 待补 {len(missing)}" if missing else ""))
        self.ready.emit()

        if missing:
            self._start_fetch(z, missing)

    def _start_fetch(self, z: int, tiles: list[tuple[int, int]]) -> None:
        # 换图源/换量程时先让上一批停下来，别在后台继续拉没用的瓦片
        if self._thread is not None and self._thread.isRunning():
            self._thread.requestInterruption()
            self._thread.wait(300)
            return
        self._thread = _Fetcher(self.store, z, tiles[:24], self)
        self._thread.done.connect(lambda n, err: self._on_fetched(z))
        self._thread.start()

    def stop(self) -> None:
        """关闭地图时终止下载线程。"""
        t = self._thread
        if t is not None and t.isRunning():
            t.requestInterruption()
            t.wait(1500)
        self._thread = None

    def _on_fetched(self, z: int) -> None:
        # 新瓦片落盘后重拼一次；_key 清掉以保证会真的重算
        self._key = None
        if self._last_req is not None:
            self.update_for(*self._last_req)


    def request(self, lat: float, lon: float, range_m: float, width_px: float) -> None:
        self._last_req = (lat, lon, range_m, width_px)
        self.update_for(lat, lon, range_m, width_px)
