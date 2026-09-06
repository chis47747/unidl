# UniDL

[English](README.md) | [简体中文](README.zh-CN.md)

UniDL is a terminal-first media browser and native downloader. It connects
streaming services to one consistent workflow: find a title or channel, sign in
when needed, inspect the available media, choose the tracks you want, resolve
DRM keys through the service contract, and download or record the result.

The project is designed around three clear layers:

- **Services** handle login, catalogues, search, manifests, service settings and
  service-owned licence requests.
- **Core/TUI** handles navigation, settings, credentials, CDM/vault selection,
  track selection, progress, logs and the interactive workflow.
- **Native downloader** parses manifests, downloads segments, decrypts media,
  writes subtitles and chapters, and muxes the final output in-process.

## Features

### Interactive TUI

- Keyboard and mouse navigation with predictable Back, Cancel and Quit behavior.
- Service home screens with URL, search, live, library and login entry points.
- Search results that continue through seasons and episodes without leaving the
  service flow.
- JustWatch title and availability search with configurable regions and provider
  mapping.
- Responsive log and progress panels, selectable text, copy actions, light and
  dark themes, and localized interface strings.
- Explicit development reload for service packages without silently watching
  files in the background.

### VOD

- DASH/MPD, HLS, ISM/Smooth Streaming, JSON manifests and direct media URLs.
- Multiple video, audio and subtitle tracks with independent output selection.
- Resolution, codec, dynamic-range, language, channel-layout and subtitle
  controls, including HDR/Dolby Vision and audio codec labels where available.
- Multi-manifest playback plans for services that expose separate ladders.
- Optional chapter retrieval and chapter embedding into the final container.
- Safe output naming with title, season/episode, resolution, audio and codec
  tags based on the tracks the user actually downloads.
- Resumable segment downloads, cache-aware retries, post-processing and muxing.

### Live recording

- Live HLS, DASH/fMP4 and other refreshable playlists.
- Track selection before recording, replay/DVR window inspection, recording
  from the live edge or a chosen offset, and finite or unlimited duration.
- `00:00:00` means no duration limit; Stop, Back or Esc ends an active recording.
- Real-time merge and pipe-mux modes where the source and container support them.
- Rotating live keys with service-provided init data and an interactive fallback
  for a genuinely new KID.
- Progress, replay-window information, segment counts, estimated size and
  cancellation state in the TUI.

### DRM, vaults and credentials

- Local Widevine, PlayReady and MonaLisa device contracts with strict system
  matching.
- Optional remote CDM endpoints for systems that support remote challenge and
  licence parsing.
- Local SQLite key vaults and compatible remote key vaults, with multi-vault
  read/write policies, service scoping and manual KID:key entry.
- Service-local licence transport: the shared DRM layer creates challenges and
  parses responses, while each service owns its endpoint, headers and request
  format.
- Independent credential slots, cookie profiles, token stores and refresh
  lifecycles per service and login method.

### Audio and metadata

- Audio-only services and audio tracks use a dedicated presentation and naming
  path.
- MP3 export with ID3v2 metadata, cover art, artist/album/title fields and
  chapter-aware post-processing.
- Clear audio formats remain available when the source already matches the
  requested container; other sources are converted through FFmpeg.

### Helpers, proxy and storage

- Declared service helpers resolved from configuration, `PATH`, project helper
  folders or package resources; no arbitrary filesystem scan.
- HTTP/HTTPS and SOCKS proxy support plus provider-specific VPN integrations
  when configured by the user.
- Project-relative paths for tokens, cookies, CDMs, vaults, caches, commands,
  subtitles and finished media.
- JSON command/export artifacts for automation and reproducible downloads.

## Install

The minimum supported runtime is **Python 3.11** on Windows, macOS or Linux.
Use a 64-bit Python build and install FFmpeg (including `ffprobe`) for the full
download, conversion and muxing workflow. The Python dependency list is kept in
[`requirements.txt`](requirements.txt); development checks are in
[`requirements-dev.txt`](requirements-dev.txt).

From a published PyPI release:

```console
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install unidl
```

