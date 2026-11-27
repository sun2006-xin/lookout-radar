"""AIS 数据源：监听 UDP 上的 NMEA 报文，维护一份目标表。

AIS 接收机（或船舶网关）通常把 NMEA 0183 语句按 UDP 广播出来，端口常见 10110。
这里起一个后台线程收包、逐行解析、按 MMSI 覆盖更新，界面每帧取一份快照。

设计上刻意不做"完整 AIS"：只解位置报告（1/2/3/18/19），
静默一段时间（默认 6 分钟）就丢弃。船用雷达也是这样，太久没更新的目标不该继续画。
"""
from __future__ import annotations

import socket
import threading
import time

from .ais import AisDecoder, AisTarget


class AisFeed:
    def __init__(self) -> None:
        self.port = 10110
        self.running = False
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._decoder = AisDecoder()
        self._targets: dict[int, tuple[AisTarget, float]] = {}
        self.received = 0
        self.errors = 0
        self.stale_s = 360.0

    # ------------------------------------------------------------------ #
    def start(self, port: int | None = None) -> str:
        """返回空串表示成功，否则是错误描述。"""
        if self.running:
            return ""
        if port:
            self.port = int(port)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.port))
            s.settimeout(0.5)
        except OSError as exc:
            return f"监听 UDP {self.port} 失败：{exc}"
        self._sock = s
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="ais-feed", daemon=True)
        self._thread.start()
        return ""

    def stop(self) -> None:
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    # ------------------------------------------------------------------ #
    def _loop(self) -> None:
        buf = b""
        while self.running and self._sock is not None:
            try:
                data, _ = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.feed_line(line.decode("ascii", "ignore"))
            if len(buf) > 8192:
                buf = b""                 # 收到一坨没有换行的垃圾，丢掉重来

    def feed_line(self, line: str) -> AisTarget | None:
        """解析一行。单独暴露出来，方便绕过网络做测试。"""
        if not self.running:
            return None                 # stop() 之后不再往表里写
        t = self._decoder.feed(line)
        if t is None:
            if line.strip().startswith(("!AIVD", "$AIVD")):
                self.errors += 1
            return None
        self.received += 1
        with self._lock:
            if t.kind in (5, 24):
                old = self._targets.get(t.mmsi)
                if old is not None and t.name:
                    old[0].name = t.name
                return t
            if not t.name and t.mmsi in self._decoder.names:
                t.name = self._decoder.names[t.mmsi]
            self._targets[t.mmsi] = (t, time.monotonic())
        return t

    def snapshot(self, ref_lat: float, ref_lon: float) -> list[tuple[float, float, str, float]]:
        """当前有效目标的本地坐标 + 名称 + 航向，供 PPI 直接画。"""
        now = time.monotonic()
        out: list[tuple[float, float, str, float]] = []
        with self._lock:
            dead = [k for k, (_, ts) in self._targets.items() if now - ts > self.stale_s]
            for k in dead:
                del self._targets[k]
            for t, _ts in self._targets.values():
                x, y = t.local_xy(ref_lat, ref_lon)
                out.append((x, y, t.name or str(t.mmsi), t.cog))
        return out

    def count(self) -> int:
        with self._lock:
            return len(self._targets)
