# Apple TV: iTunes Extras

The Apple TV service home has a dedicated **iTunes Extras** entry. It accepts a
movie URL or content ID, resolves its metadata type, and lists purchased bonus
videos separately from ordinary UTS extras and trailers. Multiple clips can be
selected. Save names include the parent film, gallery, and clip title, not the
duration. Apple Music's home is unchanged.

## Catalog and entitlement

- Use the movie response's iTunes provider, `itunesMediaApiData`, and
  `iTunesExtrasUrl`. Do not guess an Extras ID or use public preview as playback.
- Require a purchased/redownload offer with `iTunesExtras` capability and its own
  HLS URL. Rentals, channel subscriptions, buy offers, and offers without that
  capability do not qualify. No HD/SD entitlement substitution is performed.
- Launch the API-provided package URL with the same account session, passing
  that purchased offer's `hlsUrl`. Redirects and unexpected launch hosts are
  refused. A preview response or missing video URL is not a playable entitlement.
- Decode embedded JSON from JSON or `its`/`its.serverData` documents without
  executing JavaScript. Traverse the supplied root and nested menu IDs, following
  video galleries only. No gallery ID, array position, or movie ID is hardcoded.
  Cycles and duplicate video URLs/bookmarks are ignored. Main-feature PLAY nodes,
  related titles, image galleries, and background audio are not bonus videos.
- Other document schemas fail explicitly; this is not a general JavaScript or
  ITML application runtime.

## DRM

Native PlayReady/Widevine initialization data is preferred when present. Some
older Extras media playlists instead contain only a FairPlay-shaped
`skd://itunes.apple.com/p<content-number>/c<key-class>` URI with SAMPLE-AES.
That syntax alone does **not** prove the asset is FairPlay-only.

For this specific legacy format, and only for an iTunes Extras playback:

1. Decode the key identity as the 64-bit big-endian content number followed by
   ASCII `c0` through `c6`, padded with spaces to eight bytes.
2. Construct an AESCBC PlayReady WRM header and UTF-16-LE PlayReady Object. This
   initialization data is constructed locally, not a PSSH supplied by the server.
3. Send the normal PlayReady challenge to the iTunes license endpoint derived
   from the **owned parent offer**, retaining the parent movie Adam ID.
4. The license key URI is `data:text/plain;charset=UTF-16;base64,<PRO>`.
   The UTF-16 charset is required by the tested endpoint; the raw SKD URI is not
   used as the PlayReady license URI.

This legacy compatibility path has been verified with PlayReady, not Widevine.
Choosing Widevine when there is no Widevine PSSH gives an explicit instruction to
select PlayReady; the service never silently changes DRM systems.

The existing playlist selection policy remains unchanged: `license_tracks=ALL`
means representative playlists for all available Apple track classes, not every
variant in the master. Explicit classes read only those representatives. Core's
`selected` mode reads only final selected audio/video playlists. Preparation caches
those media playlists for the license step. Account/geo denials still stop further
license requests. Vault handling stays in Core. Extras does not create or borrow a
main-feature playback heartbeat/session; existing cleanup paths remain in use.

## Verified Scope and Downloader Status

Account-authorized validation of
`umc.cmc.34uxclwqqvnlartmkxtcnh9ww` returned **six Deleted Scenes videos**.
The integrated service path prepared AUDIO and FHD_SDR for the first clip, made
two PlayReady exchanges, and checked that each returned key matched its playlist
KID. No credentials or content keys were printed or added to test fixtures.

This validates **catalog access and licensing**, not a successful end-to-end
download of the tested clip:

- The tested clip uses SAMPLE-AES `.aac` and `.ts` segments without an HLS MAP.
- The native downloader recognizes the legacy KID and decrypts downloaded
  AAC/AC-3/E-AC-3/H.264 samples before handing clear parts to FFmpeg for remux and
  validation, not the MP4 CENC/CBCS path.
- Byte ranges are already sliced by the downloader. Local segment files must
  not reuse offsets into the original remote resource. Segment boundaries,
  IVs, key rotation, media sequence and discontinuities are preserved.
