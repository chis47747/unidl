# Native downloader integration

UniDL's downloader is an in-process engine behind the Core delivery contract.
Services prepare authorized playback information; Core handles selection and
passes a typed delivery plan to the downloader. No external downloader process
or separate checkout is required.


The downloader implementation now lives inside the `unidl` distribution as
`unidl.downloader`.  A fresh install contains one application and has no
external downloader dependency, editable checkout, Python import, or downloader
subprocess. The production session parses and executes through the typed Core
contract. The same installed `unidl` executable owns the optional native
`list`/`download` media commands used by exported commands.

No account login, service extraction, CDM, licence transport or key acquisition
moves into the downloader.  Those remain UniDL responsibilities.  The embedded
downloader continues to receive manifests, selected tracks and raw keys only.

The native implementation accepts public `DownloadHooks`. Live KID
rotation is delivered through a typed request to the host; UniDL no longer
replaces `sys.stdin` or monkey-patches `_prompt_for_live_key`.  Invoking the
native `unidl download` command without hooks retains its terminal prompt.
The same hook object reports final media, subtitle and metadata paths after
cleanup, so the typed backend no longer has to infer successful output names.
It also publishes normalized VOD/live track progress and accepts an independent
cancellation predicate.  Native transfer callbacks and live manifest waits
check that predicate; cancellation no longer depends on stdout being written or
on the TUI's ANSI screen adapter seeing another frame.
Panel width is supplied by a context-local callback. Resizing is observed on the
next render without replacing module globals, changing ``COLUMNS`` or serializing
otherwise independent downloads behind a global lock.
When the TUI supplies a progress consumer, it disables the native engine's console
progress and renders those typed events itself.  The standalone CLI keeps its
current ANSI progress display because `console_progress` defaults to enabled.

The TUI exposes a stateful **Pause / Resume** control that holds the same
delivery job: workers wait between segments or live refreshes, the screen stays
put, and Resume continues. Back, the first Quit and skip still signal the Core
cancellation token. Resume on a finished/cancelled row still retries from
verified parts. The TUI never types into a CLI prompt to achieve either
operation.

Several provider profiles can still produce one native delivery plan. A service
opts in with `Playback.merge_manifests` and supplies fully authorized variants
through `manifest_variants`; Core parses each request separately and the backend
builds a deduplicated, JSON-backed manifest. The combined source is used by the
picker, execution, saved command and export so their indexes cannot drift.

## Target boundary

`unidl.core.delivery` owns the host-facing contract:

- immutable source, output and plan values;
- structured progress, log, stage and output events;
- a cancellation token checked by the execution path;
- an explicit live-key callback;
- a structured result with exact output artefacts and a typed failure.

`src/unidl/downloader` is the implementation. The TUI consumes Core events
directly; only the backend implementation reaches its internal execution API.
Core does not construct argv, capture ANSI output, replace process-wide streams,
or patch private downloader functions. Unknown service delivery switches fail
at plan construction instead of crossing the boundary as arbitrary arguments.

## Script manifest URLs and request encoding

Do not treat the URL suffix as the response format. Script endpoints such as
`getm3u8.jsp` are manifest candidates even if HEAD is unsupported or the server
uses `text/plain`; inspect the response body before building tracks. A failed
candidate fetch must report its error, not silently become a single video segment.
Known direct-media extensions retain their direct-download path.

The native HTTP transports percent-encode raw spaces and UTF-8 characters in
paths and query strings, including redirect targets. Existing percent escapes,
literal plus signs, duplicate parameters and parameter order are preserved;
never decode and rebuild a signed query to fix a space. Raw control characters
are rejected before URL parsing rather than silently stripped. This encoding
belongs to the download transport and does not change service authentication.

## HLS initialization-section key scope

For whole-segment HLS encryption (such as AES-128), an `EXT-X-MAP`
initialization section retains the key and IV in effect at its declaration.
A later `EXT-X-KEY` must not retroactively encrypt a clear MAP, including after
`METHOD=NONE`. A MAP declared under an active AES key still goes through normal
decryption and ciphertext validation. Do not infer that a response is clear
merely because its length is not a multiple of the AES block size.

The compatibility path that associates trailing CMAF MAPs with later
sample-encryption metadata (SAMPLE-AES/CBCS/CENC) is separate from whole-file
AES decryption and remains supported.

## Whole-file striped Blowfish audio

`BF_CBC_STRIPE` is a transport cipher, not MP4 sample encryption. JSON audio
tracks may declare it with `encrypted: true` and
`encryption_scheme: "BF_CBC_STRIPE"`; the native command accepts
`--custom-hls-method BF_CBC_STRIPE --custom-hls-key <hex-key>` too. The key must
contain 16 bytes. An optional `--custom-hls-iv` contains 8 bytes; its default is
`0001020304050607`. The algorithm and raw key/IV use the existing saved-command
and portable-export fields; no CDM or licence exchange is added.

Only one complete, non-live media file is supported. Automatic range splitting
is disabled; explicit byte ranges and multipart inputs are rejected rather than
resetting the stripe position and producing corrupt audio. Short final blocks
are retained without padding removal. `--no-decrypt` keeps the original bytes.
This path buffers the file before decoding; it does not add streaming decryption
or partial-file resume. Existing FLAC filenames and `<Ext>` naming patterns keep
their `.flac` suffix, including when no audio conversion was requested.

