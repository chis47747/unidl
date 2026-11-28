# 轨道选择

[English](../track-selection.md) | [简体中文](track-selection.md)

## 内嵌 HLS 隐藏式字幕（Closed Captions）

主播放列表（master playlist）可能会使用 `TYPE=CLOSED-CAPTIONS` 搭配 `INSTREAM-ID` 声明字幕，而不是提供单独的字幕 URL。这类声明会在轨道选择器中显示为 `CC · Embedded` 字幕，并附带声明的语言和通道信息。它们沿用现有的字幕语言/类型筛选和默认字幕偏好设置。列出轨道时不会去探测视频分片来发现这些字幕。

对于点播（VOD），选择此类字幕需要依赖对应的视频载体：UniDL 会复用同一主列表/字幕组中已选中的视频轨，或将该组带宽最低的规格作为临时依赖。即使同时选中视频和隐藏式字幕，视频也只会下载和解密一次。仅输出字幕（Subtitle-only）时，只会保留请求的字幕文件，不会保留临时视频。载体视频所需的任何密钥都必须通过常规的已选轨道流程提供或申请许可证；这种依赖绝不会为整套码率阶梯申请许可证。

CEA-608 的 `CC1` 和 `CC3` 通过 FFmpeg 和 ffprobe 读取 MP4 中的 `c608` 轨道。分段 MP4 提取时只会把字幕样本复制到小型临时容器中，保留原有时间轴并标准化全零的空白 `c608` 占位符，既不解码视频，也不会为了字幕转换分配与视频等大的副本。提取完成后继续应用常规的字幕格式设置。启用 **Download behavior → Muxer → No mux**（不混流）时，CEA-608 原始字幕会保留为仅含字幕的 MOV 文件，而不会转为 SRT；该文件保留原始 `c608` 轨道且可能同时包含两个数据场（data fields），并非解码后的单通道文本字幕。引用导出和媒体播放列表导出都会保留字幕声明及其视频载体关系，且不会展开分片列表。

如果不存在独立的 `c608` 轨道，UniDL 可以使用支持 RCWT 输入的 FFmpeg，直接从 AVC/HEVC 视频 SEI 中提取文本形式的 `CC1`。该过程会精确分离第一场通道 1（field-one channel 1），不会回退到 `CC2` 或 CEA-708 服务，也不解码视频帧。原始 `CC1` 和 `CC3` 仍继续走原有的 `c608` 路径。

对于视频 SEI 中包含 CEA-708 字幕的 AVC/HEVC 点播内容，请将 **CCExtractor** 安装到 `PATH`（或 UniDL 的 `binaries` 目录）中。UniDL 会对选中视频执行单次扫描，仅将带时间戳的字幕数据传给 CCExtractor，并精确提取所选的 `SERVICEn`。字幕格式以及内封/外挂输出设置依然生效，且不会对视频重编码或复制。该路径不支持直播录制、保留原始轨道的 CEA-708 输出或独立的 `c708` 轨道。缺少工具时，CEA-708 轨道行会被禁用；如果所选服务未产出任何字幕，UniDL 会上报该情况并保留其他成功完成的轨道。仅下载字幕的任务若没有任何有效的已选服务，则会直接报错，不会写出空文件或擅自替换为其他通道。

内嵌字幕行会依次显示 **Waiting for video**、**Extracting captions**，最终变为 **Caption ready** 或 **Caption skipped**。视频传输进度只统计一次；字幕本身没有独立的网络下载百分比或字节计数器。

UniDL 为所有服务提供一套共享的输出轨道选择器。它在服务完成授权并解析 manifest 之后（以及添加完任何服务自有的外挂字幕行之后）运行，决定将哪些已解析的视频、音频和字幕轨道移交给原生下载器。

本文档仅说明输出轨道选择，不涉及 DRM、许可证或平台 manifest 配置档的选择。

## 两个相互独立的决策

在服务的 **Tracks and output** 设置中，存在两项彼此独立的决策：

