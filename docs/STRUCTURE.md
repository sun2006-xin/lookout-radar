# 项目文件具体解释

> 一个文件只干一件事。想定位模块职责或改动入口可先查下表。
> 源码约 13 500 行 / 73 个 Python 文件 / 39 组核心单元测试（不含依赖与打包产物）。
> 产品名「瞭望 Lookout」，Python 包名 `radarscope`。
>
> 配套：[ARCHITECTURE.md](ARCHITECTURE.md) 架构与数据契约 · [ROADMAP.md](ROADMAP.md) 开发路线图。

---

## 顶层

| 文件 | 具体职责 | 改动注意 |
|---|---|---|
| `main.py` | 程序入口：日志初始化、全局异常兜底、`--selftest` 分支、设置应用 Logo 图标、创建 MainWindow、套用上次主题 | `--selftest` **不创建窗口**，靠退出码表达成败；自检逻辑已抽到 `core/selftest.py` |
| `requirements.txt` | 运行时依赖（PyQt5 / numpy / pyserial / bleak） | 新依赖必须确认能进 PyInstaller 包体 |
| `README.md` | 项目门面：Logo、动图、特性表、快速开始、设备表、转发协议、插件、文档索引 | 已知限制与路线图要跟代码同步 |
| `LICENSE` | 纯 MIT 文本 | 别再夹第三方说明，否则 GitHub 识别成 "Other" |
| `NOTICE.md` | 第三方归属：OSM / CARTO 瓦片使用条件 | 用地图功能就必须保留归属（界面左下角也显示） |
| `.gitignore` | 排除 `dist/ build/ data/ .venv/ tiles/ __pycache__/ *.db *.spec`（保留 `installer/Lookout.spec`） | 别把 129MB 产物提交上去 |
| `.gitattributes` | 把 `firmware/**` 标 `linguist-vendored`，避免 `.ino` 把仓库语言统计成 C++ | 纯 GitHub 展示用 |
| `.github/workflows/ci.yml` | Windows / Linux / macOS + Python 3.11/3.12 跑 `tools/check.py` | Linux/macOS 走无头 `QT_QPA_PLATFORM=offscreen` |

### 目录

| 目录 | 内容 |
|---|---|
| `radarscope/` | Python 包：`core/` `drivers/` `ui/` `paths.py` |
| `assets/` | `logo.png`（512 透明底）、`app.ico`（7 尺寸）、`demo-v1.1.gif`（20 帧演示）；由 `tools/make_*.py` 生成 |
| `dist/Lookout/` | 打包产物：`Lookout.exe` + `_internal/` + `plugins/`。可删，重打包生成 |
| `data/` | 运行时生成，**不进版本库**：见下表 |
| `plugins/` | 用户插件目录，丢 `.py` 即生效 |
| `tools/` | 开发/文档工具（下面单列） |
| `tests/` | `test_core.py`（39 组测试）+ `fixtures/`（5 份 `.bin`/`.rad` 回归夹具与 `manifest.json`） |
| `docs/` | 全部文档（下面单列） |
| `installer/` | `radarscope.iss` 安装包脚本与 `Lookout.spec` 构建配置 |
| `firmware/` | `hc_sr04_servo.ino`，HC-SR04 + 舵机扫描的 Arduino 示例 |

**`data/` 内容**（打包版在 `%LOCALAPPDATA%\Lookout\data`）：

| 文件 | 内容 |
|---|---|
| `radarscope.db` | SQLite：会话 / 航迹点 / 告警事件（`-wal` `-shm` 是 WAL 临时文件） |
| `*.rad` | 帧归档：强度矩阵 float16 + 轴信息 |
| `rules.json` | 用户编辑/画布新增的告警规则（含自绘警戒区，id 前缀 `zone_user_`） |
| `sensors.json` | 多站布站配置（位置、朝向、视场） |
| `tiles/` | 地图瓦片磁盘缓存，超过 200MB 由 `TileStore.prune()` 自动清最旧 |
| `rec_*/` | 屏幕录像的 PNG 序列（4 帧/秒） |
| `radarscope.log` | 运行日志，崩溃后第一时间看这个 |

