"""声明式通用协议适配器自动注册入口。"""
from __future__ import annotations

from .. import paths
from ..core.custom_proto import load_custom_specs, register_specs


def reload_custom_drivers() -> list[str]:
    """从 data/custom_drivers.json 与内置预设加载并注册所有通用协议驱动。"""
    specs = load_custom_specs(paths.data_dir() / "custom_drivers.json")
    return register_specs(specs)


reload_custom_drivers()
