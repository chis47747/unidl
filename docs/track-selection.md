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

- `audio_langs` — comma-separated BCP-47/ISO tags;
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

`audio_selection=best` keeps the best matching track per language. Empty language
settings are treated as the service's best-language behavior; an explicit list
keeps the selection inside that list. `audio_selection=all` keeps every matching
track, including duplicate renditions, commentary, and alternate channel layouts.

For example, `audio_codec=ac4`, `audio_langs=en`, and `audio_selection=best`
does not select every English audio track. It selects the best English AC-4 track
after the codec filter. An AAC English track is not a fallback.

Language matching accepts exact and primary BCP-47 forms (`es-419` and `es`, for
example), plus common ISO-639 aliases. Unknown `und` tracks are not guessed as a
user language unless `und` is explicitly requested.

## Subtitle selection

`sub_langs` selects the final subtitle languages. `subtitle_kinds` is a
comma-separated filter containing `normal`, `forced`, `sdh`, `commentary`,
`audio_description`, or `all`.

`subtitle_selection=all` keeps every subtitle that passes both language and kind
filters. `subtitle_selection=best` keeps one preferred subtitle per language,
using the normal/forced/SDH role information and provider default markers.

Services that obtain subtitles from a separate API should expose the complete
inventory through Core's sidecar-track interface before selection. Those rows use
the same language and kind filters as manifest subtitles.

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
download. If a requested scope or custom type has no matching track, UniDL keeps
the result empty and reports the condition; it never substitutes an unrelated
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
