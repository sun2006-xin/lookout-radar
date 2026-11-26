"""录制夹具回归工具：离线验证 .bin 原始字节流与 .rad 帧归档。

用途（ROADMAP A5）：
    1. 不接参数时，自动校验 tests/fixtures/manifest.json 里的全部夹具；
    2. 传入单个 .bin / .rad 文件时，按指定驱动回放并断言帧数、峰值、目标数、CRC 拒绝数。

示例：
    .venv\\Scripts\\python.exe tools\\capture_check.py
    .venv\\Scripts\\python.exe tools\\capture_check.py data\\capture.bin --driver ldlidar --min-frames 5
    .venv\\Scripts\\python.exe tools\\capture_check.py data\\session.rad --min-frames 10 --min-peak 0.2
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import radarscope.drivers  # noqa: E402, F401  -- 注册全部驱动
from radarscope.core import pipeline  # noqa: E402
from radarscope.core.driver import available_drivers  # noqa: E402
from radarscope.core.storage import FrameArchive  # noqa: E402
from radarscope.core.transport import TransportError  # noqa: E402

MAX_FIXTURE_BYTES = 1024 * 1024  # 1 MB 上限，避免把大二进制塞进仓库
DEFAULT_MANIFEST = ROOT / "tests" / "fixtures" / "manifest.json"


@dataclass(slots=True)
class CaptureStats:
    """单次录制回放的统计结果。"""

    path: Path
    kind: str
    driver: str
    frames: int
    peak: float
    targets: int
    good: int
    bad: int


def _check_rad(path: Path) -> CaptureStats:
    """校验 .rad 帧归档（直接读包 + ReplayDriver 回放一致性）。"""
    archived = list(FrameArchive.read_frames(path))
    if not archived:
        raise AssertionError(f"{path.name}: FrameArchive 未读出任何帧")

    peak = 0.0
    for idx, (_ts, scan, az, r) in enumerate(archived):
        if scan.ndim != 2:
            raise AssertionError(f"{path.name}: 第 {idx} 帧维度异常 {scan.shape}")
        if scan.shape != (az.size, r.size):
            raise AssertionError(
                f"{path.name}: 第 {idx} 帧轴长 {az.size}x{r.size} 与矩阵 {scan.shape} 不匹配"
            )
        if not np.isfinite(scan).all():
            raise AssertionError(f"{path.name}: 第 {idx} 帧存在 NaN/Inf")
        peak = max(peak, float(scan.max()))

    drv_cls = available_drivers()["replay"]
    drv = drv_cls()
    drv.open(path=str(path), speed=1000.0, loop=False)
    caps = drv.resolve_capabilities()
    replayed = 0
    total_targets = 0
    try:
        for _ in range(len(archived) * 5 + 20):
            try:
                frame = drv.read()
            except RuntimeError:
                break
            if frame is None:
                continue
            replayed += 1
            norm = pipeline.normalize(frame, caps)
            total_targets += len(norm.targets)
    finally:
        drv.close()

    if replayed != len(archived):
        raise AssertionError(
            f"{path.name}: ReplayDriver 回放 {replayed} 帧，与归档 {len(archived)} 帧不一致"
        )
    return CaptureStats(
        path=path,
        kind="rad",
        driver="replay",
        frames=replayed,
        peak=peak,
        targets=total_targets,
        good=replayed,
        bad=0,
    )


def _check_bin(path: Path, driver_id: str, chunk: int = 256) -> CaptureStats:
    """用指定驱动通过 file:// 端点离线回放原始字节流 .bin。"""
    drivers = available_drivers()
    if driver_id not in drivers:
        raise ValueError(f"未知驱动 '{driver_id}'，可选：{', '.join(sorted(drivers))}")

    drv = drivers[driver_id]()
    uri = path.resolve().as_uri() + f"?chunk={max(1, int(chunk))}&interval=0&loop=0"
    drv.open(endpoint=uri)
    caps = drv.resolve_capabilities()

    frames = 0
    peak = 0.0
    total_targets = 0
    last_good = 0
    last_bad = 0
    deadline = time.monotonic() + 10.0

    try:
        while time.monotonic() < deadline:
            try:
                frame = drv.read()
            except TransportError:
                break
            if frame is None:
                # 旋转激光驱动内部按 1/_rate 节流，短暂让出时间即可继续吐帧
                time.sleep(0.01)
                continue
            norm = pipeline.normalize(frame, caps)
            frames += 1
            if norm.scan is not None and norm.scan.size:
                peak = max(peak, float(norm.scan.max()))
            total_targets += len(norm.targets)
            if "good" in norm.meta:
                last_good = int(norm.meta["good"])
            if "bad" in norm.meta:
                last_bad = int(norm.meta["bad"])
    finally:
        drv.close()

    return CaptureStats(
        path=path,
        kind="bin",
        driver=driver_id,
        frames=frames,
        peak=peak,
        targets=total_targets,
        good=last_good or frames,
        bad=last_bad,
    )


