# Changelog

## [2.5.2] - 2026-10-27

### Muxed audio/video separation

- Added service-opted-in separation of finite muxed sources into independently selectable video and audio tracks, using lossless extraction after a shared source download and preserving source identities and per-output resume checkpoints.
- Kept the policy off by default and retained it in commands and exports without expanded segment lists; supported clear and HLS AES-128 VOD while leaving live recording unchanged and explicitly rejecting unsupported sample-encrypted separation.

### Vault write targets

- Fixed All available write targets being treated as disabled, including live key rotation, while preserving remote safety switches, explicit empty selections and disabled/read-only exclusions.
- Preserved verification policy and avoided uploading cached or historical keys merely because write targets changed.

### Home search and return navigation

- Added explicit service-search candidates when title keywords were prefixed or suffixed with a registered searchable service's complete name, ID, alias or tag, preserving keyword case and requiring candidate confirmation.
- Preferred the longest matching service name and kept ambiguous candidates separate, without changing explicit-mode routing, automatic URL/export/key precedence or remembered service scopes.
- Preserved known readiness/import status and same-query local search results during background refreshes, avoided rebuilding unchanged lists, and discarded stale results after query or source changes.

## [2.5.1] - 2026-10-26

### Scrollable panels

- Added a small, clickable ▼ to track selection, selected-track details, batch queues and logs when more content remained below, without reserving another content row or changing selections.
- Hid the control at the end of each panel; clicking it in a log cleared only that log's text selection and resumed following new messages.

### API subtitle progress

- Opened the download frame before selected API subtitle preparation and retained completed subtitle rows alongside native video/audio progress.
- Displayed active, unknown progress for legacy subtitle hooks and measured byte progress for services using the optional prepare_download_with_progress hook, without adding requests or replacing existing transport and conversion behavior.
- Marked subtitles complete only after a nonempty prepared file was registered for muxing, kept failed or cancelled preparation from appearing successful, and left command, list and export modes free of sidecar downloads.

### Home settings search

- Included manager-owned actions and settings in the search-first Home, using page labels, aliases, descriptions and stable keys with localized and English-name matching and no duplicate results.
- Ranked exact names and keys ahead of description-only matches, and opened the owning manager with the matching row highlighted without executing its action; Back preserved the Home query, mode and selection.

### Windows process cleanup

- Fixed the missing signal.SIGKILL error on Windows by using native subprocess termination and kill operations while preserving POSIX process-group signals.
- Kept timeout, cancellation and interrupted probe/mux errors intact during bounded child cleanup, and retained ownership of children that still needed a shutdown retry.

## [2.5.0] - 2026-10-25

### New search-first Home

- Introduced an optional new Home with one search bar, Auto routing and explicit modes for services, export files, local vault keys, settings, JustWatch, service searches and links. Enabled it through Settings → Interface & diagnostics → Beta interface while retaining the classic service-list Home as the default.
- Added slash commands, independent service scopes for search and direct opening, and configurable Auto result categories without disabling explicit modes.
- Kept typing local with background export indexing and debounced vault lookup; remote searches, service requests, key writes and downloads still required explicit actions, and ambiguous links or services required a choice.
- Added localized, theme-aware category labels, responsive search alignment, fitted mouse/keyboard selectors and input clearing, while preserving modes, queries and result selections on Back.
- Documented the new Home, its result-category controls, export imports, key search and navigation workflows.

### Export filenames

- Named batch exports for the series and timestamp instead of the first episode, promoted automatically named single exports when a batch formed, and preserved each entry's release name and explicit custom paths.
- Avoided same-second filename collisions and kept the original export intact if batch promotion failed.

### Vault lookup reporting

- Reported complete and partial hits, misses and lookup failures separately for each queried local or remote vault, using counts of the keys still missing at that step.
- Named only vaults that actually supplied keys in all-cached summaries, without adding requests or changing lookup order, backfill or licence fallback.

## [2.4.9] - 2026-10-24

### Original-track output

