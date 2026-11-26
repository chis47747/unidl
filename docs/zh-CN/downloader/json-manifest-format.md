# UniDL JSON Manifest 格式

[English](../../downloader/json-manifest-format.md) | [简体中文](json-manifest-format.md)

本文档定义 `UniDL` 用于直链轨道列表的稳定 JSON 输入格式。

## 根节点

根节点可以是单个标题对象，也可以是标题对象数组。

```json
[
  {
    "id": "title-id",
    "type": "movie",
    "name": "Title Name",
    "season": 1,
    "episode": 2,
    "episode_name": "Episode Name",
    "original_language": "en",
    "duration_ms": 3600000,
    "video_tracks": [],
    "audio_tracks": [],
    "subtitle_tracks": []
  }
]
```

## 轨道列表

使用以下数组字段：

- `video_tracks`
- `audio_tracks`
- `subtitle_tracks`

为保持兼容，同时接受以下别名：

- 视频：`videos`、`video`
- 音频：`audios`、`audio`
- 字幕：`subtitles`、`text_tracks`、`texts`

## 通用轨道字段

```json
{
  "id": "track-id",
  "url": "https://cdn.example/media.mp4",
  "all_urls": [
    "https://cdn-a.example/media.mp4",
    "https://cdn-b.example/media.mp4"
  ],
  "codec": "avc1.640028",
  "mime_type": "video/mp4",
  "bitrate": 5000000,
  "duration": 3600.0,
  "duration_ms": 3600000,
  "size_bytes": 2250000000,
  "encrypted": true,
  "encryption_scheme": "CENC",
  "kid": "00112233445566778899aabbccddeeff"
}
```

支持的别名：

- URL：`url`、`uri`、`href`
- 备用 URL：`all_urls`、`urls`
- 编码：`codec`、`codecs`、`format`
- 文件大小：`size`、`size_bytes`、`sizeBytes`、`content_length`、`contentLength`、`clen`
- 时长（秒）：`duration`、`duration_seconds`、`durationSeconds`
- 时长（毫秒）：`duration_ms`、`durationMs`
- KID：`kid`、`key_id`、`keyId`
- 加密方案：`encryption_scheme`、`encryptionScheme`、`scheme`
- 扩展名：`extension`、`ext`
- MIME：`mime_type`、`mimeType`

如果 `url` 中带有 `clen=...` 或 `dur=...` 等查询参数，UniDL 也会将其用作文件大小和时长提示。

## 视频轨道字段

```json
{
  "id": "video-1",
  "url": "https://cdn.example/video.mp4",
  "codec": "hvc1.2.4.L150.90",
  "mime_type": "video/mp4",
  "width": 1920,
  "height": 1080,
  "fps": 23.976,
  "bitrate": 8000000,
  "hdr": "HDR10",
  "supplemental_codecs": "dvh1.08.06"
}
```

支持的视频别名：

- 帧率：`fps`、`frame_rate`、`frameRate`
- 动态范围：`hdr`、`video_range`、`videoRange`、`range`、`dynamic_range`
- Dolby Vision 补充编码：`supplemental_codecs`、`supplementalCodecs`

可识别的动态范围包括 `SDR`、`HDR10`、`HDR10+`、`HLG`、`DV` 和 `DV+HDR10`。

## 音频轨道字段

标题级 ID3 元数据、MP3 导出行为及封面导入规则详见 [`mp3-audio-format.md`](mp3-audio-format.md)。

```json
{
  "id": "audio-1",
  "url": "https://cdn.example/audio.mp4",
  "codec": "ec-3",
  "mime_type": "audio/mp4",
  "language": "en",
  "channels": "6",
  "bitrate": 448000,
  "is_original": true,
  "descriptive": false
}
```

支持的音频别名：

- 语言：`language`、`lang`、`locale`
- 原声标记：`is_original`、`original`
- 口述影像/描述性音轨标记：`descriptive`、`description`

## 字幕轨道字段

```json
{
  "id": "sub-1",
  "url": "https://cdn.example/subtitles.vtt",
  "codec": "webvtt",
  "language": "en",
  "forced": false,
  "sdh": true
}
```

支持的字幕编码包括 WebVTT、TTML/DFXP 和 SRT。

## 旧版显式分片（只读兼容）

已有的分片 JSON 输入仍然可以读取。新生成的 manifest 与导出文件必须使用原始 manifest/播放列表来源，不得再采用此结构。下面仅是旧版输入示例，而非当前导出格式。

