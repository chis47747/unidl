# UniDL MP3 音频导出格式

[English](../../downloader/mp3-audio-format.md) | [简体中文](mp3-audio-format.md)

本文档定义 `UniDL` 支持的稳定 MP3 导出行为以及元数据约定。

JSON 轨道结构本身详见 [`json-manifest-format.md`](json-manifest-format.md)。本文档重点说明通过 `--audio-format mp3` 选中音频轨道后的后续处理。

## 适用范围

MP3 导出支持点播（VOD）音频轨道与纯音频直播录制。

```bash
unidl download input.json \
  -sa best \
  --audio-format mp3 \
  --save-name "Artist.Album.01.Song"
```

录制直播电台或播客时，UniDL 会先录制并合并原始音频流。纯音频直播录制会将 Ctrl-C 视为优雅停止。启用 MP3 导出后，无论是达到 `--live-record-limit` 上限还是按下 Ctrl-C，UniDL 都会将录制结果收尾导出为 MP3，再按常规清理规则处理原始轨道文件和临时分片。

```bash
unidl download "https://radio.example.com/master.m3u8" \
  -sa best \
  --live-real-time-merge true \
  --live-keep-segments false \
  --audio-format mp3 \
  --audio-metadata-file "/path/to/station.metadata.json" \
  --save-name "Radio.Recording"
```

直播 MP3 导出目前要求仅选中音频轨道，且不能与 `--live-pipe-mux` 同时使用。不指定 `--audio-format mp3` 时，上述命令会保留原始直播录制容器（HLS 电台通常为封装在 MPEG-TS 中的 AAC）。

目前 `--audio-format` 仅接受 `mp3`。通用音频模块在设计上支持后续扩展其他音频输出格式，无需将格式相关的处理逻辑重新塞回主 CLI。

## 代码职责划分

实现按职责刻意拆分如下：

- `src/unidl/downloader/audio.py`：负责元数据归一化、加载外挂元数据文件、解析与下载封面图、构建 ID3 字段以及执行音频导出。
- `src/unidl/downloader/cli.py`：负责校验 CLI 参数组合、向已选音频流注入外挂元数据、控制输出命名，并在下载/解密之后调度音频导出。
- `src/unidl/downloader/parsers/json_manifest.py`：从 JSON 轨道 manifest 读取标题级的 `audio_metadata`，并将归一化后的数据传递给每条音频流。
- `tests/test_audio.py`：包含基于真实 FFmpeg/FFprobe 的测试，用于验证 MP3 时长与内嵌封面行为。

HLS、DASH、ISM、SABR、直链、解密及直播录制模块均不包含 MP3 元数据规则。它们只负责把选中的明文音频文件交给公共音频后处理层。

## HLS、DASH 与 ISM 的外部元数据

主 HLS、DASH、ISM 及直链媒体输入通常不包含完整的单曲级 ID3 元数据。此时可通过外挂文件提供同一套 JSON 约定：

```bash
unidl download "https://cdn.example.com/master.m3u8" \
  -sa best \
  --audio-format mp3 \
  --audio-metadata-file "/path/to/song.metadata.json" \
  --save-name "Artist.Album.01.Song"
```

外挂文件的根节点可以直接是元数据对象：

```json
{
  "title": "Song",
  "artist": "Artist",
  "album": "Album",
  "date": "2026",
  "track": "1/10",
  "cover": {
    "path": "artwork/cover.png"
  }
}
```

也可以使用包含 `audio_metadata` 和标题级回退字段的包装标题对象：

```json
{
  "name": "Song fallback",
  "artist": "Artist fallback",
  "year": "2026",
  "audio_metadata": {
    "title": "Exact Song",
    "album": "Exact Album",
    "cover": {
      "url": "https://images.example.com/cover.jpg"
    }
  }
}
```

外挂文件也可以是一个仅含单个对象的数组。`--audio-metadata-file` 必须配合 `--audio-format` 使用，并且无论输入来源是 HLS、DASH、ISM、JSON 还是直链媒体 URL，都会作用于所有选中的音频轨道。对于这些选中轨道，外挂文件中的字段会替换 manifest 提供的 `audio_metadata`。

