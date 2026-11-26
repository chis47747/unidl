# UniDL

[English](README.md) | [简体中文](README.zh-CN.md)

UniDL 是一款运行在终端里的流媒体浏览器、定时任务管理器与原生下载器。它把不同平台的检索、登录、轨道筛选、许可证解析、定时追更和下载混流统一在同一套终端界面中，不依赖外部下载器外壳。

整体代码严格划分为三层，职责互不越界：

- **服务层（Services）**：各平台独立封装登录、目录、搜索、manifest 获取、服务专属选项以及许可证传输。
- **核心与界面层（Core / TUI）**：负责页面导航、三级设置、凭证与 CDM/Vault 策略、选轨、定时调度、进度与日志展示。
- **原生下载核心（Native downloader）**：进程内完成 manifest 解析、分片并发下载、解密、字幕/章节处理与最终封装。

## 核心功能

### 终端界面与个性化定制

- **键鼠同等可用**：所有按钮、列表与顶部导航均支持点击或快捷键操作；`Ctrl+B` 严格逐级返回，`Esc` 连按两次防误退。
- **双模式首页**：默认提供可按字母、国家/地区或媒体类型分组的服务网格，顶部附带「最近使用」快捷栏；也可以在设置里开启**实验版搜索首页（Beta Home）**，在一个搜索框内检索服务、导出文件、本地密钥库、设置项、JustWatch 片源或直接粘贴链接，并支持 `/schedule`、`/radar`、`/vault`、`/exports`、`/settings` 等斜杠命令。
- **实时预览的外观体系**：内置 **7 套主题**（深色、浅色、Tokyo Night、Catppuccin Mocha、Nord、Gruvbox、高对比度）、**9 套系统强调色**、**12 种首页标题渐变/纯色**、**3 档排版密度**（自动 / 舒展 / 紧凑）以及**色弱友好状态色（Okabe–Ito）**；上下键或点击切换即可即时预览，回车/应用保存，`Esc` 无损还原。
- **12 种界面语言**：内置英文、简体中文、繁体中文、西语、法语、葡语、意语、日语、韩语、俄语、德语（Deutsch）与从右到左排版的阿拉伯语，切换立即生效且不中断当前会话。
- **高频效率工具**：按 `?` 查看当前快捷键速查表、`Ctrl+P` 打开全局命令面板、设置页按 `m` 仅看已修改项、编辑框未保存退出二次确认，以及下载完成提醒（系统桌面通知或终端响铃）。

### 定时下载与即将上映雷达

- **灵活定时执行**：可为任意已安装服务指定未来时间，通过**直链 / 内容 ID** 或**服务内关键词搜索**自动执行；搜索模式默认采用保守消歧策略，遇歧义时标记为 `needs_review` 而不会误下错误剧集。
- **自动选集与追更台账**：支持单部电影、多集区间表达式（如 `S01E01-E05`、`all`）以及**持续追更（Watch ongoing）**模式；内置已下载集数台账（`completed_episodes`）自动跳过旧集，并支持上线延迟重试窗（`6–48 小时`）。
- **Dry-run 预检**：保存或正式触发前可一键试跑查找与选集匹配，不下载视频也不改动任务状态。
- **四数据源即将上映雷达**：支持自由组合或单选 **JustWatch**、**TVMaze**、**AniList** 与可选 **TMDB**（勾选 TMDB 且未配置密钥时会立即弹出脱敏密钥输入框，支持 v3 Key 与 v4 Token），按日期聚合上线片单，选中即可一键带入计划下载。
- **自适应 2:3 海报预览**：在支持 Kitty 图形协议（Kitty / Ghostty）或 Sixel（iTerm2 / WezTerm）的终端下默认展示高清海报与详情卡；在 macOS 原生 `Terminal.app` 或窄窗口下自动隐藏且不发任何图片请求，也支持手动开启字符色块回退（`always`）或按 `p` 随时切换。
- **前台冲突排队与系统后台托管**：前台正在下载时到期的计划会自动排队等待接管；支持在应用内一键安装 **macOS `launchd`**、**Linux `systemd --user`** 或 **Windows 任务计划程序**，关闭 UniDL 后仍可按时在后台运行，并通过跨进程文件锁避免重复触发。

