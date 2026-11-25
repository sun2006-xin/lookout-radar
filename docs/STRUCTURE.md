# 目录结构与文件职责

> 一个文件只干一件事。想找功能先看这张表，不要靠猜。

源码总量约 7700 行（不含依赖与打包产物）。

---

## 顶层

```
radarscope/
├── main.py                 程序入口：日志、异常兜底、启动窗口
├── selftest.py             无界面自检：矩阵→检测→航迹 整条链路
├── 1-安装依赖.bat          首次使用：建 venv + 装依赖
├── 启动.bat                日常启动（控制台可见，报错会停）
├── 2-打包EXE.bat           重新打包（产物在 dist/）
├── requirements.txt        依赖（bleak 为可选）
├── README.md               项目简介 + 快速上手（本文件的入口）
├── docs/                   所有文档与截图 ← 先读这里
├── plugins/                插件目录：丢 .py 即生效
├── tools/                  开发工具（bench 性能基准 / check 一键自检）
├── .github/workflows/      CI（跑 tools/check.py）
├── installer/              Inno Setup 安装包脚本
├── tests/                  单元测试
├── data/                   运行时生成：数据库、录制、配置、日志
├── dist/                   打包产物（可删，重新打包会生成）
└── build/                  PyInstaller 中间产物（可删）
```

**data/ 目录内容**（运行时生成，不要提交到版本库）：

| 文件 | 内容 |
|---|---|
| `radarscope.db` | SQLite：会话 / 航迹点 / 告警事件 |
| `*.rad` | 帧归档（强度矩阵，float16） |
| `sensors.json` | 多站布站配置（位置、朝向、视场） |
| `rules.json` | 用户编辑过的告警规则 |
| `radarscope.log` | 运行日志（崩溃后排查用） |

---

## core/ —— 与界面无关的心脏

| 文件 | 职责 | 稳定度 |
|---|---|---|
| `model.py` | `Target` / `RadarFrame` / `Capabilities` 三个契约 | **极稳，改要慎重** |
| `driver.py` | 驱动基类 + 注册表（`@register`） | 稳 |
| `transport.py` | 串口/TCP/UDP/BLE/文件 + 端点 URL 解析 | 稳 |
| `codec.py` | 声明式切帧：帧头/长度/帧尾、粘包半包假同步 | 稳 |
| `pipeline.py` | CA-CFAR 检测、目标反投影、缺失补全 | 较稳 |
| `tracker.py` | 最近邻关联 + alpha-beta 滤波 + 航迹合并 | 较稳 |
| `fusion.py` | 多站空间对齐、扇区拼接、盲区计算 | 新，可能还要调 |
| `calibration.py` | 朝向标定、残差报告 | 新 |
| `geometry.py` | 方位线、最小二乘交会（融合与标定共用） | 稳 |
| `cpa.py` | CPA/TCPA 最近会遇距离与时间 | 稳 |
| `forwarder.py` | UDP 数据转发（每帧 JSON）+ 接收端示例 | 稳 |
| `ais.py` | AIS NMEA 报文解析（AIVDM 6bit 装甲 / 位置报告） | 稳 |
| `aisfeed.py` | AIS UDP 监听线程 + 目标表 | 稳 |
| `maptiles.py` | 地图瓦片：Web Mercator 换算 / 下载 / 磁盘缓存 | 稳 |
| `viewmath.py` | 视图换算：北向/艏向、屏幕↔极坐标、文本格式 | 稳 |

> 提交前跑 `tools/check.py`，判据与 CI 完全一致。
| `alarm.py` | 告警引擎（去抖/滞回/冷却）、规则序列化 | 稳 |
| `storage.py` | SQLite 记录 + `.rad` 帧归档 | 稳 |
| `plugins.py` | 运行时插件加载与隔离 | 新 |
| `paths.py` | 开发/打包两套路径解析 | 稳 |

**core 里不许出现 `import PyQt5`。** 这条是硬约束，破坏了就没法无界面测试。

---

## drivers/ —— 每种雷达一个文件

| 文件 | source | form | 说明 |
|---|---|---|---|
| `simulator.py` | sim | sector | 合成雷达，旋转/凝视双模式，带底噪与静止杂波 |
| `sim_aesa.py` | sim | rd | 相控阵/脉冲多普勒，9 目标 + RD + IFF + 威胁 |
| `multi.py` | sim / fusion | fusion | 多站融合：6×60° 演示 + 真实组网 |
| `ld2410.py` | device | point | 海凌科存在感应，9 距离门能量 |
| `ld2450.py` | device | sector | 海凌科三目标跟踪，直接给 x/y |
| `ldlidar.py` | device | omni | LD19/LD06/D500 旋转激光，360° |
| `ultrasonic.py` | device | sector | HC-SR04 + 舵机，180° 机械扫描 |
| `replay.py` | replay | sector | 回放 `.rad` 录制文件 |
| `__init__.py` | — | — | 导入即注册，加驱动只需加一行 |

