"""一体化任务录制包与离线重算对比引擎（ROADMAP 任务闭环）。

将过去分散的原始帧归档 (.rad)、串口/UDP 字节录制 (.bin)、SQLite 会话元数据、
以及不同信号处理参数（CFAR / 杂波抑制）的离线重算对比收拢为统一任务包：
    data/missions/mission_YYYYmmdd-HHMMSS/
        manifest.json   # 驱动、端点、Capabilities、采集时检测参数快照
        frames.rad      # FrameArchive 强度矩阵序列
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import pipeline
from .storage import FrameArchive
from .tracker import Tracker


@dataclass
class MissionManifest:
    mission_id: str
    created_at: float
    driver: str
    endpoint: str
    caps: dict[str, Any] = field(default_factory=dict)
    settings_snapshot: dict[str, Any] = field(default_factory=dict)
    rad_file: str = "frames.rad"
    raw_file: str = ""
    session_id: int | None = None
    frames: int = 0
    note: str = ""
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MissionManifest:
        return cls(
            mission_id=str(data.get("mission_id", "")),
            created_at=float(data.get("created_at", 0.0)),
            driver=str(data.get("driver", "")),
            endpoint=str(data.get("endpoint", "")),
            caps=dict(data.get("caps") or {}),
            settings_snapshot=dict(data.get("settings_snapshot") or {}),
            rad_file=str(data.get("rad_file", "frames.rad")),
            raw_file=str(data.get("raw_file", "")),
            session_id=data.get("session_id"),
            frames=int(data.get("frames", 0)),
            note=str(data.get("note", "")),
            version=int(data.get("version", 1)),
        )


class MissionRecorder:
    """统一开启与封装单次雷达任务包。"""

    def __init__(
        self,
        missions_root: str | Path,
        driver: str,
        endpoint: str,
        caps: Any,
        settings_snapshot: dict[str, Any] | None = None,
        session_id: int | None = None,
        note: str = "",
    ) -> None:
        ts = time.time()
        mid = "mission_" + time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
        root = Path(missions_root)
        bundle_dir = root / mid
        suffix = 1
        while bundle_dir.exists():
            suffix += 1
            bundle_dir = root / f"{mid}_{suffix}"
        bundle_dir.mkdir(parents=True, exist_ok=True)

        self.dir = bundle_dir
        self.rad_path = bundle_dir / "frames.rad"
        self.archive = FrameArchive(self.rad_path)
        caps_dict = {
            "name": getattr(caps, "name", ""),
            "range_max": getattr(caps, "range_max", 8.0),
            "scan_mode": getattr(caps, "scan_mode", "staring"),
            "has_azimuth": getattr(caps, "has_azimuth", False),
            "has_elevation": getattr(caps, "has_elevation", False),
            "has_range_doppler": getattr(caps, "has_range_doppler", False),
        }
        self.manifest = MissionManifest(
            mission_id=bundle_dir.name,
            created_at=ts,
            driver=driver,
            endpoint=endpoint,
            caps=caps_dict,
            settings_snapshot=dict(settings_snapshot or {}),
            rad_file="frames.rad",
            session_id=session_id,
            note=note,
        )
        self._write_manifest()

    def _write_manifest(self) -> None:
        (self.dir / "manifest.json").write_text(
            json.dumps(self.manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def append_frame(self, frame) -> None:
        self.archive.append(frame)
        self.manifest.frames = self.archive.frames

    def close(self) -> MissionManifest:
        self.archive.close()
        self.manifest.frames = self.archive.frames
        self._write_manifest()
        return self.manifest


def list_missions(missions_root: str | Path, limit: int = 50) -> list[tuple[Path, MissionManifest]]:
    root = Path(missions_root)
    if not root.exists():
        return []
    out: list[tuple[Path, MissionManifest]] = []
    for p in sorted(root.iterdir(), key=lambda x: x.name, reverse=True):
        mf = p / "manifest.json"
        if p.is_dir() and mf.exists():
            try:
                m = MissionManifest.from_dict(json.loads(mf.read_text(encoding="utf-8")))
                out.append((p, m))
            except Exception:
                continue
        if len(out) >= limit:
            break
    return out


def resolve_mission_rad(path: str | Path) -> tuple[Path, MissionManifest | None]:
    """给定任务目录、manifest.json 或 .rad 文件，统一解析出可回放的 .rad 与元数据。"""
    p = Path(path)
    if p.is_dir():
        mf = p / "manifest.json"
        if mf.exists():
            m = MissionManifest.from_dict(json.loads(mf.read_text(encoding="utf-8")))
            return p / m.rad_file, m
        cands = sorted(p.glob("*.rad"))
        if cands:
            return cands[0], None
        raise FileNotFoundError(f"任务目录中未找到 frames.rad：{p}")
    if p.name == "manifest.json" and p.exists():
        m = MissionManifest.from_dict(json.loads(p.read_text(encoding="utf-8")))
        return p.parent / m.rad_file, m
    return p, None


def _run_pipeline_pass(
    raw_frames: list[tuple[float, Any, Any, Any]],
    cfar_mode: str,
    threshold: float,
    use_clutter: bool,
    confirm_hits: int,
    confirm_window: int,
    filter_mode: str,
) -> tuple[dict[str, Any], list[dict[str, int]]]:
    cmap = pipeline.ClutterMap()
    cmap.enabled = bool(use_clutter)
    trk = Tracker(filter_mode=filter_mode, confirm_hits=confirm_hits, confirm_window=confirm_window)
    total_dets = 0
    max_tracks = 0
    confirmed_ids: set[int] = set()
    snr_sum = 0.0
    per: list[dict[str, int]] = []
    prev_ts = None

    from .model import RadarFrame

    for idx, (ts, scan, az, r_ax) in enumerate(raw_frames):
        dt = max(0.02, min(0.5, (ts - prev_ts) if prev_ts is not None else 0.05))
        prev_ts = ts
        fr = RadarFrame(timestamp=float(ts), scan=scan.copy(), az_axis=az, r_axis=r_ax)
        if cmap.enabled:
            fr = cmap.apply(fr)
        dets = pipeline.detect_targets(fr, threshold=threshold, max_targets=16, cfar_mode=cfar_mode)
        tracks = trk.update(dets, dt)
        confirmed = [t for t in tracks if not t.tentative]
        total_dets += len(dets)
        snr_sum += sum(d.snr for d in dets)
        max_tracks = max(max_tracks, len(confirmed))
        for t in confirmed:
            confirmed_ids.add(t.tid)
        per.append({"idx": idx, "dets": len(dets), "tracks": len(confirmed)})

    n = max(1, len(raw_frames))
    summary = {
        "cfar": cfar_mode,
        "clutter": bool(use_clutter),
        "filter": filter_mode,
        "confirm": f"{confirm_hits}/{confirm_window}",
        "total_dets": total_dets,
        "avg_dets": round(total_dets / n, 2),
        "max_tracks": max_tracks,
        "unique_confirmed": len(confirmed_ids),
        "mean_snr": round(snr_sum / max(1, total_dets), 3),
    }
    return summary, per


def recompute_mission_comparison(
    path: str | Path,
    *,
    baseline_cfar: str = "ca",
    compare_cfar: str = "auto",
    threshold: float = 0.30,
    baseline_clutter: bool = False,
    compare_clutter: bool = True,
    confirm_hits: int = 3,
    confirm_window: int = 5,
    filter_mode: str = "kalman3d",
    max_frames: int = 300,
) -> dict[str, Any]:
    """用两组信号处理与跟踪参数离线重算同一份任务录制帧，输出指标对比矩阵。"""
    rad_path, manifest = resolve_mission_rad(path)
    frames = []
    for i, item in enumerate(FrameArchive.read_frames(rad_path)):
        frames.append(item)
        if i + 1 >= max_frames:
            break
    if not frames:
        raise ValueError(f"录制文件无有效矩阵帧：{rad_path}")

    base_sum, base_per = _run_pipeline_pass(
        frames, baseline_cfar, threshold, baseline_clutter, 1, 1, "alpha_beta",
    )
    cmp_sum, cmp_per = _run_pipeline_pass(
        frames, compare_cfar, threshold, compare_clutter, confirm_hits, confirm_window, filter_mode,
    )
    timeline = [
        {
            "idx": b["idx"],
            "base_dets": b["dets"],
            "cmp_dets": c["dets"],
            "base_tracks": b["tracks"],
            "cmp_tracks": c["tracks"],
        }
        for b, c in zip(base_per, cmp_per)
    ]
    return {
        "rad_path": str(rad_path),
        "manifest": manifest.to_dict() if manifest else None,
        "frames": len(frames),
        "baseline": base_sum,
        "compare": cmp_sum,
        "timeline": timeline,
    }
