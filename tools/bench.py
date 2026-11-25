"""性能基准：量一下这台软件的瓶颈到底在哪。

存在的理由很实际 —— "要不要用 Rust 重构"这种问题，凭感觉讨论没有意义。
把每帧的时间拆开看清楚，才知道值不值得换语言：
  · 如果 80% 时间在 numpy 的散射/FFT 里，那已经是 C 的速度，换语言没收益
  · 如果 80% 时间在 Python 循环里，那 Rust（或 Cython/PyO3）就有意义

用法：
    .venv\\Scripts\\python.exe tools\\bench.py

输出是 Markdown 表格，可以直接贴进 docs/ADR。
"""
from __future__ import annotations

import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

import radarscope.drivers  # noqa: F401  注册驱动
from radarscope.core import pipeline
from radarscope.core.driver import available_drivers
from radarscope.core.model import Capabilities, Target
from radarscope.core.tracker import Tracker


def timeit(fn, n: int = 60, warmup: int = 5) -> float:
    """返回单次耗时的中位数（毫秒）。中位数比均值更抗偶发卡顿。"""
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(samples)


# --------------------------------------------------------------------------- #
def bench_drivers() -> list[tuple[str, str, float, int]]:
    """各驱动的单帧耗时与产出帧率。"""
    rows = []
    for did, cls in sorted(available_drivers().items()):
        if getattr(cls, "source", "") not in ("sim",):
            continue                       # 真实设备没接，跳过
        try:
            d = cls()
            d.open()
        except Exception as exc:
            rows.append((did, cls.label, -1.0, 0))
            print(f"  [skip] {did}: {exc}")
            continue
        frames = 0
        t_emit = []
        t_last = time.perf_counter()
        t0 = time.perf_counter()
        while frames < 30 and time.perf_counter() - t0 < 3.0:
            f = d.read()
            now = time.perf_counter()
            if f is not None:
                frames += 1
                t_emit.append((now - t_last) * 1000.0)
                t_last = now
        d.close()
        ms = statistics.median(t_emit) if t_emit else -1.0
        rows.append((did, cls.label, ms, frames))
    return rows


def bench_pipeline() -> list[tuple[str, float]]:
    """补全流水线各环节耗时（这是每帧都要跑的）。"""
    caps = Capabilities(has_scan=True, has_azimuth=True, az_fov=120.0,
                        range_min=0.2, range_max=8.0)
    # 造一帧有内容的矩阵
    from radarscope.core.model import RadarFrame
    f_src = pipeline.synthesize_scan(
        caps, [Target(x=1.0, y=3.0), Target(x=-2.0, y=5.0), Target(x=0.5, y=6.0)])

    rng = np.random.default_rng(7)
    scan = np.clip(f_src.scan + rng.normal(0.02, 0.01, f_src.scan.shape), 0, 1).astype(np.float32)
    frame = RadarFrame(timestamp=time.monotonic(), scan=scan,
                       az_axis=f_src.az_axis, r_axis=f_src.r_axis)

    out = [
        ("CFAR 检测（121×144 栅格）", timeit(lambda: pipeline.detect_targets(frame, 0.30), 40)),
        ("矩阵反投影（目标→矩阵）", timeit(lambda: pipeline.synthesize_scan(
            caps, [Target(x=1.0, y=3.0), Target(x=-2.0, y=5.0)]), 40)),
        ("距离剖面", timeit(lambda: pipeline.range_profile(frame), 40)),
    ]
    return out


def bench_tracker() -> list[tuple[str, float]]:
    trk = Tracker()
    dets = [Target(x=0.2 * i, y=3.0 + 0.1 * i, snr=0.6) for i in range(8)]

    def step() -> None:
        trk.update(dets, 0.05, -1)

    return [("航迹关联+滤波（8 目标）", timeit(step, 200))]


def bench_ppi_math() -> list[tuple[str, float]]:
    """PPI 渲染里纯 numpy 的部分（不含 Qt 绘制）。

    LUT 散射 + 两趟 3×3 膨胀是最耗时的两块，单独测它们就能判断
    "换语言有没有用"。
    """
    res = 512
    rng = np.random.default_rng(3)
    # 模拟 360×200 的融合栅格映射到 512² 画布
    n_az, n_rng = 360, 200
    idx = rng.integers(0, res * res, n_az * n_rng)
    vals = rng.random(n_az * n_rng).astype(np.float32)
    buf = np.zeros(res * res, dtype=np.float32)

    def scatter() -> None:
        buf[:] = 0.0
        buf[idx] = np.maximum(buf[idx], vals)

    a = (rng.random((res, res)) * 255).astype(np.uint8)

    def dilate() -> None:
        b = a.copy()
        np.maximum(b[1:, :], a[:-1, :], out=b[1:, :])
        np.maximum(b[:-1, :], a[1:, :], out=b[:-1, :])
        c = b.copy()
        np.maximum(c[:, 1:], b[:, :-1], out=c[:, 1:])
        np.maximum(c[:, :-1], b[:, 1:], out=c[:, :-1])

    return [
        ("PPI LUT 散射（7.2 万格）", timeit(scatter, 60)),
        ("PPI 两趟 3×3 膨胀（512²）", timeit(dilate, 60)),
    ]


def bench_storage() -> list[tuple[str, float]]:
    import tempfile
    from radarscope.core.storage import Store
    td = tempfile.mkdtemp()
    st = Store(Path(td) / "b.db")
    st.start_session("bench", "x", Capabilities(name="bench"))
    dets = [Target(x=0.1 * i, y=2.0, tid=i) for i in range(8)]

    def one() -> None:
        st.record_tracks(time.time(), dets)
        st.flush(force=True)

    ms = timeit(one, 40)
    st.end_session("done")
    st.close()
    return [("SQLite 写一帧航迹（8 目标，强制提交）", ms)]


# --------------------------------------------------------------------------- #
def main() -> int:
    print(f"机器：{platform.processor() or platform.machine()} · "
          f"{platform.system()} {platform.release()}")
    print(f"Python {sys.version.split()[0]} · numpy {np.__version__}")
    print()

    print("## 驱动单帧耗时\n")
    print("| 驱动 | 名称 | 单帧(ms) | 采样帧数 |")
    print("|---|---|---:|---:|")
    for did, label, ms, n in bench_drivers():
        print(f"| `{did}` | {label} | {ms:.1f} | {n} |")
    print()

    rows = bench_pipeline() + bench_tracker() + bench_ppi_math() + bench_storage()
    print("## 每帧固定开销\n")
    print("| 环节 | 中位耗时(ms) |")
    print("|---|---:|")
    total = 0.0
    for name, ms in rows:
        print(f"| {name} | {ms:.2f} |")
        total += ms
    print(f"| **合计** | **{total:.2f}** |")
    print()
    print(f"→ 单帧预算 50 ms（20 fps）时，占用 {total / 50 * 100:.0f}%。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
