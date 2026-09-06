# Optional chapters

Chapters are optional service metadata. A service that has no chapter endpoint
does nothing; there is no capability flag and no empty method to implement. The
app-wide **Fetch chapter metadata** setting (on by default) decides whether
chapter requests and optional response fields are made at all. The per-service
**Embed chapters in the final file** setting is independent: it only controls
container muxing after metadata has been acquired.
When a provider bundles transition/break metadata into its mandatory playback
response, the off mode leaves that provider request intact but does not parse or
attach the optional chapter field; this keeps the provider's primary response
contract stable while preserving the same user-visible policy.

### Paramount+ / CBS

Paramount's Android-TV video metadata carries an optional `playbackEvents`
object. UniDL reads its timestamps only when **Fetch chapter metadata** is
enabled and converts the values, which are milliseconds, into Core `Chapter`
objects. Paramount+ US and international entries retain the five provider
markers (`previewStartTimeMs`, `previewEndTimeMs`, `openCreditStartTime`,
`openCreditEndTimeMs`, and `endCreditChapterTimeMs`). CBS follows its player
player contract and exposes the end-credit marker only. These are navigation
markers, not ad-removal instructions; a missing or malformed marker simply
leaves that chapter out while the Paramount-owned licence path continues.

## Service contract

When a service's playback API returns chapters, convert its response in the
service API/module and attach `Chapter` values to the `Playback` it emits:

```python
from unidl.core import Chapter

playback = Playback(
    title=title,
    save_name=self.save_name(title),
    manifest_url=source.url,
    chapters=[
        Chapter(start_ms=0, end_ms=82_000, title="Opening", kind="intro"),
        Chapter(start_ms=82_000, title="Main story"),
    ],
)
```

Use `Chapter.from_seconds(...)` only when the API explicitly documents seconds.
The Core field is always milliseconds. `title` is a human navigation label;
`kind` is an optional display hint (`intro`, `recap`, `credits`, and so on) and
never controls ad filtering or automatic skipping.

`end_ms` is optional. Core sorts chapters, removes exact duplicates and infers a
missing end from the next marker or the known media duration. Different labels
at the same timestamp are retained. Chapter metadata is auxiliary and best
effort: a provider timeout, missing field or malformed optional entry must be
logged and reduced to an empty/partial chapter list, while the title continues
through playback, DRM, download and mux. Test the parser offline rather than
allowing optional metadata to become a title-fatal error.

Do not put chapters in `Title.data` as a substitute. `Title.data` is service
private and is not available to Core, export, or the native mux boundary.

## Where they appear

When a service API actually returns chapters, both the track picker and the
download screen show a compact count next to the programme name — for example
`Tracks · Reacher.S04E04.Karambits.and.Pieces` with `3 chapters` beside it.

Clicking that count, pressing `c`, or using the delivery `Chapters` control
opens a popup:

- numbered rows with start/end, duration, optional kind, and the fragment title;
- a visible `✕` (also `esc` / `^b`) returns to the screen that opened it;
- a download or recording already in progress is not paused, cancelled, or
  covered by a full-screen takeover.

The delivery details block still previews one to three chapters, and longer
timelines keep the count plus a reminder to open the popup. Command-only mode
shows a bounded preview in its result card; the saved command file contains
every chapter.

Exports preserve the structured chapter list. On a real VOD download Core writes
a stable JSON sidecar and the native downloader embeds it during the final mux:
FFmpeg receives FFmetadata and mkvmerge receives OGM chapter text. Every service's
**Tracks and output** settings contains **Embed chapters in the final file**,
enabled by default. Turning it off skips the sidecar/mux input while keeping the
chapters in the report, command metadata and export. `--no-mux` continues to mean
no container metadata is written.

Chapters are currently metadata for finite video delivery. Live services may show
them in the report, but a live pipe mux is not retroactively rewritten with a
static chapter file.

## What a service must test

- API timestamps are converted to milliseconds and remain monotonic;
- an empty or absent chapter response leaves ordinary playback unchanged;
- global `fetch_chapters=off` does not call the optional endpoint or consume its
  optional response field;
- a provider error or malformed optional chapter entry still emits the normal
  playback and reaches DRM/download/mux;
- Unicode/RTL titles survive unchanged;
- a missing final end is inferred from the title/manifest duration;
- command-only, export and download paths all retain the same chapter count;
- a long list opens as a popup and scrolls on a short terminal without changing
  the log follow-tail behavior or interrupting an active download.