---

## `radarscope/core/` —— 与界面无关的心脏

**硬约束：`core/` 里不许出现 `import PyQt5`。** 破坏了就没法无界面测试。

| 文件 | 具体职责 | 关键入口 / 改动注意 |
|---|---|---|
| `model.py` | 三个数据契约：`Target`（x/y/vx/vy/snr/tid/views/threat）、`RadarFrame`（timestamp/scan/az_axis/r_axis/meta）、`Capabilities`（设备能力声明） | **极稳，改要慎重**。全部 `@dataclass(slots=True)`；换设备不改界面靠的就是它。改前读 ARCHITECTURE 第二节 + 写 ADR |
| `driver.py` | 驱动基类 `RadarDriver` + `@register` 注册表 + 可选下行控制钩子 | `resolve_capabilities()` 用 `dataclasses.replace` **一次性合并** `source/form/vendor` 类属性（A2，别再手工补）；`set_control()` 基类返回 False 表示"不支持"是正当答案 |
| `transport.py` | `serial:// / tcp:// / udp:// / ble:// / file://` 端点解析与实际读写 | `open_transport()`、`endpoint_of()`、`TransportError`；BLE 需 bleak（异步→同步桥，见 ROADMAP A3） |
| `codec.py` | 声明式切帧：`FrameSpec` + `StreamDecoder` 处理粘包、半包、数据区假帧头 | 长度字段偏移 = `len(header)`；`_total_of()` 返回 `0` 表示"等数据"、`-1` 才是非法 |
| `pipeline.py` | CA-CFAR 检测、目标反投影、`synthesize_scan` 补全缺失矩阵 | 门限目前是固定值（默认 0.30），自适应见 ROADMAP B2；训练窗别落在目标自己身上 |
| `tracker.py` | 最近邻关联 + α-β 滤波 + 航迹合并 | 丢失判据用**秒**（`missed` 是 float），用帧数会在旋转雷达上断航迹 |
| `fusion.py` | 多站空间对齐、扇区拼接、盲区计算 | `_close_gaps()` 3×3 最大值闭运算补散射洞；当前是**点级融合**，加权/航迹级见 ROADMAP B3 |
| `geometry.py` | 方位线、最小二乘交会（融合与标定共用） | 共址传感器要拒绝交会 |
| `calibration.py` | 朝向装偏标定、残差报告 | 与 `ui/calibrationwizard.py` 配套 |
| `viewmath.py` | 视图换算：北向/艏向、屏幕↔极坐标、读数文本格式 | `point_to_polar()`；角度一律用圆均值，不做算术平均 |
| `alarm.py` | 告警引擎（去抖 / 滞回 / 冷却）+ 规则序列化 | `AlarmRule(id, name, kind, params, level, ...)` 字段名是 **`id`** 不是 `rule_id`；`merge_rules` 优先级：内置 < 插件 < 用户 |
| `storage.py` | `Store`（SQLite 会话/航迹/事件）+ `FrameArchive`（`.rad`） | `flush()` 失败会 `write_errors += 1` 并保留 pending 重试（上限 5000）；状态栏会亮红字。**不许把强度矩阵存 SQLite** |
| `plugins.py` | 运行时插件加载与隔离 | 插件入口统一 `def setup(api)`，别叫 `register`（会撞驱动注册器） |
| `cpa.py` | CPA / TCPA 最近会遇距离与时间 | `evaluate(tracks, speed, heading)`、`worst()`、`own_velocity()`；推导 `t* = -(r·v)/\|v\|²` |
| `forwarder.py` | UDP 数据转发：每帧一个 JSON 包（含 `"v":1`）+ 接收端示例 | `is_public_target()` 判公网，界面二次确认；`sample_receiver()` 可直接跑来验证配置 |
| `ais.py` | AIS NMEA 解析：AIVDM 6bit 装甲、异或校验、类型 1/2/3/18/19 位置报告 | **按 bit 偏移切字段，不是按字节**；经纬度是有符号 1/10000 分；`make_aivdm()` 用来造测试数据 |
| `aisfeed.py` | UDP 监听线程（默认 10110）+ 目标表 + 6 分钟过期 | `feed_line()` 在 `running=False` 后直接返回（R4）；`snapshot(ref_lat, ref_lon)` 换成本地米制坐标 |
| `maptiles.py` | Web Mercator 换算、瓦片下载、磁盘缓存 | `zoom_for_range()` **从 z=18 往下找**第一个装得下的；`prune(200MB)` 清最旧；UA 与归属见 `PROVIDERS` |
| `selftest.py` | `run_core_checks()`：跑真实驱动 + 算法，返回 `(ok, 摘要)` | 三个入口共用：`main.py --selftest`、帮助菜单、`tools/check.py` |
| `paths.py` | 开发/打包两套数据目录解析 | `data_dir()`；打包版落到 `%LOCALAPPDATA%\Lookout\data` |
| `sim_targets.py` | 模拟电子站引擎：`ElectronicStation` 动态/行业场景 + 教学级电子对抗（噪声/DRFM/RGPO/VGPO/箔条/ISRJ + ECCM/火控推演） | `set_scenario()`、`inject()`、`merge_into_frame()`、`compute_ew_metrics()` |
| `custom_proto.py` | 零代码通用协议解析引擎：7 套开箱预设（NMEA `$--TTM`、CSV/极坐标 `$SCAN`、TF/CAN-UDP/Modbus 结构体二进制帧） | `builtin_presets()`、`preview_sample()`、`build_driver_class()` |
| `nmea_nav.py` | NMEA 本船定位与航向解析：`$GPGGA/$GNRMC/$HEHDT/$RARSD` 校验与 UDP 监听 | `NmeaOwnShip`、`feed_line()`、`snapshot()` |
| `ros_export.py` | ROS 2 / Foxglove 遥测导出与二进制 MCAP v0 日志封装 | `to_ros_laserscan()`、`to_ros_pointcloud2()`、`write_mcap_json_log()` |

