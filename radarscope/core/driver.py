"""驱动抽象与注册表。

新增一种雷达 = 写一个类 + 挂一个装饰器。界面会自动发现它。
驱动只负责"把物理设备翻译成 RadarFrame"，不碰任何界面代码。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .model import Capabilities, RadarFrame


class RadarDriver(ABC):
    """所有驱动的基类。"""

    #: 唯一标识，配置文件和命令行用它
    id: str = "base"
    #: 界面上显示的中文名
    label: str = "基类"
    #: 是否为模拟器（界面会标出来）
    simulated: bool = False
    #: 分类：sim / device / replay / fusion。界面一级分栏用它。
    source: str = "device"
    #: 测量形态：point / sector / omni / tracking / rd / fusion。界面二级分组用它。
    form: str = "sector"
    #: 厂商/系列，仅展示用。**任何逻辑都不许依赖它**，否则又变成被型号写死。
    vendor: str = ""

    #: 是否支持下行控制（增益 / STC / FTC / 量程下发）。
    #: 只有真雷达才可能支持；界面据此决定控件可用性，不靠型号判断。
    supports_control: bool = False
    #: 串口默认波特率（界面扫描串口时自动按当前驱动匹配，避免 230400/256000 错配）
    default_baud: int = 256000

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """声明这个驱动能给出什么。界面据此裁剪功能。

        刻意做成实例方法：同一个驱动换个工作模式（比如旋转/凝视），
        能力声明就会跟着变，界面也会自动切换显示方式。
        """

    @abstractmethod
    def open(self, **kwargs) -> None:
        """打开设备。kwargs 由界面传入（port/baud/mode 等），驱动各取所需。"""

    @abstractmethod
    def read(self) -> RadarFrame | None:
        """取一帧。允许阻塞一小段时间（内部 poll），无数据返回 None。"""

    @abstractmethod
    def close(self) -> None:
        """释放资源。必须可重复调用。"""

    # ---- 可选钩子 ----
    def describe(self) -> str:
        """状态栏显示的一行摘要。"""
        return self.capabilities().summary()

    def resolve_capabilities(self) -> "Capabilities":
        """把类属性（source/form/vendor）合并进 Capabilities，一次做完。

        以前每个调用点都要手工补三行，漏一处就出"共视列不显示"这种鬼 bug。
        现在只有这一个地方负责同步，界面和驱动都调它。
        """
        from dataclasses import replace

        from .model import Capabilities

        caps = self.capabilities()
        t = type(self)
        return replace(
            caps,
            source=getattr(t, "source", "") or caps.source,
            form=getattr(t, "form", "") or caps.form,
            vendor=getattr(t, "vendor", "") or caps.vendor,
        )

    def set_control(self, **params) -> bool:
        """向设备下发参数：gain / stc / ftc / range。

        返回是否成功。基类直接返回 False —— "不支持"是个正当答案，
        界面看到 False 就把控制项置灰并说明原因，而不是假装生效。
        真雷达的驱动覆写它把参数编码成指令写回串口/网络即可。
        """
        return False

    def control_state(self) -> dict:
        """设备侧当前参数，供界面回显。默认空字典表示不支持。"""
        return {}


_REGISTRY: dict[str, type[RadarDriver]] = {}


def register(cls: type[RadarDriver]) -> type[RadarDriver]:
    """注册驱动。

    允许同名覆盖：插件热重载时会重新注册同一个 id，
    如果这里抛异常，重载就废了。覆盖时留一条日志，出问题能查。
    """
    old = _REGISTRY.get(cls.id)
    if old is not None and old is not cls:
        import logging
        logging.getLogger("radarscope").info(
            "驱动 id '%s' 被覆盖：%s -> %s", cls.id, old.__name__, cls.__name__)
    _REGISTRY[cls.id] = cls
    return cls


def available_drivers() -> dict[str, type[RadarDriver]]:
    return dict(_REGISTRY)


def get_driver(driver_id: str) -> type[RadarDriver]:
    return _REGISTRY[driver_id]
