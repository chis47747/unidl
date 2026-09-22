# Changelog

## [2.1.3] - 2026-09-22

### Shared exports and public downloader scope

- Added the shared mediaexport v1 reader and generic third-party import path.
- Kept imported exports isolated from service sessions, licence transports and
  service-specific authentication.
- Improved manifest URL, header and key validation during export import.
- Reject conflicting content keys and unsupported critical extensions without
  silently falling back to another source. Shared HLS AES URI records and frozen
  segment extensions remain deferred pending the format agreement.
- Preserve HLS MAP key scope, recognize script-based manifest endpoints and
  encode URL spaces without rebuilding signed query parameters.
- Add native legacy TS/AAC SAMPLE-AES handling and optional Dolby Vision + HDR10
  hybrid output with global/per-service controls and source compatibility checks.
- Removed optional public-build bindings for Apple Music decrypt, Qobuz,
  Youku and Deezer, plus Tencent/Yangshipin live-specific transport rules.
- Retained YouTube, iQIYI and the common DASH/HLS/ISM download paths.

## [2.1.2] - 2026-09-18

### First launch and download locations

- Show the effective download folder on Home with a direct link to Files & naming.
  Reviewing that screen dismisses the introduction; the folder remains editable
  under Settings at any time. No YAML file or manual path entry is required.
- Bound and scroll first-run hints so short terminals retain a usable service list.
- Distinguish built-in download defaults from explicit YAML paths in Settings.
  Downloads default to ~/unidl_downloads, grouped by service; paths.home does
  not relocate finished media. Clarified Windows startup and custom --config usage.

### Global defaults and per-service overrides

- Service vault permissions and lookup/write destinations inherit the global
  Vault policy until explicitly overridden. Display the effective value and
  inheritance source; press r to remove a service override.
- Restore the global License after final track selection default under Services.
  Explicit service choices, including off and empty destinations, are preserved.
- Keep third-party export imports isolated from service licence and vault calls.

### Native subtitle muxing

- Convert external XML/TTML/DFXP subtitles to temporary SRT before FFmpeg or
  mkvmerge, fixing BBC XML subtitle mux failures without requiring subby.
- Preserve original subtitles and input metadata, shorten temporary paths for
  Windows, and clean up temporary files after success, failure or cancellation.
- Refresh the in-app release notes in all six interface languages.

## [2.1.1] - 2026-09-17

### Per-service chapters, licensing and vaults

- Added independent **Fetch chapter metadata** controls in each service's
  settings and under **Settings → Services → Chapter metadata by service**.
  Existing global chapter preferences remain the default until a service saves
  its own choice; chapter embedding remains a separate output setting.
- Moved **License after final track selection** into each service's settings.
- Added a **License and vaults** section before **Tracks and output** for
  service-specific local/remote lookup, automatic storage and vault destinations.
  Home-screen search and manual key-entry permissions remain global.
- Fixed selected-track vault lookup expanding to unrelated manifest KIDs.
  A complete cache hit for the selected encrypted tracks avoids an unnecessary
  licence request, while licence transport remains service-owned.
- Enabled the common local-vault lookup path for service-owned DRM as well as
  opt-in remote lookup.

### Settings layout and native downloads

- Aligned translated setting labels and values using terminal-cell-aware columns,
  with independent wrapping for long labels, CJK text and values.
- Kept settings descriptions inside the window, including narrow/short terminals.
- Fixed the malformed first frame when opening settings by calculating row
  wrapping at render time, without delayed full-list rebuilds.
- Fixed first-download resume-cache directory handling on Python 3.11/3.12,
  including the missing-path error reported on Windows.
- Updated the in-app release notes in all six supported interface languages.

## [2.1.0] - 2026-09-16

### Per-service export policy

- Added **Settings → Services → Export manifest type** for every registered
  service, with the existing refreshable master manifest kept as the default.
- Added **All media manifests** export for short-lived master URLs. It stores the
  complete parsed video, audio and subtitle inventory instead of only the tracks
  selected for the current run.
- Preserved portable HLS, DASH and ISM delivery details, including segment URLs,
  byte ranges, encryption metadata, inline initialization data, timelines and
  discontinuities.
- Kept live exports on their refreshable master manifest instead of freezing an
  incomplete static snapshot.

### Import and documentation

- Native imports now always enter download delivery and no longer inherit the
  service-only **after picking → save an export file** action.
- Media-manifest imports restore their saved track semantics without reopening
  an expired master manifest or hydrating HLS child playlists.
- Documented the separate boundaries between provider manifest profiles, final
  track selection and export representation.

## [2.0.9] - 2026-09-15

This release adds safe generic imports for third-party exports while preserving
the service-owned licensing boundary.

### Third-party exports

- Added Unshackle v2 and legacy track/series export adapters under
  `core/third_party_exports.py`.
- Third-party imports always use the generic delivery path; source service
  sessions, cookies, helpers and licence transports are never loaded.
- Preserved exported alternate manifests, KID:key pairs, chapters and direct
  sidecars without re-authorizing them.
- Added lazy HLS child-playlist hydration so large subtitle inventories open
  promptly and only selected playlists are fetched during download.
- Normalized EC3/Atmos metadata without changing native service JSON-manifest
  parsing.

### Documentation and maintenance

- Documented the third-party export boundary and generic-delivery workflow.
- Kept remote vault lookup/store/search operation gates and destination policy
  explicit and independent.
- Updated all supported interface locales and the release version to 2.0.9.

## [2.0.8] - 2026-09-15

This release completes the remote key-vault policy workflow and hardens the
native delivery engine.

### Remote key vaults

- Added independent controls for Home-screen remote KID search, manual remote
  key entry, post-license key storage, and pre-license remote lookup.
- Added per-operation vault destinations for automatic lookup, acquired-key
  storage, and explicit search.
- Kept local lookup first and remote lookup second; complete remote hits can
  avoid a service license request, while partial hits are merged with the
  service response.
- Isolated remote write failures so one unavailable destination warns and does
  not fail a download.
- Extended the same vault opportunity to service-owned DRM flows without
  replacing a service's own license transport.

### Vault policy and resource UI

- Fixed policy controls being clipped by two-row bordered containers; all
  Remote operations and Destinations controls remain visible in compact
  terminals.
- Fixed mouse selection of a remote vault changing its enabled state. Clicking
  selects; Edit and Toggle remain separate actions.
- Added regression coverage for policy visibility, persistence, and passive
  remote-vault selection.

### Native downloader and playback

- Added bounded-memory, memory-mapped CENC decryption for large fragmented MP4
  files.
- Correctly handles explicitly clear CENC samples in encrypted tracks.
- Added in-process HLS AES-128/AES-ECB decryption with the existing OpenSSL
  fallback.
- Preserved additional KIDs from multiple Widevine PSSH values and service
  playback inventories before license resolution.
- Preserved DVR metadata and the additional EC-3/5.1 audio profile mapping.
- Kept transfer speed visible in narrow structured progress rows.

### Documentation and localization

- Documented the distinction between safety gates, remote operations, and
  destinations, including local-first lookup and failure-safe multi-vault
  writes.
- Updated release notes in English, Simplified Chinese, Traditional Chinese,
  Spanish, French, and Portuguese.

The public package contains only the release-build changes. Runtime
configuration, credentials, devices, databases, and internal-only service
changes are not included.
