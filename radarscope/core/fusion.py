"""多站融合内核。

场景：一个 60° 的模块只能看一个扇区。想 360° 无死角，两条路：
    1. 加舵机转 —— 慢（一圈几秒），有机械寿命，转动时还会引入平台多普勒
    2. 摆 6 个模块，各管 60° —— 全向瞬时覆盖，无机械件，冗余

第 2 条是这类 24GHz 模块唯一现实的"全向"方案，也是本模块要解决的问题。

融合要做的事只有两件：
    **空间对齐**：每个模块有自己的安装位置 (x,y) 和朝向 bearing，
                 它报的本地坐标 (ρ, α) 必须换算到平台坐标系。
    **扇区拼接**：把各模块的强度矩阵重采样到一张公共的全向栅格上。

难点在性能：4~6 个模块、每个上万个格子，每帧都做一遍坐标变换会卡死。
所以这里预计算 LUT（本地格 → 全局格的下标），每帧只做一次花式散射赋值。

坐标约定（全工程统一）：
    x 向右为正，y 向前为正，方位角 az = atan2(x, y)，正前 0°，顺时针为正。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from math import atan2, cos, degrees, hypot, radians, sin, pi

import numpy as np

from .model import Capabilities, RadarFrame, Target
from .geometry import locate, ray_of


@dataclass(slots=True)
class SensorSpec:
    """一台子雷达的安装参数。"""

    name: str
    driver: str = ""          # 驱动 id
    endpoint: str = ""        # 端点；虚拟传感器留空
    x: float = 0.0            # 安装位置，米
    y: float = 0.0
    bearing_deg: float = 0.0  # 朝向，0=正前，顺时针为正
    fov_deg: float = 120.0    # 方位覆盖宽度
    range_max: float = 8.0
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name, "driver": self.driver, "endpoint": self.endpoint,
            "x": self.x, "y": self.y, "bearing_deg": self.bearing_deg,
            "fov_deg": self.fov_deg, "range_max": self.range_max,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SensorSpec":
        return cls(
            name=str(d.get("name", "S?")),
            driver=str(d.get("driver", "")),
            endpoint=str(d.get("endpoint", "")),
            x=float(d.get("x", 0.0)),
            y=float(d.get("y", 0.0)),
            bearing_deg=float(d.get("bearing_deg", 0.0)),
            fov_deg=float(d.get("fov_deg", 120.0)),
            range_max=float(d.get("range_max", 8.0)),
            enabled=bool(d.get("enabled", True)),
        )


def _majority(values: list[str]) -> str:
    """取众数（忽略空串）：融合多个站对同一目标的分类时用。"""
    vals = [v for v in values if v]
    if not vals:
        return ""
    best = max(set(vals), key=vals.count)
    return best


def local_to_global(x: float, y: float, spec: SensorSpec) -> tuple[float, float]:
    """本地坐标 -> 平台坐标。先按 bearing 旋转，再平移到安装位置。"""
    b = radians(spec.bearing_deg)
    cb, sb = cos(b), sin(b)
    gx = x * cb + y * sb + spec.x
    gy = y * cb - x * sb + spec.y
    return gx, gy


def global_to_local(x: float, y: float, spec: SensorSpec) -> tuple[float, float]:
    b = radians(spec.bearing_deg)
    cb, sb = cos(b), sin(b)
    dx, dy = x - spec.x, y - spec.y
    return dx * cb - dy * sb, dy * cb + dx * sb


class _Child:
    __slots__ = ("spec", "dev", "lut_key", "lut_idx", "lut_ok", "frame",
                 "last_frame_t", "frames", "errors", "last_err", "fps_t", "fps_n", "fps")

    def __init__(self, spec: SensorSpec, dev):
        self.spec = spec
        self.dev = dev
        self.lut_key = None
        self.lut_idx = None
        self.lut_ok = None
        self.frame: RadarFrame | None = None
        self.last_frame_t = 0.0
        self.frames = 0
        self.errors = 0
        self.last_err = ""
        self.fps_t = time.monotonic()
        self.fps_n = 0
        self.fps = 0.0


class FusionEngine:
    """把 N 台子雷达融成一帧全向数据。

    对外只暴露 `poll()` 一个方法，返回的 RadarFrame 和单台雷达长得一模一样，
    所以 pipeline / Tracker / 所有视图都不需要改 —— 融合是对上层透明的。
    """

    def __init__(self, n_az: int = 360, n_rng: int = 200,
                 range_min: float = 0.15, range_max: float = 10.0,
                 rate: float = 20.0, stale_s: float = 2.0) -> None:
        self.n_az = n_az
        self.n_rng = n_rng
        self.range_min = range_min
        self.range_max = range_max
        self.rate = rate
        self.stale_s = stale_s

        # ---- 目标级融合参数 ----
        #: 是否在重叠区用方位线交会定位（真融合），关掉就退化成"各测各的取平均"
        self.triangulate = True
        #: 同一个目标被不同站观测到，判定为同一批的距离门限
        self.cluster_m = 1.2
        #: 两站间距小于这个值时不做交会（基线太短会病态，解出来的点没意义）
        self.min_baseline = 0.3
        self.stats: dict[str, int] = {"clusters": 0, "tri": 0, "single": 0}

        self._grid_az = np.linspace(-pi, pi, n_az, endpoint=False).astype(np.float32)
        self._grid_r = np.linspace(range_min, range_max, n_rng).astype(np.float32)
        self._acc = np.zeros((n_az, n_rng), dtype=np.float32)
        self._children: list[_Child] = []
        self._t_last = 0.0
        self._t_start = time.monotonic()

    # ------------------------------------------------------------------ #
    def attach(self, spec: SensorSpec, dev) -> None:
        """挂一台子雷达。dev 只需实现 read() / close()，鸭子类型即可。"""
        self._children.append(_Child(spec, dev))

    def detach_all(self) -> None:
        for c in self._children:
            try:
                c.dev.close()
            except Exception:
                pass
        self._children.clear()

    # ------------------------------------------------------------------ #
    def capabilities(self) -> Capabilities:
        has_targets = any(
            getattr(c.dev, "capabilities", lambda: Capabilities())().has_targets
            for c in self._children)
        return Capabilities(
            name="多站融合",
            has_scan=True, has_azimuth=True, has_targets=has_targets,
            has_velocity=has_targets,
            az_fov=360.0, az_resolution=round(360.0 / self.n_az, 2),
            range_min=self.range_min, range_max=self.range_max,
            max_targets=16, scan_mode="staring", scan_rate=self.rate,
            tier="fusion",
            transports=("virtual", "serial", "tcp", "udp"),
            views=("ppi", "ascope", "waterfall", "tracks"),
            notes=f"{len(self._children)} 台子雷达 · 全向拼接",
        )

    # ------------------------------------------------------------------ #
    def _build_lut(self, child: _Child, frame: RadarFrame) -> None:
        """预计算：本地 (az, r) 网格 -> 全局栅格下标。"""
        if frame.az_axis is None or frame.r_axis is None:
            child.lut_idx = None
            return
        az = frame.az_axis.astype(np.float32)
        r = frame.r_axis.astype(np.float32)
        b = radians(child.spec.bearing_deg)

        # 只算一遍三角，然后外积展开成 (n_az, n_rng) 的全局坐标
        sa, ca = np.sin(az + b), np.cos(az + b)
        gx = child.spec.x + np.outer(sa, r)
        gy = child.spec.y + np.outer(ca, r)

        gr = np.hypot(gx, gy)
        ga = np.arctan2(gx, gy)                       # [-pi, pi]
        az_i = np.floor((ga + pi) / (2 * pi) * self.n_az).astype(np.int32)
        np.clip(az_i, 0, self.n_az - 1, out=az_i)
        r_i = np.rint((gr - self.range_min) / max(1e-6, self.range_max - self.range_min)
                      * (self.n_rng - 1)).astype(np.int32)

        ok = (gr >= self.range_min) & (gr <= self.range_max)
        child.lut_idx = (az_i * self.n_rng + r_i).astype(np.int64)
        child.lut_ok = ok
        child.lut_key = (az.size, r.size, float(az[0]), float(r[-1]), child.spec.bearing_deg)

    def _need_lut(self, child: _Child, frame: RadarFrame) -> bool:
        if child.lut_key is None or child.lut_idx is None:
            return True
        az, r = frame.az_axis, frame.r_axis
        if az is None or r is None:
            return False
        key = (az.size, r.size, float(az[0]), float(r[-1]), child.spec.bearing_deg)
        return key != child.lut_key

    # ------------------------------------------------------------------ #
    def poll(self) -> RadarFrame | None:
        now = time.monotonic()

        # 每台子雷达都非阻塞地取一次；没数据就沿用上一帧（传感器帧率不同很正常）
        for c in self._children:
            try:
                f = c.dev.read()
            except Exception as exc:
                c.errors += 1
                c.last_err = f"{type(exc).__name__}: {exc}"
                continue
            if f is not None:
                c.frame = f
                c.last_frame_t = now
                c.frames += 1
                c.fps_n += 1
                if now - c.fps_t >= 1.0:
                    c.fps = c.fps_n / (now - c.fps_t)
                    c.fps_n = 0
                    c.fps_t = now

        if now - self._t_last < 1.0 / self.rate:
            return None
        self._t_last = now

        self._acc.fill(0.0)
        flat = self._acc.ravel()
        obs: list[tuple[SensorSpec, Target, float, float, float]] = []

        for c in self._children:
            if not c.spec.enabled or c.frame is None:
                continue
            age = now - c.last_frame_t
            if age > self.stale_s:
                continue                                  # 数据太旧，不参与本次融合

            f = c.frame
            if f.has_scan:
                if self._need_lut(c, f):
                    self._build_lut(c, f)
                if c.lut_idx is not None and c.lut_ok is not None:
                    vals = f.scan
                    if vals.shape == c.lut_ok.shape:
                        idx = c.lut_idx[c.lut_ok]
                        v = vals[c.lut_ok]
                        # 重叠扇区取较大值（近似 max 融合）
                        flat[idx] = np.maximum(flat[idx], v)

            for t in f.targets:
                gx, gy = local_to_global(t.x, t.y, c.spec)
                # 记下"本地角"，交会时要用原始角度而不是换算后的坐标：
                # 坐标里已经混进了距离误差，角度才是方位线真正的信息
                obs.append((c.spec, t, degrees(atan2(t.x, t.y)), gx, gy))

        fused_targets = self._fuse_targets(obs)

        return RadarFrame(
            timestamp=now,
            scan=self._close_gaps(),
            az_axis=self._grid_az,
            r_axis=self._grid_r,
            targets=fused_targets,
            source="融合",
            meta={
                "sensors": self.sensor_status(),
                "coverage": self.coverage(),
                "fusion": True,
                "fusion_stats": dict(self.stats),
            },
        )

    # ------------------------------------------------------------------ #
    def _close_gaps(self) -> np.ndarray:
        """补散射留下的空洞。

        子雷达的距离格比全局栅格粗（比如本地 120 格映射到全局 200 格），
        散射之后会有约四成的格子没人写，画出来就是一圈放射状条纹、
        A 显上是一条梳齿。做一次 3x3 最大值闭运算就补平了，
        代价是把目标边缘扩了 1 格（4cm / 1°），对显示没有影响。
        """
        a = self._acc
        b = a.copy()
        np.maximum(b[1:, :], a[:-1, :], out=b[1:, :])
        np.maximum(b[:-1, :], a[1:, :], out=b[:-1, :])
        c = b.copy()
        np.maximum(c[:, 1:], b[:, :-1], out=c[:, 1:])
        np.maximum(c[:, :-1], b[:, 1:], out=c[:, :-1])
        return c

    # ------------------------------------------------------------------ #
    def _fuse_targets(self, obs) -> list[Target]:
        """目标级融合：先聚类（哪些观测其实是同一个目标），再定位置。

        为什么值得多写这四十行：
        单纯"各测各的、最后取平均"的精度受单台测距误差限制；
        而两台的**方位线**可以在距离有误差的情况下仍然交会出更准的位置——
        方位角误差小，距离误差大，这是雷达的普遍特点，交会正好扬长避短。
        """
        clusters: list[list[tuple]] = []
        for spec, t, ang, gx, gy in obs:
            for cl in clusters:
                # 同一台雷达的两个目标不能被并进同一个簇，否则会把两个人合成一个
                if any(m[0].name == spec.name for m in cl):
                    continue
                cx = sum(m[3] for m in cl) / len(cl)
                cy = sum(m[4] for m in cl) / len(cl)
                if hypot(gx - cx, gy - cy) <= self.cluster_m:
                    cl.append((spec, t, ang, gx, gy))
                    break
            else:
                clusters.append([(spec, t, ang, gx, gy)])

        self.stats = {"clusters": len(clusters), "tri": 0, "single": 0}
        out: list[Target] = []
        for cl in clusters:
            x, y = self._centroid(cl)
            if len(cl) >= 2 and self.triangulate:
                pos = self._triangulate(cl)
                if pos is not None:
                    x, y = pos
                    self.stats["tri"] += 1
                else:
                    self.stats["single"] += 1
            else:
                self.stats["single"] += 1

            n = len(cl)
            out.append(Target(
                x=x, y=y,
                vx=sum(m[1].vx for m in cl) / n,
                vy=sum(m[1].vy for m in cl) / n,
                snr=max(m[1].snr for m in cl),
                threat=max(m[1].threat for m in cl),
                cls=_majority([m[1].cls for m in cl]),
                rcs=max(m[1].rcs for m in cl),
                views=n,
            ))
        return out

    @staticmethod
    def _centroid(cl) -> tuple[float, float]:
        n = len(cl)
        return sum(m[3] for m in cl) / n, sum(m[4] for m in cl) / n

    def _triangulate(self, cl) -> tuple[float, float] | None:
        # 基线检查：两台装在一起时两条射线共起点，最小二乘会解回起点，
        # 那不是"更准"，是彻底错。这种情况必须判无效并退回坐标变换。
        for i in range(len(cl)):
            for j in range(i + 1, len(cl)):
                a, b = cl[i][0], cl[j][0]
                if hypot(a.x - b.x, a.y - b.y) < self.min_baseline:
                    return None
        return locate([ray_of(m[0], m[2]) for m in cl])

    # ------------------------------------------------------------------ #
    def sensor_status(self) -> list[dict]:
        now = time.monotonic()
        out = []
        for c in self._children:
            age = (now - c.last_frame_t) if c.last_frame_t else 1e9
            out.append({
                "name": c.spec.name,
                "endpoint": c.spec.endpoint or "虚拟",
                "bearing": c.spec.bearing_deg,
                "fov": c.spec.fov_deg,
                "x": c.spec.x, "y": c.spec.y,
                "online": c.spec.enabled and age <= self.stale_s,
                "fps": round(c.fps, 1),
                "frames": c.frames,
                "errors": c.errors,
                "last_err": c.last_err,
            })
        return out

    def latest_observations(self) -> dict[str, list[Target]]:
        """每台子雷达最近一帧的原始目标（**本地坐标**，没做融合）。

        标定要用它：融合后的坐标已经把参数算进去了，反推不出偏置；
        只有原始观测才能和"这个点应该落在多少度"作比较。

        返回值可能是上一帧的（传感器帧率不同），调用方应按时间窗口多次采样。
        """
        out: dict[str, list[Target]] = {}
        for c in self._children:
            f = c.frame
            if f is not None and f.targets:
                out[c.spec.name] = list(f.targets)
        return out

    def specs(self) -> list[SensorSpec]:
        return [c.spec for c in self._children]

    def coverage(self) -> list[dict]:
        """各扇区覆盖，供界面画覆盖图和找盲区。"""
        return [{
            "name": c.spec.name,
            "bearing": c.spec.bearing_deg,
            "fov": c.spec.fov_deg,
            "range": min(c.spec.range_max, self.range_max),
            "online": c.spec.enabled,
        } for c in self._children]

    def gaps(self) -> list[tuple[float, float]]:
        """未被任何扇区覆盖的方位区间（度），用来在界面上标红。

        这就是"360° 无死角"到底做没做到的量化答案：
        扇区宽度之和 >= 360 不等于无死角，安装角度摆错了照样有洞。

        实现上注意：**必须按整数度逐一判定**，不能从扇区边缘起按 1°
        采样再四舍五入 —— 边缘是 37.5° 这种半度值时，取整会逢单跳一格，
        算出来是一百多个 1° 的假盲区。
        """
        if not self._children:
            return []
        cov = np.zeros(360, dtype=bool)
        for c in self._children:
            if not c.spec.enabled:
                continue
            half = c.spec.fov_deg / 2.0
            b = c.spec.bearing_deg
            for deg in range(360):
                d = abs((deg - b + 180.0) % 360.0 - 180.0)
                if d <= half:
                    cov[deg] = True

        gaps: list[tuple[float, float]] = []
        start = None
        for a in range(360):
            if not cov[a] and start is None:
                start = a
            elif cov[a] and start is not None:
                gaps.append((float(start), float(a)))
                start = None
        if start is not None:
            if gaps and gaps[0][0] == 0.0:
                gaps[0] = (float(start - 360), gaps[0][1])
            else:
                gaps.append((float(start), 360.0))
        return gaps
