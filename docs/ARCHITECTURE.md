# TIS_Retext 架构说明

本文描述当前实现的稳定边界和依赖方向。目标是让 GUI、PAC 容器、文本编辑与
只读预览能够独立演进，而不是把格式算法堆积在界面回调中。

## 分层

### 入口与界面

- `tk_gui.py` 负责 Tk 控件、页面状态、用户事件和异步任务反馈。
- `scripts/launch_gui.py` 是源码与 PyInstaller 共用的启动入口，同时提供隐藏的
  `--package-probe` 原生依赖检查。
- GUI 只编排服务并渲染返回值，不应拥有 PAC、TBL/DAT 或 MDL 二进制算法。

### 文本领域与业务

- `retext/domain.py` 定义 `TextDocument`、`TextUnit`、`SavePlan` 和工作流枚举。
- `retext/core.py` 的 `RetextService` 是解析、保存和后端路由的统一入口。
- `retext/session.py` 管理单文件编辑状态、源文件指纹和保存会话。
- `retext/business.py` 的 `WorkspaceBusiness` 编排批量扫描、批量执行和版本差异。

调用方向应保持为：

```text
GUI -> Business / Session -> RetextService -> Engine
```

不得让 engine 反向依赖 GUI，也不要让 GUI 直接调用旧版批处理实现。

### 文本后端

- `retext/engines/legacy/` 保留快速兼容路径及当前 DAT 默认实现。
- `retext/engines/kuro/` 包含 KuroTools 结构化 TBL、实验 DAT 以及必要 schema。
- 后端实现统一满足服务层契约；上层不依赖其临时脚本或数字缓存文件名。
- KuroTools 已有且可验证的格式约定优先于推测性实现。

### PAC 容器

- `archive/fpac.py`：严格检查、流式读取/构建和输出验证；
- `archive/workspace.py`：懒物化、manifest、指纹、锁与安全清理；
- `archive/session.py`：把 PAC 条目接入 `DocumentSession`；
- `archive/collection.py`：多 PAC 工作台、节点展开和目标物化；
- `archive/compare.py`：独立 PAC/PAC 组差异索引；
- `archive/fallback.py`：仅用户显式选择时使用的隔离回退。

PAC 是容器层，不是文本后端。业务身份使用 PAC 内逻辑路径；工作区中的数字缓存
文件名不得泄漏到界面、比较结果或批量规则中。

### 只读预览

- `retext/preview.py` 负责格式识别、媒体元数据和结构预览；
- `retext/media_probe.py` 负责有界解析 WebM/EBML 容器头；
- `retext/playback.py` 封装 LibVLC 播放状态和窗口绑定；
- `retext/model3d/` 负责 MDL 几何、材质、名称追溯、动画采样及 CPU/GPU 渲染；
- `retext/model_preview.py` 仅保留旧导入路径兼容导出，不承载实现。

预览条目可以按需物化和提取，但不得标记为编辑结果或进入 PAC 重建修改集。
选择目录、根节点、空选或折叠树不会清空当前预览；只有新的可预览文件结果才会
覆盖预览页面。

## 主要数据流

### 单文件编辑

```text
文件 -> DocumentSession -> RetextService.load -> TextDocument
用户修改 -> SavePlan -> 暂存写入 -> 回读验证 -> 原子替换/另存为
```

会话保存前检查源文件是否发生外部变化。`SessionOptions` 分别保存 TBL 与 DAT 的
全局后端选择；单文件、批量和版本对比按逻辑路径使用同一选择。`#scp` DAT 的两种
后端都使用精确二进制引用图；只有无法建立布局时才读取
`allow_risky_repack` 进入启发式回退。
TBL 的游戏版本按每个文件的已证实记录布局决定；全局1st/2nd选项只为无法判定的
布局提供后备值，不能覆盖2nd PAC中内置的 FC/Sora1 表结构。

### PAC 编辑

```text
PAC 索引 -> PacWorkspace manifest
            -> 按需物化 TBL/DAT
            -> DocumentSession / RetextService
            -> 标记明确修改条目
            -> 流式重建 PAC
            -> 完整重读与逐条验证
```

无修改构建应保持字节级一致；非文本预览缓存永远不进入修改集合。

### 批量替换

批量范围可以来自目录、单文件、PAC 根或文件树子节点，但实际目标必须经过两层
约束：

1. `PacWorkbench` 或普通目录收集器展开为明确文件目标；
2. `WorkspaceBusiness` 再拒绝所有非 `.tbl/.dat` 目标，并通过
   `RetextService` 结构化加载。

扫描命中绑定到文件指纹、文本单元和单次出现区间。同一文本单元中的重复内容会
产生多个独立操作；执行时以原始区间同时合成结果，拒绝过期或互相重叠的操作，
不会级联替换。一个文件在一次批处理中只经过一条结构化保存路径，并在暂存、
回读成功后写回。PAC 目标随后登记为工作区修改，不再需要额外保存；回包仍由
显式的 PAC 构建操作完成。旧版任意 RAW 字节替换入口已禁用；结构化变长重定位
通过统一保存路径执行，不需要风险授权，启发式回退仍需显式允许。

