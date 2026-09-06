# UniDL documentation

This directory documents the public UniDL contracts and the workflow for using
and extending the project.

## Start here

- [Architecture](architecture.md) — Core, TUI, services and native delivery.
- [Writing a service](writing-a-service.md) — package layout, registration,
  Flow asks, playback, DRM, credentials and testing.
- [Configuration](configuration.md) — YAML, paths, devices and runtime state.
- [Settings](settings.md) — global, service and output-track preferences.
- [Requirements and installation](requirements.md) — supported Python versions,
  external tools, package installs and runtime data.
- [Publishing](publishing.md) — PyPI tokens, trusted publishing and GitHub
  account switching.
- [Native downloader](downloader-integration.md) — manifest inputs and delivery
  integration.
- [Testing](testing.md) — offline checks, live checks and regression practice.

## Playback and delivery

- [DRM systems](drm.md) — local/remote CDMs, PlayReady PSSH handling and key
  acquisition boundaries.
- [Key vault](key-vault.md) — local and remote vaults, policies and manual keys.
- [Live channels](live.md) — recording, DVR/replay, duration and rotation.
- [Audio-only services](audio.md) — audio presentation, metadata and export.
- [Chapters](chapters.md) — chapter retrieval and embedding.
- [Downloader audio formats](downloader/mp3-audio-format.md) and
  [JSON manifests](downloader/json-manifest-format.md).
- [Live pipe mux](downloader/live-pipe-mux.md), [ISM](downloader/ism-smooth-streaming.md)
  and [Audio Vivid](downloader/audio-vivid.md) for specialized delivery paths.

## Interface and integration

- [Interface](interface.md) and [UI design](ui-design.md) — navigation,
  selection, logs, themes and accessibility.
- [Internationalization](i18n.md) — adding interface locales.
- [External helpers](external-helpers.md) — declaring and resolving helpers.
- [Partner authorization](partner-authorization.md) — service-to-service login
  handoffs.
- [Playback lifecycle](playback-lifecycle.md) — session, heartbeat and cleanup
  rules.
- [Availability](availability.md) — JustWatch search and regional offers.
- [Troubleshooting](troubleshooting.md) — common configuration and playback
  issues.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) before
opening a change. Keep credentials and device/private material outside commits,
and update the matching documentation whenever a Core or service contract
changes.