1. **获取哪份 manifest 配置档** —— 服务专属的 API 选项，例如源站分辨率、编码阶梯、色彩配置、设备配置或播放接口。
2. **从该 manifest 中挑选哪些轨道** —— 即本文档介绍的共享设置。

第二项决策不会重新请求新的 manifest。例如，设置 `video_quality=1080` 会在已返回的码率阶梯中挑选 1080p 规格，而不会让服务再去拉取一份 1080p manifest。各服务必须将其平台/API/配置档设置与这里的输出设置分开。

### 同排名轨道处理

Best（最佳）选择模式在每个请求的分组内仅保留一条轨道，即使有多条规格参数完全相同也是如此。排名完全并列时，保留 manifest 顺序中的第一条。轨道选择器和轨道预览会保持这一单选结果，不会把所有元数据一致的行都勾选上。重建轨道时执行一对一匹配，因此刷新播放源 URL 后不会意外扩大选择范围。

这条规则不会从阶梯中移除轨道。All（全部匹配）模式仍会选中所有符合条件的规格，用户也可以手动勾选多条等效轨道。同时请求多种音频/字幕语言或多个视频配置时，各自原有的分组依然保留；该规则不会把整个下载强行缩减为每种媒体类型各一条轨道。

## 输出范围

`output_scope` 控制哪些媒体类型参与自动选择：

| 取值 | 含义 |
|---|---|
| `package` | 常规打包输出（Normal package）：当对应筛选器选中时包含视频、音频和字幕。这是兼容默认值。 |
| `video` | 仅选择视频轨道。 |
| `audio` | 仅选择音频轨道。 |
| `subtitle` | 仅选择字幕轨道。 |
| `custom` | 读取 `output_types`，即由逗号分隔的 `video`、`audio`、`subtitle` 组合。 |

输出范围同时影响自动预选和交付行为。收窄后的范围会禁用自动混流，使多条已选轨道各自保存为带有轨道标识名称的独立文件。包含两种或三种媒体类型的 `custom` 范围同样保持独立文件输出；如需输出常规的合并容器文件，请使用 Normal package（`package`）。

示例：

```yaml
track_mode: auto
output_scope: subtitle
sub_langs: en
subtitle_kinds: normal,forced
subtitle_selection: all
```

```yaml
track_mode: auto
output_scope: custom
output_types: video,audio
video_selection: best
audio_selection: all
```

## 独立保存原始轨道（不混流 / No mux）

选择 **Settings → Download behavior → Muxer → No mux (original tracks)**（对应配置 `muxer: none`）。这是一项输出行为而非筛选过滤条件：原有的自动或手动轨道选择规则仍照常作用于每个剧目，包括批量任务。

UniDL 会正常下载、拼接分片并解密每一条选中的轨道，随后将这些独立完成的文件以带轨道信息的名称保存在常规下载目录中，不生成最终合并的 MKV/MP4。音频保留原有编码，字幕保留原有格式与样式，服务 API 字幕引用也会直接下载，不再经过服务自带的字幕转换逻辑；已准备好的本地外挂文件则原样复制。

该模式会覆盖 **Container**（容器）、**Audio-only format**（纯音频格式）、**Subtitle format**（字幕格式）、字幕清理、可选重封装/Audio Vivid 解码以及 Dolby Vision Hybrid 合成设置。由于不存在最终合并容器，默认音轨/字幕标志以及章节/歌词内嵌也不再生效。元数据仍会保留在报告与导出中；附件下载仍遵循其独立设置。

这不同于 **Keep temp files**（保留临时文件）：输出的明文轨道属于最终成品，而非加密中间文件或零散分片，常规缓存清理不会将其删除。源视频中原本内嵌的音频通常会一并保留，No mux 本身不会强制拆分音视频。如果某服务显式启用了 Core 的混流音视频分离能力，轨道选择器会列出独立的视频和音频条目，No mux 则无损保存选中的轨道，不进行转码。