- The clear local playlist and segments are private, local-only and removed on
  success, failure or cancellation. No key files are written and FFmpeg only
  receives clear local media. Publication is atomic and cancelled or
  failed validation leaves any existing output untouched.
- Decoding runs to completion rather than stopping at the first bad frame.
  The validation counts decoded frame hashes and decoder errors, allowing up
  to 1% reported frame errors with an explicit damaged-frame warning. It does
  not drop bad packets from the published stream-copy file or transcode it.
  Zero decoded frames or widespread corruption still prevents publication.
  FFmpeg exit status alone is insufficient: the installed 7.1.1 can return zero
  despite hundreds of decoder errors, even with `-max_error_rate` specified.

Real-account retesting still produces widespread AAC/H.264 decode errors.
The legacy FairPlay declaration has no explicit IV.
Bento4's FairPlay IV mode demonstrates that an IV can be delivered separately
from the playlist; a matching PlayReady KID is not evidence of the correct CBC
IV. Missing IV/key compatibility remains under investigation, not a confirmed
root cause. No alternate-IV guessing, codec substitution or error suppression
is used to declare the real clip playable.

### Segment-bound Decryption Fix

An independent Bento4 `mp42hls` fixture reproduced corruption in the original
FFmpeg-only path with known keys/IVs: the last buffered video packets remained
encrypted. FFmpeg 7.1.1's HLS packet reader advances its current segment while
demuxing, then uses that segment to select the decryption context. Buffered
packets at EOF or a key/IV transition cannot safely rely on that context.

The AAC/H.264 path now decrypts each downloaded segment first:

- AAC retains ID3 metadata, ADTS headers, the 16-byte clear leader and trailer;
  CBC resets per frame, without padding removal.
- H.264 decrypts types 1/5 only, after the 32-byte leader, with one encrypted
  block per ten. It removes only the encryption-added emulation-prevention layer.
- Complete elementary streams are assembled across PES boundaries within a
  segment, then placed back into the original TS packet spans. PES lengths,
  PTS/DTS, PCR, packet counters and adaptation data remain accounted for.
- Each segment keeps its own key/IV; clear sections remain clear. Invalid or
  truncated framing is not silently discarded. PES spanning segment boundaries
  and unsupported TS codecs are not implemented by this path.
- FFmpeg still remuxes and validates; decoding continues through isolated errors.
  Its `Decoding error:` diagnostic is now counted as well as packet submission
  errors, preventing an apparent zero-error result on affected FFmpeg versions.

Offline checks use Bento4-generated audio-only, video-only and multiplexed TS
with changing sequence IVs and explicit out-of-band IVs. Decoded frame hashes
match the clear reference for all six cases. Bento4 is an optional test tool,
not a new runtime dependency.

The remaining real-asset failure is distinct from that fixed boundary bug:
the first tested AAC segment contains 215 frames; direct sample decryption and
the previous FFmpeg decryptor produce identical audio packets, with only 29
decoded frames and 186 decoder errors. The video segment still fails after
decrypting its previously missed tail packets. Authorized PR responses report
status 0 and AES-CBC key type, but the inspected launch metadata, playlists,
ID3 metadata and parsed PR response fields did not expose an explicit IV.
These observations do not prove a key mismatch or an IV-only problem. A verified
source of the asset's complete decryption context is still needed; license
success alone is not an end-to-end playback test.

### Real-Asset Follow-Up: Still Failing

Fresh account-authorized checks reproduced the failure for the first two clips
in the purchased package. The first clip's first AAC segment still yields 29
decoded frames and 186 reported decode errors; its first H.264 segment yields
24 decoded frames and 120 reported decode errors. Native download validation
rejects the output rather than publishing it as successfully decrypted.

The following controlled diagnostics did not fix the first clip and were not
added as runtime fallbacks:

