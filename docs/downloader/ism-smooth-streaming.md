## Smooth Streaming ISM CENC Init Track IDs

Some Smooth Streaming services number MP4 track IDs across every
`QualityLevel` in the manifest instead of starting from `1` inside each
`StreamIndex`. For example, a manifest with five video qualities followed by
one audio quality can carry audio fragments whose `tfhd.track_ID` is `6`.

When UniDL builds synthetic ISM init segments, it now preserves an explicit
`TrackID` attribute when present and otherwise assigns IDs in manifest
`QualityLevel` order. This keeps the generated `tenc` defaults aligned with
the downloaded media fragments, so the internal fMP4 CENC fragment decrypter
can find the correct default key metadata for audio and video fragments.

This is scoped to the ISM parser's generated init segment metadata. It does not
change HLS, DASH, JSON direct URL, SABR/UMP, live recording, or key selection
logic.

Live ISM fragments can differ from VOD here: when a live manifest omits an
explicit `TrackID`, the media fragments commonly use `tfhd.track_ID=1` for the
single selected track even if the manifest has several video qualities before
the audio quality. For live ISM, UniDL therefore defaults the generated init
track ID to `1` unless the manifest provides an explicit track ID.

## Live Manifest Refresh

Smooth Streaming live manifests expose media URLs with `{start time}` values.
Those values are stable timeline identifiers; the visible manifest window can
slide while per-refresh list positions restart at zero.

ISM live handling is isolated in `src/unidl/downloader/ism_live.py`. For live ISM
streams, UniDL uses the Smooth `start time` as the media segment index and
stores the same value as `timeline_time`. This keeps live refresh de-duplication
and audio/video alignment on the actual Smooth timeline, so a sliding 30-segment
window can continue recording after the first downloaded batch.

For encrypted H.264/AAC live ISM pipe mux, the existing fMP4 live pipe path is
used: decrypt each media fragment with the current ISM init, restamp it, remux
supported fragments to MPEG-TS, and feed ffmpeg. No separate HLS, DASH, JSON, or
SABR live mux behavior is changed.

The ISM live rule is intentionally narrow. VOD ISM streams keep their existing
0-based segment indexes, and DASH, HLS, JSON direct URL, and SABR/UMP live paths
continue to use their own timing and refresh rules.
