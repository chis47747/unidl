# 配置指南

[English](../configuration.md) | [简体中文](configuration.md)

## 首次启动：无需编写 YAML

安装完成后直接运行 `unidl`（或 `python -m unidl`）。缺少 YAML 配置文件不会阻碍启动。首次启动时，主页会显示实际生效的下载目录，并提供可点击链接方便查看或修改。

如果没有本地配置文件，内置的不含凭据的默认 `unidl.yaml` 会提供相对路径默认值：`downloads/<service>/`、`download_commands/`、`exports/`，并将运行时状态放在预期本地配置文件的同级目录下。例如，从 `D:\unidl` 启动打包版安装时，下载目录会落在 `D:\unidl\downloads`。未加载本地文件时，显式设置的 `UNIDL_HOME` 环境变量仍会重定向运行时状态目录。如需修改输出目录而不想手改 YAML，直接前往**设置 → 文件与命名 → 输出位置 → 完成的下载**即可。

设置中保存的**完成的下载**偏好优先级高于 YAML 中的 `paths.downloads`。已有的本地 YAML 会继续保持其原有路径解析行为（包括对省略条目回退到主目录的规则）。修改 `paths.home` 只影响运行时状态，不改变成品媒体下载目录。更改目录不会自动搬迁已有文件。

仅在需要指定特定配置文件时使用 `--config PATH`。例如，执行 `unidl --config ./unidl.private.yaml` 不会读取 `unidl.yaml`，因此修改后者不会影响本次运行；后续启动也应继续传入同一显式路径。源码安装默认读取仓库根目录的 `unidl.yaml`；打包安装若当前目录存在 `unidl.yaml` 则直接读取，不存在时随包默认相对路径也以该当前目录为基准，而不是包安装目录。在主页点击**配置（Config）**可直接生成本地配置文件。**就绪检查 → 文件存放位置**会明确报告配置文件路径（包括未找到的情况）、运行数据目录以及实际生效的下载目录。

UniDL 采用两层配置体系：

- `unidl.yaml` 存放路径、设备定义、凭据、外部助手、代理以及各服务专属的静态值。
- `paths.home` 下的 `settings.json` 存放交互式偏好设置，例如画质、输出轨道、主题、界面语言、密钥库策略以及当前选用的 CDM。

如果你决定使用自定义 YAML，启动 UniDL 时显式传入即可：

~~~console
python -m unidl --config ./unidl.yaml
~~~

配置文件中的相对路径均相对于该 YAML 文件所在目录解析，而非进程当前工作目录，从而保证整个检出目录可随处迁移。私人部署环境也支持使用绝对路径和环境变量。

## 最小 YAML 示例

~~~yaml
paths:
  home: .
  commands: ./download_commands
  exports: ./exports
  keys_db: ./db/keys.db
  downloads: ./downloads
  helpers: ./helpers
  cdm: ./cdm
  cookies: ./cookies
  tokens: ./tokens

cdm:
  default: widevine-test
  devices:
    widevine-test: ./cdm/widevine/device.wvd
    playready-test: ./cdm/playready/device.prd
    monalisa-test: ./cdm/monalisa/device.mld

credentials: {}
helpers: {}
proxies: {}
services: {}
~~~

具体的服务设置与凭据字段由各服务自行声明。未知的路径键会被直接忽略，避免拼写错误悄悄把运行时状态重定向到意外位置。

## 路径与运行时状态

**设置 → 文件与命名 → 高级路径 → 服务目录**展示了当前运行环境实际使用的服务目录以及其他运行时路径。点击某一行或按 `Enter` 可直接打开对应文件夹（密钥库行会打开其所在目录），不会修改路径也不会移动已有文件。查看或打开路径不会自动新建目录；若目录不存在仅作提示，不会顺手创建。服务目录行不会向 YAML 写入路径覆盖。另请参阅[设置参考（文件与命名）](settings.md)。

`paths.home` 是临时数据与账号相关状态的根目录。下表中的大多数运行时目录都由它派生，只有成品下载目录例外：

| 路径 | 用途 |
|---|---|
| `downloads` | 成品媒体文件，按服务分子目录。随包默认值为 `./downloads`，不从 `paths.home` 派生。 |
| `commands` | 保存的命令与导出文本，按服务分子目录。 |
| `exports` | 可重新导入的已解析标题 JSON 文档。 |
| `cache` / `temp` | manifest、分段及临时工作数据。 |
| `logs` | 带时间戳的会话日志与单任务调试日志。 |
| `tokens` | 服务登录态与刷新令牌。 |
| `cookies` | 浏览器 Cookie 配置档案，按服务分子目录。 |
| `cdm` | 本地 DRM 设备文件，按 DRM 系统分子目录。 |
| `keys_db` | 本地 SQLite 内容密钥库。 |
| `subtitles` | 由服务单独抓取的外挂字幕文件。 |
| `helpers` | 服务所需的外部助手资源与模块。 |

