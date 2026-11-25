"""不调用 COM，直接按 MS-SHLLINK 规范写 .lnk 二进制。

WScript.Shell 属于可执行任意代码的 COM，被安全策略拦截是合理的；
但快捷方式本身只是一个文件，按格式写出来即可，完全不需要 COM。

只写 LinkInfo + StringData（不含 IDList），Windows 资源管理器、
任务栏固定、属性面板都能正确解析。
"""
from __future__ import annotations

import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "dist" / "Lookout" / "Lookout.exe"
WORKDIR = ROOT / "dist" / "Lookout"
ICON = ROOT / "assets" / "app.ico"
DESKTOP = Path.home() / "Desktop"


def _u16(v: int) -> bytes:
    return struct.pack("<H", v)


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _wstr(s: str) -> bytes:
    """StringData：2 字节字符数 + UTF-16LE（不含结尾 NUL）。"""
    b = s.encode("utf-16-le")
    return _u16(len(s)) + b


def build(target: Path, workdir: Path, icon: Path) -> bytes:
    # ---- ShellLinkHeader (76 bytes) ----
    flags = 0x000000D2  # HasLinkInfo | HasWorkingDir | HasIconLocation | IsUnicode
    clsid = bytes.fromhex("0114020000000000C000000000000046")
    header = b"".join([
        _u32(0x4C),                 # HeaderSize
        clsid,                      # LinkCLSID
        _u32(flags),                # LinkFlags
        _u32(0),                    # FileAttributes
        b"\x00" * 8 * 3,            # Creation / Access / Write time
        _u32(0),                    # FileSize
        _u32(0),                    # IconIndex
        _u32(1),                    # ShowCommand = SW_SHOWNORMAL
        _u16(0),                    # HotKey
        _u16(0), _u32(0), _u32(0),  # Reserved1/2/3
    ])
    assert len(header) == 0x4C, len(header)

    # ---- LinkInfo ----
    base = str(target).encode("ascii") + b"\x00"
    suffix = b"\x00"                       # CommonPathSuffix 空串
    # VolumeID: size / DriveType / Serial / LabelOffset / 标签(空串)
    volume = b"".join([_u32(17), _u32(3), _u32(0), _u32(16), b"\x00"])
    assert len(volume) == 17

    header_size = 0x1C                     # 不含 unicode 偏移字段
    volume_off = header_size
    base_off = volume_off + len(volume)
    suffix_off = base_off + len(base)
    link_info_size = suffix_off + len(suffix)

    link_info = b"".join([
        _u32(link_info_size),
        _u32(header_size),
        _u32(1),                           # VolumeIDAndLocalBasePath
        _u32(volume_off),
        _u32(base_off),
        _u32(0),                           # CommonNetworkRelativeLinkOffset
        _u32(suffix_off),
        volume, base, suffix,
    ])
    assert len(link_info) == link_info_size

    # ---- StringData：顺序按 flag 位固定 ----
    strings = _wstr(str(workdir)) + _wstr(str(icon))
    # ExtraData 的终止块
    terminal = _u32(0)

    # 描述不写进 StringData（没置 HasName 位），由 Windows 属性面板显示 TargetPath 即可
    return header + link_info + strings + terminal


def main() -> int:
    for p in (TARGET, ICON):
        if not p.exists():
            print(f"缺少 {p}，跳过")
            return 1
    DESKTOP.mkdir(parents=True, exist_ok=True)
    out = DESKTOP / "瞭望 Lookout.lnk"
    out.write_bytes(build(TARGET, WORKDIR, ICON))
    print(f"已写入 {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
