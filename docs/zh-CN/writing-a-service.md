# 编写服务

[English](../writing-a-service.md) | [简体中文](writing-a-service.md)

## Dolby Vision 混合输出

全局的 `dolby_vision_hybrid` 设置是一项输出转换，而非服务 API 或 profile 选择器。服务可以继承该设置，也可以在自身的策略设置中声明同名布尔项以覆盖它。在点播（VOD）场景下开启后，Core 会在许可列表中同时保留选中的 DV 视频及其匹配的 HDR10/HDR10+ HEVC 基础层；原生下载器会在解密后通过 `dovi_tool` 将两者合成。来源既可以是同时包含两层的单个 manifest，也可以是经 Core 合并的多个经服务授权的 profile；多个 profile 并非前提条件。不要为这项转换另行实现第二套许可客户端，也不要在直播/回看（DVR）码率阶梯中启用它。如果两份文件的帧率、时长或帧数不一致，下载器必须明确报错，而不是产出对齐错误的视频流。DV 源刻意选取可用的最低分辨率 DV 层——它不必与 HDR 基础层分辨率一致，因为合成时仅使用其 RPU 元数据。

建议结合以下三处源码一起阅读：

- `src/unidl/services/example/` — 参考服务。包含全部钩子与声明，并逐一附有注释说明它们存在的理由，以及缺失后会导致什么问题。它未注册，因此永远不会出现在平台列表中。直接复制该目录、重命名，再删掉不需要的部分即可。服务测试与注册表校验会确保它与公共 API 保持同步。
- `src/unidl/services/bbciplayer/` — 公开的原生服务，包含可选外部辅助工具（helper）声明与 Widevine 播放实现。
- `src/unidl/services/example/` — 刻意保持不完整的脚手架；它未注册，也永远不会出现在平台列表中。

## 从模板开始

对于已安装的构建版本，可在 **设置 → 文件与命名 → 高级路径 → 服务目录** 中找到它实际使用的服务目录位置。点击该行或按 Enter 即可打开；不要默认认为命令会使用附近源码检出目录中的服务。将服务包复制到该目录后，在 **设置 → 服务 → 注册服务** 中完成注册并重启 UniDL。

### 可移植的字幕与 profile 元数据

使用 `Playback.requested`、`returned` 和 `note` 填写公开的源 profile/类型标签。Core 会按每个成功解析的 manifest 变体保留这些字段；切勿在其中存放播放信封、客户端对象或会话状态。JSON 轨道源除了直接 URL 外，也应保留服务商的 `profile` 和 `kind`。

使用 `Playback.subtitle_references` 声明独立字幕，并照常通过 `augment_tracks()` 暴露给轨道选择流程。`SubtitleReference` 还可额外声明 `format`（例如 `vtt`、`ttml` 或 `srt`）以及字幕专用的 `headers`；通用请求头仍保留在 `Playback` 上。`format` 应填写实际返回的字幕格式，而不是后续经转换钩子产出的格式。Core 会从成功解析的 manifest 变体中收集这些引用，在可移植导出与生成的合并 JSON 源中保留完整列表，并在执行已安装服务的外挂钩子时将其还原。通用导入无需原服务即可下载普通字幕 URL；若某字幕的交付依赖特定服务商的定制处理，则仍需要该服务的实现。

    cp -r src/unidl/services/example src/unidl/services/mysvc

随后修改 `ID`、`NAME`、`TAG`，并删除不需要实现的钩子。随安装包内置发布的服务可以使用代码层的 `@registry.register` 装饰器；用户自行导入的服务包则可以省略 `registry`，改用 **设置 → 服务 → 注册服务** 进行注册。加载器会在用户重启 UniDL 后自动发现具体的 `Service` 子类，不再需要手动在 `src/unidl/services/__init__.py` 中添加导入语句。

## 唯一硬性规则

服务代码不得导入 UI 框架，不得调用 `print()`，也不得调用 `input()`。它只通过 `yield` 发出交互请求（*ask*）并接收答复。作为回报，你的服务无需改动即可同时运行在交互界面与无头（headless）模式下，可以通过传入脚本化答复来做单元测试，也永远不必手写菜单循环。

## 最小可用服务

```python
from collections.abc import Iterator

from ...core.flow import Ask, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import Service, registry
from ...core.titles import Title, TitleKind


@registry.register
class Example(Service):
    ID = "example"                       # stable, lowercase, used everywhere
    NAME = "Example TV"                  # shown in the interface
    ALIASES = ("ex", "exampletv")         # accepted by search and the palette
    TITLE_RE = r"example\.com"            # a pasted URL routes here
    GEOFENCE = ("US",)

    SUPPORTS_URL = True
    SUPPORTS_SEARCH = False
    SUPPORTS_LIVE = False
    SUPPORTS_LIBRARY = False

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        info = self.api.title(target)
        title = Title(id=info["id"], kind=TitleKind.MOVIE, name=info["name"],
                      year=info["year"], service=self.ID)
        yield ctx.emit(Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=info["manifest"],
            headers={"User-Agent": USER_AGENT},
            drm=DrmInfo(license_url=info["license"], headers={"authorization": info["token"]}),
        ))
```

必须导入 `Service`，因为服务类需要继承 UniDL 的服务契约。导入 `registry` 仅在使用可选的代码层装饰器时才需要；用户层面的 TUI 注册路径会在重启后自动执行相同的运行时注册。

对于单集内容，如果服务能够获取所属剧集的首播/发行年份，请设置 `Title.series_year`。`Title.year` 可以继续保留该集自身的发布年份：文件名模板会继续使用 `Title.year`，而包含 `{year}` 的剧集目录模板则会优先使用 `series_year`，确保跨自然年播出的同一季留在同一个根目录下。无法提供剧集级别年份的服务可将 `series_year` 留空，Core 会自动回退到单集年份。

到这里就是一个完整的服务了。你没有写菜单、登录提示、轨道选择器、CDM 调用、文件名拼接，也没有写任何下载命令。

## 调试日志策略

全局 **调试模式（Debug mode）** 默认会记录服务会话与 API 请求诊断信息，以便复现失败流程。如果服务作者发布的是编译后的代码，或者因其他原因不能暴露这些请求细节，可以在服务的 `__init__.py` 中声明退出记录：

```python
class Example(Service):
    DEBUG_LOGGING = False
```

该声明刻意采用显式退出（opt-out）设计：省略该属性或保持为 `True` 时，都会正常生成调试文件。当用户对已声明退出的服务开启调试模式时，UniDL 仍会保留普通的 TUI 状态和错误消息，但不会创建会话/API 诊断文件、下载器调试元数据或保留的调试临时文件集。TUI 日志中会注明该服务在本次运行中拒绝了文件采集。

## 文件布局

每个服务一个独立包：

```
services/example/
  __init__.py      the Service subclass and its flows
  api.py           HTTP client and response parsing, no UI, no printing
```

将 API 客户端独立出来很重要：它是你对接线上服务时主要调试的部分，而且应当能在临时脚本里直接单独运行。

## 入口点

覆写你的服务所支持的方法，并设置对应的 `SUPPORTS_*` 标志。基类的 `home()` 会根据这些标志自动组装顶层菜单，所以通常根本不需要自己写 `home()`。

