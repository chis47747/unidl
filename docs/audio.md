# Audio-only services

Radio and podcasts are not video with the picture missing. There is no quality
ladder to rank, the native delivery core refuses `--audio-format` if a video track is selected, and
the output wants ID3 tags that video has no use for. Treating audio as a special
case of video is how it ends up half-working everywhere it does.

So audio is a kind of title, alongside the video kinds rather than a flag on them:

| Kind | What it is | Live |
|------|------------|------|
| `TRACK` | a radio programme or podcast episode | no |
| `STATION` | a live radio station | yes |

A station is a channel in every respect except that there is no picture, so
`STATION.is_live` is true the same way `CHANNEL.is_live` is.

## A service says nothing about any of this

Setting the kind is enough. `Playback.__post_init__` derives the rest:

```python
Title(kind=TitleKind.TRACK, name="Desert Island Discs", artist="BBC Radio 4", ...)
# -> playback.audio_only is True
# -> playback.audio_tags is filled in from the title
```

`is_live` is derived the same way, from the kind. Both are only ever turned *on*
here, so a service can still force either for a kind that does not imply it -
recording a live TV channel as audio, for instance.

## What the engine then does

1. **Selects audio only.** No video selector at all rather than a filter that
   drops it: there is no ladder to rank, and the native delivery core rejects `--audio-format`
   outright if a video track is selected. A direct MP3/AAC whose probe reports an
   attached cover image as `video` is normalized back to `audio` at this boundary.
   Subtitles are left out too, because an MP3 cannot carry one and selecting it
   would produce a stray file and a confusing track list.
2. **Re-encodes, if asked.** The `audio_format` track setting. Ignored for
   anything with a picture.
3. **Writes ID3 tags.** A JSON sidecar, because that is the interface the native
   delivery core
   offers and because an exported command has to still work when it is re-run
   later - which it would not if the tags only existed in memory.
4. **Drops `--live-pipe-mux` for live audio**, which the native delivery core refuses alongside
   `--audio-format`, and which is pointless for a single track anyway.

## TUI presentation

When the user chooses tracks interactively, an audio-only title uses a two-column
picker: the left side keeps the album cover and the known ID3 fields visible, and
the right side contains the normal track list, filter, and selection controls.
During a real audio download the same preview moves to the right side of the
delivery screen; progress, decrypt, and mux status remain on the left. Video VOD
and live recording keep the ordinary full-width delivery layout. Cover loading is
best-effort and happens off the UI thread, so a missing or expired artwork URL
never blocks track selection or downloading.

On Kitty/Sixel-capable terminals the preview uses the terminal's native image
protocol and keeps the source pixels. Plain ANSI terminals (including the stock
macOS Terminal) use a 36×36 half-cell raster so the cover remains portable; tiny
cover lettering may disappear at that size because terminal cells cannot carry
browser-like image detail. No capability probe is emitted during startup.

## The format list is not written here

`audio_format` offers exactly what this UniDL build can produce, read from the
native parser. The available conversions are `mp3` at 320 Kbps, `flac`, and
`alac` in an M4A container, plus `source` for "keep the original, do not
re-encode".

When the native engine learns FLAC the option can appear with no change on this side. More
to the point, an option that cannot work never appears at all - and a value left
in `settings.json` by a build that *could* do it degrades to keeping the original,
with a line in the log, rather than failing at the last moment.

`source` is worth choosing when a service already hands over an MP3. Keeping it
avoids a re-encode and preserves the provider's original audio.

## Tags

From the title, and only what is known - an empty tag is worse than a missing one,
because a player shows it as a blank field rather than falling back:

| Tag | From |
|-----|------|
| `title` | `episode_name`, else `name` |
| `artist`, `album_artist` | `artist`, else `channel` |
| `album` | `album`, else the programme name |
| `date` | `year`, else the broadcast date |
| `track` | `track_number`, else `episode` |
| `genre`, `publisher`, `comment` | `genre`, `publisher`, `synopsis` |
| `cover` | `cover_url`, as `{"url": ...}` |

`Title` carries these fields directly rather than in `data`, because `data` is the
service's private payload that core is not allowed to look at, and these are
exactly the things core has to read.

A service may also supply a richer `Playback.audio_tags` dictionary when its
catalogue knows fields the common `Title` model does not need. YouTube Music does
this for `album_artist`, `track/total`, `disc/total`, `composer`, `copyright`,
`ISRC` and cover-request headers. The same dictionary is stored in its JSON
manifest so the native delivery core sees identical metadata whether the command runs immediately
or is exported and replayed later. If the Music next/browse enrichment fails,
description-derived fields and the basic title/artist/album data remain usable.

## Checking it

The focused project smoke tests cover audio metadata models and native
delivery contracts without contacting a service or reading account state.
