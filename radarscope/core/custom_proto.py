"""通用零代码雷达协议适配器：用声明式 JSON 描述常见文本 / 二进制雷达帧并动态注册驱动。

目标：让用户无需改动 Python 源码，即可开箱连接：
  1. 航海 ARPA / NMEA-0183 目标报文（$RATTM / $--TTM）
  2. 车载 77G / 工业 ASCII 目标文本流（$TGT,x,y,vx,vy,snr）
  3. 常见 MCU 串口定帧二进制协议（自定义帧头/帧尾/字序/字段缩放）
"""
from __future__ import annotations

import json
import math
import struct
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import pipeline
from .codec import FrameSpec, StreamDecoder
from .driver import RadarDriver, register
from .model import Capabilities, RadarFrame, Target
from .transport import endpoint_of, open_transport

# NMEA 0183 距离/速度单位换算到米制（m / m/s）
_NM_TO_M = 1852.0
_KN_TO_MS = 1852.0 / 3600.0


@dataclass(slots=True)
class CustomProtocolSpec:
    id: str
    label: str
    mode: str = "csv_xy"            # nmea_ttm | csv_xy | csv_polar | json_lines | binary_fixed | binary_point | binary_polar
    form: str = "sector"            # point | sector | omni | tracking
    vendor: str = "通用协议"
    range_min: float = 0.2
    range_max: float = 50.0
    az_fov: float = 180.0
    baud_rate: int = 115200
    # 文本协议配置
    delimiter: str = ","
    prefix: str = ""
    col_a: int = 1                  # x 或 rng 或 tid
    col_b: int = 2                  # y 或 az_deg
    col_vx: int = 3                 # vx 或 -1
    col_vy: int = 4                 # vy 或 -1
    col_snr: int = 5                # snr 或 -1
    scale: float = 1.0              # 坐标/距离缩放系数（例如 mm -> 0.001）
    angle_scale: float = 1.0        # binary_polar 角度缩放（如 0.1 表示 0.1°）
    # 二进制协议配置
    header_hex: str = "AA 55"
    tail_hex: str = ""
    frame_len: int = 12             # 含帧头的总帧长（0=用帧头后2字节长度）
    endian: str = "<"               # < 小端 | > 大端
    payload_offset: int = 2         # 第一目标起始字节偏移
    struct_fmt: str = "hhh"         # 例：hh -> (x,y); hhh -> (x,y,snr); ffff -> (x,y,vx,vy)
    transports: tuple[str, ...] = ("serial", "tcp", "udp", "file")
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["transports"] = list(self.transports)
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> "CustomProtocolSpec":
        valid = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
        if "transports" in valid and isinstance(valid["transports"], list):
            valid["transports"] = tuple(str(t) for t in valid["transports"])
        spec = cls(**valid)
        spec.id = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in spec.id).strip("_") or "custom_proto"
        return spec


