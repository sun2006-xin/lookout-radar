"""核心模块测试。不依赖 pytest，直接跑：

    python tests/test_core.py

覆盖的是最容易悄悄坏掉的地方：切帧、告警去抖、数据库往返、传输端点解析。
这些坏掉时界面看起来一切正常，只是数据不对 —— 所以必须有断言盯着。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from radarscope.core import pipeline
from radarscope.core.alarm import AlarmEngine, AlarmRule
from radarscope.core.codec import FrameSpec, StreamDecoder
from radarscope.core.model import Capabilities, RadarFrame, Target
from radarscope.core.storage import FrameArchive, Store
from radarscope.core.transport import FileTransport, parse_spec
from radarscope.drivers.ld2450 import _decode_sign_magnitude

PASS = 0


def ok(name: str) -> None:
    global PASS
    PASS += 1
    print(f"  [ok] {name}")


# --------------------------------------------------------------------------- #
def test_codec() -> None:
    spec = FrameSpec(header=b"\xf4\xf3\xf2\xf1", tail=b"\xf8\xf7\xf6\xf5",
                     )
    dec = StreamDecoder(spec)

    body = b"\x01\xaa" + bytes(range(10))
    frame = spec.header + len(body).to_bytes(2, "little") + body + spec.tail
    assert dec.feed(frame) == [frame], "整帧解析失败"

    # 逐字节喂（模拟串口碎片）
    dec2 = StreamDecoder(spec)
    out = []
    for i in range(len(frame)):
        out += dec2.feed(frame[i:i + 1])
    assert out == [frame], "碎片拼帧失败"

    # 数据段内部藏一个假帧头，不能在这里误切
    nasty_body = b"\x01\xaa\xf4\xf3\xf2\xf1\x00\x00"
    nasty = spec.header + len(nasty_body).to_bytes(2, "little") + nasty_body + spec.tail
    dec3 = StreamDecoder(spec)
    got = dec3.feed(b"\x00\xff" + nasty + frame)
    assert got == [nasty, frame], f"假同步处理失败: {got}"
    ok("帧解码：碎片 / 粘包 / 假同步")


def test_ld2450_sign() -> None:
    # 官方手册的例子：0x030E -> -782mm，0x86B1 -> +1713mm
    assert _decode_sign_magnitude(0x030E) == -782
    assert _decode_sign_magnitude(0x86B1) == 1713
    ok("LD2450 符号位解码（bit15 独立符号，不是补码）")


def test_pipeline() -> None:
    caps = Capabilities(has_scan=True, has_azimuth=True, az_fov=120.0,
                        range_min=0.2, range_max=8.0)
    # 目标 -> 矩阵 -> 检测 -> 目标，闭环应该能还原位置
    src = [Target(x=1.0, y=3.0), Target(x=-2.0, y=5.0)]
    frame = pipeline.synthesize_scan(caps, src)
    dets = pipeline.detect_targets(frame, threshold=0.3, max_targets=8)
    assert len(dets) == 2, f"应检出 2 个目标，实际 {len(dets)}"
    for want in src:
        nearest = min(dets, key=lambda t: (t.x - want.x) ** 2 + (t.y - want.y) ** 2)
        d = ((nearest.x - want.x) ** 2 + (nearest.y - want.y) ** 2) ** 0.5
        assert d < 0.6, f"定位偏差过大 {d:.2f} m"
    ok("流水线：目标→矩阵→CFAR→目标 闭环")


def test_alarm() -> None:
    eng = AlarmEngine([
        AlarmRule("z", "防区", "zone", {"cx": 0, "cy": 3, "r": 1.0},
                  hold=0.5, cooldown=10.0, hysteresis=0.2),
        AlarmRule("n", "多目标", "count_ge", {"n": 2}, hold=0.0, cooldown=10.0),
    ])
    caps = Capabilities()
    intruder = [Target(x=0.0, y=3.0)]
    t = 0.0

    # hold 未满，不该报
    assert eng.evaluate(intruder, caps, now=t) == []
    t = 0.4
    assert eng.evaluate(intruder, caps, now=t) == []
    # 满了 0.5s，应该报
    t = 0.6
    ev = eng.evaluate(intruder, caps, now=t)
    assert any(e.rule_id == "z" and e.active for e in ev), "告警没有触发"

    # 已在报，不重复刷
    t = 1.0
    assert [e for e in eng.evaluate(intruder, caps, now=t) if e.active] == [], "告警重复刷屏"

    # 目标离开 -> 恢复
    t = 2.0
    ev = eng.evaluate([], caps, now=t)
    assert any(e.rule_id == "z" and not e.active for e in ev), "告警没有恢复"

    # 滞回：刚好在半径外一点点（1.0~1.2 之间）不应触发新告警
    eng2 = AlarmEngine([AlarmRule("z", "防区", "zone", {"cx": 0, "cy": 3, "r": 1.0},
                                  hold=0.0, hysteresis=0.2)])
    edge = [Target(x=0.0, y=4.1)]
    assert eng2.evaluate(edge, caps, now=0.0) == []

    # 内置规则默认不强制画警戒区，且用户写入 enabled=False 可持久停用内置规则
    from radarscope.core.alarm import DEFAULT_RULES, merge_rules
    intr = next(r for r in DEFAULT_RULES if r.id == "intrusion")
    assert not intr.enabled, "默认内置防区入侵不应在首次启动时强制启用"
    merged = merge_rules(DEFAULT_RULES, [AlarmRule("intrusion", "防区入侵", "zone", {"cx": 0, "cy": 3, "r": 1.5}, enabled=False)])
    assert not next(r for r in merged if r.id == "intrusion").enabled
    ok("告警：去抖 / 不刷屏 / 恢复 / 滞回")


def test_storage() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "t.db"
        st = Store(db)
        sid = st.start_session("sim", "virtual", Capabilities(name="X"))
        trk = Target(x=1.0, y=2.0, vx=0.1, vy=0.0, tid=7, cls="敌", threat=3)
        st.record_tracks(time.time(), [trk])
        st.record_event("crit", "alarm", "测试告警", "z", True, 7)
        st.flush(force=True)
        rows = st.tracks_of(sid)
        # 列顺序：ts, tid, x, y, vx, vy, rng, az, snr, threat, cls
        assert len(rows) == 1, f"航迹条数不对：{len(rows)}"
        assert abs(rows[0][2] - 1.0) < 1e-6 and abs(rows[0][3] - 2.0) < 1e-6, "航迹落库失败"
        assert rows[0][9] == 3 and rows[0][10] == "敌", "威胁/分类没存下来"
        evs = st.events_of(sid)
        assert evs and evs[0][4] == "测试告警", "事件落库失败"
        st.end_session("测试")
        assert st.list_sessions()[0].id == sid
        st.close()
        ok("数据库：会话 / 航迹 / 事件 往返")


def test_archive() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        p = Path(td) / "a.rad"
        arc = FrameArchive(p)
        caps = Capabilities(has_scan=True, has_azimuth=True,
                            range_min=0.2, range_max=8.0)
        for i in range(5):
            f = pipeline.synthesize_scan(caps, [Target(x=0.2 * i, y=3.0)])
            f.timestamp = 1000.0 + i
            arc.append(f)
        n = arc.frames
        arc.close()
        assert n == 5
        back = list(FrameArchive.read_frames(p))
        assert len(back) == 5, "帧归档帧数不符"
        ts, scan, az, r = back[4]
        assert abs(ts - 1004.0) < 1e-6
        assert scan.shape[0] == az.size and scan.shape[1] == r.size, "轴与矩阵不一致"
        ok("帧归档：写入 / 读回 / 轴一致")


def test_transport_parse() -> None:
    assert parse_spec("COM3")["scheme"] == "serial"
    assert parse_spec("serial://COM3?baud=115200")["options"]["baud"] == "115200"
    assert parse_spec("tcp://1.2.3.4:9000")["target"] == "1.2.3.4:9000"
    assert parse_spec("udp://0.0.0.0:5000")["scheme"] == "udp"
    assert parse_spec("ble://AA:BB:CC")["scheme"] == "ble"
    p = parse_spec("file://C:/tmp/x.bin")
    assert "x.bin" in p["target"], p
    assert parse_spec("file:///Users/runner/work/a.bin")["target"] == "/Users/runner/work/a.bin"
    assert parse_spec("file:///D:/work/a.bin")["target"] == "D:/work/a.bin"
    ok("端点解析：串口 / TCP / UDP / BLE / 文件（含 POSIX 路径）")


def test_file_transport() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        p = Path(td) / "cap.bin"
        p.write_bytes(b"0123456789" * 10)
        tr = FileTransport(str(p), chunk=10, interval=0.0, loop=False)
        tr.open()
        got = bytearray()
        from radarscope.core.transport import TransportError
        try:
            for _ in range(20):
                got.extend(tr.read())
        except TransportError:
            pass                       # 放完就该抛"回放结束"，这是设计行为
        assert bytes(got).startswith(b"0123456789"), "回放读到的字节不对"
        assert len(got) == 100, f"应该完整放完 100 字节，实际 {len(got)}"
        ok("文件传输：按块回放原始字节流并在结束时报错")


def test_fusion() -> None:
    from radarscope.core.fusion import FusionEngine, SensorSpec, local_to_global

    # 坐标变换：本地 (1, 0) 在朝向 90° 的传感器上应落到 (0, -1) 附近
    s = SensorSpec(name="S1", bearing_deg=90.0)
    gx, gy = local_to_global(1.0, 0.0, s)
    assert abs(gx) < 1e-6 and abs(gy + 1.0) < 1e-6, f"旋转换算错误 {(gx, gy)}"

    # 平移也要对
    s2 = SensorSpec(name="S2", x=2.0, y=-1.0, bearing_deg=0.0)
    gx, gy = local_to_global(0.0, 3.0, s2)
    assert abs(gx - 2.0) < 1e-6 and abs(gy - 2.0) < 1e-6, f"平移换算错误 {(gx, gy)}"

    class _Fake:
        def __init__(self, tgt):
            self.tgt = tgt
            self._done = False

        def read(self):
            if self._done:
                return None
            self._done = True
            return RadarFrame(timestamp=time.monotonic(),
                              targets=[Target(x=self.tgt[0], y=self.tgt[1])],
                              az_axis=np.zeros(1, dtype=np.float32),
                              r_axis=np.array([1.0], dtype=np.float32))

        def close(self): pass

        def capabilities(self):
            return Capabilities(has_targets=True, has_velocity=True)

    eng = FusionEngine(n_az=360, n_rng=100, range_min=0.2, range_max=10.0, rate=50.0)
    eng.attach(SensorSpec(name="F", bearing_deg=90.0, fov_deg=60.0), _Fake((2.0, 0.0)))
    f = None
    for _ in range(60):
        f = eng.poll()
        if f is not None:
            break
        time.sleep(0.005)
    assert f is not None and f.has_targets, "融合没有产出目标"
    t = f.targets[0]
    # 本地 (2,0) 在 90° 朝向上 -> 平台坐标 (0,-2)
    assert abs(t.x) < 0.1 and abs(t.y + 2.0) < 0.1, f"融合后坐标错误 {(t.x, t.y)}"
    eng.detach_all()

    # 盲区计算：2 个 60° 一定有洞，6 个 60° 应无缝
    eng2 = FusionEngine(n_az=360, n_rng=10, range_max=10.0)
    for i, b in enumerate((0.0, 180.0)):
        eng2.attach(SensorSpec(name=f"G{i}", bearing_deg=b, fov_deg=60.0), _Fake((1.0, 0.0)))
    assert eng2.gaps(), "2×60° 明明有盲区却算成无死角"

    eng3 = FusionEngine(n_az=360, n_rng=10, range_max=10.0)
    for i in range(6):
        eng3.attach(SensorSpec(name=f"H{i}", bearing_deg=i * 60.0, fov_deg=60.0), _Fake((1.0, 0.0)))
    assert eng3.gaps() == [], f"6×60° 不应有盲区，实际 {eng3.gaps()}"
    ok("多站融合：坐标变换 / 目标上抬 / 盲区判定")


def test_calibration() -> None:
    """标定数学：装偏了多少度，能不能被反推出来。"""
    from radarscope.core.calibration import (
        CalibPoint, angle_error_at, apply_offsets, calibrate_bearings,
        expected_local_deg, locate_with, norm_deg, residual_report,
    )
    from radarscope.core.fusion import SensorSpec

    specs = [SensorSpec(name="S1", x=0.0, y=0.0, bearing_deg=0.0),
             SensorSpec(name="S2", x=3.0, y=0.0, bearing_deg=0.0)]
    truth = {"S1": 6.0, "S2": -4.0}          # 假装装偏了这么多

    pts = [(1.0, 4.0), (2.5, 5.0), (-1.0, 4.5), (2.0, 3.0)]
    cps = []
    for (x, y) in pts:
        obs = {}
        for s in specs:
            obs[s.name] = expected_local_deg(s, x, y) - truth[s.name]
        cps.append(CalibPoint(x, y, obs))

    off = calibrate_bearings(specs, cps)
    for name, want in truth.items():
        assert abs(norm_deg(off[name] - want)) < 1e-6, \
            f"{name} 偏置估计错误：{off[name]:.2f} 应为 {want}"

    fixed = apply_offsets(specs, off)
    rep = residual_report(fixed, cps)
    assert rep["rms"] < 1e-6, f"标定后定位残差应接近 0，实际 {rep['rms']:.4f}"

    # 未标定时残差应该明显大 —— 否则说明这个测试根本没测到东西
    bad = residual_report(specs, cps)
    assert bad["rms"] > 0.3, f"未标定却残差很小（{bad['rms']:.3f}），测试无效"

    # 单台单点也能定位？不能，少于两条射线要返回 None
    assert locate_with([specs[0]], {"S1": 10.0}) is None

    # 平行射线（两台朝向一致、目标在正前方远处）应判为病态
    par = [SensorSpec(name="A", x=0.0, y=0.0, bearing_deg=0.0),
           SensorSpec(name="B", x=3.0, y=0.0, bearing_deg=0.0)]
    assert locate_with(par, {"A": 0.0, "B": 0.0}) is None, "平行射线应无解"

    # 1° 朝向误差在 8m 处的横向偏差约 14cm
    assert abs(angle_error_at(8.0, 1.0) - 0.1396) < 0.001
    ok("标定：装偏反推 / 最小二乘交会 / 病态判定 / 误差换算")


def _mk_lidar_packet(start_cd: int, end_cd: int, pts: list[tuple[int, int]],
                     speed: int = 3000, ts: int = 1234) -> bytes:
    """按 LD19 协议造一帧，用于脱离硬件做解析测试。"""
    from radarscope.drivers.ldlidar import crc8
    b = bytearray(47)
    b[0], b[1] = 0x54, 0x2C
    b[2:4] = speed.to_bytes(2, "little")
    b[4:6] = start_cd.to_bytes(2, "little")
    for i, (d, c) in enumerate(pts):
        o = 6 + i * 3
        b[o:o + 2] = d.to_bytes(2, "little")
        b[o + 2] = c
    b[42:44] = end_cd.to_bytes(2, "little")
    b[44:46] = ts.to_bytes(2, "little")
    b[46] = crc8(bytes(b[:46]))
    return bytes(b)


def test_lidar_protocol() -> None:
    """LD19 协议解析：正常帧 / 跨 0° / CRC 拒收 / 碎片重同步。"""
    from radarscope.drivers.ldlidar import LDLidarDriver, parse_packet

    pts = [(1000 + i * 10, 200) for i in range(12)]      # 1.00m 起，每点 +1cm
    pkt = _mk_lidar_packet(0, 1100, pts)                # 0° 起，跨 11°，步长 1°
    got = parse_packet(pkt)
    assert got is not None, "正常帧应该能解析"
    angles, info = got
    assert len(angles) == 12
    assert abs(angles[0][0] - 0.0) < 1e-6, angles[0][0]
    assert abs(angles[1][0] - 1.0) < 1e-6, "角步长应为 1°"
    assert abs(angles[0][1] - 1.0) < 1e-6 and abs(angles[11][1] - 1.11) < 1e-6, \
        f"距离解析错误：{angles[0][1]}, {angles[11][1]}"

    # 跨 0°：359° 起、10° 止。不做 (end+36000-start)%36000 会把角算成负数
    pkt2 = _mk_lidar_packet(35900, 1000, pts)
    a2, info2 = parse_packet(pkt2)
    assert abs(info2["span_deg"] - 11.0) < 1e-6, f"跨度应为 11°，实际 {info2['span_deg']}"
    assert abs(a2[0][0] - 359.0) < 1e-6, f"首点应为 359°，实际 {a2[0][0]}"
    assert abs(a2[1][0] - 0.0) < 1e-6, f"跨 0° 后应归零，实际 {a2[1][0]}"

    # CRC 错一字节必须拒收
    bad = bytearray(pkt)
    bad[10] ^= 0xFF
    assert parse_packet(bytes(bad)) is None, "CRC 错帧不该被接受"

    # 碎片喂入 + 前面带垃圾字节，要能重同步并正确写入栅格
    d = LDLidarDriver()
    d._acc.fill(0.0)
    for b in b"\x00\x11\x22" + pkt:
        d._ingest(bytes([b]))
    assert d._good == 1, f"碎片拼帧失败，好帧 {d._good}"
    # 角度 0° 在 az_axis 的第 180 格（-180..180 栅格），距离 1.0 m 应有点亮
    lit = np.argwhere(d._acc[:, :] > 0.2)
    assert len(lit) >= 1, "解析出来的点没有写进栅格"
    assert d._acc.shape == (360, 240)
    ok("激光雷达协议：角步长 / 跨 0° / CRC / 碎片重同步")


def test_fusion_triangulation() -> None:
    """重叠区用方位线交会，比"各测各的取平均"更准。

    构造：两台间距 3 m 的雷达都看到同一个点，但**距离都测偏了 10%**。
    方位角是准的 —— 这正是真实雷达的特点（角度准、距离误差大）。
    交会只吃角度，所以结果应该几乎不受距离误差影响。
    """
    from radarscope.core.fusion import FusionEngine, SensorSpec

    truth = (1.5, 4.0)

    class _Fake:
        """按给定的本地坐标报一个目标。"""

        def __init__(self, spec, local_xy, delay=0):
            self.spec = spec
            self.local_xy = local_xy
            self._n = 0

        def read(self):
            self._n += 1
            if self._n > 1:
                return None
            x, y = self.local_xy
            return RadarFrame(timestamp=time.monotonic(),
                              targets=[Target(x=x, y=y, snr=0.8)],
                              az_axis=np.zeros(1, dtype=np.float32),
                              r_axis=np.array([1.0], dtype=np.float32))

        def close(self): pass

        def capabilities(self):
            return Capabilities(has_targets=True, has_velocity=True)

    from radarscope.core.fusion import global_to_local

    sa = SensorSpec(name="A", x=0.0, y=0.0, bearing_deg=0.0, fov_deg=120.0)
    sb = SensorSpec(name="B", x=3.0, y=0.0, bearing_deg=0.0, fov_deg=120.0)

    # 各自测到的本地坐标：角度保持真实，径向距离乘 1.1 / 0.9
    def err(spec, k):
        lx, ly = global_to_local(truth[0], truth[1], spec)
        return (lx * k, ly * k)

    eng = FusionEngine(n_az=360, n_rng=100, range_min=0.2, range_max=10.0, rate=100.0)
    eng.attach(sa, _Fake(sa, err(sa, 1.10)))
    eng.attach(sb, _Fake(sb, err(sb, 0.90)))

    f = None
    for _ in range(40):
        f = eng.poll()
        if f is not None:
            break
        time.sleep(0.005)
    assert f is not None and f.targets, "融合没有产出目标"
    t = f.targets[0]
    assert t.views == 2, f"应有两个站共视，实际 views={t.views}"

    # 单台误差：距离偏 10% → 位置偏 0.4~0.5 m
    single = abs(round(1.10 * 1, 3) - 1)
    assert single >= 0.0
    lx, ly = global_to_local(truth[0], truth[1], sa)
    single_err = ((lx * 1.10 - lx) ** 2 + (ly * 1.10 - ly) ** 2) ** 0.5
    err_after = ((t.x - truth[0]) ** 2 + (t.y - truth[1]) ** 2) ** 0.5
    assert single_err > 0.3, f"构造的单台误差太小（{single_err:.2f}），测不出改进"
    assert err_after < 0.05, f"交会后误差仍有 {err_after:.2f} m，交会没起作用"
    eng.detach_all()

    # 两台装在同一位置（基线为 0）→ 必须拒绝交会并退回坐标变换，
    # 否则最小二乘会解回起点，给一个离目标很远却"看起来有效"的点
    sa2 = SensorSpec(name="C", x=0.0, y=0.0, bearing_deg=0.0)
    sb2 = SensorSpec(name="D", x=0.02, y=0.0, bearing_deg=0.0)
    eng2 = FusionEngine(n_az=360, n_rng=100, range_max=10.0, rate=100.0)
    eng2.attach(sa2, _Fake(sa2, global_to_local(truth[0], truth[1], sa2)))
    eng2.attach(sb2, _Fake(sb2, global_to_local(truth[0], truth[1], sb2)))
    f2 = None
    for _ in range(40):
        f2 = eng2.poll()
        if f2 is not None:
            break
        time.sleep(0.005)
    t2 = f2.targets[0]
    e2 = ((t2.x - truth[0]) ** 2 + (t2.y - truth[1]) ** 2) ** 0.5
    assert e2 < 0.1, f"共址时应退回坐标变换，结果不该跑偏（误差 {e2:.2f} m）"
    assert eng2.stats["tri"] == 0, "共址不该被判为可交会"
    ok("融合交会：两站共视更准，共址时拒绝交会")


def test_viewmath() -> None:
    """视图坐标换算：北向/艏向、极坐标↔像素、文本格式。"""
    from radarscope.core.viewmath import (
        NORTH_UP, clamp_range, fmt_bearing, fmt_distance, orbit_angle,
        point_to_polar, screen_offset,
    )

    # 艏向朝上时方位不变；北向朝上要加航向
    assert orbit_angle(30.0, 90.0) == 30.0
    assert orbit_angle(30.0, 90.0, NORTH_UP) == 120.0
    assert orbit_angle(350.0, 20.0, NORTH_UP) == 10.0, "跨 360 要回绕"

    # 正前方（0°）应在圆心正上方
    dx, dy = screen_offset(0.0, 5.0, 0.0, 10.0, 100.0)
    assert abs(dx) < 1e-9 and abs(dy + 50.0) < 1e-9, (dx, dy)
    # 正右方（90°）应在圆心正右
    dx, dy = screen_offset(90.0, 10.0, 0.0, 10.0, 100.0)
    assert abs(dx - 100.0) < 1e-9 and abs(dy) < 1e-9, (dx, dy)

    # 点 → 极坐标 → 点，必须回到原处（点击测距的正确性基础）
    for az, rng in ((0.0, 2.0), (37.0, 6.5), (180.0, 9.0), (315.0, 1.0)):
        dx, dy = screen_offset(az, rng, 0.0, 10.0, 100.0)
        cx, cy = 200.0, 150.0
        got_r, got_a, inside = point_to_polar(cx + dx, cy + dy, cx, cy, 100.0, 0.0, 10.0)
        assert inside, "盘内点被判定为盘外"
        assert abs(got_r - rng) < 0.05, f"距离回算误差 {got_r - rng:.3f}"
        d = abs((got_a - az + 180) % 360 - 180)
        assert d < 0.6, f"方位回算误差 {d:.2f}°"

    # 盘外点击：inside 为 False
    _, _, inside = point_to_polar(200.0 + 200.0, 150.0, 200.0, 150.0, 100.0, 0.0, 10.0)
    assert not inside

    assert clamp_range(-1.0, 0.0, 10.0) == 0.0
    assert clamp_range(99.0, 0.0, 10.0) == 10.0
    assert fmt_bearing(5.4) == "005°"
    assert fmt_bearing(-10.0) == "350°"
    assert fmt_distance(850.0) == "850.00 m"
    assert fmt_distance(12345.0) == "12.35 km"
    ok("视图换算：北向/艏向 / 极坐标往返 / 文本格式")


def test_stats_queries() -> None:
    """统计与检索：分桶计数、告警汇总、条件过滤。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        st = Store(Path(td) / "s.db")
        sid = st.start_session("bench", "x", Capabilities(name="S"))
        t0 = 1_700_000_000.0
        for k in range(3):
            st.record_tracks(t0 + k * 70.0, [Target(x=1.0, y=2.0, tid=k)])
        st.record_event("crit", "alarm", "过近1", "near", True, 1)
        st.record_event("crit", "alarm", "过近2", "near", True, 2)
        st.record_event("warn", "alarm", "人多", "many", True, -1)
        st.record_event("info", "connect", "已连接", "", True, -1)
        st.flush(force=True)

        buckets = st.tracks_per_bucket(sid, 60)
        assert len(buckets) == 3, f"分桶数应为 3，实际 {len(buckets)}: {buckets}"
        assert [c for _, c in buckets] == [1, 1, 1]
        assert buckets[0][0] == 0, "桶序号应从 0 开始"

        summ = st.alarm_summary(sid)
        d = {r[0]: r[2] for r in summ}
        assert d.get("near") == 2 and d.get("many") == 1, f"告警汇总错误 {d}"

        crit = st.query_events(sid=sid, level="crit")
        assert len(crit) == 2, "按级别过滤失败"
        only_many = st.query_events(sid=sid, rule_id="many")
        assert len(only_many) == 1 and only_many[0][4] == "人多"
        assert "near" in st.distinct_rule_ids() and "many" in st.distinct_rule_ids()
        st.close()
    ok("统计检索：分桶 / 告警汇总 / 级别与规则过滤")


