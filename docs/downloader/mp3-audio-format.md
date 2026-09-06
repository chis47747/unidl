# UniDL MP3 Audio Export Format

This file defines the stable MP3 export behavior and the metadata contract accepted by `UniDL`.

The JSON track structure itself is documented in
[`json-manifest-format.md`](json-manifest-format.md). This document covers what
happens after an audio track is selected with `--audio-format mp3`.

## Scope

MP3 export supports VOD audio tracks and audio-only live recording.

```bash
unidl download input.json \
  -sa best \
  --audio-format mp3 \
  --save-name "Artist.Album.01.Song"
```

For live radio or podcast recording, UniDL records and merges the original audio stream first. Audio-only live recording treats Ctrl-C as a graceful stop. With MP3 export enabled, reaching `--live-record-limit` or pressing Ctrl-C finalizes that recording as MP3 and then applies normal cleanup rules to the original track file and temporary segments.

```bash
unidl download "https://radio.example.com/master.m3u8" \
  -sa best \
  --live-real-time-merge true \
  --live-keep-segments false \
  --audio-format mp3 \
  --audio-metadata-file "/path/to/station.metadata.json" \
  --save-name "Radio.Recording"
```

Live MP3 export currently requires an audio-only selection and cannot be combined with `--live-pipe-mux`. Without `--audio-format mp3`, the same command keeps the source live recording container, normally AAC in MPEG-TS for HLS radio.

Only `mp3` is currently accepted by `--audio-format`. The generic audio module is designed so that other audio output formats can be added later without moving format-specific work back into the main CLI.

## Code Ownership

The implementation is intentionally split by responsibility:

- `src/unidl/downloader/audio.py` normalizes metadata, loads metadata sidecars, resolves and downloads cover art, builds ID3 fields, and performs audio export.
- `src/unidl/downloader/cli.py` validates CLI combinations, injects sidecar metadata into selected audio streams, controls output naming, and places audio export after download/decryption.
- `src/unidl/downloader/parsers/json_manifest.py` reads title-level `audio_metadata` from a JSON track manifest and passes the normalized data into each audio stream.
- `tests/test_audio.py` contains real FFmpeg/FFprobe coverage for MP3 duration and attached cover behavior.

HLS, DASH, ISM, SABR, direct URL, decryption, and live recording implementations do not contain MP3 metadata rules. They deliver a selected clear audio file to the common audio post-processing layer.

## External Metadata for HLS, DASH, and ISM

Master HLS, DASH, ISM, and direct media inputs usually do not contain complete song-level ID3 metadata. Supply the same JSON contract through a sidecar file:

```bash
unidl download "https://cdn.example.com/master.m3u8" \
  -sa best \
  --audio-format mp3 \
  --audio-metadata-file "/path/to/song.metadata.json" \
  --save-name "Artist.Album.01.Song"
```

The sidecar root may be the metadata object directly:

```json
{
  "title": "Song",
  "artist": "Artist",
  "album": "Album",
  "date": "2026",
  "track": "1/10",
  "cover": {
    "path": "artwork/cover.png"
  }
}
```

It may also use a wrapped title object with `audio_metadata` and title-level fallbacks:

```json
{
  "name": "Song fallback",
  "artist": "Artist fallback",
  "year": "2026",
  "audio_metadata": {
    "title": "Exact Song",
    "album": "Exact Album",
    "cover": {
      "url": "https://images.example.com/cover.jpg"
    }
  }
}
```

The file may alternatively contain a one-item object array. `--audio-metadata-file` requires `--audio-format` and applies to every selected audio track, regardless of whether the source is HLS, DASH, ISM, JSON, or a direct media URL. Sidecar values replace manifest-provided `audio_metadata` for those selected tracks.

Relative `cover.path` values, and relative values supplied in `cover.url`, are resolved against the directory containing the metadata sidecar. This keeps cover resolution independent of the remote master manifest URL.

## Processing Pipeline

For each selected audio track, UniDL performs these steps:

1. Parse and select the source audio track.
2. Download the complete source with the native unidl downloader.
3. For live audio, continue recording until the configured limit or a graceful Ctrl-C stop.
4. Decrypt the source first when the selected track is encrypted.
5. Apply `--repack` first when explicitly requested.
6. Resolve title-level audio metadata and optional cover art.
7. Export or retag the track as MP3.
8. Write the completed-track cache for VOD using the output format and metadata signature.
9. Clean replaced source intermediates according to the normal task cleanup settings.

Encrypted audio cannot be exported with both `--audio-format mp3` and `--no-decrypt`, because FFmpeg must receive clear audio.

## MP3 Encoding

Non-MP3 source audio is encoded with FFmpeg and `libmp3lame` at a target bitrate of 320 Kbps.

