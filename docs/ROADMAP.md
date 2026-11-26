# 开发路线图与未来计划

> 版本：2026-11-26 · 状态：**软件侧功能与审查已闭环，下一阶段主战场是硬件与算法深水区**
> 本文件是给"接手继续开发的人 / AI"的主入口。每个任务都写了**目标、涉及文件、
> 实现路径、验收标准、风险**，可以直接照着开工，不需要再问一遍上下文。
>
> 读之前先看：[HANDOVER.md](HANDOVER.md)（怎么改、禁忌、坑）、
> [STRUCTURE.md](STRUCTURE.md)（每个文件干什么）、[ARCHITECTURE.md](ARCHITECTURE.md)（分层与数据契约）。

---

## 0. 当前快照（接手时先确认这几件事）

| 项 | 值 |
|---|---|
| 项目根 | `D:\workbudy\2026-11-25-19-11-27\radarscope`（见 HANDOVER 第 0 节） |
| 产品名 | 瞭望 Lookout（Python 包名仍是 `radarscope`，**不要改包名**） |
| 源码 | 约 13 200 行 / 73 个 Python 文件 / 39 组测试 |
| 主窗口 | `ui/mainwindow.py` + `ui/controllers.py`（三个 mixin） |
| 打包产物 | `dist/Lookout/Lookout.exe`（PyInstaller，带 `assets/app.ico`，Release `v1.1.0`） |
| 桌面入口 | `桌面\瞭望 Lookout.lnk`（`tools/make_shortcut.py`，WScript.Shell 优先 + MS-SHLLINK 回退） |
| 仓库 | https://github.com/sun2006-xin/lookout-radar （public, main, MIT） |
| 主题 | 现代白（默认，色板 daylight）/ 深色（色板 ice） |
| 已闭环 | [REVIEW.md](REVIEW.md) 20 项、A5 夹具、B1~B3、C1~C4、D1~D6、E1~E4（软件适配/EW 教学/杂波抑制/多普勒着色） |

**开工前必须跑**（三条都过才算环境正常）：

```bat
.venv\Scripts\python.exe tests\test_core.py     :: 39 组全过
.venv\Scripts\python.exe tools\check.py         :: 编译 + 测试 + 链路自检 + 夹具回归
.venv\Scripts\python.exe main.py --selftest     :: 退出码 0，不弹窗
```

改完 `core/` 必须跑前两条；改完驱动还要在界面里连一次；打包后跑 `Lookout.exe --selftest`。

---

## 1. 阶段 A —— 真机链路（**最高优先级，先做这个**）

> 为什么排第一：软件侧 9 个驱动里 **4 个从没接过真实硬件**。
> 现在所有"可靠性"结论都建立在模拟器与离线测试上，这是整个项目最大的未知数。
> 继续加界面功能的边际收益远低于把真机跑通。

### A1 · LD19 / LD06 旋转激光雷达联调 `难度 ★★★ 硬件`

- **目标**：真实 LD19 通过 USB-TTL 出 360° 点云，PPI 上出现完整扫描圈，好帧/坏帧统计合理。
- **涉及**：`drivers/ldlidar.py`、`core/codec.py`、`core/transport.py`、`ui/linkdialog.py`
- **实现路径**：
  1. 用「连接设置 → 测试连接」确认能收到字节，记下波特率（LD19 常见 230400）。
  2. 端点加 `&record=capture.bin` 录 10 秒原始流，用这个 bin 做回归夹具。
  3. 核对 `ldlidar.py` 里的帧头、点数量、CRC 表、角度跨 0° 处理；现有测试
     `test_lidar_protocol` 只覆盖了造出来的包，没覆盖真机碎片重同步。
  4. 把 `capture.bin` 喂给 `replay` 驱动，确认离线可复现（这样以后不用一直插着板子）。
- **验收**：连续跑 5 分钟，坏帧率 < 1%；拔插 USB 后「自动重连」能恢复；
  PPI 无放射状条纹、无角度跳变。
- **风险**：真机点密度与模拟器不同，`pipeline.synthesize_scan` 的散射插值可能露洞
  （已知解法：`fusion._close_gaps` 的 3×3 闭运算）。

### A2 · LD2450 / LD2410 毫米波联调 `难度 ★★ 硬件`

- **目标**：三目标 x/y 与 9 距离门能量正确显示，符号位不翻。
- **涉及**：`drivers/ld2450.py`、`drivers/ld2410.py`、`core/pipeline.py`
- **实现路径**：录原始流 → 对照厂商协议逐字段核对 → 重点验 `bit15` 独立符号位
  （不是补码，已有测试但只测了构造值）→ 检查多目标时 `Target.tid` 是否稳定。
