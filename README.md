# UniDL

[English](README.md) | [简体中文](README.zh-CN.md)

UniDL is a terminal-first media browser, scheduler, and native downloader for streaming services. It connects providers to one consistent workflow: browse or search a catalogue, sign in when needed, inspect the real track ladder, acquire DRM keys through the service's own contract, and download, schedule, or record the final output in the same application.

The codebase keeps three layers strictly separated:

- **Services** own sign-in, catalogue browsing, search, manifest discovery, provider-specific settings, and license transport.
- **Core / TUI** owns navigation, settings, credentials, CDM and vault policy, track selection, scheduling, progress, logs, and the interactive UI.
- **Native downloader** runs in-process to parse manifests, transfer segments, decrypt media, handle subtitles and chapters, and mux the finished file.

## Features

### Interactive TUI & Personalization

- Full keyboard and mouse support with predictable one-level Back, Cancel, and double-`Esc` Quit behavior.
- **Classic grid or search-first Beta Home**: browse services by A–Z, country, or media type, jump straight back via the **Recently used** bar, or enable **Beta interface** for unified search across services, exports, local vault keys, settings, JustWatch, URLs, and slash commands (`/schedule`, `/radar`, `/vault`, `/exports`, `/settings`).
- **Live appearance customization**: 7 built-in themes (`Dark`, `Light`, `Tokyo Night`, `Catppuccin Mocha`, `Nord`, `Gruvbox`, `High Contrast`), 9 system accent presets, 12 Home banner color gradients, 3 layout densities (`Auto`, `Comfortable`, `Compact`), and a colorblind-friendly status palette (Okabe–Ito) — all with real-time preview before saving.
- **12 interface languages**: English, Simplified Chinese, Traditional Chinese, Spanish, French, Portuguese, Italian, Japanese, Korean, Russian, German (`Deutsch`), and right-to-left Arabic, switchable live without dropping an active session.
- **Everyday ergonomics**: `?` shortcuts reference modal, `ctrl+p` searchable command palette, `m` modified-only filter in Settings, unsaved-change protection in text editors, and optional completion alerts (desktop notification or terminal bell).

### Scheduled Downloads & Upcoming Radar

- Schedule any registered service at a future time by **direct URL / ID** or **in-service search** (with conservative title/year disambiguation that flags ambiguous matches as `needs_review` instead of guessing).
- Automatic movie, multi-episode range (`S01E01-E05`, `all`), and **Watch ongoing** new-episode tracking with a persistent `completed_episodes` ledger and configurable release-window retries (`6–48h`) when episodes publish late.
- **Dry-run preflight** tests lookup and episode matching before the scheduled time without downloading media.
- **4-source Upcoming Radar**: combine or single-select **JustWatch**, **TVMaze**, **AniList**, and optional **TMDB** (prompts for a privately stored v3 API key or v4 token) to browse upcoming releases by date and schedule titles in one press.
- **Adaptive 2:3 poster previews**: crisp native images on Kitty, Ghostty, iTerm2, and WezTerm (`auto` mode), automatically hidden with zero network fetches on macOS `Terminal.app` or narrow panes, with an opt-in cell fallback (`always`) and instant `p` toggle.
- **Foreground conflict queueing & OS background hosting**: jobs due during an active foreground download queue automatically; install native **macOS `launchd`**, **Linux `systemd --user`**, or **Windows Task Scheduler** hosting so scheduled jobs run even when UniDL is closed.

### VOD & Track Selection

- Supports DASH/MPD, HLS, ISM/Smooth Streaming, JSON manifests, and direct media URLs, plus multi-manifest ladders when a service splits profiles.
- Independent video, audio, and subtitle filters (resolution, codec, HDR/Dolby Vision/HLG/HDR Vivid, Atmos/Audio Vivid, language, channels, subtitle kinds) and **Output scope** (`Normal package`, `Video`, `Audio`, `Subtitle`, or `Custom`) for targeted batches.
- Optional **Dolby Vision + HDR10 hybrid** output via `dovi_tool`, optional chapter fetching/embedding, and title attachment preview/download.
- Flexible delivery: `ffmpeg`, `mkvmerge`, `MP4Box`, or `No mux (original tracks)`, resumable segment checkpoints, optional pre-download **content-key verification**, and **mixed import queues** across UniDL and third-party export files.
- Customizable filename and episode-folder templates with live preview and dot/space/hyphen styles derived from the tracks actually downloaded.

### Live Recording

- Live HLS, DASH/fMP4, and refreshable playlists with track selection before capture.
- Replay/DVR window inspection, live-edge or offset start, finite or unlimited (`00:00:00`) duration, real-time merge, and pipe muxing into a growing output file.
- Rotating live-key support and clean Back finalization versus fast Quit cancellation.

### DRM, Key Vaults & Credentials

- Strict system-matched local CDMs (Widevine `.wvd`, PlayReady `.prd`, MonaLisa `.mld`) plus remote CDM endpoints and per-resolution CDM rules.
- Local SQLite and remote HTTP key vaults with independent read, write, and search target policies, service-level overrides, and manual `KID:key` entry.
- Service-owned license transport and per-service isolated credential slots, cookie profiles, and token stores.

### Audio, Helpers, Proxies & Storage