```text
AAC / Opus / AC-3 / E-AC-3 / other FFmpeg-readable audio
                         -> libmp3lame 320 Kbps -> MP3
```

UniDL does not force a sample rate or channel layout. FFmpeg negotiates them from the source and the MP3 encoder.

If the selected source is already identified as MP3, the audio stream is copied instead of encoded again. Metadata and cover art are still rewritten into a new MP3 container.

All MP3 outputs use ID3v2.3.

## Pure-Audio Output Logic

When the selection contains only audio tracks, UniDL does not automatically combine them.

- One selected audio track produces one `.mp3` file.
- Multiple selected audio tracks produce separate `.mp3` files using the normal track naming rules.
- Selected subtitles remain separate sidecar files.
- `--mux` or `--mux-format` may still explicitly request container muxing.

This avoids combining different languages, mixes, bitrates, or channel layouts into an unexpected multi-track media container.

When video and audio are selected together, the converted MP3 audio may enter the existing VOD mux pipeline. The default VOD mux container remains MKV unless another mux format is requested.

## Metadata Location

Audio export metadata belongs on the title object under `audio_metadata`:

```json
{
  "name": "Song title fallback",
  "artist": "Artist fallback",
  "album": "Album fallback",
  "year": "2026",
  "audio_metadata": {
    "title": "Exact Song Title",
    "artist": "Exact Artist",
    "album": "Exact Album"
  },
  "audio_tracks": []
}
```

`audio_metadata` is title-level metadata. It is copied to every audio track under that title. Per-track `audio_metadata` overrides are not currently supported. If tracks need different song metadata, the upstream exporter should emit separate title objects.

## ID3 Metadata Fields

The following `audio_metadata` fields are supported:

| JSON field | Meaning | Typical ID3 frame |
| --- | --- | --- |
| `title` | Song or recording title | `TIT2` |
| `artist` | Track artist | `TPE1` |
| `album` | Album title | `TALB` |
| `album_artist` | Album artist | `TPE2` |
| `date` | Release year or date | `TYER`/date metadata |
| `track` | Track number, optionally with total | `TRCK` |
| `disc` | Disc number, optionally with total | `TPOS` |
| `genre` | Genre | `TCON` |
| `composer` | Composer or songwriter | `TCOM` |
| `comment` | Free-form comment | `COMM` |
| `copyright` | Copyright notice | `TCOP` |
| `publisher` | Label or publisher | `TPUB` |
| `isrc` | International Standard Recording Code | `TSRC` |

Recommended forms:

```json
{
  "date": "2026",
  "track": "4/12",
  "disc": "1/2",
  "composer": "Writer One; Writer Two",
  "isrc": "USABC2600001"
}
```

String values are recommended. Numeric values are converted to strings. Lists and tuples are joined with `; `. Empty values are omitted.

## Metadata Precedence and Aliases

Values inside `audio_metadata` take priority. When they are missing, UniDL accepts these title-level fallbacks:

| Output field | Accepted values in priority order |
| --- | --- |
| `title` | `audio_metadata.title`, `name`, `title` |
| `artist` | `audio_metadata.artist`, `artist` |
| `album` | `audio_metadata.album`, `album` |
| `album_artist` | `audio_metadata.album_artist`, `audio_metadata.albumArtist`, `album_artist`, `albumArtist` |
| `date` | `audio_metadata.date`, `audio_metadata.year`, `release_date`, `releaseDate`, `year` |
| `track` | `audio_metadata.track`, `track_number`, `trackNumber`, title-level `track`, title-level `track_number` |
| `disc` | `audio_metadata.disc`, `disc_number`, `discNumber`, title-level `disc`, title-level `disc_number` |
| `genre` | `audio_metadata.genre`, `genre` |
| `composer` | `audio_metadata.composer`, `composer` |
| `comment` | `audio_metadata.comment`, `comment` |
| `copyright` | `audio_metadata.copyright`, `copyright` |
| `publisher` | `audio_metadata.publisher`, `publisher` |
| `isrc` | `audio_metadata.isrc`, `audio_metadata.ISRC`, `isrc`, `ISRC` |

The source media's existing container metadata is imported first. Resolved `audio_metadata` values then override fields with the same names.

## Cover Art Object

Cover art is configured under `audio_metadata.cover`.

Remote image:

```json
"cover": {
  "url": "https://images.example.com/album-cover.webp",
  "headers": {
    "Referer": "https://music.example.com/",
    "User-Agent": "Example Music Client/1.0"
  }
}
```

Local image:

```json
"cover": {
  "path": "artwork/album-cover.png"
}
```

A cover may also be provided as a string for compatibility:

```json
"cover": "https://images.example.com/album-cover.jpg"
```

The title-level aliases `cover`, `cover_url`, and `coverUrl` are accepted when `audio_metadata.cover` is absent.

## Cover Resolution Rules

- `cover.path` takes priority when both `path` and `url` are present.
- Absolute filesystem paths and `file://` paths are accepted.
- A relative local path is resolved against the directory containing the JSON manifest.
- A relative URL in a remote JSON manifest is resolved against the manifest URL.
- HTTP and HTTPS covers use the native UniDL loader.
- Normal task request headers are applied first.
- `cover.headers` are applied afterward and override headers with the same names.
- Cover requests use the task's `--http-request-timeout` and retry count.

Remote covers are cached in the task's post-processing temporary directory. The cache identity includes the resolved URL and request headers. If the bytes at an unchanged cover URL are replaced upstream, add a cache-busting query value to the URL when an immediate refresh is required.

## Supported Cover Images

Input cover art may be:

- JPEG
- PNG
- WebP

The type is detected from the image bytes rather than trusted from the URL extension or HTTP MIME type. Unsupported or invalid image data fails the MP3 post-processing step instead of silently producing a file without the requested cover.

FFmpeg writes the image into the MP3 as one MJPEG stream with:

```text
disposition: attached_pic
title: Album cover
comment: Cover (front)
```

The complete audio duration must remain independent of the single attached cover image. A real FFmpeg/FFprobe regression test protects this behavior.

## Complete JSON Example

```json
[
  {
    "id": "song-id",
    "type": "music",
    "name": "Sharpest Tool",
    "artist": "Sabrina Carpenter",
    "album": "Short n' Sweet",
    "year": "2024",
    "duration_ms": 218000,
    "audio_metadata": {
      "title": "Sharpest Tool",
      "artist": "Sabrina Carpenter",
      "album": "Short n' Sweet",
      "album_artist": "Sabrina Carpenter",
      "date": "2024",
      "track": "4/12",
      "disc": "1/1",
      "genre": "Pop",
      "composer": "Jack Antonoff; Sabrina Carpenter; Amy Allen",
      "comment": "",
      "copyright": "2024 Island Records",
      "publisher": "Universal Music Group",
      "isrc": "USUM72404101",
      "cover": {
        "url": "https://images.example.com/sharpest-tool.jpg",
        "headers": {
          "Referer": "https://music.example.com/"
        }
      }
    },
    "audio_tracks": [
      {
        "id": "774",
        "codec": "opus.opus",
        "mime_type": "audio/webm",
        "language": "und",
        "channels": "2.0",
        "bitrate": 283204,
        "url": "https://cdn.example.com/audio.webm?clen=7199362&dur=218.301"
      }
    ]
  }
]
```

## Completed-Track Cache

The completed-track cache identity includes:

- source stream identity
- requested audio output format
- normalized audio metadata signature
- output naming and relevant post-processing options

Changing ID3 metadata or cover configuration therefore prevents an older completed MP3 from being reused as if it had the new metadata.

## Failure Behavior

MP3 export fails clearly when:

- no audio track is selected
- live MP3 is requested together with a selected live video track or `--live-pipe-mux`
- encrypted audio is combined with `--no-decrypt`
- FFmpeg or `libmp3lame` is unavailable
- a local cover file does not exist
- a remote cover cannot be downloaded
- cover bytes are not JPEG, PNG, or WebP
- FFmpeg cannot decode the selected source audio
- FFmpeg cannot encode or retag the output

The source download and decrypted intermediates follow the normal cleanup policy. A failed post-processing step does not report the MP3 as completed.

## Current Limits

- Only MP3 output is currently implemented.
- Live MP3 is finalized after recording stops; it is not encoded directly while segments are arriving.
- Live MP3 cannot currently be combined with a selected live video track or `--live-pipe-mux`.
- Metadata is supplied through the manifest or `--audio-metadata-file`; individual CLI fields such as `--artist` are not currently implemented.
- One front cover is supported.
- Existing embedded source cover art is not automatically reused when no explicit `cover` is supplied.
- Embedded lyrics and synchronized lyrics are not currently written.
- The MP3 bitrate is currently fixed at 320 Kbps for encoded sources.

## Verification

Inspect audio, tags, duration, and cover disposition with FFprobe:

```bash
ffprobe -v error \
  -show_entries \
  'format=duration,bit_rate:format_tags:stream=codec_name,codec_type,sample_rate,channels:stream_disposition=attached_pic' \
  -of json \
  output.mp3
```

A valid covered MP3 should contain one MP3 audio stream and one image stream with `attached_pic=1`. The reported duration must match the complete source track, not the duration of the cover image.