| 方法 | 触发时机 |
|------|----------|
| `open_url(ctx, target)` | 提供了 URL 或内容 ID |
| `search(ctx, query)` | 自由文本搜索 |
| `live(ctx)` | 直播频道或节目单（EPG） |
| `library(ctx)` | 账号自己的片单/收藏列表 |
| `home(ctx)` | 仅当默认菜单确实不适用时 |

## 交互请求（Asks）

全部通过 `ctx` 构造：

```python
choice   = yield ctx.pick("Season", choices)                    # single
choices_ = yield ctx.pick("Episodes", choices, multi=True)      # multi
text     = yield ctx.text("URL or id", placeholder="https://…")
yes      = yield ctx.confirm("Include extras?", default=False)
row      = yield ctx.table("Live TV", columns, rows, values)    # grids, EPG
token    = yield ctx.wait_for(                                  # something done elsewhere
               "Confirm sign-in",
               [("Code", "ABCD-1234"), ("Open", "https://…"), "and nothing to type here"],
               poll,
               qr=QrPresentation(payload="https://…"))          # optional QR in the TUI
yield ctx.emit(playback)                                        # a result
yield ctx.settings_request()                                    # open settings
result   = yield ctx.suspend(callable)                           # release the terminal
```

`Choice(label, value, detail="", tags=(), disabled=False, navigates=False)` — `detail` 会渲染为较暗的第二行，`tags` 渲染为行内暗色标签。`navigates=True` 用于标记 **下一页（Next page）** 这类翻页条目：它们只用来切换列表页，在驱动器根据多选结果推断批量总数时会被排除。

多选支持传入 `preselected=[0, 3]` 以预先勾选指定项。轨道选择器打开时的自动预选就是这样实现的。

`ctx.wait_for()` 会展示传入的提示行，并每隔 `interval` 秒在流程工作线程中调用一次 `poll`，直到它返回非 `None` 值。超时或按返回（Back）都会抛出 `Back` 异常。凡是需要在其他屏幕上完成确认的操作，都应使用 `ctx.wait_for()`；只有当另一个进程必须临时接管终端时，才使用 `ctx.suspend()`。

对于扫码登录（scan-to-sign-in）验证，请从 `unidl.core.qr` 传入一个 `QrPresentation`。如果服务返回的是二维码所编码的文本/URL，请使用 `payload=`，展示层会在本地生成清晰的点阵位图。只有当服务返回的是字节流或 base64 `data:image/...` URI 格式的图片时，才使用 `image_data=`。`image_url=` 可以指定服务商返回的官方公开 HTTPS 二维码位图地址。只要提供了官方图片，它就具有最高权威性：即便图片拉取或渲染失败，展示层也绝不会用根据 `payload=` 生成的二维码来替代它。因为官方图片中编码的可能是与普通浏览器激活页不同的会话 URL。对于受保护的图片，`image_data=` 仍是首选方案：在构造 `QrPresentation` 之前，应先通过服务自身的 API 会话将其下载下来，避免 TUI 绕过服务的 Cookie、请求头或代理。`fallback_url=` 则是终端无法显示图片时供打开的普通 HTTP(S) 地址。较早的服务适配器可能会把命名明确的二维码图片端点放进 `fallback_url=`，公共二维码层会出于兼容性识别这类端点；`payload=` 中命名明确的图片 URL 与图片 data URI 同样会按图片处理。

支持图像渲染的终端会直接显示服务商位图。其他终端则会保留其模块网格（包括反色/浅底深色或深底浅色二维码），并带有四模块宽的静区（quiet zone）。用户也可以通过 **打开二维码图片（Open QR image）** 查看原图。如果终端宽度不足以完整容纳网格，界面会用提示替代被裁剪的二维码，提醒用户拉宽窗口或直接打开图片。任何登录载荷都不会写入日志，且临时预览会在登录提示关闭后立即清理。

```python
challenge = client.start_qr_login()
session = yield ctx.wait_for(
    "Scan to sign in",
    [("Open in a browser", challenge.url), "Confirm on your phone."],
    poll=lambda: client.poll_qr_login(challenge),
    qr=QrPresentation(
        payload=challenge.url,
        image_url=challenge.image_url,
        fallback_url=challenge.url,
        alt="Example TV login QR",
    ),
)
```

切勿将二维码图片或 base64 data URI 放入 `lines` 或 `ctx.log()`。手动传入的文本行会被复制并按文本记录，而专用的二维码字段仅在内存中渲染，等待结束后即销毁。受保护的图片必须在构造展示对象前通过服务自身的 API 会话拉取——TUI 绝不能绕过服务的代理、Cookie 或请求头。

### 需要用户手动完成的步骤

这不仅适用于设备激活码。凡是需要用户在别处动手完成的步骤——输入验证码、打开网页登录、在手机列表里选定一项——都写成 `(label, value)` 二元组，并用普通字符串补充周围的说明文案：

```python
yield ctx.wait_for(                       # confirmed elsewhere, polled for
    "Activate with your TV provider",
    [("Open", url), ("Code", code), "The page signs you in."],
    poll,
)
answer = yield ctx.text(                  # finished in a browser, pasted back
    "Sign in, then paste the address back",
    placeholder="https://…?code=…",
    lines=[("Open and sign in", url), "Do not press Continue; the address matters."],
)
```

这些二元组是 UI 优先呈现的核心：屏幕中央会出现一张带边框的卡片，每个值单独成行、加粗展示，带有编号以便按键直接复制，也可以点击复制，按 `o`（或在输入框旁按 `^o`）会打开其中的第一个链接。标签文案由你定义——复制提示会直接引用这些标签——所以请按用户接下来要对它执行的操作来命名。

不要把验证码或 URL 放进 `ctx.log()` 日志行，也不要拼进标题里。它们依然会记入日志以供回看，但日志是用来查看“已经发生过什么”的，不是用来呈现“应用正在等你做什么”的。

**你的客户端绝不能自己阻塞等待。** 上述规则最容易在无意间被破坏的情况是：`api.py` 接受一个 `show(code)` 回调，然后在内部自己循环等到激活码生效——这样一来，流程层没有东西可以 `yield`，激活码唯一能去的地方就只剩下日志了。之前有六个服务都是这么写的，它们在屏幕上的表现如出一辙：一片空白面板、一个埋在历史日志里的激活码，以及按下去十分钟毫无反应的返回键。

正确的做法是在接缝处把客户端一分为二：一个调用负责拿到激活码之前的所有逻辑，另一个调用只检查**一次**激活码是否已经生效。

```python
code = client.begin_provider_sign_in(provider)      # no waiting in here
session = yield ctx.wait_for(
    f"Sign in to {provider.label()}",
    [("Open", code.url), ("Code", code.code), "Nothing is typed here."],
    poll=lambda: client.finish_provider_sign_in(code, provider),   # one check
    timeout=max(60.0, float(code.seconds_left)),
)
```

驱动器会接管其余工作：在状态栏显示倒计时、将单次轮询失败视作“尚未完成”而非错误，以及把按返回键或超时统一转为 `Back`。如果临时调试脚本需要阻塞式调用，请用这两半去组装阻塞函数，而不要反过来封装。

### 进度与消息

以下调用属于旁路通道，不要对其 `yield`：

