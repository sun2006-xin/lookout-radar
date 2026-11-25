"""统一的路径解析。

开发运行时数据放在项目目录下；打包成 exe 之后，基准目录变成 exe 所在目录。
如果不做这层抽象，PyInstaller 打出来的程序会去 `C:\\Users\\...\\Temp\\_MEIxxxx`
里找 data/ 和 plugins/，然后一脸茫然地报错。

另外还有个真实场景：装到 Program Files 里时目录不可写，
所以写数据失败要能自动退到 %LOCALAPPDATA%。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_base() -> Path:
    """基准目录：exe 所在目录（打包后）或项目根目录（开发时）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # radarscope/paths.py -> radarscope/ -> 项目根
    return Path(__file__).resolve().parents[1]


def _writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_test"
        probe.write_text("1", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def data_dir() -> Path:
    """数据目录（数据库、录制、日志、配置）。"""
    for cand in (app_base() / "data",
                 Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Lookout" / "data"):
        if _writable(cand):
            return cand
    p = Path.home() / "Lookout" / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def plugins_dir() -> Path:
    """插件目录。打包后如果 exe 旁边没有，就在数据目录同级建一个并放示例。"""
    p = app_base() / "plugins"
    if p.exists():
        return p
    alt = data_dir().parent / "plugins"
    alt.mkdir(parents=True, exist_ok=True)
    return alt


def resource(*parts: str) -> Path:
    """只读资源（图标、内置固件等）。PyInstaller 解包目录用 _MEIPASS。"""
    base = Path(getattr(sys, "_MEIPASS", app_base()))
    return base.joinpath(*parts)
