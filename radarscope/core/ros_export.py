"""ROS 2 / Foxglove Studio 遥测格式导出与 MCAP v0 容器封装（ROADMAP E8）。

零第三方依赖，纯标准库实现：
    1. to_ros_laserscan():   转换 RadarFrame 为 ROS 2 sensor_msgs/msg/LaserScan 兼容 JSON
    2. to_ros_pointcloud2(): 转换目标/航迹列表为 Foxglove / PointCloud2 兼容 JSON
    3. write_mcap_json_log(): 将逐帧 LaserScan/PointCloud JSON 写入标准二进制 .mcap v0 文件
"""
from __future__ import annotations

import json
import struct
import time
from pathlib import Path

import numpy as np

from .model import RadarFrame, Target

_MCAP_MAGIC = b"\x89MCAP0\r\n"


def _mcap_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<I", len(b)) + b


def _mcap_record(opcode: int, body: bytes) -> bytes:
    return struct.pack("<BQ", opcode, len(body)) + body


def to_ros_laserscan(
    frame: RadarFrame,
    frame_id: str = "radar_link",
    threshold: float = 0.28,
) -> dict:
    """将 RadarFrame（极坐标矩阵或点目标）转换为 ROS 2 sensor_msgs/msg/LaserScan 兼容字典。"""
    ts = float(frame.timestamp or time.time())
    sec = int(ts)
    nsec = int((ts - sec) * 1e9)
    rmax = float(frame.r_axis[-1]) if frame.r_axis is not None and frame.r_axis.size else 10.0
    rmin = float(frame.r_axis[0]) if frame.r_axis is not None and frame.r_axis.size else 0.1

    ranges: list[float | None] = []
    intensities: list[float] = []
    if frame.has_scan and frame.scan is not None and frame.az_axis is not None and frame.az_axis.size:
        amin = float(frame.az_axis[0])
        amax = float(frame.az_axis[-1])
        ainc = (amax - amin) / max(1, frame.az_axis.size - 1)
        r_ax = frame.r_axis if frame.r_axis is not None else np.linspace(rmin, rmax, frame.scan.shape[1])
        for row in frame.scan:
            idx = int(np.argmax(row))
            val = float(row[idx])
            intensities.append(round(val, 4))
            ranges.append(round(float(r_ax[idx]), 4) if val >= threshold else None)
    else:
        amin, amax, ainc = -3.14159265, 3.14159265, 0.01745329
        for t in frame.targets:
            ranges.append(round(float(t.rng), 4))
            intensities.append(round(float(t.snr), 4))

    return {
        "header": {"stamp": {"sec": sec, "nanosec": nsec}, "frame_id": frame_id},
        "angle_min": round(amin, 6),
        "angle_max": round(amax, 6),
        "angle_increment": round(ainc, 6),
        "time_increment": 0.0,
        "scan_time": 0.1,
        "range_min": round(rmin, 4),
        "range_max": round(rmax, 4),
        "ranges": ranges,
        "intensities": intensities,
    }


def to_ros_pointcloud2(
    frame: RadarFrame | None,
    tracks: list[Target] | None = None,
    frame_id: str = "radar_link",
) -> dict:
    """组装 Foxglove / ROS 兼容的三维目标点云消息。"""
    src = tracks if tracks is not None else (frame.targets if frame is not None else [])
    ts = float(frame.timestamp if frame is not None and frame.timestamp else time.time())
    sec = int(ts)
    nsec = int((ts - sec) * 1e9)
    pts = [
        {
            "tid": int(t.tid),
            "x": round(float(t.x), 4),
            "y": round(float(t.y), 4),
            "z": round(float(getattr(t, "z", 0.0)), 4),
            "vx": round(float(t.vx), 4),
            "vy": round(float(t.vy), 4),
            "intensity": round(float(t.snr), 4),
        }
        for t in src
    ]
    return {
        "header": {"stamp": {"sec": sec, "nanosec": nsec}, "frame_id": frame_id},
        "fields": ["tid", "x", "y", "z", "vx", "vy", "intensity"],
        "count": len(pts),
        "points": pts,
    }