```python
ctx.status("Loading season 3")   # the crumb line, transient
ctx.log("found 12 episodes")     # the log pane
ctx.warn("no subtitles")
ctx.error("playback denied")
```

### 返回（Back）

`Back` 表示回到紧邻的上一页，而不是直接跳回服务首页。下载成功或失败、保存命令及导出完成后也适用同一规则。请保持每一层父级选择器/输入框处于活动状态，并通过 `yield from` 委派子流程，让子流程各自的 `Back` 处理器能够正常执行：

```python
def browse_seasons(ctx, seasons):
    page = ctx.pick("Season", seasons)
    while True:
        season = yield page
        try:
            yield from browse_episodes(ctx, season)
        except Back:
            continue                # child → season picker, not service home
        except RuntimeError as exc:
            ctx.problem("Could not load that season", str(exc))
            continue                # same parent after a recoverable failure
        if not ctx.interactive:
            return
```

在选季页面本身按返回时，`Back` 会向上冒泡到它真正的父级页面（如搜索结果页）。不要只在 `home()` 捕获深层子流程的 `Back`，那样会跳过中间的所有页面。共享的 `Service.home()` 只有在内层相关流程都有机会处理完 `Back` 之后才会接住它；只有当服务菜单确实是紧邻的上一页时，返回该菜单才是正确的行为。

共享的搜索计数器会同时透传答复与异常。既有的自定义 `home` 流程和导航覆写依然有效，不会多收到一次 `Back`。完整的返回契约、批量与错误行为、状态保留规则及核查清单，请参阅 [navigation.md](navigation.md)。

## 播放与 DRM

返回一个 `Playback` 对象。把 Core 需要的信息填入 `drm`；不要自己调用 CDM。

如果服务的播放 API 同时返回章节标记，可将 `unidl.core.Chapter` 对象附在 `Playback.chapters` 上；该字段可选，没有此类接口的服务无需编写空桩。时间单位使用毫秒（仅当 API 明确返回秒数时才使用 `Chapter.from_seconds`）。完整契约与展示细节见 [chapters.md](chapters.md)。通过 `self.fetch_chapters_enabled()` 遵守按服务配置的 `fetch_chapters` 策略（兼容旧版全局回退）：当其为 False 时，直接跳过该可选接口或相关查询字段。章节解析遵循尽力而为原则：捕获服务商超时、接口结构变动和格式异常的可选条目，在流程具备上下文时记一条警告日志，然后继续正常返回 `Playback`，确保不影响 DRM、下载与混流。章节内嵌已是共享的 **轨道与输出（Tracks and output）** 设置项 `embed_chapters`（默认开启）；服务不应再增加第二个混流开关。

可选的海报、缩略图和封面图通过 `Playback.attachments` 提供，填入 `Attachment(url, name, kind, mime_type, filename, headers)`。发起请求前请先检查 `self.fetch_attachments_enabled()`。UniDL 会在章节/歌词旁预览这些图片，并将它们单独下载到各片名专属的 `.attachments` 目录中；附件绝不会被混流进媒体文件。

```python
DrmInfo(
    system=None,                                 # None: service/global selection decides
    pssh=None,                                   # None: core tries MPD, init segment, then KID fallback
    wrm_header=None,                             # PlayReady: core tries MPD and init segment
    init_data=None,                              # registry-specific, e.g. MonaLisa ticket
    service_certificate=None,                    # Widevine privacy-mode certificate bytes
    license_url="https://…/getlicense",
    headers={"authorization": f"Bearer {session}"},
    context={"session": session},                # yours, passed back to get_license
    cdm=None,                                    # override the device for this playback
    hls_key=None, hls_iv=None, hls_method=None,  # HLS AES-128 instead of Widevine
    clear=False,                                 # True: this title is not encrypted
)
```

其中有两个字段表达的是意图而非数据，并且都采用失效安全（fail-safe）设计：

- 除非该服务或本次播放明确要求某个特定的已注册 DRM 系统，否则将 `system` 保持为 `None`。`None` 表示“无倾向”，由服务或全局设置决定。一旦指定了具体名称，它就会被固定下来，设置项将无法再覆盖它。
- 只要存在 `DrmInfo` 实例，就说明该流是加密的。如果你要为受保护片库里的**未加密**内容（比如加密片库中的一段明文花絮）附加 `DrmInfo`，必须显式设为 `clear=True`。Core 不会根据字段全空来推断“无 DRM”——因为有些服务是在 `get_license` 中自行构造许可 URL 的，它们的字段本来就会全部留空；若 Core 在这种情况下猜成“无 DRM”，就会在报告“不需要密钥”的同时把加密内容原样下载下来。

对于完全不含任何 DRM 的片源，不要附加 `DrmInfo`。

当一次授权明确返回多个等效的 manifest URL 时，将第一个写入 `manifest_url`，其余放入 `alternate_manifest_urls`。Core 只会按顺序尝试这些明确声明的候选地址，并将 `manifest_url` 更新为最终加载成功的那个。`manifest_attempts` 是可选的重试次数，仅适用于已确认原客户端会对瞬时 manifest 失败执行重试的平台，不可用作主机名或协议发现的回退手段。

声明该服务实际能够使用的 DRM 系统：

```python
DRM_SYSTEMS = ("widevine", "playready")  # service setting chooses between them
# DRM_SYSTEMS = ("monalisa",)             # one system is pinned, with no setting
```

空元组表示遵循应用全局的 DRM 选择。单个元素代表该服务的既定事实，会覆盖全局偏好。多个元素则会为该服务增加一个服务级的 `drm_system` 设置项，顺序即为该服务的首选顺序。

Core 不提供默认的许可 POST 实现。凡是需要联网请求许可的 DRM 服务，都必须自行实现传输方法，哪怕目标接口直接接收原始 challenge 也一样。Core 负责 CDM 加载、challenge 生成、许可解析与密钥提取；服务负责 URL、请求头、认证、请求体构造以及响应解包。两个基类钩子默认都以失败收尾（fail closed），避免尚未移植完成的服务静默继承另一套 HTTP 契约：

```python
def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
    response = self.http.post(
        drm.license_url,
        json={"payload": base64.b64encode(challenge).decode()},
        headers=drm.headers,
    )
    return base64.b64decode(response.json()["license"])
```

PlayReady 服务同样需要实现 `get_license_soap`。这两个方法都应保留在服务包内部（拆分到服务本地的 `drm.py` 中也可以）；绝不要调用 `super().get_license(...)` 或 `super().get_license_soap(...)`。

对于 Widevine 隐私模式，请在 `emit` 播放对象之前拉取服务证书，并将其解码后的字节写入 `service_certificate`。Core 会在生成 challenge 前将其应用到本地和远程 CDM。

### 声明自定义许可

在 Service 类上设置 `CUSTOM_LICENSE = True`，即可在 **许可方式（License action）** 中展示 **自定义许可（Custom）** 选项。不要依据文件名、服务 ID 或仅凭某个钩子的存在来推断此能力。无源码的编译包可以在其根目录的 `service.toml` 中显式设置 `custom_license = true`。

