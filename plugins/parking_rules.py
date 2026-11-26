"""示例插件：泊车场景的告警规则包。

演示**怎么用插件加一组告警规则**，不用改主程序的默认规则表。

装在车上时的典型布法（配 6 个 60° 模块，或 4 个角雷达）：
    正前 2.5 m 内 → 提示；正后 1.5 m 内 → 紧急；侧向 0.6 m 内 → 紧急。

注意这里的门限是"进入防区"用的，和主程序内置的通用规则是叠加关系，
id 不冲突即可共存。
"""
from radarscope.core.alarm import AlarmRule

PLUGIN = {
    "name": "泊车告警规则包",
    "version": "1.0",
    "author": "RadarScope",
    "description": "前后向与侧向分区告警，适合车载低速场景",
}

RULES = [
    AlarmRule(
        "park_rear", "倒车过近", "zone",
        {"cx": 0.0, "cy": -1.2, "r": 1.5},
        level="crit", hold=0.2, cooldown=4.0, hysteresis=0.2, enabled=False,
    ),
    AlarmRule(
        "park_front", "前向接近", "zone",
        {"cx": 0.0, "cy": 2.4, "r": 2.0},
        level="warn", hold=0.5, cooldown=8.0, hysteresis=0.25, enabled=False,
    ),
    AlarmRule(
        "park_side_left", "左侧贴边", "zone",
        {"cx": -1.0, "cy": 0.6, "r": 0.7},
        level="crit", hold=0.3, cooldown=5.0, hysteresis=0.2, enabled=False,
    ),
    AlarmRule(
        "park_side_right", "右侧贴边", "zone",
        {"cx": 1.0, "cy": 0.6, "r": 0.7},
        level="crit", hold=0.3, cooldown=5.0, hysteresis=0.2, enabled=False,
    ),
]


def setup(api) -> None:
    api.register_alarm_rules(RULES)
    api.log(f"已注册 {len(RULES)} 条泊车规则")
