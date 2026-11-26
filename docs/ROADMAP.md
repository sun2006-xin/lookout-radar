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
| 源码 | 约 11 400 行 / 61 个 Python 文件 / 20 组测试 |
| 主窗口 | `ui/mainwindow.py` 1021 行 + `ui/controllers.py` 648 行（三个 mixin） |
| 打包产物 | `dist/Lookout/Lookout.exe`（PyInstaller，带 `assets/app.ico`） |
| 桌面入口 | `桌面\瞭望 Lookout.lnk`（手写 MS-SHLLINK） |
| 仓库 | https://github.com/sun2006-xin/lookout-radar （public, main, MIT） |
| 主题 | 现代白（默认，色板 daylight）/ 深色（色板 ice） |
| 已闭环 | [REVIEW.md](REVIEW.md) 20 项全部处理，仅 4 条剩余风险 |

**开工前必须跑**（三条都过才算环境正常）：

```bat
.venv\Scripts\python.exe tests\test_core.py     :: 20 组全过
.venv\Scripts\python.exe tools\check.py         :: 编译 + 测试 + 链路自检
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

### B1 · 瀑布图重设计 `难度 ★★★ 数据形态`

- **问题**：`waterfall.py` 用 `range_profile = scan.max(axis=0)`（各方位最大值）。
  6 站融合时目标被抹宽、多站叠加 → 整片接近峰值，看起来是一个发亮的 X。
  AGC / gamma 都调过，**不是动态范围问题，是投影方式错了**。
- **方案 A（推荐）**：改成"每个航迹一条距离轨迹线"——从 `tracker.tracks`
  按 `tid` 取 `rng`，在 (时间, 距离) 平面上画折线，背景留空。这样多目标不会互相糊。
- **方案 B**：按方位分块（每 30° 一条带），适合看扇区遮挡。
- **涉及**：`ui/waterfall.py`、`ui/ppi.py`（共享配色）、`ui/mainwindow.py` 的帧分发。
- **验收**：6 站融合演示下能数清 5 条独立航迹；静止目标是一条直线，不会随时间变宽。
- **配套**：加 `test_waterfall_projection`（输入 5 个目标，断言 5 条独立轨迹）。

### B2 · CFAR 参数随场景自适应 `难度 ★★★★ 信号处理`

- **问题**：`pipeline.py` 的 CA-CFAR 门限是工具栏固定值（默认 0.30）。
  杂波强的环境（室内金属、雨雪）会满屏假目标，安静环境又会漏检。
- **方案**：实现 OS-CFAR 或 GO-CFAR 变体，按局部噪声功率自动定门限；
  保留手动门限作为覆盖项。参考 ARCHITECTURE 第四节。
- **涉及**：`core/pipeline.py`、`tests/test_core.py`（新增杂波场景用例）
- **验收**：造一组"信噪比从 3dB 到 20dB"的合成数据，
  自适应模式的虚警率与漏检率都优于固定 0.30；性能指标写进 `tools/bench.py`。
- **风险**：训练窗落在目标自己身上会把背景抬高（HANDOVER 第 6 节已有同类坑）。

### B3 · 多站加权融合与航迹级融合 `难度 ★★★★`

- **问题**：当前 `fusion.py` 是**点级**融合（先拼矩阵再检测），
  交会退化、权重缺失；多站共视时重复目标靠 `tracker` 事后合并。
- **方案**：增加"航迹级融合"路径——各站独立检测 → 按 `tid` 时空对齐 →
  权重按各站 SNR / 距离 / 视角几何分配 → 卡尔曼更新。
  保留点级融合作为 `Capabilities.form == "fusion"` 的可选分支，不要直接替换。
- **涉及**：`core/fusion.py`、`core/tracker.py`、`core/geometry.py`、`core/model.py`（可能加字段）
- **验收**：3 站模拟数据下，融合航迹位置误差低于任一单站；共址传感器拒绝交会。
- **风险**：改 `model.py` 会影响所有驱动，**先读 ARCHITECTURE 第二节**。

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

### C1 · 渲染与处理分进程 `难度 ★★★★`

- **问题**：目前 `ReaderThread`（QThread）负责取帧，但 CFAR、融合、PPI 渲染
  都在主线程。6 站融合 + 地图瓦片 + 录像同时开会掉帧（实测 17 fps 左右）。
- **方案**：把"取帧→检测→融合"移到一条独立 worker，
  用 `QImage`/`np.ndarray` 通过信号传递（注意 numpy 零拷贝与生命周期）；
  主线程只做 paint。ARCHITECTURE 第八节有性能预算，先读。
- **涉及**：`ui/controllers.py`（`ConnectionController`）、`ui/mainwindow.py`（`_on_frame`）、
  `ui/ppi.py`
- **验收**：`tools/bench.py` 里主循环 > 30 fps；录像开启时帧率下降 < 15%。

### C2 · 插件沙箱与签名 `难度 ★★★ 安全`

- **问题**：`plugins/*.py` 是任意代码，目前只有一条警示文案（REVIEW S3）。
- **方案**：至少做到「插件清单 + SHA256 校验 + 首次加载二次确认」；
  真隔离需要子进程 + 受限 IPC，工作量大，可分两期。
- **涉及**：`core/plugins.py`、`ui/pluginsdialog.py`、`NOTICE.md`

### C3 · 时间轴轨迹回放 `难度 ★★★`

- **目标**：从 SQLite 读出历史会话，用拖动时间轴重放航迹、告警、画面。
- **涉及**：`ui/history.py`（现有查库导出）、新增 `ui/timeline.py`、
  `core/storage.py`（需要按时间窗口查询的索引）
- **验收**：能拖到任意秒，CPA/告警状态与该时刻一致；10 万行航迹下滑动不卡。

### C4 · 协议抽象层与版本协商 `难度 ★★`

- **问题**：`RadarFrame.meta` 是自由字典，各驱动各写各的键，没有 schema。
- **方案**：定义 `meta` 的 TypedDict/数据类，给转发协议加 `"v"` 之后
  继续补一个 schema 注册；老驱动缺字段时填默认值而不是 KeyError。
- **涉及**：`core/model.py`、`core/forwarder.py`、`drivers/*`
- **验收**：所有驱动的 meta 通过同一个校验函数；`check.py` 加一条静态检查。

---

## 4. 阶段 D —— 产品与工程化

| 编号 | 任务 | 难度 | 验收要点 |
|---|---|---|---|
| D1 | 多边形警戒区（当前只有圆） | ★★ | 支持拖拽顶点、点在内外判定、存进 `rules.json` 且向下兼容圆形规则 |
| D2 | 报表导出（PDF/CSV 汇总） | ★★ | 会话起止、告警统计、航迹图；不引入重量级依赖 |
| C5→D3 | 英文界面（i18n） | ★★★ | 所有用户可见字符串走 `tr()`，主题/单位可切；中文不能硬编码在 paint 里 |
| D4 | 安装包签名 + 自动更新检查 | ★★★ | Inno Setup 脚本（`installer/radarscope.iss`）现成，补版本探测 |
| D5 | CI 扩展到 Linux / macOS | ★★ | 当前 CI 只有 Windows；`winstyle.py` 的 DWM 分支必须能优雅跳过 |
| D6 | Web 订阅端（转发协议的消费者示例） | ★★ | 一个单页 HTML 订阅 UDP→WebSocket 桥，用来证明"雷达服务器"定位 |

---

## 5. 明确不做（避免下一位 AI 走弯路）

- **不改名 Python 包**：`radarscope` 是内部标识，产品名 `瞭望 Lookout` 只影响 UI/exe/文档。
  改包名会破坏所有 import、插件、PyInstaller 收集规则，收益为零。
- **不引入账号 / 云端**：定位见 [ADR-0002](ADR/0002-product-positioning.md)，本地工具。
- **不做矢量地图编辑**：栅格瓦片够用，见 REVIEW 剩余风险 3。
- **不追 AIS 全报文**：只解位置报告（1/2/3/18/19），静态/航程报文对显控台没价值。
- **不做 3D 点云**，除非 B4 拿到真实 3D 数据。

---

## 6. 建议的开工顺序（给下一位 AI）

```
第 1 天：跑通 0 节三条命令 → 读 HANDOVER 第 4/6 节 → 做 A5（录制夹具）
         理由：没有可重复的真机样本，后面 A1~A4 每次都要插板子，效率极低。
第 2~4 天：A1 → A2 → A3 → A4（按手头硬件挑一个先做，不必全做）
第 2 周：B1（瀑布图）→ B2（CFAR 自适应）
         这两项纯软件、可测、见效快，适合在没有硬件的日子推进。
第 3~4 周：C1（分进程）→ B3（航迹级融合）
第 2 个月起：B4（ADC/FFT）—— 等硬件到位再开。
穿插：D 系列按需求随时插队，单件工作量都不大。
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
