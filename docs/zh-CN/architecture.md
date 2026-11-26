# 架构设计

[English](../architecture.md) | [简体中文](architecture.md)

## 三层架构

```
┌──────────────────────────────────────────────────────────────────┐
│  Shell        unidl/tui/         全屏界面、导航、设置、队列      │
├──────────────────────────────────────────────────────────────────┤
│  Services     unidl/services/    登录、目录浏览、manifest 发现、 │
│                                  许可证请求                      │
├──────────────────────────────────────────────────────────────────┤
│  Delivery     unidl/downloader/  解析、选轨、下载、解密、字幕、  │
│                                  混流                            │
└──────────────────────────────────────────────────────────────────┘
```

服务层与交付层之间的边界是刻意划定的。原生交付核心只接收 URL、manifest 和原始密钥，不包含任何账号登录、CDM、许可证请求或密钥获取逻辑。凡是涉及账号或 CDM 的操作，全部留在 UniDL 的服务层内。这项分层原则必须始终保持。

## UniDL 的边界

每个服务都是原生 UniDL 服务。服务注册被明确拆分为两个独立步骤：在代码层面，`registry.register` 接收来自 `unidl.services` 的 `Service` 子类并校验其声明；在用户层面，服务管理页面会持久化哪些已发现的服务包允许显示在主页和全局搜索中，重启后加载器即可自动注册具体的服务类。新发现的服务包在**设置 → 服务 → 注册服务**中选中后，重启即可生效。这两种注册路径等价；通过 TUI 注册并不要求服务包内部使用装饰器。

服务实现既可以作为源码包分发，也可以作为**仅编译（compiled-only）**包分发。仅编译包包含 `service.toml` 以及与 ABI/平台绑定的 Python 扩展模块，不包含服务源码。加载器会在导入前校验其声明的 Python ABI、操作系统/架构以及 UniDL 版本。不兼容的服务包会被隔离，并作为增强级别的就绪检查（Readiness）条目上报；它绝不会阻塞源码服务或 TUI 启动。服务提供方必须为每个受支持的 Python ABI 与平台分别构建产物——例如 macOS / Python 3.13 的构建包无法直接移植到 Windows 或 Linux。

跨越 UniDL 核心边界的东西只有三类，且仅此三类：

