"""大地测量坐标转换与 4/3 等效地球半径雷达视距模型（ROADMAP H2）。

纯标准库数学实现（无 pyproj/GDAL 重型依赖）：
  - WGS84 <-> ECEF 相互转换（Bowring/迭代法，精度优于 1e-9°）
  - WGS84 <-> 本地 ENU（East-North-Up，x=东, y=北, z=天向，与雷达平台坐标系一致）
  - 4/3 R_e 标准大气折射下的雷达无线电视距与地球曲率下沉量修正
"""
from __future__ import annotations

import math

WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
WGS84_B = WGS84_A * (1.0 - WGS84_F)
EARTH_MEAN_R = 6371000.0


def geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_m: float = 0.0) -> tuple[float, float, float]:
    """WGS84 经纬高 (deg, deg, m) -> 地心固定坐标 ECEF (X, Y, Z) 米。"""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sl, cl = math.sin(lat), math.cos(lat)
    so, co = math.sin(lon), math.cos(lon)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sl * sl)
    x = (n + alt_m) * cl * co
    y = (n + alt_m) * cl * so
    z = (n * (1.0 - WGS84_E2) + alt_m) * sl
    return float(x), float(y), float(z)


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    """ECEF (X, Y, Z) 米 -> WGS84 经纬高 (lat_deg, lon_deg, alt_m)。"""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-9:
        lat = (math.pi * 0.5) if z >= 0.0 else (-math.pi * 0.5)
        return math.degrees(lat), math.degrees(lon), abs(z) - WGS84_B
    lat = math.atan2(z, p * (1.0 - WGS84_E2))
    for _ in range(6):
        sl = math.sin(lat)
        n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sl * sl)
        lat_new = math.atan2(z + WGS84_E2 * n * sl, p)
        if abs(lat_new - lat) < 1e-13:
            lat = lat_new
            break
        lat = lat_new
    sl = math.sin(lat)
    cl = math.cos(lat)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sl * sl)
    alt = (p / cl - n) if abs(cl) > 1e-9 else (abs(z) / max(1e-9, abs(sl)) - n * (1.0 - WGS84_E2))
    return math.degrees(lat), math.degrees(lon), float(alt)


def geodetic_to_enu(
    lat_deg: float, lon_deg: float, alt_m: float,
    ref_lat: float, ref_lon: float, ref_alt: float = 0.0,
) -> tuple[float, float, float]:
    """WGS84 目标坐标相对参考雷达站转为本地站心 ENU (east_x, north_y, up_z) 米。"""
    x, y, z = geodetic_to_ecef(lat_deg, lon_deg, alt_m)
    x0, y0, z0 = geodetic_to_ecef(ref_lat, ref_lon, ref_alt)
    dx, dy, dz = x - x0, y - y0, z - z0

    rlat = math.radians(ref_lat)
    rlon = math.radians(ref_lon)
    sl, cl = math.sin(rlat), math.cos(rlat)
    so, co = math.sin(rlon), math.cos(rlon)

    e = -so * dx + co * dy
    n = -sl * co * dx - sl * so * dy + cl * dz
    u = cl * co * dx + cl * so * dy + sl * dz
    return float(e), float(n), float(u)


def enu_to_geodetic(
    e: float, n: float, u: float,
    ref_lat: float, ref_lon: float, ref_alt: float = 0.0,
) -> tuple[float, float, float]:
    """本地站心 ENU (m) -> WGS84 经纬高。"""
    x0, y0, z0 = geodetic_to_ecef(ref_lat, ref_lon, ref_alt)
    rlat = math.radians(ref_lat)
    rlon = math.radians(ref_lon)
    sl, cl = math.sin(rlat), math.cos(rlat)
    so, co = math.sin(rlon), math.cos(rlon)

    dx = -so * e - sl * co * n + cl * co * u
    dy = co * e - sl * so * n + cl * so * u
    dz = cl * n + sl * u
    return ecef_to_geodetic(x0 + dx, y0 + dy, z0 + dz)


def radar_horizon_m(h_radar_m: float, h_target_m: float = 0.0, k_factor: float = 4.0 / 3.0) -> float:
    """按 k*R_e（标准大气折射 k=4/3）计算雷达最大无线电视距（米）。"""
    r_eff = max(1e5, float(k_factor) * EARTH_MEAN_R)
    hr = max(0.0, float(h_radar_m))
    ht = max(0.0, float(h_target_m))
    return math.sqrt(2.0 * r_eff * hr) + math.sqrt(2.0 * r_eff * ht)


def earth_curvature_drop_m(ground_range_m: float, k_factor: float = 4.0 / 3.0) -> float:
    """给定地距下的等效地球曲率遮蔽下沉高度（米），用于远程目标真实高度与仰角修正。"""
    r_eff = max(1e5, float(k_factor) * EARTH_MEAN_R)
    r = max(0.0, float(ground_range_m))
    return (r * r) / (2.0 * r_eff)