```json
{
  "id": "video-segmented",
  "url": "https://cdn.example/base/",
  "codec": "avc1.640028",
  "segments": [
    {
      "url": "init.mp4",
      "index": -1,
      "byte_range": [0, 999]
    },
    {
      "url": "seg-1.m4s",
      "index": 1,
      "duration": 2.0,
      "byte_range": "1000-1999"
    }
  ]
}
```

分片字段：

- `url`、`uri`、`href`
- `index`；初始化分片（init segment）使用 `-1`
- `duration`
- `byte_range`、`byteRange` 或 `range`；支持 `[start, end]`、`{"start": 0, "end": 999}` 或 `"0-999"`
- `encrypted`、`encryption_scheme`、`kid`
- 用于 HLS 媒体加密的 `key_uri`、`key_iv`（`key_iv` 为十六进制）
- `data_base64`：用于合成或内联的初始化分片，包括 Smooth Streaming 的初始化数据
- `allow_range_status_200`
- `program_date_time`、`gap`、`discontinuity_after`
- `timeline_time`、`timeline_presentation_time`

相对分片 URL 相对于所属轨道的 URL 解析。

早期 UniDL 原生的 **all media manifests** 导出使用私有根标记 `_unidl_media_manifest: 1` 以及逐轨 `manifest_type` 字段，以还原精确的 HLS、DASH 或 ISM 分片语义。其他 JSON manifest 无需该标记，继续沿用上述通用 JSON/直链行为。

### 旧版紧凑分片表

已有的 format-2 原生导出可能将轨道的 `segments` 表示为对象而非列表。这只是存储形态的变化：解析器会在下载前完整还原每个原始分片，既不会重新拉取主 manifest，也不会合并字节范围。例如，来自同一 URL 的三个相邻字节范围可存储为：

```json
{
  "segments": {
    "encoding": "unidl-columns-v1",
    "count": 3,
    "defaults": {"url": "https://cdn.example/audio.mp4", "duration": 2.0},
    "columns": {
      "index": {"start": 1, "step": 1},
      "byte_range": {"range_start": 100, "lengths": [500, 600, 550]}
    }
  }
}
```

`defaults` 存放所有分片取值完全一致的字段。每个变化字段对应一列，采用以下精确表示之一：

- `values`：每个分片一个 JSON 值；
- `start` 与 `step`：等差整数序列，可选附加 `overrides: [[index, value], ...]` 处理诸如初始化分片索引等例外值；
- `runs: [[count, value], ...]`：连续重复游程编码；
- `range_start` 与 `lengths`：闭区间字节范围；可选的 `gaps` 列按同样的列规则描述非相邻范围之间的间隔。

每列可选的 `missing` 索引列表用于区分“字段缺失”与显式的 JSON `null`。内联初始化字节、分片密钥变更、IV、时间戳和不连续标记均保持精确值。长度异常、索引越界或未知编码都会直接报错，绝不回退为下载整个 BaseURL。原有的显式分片列表继续原样支持。当前没有任何写入器会生成这两种表示，仅保留其兼容解码器。

## 源集合与生成的 JSON 缓存

生成的临时 JSON 和 format-3 导出均保留原始来源：

```json
{
  "_unidl_manifest_sources": 1,
  "mode": "merge",
  "sources": [
    {"type": "reference", "url": "https://cdn.example/profile-a.mpd"},
    {"type": "reference", "url": "https://cdn.example/profile-b.mpd"}
  ]
}
```

来源记录支持 `reference`（原始 URL/路径）、`hls`、`dash` 或 `ism`（原始协议 `text` 及其 `base_url`）以及 `json`（原始源 `document`）。可选的请求头和解析器策略会随来源一并保留。HLS 媒体记录可携带描述性 `track` 元数据和主播放列表会话密钥信令；子播放列表的加密信令优先，明文轨道始终保持明文。生成媒体导出时不会拉取任何初始化分片、媒体分片或密钥字节。

`merge` 模式按常规轨道去重规则合并已授权的画质档位。`timeline` 模式依据原始序号和可选 `duration` 拼接重叠的来源时间窗。`collection` 模式保留各自独立的播放列表，不对不同版本（rendition）做去重。可选的根节点 `tracks` 行仅保存描述性修正及原声/默认标记，本身不是媒体来源。

### 规格来源信息与独立 API 字幕

每个来源可附带一个精简的 `metadata` 对象，包含公开的 `requested`、`returned`、`profile`、`kind`、`api`、`note` 和原始来源 `url` 字符串。只保留实际提供的字段；UniDL 不会从 URL 推断档位名称，也不会保存平台响应、客户端或授权上下文。描述性轨道行保留其来源关联及原生 JSON 的 `profile`/`kind` 标签。当多个档位中的相同轨道被去重合并时，所有来源标签都会保留。导入时不会重新发起授权请求。

