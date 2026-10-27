# Track selection

UniDL has one shared output-track selector for every service. It runs after the
service has authorized and parsed its manifest (and after any service-owned
subtitle sidecar rows have been added). It decides which parsed video, audio and
subtitle tracks are handed to the native downloader.

This document describes output selection only. It is not a DRM, licence, or
provider-manifest selector.

## The two independent choices

There are two different decisions in a service's Tracks and output settings:

1. **Get a manifest profile** — a service-specific API choice such as a source
   resolution, codec ladder, color profile, device profile, or playback API.
2. **Choose tracks from that manifest** — the shared settings documented here.

The second decision cannot request a new manifest. For example, selecting
`video_quality=1080` chooses a 1080p representation if one exists in the
already-returned ladder; it does not make a service fetch a 1080p manifest.
Services must keep their provider/API/profile settings separate from these
output settings.

### Equal-ranked tracks

Best selection keeps only one track per requested selection group, even when
several renditions have identical parameters. Exact ranking ties keep the first
track in manifest order. The track picker and track preview preserve that single
selection instead of checking every row with matching metadata. Rebuilt tracks
are matched one-to-one, so a renewed source URL does not expand the selection.

This does not remove tracks from the ladder. All matching mode still selects all
matching renditions, and users can manually select multiple equivalent tracks.
Multiple requested audio/subtitle languages and video profiles retain their
existing selection groups; this rule does not reduce the whole download to one
track of each type.

## Output scope

`output_scope` controls which media types participate in automatic selection:

| Value | Meaning |
|---|---|
| `package` | Normal package: video, audio and subtitles when their filters select them. This is the compatibility default. |
| `video` | Select video tracks only. |
| `audio` | Select audio tracks only. |
| `subtitle` | Select subtitle tracks only. |
| `custom` | Read `output_types`, a comma-separated combination of `video`, `audio` and `subtitle`. |

The scope affects automatic preselection and delivery. Narrow scopes disable
automatic muxing, so multiple selected tracks remain separate files with
track-aware names. A custom scope containing two or three media types also stays
separate; use Normal package when a normal final container is wanted.

Examples:

```yaml
track_mode: auto
output_scope: subtitle
sub_langs: en
subtitle_kinds: normal,forced
subtitle_selection: all
```

```yaml
track_mode: auto
output_scope: custom
output_types: video,audio
video_selection: best
audio_selection: all
```

## Separate original tracks (No mux)

Choose **Settings → Download behavior → Muxer → No mux (original tracks)**
(`muxer: none`). This is an output behavior, not a selection filter: the same
automatic or manual track selection still applies to every title, including batches.

UniDL downloads, joins segments and decrypts each selected track normally, then
saves the independent completed files in the usual download folder, with
track-aware names. It does not create a final combined MKV/MP4. Audio keeps its
source codec, subtitles keep their source format and styling, and service API
subtitle references are downloaded directly instead of passed through a service's
subtitle conversion. Prepared local sidecars are copied without modification.

This mode overrides **Container**, **Audio-only format**, **Subtitle format**,
subtitle cleanup, optional repacking/Audio Vivid decoding, and Dolby Vision
Hybrid assembly. Default audio/subtitle flags and chapter/lyrics embedding do not
apply because there is no final container. Metadata remains available in the
report and export; attachment downloading still follows its own setting.

This is not **Keep temp files**: completed clear tracks are final outputs, not
encrypted intermediates or loose segments, and normal cache cleanup does not
delete them. Existing audio embedded in a source video is normally preserved;
No mux does not itself force a split. A service may explicitly enable Core's
muxed A/V separation: the picker then exposes independent video/audio entries
and No mux saves only selected tracks, without transcoding.

Audio rows identify their original video rendition. Both projections of one
source share a download; audio-only selection still needs the original muxed
segments, so it does not necessarily reduce transfer bandwidth. Different
renditions are not assumed to contain identical audio. The policy is
service-controlled, off by default, and retained in commands and exports.

