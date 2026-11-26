# Smooth Streaming（ISM）

[English](../../downloader/ism-smooth-streaming.md) | [简体中文](ism-smooth-streaming.md)

## Smooth Streaming ISM CENC 初始化 Track ID

部分 Smooth Streaming 服务会对整个 manifest 中的所有 `QualityLevel` 连续编排 MP4 track ID，而不是在每个 `StreamIndex` 内重新从 `1` 开始编号。例如，当 manifest 包含 5 个视频档位和紧随其后的 1 个音频档位时，音频分片的 `tfhd.track_ID` 可能是 `6`。

UniDL 合成 ISM 初始化分片（init segment）时，现在会优先保留显式的 `TrackID` 属性；未显式提供时，则按 manifest 中 `QualityLevel` 的出现顺序分配 ID。这使生成的 `tenc` 默认参数与已下载媒体分片保持一致，确保内置的 fMP4 CENC 分片解密器能够为音视频分片匹配到正确的默认密钥元数据。

此逻辑仅限于 ISM 解析器生成的初始化分片元数据，不会影响 HLS、DASH、JSON 直链、SABR/UMP、直播录制或密钥选择逻辑。

直播 ISM 分片在此处可能与点播不同：当直播 manifest 省略显式 `TrackID` 时，即使音频档位之前列出了多个视频档位，媒体分片仍常为单个所选轨道使用 `tfhd.track_ID=1`。因此，对于直播 ISM，除非 manifest 显式提供 track ID，否则 UniDL 默认将合成初始化分片的 track ID 设为 `1`。

## 直播 Manifest 刷新

Smooth Streaming 直播 manifest 中的媒体 URL 带有 `{start time}` 值。这些值是稳定的时间轴标识；随着可见 manifest 窗口向前滑动，每次刷新时的列表下标会重新从 0 开始，而 `{start time}` 保持连续。

ISM 直播处理逻辑独立封装在 `src/unidl/downloader/ism_live.py` 中。针对直播 ISM 流，UniDL 将 Smooth 的 `start time` 用作媒体分片索引，并将同一数值保存为 `timeline_time`。这使得直播刷新去重和音视频对齐始终锚定在真实的 Smooth 时间轴上，即便在 30 分片的滑动窗口下，也能在首批下载完成后继续录制。

对于加密的 H.264/AAC 直播 ISM 管道混流，直接复用现有的 fMP4 直播管道路径：使用当前 ISM 初始化分片解密每个媒体分片、重标时间戳、将受支持的分片重封装为 MPEG-TS 并送入 FFmpeg。其他 HLS、DASH、JSON 或 SABR 直播混流行为均不受影响。

上述 ISM 直播规则作用域严格受限：点播 ISM 流保留原有的从 0 开始的分片索引，DASH、HLS、JSON 直链及 SABR/UMP 直播路径则继续沿用各自的时间轴与刷新规则。
