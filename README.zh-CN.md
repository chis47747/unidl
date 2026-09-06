# UniDL

[English](README.md) | [简体中文](README.zh-CN.md)

UniDL 是一款以终端界面为核心的媒体浏览器与原生下载器。它把不同流媒体服务连接到统一的工作流中：查找节目或频道、按需登录、检查可用媒体、选择需要的轨道、通过服务契约解析 DRM 密钥，并下载或录制最终内容。

项目由三个边界清晰的层组成：

- **服务（Services）**负责登录、目录、搜索、manifest、服务专属设置，以及服务自己的许可证请求。
- **Core/TUI** 负责导航、设置、凭证、CDM/vault 选择、轨道选择、进度、日志和交互工作流。
- **原生下载器（Native downloader）**在进程内解析 manifest、下载分片、解密媒体、写入字幕与章节并混流最终文件。

## 功能

### 交互式 TUI

- 支持键盘与鼠标操作，并为返回、取消和退出提供一致且可预期的行为。
- 服务首页可按服务能力提供 URL、搜索、直播、媒体库和登录入口。
- 搜索结果可以继续进入季度和单集，不会意外离开当前服务流程。
- 支持可配置地区与平台映射的 JustWatch 标题及可用性搜索。
- 支持响应式日志和进度面板、文本选择与复制、深浅色主题以及本地化界面文本。
- 提供显式的开发模式服务重载，不在后台静默监视文件。

### 点播（VOD）

- 支持 DASH/MPD、HLS、ISM/Smooth Streaming、JSON manifest 和直接媒体 URL。
- 视频、音频和字幕轨道可以独立选择输出。
- 支持分辨率、编码、动态范围、语言、声道布局和字幕筛选，并能在数据可用时标记 HDR、Dolby Vision 及音频编码。
- 支持服务通过不同档位提供多个 manifest 时构建多 manifest 播放计划。
- 可选获取章节并将章节写入最终容器。
- 输出命名可根据用户最终实际下载的轨道加入标题、季集、分辨率、音频和编码标签。
- 支持分片续传、缓存感知重试、后处理和混流。

### 直播录制

- 支持直播 HLS、DASH/fMP4 以及其他可刷新的播放列表。
- 在开始录制前完成轨道选择、回看/DVR 窗口检查、直播边缘或指定偏移选择，以及有限或无限时长设置。
- `00:00:00` 表示不限制时长；录制期间使用停止、返回或 Esc 即可结束。
- 在来源和容器允许时支持实时合并与 pipe mux。
- 支持轮换直播密钥：优先使用服务提供的 init data，仅在真正出现新 KID 时进入交互补充流程。
- TUI 中展示进度、回看窗口、分片数量、预计大小和取消状态。

### DRM、vault 与凭证

- 通过严格的 DRM system 匹配支持本地 Widevine、PlayReady 和 MonaLisa 设备契约。
- 可为受支持的系统配置远程 CDM endpoint，完成远程 challenge 与许可证解析。
- 支持本地 SQLite key vault 和兼容的远程 key vault，包括多 vault 读写策略、服务范围和手动 KID:key 录入。
- 许可证传输属于服务本身：共享 DRM 层只创建 challenge、解析响应；每个服务负责自己的 endpoint、headers 和请求格式。
- 各服务和登录方式拥有彼此独立的凭证槽、cookie profile、token 存储和刷新生命周期。

### 音频与元数据

- 纯音频服务和音频轨道使用专门的展示与命名路径。
- MP3 导出支持 ID3v2 元数据、封面、艺术家、专辑、标题字段以及章节感知后处理。
- 当来源已符合目标容器时可保留原始清晰音频，其他来源通过 FFmpeg 转换。

### Helper、代理与存储

- 服务声明的 helper 可从配置、`PATH`、项目 helper 目录或包资源中解析，不会任意扫描文件系统。
- 支持 HTTP/HTTPS 与 SOCKS 代理，并可使用用户配置的特定 VPN provider 集成。
- token、cookie、CDM、vault、缓存、命令、字幕和成品路径均可相对于项目配置。
- 可以保存 JSON 命令/导出文件，用于自动化与可复现下载。

## 安装

最低运行环境为 Windows、macOS 或 Linux 上的 **Python 3.11**。建议使用 64 位 Python，并安装 FFmpeg（包括 `ffprobe`），以获得完整的下载、转换和混流能力。Python 依赖见 [`requirements.txt`](requirements.txt)，开发检查依赖见 [`requirements-dev.txt`](requirements-dev.txt)。