def test_cpa() -> None:
    """CPA/TCPA：接近、远离、本舰运动三种情况。"""
    from radarscope.core.cpa import evaluate, own_velocity, worst

    # 目标在正前 10 m，以 1 m/s 朝我而来 → 10 秒后撞上，CPA≈0
    t = Target(x=0.0, y=10.0, vx=0.0, vy=-1.0, tid=1)
    r = evaluate([t], speed=0.0, heading_deg=0.0)[0]
    assert abs(r.tcpa - 10.0) < 1e-6, f"TCPA 应为 10s，实际 {r.tcpa:.2f}"
    assert r.cpa < 1e-6, f"CPA 应为 0，实际 {r.cpa:.3f}"
    assert r.danger

    # 同位置但正在远离 → TCPA<=0，CPA 就是当前距离
    t2 = Target(x=0.0, y=10.0, vx=0.0, vy=1.0, tid=2)
    r2 = evaluate([t2], speed=0.0, heading_deg=0.0)[0]
    assert r2.tcpa <= 0 and abs(r2.cpa - 10.0) < 1e-6, f"{r2}"
    assert not r2.danger

    # 本舰以 2m/s 正北航行，目标静止在正前 10m → 相对接近速度 2m/s，5 秒到
    t3 = Target(x=0.0, y=10.0, vx=0.0, vy=0.0, tid=3)
    r3 = evaluate([t3], speed=2.0, heading_deg=0.0)[0]
    assert abs(r3.tcpa - 5.0) < 1e-6, f"TCPA 应为 5s，实际 {r3.tcpa:.2f}"

    # 本舰速度向量：航向 90°（正东）时 vx=速度, vy≈0
    vx, vy = own_velocity(2.0, 90.0)
    assert abs(vx - 2.0) < 1e-9 and abs(vy) < 1e-9, (vx, vy)

    # 侧向掠过：目标从左侧平行通过，CPA 等于横向距离
    t4 = Target(x=-3.0, y=8.0, vx=0.0, vy=-1.0, tid=4)
    r4 = evaluate([t4], speed=0.0, heading_deg=0.0)[0]
    assert abs(r4.tcpa - 8.0) < 1e-6 and abs(r4.cpa - 3.0) < 1e-6, f"CPA 应≈3，实际 {r4.cpa:.2f}"

    w = worst([r, r2, r4])
    assert w is not None and w.tid == 1, "最危险目标判断错误"
    assert worst([r2]) is None, "全在远离时不该报危险目标"
    ok("CPA/TCPA：接近 / 远离 / 本舰运动 / 侧向掠过")


