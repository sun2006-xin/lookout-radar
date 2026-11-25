"""运行时插件系统。

解决的问题：软件要能长大，但不能每次加功能都改主程序。
所以把"扩展点"显式定义出来，第三方往 `plugins/` 丢一个 .py 就能加东西。

插件能扩展三样东西：
    1. 驱动（新雷达）      api.register_driver(cls)
    2. 告警规则            api.register_alarm_rules([...])
    3. 纯逻辑副作用        register(api) 里自己干（比如给引擎打补丁）

插件写法（`plugins/my_radar.py`）：

    PLUGIN = {
        "name": "我的雷达",
        "version": "1.0",
        "author": "xxx",
        "description": "支持 XXX 型号",
    }

    def register(api):
        api.register_driver(MyRadarDriver)
        api.register_alarm_rules([...])

设计上守住的纪律：
  * **插件炸了不能拖垮主程序**。每个插件独立 try/except，失败只记录不抛。
  * **插件目录不写死**。主程序只认一个路径参数，方便测试和打包。
  * **不信任插件**。这里不做沙箱（做不到），但至少不让它影响启动。
"""
from __future__ import annotations

import importlib.util
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .alarm import AlarmRule
from .driver import RadarDriver, register as register_driver_cls


@dataclass
class PluginInfo:
    name: str
    version: str = "-"
    author: str = "-"
    description: str = ""
    path: str = ""
    ok: bool = True
    error: str = ""
    drivers: list[str] = field(default_factory=list)
    rules: int = 0
    loaded_at: float = 0.0

    @property
    def status(self) -> str:
        return "正常" if self.ok else "加载失败"


class PluginAPI:
    """给插件用的门面。插件只应该通过它来注册，不要直接摸内部状态。"""

    def __init__(self, info: PluginInfo) -> None:
        self.info = info
        self._rules: list[AlarmRule] = []
        self.logs: list[str] = []

    # ---- 扩展点 ----
    def register_driver(self, cls: type[RadarDriver]) -> None:
        if not (isinstance(cls, type) and issubclass(cls, RadarDriver)):
            raise TypeError("register_driver 需要 RadarDriver 的子类")
        register_driver_cls(cls)
        self.info.drivers.append(getattr(cls, "id", cls.__name__))

    def register_alarm_rules(self, rules: list[AlarmRule]) -> None:
        for r in rules:
            if not isinstance(r, AlarmRule):
                raise TypeError("register_alarm_rules 只接受 AlarmRule")
        self._rules.extend(rules)
        self.info.rules = len(self._rules)

    def log(self, msg: str) -> None:
        self.logs.append(str(msg))

    # ---- 内部 ----
    @property
    def rules(self) -> list[AlarmRule]:
        return list(self._rules)


class PluginManager:
    def __init__(self, plugin_dir: str | Path) -> None:
        self.plugin_dir = Path(plugin_dir)
        self.plugins: list[PluginInfo] = []
        self._apis: list[PluginAPI] = []
        self._seq = 0

    # ------------------------------------------------------------------ #
    def _candidates(self) -> list[tuple[Path, str]]:
        if not self.plugin_dir.exists():
            return []
        out: list[tuple[Path, str]] = []
        for p in sorted(self.plugin_dir.iterdir()):
            if p.is_file() and p.suffix == ".py" and not p.name.startswith("_"):
                out.append((p, p.stem))
            elif p.is_dir() and not p.name.startswith("_"):
                for cand in (p / "plugin.py", p / "__init__.py"):
                    if cand.exists():
                        out.append((cand, p.name))
                        break
        return out

    def _load_one(self, path: Path, modname: str) -> PluginInfo:
        self._seq += 1
        uniq = f"_rs_plugin_{modname}_{self._seq}"
        info = PluginInfo(name=modname, path=str(path), loaded_at=time.time())
        try:
            spec = importlib.util.spec_from_file_location(uniq, path)
            if spec is None or spec.loader is None:
                raise ImportError("无法为插件创建模块规格")
            module = importlib.util.module_from_spec(spec)
            sys.modules[uniq] = module
            spec.loader.exec_module(module)          # 插件在 import 期就可能注册驱动

            meta = getattr(module, "PLUGIN", {}) or {}
            info.name = str(meta.get("name") or modname)
            info.version = str(meta.get("version") or "-")
            info.author = str(meta.get("author") or "-")
            info.description = str(meta.get("description") or "")

            api = PluginAPI(info)
            # 钩子统一叫 setup。早期版本用 register，但插件往往会
            # `from radarscope.core.driver import register`，名字直接撞车，
            # 结果把 API 对象当驱动类注册进去 —— 所以这里只认 setup，
            # 再兼容一个"确实定义在插件自己模块里"的 register。
            hook = getattr(module, "setup", None)
            if hook is None:
                cand = getattr(module, "register", None)
                if callable(cand) and getattr(cand, "__module__", "") == uniq:
                    hook = cand
            if callable(hook):
                hook(api)
            self._apis.append(api)
        except Exception as exc:                     # 插件自己的锅，不能影响主程序
            info.ok = False
            info.error = f"{type(exc).__name__}: {exc}"
            tb = traceback.format_exc(limit=3)
            info.description = (info.description + "\n" + tb).strip()
            print(f"[plugin] 加载失败 {path.name}: {info.error}")
        return info

    def load_all(self) -> list[PluginInfo]:
        self.plugins = [self._load_one(p, m) for p, m in self._candidates()]
        return self.plugins

    def reload(self) -> list[PluginInfo]:
        """重载。注意：驱动注册表是运行时可写的，同名 id 会被覆盖，
        所以重载是安全的；但插件在别处留下的副作用（比如开了线程）不会被回收。"""
        return self.load_all()

    # ------------------------------------------------------------------ #
    def plugin_rules(self) -> list[AlarmRule]:
        out: list[AlarmRule] = []
        for api in self._apis:
            out.extend(api.rules)
        return out

    def driver_ids(self) -> list[str]:
        ids: list[str] = []
        for info in self.plugins:
            ids.extend(info.drivers)
        return ids
