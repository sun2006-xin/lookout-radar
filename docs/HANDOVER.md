# 交接与协作规范

> 给下一个接手的人（或 AI）。目标：**读完这一页就能安全改动，不会把别人踩过的坑再踩一遍。**
> 如果你只有五分钟：读第 0 节（路径与状态）、第 4/6 节（禁忌与坑），
> 然后打开 `docs/ROADMAP.md` 挑任务。

---

## 0. 项目在哪 / 现在是什么状态

> **这一节是给刚接手的人（或 AI）看的。先确认路径，再动手。**

| 项 | 值 |
|---|---|
| **项目根目录** | `D:\workbudy\2026-11-25-19-11-27\radarscope` |
| 源码包 | `<根>\radarscope\`（包名 `radarscope`，**不要改成产品名**） |
| 文档 | `<根>\docs\`，接手先读 `ROADMAP.md` → 本文第 4/6 节 → `STRUCTURE.md` |
| 虚拟环境 | `<根>\.venv\`，Windows 下解释器是 `.venv\Scripts\python.exe` |
| 打包产物 | `<根>\dist\Lookout\Lookout.exe`（129 MB，含 `plugins/`） |
| 桌面入口 | `C:\Users\L\Desktop\瞭望 Lookout.lnk`（指向 `dist\Lookout\Lookout.exe`） |
| 运行数据 | 开发时 `<根>\data\`；打包版 `%LOCALAPPDATA%\Lookout\data` |
| Git 仓库 | https://github.com/sun2006-xin/lookout-radar （public / main / MIT） |
| 基线提交 | `558bd33`（2026-11-26，REVIEW 全部闭环）；之后以 `git log -1` 为准 |

**当前状态**：61 个 Python 文件 / 约 11 400 行 / **20 组测试全过** /
`tools/check.py` 全绿 / `Lookout.exe --selftest` 退出码 0。
产品名「瞭望 Lookout」，默认主题现代白（色板 daylight），可切深色（ice）。

**接下来做什么**：[`docs/ROADMAP.md`](ROADMAP.md) 是唯一的任务清单，
阶段 A（真机联调）优先级最高，阶段 B/C 是算法与架构深水区。
**不要在没读 ROADMAP 的情况下自己挑功能加。**

---

## 1. 三十秒上手

```bat
cd /d D:\workbudy\2026-11-25-19-11-27\radarscope
.venv\Scripts\python.exe -m pip install -r requirements.txt
rem 20 组测试，必须全绿
.venv\Scripts\python.exe tests\test_core.py
rem 编译 + 测试 + 链路自检，提交前必跑
.venv\Scripts\python.exe tools\check.py
rem 无界面自检，不弹窗，退出码 0 才算通过
.venv\Scripts\python.exe main.py --selftest
rem 启动图形界面（会弹窗口）
.venv\Scripts\python.exe main.py
```

启动后默认是虚拟雷达，点「连接」就有画面。
想接手继续开发，按顺序读：`docs/ROADMAP.md`（做什么）→ 本文第 4/6 节（别踩坑）→
`docs/STRUCTURE.md`（代码在哪）→ `docs/ARCHITECTURE.md`（为什么这么分层）→ `core/model.py`。

**改动前必须知道的三件事**：

1. 这是个**分层**项目：传输 / 协议 / 驱动 / 数据 / 处理 / 呈现，单向依赖。
2. 界面是**能力驱动**的：显示什么由 `Capabilities` 决定，不按型号写分支。
3. 有 **20 组测试**，改完必须全绿。测试就是规格文档，不打桩不 mock。

---

## 2. 你的第一次改动（推荐路径）

按风险从低到高：

1. **改配色**（`ui/theme.py` 的 `PALETTES`）——零风险，验证环境可用
2. **加一条告警规则**（界面「工具→告警规则」或 `plugins/parking_rules.py`）
3. **加一个驱动**（照下面第 3 节模板）
4. **改显示**（`ui/*.py`）——注意别把协议解析带进 UI
5. **改核心数据结构**（`core/model.py`）——先读完 `ARCHITECTURE.md` 第二节

---

## 3. 加一个驱动的完整模板

复制这段就能跑，改三处即可。

```python
"""新雷达驱动。说明它是什么形态、给什么数据。"""
from __future__ import annotations

import time

import numpy as np

from ..core.codec import FrameSpec, StreamDecoder
from ..core.driver import RadarDriver, register
from ..core.model import Capabilities, RadarFrame, Target
from ..core.transport import endpoint_of, open_transport


@register
class MyRadarDriver(RadarDriver):
    id = "myradar"                    # 全局唯一，配置文件用它
    label = "我的雷达 XX-100"          # 只用于显示
    simulated = False
    source = "device"                 # sim / device / fusion / replay
    form = "sector"                   # point / sector / omni / tracking / rd / fusion
    vendor = "某厂"                    # 仅展示

    def __init__(self) -> None:
        self._tr = None
        self._dec: StreamDecoder | None = None
        self._endpoint = ""

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="XX-100", form="sector", source="device",
            has_scan=True, has_azimuth=True, has_targets=False,
            has_velocity=False,
            az_fov=120.0, range_min=0.2, range_max=10.0,
            scan_mode="staring", scan_rate=10.0,
            transports=("serial", "tcp"),
            views=("ppi", "ascope", "waterfall", "tracks"),
            notes="一句话说明它的局限",
        )

    def open(self, **kwargs) -> None:
        self._endpoint = endpoint_of(kwargs, 115200)
        self._tr = open_transport(self._endpoint, default_baud=115200)
        self._dec = StreamDecoder(
            FrameSpec(header=b"\xaa\x55", tail=b"\x0d\x0a"))   # 长度字段默认在帧头之后

    def close(self) -> None:
        if self._tr is not None:
            self._tr.close()
        self._tr = None

    def read(self) -> RadarFrame | None:
        if self._tr is None or self._dec is None:
            return None
        raw = self._tr.read(256)
        if not raw:
            return None
        frame = None
        for fr in self._dec.feed(raw):
            frame = self._parse(fr) or frame
        return frame

    def _parse(self, fr: bytes) -> RadarFrame | None:
        if len(fr) < 8:
            return None
        # ↓↓↓ 只填你擅长的那一半，另一半框架会补
        return RadarFrame(
            timestamp=time.monotonic(),
            targets=[Target(x=..., y=..., snr=...)],
            source=self.label,
            meta={"scan_period": 0.1},
        )
```

然后在 `drivers/__init__.py` 加一行 `from .myradar import MyRadarDriver`。

`source` / `form` / `vendor` 是**类属性**，不用在 `capabilities()` 里重复声明 ——
基类 `resolve_capabilities()` 会统一合并（A2）。漏了这一步曾导致"共视列不显示"。

**不要做的**：
- 不要自己写 `while` 循环切帧 —— 用 `StreamDecoder`
- 不要 `import serial` —— 用 `open_transport()`
- 不要在驱动里判断界面状态

---

## 4. 十条硬性禁忌

违反任何一条都算 bug，评审时直接打回。

| # | 禁忌 | 为什么 |
|---|---|---|
| 1 | `core/` 里 `import PyQt5` | 核心层要能无界面测试 |
| 2 | `ui/` 里出现 `struct.unpack` / `serial.` | 分层塌了 |
| 3 | 代码里按型号写 `if did == "ld2450"` | 型号会过时，应该看 `Capabilities` |
| 4 | 把强度矩阵存进 SQLite | 一小时几十 GB BLOB，库会死 |
| 5 | 噪声写成 `scan += noise` | 稳态收敛到 `noise/(1-decay)`，底噪整体抬升 |
| 6 | 角度做算术平均 | `+179°` 与 `−179°` 平均出 0°，要用圆均值 |
| 7 | 插件钩子叫 `register` | 会和 `from core.driver import register` 撞名 |
| 8 | 改 Python 包名 `radarscope` | 会破坏所有 import、插件、PyInstaller 收集规则；产品名只影响 UI/exe/文档 |
| 9 | 把方法继续堆进 `mainwindow.py` | 已拆到 `ui/controllers.py` 三个控制器，新功能请放进对应的那个 |
| 10 | QSS 用 f-string 拼 | QSS 全是 `{}`，要写成 `{{}}` 极易漏；`theme.py` 用 `@bg@` 占位符替换 |

---

## 5. 代码风格

- **注释解释"为什么"，不解释"是什么"。**
  `# 这里减 2 是因为长度字段不含帧尾` ✅
  `# 循环 i 从 0 到 n` ❌
- 踩过坑的地方，把坑写进注释，否则下一个人会改回去。
- 公开函数写类型注解；`core/` 的类尽量用 `@dataclass(slots=True)`。
- 局部变量短、模块级常量全大写。
- 中文注释可以，但**字符串常量里的单位用 ASCII**（`m` / `m/s`），避免编码问题。
- 文件头写四行说明：这是什么、解决什么问题、关键取舍。

---

## 6. 已知坑清单（症状 → 原因 → 修法）

| 症状 | 原因 | 修法 |
|---|---|---|
| 界面在跑但数据全错 | 帧长度字段偏移算错 | 长度字段紧跟帧头，偏移 = `len(header)` |
| 逐字节喂数据永远拼不出帧 | 半包被当"非法长度"丢弃 | `_total_of()` 用 `0` 表示"等数据"，`-1` 才表示非法 |
| 回放丢最后一帧 | 到文件尾先抛异常后交付 | `FileTransport` 与 `ReplayDriver` 都用 `_finished` 标记，下一轮再抛 |
| 文本协议 `file://` 回放读不到行 | `FileTransport.read(n)` 忽略 `n`，`readline()` 调 `read(1)` 一次返回整块 | `step = min(max(1, n), self.chunk)`，严格遵守 `n` 上限 |
| LD2410 无法用 `file://` 离线回放 | `open()` 强行发配置命令调 `write()`，而文件传输只读 | 检测 `scheme == "file"` 时跳过握手，直接按工程模式解析 |
| 目标跑到屏幕外 | 量程上限写死 | 量程从 `Capabilities.range_max` 取 |
| 面板糊成一片绿 | 刻度步长写死 | 用 `theme.nice_step()` |
| RD 图上下反了 | 数组第 0 行是 `-V_MAX` | 显示前 `arr[::-1]` |
| 融合后一圈放射状条纹 | 本地距离格比全局粗，散射留洞 | 3×3 最大值闭运算（`_close_gaps`） |
| 底噪越来越高 | 噪声累加 | 改成 `np.maximum(scan, noise)` |
| 旋转雷达航迹老是断 | 丢失判据用了帧数 | 用秒（`missed` 是 float，秒） |
| 标签糊成一团 | 多目标位置接近 | `ppi._draw_targets` 里的避让逻辑 |
| 插件加载后把 API 当驱动注册 | 钩子名撞车 | 入口改叫 `setup(api)` |
| 北向朝上时有个跟着转的黑菱形 | 调色板 0 号色是纯黑，控件背景不是 | `PPIWidget._ctable_for()` 把低电平(<26)设成**全透明**（同时解决地图被盖住） |
| 界面明明按 form 判断却没生效 | 分类属性在驱动**类**上，`capabilities()` 里没写 | 调 `drv.resolve_capabilities()`（基类用 `dataclasses.replace` 一次合并），别在调用点手工补 |
| 统计图变成一个整块色块 | 只有 1 个时间桶，柱宽=全宽 | 柱宽设上限，少于 2 桶直接提示"数据太少" |
| 报警一响整个画面就抖一下 | 报警条用显隐切换，布局重排 | 改成常驻 + 固定高度，只换文案与配色 |
| 顶部出现白色条，和深色界面不搭 | 没给 QMenuBar/QMenu 写样式，走了系统主题 | `theme.py` 里补 QMenuBar/QMenu 样式 |
| 回波比最外圈距离环还大一圈 | 光栅铺满方框(2R/0.94)而不是 2R | 画进 (cx±R, cy±R) 并裁成圆形 |
| 上次配的串口不在了就没了 | 只按"已检测到的端口"回填 | 找不到就把原值加进列表并标注"本次未检测到" |
| 窗口标题栏还是浅色，和深色界面割裂 | 标题栏由 Windows 画，QSS 管不到 | `winstyle.enable_dark_titlebar()` 调 DWM，属性号 20/19 都试 |
| 深色标题栏偶尔失效 | Qt 重建窗口句柄会丢属性 | `showEvent` 里再设一次 |
| 真运动时圆内出现方形缺口 | 先把画面裁成圆再平移，圆外数据被抹掉 | 裁剪要放在平移**之后**（天线扫描圆跟着本舰走） |
| AIS 目标全堆在原点 | 没填本船经纬度，参考点是 (0,0) | 「工具→AIS 接入」里必须填实际经纬度 |
| 离屏截图完全没有文字 | offscreen 平台找不到字体库 | 设 `QT_QPA_FONTDIR=C:\Windows\Fonts` |
| QImage 存不到内存流 | `QImage.save(BytesIO)` 没有这个重载 | 用 `QBuffer` 或临时文件 |
| 拆 mixin 后 NameError | 方法搬走了，模块级常量/类没跟着搬 | 常量与 `ReaderThread` 一起放进 controllers.py 并显式 import |
| 手写 `.lnk` 快捷方式双击打不开 | `WScript.Shell` 读 `TargetPath` 为空（缺少完整 IDList） | `make_shortcut.py` 优先用 `WScript.Shell` 生成，COM 被拦时再回退二进制 |
| 地图整片被盖住看不见 | 光栅 0 号色不透明，把底图糊住了 | `_ctable_for()` 把低电平（<26）设成全透明 |
| 地图级别永远是最粗的 | 从低级往高级找，第一个满足条件的总是 z=3 | 从 z=18 往下找，取第一个能装下的 |
| 换浅色主题后雷达图一片灰白 | 深色色板挂在白底上 | `_apply_theme()` 里按主题自动配对色板（daylight/ice） |
| 工具栏留下没有控件的孤字 | 控件搬进面板后 QLabel 还在，hide 不拆 action | 干脆删掉那些 `tb.addWidget(QLabel(...))` |
| 点「录像」实际动了「录制帧」 | 两个按钮都叫 `btn_rec`，后者覆盖前者 | 录像改名 `btn_clip` |
| 工具栏中间出现空白条且「设置」弹窗右侧空白 | 先 `tb.addWidget(w)` 再 `form.addRow(..., w)`，Qt 把 `QWidgetAction` 留在了工具栏且把原控件设为隐藏 | 只往弹窗 `form` 里加控件，不要先 `tb.addWidget()` |

---

## 7. 测试要求

- 改 `core/` 的任何文件 → **必须**跑通 `tests/test_core.py`
- 改驱动 → 至少跑 `selftest.py` + 界面里连一次
- 修 bug → **先写一个会失败的断言**，再修（这是防止回归的唯一可靠办法）
- 打完包 → 必须跑 `Lookout.exe --selftest`（退出码 0 才算成，不弹窗，脚本可判）
- 新增功能 → 加一组测试，命名用中文短语描述行为

测试不依赖 pytest，`assert` + 打印即可，保持零额外依赖。

---

## 8. 文档更新义务

改以下内容时必须同步更新文档：

| 改了什么 | 更新哪里 |
|---|---|
| 数据契约（`Target`/`RadarFrame`/`Capabilities`） | `ARCHITECTURE.md` 第二节 |
| 新增/删除文件 | `STRUCTURE.md` 表格 |
| 里程碑完成或调整 | `PLAN.md` 第四节 |
| 新增已知限制 | `README.md` 限制表 + `PLAN.md` 风险表 |
| 新的踩坑 | 本文第 6 节 |
| 新增/删除文件、改职责 | `STRUCTURE.md` 对应表格 |
| 完成或调整路线图任务 | `ROADMAP.md`（勾选 + 写实测结论），别只改代码 |
| 新增架构决策 / 改数据契约 | 新增一条 `ADR/000x-*.md` |

---

## 9. 交接上下文清单

接手时按需读，不必全读：

| 目的 | 读这些 |
|---|---|
| **接下来该做什么** | **`ROADMAP.md`**（阶段 A 优先） |
| 只想跑起来 | 第 0 / 1 节 |
| 加一块新雷达 | 第 3 节 + `drivers/ld2450.py`（约 140 行，最标准的例子） |
| 改显示 | `ui/ppi.py` 文件头注释 + `ARCHITECTURE.md` 第八节性能预算 |
| 动融合 | `core/fusion.py` + `ARCHITECTURE.md` 第四节 + `tests` 里 `test_fusion` |
| 动标定 | `core/calibration.py` 模块文档 + `ARCHITECTURE.md` 第五节 |
| 排查线上问题 | `data/radarscope.log`（打包版在 `%LOCALAPPDATA%\Lookout\data`）+ 本文第 6 节 |

---

## 10. 提交规范

```
<类型>: <一句话说明>

类型：feat / fix / refactor / docs / test / perf / chore
```

例：

```
fix: 帧长度字段偏移改为由帧头长度推导

原来手写 length_prefix=6，同时被当成偏移和总长两种语义，
导致从数据区读长度，整条链路静默出错。加了逐字节喂数据的测试。
```

改核心逻辑的提交，正文必须写清"为什么"，不是"改了什么"。


---

## 11. 给下一位 AI 的工作方式（重要）

这一节是踩过几次坑之后总结的，照做能少走弯路。

### 验证手段：不要动不动就启动 GUI

| 想验证什么 | 用什么 |
|---|---|
| 算法、协议、存储 | `tests/test_core.py` + `tools/check.py`（纯命令行） |
| 打包产物能跑 | `Lookout.exe --selftest`（**不创建窗口**，看退出码与 `data/radarscope.log`） |
| 界面构造、方法是否齐备、帧循环 | `QT_QPA_PLATFORM=offscreen` 起 QApplication，构造 MainWindow 但**不 `show()`** |
| 需要出图（README 截图 / 动图） | 同样 offscreen，但**必须设** `QT_QPA_FONTDIR=C:\Windows\Fonts`，否则渲染出来一个字都没有 |

离屏渲染时用 `QImage` + `QPainter` + `w.render(p)`；`QImage.save()` 不接受 `BytesIO`，
要用 `QBuffer` 或临时文件。

### 改动顺序

1. **先写一个会失败的断言**，再改代码（HANDOVER 第 7 节）。
2. 改完跑三条命令（第 1 节），全绿才继续。
3. 改了 `core/model.py` → 读 ARCHITECTURE 第二节 + 写一条 ADR。
4. 收尾同步四份文档：`STRUCTURE.md`（文件/职责）、本文第 6 节（新坑）、
   `ROADMAP.md`（勾选 + 实测结论）、`README.md`（已知限制）。
5. 打包 → `--selftest` → commit（正文写**为什么**，不是改了什么）。

### 别替用户下结论

- 沙箱里连不上的东西（地图瓦片、公网、COM 蓝牙），
  明确写"未验证网络链路"，**不要写"已验证"**。
- 需要实机点一下才确认的观感问题（真运动边缘、`.lnk` 双击），
  写"待用户实机确认"。
- 用户在打游戏/不方便时，全程用 offscreen + `--selftest`，不弹窗口。

### 深水区在哪

`ROADMAP.md` 阶段 B/C 是真正难的部分：
瀑布图数据形态重设计（B1）、CFAR 自适应（B2）、航迹级加权融合（B3）、
原始 ADC→FFT（B4，需硬件）、渲染与处理分进程（C1）。
**每一项都写了涉及文件、实现路径、验收标准、风险**，可以直接开工。

### 最容易出错的三处

1. `AlarmRule` 的字段叫 **`id`** 不是 `rule_id`（`rule_id` 是 `AlarmEvent` 的字段）。
2. `PALETTES` 的 `daylight` 是**白底深回波**，深浅逻辑和其他五套相反。
3. `ui/controllers.py` 里的方法引用了模块级常量 `PRESET_ENDPOINTS` /
   `ALARM_STYLE*`，搬方法时这些常量必须跟着搬，否则运行时 `NameError`
   （编译期发现不了）。