从 PyPI 安装：

```console
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install unidl
```

从源码安装：

```console
git clone https://github.com/chis47747/unidl.git
cd unidl
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install .
```

开发和测试环境：

```console
python -m pip install -e '.[dev]'
```

也可以使用 `uv`：

```console
uv sync --extra dev
```

本地 CDM 路径请通过 `unidl.yaml` 中的 `cdm.devices` 配置。设备文件、Helper、cookie、token 和 vault 数据库都属于运行时数据，必须置于版本控制之外，并保存在配置的项目目录中。

外部工具、平台说明与完整的运行前检查请参阅[环境要求与安装](docs/requirements.md)。

## 使用 UniDL

启动已安装的 TUI：

```console
unidl
```

`python -m unidl` 与上面的命令等价。通过包安装时，如果当前目录存在 `unidl.yaml`，程序将使用该配置；否则使用安全的内置路径默认值。配置位于其他位置时请显式指定：

```console
unidl --config ./unidl.yaml
```

常用只读诊断命令：

```console
unidl --config ./unidl.yaml --help
unidl --config ./unidl.yaml services
unidl --config ./unidl.yaml cdm --check
unidl --config ./unidl.yaml keys <kid> --service <service-id>
```

原生下载器也可以读取导出的 JSON manifest 或直接来源 URL：

```console
unidl list <manifest-or-json>
unidl download <manifest-or-url> --save-name "Example.Title"
```

在 TUI 中选择服务，搜索或打开 URL，选择标题和轨道，再选择立即下载或仅保存命令/导出文件。服务专属设置只控制服务 API/profile 等选项；共享轨道设置只控制最终输出轨道。详见 [docs/settings.md](docs/settings.md)。

录制直播时，请先选择轨道，再设置录制、回看/DVR 行为和时长。保持 `00:00:00` 即可无限录制，使用停止、返回或 Esc 结束。

## 配置与数据

`unidl.yaml` 是静态配置入口。相对路径从该文件所在目录解析，因此源码目录可以安全移动。交互偏好保存在 `paths.home` 下的 `settings.json`。凭证、cookie、token、CDM、vault、日志、命令和导出文件均不需要提交。包含秘密的配置请使用私有文件：

```console
python -m unidl --config ./unidl.private.yaml
```

从源码运行时，程序使用显式指定的配置，或项目根目录下的配置。

## 项目结构

```text
src/unidl/core/        契约、DRM、vault、存储与流程引擎
src/unidl/tui/         Textual 界面与各个 screen
src/unidl/downloader/  原生解析、传输、解密和混流流程
src/unidl/services/    每个服务一个独立 package
helpers/               声明式 helper 资源与模块（保持私有）
cdm/                   本地设备文件（保持私有）
docs/                  架构、服务与下载器文档
tests/                 离线契约与集成测试
```

## 文档

请从 [docs/README.md](docs/README.md) 开始。常用文档包括：

- [架构](docs/architecture.md)：Core、TUI、服务与原生下载流程的边界。
- [环境要求与安装](docs/requirements.md)：运行环境、依赖与运行时数据。
- [编写服务](docs/writing-a-service.md)：新增服务并接入 Core 原生能力。
- [原生下载器](docs/downloader-integration.md)：输入格式、交付契约与进度。
- [配置](docs/configuration.md)与[设置](docs/settings.md)：静态配置和交互偏好的职责。
- [DRM](docs/drm.md) 与 [Key vault](docs/key-vault.md)：本地/远程 key 解析及服务自有许可证请求。
- [直播](docs/live.md)、[音频](docs/audio.md)与[章节](docs/chapters.md)：专用播放路径。
- [测试](docs/testing.md)、[故障排查](docs/troubleshooting.md)与[安全](docs/SECURITY.md)：验证和安全运行。

## 开发检查

```console
python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
```

修改服务后，应使用已授权的账号、地区和设备数据运行该服务的离线检查与真实播放检查。不要在 commit 或问题报告中包含凭证、cookie、token、CDM 私有材料、vault key 或已签名 URL。

## 许可证

UniDL 采用 [MIT License](LICENSE) 发布。列在
[`docs/downloader/legal/THIRD_PARTY_NOTICES`](docs/downloader/legal/THIRD_PARTY_NOTICES)
中的组件仍受其各自许可证条款约束。

Copyright © 2026 Chris20