---

## `radarscope/drivers/` —— 每种雷达一个文件

| 文件 | source | form | 具体职责 |
|---|---|---|---|
| `__init__.py` | — | — | 导入即注册所有驱动，**加驱动只需加一行 import** |
| `simulator.py` | sim | sector | 合成雷达：旋转/凝视双模式、底噪、静止杂波；唯一实现 `set_control()`（gain/STC/FTC）的驱动 |
| `sim_aesa.py` | sim | rd | 相控阵/脉冲多普勒：9 目标 + RD 图 + IFF + 威胁分级 |
| `multi.py` | sim / fusion | fusion | 多站融合：6×60° 全向演示 + 真实组网入口 |
| `ld2410.py` | device | point | 海凌科存在感应，9 距离门能量 |
| `ld2450.py` | device | sector | 海凌科三目标跟踪，直接给 x/y；**bit15 是独立符号位，不是补码** |
| `ldlidar.py` | device | omni | LD19/LD06/D500 旋转激光，360°，CRC + 碎片重同步 |
| `ultrasonic.py` | device | sector | HC-SR04 + 舵机，180° 机械扫描，文本协议 |
| `custom_generic.py` | device | omni/sector | 加载预设与 `custom_drivers.json`，自动注册 NMEA TTM 航海雷达 / 77G CSV / 通用串口二进制帧 |
| `replay.py` | replay | sector | 回放 `.rad` 帧归档；到文件尾标记 `_finished`，下一轮再抛，不丢最后一帧 |

**驱动里不许操作界面、不许按型号写 `if did == "..."`。** 型号只出现在 `label`/`vendor` 字符串。

> **A1~A4 待办**：`ldlidar / ld2450 / ld2410 / ble` 与真实多站组网都还没接过硬件。
> 详见 [ROADMAP.md](ROADMAP.md) 阶段 A。

