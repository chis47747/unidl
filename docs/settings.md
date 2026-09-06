# Settings

Three tiers, kept apart on purpose. Click Settings or press `ctrl+s` anywhere
to edit them; plain `s` is the shorter form when no text field has focus.
Changes persist immediately and apply to the next request without a restart.

## Where values are kept

`<paths.home>/settings.json`, one object per scope — `@global` and one per service id.
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
manifests independently, merges their track ladders and removes exact duplicate
representations; it never invents profile names or derives URLs from shared track
preferences. Amazon has `FHD_H264_CBR_DASH` and `4K_DV_CVBR_DASH`. BBC has
`auto/4k/1080p/720p`. Xfinity has `sd|hd`. Paramount has platform, region and
local market. None of these vocabularies translate into each other, so each
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

For example, Movies Anywhere exposes `manifest_resolution`, `manifest_codec` and
`manifest_color` for the source manifest. Its shared `video_quality`,
`video_codec` and `video_range` settings remain available for the final tracks
inside that manifest. A 4K Dolby Vision source manifest and 1080p SDR output
tracks are therefore a valid, intentional combination. A service port must not
read a shared track setting to decide which provider manifest to fetch when a
service-level source setting exists.

The same boundary applies when a provider returns media assets beside the
manifest. A provider-specific audio playlist or external subtitle URL is selected
and fetched by that service's own settings. Once fetched, an external subtitle is
passed to native delivery core through `Playback.mux_imports`; shared `audio_langs` and
`sub_langs` only select tracks already present in the parsed input and must not be
reused to choose provider assets.

This is also why neither a service API option nor a command-line default can
truthfully choose a final bitrate before the returned ladder has been parsed.

## Global

