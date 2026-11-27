"""航迹处理：支持 GNN 匈牙利全局关联、M/N 起批确认与 3D Kalman / 轻量 IMM 机动滤波。

设计原则：
1. 纯 NumPy + 标准库实现，零 SciPy/Qt 依赖，保持 core 层轻量与可离线测试；
2. 保留 `alpha_beta` 兼容模式，默认启用 `gnn` 全局最优分配 + `kalman3d`（3D 状态含 z/vz
   与极坐标旋转到直角系的 2×2 误差协方差，用于 PPI 绘制真实不确定度置信椭圆）；
3. 支持 `imm3d`（匀速 CV + 高机动交互多模型），应对无人机大机动转弯不发散；
4. 丢失判据坚持用"秒"（missed <= max_missed_s）而非帧数，兼容低转速机械扫描雷达。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import atan2, cos, degrees, exp, hypot, pi, sin
from typing import Iterable

import numpy as np

from .model import Target


def linear_sum_assignment_min(cost: np.ndarray) -> tuple[list[int], list[int]]:
    """纯 NumPy O(n^3) 匈牙利（Jonker-Volgenant/Munkres 增广路）最小代价指派，无需 SciPy。"""
    n_rows, n_cols = cost.shape
    if n_rows == 0 or n_cols == 0:
        return [], []
    transposed = n_rows > n_cols
    a = cost.T if transposed else cost
    n, m = a.shape  # n <= m
    u = np.zeros(n + 1, dtype=np.float64)
    v = np.zeros(m + 1, dtype=np.float64)
    p = np.zeros(m + 1, dtype=np.int64)
    way = np.zeros(m + 1, dtype=np.int64)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = np.full(m + 1, np.inf, dtype=np.float64)
        used = np.zeros(m + 1, dtype=bool)
        row_i = a[i - 1]
        while True:
            used[j0] = True
            i0 = int(p[j0])
            delta = np.inf
            j1 = 0
            row_cur = a[i0 - 1] if i0 == i else a[i0 - 1]
            for j in range(1, m + 1):
                if not used[j]:
                    cur = float(row_cur[j - 1]) - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[int(p[j])] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = int(way[j0])
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    row_ind, col_ind = [], []
    for j in range(1, m + 1):
        if p[j] > 0:
            ri = int(p[j] - 1)
            ci = j - 1
            if transposed:
                row_ind.append(ci)
                col_ind.append(ri)
            else:
                row_ind.append(ri)
                col_ind.append(ci)
    return row_ind, col_ind


def _cartesian_meas_cov(x: float, y: float, sigma_r: float = 0.14, sigma_az_rad: float = 0.035) -> np.ndarray:
    """将极坐标量测噪声（距离方差与切向波束弧宽方差）旋转投影到笛卡尔 (x, y) 协方差。"""
    r = hypot(x, y)
    if r < 1e-4:
        return np.diag([sigma_r * sigma_r, sigma_r * sigma_r])
    # 远距雷达按千分之四距离自动放宽距离标准差，避免 20km 雷达仍套用 0.14m 造成协方差过紧
    eff_sr = max(sigma_r, 0.004 * r)
    ux, uy = x / r, y / r          # 径向单位向量
    vx, vy = uy, -ux               # 切向单位向量
    sc = max(eff_sr, r * sigma_az_rad)
    sr2 = eff_sr * eff_sr
    sc2 = sc * sc
    return np.array([
        [sr2 * ux * ux + sc2 * vx * vx, sr2 * ux * uy + sc2 * vx * vy],
        [sr2 * ux * uy + sc2 * vx * vy, sr2 * uy * uy + sc2 * vy * vy],
    ], dtype=np.float64)


@dataclass
class _KFBranch:
    """单模型 3D 运动分支（状态 [x, y, z, vx, vy, vz]，支持 CV 与 Coordinated-Turn 协同转弯）。"""

    x: np.ndarray     # (6,)
    P: np.ndarray     # (6, 6)
    q_acc: float      # 过程加速度噪声强度 (m/s^2)^2
    is_ct: bool = False
    omega: float = 0.0

    def predict(self, dt: float) -> None:
        F = np.eye(6, dtype=np.float64)
        F[2, 5] = dt
        w = float(self.omega) if self.is_ct else 0.0
        if self.is_ct and abs(w) > 1e-4:
            wt = w * dt
            sw, cw = sin(wt), cos(wt)
            F[0, 3] = sw / w
            F[0, 4] = -(1.0 - cw) / w
            F[1, 3] = (1.0 - cw) / w
            F[1, 4] = sw / w
            F[3, 3] = cw
            F[3, 4] = -sw
            F[4, 3] = sw
            F[4, 4] = cw
        else:
            F[0, 3] = dt
            F[1, 4] = dt
        dt2 = dt * dt
        dt3 = dt2 * dt * 0.5
        dt4 = dt2 * dt2 * 0.25
        Q = np.zeros((6, 6), dtype=np.float64)
        for p_idx, v_idx, q in ((0, 3, self.q_acc), (1, 4, self.q_acc), (2, 5, self.q_acc * 0.5)):
            Q[p_idx, p_idx] = dt4 * q
            Q[p_idx, v_idx] = dt3 * q
            Q[v_idx, p_idx] = dt3 * q
            Q[v_idx, v_idx] = dt2 * q
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q

    def update(self, meas_xyz: np.ndarray, R_xy: np.ndarray, r_z: float = 0.04,
               meas_vxy: tuple[float, float] | None = None, r_v: float = 1.5) -> float:
        """执行 3D 位置（及可选速度）量测更新并返回归一化似然（用于 IMM 模型概率更新）。"""
        if meas_vxy is not None:
            H = np.zeros((5, 6), dtype=np.float64)
            H[0, 0] = H[1, 1] = H[2, 2] = H[3, 3] = H[4, 4] = 1.0
            meas = np.array([meas_xyz[0], meas_xyz[1], meas_xyz[2], meas_vxy[0], meas_vxy[1]], dtype=np.float64)
            R = np.zeros((5, 5), dtype=np.float64)
            R[:2, :2] = R_xy
            R[2, 2] = r_z
            R[3, 3] = R[4, 4] = r_v
        else:
            H = np.zeros((3, 6), dtype=np.float64)
            H[0, 0] = 1.0
            H[1, 1] = 1.0
            H[2, 2] = 1.0
            meas = meas_xyz
            R = np.zeros((3, 3), dtype=np.float64)
            R[:2, :2] = R_xy
            R[2, 2] = r_z
        y_res = meas - H @ self.x
        S = H @ self.P @ H.T + R
        S = 0.5 * (S + S.T) + 1e-9 * np.eye(H.shape[0])
        try:
            S_inv = np.linalg.inv(S)
            det_s = max(1e-12, float(np.linalg.det(S)))
        except np.linalg.LinAlgError:
            return 1e-6
        K = self.P @ H.T @ S_inv
        self.x = self.x + K @ y_res
        I_KH = np.eye(6) - K @ H
        self.P = I_KH @ self.P @ I_KH.T + K @ R @ K.T
        maha2 = float(y_res.T @ S_inv @ y_res)
        return exp(-0.5 * min(40.0, max(0.0, maha2))) / (((2.0 * pi) ** (0.5 * H.shape[0])) * (det_s ** 0.5))


@dataclass
class _TrackInternal:
    """每个航迹背后的滤波器与 M/N 起批状态。"""

    branches: list[_KFBranch]
    probs: list[float]
    hits_hist: deque = field(default_factory=lambda: deque(maxlen=5))


class Tracker:
    FILTER_MODES = ("kalman3d", "imm3d", "alpha_beta")
    ASSOC_MODES = ("gnn", "greedy")
    GATING_MODES = ("euclidean", "mahalanobis")

    def __init__(
        self,
        gate_m: float = 1.5,
        max_missed_s: float = 2.5,
        alpha: float = 0.55,
        beta: float = 0.12,
        merge_m: float = 0.6,
        assoc_mode: str = "gnn",
        filter_mode: str = "kalman3d",
        confirm_hits: int = 1,
        confirm_window: int = 1,
        gating_mode: str = "euclidean",
        maha_gate: float = 4.5,
        use_vel_meas: bool = False,
    ) -> None:
        self.gate_m = gate_m
        self.max_missed_s = max_missed_s
        self.alpha = alpha
        self.beta = beta
        self.merge_m = merge_m
        self.assoc_mode = assoc_mode if assoc_mode in self.ASSOC_MODES else "gnn"
        self.filter_mode = filter_mode if filter_mode in self.FILTER_MODES else "kalman3d"
        self.gating_mode = gating_mode if gating_mode in self.GATING_MODES else "euclidean"
        self.maha_gate = max(1.5, float(maha_gate))
        self.use_vel_meas = bool(use_vel_meas)
        self.confirm_hits = max(1, int(confirm_hits))
        self.confirm_window = max(self.confirm_hits, int(confirm_window))
        self.tracks: list[Target] = []
        self._states: dict[int, _TrackInternal] = {}
        self._next_id = 1

    def configure(
        self,
        *,
        gate_m: float | None = None,
        assoc_mode: str | None = None,
        filter_mode: str | None = None,
        confirm_hits: int | None = None,
        confirm_window: int | None = None,
    ) -> None:
        if gate_m is not None:
            self.gate_m = max(0.3, float(gate_m))
        if assoc_mode in self.ASSOC_MODES:
            self.assoc_mode = assoc_mode
        if filter_mode in self.FILTER_MODES:
            self.filter_mode = filter_mode
        if confirm_hits is not None:
            self.confirm_hits = max(1, int(confirm_hits))
        if confirm_window is not None:
            self.confirm_window = max(self.confirm_hits, int(confirm_window))
        else:
            self.confirm_window = max(self.confirm_hits, self.confirm_window)

    def reset(self) -> None:
        self.tracks.clear()
        self._states.clear()
        self._next_id = 1

    def _make_internal(self, det: Target) -> _TrackInternal:
        x0 = np.array([det.x, det.y, det.z, det.vx, det.vy, det.vz], dtype=np.float64)
        R0 = _cartesian_meas_cov(det.x, det.y)
        r_dist = hypot(det.x, det.y)
        v_var = max(12.0, (0.006 * r_dist) ** 2)
        P0 = np.diag([R0[0, 0], R0[1, 1], max(0.16, (0.005 * r_dist) ** 2), v_var, v_var, 0.5])
        P0[0, 1] = P0[1, 0] = R0[0, 1]
        b_cv = _KFBranch(x=x0.copy(), P=P0.copy(), q_acc=0.25)
        b_man = _KFBranch(x=x0.copy(), P=P0.copy(), q_acc=3.8)
        b_ct = _KFBranch(x=x0.copy(), P=P0.copy(), q_acc=1.8, is_ct=True, omega=0.0)
        hist = deque([True], maxlen=self.confirm_window)
        return _TrackInternal(branches=[b_cv, b_man, b_ct], probs=[0.70, 0.18, 0.12], hits_hist=hist)

    def _predict_xy(self, trk: Target, dt: float) -> tuple[float, float]:
        return trk.x + trk.vx * dt, trk.y + trk.vy * dt

    def _advance_filter(self, trk: Target, st: _TrackInternal, dt: float) -> None:
        if self.filter_mode == "alpha_beta":
            return
        if self.filter_mode == "imm3d":
            # 3-模型交互混合：CV（匀速） / High-G（机动加速） / CT（协同转弯）
            trans = np.array([
                [0.90, 0.05, 0.05],
                [0.14, 0.78, 0.08],
                [0.12, 0.08, 0.80],
            ], dtype=np.float64)
            m_cnt = len(st.branches)
            probs = np.array(st.probs[:m_cnt], dtype=np.float64)
            c_bar = np.maximum(trans.T @ probs, 1e-6)
            mixed_x = []
            mixed_P = []
            for j in range(m_cnt):
                mu_ij = (trans[:, j] * probs) / c_bar[j]
                xj = sum(mu_ij[i] * st.branches[i].x for i in range(m_cnt))
                Pj = np.zeros((6, 6), dtype=np.float64)
                for i in range(m_cnt):
                    dx = (st.branches[i].x - xj).reshape(6, 1)
                    Pj += mu_ij[i] * (st.branches[i].P + dx @ dx.T)
                mixed_x.append(xj)
                mixed_P.append(Pj)
            for j in range(m_cnt):
                st.branches[j].x = mixed_x[j]
                st.branches[j].P = mixed_P[j]
                st.branches[j].predict(dt)
            st.probs = (c_bar / c_bar.sum()).tolist()
        else:
            st.branches[0].predict(dt)

    def _sync_track_from_state(self, trk: Target, st: _TrackInternal) -> None:
        if self.filter_mode == "alpha_beta":
            R0 = _cartesian_meas_cov(trk.x, trk.y)
            trk.cov_x, trk.cov_y, trk.cov_xy = float(R0[0, 0]), float(R0[1, 1]), float(R0[0, 1])
            return
        if self.filter_mode == "imm3d":
            m_cnt = len(st.branches)
            probs = st.probs[:m_cnt]
            x_est = sum(probs[i] * st.branches[i].x for i in range(m_cnt))
            P_est = np.zeros((6, 6), dtype=np.float64)
            for i in range(m_cnt):
                di = (st.branches[i].x - x_est).reshape(6, 1)
                P_est += probs[i] * (st.branches[i].P + di @ di.T)
        else:
            x_est = st.branches[0].x
            P_est = st.branches[0].P
        trk.x, trk.y, trk.z = float(x_est[0]), float(x_est[1]), float(x_est[2])
        trk.vx, trk.vy, trk.vz = float(x_est[3]), float(x_est[4]), float(x_est[5])
        trk.cov_x = max(1e-4, float(P_est[0, 0]))
        trk.cov_y = max(1e-4, float(P_est[1, 1]))
        trk.cov_xy = float(P_est[0, 1])
        r2d = hypot(trk.x, trk.y)
        if r2d > 1e-4 and abs(trk.z) > 1e-4:
            trk.el_deg = degrees(atan2(trk.z, r2d))

    def _gate_cost(self, trk: Target, px: float, py: float, det: Target) -> float | None:
        d = hypot(px - det.x, py - det.y)
        if self.gating_mode == "mahalanobis" and self.filter_mode != "alpha_beta" and trk.age > 3:
            if d > self.gate_m * 1.8:
                return None
            R_xy = _cartesian_meas_cov(det.x, det.y)
            S00 = max(1e-4, float(trk.cov_x) + float(R_xy[0, 0]))
            S11 = max(1e-4, float(trk.cov_y) + float(R_xy[1, 1]))
            S01 = float(trk.cov_xy) + float(R_xy[0, 1])
            det_s = S00 * S11 - S01 * S01
            if det_s <= 1e-9:
                return d if d <= self.gate_m else None
            dx, dy = det.x - px, det.y - py
            maha = (S11 * dx * dx - 2.0 * S01 * dx * dy + S00 * dy * dy) / det_s
            if maha > self.maha_gate * self.maha_gate:
                return None
            return float(maha ** 0.5)
        return d if d <= self.gate_m else None

    def _associate(self, detections: list[Target], dt: float) -> list[tuple[int, int]]:
        nt, nd = len(self.tracks), len(detections)
        if nt == 0 or nd == 0:
            return []

        # kalman3d / imm3d 在 _advance_filter 中已前推 dt 并同步到 t.x/t.y，不可二次外推 2*dt
        if self.filter_mode == "alpha_beta":
            preds = [self._predict_xy(t, dt) for t in self.tracks]
        else:
            preds = [(t.x, t.y) for t in self.tracks]
        if self.assoc_mode == "greedy":
            pairs = []
            for ti, (px, py) in enumerate(preds):
                trk = self.tracks[ti]
                for di, det in enumerate(detections):
                    c_val = self._gate_cost(trk, px, py, det)
                    if c_val is not None:
                        pairs.append((c_val, ti, di))
            pairs.sort(key=lambda p: p[0])
            used_t, used_d, out = set(), set(), []
            for _d, ti, di in pairs:
                if ti not in used_t and di not in used_d:
                    used_t.add(ti)
                    used_d.add(di)
                    out.append((ti, di))
            return out

        # GNN 匈牙利全局最小代价匹配；门限外代价设为 BIG 并在分配后剔除
        big = 1e6
        cost = np.full((nt, nd), big, dtype=np.float64)
        for ti, (px, py) in enumerate(preds):
            trk = self.tracks[ti]
            # 对已连续命中的稳定航迹略微放宽（降低 10% 代价偏好），减少新杂波抢关联
            bonus = 0.90 if not trk.tentative and trk.age >= 3 else 1.0
            for di, det in enumerate(detections):
                c_val = self._gate_cost(trk, px, py, det)
                if c_val is not None:
                    cost[ti, di] = c_val * bonus
        rows, cols = linear_sum_assignment_min(cost)
        return [(ti, di) for ti, di in zip(rows, cols) if cost[ti, di] < big * 0.5]

    def update(self, detections: list[Target], dt: float, locked_id: int = -1) -> list[Target]:
        # 放宽到 10s 以支持 24 RPM（2.5s/圈）等慢速机械扫描雷达的跨圈预测
        dt = max(1e-3, min(dt, 10.0))

        for t in self.tracks:
            t.age += 1
            t.missed += dt
            st = self._states.get(t.tid)
            if st is None:
                st = self._make_internal(t)
                self._states[t.tid] = st
            self._advance_filter(t, st, dt)
            if self.filter_mode != "alpha_beta":
                self._sync_track_from_state(t, st)

        matches = self._associate(detections, dt)
        used_t = {ti for ti, _ in matches}
        used_d = {di for _, di in matches}

        for ti, trk in enumerate(self.tracks):
            if ti not in used_t:
                st = self._states.get(trk.tid)
                if st is not None:
                    if st.hits_hist.maxlen != self.confirm_window:
                        st.hits_hist = deque(st.hits_hist, maxlen=self.confirm_window)
                    st.hits_hist.append(False)

        for ti, di in matches:
            trk, det = self.tracks[ti], detections[di]
            st = self._states[trk.tid]
            if self.filter_mode == "alpha_beta":
                px, py = self._predict_xy(trk, dt)
                rx, ry = det.x - px, det.y - py
                trk.x, trk.y = px + self.alpha * rx, py + self.alpha * ry
                trk.vx += self.beta * rx / dt
                trk.vy += self.beta * ry / dt
                trk.z = det.z
                trk.el_deg = det.el_deg
                self._sync_track_from_state(trk, st)
            else:
                meas = np.array([det.x, det.y, det.z], dtype=np.float64)
                R_xy = _cartesian_meas_cov(det.x, det.y)
                r_z = max(0.04, (0.005 * det.rng) ** 2)
                vxy = (det.vx, det.vy) if (self.use_vel_meas and (abs(det.vx) > 1e-4 or abs(det.vy) > 1e-4)) else None
                prev_vx, prev_vy = trk.vx, trk.vy
                if self.filter_mode == "imm3d":
                    lik = np.array([
                        b.update(meas, R_xy, r_z=r_z, meas_vxy=vxy)
                        for b in st.branches
                    ], dtype=np.float64)
                    post = np.array(st.probs[:len(st.branches)], dtype=np.float64) * np.maximum(lik, 1e-12)
                    s = float(post.sum())
                    st.probs = (post / s).tolist() if s > 0 else [1.0 / len(st.branches)] * len(st.branches)
                    # 根据速度方向变化平滑更新协同转弯分支的瞬时角速率 ω
                    if len(st.branches) >= 3 and st.branches[2].is_ct:
                        sp_prev = hypot(prev_vx, prev_vy)
                        sp_now = hypot(st.branches[2].x[3], st.branches[2].x[4])
                        if sp_prev > 0.5 and sp_now > 0.5 and dt > 1e-3:
                            d_ang = atan2(
                                prev_vx * st.branches[2].x[4] - prev_vy * st.branches[2].x[3],
                                prev_vx * st.branches[2].x[3] + prev_vy * st.branches[2].x[4],
                            )
                            w_est = max(-1.5, min(1.5, d_ang / dt))
                            st.branches[2].omega = 0.65 * st.branches[2].omega + 0.35 * w_est
                else:
                    st.branches[0].update(meas, R_xy, r_z=r_z, meas_vxy=vxy)
                self._sync_track_from_state(trk, st)
                if det.el_deg and abs(trk.z) < 1e-4:
                    trk.el_deg = det.el_deg

            trk.snr = det.snr
            trk.threat = det.threat
            trk.cls = det.cls or trk.cls
            trk.rcs = det.rcs or trk.rcs
            trk.views = det.views
            trk.missed = 0.0

            if st.hits_hist.maxlen != self.confirm_window:
                st.hits_hist = deque(st.hits_hist, maxlen=self.confirm_window)
            st.hits_hist.append(True)
            if sum(1 for h in st.hits_hist if h) >= self.confirm_hits:
                trk.tentative = False

        # ---- 新目标起批（遵循 M/N 确认门限；confirm_hits==1 时首帧即正式建轨） ----
        for di, det in enumerate(detections):
            if di in used_d:
                continue
            det.tid = self._next_id
            self._next_id += 1
            det.age = 1
            det.missed = 0.0
            det.tentative = self.confirm_hits > 1
            st = self._make_internal(det)
            self._states[det.tid] = st
            self._sync_track_from_state(det, st)
            self.tracks.append(det)

        # ---- 超时淘汰（未确认的试探点更早清退，避免杂波积压） ----
        kept_alive: list[Target] = []
        for t in self.tracks:
            ttl = self.max_missed_s * (0.6 if t.tentative else 1.0)
            if t.missed <= ttl:
                kept_alive.append(t)
            else:
                self._states.pop(t.tid, None)
        self.tracks = kept_alive

        # ---- 合并重合航迹（优先保留已确认/更老的航迹） ----
        if len(self.tracks) > 1:
            kept: list[Target] = []
            for t in sorted(self.tracks, key=lambda x: (x.tentative, x.missed, -x.age)):
                if all(hypot(t.x - k.x, t.y - k.y) > self.merge_m for k in kept):
                    kept.append(t)
                else:
                    self._states.pop(t.tid, None)
            self.tracks = kept

        for t in self.tracks:
            t.locked = (t.tid == locked_id)

        return list(self.tracks)