---

## `radarscope/ui/` —— 只负责画和交互

**硬约束：`ui/` 里不许出现 `struct.unpack` 或 `serial`。** 协议解析漏到界面就是分层破了。

| 文件 | 具体职责 | 关键入口 / 改动注意 |
|---|---|---|
| `mainwindow.py` | 编排：工具栏、菜单、页签、帧分发、主题应用、连接生命周期 | 1021 行；继承三个控制器 mixin。**新功能优先放进 `controllers.py` 对应类**，别让这里再膨胀 |
| `controllers.py` | 三个 mixin + `ReaderThread` + `PRESET_ENDPOINTS` + `ALARM_STYLE*` 常量 | `ConnectionController`（连接/重连/回放）、`ExportController`（转发/录像/静音/导出）、`AlarmBarController`（告警条/规则/警戒区）。mixin 依赖 `self` 是 MainWindow，**搬方法时模块级常量要一起搬** |
| `theme.py` | 8 套雷达色板 + 7 套现代 UI 主题 + QSS 卡片/按钮样式 | `PALETTES`（daylight/ocean_teal/ice/aurora/mono/green/amber/cyan）、`THEMES`（light/nordic/dark/graphite/marine_night/tactical/amber_ops）、`is_dark_theme(mode)`、`theme_palette(mode)`、`build_stylesheet(mode, scale)` 用 `@bg@` 占位符替换（**不要用 f-string，QSS 全是花括号**）；`LIGHT_PALETTE_KEYS` 用于浅色/深色主题配对 |
| `ppi.py` | 环视显示器：LUT 光栅、余晖、尾迹、警戒区、AIS、地图、EBL/VRM、真运动、点击锁定 | 最复杂的控件。`_ctable_for()` 把低电平(<26)设**透明**（否则地图被盖住）；真运动裁剪 = 屏幕圆 ∩ 天线圆；`zoneDeleteRequested(rule_id)` 删点中的圈 |
| `ascope.py` | A 显（距离剖面） | `set_capabilities()` / `set_threshold()` |
| `waterfall.py` | 距离-时间瀑布图（按航迹 `tid` 画独立距离折线，B1 已重设计） | `push_frame(frame, tracks)` / `track_series()` |
| `rdscope.py` | 距离-多普勒热图 | 数组第 0 行是 `-V_MAX`，显示前 `arr[::-1]` |
| `rhiscope.py` | 3D 俯仰 RHI 距离-高度剖面图（`has_elevation` 设备自动显示） | 支持 0°~45° 仰角网格、高度分层过滤与 3D 航迹投影 |
| `tracktable.py` | 航迹表，列随 `Capabilities` 变化 | `configure(caps)` / `refresh(tracks)` / `set_locked(tid)` |
| `sensorpanel.py` | 多站传感器状态表 | `update_sensors(list[dict])`；单站驱动由主窗口合成一行回退数据，避免空表 |
| `eventlog.py` | 事件日志 + 导出 | `push(level, text)`；告警条与日志同源 |
| `history.py` | 历史会话查库 + CSV 导出 | 未来时间轴回放从这里长（ROADMAP C3） |
| `statsdialog.py` | 统计分析：KPI / 活动量图 / 告警汇总 / 事件检索 | 少于 2 个时间桶要提示"数据太少"，别画成整块色块 |
| `linkdialog.py` | 连接设置：串口枚举（识别 CH340/CP210x）、BLE 扫描、TCP/UDP、文件回放 | `测试连接` 走 **QTimer 20ms 非阻塞轮询**，按钮再点即停；`closeEvent` 收尾别把端口留后台 |
| `rulesdialog.py` | 告警规则编辑器 | 与 `alarm.py` 的序列化配套 |
| `sensorsdialog.py` | 多站布站配置 → `sensors.json` | 朝向/视场写错会让融合整体偏 |
| `calibrationwizard.py` | 标定向导：采集 → 计算 → 写入 | 对应 `core/calibration.py` |
| `pluginsdialog.py` | 插件管理（含琥珀色"任意代码"警示） | `reloaded` 信号通知主窗口 |
| `extra.py` | 小配置对话框：`ControlDialog`（增益/STC/FTC）、`AisDialog`（端口+经纬度）、`MapLocationDialog`（地图图源+IP自动定位+手动经纬度） | 设备不支持下行控制时**控件置灰并说明原因**；IP 自动定位走后台线程 |
| `maplayer.py` | 瓦片拼图 + 后台 `_Fetcher` 线程 | `stop()` 会 `requestInterruption()`；地图按 `-heading` 反向旋转对齐雷达 |
| `winstyle.py` | Windows 标题栏深浅色（DWM）+ 代码生成图标 + 全局对话框标题栏跟随 | `enable_dark_titlebar()` 属性号 **20/19 都试**；`install_dialog_titlebar()` 是应用级事件过滤器（QDialog 是独立原生窗口） |
| `simdialog.py` | 模拟电子站控制台对话框：`SimStationDialog` 管理虚拟场景、手动注入与电子对抗干扰教学 | 对应 `core/sim_targets.py`；支持 `patrol/marine/automotive/drone` 与干扰样式 |
| `protodialog.py` | 通用协议雷达适配向导：`ProtoWizardDialog` 零代码配置并在线试解析样本报文 | 对应 `core/custom_proto.py`；保存后实时刷新主窗口驱动列表 |

