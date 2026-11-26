# 故障排查

[English](../troubleshooting.md) | [简体中文](troubleshooting.md)

排查问题前请先打开 `debug` 模式（按 `s` → 界面与诊断 → 调试模式）。开启后，UniDL 会在 `paths.logs` 下生成带时间戳的会话日志和独立的单任务日志，保留临时文件，并记录解析后的完整 UniDL 命令。签名 manifest URL 会完整保留，便于复现短时效 manifest 问题。API 请求日志包含脱敏后的请求头、响应状态码/响应头及有界响应正文预览。账号密码、Cookie、Bearer Token 等凭据均会自动脱敏。内容密钥会保持可见以便排查密钥问题；许可证质询（challenge）与响应体仅在许可证请求失败时才会记录。

## 导入并注册服务包

服务包可以复制到可写的 `<paths.home>/services` 目录、当前运行包的 `unidl/services` 目录，或源码仓库的 `src/unidl/services` 目录。随后打开**设置 → 服务 → 注册服务**并选中它；已注册的包会呈灰显状态，未注册的可用包则可选中。确认后请重启 UniDL。必须重启是因为服务代码在启动阶段加载，持久化后的注册选择会在新进程构建主页与全局搜索时生效。单文件服务模块与包目录格式均受支持。

在未内置任何服务包的精简发行版中，主页会直接提示服务目录路径及上述设置入口。此时可能同时出现 CDM 警告——两者相互独立，CDM 仅在服务需要请求 DRM 密钥时才会用到。

如果该服务包属于**纯编译（compiled-only）**形态，就绪检查中可能会出现蓝色增强提示，例如 `Python ABI cpython-312 is not supported` 或 `platform windows-x86_64 is not supported`。这说明该服务包是为不同的 Python ABI 或操作系统构建的，请安装对应平台的原生构建包。UniDL 刻意不会回退到源码加载（包内也可能根本不含源码），同时保证应用其余部分正常运行。manifest 字段与构建矩阵请参阅[纯编译服务](compiled-services.md)。

## 主界面只显示原生服务

主界面仅显示既已成功加载、又经用户注册的服务。如需查看当前运行时已加载的完整注册表，可运行 `unidl services`——这是一份诊断列表，可能包含仅在当前进程中加载的包。TUI 主页与全局搜索始终遵循已持久化的服务注册状态与主页可见性配置。

## 服务提示 "no credentials"

进入对应服务，查看登录行或账号身份栏（主页不设账号列）。如果详情中提到了具体的凭据槽位（例如 `slot 'us'`），请在 `unidl.yaml` 的 `credentials.<service>.<slot>` 下补全配置。具体字段名取决于该服务的读取定义，通常为 `username` 和 `password`，有时也可能是 Cookie 或 API 密钥。

## 进入服务被拒并提示 "needs Java runtime"

这说明服务声明的外部助手缺失。请安装对应组件，或在配置中显式指定路径：

```yaml
helpers:
  java: /path/to/java
```

共享配置中请使用可移植路径，或将 Java 加入系统 `PATH`；不要提交绑定某台机器的本地安装路径。

必需的外部助手会在尝试进入服务时进行校验，配置提示通知中会附带安装指引。详见 [external-helpers.md](external-helpers.md)。

## 提示 "No CDM device configured" 或设备 DRM 系统不匹配

点击主页上的 `cdm` 标签为当前 DRM 系统选择设备，或在配置中的 `cdm.devices` 下添加设备并设置 `cdm.default`。校验已配置的本地设备文件：

```bash
unidl cdm --check
```

输出 `missing` 表示文件路径有误；`unusable (...)` 表示对应扩展名的实现无法加载该文件。Widevine 会报告安全级别与 System ID；PlayReady 在底层库支持时会报告安全级别；MonaLisa 会同时校验 `.mld` 文件及其引用的 wasm 模块。

如果报错指出所选文件属于其他 DRM 系统，请按 Widevine、PlayReady 或 MonaLisa 对应选择 `.wvd`、`.prd` 或 `.mld` 文件。远程 CDM 同样可在选择器中按名称选用，但它不会出现在上述诊断命令的输出中，因为该命令只检查本地配置的文件。

## "License server returned 403" / 无返回密钥

按常见程度从高到低排查：

1. **登录态过期。** 在该服务的菜单中退出登录，或删除 `paths.tokens` 下对应的会话文件后重新登录。该文件是会话读取的唯一来源，无需再到别处寻找第二份副本。
2. **CDM 安全级别不匹配。** 4K 内容通常需要 L1 设备。请将 `cdm.by_service.<service>` 指向一个 L1 设备条目。
3. **地区限制。** 当前账号或出口 IP 没有该作品的播放权益。请检查该服务自身的地区设置以及 `proxies` 配置。
4. **服务方变更了 API。** 开启 `debug` 模式后，日志会记录许可证请求 URL 以及响应体预览。

## 获取到了密钥但文件无法播放

这说明拿到的密钥是错的，而不是没拿到密钥。不要轻信 “Decrypted” 提示，应独立校验解码是否正常：

```bash
ffprobe -v error -show_entries stream=codec_name,codec_type,width,height \
  -of csv=p=0 "output.mkv"
ffmpeg -v error -i "output.mkv" -t 5 -f null -
```