相对路径形式的 `cover.path` 以及相对路径形式的 `cover.url` 均相对于外挂元数据文件所在目录解析，避免封面解析受远程主 manifest URL 影响。

## 处理流程

对每条选中的音频轨道，UniDL 按以下步骤执行：

1. 解析并选定源音频轨道。
2. 使用原生下载器完整下载源文件。
3. 若为直播音频，则持续录制直到达到设定的时长上限或通过 Ctrl-C 优雅停止。
4. 若所选轨道已加密，先执行解密。
5. 若显式指定了 `--repack`，先执行重封装。
6. 解析标题级音频元数据及可选封面图。
7. 将轨道导出或重写标签为 MP3。
8. 对于点播任务，结合输出格式与元数据签名写入已完成轨道缓存。
9. 按常规任务清理配置删除已被替换的源中间文件。

加密音频不能同时指定 `--audio-format mp3` 和 `--no-decrypt`，因为 FFmpeg 必须接收明文音频输入。

## MP3 编码

非 MP3 源音频通过 FFmpeg 的 `libmp3lame` 以 320 Kbps 目标码率进行编码：

```text
AAC / Opus / AC-3 / E-AC-3 / other FFmpeg-readable audio
                         -> libmp3lame 320 Kbps -> MP3
```

UniDL 不会强制指定采样率或声道布局，交由 FFmpeg 根据源流和 MP3 编码器自动协商。

如果选中的源音频本身已识别为 MP3，则直接流拷贝音频轨而不重新编码，但仍会将元数据和封面重新写入新的 MP3 容器。

所有 MP3 输出均使用 ID3v2.3。

### 转换进度

下载进度达到 100% 仅表示源数据字节已全部下载完成，并不代表最终输出文件已经就绪。随后的 **Transcoding MP3** 进度行会展示实际已处理的媒体时长与 FFmpeg 转换速率（例如 `200x` 表示 200 倍播放速度）。百分比与预计剩余时间（ETA）基于已知轨道时长计算，不再额外探测媒体；若时长或速率不可用，则省略对应的估算值。对本身兼容的编码直接流拷贝时，该行会显示为 **Remuxing**。只有在 FFmpeg 正常退出（包括完成最终元数据与容器写入）之后，转换进度才会达到 100%。

### macOS xHE-AAC 解码器兼容性

在 macOS 上转码 xHE-AAC 时，UniDL 会先用 FFprobe 检查第一条音频流的实际编码/档次（profile），再通过一段短解码校验 FFmpeg 的 AudioToolbox（`aac_at`）解码器。仅在校验通过后，才会为 MP3、FLAC 或 ALAC 转换启用该输入解码器。遇到解码错误时会立即中止转换，不会忽略错误；采样率与声道协商、元数据、封面和章节仍沿用既有逻辑。

该兼容路径不会在 Windows 或 Linux 上触发，也不适用于其他音频档次或流拷贝操作。M4A 输出仍直接拷贝原始音频流：不会将 xHE-AAC 转为 AAC-LC，也不保证目标播放器能够解码 xHE-AAC。

## 纯音频输出逻辑

当选择结果只包含音频轨道时，UniDL 不会自动将它们合并：

- 选中单条音频轨道时，输出单个 `.mp3` 文件。
- 选中多条音频轨道时，按常规轨道命名规则分别输出独立的 `.mp3` 文件。
- 选中的字幕保留为独立的外挂文件。
- 仍可通过 `--mux` 或 `--mux-format` 显式要求容器混流/封装。

这样可以避免将不同语言、混音版本、码率或声道布局意外塞进同一个多轨媒体容器中。

当同时选中视频和音频轨道时，转换后的 MP3 音频可以进入现有的点播混流流程。除非显式指定其他封装格式，否则点播混流的默认容器仍为 MKV。

## 元数据位置

音频导出元数据应放在标题对象的 `audio_metadata` 字段下：

```json
{
  "name": "Song title fallback",
  "artist": "Artist fallback",
  "album": "Album fallback",
  "year": "2026",
  "audio_metadata": {
    "title": "Exact Song Title",
    "artist": "Exact Artist",
    "album": "Exact Album"
  },
  "audio_tracks": []
}
```

