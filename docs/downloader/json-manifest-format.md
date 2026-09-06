# UniDL JSON Manifest Format

This file defines the stable JSON input format accepted by `UniDL` for direct URL track lists.

## Root

The root may be one title object or an array of title objects.

```json
[
  {
    "id": "title-id",
    "type": "movie",
    "name": "Title Name",
    "season": 1,
    "episode": 2,
    "episode_name": "Episode Name",
    "original_language": "en",
    "duration_ms": 3600000,
    "video_tracks": [],
    "audio_tracks": [],
    "subtitle_tracks": []
  }
]
```

## Track Lists

Use these arrays:

- `video_tracks`
- `audio_tracks`
- `subtitle_tracks`

Aliases are also accepted for compatibility:

- video: `videos`, `video`
- audio: `audios`, `audio`
- subtitles: `subtitles`, `text_tracks`, `texts`

## Common Track Fields

```json
{
  "id": "track-id",
  "url": "https://cdn.example/media.mp4",
  "all_urls": [
    "https://cdn-a.example/media.mp4",
    "https://cdn-b.example/media.mp4"
  ],
  "codec": "avc1.640028",
  "mime_type": "video/mp4",
  "bitrate": 5000000,
  "duration": 3600.0,
  "duration_ms": 3600000,
  "size_bytes": 2250000000,
  "encrypted": true,
  "encryption_scheme": "CENC",
  "kid": "00112233445566778899aabbccddeeff"
}
```

Supported aliases:

- URL: `url`, `uri`, `href`
- URL fallbacks: `all_urls`, `urls`
- codec: `codec`, `codecs`, `format`
- size: `size`, `size_bytes`, `sizeBytes`, `content_length`, `contentLength`, `clen`
- duration seconds: `duration`, `duration_seconds`, `durationSeconds`
- duration milliseconds: `duration_ms`, `durationMs`
- KID: `kid`, `key_id`, `keyId`
- encryption scheme: `encryption_scheme`, `encryptionScheme`, `scheme`
- extension: `extension`, `ext`
- MIME: `mime_type`, `mimeType`

If `url` contains query values like `clen=...` or `dur=...`, UniDL also uses them as size and duration hints.

## Video Track Fields

```json
{
  "id": "video-1",
  "url": "https://cdn.example/video.mp4",
  "codec": "hvc1.2.4.L150.90",
  "mime_type": "video/mp4",
  "width": 1920,
  "height": 1080,
  "fps": 23.976,
  "bitrate": 8000000,
  "hdr": "HDR10",
  "supplemental_codecs": "dvh1.08.06"
}
```

Supported video aliases:

- frame rate: `fps`, `frame_rate`, `frameRate`
- dynamic range: `hdr`, `video_range`, `videoRange`, `range`, `dynamic_range`
- Dolby Vision helper: `supplemental_codecs`, `supplementalCodecs`

Recognized ranges include `SDR`, `HDR10`, `HDR10+`, `HLG`, `DV`, and `DV+HDR10`.

## Audio Track Fields

Title-level ID3 metadata, MP3 export behavior, and cover art imports are defined in
[`mp3-audio-format.md`](mp3-audio-format.md).

```json
{
  "id": "audio-1",
  "url": "https://cdn.example/audio.mp4",
  "codec": "ec-3",
  "mime_type": "audio/mp4",
  "language": "en",
  "channels": "6",
  "bitrate": 448000,
  "is_original": true,
  "descriptive": false
}
```

Supported audio aliases:

- language: `language`, `lang`, `locale`
- original marker: `is_original`, `original`
- descriptive marker: `descriptive`, `description`

## Subtitle Track Fields

```json
{
  "id": "sub-1",
  "url": "https://cdn.example/subtitles.vtt",
  "codec": "webvtt",
  "language": "en",
  "forced": false,
  "sdh": true
}
```

Supported subtitle codecs include WebVTT, TTML/DFXP, and SRT.

## Explicit Segments

For already segmented direct inputs, put segment entries on the track.

```json
{
  "id": "video-segmented",
  "url": "https://cdn.example/base/",
  "codec": "avc1.640028",
  "segments": [
    {
      "url": "init.mp4",
      "index": -1,
      "byte_range": [0, 999]
    },
    {
      "url": "seg-1.m4s",
      "index": 1,
      "duration": 2.0,
      "byte_range": "1000-1999"
    }
  ]
}
```

Segment fields:

- `url`, `uri`, `href`
- `index`; use `-1` for init segments
- `duration`
- `byte_range`, `byteRange`, or `range`; accepted forms are `[start, end]`, `{"start": 0, "end": 999}`, or `"0-999"`
- `encrypted`, `encryption_scheme`, `kid`

Relative segment URLs are resolved against the track URL.

## SegmentBase / Single Direct File Hints

For single-file direct URLs, provide exact size and optional init/index ranges when available.

```json
{
  "id": "649",
  "codec": "vp9.vp09.00.30.08.00.01.01.01.00",
  "mime_type": "video/mp4",
  "url": "https://rr.example/videoplayback?clen=219054029&dur=6832.867",
  "size_bytes": 219054029,
  "duration": 6832.867,
  "init_range": {"start": "0", "end": "1732"},
  "index_range": {"start": "1733", "end": "18156"}
}
```

`init_range` and `index_range` are preserved in metadata. `size_bytes` or URL `clen` lets UniDL split a large direct file into byte-range parts without a slow size probe.

## Live JSON Template

For JSON live/DVR exports, use `liveMetadata`.

```json
{
  "liveMetadata": {
    "downloadableIdToSegmentTemplateId": {
      "video-1": "0"
    },
    "segmentTemplateIdToSegmentTemplate": {
      "0": {
        "availabilityStartTime": "2026-05-10T00:00:00Z",
        "timescale": 1000,
        "media": "video_$Number$.cmfv",
        "initialization": "video_init.cmfv",
        "duration": 2000,
        "startNumber": 1
      }
    },
    "eventStartTime": "2026-05-10T00:00:00Z",
    "eventEndTime": "2026-05-10T01:00:00Z",
    "eventAvailabilityOffsetMs": 0,
    "ocLiveWindowDurationSeconds": 14400
  },
  "video_tracks": [
    {
      "id": "video-1",
      "url": "https://cdn.example/live?token=abc",
      "all_urls": [
        "https://cdn-a.example/live?token=abc",
        "https://cdn-b.example/live?token=abc"
      ],
      "codec": "hevc-main10-dash-cenc-live",
      "kid": "00112233445566778899aabbccddeeff"
    }
  ]
}
```

`$Number$` is expanded using the template timing. `all_urls` is used as a CDN fallback pool.

## Notes

- Prefer `mime_type` when the container matters. For example, VP9 can be `video/webm` or `video/mp4`; UniDL treats these differently during decryption.
- Prefer exact byte sizes for large direct URLs. `size_bytes` or URL `clen` avoids an extra network probe.
- Use `CENC` or `CBCS` when known. If omitted but `encrypted` or `kid` is present, UniDL marks the track as encrypted.
