"""传输层 —— 把"字节怎么来"和"字节是什么意思"彻底分开。

这是"能不能接蓝牙/WiFi/有线"这个问题的唯一正确答法：
不要在驱动里写 serial.Serial，而是让驱动只依赖一个 read()/write() 接口。
于是同一个 LD2410 解析器，可以跑在：

    serial://COM3?baud=256000           USB 转串口 / CH340
    serial://COM4?baud=256000           HC-05/06 蓝牙串口（系统里就是一个 COM 口）
    ble://AA:BB:CC:DD:EE:FF             BLE 低功耗蓝牙（透传特征值）
    tcp://192.168.4.1:8080              ESP32 / 树莓派 WiFi 透传
    udp://0.0.0.0:9000                  组播/广播方式的雷达数据流
    file://capture.bin                  抓包回放

而且任何传输都支持 `&record=capture.bin` 参数，把收到的原始字节流顺手存下来，
下次用 file:// 就能完整复现 —— 包括时序。这是调试协议最省命的功能。

设计上有个必须守住的纪律：**传输层不认识协议**。
它只搬运字节，不做校验、不切帧、不解析。切帧是 codec 的事。
"""

from __future__ import annotations

import queue
import socket
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


class TransportError(RuntimeError):
    """传输层故障：连不上、断了、超时。上层据此决定是否重连。"""


class Transport(ABC):
    scheme = ""

    def __init__(self, record: str | None = None) -> None:
        self._rec = open(record, "wb") if record else None
        self._rec_buf_n = 0
        self._rec_last_flush = time.monotonic()

    # ---- 子类实现 ----
    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def read(self, n: int = 4096) -> bytes: ...

    def write(self, data: bytes) -> None:
        raise TransportError(f"{self.scheme} 不支持写入")

    def readline(self) -> bytes:
        """默认实现：逐字节拼到换行。逐字节读对串口有额外开销，
        但保证了不同传输的行为一致；文本协议雷达用这个就够了。"""
        buf = bytearray()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            ch = self.read(1)
            if not ch:
                break
            buf.extend(ch)
            if ch == b"\n":
                break
        return bytes(buf)

    def close(self) -> None:
        if self._rec is not None:
            try:
                self._rec.close()
            except Exception:
                pass
            self._rec = None

    # ---- 公共 ----
    @property
    def is_open(self) -> bool:
        return True

    @property
    def label(self) -> str:
        return self.scheme

    def _record(self, data: bytes) -> None:
        if self._rec is not None and data:
            self._rec.write(data)
            self._rec_buf_n += len(data)
            now = time.monotonic()
            if self._rec_buf_n >= 4096 or (now - self._rec_last_flush) >= 1.0:
                self._rec.flush()
                self._rec_buf_n = 0
                self._rec_last_flush = now

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()


# --------------------------------------------------------------------------- #
# 串口：USB 转串口、蓝牙 SPP（HC-05/06）、RS485 转接，本质上都是串口
# --------------------------------------------------------------------------- #
class SerialTransport(Transport):
    scheme = "serial"

    def __init__(self, port: str, baud: int = 256000, record: str | None = None,
                 timeout: float = 0.05) -> None:
        super().__init__(record)
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser = None

    def open(self) -> None:
        try:
            import serial
        except ImportError as exc:            # pragma: no cover
            raise TransportError("未安装 pyserial：pip install pyserial") from exc
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        except Exception as exc:
            raise TransportError(f"打不开串口 {self.port}：{exc}") from exc

    def read(self, n: int = 4096) -> bytes:
        if self._ser is None:
            return b""
        try:
            data = self._ser.read(n)
        except Exception as exc:
            raise TransportError(f"串口读失败：{exc}") from exc
        self._record(data)
        return data

    def write(self, data: bytes) -> None:
        if self._ser is None:
            raise TransportError("串口未打开")
        try:
            self._ser.write(data)
            self._ser.flush()
        except Exception as exc:
            raise TransportError(f"串口写失败：{exc}") from exc

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        super().close()

    @property
    def is_open(self) -> bool:
        return self._ser is not None and bool(getattr(self._ser, "is_open", False))

    @property
    def label(self) -> str:
        return f"{self.port}@{self.baud}"


