# Settings

Three tiers, kept apart on purpose. Click Settings or press `ctrl+s` anywhere
to edit them; plain `s` is the shorter form when no text field has focus.
Changes persist immediately and apply to the next request without a restart.

## Content-key verification

**Settings → Download behavior → Verify content keys before download** is off
by default. A service can override it under **License & vaults**. It checks only
final selected encrypted VOD tracks, after key acquisition and before transfer;
live, export-only and command-only operations are skipped. A clear lead does not
count as successful verification. Inconclusive samples warn without rejecting
keys. Failed vault candidates try other enabled sources, then the configured
service/CDM once. See [Content-key verification](key-verification.md) for limits,
fallback rules, standalone commands and vault-write safety.

## Where values are kept

`<paths.home>/settings.json`, one object per scope — `@global` and one per service id.

## Services and homepage visibility

Global Settings → **Services** contains the global **License action**
default, **Chapter metadata by service**,
**Title attachments by service**, **Actual video bitrate by service**,
**Register a service**, **Services shown on home**, and **Export manifest type**.
UniDL discovers service packages from the active package directory, the source
checkout's `src/unidl/services`, the current project's `services` directory and
the writable `<paths.home>/services` directory. It shows already registered
packages as disabled entries. To add a service, copy its completed service
package (or single-file service module) into the writable directory shown on the
home setup notice, open **Settings → Services → Register a service**, and select
it. Registration records the choice and asks you to restart UniDL; the restart
is required for the package to be imported in the new process and for the
registration to take effect on the homepage grid or global search. If you are
unsure which installation is running, open **Settings → Files & naming →
Advanced paths**. **Service directory** shows that installation's actual
service directory. Click the row or press Enter to open it. If you are
running a source checkout with the `unidl` command from another installation,
run `uv sync` (or `python -m pip install -e .`) in that checkout, or set
`UNIDL_SERVICES` to the directory containing the package. Homepage visibility is
a separate checkbox list: an unchecked registered service remains available to
global search but is omitted from the homepage grid. If a previously registered
package is temporarily unavailable during startup, UniDL keeps its registration
and will use it again when the package is discovered.

**Export manifest type** is stored independently in each registered service's
`settings.json` namespace. The legacy/default value is **master manifest**: a
native export retains the original authorized MPD, ISM or HLS entry exactly as
before. When multiple authorized master manifests were merged, it records all
contributing master URLs instead of expanding their complete segment lists.
Import reads exactly those saved URLs, without asking the installed service to
authorize fresh variants. If an original reference cannot be recovered safely
(for example an inline JSON source or a concatenated timeline), a bundle of the
original sources and required track metadata is retained instead. Missing
provenance is an explicit error, never a silently incomplete export.

**All media manifests** stores original MPD/ISM text or the original HLS media
playlists, plus the track metadata needed to interpret them. It does **not**
write parsed segment lists, range tables or generated init bytes into JSON.
Import expands the saved protocol documents through the normal parsers, without
requesting the original master again. A direct media URL or an original JSON
track list remains a source reference; export does not download the media.

Both modes preserve independent API subtitle URLs, including unselected
languages, Normal/SDH/Forced kinds, format, original-language flags and required
request headers. Multiple authorized profiles retain their individual request
and returned-type labels and track associations, even when duplicate tracks
collapse to one picker row. Source labels and subtitle URLs are not the large
playback/API envelopes: those and expanded JSON segment lists remain excluded.
On import, an installed service keeps its custom sidecar hooks; without it,
direct subtitle sources use generic delivery. Expired or missing URLs cannot be
recovered from labels alone; recreate an older export if it omitted them.

The export choice is a third setting boundary, separate from both provider
manifest profile and final track selection. Media export records every parsed
track, not only the tracks selected for that run; the imported title can therefore
be selected again as long as its required content keys and media URLs are still
valid. It does not renew CDN authorization: if the media URLs themselves expire,
the export expires with them. Live manifests are never frozen as a static media
snapshot; even when a service is configured for media export, live playback keeps
its refreshable original manifest and reports that fallback in the log.