| Setting | Values | Default | Notes |
|---------|--------|---------|-------|
| `download_manager` | manager screen | — | Opens **Download behavior**, which owns `after_resolve`, the live defaults, selected-track licence compatibility, batch confirmation, and native transfer options that are not in a service's **Tracks and output** list (`workers`, `concurrent_tracks`, `mux_format` and so on stay per-service). The individual compatibility keys remain in settings storage. |
| `resource_manager` | manager screen | — | Opens **DRM & vaults**, the single place to choose the app-wide DRM/CDM, edit CDM rules, add/edit/delete remote CDMs and vaults, enable/disable backends, choose local vaults, and open vault policy. |
| `storage_manager` | manager screen | — | Opens **Files & naming**, the single place to edit ordinary output folders and file-name templates, preview a sample release name, and inspect sensitive runtime paths without moving them. |
| `proxy_manager` | manager screen | — | Opens **Proxy & VPN**, where the default route, segment-download routing, named endpoints and HTTPS proxy providers are managed together. |
| `justwatch_manager` | manager screen | — | Opens **JustWatch search**, where the one title-catalogue country and the separate availability-country list are edited together. The two underlying values remain independent. |
| `interface_manager` | manager screen | — | Opens **Interface & diagnostics**, which owns the `theme`, `interface_locale` and `debug` choices. |
| `after_resolve` | `download` / `command` / `ask` | `download` | Compatibility key controlled by **Download behavior**. `command` reproduces the old scripts' behaviour of only emitting a command; the main screen shortcut `d` still cycles it. |
| `drm_system` | registered DRM systems | first registered system | Compatibility key controlled by **DRM & vaults → DRM system**. A service that declares exactly one system pins that system; a multi-system service may expose its own override. |
| `cdm_rules` | resolution-to-device rules | empty | Compatibility key controlled by **DRM & vaults → CDM rules**. Rules choose the app-wide CDM by output resolution and never replace a service's own CDM setting. |
| `theme` | `dark` / `light` | `dark` | Compatibility key controlled by **Interface & diagnostics**. Also switch with `ctrl+t`; changes repaint the current screen immediately. |
| `interface_locale` | `system` / `en` / `zh-Hans` / `zh-Hant` / `es` / `fr` / `pt` | `system` | Compatibility key controlled by **Interface & diagnostics**. Language of UniDL's own labels and settings. Service names, titles, URLs and API locales are not translated. |
| `local_vault` | on / off | on | Compatibility key controlled by **DRM & vaults → Vault policy**. It governs automatic local lookup; it is not a duplicate backend-enable switch. |
| `remote_vault` | on / off | off | Compatibility key controlled by **DRM & vaults → Vault policy**. It is the explicit network/write safety gate for remote vaults. |
| `vault_read_targets` | multi-select configured vaults | all | Compatibility key controlled by **Vault policy → Automatic playback lookup**. |
| `vault_search_targets` | multi-select search-capable vaults | local vaults | Compatibility key controlled by **Vault policy → Home-screen key search**. Remote search remains explicit from the search result. |
| `vault_write_targets` | multi-select writable vaults | all writable | Compatibility key controlled by **Vault policy → Store acquired keys**. `no_push` backends are omitted. |
| `live_record` | on / off | **off** | Compatibility key controlled by **Download behavior**. In the TUI this preselects the record-vs-command question asked after final track selection; headless runs use it directly. A recording then uses the service's `live_record_limit` default unless the user changes it for that run; `00:00:00` means unlimited until stopped. See [live.md](live.md). |
| `license_after_tracks` | on / off | **off** | Compatibility key controlled by **Download behavior**. Off uses the complete encrypted parsed inventory before output selection. On waits for the final track choice, then resolves only those tracks' KIDs/init data. This is for HLS/per-track init-data compatibility, not a service API/profile selector. |
| `download_dir` | a path | empty | Compatibility key controlled by **Files & naming → Output locations**. Where downloads and live recordings land. Empty means `paths.downloads` from `unidl.yaml`; `~` is expanded and the folder is created if it does not exist. |
| `debug` | on / off | off | Compatibility key controlled by **Interface & diagnostics**. Verbose logging, full URLs, keeps temp files, writes stream metadata and a per-task log. Also enables the Home screen's explicit `ctrl+r` service-code reload; there is no automatic watcher. |
| `confirm_batch` | on / off | off | Compatibility key controlled by **Download behavior**. Ask once before processing a multi-episode selection. |
| `retries` | integer | `5` | Compatibility key controlled by **Download behavior**. Segment retry count for the native engine. |
| `http_timeout` | seconds | `30` | Compatibility key controlled by **Download behavior**. HTTP timeout for native segment requests. |
| `max_speed` | e.g. `15M` | empty | Compatibility key controlled by **Download behavior**. Empty means unlimited. |
| `muxer` | `auto` / `ffmpeg` / `mkvmerge` | `auto` | Compatibility key controlled by **Download behavior**. Which mux tool writes the final file. Per-service **Container** still chooses mkv vs mp4. |
| `segment_downloader` | `python` / `aria2c` | `python` | Compatibility key controlled by **Download behavior**. `aria2c` is used only when chosen and installed. |
| `resume_parts` | on / off | on | Compatibility key controlled by **Download behavior**. Reuse verified tmp parts after a pause or retry. |
| `check_segments_count` | on / off | on | Compatibility key controlled by **Download behavior**. Fail a track whose downloaded part count does not match the manifest. |
| `keep_temp` | on / off | off | Compatibility key controlled by **Download behavior**. Debug mode also keeps temp files. |
| `delete_temp_after_done` | on / off | on | Compatibility key controlled by **Download behavior**. Remove temp directories after a successful title. |
| `auto_subtitle_fix` | on / off | on | Compatibility key controlled by **Download behavior**. Tidy converted subtitle cues. |
| `live_real_time_merge` | on / off | on | Compatibility key controlled by **Download behavior**. Append live segments while recording. |
| `live_keep_segments` | on / off | off | Compatibility key controlled by **Download behavior**. Keep per-segment live files. |
| `live_pipe_mux` | on / off | on | Compatibility key controlled by **Download behavior**. Mux live audio/video after recording; ignored for audio-only titles. |
| `fetch_chapters` | on / off | on | Whether services may request/parse optional chapter metadata. A failed optional chapter response never blocks playback or muxing; **Embed chapters in the final file** remains a per-service Track/output choice. |
| `justwatch_search_region` | one ISO country code | first built-in region | Compatibility key controlled by **JustWatch search → Title catalogue**. The JustWatch search screen can edit it directly. |
| `justwatch_regions` | comma-separated ISO country codes | built-in region list | Availability lookup order. Each region is a separate request; edit it from Settings or the region picker. See [availability.md](availability.md). |

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

The global Settings list shows one **Files & naming** row rather than five
separate path/template rows. Its first-phase tabs are:

- **Output locations** — the `download_dir` setting plus `paths.subtitles`,
  `paths.commands` and `paths.exports`. Saving a path creates the directory but
  never moves existing files. Resetting one removes that override and returns to
  the configured/default location.
