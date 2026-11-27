"""EUROCONTROL ASTERIX CAT 048 & CAT 240 基础二进制解码驱动（ROADMAP G1）。

零第三方依赖实现标准大端 Data Block (CAT u8 + LEN u16) + 变长 FSPEC 解析：
  - CAT 048：单部空管监视雷达目标报告（SAC/SIC、Time-of-Day、极坐标 rho/theta、Flight Level）
  - CAT 240：原始雷达视频扇区数据（Video Header + 8-bit 距离格回波阵列 -> 直接输出 RadarFrame.scan）
"""
from __future__ import annotations

import math
import struct
import time

import numpy as np

from ..core.driver import RadarDriver, register
from ..core.model import Capabilities, RadarFrame, Target
from ..core.transport import endpoint_of, open_transport

_NM_TO_M = 1852.0
_FL_TO_M = 30.48  # 1 FL = 100 ft = 30.48 m


def _read_fspec(buf: bytes, pos: int) -> tuple[list[int], int]:
    """解析 ASTERIX 变长 FSPEC 位图（每字节 bit7..bit1 为 FRN，bit0=FX 续延位），返回激活的 1-based FRN 列表。"""
    frns: list[int] = []
    frn = 1
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        for bit in range(7, 0, -1):
            if b & (1 << bit):
                frns.append(frn)
            frn += 1
        if not (b & 0x01):
            break
    return frns, pos


def _skip_fx_field(buf: bytes, pos: int) -> int:
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        if not (b & 0x01):
            break
    return pos


def parse_asterix_cat048(record: bytes, pos: int = 0) -> tuple[Target | None, int]:
    """解析一条 CAT 048 记录，提取标准极坐标定位与高度层。"""
    frns, pos = _read_fspec(record, pos)
    rng_m: float | None = None
    az_rad: float | None = None
    z_m = 0.0
    mode3a = ""

    for frn in frns:
        if pos >= len(record):
            break
        if frn == 1:      # I048/010 SAC/SIC (2B)
            pos += 2
        elif frn == 2:    # I048/140 Time of Day (3B, 1/128 s)
            pos += 3
        elif frn == 3:    # I048/020 Target Report Descriptor (1+FX B)
            pos = _skip_fx_field(record, pos)
        elif frn == 4:    # I048/040 Measured Position in Polar Coordinates (4B: rho u16 1/256 NM, theta u16 360/65536 deg)
            if pos + 4 > len(record):
                break
            rho_raw, theta_raw = struct.unpack_from(">HH", record, pos)
            pos += 4
            rng_m = (rho_raw / 256.0) * _NM_TO_M
            az_rad = math.radians((theta_raw * 360.0) / 65536.0)
        elif frn == 5:    # I048/070 Mode-3/A Code (2B)
            if pos + 2 > len(record):
                break
            code = struct.unpack_from(">H", record, pos)[0] & 0x0FFF
            mode3a = f"SQ{code:04o}"
            pos += 2
        elif frn == 6:    # I048/090 Flight Level (2B signed, LSB = 1/4 FL = 25 ft)
            if pos + 2 > len(record):
                break
            fl_raw = struct.unpack_from(">h", record, pos)[0]
            # 去掉最高两位 V/G 状态位，保留 14-bit 有符号补码
            fl_signed = fl_raw & 0x3FFF
            if fl_signed & 0x2000:
                fl_signed -= 0x4000
            z_m = max(0.0, (fl_signed / 4.0) * _FL_TO_M)
            pos += 2
        elif frn == 7:    # I048/130 Compound Radar Plot Characteristics
            if pos < len(record):
                sub = record[pos]
                pos += 1
                for bit in range(7, 0, -1):
                    if sub & (1 << bit):
                        pos += 1
        elif frn == 8:    # I048/220 Aircraft Address (3B)
            pos += 3
        elif frn == 9:    # I048/240 Aircraft Identification (6B)
            pos += 6
        elif frn == 10:   # I048/250 Mode S MB Data (1 + 8*REP B)
            if pos < len(record):
                rep = record[pos]
                pos += 1 + 8 * rep
        elif frn == 11:   # I048/161 Track Number (2B)
            pos += 2
        elif frn == 12:   # I048/042 Cartesian (4B)
            pos += 4
        elif frn == 13:   # I048/200 Calculated Track Velocity (4B)
            pos += 4
        else:
            break

    if rng_m is None or az_rad is None:
        return None, pos
    x = rng_m * math.sin(az_rad)
    y = rng_m * math.cos(az_rad)
    el = math.degrees(math.atan2(z_m, max(1.0, rng_m)))
    return Target(x=x, y=y, z=z_m, el_deg=el, snr=0.90, cls=mode3a or "CAT048"), pos


def parse_asterix_cat240(record: bytes, pos: int = 0) -> tuple[tuple[float, float, np.ndarray] | None, int]:
    """解析简化标准 CAT 240 雷达视频记录，返回 ((start_az_deg, end_az_deg, cells_f32), next_pos)。"""
    frns, pos = _read_fspec(record, pos)
    start_az_deg = 0.0
    end_az_deg = 1.0
    cells: np.ndarray | None = None

    for frn in frns:
        if pos >= len(record):
            break
        if frn == 1:      # I240/010 SAC/SIC (2B)
            pos += 2
        elif frn == 2:    # I240/000 Msg Type (1B)
            pos += 1
        elif frn == 3:    # I240/020 Video Record Header (4B)
            pos += 4
        elif frn == 4:    # I240/030 Video Summary (1B len + str)
            if pos < len(record):
                pos += 1 + record[pos]
        elif frn == 5:    # I240/040 Video Header Nano (12B: start_az u16, end_az u16, start_rg u32, cell_dur u32)
            if pos + 12 > len(record):
                break
            sa, ea, _sr, _dur = struct.unpack_from(">HHII", record, pos)
            start_az_deg = (sa * 360.0) / 65536.0
            end_az_deg = (ea * 360.0) / 65536.0
            pos += 12
        elif frn == 6:    # I240/048 Video Resolution (2B)
            pos += 2
        elif frn == 7:    # I240/050 Video Blocks Low Data Volume (REP u8 + N bytes 或 直接 len u16)
            if pos + 2 > len(record):
                break
            n_cells = struct.unpack_from(">H", record, pos)[0]
            pos += 2
            if pos + n_cells > len(record):
                break
            raw = np.frombuffer(record, dtype=np.uint8, count=n_cells, offset=pos)
            cells = (raw.astype(np.float32) / 255.0)
            pos += n_cells
        else:
            break

    if cells is None or cells.size == 0:
        return None, pos
    return (start_az_deg, end_az_deg, cells), pos