## MP4 encryption evidence and clear audio

Encryption metadata is read at actual ISO-BMFF box boundaries, including nested
sample entries and protection containers. Audio/video bytes inside `mdat`, codec
configuration records, and unknown leaf boxes are not searched for box names.
Coincidental `tenc`, `senc`, `schm` or `sinf` bytes in a media payload must never
invent DRM metadata or be rewritten as if they were real encryption boxes.

After download, an audio track labeled encrypted can be corrected to clear only
when its complete fragmented MP4 proves this: one audio track with valid clear
sample entries, no protection scheme/defaults, and no fragment encryption data.
The correction clears stale track/segment KIDs, is logged, and skips unnecessary
decryption without copying or changing the audio. It uses the downloaded file;
no extra network probe is made. Missing KIDs, null placeholder KIDs, incomplete
initialization data and unsupported layouts are not evidence of clear audio.
`--no-decrypt`, transport ciphers and genuinely encrypted media retain their
existing behavior. This does not change service licensing or key-vault policy.

## Separately compressed MP4 initialization

An origin can gzip its MP4 initialization resource independently of the media
segments. During assembly, UniDL expands only a validated gzip member containing
complete `ftyp`/`moov` init boxes; subsequent media bytes are copied unchanged.
The original wire parts remain intact for resume and size checks. The same
normalization applies to initialization-section outputs and Python/aria2 assembly.

Previously completed tracks with this compressed prefix are normalized into
temporary mux inputs, so retrying a failed mux does not require downloading the
audio again. Their originals, language/default flags and other metadata are
preserved; temporary files are removed on success or failure. Expansion is
bounded, truncated/corrupt gzip is rejected, and this does not relax mux errors
or change the normal embedded-versus-separate audio selection policy.

## Service-staged XML subtitles

External subtitle files in `Playback.mux_imports` bypass downloaded-track
conversion. The shared mux preparation step therefore converts `.xml`, `.ttml`
and `.dfxp` inputs to temporary SRT files with UniDL's native subtitle converter
before passing them to FFmpeg or mkvmerge. This also covers BBC EBU-TT/IMSC
subtitles when the optional `subby` helper is absent or fails.

The original XML remains untouched; timing, text, language, name, disposition
and delay are retained. SRT does not preserve TTML styling or screen positioning.
Conversion uses short, per-job filenames to avoid repeating long release names
in Windows paths. Temporary files are removed on success, preparation failure,
cancellation or mux failure. Invalid subtitles fail with a subtitle-preparation
error rather than being silently discarded; downloaded media remains available.
Existing SRT/VTT and audio/video inputs keep their normal path.

## ASS/SSA subtitle inputs

Service-owned API subtitle preparation participates in the shared download
frame before native media transfer. Core observes selected sidecar rows and
their prepared files; it does not parse service log text to invent percentages.
Legacy hooks retain their transport and conversion behavior and display an
active/unknown progress state until file preparation succeeds. A service may
implement `prepare_download_with_progress(playback, log, progress)` and call
`progress(reference, received_bytes, total_bytes)` for measured byte progress;
use `None` for an unknown total. Core publishes completion only after the file
exists and has been added to `Playback.mux_imports`. Keep language, name, kind,
and forced/original flags on each import so identical-language tracks remain
distinct. Completed subtitle rows survive the handoff to native video/audio
progress. Command, list, and export modes do not download sidecars.

ASS/SSA tracks retain their input extensions, including codec-only JSON tracks.
Content-based recognition handles ASS/SSA text mislabeled as TTML or WebVTT
without treating header-like dialogue as a script. Recognized empty subtitle
documents do not cause an FFmpeg conversion warning. See
[ASS/SSA subtitle inputs](downloader/ass-subtitles.md) for conversion,
plain-text timing repair and style-preservation behavior.

## Service-opted-in muxed A/V projections

`Playback.split_muxed_audio` defaults to false. Services may opt in for clear or
AES-128 VOD to expose independent video/audio entries without fetching media or
init bytes before selection. The native backend retains the original source
recipe plus `split_muxed_audio: true` in the manifest-source JSON, preserving
master/media export and command replay without expanded segment arrays.
Service filters annotate originals before projection; origin and output-filter
identities are carried to both derived tracks.

The downloader shares each physical source within one task, then joins,
decrypts and losslessly extracts only selected first-video/first-audio streams.
Each output has an independent durable checkpoint. Failed extraction retains a
completed source checkpoint for retries; waiting for a shared source remains
cancellable. FFmpeg is owned by the existing download runtime. Default services,
unrelated tracks, legacy resume identities and live recording remain unchanged.
Sample-encrypted CENC/CBCS/SAMPLE-AES sources are not supported by this initial
separation contract; enabling it for them fails explicitly.

## Migration gates

1. One UniDL distribution contains the service layer and download engine; no
   second downloader package is installed or imported.
2. A typed backend can parse and execute without callers constructing argv.
3. Progress, final paths, errors and live-key requests are structured events.
4. Cancellation is checked in network/download workers, not at the next print.
5. Known service delivery switches are decoded into typed plan fields; unknown
   switches fail at plan construction instead of crossing the boundary.
6. ANSI/stdin/private-function compatibility shims are removed from Core.
7. VOD, audio-only, JSON, HLS, DASH, ISM, live, SABR, VGC and special HLS
   parity tests pass before the original source checkouts are retired.