分离出的音频行会标明其所属的原始视频规格。同一物理源派生出的两条投影共享一次下载；仅选择音频时依然需要拉取原始混流分片，因此不一定减少网络流量。不同视频规格不会默认被假设包含相同的音频。该策略由服务控制，默认关闭，并在保存的命令和导出中保留。

当服务提供 `extra["audio_language"]`（优先）或源轨道的 `language` 时，Core 会将该值带入投影出的音频行及独立提取出的文件中。MP4/M4A 输出使用容器对应的 ISO-639-2 代码（例如 `ja` 转为 `jpn`）；MKA 则保留标准化后的 BCP-47 形式。缺失或无法识别的语言会记为 `und`，绝不会根据编码格式、标题或 URL 猜测。服务自身负责在进入 Core 投影前提供可靠的语言信息。

对于直播流，跨轨道管道混流会被禁用，但单轨道的实时分片拼接仍可用。按 Back 依然会正常收尾录制并保存各条独立轨道；按 Quit 则保持原有的立即退出行为。

对应的独立 CLI 用法如下：

```sh
unidl download 'MANIFEST_URL' --muxer none --select 1,3,8
```

保存的命令会自动包含 `--muxer none`。旧版 `--no-mux` 参数仅抑制最终混流且仍执行已配置的转换操作，其行为保持不变。默认配置 `muxer: auto` 也不受影响。

## 视频轨道选择

视频约束条件优先执行：

- `video_quality`：`best`、`worst`，或目标高度（如 `1080`）；
- `video_codec`：`h264`、`h265`、`h266`/VVC、`av1`、`vp9` 或 `any`；
- `video_range`：`sdr`、`hdr10`、`hdr10+`、`hdrvivid`、`hlg`、`dv` 或 `any`；
  HDR Vivid 仅匹配显式标注的轨道，并在发布命名中使用 `HDRVivid`；显式标注为 EDR 的视频在发布命名中保留 `EDR`。未能识别编码格式的内嵌音频会从文件名的音频段中省略；仅存在混流音频标记并不意味着它是 AAC。
- `drop_video`：带边界感知能力的过滤模式，用于剔除快进预览（trick-play）和缩略图阶梯。

`video_selection=best` 保留符合条件的最高排名规格。`video_selection=all` 保留所有通过筛选的规格。如果指定了目标高度，All 表示该高度下所有符合条件的规格，而非 manifest 中的所有高度。

## 音频轨道选择

音频筛选条件以 **AND**（逻辑与）组合：

- `audio_langs` —— 逗号分隔的 BCP-47/ISO 语言标签，以及/或 `original`；
- `audio_codec` —— 仅指音频编码，包括 AAC、AC-3、E-AC-3、**AC-4**、Opus、FLAC、ALAC 和 MP3；
- `audio_profile` —— 角色或特性，如 Atmos、main、commentary、description 或 dialog；
- `audio_channels` —— any、立体声（`2`）、5.1（`6`）或 7.1（`8`）。

编码（codec）与特性（profile）是两个独立字段。`audio_codec=eac3` 匹配所有 E-AC-3 音频，不论是否带有 Atmos；`audio_profile=atmos` 匹配所有带 Atmos/JOC 信号的音轨，不论其编码标签为何。旧版写法 `audio_codec=atmos` 仍作为 Atmos 特性的别名被兼容接受。`audio_codec=ac4` 匹配原始的 `ac-4`、`ac4` 和 `dac4` 编码声明。

对于 Smooth Streaming（ISM），显式声明了 `HasAtmos="true"` 或 `HasAtmos="1"` 的 E-AC-3 音频会显示为 **E-AC-3 Atmos** 并匹配 Atmos 特性。QualityLevel 级别的声明优先于 StreamIndex 默认值，包括显式的 `false`/`0`。该标记同时参与发布命名，并在重放原生 manifest 源时得以保留。仅靠 448 Kbps 码率或六声道布局不足以作为 Atmos 的证据。该元数据直接从 manifest 读取，无需请求媒体分片。

