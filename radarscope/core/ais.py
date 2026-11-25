"""AIS 报文解析（NMEA 0183 / AIVDM）。

AIS 是船舶自动识别系统，广播本船/他船的 MMSI、位置、航向航速。
船用雷达把 AIS 目标叠在雷达画面上，用来区分"有雷达回波也有身份"的船
和"只有一个亮点、不知道是什么"的目标。

我们只解两类最常用的：
    1 / 2 / 3   位置报告（Class A，商船）
    18 / 19     位置报告（Class B，小船/渔船）

报文格式（一条 NMEA 语句）：
    !AIVDM,1,1,,A,15Muq?0P00PD;88MD5MTDww@2D0S,0*6B
      |  | | | | |                              |
      |  | | | | |                              └ 校验和
      |  | | | | └ 填充位数（0-5）
      |  | | | └ 信道 A/B
      |  | | └ 序号
      |  | └ 分片总数
      |  └ 当前分片号
      └ 载荷（6bit ASCII 装甲编码）

**关键细节**：载荷不是普通 base64，是"每字符减 48，若 >40 再减 8"的 6bit 编码；
字段按 bit 偏移切，不是按字节。经纬度是有符号数，单位 1/10000 分。
"""
from __future__ import annotations

from dataclasses import dataclass
from math import floor

#: 6bit 装甲编码的合法字符范围
_VALID = {chr(c) for c in range(48, 120)}


@dataclass
class AisTarget:
    mmsi: int
    lat: float
    lon: float
    sog: float = 0.0        # 对地航速，节
    cog: float = 0.0        # 对地航向，度
    heading: int = 511      # 真航向，511 = 不可用
    name: str = ""
    kind: int = 0           # 报文类型

    def local_xy(self, ref_lat: float, ref_lon: float) -> tuple[float, float]:
        """经纬度 → 以参考点为原点的本地米制坐标（等距圆柱近似）。

        在几公里范围内这个近似的误差远小于 AIS 本身的定位误差，
        没必要上 UTM 投影。
        """
        lat_m = (self.lat - ref_lat) * 111_320.0
        lon_m = (self.lon - ref_lon) * 111_320.0 * _cos_deg(ref_lat)
        return lon_m, lat_m          # x 向东，y 向北 → 与雷达坐标一致


def _cos_deg(deg: float) -> float:
    import math
    return math.cos(math.radians(deg))


def checksum_ok(sentence: str) -> bool:
    """校验 `!AIVDM,...*HH` 的异或校验和。"""
    s = sentence.strip()
    if "*" not in s:
        return False
    body, _, cs = s.rpartition("*")
    body = body[1:] if body.startswith(("!", "$")) else body
    x = 0
    for ch in body:
        x ^= ord(ch)
    try:
        return int(cs[:2], 16) == x
    except ValueError:
        return False


def payload_to_bits(payload: str, fill: int = 0) -> str:
    """6bit 装甲 → 二进制串。fill 是末尾要丢弃的填充位数。"""
    bits = []
    for ch in payload:
        v = ord(ch) - 48
        if v > 40:
            v -= 8
        if not (0 <= v < 64):
            raise ValueError(f"非法字符 {ch!r}")
        bits.append(f"{v:06b}")
    out = "".join(bits)
    return out[: len(out) - fill] if fill else out


def _u(bits: str, start: int, length: int) -> int:
    return int(bits[start:start + length], 2)


def _i(bits: str, start: int, length: int) -> int:
    """有符号（补码）。AIS 的经纬度就是这么存的。"""
    v = _u(bits, start, length)
    if v & (1 << (length - 1)):
        v -= 1 << length
    return v


def parse_aivdm(sentence: str) -> AisTarget | None:
    """解析一条 AIVDM 语句。非位置报告、校验失败、分片包头都返回 None。"""
    s = sentence.strip()
    if not s.startswith(("!AIVDM", "!AIVDO")):
        return None
    if not checksum_ok(s):
        return None
    body = s[1:].split("*")[0]
    parts = body.split(",")
    if len(parts) < 7:
        return None
    try:
        frag_cnt = int(parts[1])
        frag_no = int(parts[2])
        payload = parts[5]
        fill = int(parts[6] or 0)
    except (ValueError, IndexError):
        return None
    if frag_cnt != 1 or frag_no != 1:
        return None                     # 多分片报文暂不支持（位置报告都在单分片里）
    try:
        bits = payload_to_bits(payload, fill)
    except ValueError:
        return None
    if len(bits) < 38:
        return None

    kind = _u(bits, 0, 6)
    mmsi = _u(bits, 8, 30)
    t = AisTarget(mmsi=mmsi, lat=0.0, lon=0.0, kind=kind)

    if kind in (1, 2, 3):
        if len(bits) < 168:
            return None
        sog_raw = _u(bits, 50, 10)
        lon_raw = _i(bits, 61, 28)
        lat_raw = _i(bits, 89, 27)
        cog_raw = _u(bits, 116, 12)
        hdg = _u(bits, 128, 9)
        t.sog = sog_raw / 10.0 if sog_raw != 1023 else 0.0
        t.cog = cog_raw / 10.0 if cog_raw != 3600 else 0.0
        t.heading = hdg
        t.lon = lon_raw / 600000.0
        t.lat = lat_raw / 600000.0
    elif kind in (18, 19):
        if len(bits) < 133:
            return None
        sog_raw = _u(bits, 46, 10)
        lon_raw = _i(bits, 57, 28)
        lat_raw = _i(bits, 85, 27)
        cog_raw = _u(bits, 112, 12)
        t.sog = sog_raw / 10.0 if sog_raw != 1023 else 0.0
        t.cog = cog_raw / 10.0 if cog_raw != 3600 else 0.0
        t.lon = lon_raw / 600000.0
        t.lat = lat_raw / 600000.0
    else:
        return None

    # 未定位时 AIS 会发 lon=181 / lat=91，这种要丢掉
    if abs(t.lat) > 90 or abs(t.lon) > 180:
        return None
    return t


def make_aivdm(payload_bits: str, fill: int = 0, chan: str = "A") -> str:
    """把 bit 串反向打成一条 AIVDM（自测用，也方便造测试数据）。"""
    bits = payload_bits + "0" * fill
    chars = []
    for i in range(0, len(bits), 6):
        v = int(bits[i:i + 6].ljust(6, "0"), 2)
        chars.append(chr(v + 48 + (8 if v + 48 > 87 else 0)))
    payload = "".join(chars)
    body = f"AIVDM,1,1,,{chan},{payload},{fill}"
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"!{body}*{x:02X}"
