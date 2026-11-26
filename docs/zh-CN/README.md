# UniDL 文档

[English](../README.md) | [简体中文](README.md)

本目录汇总了 UniDL 的公开架构、用户工作流、配置契约及服务扩展规范。

## 快速开始

- [环境要求与安装](requirements.md) — 支持的 Python 版本、外部工具、包安装方式与运行时数据。
- [配置说明](configuration.md) — `unidl.yaml`、路径配置、设备、代理与运行时状态。
- [设置参考](settings.md) — 全局管理器、单服务选项与共享输出轨道偏好。
- [就绪检查](readiness.md) — 核心、全局与可选增强依赖的就绪等级。
- [系统架构](architecture.md) — 核心层/TUI、服务层与原生下载器之间的边界划分。
- [故障排查](troubleshooting.md) — 常见安装配置、DRM、播放、定时任务与海报显示问题。
- [更新日志](../../CHANGELOG.md) — 公开版本发布说明。

## TUI、发现与自动化

- [界面说明](interface.md) — 核心界面、快捷键、命令面板与主题。
- [UI 设计](ui-design.md) — 布局规则、视觉 Token、外观控制与无障碍设计。
- [导航规则](navigation.md) — 单级返回（Back）规则与流程归属。
- [实验性主界面](home-interface.md) — 可选开启的搜索优先主界面、模式、本地建议与斜杠命令。
- [定时下载与即将上映雷达](scheduled-downloads.md) — 定时任务、自动选集、4 源即将上映雷达（JustWatch、TVMaze、AniList、TMDB）、海报预览、操作系统后台托管与 CLI。
- [片源可用性查询](availability.md) — JustWatch 检索与地区播放源映射。
- [国际化](i18n.md) — 12 种内置界面语言、阿拉伯语 RTL 布局与翻译边界。

## 播放与交付

- [轨道选择](track-selection.md) — 输出范围、筛选器、Best/All 规则、默认轨道与字幕位置。
- [原生下载器集成](downloader-integration.md) — manifest 输入、直播重授权、点播源续期与交付集成。
- [批量队列](batch.md) — 多集选择与队列执行。
- [取消与断点续传](cancelling.md) — 停止点播或直播任务并保留检查点。
- [混合导入下载队列](import-queue.md) — 跨导出文件、跨服务、跨媒体类型的顺序下载队列。
- [第三方导出文件](third-party-exports.md) — 通过通用下载器交付 Unshackle 等非 UniDL 导出文件。
- [直播频道](live.md) — 录制、DVR/回看窗口、时长控制与密钥轮换。
- [纯音频服务](audio.md) — 音频展示、元数据与导出。
- [章节处理](chapters.md) — 章节获取与容器内嵌。
- 下载器专项参考：
  [ASS 字幕](downloader/ass-subtitles.md)、
  [MP3 音频格式](downloader/mp3-audio-format.md)、
  [JSON manifest 格式](downloader/json-manifest-format.md)、
  [直播管道混流](downloader/live-pipe-mux.md)、
  [ISM / Smooth Streaming](downloader/ism-smooth-streaming.md) 与
  [Audio Vivid](downloader/audio-vivid.md)。

## DRM、密钥库与安全

- [DRM 系统](drm.md) — 本地/远程 CDM、PlayReady PSSH 处理与许可证操作边界。
- [密钥库（Key Vault）](key-vault.md) — 本地 SQLite 与远程 HTTP 密钥库、读写策略与手动密钥。
- [内容密钥验证](key-verification.md) — 可选的下载前加密轨道密钥验证与回退规则。

## 扩展与贡献

- [编写服务](writing-a-service.md) — 包目录结构、注册、Flow ask、播放、DRM、凭据与测试。
- [仅编译服务](compiled-services.md) — 原生扩展清单与 ABI/平台兼容性。
- [外部工具（Helper）](external-helpers.md) — 外部依赖声明与解析规则。
- [合作方授权](partner-authorization.md) — 服务间登录交接契约。
- [播放会话生命周期](playback-lifecycle.md) — 会话、心跳与清理规范。
- [测试指南](testing.md) — 离线检查、在线检查与回归实践。
- [发布指南](publishing.md) — PyPI 发布、受信任发布与 GitHub 账号切换。

提交改动前请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [SECURITY.md](SECURITY.md)。切勿将凭据、密钥和设备文件纳入提交；核心层或服务契约发生变更时，请同步更新对应的文档。