- Added No mux (original tracks) to save selected tracks as separate final files after normal segment joining and decryption, preserving source audio codecs and subtitle formats and styling.
- Integrated original-track output with batches, live recording and saved commands, including selected API subtitles and prepared local sidecars, while bypassing conversions, repacking, Hybrid assembly and final-container metadata.
- Preserved raw AAC, AC-3 and E-AC-3 filename extensions and kept default Auto output and the older --no-mux flag unchanged.

### Resource management

- Fixed the confirmation-dialog crash when deleting vault or remote CDM definitions while preserving confirmation and cancellation behavior.

## [2.4.8] - 2026-10-23

### Content-key verification

- Added optional, default-off content-key verification before downloading final selected encrypted VOD tracks, with global settings, per-service overrides and a standalone --verify-keys option.
- Used bounded encrypted samples and actual decoding, skipped live and export/command-only operations, and treated unsupported or inconclusive samples as warnings rather than invalid keys.
- Tried alternative enabled vault candidates after confirmed failures, then used the configured service/CDM once without repeated licensing or changing the playback profile.
- Deferred verification-mode vault writes and local backfill until successful checks, preserving write policies without deleting records or synchronizing vaults.

### Portable export filenames

- Rebuilt imported release names from final selected quality, codecs, audio channels and dynamic range instead of retaining the export's original track suffix.
- Preserved the exact title base in new exports for custom naming templates while keeping older conventional names compatible.

## [2.4.7] - 2026-10-22

### MP4 encryption detection

- Read encryption metadata at actual ISO-BMFF box boundaries and container hierarchies instead of matching box names inside media, codec configuration or unknown leaf payloads.
- Corrected stale encrypted-audio metadata only when a complete fragmented MP4 explicitly proved the audio was clear, removing stale track and segment KIDs and skipping unnecessary decryption without extra network requests or media copies.
- Preserved genuine encryption checks and treated missing KIDs, null placeholders and incomplete or unsupported metadata as insufficient evidence of clear audio.

## [2.4.6] - 2026-10-20

### Striped audio and filenames

- Added complete-file BF_CBC_STRIPE decoding and routed JSON audio through transport decoding instead of MP4 sample decryption.
- Preserved FLAC extensions in output filenames and naming templates.

### Compressed MP4 initialization

- Expanded validated gzip-compressed standalone MP4 initialization headers during assembly while preserving media segments and original download parts.
- Normalized older completed audio checkpoints for mux retries without downloading them again, with bounded expansion and temporary-file cleanup.

### Back navigation

- Restored one-press Back from the service home after earlier job failures while retaining their outcomes and logs.

## [2.4.5] - 2026-10-19

### KID extraction

- Ignored all-zero placeholder KIDs in protected MP4 initialization metadata so they no longer created an extra content-key requirement.
- Preserved validation and errors for genuinely missing content keys.

## [2.4.4] - 2026-10-19

### License policy

- Added an explicit License action choice for Before, After and service-declared Custom workflows, preserving legacy settings and global/service override precedence.
- Scoped post-selection licensing to selected encrypted tracks across DASH, HLS and ISM, improved distinct Widevine PSSH coverage and PlayReady representation matching, and kept license transport service-owned.
- Added explicit Custom-license declarations for compiled service manifests without inferring support from filenames or hook overrides.

### Back navigation

- Forwarded answers and navigation exceptions through shared search flows so Back restored the immediate parent page and closed child flows correctly.
- Documented one-level return behavior for results, failures, batches and overlays.

### Track languages

- Displayed full language names with explicit country or region names using offline data, while preserving original tags and filtering behavior.
- Added additive original-language audio and subtitle selection, including mixed rules such as original,en without duplicate language-group selection.

### Interface language

- Added German (Deutsch), including regional system-locale aliases, settings labels and application messages.

## [2.4.3] - 2026-10-17

### Track information

- Highlighted the Original badge on audio and subtitle rows consistently in track selection and download details, with separate dark and light theme colours.
- Displayed full country or region names alongside explicit language tags and unambiguous matching locale metadata, without changing language filters, mux metadata or filenames.

### Portable exports