For live streams, cross-track pipe muxing is disabled, but per-track real-time
segment joining remains available. Back still finalizes the recording and saves
the independent tracks; Quit retains the existing immediate-exit behavior.

The standalone equivalent is:

```sh
unidl download 'MANIFEST_URL' --muxer none --select 1,3,8
```

Saved commands include `--muxer none`. The older `--no-mux` flag only suppresses
the final mux and still honors configured conversions; its behavior is unchanged.
The default `muxer: auto` is unchanged.

## Video selection

Video constraints are applied first:

- `video_quality`: `best`, `worst`, or a target height such as `1080`;
- `video_codec`: `h264`, `h265`, `h266`/VVC, `av1`, `vp9`, or `any`;
- `video_range`: `sdr`, `hdr10`, `hdr10+`, `hlg`, `dv`, or `any`;
- `drop_video`: the boundary-aware pattern used to remove trick-play and
  thumbnail ladders.

`video_selection=best` keeps the highest-ranked matching representation.
`video_selection=all` keeps every representation that passes the filters. If a
target height is set, All means every matching representation at that height,
not every height in the manifest.

## Audio selection

Audio filters are combined with **AND**:

- `audio_langs` — comma-separated BCP-47/ISO tags and/or `original`;
- `audio_codec` — codec only, including AAC, AC-3, E-AC-3, **AC-4**, Opus,
  FLAC, ALAC and MP3;
- `audio_profile` — role or feature, such as Atmos, main, commentary,
  description, or dialog;
- `audio_channels` — any, stereo (`2`), 5.1 (`6`) or 7.1 (`8`).

Codec and profile are different fields. `audio_codec=eac3` matches E-AC-3
regardless of whether it is Atmos; `audio_profile=atmos` matches Atmos/JOC
signalling regardless of the codec label. The legacy `audio_codec=atmos` value
is accepted as an alias for the Atmos profile. `audio_codec=ac4` matches raw
`ac-4`, `ac4`, and `dac4` codec declarations.

`audio_quality=best` or `worst` ranks only tracks left after all of those filters.
It never widens a language, codec, profile, or channel condition.

`audio_profile=dialog` means ordinary dialogue, not Voice Booster, Boosted
Dialog, or enhanced-dialogue mixes. For Best, Worst, and target-bitrate selection,
UniDL prefers ordinary audio within each already-filtered language group before
ranking quality. Commentary, audio description, and enhanced-dialogue tracks do
not displace an available ordinary mix simply because they appear first or have
a higher bitrate. If no ordinary mix passes the filters, Any profile can still
use an alternate mix; an explicit Dialog profile instead reports no match and
leaves audio unchecked for manual selection.

`audio_selection=best` keeps the best matching track per language. Empty language
settings are treated as the service's best-language behavior; an explicit list
keeps the selection inside that list. `audio_selection=all` keeps every matching
track, including duplicate renditions, commentary, and alternate channel layouts.
With Any profile, All matching also retains Voice Booster and other enhanced
mixes. These tracks always remain available in the manual picker; explicit
name/group/role filters can also target them.

For example, `audio_codec=ac4`, `audio_langs=en`, and `audio_selection=best`
does not select every English audio track. It selects the best English AC-4 track
after the codec filter. An AAC English track is not a fallback.

Language matching accepts exact and primary BCP-47 forms (`es-419` and `es`, for
example), plus common ISO-639 aliases. Unknown `und` tracks are not guessed as a
user language unless `und` is explicitly requested.

### Selecting original audio and subtitles

Both **Audio languages** (`audio_langs`) and **Subtitle languages**
(`sub_langs`) accept `original`, alone or mixed with language tags:

| Value | Eligible tracks |
| --- | --- |
| `original` | Only tracks explicitly marked Original by the provider/service. |
| `original,en` | Include original tracks **and** English tracks. If the original language is English, Best selects that language group only once. |
| `en,de,it` | Include English, German **and** Italian tracks, not just the first matching language. |