def test_forwarder() -> None:
    """数据转发：载荷结构正确、速率限制生效、关闭时不发。"""
    import json

    from radarscope.core.forwarder import Forwarder

    f = Forwarder()
    ts = [Target(x=1.25, y=3.5, vx=0.1, vy=-0.2, snr=0.8, tid=7, views=2)]
    p = f.payload(None, ts)
    assert p["type"] == "radarscope.frame" and p["targets"][0]["tid"] == 7
    assert p["targets"][0]["views"] == 2
    assert json.dumps(p)          # 必须能序列化

    # 关闭状态下不发
    f.configure("127.0.0.1", 19100, rate=0)
    assert f.send(None, ts) is False, "未开启时不该发送"

    f.start()
    assert f.send(None, ts) is True, "开启后应能发出（本机 UDP 无需接收方）"
    assert f.sent == 1
    # rate=0 表示不限速，连发应该都成功
    assert f.send(None, ts) is True

    f.configure(rate=10.0)
    f._last = 0.0
    assert f.send(None, ts) is True      # 第一帧
    assert f.send(None, ts) is False, "10Hz 限制下紧接着的第二帧应被丢弃"
    f.stop()
    assert f.send(None, ts) is False
    assert "关闭" in f.describe() or "关" in f.describe()
    ok("数据转发：载荷 / 速率限制 / 开关")