- **验收**：人手在雷达前左右移动，航迹表 `tid` 不跳号、速度方向不反。
- **风险**：LD2450 的"无目标"帧与"零坐标"帧容易混淆，会画出原点假目标。

### A3 · BLE 链路（bleak 可选依赖） `难度 ★★★ 硬件`

- **目标**：`ble://` 端点能扫描、连接、订阅 notify，断连后自动重连。
- **涉及**：`core/transport.py`、`ui/linkdialog.py`
- **实现路径**：bleak 是异步 API，当前 transport 是同步 `read()`。
  需要一个**后台线程跑 asyncio event loop**，用 `queue.Queue` 把 notify 数据桥回同步接口；
  不要试图把 async 冒泡到 UI 层。
- **验收**：断开设备 10 秒内自动恢复；`test_transport` 增加一组 mock-queue 用例。
- **风险**：Windows 上 bleak 需要 WinRT，打包时 PyInstaller 常漏 hidden import，
  要检查 `--collect-submodules bleak` 是否够。

### A4 · 真实多站组网（≥2 台设备） `难度 ★★★★ 硬件 + 算法`

- **目标**：两台同型雷达按 `sensors.json` 布站，融合出的目标位置优于单站。
- **涉及**：`drivers/multi.py`、`core/fusion.py`、`core/geometry.py`、
  `ui/sensorsdialog.py`、`ui/calibrationwizard.py`
- **实现路径**：
  1. 先跑标定向导，把装偏角写进 `sensors.json`（现在只验过合成数据）。
  2. 用 `geometry` 的最小二乘交会核对 3 站以上的加权结果 —— 当前实现**未加权**，
     2 站时结果与权重无关，3 站以上才有意义（见 STRUCTURE 的"还没做的"）。
  3. 检查盲区计算与 `ppi._draw_coverage` 是否一致。
- **验收**：已知位置的角反射器，融合误差 < 单站误差；盲区标注与实际漏检一致。
- **风险**：不同设备的 `range_min/max` 不一致时，全局距离格对齐会错位。

### [x] A5 · 录制回放作为回归夹具（**已完成，2026-11-26**） `难度 ★★ 纯软件`

- **目标**：把每段真机录制变成可重复的自动化测试输入，以后改协议不用插板子。
- **涉及**：`core/storage.py`（`FrameArchive`）、`drivers/replay.py`、`core/transport.py`、`drivers/ld2410.py`、`tests/test_core.py`、`tools/capture_check.py`、`tools/check.py`
- **实现与实测结论**：
  - 新增 `tools/capture_check.py`（支持单文件 `.bin`/`.rad` CLI 校验与 `tests/fixtures/manifest.json` 批量回归），已接入 `tools/check.py`（4/4）与 `tests/test_core.py`（`test_capture_fixtures`，第 21 组测试）。
  - `tests/fixtures/` 已放置 5 份离线回归样本（`ld19_sample.bin` / `ld2450_sample.bin` / `ld2410_sample.bin` / `ultrasonic_sample.bin` / `sim_sample.rad`，总计 < 10 KB），覆盖帧数、峰值、目标数、CRC 拒绝数与碎片重同步。
  - 夹具回归顺手发现并修复 3 个真实问题：`ReplayDriver` 非循环回放丢最后一帧、`LD2410Driver` 在 `file://` 下发配置命令报错、`FileTransport.read(n)` 忽略 `n` 导致 `readline()` 失效。
  - 后续真机联调（A1~A4）录到真实 `.bin` 后，只需脱敏放进 `tests/fixtures/` 并在 `manifest.json` 追加条目即可。

---

## 2. 阶段 B —— 算法深水区（`难度深` 的主战场）

### [x] B1 · 瀑布图重设计（**已完成，2026-11-26**） `难度 ★★★ 数据形态`

- **问题**：`waterfall.py` 原先用 `range_profile = scan.max(axis=0)`（各方位最大值），6 站融合时目标被抹宽、多站叠加整片饱和。
- **实现与实测结论**：
  - `radarscope/ui/waterfall.py` 已按方案 A 重写为「每条航迹一条距离-时间折线」，背景留空、最新位置标注 `T{tid}`。
  - `MainWindow._on_frame` 已将 `all_tracks` 传入 `waterfall.push_frame(f, all_tracks)`。
  - `tests/test_core.py` 新增 `test_waterfall_projection`（5 个独立目标、8 帧滚动，断言 5 条独立轨迹且静止目标距离恒定）。