Comma-separated entries are additive, not an ordered fallback list. For example,
if the original language is Japanese, `original,en` selects Japanese original
tracks and English tracks together. If it is English, the overlapping request
does not create a second English selection. Reordering the entries does not
exclude any requested language.

The token is case-insensitive. It reuses UniDL's original-track markers, including
explicit flags, Original roles and declared original-language metadata. It does
not infer the original language from a title's country, interface language, track
order or source-default flag. A track can be marked original even when its
language tag is `und`.

Language/original matching combines all requested groups within this one field;
it never stops after the first matching group. All other filters
still apply with **AND**: original audio must also satisfy the codec, profile and
channel settings; original subtitles must also satisfy the subtitle-kind filter.
Quality ranking runs only after those constraints.

Best selects one eligible track per language group; original tracks in different
languages retain separate groups. If an original track also matches an explicit
language in the list, that language group is ranked once, not once per token.
All matching retains every eligible rendition. For example, `original,en` does
not add a second, lower-ranked English rendition merely because it is original.

If no original marker is available, `original` matches nothing; UniDL does not
silently select every language. A mixed list such as `original,en` can still
match English even without original markers: it is a union, not a requirement
that each token find a track. If the complete rule has no match, normal
unmatched-selection handling leaves that media type unchecked for manual repair
in the picker; automatic batches use the existing skip/report behavior.

Example: download original audio and English subtitles, also retaining original
subtitles when available:

```yaml
audio_langs: original
audio_selection: best
sub_langs: original,en
subtitle_kinds: normal,forced
subtitle_selection: best
```

For subtitle-only batch downloads, combine `output_scope: subtitle` with
`sub_langs: original`. Separate API/sidecar subtitles and native imported
manifests use the same rule, provided they carry the original markers.

The CLI equivalents are `--audio-lang original` and
`--sub-lang original,en`; filter expressions also accept
`--select-audio lang=original:for=best` and
`--select-subtitle lang=original,en:for=best`.

These are **download selection** rules. `default_audio=original` and
`default_subtitle=original` remain separate: they only choose a default playback
flag among tracks already selected and do not request original tracks.

### Language, country and region names in track lists

Audio and subtitle rows keep the original language tag and add its full English
language name: `fr | French`, `it | Italian`, or `nl | Dutch`. If a country or
region is explicitly declared, it follows in parentheses:
`en-US | English (United States)`, `es-ES | Spanish (Spain)`, or
`pt-BR | Portuguese (Brazil)`.
Names come from local ISO-639 and country data; displaying them makes no network
requests. Two-letter, three-letter and bibliographic ISO language aliases are
recognized. Unrecognized tags are kept as provided rather than assigned a name.

Tags without a region, such as `en`, `und`, or `fil`, do not imply a country.
When a manifest shortens the language to `en` but retains an explicit locale in
track metadata such as `en-us_dialog_0`, UniDL can display United States without
changing the original `en` tag. The fallback accepts only a locale at the start
of a track identifier/name whose primary language matches the track. Conflicting
fallback regions are left unlabeled; an explicit region in the language tag
always takes priority. URLs and free-form country mentions are not inspected.
`es-419` displays Spanish (Latin America and the Caribbean), not one particular
country. A provider label identical to the computed language label is not
repeated, while role-specific names such as English SDH are preserved.
These names describe the region declared by the track's metadata, not a
verified recording location. They are display-only: language matching, drop
filters, mux language metadata, export language tags and filenames stay unchanged.

## Default audio in the muxed file

**Audio languages** chooses which tracks to download. **Default audio language**
(`default_audio`) chooses which of the final selected audio tracks receives the
default playback flag. It never adds a track, widens a filter, changes licence
selection, or changes the order of the downloaded tracks.

The setting is available per service, immediately below Audio languages. Choose
Auto, Original, or Specify a language and enter one tag:

| Value | Result |
|---|---|
| `auto` (default) | Prefer ordinary/main selected audio, then a source-default track, then explicitly marked original audio. Without either marker, keep the first selected language. |
| `original` | Prefer selected audio explicitly marked as original. Do not infer original language from the service's country or interface language. |
| A single tag such as `en`, `fr-CA`, `es-419`, `und`, or a provider tag | Prefer selected audio matching that language. ISO aliases and case/underscore normalization are accepted. A primary tag such as `en` includes regional forms; a regional tag such as `en-GB` requires that region and does not silently become `en-US`. |

Within matching candidates, ordinary/main audio takes precedence over commentary,
audio description, and Voice Booster/enhanced-dialogue mixes. If only alternate
mixes were selected, they remain eligible. Multiple candidates at the same
priority use the existing Audio Best ranking; exact ties keep their stable
selected-track order.

If the requested language or original marker is missing, UniDL reports a warning
and falls back to Auto. It does not fetch additional tracks or cancel the job.
A single selected audio track becomes default. With no audio, separate-file
output, or audio-only delivery without muxing, this preference has no effect.

The rule runs after the final picker choices and is evaluated separately for
every batch title. For MKV/MP4 output, UniDL sets one audio default flag and clears
the other audio default flags, including audio embedded in input containers.
Video and subtitle defaults are independent. Live pipe muxing uses the same
preference. MPEG-TS does not reliably retain a default-audio flag; UniDL reports
that limitation instead of promising a default playback language.

The download details and log show the resolved default audio and any fallback.
Native exports preserve `default_audio`; the saved export value takes precedence
over the current service setting on import. Older exports use the current
service setting, or Auto when there is none. Saved commands include
`--default-audio` so replay keeps the preference.

Example: download English and French, with French as the default:

```yaml
audio_langs: en,fr
audio_selection: best
default_audio: fr
```

To verify the final file, inspect its audio tracks in MediaInfo for
`Default: Yes`, or use:

```sh
ffprobe -v error -select_streams a \
  -show_entries stream=index:stream_tags=language,title:stream_disposition=default \
  -of json "file.mkv"
```

The file's default flag is a playback hint. A player's preferred-language
settings may override it.

## Subtitle selection

`sub_langs` selects the final subtitle languages; it accepts language tags,
`original`, or a mixture such as `original,en`, as described above.
`subtitle_kinds` is a
comma-separated filter containing `normal`, `forced`, `sdh`, `commentary`,
`audio_description`, or `all`.

`subtitle_selection=all` keeps every subtitle that passes both language and kind
filters. `subtitle_selection=best` keeps one preferred subtitle per language,
using the normal/forced/SDH role information and provider default markers.

Services that obtain subtitles from a separate API should expose the complete
inventory through Core's sidecar-track interface before selection. Those rows use
the same language and kind filters as manifest subtitles.

## Default subtitle in the muxed file

**Subtitle languages** chooses which subtitle tracks to download. **Default
subtitle language** (`default_subtitle`) chooses which of the final selected
subtitle tracks receives the container's default playback flag. It never adds a
track or widens the subtitle filters.

Choose `Auto`, `Original`, or one language tag such as `en`, `fr-CA`, or
`es-419`:

| Value | Result |
| --- | --- |
| `auto` (default) | Prefer a source-marked default subtitle, then an explicitly original subtitle. Normal subtitles are preferred over forced/SDH when several candidates remain. |
| `original` | Use an explicitly original subtitle among the selected tracks. If none is marked original, no default subtitle is set and a warning is shown. |
| One language tag | Match that language only among selected tracks. A primary tag such as `en` accepts regional variants; a regional tag requires that region. If it is missing, UniDL falls back to the selected original-language subtitle; if that is also missing, no default is set. |