- Preserved requested and returned profile labels, source associations and the complete independent subtitle inventory, including unselected languages, subtitle kinds, original flags and request headers.
- Restored portable subtitle sources for generic imports while allowing installed services to retain their custom sidecar hooks without duplicate tracks.
- Fixed JSON source detection when a generated document contained embedded manifest markup.

### Download progress

- Distinguished completed transfers from completed processing and displayed audio transcoding or remuxing progress with processed time, speed and available percentage and ETA.
- Kept processing status visible ahead of long filenames and hid the audio artwork rail in narrow download windows to preserve progress space.

## [2.4.2] - 2026-10-16

### Advanced paths

- Displayed full read-only runtime paths and the running installation's service directory, with folder-opening actions that left configuration and existing files unchanged.

### Back navigation

- Fixed returning from saved commands, exports and track-only results so the preceding prompt was restored without racing screen teardown or replaying completed work.

### Batch downloads

- Added clickable details for completed, active, failed and queued titles, with selected tracks, output paths, timing and explicit retry controls.
- Prevented preparation and licence failures from leaving a batch waiting on a hidden question, and made cancellation dismiss pending download prompts without dismissing service or login prompts.
- Stabilized queue rows and kept track-selection space available when the terminal was resized or a later title refreshed its details.

### Attachments

- Separated attachment previews and metadata from automatic attachment-file downloads with a default-on output setting, and kept attachment failures from failing completed media downloads.

## [2.4.1] - 2026-10-16

### Track selection

- Preferred ordinary dialogue audio before bitrate ranking within the selected language and codec filters, while keeping enhanced dialogue, commentary and audio description available through explicit selection and All mode.
- Applied the same ordinary-audio preference when choosing the default mux audio track.

### Manifest and export size

- Reduced generated JSON manifest and export sizes by preserving original source manifests and request context instead of expanded segment snapshots.
- Added export format 3 with backward-compatible reading of formats 1 and 2, including merged source profiles and legacy imports.
- Added bounded JSON generation and atomic export writes that reported oversized or incomplete sources without silently dropping tracks or replacing a valid export.

### Documentation

- Clarified audio-role selection, source-based manifests, export compatibility and file-size limits.

## [2.4.0] - 2026-10-15

### Subtitles

- Improved ASS/SSA input detection, preserved subtitle extensions and styles, and handled valid empty subtitle documents without unnecessary conversion warnings.

### macOS audio

- Added a validated AudioToolbox decoder path for xHE-AAC transcoding on macOS while leaving stream-copy output and other platforms unchanged.

### Decryption performance

- Accelerated fragmented MP4 audio/video decryption range lookups and added progress reporting for whole-file CENC processing.

### Resume and file cleanup

- Added verified checkpoints for completed download, decryption and processing stages so retries could reuse prepared tracks after cancellation or mux failure.
- Kept valid resume checkpoints separate from failed intermediate files and deferred cache cleanup until successful output commit.

## [2.3.9] - 2026-10-14

### QR sign-in

- Improved QR image clarity, preserved provider-supplied codes and added an image-opening fallback for compact terminals.

### Live recording

- Made Back finish and save live recordings normally, while Quit and failed finalization preserved already captured media without starting extra processing.

### PlayReady licensing

- Restricted License after final track selection to selected encrypted DASH tracks for both local and remote PlayReady CDMs, without changing full-manifest mode.

### Video bitrate

- Added independent per-service actual video bitrate controls with global inheritance and selected-track probing.

### Batch cancellation

- Fixed paused batch cancellation stalls by releasing pause state and moving socket and process cleanup off the interface thread.

## [2.3.8] - 2026-10-13

### Download reliability

- Isolated each title's intermediate files and cleaned failed or cancelled outputs without removing resumable segment caches.
- Added optional episode-folder layouts under the service download directory while preserving the existing flat layout by default.

### Track selection and search

- Made rebuilt and equivalent tracks map one-to-one during automatic selection and manual track-picker recovery.
- Aligned Search page status information with its heading while keeping the home status row centered.

### Services

