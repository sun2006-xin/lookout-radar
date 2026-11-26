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

    def set_provider(self, provider: str) -> None:
        from ..core.maptiles import PROVIDERS

        if provider in PROVIDERS and provider != self.store.provider:
            self.store.provider = provider
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
        # 必须用透明底，否则离线或瓦片加载失败时整块画布会变成纯黑盖住白昼界面
        canvas = QImage(cols * TILE_PX, rows * TILE_PX, QImage.Format_ARGB32)
        canvas.fill(Qt.transparent)
        p = QPainter(canvas)
        missing: list[tuple[int, int]] = []
        loaded = 0
        for ix, x in enumerate(range(x0, x1)):
            for iy, y in enumerate(range(y0, y1)):
                data = self.store.get(z, x, y) if self.store.has_local_tile(z, x, y) else None
                if data is None:
                    missing.append((x, y))
                    continue
                img = QImage()
                if img.loadFromData(data):
                    p.drawImage(ix * TILE_PX, iy * TILE_PX, img)
                    loaded += 1
        p.end()

        self.image = canvas if loaded > 0 else None
        self.mpp = meters_per_pixel(lat, z)
        xc, yc = lonlat_to_tile(lat, lon, z)
        self.anchor_px = ((xc - x0) * TILE_PX, (yc - y0) * TILE_PX)
        total = max(0, cols * rows)
        err = f"（失败: {self.store.last_error}）" if (loaded == 0 and self.store.last_error) else ""
        self.status = (f"z{z} · {self.store.provider} · 瓦片 {loaded}/{total}"
                       + (f" · 下载中 {len(missing)}" if missing else "") + err)
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