# --------------------------------------------------------------------------- #
# TCP：ESP32 / 树莓派 / 串口服务器做 WiFi 透传
# --------------------------------------------------------------------------- #
class TcpTransport(Transport):
    scheme = "tcp"

    def __init__(self, host: str, port: int, record: str | None = None,
                 timeout: float = 0.05) -> None:
        super().__init__(record)
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None

    def open(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=2.0)
            self._sock.settimeout(self.timeout)
        except Exception as exc:
            raise TransportError(f"连不上 {self.host}:{self.port}：{exc}") from exc

    def read(self, n: int = 4096) -> bytes:
        if self._sock is None:
            return b""
        try:
            data = self._sock.recv(n)
        except socket.timeout:
            return b""
        except Exception as exc:
            raise TransportError(f"TCP 读失败：{exc}") from exc
        if data == b"":
            raise TransportError("TCP 连接被对端关闭")
        self._record(data)
        return data

    def write(self, data: bytes) -> None:
        if self._sock is None:
            raise TransportError("TCP 未连接")
        try:
            self._sock.sendall(data)
        except Exception as exc:
            raise TransportError(f"TCP 写失败：{exc}") from exc

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        super().close()

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}"


# --------------------------------------------------------------------------- #
# UDP：雷达主动往某个端口推数据流，不需要先连
# --------------------------------------------------------------------------- #
class UdpTransport(Transport):
    scheme = "udp"

    def __init__(self, host: str, port: int, record: str | None = None,
                 timeout: float = 0.05) -> None:
        super().__init__(record)
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._peer = None

    def open(self) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            self._sock.settimeout(self.timeout)
        except Exception as exc:
            raise TransportError(f"UDP 绑定 {self.host}:{self.port} 失败：{exc}") from exc

    def read(self, n: int = 4096) -> bytes:
        if self._sock is None:
            return b""
        try:
            data, self._peer = self._sock.recvfrom(max(n, 65535))
        except socket.timeout:
            return b""
        except Exception as exc:
            raise TransportError(f"UDP 读失败：{exc}") from exc
        self._record(data)
        return data

    def write(self, data: bytes) -> None:
        if self._sock is None or self._peer is None:
            raise TransportError("UDP 还没有对端地址（先收到一帧才能回发）")
        self._sock.sendto(data, self._peer)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        super().close()

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    @property
    def label(self) -> str:
        return f"udp:{self.host}:{self.port}"


# --------------------------------------------------------------------------- #
# 文件回放：把录下来的原始字节流按大致相同时序喂回去
# --------------------------------------------------------------------------- #
class FileTransport(Transport):
    scheme = "file"

    def __init__(self, path: str, chunk: int = 256, interval: float = 0.02,
                 loop: bool = True) -> None:
        super().__init__(None)
        self.path = path
        self.chunk = max(1, chunk)
        self.interval = max(0.0, interval)
        self.loop = loop
        self._data = b""
        self._pos = 0
        self._t_next = 0.0
        self._finished = False

    def open(self) -> None:
        p = Path(self.path)
        if not p.exists():
            raise TransportError(f"回放文件不存在：{self.path}")
        self._data = p.read_bytes()
        if not self._data:
            raise TransportError("回放文件是空的")
        self._pos = 0
        self._t_next = 0.0

    def read(self, n: int = 4096) -> bytes:
        if not self._data:
            return b""
        if self._finished:
            raise TransportError("回放结束")
        now = time.monotonic()
        if now < self._t_next:
            time.sleep(min(0.01, self._t_next - now))
            return b""
        self._t_next = now + self.interval
        step = min(max(1, n), self.chunk)
        end = min(len(self._data), self._pos + step)
        out = self._data[self._pos:end]
        self._pos = end
        if self._pos >= len(self._data):
            if self.loop:
                self._pos = 0
            else:
                # 先把手里的数据交出去，下一次再报结束，不能把尾巴丢了
                self._finished = True
        return out

    @property
    def label(self) -> str:
        return f"回放 {Path(self.path).name}"