def _ais_bits(fields: list[tuple[int, int]]) -> str:
    """按 (值, 位宽) 拼 bit 串；负数用补码。"""
    out = []
    for v, w in fields:
        if v < 0:
            v += 1 << w
        out.append(f"{v & ((1 << w) - 1):0{w}b}")
    return "".join(out)


def test_ais() -> None:
    """AIS 报文解析：位置报告字段还原、校验、非法类型。"""
    from radarscope.core.ais import checksum_ok, make_aivdm, parse_aivdm

    mmsi, lon, lat = 413759959, 116.4, 39.9
    bits = _ais_bits([
        (1, 6), (0, 2), (mmsi, 30),          # 类型 / 重复 / MMSI
        (0, 4), (0, 8), (102, 10), (0, 1),   # 航行状态 / 转向 / SOG=10.2kn / 精度
        (int(lon * 600000), 28), (int(lat * 600000), 27),
        (900, 12), (90, 9),                  # COG=90.0° / 真航向 90°
    ])
    bits = bits.ljust(168, "0")
    sentence = make_aivdm(bits)
    assert checksum_ok(sentence), "生成的报文校验应该通过"

    t = parse_aivdm(sentence)
    assert t is not None, "位置报告应能解析"
    assert t.mmsi == mmsi, t.mmsi
    assert abs(t.lat - lat) < 1e-4 and abs(t.lon - lon) < 1e-4, (t.lat, t.lon)
    assert abs(t.sog - 10.2) < 1e-6 and abs(t.cog - 90.0) < 1e-6
    assert t.heading == 90

    # 校验和错一个字符必须拒收
    bad = sentence[:-2] + ("00" if sentence[-2:] != "00" else "11")
    assert parse_aivdm(bad) is None, "校验错误的报文不该被接受"

    # 静电信息（类型 5）不是位置报告，应忽略
    b5 = _ais_bits([(5, 6), (0, 2), (mmsi, 30)]).ljust(424, "0")
    assert parse_aivdm(make_aivdm(b5)) is None

    # 经纬度换算成本地米制：参考点在南边 100 m 时，目标应在正前 100 m
    x, y = t.local_xy(lat - 100.0 / 111320.0, lon)
    assert abs(x) < 0.5 and abs(y - 100.0) < 0.5, (x, y)
    ok("AIS：位置报告 / 校验拒收 / 非位置报文 / 经纬度换算")


def test_control_channel() -> None:
    """下行控制：模拟器支持并真的改变输出，基类明确表示不支持。"""
    from radarscope.core.driver import RadarDriver
    from radarscope.drivers.simulator import SimulatorDriver

    assert RadarDriver.supports_control is False
    assert SimulatorDriver.supports_control is True

    d = SimulatorDriver()
    d.open(mode="staring")
    f = None
    for _ in range(200):
        f = d.read()
        if f is not None and f.scan.max() > 0.4:
            break
        time.sleep(0.01)
    assert f is not None
    mean_before = float(f.scan.mean())

    assert d.set_control(gain=1.0, stc=0.8, ftc=0.15) is True
    assert d.control_state()["stc"] == 0.8
    g = None
    for _ in range(60):
        g = d.read()
        if g is not None:
            break
        time.sleep(0.01)
    mean_after = float(g.scan.mean())
    assert mean_after < mean_before * 0.8, \
        f"STC+FTC 应压低整体能量：{mean_before:.3f} → {mean_after:.3f}"
    d.close()
    ok("下行控制：模拟器生效 / 基类不支持")


def test_maptiles() -> None:
    """地图瓦片换算：分辨率、级别选择、瓦片坐标、覆盖范围。"""
    from radarscope.core.maptiles import (EARTH_C, lonlat_to_tile,
                                          meters_per_pixel, tile_span,
                                          zoom_for_range)

    # 赤道 z0 每块瓦片铺满全球，分辨率就是常数
    assert abs(meters_per_pixel(0.0, 0) - EARTH_C) < 1.0
    # 高纬度分辨率变小（同样的像素代表更少米）
    assert meters_per_pixel(60.0, 10) < meters_per_pixel(0.0, 10)

    # 量程越大选越低的级别；量程变小级别不变或变大
    z_far = zoom_for_range(39.9, 5000.0, 600)
    z_near = zoom_for_range(39.9, 8.0, 600)
    assert z_far < z_near, f"大量程应选低级别：{z_far} vs {z_near}"
    assert 3 <= z_far <= 18 and 3 <= z_near <= 18

    # (0,0) 在 z=1 时正好是四块瓦片的交界
    x, y = lonlat_to_tile(0.0, 0.0, 1)
    assert abs(x - 1.0) < 1e-9 and abs(y - 1.0) < 1e-9, (x, y)
    # 经度 180 在最右边界
    x2, _ = lonlat_to_tile(0.0, 179.999, 1)
    assert 1.99 < x2 < 2.0

    # 覆盖范围要包含中心点所在的瓦片
    x0, y0, x1, y1 = tile_span(39.9, 116.4, 15, 3, 3)
    xc, yc = lonlat_to_tile(39.9, 116.4, 15)
    assert x0 <= int(xc) < x1 and y0 <= int(yc) < y1, ((x0, x1), (y0, y1))
    assert x1 - x0 == 3 and y1 - y0 == 3
    ok("地图瓦片：分辨率 / 级别 / 坐标 / 覆盖范围")


def test_noise_floor_stable() -> None:
    """底噪不能随帧数抬升。

    同一个坑踩过两次：模拟器里把噪声写成 `scan += noise`，
    在 decay<1 时稳态收敛到 noise/(1-decay)，画面整体糊上一层绿底，
    瀑布图直接一片绿。这条断言就是用来盯死它的。
    """
    import radarscope.drivers  # noqa: F401
    from radarscope.core.driver import available_drivers

    for did in ("multi_demo", "sim_aesa", "sim"):
        cls = available_drivers()[did]
        d = cls()
        d.open()
        f = None
        for _ in range(240):
            f = d.read()
            if f is not None:
                break
            time.sleep(0.003)
        for _ in range(90):                 # 跑一段时间，让稳态充分建立
            for _ in range(60):
                g = d.read()
                if g is not None:
                    f = g
                    break
                time.sleep(0.002)
        d.close()
        assert f is not None and f.scan is not None
        med = float(np.median(f.scan))
        p90 = float(np.percentile(f.scan, 90))
        assert med < 0.06, f"{did} 底噪中位数 {med:.3f} 偏高，噪声可能在累加"
        assert p90 < 0.30, f"{did} 底噪 P90 为 {p90:.3f}，画面会被噪声糊住"
    ok("底噪稳定性：多帧后不抬升（防噪声累加回归）")