**1. 原生交付核心。**通过 `core/delivery.py` 与 `NativeDownloaderBackend` 调用，向其传入 manifest、保存目录以及所需密钥。不存在第二套下载器包，也不派生外部下载器子进程。传入的数据结构详见[交接对象](#交接对象)。

**2. 服务声明的外部助手。**服务在 `HELPERS` 列表中声明的二进制程序、Python 模块或资源文件——例如 `java`、`node`、`adb`、签名 jar 或客户端证书。解析流程固定为五步，且每一步都来自明确指定的位置：`unidl.yaml` 中的服务专属配置键、共享配置键、二进制程序的 `PATH`、`<paths.helpers>/<service>/`，以及声明自身的 `extra_paths`。整个过程不做漫无目的的搜索。若这五步均未找到，系统会在你浏览内容之前就报告缺失并给出安装提示，而不是等你选完剧集才报错。详见 [external-helpers.md](external-helpers.md)。

**3. 下载目录** `paths.downloads`，按服务各自建一个子目录。旁边是以同样方式按服务划分的 `paths.subtitles`，供自行拉取字幕而非交给交付核心处理的服务使用。

除此之外的所有内容都留在项目目录内部：登录态在 `tokens/`，内容密钥在 `db/`，CDM 设备在 `cdm/`，Cookie 在 `cookies/<service>/`，导出命令在 `download_commands/`，可重新拉取的临时缓存在 `cache/`。任何模块都不应引用其他工具目录树中的路径，也不应从中读取登录态；`unidl.yaml` 中 `paths:` 下未识别的键会被直接忽略而不会静默生效——一个悄悄不起作用的配置项比直接不读取更糟。

## 让多服务架构保持可维护的核心规则

服务代码绝不导入 UI 框架，绝不调用 `print()`，也绝不调用 `input()`。它只负责描述“需要向用户询问什么”，如何渲染则完全交给 Shell 层决定。详见 [writing-a-service.md](writing-a-service.md)。

界面呈现只需在 Shell 中实现一套；如果每个服务各自实现菜单，导航体验和无障碍支持就会彻底失控。

## Flow 协议

服务入口本质上是一个生成器：通过 `yield` 发出交互请求（*ask*），并接收用户的回复。

```python
def open_url(self, ctx, target):
    show = self.api.show(target)
    season = yield ctx.pick("Season", [Choice(f"Season {s}", s) for s in show.seasons])
    episodes = yield ctx.pick("Episodes", choices, multi=True)
    for episode in episodes:
        yield ctx.emit(self.get_playback(episode))
```

`core/flow.py` 定义了所有 ask 类型（`Pick`、`TextAsk`、`Confirm`、`TableAsk`、`Await`、`SettingsAsk`、`Suspend`、`Emit`）、构造它们的 `FlowContext`，以及驱动整个流程对接展示层（*presenter*）的 `run_flow()`。

目前有两种展示层实现：

- `tui/session.py:TextualPresenter` 将 ask 渲染为终端控件。Flow 运行在工作线程上；每个 ask 都会封送到事件循环中，工作线程则阻塞在 `Event` 上等待用户作答。
- `core/flow.py:AutoPresenter` 根据规则表自动作答，方便开发服务时在无终端环境下跑通流程（见 `writing-a-service.md`）。

`Back` 和 `Quit` 会被*抛入（throw）*生成器内部，因此流程可以在需要的层级捕获 `Back` 以退回上一级，否则任其向上冒泡。

`PartnerHandoff` 是唯一不渲染界面的路由类 ask。它携带一个仅存内存、一次性使用的 `PartnerAuthorization`，从生产方服务移交给显式列入白名单的消费方服务。会话驱动通过注册表实例化消费方，核心层则在任意结果下立即擦除该 URL；两个服务之间互不导入。关于契约、安全规则以及生产方/消费方模板，详见 [partner-authorization.md](partner-authorization.md)。

### Ask 作用域决定所在界面

整个 UI 由四个界面组成，每个界面各回答一个明确的问题，因此 ask 会显式携带作用域声明自己归属哪一层，而不是让 UI 根据嵌套深度去猜测。详见 [ui-design.md](ui-design.md)。

| 作用域 | 对应界面 | 设置方 |
|--------|----------|--------|
| `root` | 界面 2：服务主菜单 | `Service.home()` |
| `flow` | 界面 3：服务处理流程 | 默认值，服务通常无需关心 |
| `delivery` | 界面 4：下载或命令输出 | `SessionController` 为自身两个 ask 设置 |

`tui/session.py:SessionController` 持有工作线程并负责界面路由：收到首个 `flow` 作用域 ask 时推入界面 3，进入下载任务时推入界面 4，流程回到服务主菜单时依次弹出两者。界面切换采用替换而非复用，有意丢弃旧界面的临时状态；凡是需要跨越界面存活的状态（如日志、失败标记）都保存在控制器上。

### 导航层级

| 当前位置 | `^b` 的行为 |
|----------|-------------|
| 界面 1：平台列表 | 清除筛选条件；无筛选时无动作（已是根界面） |
| 界面 2 至 4：存在待处理的 ask | 回复 `Back`，具体含义由流程自行决定 |
| 界面 2：无待处理 ask | 退出当前服务，返回平台列表 |
| 界面 3：无待处理 ask | 拒绝操作：服务正处于处理中 |
| 界面 4：下载运行中 | 停止下载；详见 [cancelling.md](cancelling.md) |
| 设置 / 搜索 | 关闭并返回调用方界面 |

这一行为通过两处配合实现：`Service.home()` 捕获来自子流程的 `Back`，各界面则实现 `go_back() -> bool`（返回 `True` 表示已在内部消化，返回 `False` 表示弹出当前界面）。

### 失败对 Flow 的影响

Flow 是一个生成器，一旦抛出未捕获异常就会彻底终止——无法恢复，只能重建。这一事实决定了错误处理必须放在菜单循环内部，否则就根本无法恢复。

`Service.home` 会在每个菜单分支外捕获 `RuntimeError`，通过 `ctx.problem()` 向用户报告错误并重新显示菜单。整个代码树中的所有服务异常类，以及核心层的 `CdmError` 和 `HelperError`，都继承自 `RuntimeError`，因此“服务拒绝或失败”能稳定地对应“这一步结束了”，而不是“整个会话崩掉了”。除此之外的异常（如 `TypeError`、`AttributeError`）会一路展开到会话驱动层，展示错误面板并中止运行：那属于代码 bug，应当清晰暴露出来。

自行编写菜单循环的服务也必须遵守同样做法，原因完全一致；原生服务契约明确要求保持这种行为。

## 交接对象

服务最终产出的唯一对象是 `Playback`（定义于 `core/playback.py`）：

```python
Playback(
    title=Title(...),
    save_name="Show.S01E02.Episode.Name",
    manifest_url="https://.../stream.mpd",   # 或使用 json_manifest=
    headers={"User-Agent": ...},
    is_live=False,
    drm=DrmInfo(license_url=..., headers=..., context=...),
)
```

该模型接受三种带类型的源形态：

- `manifest_url` — HLS、DASH、ISM 链接，或本地 `.json` 轨道清单文件路径
- `json_manifest` — 字典结构，专供已直接掌握各轨道直链的服务使用
- 内联 HLS 文本 — 解析前会原子写入 Core 的私密临时文件

三种形态最终都会进入同一个带类型的 `ParseRequest`；TUI 与独立的 `unidl list/download` 命令共享同一条解析链路，不维护两套解析逻辑。

## 原生交付边界

`core/engine.py` 构造带类型的 `ParseRequest`/`DeliveryPlan` 并交给 `NativeDownloaderBackend`，执行步骤如下：

1. 通过原生后端带类型的 `parse()` 方法解析；当服务显式提供多个已授权的 `Playback` 变体时，逐一解析并构建单一的、以 JSON 承载且已去重的轨道阶梯。
2. 基于所有已解析的加密轨道构建仅含 DRM 信息的清单。
3. 若服务持有经验证的、按清晰度变体区分的初始化种子，允许通过 `prepare_drm` 选定精确初始化数据，但不在此处发起许可证请求。
4. 默认在共享轨道选择之前查询密钥库并请求缺失密钥；开启兼容模式后仅反转这两步，并将 KID/初始化数据探测范围收窄到最终选中的轨道。
5. 执行共享输出轨道选择，且禁止轨道选择反向干预服务 API 配置或服务自有的许可证配置。
6. 通过后端带类型的 `run()` 方法启动下载，直接传入已解析好的流列表，避免二次网络解析并防止轨道索引漂移。
7. 在 TUI 中直接渲染结构化进度、日志消息、取消状态和最终输出产物，不劫持进程级标准输出。

## DRM

`core/drm.py` 将 Widevine、PlayReady 和 MonaLisa 统一注册在同一种交互接口之后。以下 Widevine 本地路径展示了联网 DRM 系统的基本流程：

```
Device.load -> Cdm.from_device -> open -> get_license_challenge
            -> service.get_license(challenge)   <- 唯一随服务变化的部分
            -> parse_license -> get_keys -> filter SIGNING
```

走 Core 联网路径的服务必须自行实现许可证传输。系统不提供默认的裸 POST 实现：端点 URL、请求头、鉴权方式、请求体封装与响应解包全部留在对应服务包内。基类的 Widevine 与 PlayReady 钩子默认 fail-closed（未实现即拒绝）。PlayReady 会相应替换为 WRM 初始化数据与 SOAP 交换。MonaLisa 接收伴随播放数据下发的票据，通过本地 `.mld` 与 wasm 模块完成解包。远程 Widevine 与 PlayReady CDM 复用同一注册表，无需本地设备文件。详见 [drm.md](drm.md)。

默认的密钥获取顺序：

```
解析完整 manifest -> 服务许可证方案 / 初始化数据钩子 -> 获知 KID
                  -> 密钥库全命中？直接使用，跳过许可证请求
                  -> 部分命中？仅请求缺失 KID 的许可证，合并后写入密钥库
                  -> 未命中？请求许可证，随后写入密钥库
                  -> 共享输出轨道选择 -> 原生交付核心
```

对于主播放列表无法暴露所选媒体播放列表初始化数据的服务，可在**设置 → 服务 → 最终选轨后获取许可**中配置全局默认值。服务级设置可覆盖该项；否则沿用全局服务设置：

```
解析完整/合并阶梯 -> 共享输出轨道选择 -> 最终选中的加密轨道
                  -> 服务许可证方案 / 初始化数据钩子 -> 密钥库 / 许可证请求
                  -> 原生交付核心
```

该开关仅改变 Core 提供 DRM 清单的时机。密钥库总开关与目标列表同样在未显式覆盖前继承全局**DRM 与密钥库 → 密钥库策略**。开启此项并不授权服务去读取 `video_quality`、音频、字幕或交互复选框设置来决定请求哪个服务商端点。无论哪种模式，服务自有的 `license_*` / `pssh_*` 设置都与共享输出轨道选择保持独立。

解析必须先于密钥获取执行，因为只有 MPD 或其初始化分片才能提供 KID 与 DRM 初始化数据。详见 [drm.md](drm.md) 与 [key-vault.md](key-vault.md)。

## 能力声明（Capabilities）

`Capabilities(auth, catalog, drm, tracks, naming)` 中的每一项取值均为 `"core"` 或 `"self"`。它用于向诊断系统标明各能力的归属方，并允许服务显式接管某项实现。原生运行时会在 `drm="self"` 时分派至服务自有的 DRM 路径；其余字段仅用于描述归属，不会衍生额外的插件 API。

原生服务应使用 [writing-a-service.md](writing-a-service.md) 中记载的标准钩子。每个已注册服务都是原生的 `Service` 子类。

## 模块分布

```
src/unidl/
  __main__.py            启动入口与只读诊断命令
  core/
    flow.py              Flow 协议——服务层与 UI 层的唯一接口
    service.py           Service 基类、Capabilities 与服务注册表
    devreload.py         仅调试模式下显式触发的原生服务热重载
    playback.py          Playback、DrmInfo 数据模型
    titles.py            Title 模型；直播频道与剧集地位对等
    engine.py            Core 编排与带类型的交付计划
    drm.py               DRM 注册表与系统分发
    cdm.py  pssh.py      Widevine 设备与初始化数据支持
    playready.py         PlayReady 设备与 SOAP 支持
    monalisa.py          MonaLisa 票据与本地 wasm 支持
    remotecdm.py         pywidevine / pyplayready HTTP 远程 CDM
    vault.py             本地 SQLite 密钥库
    vaults.py            有序的本地 / API 密钥库集合
    settings.py          全局 / 服务 / 轨道三层设置
    config.py            unidl.yaml 配置加载
    secureio.py          带文件锁、仅属主可读写的安全原子写入
    credentials.py       凭据槽位管理
    cookies.py           浏览器 Cookie 导入与存储
    cache.py             令牌存储：单一目录读写
    exports.py           可移植的已解析标题导出文档
    naming.py            输出文件命名
    helpers.py           外部二进制、模块与资源助手解析
    brands.py            服务 ID 到品牌名称的映射
    justwatch.py         上架查询与平台到服务的映射
    appearance.py        持久化的主题外观选择
  tui/
    app.py               全局快捷键与返回层级规则
    chrome.py            常驻顶部状态栏
    banner.py            两种尺寸的字符块横幅
    home.py              界面 1：平台列表
    askhost.py           界面 2 至 4 的公共基类
    service_screen.py    界面 2：服务主菜单
    flow_screen.py       界面 3：服务处理流程
    download_screen.py   界面 4：下载执行或命令输出
    session.py           SessionController：工作线程与 ask 路由
    asks.py              与各 ask 类型对应的交互控件
    settings_screen.py   三分区设置面板
    search.py            服务、标题与密钥搜索
    justwatch_screen.py  跨地区上架查询界面
    cdm_screen.py        本地与远程 CDM 设备选择器
    palette.py           命令面板
    theme.py             样式表
  services/
    bbciplayer/          原生服务包示例
    <service>/           每个服务独立一个包
```

## 验证与测试

在这个项目里，代码“看起来没问题”绝不代表它真的能工作。本地与无头（headless）测试覆盖接口契约与界面行为；实时（live）检查则验证真实网络结构、账号鉴权、许可证流程与线上 manifest。完整测试分组、前置要求与运行命令详见 [testing.md](testing.md)。
