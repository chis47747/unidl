# Readiness levels

The readiness chip and **Readiness** screen use three dependency levels.  A
missing item is not automatically a broken installation; its colour describes
the scope of what is unavailable.

| Colour | Level | Examples | Effect |
| --- | --- | --- | --- |
| red | `core` | Textual, PyYAML, Requests, Cryptography, PyCryptodome, writable UniDL paths | UniDL's normal TUI/downloader runtime is incomplete |
| yellow | `global` | Widevine/PlayReady/MonaLisa libraries and local or remote CDM devices | DRM-protected services may be unavailable; clear playback and services that do not use that DRM continue to work |
| blue | `enhancement` | service-declared helpers, `dovi_tool`, and Hybrid's ffmpeg/ffprobe/mkvmerge tools | The affected service or optional feature is unavailable; the rest of UniDL remains usable |
| green | — | all three levels are present | The installation is fully ready |

Service helpers are always scoped to their service.  A helper marked required by
that service can stop that service, but it must not turn the whole application
red.  Likewise, a CDM is a global capability rather than a prerequisite for
opening UniDL: its absence is yellow until the user adds a local/remote device.

`dovi_tool` is intentionally blue.  Dolby Vision + HDR10 Hybrid output is an
optional post-processing feature and is never required for ordinary playback or
downloads.  Its readiness row also checks the companion tools that the Hybrid
pipeline invokes.