- Dedicated audio-only layout with album artwork rail, ID3v2 tagging, and lossless/clear passthrough or FFmpeg conversion (`mp3`, `flac`, `alac`, `m4a`, `opus`, `wav`).
- Declarative helper resolution (`PATH`, config, project `binaries/` / `helpers/`, or package resources) without arbitrary filesystem scanning.
- Static HTTP/HTTPS/SOCKS proxy pools plus integrated proxy providers (ExpressVPN OAuth device grant, NordVPN, Surfshark, Windscribe) with separate API vs. segment-download routing.

## Install

UniDL requires **Python 3.11+** on macOS, Windows, or Linux (64-bit recommended). Install **FFmpeg** (including `ffprobe`) for full muxing, conversion, and probing; `mkvmerge`, `MP4Box`, `aria2c`, and `dovi_tool` are optional. See [Requirements and installation](docs/requirements.md) for platform notes.

From PyPI:

```console
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install unidl
```

From a source checkout:

```console
git clone https://github.com/chis47747/unidl.git
cd unidl
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install .
```

For development and tests:

```console
python -m pip install -e '.[dev]'
# or with uv:
uv sync --extra dev
```

## Quick start

No YAML file is required on first launch. Start the interactive TUI with:

```console
unidl
```

(`python -m unidl` is equivalent; source users with `uv` can run `uv run unidl`.)

Without a custom `unidl.yaml`, finished downloads default to `downloads/<service>/` inside the current project folder. Home displays the active download directory on first launch — click it or open **Settings → Files & naming → Output locations** anytime to change folders without touching YAML.

To target a specific configuration file:

```console
unidl --config ./unidl.yaml
```

### Common CLI commands

Read-only inspection and diagnostics:

```console
unidl --help
unidl services
unidl cdm --check
unidl keys <kid> --service <service-id>
```

Standalone native delivery for an exported JSON manifest or direct URL:

```console
unidl list <manifest-or-json>
unidl download <manifest-or-url> --save-name "Example.Title"
```

Scheduled downloads and OS background host management:

```console
unidl schedule list
unidl schedule run --dry-run --id <job-id>
unidl schedule run
unidl schedule daemon --interval 60
unidl schedule host status
unidl schedule host install --interval 5
```

## Configuration and runtime data

- **`unidl.yaml`** holds static configuration (directories, local/remote CDMs, credentials, proxies, helpers). Copy [`docs/unidl.example.yaml`](docs/unidl.example.yaml) when you need local overrides, or manage CDMs, vaults, proxies, and output paths directly inside **Settings**. Keep working configs with secrets out of Git (`chmod 600` on Unix).
- **`<paths.home>/settings.json`** (`0600`, atomic locked writes with `.bak` fallback) stores interactive global, service, and track preferences.
- **`<paths.home>/schedules.json`** (`0600`) stores scheduled jobs, watch ledgers, and redacted execution history.
- Tokens, cookies, CDM files, vault databases, logs, and exported commands are runtime data and should never be committed.

## Project layout

```text
src/unidl/core/        contracts, DRM, vaults, scheduler, upcoming radar, storage, flow engine
src/unidl/tui/         Textual interface, themes, poster preview, and screens
src/unidl/downloader/  in-process manifest parsers, transfer, decryption, and muxing
src/unidl/locales/     12 built-in interface language catalogues
src/unidl/services/    service packages (one package per service)
docs/                  architecture, user guides, and service contracts
tests/                 offline contract and integration tests
```

## Documentation

Start with [docs/README.md](docs/README.md):

- **Getting started**: [Requirements & installation](docs/requirements.md) · [Configuration](docs/configuration.md) · [Settings](docs/settings.md) · [Readiness](docs/readiness.md)
- **TUI & workflows**: [Interface](docs/interface.md) · [UI design](docs/ui-design.md) · [Experimental home](docs/home-interface.md) · [Scheduled downloads & Upcoming Radar](docs/scheduled-downloads.md) · [Availability lookup](docs/availability.md) · [Mixed import queue](docs/import-queue.md) · [Internationalization](docs/i18n.md)
- **Playback & delivery**: [Track selection](docs/track-selection.md) · [Native downloader](docs/downloader-integration.md) · [Live channels](docs/live.md) · [Audio](docs/audio.md) · [Chapters](docs/chapters.md) · [Third-party exports](docs/third-party-exports.md)
- **DRM & security**: [DRM systems](docs/drm.md) · [Key vault](docs/key-vault.md) · [Content-key verification](docs/key-verification.md) · [Security](docs/SECURITY.md)
- **Extending & troubleshooting**: [Architecture](docs/architecture.md) · [Writing a service](docs/writing-a-service.md) · [Testing](docs/testing.md) · [Troubleshooting](docs/troubleshooting.md)

## Development checks

```console
python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
```

Never commit credentials, cookies, tokens, CDM devices, vault keys, TMDB API keys, or signed URLs.

## License

UniDL is released under the [MIT License](LICENSE). Third-party components listed in [`docs/downloader/legal/THIRD_PARTY_NOTICES`](docs/downloader/legal/THIRD_PARTY_NOTICES) remain governed by their respective licenses.

Copyright © 2026 Chris20
