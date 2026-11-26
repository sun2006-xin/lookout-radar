"""版本信息与可选在线更新检查（ROADMAP D4）。"""
from __future__ import annotations

import json
import urllib.request

__version__ = "1.3.1"
REPO = "sun2006-xin/lookout-radar"


def is_newer(remote_tag: str, current: str = __version__) -> bool:
    def _nums(v: str) -> tuple[int, ...]:
        parts = []
        for seg in v.lstrip("vV").split("."):
            digits = "".join(ch for ch in seg if ch.isdigit())
            parts.append(int(digits or 0))
        return tuple(parts)

    return _nums(remote_tag) > _nums(current)


def check_latest_release(timeout_s: float = 3.0) -> tuple[bool, str]:
    """查询 GitHub Releases 最新版本；始终带显式超时。"""
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": f"Lookout/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = str(data.get("tag_name") or "").strip()
        if not tag:
            return False, "未找到已发布的 Release 版本"
        if is_newer(tag):
            return True, f"发现新版本 {tag}（当前 {__version__}）：{data.get('html_url', '')}"
        return False, f"当前已是最新版本（{__version__}）"
    except Exception as exc:
        return False, f"检查更新失败（{type(exc).__name__}: {exc}）"