# --------------------------------------------------------------------------- #
# BLE：低功耗蓝牙。设备把雷达数据放在一个 notify 特征值里
# --------------------------------------------------------------------------- #
class BleTransport(Transport):
    """实现要点：bleak 是纯 asyncio 的，而驱动层是同步阻塞读取。
    所以这里开一个后台线程跑事件循环，把通知回调收到的数据塞进线程安全队列，
    同步 read() 从队列取。这样上层驱动完全不用关心 asyncio。"""

    scheme = "ble"

    def __init__(self, address: str, char_uuid: str | None = None,
                 record: str | None = None, timeout: float = 0.05) -> None:
        super().__init__(record)
        self.address = address
        self.char_uuid = char_uuid
        self.timeout = timeout
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=1024)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = False
        self._err: str | None = None
        self._char: str | None = None
        self._loop = None
        self._client = None

    # ---- 后台线程 ----
    def _run(self) -> None:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:                       # pragma: no cover
            self._err = f"BLE 线程异常：{exc}"
        finally:
            self._client = None
            self._loop = None
            try:
                loop.close()
            except Exception:
                pass

    async def _main(self) -> None:
        import asyncio
        from bleak import BleakClient
        try:
            def _on_disc(_cli) -> None:
                if not self._stop.is_set() and self._err is None:
                    self._err = "BLE 设备已断开连接"

            async with BleakClient(self.address, disconnected_callback=_on_disc) as client:
                self._client = client
                uuid = self.char_uuid
                if uuid is None:
                    for svc in client.services:
                        for ch in svc.characteristics:
                            if "notify" in ch.properties:
                                uuid = ch.uuid
                                break
                        if uuid:
                            break
                if uuid is None:
                    self._err = "这个 BLE 设备没有可订阅的特征值"
                    return
                self._char = uuid

                def _cb(_sender, data: bytearray) -> None:
                    self._record(bytes(data))
                    try:
                        self._q.put_nowait(bytes(data))
                    except queue.Full:
                        pass

                await client.start_notify(uuid, _cb)
                self._ready = True
                while not self._stop.is_set() and getattr(client, "is_connected", True):
                    await asyncio.sleep(0.05)
                if not self._stop.is_set() and self._err is None:
                    self._err = "BLE 连接已中断"
        except Exception as exc:
            self._err = f"BLE 连接失败：{exc}"

    # ---- Transport 接口 ----
    def open(self) -> None:
        if not self.address or not self.address.strip():
            raise TransportError(
                "BLE 需要指定设备地址，例如 ble://AA:BB:CC:DD:EE:FF\n"
                "可点「连接设置 → 蓝牙 BLE → 扫描附近的 BLE 设备」自动获取地址。"
            )
        try:
            import bleak  # noqa: F401
        except ImportError as exc:
            raise TransportError(
                "BLE 需要额外安装：pip install bleak\n"
                "注意：Windows 上 BLE 和经典蓝牙 SPP 是两回事，"
                "HC-05/06 这类模块在系统里表现为一个串口，应该用 serial:// 而不是 ble://"
            ) from exc

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._err:
                raise TransportError(self._err)
            if self._ready:
                return
            time.sleep(0.05)
        raise TransportError("BLE 连接超时（10 秒）")

    def read(self, n: int = 4096) -> bytes:
        if self._err:
            raise TransportError(self._err)
        try:
            return self._q.get(timeout=self.timeout)
        except queue.Empty:
            if self._err:
                raise TransportError(self._err)
            return b""

    def write(self, data: bytes) -> None:
        if not self._ready or self._char is None or self._client is None or self._loop is None:
            raise TransportError("BLE 未连接")
        import asyncio
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._client.write_gatt_char(self._char, data), self._loop
            )
            fut.result(timeout=2.0)
        except Exception as exc:
            raise TransportError(f"BLE 写失败：{exc}") from exc

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        super().close()

    @property
    def is_open(self) -> bool:
        return self._ready and self._err is None

    @property
    def label(self) -> str:
        return f"BLE {self.address}"


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #
_SCHEMES = {
    "serial": SerialTransport,
    "tcp": TcpTransport,
    "udp": UdpTransport,
    "file": FileTransport,
    "ble": BleTransport,
}


