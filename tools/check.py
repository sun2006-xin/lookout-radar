"""一键自检：编译 + 测试 + 打包环境。

提交前跑这一个就够，不用记一堆命令：

    .venv\\Scripts\\python.exe tools\\check.py

CI 里也跑它（见 .github/workflows/ci.yml），保证本地和 CI 判据完全一致 ——
否则会出现"本地过了 CI 挂"这种最浪费时间的情况。
"""
from __future__ import annotations

import compileall
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 英文 Windows GitHub Runner 默认 stdout 可能是 cp1252，输出中文日志前强制切 UTF-8
for _s in (sys.stdout, sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def run(title: str, cmd: list[str]) -> tuple[bool, float, str]:
    print(f"\n=== {title} ===")
    t0 = time.perf_counter()
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    dt = time.perf_counter() - t0
    ok = r.returncode == 0
    tail = (r.stdout or "").strip().splitlines()[-6:]
    for line in tail:
        print("   ", line)
    if not ok:
        err = (r.stderr or "").strip().splitlines()[-12:]
        for line in err:
            print("  !", line)
    print(f"    {'通过' if ok else '失败'} · {dt:.1f}s")
    return ok, dt, r.stderr or ""


def main() -> int:
    py = sys.executable
    results = []

    print("=== 1/4 字节码编译 ===")
    ok = compileall.compile_dir(str(ROOT / "radarscope"), quiet=1) and \
        compileall.compile_dir(str(ROOT / "plugins"), quiet=1) and \
        compileall.compile_file(str(ROOT / "main.py"), quiet=1)
    print(f"    {'通过' if ok else '失败'}")
    results.append(("编译", ok))

    ok, _, _ = run("2/4 单元测试", [py, str(ROOT / "tests" / "test_core.py")])
    results.append(("单元测试", ok))

    ok, _, _ = run("3/4 链路自检", [py, str(ROOT / "tools" / "selftest.py")])
    results.append(("链路自检", ok))

    ok, _, _ = run("4/4 录制夹具回归", [py, str(ROOT / "tools" / "capture_check.py")])
    results.append(("录制夹具", ok))

    print("\n" + "=" * 46)
    bad = [n for n, o in results if not o]
    if bad:
        print(f"失败：{', '.join(bad)}")
        return 1
    print("全部通过。可以提交了。")
    print("提交前记得：如涉及 core/ 架构或模块变动，同步更新 docs/。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