def verify_capture(
    path: Path,
    driver: str = "",
    *,
    chunk: int = 256,
    min_frames: int = 1,
    max_frames: int | None = None,
    min_peak: float = 0.0,
    min_targets: int = 0,
    expected_bad: int | None = None,
    max_bad_ratio: float | None = None,
    enforce_size_limit: bool = False,
) -> CaptureStats:
    """校验单个录制文件并按断言阈值检查。"""
    if not path.exists():
        raise FileNotFoundError(f"录制文件不存在：{path}")
    size = path.stat().st_size
    if enforce_size_limit and size > MAX_FIXTURE_BYTES:
        raise AssertionError(
            f"{path.name}: 体积 {size} 字节超过夹具上限 {MAX_FIXTURE_BYTES} 字节"
        )

    suffix = path.suffix.lower()
    if suffix == ".rad":
        stats = _check_rad(path)
    else:
        if not driver:
            raise ValueError(f"{path.name}: .bin 文件必须指定 --driver")
        stats = _check_bin(path, driver, chunk=chunk)

    if stats.frames < min_frames:
        raise AssertionError(f"{path.name}: 帧数 {stats.frames} < 最小要求 {min_frames}")
    if max_frames is not None and stats.frames > max_frames:
        raise AssertionError(f"{path.name}: 帧数 {stats.frames} > 最大上限 {max_frames}")
    if stats.peak < min_peak:
        raise AssertionError(f"{path.name}: 峰值 {stats.peak:.3f} < 最小要求 {min_peak:.3f}")
    if stats.targets < min_targets:
        raise AssertionError(f"{path.name}: 目标数 {stats.targets} < 最小要求 {min_targets}")
    if expected_bad is not None and stats.bad != expected_bad:
        raise AssertionError(f"{path.name}: CRC/错帧拒绝数 {stats.bad} != 期望 {expected_bad}")
    if max_bad_ratio is not None:
        total = max(1, stats.good + stats.bad)
        ratio = stats.bad / total
        if ratio > max_bad_ratio:
            raise AssertionError(
                f"{path.name}: 坏帧率 {ratio:.3f} > 上限 {max_bad_ratio:.3f}"
            )
    return stats


def verify_manifest(manifest_path: Path = DEFAULT_MANIFEST) -> list[CaptureStats]:
    """批量校验 manifest.json 中登记的所有夹具。"""
    if not manifest_path.exists():
        raise FileNotFoundError(f"夹具清单不存在：{manifest_path}")
    entries: list[dict[str, Any]] = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not entries:
        raise AssertionError(f"夹具清单为空：{manifest_path}")

    base = manifest_path.parent
    results: list[CaptureStats] = []
    for item in entries:
        stats = verify_capture(
            base / item["file"],
            driver=item.get("driver", ""),
            chunk=int(item.get("chunk", 256)),
            min_frames=int(item.get("min_frames", 1)),
            max_frames=item.get("max_frames"),
            min_peak=float(item.get("min_peak", 0.0)),
            min_targets=int(item.get("min_targets", 0)),
            expected_bad=item.get("expected_bad"),
            max_bad_ratio=item.get("max_bad_ratio"),
            enforce_size_limit=True,
        )
        results.append(stats)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线校验 .bin / .rad 雷达录制夹具")
    parser.add_argument("file", nargs="?", help="单个录制文件路径；省略则校验全部夹具清单")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="夹具 manifest.json 路径")
    parser.add_argument("--driver", default="", help=".bin 文件对应的驱动 ID")
    parser.add_argument("--chunk", type=int, default=256, help="file:// 每次读取字节数")
    parser.add_argument("--min-frames", type=int, default=1, help="最少期望帧数")
    parser.add_argument("--max-frames", type=int, default=None, help="最多允许帧数")
    parser.add_argument("--min-peak", type=float, default=0.0, help="最小期望峰值强度")
    parser.add_argument("--min-targets", type=int, default=0, help="最少期望累计目标数")
    parser.add_argument("--expected-bad", type=int, default=None, help="精确期望的坏帧/CRC 拒绝数")
    parser.add_argument("--max-bad-ratio", type=float, default=None, help="允许的最大坏帧率 (0~1)")
    args = parser.parse_args(argv)

    if args.file:
        st = verify_capture(
            Path(args.file),
            driver=args.driver,
            chunk=args.chunk,
            min_frames=args.min_frames,
            max_frames=args.max_frames,
            min_peak=args.min_peak,
            min_targets=args.min_targets,
            expected_bad=args.expected_bad,
            max_bad_ratio=args.max_bad_ratio,
        )
        print(
            f"[ok] {st.path.name} ({st.driver}) · 帧={st.frames} · "
            f"峰值={st.peak:.2f} · 目标={st.targets} · 好/坏={st.good}/{st.bad}"
        )
        return 0

    results = verify_manifest(Path(args.manifest))
    for st in results:
        print(
            f"  [ok] {st.path.name:<24} {st.driver:<10} "
            f"frames={st.frames:<3} peak={st.peak:.2f} targets={st.targets:<3} bad={st.bad}"
        )
    print(f"夹具校验全部通过：{len(results)} 份")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
