# OAT2new

<div align="center">
    <img src="https://img.shields.io/badge/version-v234.1-blue.svg" alt="Version">
    <img src="https://img.shields.io/badge/system-Windows-blue.svg" alt="System">
    <img src="https://img.shields.io/badge/python-v3.12-blue.svg" alt="Python">
    <img src="https://img.shields.io/badge/license-GPL--3.0-green.svg" alt="License">
</div>

<div align="center">
    <b>阴阳师自动化工具 · 基于上游 <a href="https://github.com/RMA-MUN/OnmyojiAuto">RMA-MUN/OnmyojiAuto</a> 的修改版</b>
</div>

---

## 📍 上游地址

| 项目 | 地址 / 说明 |
| --- | --- |
| **上游仓库** | <https://github.com/RMA-MUN/OnmyojiAuto> |
| **上游作者** | RMA-MUN（邮箱 n3032747608@163.com） |
| **本项目基线** | 上游 `main` 分支（代码版本 **v2.3.4**） |
| **上游最新 release** | `OAT-v2.3.3`（2026-09-25） |
| **许可证** | [GNU GPL v3.0](./LICENSE) |

> **说明**：上游 v2.3.3 的 release 包与 `main` 分支之间还有若干提交（版本号、多开客户端标签修复等）。
> 本项目直接以 `main` 为基线，因此**已经包含 v2.3.4 的全部上游改动**，再叠加下述修复与新增功能。

本仓库是上游的衍生作品（derivative work），按 GPL-3.0 要求：
**保留原许可证、注明来源、并以相同许可证开源**。上游原始 README 保留在
[README-upstream.md](./README-upstream.md)。

---

## 📥 程序下载 & 更新日志

