"""UDP -> WebSocket 轻量桥接与静态页服务器（ROADMAP D6）。

零第三方依赖，纯 Python 标准库实现（UDP 监听 + RFC 6455 WebSocket 握手与帧广播
+ HTTP 托管 tools/web_viewer.html），用于配合主程序「转发」功能在浏览器看实时态势。

用法：
    .venv\\Scripts\\python.exe tools\\ws_bridge.py --udp-port 9100 --http-port 8088
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import socket
import struct
import threading
from pathlib import Path

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HTML_PATH = Path(__file__).resolve().parent / "web_viewer.html"


def radarscope_json_to_foxglove(raw_json: str) -> str:
    """将 Lookout Forwarder UDP JSON 转换为 Foxglove / ROS 2 兼容 JSON 消息。"""
    try:
        pkt = json.loads(raw_json)
    except Exception:
        return raw_json
    if pkt.get("type") != "radarscope.frame":
        return raw_json
    ts = float(pkt.get("ts") or 0.0)
    sec = int(ts)
    nsec = int((ts - sec) * 1e9)
    ros_msg = {
        "op": "publish",
        "topic": "/radar/targets",
        "msg": {
            "header": {"stamp": {"sec": sec, "nanosec": nsec}, "frame_id": "radar_link"},
            "seq": int(pkt.get("seq", 0)),
            "range_max": float(pkt.get("range_max", 0.0)),
            "points": pkt.get("targets", []),
        },
    }
    return json.dumps(ros_msg, ensure_ascii=False, separators=(",", ":"))


def ws_accept_key(client_key: str) -> str:
    raw = (client_key.strip() + WS_MAGIC).encode("ascii")
    return base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")


def encode_ws_text(payload: str) -> bytes:
    data = payload.encode("utf-8")
    n = len(data)
    if n < 126:
        header = struct.pack("!BB", 0x81, n)
    elif n < 65536:
        header = struct.pack("!BBH", 0x81, 126, n)
    else:
        header = struct.pack("!BBQ", 0x81, 127, n)
    return header + data


class BridgeServer:
    """把 Forwarder 发出的 UDP JSON 帧实时广播给 WebSocket 客户端。"""

    def __init__(self, udp_port: int = 9100, ws_port: int = 9101,
                 http_port: int = 8088, ros_mode: bool = False) -> None:
        self.udp_port = udp_port
        self.ws_port = ws_port
        self.http_port = http_port
        self.ros_mode = bool(ros_mode)
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def broadcast(self, text: str) -> None:
        frame = encode_ws_text(text)
        dead: list[socket.socket] = []
        with self._lock:
            for c in self._clients:
                try:
                    c.sendall(frame)
                except Exception:
                    dead.append(c)
            for c in dead:
                self._clients.remove(c)
                try:
                    c.close()
                except Exception:
                    pass

    def _udp_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", self.udp_port))
        sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(65535)
                msg = data.decode("utf-8", errors="replace")
                if self.ros_mode:
                    msg = radarscope_json_to_foxglove(msg)
                self.broadcast(msg)
            except socket.timeout:
                continue
            except Exception:
                break
        sock.close()

    def _ws_loop(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.ws_port))
        srv.listen(16)
        srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            threading.Thread(target=self._handshake, args=(conn,), daemon=True).start()
        srv.close()

    def _handshake(self, conn: socket.socket) -> None:
        try:
            req = conn.recv(2048).decode("utf-8", errors="ignore")
            key = ""
            for line in req.splitlines():
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
                    break
            if not key:
                conn.close()
                return
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {ws_accept_key(key)}\r\n\r\n"
            )
            conn.sendall(resp.encode("ascii"))
            with self._lock:
                self._clients.append(conn)
        except Exception:
            conn.close()

    def start(self) -> None:
        threading.Thread(target=self._udp_loop, daemon=True).start()
        threading.Thread(target=self._ws_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for c in self._clients:
                try:
                    c.close()
                except Exception:
                    pass
            self._clients.clear()


def main() -> int:
    parser = argparse.ArgumentParser(description="Lookout UDP -> WebSocket 订阅桥")
    parser.add_argument("--udp-port", type=int, default=9100)
    parser.add_argument("--ws-port", type=int, default=9101)
    parser.add_argument("--http-port", type=int, default=8088)
    parser.add_argument("--ros", action="store_true", help="转换为 Foxglove / ROS 兼容 JSON 载荷")
    args = parser.parse_args()

    bridge = BridgeServer(args.udp_port, args.ws_port, args.http_port, ros_mode=args.ros)
    bridge.start()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    print(f"Web 订阅端已启动：http://127.0.0.1:{args.http_port} (UDP:{args.udp_port} -> WS:{args.ws_port})")
    try:
        http.server.HTTPServer(("127.0.0.1", args.http_port), _Handler).serve_forever()
    except KeyboardInterrupt:
        bridge.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