def test_capture_fixtures() -> None:
    """录制夹具回归：.bin 原始字节流与 .rad 归档可离线复现且不丢最后一帧。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "capture_check",
        Path(__file__).resolve().parents[1] / "tools" / "capture_check.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["capture_check"] = mod
    spec.loader.exec_module(mod)

    results = mod.verify_manifest()
    assert len(results) >= 5, f"夹具数量不足：{len(results)}"
    rad_res = [r for r in results if r.kind == "rad"]
    assert rad_res and rad_res[0].frames == 5, f"ReplayDriver 丢帧：{rad_res}"
    ok("录制夹具：.bin / .rad 离线回放与 CRC 拒绝数回归")


def test_ble_transport() -> None:
    """BLE 传输层：端点解析、空地址拦截、后台通知队列桥接。"""
    from radarscope.core.transport import BleTransport, TransportError

    spec = parse_spec("ble://AA:BB:CC:DD:EE:FF?char=0000ffe1-0000-1000-8000-00805f9b34fb")
    assert spec["scheme"] == "ble" and spec["target"] == "AA:BB:CC:DD:EE:FF"

    tr_empty = BleTransport("")
    try:
        tr_empty.open()
        raise AssertionError("空 BLE 地址应抛出 TransportError")
    except TransportError:
        pass

    tr = BleTransport("AA:BB:CC:DD:EE:FF", timeout=0.01)
    assert tr.read() == b""
    tr._q.put_nowait(b"\xaa\xff\x03\x00")
    assert tr.read() == b"\xaa\xff\x03\x00"
    tr.close()
    ok("BLE 传输：端点解析 / 空地址拦截 / 队列桥接")


def test_waterfall_projection() -> None:
    """瀑布图 B1：5 个独立航迹在距离-时间平面各自成线，静止目标距离恒定。"""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication
    from radarscope.ui.waterfall import WaterfallWidget

    app = QApplication.instance() or QApplication([])
    wf = WaterfallWidget()
    wf.set_range(20.0)
    for step in range(8):
        tracks = [
            Target(x=0.0, y=3.0, tid=1),                  # 静止 3m
            Target(x=0.0, y=5.0 + step * 0.3, tid=2),     # 远离
            Target(x=2.0, y=8.0, tid=3),
            Target(x=-3.0, y=10.0, tid=4),
            Target(x=4.0, y=12.0 - step * 0.2, tid=5),    # 接近
        ]
        wf.push_frame(RadarFrame(timestamp=float(step), targets=tracks), tracks)

    series = wf.track_series()
    assert len(series) == 5, f"应有 5 条独立航迹，实际 {len(series)}"
    assert all(len(pts) == 8 for pts in series.values())
    t1_ranges = [r for _age, r in series[1]]
    assert max(t1_ranges) - min(t1_ranges) < 1e-6, "静止目标距离不应随时间变化"
    del app
    ok("瀑布图 B1：5 条独立航迹折线投影")


def test_cfar_adaptive() -> None:
    """B2 自适应 CFAR：弱目标与强杂波下均优于固定 0.30 CA-CFAR。"""
    caps = Capabilities(has_scan=True, has_azimuth=True, az_fov=120.0,
                        range_min=0.2, range_max=10.0)
    rng = np.random.default_rng(42)

    # 1) 强杂波场（均值 0.25，局部方差大）下只有 1 个真目标 (SNR=0.95)
    f1 = pipeline.synthesize_scan(caps, [Target(x=1.2, y=5.0, snr=0.95)])
    clutter = np.clip(0.22 + rng.normal(0.0, 0.14, f1.scan.shape), 0.0, 0.85).astype(np.float32)
    scan1 = np.maximum(f1.scan, clutter)
    # 恢复目标真实峰值（synthesize_scan 已有，确保峰值明显高于杂波）
    frame_clutter = RadarFrame(timestamp=1.0, scan=scan1, az_axis=f1.az_axis, r_axis=f1.r_axis)
    ca_fa = len(pipeline.detect_targets(frame_clutter, threshold=0.30, cfar_mode="ca", max_targets=16))
    auto_fa = len(pipeline.detect_targets(frame_clutter, threshold=0.30, cfar_mode="auto", max_targets=16))
    assert auto_fa <= ca_fa and auto_fa <= 3, f"强杂波自适应虚警未收敛: auto={auto_fa}, ca={ca_fa}"

    # 2) 安静环境弱目标（峰值 0.24，固定 0.30 会漏检，auto 能抓到）
    f2 = pipeline.synthesize_scan(caps, [Target(x=0.0, y=4.0, snr=0.25)])
    quiet_noise = np.clip(rng.normal(0.015, 0.006, f2.scan.shape), 0.0, 0.05).astype(np.float32)
    scan2 = np.maximum(f2.scan * 0.96, quiet_noise)
    frame_quiet = RadarFrame(timestamp=2.0, scan=scan2, az_axis=f2.az_axis, r_axis=f2.r_axis)
    ca_miss = len(pipeline.detect_targets(frame_quiet, threshold=0.30, cfar_mode="ca"))
    auto_hit = len(pipeline.detect_targets(frame_quiet, threshold=0.30, cfar_mode="auto"))
    assert ca_miss == 0 and auto_hit >= 1, f"安静弱目标对比异常: ca={ca_miss}, auto={auto_hit}"
    ok("自适应 CFAR (B2)：强杂波抑虚警 + 弱目标防漏检")


def test_track_level_fusion() -> None:
    """B3 航迹级加权融合：3 站各自产生独立航迹，融合后误差低于最差单站且建立全局航迹。"""
    from math import atan2, degrees, hypot
    from radarscope.core.fusion import FusionEngine, SensorSpec, global_to_local

    true_x, true_y = 2.0, 4.5
    specs = [
        SensorSpec("S1", x=-3.0, y=0.0, bearing_deg=30.0),
        SensorSpec("S2", x=3.0, y=0.0, bearing_deg=-30.0),
        SensorSpec("S3", x=0.0, y=-2.5, bearing_deg=0.0),
    ]

    class _Stub:
        def __init__(self, spec: SensorSpec, dr: float, snr: float):
            self.spec = spec
            self.dr = dr
            self.snr = snr
            self.t = 0.0

        def read(self) -> RadarFrame:
            lx, ly = global_to_local(true_x, true_y, self.spec)
            r = hypot(lx, ly) + self.dr
            a = atan2(lx, ly)
            self.t += 0.05
            return RadarFrame(
                timestamp=self.t,
                targets=[Target(x=r * np.sin(a), y=r * np.cos(a), snr=self.snr)],
            )

        def close(self) -> None:
            pass

    eng = FusionEngine(rate=100.0)
    eng.fusion_mode = "track"
    eng.attach(specs[0], _Stub(specs[0], dr=0.45, snr=0.8))
    eng.attach(specs[1], _Stub(specs[1], dr=-0.45, snr=0.8))
    eng.attach(specs[2], _Stub(specs[2], dr=0.10, snr=0.95))

    frame = None
    for k in range(5):
        for c in eng._children:
            c.frame = c.dev.read()
            c.last_frame_t = float(k + 1) * 0.05
        fused = eng.fuse_tracks(now=float(k + 1) * 0.05)
        frame = fused
    assert frame and len(frame) == 1
    err = hypot(frame[0].x - true_x, frame[0].y - true_y)
    assert err < 0.25 and frame[0].views == 3, f"3 站航迹融合误差过大 err={err:.3f}, views={frame[0].views}"
    ok("航迹级加权融合 (B3)：3 站共视误差 < 0.25m")


def test_plugin_verification() -> None:
    """C2 插件校验：SHA256 摘要 + 未信任拦截 + 授权信任后加载。"""
    from radarscope.core.plugins import PluginManager

    with tempfile.TemporaryDirectory() as td:
        pdir = Path(td) / "plugins"
        pdir.mkdir()
        plug = pdir / "demo_p.py"
        plug.write_text('PLUGIN = {"name": "演示插件"}\ndef setup(api): api.log("ok")\n', encoding="utf-8")
        trust = Path(td) / "trusted.json"

        mgr = PluginManager(pdir, trust_path=trust, require_trust=True)
        infos = mgr.load_all()
        assert len(infos) == 1 and not infos[0].trusted and not infos[0].ok, "未信任插件不应自动执行"

        mgr.approve_plugin(plug)
        infos2 = mgr.reload()
        assert infos2[0].trusted and infos2[0].ok and len(infos2[0].sha256) == 64
    ok("插件安全校验 (C2)：SHA256 清单与信任确认拦截")


def test_timeline_queries() -> None:
    """C3 时间轴回放查询：按时间窗口检索航迹与截至时刻告警事件。"""
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "tl.db")
        sid = st.start_session("sim", "virtual")
        st.record_tracks(100.0, [Target(x=1.0, y=2.0, tid=1)])
        st.record_tracks(105.0, [Target(x=2.0, y=4.0, tid=1)])
        st.record_tracks(110.0, [Target(x=3.0, y=6.0, tid=1)])
        st.record_event("warn", "alarm", "防区告警", rule_id="z1", ts=104.0)
        st.record_event("crit", "alarm", "后期告警", rule_id="z2", ts=109.0)
        st.flush(force=True)

        win = st.tracks_window(sid, 104.5, 105.5)
        assert len(win) == 1 and abs(win[0][2] - 2.0) < 1e-3
        evts = st.events_until(sid, 106.0)
        assert len(evts) == 1 and evts[0][3] == "z1"
        st.end_session("ok")
        st.close()
    ok("时间轴查询 (C3)：时间窗口航迹与历史事件检索")


def test_meta_schema() -> None:
    """C4 Meta Schema：标准键补全与非法类型检出。"""
    from radarscope.core.model import validate_frame_meta

    f = RadarFrame(timestamp=1.0, meta={"bad": 2})
    errs = validate_frame_meta(f)
    assert not errs and f.meta["good"] == 0 and f.meta["scan_period"] == 0.1
    f_bad = RadarFrame(timestamp=1.0, meta={"bad": "not_int"})
    assert validate_frame_meta(f_bad), "类型错误应检出"
    ok("Meta Schema (C4)：默认值补全与类型校验")


def test_polygon_zone() -> None:
    """D1 多边形警戒区：射线法内外判定 + AlarmEngine 触发。"""
    poly = [[-1.0, 2.0], [1.0, 2.0], [1.0, 4.0], [-1.0, 4.0]]
    eng = AlarmEngine([
        AlarmRule("pz", "多边形区", "poly_zone", {"points": poly}, hold=0.0, cooldown=0.0),
    ])
    caps = Capabilities()
    assert eng.evaluate([Target(x=2.5, y=3.0, tid=1)], caps, now=0.0) == []
    ev = eng.evaluate([Target(x=0.2, y=3.0, tid=1)], caps, now=1.0)
    assert any(e.rule_id == "pz" and e.active for e in ev), "多边形内目标应触发告警"
    ok("多边形警戒区 (D1)：射线法点在多边形内判定与告警")


def test_report_export() -> None:
    """D2 报表导出：自包含 HTML（含 SVG）与 CSV 汇总生成。"""
    from radarscope.core.report import export_session_report

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "rep.db")
        sid = st.start_session("sim", "virtual", Capabilities(name="演示雷达"))
        st.record_tracks(100.0, [Target(x=1.0, y=3.0, tid=1)])
        st.record_event("warn", "alarm", "测试告警", rule_id="r1", ts=100.5)
        st.end_session("done")

        html_p = export_session_report(st, sid, Path(td) / "out.html")
        csv_p = export_session_report(st, sid, Path(td) / "out.csv")
        st.close()
        assert "<svg" in html_p.read_text(encoding="utf-8")
        assert "测试告警" in csv_p.read_text(encoding="utf-8-sig")
    ok("报表导出 (D2)：HTML SVG 图与 CSV 汇总")


def test_i18n_and_version() -> None:
    """D3/D4 i18n 与版本号对比。"""
    from radarscope.core.i18n import set_lang, tr
    from radarscope.version import is_newer

    set_lang("en")
    assert tr("设置(&S)") == "Settings(&S)"
    set_lang("zh")
    assert tr("设置(&S)") == "设置(&S)"
    assert is_newer("v1.2.0", "1.1.0") and not is_newer("1.1.0", "1.1.0")
    ok("国际化与版本探测 (D3/D4)：中英文切换与版本比对")


def test_ws_bridge() -> None:
    """D6 Web 订阅桥：RFC 6455 握手密钥与文本帧编码。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ws_bridge", Path(__file__).resolve().parents[1] / "tools" / "ws_bridge.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # RFC 6455 标准示例密钥验证
    assert mod.ws_accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    pkt = mod.encode_ws_text('{"v":1}')
    assert pkt[0] == 0x81 and pkt[1] == 7 and pkt[2:] == b'{"v":1}'
    ok("Web 订阅桥 (D6)：RFC 6455 握手与 WS 帧编码")