- **Naming templates** — `name_template_episode`, `name_template_movie`,
  `release_template` and `release_tag`, with an episode and film preview. These
  remain ordinary `settings.json` keys; the manager is only their coherent UI.
  The release template can use `audio`, `audio_channels`, `audio_full`, `atmos`
  and `video`. Its default produces names such as `AAC2.0`, `DDP5.1.Atmos` and
  `H.265`. These values are calculated only after the output picker is
  confirmed, from the tracks the user actually leaves selected—not from its
  automatic initial checkmarks or from the complete manifest inventory.
- **Advanced paths** — `home`, cache/temp/logs, tokens, cookies, helpers, CDMs and
  the default local vault. They are deliberately read-only in phase one because
  changing one is a state migration, not a folder preference.

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

The app-wide **License after final track selection** compatibility mode is a
deliberate Core-level exception for inputs whose master manifest does not carry
the selected media playlists' KIDs/PSSH. It waits for the final checkboxes, then
uses only those encrypted track objects for vault/licence resolution. It still
does not let service code read shared quality/language settings to choose a
provider API request or replace a service's explicit `license_*` policy.

| Setting | Values | Default |
|---------|--------|---------|
| `track_mode` | `interactive` / `auto` | `interactive` |
| `video_quality` | `best` / `2160` / `1440` / `1080` / `720` / `480` / `worst` | `best` |
| `video_codec` | `any` / `h264` / `h265` / `av1` / `vp9` | `any` |
| `video_range` | `any` / `sdr` / `hdr10` / `hdr10+` / `hlg` / `dv` | `any` |
| `audio_codec` | `any` / `aac` / `ac3` / `eac3` / `atmos` | `any` |
| `audio_channels` | `any` / `2` / `6` / `8` | `any` |
| `audio_langs` | comma separated, empty = best only | empty |
| `sub_langs` | comma separated, `all`, or empty to skip | `all` |
| `drop_video` | regex | boundary-aware `trick` / `trickplay` / `thumbnail` / `image` |
| `sub_format` | `srt` / `vtt` / `raw` | `srt` |
| `mux_format` | `mkv` / `mp4` | `mkv` |
| `embed_chapters` | `on` / `off` | `on` |
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

`audio_format` only applies to audio-only titles. Its choices are discovered
from native delivery core at runtime, so the settings panel never advertises an encoder the
installed engine cannot provide. See [audio.md](audio.md).

### Do not remove `drop_video` without reading this

Trick-play and thumbnail ladders are video tracks with tall dimensions. A real
example from Paramount: a `1280x1440` thumbnail track beat the genuine `1920x1080`
video when "best" was decided on height. The default regex keeps them out of
quality selection. Its terms are bounded so a normal title such as `Strickland`
is not mistaken for a `trick` track. Existing settings containing the original
`trick|thumbnail|image` default are upgraded at runtime. The native track
selector keeps these auxiliary representations out of the normal quality
choice for the same reason.

## Chapter metadata

The global **Fetch chapter metadata** setting controls whether a service may
request and parse optional chapter data. It is enabled by default. Turning it
off skips chapter endpoints and optional chapter fields before playback is
created; `embed_chapters` remains a separate per-service output choice that
controls only container muxing. Chapter metadata is best effort: a missing,
malformed or temporarily unavailable response is reduced to no chapters (with a
warning where the service can report one) and never prevents playback, licence
acquisition, downloading or muxing.

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

Declare as many as the service genuinely has. Paramount declares three
(`platform`, the unified `region`, and `dma`) and none of them is a resolution.
Its `platform` and `region` rows are routing/profile selectors; changing them
must not sign out or delete the token/cookie cache belonging to another
Paramount+/CBS route. `region` offers `AUTO`, `US`, and the public Paramount+
international market country codes. The old `region: intl` plus `intl_country`
configuration is read for compatibility, but the separate country row is no
longer shown. Account versus TV-provider login is chosen from the service-home
`Sign in` action and is not a service setting.

yes+ declares `manifest_profile` with `hd` and `uhd` values. This chooses the
yes+ catalogue resource before playback authorization; it does not replace or
shadow the shared `video_quality`, `video_codec` or `video_range` settings used
after the returned manifest is parsed.

## Reading

```python
self.settings.get("dma")            # your own
self.settings.get("video_quality")  # shared
self.settings.get("debug")          # global, resolved through the parent scope
```

One object, three scopes, resolved by fallback. Service settings shadow globals
if a key collides, so avoid reusing global key names.

## Storage

`<paths.home>/settings.json`, keyed by service id, with `@global` reserved for the
global tier. Overrides passed in code (used by the verification scripts) apply
for one run and are not written.
