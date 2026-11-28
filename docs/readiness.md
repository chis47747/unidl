# Readiness levels

[English](readiness.md) | [简体中文](zh-CN/readiness.md)

The readiness chip and **Readiness** screen use three dependency levels.  A
missing item is not automatically a broken installation; its colour describes
the scope of what is unavailable.

| Colour | Level | Examples | Effect |
| --- | --- | --- | --- |
| red | `core` | Textual, PyYAML, Requests, Cryptography, PyCryptodome, writable UniDL paths | UniDL's normal TUI/downloader runtime is incomplete |
| yellow | `global` | Widevine/PlayReady/MonaLisa libraries and local or remote CDM devices | DRM-protected services may be unavailable; clear playback and services that do not use that DRM continue to work |
| blue | `enhancement` | service-declared helpers, `dovi_tool`, `hdr10plus_tool`, `MP4Box`, `shaka-packager`, `aria2c`, and Hybrid's ffmpeg/ffprobe/mkvmerge tools | The affected service or optional feature is unavailable; the rest of UniDL remains usable |
| green | — | all three levels are present | The installation is fully ready |

Service helpers are always scoped to their service.  A helper marked required by
that service can stop that service, but it must not turn the whole application
red.  Likewise, a CDM is a global capability rather than a prerequisite for
opening UniDL: its absence is yellow until the user adds a local/remote device.

`dovi_tool` and `hdr10plus_tool` are intentionally blue.  Dolby Vision + HDR10
Hybrid output (and optional HDR10+-to-DV Profile 8.1 synthesis) are optional
post-processing features and are never required for ordinary playback or
downloads.  The readiness check also reports detected binary versions via
UniDL's shared binary registry (system `PATH` first, project `binaries/`
fallback, plus service-registered tools).

## Installing external tools from Readiness

From the **Readiness** screen, click the **Install tools** chip in the header
bar or press `i` to open the explicit confirmation install wizard. It inspects
standard tools (`ffmpeg`, `ffprobe`, `mkvmerge`, `MP4Box`, `dovi_tool`,
`hdr10plus_tool`, `shaka-packager`, `aria2c`) alongside any service-registered
binaries, shows detected versions or the recommended package-manager command
for your platform (`brew`, `apt`, `winget`, or `choco`), and installs a tool
only when you select it and click **Install selected** (or press `Enter`).
