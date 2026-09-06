# AVS3 and Audio Vivid support

`UniDL-avs3` handles MPEG-TS inspection and elementary-stream extraction in
Python. It does not call FFmpeg. Decoding is delegated to a native codec
implementation because AVS3-P2 video and AVS3-P3/Audio Vivid are not practical
pure-Python codecs.

## Inspect and extract

```console
UniDL-avs3 inspect recording.ts
UniDL-avs3 extract recording.ts audio.av3a --codec audio-vivid
UniDL-avs3 extract recording.ts video.avs3 --codec avs3-video
```

The MPEG-TS parser recognizes AVS3-P2 video as stream type `0xd4` or registration
`AVSV`. Audio Vivid is recognized as stream type `0xd5` or registration `av3a`
or `AVSA`. A `0xd5` stream is never treated as MPEG audio.

## Decode AVS3-P2 video

Install the BSD-3-Clause `uavs3d` decoder and expose its `uavs3dec` executable
in `PATH`, or set `UNIDOWN_UAVS3D` to its path:

```console
UniDL-avs3 decode-video input.avs3 output.yuv --threads 4
UniDL-avs3 decode-video recording.ts output.yuv --ts --frames 1
```

The output is planar YUV. The decoder's sequence log reports dimensions and bit
depth.

## Decode Audio Vivid

Use an Audio Vivid decoder obtained under terms that permit your use. Put the
decoder in `PATH` as `avs3Decoder`, or set `UNIDOWN_AUDIO_VIVID_DECODER`:

```console
UniDL-avs3 decode-audio input.av3a output.wav
UniDL-avs3 decode-audio recording.ts output.wav --ts
```

The current UWA reference CLI accepts input and output as positional arguments.
For a compatible decoder with another CLI, set a safe argument template:

```console
UNIDOWN_AUDIO_VIVID_DECODER_ARGS='-if {input} -of {output}' \
  UniDL-avs3 decode-audio recording.ts output.wav --ts
```

The template is tokenized without a shell and must contain both `{input}` and
`{output}`.

## Yangshipin integration

The Yangshipin service enables `--decode-audio-vivid` on both VOD and live
downloads. The policy reads the downloaded container rather than inferring an
audio codec from the requested definition or channel name:

- MPEG-TS uses its PMT stream type and registration descriptor.
- MP4/M4A uses the audio sample entry (`mp4a`, `ac-3`, `ec-3`, or `av3a`).
- AAC and E-AC-3/DDP are preserved without conversion.
- Only a confirmed `0xd5`/`av3a` track is extracted and decoded to WAV.

For a muxed source, the original container remains available while Audio Vivid
is decoded. A successful WAV becomes a companion audio input for the final mux;
if no decoder is available, automatic muxing is skipped so the original media is
preserved and the unknown `av3a` track is never handed to FFmpeg.

The UWA reference source carries use restrictions for AVS standards development,
testing, and promotion, and it does not grant patent rights. For that reason,
UniDL does not redistribute the reference source, model, or compiled decoder.
