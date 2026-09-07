# Live Pipe Mux Notes

## HLS live multi-KID fMP4

For live HLS fMP4/CMAF streams that expose more than one KID, pipe mux verifies the KID on each downloaded media fragment before decryption. The fragment KID is treated as the source of truth for key selection, init patching, and key-rotation synchronization.

This fragment scan is intentionally narrow. It only runs for HLS live fMP4/CMAF streams that need external CENC/CBCS decryption and already have multiple known KIDs. Single-KID HLS, MPEG-TS HLS, VOD, DASH, JSON, and SABR paths do not pay this extra scan cost.

## DASH HEVC live finalization

Some DASH HEVC live feeds produce an incrementally writable MKV whose media
packets are present, but whose final container metadata and video DTS are not
compatible with stricter readers after recording stops. Some players then
report the file as internally abnormal.

For an eligible feed, UniDL keeps writing the MKV in real time during
recording. When recording stops, it first stream-copies the MKV to a temporary
MP4 to rebuild the DTS timeline. If `mkvmerge` is available, UniDL then extracts
an Annex-B HEVC elementary stream from that MP4 and lets `mkvmerge` write a
temporary MKV so the HEVC `CodecPrivate` contains VPS/SPS/PPS parameter sets.
The final user-facing file is then stream-copied to MP4 with an `hvc1` tag and
explicit BT.2020/HLG HEVC metadata for broader player compatibility. If
`mkvmerge` is not available or that compatibility pass fails, UniDL falls back
to ffmpeg stream-copy Matroska finalization before producing the compatible MP4.

When a live pipe mux recording is stopped with Ctrl-C, UniDL treats that interrupt as a user-requested stop for the current recording and still closes/finalizes the pipe mux output. Non-pipe live recording and ordinary download paths keep the existing cancellation behavior.

After this finalization succeeds and the output file exists, a non-zero exit
from the original live pipe ffmpeg process is tolerated. This is deliberately
tied to the successful compatibility path so unrelated live pipe mux failures
still surface as errors.

The finalization step is intentionally narrow. It requires all of these conditions:

- live pipe mux recording container is Matroska;
- selected stream is live DASH video;
- selected video codec is HEVC/H.265;
- selected stream URL/template/segment metadata matches the provider-specific
  compatibility rule.

Generic Matroska live pipe outputs, other HEVC live feeds, MPEG-TS live pipe outputs, HLS, SABR, ISM, JSON direct URLs, and VOD paths keep their existing behavior.