- Native PR declarations from the owned parent movie use AESCBC and the same
  GUID byte-order convention used by the synthesized Extras declaration.
  Requesting AESCTR instead returned identical keys and identical decode errors.
  Reversing the GUID serialization was rejected by the license flow.
- Adding the same purchased parent's Adam ID as the manifest's `a` parameter
  changed the reported client profile from `AppleTV6,3` to the configured TV
  model, with more CDN alternatives, but the selected media remained packed AAC
  and TS with FairPlay key declarations and no explicit IV. Fresh licensing and
  native decoding still failed. Platform `xapdn`/`xapdm`/`xapdl` parameters alone
  did not change the old profile.
- Independently requesting a license at the Extra playback host's MZPlay
  endpoint, instead of the parent's MZPlayLocal endpoint, still produced an
  authorized response followed by decode validation failure.

These checks do not establish whether the remaining incompatibility is the
content key, an out-of-band IV, or a source-specific sample layout. No IV was
recovered or guessed, and no other title's keys were used. A matching KID and
successful license response are insufficient evidence to choose a production
decryption change. The real-asset AAC/H.264 issue remains unresolved.

### Direct mp4decrypt Test

The locally installed Bento4 1.6.0.0 `mp4decrypt` 1.4 was tested directly on
the first two original encrypted AAC segments (153,717 bytes) and H.264 TS
segments (13,492,384 bytes) from the authorized first clip. Each input was
tested with its exact KID/key, an all-zero KID with the same key, and track ID
1 with the same key. All six
invocations returned exit 0, no stderr and **zero-byte output**. FFmpeg decoded
zero frames from those outputs. This tool invocation does not decrypt raw
TS/ADTS SAMPLE-AES; successful process exit is not successful decryption.
Renaming these files to MP4 would not supply the missing MP4 sample/encryption
metadata. No default engine change was made based on this failed test, and
temporary media were removed.

The all-zero KID workaround is an MP4 key lookup issue: an init segment can
advertise a zero `tenc.default_KID` while the license identifies the key with
its actual KID. UniDL's MP4 parser can map that zero default to the single
expected KID. This does not change the AES key or IV. These Extras segments
are raw TS/ADTS with no `tenc` box; their sample decryptor receives key bytes
directly after playlist KID lookup. Relabeling an otherwise identical key
cannot change that AES operation. The direct tool test does not establish
whether the issued key and missing IV match the underlying media.

### Supplied Log: Packed AC-3 Is a Separate Case

The supplied `message.txt` logs media content number `54401568`, whereas the
first clip used in the local AAC/H.264 diagnostics is `54431481`. These are not
the same media sample. The provider reports that video plays and that audio
packing is being fixed; the supplied log itself ends without a video
decryption/completion result.

The log's English and German audio failures occur in Shaka's container
detection, before sample decryption/codec decoding. Its diagnostic buffer
contains ID3v2.4 (138 bytes), the private owners
`com.apple.streaming.transportStreamTimestamp` and
`com.apple.streaming.audioDescription`, codec tag `zac3`, and an AC-3 sync
word `0b77` with bsid 8 immediately after ID3. This is packed encrypted AC-3,
not AAC or fragmented MP4, despite the downloader naming it `output.Audio.m4s`.
Renaming alone cannot convert the container. These failures support investigating
packed-audio demux/sample handling, not concluding that PR licensing failed or
that video is unplayable. The local AAC/video decoding failures remain separate
observations, not a reproduction of this specific packed AC-3 failure.

### Packed AC-3 Fix and Verification

Bento4-generated packed AC-3 with known keys/IVs reproduced a local failure in
the previous FFmpeg-only demux/decryption path. Packed `.ac3` segments now use
native syncframe decryption before FFmpeg demuxing. ID3 is preserved, frame
sizes come from the clear AC-3 headers, CBC resets at each syncframe, the first
16 bytes remain clear, and only complete subsequent AES blocks are decrypted.
No MP4 wrapping, codec conversion, guessed key, or alternative-IV retries are
involved. Invalid/truncated frames are reported rather than silently dropped.