**驱动里不许操作界面，也不许出现型号判断逻辑。**
型号只出现在 `label` 和 `vendor` 字符串里。

---

## ui/ —— 只负责画和交互

| 文件 | 职责 |
|---|---|
| `mainwindow.py` | 编排：工具栏、菜单、页签、帧分发、连接生命周期 |
| `theme.py` | 三套配色、刻度步长算法、全局样式表 |
| `ppi.py` | 环视显示器：LUT 渲染、余晖、标签避让、点击锁定、扇区与盲区 |
| `ascope.py` | A 显（距离剖面） |
| `waterfall.py` | 距离-时间瀑布 |
| `rdscope.py` | 距离-多普勒热图 |
| `tracktable.py` | 航迹表（列随能力变化） |
| `sensorpanel.py` | 多站传感器状态 |
| `eventlog.py` | 事件日志（可导出） |
| `history.py` | 历史会话（查库 + 导出 CSV） |
| `sensorsdialog.py` | 传感器布站配置 |
| `statsdialog.py` | 统计分析：KPI / 活动量图 / 告警汇总 / 事件检索 |
| `linkdialog.py` | 连接设置：串口枚举(识别 CH340/CP210x) / BLE 扫描 / TCP / UDP / 文件 |
| `extra.py` | 设备控制（增益/STC/FTC）与 AIS 接入两个小对话框 |
| `maplayer.py` | 瓦片拼图 + 后台下载线程，供 PPI 当底图 |
| `winstyle.py` | 标题栏深浅色（DWM）+ 程序图标 + 对话框标题栏全局跟随 |
| `rulesdialog.py` | 告警规则编辑器 |
| `pluginsdialog.py` | 插件管理 |
| `calibrationwizard.py` | 标定向导（采集 → 计算 → 写入） |

**ui 里不许出现 `struct.unpack` 或 `serial`。** 协议解析漏到界面层就是分层破了。

---

## plugins/ —— 第三方扩展

| 文件 | 演示什么 |
|---|---|
| `example_sweep_ranger.py` | 用插件注册一台新雷达（单波束测距） |
| `parking_rules.py` | 用插件注册一组告警规则（泊车分区） |

插件入口统一是 `def setup(api)`。**不要叫 `register`**——插件通常会
`from radarscope.core.driver import register`，名字会撞车。

---

## tests/ —— 测试即规格

`tests/test_core.py` 共 20 组，不依赖 pytest，直接 `python tests/test_core.py`。

| 组 | 盯的是什么 |
|---|---|
| 帧解码 | 碎片、粘包、数据区里的假帧头 |
| LD2450 符号位 | bit15 独立符号位，不是补码 |
| 流水线闭环 | 目标→矩阵→CFAR→目标，位置能还原 |
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
| 底噪稳定性 | 多帧后底噪不抬升（防噪声累加回归） |

---

## docs/ —— 文档

| 文件 | 内容 |
|---|---|
| `PLAN.md` | 总体计划：里程碑、优先级、风险、参考项目 |
| `ARCHITECTURE.md` | 分层、数据契约、扩展点、性能预算、取舍 |
| `STRUCTURE.md` | 本文件：目录与文件职责 |
| `HANDOVER.md` | 交接：怎么改、禁忌、常见坑 |
| `ADR/` | 架构决策记录（为什么这样选，附实测数据） |
| `screenshot*.png` | 界面截图 |

---

## 还没做的（与 PLAN 对应）

| 项 | 原因 |
|---|---|
| GIS 底图 | 需要罗盘/IMU 给航向，否则世界坐标无意义 |
| 真信号处理（ADC→FFT） | 需要能吐原始数据的板子（IWR6843 + DCA1000） |
| 3D 点云 | 同上，且要先有 3D 数据 |
| 多站加权融合 | 2 站时交会结果与权重无关，需 3 站以上才有意义 |
| 覆盖率门槛 | 需要额外依赖；当前按关键路径写测试 |

---

## 可以删的东西

| 路径 | 说明 |
|---|---|
| `build/` | PyInstaller 中间产物 |
| `dist/` | 打包产物，`2-打包EXE.bat` 可重建 |
| `data/` | 运行时数据，删掉会丢历史记录与布站配置 |
| `__pycache__/` | 解释器缓存 |
