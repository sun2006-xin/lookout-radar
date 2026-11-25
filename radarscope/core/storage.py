"""持久化：会话 / 航迹 / 事件存 SQLite，强度矩阵单独存二进制文件。

这里有个工程上的判断必须说清楚：
**绝对不要把强度矩阵塞进数据库。**
20 帧/秒、180×144 的 float32 图，一小时就是 74 GB 的 BLOB，
SQLite 会被撑死，查询也会变得没法用。

所以拆成两层：
    SQLite  存"结构化、要查询、数据量小"的东西 —— 会话、航迹点、告警事件
    二进制  存"大块、只顺序读"的东西 —— 强度矩阵，帧级追加写，DB 里只记路径

这也是所有正经雷达记录设备的做法：航迹文件（track file）和视频记录（video）本来就分开存。

另外：SQLite 默认每次 commit 都 fsync，20 帧/秒写航迹会卡。
这里开了 WAL + 批量提交，写盘次数降到每秒两三次。
"""
from __future__ import annotations

import json
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import numpy as np

from .model import Capabilities, RadarFrame, Target

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started   REAL    NOT NULL,
    ended     REAL,
    driver    TEXT    NOT NULL,
    endpoint  TEXT,
    caps      TEXT,
    note      TEXT
);
CREATE TABLE IF NOT EXISTS tracks (
    sid    INTEGER NOT NULL,
    ts     REAL    NOT NULL,
    tid    INTEGER,
    x      REAL, y  REAL, vx REAL, vy REAL,
    rng    REAL, az REAL, snr REAL,
    threat INTEGER, cls TEXT
);
CREATE INDEX IF NOT EXISTS ix_tracks ON tracks(sid, ts);
CREATE TABLE IF NOT EXISTS events (
    sid     INTEGER,
    ts      REAL,
    level   TEXT,
    kind    TEXT,
    rule_id TEXT,
    message TEXT,
    active  INTEGER,
    tid     INTEGER
);
CREATE INDEX IF NOT EXISTS ix_events ON events(sid, ts);
CREATE TABLE IF NOT EXISTS archives (
    sid      INTEGER,
    path     TEXT,
    first_ts REAL,
    last_ts  REAL,
    frames   INTEGER
);
"""


@dataclass(slots=True)
class SessionInfo:
    id: int
    started: float
    ended: float | None
    driver: str
    endpoint: str
    caps: dict
    note: str

    @property
    def duration(self) -> float:
        return (self.ended or time.time()) - self.started


class FrameArchive:
    """强度矩阵的顺序归档文件。

    格式（小端）：
        b"RSF1" | n_az u16 | n_rng u16 | ts f64 | az f32[n_az] | r f32[n_rng] |
        data f16[n_az, n_rng]
    刻意用 float16 存：显示用途 16 位精度足够，体积直接砍一半。
    """

    MAGIC = b"RSF1"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "wb")
        self.frames = 0
        self.first_ts = 0.0
        self.last_ts = 0.0

    def append(self, frame: RadarFrame) -> bool:
        if not frame.has_scan or frame.az_axis is None or frame.r_axis is None:
            return False
        scan = np.ascontiguousarray(frame.scan, dtype=np.float16)
        n_az, n_rng = scan.shape
        self._fh.write(self.MAGIC)
        self._fh.write(struct.pack("<HH", n_az, n_rng))
        self._fh.write(struct.pack("<d", float(frame.timestamp)))
        self._fh.write(np.ascontiguousarray(frame.az_axis, dtype=np.float32).tobytes())
        self._fh.write(np.ascontiguousarray(frame.r_axis, dtype=np.float32).tobytes())
        self._fh.write(scan.tobytes())
        self.frames += 1
        if not self.first_ts:
            self.first_ts = frame.timestamp
        self.last_ts = frame.timestamp
        return True

    def close(self) -> None:
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass

    # ---- 读 ----
    @classmethod
    def read_frames(cls, path: str | Path) -> Iterator[tuple[float, np.ndarray, np.ndarray, np.ndarray]]:
        with open(path, "rb") as fh:
            while True:
                magic = fh.read(4)
                if len(magic) < 4:
                    return
                if magic != cls.MAGIC:
                    return
                head = fh.read(12)
                if len(head) < 12:
                    return
                n_az, n_rng = struct.unpack("<HH", head[:4])
                ts = struct.unpack("<d", head[4:12])[0]
                az = np.frombuffer(fh.read(4 * n_az), dtype=np.float32)
                r = np.frombuffer(fh.read(4 * n_rng), dtype=np.float32)
                data = np.frombuffer(fh.read(2 * n_az * n_rng), dtype=np.float16)
                if data.size != n_az * n_rng:
                    return
                yield ts, data.reshape(n_az, n_rng).astype(np.float32), az, r


class Store:
    """记录器 + 查询接口。线程安全（内部加锁）。"""

    def __init__(self, path: str | Path = "data/radarscope.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._pending_tracks: list[tuple] = []
        self._pending_events: list[tuple] = []
        self._last_flush = time.monotonic()
        self.session_id: int | None = None

    # ------------------------------------------------------------------ #
    def start_session(self, driver: str, endpoint: str = "",
                      caps: Capabilities | None = None, note: str = "") -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO sessions(started, driver, endpoint, caps, note) VALUES(?,?,?,?,?)",
                (time.time(), driver, endpoint,
                 json.dumps(_caps_to_dict(caps)) if caps else "{}", note))
            self._db.commit()
            self.session_id = int(cur.lastrowid)
            return self.session_id

    def end_session(self, note: str | None = None) -> None:
        with self._lock:
            self.flush(force=True)
            if self.session_id is None:
                return
            if note is not None:
                self._db.execute("UPDATE sessions SET ended=?, note=? WHERE id=?",
                                 (time.time(), note, self.session_id))
            else:
                self._db.execute("UPDATE sessions SET ended=? WHERE id=?",
                                 (time.time(), self.session_id))
            self._db.commit()
            self.session_id = None

    # ------------------------------------------------------------------ #
    def record_tracks(self, ts: float, tracks: Iterable[Target]) -> None:
        sid = self.session_id
        if sid is None:
            return
        ts = ts or time.time()
        for t in tracks:
            self._pending_tracks.append(
                (sid, ts, t.tid, t.x, t.y, t.vx, t.vy, t.rng, t.az_deg, t.snr,
                 t.threat, t.cls))
        self.flush()

    def record_event(self, level: str, kind: str, message: str,
                     rule_id: str = "", active: bool = True, tid: int = -1) -> None:
        sid = self.session_id
        if sid is None:
            return
        self._pending_events.append(
            (sid, time.time(), level, kind, rule_id, message, int(active), tid))
        self.flush()

    def flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_flush < 0.4:
            return
        with self._lock:
            if self._pending_tracks:
                self._db.executemany(
                    "INSERT INTO tracks(sid,ts,tid,x,y,vx,vy,rng,az,snr,threat,cls)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", self._pending_tracks)
                self._pending_tracks.clear()
            if self._pending_events:
                self._db.executemany(
                    "INSERT INTO events(sid,ts,level,kind,rule_id,message,active,tid)"
                    " VALUES(?,?,?,?,?,?,?,?)", self._pending_events)
                self._pending_events.clear()
            self._db.commit()
            self._last_flush = now

    # ------------------------------------------------------------------ #
    def attach_archive(self, sid: int, arc: FrameArchive) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO archives(sid,path,first_ts,last_ts,frames) VALUES(?,?,?,?,?)",
                (sid, str(arc.path), arc.first_ts, arc.last_ts, arc.frames))
            self._db.commit()

    # ---- 查询 ----
    def list_sessions(self, limit: int = 50) -> list[SessionInfo]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id,started,ended,driver,endpoint,caps,note FROM sessions"
                " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [SessionInfo(r[0], r[1], r[2], r[3], r[4] or "",
                            json.loads(r[5] or "{}"), r[6] or "") for r in rows]

    def tracks_of(self, sid: int, limit: int = 200000) -> list[tuple]:
        with self._lock:
            return self._db.execute(
                "SELECT ts,tid,x,y,vx,vy,rng,az,snr,threat,cls FROM tracks"
                " WHERE sid=? ORDER BY ts LIMIT ?", (sid, limit)).fetchall()

    def events_of(self, sid: int, limit: int = 2000) -> list[tuple]:
        with self._lock:
            return self._db.execute(
                "SELECT ts,level,kind,rule_id,message,active,tid FROM events"
                " WHERE sid=? ORDER BY ts DESC LIMIT ?", (sid, limit)).fetchall()

    def archive_of(self, sid: int) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT path FROM archives WHERE sid=? ORDER BY rowid DESC LIMIT 1",
                (sid,)).fetchone()
        return row[0] if row else None

    # ---- 统计与检索（M6 数据分析）----
    def alarm_summary(self, sid: int) -> list[tuple[str, str, int]]:
        """按规则统计告警次数。返回 [(rule_id, 最后一条消息, 次数)]。"""
        with self._lock:
            return self._db.execute(
                "SELECT rule_id, message, COUNT(*) c FROM events"
                " WHERE sid=? AND kind='alarm' GROUP BY rule_id ORDER BY c DESC",
                (sid,)).fetchall()

    def tracks_per_bucket(self, sid: int, bucket_s: int = 60) -> list[tuple[int, int]]:
        """按时间分桶统计航迹点数，用来画"目标活动随时间变化"。

        bucket_s 是桶宽（秒）。用整数除法分桶，SQL 里就能算，不用把百万行拉回内存。
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT CAST(ts / ? AS INTEGER) AS b, COUNT(*) FROM tracks"
                " WHERE sid=? GROUP BY b ORDER BY b", (bucket_s, sid)).fetchall()
        if not rows:
            return []
        base = rows[0][0]
        return [(int(b - base), int(c)) for b, c in rows]

    def query_events(self, sid: int | None = None, level: str | None = None,
                     rule_id: str | None = None, kind: str | None = None,
                     limit: int = 1000) -> list[tuple]:
        """事件检索：按会话 / 级别 / 规则 / 类型过滤。"""
        sql = ("SELECT ts, level, kind, rule_id, message, active, tid, sid"
               " FROM events WHERE 1=1")
        args: list = []
        if sid is not None:
            sql += " AND sid=?"
            args.append(sid)
        if level:
            sql += " AND level=?"
            args.append(level)
        if rule_id:
            sql += " AND rule_id=?"
            args.append(rule_id)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def distinct_rule_ids(self) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT rule_id FROM events WHERE rule_id<>''").fetchall()
        return sorted(r[0] for r in rows)

    def session_stats(self, sid: int) -> dict:
        with self._lock:
            n_trk = self._db.execute("SELECT COUNT(*) FROM tracks WHERE sid=?", (sid,)).fetchone()[0]
            n_evt = self._db.execute("SELECT COUNT(*) FROM events WHERE sid=?", (sid,)).fetchone()[0]
            row = self._db.execute(
                "SELECT MAX(tid), MIN(ts), MAX(ts) FROM tracks WHERE sid=?", (sid,)).fetchone()
        return {"tracks_rows": n_trk, "events": n_evt,
                "max_tid": row[0] or 0, "first_ts": row[1], "last_ts": row[2]}

    def close(self) -> None:
        with self._lock:
            try:
                self.flush(force=True)
                self._db.close()
            except Exception:
                pass


def _caps_to_dict(caps: Capabilities | None) -> dict:
    if caps is None:
        return {}
    return {
        "name": caps.name, "has_scan": caps.has_scan, "has_azimuth": caps.has_azimuth,
        "has_targets": caps.has_targets, "has_velocity": caps.has_velocity,
        "has_range_doppler": caps.has_range_doppler, "has_iff": caps.has_iff,
        "has_threat": caps.has_threat, "az_fov": caps.az_fov,
        "range_min": caps.range_min, "range_max": caps.range_max,
        "max_targets": caps.max_targets, "scan_mode": caps.scan_mode,
        "scan_rate": caps.scan_rate, "tier": caps.tier, "views": list(caps.views),
    }
