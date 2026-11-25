"""声明式串口帧解码。

海凌科全系、大多数模组的串口协议都是同一个套路：帧头 + 长度 + 数据 + 帧尾。
所以这里不做"每个雷达写一套 while 循环"，而是让驱动声明一个 FrameSpec，
由 StreamDecoder 统一处理粘包、半包、假同步。

踩过的坑都在这里解决：
  * 数据段内部可能出现和帧头相同的字节 -> 只在缓冲区头部匹配帧头
  * 一次 read() 可能拿到半帧或两帧半        -> 用长度字段凑够整帧再切
  * 校验失败时不能整段丢弃                -> 丢 1 字节后重新找帧头（重同步）
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FrameSpec:
    """帧格式描述。

    变长模式（length_size > 0）：
        长度字段紧跟在帧头之后；total = len(header) + length_size + 数据长度 + len(tail)
    定长模式（fixed_total > 0）：
        total = fixed_total

    注意长度字段的偏移量由 len(header) 推出来，不要手填。
    之前这里手写了个 length_prefix=6，结果偏移和总长用了两套语义，
    解析时从数据区读长度，整条链路静默出错 —— 测试就是为这种事准备的。
    """

    header: bytes
    tail: bytes = b""
    length_size: int = 2
    fixed_total: int = 0
    max_total: int = 8192

    @property
    def length_offset(self) -> int:
        return len(self.header)

    @property
    def prefix(self) -> int:
        """帧头 + 长度字段一共占多少字节，即数据区起始位置。"""
        return len(self.header) + self.length_size


class StreamDecoder:
    """字节流 -> 完整帧列表。线程内单实例使用，非线程安全。"""

    def __init__(self, spec: FrameSpec):
        self.spec = spec
        self._buf = bytearray()
        self.resync_count = 0     # 假同步/重同步次数，调试用

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[bytes]:
        if data:
            self._buf.extend(data)
        return list(self._split())

    def _total_of(self, buf: bytes) -> int:
        """算出整帧长度。

        返回  0：数据还不够，等着（不能当错误处理，否则逐字节喂会一直丢帧）
        返回 -1：长度字段非法，调用方应丢 1 字节重同步
        """
        spec = self.spec
        if spec.fixed_total > 0:
            return spec.fixed_total
        off = spec.length_offset
        if len(buf) < off + spec.length_size:
            return 0
        raw = buf[off: off + spec.length_size]
        length = int.from_bytes(raw, "little")
        total = spec.prefix + length + len(spec.tail)
        if total <= spec.prefix or total > spec.max_total:
            return -1
        return total

    def _split(self):
        spec = self.spec
        hlen = len(spec.header)
        while True:
            if len(self._buf) < hlen:
                return
            # 只在缓冲区头部找帧头，避免数据段内的假帧头
            if self._buf[:hlen] != spec.header:
                idx = self._buf.find(spec.header)
                if idx < 0:
                    # 保留末尾 hlen-1 字节，防止帧头被切断
                    keep = hlen - 1
                    if len(self._buf) > keep:
                        del self._buf[: len(self._buf) - keep]
                    return
                del self._buf[:idx]
                self.resync_count += 1
                continue

            total = self._total_of(bytes(self._buf[:64]))
            if total == 0:
                return                      # 半包，等下一批数据
            if total < 0:
                # 长度字段非法：丢 1 字节重同步，避免死等
                del self._buf[:1]
                self.resync_count += 1
                continue
            if len(self._buf) < total:
                return

            frame = bytes(self._buf[:total])
            if spec.tail and not frame.endswith(spec.tail):
                del self._buf[:1]
                self.resync_count += 1
                continue
            del self._buf[:total]
            yield frame


def build_command(header: bytes, payload: bytes, tail: bytes) -> bytes:
    """按 帧头 + 长度(LE) + 数据 + 帧尾 组一条下行命令。"""
    return header + len(payload).to_bytes(2, "little") + payload + tail