每个 `BatchMapping` 自身携带 `full_match`。普通映射枚举文本单元中的全部非重叠
子串；完全匹配映射只在 `TextUnit.current_text == old` 时生成一个覆盖整个单元的
命中。匹配方式随映射 JSON 和逐次处理选择一起持久化，不能退化成 GUI 全局状态。
历史二元组 API 与 v1 JSON/TXT 会被规范化为 `full_match=False`。

界面输入的查找词和映射旧文本在进入服务前统一做 NFC 规范化，并移除首尾粘贴
噪声；单文件查找状态由“查询词、大小写选项、结果索引”共同标识，不能只比较
结果索引列表。底层匹配仍保持文字语义，不执行简繁转换或 NFKC 兼容折叠。

TBL 等编码字节长度时直接原位替换；变长时剪接外部字符串池。已知 Header 记录
Schema 的 `toffset`、数组与纯外部偏移字段，未知 Header 通过完整记录列发现引用。
多个字段指向同一 C 字符串后缀时合并为一个存储单元，并映射内部别名边界。不得
用“数值落在文件范围内”或“数值等于旧偏移”搜索替换字段。

TBL 文本覆盖先使用游戏标签和记录长度匹配的 Schema，再处理未知 Header 的引用
列。可编辑项必须由具体 64 位字段引用；纯外部偏移参与重定位但不展示为文本，
从而排除二进制块中偶然可打印的片段。Kuro 与 Legacy 消费同一清单，因此后端
切换不会改变可搜索文本集合。

### 版本对比

版本对比是顶层独立业务，不依赖已打开的左侧 PAC 树。PAC 组先按相对 PAC 路径
配对，文件再按 PAC 内逻辑路径配对；只有用户展开具体文本差异时才物化条目。

## 程序数据目录

`retext/paths.py` 是路径的唯一来源：

| 场景 | 默认数据根 |
|---|---|
| 源码运行 | `<repo>/.runtime/` |
| Windows 冻结版 | `%LOCALAPPDATA%/TIS_Retext/` |
| 显式覆盖 | `TIS_RETEXT_DATA_DIR` |

数据根下的稳定子目录：

- `workspaces/`：PAC manifest 和按需物化内容；
- `staging/`：比较和隔离操作的受管暂存；
- `trash/`：可恢复的清理结果；
- `transient/`：短期中间文件。

冻结版不再把运行数据写在 exe 旁，因此运行一次发行包不会污染待分发目录。
清理工作区前必须尊重 dirty 状态和跨进程锁。

## 模型预览边界

MDL 模块与 GUI 通过服务数据对象交互。解析、候选基础模型选择、纹理关联、动画
采样和渲染上下文均位于 `retext/model3d/` 或 CPU renderer 中。

- GPU 路径使用专用渲染线程和缓存；失败时回退 CPU；
- 相机变化复用已采样姿态，不重复计算整套蒙皮；
- 纯动画 MDL 只在基础模型候选明确且实际影响可渲染顶点时绑定；
- 多候选、低兼容 `_gs` 或仅材质控制轨道不会被强行套用；
- 外部 DDS 是增强依赖，缺失时返回清晰诊断而不是伪造贴图；
- 当前未还原游戏完整 shader、toon、法线、遮罩和全部材质动画。

## 发布构建边界

普通 `build_exe.ps1` 用于开发验证；`build_exe.ps1 -Release` 是严格门禁。严格
模式要求固定 CPython、固定 VLC manifest、严格测试、许可证/语料条件、干净输出
以及打包后原生依赖 probe。

冻结包的数据目录位于 `LOCALAPPDATA`，而 probe 通过临时
`TIS_RETEXT_DATA_DIR` 隔离。构建完成后会拒绝含 `TIS_Retext_Data`、PAC、MDL 或
DDS 的发行目录。

默认依赖和冻结包不包含独立 FFmpeg。WebM 元数据由 `retext/media_probe.py`
有界读取 EBML 的 Segment Info/Tracks；视频首帧和播放前拖动定位复用 LibVLC
原生画布，通过“静音启动、等待可跳转、seek、确认目标时间、请求暂停、确认已暂停”
状态机完成。只有确认暂停后才恢复用户音量；超时或解码错误会关闭仍静音的播放器。
自动首帧任务持有可取消 ID，切离预览页、手动播放、停止或拖动都不会被迟到回调
反向覆盖。
GUI 不复制视频帧，也不启动额外解码进程。

## 扩展规则

新增能力时依次判断：

1. 是否是格式算法：放入 engine、archive、preview 或 model3d；
2. 是否是跨文件流程：放入 business/session；
3. 是否只是显示与交互：留在 GUI；
4. 是否会写文件：必须提供指纹检查、暂存、验证和失败回滚；
5. 是否会进入批量：必须证明目标是结构化 TBL/DAT；
6. 是否引入原生/第三方组件：同步更新 release pins、许可证清单和 package probe。

不要为了方便让 GUI 持有解析器内部状态，不要把非文本预览资源扩展成隐式可编辑
目标，也不要把真实游戏 corpus 提交到公开仓库。
