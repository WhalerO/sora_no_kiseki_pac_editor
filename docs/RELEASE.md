# TIS_Retext 发布指南

## 当前状态

当前为 `0.1.3.dev20260917` 预览版。项目所有者已选择 MIT，并授权更新 GitHub
与 Windows 编译预览包。本次发布不包含真实游戏语料或本地开发历史；文本/PAC
自动验证使用原创合成样本。实际发行环境及证据见
[本次版本说明](RELEASE_0.1.3.dev20260917.md)。

下面的 `-Release` 是既有 CPython 3.11 严格构建配置，与本次 CPython 3.14.3
预览构建区分记录。尚未完成的严格审核记录保持缺失，不填写虚假的人工批准。

自动检查通过不替代许可证和授权审核，不得通过添加占位 `LICENSE` 或虚假授权
文件绕过门禁。

## GitHub 预览版发布步骤

源码推送、构建成功、上传附件和公开发布是不同的步骤。草稿即使已有 ZIP，普通用户也无法下载。

1. 完成版本号、CHANGELOG 和版本说明更新，将源码推送到公开仓库的 `main`。
2. 为该提交建立与版本号一致的标签，例如 `v0.1.3.dev20260917`；不要移动已发布的版本标签。
3. 在 Releases 创建使用该标签的草稿，填写更新说明，并勾选 **This is a pre-release**。
4. 在 Actions 运行 **Build preview release assets (draft)**，填写刚才的标签。工作流从该标签构建，完成自检后只上传到草稿，不自动公开。
5. 检查草稿的五个附件：Windows `win-x64-onedir.zip`、`third-party-sources.zip`、参考映射 JSON、版本 `release.json` 和 `SHA256SUMS.txt`。确认版本、公开源码提交与附件完整性一致。
6. 点击 **Publish release**。保留预览版标记也能公开下载，无需为了下载而将其标成稳定版。
7. 用未登录的浏览器打开该版本页面，确认 Assets 中能下载 Windows ZIP。GitHub 的 `Source code` 附件不是可直接运行的工具包。

本项目的下载入口使用具体版本页面；预览版不应依赖只指向非预览版本的 `/releases/latest`。

## 两种构建

### 开发构建

开发构建用于本机验证。它允许显式指定或回退到可用的 64 位 LibVLC，不把
“成功生成 exe”解释为可对外发行。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 `
    -PythonExecutable .\.venv\Scripts\python.exe
```

开发构建仍会：

- 清理旧的 PyInstaller 输出；
- 收集第三方许可证；
- 运行打包后的原生依赖 probe；
- 拒绝发行目录中的 `TIS_Retext_Data`、PAC、MDL 和 DDS；
- 生成 `dist\TIS_Retext-build.json`。

### 严格 Release 构建

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -Release `
    -PythonExecutable .\.venv\Scripts\python.exe
```

严格模式额外要求：

- CPython 3.11.x；
- `scripts/prepare_vlc_runtime.ps1` 准备的 VLC 3.0.23 win64 runtime 及匹配
  manifest；
- `scripts/release_check.py --strict` 成功；
- 根项目许可证、第三方通知和真实 corpus 授权门禁全部满足；
- 发行目录不含任何运行数据或真实游戏资源。

当前阻断项存在时，`-Release` 失败是正确结果。

## 准备干净环境

严格构建只支持 Windows x64 和 CPython 3.11。不要使用装有大量无关包的共享
Conda 环境。

共享 Conda 环境还可能让 PyInstaller 收入整套 MKL/BLAS DLL，显著放大发行物，
并把环境中无关包的依赖冲突带进审计结果。不要手工删减这些 DLL；应在干净的
CPython 3.11 venv 中重新构建，并以包内 probe 和目标机烟测确认依赖闭包。严格
`-Release` 会直接拒绝 Conda；普通开发构建只给出体积警告。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-release.txt
```

`requirements-release.txt` 固定当前验证过的版本，但尚未提供带 hash 的完整锁。
更新任何依赖时应在独立变更中完成，并重新验证源程序、GPU/CPU fallback、
LibVLC 和冻结包。

构建脚本会把 `TEMP`、`TMP` 和 `PYINSTALLER_CONFIG_DIR` 放到项目内
`.runtime/build-temp` 与 `.runtime/pyinstaller`，使大型中间文件的位置明确可控。

准备固定 LibVLC：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\prepare_vlc_runtime.ps1
```