在自定义模式下，`prepare_drm(playback, tracks, log)` 会在查询密钥库之前选定由服务主导的许可方案；`resolve_keys` 则可用于编排服务专属的许可交换。请填充实际的 `drm.pssh` / `pssh_values` 或 `wrm_header` / `wrm_headers`。只要其中能提取出 KID，这些 KID 就决定了自定义方案在密钥库中的覆盖范围；如果不透明初始化数据无法暴露 KID，请将 `license_track_kids` 显式设为实际计划覆盖的 KID。这两个钩子都不得借用共享输出偏好来修改服务商接口。仅当你的自定义流程必须等到最终轨道选择器完成之后才能运行时，才覆写 `custom_license_after_tracks(playback)` 返回 `True`；否则默认在选轨之前执行。自定义模式始终会收到完整轨道列表，并自行决定如何使用最终选轨结果（延迟执行时，该结果位于 `drm.context["license_final_tracks"]`）。

选择标准的“选轨前（Before）”或“选轨后（After）”时，会绕过显式声明的自定义规划器及自定义的 `resolve_keys` 覆写，但 Core 仍会调用你自己的 `get_license` / `get_license_soap` 传输实现。“选轨前”使用默认 Widevine 种子或完整的 PlayReady 轨道范围；“选轨后”仅针对 DASH/HLS/ISM 上最终选中的加密轨道请求许可。现有的未声明自定义许可的服务钩子仍保持兼容，但必须尊重传入的轨道列表；新编写的自定义方案则必须显式声明支持。MonaLisa 不受影响。

如果经验证的原客户端在选择许可种子时不依赖于最终要下载的轨道，请覆写 `prepare_drm(playback, tracks, log)`。它在密钥库/CDM 解析以及共享输出选择之前运行，接收完整的加密许可轨道列表。该钩子可以设置 `drm.pssh` 或对应已注册系统的等效初始化数据，但不能发起许可请求，也不能用输出偏好去改写 `license_tracks` / `license_track_kids`。DIRECTV 是参考范例：其服务设置默认使用旧版 720p 视频 profile，由精确的最高码率 1280x720 媒体播放列表提供单个 PSSH，即便用户最终选择了其他音视频组合，这唯一一次 Widevine 许可响应也会保留返回的全部内容密钥。

对于加密直播录制，只有当服务能够把新出现的 KID 精确映射到所选流/分片的初始化数据时，才覆写 `live_key_pssh(playback, stream, segment, kid, log)`。优先使用已绑定在触发分片上的初始化数据；对于滚动更新的 HLS 播放列表，这同时覆盖了密钥行已滚出当前播放列表的较早 DVR 窗口分片。Core 会依次尝试：先查密钥库，再调用此钩子并走该服务自身的许可传输方法，最后回退到带密码遮罩的 TUI `KEY` / `KID:KEY` 输入框。返回 `None` 表示放弃自动轮换。绝不要在这里凭空构造通用的纯 KID PSSH：服务必须自行证实该请求格式有效。Core 会保留并存储许可返回的所有内容密钥，但只向交付后端提供匹配当前暂停录制 KID 的那一组密钥对。已经存在于录制密钥集合中的 KID 不算自动轮换事件；针对该 KID 的校验失败会直接进入用户手动输入流程。

### 多个 PSSH 对象

默认的 PlayReady 移植应当把完整的 DASH/ISM manifest 交给 Core，而不是在服务层里只抽取第一个 PSSH。Core 会过滤 PlayReady 保护数据，对原始 PSSH 与等效的 WRM/KID 标识去重，为每个剩余标识独立执行一次许可交换，再合并全部密钥。多数服务恰好只产出单个密钥，但这个观察结果不是硬编码单个 PSSH 的理由。

启用 **最终选轨后获取许可（License after track selection）** 后，Core 会在收集 PlayReady 对象之前，先将 DASH 保护数据缩窄至最终选中的加密 representation/KID。未被选中的 MPD 头部——包括已残留在播放上下文中的过期头部——不得引入额外的许可请求或密钥库要求。如果无法将所选 representation 安全映射到保护数据，只检查它们的初始化分片，切勿静默回退到全 MPD 许可扫描。共享的多 KID WRM 头部会保持原样，但仅由选中的 KID 驱动补齐。这些规则对本地和远程 CDM 同等适用；关闭该选项则保持默认的完整轨道范围行为。

请保留经验证的服务专属行为，不要机械套用上述默认规则。如果 HLS 主播放列表省略了分轨 PSSH 数据，应改用该服务单独配置或默认的**许可轨道（licence tracks）**及其媒体播放列表，而不是扫描每一个码率版本。像 Apple 这样的移植服务，其原有的 PlayReady 与 Widevine 路径都受各自 `license_tracks` 控制，两个系统都必须保持这种分离。任何服务层许可辅助代码都不得读取共享输出设置；只有 Core 显式的选轨后兼容模式才可以提供仅含已选轨道的 DRM 列表。详见 [drm.md](drm.md)。

明文流：服务从不使用 DRM 时将 `drm` 保持为 `None`。在受保护片库内则使用 `DrmInfo(clear=True)`，让“无需许可”成为明确声明的事实，而不是残缺的加密播放信息。

## 设置

服务实际有哪些控制项就声明哪些，不存在固定集合。

```python
from ...core.settings import Option, Setting, multi_choice_setting

# Use this only when every value maps to a real, service-owned API request.
LADDERS = multi_choice_setting(
    "ladders",
    "Requested ladders",
    [("hd", "1080p"), ("uhd", "2160p")],
    default=("hd",),
)

SETTINGS = [
    Setting(
        key="profile",
        label="Stream profile",
        kind="choice",                       # choice | bool | text | int | multi
        options=[Option("hd", "1080p H.264"), Option("uhd", "4K Dolby Vision")],
        default="hd",
        help="4K needs a TV login and an L1 device.",
        resets_session=True,                 # changing it signs the user out
    ),
    LADDERS,
]
```

这些设置会自动显示在以你的服务命名的分组中。共享的轨道设置（画质、编码、动态范围、音频、字幕）会自动追加在后面——不要重复声明。关于某个配置项应放在哪个层级，请参阅 [settings.md](settings.md)。

通过 `self.settings.get("profile")` 读取配置值。

下面三项决策彼此独立，不要因为它们的取值看起来相像就把任意两项合二为一：

| 决策 | 配置归属 | 允许改变的内容 |
|------|----------|----------------|
| 服务商源 / manifest profile | 服务设置，例如 `manifest_*`、`source_*` 或 `profile_*` | API 请求载荷与返回的 manifest URL |
| 轨道输出选择 | 共享的 `video_*`、音频、字幕与 `track_mode` 设置 | 交给原生交付核心进行下载/混流的轨道 |
| 许可轨道 / profile / 种子 | 独立的服务设置，例如 `license_tracks`、`license_profile` 或 `pssh_video_profile` | 仅限初始化数据与服务自有的许可方案 |

共享的轨道输出选择设置**绝不是许可设置**。`get_keys`、`get_license`、`get_license_soap`、`prepare_drm` 以及服务本地的 DRM 辅助函数，都不得读取 `video_quality`、`video_codec`、`video_range`、音频或字幕输出偏好来决定要申请哪些许可。如果服务需要专门的许可轨道、profile 或 PSSH 种子，请在该服务自己的 `SETTINGS` 中单独声明一个设置项——哪怕它的选项和默认值与某个输出设置完全一致。