`audio_metadata` 属于标题级元数据，会复制到该标题下的每条音频轨道。目前不支持单轨级别的 `audio_metadata` 覆盖。如果不同轨道需要不同的单曲元数据，上游导出方应分别生成独立的标题对象。

## ID3 元数据字段

支持以下 `audio_metadata` 字段：

| JSON 字段 | 含义 | 对应 ID3 帧 |
| --- | --- | --- |
| `title` | 歌曲或录音标题 | `TIT2` |
| `artist` | 曲目艺人 | `TPE1` |
| `album` | 专辑标题 | `TALB` |
| `album_artist` | 专辑艺人 | `TPE2` |
| `date` | 发行年份或日期 | `TYER`/日期元数据 |
| `track` | 曲目编号（可选附带总数） | `TRCK` |
| `disc` | 碟片编号（可选附带总数） | `TPOS` |
| `genre` | 流派 | `TCON` |
| `composer` | 作曲或词曲作者 | `TCOM` |
| `comment` | 自由格式备注 | `COMM` |
| `copyright` | 版权声明 | `TCOP` |
| `publisher` | 唱片公司或发行商 | `TPUB` |
| `isrc` | 国际标准录音制品编码 | `TSRC` |

推荐格式示例：

```json
{
  "date": "2026",
  "track": "4/12",
  "disc": "1/2",
  "composer": "Writer One; Writer Two",
  "isrc": "USABC2600001"
}
```

推荐使用字符串值。数值会自动转为字符串；列表和元组会用 `; ` 拼接；空值会被忽略。

## 元数据优先级与别名

`audio_metadata` 中的字段优先级最高。缺失时，UniDL 按以下顺序接受标题级回退字段：

| 输出字段 | 按优先级排序的可接受字段 |
| --- | --- |
| `title` | `audio_metadata.title`、`name`、`title` |
| `artist` | `audio_metadata.artist`、`artist` |
| `album` | `audio_metadata.album`、`album` |
| `album_artist` | `audio_metadata.album_artist`、`audio_metadata.albumArtist`、`album_artist`、`albumArtist` |
| `date` | `audio_metadata.date`、`audio_metadata.year`、`release_date`、`releaseDate`、`year` |
| `track` | `audio_metadata.track`、`track_number`、`trackNumber`、标题级 `track`、标题级 `track_number` |
| `disc` | `audio_metadata.disc`、`disc_number`、`discNumber`、标题级 `disc`、标题级 `disc_number` |
| `genre` | `audio_metadata.genre`、`genre` |
| `composer` | `audio_metadata.composer`、`composer` |
| `comment` | `audio_metadata.comment`、`comment` |
| `copyright` | `audio_metadata.copyright`、`copyright` |
| `publisher` | `audio_metadata.publisher`、`publisher` |
| `isrc` | `audio_metadata.isrc`、`audio_metadata.ISRC`、`isrc`、`ISRC` |

处理时会先导入源媒体自带的容器元数据，再由解析后的 `audio_metadata` 覆盖同名字段。

## 封面对象

封面图通过 `audio_metadata.cover` 配置。

远程图片：

```json
"cover": {
  "url": "https://images.example.com/album-cover.webp",
  "headers": {
    "Referer": "https://music.example.com/",
    "User-Agent": "Example Music Client/1.0"
  }
}
```

本地图片：

```json
"cover": {
  "path": "artwork/album-cover.png"
}
```

为兼容旧格式，封面也可以直接写成字符串：

```json
"cover": "https://images.example.com/album-cover.jpg"
```

当 `audio_metadata.cover` 缺失时，标题级别名 `cover`、`cover_url` 和 `coverUrl` 同样会被接受。

## 封面解析规则

- 同时提供 `path` 和 `url` 时，`cover.path` 优先。
- 支持文件系统绝对路径和 `file://` 路径。
- 相对本地路径相对于 JSON manifest 所在目录解析。
- 远程 JSON manifest 中的相对 URL 相对于该 manifest URL 解析。
- HTTP 与 HTTPS 封面使用 UniDL 原生加载器获取。
- 先应用常规任务请求头。
- 随后应用 `cover.headers`，并覆盖同名请求头。
- 封面请求沿用任务的 `--http-request-timeout` 和重试次数设置。

