"""告警引擎。

雷达软件没有告警就是一块好看的屏保。但告警最容易做烂的地方不是"会不会报"，
而是"会不会刷屏"——目标在门限附近抖一下，就给你弹十条消息，操作员三分钟后就
把告警关了，等于没有。

所以这里有三道闸：
    去抖（hold）    条件必须持续这么久才算真
    滞回（hysteresis） 触发和恢复用不同门限，避免边界抖动反复触发
    冷却（cooldown） 同一条规则触发后，这段时间内不再重复报

规则本身是数据，不是代码：可以存成 JSON、界面上编辑、运行时加载。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from math import hypot
from typing import Any, Iterable

from .model import Capabilities, Target

LEVELS = ("info", "warn", "crit")


@dataclass(slots=True)
class AlarmRule:
    id: str
    name: str
    kind: str                     # count_ge | range_lt | range_gt | speed_ge | zone | lost | offline
    params: dict[str, Any] = field(default_factory=dict)
    level: str = "warn"
    hold: float = 1.0             # 条件持续多久才触发，秒
    cooldown: float = 8.0         # 触发后多久内不重复报，秒
    hysteresis: float = 0.15      # 恢复门限的比例余量（0.15 = 需要回到 85% 才清除）
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind,
            "params": dict(self.params), "level": self.level,
            "hold": self.hold, "cooldown": self.cooldown,
            "hysteresis": self.hysteresis, "enabled": self.enabled,
        }


@dataclass(slots=True)
class AlarmEvent:
    ts: float
    rule_id: str
    name: str
    level: str
    message: str
    active: bool                  # True=触发，False=恢复
    tid: int = -1
    value: float = 0.0

    @property
    def level_rank(self) -> int:
        return LEVELS.index(self.level) if self.level in LEVELS else 0


DEFAULT_RULES = [
    AlarmRule("intrusion", "防区入侵", "zone", {"cx": 0.0, "cy": 3.0, "r": 1.5},
              level="crit", hold=0.5, cooldown=10.0, enabled=False),
    AlarmRule("too_close", "目标过近", "range_lt", {"m": 0.8},
              level="crit", hold=0.3, cooldown=6.0),
    AlarmRule("many", "目标数超限", "count_ge", {"n": 4},
              level="warn", hold=1.0, cooldown=20.0),
    AlarmRule("fast", "高速目标", "speed_ge", {"mps": 2.0},
              level="warn", hold=0.5, cooldown=15.0),
    AlarmRule("offline", "数据中断", "offline", {"s": 3.0},
              level="crit", hold=0.0, cooldown=30.0),
]

PARAM_HINTS = {
    "zone": '{"cx": 0.0, "cy": 3.0, "r": 1.5}     圆心(米) 与半径(米)',
    "poly_zone": '{"points": [[-1,2],[1,2],[1,4],[-1,4]]} 多边形顶点(米)',
    "range_lt": '{"m": 0.8}                        距离小于该值触发',
    "range_gt": '{"m": 10.0}                       距离大于该值触发',
    "speed_ge": '{"mps": 2.0}                      速度大于该值触发',
    "count_ge": '{"n": 4}                          目标数达到该值触发',
    "lost": "{}                                     所有目标丢失时触发",
    "offline": '{"s": 3.0}                         超过该秒数没有数据触发',
}


def point_in_polygon(px: float, py: float, points: list[list[float] | tuple[float, float]]) -> bool:
    """射线法判断点是否在多边形内（至少 3 个顶点，向下兼容圆形规则）。"""
    n = len(points)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = float(points[i][0]), float(points[i][1])
        xj, yj = float(points[j][0]), float(points[j][1])
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def merge_rules(*groups: Iterable[AlarmRule]) -> list[AlarmRule]:
    """合并多组规则，同 id 后者覆盖前者，保持首次出现的顺序。

    优先级设计：内置默认 < 插件 < 用户手动编辑。
    这样用户改过的规则永远说话算数，插件升级也不会悄悄改掉用户的门限。
    """
    out: dict[str, AlarmRule] = {}
    for g in groups:
        for r in g or []:
            out[r.id] = r
    return list(out.values())


def rules_to_json(rules: Iterable[AlarmRule]) -> str:
    import json
    return json.dumps([r.to_dict() for r in rules], ensure_ascii=False, indent=2)


def rules_from_json(text: str) -> list[AlarmRule]:
    import json
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("rules", [])
    out: list[AlarmRule] = []
    for d in data:
        out.append(AlarmRule(
            id=str(d["id"]), name=str(d.get("name", d["id"])),
            kind=str(d["kind"]), params=dict(d.get("params") or {}),
            level=str(d.get("level", "warn")),
            hold=float(d.get("hold", 1.0)),
            cooldown=float(d.get("cooldown", 8.0)),
            hysteresis=float(d.get("hysteresis", 0.15)),
            enabled=bool(d.get("enabled", True)),
        ))
    return out


class _RuleState:
    __slots__ = ("since", "last_fire", "active", "active_tid")

    def __init__(self) -> None:
        self.since: float | None = None
        self.last_fire: float = -1e9
        self.active: bool = False
        self.active_tid: int = -1


class AlarmEngine:
    def __init__(self, rules: Iterable[AlarmRule] | None = None) -> None:
        self.rules: list[AlarmRule] = list(rules if rules is not None else DEFAULT_RULES)
        self._state: dict[str, _RuleState] = {}
        self._last_frame_ts = time.monotonic()

    # ------------------------------------------------------------------ #
    def set_rules(self, rules: Iterable[AlarmRule]) -> None:
        self.rules = list(rules)
        self._state.clear()

    def reset(self) -> None:
        self._state.clear()
        self._last_frame_ts = time.monotonic()

    def active_events(self) -> list[AlarmEvent]:
        out = []
        for r in self.rules:
            st = self._state.get(r.id)
            if st and st.active:
                out.append(AlarmEvent(0.0, r.id, r.name, r.level,
                                      "持续中", True, st.active_tid))
        return out

    # ------------------------------------------------------------------ #
    @staticmethod
    def _measure(rule: AlarmRule, tracks: list[Target], caps: Capabilities,
                 silence_s: float = 0.0, active: bool = False) -> tuple[bool, float, int, str]:
        """返回 (是否满足, 度量值, 关联航迹号, 描述)。

        active=True 表示这条告警已经在报，此时门限按 hysteresis 放宽，
        让"恢复"比"触发"更难一点 —— 否则目标恰好在门限上时，
        告警会一秒钟开合十几次。
        """
        p = rule.params
        alive = [t for t in tracks if t.missed <= 1e-3 and not getattr(t, "tentative", False)]
        hyst = max(0.0, min(0.9, rule.hysteresis))

        def relax(lim: float) -> float:
            return lim * (1.0 + hyst) if active else lim

        if rule.kind == "count_ge":
            n = len(tracks)
            need = float(p.get("n", 1))
            return n >= relax(need), float(n), -1, f"当前 {n} 个目标（门限 {int(need)}）"

        if rule.kind == "range_lt":
            if not alive:
                return False, 0.0, -1, ""
            t = min(alive, key=lambda x: x.rng)
            lim = float(p.get("m", 1.0))
            return t.rng < relax(lim), t.rng, t.tid, f"T{t.tid} 距离 {t.rng:.2f} m（门限 {lim:.1f} m）"

        if rule.kind == "range_gt":
            if not alive:
                return False, 0.0, -1, ""
            t = max(alive, key=lambda x: x.rng)
            lim = float(p.get("m", 10.0))
            eff = lim * (1.0 - hyst) if active else lim
            return t.rng > eff, t.rng, t.tid, f"T{t.tid} 距离 {t.rng:.2f} m（门限 {lim:.1f} m）"

        if rule.kind == "speed_ge":
            if not alive:
                return False, 0.0, -1, ""
            t = max(alive, key=lambda x: x.speed)
            lim = float(p.get("mps", 2.0))
            eff = lim * (1.0 - hyst) if active else lim
            return t.speed >= eff, t.speed, t.tid, f"T{t.tid} 速度 {t.speed:.2f} m/s（门限 {lim:.1f}）"

        if rule.kind == "zone":
            cx, cy = float(p.get("cx", 0.0)), float(p.get("cy", 0.0))
            r = float(p.get("r", 1.0))
            r_eff = relax(r)
            best: tuple[float, int] | None = None
            for t in alive:
                d = hypot(t.x - cx, t.y - cy)
                if d <= r_eff and (best is None or d < best[0]):
                    best = (d, t.tid)
            if best is None:
                return False, 0.0, -1, ""
            return True, best[0], best[1], f"T{best[1]} 进入防区（距圆心 {best[0]:.2f} m，半径 {r:.1f} m）"

        if rule.kind == "poly_zone":
            pts = p.get("points") or []
            for t in alive:
                if point_in_polygon(t.x, t.y, pts):
                    return True, t.rng, t.tid, f"T{t.tid} 进入多边形防区（距离 {t.rng:.2f} m）"
            return False, 0.0, -1, ""

        if rule.kind == "lost":
            return len(tracks) == 0, float(len(tracks)), -1, "所有目标已丢失"

        if rule.kind == "offline":
            lim = float(p.get("s", 3.0))
            return silence_s > lim, silence_s, -1, f"已 {silence_s:.1f} 秒没有数据（门限 {lim:.0f} 秒）"

        return False, 0.0, -1, ""

    # ------------------------------------------------------------------ #
    def evaluate(self, tracks: list[Target], caps: Capabilities,
                 now: float | None = None, silence_s: float = 0.0) -> list[AlarmEvent]:
        """跑一遍所有规则，返回本次新产生的"触发/恢复"事件。

        silence_s 由调用方算好传进来（距上次收到数据的秒数）。
        不在引擎里读全局时间，是为了让这个类可以被单元测试。
        """
        now = time.monotonic() if now is None else now

        events: list[AlarmEvent] = []
        for rule in self.rules:
            st = self._state.setdefault(rule.id, _RuleState())
            if not rule.enabled:
                if st.active:
                    st.active = False
                continue

            hit, value, tid, text = self._measure(rule, tracks, caps, silence_s, st.active)

            # ---- 去抖：条件必须持续 hold 秒 ----
            if hit:
                if st.since is None:
                    st.since = now
            else:
                st.since = None

            sustained = hit and st.since is not None and (now - st.since) >= rule.hold
            if st.active and hit:
                # 已经在报，只要仍然满足（门限已含滞回）就延续，不必重新计时
                sustained = True

            if sustained and not st.active:
                if now - st.last_fire >= rule.cooldown:
                    st.active = True
                    st.last_fire = now
                    st.active_tid = tid
                    events.append(AlarmEvent(now, rule.id, rule.name, rule.level,
                                             text or rule.name, True, tid, value))
            elif not sustained and st.active:
                st.active = False
                st.active_tid = -1
                events.append(AlarmEvent(now, rule.id, rule.name, rule.level,
                                         f"{rule.name} 已恢复", False, tid, value))

        return events