- Added ITVX, Channel 4, Channel 5, Eternal Family, KVIFF.TV and DAFilms to the public service set.

## [2.3.7] - 2026-10-11

### Batch downloads

- Added optional fixed or random delays between automatically queued titles, with responsive pause, resume and cancellation controls.

### Metadata windows

- Added clickable Previous/Next controls to attachment, chapter and lyrics windows alongside keyboard navigation, including compact layouts for short terminals.

### Default subtitles

- Added a default subtitle language preference for muxed output, including selected-track matching, sidecar subtitles, fallback notices and import/export persistence.

## [2.3.6] - 2026-10-09

### BBC playback

- Expanded BBC iPlayer playback and search to cover BBC Sounds audio, radio and podcast sources under the unified BBC service.

### Track selection and metadata

- Improved automatic track selection and back-navigation behavior while preserving the final selection constraints.
- Validated missing video colour metadata with a bounded init-segment probe and kept chapters, attachments and lyrics positioning aligned during delivery.

### File naming

- Updated filename generation to apply corrected media metadata consistently after probing and selection.

## [2.3.5] - 2026-10-08

### Interface language

- Added Italian (`it`), Japanese (`ja`), Korean (`ko`) and Russian (`ru`) as shipped interface catalogues alongside English, Chinese, Spanish, French and Portuguese.
- Language names in the interface-language picker now follow the current interface language.

### Local key vault

- Accepted both canonical and little-endian PlayReady KID spellings during local vault lookup while keeping service-scoped matches preferred.

### Bitrate metadata

- Improved actual video bitrate sampling by distributing probes across the presentation and rejecting unreliable measurements.

## [2.3.4] - 2026-10-08

### Default audio

- Added a separate default-audio preference for muxed output, including language and original-audio matching, batch-safe fallback, export persistence and container capability reporting.

### Subtitle conversion

- Improved subtitle conversion for SRT, WebVTT, ASS and TTML/fragmented-MP4 sources while preserving supported cue positioning, styling and document metadata.

## [2.3.3] - 2026-10-07

### Windows progress

- Fixed download progress cards that could remain empty in narrow Windows terminals, including Python 3.14 environments.

### Track selection

- Added detailed language, codec, audio-profile, channel, subtitle-type and quality selection controls with safe manual picker recovery.
- Documented the track-selection filters, ranking behavior and manual fallback in the new track-selection guide.

## [2.3.2] - 2026-10-06

### Windows paths

- Hardened nested temporary, resume, decryption, subtitle, live-pipe and post-processing paths so derived filenames stay within the Windows legacy path limit.

### Windows live recording

- Added native Windows named-pipe support for real-time FFmpeg audio/video muxing while preserving the existing POSIX FIFO path on macOS and Linux.

### Track selection

- Added hard audio codec/profile/channel, subtitle-kind, language and best/worst quality filters, with manual Track Picker recovery when no automatic combination matches.

## [2.3.1] - 2026-10-05

### Windows paths

- Shortened Windows output and temporary paths when legacy path limits would otherwise cause `WinError 206` during download, post-processing, or cleanup.

### Service diagnostics

- Allowed service authors to disable persisted Debug/API diagnostics with `DEBUG_LOGGING = False` while keeping the user-visible TUI notice.

### Service registration

- Preserved registered services across temporary discovery failures and recovered registrations cleared by older startup logic.

### Terminal status

- Centered the status information row across the available terminal width.

## [2.3.0] - 2026-10-04

### Service discovery

- Discovered service code installed through pip, source checkouts, and user service folders so registered services are available without an extra `uv sync` step.

### Track metadata

- Kept clear DASH/ISM audio tracks from being marked as encrypted when only the video requires DRM.

### Terminal layout

- Corrected terminal cell widths for Thai and other combining characters so translated labels and track details remain aligned.

## [2.2.9] - 2026-10-03

### Windows input and paste

- Fixed Windows Terminal account-field paste events so copied credentials are
  inserted into the focused field.
- Preserved printable Unicode characters such as `@` while ignoring modifier
  and synthetic control records that could incorrectly open Settings.

### Filename formats

