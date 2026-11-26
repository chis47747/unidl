# AVS3 与 Audio Vivid 支持

[English](../../downloader/audio-vivid.md) | [简体中文](audio-vivid.md)

`UniDL-avs3` 以纯 Python 实现 MPEG-TS 流检查与基本流（elementary stream）提取，不调用 FFmpeg。由于 AVS3-P2 视频和 AVS3-P3 / Audio Vivid 并不适合用纯 Python 解码，实际解码工作交由原生编解码器实现完成。

## 检查与提取

```console
UniDL-avs3 inspect recording.ts
UniDL-avs3 extract recording.ts audio.av3a --codec audio-vivid
UniDL-avs3 extract recording.ts video.avs3 --codec avs3-video
```

MPEG-TS 解析器将流类型 `0xd4` 或注册描述符 `AVSV` 识别为 AVS3-P2 视频，将流类型 `0xd5` 或注册描述符 `av3a` / `AVSA` 识别为 Audio Vivid。`0xd5` 流绝不会被误判为 MPEG 音频。

## 解码 AVS3-P2 视频

安装遵循 BSD-3-Clause 许可的 `uavs3d` 解码器，并将其 `uavs3dec` 可执行文件放入 `PATH`，或通过 `UNIDOWN_UAVS3D` 指定其路径：

```console
UniDL-avs3 decode-video input.avs3 output.yuv --threads 4
UniDL-avs3 decode-video recording.ts output.yuv --ts --frames 1
```

输出为平面 YUV（planar YUV）格式。解码器的序列日志会输出分辨率与位深信息。

## 解码 Audio Vivid

请使用依据合法授权条款获取的 Audio Vivid 解码器。将其可执行文件命名为 `avs3Decoder` 放入 `PATH`，或通过 `UNIDOWN_AUDIO_VIVID_DECODER` 指定路径：

```console
UniDL-avs3 decode-audio input.av3a output.wav
UniDL-avs3 decode-audio recording.ts output.wav --ts
```

当前的 UWA 参考 CLI 以位置参数接受输入与输出路径。若使用参数格式不同的兼容解码器，可通过安全的参数模板环境变量进行配置：

```console
UNIDOWN_AUDIO_VIVID_DECODER_ARGS='-if {input} -of {output}' \
  UniDL-avs3 decode-audio recording.ts output.wav --ts
```

该模板在不经过 shell 的情况下直接分词，且必须同时包含 `{input}` 和 `{output}` 占位符。

## 服务集成

启用 `--decode-audio-vivid` 的服务可以对点播和直播下载使用同一套策略。该策略直接读取已下载容器的实际信息，而非根据请求的清晰度或频道名称推断音频编码：

- MPEG-TS 依据 PMT 中的流类型和注册描述符判断。
- MP4/M4A 依据音频采样条目（`mp4a`、`ac-3`、`ec-3` 或 `av3a`）判断。
- AAC 与 E-AC-3/DDP 原样保留，不做转换。
- 仅提取确认属于 `0xd5`/`av3a` 的轨道并解码为 WAV。

对于已封装的多轨源文件，解码 Audio Vivid 期间原始容器保持可用。成功生成的 WAV 会作为伴随音频输入参与最终混流；如果没有可用的解码器，则跳过自动混流，保留原始媒体文件，且不会将无法识别的 `av3a` 轨道交给 FFmpeg。

UWA 参考源码附带限于 AVS 标准研发、测试和推广的使用限制，且不授予专利许可。因此，UniDL 不再分发参考源码、模型或预编译解码器。