### [x] B2 · CFAR 参数随场景自适应（**已完成，2026-11-26**） `难度 ★★★★ 信号处理`

- **实现与实测结论**：
  - `core/pipeline.py` 新增 GO-CFAR（`_go_cfar_background`）、OS-CFAR（`_os_cfar_background`）与局部标准差自适应门限（`cfar_mode="auto"`，使用 `guard=3, train=6` 防宽波束目标旁瓣抬高背景估计）。
  - `tests/test_core.py` 新增 `test_cfar_adaptive`，验证强杂波场抑虚警与安静场抓弱目标均优于固定 0.30 CA-CFAR；`tools/bench.py` 实测单帧耗时：CA 0.40ms、GO 自适应 1.28ms、OS 0.42ms。
  - 「设置 → 检测算法」支持运行时切换 `auto / os / go / ca`。

### [x] B3 · 多站加权融合与航迹级融合（**已完成，2026-11-26**） `难度 ★★★★`

- **实现与实测结论**：
  - `core/geometry.py` 的 `locate(rays, weights=None)` 支持按观测权重求解加权最小二乘交会，继续拒绝平行/共址退化。
  - `core/fusion.py` 新增 `FusionEngine.fusion_mode = "point" | "track"`、`fuse_tracks()` 与 `_obs_weight()`（按 SNR、距离、偏轴几何加权），保留点级融合作为默认分支；`test_track_level_fusion` 验证 3 站共视航迹误差 < 0.25m。

### B4 · 原始 ADC → FFT 距离多普勒链路 `难度 ★★★★★ 需要新硬件`

- **前提**：必须先有能吐原始数据的采集链（IWR6843 + DCA1000，或自制 ADC 采样板）。
  **没有硬件就不要开工**，否则只能造数据自娱自乐。
- **方案**：新增 `core/dsp.py`：直流去除 → 加窗 → Range FFT → 多普勒 FFT →
  CFAR → 角度估计（Capon/MUSIC 可选）。输出仍是 `RadarFrame`，界面零改动。
- **涉及**：新增 `core/dsp.py`、新增 `drivers/rawadc.py`、`core/model.py`（扩展 meta）
- **验收**：合成 chirp 数据能还原出已知距离与速度，误差 < 一个距离门；
  `tools/bench.py` 记录单帧耗时（目标 < 20 ms/帧）。
- **这是整个路线图里最难的一项**，建议放到 B1/B3 之后。

---

## 3. 阶段 C —— 架构深水区

### [x] C1 · 渲染与处理后台解耦（**已完成，2026-11-26**） `难度 ★★★★`

- **实现与实测结论**：`ui/controllers.py` 的 `ReaderThread` 在后台线程完成 `pipeline.normalize()`（CFAR / 反投影 / 轴裁剪）并标 `_normalized = True`，主线程 `_on_frame` 只做航迹更新与轻量绘制；`tools/bench.py` 单帧总固定开销 2.44ms（20fps 预算仅占 5%）。

### [x] C2 · 插件沙箱与签名（**已完成，2026-11-26**） `难度 ★★★ 安全`

- **实现与实测结论**：`core/plugins.py` 新增 `file_sha256()` 与 `PluginTrustStore`（`data/trusted_plugins.json`），支持 `require_trust=True` 拦截未确认插件；`ui/pluginsdialog.py` 展示 SHA256 摘要并提供「信任并重载所选」二次确认；`test_plugin_verification` 覆盖。

### [x] C3 · 时间轴轨迹回放（**已完成，2026-11-26**） `难度 ★★★`

- **实现与实测结论**：`core/storage.py` 新增基于 `(sid, ts)` 索引的 `tracks_window()` / `events_until()`；新增 `ui/timeline.py`（`TimelineDialog` 滑块拖动 + 1x/2x/5x 播放 + PPI 与历史事件联动）并接入 `ui/history.py`；`test_timeline_queries` 覆盖。

### [x] C4 · 协议抽象层与版本协商（**已完成，2026-11-26**） `难度 ★★`

- **实现与实测结论**：`core/model.py` 新增 `META_SCHEMA` 与 `validate_frame_meta()`（缺失标准字段补默认值、类型错误直接检出），已接入 `tools/selftest.py` 与 `test_meta_schema`。

---

## 4. 阶段 D —— 产品与工程化

