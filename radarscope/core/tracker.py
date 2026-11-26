"""航迹处理：最近邻关联 + alpha-beta 滤波。

两个设计决定值得说明：

1. 用 alpha-beta 而不是卡尔曼。目的不是发导弹，是让屏幕上的框别抖。
   10Hz、0.1m 级量测噪声下两者效果几乎一致，但这里只有三十行，你随时能改。

2. 丢失判据用"秒"而不是"帧"。旋转扫描雷达一圈可能 1.5 秒才照到目标一次，
   如果按帧数计数，目标每转一圈就被删一次航迹，屏幕上永远建不起稳定航迹。
   真实雷达的航迹文件本来也是按秒设保持时间的。
"""
from __future__ import annotations

from .model import Target


class Tracker:
    def __init__(self, gate_m: float = 1.5, max_missed_s: float = 2.5,
                 alpha: float = 0.55, beta: float = 0.12, merge_m: float = 0.6):
        self.gate_m = gate_m
        self.max_missed_s = max_missed_s
        self.alpha = alpha
        self.beta = beta
        self.merge_m = merge_m
        self.tracks: list[Target] = []
        self._next_id = 1

    def reset(self) -> None:
        self.tracks.clear()
        self._next_id = 1

    @staticmethod
    def _predict(t: Target, dt: float) -> tuple[float, float]:
        return t.x + t.vx * dt, t.y + t.vy * dt

    def update(self, detections: list[Target], dt: float, locked_id: int = -1) -> list[Target]:
        dt = max(1e-3, min(dt, 1.0))

        for t in self.tracks:
            t.age += 1
            t.missed += dt

        # ---- 关联：贪心最小距离，够近才连 ----
        pairs = []
        for ti, trk in enumerate(self.tracks):
            px, py = self._predict(trk, dt)
            for di, det in enumerate(detections):
                d = ((px - det.x) ** 2 + (py - det.y) ** 2) ** 0.5
                if d <= self.gate_m:
                    pairs.append((d, ti, di))
        pairs.sort(key=lambda p: p[0])

        used_t, used_d = set(), set()
        for _d, ti, di in pairs:
            if ti in used_t or di in used_d:
                continue
            trk, det = self.tracks[ti], detections[di]
            px, py = self._predict(trk, dt)
            rx, ry = det.x - px, det.y - py
            trk.x, trk.y = px + self.alpha * rx, py + self.alpha * ry
            trk.vx += self.beta * rx / dt
            trk.vy += self.beta * ry / dt
            trk.snr = det.snr
            # 高端雷达会直接给出分类/威胁/RCS，别在滤波时丢掉
            trk.threat = det.threat
            trk.cls = det.cls or trk.cls
            trk.rcs = det.rcs or trk.rcs
            trk.views = det.views
            trk.z = det.z
            trk.el_deg = det.el_deg
            trk.missed = 0.0
            used_t.add(ti)
            used_d.add(di)

        # ---- 新目标起航迹 ----
        for di, det in enumerate(detections):
            if di in used_d:
                continue
            det.tid = self._next_id
            self._next_id += 1
            det.age = 1
            det.missed = 0.0
            self.tracks.append(det)

        # ---- 超过保持时间删航迹 ----
        self.tracks = [t for t in self.tracks if t.missed <= self.max_missed_s]

        # ---- 合并重合航迹 ----
        # 同一个目标在两次扫描间被检到略有不同的位置时，容易分裂出两条航迹，
        # 屏幕上就变成两个框叠在一起、标签糊成一团。按"老的优先"合并掉。
        if len(self.tracks) > 1:
            kept: list[Target] = []
            for t in sorted(self.tracks, key=lambda x: (x.missed, -x.age)):
                if all(((t.x - k.x) ** 2 + (t.y - k.y) ** 2) ** 0.5 > self.merge_m
                       for k in kept):
                    kept.append(t)
            self.tracks = kept

        for t in self.tracks:
            t.locked = (t.tid == locked_id)

        return list(self.tracks)