### 成品文件布局与失败任务

成品媒体统一按 `downloads/<service>/` 分组存放。可选设置 `episode_folder_template` 仅针对剧集标题增加一层相对子目录；电影、直播节目和纯音频标题仍直接保存在服务目录下。例如 `{series}/{season}` 会生成 `downloads/<service>/<series>/Season 01/<file>`。可用模板字段包括 `{series}`、`{season}`、`{season_number}`、`{year}`、`{season_episode}` 和 `{service}`。对剧集而言，若服务提供了剧集首播/发行年份，`{year}` 会使用该年份，否则回退至单集年份。模板会按相对路径严格校验，不允许逃逸出服务目录；留空则保持平铺结构。

下载进行期间，轨道文件、外挂字幕和正在混流的文件都会暂存在隔离的单任务工作目录中。任务成功后只把最终成品提交到目标目录；失败或取消的任务默认会删除该工作目录，避免残缺文件堆积在成品旁边。调试时可开启 `keep_failed_intermediates` 以保留失败工作目录。有效分段与已完成轨道阶段检查点同失败残余文件完全分离：`resume_parts` 控制失败或取消后是否复用这些有效成果，`keep_temp` 控制成功后是否保留缓存。检查点只在最终成品成功提交后才会被清理。上述暂存清理机制仅适用于点播（VOD），不适用于已录制的直播内容。实时直播混流会直接在服务下载目录中写入持续增长的可见文件；分轨录制的直播则使用可见的 `<name>.recording/` 目录。按返回（Back）会正常完成并提交录制文件，再清理被替代的分轨；按退出（Quit）或收尾失败时会保留已写入的录制文件，不再启动额外后处理。

Core 会按需创建目录，并在操作系统支持的平台上自动为凭据、设备文件、密钥库与日志目录应用仅当前用户可访问的权限。

## CDM 配置项

`cdm.devices` 中的每一项将一个易读名称映射到 `.wvd`、`.prd` 或 `.mld` 文件。文件后缀直接决定其所属 DRM 系统；如果系统不匹配会被明确拒绝，不会静默回退到其他设备。MonaLisa 设备还需要其 `.mld` 文件所声明的 wasm 模块。

TUI 的 **DRM 与密钥库**管理器会自动扫描 `paths.cdm` 以及配置的搜索路径中的设备。各服务可以单独提供专属 CDM 设置，该配置与全局默认 CDM 以及输出轨道设置彼此独立。

远程 CDM 在 `remote_cdm` 下声明，并按名称选用。远程端点仅接收其对应 DRM 系统支持的质询（challenge）与解析操作。请务必使用 HTTPS，并将访问令牌保存在私有配置文件中。

## 密钥库配置项

可在 `key_vaults` 下声明一个或多个本地及远程密钥库。密钥库管理器支持启停切换、读写策略、服务过滤以及手动录入 `KID:key`。这些资源均可直接在 TUI 中编辑，无需手改 YAML。请妥善保管密钥库令牌与内容密钥。

配置 HTTP 密钥库时，请保留服务端要求的末尾斜杠，因为部分路由会区分 `/vault` 与 `/vault/`。`supported_services` 列表是用于主页远程搜索的显式能力声明。自动查库与入库直接使用当前活跃服务 ID，而 `service_map` 别名映射仅作用于显式搜索。

## 服务专属画质设置

公开示例服务将 `manifest_profile`（`hd` 或 `uhd`）与 `license_profile` 独立暴露。服务实现也可以独立声明编码、加密方式或码率自适应相关的源站选项。通用的 `video_quality` 设置仅在返回 manifest 解析完成后生效，不会决定服务向源站请求哪一种 manifest profile。

## 凭据与 Cookie

凭据槽位由各服务自行声明，存储在对应服务的命名空间下，绝不会隐式跨服务共享。Cookie 配置按名称选取，且仅从 `<paths.cookies>/<service>/` 读取。

真实凭据请放入私有覆盖配置中使用：

~~~console
python -m unidl --config ./unidl.private.yaml
~~~

切勿将账号凭据、刷新令牌、Cookie、CDM 设备文件、内容密钥、签名 URL 或远程密钥库/CDM 的 Bearer Token 提交到版本库。