默认情况下，Core 在共享输出选择之前就会把解析出的完整加密轨道列表作为 `license_tracks` 交给服务。服务只能通过自身显式的 `license_*` / `pssh_*` 策略缩窄该列表。服务代码绝不能读取共享输出设置来决定其 API/profile 或许可种子。

轨道元数据应保留服务商原始的语言标签，并尽可能暴露规范化后的字段。`und` 代表未定义语言，不得靠猜测填充；诸如 `es-419` 这类标签既要保持可单独选中，也要能匹配主语言 `es`。音频编码、音频 profile（如 Atmos）、角色（role）、声道布局与字幕类型都是各自独立的事实。服务应根据 API 或 manifest 显式填入 Atmos/forced/SDH/CC 标记，而不是把它们揉进显示名称。Core 会将共享的语言、编码、profile、声道与字幕类型设置组合成硬性输出过滤条件，然后在余下候选中应用最佳/最差（Best/Worst）画质策略。如果过滤后结果为空，会交由 TUI 轨道选择器进行人工修正；服务代码绝不得静默替换成毫不相干的轨道。

按服务配置的 **最终选轨后获取许可（License after final track selection）** 兼容模式会改变 Core 传给同一批钩子的轨道列表：此时传入的是最终选中的加密轨道，而非完整码率阶梯。它专为 HLS/分媒体播放列表初始化数据的场景而设。服务无需针对全局开关写条件分支，也不要自己去读复选框设置——直接处理 Core 按当前模式传入的轨道列表即可。

无论在设置名称还是代码中，都要把这三个阶段彻底分开。服务级的 `manifest_*`、`source_*` 或 `profile_*` 设置可以在请求 MPD/播放列表前选定服务商的 manifest profile。共享的 `video_quality`、`video_codec` 和 `video_range` 设置必须在 manifest 解析完成后交给 Core 的轨道选择逻辑，绝不能被复用去悄悄切换不同的服务商 URL 或许可轨道。服务专属的 `license_*` / `pssh_*` 设置可以选择许可输入，但不能改写最终交给原生交付核心的 `TrackSet.selected`。

### 多个已授权 manifest，统一到一个选择器

当经验证的原客户端确实需要为每个分辨率/编码族单独发起一次播放请求时，服务可以提供多值的源 profile 设置。用 `multi_choice_setting` 声明该值，照常请求第一个 profile，设置 `Playback.merge_manifests=True`，并覆写：

```python
def manifest_variants(self, playback: Playback, log) -> list[Playback]:
    return [
        replace(playback, manifest_url=self.client.playback(profile),
                merge_manifests=False)
        for profile in self.remaining_profiles(playback)
    ]
```

返回的每个对象都必须已完成授权，并携带访问该 URL 所需的确切请求头、代理与服务状态。Core 既不会猜测 profile 名称，也不会改写 URL。它会独立解析每个变体，遇到不可用变体时记录日志并跳过，然后构建一份带类型的 JSON manifest，内含去重后的全部流。去重依据的是暴露给选择器的轨道属性（媒体类型、语言/角色、分辨率、编码、帧率、动态范围、声道、码率、容器和 KID），而不是签名 URL 或服务商的 representation ID——当每次 profile 请求都返回不同的限时签名 URL 时，这一点非常关键。直播播放必须保持为单一可刷新的 manifest，因此 Core 会刻意对 `Playback.is_live` 忽略合并。

命令生成与导出路径收到的同样是这份合并后的源；不要在服务内部自行拼接 `TrackSet` 对象，也不要从该钩子返回原始流对象。Disney 是参考实现。

### 为合并后的 profile 集合获取许可

合并后的码率阶梯只是展示与交付视图，并不会让主 profile 获得为其他 profile 所返回轨道签发许可的权限。Core 会为合并后的每一条流保留来源（origin），并按来源对最终的许可列表分组。因此，由服务实现的 `resolve_keys` 必须让每个 profile 的许可上下文、端点与会话各自独立，对每个需要的 profile/PSSH 只请求一次，并返回所得密钥的并集。当选择了 `license_action=after` 时，应将范围缩窄至最终选轨对应的来源和加密轨道，而不要因为前面请求过就去处理整个 profile 列表。在每次独立许可交换之间清理重复的 `playback.keys`/缓存密钥状态，防止前一个成功的 profile 抑制了下一个 profile 的请求。

对于暴露源选项的服务，`manifest_resolution`、`manifest_codec` 和 `manifest_color` 决定请求哪一族源，而共享轨道设置决定从所选 MPD 中挑选哪些 representation。编写测试时，应刻意为各适用阶段配置不同的值，并分别断言源 URL/profile、最终输出轨道以及许可输入，避免日后移植时又把它们重新合并成一个开关。DIRECTV 是三向分离的范例：manifest 授权返回码率阶梯，共享输出设置决定 unidl 下载什么，而 `pssh_video_profile` 独立决定用哪条视频媒体播放列表作为 Widevine 许可种子。

## 续期活跃的直播录制授权

在 `__init__.py` 的服务类上设置 `LIVE_REAUTHORIZATION = True`，并实现 `reauthorize_live(playback, request, log)`。省略该标志或保持为 `False` 将沿用现有行为。这是服务能力声明，而非 `unidl.yaml` 配置项。它不会用于 VOD、以 VOD 方式处理的直播或可移植导出导入，也不会改变初始许可流程或已在使用中的旧版 `live_key_pssh` 钩子。

- `LiveKeyRequest`：除非正在替换失效密钥，否则 Core 会优先查询已配置的密钥库。请使用当前活跃的播放会话以及请求中精确指定的轨道、分片和 KID。在可用时，`manifest_url` 和 `initialization` 会描述当前源和初始化字节范围，较小的内存初始化数据则放在 `initialization.init_data` 中。返回一组 `kid:key` 列表；Core 要求其中包含所请求的 KID，且不会自行推断 Widevine、PlayReady 或其他系统。如果 KID 未知、服务无法续期或返回值不匹配，仍会回退到手动输入密钥提示。
- `LiveSourceExpired`：直播 manifest 或某个分片返回了 HTTP 401/403。请使用现有已认证客户端按同一频道与同一播放 profile 重新授权。对于网络 HLS、DASH 或 ISM manifest，返回 `LiveSourceRenewal(manifest_urls=(...))`——这些 URL 是互为备选的地址，而不是需要合并的轨道。如果服务商 API 直接返回 manifest 正文，请传入 `source=DeliverySource.from_hls(...)`、`from_dash(...)` 或 `from_json(...)`，并用 `base_url` 提供相对 URL 所基于的 HTTP(S) 源站。JSON 可描述一组已授权 manifest，但解析出的轨道仍必须暴露可持续刷新的源以供后续窗口使用。仅当需要替换整套媒体请求头时才传入 `headers`；省略它则会保留现有活跃请求头。请保持新会话可用于后续密钥请求，并在正常播放销毁时释放它。