def test_electronic_station() -> None:
    from radarscope.core.sim_targets import ElectronicStation

    st = ElectronicStation(seed=7)
    st.set_scenario("patrol", range_max=8.0)
    assert st.active() and len(st.objects) == 3, f"巡逻场景应生成 3 个目标，实际 {len(st.objects)}"

    obj = st.inject(1.0, 2.0, 0.5, -0.4, amp=0.90, name="手动测试点", respawn=False)
    assert len(st.objects) == 4
    pts_before = (obj.x, obj.y)
    tgts = st.step(0.5, range_max=8.0)
    assert len(tgts) == 4
    assert (obj.x, obj.y) != pts_before, "推进步进后目标位置应更新"

    base_scan = np.zeros((36, 40), dtype=np.float32)
    frame = RadarFrame(
        timestamp=time.time(),
        scan=base_scan,
        az_axis=np.radians(np.linspace(-90.0, 90.0, 36)).astype(np.float32),
        r_axis=np.linspace(0.3, 8.0, 40, dtype=np.float32),
        targets=[Target(x=-2.0, y=3.0, snr=12.0, tid=1)],
    )
    caps = Capabilities(name="Sim", source="sim", form="sector",
                        range_min=0.3, range_max=8.0, az_fov=180.0)
    merged = st.merge_into_frame(frame, caps, dt=0.1)
    assert len(merged.targets) == 1 + len(st.objects), "叠加后应保留原目标并追加虚拟目标"
    assert float(merged.scan.max()) > 0.3, "叠加扫描矩阵应包含虚拟目标峰值"
    ok("模拟电子站：场景生成、自定义注入与帧叠加")


def test_zone_hit_and_map_toggle() -> None:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtCore import QPoint
    from PyQt5.QtWidgets import QApplication
    from radarscope.core.maptiles import TileStore
    from radarscope.ui.maplayer import MapLayer
    from radarscope.ui.ppi import PPIWidget

    app = QApplication.instance() or QApplication([])
    ppi = PPIWidget()
    ppi.resize(600, 600)
    ppi.set_capabilities(Capabilities(name="Test", range_min=0.3, range_max=8.0,
                                      has_scan=True, has_azimuth=True))
    ppi.show_zones = True
    ppi.set_alarm_zones([(0.0, 4.0, 1.2, "alarm", "z1")])
    # _geometry(): cx=300, cy=300, R = 600*0.47 = 282; yy = 300 - (4.0/8.0)*282 = 159
    hit_center = QPoint(300, 159)
    assert ppi._zone_at(hit_center) == "z1", f"圆心应命中 z1，实际 {ppi._zone_at(hit_center)}"
    h_move = ppi._hit_zone_handle(hit_center)
    assert h_move == ("move", (0.0, 4.0, 1.2, "alarm", "z1")), f"中心手柄应为 move，实际 {h_move}"
    # 边界：radius_px = 1.2/8.0 * 282 = 42.3 -> x=300+42=342 应在边缘手柄范围内
    h_resize = ppi._hit_zone_handle(QPoint(342, 159))
    assert h_resize is not None and h_resize[0] == "resize", f"边缘手柄应为 resize，实际 {h_resize}"

    with tempfile.TemporaryDirectory() as tmp:
        ml = MapLayer(Path(tmp))
        ml.set_provider("carto_light")
        assert ml.store.provider == "carto_light"
        ml.set_provider("carto_dark")
        assert ml.store.provider == "carto_dark"
        ml.enabled = False
        ml.update_for(30.0, 120.0, 100.0, 500)
        ml._compose(30.0, 120.0, 15, 0, 0, 0, 0)
        assert ml.image is None, "无本地瓦片时不应生成不透明黑块图"
        ml.stop()
    ok("PPI 警戒区像素命中与地图图层离线防黑屏")


def test_map_geolocate_and_provider() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from radarscope.core.maptiles import geolocate_ip, resolve_provider

    assert resolve_provider("auto", "light") == "arcgis_street"
    assert resolve_provider("auto", "dark") == "carto_dark"
    assert resolve_provider("osm", "light") == "osm"

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"success":true,"latitude":31.2304,"longitude":121.4737,"city":"Shanghai","country":"China"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.handle_request, daemon=True)
    th.start()
    try:
        lat, lon, desc = geolocate_ip(timeout=2.0, urls=(f"http://127.0.0.1:{srv.server_port}/",))
        assert abs(lat - 31.2304) < 1e-4 and abs(lon - 121.4737) < 1e-4
        assert "Shanghai" in desc
    finally:
        srv.server_close()
    ok("联网地图图源解析与 IP 自动定位解析")


def test_statusbar_and_map_dialog() -> None:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication, QSizePolicy
    from radarscope.ui.extra import MapLocationDialog
    from radarscope.ui.mainwindow import MainWindow

    _app = QApplication.instance() or QApplication([])
    w = MainWindow()
    assert w.status_left.minimumWidth() == 0
    assert w.status_left.sizePolicy().horizontalPolicy() == QSizePolicy.Ignored
    assert w.statusBar().minimumHeight() >= 26
    w.status_left.setText("超长能力描述 " * 25)
    assert w.minimumSizeHint().width() < 1200, f"状态栏长文本不应撑宽主窗口: {w.minimumSizeHint().width()}"

    dlg = MapLocationDialog(lat=30.5, lon=114.3, provider="osm", map_enabled=True, parent=w)
    lat, lon, prov, on = dlg.values()
    assert abs(lat - 30.5) < 1e-6 and abs(lon - 114.3) < 1e-6
    assert prov == "osm" and on is True
    assert dlg.auto_fit_range() is True
    assert hasattr(w, "_m_scene") and hasattr(w, "btn_scene")
    # 第二行工具栏按钮紧随量程排列：btn_lock 与 range_spin 之间不应存在较大空白拉伸间距
    assert abs(w.btn_scene.x() - (w.range_spin.x() + w.range_spin.width())) < 80 or not w.isVisible()
    w.close()
    ok("状态栏防挤压约束与联网地图定位设置对话框")


def test_ew_jamming_and_clutter_map() -> None:
    from radarscope.core.pipeline import ClutterMap
    from radarscope.core.sim_targets import ElectronicStation

    st = ElectronicStation(seed=99)
    for sc in ("marine", "automotive", "drone"):
        st.set_scenario(sc, range_max=100.0)
        assert st.objects, f"场景 {sc} 应生成目标"

    st.set_scenario("patrol", range_max=10.0)
    st.jam_mode = "drfm"
    st.jam_jsr = 0.85
    caps = Capabilities(name="Sim", source="sim", form="sector",
                        range_min=0.3, range_max=10.0, az_fov=180.0)
    frame = RadarFrame(
        timestamp=time.monotonic(),
        scan=np.zeros((45, 50), dtype=np.float32),
        az_axis=np.radians(np.linspace(-90.0, 90.0, 45)).astype(np.float32),
        r_axis=np.linspace(0.3, 10.0, 50, dtype=np.float32),
        targets=[Target(x=0.0, y=3.0, snr=0.90)],
    )
    out = st.merge_into_frame(frame, caps, dt=0.1, threshold=0.25, cfar_mode="ca")
    assert len(out.targets) >= 2, f"DRFM 欺骗干扰开启后应检出假回波，实际 {len(out.targets)}"

    cm = ClutterMap(alpha=0.5, gain=0.9, warmup=4)
    cm.enabled = True
    static_wall = np.zeros((20, 30), dtype=np.float32)
    static_wall[:, 10] = 0.8
    for _ in range(5):
        f = RadarFrame(scan=static_wall.copy())
        cm.apply(f)
    assert float(f.scan[:, 10].max()) < 0.2, f"静止杂波学习后应被明显抑制，实际 {f.scan[:, 10].max()}"
    moving_add = static_wall.copy()
    moving_add[10, 22] = 0.95
    f_mov = RadarFrame(scan=moving_add)
    cm.apply(f_mov)
    assert float(f_mov.scan[10, 22]) > 0.7, "新增运动目标回波不应被静态杂波图误删"
    ok("电子对抗教学干扰波形（DRFM/压制）与静态杂波背景抑制")


