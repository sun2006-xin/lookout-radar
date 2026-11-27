"""无界面雷达核心运行时引擎（Headless RadarEngineRuntime，ROADMAP I1）。

硬约束：零 PyQt5 依赖。同时服务于：
  1. `main.py --daemon` 无头守护服务（Linux/Windows 工控机或容器 7x24 运行 + UDP 转发 + 落库）
  2. 自动化端到端长稳压测与核心链路回归（无需创建 QApplication/QMainWindow）
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from . import cpa, pipeline
from .alarm import AlarmEngine, DEFAULT_RULES
from .driver import RadarDriver, get_driver
from .forwarder import Forwarder
from .model import Capabilities, RadarFrame, Target
from .storage import FrameArchive, Store
from .tracker import Tracker

LOG = logging.getLogger("radarscope.runtime")


class RadarEngineRuntime:
    """无 UI 雷达实时处理流水线：驱动采集 -> normalize -> 杂波抑制 -> 跟踪 -> 告警 -> 转发/存储。"""

    def __init__(
        self,
        driver_id: str = "sim",
        endpoint: str = "",
        db_path: str | Path | None = None,
        archive_path: str | Path | None = None,
        forward_target: str | None = None,
        cfar_mode: str = "auto",
        thresh: float = 0.30,
        filter_mode: str = "kalman3d",
        assoc_mode: str = "gnn",
    ) -> None:
        self.driver_id = driver_id
        self.endpoint = endpoint
        self.cfar_mode = cfar_mode
        self.thresh = float(thresh)
        self.driver: RadarDriver = get_driver(driver_id)()
        self.caps: Capabilities = Capabilities()
        self.tracker = Tracker(filter_mode=filter_mode, assoc_mode=assoc_mode)
        self.alarm = AlarmEngine(list(DEFAULT_RULES))
        self.clutter_map = pipeline.ClutterMap()
        self.store = Store(db_path) if db_path else None
        self.archive = FrameArchive(archive_path, version=2) if archive_path else None
        self.forwarder = Forwarder()
        if forward_target:
            self.forwarder.configure(enabled=True, target=forward_target, rate_hz=10.0)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_ts = 0.0
        self.frames = 0
        self.errors = 0
        self.last_error = ""
        self._latest_snapshot: dict = {"frames": 0, "tracks": [], "alarms": []}

    def open(self) -> Capabilities:
        kw = {"endpoint": self.endpoint} if self.endpoint else {}
        self.driver.open(**kw)
        self.caps = self.driver.resolve_capabilities()
        if self.store is not None:
            self.store.start_session(self.driver_id, self.endpoint, self.caps, note="headless")
        return self.caps

    def step(self) -> RadarFrame | None:
        """执行单步同步采集与处理；返回处理后的帧，无数据时返回 None。"""
        raw = self.driver.read()
        if raw is None:
            return None
        f = pipeline.normalize(raw, self.caps, threshold=self.thresh, cfar_mode=self.cfar_mode)
        if self.clutter_map.enabled and f.has_scan:
            f = self.clutter_map.apply(f)
            if self.clutter_map.frames >= self.clutter_map.warmup:
                f.targets = pipeline.detect_targets(
                    f, threshold=self.thresh,
                    max_targets=max(8, self.caps.max_targets or 8),
                    cfar_mode=self.cfar_mode,
                )
        now = f.timestamp or time.monotonic()
        dt = (now - self._last_ts) if self._last_ts else 0.1
        self._last_ts = now

        tracks = self.tracker.update(f.targets, dt)
        events = self.alarm.evaluate(tracks, self.caps)
        if self.forwarder.enabled:
            self.forwarder.send(f, tracks)
        if self.store is not None and self.store.session_id is not None:
            self.store.record_tracks(time.time(), tracks)
            for ev in events:
                self.store.record_event(ev.level, "alarm", ev.message,
                                        rule_id=ev.rule_id, active=ev.active, tid=ev.tid)
        if self.archive is not None and f.has_scan:
            self.archive.append(f)

        cpa_res = cpa.evaluate(tracks, 0.0, 0.0)
        self.frames += 1
        with self._lock:
            self._latest_snapshot = {
                "frames": self.frames,
                "ts": time.time(),
                "driver": self.driver_id,
                "range_max": self.caps.range_max,
                "tracks": [
                    {
                        "tid": t.tid, "x": round(t.x, 3), "y": round(t.y, 3),
                        "z": round(getattr(t, "z", 0.0), 3),
                        "vx": round(t.vx, 3), "vy": round(t.vy, 3),
                        "rng": round(t.rng, 3), "az": round(t.az_deg, 2),
                        "snr": round(t.snr, 3), "cls": t.cls,
                    }
                    for t in tracks
                ],
                "alarms": [e.message for e in events],
                "cpa_count": len(cpa_res),
            }
        return f

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._latest_snapshot)

    def start(self, on_frame: Callable[[RadarFrame], None] | None = None) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self.caps.name:
            self.open()
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.is_set():
                try:
                    f = self.step()
                    if f is not None and on_frame is not None:
                        on_frame(f)
                except Exception as exc:
                    self.errors += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    LOG.warning("Headless step error: %s", self.last_error)
                    time.sleep(0.05)

        self._thread = threading.Thread(target=_loop, name="lookout-runtime", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            self.driver.close()
        except Exception:
            pass
        self.forwarder.stop()
        if self.archive is not None:
            if self.store is not None and self.store.session_id is not None:
                self.store.attach_archive(self.store.session_id, self.archive)
            self.archive.close()
            self.archive = None
        if self.store is not None:
            if self.store.session_id is not None:
                self.store.end_session("headless退出")
            self.store.close()
            self.store = None