在续期过程中，Core 绝不会重启服务菜单、更改已选轨道或直接跳到直播最新边缘。它会校验每条已选轨道的 rendition 标识与连续游标。即便分片序号变化，HLS program date-time、DASH 呈现时间以及规范化的 ISM 时间也能证明连续性。在缺乏可比较时间信息时，Core 要求存在无歧义的重叠以及匹配的连续序号。不要假定两个 rendition 的分片序号会同步推进，也不要认为拿到新 URL 本身就足以证明流能够衔接。如果找不到同步对齐的批次，Core 会停止录制并保存已经录好的部分。在录制实际向前推进前，源续期最多只尝试一次。普通的超时、404 或 503 不会被视作授权过期。未暴露此类授权失败事件或可刷新原生轨道列表的自定义传输层，需要自行实现传输层集成。

该钩子仅限运行时使用：不得将账号、会话或回调序列化到导出文件中，不得假定独立下载环境持有已授权会话，也不得在日志中输出授权数据。当活跃会话无法应答时返回 `None`，使用可取消的网络 I/O，并保持原有的返回/停止行为。关于录制器的校验与回退机制，请参阅 [交付契约](downloader-integration.md)。

## 续期 VOD 下载授权

VOD 续期是一项独立能力：在 `__init__.py` 的服务类上设置 `VOD_REAUTHORIZATION = True`，并在初次授权流时设置 `Playback.vod_asset_id`。请使用能稳定标识实际内容资产与剪辑版本的服务商 ID，而不是展示标题、manifest URL 或会话令牌；同一作品的不同剪辑版本必须使用不同标识。如果服务无法凭借活跃账号重新授权同一版本，请不要设置该标志。

覆写 `reauthorize_playback(playback, request, log)` 以处理 `VodSourceExpired`。请求对象包含 HTTP 状态码、失败阶段、资产标识以及已选轨道描述符。请使用现有的已认证客户端，并针对同一个已授权资产/profile 续期。切勿在此回调中重启服务菜单、切换 rendition，或在日志里打印签名 URL 与密钥。如果访问被拒或无法续期，返回 `None`。

成功时回调返回 `PlaybackSourceRenewal`，包含实际响应对应的已验证 `asset_id`，以及 `manifest_urls=(...)` 或带 `base_url` 的类型化 `source`。这些 URL 是同一份轨道列表的备选源。可选的 `headers` 用于替换媒体请求头；若省略，仅在同源时继承当前活跃请求头，跨源目标则只会收到可移植的通用请求头。如果续期后的选轨引入了新的 KID，且尚无对应密钥可用，请一并在 `keys` 中返回相应的已授权 `kid:key` 对——Core 不会自行猜测许可交换流程。原有的结果类名 `LiveSourceRenewal` 保留为别名；仅支持直播续期的实现可以继续使用 `reauthorize_live`。

原生执行器会在恢复下载前校验新的轨道列表。它会保留最初的选择器选轨与输出设置，仅复用来源、字节范围、时间信息、初始化数据及加密指纹仍然匹配的已完成分片。若某项依赖发生变化或无法验证，默认仅令受影响的缓存失效，而不作废整个任务。自定义传输层需要暴露类型化的 HTTP 授权错误与兼容的原生轨道列表，才能使用这条自动续期路径。运行时限制与回退行为见 [VOD 源续期](downloader-integration.md)。

## 凭据

声明你的服务所需的登录槽位，`unidl.yaml` 会负责填充它们。

```python
from ...core.credentials import CredentialSlot

CREDENTIALS = [
    CredentialSlot("us", "Example US account"),
    CredentialSlot("intl", "Example international account"),
]
```

```python
cred = self.ctx.credential("us")
cred.username, cred.password, cred.cookies
```

字段名由你自己决定：`unidl.yaml` 会原样透传你在该槽位下填写的任意键，所以需要 `device_id` 或 `api_key` 的服务只需读取 `cred.get("device_id")` 即可。

### 来自另一服务的合作伙伴授权

当一个已安装服务为另一个服务获取短时效 SSO URL 时，请使用 Core 的 `PartnerAuthorization` 契约。生产方通过 `yield ctx.partner_handoff()` 移交；消费方声明 `PARTNER_AUTHORIZATION_SOURCES`，一次性认领该 URL，并且只保存自己最终换取的会话。服务之间不得相互导入，消费方也绝不调用生产方的授权端点。

不要将其视作登录回退手段或通用的浏览器链接包装器。如果消费方同时也接受账号凭据，应提供一个带 `resets_session=True` 的显式 `login_method` 设置，并隔离两套令牌与刷新域。完整的生产方与消费方模板、持久化规则及测试见 [partner-authorization.md](partner-authorization.md)。

## 错误处理

抛出具备明确指引的服务错误。失败时不要只打印消息或静默返回。在距离最近、拥有当前页面的父级选择器/输入框处捕获预期的子流程失败，让用户能够重试或仅退回一页。不要让一次普通的子流程失败一路回退穿透所有父级直到服务首页；详见 [navigation.md](navigation.md)。

接下来的行为取决于异常类型，分界线在于**是外部状况出了问题**，还是**这段代码本身写错了**：

| 抛出的异常 | 会话行为 |
|------------|----------|
| 任意 `RuntimeError`——本代码树中的服务异常类都继承自它 | 最近的所属页面应报告该错误并保持可用；未被捕获而到达 `Service.home()` 的错误，会以服务菜单作为兜底安全边界 |
| 其他任何异常——如 `TypeError`、`AttributeError`、`KeyError` | 终止会话，弹出错误面板，并在调试模式下输出完整堆栈 |

因此，遇到登录过期、CDM 拒绝签发许可、当前地区未提供某集时：抛出你自己的异常，并告诉用户该如何应对。报告错误的同时保持紧邻的父级页面依然可用。诸如“请从本服务菜单重新登录”的建议很有帮助，但这不是直接跳回菜单的理由；用户可以自己逐级退回。

`TypeError` 是程序 bug，刻意让它明显地暴露出来。把它藏在友好的菜单后面，只会让 bug 变成难查的谜团。

有两点影响值得注意：

- **自定义错误必须继承自 `RuntimeError`。** 例如 `class ExampleError(RuntimeError)`——模板已经这样写，服务测试也会校验这条规则。
- **如果你自己编写了 `home()` 菜单循环**而不是沿用基类实现，你也必须自行处理恢复逻辑，因为抛出异常的生成器无法从外部恢复执行。把捕获代码放在你已有的 `except Back: continue` 旁边：

  ```python
  except RuntimeError as exc:
      ctx.problem(
          f"{self.NAME} could not finish that",
          f"{type(exc).__name__}: {exc}",
          "The service menu is still open - the rest of the session is fine.",
      )
      continue
  ```

  `ctx.problem()` 是高显眼度通道：既会在屏幕上显示面板，也会写一条日志。`ctx.warn()` 和 `ctx.log()` 则保留给常规的过程记录。

## 令牌

`self.ctx.tokens` 是一个以**当前服务专属目录**（`paths.tokens/<your id>/`）为根的 `TokenStore`。传给它的应当只是文件名，绝不要传路径：名字中带有 `/` 会在你的目录里创建子目录，而且其他服务都无法访问这里，根本不需要靠子目录来防冲突。

```python
cached = self.ctx.tokens.read("example_token.json")
self.ctx.tokens.write("example_token.json", {"token": ..., "expires": ...})
```

没有第二处读取位置。如果你的服务在早期版本中使用过不同的文件名，只需读取一次旧文件名、写入新文件名，然后**删除旧文件**——保留副本会导致退出登录时删不掉旧文件，下次读取时又把它重新载入，在用户看来就是退出登录没生效。

