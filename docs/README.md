# UniDL documentation

[English](README.md) | [简体中文](zh-CN/README.md)

This directory documents UniDL's public architecture, user workflows,
configuration contracts, and service-extension rules.

## Start here

- [Requirements and installation](requirements.md) — supported Python versions,
  external tools, package installs, and runtime data.
- [Configuration](configuration.md) — `unidl.yaml`, paths, devices, proxies, and
  runtime state.
- [Settings](settings.md) — global managers, per-service options, and shared
  output-track preferences.
- [Readiness](readiness.md) — core, global, and optional-enhancement dependency
  levels.
- [Architecture](architecture.md) — boundaries between Core/TUI, services, and
  the native downloader.
- [Troubleshooting](troubleshooting.md) — common setup, DRM, playback,
  scheduling, and poster issues.
- [Changelog](../CHANGELOG.md) — public release notes.

## TUI, discovery & automation

- [Interface](interface.md) — core screens, shortcuts, command palette, and
  themes.
- [UI design](ui-design.md) — layout rules, visual tokens, appearance controls,
  and accessibility.
- [Navigation](navigation.md) — one-level Back rules and flow ownership.
- [Experimental home](home-interface.md) — opt-in search-first home, modes,
  local suggestions, and slash commands.
- [Scheduled downloads & Upcoming Radar](scheduled-downloads.md) — timed
  downloads, automatic episode selection, 4-source upcoming radar (JustWatch,
  TVMaze, AniList, TMDB), poster previews, OS background hosting, and CLI.
- [Availability lookup](availability.md) — JustWatch search and regional offer
  mapping.
- [Internationalization](i18n.md) — 12 built-in interface locales, RTL Arabic,
  and translation boundaries.

## Playback & delivery

- [Track selection](track-selection.md) — output scope, filters, Best/All rules,
  default tracks, and subtitle positioning.
- [Native downloader](downloader-integration.md) — manifest inputs, live
  reauthorization, VOD source renewal, and delivery integration.
- [Batch queue](batch.md) — multi-episode selection and queue execution.
- [Cancelling & resuming](cancelling.md) — stopping VOD or live jobs and
  preserving checkpoints.
- [Mixed import queue](import-queue.md) — ordered downloads across export files,
  services, and media types.
- [Third-party exports](third-party-exports.md) — Unshackle and other non-UniDL
  export files delivered through the generic downloader.
- [Live channels](live.md) — recording, DVR/replay windows, duration, and key
  rotation.
- [Audio-only services](audio.md) — audio presentation, metadata, and export.
- [Chapters](chapters.md) — chapter retrieval and container embedding.
- Specialized downloader references:
  [ASS subtitles](downloader/ass-subtitles.md),
  [MP3 audio format](downloader/mp3-audio-format.md),
  [JSON manifests](downloader/json-manifest-format.md),
  [Live pipe mux](downloader/live-pipe-mux.md),
  [ISM / Smooth Streaming](downloader/ism-smooth-streaming.md), and
  [Audio Vivid](downloader/audio-vivid.md).

## DRM, vaults & security

- [DRM systems](drm.md) — local/remote CDMs, PlayReady PSSH handling, and
  license-action boundaries.
- [Key vault](key-vault.md) — local SQLite and remote HTTP vaults, read/write
  policies, and manual keys.
- [Content-key verification](key-verification.md) — optional pre-download
  encrypted track verification and fallback rules.

## Extending & contributing

- [Writing a service](writing-a-service.md) — package layout, registration,
  Flow asks, playback, DRM, credentials, and testing.
- [Compiled-only services](compiled-services.md) — native extension manifests
  and ABI/platform compatibility.
- [External helpers](external-helpers.md) — declaring and resolving helpers.
- [Partner authorization](partner-authorization.md) — service-to-service login
  handoffs.
- [Playback lifecycle](playback-lifecycle.md) — session, heartbeat, and cleanup
  rules.
- [Testing](testing.md) — offline checks, live checks, and regression practice.
- [Publishing](publishing.md) — PyPI releases, trusted publishing, and GitHub
  account switching.

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) before
opening a change. Keep credentials, keys, and device material outside commits,
and update the matching documentation whenever a Core or service contract
changes.
