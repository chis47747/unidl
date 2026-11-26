# 直播管道混流说明

[English](../../downloader/live-pipe-mux.md) | [简体中文](live-pipe-mux.md)

## HLS 直播多 KID fMP4

对于包含多个 KID 的 HLS 直播 fMP4/CMAF 流，管道混流会在解密前逐一校验每个已下载媒体分片（fragment）中的 KID，并以分片 KID 作为密钥选择、初始化分片修补（init patching）以及密钥轮换同步的唯一依据。

该分片扫描的作用范围刻意收敛：仅对需要外部 CENC/CBCS 解密且已知存在多个 KID 的 HLS 直播 fMP4/CMAF 流启用。单 KID HLS、MPEG-TS HLS、点播（VOD）、DASH、JSON 与 SABR 路径均不承担此项额外扫描开销。

## DASH HEVC 直播收尾处理

部分 DASH HEVC 直播流在边录边写生成的 MKV 中虽然完整保留了媒体数据包，但停止录制后的容器最终元数据与视频 DTS 不符合严格播放器的校验规范，可能导致播放器报告文件内部异常。

对于符合条件的流，UniDL 在录制期间依然实时写入 MKV。停止录制后，先将该 MKV 流拷贝到临时 MP4 以重建 DTS 时间轴；如果环境中可用 `mkvmerge`，再从该临时 MP4 提取 Annex-B HEVC 基本流，交由 `mkvmerge` 写入临时 MKV，确保 HEVC 的 `CodecPrivate` 包含完整的 VPS/SPS/PPS 参数集；最后流拷贝生成面向用户的最终 MP4 文件，写入 `hvc1` 标签及显式的 BT.2020/HLG HEVC 元数据以获得更好的播放器兼容性。若 `mkvmerge` 不可用或该兼容步骤失败，则回退到 FFmpeg 流拷贝 Matroska 收尾，再生成兼容 MP4。

当使用 Ctrl-C 中断直播管道混流录制时，UniDL 会将其视为用户主动停止当前录制，并继续关闭与收尾管道混流输出。非管道直播录制及普通下载路径则维持既有的取消行为。

一旦该收尾流程成功且输出文件已生成，原直播管道 FFmpeg 进程的非零退出码会被容忍。此容忍策略严格绑定到成功的兼容收尾路径，其他无关的直播管道混流故障依然会正常报错。

该收尾步骤的触发条件同样严格限定，必须同时满足以下全部条件：

- 直播管道混流的录制容器为 Matroska；
- 选中的流为 DASH 直播视频；
- 选中的视频编码为 HEVC/H.265；
- 所选流的 URL、模板或分片元数据匹配特定服务商的兼容规则。

常规 Matroska 直播管道输出、其他 HEVC 直播流、MPEG-TS 直播管道输出，以及 HLS、SABR、ISM、JSON 直链和点播路径均保持原有行为不变。