当一个服务暴露多套相互独立的 API 家族时，客户端选择器不是授权选择器。请为每套家族分配各自独立的令牌文件或受管 Cookie 配置，将选择器的 `resets_session=False`，并根据当前所选家族路由读取、刷新和登出操作，不要检查或清理另一套家族的状态。`Playback` 必须把所属家族写入 `DrmInfo.context`；许可路由应依据该记录的上下文执行，而不是取事后恰好切换到的设置值。包含多个独立客户端家族的服务应始终让这些配置彼此隔离。

无论这个设置项被命名为 **platform**、**API version**、**region**、**delivery** 还是 **profile**，这条规则都同样适用。切换路由不等于退出登录。切勿在设置变更回调中调用 `logout()`、删除令牌/Cookie 文件、覆盖其他变体的缓存，或把其他变体标记为已登出。保持每条路由的令牌/Cookie 状态可独立寻址，并且只刷新当前请求所选中的那一条。显式的“退出登录（Sign out）”操作可以按该服务文档规定的策略清理所选服务会话，但绝不能作为修改选择器的副作用来执行。

如果文件缺失会被解读为“从未登录”从而静默重新进入用户刚刚退出的流程，那么退出登录时必须写入空状态或墓碑状态（tombstone state），而不能仅仅删除文件。保持所选路由的状态可独立寻址。

## Cookie

许多服务的登录过程根本无法通过脚本复现——验证码、设备证明、SSO 重定向——此时导出的 `cookies.txt` 不是捷径，而是唯一途径。只要声明它，其余工作由 Core 接管：

```python
class Example(Service):
    USES_COOKIES = True
```

仅这一项声明就会为你提供：

- 服务菜单中的登录入口，用于校验受管的 Cookie 配置
- 一个 **浏览器 Cookie 文件（Browser cookie file）** 服务设置项，仅从 `~/.unidl/cookies/example/` 下的直接 `.txt` 文件中读取选项
- 显式选中的 `example/<profile>.txt` 会被精确使用，绝不回退到其他账号；留空或自动选择时仅使用 `example/default.txt`
- **自动附加到每个会话的 CookieJar**：所有通过 `self.ctx.session()` 创建的会话都会自动携带它
- `logout()` 自动删除当前选中的文件
- 供 `auth_status()` 使用的 `self.cookie_status()`

大多数服务除了这行声明之外不需要编写任何 Cookie 代码，因为 Cookie 并不是另一条独立代码路径——它只是带上账号身份发送相同的请求。

登录流程不会向用户询问文件路径。只需把浏览器导出的文件直接放入该服务的 Cookie 目录，然后在设置中选择对应配置即可。这是有意为之的设计：允许填写任意路径会打破按服务隔离的目录边界。

```python
def auth_status(self) -> AuthStatus:
    status = self.cookie_status()          # logged_in + "17 cookie(s) for .example.com"
    if status.logged_in:
        return status
    return AuthStatus(logged_in=False, anonymous_ok=True, label="free tier needs no sign-in")
```

有几点值得了解：

- **刻意忽略过期时间。** 浏览器导出的文件往往本身就已过期，而 `requests` 会在**发送请求时**静默丢弃过期 Cookie——结果就是 CookieJar 看似加载正常，实际却什么都没发出去，服务端直接按未登录响应。内存中的 CookieJar 已消除过期限制，用户的原文件则绝不会被改写。
- 对于必须保持匿名的请求（例如签发访客令牌），请使用 `self.ctx.session(cookies=False)`。
- 使用 `self.ctx.cookie_header(url)` 为 `Playback` 构造 `Cookie:` 请求头，因为下载器接收的是请求头而不是 CookieJar。它会按目标 URL 的主机名收窄范围，避免把整个账号的 Cookie 都发给 CDN。
- Cookie 选项应当是配置名而非路径。不要自行添加自由文本格式的服务级 Cookie 路径设置：那样会绕过按服务隔离的目录边界，也会阻止 Core 统一展示可用账号。

同时具备原生登录**与** Cookie 的服务，会在菜单中同时提供两者。它们不是抽象意义上的平替方案：原生登录可用时体验更好，而 Cookie 是原生登录走不通时的退路。

## 报告登录状态

`auth_status()` 必须开销极低，且绝不能访问网络。它在构建服务菜单和身份栏时会被调用，在登录、登出或设置变更后也可能再次触发。主页刻意不对每一行平台都调用它。

```python
def auth_status(self) -> AuthStatus:
    cached = self.ctx.tokens.read("example_token.json")
    if cached and cached.get("token"):
        return AuthStatus(True, f"Example · {cached.get('username', 'signed in')}")
    if self.ctx.credential("us").complete:
        return AuthStatus(False, "credentials ready", detail="will sign in on demand")
    return AuthStatus(False, "no credentials", detail="slot 'us'")
```

`logout()` 应当丢弃你在内存中缓存的一切状态；当带有 `resets_session=True` 的设置项发生变更时，Core 会自动调用它。

## 命名

`self.save_name(title)` 按项目规范生成名称：`Title.S01E02.Episode.Name` / `Title.YEAR`。只有当你的服务确实需要截然不同的命名方式时才覆写它，同时声明 `naming="self"`。

## 能力声明

默认所有能力都交由 `core` 处理。目前唯一实际接入原生运行时覆盖的声明是 DRM：

```python
USES = Capabilities().with_self("drm")     # I fetch my own keys
```

设置 `drm="self"` 后，引擎会调用 `get_keys(playback) -> ["kid:key", ...]`，而不再走已注册的 DRM 系统。`auth`、`catalog`、`tracks` 和 `naming` 字段目前仅用于在诊断信息和平台列表中描述归属，不会切换替代钩子。无论这些字段怎么填，直接实现 `login()`/`auth_status()`、流程入口点、`Playback` 和 `save_name()` 即可。

### 可选的混流音视频分离

只有当服务希望把有限时长的已混流源暴露为可独立选择的视频轨和音频轨时，才设置 `Playback(split_muxed_audio=True, ...)`：

```python
return Playback(
    title=title,
    save_name=self.save_name(title),
    manifest_url=authorized_master,
    split_muxed_audio=True,
)
```

默认值为 **False**。不新增全局开关，普通混流源保持自带音频不变。Core 只会对标记了 `extra["muxed_audio"]` 的源执行投影拆分；独立音频与字幕不受影响。每个源分别暴露其第一条视频轨和第一条音频轨。Core 不会在轨道选择器弹出前探测媒体或初始化字节，不会假定不同码率版本的音频完全一致，也不会凭空编造音频码率或声道元数据。拆分出的音频行会在分组标签中保留原视频源分辨率，方便用户区分各个版本。

投影拆分运行在 `filter_tracks()` 之后。请先在 `filter_tracks()` 中为原始源标注真实的 `language`、`name`、角色以及 original/default 标记。可选的已核实内嵌音频提示包括 `extra["audio_language"]`、`muxed_audio_codec`、`muxed_audio_bitrate`（比特/秒）和 `muxed_audio_channels`。不要在服务过滤器中自行合成新的音频流；Core 会为拆分后的两条投影行保留来源信息与过滤能力。