def write_mcap_json_log(
    path: Path,
    messages: list[dict],
    topic: str = "/radar/laserscan",
    schema_name: str = "lookout.LaserScan",
) -> int:
    """写入含真实时间戳、Chunk 压缩块、Statistics 与 SummaryOffset 索引的标准 MCAP v0 日志。"""
    import zlib

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = bytearray()
    buf.extend(_MCAP_MAGIC)

    # Header (0x01): profile(str) + library(str)
    buf.extend(_mcap_record(0x01, _mcap_str("ros2") + _mcap_str("Lookout-MCAP/2.0")))

    schema_json = json.dumps({"type": "object", "title": schema_name}).encode("utf-8")
    schema_rec = _mcap_record(
        0x03,
        struct.pack("<H", 1) + _mcap_str(schema_name) + _mcap_str("jsonschema")
        + struct.pack("<I", len(schema_json)) + schema_json,
    )
    chan_rec = _mcap_record(0x04, struct.pack("<HH", 1, 1) + _mcap_str(topic) + _mcap_str("json") + struct.pack("<I", 0))
    buf.extend(schema_rec)
    buf.extend(chan_rec)

    now_ns = int(time.time() * 1e9)
    first_hdr_ns: int | None = None
    msg_buf = bytearray()
    written = 0
    start_ns = 0
    end_ns = 0
    for seq, msg in enumerate(messages):
        raw = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        stamp = (msg.get("header") or {}).get("stamp") if isinstance(msg, dict) else None
        if isinstance(stamp, dict) and ("sec" in stamp):
            ts_ns = int(stamp.get("sec", 0)) * 1_000_000_000 + int(stamp.get("nanosec", 0))
            if ts_ns < 1_000_000_000_000:  # relative monotonic stamp -> shift to wall epoch
                if first_hdr_ns is None:
                    first_hdr_ns = ts_ns
                ts_ns = now_ns + (ts_ns - first_hdr_ns)
        else:
            ts_ns = now_ns + seq * 100_000_000
        if written == 0:
            start_ns = ts_ns
        end_ns = ts_ns
        msg_buf.extend(_mcap_record(0x05, struct.pack("<HIQQ", 1, seq + 1, ts_ns, ts_ns) + raw))
        written += 1

    # Chunk (0x06): msg_start_time(u64), msg_end_time(u64), uncompressed_size(u64), uncompressed_crc(u32), compression(str), records(u64_len + bytes)
    raw_bytes = bytes(msg_buf)
    crc = zlib.crc32(raw_bytes) & 0xFFFFFFFF
    comp_bytes = zlib.compress(raw_bytes)
    chunk_body = (
        struct.pack("<QQQI", start_ns, end_ns, len(raw_bytes), crc)
        + _mcap_str("zlib")
        + struct.pack("<Q", len(comp_bytes))
        + comp_bytes
    )
    buf.extend(_mcap_record(0x06, chunk_body))

    # DataEnd (0x0F): data_section_crc(u32)
    data_crc = zlib.crc32(bytes(buf[len(_MCAP_MAGIC):])) & 0xFFFFFFFF
    buf.extend(_mcap_record(0x0F, struct.pack("<I", data_crc)))

    summary_start = len(buf)
    summary_buf = bytearray()
    summary_buf.extend(schema_rec)
    summary_buf.extend(chan_rec)
    # Statistics (0x0B): message_count(u64) schema_count(u16) channel_count(u32) attachment_count(u32) metadata_count(u32) chunk_count(u32) start(u64) end(u64) + chan_msg_counts_map_u32_len
    stat_map = struct.pack("<HQ", 1, written)
    stat_body = (
        struct.pack("<QHIIIIQQ", written, 1, 1, 0, 0, 1, start_ns, end_ns)
        + struct.pack("<I", len(stat_map)) + stat_map
    )
    stat_off = summary_start + len(summary_buf)
    stat_rec = _mcap_record(0x0B, stat_body)
    summary_buf.extend(stat_rec)
    buf.extend(summary_buf)

    summary_offset_start = len(buf)
    buf.extend(_mcap_record(0x0E, struct.pack("<BQQ", 0x0B, stat_off, len(stat_rec))))
    sum_crc = zlib.crc32(bytes(buf[summary_start:])) & 0xFFFFFFFF

    # Footer (0x02): summary_start(u64) + summary_offset_start(u64) + summary_crc(u32)
    buf.extend(_mcap_record(0x02, struct.pack("<QQI", summary_start, summary_offset_start, sum_crc)))
    buf.extend(_MCAP_MAGIC)
    path.write_bytes(bytes(buf))
    return written