Tests cover 32/44.1/48 kHz (including the alternating 44.1 kHz frame lengths),
retained ID3, wrong-key rejection, native download to MKV and actual decode.
Bento4-generated packed AC-3 with sequence and explicit IVs also matches the
clear reference's decoded frame hashes. Runtime dependencies are unchanged.

Fetching the supplied log's manifest confirmed `.aac` and `.ac3` audio playlists
and `.ts` video. Its first AC-3 range has 138 ID3 bytes followed by sync `0b77`,
bsid 8. However, content `54401568` is absent from the authorized test movie's
six Extras. Rechecking the beginning of the supplied log identified the parent
as `umc.cmc.4ag0mvetbi9sovd6rzaxyl0cn`, with playable identity
`itunes-extra:962931571:54401568`. The earlier statement that the log contained
no parent identifier was incorrect. That parent's entitlement has not been
verified, so no other movie's authorization was substituted. Real-asset
audio/video decryption for that log remains unverified; the passing offline
tests do not establish that it now plays.

### Packed Audio Follow-Up

Standalone `.ac3`, `.ec3` and `.eac3` media playlists were incorrectly classified
as video. They are now classified as audio; `.ec3` is recognized as the E-AC-3
container alias and normalized to `.eac3` in the local processing stage. This
does not route fragmented MP4 with an initialization segment into packed audio.

Bento4's sequence-IV E-AC-3 fixture reproduced a demux/decryption failure in
the previous FFmpeg path after fixing the `.ec3` detection gap. E-AC-3 now uses
native syncframe processing too: frame lengths come from clear headers, the
16-byte leader and incomplete trailing block stay clear, and CBC restarts for
each syncframe, including dependent substreams. Key/IV selection remains bound
to each downloaded segment. There is no alternative-key or IV-guessing fallback.

Independent Bento4 fixtures for packed AAC/AC-3/E-AC-3, H.264 TS and combined
AAC/H.264 TS pass native download, decryption, MKV remux and decoded-frame hash
comparison with clear references, using sequence and explicit IVs. Additional
tests cover dependent E-AC-3 frames, malformed headers, clear sections, key/IV
rotation and keeping keys out of FFmpeg arguments and temporary files.

These are confirmed container/sample-processing fixes, not confirmation that
the separate real-asset AAC/H.264 corruption described above has been resolved.

### Implementation References

- [FFmpeg HLS demuxer](https://github.com/FFmpeg/FFmpeg/blob/n7.1.1/libavformat/hls.c)
- [FFmpeg sample encryption](https://github.com/FFmpeg/FFmpeg/blob/n7.1.1/libavformat/hls_sample_encryption.c)
- [hls.js sample decryptor](https://github.com/video-dev/hls.js/blob/master/src/demux/sample-aes.ts)
- [Bento4 HLS packaging and FairPlay IV mode](https://github.com/axiomatic-systems/Bento4/blob/master/Source/C%2B%2B/Apps/Mp42Hls/Mp42Hls.cpp)

AAC leaves its header and first 16 payload bytes clear; H.264 protects one
16-byte block per 160 bytes after the first 32 NAL bytes and requires the
specified emulation-prevention handling. Neither is whole-segment CBC.

## Offline Tests

```sh
./.venv/bin/ruff check src/unidl/services/apple/__init__.py src/unidl/services/apple/drm.py src/unidl/services/apple/itunes_extras.py tests/test_apple_itunes_extras.py
env -u NO_COLOR TERM=xterm-256color ./.venv/bin/pytest -q tests/test_apple_itunes_extras.py tests/test_apple_service.py tests/test_apple_music_service.py
env -u NO_COLOR TERM=xterm-256color ./.venv/bin/pytest -q tests/downloader/test_sample_aes.py
```

Fixtures are synthetic and need no account, network, or CDM. Tests cover wrapper
formats, dynamic menus, ownership, preview rejection, KID/PRO encoding, transport,
representative and final-selected inventories, navigation, and existing Apple
TV/Apple Music behavior.
