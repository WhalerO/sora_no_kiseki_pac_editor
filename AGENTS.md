# AGENTS.md

本文件写给第一次接触 `TIS_Retext/` 的协作者、代理或维护者。
它的目标不是回顾历史，而是帮助你快速回答三个问题：

1. 这个包是干什么的？
2. 主要代码应该从哪里看起？
3. 修改时最容易踩到什么坑？

## 先建立心智模型

把 `TIS_Retext/` 理解成一层“统一文本服务”就够了。

它主要做四件事：

- 解析 `.tbl/.dat` 中的文本；
- 让上层能够编辑这些文本；
- 把修改后的文本重新保存回文件；
- 把这些能力组织成 GUI、批量替换和版本对比功能。

这意味着：

- GUI 不是核心；
- 核心是服务层和业务层；
- 各种按钮、批处理、对比页，本质上都在调同一套底层接口。

## 代码应该从哪里看起

第一次阅读时，建议按这个顺序看：

### 1. `retext/core.py`

这里是统一入口。
最重要的类是：

- `RetextService`

### 2. `retext/domain.py`

这里定义核心数据对象：

- `TextDocument`
- `TextUnit`
- `SavePlan`

### 3. `retext/session.py`

这是单文件编辑的会话层。

### 4. `retext/business.py`

这里是业务层。

### 5. `retext/engines/`

这里是各后端算法引擎。

- `legacy/`: 包含旧版敏捷逻辑及其内置实现。
- `kuro/`: 包含 KuroTools 结构化后端及其内置库与 Schema。

### 6. `retext/archive/`

这是 PAC 服务层：

- `fpac.py`: 严格解析、流式构建与输出验证；
- `workspace.py`: 懒物化缓存、清单、锁和安全清理；
- `session.py`: 将 PAC 条目接入现有 `DocumentSession`；
- `collection.py`: 多 PAC 工作台、树节点展开与批量目标物化；
- `compare.py`: 独立比较 PAC/PAC 组，并按需物化被选中的差异条目；
- `fallback.py`: 只在用户显式选择时调用 `pac_tools/`。

PAC 只负责容器层；条目物化后仍由现有 TBL/DAT 服务处理。

### 7. `tk_gui.py`

这里是界面，位于根目录。

界面有“PAC 模式”和“解包模式”，默认使用 PAC 模式。预览、单文件和批量
在两种模式下复用相同的文本业务，只切换来源与保存策略：PAC 模式以左侧
PAC 树为唯一范围来源，解包模式以左侧普通 TBL/DAT 树为范围来源。
包内文件管理页例外：可以在页内显式选择已打开的 PAC 及当前目录，负责提取、
替换、插入和导出；不要将它的选择隐式覆盖为左侧树选择。
左侧文件名/正文共用一个搜索框，正文只读搜索当前模式下全部已打开的文本资源。

只读预览是右侧独立业务页，不属于左侧文件树面板。单文件编辑直接在条目
表格的“当前文本”单元格中完成，不要重新增加底部双文本编辑框。

版本对比是顶层独立功能，不依赖已打开的 PAC 文件树。PAC 模式比较用户
分别选择的新旧 PAC 或 PAC 组；解包模式保留目录或单个 TBL/DAT 对比。
PAC 差异条目只能物化到程序数据目录下的受管暂存区，不要使用系统临时目录。

## 后端是怎么分的

这个包内部有三条主要后端路径：

- `legacy`
- `kuro_tbl`
- `kuro_dat`

你可以这样理解：

### `legacy`

- 更快；
- 更偏兼容；
- 更适合敏捷任务；
- 当前 DAT 默认路径。
- `#TBL` / `#scp` 使用共享的精确引用清单：SLOT 放不下时执行布局保持型重定位，
  不属于启发式回退；只有无法建立结构布局的旧格式 REPACK 默认关闭。

### `kuro_tbl`