`audio_quality=best` 或 `worst` 只对经过上述所有筛选条件后剩余的轨道进行排序，绝不会放宽语言、编码、特性或声道限制。

`audio_profile=dialog` 指普通对白音轨，不包含 Voice Booster、Boosted Dialog 或增强对白（enhanced-dialogue）混音。在 Best、Worst 及目标码率选择中，UniDL 会在每个已筛选的语言组内先优先挑选普通音轨，再按音质排序。解说（commentary）、口述影像（audio description）和增强对白轨道不会仅仅因为排在前面或码率更高就顶替可用的普通混音。如果筛选后没有任何普通混音，选择 Any 特性仍可回退选用替代混音；但显式指定 Dialog 特性则会报告无匹配，并将音频留空供手动选择。

`audio_selection=best` 为每种语言保留一条最佳匹配轨道。语言列表留空时，遵循该服务默认的最佳语言行为；显式指定语言列表时，选择范围严格限定在该列表内。`audio_selection=all` 保留所有符合条件的轨道，包括重复规格、解说轨和不同声道布局。搭配 Any 特性时，All 也会保留 Voice Booster 等增强混音。这些轨道在手动选择器中始终可见，也可通过显式的名称/分组/角色过滤器定向选取。

例如，`audio_codec=ac4`、`audio_langs=en`、`audio_selection=best` 并不会选中所有英语音轨，而是先按编码筛选，再挑选最佳的英语 AC-4 音轨，不会拿英语 AAC 音轨作为回退。

语言匹配具有方向性。主语言标签（如 `es`）可匹配 `es`、`es-419` 和 `es-ES`；而显式变体标签（如 `es-419`）仅匹配这一完整标签，不匹配 `es-ES` 或未指明地区的 `es`。文字代码与平台后缀遵循同一规则：`zh-Hant` 不会匹配 `zh-Hans`。常见的 ISO-639 别名、大小写及下划线会做标准化处理且不丢弃后缀（如 `SPA_419` 与 `es-419` 等价）。除非显式请求 `und`，否则未知语言 `und` 轨道不会被猜测为用户语言。

使用 `audio_langs=es-419,es-ES` 搭配 Best 模式时，两个显式请求的变体会分开排序，两者均存在时都会保留。若只填写 `es`，Best 则会在所有符合条件的西班牙语变体中只选一条。缺失的显式变体不会回退到其他地区，而是触发常规的无匹配处理逻辑。以上规则同样适用于字幕语言。

### 选择原声（Original）音频与字幕

**Audio languages**（`audio_langs`）和 **Subtitle languages**（`sub_langs`）均支持 `original`，既可单独使用，也可与语言标签混用：

| 取值 | 符合条件的轨道 |
| --- | --- |
| `original` | 仅包含平台/服务显式标记为 Original 的轨道。 |
| `original,en` | 同时包含原声/原版轨道**和**英语轨道。如果原语言就是英语，Best 模式对该语言组只选一次。 |
| `en,de,it` | 同时包含英语、德语**和**意大利语轨道，而非只取第一个匹配上的语言。 |

逗号分隔的条目是并集累加关系，不是按顺序回退的优先级列表。例如，如果作品原语言是日语，`original,en` 会同时选中日语原声轨道和英语轨道；如果原语言本身是英语，重叠请求不会产生第二条英语选择。调整条目顺序不会排除任何已请求的语言。

`original` 令牌不区分大小写，并复用 UniDL 现有的原版轨道标记，包括显式标志、Original 角色以及声明的原语言元数据。它不会根据作品所属国家、界面语言、轨道顺序或源站默认标志去推断原语言。即使轨道的语言标签为 `und`，只要带有原版标记也可被匹配。