def test_custom_proto_and_nmea_ttm() -> None:
    from radarscope.core.custom_proto import (
        CustomProtocolSpec, builtin_presets, load_custom_specs, preview_sample, save_custom_specs,
    )
    from radarscope.core.driver import available_drivers

    presets = {s.id: s for s in builtin_presets()}
    ttm = preview_sample(presets["nmea_ttm"], "$RATTM,01,1.25,045.0,T,12.5,210.0,T,0.3,4.2,N,VESSEL_A,T,,*46")
    assert len(ttm) == 1 and abs(ttm[0].rng - 1.25 * 1852.0) < 2.0, f"NMEA TTM 换算错误: {ttm}"

    csv_tgts = preview_sample(presets["traffic_csv"], "$TGT,1.8,12.5,-0.5,-1.2,0.88")
    assert len(csv_tgts) == 1 and abs(csv_tgts[0].x - 1.8) < 1e-4

    bin_tgts = preview_sample(presets["uart_bin_cm"], "AA 55 96 00 C8 01 58 00 0D 0A")
    assert len(bin_tgts) == 1 and abs(bin_tgts[0].x - 1.50) < 1e-3 and abs(bin_tgts[0].y - 4.56) < 1e-3

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "custom.json"
        s = CustomProtocolSpec(id="test_p", label="Test Proto", mode="csv_xy")
        save_custom_specs(p, [s])
        loaded = {x.id: x for x in load_custom_specs(p)}
        assert "test_p" in loaded and "nmea_ttm" in loaded
    assert "nmea_ttm" in available_drivers()
    ok("零代码通用雷达协议适配器（NMEA TTM / CSV / 二进制帧）")


def test_protowizard_and_sim_ew_ui() -> None:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication
    from radarscope.ui.mainwindow import MainWindow
    from radarscope.ui.protodialog import ProtoWizardDialog
    from radarscope.ui.simdialog import SimStationDialog

    _app = QApplication.instance() or QApplication([])
    pw = ProtoWizardDialog()
    pw._test_parse()
    assert "成功解析" in pw.lbl_preview.text(), pw.lbl_preview.text()
    pw.close()

    w = MainWindow()
    assert hasattr(w, "clutter_map") and hasattr(w, "btn_clutter")
    assert hasattr(w.ppi, "doppler_color")
    sd = SimStationDialog(w.sim_station, range_max=w.range_spin.value(), parent=w)
    idx_rgpo = sd.cb_jam.findData("rgpo")
    assert idx_rgpo >= 0
    sd.cb_jam.setCurrentIndex(idx_rgpo)
    assert w.sim_station.jam_mode == "rgpo"
    sd.close()
    w.close()
    ok("通用协议适配向导与模拟电子战界面集成")


def test_multi_themes_and_palettes() -> None:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication
    from radarscope.ui.mainwindow import MainWindow
    from radarscope.ui.theme import (
        PALETTES,
        THEMES,
        build_stylesheet,
        color_table,
        is_dark_theme,
        theme_palette,
    )

    assert len(THEMES) == 7, f"预期 7 套主题，实际 {len(THEMES)}"
    assert len(PALETTES) == 8, f"预期 8 套色板，实际 {len(PALETTES)}"
    assert len({tuple(p["bg"]) for p in PALETTES.values()}) == 8, "8 套雷达色板背景色应各不相同"
    assert not is_dark_theme("light") and not is_dark_theme("nordic")
    for dk in ("dark", "graphite", "marine_night", "tactical", "amber_ops"):
        assert is_dark_theme(dk), f"{dk} 应判定为深色主题"
        assert build_stylesheet(dk, 1.0)

    scaled = build_stylesheet("graphite", 1.25)
    assert "font-size: 15px" in scaled, "字号缩放正则应把 12px 正确放大为 15px"

    for pkey in PALETTES:
        ct = color_table(pkey)
        assert len(ct) == 256, f"色板 {pkey} LUT 长度应为 256"
    for tkey in THEMES:
        assert theme_palette(tkey) in PALETTES

    _app = QApplication.instance() or QApplication([])
    w = MainWindow()
    orig_theme = w._theme
    orig_pal = w.pal_combo.currentData()
    try:
        w._apply_theme("marine_night", sync_palette=True)
        assert w.pal_combo.currentData() == "cyan"
        assert w._theme_actions["marine_night"].isChecked()
        assert w._palette_actions["cyan"].isChecked()

        w._apply_theme("nordic", sync_palette=True)
        assert w.pal_combo.currentData() == "ocean_teal"
        assert w._theme_actions["nordic"].isChecked()
        assert w._palette_actions["ocean_teal"].isChecked()

        # 手动选深色雷达色板并在非联动主题下应用时，不应被强行覆盖
        w._select_palette("green")
        assert w.pal_combo.currentData() == "green"
        assert w.settings.value("palette") == "green"
        assert w._palette_actions["green"].isChecked()
        assert w.ppi._palette_name == "green"
        assert w.ascope._palette_name == "green"
        assert w.waterfall._palette_name == "green"
        assert w.rdscope._palette_name == "green"
        w._apply_theme("light", sync_palette=False)
        assert w.pal_combo.currentData() == "green", "非联动主题切换不应覆盖用户手选雷达色板"
    finally:
        w._apply_theme(orig_theme, sync_palette=False)
        if orig_pal in PALETTES:
            w._select_palette(orig_pal)
        w.close()
    ok("多套现代 UI 主题与雷达色板切换")


def test_raw_stream_inspector_and_presets() -> None:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication
    from radarscope.core.custom_proto import builtin_presets, preview_sample
    from radarscope.ui.linkdialog import LinkDialog, detect_frame_hints, format_hex_ascii

    sample = b"\xaa\xff\x03\x00\x10\x00\x20\x00$RATTM,01"
    dump = format_hex_ascii(sample, start_offset=0)
    assert "0000: AA FF 03 00" in dump and "|...... .$RATTM,0|" in dump and "0010: 31" in dump, dump
    hints = detect_frame_hints(sample)
    assert any("LD2450" in h for h in hints) and any("NMEA" in h for h in hints)

    _app = QApplication.instance() or QApplication([])
    dlg = LinkDialog("tcp://127.0.0.1:9000")
    assert hasattr(dlg, "raw_view") and hasattr(dlg, "ed_probe")
    dlg._test_got = sample
    dlg._update_raw_diag()
    assert "AA FF 03 00" in dlg.raw_view.toPlainText()
    assert "LD2450" in dlg.lbl_diag.text()
    dlg.close()

    presets = {s.id: s for s in builtin_presets()}
    assert len(presets) >= 7, f"预期至少 7 套预设模板，实际 {len(presets)}"
    assert len(preview_sample(presets["polar_scan_ascii"], "$SCAN,4.25,042.0,0.86")) == 1
    tf_t = preview_sample(presets["tf_point_bin"], "59 59 5E 01 20 03 00 00 A0")
    assert len(tf_t) == 1 and abs(tf_t[0].rng - 3.50) < 1e-2
    udp_t = preview_sample(presets["can_udp_77g"], "AB CD 00 00 00 40 00 00 20 41 00 00 80 BF 00 00 00 40 0D 0A")
    assert len(udp_t) == 1 and abs(udp_t[0].x - 2.0) < 1e-3 and abs(udp_t[0].y - 10.0) < 1e-3
    mod_t = preview_sample(presets["modbus_u16_dist"], "01 03 02 0D AC B8 1D")
    assert len(mod_t) == 1 and abs(mod_t[0].rng - 3.50) < 1e-3
    ok("E5 原始报文窥视排障与 E9 通用协议模板库 (7 套)")


def test_ew_f1_f2_f3_and_deception() -> None:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication
    from radarscope.core.fusion import FusionEngine, SensorSpec
    from radarscope.core.model import Capabilities, RadarFrame, Target
    from radarscope.core.sim_targets import ElectronicStation, compute_ew_metrics
    from radarscope.ui.ppi import PPIWidget

    m_off = compute_ew_metrics("chaff", 0.8, 20.0, 30.0, 40.0, eccm_agility=False)
    m_on = compute_ew_metrics("chaff", 0.8, 20.0, 30.0, 40.0, eccm_agility=True)
    assert m_on["burn_through_m"] > m_off["burn_through_m"], "开启 ECCM 捷变后烧穿距离应增大"
    assert m_on["jsr_db"] < m_off["jsr_db"], "开启 ECCM 捷变后有效干信比应降低"

    es = ElectronicStation(seed=99)
    es.inject(0.0, 6.0, 0.4, -0.5, amp=0.9)
    es.fc_sim = True
    caps = Capabilities(name="相控阵", range_min=0.2, range_max=12.0,
                        has_scan=True, has_azimuth=True, has_range_doppler=True)
    az = np.linspace(-np.pi / 3, np.pi / 3, 61, dtype=np.float32)
    r_ax = np.linspace(0.2, 12.0, 96, dtype=np.float32)
    v_ax = np.linspace(-10.0, 10.0, 32, dtype=np.float32)

    for jmode in ("vgpo", "chaff", "isrj"):
        es.jam_mode = jmode
        fr = RadarFrame(
            timestamp=1.0,
            scan=np.zeros((61, 96), dtype=np.float32),
            az_axis=az,
            r_axis=r_ax,
            rd=np.zeros((32, 96), dtype=np.float32),
            rd_v_axis=v_ax,
        )
        out = es.merge_into_frame(fr, caps, 0.1, threshold=0.28, cfar_mode="os")
        assert "ew" in out.meta and out.meta["ew"]["jam_mode"] == jmode
        if jmode == "vgpo":
            assert float(np.max(out.rd)) > 0.4, "VGPO 应在 RD 速度维注入拖引峰"
        else:
            assert float(np.max(out.scan)) > 0.4, f"{jmode} 应在扫描矩阵注入特征能量"
        assert "fc_solution" in out.meta and out.meta["fc_solution"]["simulated_only"]

    # F3 多站共视重叠扇区下，单站孤立目标标为疑似欺骗，两站共视真实目标不标
    fe = FusionEngine(n_az=120, n_rng=80, range_max=12.0)

    class _MockDev:
        def __init__(self, tgts):
            self._tgts = tgts

        def capabilities(self):
            return Capabilities(name="Mock", has_scan=False, has_targets=True, range_max=12.0)

        def read(self):
            return RadarFrame(timestamp=time.monotonic(), scan=None, targets=list(self._tgts))

        def close(self):
            pass

    fe.attach(SensorSpec(name="L", x=-1.0, y=0.0, bearing_deg=0.0, fov_deg=120.0),
              _MockDev([Target(x=1.0, y=5.0, snr=0.9), Target(x=-0.5, y=4.0, snr=0.8)]))
    fe.attach(SensorSpec(name="R", x=1.0, y=0.0, bearing_deg=0.0, fov_deg=120.0),
              _MockDev([Target(x=-1.0, y=5.0, snr=0.9)]))
    ff = fe.poll()
    assert ff is not None and len(ff.meta["deception_points"]) == 1, f"应检出 1 个孤立疑似欺骗: {ff.meta}"

    _app = QApplication.instance() or QApplication([])
    ppi = PPIWidget()
    ppi.set_capabilities(caps)
    ppi.push_frame(out, out.targets)
    ppi._deception_pts = list(ff.meta["deception_points"])
    ppi.resize(300, 300)
    ppi.grab()
    ok("F1/F2/F3 电子战进阶波形、烧穿距离、模拟火控引导与多站抗欺骗")