`forced`, `sdh`, `commentary`, and `audio_description` are independent subtitle
kind flags. A forced or SDH track can be the default when it is the only matching
track, but it does not become default merely because it is forced. The setting
also applies to service sidecar subtitles added through `prepare_download()`.

The result is evaluated after the final picker selection for every title. It is
stored in saved commands and native exports as `default_subtitle`; older exports
use the current service setting or `Auto`. If no subtitles are selected, this
setting has no effect. The default flag is a player hint and can be overridden by
the player's own subtitle-language preference. MKV provides the most reliable
subtitle disposition support; MP4 may have container/player-specific limits.

## Subtitle format and positioning

Select **Tracks and output → Subtitle format** independently of subtitle
languages and types. The default remains SRT.

| Format | Positioning and styles |
| --- | --- |
| SRT | Keeps basic inline emphasis and adds `{\an8}` to cues explicitly positioned in the upper part of the screen. Many players support this extension; some ignore it. Exact coordinates, regions and CSS cannot be represented. |
| WebVTT | Retains native cue settings such as `line`, `position`, `align`, `size`, `vertical` and `region`, plus STYLE/REGION blocks and cue markup. The player must support them. |
| ASS | Maps common horizontal cue positions to a 1920×1080 script canvas, including top/bottom alignment, explicit coordinates and basic bold/italic/underline/colour. Use MKV to retain these instructions. |
| Original | Keeps the downloaded subtitle file unchanged. The container and player must accept its format. |

For dialogue above on-screen credits, choose **ASS** and **MKV**. UniDL uses
each cue's own placement; it does not move every subtitle to the top. The
equivalent saved command option is `--sub-format ass --mux-format mkv`.

The shared converter reads placement from text WebVTT, MP4 WebVTT `sttg` boxes,
and common TTML regions, inherited styles, `origin`, `extent`, `displayAlign`
and `textAlign`. Timing correction, clipping, duplicate removal and incremental
cue repair retain this information. Identical words at different positions are
kept as separate cues.

ASS mapping approximates WebVTT snap-to-line positions and common horizontal
TTML regions; arbitrary CSS, vertical writing, animation, ruby and complete
TTML typography are not reproduced. Native WebVTT is preferable when its full
layout matters and the player supports it. External TTML/XML sidecars are
converted to ASS for MKV because muxers cannot read them directly.

MP4 cannot copy ASS, WebVTT or SRT tracks directly, so UniDL muxes them as
`mov_text`. That conversion may lose positioning and styling. Use MKV for
positioned subtitles, or keep standalone originals if required.

## Interactive, automatic, and batch behavior

- `track_mode=interactive` opens the picker with the automatic result prechecked;
  the user may change it and manually select any visible rows.
- `track_mode=auto` accepts the result without opening the picker.
- A single automatic title with no match opens the normal picker rather than
  silently widening the rule.
- In an automatic batch, a title with no matching track is recorded as skipped
  and the next title continues. The batch never pauses for a picker that cannot
  share an answer across episodes.
- `track_mode=list` reports the inventory and the unmatched reason without
  licensing or downloading.

Focused output scopes do not turn a missing optional track into a package
download. If one requested media type has no matching track, UniDL reports the
condition, leaves that type unselected, and keeps any other tracks that did
match preselected for manual confirmation; it never substitutes an unrelated
codec, language, or subtitle kind.

## DRM and licence boundary

Output selection is deliberately separate from service DRM. Shared track settings
must not change a service's PSSH, licence profile, device/API choice, or licence
track plan. The service-owned DRM path resolves the keys required by its own
contract; Core then uses the selected track objects for delivery.

The optional **License after final track selection** compatibility mode only
changes when Core resolves selected-track init data. It does not make output
settings into a provider API selector.

## Commands and exports

Saved commands include the selected track indexes and a human-readable selection
comment. Native exports also store the output scope, output types, and Best/All
rules. Re-running a command uses its exact selected indexes. Importing a native
export restores its stored output scope before automatic selection; the manifest
and keys remain the export's source of truth.