语言/原版匹配会在该字段内合并所有请求的分组，绝不会在命中第一组后停止。其他所有筛选条件仍以 **AND** 叠加生效：原声音频必须同时满足编码、特性和声道要求；原版字幕必须同时满足字幕类型筛选。质量排序仅在满足上述全部约束之后运行。

Best 模式在每个语言组内选出一条符合条件的轨道；不同语言或变体的原版轨道会按其完整的标准化标签维持各自独立分组。未设置语言筛选时的字幕 Best 模式也采用这套完整标签分组规则。若一条原版轨道同时命中了列表中显式指定的语言，该语言组只排序一次，不会按令牌重复计算。All 模式保留所有符合条件的规格。例如，`original,en` 不会仅仅因为英语轨道带原版标记就额外多选一条排名更低的英语规格。

如果清单中没有任何原版标记，`original` 将匹配不到任何轨道，UniDL 不会静默选中所有语言。像 `original,en` 这样的混合列表在没有原版标记时仍可正常匹配英语：它是并集，不要求每个令牌都必须找到轨道。如果整条规则都无匹配结果，常规的无匹配处理会在选择器中取消勾选该媒体类型以供手动调整；自动批量任务则沿用既有的跳过并记录行为。

示例：下载原声音频和英语字幕，同时在有原版字幕时一并保留：

```yaml
audio_langs: original
audio_selection: best
sub_langs: original,en
subtitle_kinds: normal,forced
subtitle_selection: best
```

若要批量只下原版字幕，可组合 `output_scope: subtitle` 与 `sub_langs: original`。独立 API/外挂字幕以及原生导入的 manifest 只要携带原版标记，同样适用该规则。

对应的 CLI 参数为 `--audio-lang original` 和 `--sub-lang original,en`；过滤表达式同样接受 `--select-audio lang=original:for=best` 与 `--select-subtitle lang=original,en:for=best`。

以上属于**下载选择**规则。`default_audio=original` 和 `default_subtitle=original` 与此独立：它们只决定在已经选中的轨道中把哪一条设为默认播放，并不会去请求或新增原版轨道。

### 轨道列表中的语言、国家与地区名称

音频和字幕行会保留原始语言标签，并在其后附加完整的英文语言名称，例如 `fr | French`、`it | Italian` 或 `nl | Dutch`。若显式声明了国家或地区，则以括号跟在后面：`en-US | English (United States)`、`es-ES | Spanish (Spain)` 或 `pt-BR | Portuguese (Brazil)`。名称源自本地 ISO-639 与国家/地区数据，展示时不发起任何网络请求。支持识别两字母、三字母及书目型（bibliographic）ISO 语言别名；无法识别的标签原样保留，不强行赋予名称。

不带地区代码的标签（如 `en`、`und`、`fil`）不会隐含任何国家。当 manifest 将语言简写为 `en` 但在轨道元数据（如 `en-us_dialog_0`）中保留了显式区域信息时，UniDL 可显示 `United States` 而不改动原本的 `en` 标签。该回退逻辑仅接受位于轨道标识符/名称开头、且主语言与轨道一致的区域标签；回退地区发生冲突时留空不标；语言标签里显式声明的地区始终优先。URL 和自由文本中的国家提及不会被扫描解析。`es-419` 显示为 `Spanish (Latin America and the Caribbean)`，不会指定为某一个具体国家。如果平台标签与计算出的语言名称完全一致，则不重复显示；像 `English SDH` 这类包含角色信息的名称则会保留。这些名称仅反映轨道元数据所声明的地区，不代表经核实的录制地，并且仅用于界面展示：语言匹配、丢弃过滤器、混流语言元数据、导出语言标签和文件名均保持不变。

## 混流文件中的默认音频

**Audio languages** 决定下载哪些音轨，而 **Default audio language**（`default_audio`）决定在最终选中的音轨中为哪一条设置默认播放标志。它绝不会新增轨道、放宽筛选条件、改变许可证选择，也不会改变已下载轨道的排列顺序。