### 点播（VOD）与轨道控制

- 支持 DASH/MPD、HLS、ISM/Smooth Streaming、JSON manifest 与直链媒体，并支持多 manifest 合并选轨。
- 视频、音频、字幕独立筛选（分辨率、编码、HDR/Dolby Vision/HLG/HDR Vivid、Atmos/Audio Vivid、语言、声道与字幕类型），并提供**输出范围（Output scope）**（常规封装 / 仅视频 / 仅音频 / 仅字幕 / 自定义），适合批量只下字幕或音轨。
- 支持通过 `dovi_tool` 生成 **Dolby Vision + HDR10 混合轨**、可选章节获取与内嵌、以及标题附件（海报/剧照）预览与保存。
- 封装支持 `ffmpeg`、`mkvmerge`、`MP4Box` 或**不混流（保留原始解密轨道）**；内置断点续传缓存、可选下载前**内容密钥校验**、以及跨导出文件与服务的**混合导入队列**（兼容 UniDL 与第三方导出格式）。
- 支持自定义剧集/电影命名模板、分季子目录模板以及点/空格/连字符风格，文件标签严格按最终实际选中的轨道生成。

### 直播录制

- 支持直播 HLS、DASH/fMP4 等可刷新流，录制前可先完成轨道筛选。
- 支持检查回看（DVR）窗口、从直播边缘或指定时间偏移起录、定时长或无限时长（`00:00:00`）录制、边录边合并与 pipe mux 实时封装。
- 支持直播密钥轮换，并区分 `Back` 正常收尾封包与 `Esc` 快速中断保留已录分段。

### DRM、密钥库与凭证隔离

- 严格按 DRM 体系匹配本地 CDM（Widevine `.wvd`、PlayReady `.prd`、MonaLisa `.mld`），支持远程 CDM 与按分辨率分流规则。
- 支持本地 SQLite 与远程 HTTP Key Vault，读取、写入、搜索目标均可独立配置并支持单服务覆盖，也可在界面内手动录入 `KID:key`。
- 每个服务及登录方式拥有独立的凭证槽、cookie 配置档与 token 生命周期。

### 纯音频、代理与路径管理

- 纯音频服务与音轨采用专属分栏布局（封面预览 + ID3v2 标签），原生兼容格式可直接保留，其余通过 FFmpeg 转换（`mp3 / flac / alac / m4a / opus / wav`）。
- 外部 Helper 按声明顺序从配置、`PATH`、项目 `binaries/` / `helpers/` 或包资源解析，不扫描无关目录。
- 支持静态 HTTP/HTTPS/SOCKS 代理池，以及 ExpressVPN（OAuth 设备授权）、NordVPN、Surfshark、Windscribe 代理集成，并可将 API 请求与媒体分片下载分流控制。

## 安装

需要 macOS、Windows 或 Linux 上的 **Python 3.11+**（建议 64 位）。建议同时安装 **FFmpeg（含 `ffprobe`）** 以获得完整的混流、转码与探测能力；`mkvmerge`、`MP4Box`、`aria2c`、`dovi_tool` 按需安装即可。详细依赖见 [环境要求与安装](docs/zh-CN/requirements.md)。

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

开发与测试环境：

```console
python -m pip install -e '.[dev]'
# 或使用 uv：
uv sync --extra dev
```

## 快速上手

首次启动无需先手写 `unidl.yaml`，直接运行即可进入交互界面：

```console
unidl
```

（等价于 `python -m unidl`；源码环境下也可执行 `uv run unidl`。）

未指定自定义配置时，成品默认保存在当前项目目录的 `downloads/<服务名>/`。首次进入主页会提示当前生效路径，点击即可直接进入 **设置 → 文件与命名 → 输出位置** 修改，无需手动编辑 YAML。

如需加载指定配置文件：

```console
unidl --config ./unidl.yaml
```

### 常用命令行工具

只读查看与环境检查：

```console
unidl --help
unidl services
unidl cdm --check
unidl keys <kid> --service <service-id>
```