def builtin_presets() -> list[CustomProtocolSpec]:
    """开箱内置 7 套通用消费/工业/航海/车载雷达协议模板。"""
    return [
        CustomProtocolSpec(
            id="nmea_ttm",
            label="航海雷达 · NMEA TTM 目标流",
            mode="nmea_ttm",
            form="omni",
            vendor="NMEA-0183 / ARPA",
            range_min=5.0,
            range_max=2000.0,
            az_fov=360.0,
            prefix="$",
            notes="兼容标准 IEC 61162 / NMEA $RATTM 报文（支持海里/公里/法定英里自动转米制）",
        ),
        CustomProtocolSpec(
            id="traffic_csv",
            label="车载/交通 77G · ASCII 目标流 ($TGT)",
            mode="csv_xy",
            form="sector",
            vendor="通用 CSV",
            range_min=0.5,
            range_max=80.0,
            az_fov=120.0,
            delimiter=",",
            prefix="$TGT",
            col_a=1,
            col_b=2,
            col_vx=3,
            col_vy=4,
            col_snr=5,
            scale=1.0,
            notes="文本行格式：$TGT,x_m,y_m,vx_ms,vy_ms,snr（每行一个目标）",
        ),
        CustomProtocolSpec(
            id="polar_scan_ascii",
            label="二维激光 / 扇扫 · 极坐标 ASCII ($SCAN)",
            mode="csv_polar",
            form="omni",
            vendor="通用激光/机械扫描",
            range_min=0.1,
            range_max=25.0,
            az_fov=360.0,
            delimiter=",",
            prefix="$SCAN",
            col_a=1,
            col_b=2,
            col_vx=-1,
            col_vy=-1,
            col_snr=3,
            scale=1.0,
            notes="文本行格式：$SCAN,距离(m),方位角(deg),强度(0~1)",
        ),
        CustomProtocolSpec(
            id="uart_bin_cm",
            label="嵌入式串口 · 通用二进制帧 (AA55 · cm)",
            mode="binary_fixed",
            form="sector",
            vendor="通用 MCU",
            range_min=0.2,
            range_max=15.0,
            az_fov=120.0,
            header_hex="AA 55",
            tail_hex="0D 0A",
            frame_len=10,
            endian="<",
            payload_offset=2,
            struct_fmt="hhh",
            scale=0.01,
            notes="10字节定长帧：AA 55 + int16(x_cm) + int16(y_cm) + int16(snr*100) + 0D 0A",
        ),
        CustomProtocolSpec(
            id="tf_point_bin",
            label="北醒 TF 系列 / 激光单点 · 9B 串口帧 (59 59)",
            mode="binary_point",
            form="point",
            vendor="Benewake / 单点测距",
            range_min=0.1,
            range_max=20.0,
            az_fov=15.0,
            header_hex="59 59",
            tail_hex="",
            frame_len=9,
            endian="<",
            payload_offset=2,
            struct_fmt="HH",
            scale=0.01,
            notes="9字节定长帧：59 59 + uint16(dist_cm) + uint16(strength) + 温度/校验",
        ),
        CustomProtocolSpec(
            id="can_udp_77g",
            label="77G 毫米波网口 · 浮点四元组 UDP 帧 (AB CD)",
            mode="binary_fixed",
            form="sector",
            vendor="77G 车载/安防",
            range_min=0.5,
            range_max=120.0,
            az_fov=120.0,
            header_hex="AB CD",
            tail_hex="0D 0A",
            frame_len=20,
            endian="<",
            payload_offset=2,
            struct_fmt="ffff",
            scale=1.0,
            transports=("udp", "tcp", "file"),
            notes="20字节定长帧：AB CD + float32(x, y, vx, vy) + 0D 0A",
        ),
        CustomProtocolSpec(
            id="modbus_u16_dist",
            label="工业 Modbus-RTU 测距读数帧 (01 03 · mm)",
            mode="binary_point",
            form="point",
            vendor="工业测距传感器",
            range_min=0.05,
            range_max=50.0,
            az_fov=15.0,
            baud_rate=9600,
            header_hex="01 03",
            tail_hex="",
            frame_len=7,
            endian=">",
            payload_offset=3,
            struct_fmt="H",
            scale=0.001,
            notes="7字节大端帧：01 03 byte_cnt + uint16(dist_mm) + CRC16",
        ),
        CustomProtocolSpec(
            id="json_radar_stream",
            label="网络 / ESP32 JSON 行目标流 (TCP/UDP/串口)",
            mode="json_lines",
            form="sector",
            vendor="通用 JSON 传感器",
            range_min=0.2,
            range_max=80.0,
            az_fov=180.0,
            baud_rate=115200,
            transports=("tcp", "udp", "serial", "file"),
            notes="支持 {\"x\":1.2,\"y\":3.4}、{\"rng\":5.0,\"az\":30} 或 {\"targets\":[...]}",
        ),
        CustomProtocolSpec(
            id="polar_bin_radar",
            label="毫米波/ToF · 二进制极坐标帧 (AA 56 · cm/0.1°)",
            mode="binary_polar",
            form="sector",
            vendor="通用极坐标二进制",
            range_min=0.2,
            range_max=40.0,
            az_fov=120.0,
            baud_rate=115200,
            header_hex="AA 56",
            tail_hex="0D 0A",
            frame_len=10,
            endian="<",
            payload_offset=2,
            struct_fmt="HhH",
            scale=0.01,
            angle_scale=0.1,
            notes="10字节帧：AA 56 + uint16(dist_cm) + int16(az_0.1deg) + uint16(snr) + 0D 0A",
        ),
    ]


def nmea_checksum_ok(line: str) -> bool:
    s = line.strip()
    if "*" not in s:
        return True
    body, _, cs = s.partition("*")
    body = body.lstrip("$!")
    want = 0
    for ch in body:
        want ^= ord(ch)
    try:
        return int(cs[:2], 16) == want
    except ValueError:
        return False