- Added selectable dot, space and hyphen filename styles under **Files & naming**
  while keeping custom title and release templates available.

## [2.2.8] - 2026-10-02

### Batch track selection

- Expanded queued video track pickers into the available terminal space and hid
  stale delivery panels, keeping later batches selectable in short windows.

### Windows console input

- Stabilized Windows console input mode and mouse reporting, including recovery
  when the host resets the mode and cleanup of pending reports on exit.

### Selected-track KIDs

- Reported KIDs from the selected rendition and initialization data so Core,
  the delivery screen and the native downloader show the same key identity.

### Windows temporary paths

- Bounded temporary directory and section names to avoid legacy Windows path
  length failures while retaining resume identity through stable digests.

## [2.2.7] - 2026-10-01

### CDM identity

- Displayed the selected CDM's authoritative security level and Widevine system
  ID in the service identity row when the device provides those values.

### Windows Terminal compatibility

- Disabled incompatible Kitty keyboard negotiation on Windows and kept the TUI
  on a reliable 256-colour path, restoring mouse input and Ctrl+S settings access
  on affected Windows Terminal and Python 3.14 installations.

## [2.2.6] - 2026-09-30

### Live recording

- Fixed Pluto TV live Matroska recording when independent HLS AAC audio needs
  to be probed before FFmpeg writes the output header.

### HDR metadata

- Detected HDR10+ and related video-range markers from DASH media BaseURL
  metadata when adaptation-level fields are absent.

### BBC iPlayer and dependencies

- Kept BBC iPlayer subtitle sidecars in their provider format without requiring
  the optional external subtitle helper.
- Updated the MonaLisa DRM and HTTP SOCKS dependency metadata and lockfile.

## [2.2.5] - 2026-09-29

### HLS audio decryption

- Corrected clear Apple HLS audio fragments that contain ordinary `roll`
  sample groups, while retaining strict handling for encrypted CBCS/CENC data.

### Subtitle handling

- Treated valid empty WebVTT, TTML and SRT sidecars as tracks with no cues
  instead of reporting a misleading ffmpeg conversion failure.

### Local binaries

- Added project-level `binaries/` lookup as a fallback after the operating
  system `PATH`, without overriding system-installed tools.

## [2.2.4] - 2026-09-28

### Audio and clear-track metadata

- Preserved Amazon E-AC-3/AC-3 5.1 channel metadata when a generic MP4 init
  field reports stereo.
- Prevented clear audio tracks with provider bookkeeping KIDs from being
  labelled as encrypted CENC tracks.

### Export and vault handling

- Reduced native export size by omitting unused service-private title payloads
  while keeping older exports readable.
- Queried remote key vaults using both canonical and PlayReady byte-order KID
  aliases.

### Dependencies

- Updated the public dependency and lock metadata for the current service
  integrations.

## [2.2.3] - 2026-09-27

### BBC iPlayer playback

- Updated BBC source profiles to UHD, FHD and HD while preserving compatibility
  with existing resolution settings.
- Added multi-source VOD selection, chapter metadata and shared subtitle
  references for BBC playback.

### DASH and HLS delivery

- Preserved the selected HLS rendition KID in exported downloader commands by
  requesting the matching media playlists when needed.
- Accepted explicitly clear fragmented MP4 tracks even when a provider marks
  their adaptation set as encrypted, while retaining strict CENC/CBCS handling
  for encrypted media.

## [2.2.2] - 2026-09-27

### Compiled service loading

- Improved source-free compiled service loading by discovering sibling native
  extensions and preserving canonical package namespaces for relative imports.

### DRM key vault matching

- Corrected vault key inventory handling when services declare authoritative KIDs
  even when selected stream rows are not marked encrypted.
- Allowed explicitly selected PSSH metadata to participate in selected-track
  licensing without reintroducing unrelated manifest KIDs.

## [2.2.1] - 2026-09-26

### Shared subtitle sidecars

- Added the generic `augment_tracks()` service hook for subtitle inventories
  returned by separate provider APIs.