该设置按服务独立配置，位于 Audio languages 正下方。可选择 Auto、Original，或指定单个语言标签：

| 取值 | 行为结果 |
|---|---|
| `auto`（默认） | 优先选择普通/主音轨（ordinary/main），其次选择源站标记的默认音轨，再次选择显式标记的原声音轨。若都没有这些标记，则保持第一条选中的语言。 |
| `original` | 优先选择显式标记为原声的已选音轨。不会根据服务所属国家或界面语言推断原语言。 |
| 单个语言标签（如 `en`、`fr-CA`、`es-419`、`und` 或平台标签） | 优先选择匹配该语言的已选音轨。接受 ISO 别名及大小写/下划线标准化。主标签（如 `en`）涵盖地区变体；地区标签（如 `en-GB`）要求精确匹配该地区，不会静默匹配为 `en-US`。 |

在匹配的候选轨道中，普通/主音轨优先于解说、口述影像以及 Voice Booster/增强对白混音。如果只选中了替代混音，它们依然具备候选资格。同一优先级下的多个候选遵循现有的音频 Best 排序规则；完全并列时保持已选轨道的稳定顺序。

如果请求的语言或原声标记在已选轨道中不存在，UniDL 会报告警告并回退至 Auto，既不会额外拉取轨道，也不会取消任务。仅选中一条音轨时，该音轨自动成为默认音轨。无音频、独立文件输出或未混流的纯音频交付场景下，该偏好不生效。

该规则在轨道选择器最终确认之后执行，并对批量任务中的每个剧目单独求值。对于 MKV/MP4 输出，UniDL 只会将一条音轨设为默认，并清除其他所有音轨（包括输入容器内嵌音频）的默认标志。视频和字幕的默认标志各自独立。直播管道混流同样遵循该偏好。MPEG-TS 容器无法可靠保留默认音频标志，UniDL 会明确提示该限制，而不会承诺设置默认播放语言。

下载详情和日志会显示解析后的默认音轨及任何回退情况。原生导出会保存 `default_audio`，导入时导出文件中的值优先于当前服务设置；旧版导出文件则使用当前服务设置，若无则回退为 Auto。保存的命令会包含 `--default-audio`，确保重放时保留该偏好。

示例：下载英语和法语音轨，并将法语设为默认：

```yaml
audio_langs: en,fr
audio_selection: best
default_audio: fr
```

如需核验最终文件，可在 MediaInfo 中检查音轨的 `Default: Yes`，或运行：

```sh
ffprobe -v error -select_streams a \
  -show_entries stream=index:stream_tags=language,title:stream_disposition=default \
  -of json "file.mkv"
```

文件中的默认标志仅作为播放提示，播放器自身的首选语言设置可能会将其覆盖。

## 字幕轨道选择

`sub_langs` 用于选择最终下载的字幕语言；如前所述，支持语言标签、`original` 或混合列表（如 `original,en`）。`subtitle_kinds` 是逗号分隔的类型过滤器，可选值包括 `normal`、`forced`、`sdh`、`commentary`、`audio_description` 或 `all`。

`subtitle_selection=all` 保留所有同时通过语言和类型筛选的字幕。`subtitle_selection=best` 为每种语言保留一条首选字幕，综合考量 normal/forced/SDH 角色信息以及平台默认标记。

通过独立 API 获取字幕的服务应先经由 Core 的外挂轨道接口暴露完整的字幕清单，再进入选择流程。这些字幕行与 manifest 内字幕使用完全相同的语言和类型筛选器。

## 混流文件中的默认字幕

**Subtitle languages** 决定下载哪些字幕轨，**Default subtitle language**（`default_subtitle`）决定在最终选中的字幕轨中为哪一条设置容器的默认播放标志。它绝不会新增轨道或放宽字幕筛选条件。

可选值包括 `Auto`、`Original` 或单个语言标签（如 `en`、`fr-CA`、`es-419`）：