| 编号 | 任务 | 状态 | 实现与验收要点 |
|---|---|---|---|
| D1 | 多边形警戒区 | `[x]` 已完成 | `core/alarm.py` 新增 `poly_zone` + 射线法 `point_in_polygon()`；`ui/ppi.py` 与 `controllers.py` 支持与圆形规则共存及持久化；`test_polygon_zone` 覆盖 |
| D2 | 报表导出 | `[x]` 已完成 | 新增 `core/report.py`（自包含 HTML 含 SVG 航迹图 + CSV 汇总，零重量级依赖），接入 `ui/history.py`；`test_report_export` 覆盖 |
| D3 | 英文界面（i18n） | `[x]` 已完成 | 新增 `core/i18n.py` 与「设置 → 界面语言（中文/English）」实时切换；`test_i18n_and_version` 覆盖 |
| D4 | 安装包脚本 + 版本探测 | `[x]` 已完成 | 新增 `radarscope/version.py`（`__version__` + `check_latest_release` 超时检查 + `帮助 → 检查更新…`），`installer/radarscope.iss` 对齐 `Lookout` |
| D5 | CI 扩展到 Linux / macOS | `[x]` 已完成 | `.github/workflows/ci.yml` 矩阵已覆盖 `windows-latest / ubuntu-latest / macos-latest`（已修复 `cp1252` 与 POSIX `file:///` 解析） |
| D6 | Web 订阅端 | `[x]` 已完成 | 新增零依赖 `tools/ws_bridge.py`（UDP→RFC 6455 WebSocket 桥）+ `tools/web_viewer.html` 单页实时态势；`test_ws_bridge` 覆盖 |

---

## 4.5 阶段 E —— 通用雷达生态、专业显控与教学演练（软件层先做完）

| 编号 | 任务 | 状态 | 实现与验收要点 |
|---|---|---|---|
| E1 | 多普勒趋近着色 & 静态杂波抑制 | `[x]` 已完成 | 借鉴 `OpenCPN radar_pi` 与 `Smart Radar`：`ui/ppi.py` 按径向接近速率着暖红/青蓝；`core/pipeline.py` 新增 `ClutterMap` 学习静态背景杂波并抑制 |
| E2 | 电子对抗 / 无线电干扰教学仿真 | `[x]` 已完成 | **纯软件信号建模，无射频发射**：`core/sim_targets.py` + `ui/simdialog.py` 支持宽带压制/扇区瞄准/DRFM多假目标/RGPO距离拖引，实时对比 `CA/OS/GO-CFAR` |
| E3 | 零代码通用雷达协议适配器 | `[x]` 已完成 | 新增 `core/custom_proto.py`、`drivers/custom_generic.py`、`ui/protodialog.py`，内置 NMEA `$RATTM` 航海雷达、77G `$TGT` CSV 与二进制结构体帧，开箱即用 |
| E4 | 航海 / 车载 / 无人机预设场景 | `[x]` 已完成 | `ElectronicStation` 新增 `marine`（航道避碰）、`automotive`（77G 多车道）、`drone`（低空周界突防）动态演练场景 |
| E5 | 原始报文实时窥视与字节切帧诊断 | `[ ]` 排期 | 借鉴 Foxglove/串口调试器：在「连接设置 → 测试连接」旁实时滚动 Hex/ASCII 原始流并标出帧头/长度匹配位，快速排障波特率/字序 |
| E6 | 离线 MBTiles 海图、NMEA 本船定位与陆地掩膜 | `[ ]` 排期 | 借鉴 `OpenCPN` 与 `Furuno Smart Radar`：支持离线 `.mbtiles`、解析 `$GPGGA/$GNRMC/$HEHDT/$RARSD` 自动更新本船坐标航向、陆地静止回波掩膜 |
| E7 | 3D 俯仰 RHI 显示（Distance-Height） | `[ ]` 排期 | 借鉴 ROS RViz2：针对 4D 成像毫米波/3D 激光（`Capabilities.has_elevation`），扩展距离-高度（RHI）剖面与高度分层过滤 |
| E8 | 机器人与遥测标准协议互通（ROS / Foxglove） | `[ ]` 排期 | 借鉴 `Foxglove Studio`：扩展 `tools/ws_bridge.py` 与导出链路，支持标准 `LaserScan / PointCloud2` JSON 及 `.mcap` 回放日志互通 |
| E9 | 常用消费/工业雷达零代码模板扩充 | `[ ]` 排期 | 扩充 `core/custom_proto.py` 开箱预设库（思岚 RPLIDAR / 北醒 TF 系列 / 常见 Modbus-RTU / CAN-UDP 77G 帧等），选型即连 |

---

## 4.6 阶段 F —— 军方级模拟电子战与抗干扰教学演练（软件仿真，禁止控制真实武器或物理射频硬件发射）