第二条命令无报错地完成解码才是真正有效的证明。如果报告花屏或损坏帧，说明密钥与该轨道不匹配。

## "best" 画质选中了异常轨道

几乎总是因为缩略图或快进预览轨（trick-play / thumbnail ladder）靠更高的高度值胜出——曾有真实案例是一个 `1280x1440` 的缩略图轨压过了真正的 `1920x1080` 正片。轨道设置中的 `drop_video` 使用带词边界限制的 `trick`、`trickplay`、`thumbnail` 和 `image` 规则来过滤这类轨道，同时避免误伤标题里碰巧包含这些字母的正常作品（如 `Strickland`）。如果某个服务用了其他命名，可扩展该正则表达式。

## 选轨器为空或缺少轨道

- 为保证速度，HLS 主 manifest 默认采用浅层解析。原生交付核心的 `--details` 等效能力目前尚未作为设置项暴露，但某些 manifest 可能需要它。
- `drop_video` 正则可能过滤得过严，请检查匹配规则。
- 部分 CDN 要求把主 manifest 的查询参数透传到子播放列表。原生交付核心会对其识别的常见主机自动执行透传。

## 下载临近结束时卡住

这通常是高并发触发了 CDN 限流。请在轨道设置中调低 `workers`。`paths.temp` 下的分段缓存会自动复用，因此重新下载同一作品和轨道时会续传而非重下——涵盖已完成的 HLS/DASH 分段，以及源站支持 HTTP Range 时的直链文件前缀。请保持 `resume_parts` 开启。UniDL 还会对轮换的签名 URL 参数进行归一化处理，当刷新后的 manifest 分段列表略有变化时也能定位到先前的缓存目录；而更改标题、轨道、输出路径或服务身份时，则会刻意避免误用其他任务的分段缓存。

按下 `Ctrl+C` 后，原生运行时会在 TUI 退出前主动关闭活跃套接字与下载器子进程。该中止会按取消处理（而非抛出错误堆栈），已完成的分段仍安全保留在磁盘上供下次续传。仅当前正在传输中的单个网络分块可能丢失，下次尝试会从最后一个安全的临时边界继续。

## 按 Esc 无法退出

这是刻意设计的——需要在 1.5 秒内连按两次，防止误触按键意外中断进行中的任务。按第一次后会弹出提示说明。

## 输入文字时单字母快捷键无效

同样是刻意设计的。当文本输入框获得焦点时，所有可打印按键都会输入到框内，因此 `s`、`d`、`b` 会作为字符键入而非触发快捷键。将焦点移出输入框，或直接点击顶部栏中的按钮即可。

## 定时任务停在 `needs_auth` 或 `needs_review`

- **`needs_auth`**：定时任务以无头模式运行，绝不会阻塞等待交互式登录提示。请在 TUI 中打开该服务重新登录，然后点击**立即运行（Run now）**（或执行 `unidl schedule run --id <job-id> --force`）。
- **`needs_review`**：服务内搜索找到了多个相近候选，程序拒绝盲猜。请编辑该任务补充上映年份，或将**查找方式**改为**直接链接 / ID**，然后用**测试查找（Dry-run 试跑）**或 `unidl schedule run --id <job-id> --dry-run` 验证匹配结果。
- 如需检查系统后台托管是否生效，请打开**定时下载 → 设置 → 后台系统托管**，或运行 `unidl schedule host status`。

## macOS Terminal.app 中不显示海报

这是刻意设计的。macOS 自带的 `Terminal.app` 不支持 Kitty Graphics Protocol 或 Sixel，因此 `poster_preview = auto` 会自动隐藏海报面板且不发任何图片请求。如需原生高清海报，请改用 Ghostty、Kitty、iTerm2 或 WezTerm；或者将**海报预览**切换为**始终显示（Always）**（或在当前页面按 `p`）以启用纯色单元格预览。

## 各类文件存放位置

| 类别 | 路径 |
|---|---|
| 配置文件 | 源码检出目录根下的 `unidl.yaml`、打包安装时当前目录下的 `unidl.yaml`，或显式传入的 `--config PATH` |
| 交互设置 | `<paths.home>/settings.json` |
| 定时任务 | `<paths.home>/schedules.json`（及锁文件 `<paths.home>/schedules.lock`） |
| 登录令牌 | `<paths.tokens>/<service>/` |
| 密钥库 | `paths.keys_db` |
| 导出命令 | `<paths.commands>/<service>/` |
| 任务日志 | `paths.logs` |
| 分段缓存 | `paths.temp` |
| 海报缓存 | `<paths.cache>/posters/` |
| 下载成品 | 实际生效的下载目录（可在就绪检查中查看），按服务分目录存放。随包默认配置指向相对路径 `downloads/`。 |

## 在界面外复现问题

在干净的终端中使用相同服务和配置复现故障，然后运行项目自检：

```bash
python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
```

## 提交 Bug 反馈

提交时请一并附上：涉及的服务名、`debug` 日志输出、`unidl cdm --check` 结果，以及 `ffmpeg -f null -` 能否正常解码输出文件。如果没有解码校验结果，一句“不能用”根本无法区分是程序缺陷还是密钥错误。
