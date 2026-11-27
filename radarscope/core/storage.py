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
import logging
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
CREATE INDEX IF NOT EXISTS ix_tracks_tid ON tracks(sid, tid, ts);
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
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(sid, kind, rule_id);
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
    """雷达帧归档文件（兼容 RSF1 紧凑矩阵与 RSF2 全维度自描述帧）。

    RSF1（小端）：
        b"RSF1" | n_az u16 | n_rng u16 | ts f64 | az f32[n_az] | r f32[n_rng] | data f16[n_az, n_rng]
    RSF2（小端，含长度可跳转索引与统一单调/UTC双时钟）：
        b"RSF2" | rec_len u32 | body[rec_len]
        body: mono_ts f64 | utc_ts f64 | flags u8 | n_az u16 | n_rng u16 | n_dop u16 | n_el u16
              | az f32 | r f32 | scan f16 | [rd_v f32 + rd f16] | [el f32 + rhi f16] | meta_targets_json
    """

    MAGIC = b"RSF1"
    MAGIC_V2 = b"RSF2"

    def __init__(self, path: str | Path, version: int = 2) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.version = 2 if int(version) >= 2 else 1
        self._fh = open(self.path, "wb")
        self.frames = 0
        self.first_ts = 0.0
        self.last_ts = 0.0

    def append(self, frame: RadarFrame, utc_ts: float | None = None) -> bool:
        if not frame.has_scan or frame.az_axis is None or frame.r_axis is None:
            return False
        scan = np.ascontiguousarray(frame.scan, dtype=np.float16)
        n_az, n_rng = scan.shape
        az_b = np.ascontiguousarray(frame.az_axis, dtype=np.float32).tobytes()
        r_b = np.ascontiguousarray(frame.r_axis, dtype=np.float32).tobytes()
        sc_b = scan.tobytes()
        ts_val = float(frame.timestamp)

        if self.version == 1:
            self._fh.write(self.MAGIC)
            self._fh.write(struct.pack("<HH", n_az, n_rng))
            self._fh.write(struct.pack("<d", ts_val))
            self._fh.write(az_b)
            self._fh.write(r_b)
            self._fh.write(sc_b)
        else:
            flags = 0
            rd_b = b""
            n_dop = 0
            if frame.rd is not None and frame.rd_v_axis is not None and frame.rd.shape == (frame.rd_v_axis.size, n_rng):
                flags |= 0x01
                n_dop = int(frame.rd_v_axis.size)
                rd_b = (
                    np.ascontiguousarray(frame.rd_v_axis, dtype=np.float32).tobytes()
                    + np.ascontiguousarray(frame.rd, dtype=np.float16).tobytes()
                )
            rhi_b = b""
            n_el = 0
            if frame.rhi is not None and frame.el_axis is not None and frame.rhi.shape == (frame.el_axis.size, n_rng):
                flags |= 0x02
                n_el = int(frame.el_axis.size)
                rhi_b = (
                    np.ascontiguousarray(frame.el_axis, dtype=np.float32).tobytes()
                    + np.ascontiguousarray(frame.rhi, dtype=np.float16).tobytes()
                )
            safe_meta = {
                k: v for k, v in (frame.meta or {}).items()
                if isinstance(v, (str, int, float, bool, list, dict, type(None)))
            }
            js = json.dumps({
                "source": frame.source,
                "meta": safe_meta,
                "targets": [
                    [float(t.x), float(t.y), float(t.vx), float(t.vy),
                     float(getattr(t, "z", 0.0)), float(getattr(t, "el_deg", 0.0)),
                     float(getattr(t, "rcs", 0.0)), float(t.snr), int(t.tid),
                     int(t.threat), str(t.cls)]
                    for t in frame.targets
                ],
            }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            utc = float(time.time() if utc_ts is None else utc_ts)
            body = b"".join([
                struct.pack("<ddBHHHH", ts_val, utc, flags, n_az, n_rng, n_dop, n_el),
                az_b, r_b, sc_b, rd_b, rhi_b, js,
            ])
            self._fh.write(self.MAGIC_V2)
            self._fh.write(struct.pack("<I", len(body)))
            self._fh.write(body)

        self.frames += 1
        if not self.first_ts:
            self.first_ts = ts_val
        self.last_ts = ts_val
        return True

    def close(self) -> None:
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass

    @classmethod
    def _decode_v2_body(cls, body: bytes) -> tuple[RadarFrame, float] | None:
        hdr_sz = struct.calcsize("<ddBHHHH")
        if len(body) < hdr_sz:
            return None
        mono_ts, utc_ts, flags, n_az, n_rng, n_dop, n_el = struct.unpack_from("<ddBHHHH", body, 0)
        pos = hdr_sz
        az = np.frombuffer(body, dtype=np.float32, count=n_az, offset=pos).copy()
        pos += 4 * n_az
        r = np.frombuffer(body, dtype=np.float32, count=n_rng, offset=pos).copy()
        pos += 4 * n_rng
        scan = np.frombuffer(body, dtype=np.float16, count=n_az * n_rng, offset=pos).reshape(n_az, n_rng).astype(np.float32)
        pos += 2 * n_az * n_rng
        rd = rd_v = None
        if (flags & 0x01) and n_dop > 0:
            rd_v = np.frombuffer(body, dtype=np.float32, count=n_dop, offset=pos).copy()
            pos += 4 * n_dop
            rd = np.frombuffer(body, dtype=np.float16, count=n_dop * n_rng, offset=pos).reshape(n_dop, n_rng).astype(np.float32)
            pos += 2 * n_dop * n_rng
        rhi = el_ax = None
        if (flags & 0x02) and n_el > 0:
            el_ax = np.frombuffer(body, dtype=np.float32, count=n_el, offset=pos).copy()
            pos += 4 * n_el
            rhi = np.frombuffer(body, dtype=np.float16, count=n_el * n_rng, offset=pos).reshape(n_el, n_rng).astype(np.float32)
            pos += 2 * n_el * n_rng
        meta: dict = {}
        targets: list[Target] = []
        source = ""
        if pos < len(body):
            try:
                info = json.loads(body[pos:].decode("utf-8"))
                source = str(info.get("source") or "")
                meta = dict(info.get("meta") or {})
                for item in info.get("targets") or ():
                    targets.append(Target(
                        x=item[0], y=item[1], vx=item[2], vy=item[3],
                        z=item[4], el_deg=item[5], rcs=item[6],
                        snr=item[7], tid=int(item[8]), threat=int(item[9]), cls=str(item[10]),
                    ))
            except Exception:
                pass
        meta["utc_ts"] = utc_ts
        return RadarFrame(
            timestamp=mono_ts, scan=scan, az_axis=az, r_axis=r,
            targets=targets, rd=rd, rd_v_axis=rd_v, rhi=rhi, el_axis=el_ax,
            source=source, meta=meta,
        ), utc_ts

    # ---- 读 ----
    @classmethod
    def read_full_frames(cls, path: str | Path) -> Iterator[RadarFrame]:
        """读取全部完整 RadarFrame（兼容 RSF1 与 RSF2，含 RD/RHI/目标/UTC时钟）。"""
        with open(path, "rb") as fh:
            while True:
                magic = fh.read(4)
                if len(magic) < 4:
                    return
                if magic == cls.MAGIC:
                    head = fh.read(12)
                    if len(head) < 12:
                        return
                    n_az, n_rng = struct.unpack("<HH", head[:4])
                    ts = struct.unpack("<d", head[4:12])[0]
                    az = np.frombuffer(fh.read(4 * n_az), dtype=np.float32).copy()
                    r = np.frombuffer(fh.read(4 * n_rng), dtype=np.float32).copy()
                    data = np.frombuffer(fh.read(2 * n_az * n_rng), dtype=np.float16)
                    if data.size != n_az * n_rng:
                        return
                    yield RadarFrame(
                        timestamp=ts, scan=data.reshape(n_az, n_rng).astype(np.float32),
                        az_axis=az, r_axis=r,
                    )
                elif magic == cls.MAGIC_V2:
                    raw_len = fh.read(4)
                    if len(raw_len) < 4:
                        return
                    rec_len = struct.unpack("<I", raw_len)[0]
                    body = fh.read(rec_len)
                    if len(body) < rec_len:
                        return
                    res = cls._decode_v2_body(body)
                    if res is None:
                        return
                    yield res[0]
                else:
                    return

    @classmethod
    def read_frames(cls, path: str | Path) -> Iterator[tuple[float, np.ndarray, np.ndarray, np.ndarray]]:
        for f in cls.read_full_frames(path):
            if f.scan is not None and f.az_axis is not None and f.r_axis is not None:
                yield f.timestamp, f.scan, f.az_axis, f.r_axis

    @classmethod
    def index_frames(cls, path: str | Path) -> list[tuple[int, float, float]]:
        """快速扫描归档帧偏移索引 [(file_offset, mono_ts, utc_ts), ...]，用于 O(1) 随机跳转。"""
        idx: list[tuple[int, float, float]] = []
        with open(path, "rb") as fh:
            while True:
                off = fh.tell()
                magic = fh.read(4)
                if len(magic) < 4:
                    break
                if magic == cls.MAGIC:
                    head = fh.read(12)
                    if len(head) < 12:
                        break
                    n_az, n_rng = struct.unpack("<HH", head[:4])
                    ts = struct.unpack("<d", head[4:12])[0]
                    skip = 4 * n_az + 4 * n_rng + 2 * n_az * n_rng
                    fh.seek(skip, 1)
                    idx.append((off, ts, ts))
                elif magic == cls.MAGIC_V2:
                    raw_len = fh.read(4)
                    if len(raw_len) < 4:
                        break
                    rec_len = struct.unpack("<I", raw_len)[0]
                    hdr = fh.read(16)
                    if len(hdr) < 16:
                        break
                    mono_ts, utc_ts = struct.unpack("<dd", hdr)
                    fh.seek(max(0, rec_len - 16), 1)
                    idx.append((off, mono_ts, utc_ts))
                else:
                    break
        return idx

    @classmethod
    def read_frame_at(cls, path: str | Path, offset: int) -> RadarFrame | None:
        with open(path, "rb") as fh:
            fh.seek(int(offset))
            magic = fh.read(4)
            if magic == cls.MAGIC:
                head = fh.read(12)
                if len(head) < 12:
                    return None
                n_az, n_rng = struct.unpack("<HH", head[:4])
                ts = struct.unpack("<d", head[4:12])[0]
                az = np.frombuffer(fh.read(4 * n_az), dtype=np.float32).copy()
                r = np.frombuffer(fh.read(4 * n_rng), dtype=np.float32).copy()
                data = np.frombuffer(fh.read(2 * n_az * n_rng), dtype=np.float16)
                if data.size != n_az * n_rng:
                    return None
                return RadarFrame(timestamp=ts, scan=data.reshape(n_az, n_rng).astype(np.float32),
                                  az_axis=az, r_axis=r)
            if magic == cls.MAGIC_V2:
                raw_len = fh.read(4)
                if len(raw_len) < 4:
                    return None
                rec_len = struct.unpack("<I", raw_len)[0]
                body = fh.read(rec_len)
                if len(body) < rec_len:
                    return None
                res = cls._decode_v2_body(body)
                return res[0] if res is not None else None
        return None


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
        self._migrate_schema()
        self._close_dangling_sessions()
        self._db.commit()
        self._pending_tracks: list[tuple] = []
        self._pending_events: list[tuple] = []
        self._last_flush = time.monotonic()
        self.session_id: int | None = None
        # 写失败必须可见：航迹没进库却一声不吭，等于"以为在留证其实没留"
        self.write_errors = 0
        self.last_error = ""

    def _migrate_schema(self) -> None:
        """向后兼容补齐 3D 高度 / 仰角 / RCS 列，旧库无痛升级。"""
        cols = {row[1] for row in self._db.execute("PRAGMA table_info(tracks)").fetchall()}
        for col, spec in (("z", "REAL DEFAULT 0.0"), ("el_deg", "REAL DEFAULT 0.0"), ("rcs", "REAL DEFAULT 0.0")):
            if col not in cols:
                self._db.execute(f"ALTER TABLE tracks ADD COLUMN {col} {spec}")

    def _close_dangling_sessions(self) -> None:
        """将上次异常崩溃未调用 end_session() 的历史挂起会话回填合理结束时间。"""
        self._db.execute(
            "UPDATE sessions SET ended = COALESCE("
            "(SELECT MAX(ts) FROM tracks WHERE sid = sessions.id), started)"
            " WHERE ended IS NULL"
        )

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
        with self._lock:
            for t in tracks:
                self._pending_tracks.append((
                    sid, ts, t.tid, t.x, t.y, t.vx, t.vy, t.rng, t.az_deg,
                    t.snr, t.threat, t.cls,
                    float(getattr(t, "z", 0.0) or 0.0),
                    float(getattr(t, "el_deg", 0.0) or 0.0),
                    float(getattr(t, "rcs", 0.0) or 0.0),
                ))
        self.flush()

    def record_event(self, level: str, kind: str, message: str,
                     rule_id: str = "", active: bool = True, tid: int = -1,
                     ts: float | None = None) -> None:
        sid = self.session_id
        if sid is None:
            return
        with self._lock:
            self._pending_events.append(
                (sid, ts if ts is not None else time.time(), level, kind, rule_id, message, int(active), tid))
        self.flush()

    def flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_flush < 0.4:
            return
        with self._lock:
            try:
                if self._pending_tracks:
                    self._db.executemany(
                        "INSERT INTO tracks(sid,ts,tid,x,y,vx,vy,rng,az,snr,threat,cls,z,el_deg,rcs)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", self._pending_tracks)
                    self._pending_tracks.clear()
                if self._pending_events:
                    self._db.executemany(
                        "INSERT INTO events(sid,ts,level,kind,rule_id,message,active,tid)"
                        " VALUES(?,?,?,?,?,?,?,?)", self._pending_events)
                    self._pending_events.clear()
                self._db.commit()
                self._last_flush = now
            except Exception as exc:
                # 不吞：计数 + 留最后一条错误，界面状态栏会亮出来。
                # pending 保留以便下次重试，但设上限，避免一直失败把内存吃光。
                self.write_errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                logging.getLogger("radarscope").warning(
                    "存储写入失败(%d)：%s", self.write_errors, self.last_error)
                if len(self._pending_tracks) > 5000:
                    del self._pending_tracks[:2500]
                if len(self._pending_events) > 5000:
                    del self._pending_events[:2500]

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

    def tracks_of(self, sid: int, limit: int = 200000,
                  include_3d: bool = False) -> list[tuple]:
        cols = (
            "ts,tid,x,y,vx,vy,rng,az,snr,threat,cls,z,el_deg,rcs"
            if include_3d else
            "ts,tid,x,y,vx,vy,rng,az,snr,threat,cls"
        )
        with self._lock:
            return self._db.execute(
                f"SELECT {cols} FROM tracks WHERE sid=? ORDER BY ts LIMIT ?",
                (sid, limit),
            ).fetchall()

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

    def track_summaries(self, sid: int) -> list[dict]:
        """按目标航迹号聚合历史会话详情（点数 / 最近最远距 / 最大速度 / 峰值SNR / 最高威胁 / 高度）。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT tid, COUNT(*) AS pts, MIN(rng), MAX(rng), "
                "MAX(SQRT(vx*vx + vy*vy)), MAX(snr), MAX(threat), "
                "MAX(COALESCE(cls, '')), MAX(COALESCE(z, 0.0)), MIN(ts), MAX(ts) "
                "FROM tracks WHERE sid=? GROUP BY tid ORDER BY pts DESC, tid ASC",
                (sid,),
            ).fetchall()
        return [
            {
                "tid": int(r[0]),
                "points": int(r[1]),
                "min_rng": float(r[2] or 0.0),
                "max_rng": float(r[3] or 0.0),
                "max_speed": float(r[4] or 0.0),
                "peak_snr": float(r[5] or 0.0),
                "max_threat": int(r[6] or 0),
                "cls": str(r[7] or ""),
                "max_z": float(r[8] or 0.0),
                "first_ts": float(r[9] or 0.0),
                "last_ts": float(r[10] or 0.0),
            }
            for r in rows
        ]

    def delete_session(self, sid: int) -> None:
        """事务级删除指定会话及其关联航迹/事件/归档索引。"""
        with self._lock:
            if self.session_id == sid:
                self.session_id = None
            for tbl in ("tracks", "events", "archives"):
                self._db.execute(f"DELETE FROM {tbl} WHERE sid=?", (sid,))
            self._db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            self._db.commit()

    def cleanup_empty_sessions(self) -> int:
        """清理既无航迹点又无事件的历史空会话，返回清理数量。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM sessions WHERE id != COALESCE(?, -1) "
                "AND NOT EXISTS (SELECT 1 FROM tracks WHERE tracks.sid = sessions.id) "
                "AND NOT EXISTS (SELECT 1 FROM events WHERE events.sid = sessions.id)",
                (self.session_id,),
            ).fetchall()
            sids = [int(r[0]) for r in rows]
            for sid in sids:
                self._db.execute("DELETE FROM archives WHERE sid=?", (sid,))
                self._db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            self._db.commit()
            return len(sids)

    def tracks_window(self, sid: int, t0: float, t1: float,
                      include_3d: bool = False) -> list[tuple]:
        """按时间窗口查询航迹（走 ix_tracks(sid, ts) 索引，用于时间轴回放 C3）。"""
        cols = (
            "ts, tid, x, y, vx, vy, rng, az, snr, threat, cls, z, el_deg, rcs"
            if include_3d else
            "ts, tid, x, y, vx, vy, rng, az, snr, threat, cls"
        )
        with self._lock:
            return self._db.execute(
                f"SELECT {cols} FROM tracks WHERE sid=? AND ts>=? AND ts<=? ORDER BY ts ASC",
                (sid, float(t0), float(t1)),
            ).fetchall()

    def events_until(self, sid: int, ts: float, limit: int = 100) -> list[tuple]:
        """查询截至指定时刻的历史告警事件（走 ix_events(sid, ts) 索引）。"""
        with self._lock:
            return self._db.execute(
                "SELECT ts, level, kind, rule_id, message, active, tid"
                " FROM events WHERE sid=? AND ts<=? ORDER BY ts DESC LIMIT ?",
                (sid, float(ts), int(limit)),
            ).fetchall()

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
