# 定时下载与即将上映雷达

[English](../scheduled-downloads.md) | [简体中文](scheduled-downloads.md)

UniDL 可以为任意已注册的服务安排定时任务，在指定时间自动运行——既支持直接输入 URL / 内容 ID，也可以走该服务自带的搜索流程。功能涵盖自动选集、上映窗口自动重试、持续追更台账、多源聚合的**即将上映雷达**、可选 2:3 海报预览，以及无人值守的系统后台托管（`launchd`、`systemd --user` 和 Windows 任务计划程序）。

## 打开管理界面

你可以在 TUI 的任意位置打开**定时下载**：

- **主页状态栏**：点击 **`schedule`** 标签（`cdm · drm · after picking · ready · import · schedule`）。当存在待执行、排队中、运行中或需要人工处理的任务时，该标签会直接摘要显示当前状态（例如 `schedule 2 · in 3h` 或 `schedule 1 !`）。
- **命令面板**：按 `ctrl+p` 并搜索 `Scheduled downloads`（别名：`schedule`、`timer`、`upcoming`、`radar`、`cron`、`launchd`）。
- **实验主页（Beta）**：输入 `/schedule` 打开定时任务标签页，或输入 `/radar` 直接打开即将上映雷达。
- **JustWatch 搜索与上架情况页面**：高亮任意作品并按 `t`，即可打开任务编辑器，并自动预填该作品的标题、年份、内容类型、海报和简介。

## 管理界面布局

管理界面沿用 UniDL 的四标签页布局（按 `1`–`4` 或鼠标点击切换），状态栏包含只读摘要胶囊，底部提供随上下文切换的操作按钮：

| 标签页 | 快捷键 | 用途 |
|---|---|---|
| **定时任务（Scheduled）** | `1` | 管理定时任务（新建任务、编辑、立即运行、暂停 / 启用、删除），右侧提供详情与海报检查器。 |
| **即将上映雷达（Upcoming radar）** | `2` | 按日期分组浏览即将上线的流媒体电影、剧集和番剧（预约所选作品、刷新、仅已安装服务、实时筛选框）。 |
| **执行历史（History）** | `3` | 查看最近的定时执行记录、阅读脱敏日志（`Enter` / 查看日志、`ctrl+y` 复制）、重跑任务或清空历史。 |
| **设置（Settings）** | `4` | 在 UniDL 标准的双列设置列表中配置系统后台托管、即将上映数据源、TMDB API 密钥、即将上映雷达地区、仅显示已安装服务以及海报预览。 |

除底部可点击的操作按钮外，快捷键同样可用：`n` 新建任务，`enter` 编辑 / 预约 / 打开日志或设置，`r` 立即运行或刷新雷达，`space` 启用/暂停任务，`d` 删除任务，`o` 切换仅显示已安装服务，`p` 开关海报检查器，`c` 清空历史。

## 创建并测试定时任务

点击 **`+ New job`（新建任务）**（或按 `n`，或在即将上映雷达 / JustWatch 中选择作品）打开编辑器弹窗。

### 1. 服务与目标

- **服务**：任意已安装的服务。切换服务后会立即运行实时预检，显示该服务是否已安装、是否已登录以及是否有可用 CDM（`✓ Ready` / `⚠ Sign in required` / `✗`）。
- **查找方式**：
  - **直接链接 / ID（Direct URL / ID）**：直接将 URL 或内容标识符交给服务的 `open_url` 流程处理。
  - **服务内搜索（In-service search）**：运行服务自带的 `search` 流程，并采用**保守消歧策略**（结合标准化标题、可选上映年份和内容类型匹配）。如果仍有多个候选难以区分，UniDL 会拒绝猜测，并将任务标记为 `needs_review`，避免下错作品。
- **显示名称（Display name）**、**内容类型（Media type）**（自动 / 电影 / 剧集）和**年份（Year）**有助于保持队列清晰并缩小搜索匹配范围。

### 2. 内容与选集

- **电影 / 单条内容（Movie / single）**：解析并下载单个标题。
- **指定集数（Specific episodes）**：在**集数表达式（Episode expression）**中使用 UniDL 的范围语法（例如 `S01E06`、`S01E01-E05`、`S01E01,S01E03` 或 `all`）。
- **追更后续新集（Watch new episodes）**：配合 `Repeat = Watch ongoing`（持续追更）使用。UniDL 会将每一集成功下载的记录写入任务的 `completed_episodes` 台账，后续运行时自动跳过已下载集数。