在 MPD/HLS 文档之外单独下发的字幕会作为可执行来源保存在根节点 `subtitle_sources` 列表中，而不仅仅是供人阅读的描述行：

```json
{
  "url": "https://subtitles.example/en-sdh.vtt",
  "language": "en",
  "kind": "sdh",
  "format": "vtt",
  "name": "English SDH",
  "original": true,
  "selected": false,
  "headers": {"Referer": "https://provider.example/"},
  "sources": [{"requested": "HEVC HDR", "returned": "HEVC SDR"}]
}
```

来自已成功解析的授权变体的所有可用字幕语言和类型（包括未选中的）都会保留。请求头为可选项，只有显式指定的字幕专用请求头会覆盖刷新后的公共请求头。脱离服务直接导入时，这些 URL 会作为明文字幕轨道出现在常规选轨器中，并且仅下载选中的条目。已安装的服务可以通过 `augment_tracks()` 认领匹配行，继续走其自定义的 `prepare_download()` 转换/混流路径，而不会产生重复轨道。无论是来源元数据还是字幕来源，都不包含 JSON 分片列表。重新导出时仅更新选择状态标记，不会复制底层来源。

新写入的源文档中既不允许出现 JSON `segments` 数组，也不允许出现紧凑分片表。原始播放列表/MPD 自然保留各自的协议分片语法，由解析器在运行时展开。断点续传检查点独立保存，不属于可移植的导出内容。

生成的 JSON 缓存遵循 **32 MiB** UTF-8 大小上限和确定性编码。超过上限、缺少来源溯源信息、源版本不受支持或来源集合不完整时，均会显式报错，绝不截断或静默降级为直链媒体 URL。已有缓存和导出文件不会被改写或删除。旧版导入仍引用其原始文件，因此回放对应命令时请保留这些原文件。

## SegmentBase / 单文件直链提示

对于单文件直链 URL，在已知的情况下请提供精确文件大小以及可选的初始化/索引字节范围：

```json
{
  "id": "649",
  "codec": "vp9.vp09.00.30.08.00.01.01.01.00",
  "mime_type": "video/mp4",
  "url": "https://rr.example/videoplayback?clen=219054029&dur=6832.867",
  "size_bytes": 219054029,
  "duration": 6832.867,
  "init_range": {"start": "0", "end": "1732"},
  "index_range": {"start": "1733", "end": "18156"}
}
```

`init_range` 和 `index_range` 会保留在元数据中。提供 `size_bytes` 或 URL 中的 `clen` 后，UniDL 可直接将大体积直链文件按字节范围分段下载，避免耗时的体积探测请求。

## 直播 JSON 模板

对于 JSON 直播/回看（DVR）导出，使用 `liveMetadata`：

```json
{
  "liveMetadata": {
    "downloadableIdToSegmentTemplateId": {
      "video-1": "0"
    },
    "segmentTemplateIdToSegmentTemplate": {
      "0": {
        "availabilityStartTime": "2026-05-10T00:00:00Z",
        "timescale": 1000,
        "media": "video_$Number$.cmfv",
        "initialization": "video_init.cmfv",
        "duration": 2000,
        "startNumber": 1
      }
    },
    "eventStartTime": "2026-05-10T00:00:00Z",
    "eventEndTime": "2026-05-10T01:00:00Z",
    "eventAvailabilityOffsetMs": 0,
    "ocLiveWindowDurationSeconds": 14400
  },
  "video_tracks": [
    {
      "id": "video-1",
      "url": "https://cdn.example/live?token=abc",
      "all_urls": [
        "https://cdn-a.example/live?token=abc",
        "https://cdn-b.example/live?token=abc"
      ],
      "codec": "hevc-main10-dash-cenc-live",
      "kid": "00112233445566778899aabbccddeeff"
    }
  ]
}
```

`$Number$` 按模板时间轴展开，`all_urls` 用作 CDN 故障切换池。

## 注意事项

- 当容器类型很关键时，优先显式提供 `mime_type`。例如 VP9 可能是 `video/webm` 或 `video/mp4`，UniDL 在解密阶段对两者采取不同处理。
- 对于大体积直链 URL，优先提供精确字节大小；`size_bytes` 或 URL 中的 `clen` 可避免额外的网络探测请求。
- 已知加密方案时请填写 `CENC` 或 `CBCS`。若省略该字段但存在 `encrypted` 或 `kid`，UniDL 仍会将轨道标记为已加密。