def parse_nmea_ttm(line: str) -> Target | None:
    """解析标准 NMEA 0183 TTM（Tracked Target Message）报文。"""
    s = line.strip()
    if not s or "TTM," not in s or not nmea_checksum_ok(s):
        return None
    if "*" in s:
        s = s.split("*", 1)[0]
    fields = s.split(",")
    if len(fields) < 7:
        return None
    try:
        tid = int(fields[1] or "0")
        dist = float(fields[2])
        brg_deg = float(fields[3])
    except ValueError:
        return None
    spd = 0.0
    crs_deg = 0.0
    try:
        if fields[5]:
            spd = float(fields[5])
        if fields[6]:
            crs_deg = float(fields[6])
    except ValueError:
        pass
    unit = (fields[10] if len(fields) > 10 else "N").upper()
    if unit == "N":
        dist_m = dist * _NM_TO_M
        spd_ms = spd * _KN_TO_MS
    elif unit == "K":
        dist_m = dist * 1000.0
        spd_ms = spd / 3.6
    elif unit == "S":
        dist_m = dist * 1609.344
        spd_ms = spd * 0.44704
    else:
        dist_m = dist
        spd_ms = spd

    brg = math.radians(brg_deg)
    crs = math.radians(crs_deg)
    x = dist_m * math.sin(brg)
    y = dist_m * math.cos(brg)
    vx = spd_ms * math.sin(crs)
    vy = spd_ms * math.cos(crs)
    return Target(x=x, y=y, vx=vx, vy=vy, snr=0.85, tid=tid)


def parse_hex_bytes(text: str) -> bytes:
    cleaned = "".join(ch for ch in (text or "").replace("0x", "").replace("0X", "") if not ch.isspace())
    if not cleaned:
        return b""
    if len(cleaned) % 2 != 0:
        raise ValueError("十六进制长度必须为偶数")
    return bytes.fromhex(cleaned)


def _finite_target(
    x: float, y: float, vx: float = 0.0, vy: float = 0.0,
    snr: float = 0.85, tid: int = -1, z: float = 0.0, cls: str = "",
) -> Target | None:
    if not all(math.isfinite(v) for v in (x, y, vx, vy, snr, z)):
        return None
    return Target(
        x=float(x), y=float(y), vx=float(vx), vy=float(vy),
        snr=max(0.1, min(1.0, float(snr))), tid=int(tid), z=float(z), cls=str(cls),
    )


def _target_from_mapping(item: dict, scale: float = 1.0, angle_scale: float = 1.0) -> Target | None:
    if not isinstance(item, dict):
        return None
    try:
        if "x" in item and "y" in item:
            x = float(item["x"]) * scale
            y = float(item["y"]) * scale
        elif any(k in item for k in ("rng", "range", "dist", "r")):
            rk = next(k for k in ("rng", "range", "dist", "r") if k in item)
            ak = next((k for k in ("az", "az_deg", "angle", "bearing", "theta") if k in item), None)
            rng = float(item[rk]) * scale
            az_deg = float(item[ak]) * angle_scale if ak else 0.0
            az = math.radians(az_deg)
            x, y = rng * math.sin(az), rng * math.cos(az)
        else:
            return None
        vx = float(item.get("vx", 0.0) or 0.0)
        vy = float(item.get("vy", 0.0) or 0.0)
        raw_s = float(item.get("snr", item.get("strength", 0.85)) or 0.85)
        snr = raw_s / 100.0 if raw_s > 1.5 else raw_s
        tid = int(item.get("tid", item.get("id", -1)) or -1)
        z = float(item.get("z", 0.0) or 0.0)
        cls = str(item.get("cls", item.get("type", "")) or "")
    except (TypeError, ValueError):
        return None
    return _finite_target(x, y, vx, vy, snr, tid=tid, z=z, cls=cls)


def parse_json_line(spec: CustomProtocolSpec, line: str) -> list[Target]:
    """解析单行 JSON 雷达目标或 {"targets":[...]} 目标列表。"""
    s = line.strip()
    if not s or not (s.startswith("{") or s.startswith("[")):
        return []
    try:
        data = json.loads(s)
    except ValueError:
        return []
    items = data.get("targets", []) if isinstance(data, dict) and "targets" in data else (data if isinstance(data, list) else [data])
    out: list[Target] = []
    for item in items:
        t = _target_from_mapping(item, scale=spec.scale, angle_scale=spec.angle_scale)
        if t is not None:
            out.append(t)
    return out


