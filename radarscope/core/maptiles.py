"""Web 地图瓦片：坐标换算 + 下载 + 磁盘缓存。

只做纯计算和 IO，不碰 Qt —— 这样能单测，也能被别的界面复用。

瓦片体系（Web Mercator / XYZ）：
    第 z 级把地球切成 2^z × 2^z 块，每块 256×256 像素
    地面分辨率 = 156543.03392 * cos(纬度) / 2^z     米/像素
所以"量程 8 米、画面 600 像素"要选的最优级别可以直接算出来，
不用让用户自己调。

图源用 CARTO 的深色底图：它本来就是深色，和本软件的界面一致，
不用再叠一层黑蒙版压亮度。使用必须标注来源（OSM + CARTO），
所以界面上会显示归属文字 —— 这不是可选项。
"""
from __future__ import annotations

import math
import urllib.request
from pathlib import Path

TILE_PX = 256
EARTH_C = 156543.03392          # 赤道处 z=0 的米/像素

#: 深色底图。换成别的图源只要改这两个模板。
PROVIDERS = {
    "carto_dark": {
        "url": "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        "attribution": "地图 © OpenStreetMap 贡献者 · © CARTO",
    },
    "osm": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "地图 © OpenStreetMap 贡献者",
    },
}
DEFAULT_PROVIDER = "carto_dark"


def meters_per_pixel(lat: float, z: int, tile_px: int = TILE_PX) -> float:
    """该纬度、该级别下每像素多少米。"""
    return EARTH_C * math.cos(math.radians(lat)) / (2 ** z) * (TILE_PX / tile_px)


def zoom_for_range(lat: float, range_m: float, width_px: float,
                   tile_px: int = TILE_PX) -> int:
    """让"整幅量程刚好铺满画面宽度"的级别。

    要从**高级别往下找**：级别越高越清晰，但也越贵。
    找"仍能覆盖 2.2 倍量程"的最清晰一级，而不是反过来 ——
    从低级别往上找的话，第一个满足条件的永远是 z=3（全球图），
    放大到 8 米量程时满屏还是整个亚洲。
    """
    if range_m <= 0 or width_px <= 0:
        return 16
    need_mpp = range_m * 2.2 / width_px        # 每像素至少要代表多少米才装得下
    for z in range(18, 2, -1):
        mpp = EARTH_C * math.cos(math.radians(lat)) / (2 ** z)
        if mpp >= need_mpp:
            return z                            # 从最清晰往下，第一个装得下的
    return 3


def lonlat_to_tile(lat: float, lon: float, z: int) -> tuple[float, float]:
    """经纬度 → 瓦片坐标（带小数）。"""
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
    return x, y


def tile_span(lat: float, lon: float, z: int, cols: int,
              rows: int) -> tuple[int, int, int, int]:
    """以 (lat,lon) 为中心，覆盖 cols×rows 块瓦片时需要的整数范围。

    返回 (x0, y0, x1, y1)，右下开区间。
    """
    xc, yc = lonlat_to_tile(lat, lon, z)
    x0 = int(math.floor(xc - cols / 2.0))
    y0 = int(math.floor(yc - rows / 2.0))
    return x0, y0, x0 + cols, y0 + rows


class TileStore:
    """磁盘缓存的瓦片库。

    磁盘缓存不是优化，是必需品：地图瓦片每块 5~20 KB，
    移一下视野就要几十块，不缓存等于每次都在打别人的服务器。
    """

    def __init__(self, cache_dir: Path, provider: str = DEFAULT_PROVIDER,
                 timeout: float = 6.0):
        self.root = Path(cache_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.provider = provider if provider in PROVIDERS else DEFAULT_PROVIDER
        self.timeout = timeout
        self.downloaded = 0
        self.failed = 0
        self.last_error = ""

    @property
    def attribution(self) -> str:
        return PROVIDERS[self.provider]["attribution"]

    def path_of(self, z: int, x: int, y: int) -> Path:
        return self.root / self.provider / str(z) / str(x) / f"{y}.png"

    def get(self, z: int, x: int, y: int) -> bytes | None:
        """取瓦片字节；本地没有就下载。失败返回 None。"""
        n = 2 ** z
        if not (0 <= x < n) or not (0 <= y < n):
            return None
        p = self.path_of(z, x, y)
        if p.exists():
            try:
                return p.read_bytes()
            except OSError:
                pass
        url = PROVIDERS[self.provider]["url"].format(z=z, x=x, y=y)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Lookout-RadarConsole/1.0 (tile cache)",
                "Accept": "image/png,image/*",
            })
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = resp.read()
            if not data:
                raise ValueError("空响应")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            self.downloaded += 1
            return data
        except Exception as exc:                 # 网络问题不该影响雷达
            self.failed += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    def missing(self, z: int, x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
        out = []
        for x in range(x0, x1):
            for y in range(y0, y1):
                if not self.path_of(z, x, y).exists():
                    out.append((x, y))
        return out

    def describe(self) -> str:
        return (f"{self.provider} · 已下载 {self.downloaded} · "
                f"失败 {self.failed}")
