"""TI mmWave IWR6843 / AWR1843 / AWR2944 标准 UART TLV 二进制驱动（ROADMAP G1）。

协议结构（TI mmWave SDK Out-of-Box / People-Counting）：
  - Frame Header (40B): MagicWord(8B: 02 01 04 03 06 05 08 07) + 8 × uint32
      version, totalPacketLen, platform, frameNumber, timeCpuCycles,
      numDetectedObj, numTLVs, subFrameNumber
  - 紧随 numTLVs 个 TLV 块：Header(8B: type u32, length u32) + payload[length]
      TLV 1    : Detected Points (N × 16B: x, y, z, doppler float32)
      TLV 7    : Side Info (N × 4B: snr int16, noise int16, 单位 0.1 dB)
      TLV 1010 : 3D Target List (M × 112B 或 40B 简化结构体，提取 tid/x/y/z/vx/vy/vz)
"""
from __future__ import annotations

import math
import struct
import time

from ..core import pipeline
from ..core.codec import FrameSpec, StreamDecoder
from ..core.driver import RadarDriver, register
from ..core.model import Capabilities, RadarFrame, Target
from ..core.transport import endpoint_of, open_transport

TI_MAGIC = b"\x02\x01\x04\x03\x06\x05\x08\x07"
_HDR_FMT = "<8s8I"
_HDR_SIZE = struct.calcsize(_HDR_FMT)  # 40 bytes


def parse_ti_tlv_packet(pkt: bytes) -> tuple[list[Target], dict]:
    """解析一帧完整 TI mmWave UART TLV 报文，返回 (targets, meta)。"""
    if len(pkt) < _HDR_SIZE:
        return [], {}
    magic, version, total_len, platform, frame_no, _cycles, num_obj, num_tlvs, sub_frame = (
        struct.unpack_from(_HDR_FMT, pkt, 0)
    )
    if magic != TI_MAGIC or total_len > len(pkt):
        return [], {}

    pts: list[tuple[float, float, float, float]] = []
    snrs: list[float] = []
    tracked: list[Target] = []

    off = _HDR_SIZE
    for _ in range(min(num_tlvs, 32)):
        if off + 8 > total_len:
            break
        tlv_type, tlv_len = struct.unpack_from("<2I", pkt, off)
        off += 8
        if tlv_len < 0 or off + tlv_len > total_len:
            break
        payload = pkt[off: off + tlv_len]
        off += tlv_len

        if tlv_type == 1:
            count = len(payload) // 16
            for i in range(count):
                x, y, z, dop = struct.unpack_from("<4f", payload, i * 16)
                if all(math.isfinite(v) for v in (x, y, z, dop)) and (x * x + y * y) > 1e-4:
                    pts.append((float(x), float(y), float(z), float(dop)))
        elif tlv_type == 7:
            count = len(payload) // 4
            for i in range(count):
                snr_01db, _noise = struct.unpack_from("<2h", payload, i * 4)
                snrs.append(max(0.1, min(1.0, (snr_01db * 0.1) / 35.0)))
        elif tlv_type == 1010:
            # 标准 TI 3D Tracker Target List（至少前 28B 为 tid u32 + posX/Y/Z + velX/Y/Z）
            rec_sz = 112 if len(payload) % 112 == 0 and len(payload) >= 112 else 28
            count = len(payload) // rec_sz
            for i in range(count):
                tid, x, y, z, vx, vy, vz = struct.unpack_from("<I6f", payload, i * rec_sz)
                if all(math.isfinite(v) for v in (x, y, z, vx, vy, vz)):
                    r_xy = math.hypot(x, y)
                    el = math.degrees(math.atan2(z, max(1e-3, r_xy)))
                    tracked.append(Target(
                        x=float(x), y=float(y), vx=float(vx), vy=float(vy),
                        z=float(z), vz=float(vz), el_deg=el,
                        snr=0.88, tid=int(tid),
                    ))

    targets: list[Target] = list(tracked)
    if not targets and pts:
        for idx, (x, y, z, dop) in enumerate(pts):
            r_xy = math.hypot(x, y)
            ux, uy = (x / r_xy, y / r_xy) if r_xy > 1e-4 else (0.0, 1.0)
            el = math.degrees(math.atan2(z, max(1e-3, r_xy)))
            snr = snrs[idx] if idx < len(snrs) else 0.78
            targets.append(Target(
                x=x, y=y, vx=dop * ux, vy=dop * uy,
                z=z, el_deg=el, snr=snr,
            ))

    meta = {
        "ti_frame": int(frame_no),
        "ti_platform": hex(int(platform)),
        "ti_sdk": hex(int(version)),
        "ti_num_obj": int(num_obj),
        "subframe": int(sub_frame),
    }
    return targets, meta