指定集数时，只把实际成功交付的剧集记为完成。服务列出两集但只返回一集，另一集仍待处理；重试时会跳过已全部完成的季。单次任务如果在最后一集记账后异常退出，恢复时会直接标记完成。编辑窗口打开期间即使任务已经结束，只改名称也会保留最新执行状态。

### 3. 执行时间、重试窗口与动作

- **开始时间（Start time）**：支持本地挂钟时间（`YYYY-MM-DD HH:MM`）、完整 ISO UTC 时间戳（`2026-12-04T05:00:00Z`）、纯日期字符串（`2026-12-04`，若已知平台上线时区偏移会自动顺延）或相对偏移（`+30m`、`+2h`）。提供快捷预设按钮（`+1h`、今晚 20:00、明早 09:00）一键填充。
- **重复模式（Repeat）**：仅一次（`Once`）、每天（`Daily`）、每周（`Weekly`）或持续追更（`Watch ongoing`）。
- **未上线重试窗口（Release retry window）**：不重试（`No retries`）、`6`、`12`、`24` 或 `48` 小时。如果到达触发时间时目标作品或目标集数尚未在平台上线，任务会进入 `waiting_release` 状态，并在窗口期内自动重试，而不是直接失败。
- **解析后动作（After resolving）**：下载（`Download`）、导出便携 JSON（`Export portable JSON`）或仅保存命令（`Save command only`）。
  导出 JSON 遵循该服务的主清单 / 媒体清单设置，与手动导出一致。
- **测试查找（Dry-run 试跑）**：执行服务查找与选集匹配，但不会下载媒体文件，也不会改动已保存的任务状态；用于在上线时间到达前确认查询条件和集数表达式是否符合预期。

## 任务状态

| 状态 | 含义 |
|---|---|
| `scheduled` | 已启用，正等待下一次触发时间。 |
| `waiting_release` | 目标作品或集数尚未发布；正在配置的未上线重试窗口内自动重试。 |
| `queued` | 到期时已有前台下载或服务会话正在运行；当前台会话结束后会自动开始执行。 |
| `running` | 正在执行中。 |
| `done` | 已成功完成（单次任务就此结束；周期或追更任务会自动推进至下一次触发时间）。 |
| `paused` | 已被用户临时暂停。 |
| `needs_auth` | 服务登录会话缺失或已过期；请在 TUI 中重新登录后重跑。 |
| `needs_review` | 保守搜索匹配发现了多个相近候选；请补充目标信息或年份，或改用直接链接模式。 |
| `failed` | 遇到不可恢复的错误，或未上线重试窗口已耗尽；请查看执行历史日志排查详情。 |

## 即将上映雷达与数据源

**即将上映雷达**标签页最多可聚合四个可配置数据源，汇总即将上线的流媒体电影、剧集和番剧：

| 数据源 | 默认状态 | 特点 | 是否需要 API 密钥 |
|---|---|---|---|
| **JustWatch** | 已启用 | 覆盖各地区流媒体片单上线信息及平台到服务的映射。 | 否 |
| **TVMaze** | 已启用 | 提供剧集精确的 UTC 播出时刻（`SxxExx`）。 | 否 |
| **AniList** | 已启用 | 提供当季番剧精确的播出时间戳。 | 否 |
| **TMDB** | 可选 | 提供覆盖面广的即将上映电影与剧集日历及高清海报。 | 是（v3 密钥或 v4 令牌） |

同时启用多个数据源时，UniDL 会并发拉取并合并同一部作品的信息（例如把 JustWatch 的平台/服务映射和海报，与 TVMaze 的精确 UTC 播出时间和集数编号合并到一起）。

可在**定时下载 → 设置 → 即将上映数据源**（或**设置 → JustWatch → 即将上映数据源**）中配置：

- 按 `space` 勾选/取消数据源（**多选**）。
- 按 `o` 仅选中当前高亮的数据源（**单选**）。
- 若尚未保存密钥就选择 **TMDB**，会立即弹出带掩码的 **TMDB API 密钥**输入框。同时支持 TMDB v3 API Key 和 v4 Read Access Token。密钥以 `0600` 权限私密保存在 `settings.json` 中，并在日志、错误信息与就绪诊断报告中自动掩码/脱敏。

## 海报预览与终端兼容性

右侧检查器可在**定时下载**、**即将上映雷达**、**JustWatch 搜索**和**JustWatch 上架情况**页面中，在结构化元数据与简介上方渲染 2:3 比例的海报。

全局设置 `poster_preview`（可在**设置 → 界面与诊断 → 海报预览**、**定时下载 → 设置 → 海报预览**中修改，或在各页面按 `p` 临时切换）支持三种模式：