The project is also installable directly from a source checkout:

```console
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install .
```

For development and tests:

```console
python -m pip install -e '.[dev]'
```

The project also works with `uv`:

```console
uv sync --extra dev
```

`pip install unidl` is the shortest installation after a release has been
published to PyPI. Until then, use the source-checkout command above.

Configure local CDM paths through `cdm.devices` in `unidl.yaml`. Device files,
helpers, cookies, tokens and vault databases are runtime data and must stay
outside version control; store them in the configured project directories.

See [Requirements and installation](docs/requirements.md) for external tools,
platform notes and a complete preflight checklist.

## Use UniDL

Launch the installed TUI:

```console
unidl
```

`python -m unidl` is equivalent. A packaged install uses `unidl.yaml` from the
current directory when it exists; otherwise it starts with the safe built-in
path defaults. Pass an explicit configuration whenever the file lives elsewhere:

```console
unidl --config ./unidl.yaml
```

Useful read-only diagnostics:

```console
unidl --config ./unidl.yaml --help
unidl --config ./unidl.yaml services
unidl --config ./unidl.yaml cdm --check
unidl --config ./unidl.yaml keys <kid> --service <service-id>
```

The native downloader can also consume an exported JSON manifest or a direct
source URL:

```console
unidl list <manifest-or-json>
unidl download <manifest-or-url> --save-name "Example.Title"
```

In the TUI, choose a service, search or open a URL, select the title and tracks,
then choose whether to download now or save a command/export. A service's own
settings control provider API/profile choices; the shared track settings control
the final output tracks only. See [docs/settings.md](docs/settings.md).

For live channels, choose the tracks first, then choose recording, replay/DVR
behavior and duration. Leave the duration at `00:00:00` for an unlimited
recording and use Stop/Back/Esc to finish it.

## Configuration and data

`unidl.yaml` is the static configuration surface. Relative paths are resolved
from the directory containing that file, so a checkout can be moved safely.
Interactive preferences are stored in `settings.json` under `paths.home`.
Credentials, cookies, tokens, CDMs, vaults, logs, command files and exports are
never required to be committed. Use a private override file for secrets:

```console
python -m unidl --config ./unidl.private.yaml
```

The application uses the explicitly selected configuration or the project-root
configuration when launched from a source checkout.

## Project layout

```text
src/unidl/core/        contracts, DRM, vaults, storage and flow engine
src/unidl/tui/         Textual interface and screens
src/unidl/downloader/  native parsers, transfer, decrypt and mux pipeline
src/unidl/services/    one package per service
helpers/               declared helper assets and modules
cdm/                   local device files, kept private
docs/                  architecture, service and downloader documentation
tests/                 offline contract and integration tests
```

## Documentation

Start with [docs/README.md](docs/README.md). The most useful paths are:

- [Architecture](docs/architecture.md) — boundaries and data flow.
- [Requirements and installation](docs/requirements.md) — supported runtimes,
  package installation and external tools.
- [Publishing](docs/publishing.md) — PyPI releases, token handling and GitHub
  account switching.
- [Writing a service](docs/writing-a-service.md) — add a service and integrate
  native Core capabilities.
- [Native downloader](docs/downloader-integration.md) — supported inputs,
  delivery contracts and progress.
- [Configuration](docs/configuration.md) and [Settings](docs/settings.md) —
  static configuration versus interactive preferences.
- [DRM](docs/drm.md) and [Key vault](docs/key-vault.md) — local/remote key
  resolution and service-owned licensing.
- [Live channels](docs/live.md), [Audio](docs/audio.md) and
  [Chapters](docs/chapters.md) — specialized playback paths.
- [Testing](docs/testing.md), [Troubleshooting](docs/troubleshooting.md) and
  [Security](docs/SECURITY.md) — verification and safe operation.

## Development checks

```console
python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
```

When changing a service, run its offline checks and a real playback check with
authorized account, region and device data. Never include credentials, cookies,
tokens, CDM private material, vault keys or signed URLs in commits or bug
reports.

Copyright © 2026 Chris20