下载器会合并/解密单个物理源，并使用 FFmpeg 流拷贝只提取选中的投影轨。同时选中同一源的两条投影轨会共享下载传输，并发下载时同样如此。只选音频仍会传输该混流源；选择另一码率版本的音频同样需要传输那个源。同组其他轨道失败时，已完成的源/输出检查点仍会保留。缺少 FFmpeg 或实际源中缺少所选轨道时会明确报错，而不会静默退回为混流输出。

初始契约支持明文和 HLS AES-128 点播（VOD）。直播录制保持不变；在多轨采样解密得到支持并验证之前，CENC/CBCS/SAMPLE-AES 源的分离会被明确拒绝。加密分离也不能配合 `--no-decrypt` 使用。主/媒体导出和命令会保留该策略，不展开分片数组，也不依赖服务插件。正常输出仍可将选中的文件重新混流在一起；选择 **不混流（保留原始轨道）** 则保留独立的原始编码文件。

### 解析后轨道过滤

框架不提供让服务替换 Core 解析器的 `get_tracks()` 钩子。但服务可以在 manifest 解析完成后实现 `filter_tracks(playback, tracks, log)`。返回一个仅包含现有 `StreamInfo` 对象的可迭代集合，用于限定暴露给自动选择与交互式轨道选择器的轨道；返回 `None` 则保留完整码率阶梯：

```python
def filter_tracks(self, playback, tracks, log):
    del playback, log
    return [
        stream
        for stream in tracks.streams
        if not (
            stream.media_type == "audio"
            and str(stream.role or "").casefold() == "audio description"
        )
    ]
```

这只是输出过滤器。Core 会保持 `tracks.streams` 不变，用于 DRM、密钥库查询和导出溯源；返回的轨道子集保存在 `tracks.selectable_streams` 中。切勿创建替代流对象、执行许可请求或从 `tracks.streams` 中移除条目。当服务商的口述影像（Audio Description）元数据不统一时，应结合 `role`、`name`、`language` 和 `extra` 一起判断。`name` 只是显示标签而非语言字段，因此将其改为 `und` 并不能可靠地影响自动轨道选择。

如果服务商通过单独的已授权 API 而非 manifest 内部返回字幕，请将完整列表保存在 `Playback.subtitle_references` 中，并通过 `augment_tracks()` 钩子暴露出来。返回的 `StreamInfo` 行需标记 `extra["service_sidecar"] = "subtitle"` 和 `extra["subtitle_reference_url"] = reference.url`。Core 会将这些行放进同一个轨道选择器并应用共享的 `sub_langs` 规则，且不会把它们交给原生 manifest 下载器。实际下载时，Core 会把选择结果同步回引用对象，再由服务的 `prepare_download()` 只拉取被选中的文件并加入 `Playback.mux_imports`。服务不应通过设置项决定存在哪些字幕行，也不应使用共享字幕输出设置去改动服务商的 API 请求。

Core 会在该钩子之前打开下载进度框架，并将每条已选 API 字幕的结果与原生媒体进度并列保留。现有钩子无需改动：在本地文件注册完成前，它们会显示为活动/未知进度状态。若需要精确的字节进度，可改为实现可选的 `prepare_download_with_progress(playback, log, progress)`，并在读取响应过程中调用 `progress(reference, received_bytes, total_bytes)`（总大小未知时传 `None`）。该钩子在本次运行中会**替换**而非补充旧版钩子：不要把同一条字幕拉取两次。完成后仍需将本地文件加入 `Playback.mux_imports`，保留其语言、名称、类型和标记。只有成功准备完成的文件才会获得已完成的进度行。

可以将本地 `.json` manifest 路径作为 `manifest_url` 传入，因为原生交付核心能够解析此类输入。模型中虽然存在 `Playback.json_manifest` 字典字段，但当前交付控制器会跳过它，原生服务暂时不要使用该字段。

## 播放与并发会话

在移植任何服务端的播放、观看、并发控制、心跳或辅助会话逻辑前，先在源脚本的 WV 与 PR 变体中追踪其实际调用点。响应里出现某个字段，或源码中存在未调用的方法，都不是旧流程确实使用过该生命周期的证据。

当确认旧流程确实使用过它时，请将传输逻辑保留在服务本地，并把停止/释放调用放进包裹同步 `yield ctx.emit(playback)` 的外层 `finally` 块中（如果旧脚本是在服务自管的多 PSSH 密钥循环期间开启的，则放在整个密钥循环的外层 `finally` 中）。这样即可涵盖 manifest 失败、许可失败、取消以及返回/退出等所有场景。当前的审计矩阵、易混淆的非会话伪装项以及详细核查清单，请参阅 [playback-lifecycle.md](playback-lifecycle.md)。

## 外部辅助工具

如果你的服务需要 `java`、`node`、`adb`、证书或按路径加载的 Python 模块，请显式声明它。不要硬编码路径并在流程执行到一半时崩溃。详见 [external-helpers.md](external-helpers.md)。

## 脱离界面测试服务

```python
from unidl.core.flow import AutoPresenter, FlowContext, run_flow

ctx = FlowContext(settings=service.settings)
emitted = run_flow(
    service.open_url(ctx, "https://example.com/show/x"),
    AutoPresenter({"Season": 2}),          # match by ask title
)
assert emitted[0].save_name == "Show.S02E01.Pilot"
```

`AutoPresenter` 默认选取第一个启用项，多选时遵守 `preselected`，并接受以交互请求标题为键的覆盖答复。遇到 `Await` 时，它只调用一次 `poll`，若结果仍为 `None` 则抛出 `Back`，因此无头遍历绝不会苦等完整的设备激活码超时。

## 移植核查清单

- [ ] `ID`、`NAME`、`ALIASES`、`TITLE_RE`、`GEOFENCE`、`MEDIA_TYPES`（默认为 `("video",)`；纯音频服务用 `("audio",)`，平台同时提供音视频时用 `("audio", "video")`）。主页的国家/地区视图将第一个 `GEOFENCE` 代码作为主市场，把空声明归入 International；类型视图则会把音视频平台放入独立的综合分组。
- [ ] `SUPPORTS_*` 标志与已实现的方法一致
- [ ] `DRM_SYSTEMS` 仅列出服务真正支持的系统
- [ ] API 客户端位于 `api.py` 中，无打印输出
- [ ] `auth_status()` 开销低且离线运行
- [ ] 令牌文件名一致；若登录态需要从外部旧版状态承接，已显式校验并仅复制一次
- [ ] 设置项已声明，未使用硬编码常量
- [ ] 凭据从槽位读取，绝不硬编码
- [ ] `Playback` 的 `save_name` 来自 `self.save_name(title)`
- [ ] 已填充 `DrmInfo`，未直接调用 CDM
- [ ] 已审计旧版 WV/PR 播放会话调用点，未仅凭名称推断生命周期
- [ ] 启动/心跳/停止保持在服务本地，且清理操作放在外层 `finally` 中
- [ ] 已测试成功、manifest/许可失败、取消及返回/退出场景下的清理次数
- [ ] 翻页选项使用了 `navigates=True`
- [ ] 若使用任何外部工具，均已声明辅助工具依赖
- [ ] 已对照线上真实服务完成验证，而不仅仅是能够导入