| 编号 | 任务 | 状态 | 实现与验收要点 |
|---|---|---|---|
| F1 | 进阶电子进攻（EA）与模拟射频波形扩展 | `[ ]` 排期 | 在 `E2`（barrage/spot/drfm/rgpo）基础上，补充 **VGPO 速度波门拖引**（RD 多普勒维）、**箔条云（Chaff）面状散射** 与 **ISRJ 间歇采样转发干扰** 等数字射频波形建模 |
| F2 | ECCM 抗干扰对照、烧穿曲线与模拟火控推演 | `[ ]` 排期 | 新增烧穿距离（Burn-through Range）理论圈绘制、`JSR(dB)` 实时显示、模拟频率捷变/PRF 参差、教学级模拟跟踪锁定/截击航路推演，支持原始 vs 抗干扰同屏对比 |
| F3 | 多站交叉抗欺骗与电子侦察（RWR/ESM） | `[ ]` 排期 | 利用 `sensors.json` 多站共视校验：无他站交叉印证的孤立假目标标黄提示「疑似欺骗」；PPI 外环增加干扰源方位截获扇区告警（ESM/RWR） |

---

## 5. 明确不做与合规边界（避免下一位 AI 走弯路）

- **软件仿真不设限，真实硬件严禁控武器/发物理干扰**：教学/科研向的模拟电子战（模拟射频链路方程、模拟火控跟踪推演与拦截几何、DRFM/RGPO/VGPO）全部支持；但**禁止向真实无线电功放硬件下发违规频段干扰发射指令，或接入实弹武器火控作动器**。
- **不改名 Python 包**：`radarscope` 是内部标识，产品名 `瞭望 Lookout` 只影响 UI/exe/文档。
  改包名会破坏所有 import、插件、PyInstaller 收集规则，收益为零。
- **不引入账号 / 云端**：定位见 [ADR-0002](ADR/0002-product-positioning.md)，本地工具。
- **不做矢量地图编辑**：在线/离线栅格瓦片（含后续 MBTiles 与掩膜层）够用，见 REVIEW 剩余风险 3。
- **不追 AIS 静态航程冗余报文**：聚焦位置报告（1/2/3/18/19）与航海 TTM/GGA/HDT 对显控真正有用的报文。

---

## 6. 建议的开工顺序（给下一位 AI）

> **当前进度（2026-11-26）**：`A5`、`B1~B3`、`C1~C4`、`D1~D6`、`E1~E4` 已全部完成并有 39 组测试兜底；以下为后续优先级顺序（**在未安排硬件联调前优先推进纯软件部分**）：

```
软件主线 1（航海/通用零代码生态）：E5（原始流窥视排障）→ E9（扩充零代码驱动模板库）→ E6（离线 MBTiles + NMEA 本船定位 + 陆地掩膜）
软件主线 2（电子战教学与信号对照）：F1（VGPO/箔条/ISRJ 干扰样式）→ F2（烧穿距离圈 + 捷变抗干扰对比）→ F3（多站抗欺骗与 ESM 方位指示）
软件主线 3（机器人/三维扩展）：  E8（ROS/Foxglove 生态互通）→ E7（俯仰 RHI 剖面）
硬件主线（实体硬件到位后再开）：A1 → A2 → A3 → A4（真机联调与样本录制）→ B4（IWR6843+DCA1000 原始 ADC→FFT）
```

**每一项的收尾动作**（硬性）：
1. 加一组会失败的断言 → 再改 → 测试全绿
2. 更新 `STRUCTURE.md`（新文件/职责变化）、`HANDOVER.md` 第 6 节（新坑）、
   `ROADMAP.md`（勾选本项 + 写实测结论）
3. 打包后 `Lookout.exe --selftest` 退出码 0
4. commit 正文写"为什么"，不是"改了什么"（HANDOVER 第 10 节）

---

## 7. 决策门槛（什么时候该停下来问）

- 要改 `core/model.py` 的三个数据契约 → **先读 ARCHITECTURE 第二节并写一条 ADR**
- 要引入新运行时依赖 → 先确认能否进 PyInstaller 包体，且 `requirements.txt` 与
  `requirements-dev.txt` 分清楚
- 要改 UI 线程模型（C1）→ 先跑 `tools/bench.py` 留基线数据
- 沙箱/网络不通时 → 用离屏渲染与假瓦片验证逻辑，**不要假装验证过网络链路**
  （REVIEW 剩余风险 3 就是这样留下的）
- 需要启动 GUI 才能确认的观感问题 → 明确写"待用户实机确认"，别替用户下结论