该脚本固定 VLC 3.0.23 win64 的 URL、文件大小和 SHA-256，清除旧插件树，并在
`_vendor\libvlc\win-x64\.tis-retext-runtime.json` 记录输入身份及每个运行时文件的
大小和 SHA-256。严格构建逐项复核该清单，不接受系统安装的 VLC 或任意
`-VlcRuntimeDir` 作为替代。

## 严格阻断项

### 根项目许可证

项目所有者于 2026-09-16 选择 MIT，根 `LICENSE` 已收录。许可适用于项目自有
代码，第三方组件继续遵循各自许可证，不能将整个包含第三方组件的目录简单视为
仅受 MIT 约束。

### 媒体栈边界

默认依赖、开发构建和严格 Release 均不安装或分发独立 FFmpeg。WebM 头信息由
包内 EBML 解析器读取，视频解码与定位统一交给固定的 LibVLC 运行时。构建脚本
显式排除 `imageio_ffmpeg`，避免构建机上偶然安装的副本泄漏进发行目录。

这只消除了独立的 FFmpeg 可执行文件及其重复体积；官方 VLC runtime 仍包含
`libavcodec_plugin.dll` 等组件化编解码模块。发布审计必须继续以完整 VLC 发行物
为单位核对其许可证、组件通知与对应源码条件，不能宣称媒体栈完全不含 FFmpeg
衍生组件。

严格门禁还要求人工审核后的 `licenses/VLC-SOURCE.json`。记录必须绑定固定的
二进制版本与哈希，并列出对应源码 URL、源码 SHA-256、提供方式、审核人和日期；
缺失或仅有未经审核的占位记录都会阻断正式 Release。此门禁用于防止许可证/源码
义务只停留在文档提示中，不代表程序替项目所有者作出法律判断。

审核完成后记录结构如下（哈希、提供方式、审核人和日期必须填写真实值）：

```json
{
  "component": "VLC win64 runtime",
  "version": "3.0.23",
  "binary_archive": "vlc-3.0.23-win64.zip",
  "binary_archive_sha256": "992D19DBD0B8A7CDE9167D2F7780B1EF6F92ACC8A71ACFA736101A21F35181E1",
  "source_archive_url": "https://…",
  "source_archive_sha256": "<64 位十六进制 SHA-256>",
  "corresponding_source_offer": "<随发行物提供源码的具体方式>",
  "component_notice_inventory": "<插件/编解码组件通知审计清单或其位置>",
  "reviewed_by": "<审核人>",
  "reviewed_on": "YYYY-MM-DD",
  "status": "approved"
}
```

嵌入式播放器不使用 VLC 自带 Qt/skins 界面，因此构建只排除 `plugins/gui`；
解封装、VP8/VP9、Opus/Vorbis、音视频输出和软硬件回退插件仍完整保留。PyInstaller
若额外收集出同哈希的根目录 `libvlccore.dll`，会在包内 probe 前移除该重复副本。

以当前固定运行时计，独立 FFmpeg 约 83.6 MiB，已完全移除；VLC GUI 插件约
18.9 MiB，也已排除。剩余自包含 LibVLC 播放运行时约 116.7 MiB，其中
`libavcodec_plugin.dll` 约 16.5 MiB，但真实 WebM 仍会使用它参与 packetize/decode，
不能仅按文件名继续删除。若未来提供不含媒体播放的 Core 包，可整体省去这部分；
当前默认包优先保证 WAV/WebM 离线、内嵌、可 seek/调音量的完整体验。