| 取值 | 行为结果 |
| --- | --- |
| `auto`（默认） | 优先选择源站标记的默认字幕，其次选择显式原版字幕。若仍有多个候选，普通字幕（normal）优先于强制字幕（forced）/SDH。 |
| `original` | 在已选字幕中选用显式标记为原版的字幕。若没有原版标记，则不设置默认字幕并显示警告。 |
| 单个语言标签 | 仅在已选字幕中匹配该语言。主标签（如 `en`）接受地区变体；地区标签要求精确匹配该地区。若未命中，UniDL 会回退到已选的原语言字幕；若原语言字幕也缺失，则不设置默认字幕。 |

`forced`、`sdh`、`commentary` 和 `audio_description` 属于独立的字幕类型标志。当强制字幕或 SDH 字幕是唯一匹配项时可以成为默认字幕，但不会仅仅因为它是强制字幕就自动成为默认。该设置同样适用于通过 `prepare_download()` 添加的服务外挂字幕。

结果在轨道选择器最终确认后针对每个剧目分别求值，并以 `default_subtitle` 存入保存的命令和原生导出中；旧版导出使用当前服务设置或 `Auto`。未选中任何字幕时，该设置不生效。默认标志仅为播放器提示，可能被播放器自身的字幕语言偏好覆盖。MKV 对字幕 disposition 标志的支持最可靠；MP4 可能存在容器或播放器相关的限制。

## 字幕格式与定位

**Tracks and output → Subtitle format** 独立于字幕语言和类型进行选择，默认格式仍为 SRT。

| 格式 | 定位与样式支持 |
| --- | --- |
| SRT | 保留基础行内强调样式，并为显式定位在屏幕上方的字幕条添加 `{\an8}`。许多播放器支持该扩展，但部分会忽略。无法表达精确坐标、区域和 CSS。 |
| WebVTT | 保留原生 cue 设置（如 `line`、`position`、`align`、`size`、`vertical` 和 `region`），以及 STYLE/REGION 块和 cue 标记。需要播放器自身支持。 |
| ASS | 将常见水平定位映射到 1920×1080 画布，包括顶部/底部对齐、显式坐标以及基础粗体/斜体/下划线/颜色。请配合 MKV 容器以保留这些指令。 |
| Original | 原样保留下载的字幕文件。要求目标容器和播放器支持其原始格式。 |

如果需要在画面演职员表上方准确显示对白，请选择 **ASS** 搭配 **MKV**。UniDL 会按每条字幕自身的定位信息处理，不会把所有字幕都移到顶部。对应的保存命令参数为 `--sub-format ass --mux-format mkv`。

共享转换器会从纯文本 WebVTT、MP4 WebVTT `sttg` box，以及常见的 TTML 区域、继承样式、`origin`、`extent`、`displayAlign` 和 `textAlign` 中读取定位信息。时间轴修正、裁剪、去重和增量字幕修复都会保留这些定位数据；文字相同但位置不同的字幕会被保留为独立条目。

ASS 映射会近似还原 WebVTT 的 snap-to-line 位置以及常见水平 TTML 区域；任意 CSS、竖排文字、动画、注音（ruby）以及完整的 TTML 排版特性无法完整复现。当完整排版很关键且播放器支持时，建议直接使用原生 WebVTT。外部 TTML/XML 外挂字幕在封装进 MKV 时会被转为 ASS，因为混流器无法直接读取它们。

MP4 无法直接复制封装 ASS、WebVTT 或 SRT 轨道，因此 UniDL 会将其转为 `mov_text` 封装，这可能导致定位和样式丢失。如需保留带定位的字幕，请使用 MKV，或在需要时单独保留原始外挂字幕。

## 优先级排序、多语言视频与严格必选校验

**轨道与输出** 下另有六项进阶规则用于细化自动选轨与缺失校验：