| 模式 | 行为 |
|---|---|
| `auto` *（默认）* | **仅在**支持 Kitty Graphics Protocol（Ghostty、Kitty）或 Sixel（iTerm2、WezTerm）的终端，且窗口尺寸至少为 `104×20` 时显示原生高清海报。在 macOS 原生 `Terminal.app`（`Apple_Terminal`）、基础终端、`dumb` 终端以及窄窗口下自动隐藏海报面板，**零 HTTP 请求**。 |
| `always` | 终端支持时使用原生图形协议；在 macOS `Terminal.app` 上回退为纯色单元格渲染（避免半块字符出现发丝细缝），在其他真彩色终端上回退为半高字符块。窗口小于 `104×20` 或 `dumb` 终端下仍会自动折叠。 |
| `off` | 始终隐藏海报，且永不发起海报图片请求。 |

海报请求采用 150 ms 高亮防抖、有界内存 LRU 缓存以及 `<paths.cache>/posters/` 下的 `0600` 私有磁盘缓存，不附带任何服务的 `Cookie` 或 `Authorization` 请求头，并在界面挂起、切换标签、隐藏和卸载时主动清理原生 Kitty/Sixel 图层，防止弹窗上残留画面重影。

## 前台冲突与系统后台托管

### UniDL 运行期间（应用内调度器）

TUI 每 30 秒轮询一次到期任务。如果当前已有前台下载或服务会话正在运行，到期任务**不会**被丢弃，也不会叠加在前台会话之上并发抢占资源——它们会自动转为 `queued` 状态，待当前前台会话结束后立即接管执行。

### UniDL 关闭后（系统后台托管调度器）

可在**定时下载 → 设置 → 后台系统托管**（或通过 CLI）安装系统原生调度器，即使退出 TUI 后，到期任务也能按时自动执行：

- **macOS**：在 `~/Library/LaunchAgents/com.unidl.scheduler.plist` 安装 `launchd` LaunchAgent plist（默认每 5 分钟检查一次，可选 5 / 10 / 15 / 30 分钟）。
- **Linux**：在 `~/.config/systemd/user/` 下安装 `unidl-scheduler.service` 和 `unidl-scheduler.timer`。
- **Windows**：通过 `schtasks` 注册任务计划程序作业（`UniDL-Scheduler`）。

跨进程文件锁（`<paths.home>/schedules.lock`）确保 TUI、CLI 和系统后台托管进程绝不会同时重复执行同一个到期任务。

### 无人值守的认证边界

定时任务以无头（headless）方式执行，**绝不会**阻塞等待交互式登录提示、设备授权码或密码输入。如果服务的缓存会话或 Cookie 已过期，任务会立即停止并标记为 `needs_auth`，等待你回到 TUI 重新登录后再手动重跑。

## CLI 命令

所有定时调度操作都可通过命令行执行，与 TUI 共用同一套任务存储与锁文件：

```console
# List all scheduled jobs and their next trigger time
unidl schedule list

# Run all currently due jobs once (respects the cross-process lock)
unidl schedule run

# Test lookup and episode matching for one job without downloading
unidl schedule run --id <job-id> --dry-run

# Force-run a specific job immediately even if its run_at is in the future
unidl schedule run --id <job-id> --force

# Run a foreground polling loop (useful in containers or tmux/screen)
unidl schedule daemon --interval 60

# Inspect, install/update, or remove the OS background host scheduler
unidl schedule host status
unidl schedule host install --interval 5
unidl schedule host uninstall
```

如果使用非默认配置文件，请将 `--config /path/to/unidl.yaml` 放在 `schedule` 子命令之前；`host install` 会将该精确的配置文件路径与 Python 可执行文件路径写入生成的 `launchd` / `systemd` / `schtasks` 配置中。

## 存储与隐私

| 路径 | 用途 | 权限 |
|---|---|---|
| `<paths.home>/schedules.json` | 保存定时任务、追更台账与执行历史（原子写入）。 | `0600` |
| `<paths.home>/schedules.lock` | 跨进程执行锁，防止任务重叠运行。 | 私有 |
| `<paths.home>/settings.json` | 持久化保存 `upcoming_sources`、`tmdb_api_key`、`poster_preview` 与地区设置。 | `0600` |
| `<paths.cache>/posters/` | 海报预览的有界缩略图缓存（保留 7 天 / 上限 25 MB）。 | `0600` |

存入执行历史及通过 `ctrl+y` 复制的日志会自动脱敏，避免 Bearer Token、Cookie 和 API 密钥写入磁盘或剪贴板。