def parse_custom_line(spec: CustomProtocolSpec, line: str) -> list[Target]:
    """按文本协议规则解析一行报文为目标列表。"""
    s = line.strip()
    if not s:
        return []
    if spec.mode == "nmea_ttm":
        t = parse_nmea_ttm(s)
        return [t] if t is not None else []
    if spec.mode == "json_lines":
        return parse_json_line(spec, s)
    if spec.prefix and not s.startswith(spec.prefix):
        return []
    parts = [p.strip() for p in s.split(spec.delimiter or ",")]
    need_idx = max(spec.col_a, spec.col_b)
    if len(parts) <= need_idx:
        return []
    try:
        va = float(parts[spec.col_a]) * spec.scale
        vb = float(parts[spec.col_b])
        if spec.mode == "csv_polar":
            # col_a = 距离（乘 scale），col_b = 方位角（度，正北为0顺时针）
            az = math.radians(vb * spec.angle_scale)
            x, y = va * math.sin(az), va * math.cos(az)
        else:
            x, y = va, vb * spec.scale
        vx = float(parts[spec.col_vx]) if 0 <= spec.col_vx < len(parts) and parts[spec.col_vx] else 0.0
        vy = float(parts[spec.col_vy]) if 0 <= spec.col_vy < len(parts) and parts[spec.col_vy] else 0.0
        raw_s = float(parts[spec.col_snr]) if 0 <= spec.col_snr < len(parts) and parts[spec.col_snr] else 0.85
        snr = raw_s / 100.0 if raw_s > 1.5 else raw_s
    except ValueError:
        return []
    t = _finite_target(x, y, vx, vy, snr)
    return [t] if t is not None else []


def parse_custom_binary(spec: CustomProtocolSpec, pkt: bytes) -> list[Target]:
    """按声明式结构体格式解析单帧二进制报文。"""
    fmt = f"{spec.endian}{spec.struct_fmt}"
    sz = struct.calcsize(fmt)
    if len(pkt) < spec.payload_offset + sz:
        return []
    vals = struct.unpack_from(fmt, pkt, spec.payload_offset)
    if not vals:
        return []
    if spec.mode == "binary_point":
        dist = float(vals[0]) * spec.scale
        if dist <= 0.0:
            return []
        snr = 0.85
        if len(vals) >= 2:
            raw_s = float(vals[1])
            snr = min(1.0, raw_s / 1000.0) if raw_s > 100.0 else (raw_s / 100.0 if raw_s > 1.5 else raw_s)
        t = _finite_target(0.0, dist, snr=snr)
        return [t] if t is not None else []
    if len(vals) < 2:
        return []
    if spec.mode == "binary_polar":
        dist = float(vals[0]) * spec.scale
        if dist <= 0.0:
            return []
        az_deg = float(vals[1]) * spec.angle_scale
        az = math.radians(az_deg)
        x, y = dist * math.sin(az), dist * math.cos(az)
        vx, vy, snr = 0.0, 0.0, 0.85
        if len(vals) >= 3:
            raw_s = float(vals[2])
            snr = min(1.0, raw_s / 1000.0) if raw_s > 100.0 else (raw_s / 100.0 if raw_s > 1.5 else raw_s)
        t = _finite_target(x, y, vx, vy, snr)
        return [t] if t is not None else []
    x = float(vals[0]) * spec.scale
    y = float(vals[1]) * spec.scale
    vx, vy, snr = 0.0, 0.0, 0.85
    if len(vals) == 3:
        raw_s = float(vals[2])
        snr = raw_s / 100.0 if raw_s > 1.5 else raw_s
    elif len(vals) >= 4:
        vx = float(vals[2]) * spec.scale
        vy = float(vals[3]) * spec.scale
        if len(vals) >= 5:
            raw_s = float(vals[4])
            snr = raw_s / 100.0 if raw_s > 1.5 else raw_s
    t = _finite_target(x, y, vx, vy, snr)
    return [t] if t is not None else []


def _make_decoder(spec: CustomProtocolSpec) -> StreamDecoder:
    hdr = parse_hex_bytes(spec.header_hex)
    tail = parse_hex_bytes(spec.tail_hex)
    ft = max(0, int(spec.frame_len))
    return StreamDecoder(FrameSpec(
        header=hdr,
        tail=tail,
        length_size=0 if ft > 0 else 2,
        fixed_total=ft,
    ))


def preview_sample(spec: CustomProtocolSpec, sample_text: str) -> list[Target]:
    """UI 向导调测用：把一段文本行或十六进制字节样本送入规则解析。"""
    if spec.mode in ("binary_fixed", "binary_point", "binary_polar"):
        raw = parse_hex_bytes(sample_text)
        dec = _make_decoder(spec)
        out: list[Target] = []
        for pkt in dec.feed(raw):
            out.extend(parse_custom_binary(spec, pkt))
        return out
    out = []
    for line in sample_text.splitlines():
        out.extend(parse_custom_line(spec, line))
    return out


def load_custom_specs(path: Path) -> list[CustomProtocolSpec]:
    """读取用户保存的自定义协议；始终保留内置预设，用户同 ID 覆盖预设。"""
    by_id: dict[str, CustomProtocolSpec] = {s.id: s for s in builtin_presets()}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw if isinstance(raw, list) else []:
                if isinstance(item, dict) and item.get("id"):
                    spec = CustomProtocolSpec.from_dict(item)
                    by_id[spec.id] = spec
        except Exception:
            pass
    return list(by_id.values())


