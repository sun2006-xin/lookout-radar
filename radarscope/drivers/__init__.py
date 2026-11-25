"""导入即注册。新增驱动只要在这里加一行 import。

再往下走一步就是运行时插件：把这一行改成扫描 plugins/ 目录下的所有模块。
架构已经支持，因为注册表是运行时可写的（见 core/driver.py）。
"""
from .ld2410 import LD2410Driver
from .ld2450 import LD2450Driver
from .ldlidar import LDLidarDriver
from .multi import MultiRadarDriver, MultiSectorDemoDriver
from .replay import ReplayDriver
from .sim_aesa import AesaSimDriver
from .simulator import SimulatorDriver
from .ultrasonic import UltrasonicDriver

__all__ = [
    "LD2410Driver", "LD2450Driver", "LDLidarDriver",
    "MultiRadarDriver", "MultiSectorDemoDriver",
    "ReplayDriver", "AesaSimDriver", "SimulatorDriver", "UltrasonicDriver",
]
