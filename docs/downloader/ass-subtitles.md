# ASS and SSA subtitle inputs

[English](ass-subtitles.md) | [简体中文](../zh-CN/downloader/ass-subtitles.md)

UniDL accepts ASS/SSA subtitle tracks from service manifests, JSON tracks, and
direct subtitle files or URLs. Explicit `.ass` and `.ssa` extensions are kept;
when the extension is missing, an exact `ass` or `ssa` codec declaration also
supplies the correct subtitle type and download extension. Video and audio
tracks retain their existing classification.

Some sources label ASS text as TTML or WebVTT. Before relying on that label,
the converter checks for a real ASS/SSA document: a leading `[Script Info]`
section, allowing a BOM, blank lines and semicolon comments, followed by a
ScriptType declaration or an Events field layout. Header-like dialogue inside
ordinary VTT, SRT or TTML does not switch converters.

## Conversion and preservation

- `--sub-format raw` preserves the original file without conversion.
- `--sub-format srt` and `--sub-format vtt` convert recognized ASS/SSA through
  FFmpeg. With automatic subtitle fixing enabled, the converted text then uses
  the normal cue cleanup and duration adjustment.
- `--sub-format ass` retains the ASS/SSA styles and override tags supported by
  FFmpeg rather than rebuilding them with a generic style. Existing ASS files
  already in the requested format remain unchanged.
- A valid Events layout with no Dialogue records is treated as an empty
  subtitle without invoking FFmpeg. Malformed events are not silently
  discarded as empty.
- If FFmpeg is missing or conversion fails, the download flow warns and keeps
  the original subtitle according to its existing policy.

ASS/SSA positioning, fonts, animation and typesetting cannot all be represented
in SRT or WebVTT. Conversion preserves only the features supported by FFmpeg
and the destination format; use raw or ASS output when those styles matter.
There is no separate SSA output option.