def save_custom_specs(path: Path, specs: list[CustomProtocolSpec]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([s.to_dict() for s in specs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_driver_class(spec: CustomProtocolSpec) -> type[RadarDriver]:
    """为声明式协议构造并注册一个标准 RadarDriver 子类。"""

    class _CustomProtoDriver(RadarDriver):
        id = spec.id
        label = spec.label
        simulated = False
        source = "device"
        form = spec.form
        vendor = spec.vendor
        default_baud = int(spec.baud_rate or 115200)

        def __init__(self) -> None:
            self._spec = spec
            self._tr = None
            self._dec: StreamDecoder | None = None
            self._txt_buf = ""
            self._endpoint = ""
            self._good = 0
            self._bad = 0

        def capabilities(self) -> Capabilities:
            has_v = spec.mode in ("nmea_ttm", "json_lines") or spec.col_vx >= 0 or len(spec.struct_fmt) >= 4
            return Capabilities(
                name=spec.label,
                form=spec.form,
                source="device",
                vendor=spec.vendor,
                has_scan=True,
                has_azimuth=(spec.form != "point"),
                has_targets=True,
                has_velocity=has_v,
                az_fov=spec.az_fov,
                range_min=spec.range_min,
                range_max=spec.range_max,
                max_targets=32,
                scan_mode="rotating" if spec.form == "omni" else "staring",
                scan_rate=10.0,
                transports=spec.transports,
                views=("ppi", "ascope", "waterfall", "tracks"),
                notes=spec.notes or f"零代码通用协议适配器（{spec.mode}）",
            )

        def open(self, **kwargs) -> None:
            baud = int(self._spec.baud_rate or 115200)
            self._endpoint = endpoint_of(kwargs, baud)
            self._tr = open_transport(self._endpoint, default_baud=baud)
            self._good = 0
            self._bad = 0
            self._txt_buf = ""
            if self._spec.mode in ("binary_fixed", "binary_point", "binary_polar"):
                self._dec = _make_decoder(self._spec)
            else:
                self._dec = None

        def read(self) -> RadarFrame | None:
            if self._tr is None:
                return None
            tgts: list[Target] = []
            if self._spec.mode in ("binary_fixed", "binary_point", "binary_polar") and self._dec is not None:
                chunk = self._tr.read(4096)
                if not chunk:
                    time.sleep(0.01)
                    return None
                pkts = self._dec.feed(chunk)
                self._good += len(pkts)
                self._bad = self._dec.rejected
                for p in pkts:
                    tgts.extend(parse_custom_binary(self._spec, p))
                if not pkts:
                    return None
            else:
                # 批量读取缓冲并按行切分，防止多目标 NMEA/CSV/JSON 突发报文一次只画一个目标
                chunk = self._tr.read(4096)
                if chunk:
                    self._txt_buf += chunk.decode("utf-8", errors="ignore")
                if len(self._txt_buf) > 65536:
                    self._txt_buf = self._txt_buf[-32768:]
                if "\n" not in self._txt_buf:
                    raw_line = self._tr.readline()
                    if raw_line:
                        self._txt_buf += raw_line.decode("utf-8", errors="ignore")
                if "\n" not in self._txt_buf:
                    time.sleep(0.01)
                    return None
                lines = self._txt_buf.split("\n")
                self._txt_buf = lines.pop()
                for ln in lines[:64]:
                    if not ln.strip():
                        continue
                    parsed = parse_custom_line(self._spec, ln)
                    if parsed:
                        self._good += 1
                        tgts.extend(parsed)
                    else:
                        self._bad += 1
                if not tgts:
                    return None

            caps = self.capabilities()
            f = pipeline.synthesize_scan(caps, tgts)
            f.timestamp = time.monotonic()
            f.source = self._endpoint
            f.meta = {
                "good": self._good,
                "bad": self._bad,
                "mode": caps.scan_mode,
                "scan_period": 1.0 / caps.scan_rate,
            }
            return f

        def close(self) -> None:
            if self._tr is not None:
                self._tr.close()
                self._tr = None

        def describe(self) -> str:
            return f"{self.label} | {self._endpoint}"

    _CustomProtoDriver.__name__ = f"CustomDriver_{spec.id}"
    return register(_CustomProtoDriver)


def register_specs(specs: list[CustomProtocolSpec]) -> list[str]:
    return [build_driver_class(s).id for s in specs]
