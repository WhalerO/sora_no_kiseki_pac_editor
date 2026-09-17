# TIS_Retext

`TIS_Retext` 是面向《空之轨迹》重制版系列资源的 Windows 桌面工作台。它把
the 1st / the 2nd 的 TBL/DAT 文本服务、FPAC v1 容器工作区、媒体预览和 MDL
三维预览组织在同一套业务边界内。

当前开发版本为 `0.1.3.dev20260917`。下载包的版本以 Releases 页面为准。
项目自有代码采用 [MIT 许可证](LICENSE)，
第三方组件保留各自的许可与版权声明。

## 下载与开始使用

在 [0.1.3 下载页面](https://github.com/WhalerO/sora_no_kiseki_pac_editor/releases/tag/v0.1.3.dev20260917)
的 **Assets** 中下载 Windows x64 的 `win-x64-onedir.zip`，完整解压后运行 `TIS_Retext.exe`，
无需安装 Python。请保留同目录的 `_internal` 文件夹，并解压到可写的非系统盘目录。
`Source code` 是源码，`third-party-sources.zip` 是第三方对应源码，均不是工具安装包。
其他版本见 [全部 Releases](https://github.com/WhalerO/sora_no_kiseki_pac_editor/releases)。

使用步骤见 [编辑器使用指南](docs/EDITOR_GUIDE.md)。先备份原始资源；修改后使用
“导出 PAC”另存为新文件，检查结果后再替换游戏资源。

[常用文本替换_不含金.json](mappings/常用文本替换_不含金.json) 是附带的参考方案，
也可以在 Release 附件或工具压缩包的 `mappings/` 目录取得。它包含 30 条规则，
在“批量”页导入映射表后使用；不包含“金”的替换和章节图片替换。
导入会替换当前映射表，执行前请查找匹配并核对勾选结果。详见
[参考方案说明](mappings/README.md)。

本项目为非官方工具，不附带游戏 PAC、贴图、模型、语音或完整游戏文本。
工具许可证不授予游戏资源的使用或再分发权利。本次修复说明见
[版本说明](docs/RELEASE_0.1.3.dev20260917.md)。

## 主要能力

- 解析、编辑和回写 `.tbl` / `.dat` 文本；
- 打开多个 FPAC v1 `.pac`，按需物化条目并只重建明确修改的内容；
- 对 PAC、PAC 组、解包目录或单个 TBL/DAT 做版本对比；
- 预览 PNG/DDS、WAV、WebM、FNT、MI 和 MDL；
- 预览 MDL 网格、材质、外部 DDS、节点/骨骼动画和模型名称追溯；
- 提取包内文件、文件夹或多个所选节点；替换文件；批量插入外部文件。

预览是只读业务。图片、音频、视频、模型及其他资源即使出现在所选文件树中，
也不会进入批量修改流程。

PAC 容器本身没有可靠的 1st/2nd 标记。工具默认按每个 TBL 的 Header 名称、
记录长度与数量对 Schema 布局评分；也可以在“设置”中手动指定 the 1st 或 the 2nd。
已经由记录结构证明的逐文件布局始终优先，避免2nd PAC中内置的 FC/Sora1 表被
错误 Schema 漏读；手动选项只在布局无法唯一判断时作为后备，并贯穿预览、
单文件、批量写回及回读校验。自动模式会明确显示回退状态，而不会把推测伪装成
PAC 元数据。

## 关键安全边界

- **批量替换只接受结构化解析成功的 TBL/DAT。** 文件夹、PAC 根或混合子树
  只是选择范围；展开后仍会过滤掉所有非 `.tbl/.dat` 文件。
- 批量扫描与保存通过 `RetextService` / `WorkspaceBusiness`；旧版任意原始字节
  替换入口已禁用。TBL 与 `#scp` DAT 的等长/非等长替换均使用具体引用字段重定位，
  不需要风险开关；只有无法建立结构布局的启发式回退默认关闭。写回后同时复核
  全部文本与非文本结构。
- 写入先在受管暂存区完成，并在回读验证后替换目标；覆盖已有文件时按业务规则
  保留备份。
- PAC 负责容器层，条目物化后仍由统一 TBL/DAT 服务解析；普通预览缓存不会被回包。
  只有主动“替换文件”或“插入外部文件”导入的非文本资源才会进入输出包。
- 源 PAC 或目标文件在操作期间发生变化时，流程会停止，避免覆盖外部更新。
- 模型、贴图、音视频等资源只读预览，不因勾选父节点而被修改。

## 批量替换与回包

批量页中的每一行代表一次明确的替换操作；同一文本单元内出现两次相同旧文本时，
会显示为两行并可分别勾选。映射表和逐次勾选结果可以分别导入、导出；“原文本”与
“新文本”单元格只显示本次命中附近的上下文，并以粗体标出实际替换片段。
映射表的每一对新旧文本都可以独立启用“完全匹配”：关闭时在文本单元内搜索全部
子串，启用时只有整个解析文本单元与旧文本相等才会命中。同一映射表可以混用两种
模式；JSON v2 使用逐行 `full_match` 保存选择，旧 JSON/TXT 默认保持包含匹配。
单文件“查找下一个”会显示“当前序号/总命中数”和字段位置；同一关键词存在多个
结果时需继续点击以循环定位。查找词和手输映射旧文本会清除复制时常见的首尾
空白、BOM 与零宽字符，但不会把简繁体或兼容汉字视为同一字符。

PAC 模式的保存顺序是：

1. 打开 PAC；一般保持默认设置即可，需要时在“设置”调整游戏版本和 TBL/DAT 引擎；
2. 在批量页添加左侧所选范围和替换映射，点击“1. 查找匹配”，复核勾选项；
3. 点击“2. 替换勾选项”，成功项会立即保存到受管 PAC 工作区，并自动复扫；
4. 不需要再执行其他保存操作；在左侧选择对应 PAC 或其子节点；
5. 点击“导出 PAC”，另存为新的 PAC 文件。

执行失败的操作会保留在复扫结果中，并在日志中按文件说明原因。解包模式不使用
PAC 工作区，成功项会直接写回 TBL/DAT，并按既有规则保留备份。
“允许启发式回退写入”只适用于无法建立结构布局的异常文件；正常 TBL/#scp DAT
变长写入不依赖该开关。具体能力与验证证据见
[文本引擎能力边界](docs/TEXT_ENGINE_BOUNDARIES.md)。

## 编辑与文件操作

完整的操作步骤见 [编辑器使用指南](docs/EDITOR_GUIDE.md)。
字号不合适时，在“设置 → 界面缩放”调整；字体、表格行高和侧栏宽度会一起调整。
文本直接在表格的“当前文本”单元格编辑，Enter 确认、Shift+Enter 换行、Esc 取消。

左侧资源树支持右键菜单，也可使用“提取所选”“替换文件”“插入外部文件”按钮。
提取文件夹会保留包内路径；多个 PAC 一起提取时，各自放进独立子目录。
替换与插入先写入当前会话的工作区，不会立刻修改源 PAC，最后务必“导出 PAC”。
关闭或退出时放弃未导出的会话，将丢失这些修改；这不是自动保存工程。

可使用当前游戏资源做只读源文件的集成检查：

```powershell
python scripts/check_editor_workflows.py --pac-dir '你的原版PAC目录'
```

该检查使用受管临时目录测试文本变长保存、图片替换/插入/提取和 PAC 重建，
结束后自动清理测试产物；`--keep` 可保留产物用于手动检查，不会覆盖原 PAC。

## 运行环境

- Windows x64；
- 开发与严格构建的基准解释器为 CPython 3.11；
- Tk、Pillow、NumPy、ModernGL、LibVLC 等依赖见 `requirements.txt`；
- 媒体播放需要 64 位 LibVLC。项目提供固定版本和 SHA-256 校验的准备脚本。

应用不依赖独立 FFmpeg，也不会把 `imageio-ffmpeg` 打入冻结包。WebM 头信息由
有界的纯 Python EBML 解析器读取；首帧和拖动定位画面由内嵌 LibVLC 静音解码后
暂停显示，不会启动系统播放器或额外解码进程。

在项目根目录执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\prepare_vlc_runtime.ps1
.\.venv\Scripts\python.exe scripts\launch_gui.py
```

也可以直接运行：

```powershell
.\.venv\Scripts\python.exe tk_gui.py
```

源码运行时，程序数据默认写入项目下的 `.runtime/`。冻结版默认写入：

```text
<TIS_Retext.exe 所在目录>\TIS_Retext_Data\
```

可以通过 `TIS_RETEXT_DATA_DIR` 显式指定其他位置。程序数据包含受管工作区、
暂存、可恢复垃圾区和短期缓存，不应复制到源码仓库或发布压缩包。
请将工具解压到可写的非系统盘目录；默认不再将缓存写入 AppData，也不会在目录
不可写时偷偷退回系统盘。旧版 AppData 缓存可在关闭旧程序并确认无需保留修改后清理。

## 验证

```powershell
$env:PYTHONPATH='.'
.\.venv\Scripts\python.exe scripts\smoke_test.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\release_check.py
```

`release_check.py` 会检查仓库卫生、依赖导入、Python 源码编译、回归测试和
合成 TBL/DAT 烟测。`--strict` 还会执行独立的严格构建配置；本次预览构建的
实际环境、检查结果和与严格配置的差异记录在版本说明中。

## 构建

日常开发可在 PowerShell 中一键构建：

```powershell
.\build.ps1
```

默认使用项目的 `.venv`；不存在时用 `py -3.14` 创建，再安装固定版本依赖。
首次构建前需准备 LibVLC 运行时，见 `scripts/prepare_vlc_runtime.ps1`。
入口会保持当前 PowerShell 版本，不会从 PowerShell 7 切换到 5.1；中文路径可以使用。
从其他目录调用脚本时也会定位到项目根目录。任一步失败立即停止，只有完整构建和包内自检
成功后才显示 `Build finished`，不再等待按键。

依赖已经安装时可以跳过联网安装；也可以指定现有 Python：

```powershell
.\build.ps1 -SkipDependencyInstall
.\build.ps1 -PythonExecutable 'D:\PythonEnv\Scripts\python.exe' -SkipDependencyInstall
```

也可直接调用底层普通构建脚本：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-release.txt
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 `
    -PythonExecutable .\.venv\Scripts\python.exe
```

默认输出为 `dist\TIS_Retext\TIS_Retext.exe`。这是 onedir 应用，运行时必须
保留整个 `dist\TIS_Retext\`，不能只复制 exe。
`dist\TIS_Retext-build.json` 仅在自检成功后生成；失败时不会保留上一次的完成标记。

构建脚本把 `TEMP`、`TMP` 和 PyInstaller 配置缓存放在项目目录的 `.runtime/`
下；默认包排除独立 FFmpeg、VLC GUI 插件及重复的 `libvlccore.dll`。正式体积
评估必须使用干净 CPython venv，不能使用会引入整套 MKL/BLAS 的共享 Conda 环境。

`-Release` 是严格发行构建，不等同于普通开发打包：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -Release `
    -PythonExecutable .\.venv\Scripts\python.exe
```

严格构建要求 CPython 3.11、逐文件校验的 vendored VLC、严格测试、干净的发行
目录、包内实际解码 probe、许可证闭包和发布审核记录。根 `LICENSE` 已由项目
所有者选择为 MIT；真实游戏语料不随源码或工具包分发。严格配置仍要求单独的
VLC 审核记录，不应把普通构建通过解释为严格构建通过。

发布依赖的已验证版本记录在 `requirements-release.txt`。它是版本基线，不是
带哈希的完整供应链锁；更新任一原生依赖后必须重新执行包内验证。

## 架构概览

```text
tk_gui.py                       GUI 组合与事件转发
retext/core.py                  统一文本服务入口
retext/domain.py                核心数据对象
retext/session.py               单文件编辑会话
retext/business.py              批量、对比等业务编排
retext/archive/                 FPAC、工作区、工程与比较
retext/preview.py               媒体/结构预览服务
retext/media_probe.py           有界 WebM/EBML 元数据解析
retext/model3d/                 MDL 几何、关联、动画及 CPU/GPU 渲染
retext/model_preview.py         旧导入路径兼容层
retext/playback.py              媒体播放控制
retext/engines/kuro/            KuroTools 结构化后端与资源
retext/engines/legacy/          Legacy 兼容后端
scripts/                        验证、依赖准备与打包
tests/                          合成 fixture 为主的自动化测试
```

详细依赖方向、数据流和扩展约束见 [架构说明](docs/ARCHITECTURE.md)。

## 文本后端

- `legacy`：兼容、快速；TBL/DAT 均使用完整引用清单，优先 SLOT，增长时执行结构化
  字符串池重定位。DAT 默认选择该后端。
- `kuro_tbl`：Schema 与通用引用发现结合的 TBL 后端，默认用于 TBL。
- `kuro_dat`：`#scp` 默认走精确二进制字符串池重定位；只有不支持的输入才回退到
  实验性脚本反汇编/重编。

GUI 顶部的全局路由默认是 TBL=`kuro_tbl`、DAT=`legacy`，两者可以独立切换，且同一
选择贯穿预览、单文件、批量替换、版本对比和回读验证。服务 API 在调用者没有提供
显式引擎时仍保留以下兼容路由：

| 工作流 | 文件 | 后端 |
|---|---|---|
| `AGILE` | TBL/DAT | `legacy` |
| `SAFE` | TBL | `kuro_tbl` |
| `SAFE` | DAT | `legacy` |

如 KuroTools 已有可验证的格式规则，解析实现优先遵循其结构和约定；自研补充逻辑
必须由真实样本与合成回归共同验证。TBL 会合并 Schema 字段与未知 Header 的完整
记录列引用；短文本、标点文本和字符串内部别名都能参与替换。保存只重写记录过的
具体字段位置，不再按 4/8 字节数值模式扫描固定记录区。

## 预览范围

- PNG/DDS：页内解码和缩放；
- WAV/WebM：LibVLC 内嵌播放、进度、音量、首帧与拖动定位；WebM 容器元数据
  由轻量 EBML 解析器读取；
- FNT：FCV 字形记录，可选关联 DDS 图集；
- MDL：结构、材质、外部 DDS、三维网格、名称追溯和兼容动画；
- MI：校验并展示字段字典；
- LAY/JSON/VFX/BIN/FXO 等当前只提供提取或有限结构信息，不承诺完整语义预览。

MDL 预览优先使用 ModernGL/OpenGL 3.3；不可用时回退到 Pillow CPU 渲染。
当前基础颜色预览不等同于游戏完整着色器，遮罩、法线、toon、骨骼蒙皮之外的
材质动画仍可能缺失。

## 发布与第三方组件

- 当前应用版本来源：`retext/version.py`；
- 第三方组件清单：`THIRD_PARTY_NOTICES.md`；
- 发行步骤与阻断项：[docs/RELEASE.md](docs/RELEASE.md)；
- 变更记录：[CHANGELOG.md](CHANGELOG.md)。

项目自有代码采用 [MIT 许可证](LICENSE)。MIT 不覆盖第三方组件的
独立许可：KuroTools 保留 MIT 声明，PAC 回退脚本保留 GPL-3.0，媒体运行时及
Python 依赖保留各自声明。再分发完整工具包时，也需要遵守其中第三方组件的条款。