直接解析或下载已导出的 JSON manifest / 媒体链接：

```console
unidl list <manifest-or-json>
unidl download <manifest-or-url> --save-name "Example.Title"
```

管理与执行定时下载任务及系统后台托管：

```console
unidl schedule list
unidl schedule run --dry-run --id <job-id>
unidl schedule run
unidl schedule daemon --interval 60
unidl schedule host status
unidl schedule host install --interval 5
```

## 配置与运行时数据

- **`unidl.yaml`**：静态配置入口（自定义路径、本地/远程 CDM、账号凭证、代理与 Helper）。需要自定义时可复制 [`docs/unidl.example.yaml`](docs/unidl.example.yaml)，常见路径、CDM、密钥库与代理也可直接在 **设置** 中图形化管理。包含私密信息的配置请妥善保管并排除在 Git 之外（Unix 建议 `chmod 600`）。
- **`<paths.home>/settings.json`**（`0600` 权限、跨进程加锁原子写入并保留 `.bak` 备份）：保存全局、单服务与选轨偏好。
- **`<paths.home>/schedules.json`**（`0600` 权限）：保存计划任务、追更台账与脱敏后的执行历史。
- token、cookie、CDM 设备文件、密钥库、日志与导出文件均属本地运行时数据，切勿提交到版本库。

## 目录结构

```text
src/unidl/core/        核心契约、DRM、密钥库、定时调度、即将上映雷达、存储与流程引擎
src/unidl/tui/         Textual 终端界面、主题系统、海报预览与各功能页面
src/unidl/downloader/  进程内 manifest 解析、并发传输、解密与混流引擎
src/unidl/locales/     12 种界面语言本地化词库
src/unidl/services/    流媒体服务包（每项服务独立子包）
docs/                  架构说明、功能手册与服务接入规范
tests/                 离线契约测试与集成测试
```

## 文档导航

完整索引见 [docs/README.md](docs/zh-CN/README.md)：

- **入门与配置**：[环境要求与安装](docs/zh-CN/requirements.md) · [静态配置](docs/zh-CN/configuration.md) · [三级设置](docs/zh-CN/settings.md) · [就绪检查](docs/zh-CN/readiness.md)
- **界面与效率功能**：[界面说明](docs/zh-CN/interface.md) · [UI 设计规范](docs/zh-CN/ui-design.md) · [实验版搜索首页](docs/zh-CN/home-interface.md) · [定时下载与即将上映雷达](docs/zh-CN/scheduled-downloads.md) · [JustWatch 片源查询](docs/zh-CN/availability.md) · [混合导入队列](docs/zh-CN/import-queue.md) · [多语言本地化](docs/zh-CN/i18n.md)
- **播放与交付**：[轨道筛选规则](docs/zh-CN/track-selection.md) · [原生下载器集成](docs/zh-CN/downloader-integration.md) · [直播录制](docs/zh-CN/live.md) · [纯音频服务](docs/zh-CN/audio.md) · [章节支持](docs/zh-CN/chapters.md) · [第三方导出兼容](docs/zh-CN/third-party-exports.md)
- **DRM 与安全**：[DRM 体系](docs/zh-CN/drm.md) · [Key Vault 密钥库](docs/zh-CN/key-vault.md) · [内容密钥校验](docs/zh-CN/key-verification.md) · [安全规范](docs/zh-CN/SECURITY.md)
- **扩展与排错**：[架构设计](docs/zh-CN/architecture.md) · [编写服务插件](docs/zh-CN/writing-a-service.md) · [测试指南](docs/zh-CN/testing.md) · [故障排查](docs/zh-CN/troubleshooting.md)

## 开发检查

```console
python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
```

提交代码或反馈问题时，请勿包含任何账号凭证、cookie、token、CDM 设备材料、密钥库内容、TMDB API 密钥或带签名的私有链接。

## 许可证

UniDL 基于 [MIT License](LICENSE) 开源发布。[`docs/downloader/legal/THIRD_PARTY_NOTICES`](docs/downloader/legal/THIRD_PARTY_NOTICES) 中列出的第三方组件仍遵循各自原有许可协议。

Copyright © 2026 Chris20