- **`audio_codec_priority`** — 可选的逗号分隔音频编码优先级顺序（如 `atmos,eac3,ac3,aac`），在 `audio_selection=best` 时决定候选音轨的先后排名。
- **`subtitle_type_priority`** — 可选的逗号分隔字幕类型优先级顺序（如 `normal,forced,sdh`），在 `subtitle_selection=best` 时决定候选字幕的先后排名。
- **`merge_video`** — 开启后，若存在分辨率、动态范围与编码相同但语言不同的多个视频变体，会将它们一并合并封装，而不丢弃其他语言画面版本。
- **`require_video` / `require_audio` / `require_subtitles`** — 严格必选校验开关（默认关闭）。开启后，若请求的画质/语言、音轨语言或字幕语言/类型缺失，任务会直接判定失败（在定时下载中则继续在重试窗口内等待），不会静默降级回退。

## HDR 码流探测、VUI 规范化与 HDR10+ 转杜比视界

位于 **设置 → 下载行为**（以及 **服务 → 杜比视界混合输出**）：

- **`probe_hdr_metadata`** — 在选轨前抽样检测明文 HEVC HDR 码流中的 SMPTE ST 2094-40 HDR10+ SEI 与 Dolby Vision RPU NAL 单元（自动剥离防竞争字节），当 manifest 漏标动态范围时自动修正标签。
- **`dolby_vision_hybrid`** — 使用 `dovi_tool`（mode 3）将选中的 HDR10/HDR10+ 基础层与可用最低分辨率 DV 层合成为混合视频，并自动执行有界 `ffmpeg cropdetect` 计算 L5 黑边裁切以及通过 `--edit-config` 写入基础层的 L6 `MaxCLL`/`MaxFALL` 亮度元数据。
- **`generate_dv_from_hdr10p`** — 当 HDR10+ 视频缺少匹配的 DV 层时，借助 `hdr10plus_tool`（`UNIDL_HDR10PLUS_TOOL`）提取动态元数据并由 `dovi_tool` 合成 Dolby Vision Profile 8.1 RPU。
- **`normalize_vui`** — 通过 FFmpeg 码流过滤器无损修补已下载 HDR 视频中缺失或不匹配的 BT.2020 PQ/HLG VUI 色彩标记。

## 交互模式、自动模式与批量行为

- `track_mode=interactive` 会打开轨道选择器并预勾选自动选择的结果；用户可自由修改并手动勾选任意可见行。
- `track_mode=auto` 不打开选择器，直接采纳自动选择结果。
- 单个剧目在自动模式下无匹配时，会打开常规选择器供处理，而不会静默放宽筛选规则。
- 在自动批量任务中，无匹配轨道的剧目会被记为跳过，并继续处理下一剧目；批量流程绝不会因为无法跨集复用选择结果而停在选择器界面。
- `track_mode=list` 仅输出轨道清单及未匹配原因，不申请许可证也不执行下载。

聚焦型的输出范围（Focused output scopes）不会把缺失的可选轨道变成整包下载。如果某个请求的媒体类型没有匹配轨道，UniDL 会报告该情况、让该类型保持未勾选状态，同时保留其他已匹配类型的预选状态供手动确认；绝不会擅自用无关的编码、语言或字幕类型替代。

## DRM 与许可证边界

输出轨道选择有意与服务的 DRM 流程解耦。共享轨道设置不得改变服务的 PSSH、许可证配置、设备/API 选择或许可证轨道计划。由服务管理的 DRM 路径按自身契约解析所需密钥；随后 Core 使用选中的轨道对象执行交付。

可选的 **License after final track selection**（在最终轨道选择后申请许可证）兼容模式仅改变 Core 解析已选轨道初始化数据的时机，并不会把输出设置变成平台 API 选择器。

## 命令与导出

保存的命令包含选中的轨道索引以及人类可读的选择注释。原生导出还会一并存储输出范围、输出类型以及 Best/All 规则。重新运行保存的命令时，会精确使用其中的轨道索引。导入原生导出文件时，会在自动选择之前先还原其存储的输出范围；manifest 和密钥始终以导出文件为准。