---

## `plugins/` —— 用户扩展

| 文件 | 演示什么 |
|---|---|
| `example_sweep_ranger.py` | 用插件注册一台新雷达（单波束测距） |
| `parking_rules.py` | 用插件注册一组告警规则（泊车分区，`zone` 类规则会直接画到 PPI 上） |

插件是**任意 Python 代码，加载即执行**（ROADMAP C2 要做沙箱/校验）。

---

## `tools/` —— 开发 / 文档工具

| 文件 | 具体职责 |
|---|---|
| `check.py` | **提交前必跑**：编译 + 45 组测试 + 链路自检 + 录制夹具回归，判据与 CI 一致 |
| `selftest.py` | 独立的无界面链路自检（含 `validate_frame_meta` 校验） |
| `capture_check.py` | 离线校验 `.bin`/`.rad` 录制夹具（帧数/峰值/目标数/CRC 拒绝数），默认批量跑 `tests/fixtures/manifest.json` |
| `ws_bridge.py` | 零依赖 UDP→WebSocket 桥接与静态页服务器（配合 `web_viewer.html` 做 Web 订阅端） |
| `web_viewer.html` | 单页浏览器实时 PPI 订阅示例（ROADMAP D6） |
| `requirements-dev.txt` | 开发/文档工具依赖（Pillow、PyInstaller） |
| `bench.py` | 性能基准（覆盖 CA / GO 自适应 / OS CFAR、融合、PPI 与 SQLite） |
| `make_icon.py` | 离屏画 `assets/logo.png` + `assets/app.ico`（手写多尺寸 ICO 头 + PNG 负载） |
| `make_shortcut.py` | 生成 `桌面\瞭望 Lookout.lnk`（优先 WScript.Shell，COM 被拦时回退 MS-SHLLINK） |
| `make_demo.py` | 离屏渲染 20 帧 PPI → `assets/demo.gif`（需要 Pillow，见 `tools/requirements-dev.txt`） |

> 三个 `make_*.py` 都用 `QT_QPA_PLATFORM=offscreen`，**不弹窗口**；
> 截图/动图必须设 `QT_QPA_FONTDIR=C:\Windows\Fonts`，否则渲染出来完全没有文字。

---

## `tests/` —— 测试即规格

`tests/test_core.py` 共 **45 组**，不依赖 pytest，直接 `python tests/test_core.py`。
**不打桩、不 mock**：直接跑真实算法与真实 SQLite。

