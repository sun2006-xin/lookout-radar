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
    """写入标准 MCAP v0 二进制日志（Header + Schema + Channel + Messages + Footer）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = bytearray()
    buf.extend(_MCAP_MAGIC)

    # Header (0x01): profile(str) + library(str)
    buf.extend(_mcap_record(0x01, _mcap_str("ros2") + _mcap_str("Lookout-MCAP/1.0")))

    # Schema (0x03): id(u16) + name(str) + encoding(str) + data(bytes_with_u32_len)
    schema_json = json.dumps({"type": "object", "title": schema_name}).encode("utf-8")
    schema_body = struct.pack("<H", 1) + _mcap_str(schema_name) + _mcap_str("jsonschema") + struct.pack("<I", len(schema_json)) + schema_json
    buf.extend(_mcap_record(0x03, schema_body))

    # Channel (0x04): id(u16) + schema_id(u16) + topic(str) + message_encoding(str) + metadata_map_u32_len(0)
    chan_body = struct.pack("<HH", 1, 1) + _mcap_str(topic) + _mcap_str("json") + struct.pack("<I", 0)
    buf.extend(_mcap_record(0x04, chan_body))

    base_ns = int(time.time() * 1e9)
    written = 0
    for seq, msg in enumerate(messages):
        raw = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ts_ns = base_ns + seq * 100_000_000
        # Message (0x05): channel_id(u16) + sequence(u32) + log_time(u64) + publish_time(u64) + data
        msg_body = struct.pack("<HIQQ", 1, seq + 1, ts_ns, ts_ns) + raw
        buf.extend(_mcap_record(0x05, msg_body))
        written += 1

    # Footer (0x02): summary_start(u64) + summary_offset_start(u64) + summary_crc(u32)
    buf.extend(_mcap_record(0x02, struct.pack("<QQI", 0, 0, 0)))
    buf.extend(_MCAP_MAGIC)
    path.write_bytes(bytes(buf))
    return written