class _TiPacketDecoder:
    """TI UART TLV 流切帧器（基于 magic + offset=12 totalPacketLen）。"""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.resync_count = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        if chunk:
            self._buf.extend(chunk)
        pkts: list[bytes] = []
        while True:
            idx = self._buf.find(TI_MAGIC)
            if idx < 0:
                keep = len(TI_MAGIC) - 1
                if len(self._buf) > keep:
                    self.resync_count += len(self._buf) - keep
                    del self._buf[:len(self._buf) - keep]
                break
            if idx > 0:
                self.resync_count += idx
                del self._buf[:idx]
            if len(self._buf) < _HDR_SIZE:
                break
            total_len = struct.unpack_from("<I", self._buf, 12)[0]
            if total_len < _HDR_SIZE or total_len > 65536:
                self.resync_count += 1
                del self._buf[0]
                continue
            if len(self._buf) < total_len:
                break
            pkts.append(bytes(self._buf[:total_len]))
            del self._buf[:total_len]
        return pkts


@register
class TIMmWaveDriver(RadarDriver):
    """TI IWR6843 / AWR1843 / AWR2944 毫米波雷达 UART TLV 真机驱动。"""

    id = "ti_mmwave"
    label = "TI IWR6843 / AWR1843 · UART TLV 毫米波"
    source = "device"
    form = "rd"
    vendor = "Texas Instruments"
    default_baud = 921600
    simulated = False

    def __init__(self) -> None:
        self._tr = None
        self._dec = _TiPacketDecoder()
        self._endpoint = ""
        self._good = 0
        self._bad = 0

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="TI mmWave UART TLV",
            has_scan=False,
            has_azimuth=True,
            has_targets=True,
            has_velocity=True,
            has_elevation=True,
            range_min=0.2,
            range_max=50.0,
            az_fov=120.0,
            az_resolution=4.0,
            max_targets=64,
            scan_mode="staring",
            scan_rate=20.0,
            tier="mmwave",
            transports=("serial", "tcp", "udp", "file"),
            views=("ppi", "ascope", "waterfall", "rhi", "tracks"),
            notes="支持 TI mmWave SDK TLV Type 1 点云 / Type 7 SNR / Type 1010 3D 跟踪列表（默认 921600 波特）",
        )

    def open(self, **kwargs) -> None:
        self._endpoint = endpoint_of(kwargs, self.default_baud)
        self._tr = open_transport(self._endpoint, default_baud=self.default_baud)
        self._dec = _TiPacketDecoder()
        self._good = 0
        self._bad = 0

    def read(self) -> RadarFrame | None:
        if self._tr is None:
            return None
        chunk = self._tr.read(4096)
        if not chunk:
            time.sleep(0.01)
            return None
        pkts = self._dec.feed(chunk)
        self._bad = self._dec.resync_count
        if not pkts:
            return None
        self._good += len(pkts)
        targets, meta = parse_ti_tlv_packet(pkts[-1])
        return RadarFrame(
            timestamp=time.monotonic(),
            targets=targets,
            source=self.label,
            meta={"good_frames": self._good, "bad_frames": self._bad, **meta},
        )

    def close(self) -> None:
        if self._tr is not None:
            self._tr.close()
            self._tr = None

    def describe(self) -> str:
        return f"{self.label} ({self._endpoint}) 好帧 {self._good} 错帧 {self._bad}"