| 组 | 盯的是什么 |
|---|---|
| 帧解码 | 碎片、粘包、数据区里的假帧头 |
| LD2450 符号位 | bit15 独立符号位，不是补码 |
| 流水线闭环 | 目标 → 矩阵 → CFAR → 目标，位置能还原 |
| 告警 | 去抖、不刷屏、恢复、滞回 |
| 数据库 | 会话/航迹/事件往返，威胁与分类不丢 |
| 帧归档 | 写入/读回/轴一致性 |
| 端点解析 | 串口/TCP/UDP/BLE/文件 |
| 文件传输 | 按块回放且不丢尾巴 |
| 多站融合 | 坐标变换、目标上抬、盲区判定 |
| 融合交会 | 两站共视更准、共址时拒绝交会 |
| 标定 | 装偏反推、交会、病态判定、误差换算 |
| 激光雷达协议 | 角步长、跨 0°、CRC 拒收、碎片重同步 |
| 视图换算 | 北向/艏向、极坐标往返、文本格式 |
| 统计检索 | 分桶计数、告警汇总、级别与规则过滤 |
| CPA/TCPA | 接近 / 远离 / 本舰运动 / 侧向掠过 |
| 数据转发 | 载荷 / 速率限制 / 开关 |
| AIS | 位置报告 / 校验拒收 / 非位置报文 / 经纬度换算 |
| 下行控制 | 模拟器生效 / 基类明确返回不支持 |
| 地图瓦片 | 分辨率 / 级别 / 坐标 / 覆盖范围 |
| 底噪稳定性 | 多帧后不抬升（防 `scan += noise` 回归） |

---

## `docs/` —— 公开文档

| 文件 | 内容 | 说明 |
|---|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 系统分层、数据契约、扩展点与线程模型 | 扩展核心数据/架构前必读 |
| [STRUCTURE.md](STRUCTURE.md) | 逐文件职责解释与目录导航 | 定位源码入口 |
| [ROADMAP.md](ROADMAP.md) | 开发计划与未来路线图（阶段 A~F） | 查看已落地能力与后续排期 |
| [ADR/0001-language-choice.md](ADR/0001-language-choice.md) | 技术栈选型依据（Python + PyQt5 + NumPy 性能剖析） | — |
| [ADR/0002-product-positioning.md](ADR/0002-product-positioning.md) | 产品定位与取舍原则 | — |
| `screenshot-v1.1.png` | 最新主界面展示截图 | 用于 README 展示 |

---

## 已知边界与待验证项

| 项 | 状态 | 详见 |
|---|---|---|
| 实体外设联调（LD19 / LD2450 / LD2410 / BLE / 多站组网） | 有录制样本回归，待真实硬件实地联调 | [ROADMAP.md](ROADMAP.md) 阶段 A |
| 原始 ADC → FFT 距离-多普勒信号处理链 | 需配合相参硬件（如 IWR6843 + DCA1000）启动 | [ROADMAP.md](ROADMAP.md) B4 |
| 离线 MBTiles 海图、3D RHI 俯仰视图与电子战 F1~F3 进阶 | 已纳入软件主线排期 | [ROADMAP.md](ROADMAP.md) 阶段 E / F |

---

## 本地构建与运行时目录（不进 Git）

| 路径 | 说明 |
|---|---|
| `build/` | PyInstaller 编译临时缓存，可随时删除 |
| `dist/Lookout/` | Windows 便携可执行版（`Lookout.exe` + `_internal/` + `plugins/`），已随仓库提供以便克隆/下载即用，也可用 `installer/Lookout.spec` 重建 |
| `data/` | 开发期运行时数据（SQLite、帧归档、`rules.json`、`custom_drivers.json`）；打包版位于 `%LOCALAPPDATA%\Lookout\data` |
| `data/tiles/` | 地图瓦片磁盘缓存（上限 200 MB 自动清理） |
| `__pycache__/` | Python 字节码缓存，可随时删除 |
| `.venv/` | 虚拟环境，`1-安装依赖.bat` 可重建 |