Import recalculates the release suffix from the final selected tracks. Choosing
1080p SDR and stereo AAC from a 2160p HDR/DDP export therefore writes
`1080p.DEMO.WEB-DL.AAC2.0` followed by the selected video codec, with no HDR tag
(where `DEMO` represents the source's release tag).
New exports retain the exact title portion separately so custom release
templates can also be rebuilt. Older conventional dot, space and hyphen names
remain supported without editing the original export.

Native exports use format version 3. New JSON manifests and exports store source
documents, not expanded downloader state. Raw MPD/HLS/ISM text naturally contains
its own segment syntax; UniDL does not duplicate that syntax as JSON segment
objects. Expanded segments belong only to runtime parsing and resume caches.
Version-1 and version-2 exports remain readable from their existing files;
their segment snapshots are not copied into new files. Keep the original file
when replaying such an import. Older readers reject version 3 explicitly.

Each export file, including an entire batch, is limited to **32 MiB**.
Smaller documents remain indented; a large source bundle may
use compact JSON spacing to fit the limit. If it still exceeds the limit, export
fails explicitly before replacing or creating the file. An existing batch file
remains intact. Use smaller batches or Master mode; UniDL does not truncate
tracks, discard titles or switch modes silently to force the file to fit.

If you installed a minimal distribution with no service packages, the home
screen shows the same import path and links to Settings. Copy a service into the
directory shown there, register it, then restart UniDL. The CDM setup notice is
independent: a service can be registered without a CDM, but DRM playback still
needs a matching `.wvd` or `.prd` device.
Every change is written the moment it is made, and written so that it survives the
process not coming back:

- **atomically.** A temporary file is replaced into place, so being killed
  mid-write cannot leave half a file. It used to truncate the real one, and an
  unreadable file was answered by starting from an empty dict — which reads as
  "every setting went back to default".
- **with a fallback.** The previous file is kept as `settings.json.bak` and used
  when the current one cannot be read. The damaged file is never copied over it.
- **locked and merged.** A cross-process lock covers the final read, delta merge,
  backup and replace, so a second unidl - or a check script - writing the same
  file does not have its values erased.
- **owner-only.** The current file, backup, temporary file and lock are all `0600`.

A write that fails - read-only home, full disk - is recorded rather than raised,
so it can be reported instead of looking like a setting that would not stick.

```
Global                six concise managers for download, DRM/vault, files, proxy, interface and JustWatch
<Service>             that service's own vocabulary
Tracks and output     the shared quality vocabulary
```

Every native service also keeps its own CDM-device rows in its service section:
one `Widevine CDM`, `PlayReady CDM` (or another registered system) per system it
declares. An empty row means “use the app-wide choice”; a named row affects that
service only. **Settings → DRM & vaults** manages the local files and remote CDM
definitions themselves, but it does not replace these per-service selections.

Static things — CDM paths, credentials, directories — are **not** settings. They
live in `unidl.yaml`. See [configuration.md](configuration.md).

## Why service settings and track settings are separate

They act at different moments.

**Service settings apply before the request** and usually change *which manifest
you get*. A service may expose one profile or a multi-select when each selected
profile maps to a verified API request. Core parses those service-authorized
manifests independently, merges their track ladders and removes duplicate
representations by their displayed media properties (not by short-lived signed
URLs or provider-specific representation IDs); it never invents profile names or
derives URLs from shared track preferences. The example service has `hd/uhd`
manifest profiles; BBC has `auto/uhd/fhd/hd`. Other service packages may
declare platform, region or local-market choices. These vocabularies do not
automatically translate into each other, so each
service declares its own and they only appear while that service is active.

**Track settings apply after parsing**, against the real ladder, and are one
shared vocabulary for every service. `1080` means the same thing everywhere
because it is matched against actual resolutions.

### Manifest profile is not track selection

These are two different choices and must never be represented by the same setting:

1. **Getting a manifest profile** happens before the MPD or playlist is opened.
   A service-owned setting chooses the provider's source profile or URL, such as
   its resolution, codec, dynamic range, platform, or API capability family.
2. **Choosing tracks from the manifest** happens after that manifest has been
   parsed. The shared `video_quality`, `video_codec`, `video_range`, audio and
   subtitle settings choose representations from the ladder that was actually
   returned. They do not choose another source URL.

For example, a service may expose `manifest_resolution`, `manifest_codec` and
`manifest_color` for the source manifest. Its shared `video_quality`,
`video_codec` and `video_range` settings remain available for the final tracks
inside that manifest. A 4K Dolby Vision source manifest and 1080p SDR output
tracks are therefore a valid, intentional combination. A service port must not
read a shared track setting to decide which provider manifest to fetch when a
service-level source setting exists.

The same boundary applies when a provider returns media assets beside the
manifest. A provider-specific audio playlist is selected and fetched by that
service's source/API contract. For subtitles returned by a separate subtitle
API, the service exposes the complete inventory as Core sidecar track rows via
`augment_tracks`; they appear beside manifest subtitles in the normal picker,
and shared `sub_langs` selects them. `prepare_download` fetches the selected
sidecars and passes local files to native delivery through `Playback.mux_imports`.
The shared setting never decides which subtitle API request is made.

This is also why neither a service API option nor a command-line default can
truthfully choose a final bitrate before the returned ladder has been parsed.

A provider may return its full authorized ladder without exposing a separate
source-resolution setting. The parsed manifest, not a catalogue quality badge,
is the authority for the resolution actually delivered. Shared
`video_quality=best` preselects the highest matching representation;
`track_mode=auto` accepts that selection without opening the track picker.
Provider entitlement and CDM requirements remain independent constraints.

If playback creates a provider-side session, that service must own its renewal
and close it after delivery or cancellation. See
[Playback lifecycle](playback-lifecycle.md); no particular session endpoint or
heartbeat interval is part of the shared settings contract.

## Global

| Setting | Values | Default | Notes |
|---------|--------|---------|-------|
| `download_manager` | manager screen | — | Opens **Download behavior**, which owns `after_resolve`, live defaults, batch confirmation and native transfer options. Licence timing defaults are configured under **Services** and may be overridden per service. |
| `services_manager` | manager screen | — | Opens **Services**, including registration, homepage visibility, chapters, attachments, actual video bitrate, licence defaults and export manifest type. Metadata and bitrate switches can be set independently for each service. |
| `resource_manager` | manager screen | — | Opens **DRM & vaults**, the single place to choose the app-wide DRM/CDM, edit CDM rules, add/edit/delete remote CDMs and vaults, enable/disable backends, choose local vaults, and open vault policy. |
| `storage_manager` | manager screen | — | Opens **Files & naming**, the single place to edit ordinary output folders and file-name templates, preview a sample release name, and inspect sensitive runtime paths without moving them. |
| `proxy_manager` | manager screen | — | Opens **Proxy & VPN**, where the default route, segment-download routing, named endpoints and HTTPS proxy providers are managed together. |
| `justwatch_manager` | manager screen | — | Opens **JustWatch search**, where the one title-catalogue country and the separate availability-country list are edited together. The two underlying values remain independent. |
| `interface_manager` | manager screen | — | Opens **Interface & diagnostics**, which owns the `theme`, `interface_locale` and `debug` choices. |
| `after_resolve` | `download` / `command` / `ask` | `download` | Compatibility key controlled by **Download behavior**. `command` reproduces the old scripts' behaviour of only emitting a command; the main screen shortcut `d` still cycles it. |
| `fetch_actual_bitrate` | on / off | **on** | Controlled by **Settings → Services → Actual video bitrate by service** or the individual service's settings. Unconfigured services inherit the global default; saved service overrides are independent. Press `r` on a service row to resume inheritance. Existing global preferences are preserved. After the final video tracks are selected and the download details screen is visible, UniDL samples distributed media segments to show an actual bitrate beside the manifest estimate. During that same probe it may read the selected video's bounded MP4 initialization metadata to correct a missing HLG/HDR label before final naming. A probe is accepted only when the complete segment size is known; unreliable CDN responses are omitted. Off skips all of this and keeps manifest metadata only. |
| `drm_system` | registered DRM systems | first registered system | Compatibility key controlled by **DRM & vaults → DRM system**. A service that declares exactly one system pins that system; a multi-system service may expose its own override. |
| `cdm_rules` | resolution-to-device rules | empty | Compatibility key controlled by **DRM & vaults → CDM rules**. Rules choose the app-wide CDM by output resolution and never replace a service's own CDM setting. |
| `theme` | `dark` / `light` | `dark` | Compatibility key controlled by **Interface & diagnostics**. Also switch with `ctrl+t`; changes repaint the current screen immediately. |
| `interface_locale` | `system` / `en` / `zh-Hans` / `zh-Hant` / `es` / `fr` / `pt` / `it` / `ja` / `ko` / `ru` / `de` | `system` | Compatibility key controlled by **Interface & diagnostics**. Language of UniDL's own labels and settings. German is shown as Deutsch. Service names, titles, URLs and API locales are not translated. |
| `local_vault` | on / off | on | Global **DRM & vaults → Vault policy** value, inherited by each service until explicitly overridden under **License and vaults**. It governs automatic local lookup; it is not a duplicate backend-enable switch. |
| `remote_vault` | on / off | off | Global **DRM & vaults → Vault policy** value, inherited by each service until explicitly overridden. It remains the master network/write safety gate for remote-vault operations. |
| `remote_vault_home_search` | on / off | on | Compatibility key controlled by **Vault policy → Remote operations**. Offers an explicit remote KID search row on the Home/search screen; it never searches while typing and requires a selected searchable backend. This explicit action is independent from the automatic playback/write `remote_vault` gate. |
| `remote_vault_manual_add` | on / off | on | Compatibility key controlled by **Vault policy → Remote operations**. Allows the Home/search **Add keys** editor to send manually entered pairs to selected remote writable vaults. Local manual writes are unaffected. |
| `remote_vault_auto_store` | on / off | on | Global vault-policy value inherited by services until overridden. After this service's licence succeeds, writes acquired pairs to selected remote writable vaults. |
| `remote_vault_auto_lookup` | on / off | on | Global vault-policy value inherited by services until overridden. Queries selected remote vaults immediately before this service's licence request; complete hits skip that request and partial hits are merged with the service response. |
| `vault_read_targets` | multi-select configured vaults | all | Global **Automatic playback lookup** destinations inherited by services until overridden under **License and vaults**. |
| `vault_search_targets` | multi-select search-capable vaults | local vaults | Compatibility key controlled by **Vault policy → Home-screen key search**. Remote search remains explicit from the search result. |
| `vault_write_targets` | multi-select writable vaults | all writable | Global **Store acquired keys** destinations inherited by services until overridden. `no_push` backends are omitted. |
| `live_record` | on / off | **off** | Compatibility key controlled by **Download behavior**. In the TUI this preselects the record-vs-command question asked after final track selection; headless runs use it directly. A recording then uses the service's `live_record_limit` default unless the user changes it for that run; `00:00:00` means unlimited until stopped. See [live.md](live.md). |
| `license_action` | `before` / `after` / `custom` | **before**, or **custom** for supporting services | Global Before/After default under **Settings → Services**; per-service override under **License and vaults**. Custom appears only for explicitly supporting services. Legacy `license_after_tracks` true/false remains readable as After/Before. See [License action](drm.md#license-action). |
| `dolby_vision_hybrid` | on / off | **off** | Global default under **Settings → Services**. Services inherit it until overridden under **License and vaults**. For VOD only, a selected HDR10/HDR10+ base is combined with the lowest-resolution available DV layer from the same manifest or merged profile ladder using `dovi_tool`. |
| `download_dir` | a path | empty | Compatibility key controlled by **Files & naming → Output locations**. Where downloads and live recordings land. Empty uses YAML `paths.downloads` when configured, otherwise `~/unidl_downloads`, grouped by service. No YAML editing is required; `~` is expanded and the folder is created if it does not exist. |
| `episode_folder_template` | relative template | empty | Compatibility key controlled by **Files & naming → Naming templates**. Adds optional folders below `downloads/<service>/` for episode titles only. Empty preserves the flat layout. Use `{series}`, `{season}`, `{season_number}`, `{year}`, `{season_episode}` and `{service}`; `{series}/{season}` is a typical choice. Movies, live titles and audio remain in the service folder. |
| `filename_style` | `dot` / `space` / `hyphen` | `dot` | Compatibility key controlled by **Files & naming → Naming templates**. Chooses separators for the final title and release name while preserving the selected template fields. |
| `debug` | on / off | off | Compatibility key controlled by **Interface & diagnostics**. Verbose logging, full URLs, keeps temp files, writes stream metadata and a per-task log. A service may explicitly set `DEBUG_LOGGING = False` in its service class to suppress diagnostic files for that service; the TUI reports the opt-out. Also enables the Home screen's explicit `ctrl+r` service-code reload; there is no automatic watcher. |
| `confirm_batch` | on / off | off | Compatibility key controlled by **Download behavior**. Ask once before processing a multi-episode selection. |
| `retries` | integer | `5` | Compatibility key controlled by **Download behavior**. Segment retry count for the native engine. |
| `http_timeout` | seconds | `30` | Compatibility key controlled by **Download behavior**. HTTP timeout for native segment requests. |
| `max_speed` | e.g. `15M` | empty | Compatibility key controlled by **Download behavior**. Empty means unlimited. |
| `title_delay` | `off`, `30`, or `20-40` | `off` | Compatibility key controlled by **Download behavior**. Adds a random or fixed delay between automatically queued VOD titles. It does not delay tracks within one title or live recording segments. |
| `muxer` | `auto` / `ffmpeg` / `mkvmerge` | `auto` | Compatibility key controlled by **Download behavior**. Which mux tool writes the final file. Per-service **Container** still chooses mkv vs mp4. |
| `segment_downloader` | `python` / `aria2c` | `python` | Compatibility key controlled by **Download behavior**. `aria2c` is used only when chosen and installed. |
| `resume_parts` | on / off | on | Compatibility key controlled by **Download behavior**. Reuse verified segments and completed track-stage checkpoints after failure or cancellation. Off starts fresh. |
| `check_segments_count` | on / off | on | Compatibility key controlled by **Download behavior**. Fail a track whose downloaded part count does not match the manifest. |
| `keep_temp` | on / off | off | Compatibility key controlled by **Download behavior**. Debug mode also keeps temp files. |
| `keep_failed_intermediates` | on / off | off | Compatibility key controlled by **Download behavior**. Keeps failed VOD track/mux staging for debugging. Valid resume checkpoints are separate and follow `resume_parts`; `keep_temp` controls retention after success. Captured live recordings are preserved on Quit or failure regardless of this setting. |
| `delete_temp_after_done` | on / off | on | Compatibility key controlled by **Download behavior**. Remove temp directories after a successful title. |
| `auto_subtitle_fix` | on / off | on | Compatibility key controlled by **Download behavior**. Tidy converted subtitle cues. |
| `live_real_time_merge` | on / off | on | Compatibility key controlled by **Download behavior**. Append live segments while recording. |
| `live_keep_segments` | on / off | off | Compatibility key controlled by **Download behavior**. Keep per-segment live files. |
| `live_pipe_mux` | on / off | on | Compatibility key controlled by **Download behavior**. Mux compatible live audio/video while recording into a visible, growing file in the download directory. When unavailable/disabled, tracks are muxed after recording stops normally. Audio conversion may disable pipe mux. |
| `fetch_chapters` | on / off | on | Legacy global default for services without an explicit chapter policy. Edit each service under **Services → Chapter metadata by service**, or in its own settings. Acquisition and **Embed chapters in the final file** are independent. |
| `justwatch_search_region` | one ISO country code | first built-in region | Compatibility key controlled by **JustWatch search → Title catalogue**. The JustWatch search screen can edit it directly. |
| `justwatch_regions` | comma-separated ISO country codes | built-in region list | Availability lookup order. Each region is a separate request; edit it from Settings or the region picker. See [availability.md](availability.md). |

### Ending a live recording

- **Back** ends capture like reaching the duration limit. It completes the
  current segment batch, closes the real-time mux or runs the post-recording
  mux, saves the final file, then returns. A paused recording is unpaused for
  this finish; repeated Back does not cancel finalization.
  If a rotated-key prompt is open, Back finishes the already captured portion
  and excludes the pending batch whose key is unavailable.
- **Quit** uses fast cancellation. It does not start extra mux, decryption or
  transcoding passes. Already-written recordings are kept, but an interrupted
  container may be incomplete and raw tracks may still require processing.
- A real-time mux file is visible directly in the download directory during
  recording. Separate tracks live in `<name>.recording/`. Successful normal
  completion removes superseded tracks only after the final file is committed,
  respecting the existing retention settings. Quit, failed mux or failed
  commit preserves captured media there; a failed finish stays on screen for
  inspection/retry. Only empty owned recording directories/files are removed.
- Ordinary VOD cancellation and live replay taken **as VOD** keep their existing
  cancellation/resume behaviour.

### JustWatch search

The manager presents two deliberately different choices:

- **Title catalogue** (`justwatch_search_region`) is one country. It decides which
  catalogue supplies title matches and localized names.
- **Availability regions** (`justwatch_regions`) is a list. JustWatch makes one
  offer lookup per country to answer where the selected title is available.

The JustWatch search screen and the availability results screen keep their own
`Ctrl+R` pickers, so a one-off lookup does not require opening global Settings.

The first-level menu deliberately contains only these six managers: **Download
behavior**, **DRM & vaults**, **Files & naming**, **Proxy & VPN**, **JustWatch
search**, and **Interface & diagnostics**. The rows below that table are compatibility
keys and are not duplicated in the main menu.

### Files & naming

The global Settings list shows one **Files & naming** row rather than six
separate path/template rows. Its tabs are:

- **Output locations** — the `download_dir` setting plus `paths.subtitles`,
  `paths.commands` and `paths.exports`. Saving a path creates the directory but
  never moves existing files. Resetting one removes that override and returns to
  the configured/default location.
- **Naming templates** — `name_template_episode`, `name_template_movie`,
  `release_template` and `release_tag`, with an episode and film preview. These
  remain ordinary `settings.json` keys; the manager is only their coherent UI.
  `episode_folder_template` is edited in the same tab and controls optional
  episode folders below the service directory; it does not change the file
  name or move existing downloads.
  **Filename format** is the quick preset above those templates: **Dots (scene)**
  keeps the traditional `Real.Time.With.Bill.Maher.S24E29.720p...` form,
  **Spaces** uses `Real Time With Bill Maher S24E29 720p...`, and
  **Hyphens between sections** uses `Real Time With Bill Maher-S24E29-720p...`.
  The choice changes separators only; the templates still control which title,
  quality, audio, video and tag fields are present.
  The release template can use `audio`, `audio_channels`, `audio_full`, `atmos`
  and `video`. Its default produces names such as `AAC2.0`, `DDP5.1.Atmos` and
  `H.265`. These values are calculated only after the output picker is
  confirmed, from the tracks the user actually leaves selected—not from its
  automatic initial checkmarks or from the complete manifest inventory.
- **Advanced paths** — the running installation's **Service directory**,
  `home`, cache/temp/logs, tokens, cookies, helpers, CDMs and the default
  local vault. Paths remain read-only: click a row or press Enter to open its
  folder in the system file manager. The vault row opens its containing folder,
  not the database. Existing files are never moved and no path setting is changed.
  Viewing or opening paths creates nothing; missing directories are reported
  without creating them. Without a desktop opener, UniDL shows the full path
  instead.

The installed service path is derived from the running package, not guessed from
the working directory, so source, pip, virtual-environment and uv installations
show their own location. Copy a completed service package there, register it
under **Settings → Services → Register a service**, then restart UniDL. This
read-only row does not add a `paths.services` configuration key; existing
user-service, custom `UNIDL_SERVICES` and project discovery paths continue to
work as before.

The hidden compatibility keys remain available to code, existing settings files
and verification scripts. No service setting or Track output selection setting
is moved into this manager.

### Proxy & VPN

The global Settings list exposes one **Proxy & VPN** row. Its Routing tab accepts
an empty value (direct), a full `http(s)://`/`socks` URI, a name under `proxies`, or
a provider query such as `expressvpn:us`, `nordvpn:us`, `surfshark:gb` or
`windscribe:ca:toronto`.
`proxy_downloads` separately controls whether native delivery core's segment requests use the
resolved route; UniDL's own API, manifest and licence requests remain on it.

The manager supports static HTTP/HTTPS/SOCKS endpoint pools and HTTPS proxy
endpoints from ExpressVPN, NordVPN, Surfshark and Windscribe. ExpressVPN uses an
explicit OAuth device-authorization screen (`l` on its provider row); its rotating
refresh/access/connection tokens are written owner-only under
`<paths.tokens>/vpn/expressvpn_tokens.json` and refreshed headlessly later.
NordVPN, Surfshark and Windscribe use manual-setup service credentials stored in
`unidl.yaml`. Providers are resolved only when selected and do not change the
computer's system VPN/default route. Gluetun, Proton TV login and Hola still require
external-process or different login lifecycles and are not silent URI fallbacks. A
service-level Proxy setting remains available and overrides the app-wide route when
non-empty.

Two things happen on **every** run regardless of these settings:

- the UniDL command is written to `<paths.commands>/<service>/<name>_<timestamp>.txt`
- acquired `KID:key` pairs are stored in the key vault with the service and title

## Tracks and output

Each service page places a **License and vaults** section immediately before
this shared section. It controls licence timing and that service's local/remote
vault lookup, storage, and target lists. These settings are independent per
service when explicitly overridden; otherwise they inherit the values from the
global **DRM & vaults → Vault policy** and **Services** settings. Home-screen
remote search and manual key-add permissions remain global. Press `r` on a
service policy row to clear its override and return to the global value. Rows
display the effective value and mark whether it follows global settings or is a
service override. Explicit off and explicit empty destination selections are
overrides, not missing values. Opening an editor never saves inherited defaults.

With **License action → License after final track selection**, only the final selected
encrypted tracks contribute KIDs/PSSH values to vault lookup and licensing.
Unselected manifest renditions are not queried or licensed.

**Dolby Vision hybrid output** is a separate post-download transformation, not a
manifest profile and not a replacement for final track selection. When enabled,
Core adds the matching DV/HDR10 ingredient to the selected ladder so both
ingredients receive normal service-owned licence/vault handling. After both
decrypted files finish, `ffmpeg`, `dovi_tool` and `mkvmerge` inject the DV RPU
into the HDR10 base and pass one hybrid video to the final mux. It is disabled
by default, ignored for live/replay streams, and safely leaves the original
tracks alone when one layer, compatible frame rate, duration, frame count or
HEVC data is missing. The DV input is the lowest-resolution available DV layer;
its resolution need not match the HDR base. `dovi_tool` must be installed and available on `PATH` (or
selected with `UNIDL_DOVI_TOOL`). The first implementation normalizes the RPU
with dovi mode 3 and keeps the source frame geometry; it does not invent L5/L6
crop metadata.

This section is **Track output selection**. Its settings answer which parsed
tracks native delivery core will download, keep and mux into the result. By
themselves they are not a licence-track picker and must never silently decide a
service's PSSH seed, requested KIDs or licence profile/API route.

That boundary applies to every setting in the table below. In particular,
`video_quality`, `video_codec`, `video_range`, the audio/subtitle settings and the
interactive checkboxes select output only. Changing the output from 720p to 4K,
AAC to Atmos, or one language to another does not implicitly rewrite a service's
licence plan.

If a platform has a configurable licence-track rule, it appears separately in
that platform's own service section, with an explicit name such as
`license_tracks`, `license_profile` or `pssh_video_profile`. The two settings may
offer similar-looking qualities or track classes, but they remain separate even
when their defaults happen to match. By default licence acquisition finishes
before the interactive output picker, so the later output representation is not
runtime input to a licence request.

**License action → After** is the explicit exception: it waits for the final
picker and hands Core only those encrypted tracks for vault/licence resolution.
**Before** uses the normal pre-selection inventory. **Custom** lets a supporting
service choose its own plan and is not offered by undeclared services. This
does not let services read shared quality/language settings to choose a provider
API request. See [License action](drm.md#license-action) for precedence,
compatibility and DASH/HLS/ISM scope.

| Setting | Values | Default |
|---------|--------|---------|
| `track_mode` | `interactive` / `auto` | `interactive` |
| `output_scope` | `package` / `video` / `audio` / `subtitle` / `custom` | `package` |
| `output_types` | comma separated `video`, `audio`, `subtitle` (Custom only) | `video,audio,subtitle` |
| `video_selection` | `best` / `all` | `best` |
| `video_quality` | `best` / `2160` / `1440` / `1080` / `720` / `480` / `worst` | `best` |
| `video_codec` | `any` / `h264` / `h265` / `h266` / `av1` / `vp9` | `any` |
| `video_range` | `any` / `sdr` / `hdr10` / `hdr10+` / `hlg` / `dv` | `any` |
| `audio_codec` | `any` / `aac` / `ac3` / `eac3` / `ac4` / `opus` / `flac` / `alac` / `mp3` | `any` |
| `audio_profile` | `any` / `atmos` / `main` / `description` / `commentary` / `dialog` | `any` |
| `audio_quality` | `best` / `worst` | `best` |
| `audio_channels` | `any` / `2` / `6` / `8` | `any` |
| `audio_langs` | comma separated BCP-47/ISO tags, empty = best language | empty |
| `default_audio` | `auto` / `original` / one language tag; only final selected audio participates | `auto` |
| `default_subtitle` | `auto` / `original` / one language tag; only final selected subtitles participate | `auto` |
| `audio_selection` | `best` / `all` | `best` |
| `sub_langs` | comma separated, `all`, or empty to skip | `all` |
| `subtitle_kinds` | comma separated `normal`, `forced`, `sdh`, `commentary`, `audio_description`, or `all` | `normal,forced` |
| `subtitle_selection` | `all` / `best` | `all` |
| `drop_video` | regex | boundary-aware `trick` / `trickplay` / `thumbnail` / `image` |
| `sub_format` | `srt` / `vtt` / `ass` / `raw` | `srt` |
| `mux_format` | `mkv` / `mp4` | `mkv` |
| `embed_chapters` | `on` / `off` | `on` |
| `download_attachments` | `on` / `off` | `on` |
| `audio_format` | `source` plus `mp3`, `flac`, and `alac` when supported | `mp3` when supported, otherwise `source` |
| `workers` | integer | `16` |
| `concurrent_tracks` | on / off | on |

`track_mode` decides whether the ladder is shown for confirmation or applied
silently. In `interactive` mode the picker arrives with the automatic selection
already checked, so `enter` accepts it. Those checkboxes still control only the
tracks handed to native delivery core for output. In the default DRM order they
appear after key acquisition. With the explicit compatibility switch on, Core
also uses the resulting track objects to locate their own KIDs/init data, without
turning any checkbox into a service profile choice.

`output_scope` controls the media types that automatic selection is allowed to
choose. `package` is the compatibility default and selects video, audio and
subtitle tracks as before. `video`, `audio`, and `subtitle` are useful for batch
jobs such as collecting every episode's subtitles without opening the picker.
`custom` reads the comma-separated `output_types` value. `video_selection` and
the existing `audio_selection`/`subtitle_selection` then choose either the best
matching track(s) or all tracks that pass the filters. Narrow scopes disable
automatic muxing so each selected track remains an independent output with a
track-aware filename.

The selected scope and Best/All rules are written into saved command metadata
and native exports. Re-running a command uses its exact selected indexes; an
imported native export restores its saved output scope before automatic
selection.

When an automatic rule has no match, a single title is offered the normal manual
picker. Any matching media types remain checked and the unmatched type is left
unchecked, so the user can repair only the part that needs attention. In a
batch, the title is recorded as skipped and the remaining titles continue; the
batch never pauses waiting for a picker that cannot share one answer across
episodes.

Automatic selection treats language, codec, profile, channels and subtitle type
as hard filters combined with AND. `best` or `worst` only ranks tracks remaining
after those filters; it never widens them. If no track matches, UniDL reports the
conditions and opens the normal Track Picker so the user can choose manually
without cancelling the delivery. `Atmos` is an audio profile, not a codec;
`audio_codec=atmos` remains accepted as a legacy alias for `audio_profile=atmos`.
Language matching accepts exact BCP-47 tags and their primary language (so
`es-419` matches `es`), while `und` and unknown provider tags are not guessed.

Subtitle conversion preserves each cue's positioning during timing repair and
deduplication. Choose **ASS + MKV** to retain common top/bottom positions, cue
coordinates and basic emphasis. SRT retains the common `{\an8}` top-alignment
extension, whose support depends on the player; it cannot express full layouts.
WebVTT preserves cue settings, STYLE/REGION blocks and markup. Original keeps the
downloaded subtitle unchanged. MP4 requires compatible `mov_text` subtitles and
may lose positioning when muxed. See
[track-selection.md](track-selection.md#subtitle-format-and-positioning).

`audio_format` applies to audio-only titles and Audio-only output scope. Its choices are discovered
from native delivery core at runtime, so the settings panel never advertises an encoder the
installed engine cannot provide. See [audio.md](audio.md).

`default_audio` controls the default playback audio in a muxed MKV/MP4, separately
from `audio_langs`, which controls downloading. Choose Auto, Original, or one
language. Missing preferences warn and fall back to Auto; they never add tracks
or cancel a download. Each batch title resolves the preference after its final
selection. Exports and saved commands preserve it. See
[track-selection.md](track-selection.md#default-audio-in-the-muxed-file) for
priority, regional matching, live recording, and container limitations.

`default_subtitle` is the corresponding muxed-subtitle setting. It is separate
from `sub_langs` and `subtitle_kinds`: those settings choose what is downloaded,
while this setting only marks one already-selected subtitle as the container
default. It supports Auto, Original, and one language tag, including service
sidecar subtitles; missing matches fall back to the selected original subtitle
or leave the default flag unset with a warning. See
[track-selection.md](track-selection.md#default-subtitle-in-the-muxed-file).

### Do not remove `drop_video` without reading this

Trick-play and thumbnail ladders are video tracks with tall dimensions. A real
example: a `1280x1440` thumbnail track can beat the genuine `1920x1080`
video when "best" was decided on height. The default regex keeps them out of
quality selection. Its terms are bounded so a normal title such as `Strickland`
is not mistaken for a `trick` track. Existing settings containing the original
`trick|thumbnail|image` default are upgraded at runtime. The legacy scripts
passed `-dv 'trick|thumbnail'` for the same reason.

## Chapter metadata

**Fetch chapter metadata** is independently configurable for each service, either
in its own settings or under **Settings → Services → Chapter metadata by service**.
Both editors save the same service-scoped value. Services without an explicit
choice inherit the legacy global value (on for a new install), so upgrading does
not silently re-enable previously disabled chapter requests. Turning it
off skips chapter endpoints and optional chapter fields before playback is
created; `embed_chapters` remains a separate per-service output choice that
controls only container muxing. Chapter metadata is best effort: a missing,
malformed or temporarily unavailable response is reduced to no chapters (with a
warning where the service can report one) and never prevents playback, licence
acquisition, downloading or muxing.

## Actual video bitrate

Open **Settings → Services → Actual video bitrate by service** to change the
global default (on for a new install) or toggle individual registered services.
The same **Fetch actual video bitrate** switch is available in each service's
settings. Both editors save the same service-scoped `fetch_actual_bitrate`
value. A service without an override follows the global default; changing the
global row does not overwrite service choices. Press `r` on a service row to
remove its override, or on the global row to restore the on default. Existing
global preferences survive this move from **Download behavior**.

Measurement still runs only after final video selection, with the download
details screen already visible. It samples selected video tracks, not the full
manifest inventory, and may correct their colour-range labels and final naming
using the same probe. It adds no probing to the initial manifest/track listing.
Off skips this probe entirely and retains the manifest bitrate and colour
metadata. Live recording is not probed, and unavailable or unreliable samples
are omitted without failing the download.

## Title attachments

**Fetch title attachments** is independently configurable for each service in
its own settings or under **Settings → Services → Title attachments by service**.
Services should check `fetch_attachments_enabled()` before requesting optional
posters, thumbnails or artwork, then place them in `Playback.attachments`.
UniDL previews attachments beside Chapters and Lyrics.

**Download title attachments** is a separate per-service switch under
**Tracks and output** (`download_attachments`, on by default to preserve existing
behavior). It saves available attachments under a separate
`<save name>.attachments` directory only after the media download succeeds.
Turn fetching on and downloading off to retain attachment counts, previews,
saved-command metadata and exports without automatically saving attachment
files. The download switch does not enable metadata fetching, remove existing
files, or affect explicitly opening a preview. Attachments are auxiliary: a failed
attachment never fails an otherwise successful media download, and attachments
are never muxed into media.

## Declaring service settings

```python
from ...core.settings import Option, Setting

SETTINGS = [
    Setting(
        key="dma",
        label="Live market (DMA)",
        kind="choice",
        options=[Option("auto", "Auto from account"), Option("501", "New York (501)")],
        default="auto",
        help="Which local station feed to request for live TV.",
    ),
]
```

`kind` is `choice`, `bool`, `text` or `int`. `resets_session=True` marks a
setting that invalidates the login; the panel labels it `(signs out)` and core
calls `logout()` when it changes. A platform, API-version, region, manifest or
delivery selector is not an authentication invalidation: it must leave every
other variant's token and cookie untouched and therefore must keep
`resets_session=False`. Only an explicit user sign-out (or a provider response
that definitively invalidates that selected session) may clear authentication
state.

Declare only settings that the service genuinely supports. `platform`, `region`
and `dma`, when present, are routing/profile selectors rather than output
resolution settings. Switching them must preserve each route's independent
token and cookie cache. Account versus TV-provider login belongs to the
service-home `Sign in` action.

The public example service declares `manifest_profile` with `hd` and `uhd`
values. This chooses a provider source before playback authorization; it does
not replace the shared `video_quality`, `video_codec` or `video_range` settings
used after parsing the returned manifest.

## Reading

```python
self.settings.get("dma")            # your own
self.settings.get("video_quality")  # shared
self.settings.get("debug")          # global, resolved through the parent scope
```

One object, three scopes, resolved by fallback. Most service settings shadow a
global key when a service declares the same name. Shared policy keys
(`fetch_chapters`, `fetch_actual_bitrate`, `license_action`, vault gates, and vault target lists)
instead inherit the global value until a service explicitly saves an override.
An explicit `false` or empty target list is still an override. Press `r` on an
inherited service-policy row to remove that override and resume global fallback.

## Storage

`<paths.home>/settings.json`, keyed by service id, with `@global` reserved for the
global tier. Overrides passed in code (used by the verification scripts) apply
for one run and are not written.
