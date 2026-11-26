# ASS 与 SSA 字幕输入

[English](../../downloader/ass-subtitles.md) | [简体中文](ass-subtitles.md)

UniDL 接受来自服务 manifest、JSON 轨道以及本地字幕文件或直链 URL 的 ASS/SSA 字幕轨道。显式的 `.ass` 和 `.ssa` 扩展名会被保留；缺少扩展名时，精确的 `ass` 或 `ssa` 编码声明同样可用于确定正确的字幕类型与下载扩展名。视频和音频轨道保持原有分类逻辑不变。

部分来源会将 ASS 文本错误标记为 TTML 或 WebVTT。在信任该标签之前，转换器会先检查文件是否为真正的 ASS/SSA 文档：以 `[Script Info]` 节开头（允许前置 BOM、空行和分号注释），后接 `ScriptType` 声明或 `Events` 字段布局。普通 VTT、SRT 或 TTML 对白中出现类似头部的文本不会误触转换器切换。

## 转换与保留

- `--sub-format raw` 原样保留原始文件，不作转换。
- `--sub-format srt` 和 `--sub-format vtt` 会通过 FFmpeg 转换识别出的 ASS/SSA 字幕。如果启用了自动字幕修复，转换后的文本随后还会走常规的字幕条目清理与时长调整流程。
- `--sub-format ass` 会保留 FFmpeg 所支持的 ASS/SSA 样式与特效覆写标签，而不是用通用默认样式重建。原本就符合目标格式的 ASS 文件保持原样不动。
- 拥有合法 `Events` 布局但没有任何 `Dialogue` 记录的文件会被直接视为空字幕，无需调用 FFmpeg；格式损坏的 `Events` 不会被静默丢弃为空字幕。
- 若缺少 FFmpeg 或转换失败，下载流程会按既有策略发出警告并保留原始字幕。

ASS/SSA 的定位、字体、动画与排版特性无法全部在 SRT 或 WebVTT 中表达。转换仅保留 FFmpeg 及目标格式支持的特性；如果这些样式很关键，请使用 `raw` 或 `ass` 输出。目前没有单独的 SSA 输出选项。