远程封面会缓存在任务的后处理临时目录中，缓存标识包含解析后的 URL 与请求头。如果上游在 URL 不变的情况下替换了封面内容，需要立即刷新时请在 URL 上附加去缓存查询参数。

## 支持的封面图片格式

输入封面支持以下图片格式：

- JPEG
- PNG
- WebP

格式类型直接依据图片字节内容识别，而不轻信 URL 扩展名或 HTTP MIME 类型。遇到不支持或损坏的图片数据时，MP3 后处理会直接报错失败，而不是静默生成缺少封面的文件。

FFmpeg 会将封面图片以单条 MJPEG 流写入 MP3，并附带以下属性：

```text
disposition: attached_pic
title: Album cover
comment: Cover (front)
```

完整音频时长必须不受单帧内嵌封面图的影响。该行为受真实 FFmpeg/FFprobe 回归测试保护。

## 完整 JSON 示例

```json
[
  {
    "id": "song-id",
    "type": "music",
    "name": "Sharpest Tool",
    "artist": "Sabrina Carpenter",
    "album": "Short n' Sweet",
    "year": "2024",
    "duration_ms": 218000,
    "audio_metadata": {
      "title": "Sharpest Tool",
      "artist": "Sabrina Carpenter",
      "album": "Short n' Sweet",
      "album_artist": "Sabrina Carpenter",
      "date": "2024",
      "track": "4/12",
      "disc": "1/1",
      "genre": "Pop",
      "composer": "Jack Antonoff; Sabrina Carpenter; Amy Allen",
      "comment": "",
      "copyright": "2024 Island Records",
      "publisher": "Universal Music Group",
      "isrc": "USUM72404101",
      "cover": {
        "url": "https://images.example.com/sharpest-tool.jpg",
        "headers": {
          "Referer": "https://music.example.com/"
        }
      }
    },
    "audio_tracks": [
      {
        "id": "774",
        "codec": "opus.opus",
        "mime_type": "audio/webm",
        "language": "und",
        "channels": "2.0",
        "bitrate": 283204,
        "url": "https://cdn.example.com/audio.webm?clen=7199362&dur=218.301"
      }
    ]
  }
]
```

## 已完成轨道缓存

已完成轨道的缓存标识包含：

- 源流标识
- 请求的音频输出格式
- 归一化后的音频元数据签名
- 输出命名及相关后处理选项

因此，修改 ID3 元数据或封面配置后，不会错误复用带有旧元数据的已完成 MP3 文件。

## 失败行为

出现以下情况时，MP3 导出会明确报错失败：

- 未选中任何音频轨道
- 直播 MP3 与已选直播视频轨道或 `--live-pipe-mux` 同时使用
- 加密音频同时指定了 `--no-decrypt`
- FFmpeg 或 `libmp3lame` 不可用
- 本地封面文件不存在
- 远程封面下载失败
- 封面字节不是有效的 JPEG、PNG 或 WebP
- FFmpeg 无法解码所选源音频
- FFmpeg 无法编码或重写输出标签

源下载文件与解密中间文件仍遵循常规清理策略。后处理失败不会将该 MP3 标记为已完成。

## 当前限制

- 目前仅实现了 MP3 输出。
- 直播 MP3 在录制停止后才进行收尾转换，不会在分片到达时边录边编码。
- 直播 MP3 目前不能与直播视频轨道或 `--live-pipe-mux` 组合使用。
- 元数据只能通过 manifest 或 `--audio-metadata-file` 提供，暂不支持 `--artist` 这类单独的 CLI 字段参数。
- 仅支持单张正面封面。
- 未显式提供 `cover` 时，不会自动复用源文件内原有的内嵌封面。
- 目前暂不支持写入内嵌歌词及同步歌词。
- 重新编码时的 MP3 码率目前固定为 320 Kbps。

## 验证方法

使用 FFprobe 检查音频流、标签、时长与封面 disposition：

```bash
ffprobe -v error \
  -show_entries \
  'format=duration,bit_rate:format_tags:stream=codec_name,codec_type,sample_rate,channels:stream_disposition=attached_pic' \
  -of json \
  output.mp3
```

合规的带封面 MP3 应包含一条 MP3 音频流和一条 `attached_pic=1` 的图像流，且报告的总时长必须与完整源音轨一致，而不是封面图的时长。