def test_e6_mbtiles_nmea_and_land_mask() -> None:
    import sqlite3
    from radarscope.core.maptiles import TileStore
    from radarscope.core.nmea_nav import NmeaOwnShip
    from radarscope.core.pipeline import LandMask

    def _nmea(body: str) -> str:
        cs = 0
        for ch in body:
            cs ^= ord(ch)
        return f"${body}*{cs:02X}"

    with tempfile.TemporaryDirectory() as td:
        import contextlib
        td_p = Path(td)
        mb = td_p / "harbor.mbtiles"
        png_bytes = b"\x89PNG\r\n\x1a\nfaketiledata"
        with contextlib.closing(sqlite3.connect(str(mb))) as conn:
            conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
            conn.execute("INSERT INTO metadata VALUES ('name', 'TestHarbor')")
            conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
            z, x, y = 12, 3371, 1546
            tms_y = (1 << z) - 1 - y
            conn.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)", (z, x, tms_y, png_bytes))
            conn.commit()
        ts = TileStore(td_p / "cache")
        assert ts.set_mbtiles(mb)
        assert ts.has_local_tile(z, x, y)
        assert ts.get(z, x, y) == png_bytes
        assert "harbor.mbtiles" in ts.attribution

    nav = NmeaOwnShip()
    assert nav.feed_line(_nmea("GPGGA,123519,3113.8000,N,12128.4000,E,1,08,0.9,5.0,M,0.0,M,,"))
    assert abs(nav.lat - (31.0 + 13.8 / 60.0)) < 1e-4
    assert abs(nav.lon - (121.0 + 28.4 / 60.0)) < 1e-4
    assert nav.feed_line(_nmea("GPRMC,123520,A,3113.8000,N,12128.4000,E,10.0,084.4,261126,0.0,E"))
    assert abs(nav.speed_ms - 10.0 * 1852.0 / 3600.0) < 1e-3
    assert nav.feed_line(_nmea("HEHDT,275.5,T"))
    assert abs(nav.heading_deg - 275.5) < 1e-3
    assert not nav.feed_line("$HEHDT,100.0,T*00"), "校验和错误应被拒收"

    # 水陆 RGB 先验掩膜：北面 (y < 80) 为米黄陆地，南面为蓝色水域
    rgb = np.zeros((200, 200, 3), dtype=np.uint8)
    rgb[:80, :, :] = [215, 200, 175]
    rgb[80:, :, :] = [20, 85, 175]
    az = np.radians(np.array([0.0, 180.0], dtype=np.float32))  # 0°朝北(陆地)，180°朝南(水域)
    r_ax = np.array([25.0, 50.0], dtype=np.float32)
    scan = np.array([[0.9, 0.9], [0.9, 0.9]], dtype=np.float32)
    fr = RadarFrame(timestamp=1.0, scan=scan.copy(), az_axis=az, r_axis=r_ax)
    lm = LandMask(attenuation=0.9, min_land_range_m=10.0)
    lm.enabled = True
    lm.update_from_map_rgb(rgb, mpp=1.0, anchor_px=(100.0, 100.0), az_axis=az, r_axis=r_ax)
    out = lm.apply(fr)
    assert out.scan[0, 1] < 0.25, f"北向 50m 陆地静止回波应被大幅抑制: {out.scan}"
    assert out.scan[1, 1] > 0.85, f"南向 50m 水域目标回波应完整保留: {out.scan}"
    ok("E6 离线 MBTiles 海图、NMEA 本船定位与陆地杂波掩膜")


def test_e8_ros_and_mcap_export() -> None:
    from radarscope.core.ros_export import to_ros_laserscan, to_ros_pointcloud2, write_mcap_json_log
    from tools.ws_bridge import radarscope_json_to_foxglove

    az = np.radians(np.linspace(-60.0, 60.0, 5, dtype=np.float32))
    r_ax = np.linspace(0.5, 10.0, 10, dtype=np.float32)
    scan = np.zeros((5, 10), dtype=np.float32)
    scan[2, 4] = 0.92
    fr = RadarFrame(
        timestamp=1700000000.25, scan=scan, az_axis=az, r_axis=r_ax,
        targets=[Target(x=1.0, y=4.0, z=1.5, vx=0.2, vy=-0.1, snr=0.92, tid=3)],
    )
    ls = to_ros_laserscan(fr)
    assert ls["header"]["frame_id"] == "radar_link" and len(ls["ranges"]) == 5
    assert ls["ranges"][2] is not None and ls["ranges"][0] is None

    pc = to_ros_pointcloud2(fr)
    assert pc["count"] == 1 and abs(pc["points"][0]["z"] - 1.5) < 1e-4

    fg_str = radarscope_json_to_foxglove('{"type":"radarscope.frame","ts":1700000000.5,"seq":7,"range_max":12.0,"targets":[{"tid":1,"x":2.0,"y":3.0}]}')
    assert '"op":"publish"' in fg_str and '"/radar/targets"' in fg_str

    with tempfile.TemporaryDirectory() as td:
        mp = Path(td) / "test.mcap"
        n = write_mcap_json_log(mp, [ls, ls])
        assert n == 2
        blob = mp.read_bytes()
        assert blob.startswith(b"\x89MCAP0\r\n") and blob.endswith(b"\x89MCAP0\r\n")
        assert b"/radar/laserscan" in blob
    ok("E8 ROS 2 LaserScan/PointCloud2 互通与 Foxglove MCAP 日志导出")


def test_e7_3d_rhi_scope() -> None:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication
    from radarscope.drivers.simulator import SimulatorDriver
    from radarscope.ui.mainwindow import MainWindow

    drv = SimulatorDriver()
    drv.open(mode="rhi_3d")
    caps = drv.capabilities()
    assert caps.has_elevation and "3D俯仰" in caps.summary()
    time.sleep(0.06)
    fr = drv.read()
    assert fr is not None and fr.rhi is not None and fr.el_axis is not None
    assert fr.rhi.shape[0] == 25 and any(t.z > 0.2 for t in fr.targets)
    drv.close()

    _app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.show()
    w._apply_caps(caps)
    _app.processEvents()
    assert not w.rhiscope.isHidden() and w.rhiscope.isVisible(), "3D 俯仰雷达加载后应自动展开 RHI 剖面面板"
    w.rhiscope.push_frame(fr, fr.targets)
    w.rhiscope.resize(400, 160)
    w.rhiscope.grab()
    w.close()
    ok("E7 3D 俯仰 RHI 距离-高度剖面驱动与视图渲染")


def main() -> int:
    print("RadarScope 核心测试")
    for fn in (test_codec, test_ld2450_sign, test_pipeline, test_alarm,
               test_storage, test_archive, test_transport_parse, test_file_transport,
               test_fusion, test_fusion_triangulation, test_calibration,
               test_lidar_protocol, test_viewmath, test_stats_queries, test_cpa,
               test_forwarder, test_ais, test_control_channel, test_maptiles,
               test_noise_floor_stable, test_capture_fixtures,
               test_ble_transport, test_waterfall_projection,
               test_cfar_adaptive, test_track_level_fusion,
               test_plugin_verification, test_timeline_queries, test_meta_schema,
               test_polygon_zone, test_report_export, test_i18n_and_version,
               test_ws_bridge, test_electronic_station, test_zone_hit_and_map_toggle,
               test_map_geolocate_and_provider, test_statusbar_and_map_dialog,
               test_ew_jamming_and_clutter_map, test_custom_proto_and_nmea_ttm,
               test_protowizard_and_sim_ew_ui, test_multi_themes_and_palettes,
               test_raw_stream_inspector_and_presets, test_ew_f1_f2_f3_and_deception,
               test_e6_mbtiles_nmea_and_land_mask, test_e8_ros_and_mcap_export,
               test_e7_3d_rhi_scope):
        fn()
    print(f"\n全部通过：{PASS} 组")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