- 专门处理 TBL；
- 更结构化；
- 更适合安全回环与整体 repack。
- 等长文本可原位写回，变长文本使用保留固定记录的 `pool-repack`。引用来自 Schema
  或完整记录列发现；未知 Header、短文本和字符串后缀别名都必须进入同一引用图。
  不要改回按数值搜索或 4 字节模式替换。

### `kuro_dat`

- `#scp` 默认通过精确二进制布局剪接全部结构化字符串，脚本字节不重编；
- 只有布局解析失败时才使用 experimental 的反汇编/重编回退。
- 未知 OP_24 命令使用保留数值对的 `Cmd_unknown_XX_YY` 占位；这只补齐读取，
  不代表脚本语义或重编字节已经得到证明。
- 重编产物必须与修改后的反汇编脚本具有相同的函数、指令、跳转、命令和非文本
  操作数结构；即使 `PUSHSTRING` 全部回读成功，只要结构指纹变化也必须拒绝写入。

## 默认路由规则

GUI 使用两个独立的全局选项，默认 TBL=`kuro_tbl`、DAT=`legacy`；预览、单文件、
批量、对比和回读必须使用同一组选项。`RetextService` 在调用者没有显式指定引擎时，
仍保留下列 API 兼容路由：

- `WorkflowMode.AGILE`
  - 走 `legacy`
- `WorkflowMode.SAFE` + `.tbl`
  - 走 `kuro_tbl`
- `WorkflowMode.SAFE` + `.dat`
  - 目前仍走 `legacy`

这条规则很重要，因为很多表面上的 GUI 行为，最后都取决于这里。

## 修改时的基本原则

### 1. 优先改服务层和业务层

如果你要加功能，优先考虑接到：

- `RetextService`
- `DocumentSession`
- `WorkspaceBusiness`

不要把复杂逻辑直接写死在 GUI 回调里。

PAC 范围选择应优先表达为 `PacNodeRef`，再由 `PacWorkbench` 展开和物化；
业务层使用 `BusinessFileTarget` 同时保留缓存实际路径与 PAC 内逻辑路径。
不要把工作区中的数字缓存文件名泄漏为业务身份。

编辑器批量使用 `ordered=True` 按映射顺序预演和执行；默认业务 API 仍保留同时
替换兼容语义。更改映射后必须重新扫描，取消前序依赖导致后续输入不符时不得
猜测偏移继续写入。正文搜索独立于批量扫描状态，不应解锁或重置批量完整性门禁。

### 2. 不要轻易破坏 `legacy`

即使结构化后端更“优雅”，`legacy` 仍然有价值：

- 速度快；
- 与现有流程兼容；
- 对某些 DAT 场景更稳。

### 3. 对 DAT 回退保守一点

`kuro_dat` 的精确 `#scp` 路径与脚本回退必须分开判断。如果你改它，必须关心：

- 是否发现全部结构化字符串字段；
- 变长后非文本指令字节是否保持；
- 回退重编后的结构指纹和全文是否都能回读。

### 4. 不要把测试产物混进源码逻辑

包内常见的非源码目录有：

- `scratch/`
- `.runtime/`

它们是运行期或测试期产物，不应当被当成正式代码的一部分。

## 资源依赖

这个包已经内置了所有必要的底层引擎资源：

- `KuroTools` 相关资源（已集成在 `retext/engines/kuro/`）
- legacy 兼容实现资源（已集成在 `retext/engines/legacy/`）

在维护时要记住：

- 这个包是整合层；
- 它的能力来自“包内代码 + 内置引擎资源”共同工作。

## 推荐验证方式

至少执行：

```powershell
# 编译检查
python -m py_compile tk_gui.py retext/domain.py retext/core.py retext/business.py
# 烟测
$env:PYTHONPATH='.'; python scripts/smoke_test.py
```

发布前建议执行：

```powershell
# 发布预检
$env:PYTHONPATH='.'; python scripts/release_check.py
```

## 打包提醒

如果只是开发，不需要打包。
如果需要交付给不装 Python 的用户，优先使用：

- `PyInstaller onedir`

对应脚本：

- `scripts/build_exe.ps1`