### 真实测试语料

正式仓库不应跟踪游戏 PAC、完整解包 TBL/DAT、MDL/DDS 或它们的重建副本。
`test_raws/README.md` 可以说明如何由有权访问语料的维护者在本地配置 corpus，
但不能包含语料本身。

若项目所有者确实拥有再分发授权，授权记录至少要包含权利人、范围、地域、期限、
允许分发的具体文件摘要和证明文件位置，并经过人工审核；简单的布尔 marker 或
维护者自述不能替代授权。

自动化测试应以合成 fixture 为主；真实 corpus 只作为仓库外、可跳过的集成测试。

## 发布前检查

在项目根目录执行：

```powershell
$env:PYTHONPATH='.'
.\.venv\Scripts\python.exe scripts\release_check.py --strict
git status --short
git diff --check
```

必须确认：

- 没有 tracked `.pyc`、`__pycache__`、`.runtime/`、`build/` 或 `dist/`；
- 没有真实语料和未授权二进制；
- 全部单元测试和 TBL/DAT 烟测通过；
- 依赖版本与 `requirements-release.txt` 一致；
- `THIRD_PARTY_NOTICES.md` 与实际包内容一致；
- `licenses/` 和运行时收集的 Python 包许可证完整；
- `licenses/VLC-SOURCE.json` 已由有权审核者批准，且与固定 VLC 输入一致；
- `retext/version.py`、CHANGELOG、exe version resource 和计划 tag 一致。

## 构建后验证

严格构建完成后检查：

```powershell
Get-Content build\package-probe.json
Get-Content dist\TIS_Retext-build.json
Get-FileHash dist\TIS_Retext\TIS_Retext.exe -Algorithm SHA256
```

要求：

- probe 的 `ok` 为 `true`；
- NumPy、Pillow、zstandard、ModernGL 和 LibVLC 均完成包内检查；VLC 来源为
  `bundled`，且生成的 PCM WAV 已通过实际解码和 seek；
- `dist\TIS_Retext\` 不存在 `TIS_Retext_Data/`；
- 除程序自身资源外，不存在 `.pac`、`.mdl`、`.dds`；
- 不存在独立 `ffmpeg.exe`、`ffprobe.exe` 或 `imageio_ffmpeg`；
- `_internal` 中包含项目/第三方 notice 和对应许可证；
- build manifest 记录版本、Git SHA、Python、VLC、打包模式和 exe SHA-256。

还应在没有开发环境的 Windows 测试机上完成一次人工烟测：

1. 启动 GUI；
2. 打开合成 PAC；
3. 预览 PNG、WAV、WebM 和 MDL；
4. 验证 GPU 路径及强制 CPU fallback；
5. 执行一次结构化 TBL/DAT 编辑和批量替换；
6. 重建 PAC 并回读；
7. 确认程序数据只出现在 exe 同目录的 `TIS_Retext_Data`（或显式指定的目录）。

## 组装发行物

以全新 staging 目录组装，不要直接压缩长期使用过的 `dist`：

- 复制完整 `dist\TIS_Retext\` onedir；
- 同时发布 `dist\TIS_Retext-build.json` 和 SHA-256；
- 保留全部许可证与第三方 notice；
- 不包含 `.runtime/`、workspace、trash、测试语料、诊断截图和构建缓存；
- 不包含私钥、签名配置或本地绝对路径日志。

若进行代码签名，签名后重新计算最终 ZIP 和 exe 的 SHA-256。最后创建与
`retext/version.py` 一致的 `vX.Y.Z` tag，并将 CHANGELOG 中对应版本从
Unreleased 固化为发布日期。

`-OneFile` 只用于普通开发构建的额外实验；严格 `-Release` 会明确拒绝该组合，
因为无法在启动前检查内嵌的 VLC 插件与独立 FFmpeg。资源、解压目录、杀毒误报
和故障诊断也都比 onedir 更困难。