- Added service-owned subtitle sidecars to the shared track picker and applied
  the shared `sub_langs` selection before `prepare_download()` fetches files for
  muxing.
- Included selected sidecar subtitle rows in command records and documented
  the service integration contract.

## [2.2.0] - 2026-09-26

### Fragmented media postprocessing

- Corrected timestamp restamping when one HLS/CMAF segment contains multiple
  MP4 fragments, preventing accumulated audio/video drift.

### Windows state storage

- Made settings, service registration, token, cookie and cache writes work on
  Windows where `os.fchmod` is unavailable.

## [2.1.9] - 2026-09-25

### Playback metadata

- Added optional actual video bitrate sampling for the selected video tracks.
- Corrected audio channel metadata from the selected MP4 initialization segment
  when a manifest reports a stale channel count.
- Refreshed the download details view after the metadata probes complete.

### Remote CDM and live recording

- Fixed PlayReady remote CDMs being omitted from service-level CDM choices when
  older configuration entries still declared `system: widevine`.
- Showed a stored-value hint when editing remote CDM and vault secrets without
  rendering the credentials themselves.
- Reduced live recording finalization space usage by releasing each full-size
  intermediate as soon as the next stage is verified, with recovery of the
  raw recording or a valid MP4 fallback when finalization fails.

## [2.1.8] - 2026-09-25

### Title attachments

- Added per-service poster, thumbnail and artwork attachments with optional TUI previews.
- Preserved attachment aspect ratios in native terminal image widgets with letterboxed rendering.
- Downloaded enabled attachments as separate files without muxing them into media.
- Added attachment metadata to native exports and imports.

## [2.1.7] - 2026-09-24

### Compiled-only service compatibility

- Added ABI- and platform-aware selection for services that ship several
  compiled-only variants in one package.
- Load the selected native service and API extensions under their canonical
  service namespace, preserving normal relative imports and isolating
  incompatible variants without blocking other services.
- Expanded compiled-service documentation with multi-variant packaging and
  compatibility guidance.

## [2.1.6] - 2026-09-24

### DRM and chapter compatibility

- Added a PlayReady KID-only PSSH fallback that builds the minimal WRMHEADER
  needed by licence services when a manifest omits a full PRO payload.
- Added `chapters_file` support to audio conversion, so chapter metadata can be
  embedded in MP3, M4A, FLAC and ALAC output as well as video containers.
- Kept chapter validation and automatic mux decisions consistent for selected
  audio and video tracks.

### Service loading and packaging

- Added ABI/platform-aware compiled-only service registration and readiness
  reporting without requiring private service source code in the release.
- Standardized the public release-note format and kept package metadata and
  documentation aligned with the shipped feature set.

## [2.1.5] - 2026-09-23

### Diagnostics and safer batch handling

- Added timestamped session and per-task debug diagnostics with task IDs,
  request/response metadata, bounded response previews and full tracebacks.
- Redact credentials, cookies and bearer tokens in diagnostics while retaining
  signed manifest URLs and content keys; record licence challenge/response
  payloads only when a licence exchange fails.
- Keep debug logging active when the TUI replaces the normal log callback and
  when downloads run concurrently.
- Opening a multi-title export from the Home input now opens the title chooser
  instead of silently starting the first entry.
- Document the debug log layout and sensitive-data policy.

## [2.1.4] - 2026-09-22

### Public readiness and documentation

- Classify readiness gaps as core (red), global capability (yellow), or
  optional enhancement (blue); fully ready installations are green.
- Include CDM devices and Dolby Vision Hybrid tools in the readiness report,
  with status translations in all six interface languages.
- Document the dependency tiers and explain which capabilities are required
  for the base application, DRM playback, or optional enhancements.
- Replace unpublished service implementation examples, internal service
  names, and device-specific paths with public-package references and portable
  placeholders.
- Refresh the public installation, configuration, vault, troubleshooting, and
  downloader documentation to match the released package.

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
- Removed unshipped service-specific decryption and live transport bindings.
- Retained generic export compatibility and common DASH/HLS/ISM download paths;
  format compatibility does not imply that a provider service is bundled.

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