| 功能 | 链接 |
| --- | --- |
| **下载最新打包版（zip）** | [**OTA2new-v234.1.zip**](https://github.com/1939872325as-dotcom/OAT2new/releases/download/OTA2new-v234.1/OTA2new-v234.1.zip) |
| 查看版本更新内容 | [前往 Releases](https://github.com/1939872325as-dotcom/OAT2new/releases) |
| 下载源码 | [Download ZIP](https://github.com/1939872325as-dotcom/OAT2new/archive/refs/heads/main.zip) |

> 与上游一样，**exe 不进仓库**（仓库只放源码），打包版作为 **Release 资源**提供。
> 下载 zip → 解压 → 运行里面的 `OAT.exe`（建议以**管理员身份**运行）。
> 包内 `OAT/` 目录是业务源码，**不要删除** —— 本版本采用免编译热加载，改磁盘源码即可生效。

---

## 🎯 本仓库相对上游做了什么

上游工具本身可用，但在「后台点击 + 多模式 + 长时间挂机」这条路上有一批**静默失败**型缺陷：
日志显示「挑战完成」，游戏里却没点；或计数被误判成卡死而提前中止。
本项目把这些缺陷逐条定位修掉，并新增了几个实用功能。

**改动规模：18 个文件改写、3 个文件新增，约 +1300 / −300 行。**

完整 unified diff 见 👉 [**CHANGES-vs-upstream.patch**](./CHANGES-vs-upstream.patch)
（以「上游 main」为 `a/`、本仓库为 `b/`）。逐条说明见 [docs/变更明细.md](./docs/变更明细.md)。

---

## 🎲 核心改动：随机点击 / 随机等待

固定坐标 + 固定间隔是脚本最容易被识别的特征：每次都点在**同一个像素**、每次都等**同样的秒数**。
这两项功能让操作带上人类的不确定性。**详细说明（含计算过程、日志示例、调参建议、常见问题）见
👉 [操作指引 · 随机点击与随机等待](./docs/操作指引-随机点击与随机等待.md)。**

### 随机点击

点击方式两选一（**编辑图像配置 → 点击区域设置 → 点击方式**）：

| 方式 | 随机范围 | 适用 |
| --- | --- | --- |
| **点击图片区域**（默认） | 识别到的区域**中间约 2/3** 内均匀随机（`中心 ± 宽/3`，并夹回区域内） | 有稳定图案的按钮/开关，推荐 |
| **点击自定义区域** | 你填的 `[x1, x2, y1, y2]` **整块范围**内随机 | 目标无稳定图案、或位置基本固定 |

举例：识别区域 `x=600, y=400, w=138, h=61` → 点击点落在 **X∈[623,715]、Y∈[410,450]**（实测采样 400 次）。
> 不是整个区域都随机，是**故意收窄到中间 2/3**：识别区域往往比目标按钮大一圈，
> 放宽到边缘就可能点到相邻按钮。这样「既随机、又点得准」。
>
> 若 `x1 == x2` 且 `y1 == y2`，则等价于**固定点点击**（无随机）。

### 随机等待

**按图配置**（**编辑图像配置 → 点击区域设置 → 点击后随机等待(秒)**），默认 `0.8~5` 秒：

| 填写情况 | 行为 |
| --- | --- |
| 两个框都留空 | 用默认 `0.8~5`，且**不写进配置** |
| 只填一个 | 另一个按默认补（只填 `2` → `2~5`） |
| 写反了（`8` 与 `1`） | **自动交换**为 `1~8` |

执行细节：① 等待在**点击之后**；② **暂停时长不计入**等待，停止则立即中断；
③ **每次实际秒数都写进日志**，便于核对：

```
点击后随机等待：默认 0.8~5 秒；已单独配置的图：tanshi, jieshu   ← 开局列出哪些图单独配了
后台点击 (655, 420)
点击后随机等待 2.64 秒（任务 tanshi 区间 1~3）                  ← 带任务名，可确认按图配置生效
```

> ⚠️ 这是**降低规律性**的手段，不是加速手段 —— 它**主动多花时间**，追求稳定挂机的必要成本。
> 但**别把等待调到 0**：界面还没刷新就执行下一步，反而更容易出错、重试更多，总耗时更久。

---

## ✨ 相对上游的变更清单

### 一、新增文件（3）

| 文件 | 作用 |
| --- | --- |
| `OAT/tools/region_capture.py` | **飞书式框选截图**：贴住游戏窗口、1:1 原始像素、后台抓图（PrintWindow），框选即得模板。截出来的尺寸**就是识别时用的客户区尺寸**，从根上避免「模板在 A 尺寸截、运行时 B 尺寸」导致的匹配失败 |
| `OAT/utils/cache_utils.py` | **缓存占用统计**：算出「清理缓存」实际会清掉多少体积，与清理逻辑共用同一批路径 |
| `_nuitka_deps.py` | Nuitka 打包用的依赖收集（导入语句集中在一个不会被调用的函数里，规避 Nuitka 顶层导入崩溃） |

### 二、修改文件（18）

#### 1. 崩溃 / 句柄失效类

| 文件 | 改动 |
| --- | --- |
| `OAT/tools/WindowChecker.py` | 新增 `_resolve_hwnd()`：缓存句柄先用 `IsWindow()` 校验，失效则按标题重找。`GetWindowRect`/`GetClientRect` 包异常，窗口消失时返回 `None` 而不再抛 `(1400, '无效的窗口句柄')`；**区分外框与客户区尺寸**，`resize_window` 改按客户区语义（`AdjustWindowRectEx` 换算）、调整后轮询等待生效再复测 |
| `OAT/app/main_window.py` | 窗口检测定时器回调整体兜底，异常不再逃逸；清理缓存后刷新占用显示；`logger.warn` → `logger.warning` |
| `OAT/source/huijuanshuafen/{base,explore,huijuan}.py` | 截图失败时空图保护（不再 `shot.shape` 报 AttributeError）；k28 用显式阈值；突破阶段被停止/失败时不再白等 5 分钟收尾；`logger.warn` → `logger.warning` |
| `OAT/source/__init__.py` | 模式无法启动时**弹窗提示并列出可用模式**（原来只写一条 ERROR 日志，界面表现是「点了开始没反应」）；字典模式缺 `default` 键时不再 `KeyError` |

#### 2. OCR 点错位置（一个页面有两处相同文字）

| 文件 | 改动 |
| --- | --- |
| `OAT/tools/OCRManager.py` | `find_text_offline()` 改为**收集全部候选 → 完全相等优先 → 达阈值者优先 → 置信度最高者**（原来命中第一个就 `break`）；新增 `region` 参数；命中多处时把每一处的文字/置信度/坐标写进日志 |
| `OAT/utils/OCRService.py` | `confidence_threshold` / `region` 真正透传（原来阈值从未传下去，配置里那个 0.8 是摆设）；**修掉缓存键缺陷**——原来对 numpy 图固定用 `image:{目标文字}`、不含图像内容，换帧后仍返回上一帧坐标；改为 64×64 降采样指纹做键 |
| `OAT/tools/OnmyojiAuto.py` | 新增 `ocr_region` / `ocr_confidence_threshold`；先算「截图坐标→客户区坐标」偏移 `dy`，再把客户区描述的 `ocr_region` 换算成截图坐标后裁剪 |

> **`click_area` / `ocr_region` 的真实语义是 `[x1, x2, y1, y2]`（两个区间），不是官方说明里的 `[左, 上, 宽, 高]`。**
> 依据是 `OnmyojiAuto.py` 里的 `x1, x2, y1, y2 = click_area`。

#### 3. MAA 管道模式（「显示挑战完成但根本没点」）

| 文件 | 改动 |
| --- | --- |
| `OAT/pipeline/runner.py` | ① `_resolve_click_coords` 无法确定坐标时返回 `None` 并跳过点击（原来静默返回 `(0,0)`，点到窗口左上角）；② 主循环取错任务——`current_next` 已被改成「它的 next」，导致 `is_start` 判错、且 `next` 为空时 `_task_map[None]` **KeyError 直接把挑战打成致命错误**；③ 新增「某任务的 next 曾超时、之后它自己又匹配到」的告警；④ 详细日志；⑤ **每张图独立的点击后随机等待** |
| `OAT/pipeline/recognition_opencv.py` | `find_text` **真正使用 `confidence`** 并回填真实置信度（原来不传阈值、不填置信度，日志必然显示 `0.00`）；新增 `region` 支持；模板未达阈值时打印「最高分 / 阈值 / 最佳位置 / 模板与截图尺寸」 |
| `OAT/pipeline/task_definition.py` | 新增 `ocr_region`、`click_wait_min`、`click_wait_max` 字段与解析 |

#### 4. 更新检查

| 文件 | 改动 |
| --- | --- |
| `OAT/config/check_update.py` | **更新检查改指本仓库**（对 fork 是必须的：继续指上游会把本地修复覆盖掉）；缓存带上 `owner`/`repo`，换仓库后旧缓存自动失效；加 **1 小时本地缓存**（未认证访问 `api.github.com` 仅 60 次/小时，原来每次启动都请求，开关几次就 403）；403/解析失败降级为 warning 并回退缓存；新增 `last_error` 让「检查失败」与「已是最新」可区分；`except KeyError`（永远进不来）改为显式 `json.JSONDecodeError` 并**放在 `RequestException` 之前**（`requests` 的 JSONDecodeError 同时继承两个基类，顺序放错会被吃掉）；版本号解析健壮化；**去掉 `verify=False`**（自更新会下载并执行 zip，走未校验 TLS 是供应链风险） |
| `OAT/config/update_manager.py` | 去掉 `verify=False`（×3）与告警抑制；`logger.warn` → `logger.warning` |

#### 5. 模式 / 图像编辑器

| 文件 | 改动 |
| --- | --- |
| `OAT/tools/edit_mode_and_img.py` | 新增 **「截图添加」**、**「截图替换」** 两个按钮（截图替换=选中一张图直接框选覆盖，文件名与配置不变）；「OCR配置」新增 **「OCR搜索区域」** 输入框；「点击区域设置」新增 **「点击后随机等待(秒)」**；配置正/反向转换保留 `ocr_region` 与等待字段；新增 `_normalize_area` / `_normalize_wait`（同时兼容 list 与 str，否则从 config.json 读出的 list 会被当成非法而静默丢弃） |
| `OAT/app/settings_page.py` | 「GitHub 仓库」超链接**指向本仓库**（顺带修掉上游笔误 —— 原链接 `RMA-MUN/Onmyou**u**jiAuto` 多一个 u，是坏链接）；「清理缓存」卡片**显示当前占用体积**（分项展示），切到设置页与清理完成后自动刷新 |

#### 6. 打包 / 运行

| 文件 | 改动 |
| --- | --- |
| `main.py` | **热更新启动器**：冻结运行时优先把「exe 同目录的磁盘源码」放进 `sys.path` 最前，使业务代码（`OAT/`）改动后**无需重新编译**即可生效 |
| `OAT/tools/settings.py` | 版本号 `234.1` |
| `OAT/source/common_challenge.py` | 删掉**跨图共享**的 `retry_count`（原来任何图片命中都自增，某张图触发过一次「连续 2 次」后，之后任意 5 次匹配就把整个挑战判为卡死并中止）；修掉 `NameError` 被裸 `except` 静默吞掉导致的「切图重置计数」失效；新增 `_build_recognition_order()` **真正读取 `recognition_order`**（此前该字段无任何代码读取，识别顺序退化成 JSON 键序）；两处裸 `except` 改为记录 warning |

### 三、差异汇总（+行 / −行）

| 文件 | + | − |
| --- | ---: | ---: |
| `OAT/tools/edit_mode_and_img.py` | 345 | 3 |
| `OAT/tools/WindowChecker.py` | 219 | 40 |
| `OAT/pipeline/runner.py` | 141 | 22 |
| `OAT/config/check_update.py` | 101 | 42 |
| `OAT/tools/OCRManager.py` | 66 | 40 |
| `OAT/source/common_challenge.py` | 62 | 52 |
| `main.py` | 58 | 0 |
| `OAT/pipeline/recognition_opencv.py` | 52 | 8 |
| `OAT/tools/OnmyojiAuto.py` | 43 | 22 |
| `OAT/app/main_window.py` | 35 | 17 |
| `OAT/pipeline/task_definition.py` | 32 | 0 |
| `OAT/source/huijuanshuafen/huijuan.py` | 31 | 24 |
| `OAT/utils/OCRService.py` | 31 | 8 |
| `OAT/source/__init__.py` | 21 | 1 |
| `OAT/app/settings_page.py` | 20 | 0 |
| `OAT/source/huijuanshuafen/explore.py` | 13 | 13 |
| `OAT/source/huijuanshuafen/base.py` | 9 | 5 |
| `OAT/config/update_manager.py` | 7 | 10 |

---

## ⚠️ 重要：一个行为变更

上一节 `WindowChecker` 的修复**改变了窗口尺寸的语义**：

- **修复后**：「客户区」（真正的游戏画面区域）才等于配置里的 `custom_res_width/height`（**默认 1441×812**）。
- **上游原版**：`SetWindowPos` 收到的是客户区目标却被当作**外框**尺寸使用，且用外框和客户区目标比较，
  于是永远不相等、每次点「窗口检测」尺寸都漂移。

**因此：升级到本版本后，原先按旧尺寸截的模板图需要按新的客户区尺寸重新截取。**
用新增的「截图添加 / 截图替换」按钮重截最方便，它抓的就是客户区、与识别坐标系一致。

---

## 🛠️ 构建 / 运行

### 源码运行

```bash
git clone <本仓库地址>
cd OAT2new

# 方式一：uv（与上游一致）
uv venv && uv sync
python main.py

# 方式二：pip（注意依赖版本坑，见下）
pip install -r requirements-fixed.txt
python main.py
```

### 依赖说明（重要）

上游 `requirements.txt` 存在**版本冲突与缺失**，直接用会装不上或运行报错：

- `numpy == 2.2.6` 与 `opencv-python == 4.8.0.74` 不兼容（后者要求 numpy < 2）
- 缺少 `loguru`、`qfluentwidgets`、`rapidocr`、`onnxruntime`
- `pyyaml` 重复声明两次；`pillow == 9.5.0` 与 numpy 2.x 冲突

本仓库**保留了上游的 `requirements.txt` 原样**，另附一份实测可用的
[`requirements-fixed.txt`](./requirements-fixed.txt)（Python 3.12）：

```bash
pip install -r requirements-fixed.txt
```

### 打包

上游用 **Nuitka**（不是 PyInstaller）：

```bash
python build_release.py
```

本仓库的 `main.py` 支持**热更新**打包方式：用
`--nofollow-import-to=OAT` 编译 exe，业务代码留在磁盘上，改动后不必重新编译。

---

## 📄 文档

- [**项目特点**](./docs/项目特点.md) ← 先看这个：整体特点 + 三个重点功能详解
- [**操作指引 · 随机点击与随机等待**](./docs/操作指引-随机点击与随机等待.md) ← 用户最关心的两项
- [变更明细（逐条修复说明）](./docs/变更明细.md)
- [操作指引 · 配置与截图逻辑](./docs/操作指引-配置与截图逻辑.md)
- [操作指引 · 模式与图片编辑（新手）](./docs/操作指引-模式与图片编辑(新手).md)
- [上游原始 README](./README-upstream.md)

---

## 📜 许可证

本项目与上游一致，采用 [GNU General Public License v3.0](./LICENSE) 开源。
允许自由使用、修改和分发，但**衍生作品必须以相同许可证开源**，并保留原始版权声明。

原始项目版权归 **RMA-MUN** 所有；本仓库仅包含在此基础上进行的修改。

---

## ⚠️ 免责声明

本工具仅供学习交流，禁止商用及违规使用，使用风险自负。
