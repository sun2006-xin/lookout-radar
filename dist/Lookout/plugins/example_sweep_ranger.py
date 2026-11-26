"""示例插件：一台"单波束测距仪"虚拟雷达。

这个文件演示**怎么用插件加一种新雷达**，全程不碰主程序任何代码。

用法：放在 plugins/ 目录下，重启软件（或在「插件」对话框点重新加载），
雷达下拉框里就会多出「插件示例 · 单波束测距仪」。
"""
import time

import numpy as np

from radarscope.core.driver import RadarDriver, register as register_driver
from radarscope.core.model import Capabilities, RadarFrame

PLUGIN = {
    "name": "示例 · 单波束测距仪",
    "version": "1.0",
    "author": "RadarScope",
    "description": "演示如何用插件注册一台新雷达：只有距离维、没有方位",
}


@register_driver
class SweepRangerDriver(RadarDriver):
    id = "plugin_sweep"
    label = "插件示例 · 单波束测距仪"
    simulated = True
    source = "sim"
    form = "point"
    vendor = "示例"

    N = 64

    def __init__(self) -> None:
        self._r = np.linspace(0.2, 15.0, self.N).astype(np.float32)
        self._t = 0.0
        self._rng = np.random.default_rng(3)
        self._ph = 0.0

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="单波束测距仪",
            has_scan=True, has_azimuth=False, has_targets=False,
            az_fov=30.0, range_min=0.2, range_max=15.0,
            scan_mode="staring", scan_rate=20.0, tier="sensor",
            transports=("virtual",),
            views=("ppi", "ascope", "waterfall", "tracks"),
            notes="插件注册的示例雷达，只有距离维",
        )

    def open(self, **kwargs) -> None:
        self._t = time.monotonic()

    def close(self) -> None:
        pass

    def read(self) -> RadarFrame | None:
        now = time.monotonic()
        if now - self._t < 0.05:
            time.sleep(0.004)
            return None
        self._t = now
        self._ph += 0.05
        d = 6.0 + 3.0 * np.sin(self._ph)          # 目标在 3~9 m 之间来回走
        prof = np.exp(-0.5 * ((self._r - d) / 0.25) ** 2) * 0.9
        prof += self._rng.normal(0.01, 0.006, self.N)
        return RadarFrame(
            timestamp=now,
            scan=np.clip(prof, 0, 1).reshape(1, -1).astype(np.float32),
            az_axis=np.zeros(1, dtype=np.float32),
            r_axis=self._r,
            source=self.label,
            meta={"fresh_az": (0, 1), "scan_period": 1.0},
        )


def setup(api) -> None:
    """插件入口。可选的：驱动在 import 期就被 @register_driver 注册了，
    这里只是用 API 再确认一次，顺便留条日志。"""
    api.register_driver(SweepRangerDriver)
    api.log("示例测距仪已注册")