def parse_asterix_block(block: bytes) -> tuple[int, list[Target], list[tuple[float, float, np.ndarray]]]:
    """解析一个完整的 ASTERIX Data Block (CAT u8 + LEN u16 BE + records)。"""
    if len(block) < 4:
        return 0, [], []
    cat, blk_len = struct.unpack_from(">BH", block, 0)
    end = min(len(block), blk_len)
    pos = 3
    targets: list[Target] = []
    videos: list[tuple[float, float, np.ndarray]] = []
    while pos < end:
        prev = pos
        if cat == 48:
            t, pos = parse_asterix_cat048(block[:end], pos)
            if t is not None:
                targets.append(t)
        elif cat == 240:
            v, pos = parse_asterix_cat240(block[:end], pos)
            if v is not None:
                videos.append(v)
        else:
            break
        if pos <= prev:
            break
    return cat, targets, videos


@register
class AsterixDriver(RadarDriver):
    """EUROCONTROL ASTERIX CAT 048（点迹航迹）与 CAT 240（雷达原始视频）解码驱动。"""

    id = "asterix"
    label = "ASTERIX CAT048 / CAT240 · 行业监视协议"
    source = "device"
    form = "omni"
    vendor = "EUROCONTROL"
    default_baud = 115200
    simulated = False

    def __init__(self) -> None:
        self._tr = None
        self._endpoint = ""
        self._buf = bytearray()
        self._range_max = 120_000.0
        self._az = np.radians(np.linspace(-180.0, 180.0, 180, endpoint=False)).astype(np.float32)
        self._r: np.ndarray | None = None
        self._scan: np.ndarray | None = None
        self._good = 0
        self._bad = 0

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="ASTERIX CAT048 / CAT240",
            has_scan=True,
            has_azimuth=True,
            has_targets=True,
            has_elevation=True,
            range_min=100.0,
            range_max=self._range_max,
            az_fov=360.0,
            az_resolution=2.0,
            max_targets=64,
            scan_mode="rotating",
            scan_rate=0.5,
            tier="airborne",
            transports=("udp", "tcp", "file"),
            views=("ppi", "ascope", "waterfall", "rhi", "tracks"),
            notes="支持 EUROCONTROL ASTERIX CAT 048 目标报告与 CAT 240 原始视频块（udp://0.0.0.0:8600）",
        )

    def open(self, **kwargs) -> None:
        self._endpoint = endpoint_of(kwargs, self.default_baud) or "udp://0.0.0.0:8600"
        self._tr = open_transport(self._endpoint, default_baud=self.default_baud)
        self._buf.clear()
        self._scan = None
        self._r = None
        self._good = 0
        self._bad = 0

    def read(self) -> RadarFrame | None:
        if self._tr is None:
            return None
        chunk = self._tr.read(8192)
        if not chunk:
            time.sleep(0.01)
            return None
        self._buf.extend(chunk)
        if len(self._buf) < 3:
            return None

        all_targets: list[Target] = []
        last_cat = 0
        fresh_az: tuple[int, int] | None = None
        while len(self._buf) >= 3:
            cat, blk_len = struct.unpack_from(">BH", self._buf, 0)
            if blk_len < 4 or blk_len > 65535:
                self._bad += 1
                del self._buf[0]
                continue
            if len(self._buf) < blk_len:
                break
            blk = bytes(self._buf[:blk_len])
            del self._buf[:blk_len]
            last_cat, tgts, vids = parse_asterix_block(blk)
            self._good += 1
            all_targets.extend(tgts)
            for sa_deg, ea_deg, cells in vids:
                if self._scan is None or self._scan.shape[1] != cells.size:
                    self._r = np.linspace(100.0, self._range_max, cells.size, dtype=np.float32)
                    self._scan = np.zeros((self._az.size, cells.size), dtype=np.float32)
                az_rad = math.radians(((sa_deg + 180.0) % 360.0) - 180.0)
                idx = int(np.argmin(np.abs(self._az - az_rad)))
                self._scan[idx, :] = cells
                fresh_az = (idx, (idx + 1) % self._az.size)

        if not all_targets and self._scan is None:
            return None
        meta = {"good_frames": self._good, "bad_frames": self._bad, "asterix_cat": last_cat}
        if fresh_az is not None:
            meta["fresh_az"] = fresh_az
        return RadarFrame(
            timestamp=time.monotonic(),
            scan=self._scan.copy() if self._scan is not None else None,
            az_axis=self._az if self._scan is not None else None,
            r_axis=self._r if self._scan is not None else None,
            targets=all_targets,
            source=self.label,
            meta=meta,
        )

    def close(self) -> None:
        if self._tr is not None:
            self._tr.close()
            self._tr = None

    def describe(self) -> str:
        return f"{self.label} ({self._endpoint}) 好包 {self._good} 错包 {self._bad}"
