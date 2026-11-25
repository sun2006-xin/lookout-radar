"""数据转发：把融合结果广播出去。

这是把本软件从"一个看板"变成"一台雷达服务器"的关键 ——
现在所有价值都锁在窗口里，别人想用只能截图。

协议刻意做得很朴素：**每帧一个 UDP 包，JSON 载荷，一行一帧**。
理由：
    · UDP 不怕丢几帧，雷达数据本来就是连续流，丢一帧无所谓
    · JSON 谁都能解析，Python / JS / Unity / C# 拿来就用
    · 不用定框架、不用握手，接收端写十行就能跑

载荷示例：
    {"type":"radarscope.frame","ts":1700000000.123,"seq":42,
     "range_max":8.0,"targets":[
        {"tid":1,"x":1.2,"y":3.4,"vx":0.1,"vy":0.2,"snr":0.8,"cls":"","views":2}]}

需要更紧的带宽时可以再开一个二进制模式，但先把能用的跑通。
"""
from __future__ import annotations

import json
import socket
import time

from .model import RadarFrame, Target


def is_public_target(host: str) -> bool:
    """判断转发目标是不是"公网地址"。

    环回 / 私网 / 链路本地 / 组播 / 受限广播都算安全目标；
    其余（含解析后的公网 IP）需要界面二次确认，避免误把目标数据发到外网。
    """
    import ipaddress
    import socket

    h = (host or "").strip()
    if not h:
        return False
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(h))
        except Exception:
            return True                     # 域名解析不了 → 按"不确定"从严处理
    return not (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified
                or str(ip) == "255.255.255.255")


class Forwarder:
    """UDP 广播器。不依赖 Qt，方便单测与嵌入到别的脚本里。"""

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = 9100
        self.enabled = False
        self.public_target = False
        self.rate = 10.0                 # 每秒最多发多少帧，0 表示不限
        self._sock: socket.socket | None = None
        self._last = 0.0
        self._seq = 0
        self.sent = 0
        self.errors = 0
        self.last_error = ""

    # ------------------------------------------------------------------ #
    def configure(self, host: str | None = None, port: int | None = None,
                  rate: float | None = None) -> None:
        if host is not None:
            self.host = host.strip() or "127.0.0.1"
            self.public_target = is_public_target(self.host)
        if port is not None:
            self.port = int(port)
        if rate is not None:
            self.rate = max(0.0, float(rate))
        # 地址变了要重建 socket：广播地址需要 SO_BROADCAST
        self._close_sock()

    def start(self) -> None:
        self.enabled = True
        self._open_sock()

    def stop(self) -> None:
        self.enabled = False
        self._close_sock()

    def _open_sock(self) -> None:
        if self._sock is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 允许发广播（255.255.255.255 或 x.x.x.255）
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        s.setblocking(False)
        self._sock = s

    def _close_sock(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    # ------------------------------------------------------------------ #
    def payload(self, frame: RadarFrame | None, tracks: list[Target]) -> dict:
        """组装一帧的 JSON。单独抽出来是为了能脱离网络做测试。"""
        caps_range = 0.0
        if frame is not None and frame.r_axis is not None and len(frame.r_axis):
            caps_range = float(frame.r_axis[-1])
        return {
            "type": "radarscope.frame",
            # 协议版本。字段只会加不会删；接收端应该忽略不认识的键。
            # 没有版本号的话，某天加个字段老接收端就会静默错位 —— 这种 bug 极难查。
            "v": 1,
            "ts": round(time.time(), 3),
            "seq": self._seq,
            "range_max": round(caps_range, 3),
            "targets": [
                {
                    "tid": int(t.tid),
                    "x": round(float(t.x), 3),
                    "y": round(float(t.y), 3),
                    "vx": round(float(t.vx), 3),
                    "vy": round(float(t.vy), 3),
                    "snr": round(float(t.snr), 3),
                    "cls": t.cls,
                    "views": int(t.views),
                    "threat": int(t.threat),
                }
                for t in tracks
            ],
        }

    def send(self, frame: RadarFrame | None, tracks: list[Target]) -> bool:
        """按速率限制发一帧。返回是否真的发出去了。"""
        if not self.enabled:
            return False
        now = time.monotonic()
        if self.rate > 0 and (now - self._last) < (1.0 / self.rate):
            return False
        self._last = now
        self._seq += 1
        self._open_sock()
        if self._sock is None:
            return False
        try:
            data = json.dumps(self.payload(frame, tracks),
                              ensure_ascii=False, separators=(",", ":")).encode()
            self._sock.sendto(data, (self.host, self.port))
            self.sent += 1
            return True
        except Exception as exc:          # 网络问题不该弄崩主程序
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def describe(self) -> str:
        if not self.enabled:
            return "转发：关"
        return f"转发：{self.host}:{self.port} · 已发 {self.sent}"


def sample_receiver(port: int = 9100) -> None:
    """接收端示例。运行它就能看到转发出来的数据，顺便验证配置对不对：

        python -c "from radarscope.core.forwarder import sample_receiver; sample_receiver()"
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", port))
    print(f"监听 UDP {port}，Ctrl+C 退出")
    while True:
        data, addr = s.recvfrom(65535)
        print(addr, data.decode(errors="replace")[:240])