def parse_spec(spec: str) -> dict:
    """解析端点字符串，返回 {scheme, target, options}。

    兼容裸写：直接给 "COM3" 或 "/dev/ttyUSB0" 当作串口。
    """
    spec = (spec or "").strip()
    if not spec:
        raise TransportError("端点为空")
    if "://" not in spec:
        return {"scheme": "serial", "target": spec, "options": {}}

    u = urlparse(spec)
    opts = {k: v[0] for k, v in parse_qs(u.query).items()}
    target = u.netloc or u.path.lstrip("/")
    if u.scheme == "file":
        p = unquote(u.path)
        if u.netloc in ("", "localhost"):
            # Windows file:///C:/x.bin -> C:/x.bin；POSIX file:///Users/x.bin 必须保留前导 '/'
            if len(p) >= 3 and p[0] == "/" and p[1].isalpha() and p[2] == ":":
                target = p[1:]
            else:
                target = p
        elif len(u.netloc) == 2 and u.netloc[1] == ":":
            target = f"{u.netloc}{p}"
        else:
            target = f"{u.netloc}{p}"
    return {"scheme": u.scheme, "target": target, "options": opts}


def open_transport(spec: str, default_baud: int = 256000, **overrides) -> Transport:
    """按端点字符串创建并打开传输。"""
    info = parse_spec(spec)
    scheme, target, opts = info["scheme"], info["target"], dict(info["options"])
    opts.update({k: v for k, v in overrides.items() if v is not None})
    record = opts.pop("record", None)

    if scheme not in _SCHEMES:
        raise TransportError(
            f"不支持的传输方式 '{scheme}'，可选：{', '.join(sorted(_SCHEMES))}")

    if scheme == "serial":
        tr = SerialTransport(target, int(opts.pop("baud", default_baud)), record=record)
    elif scheme in ("tcp", "udp"):
        if ":" in target:
            host, _, ps = target.rpartition(":")
            tr = _SCHEMES[scheme](host or "127.0.0.1", int(ps), record=record)
        else:
            tr = _SCHEMES[scheme]("127.0.0.1", int(target), record=record)
    elif scheme == "file":
        tr = FileTransport(target,
                           int(opts.pop("chunk", 256)),
                           float(opts.pop("interval", 0.02)),
                           str(opts.pop("loop", "1")) not in ("0", "false", "False"))
    elif scheme == "ble":
        tr = BleTransport(target, opts.pop("char", None), record=record)
    else:                                            # pragma: no cover
        raise TransportError(f"未实现的传输：{scheme}")

    tr.open()
    return tr


def available_schemes() -> list[str]:
    return sorted(_SCHEMES)


def endpoint_of(kwargs: dict, baud: int = 256000) -> str:
    """从驱动收到的 kwargs 里取出端点字符串。

    兼容两种写法：
        open(endpoint="tcp://192.168.4.1:8080")
        open(port="COM3", baud=256000)        <- 老写法，自动拼成 serial://
    """
    ep = (kwargs.get("endpoint") or "").strip()
    if ep:
        return ep
    port = (kwargs.get("port") or "").strip()
    if not port or port.startswith("--"):
        return ""
    if "://" in port:
        return port
    b = int(kwargs.get("baud") or baud)
    return f"serial://{port}?baud={b}"
