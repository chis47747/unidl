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

## HTTP connection reuse and adaptive concurrency

Native HTTP/1 connections apply each request's timeout to both the connection
object and its existing socket. A short metadata/range probe therefore cannot
silently impose its deadline on a later media download; reducing the timeout
also takes effect on reused connections. Closed pooled sockets are discarded.

The Python downloader limits active requests per origin within its configured
worker budget. A cohort of requests already in flight when an error occurs
does not repeatedly reduce the same host's concurrency. Timeouts, disconnects
and incomplete transfers use a smaller reduction and a short bounded cooldown;
HTTP 429 and retryable server errors retain stronger reduction/backoff. A
server refusal may upgrade a mild reduction from the same cohort once.
Successes from before a reduction cannot cancel its cooldown. Successful new
requests increase concurrency progressively; repeated failing new cohorts can
still reduce it to one. Authentication errors and rejected Range requests do not
trigger host-wide adaptive throttling. Waiting for a host slot is cancellable.

When a task debug log is enabled, `network host=...` entries record the initial
limit, failure class/count, whether a new backoff was applied, active requests,
cooldown and concurrency recovery. These entries include only the origin, not
signed segment paths, query strings, request headers, raw error messages or keys.
They do not add network probes or console/TUI messages. This is generic transport
behavior, not a per-service speed override or a guarantee of CDN throughput.

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

### Apple HLS programme identity and A/V alignment

Apple HLS playlists may bundle a main programme with a bumper and other assets.
Localized video, audio and subtitle resources can have different `P` programme
numbers while sharing one `A` playback asset number. For Apple-hosted VOD with
an explicit `mainAssetAdamId`, or a single numeric `a` parameter, the downloader
matches that asset against the actual `P…_A…_` media filenames. It applies the
same rule to all selected track types, retaining complete initialization groups
(or individual plain WebVTT segments). Query strings and `_HLS_prefill` hints do
not identify the current media. A list of assets is never resolved by choosing
its first entry. The legacy programme-ID rule remains available when no usable
Apple asset identity is exposed.

Filtering also runs for already populated/imported segment lists before resume
checkpoints are consulted. Duration and size estimates are refreshed; complete
segment KID inventories exclude keys used only by removed assets. Incomplete
inventories retain their existing init/session fallback. A changed track cannot
reuse a completed checkpoint for the old, unfiltered audio. No extra playlist,
media or licence request is introduced by identity matching.
Numbered fragment caches use the exact filtered segment-list identity: the
legacy URL-only fallback cannot adopt an older cache with shifted part indexes.
Existing source caches are not modified or deleted by this check.

This does not crop by duration, stretch audio, impose an arbitrary sync delay,
or rewrite source timestamps. Same-asset sections, including localized credits,
remain available for the separate compatibility check below. A missing or
unrecognized asset match leaves the track intact rather than guessing. Live
recordings are not programme-filtered, and other providers do not gain Apple's
asset-ID interpretation.

### HLS initialization sections and placeholder KIDs

A selected HLS track may contain several `EXT-X-MAP` sections with different
keys, for example a main feature followed by a localized credit sequence.
All required keys are retained. During section decryption, each MAP's segment
KIDs are passed separately instead of giving every section the track-wide list.
This lets a section with a zero `tenc.default_KID` use its one unambiguous
playlist KID. A nonzero initialization KID or an explicit fragment KID is not
overwritten by that fallback.

Unknown or ambiguous section KIDs are not resolved by trying the first supplied
key; normal missing-key errors remain in effect. The mapping is rebuilt from
the selected segments on retries too, so already downloaded sections can be
reused. No additional manifest, media or licence request is introduced, and
single-init, DASH/ISM and live-recording decryption paths are unchanged.

### Section concat compatibility and output validation

During MP4 section finalization, clear `c608` samples that consist entirely of
zero bytes are normalized to same-size EIA-608 padding atoms. Some origins use
these as empty caption placeholders; FFmpeg's concat reader rejects them even
when only video is selected. Track/sample boundaries and media-data bounds are
validated before modification. Valid captions, protected entries and all
audio/video payloads are left unchanged; arbitrary malformed captions are not
silently ignored.

For non-live Apple HLS video, a trailing sequence explicitly named as the same
asset's `FF_credit-` resources may contain an incompatible optional credit
render. After download, the actual initialization data is compared with the
main programme's codec, coded dimensions and available colour declarations.
Only confirmed incompatible credit sections are excluded, with their count and
duration logged. Compatible or unknown sections, ordinary programme sections,
other assets/providers and live streams are retained. This does not infer
incompatibility from a KID or discontinuity, transcode video, alter the cached
source files, or make extra network/licence requests.

FFmpeg concat runs with fatal-error handling and also rejects an explicit
demuxing error when the process unexpectedly returns zero. It writes a temporary
output that is published only after success; partial/empty outputs are removed
without replacing an existing finished file.

### Bounded-memory MP4 metadata editing

Timestamp/section finalization and local timing/KID probes no longer load the
entire media file into a Python byte buffer. Probes use a read-only file mapping
and skip media payload boxes. Edits retain only changed 4 KiB pages; publishing
streams the source in 8 MiB chunks and applies those metadata pages to an
atomic temporary output. This avoids both file-sized Python copies and
Windows file-sized copy-on-write mapping reservations. Working memory still
depends on metadata complexity, and operating-system file caching is separate.

Timing calculations, media bytes and empty-caption normalization are unchanged.
Failed edits leave the source, an existing destination and linked checkpoints
intact. Unchanged in-place edits do not copy the file. This is a memory
optimization, not a change to temporary-file retention, licence/decryption
policy, muxer selection or the number of staged sections.

Checkpoint directory totals may count several hard-linked names for the same
media data. Compare allocated/unique-file space, not only the sum of file sizes.
Debug/keep-temp mode may also retain earlier encrypted and clear generations;
those are disk files, not a corresponding allocation of resident RAM.

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

## VOD temporary disk usage

After a complete track has been assembled, normal cleanup removes redundant
download parts before whole-file decryption, but only when no later processing
needs them. With resume enabled, the assembled track is validated and its
checkpoint is atomically made independent of those parts before removal. A
decryption failure can therefore retry the assembly without downloading it
again. Older downloaded checkpoints are reduced in the same way on retry.
Fragment-only decryption, section/timestamp processing and live recording keep
their required inputs. `keep_temp` and disabled automatic cleanup retain files
as requested.

For an ordinary encrypted DASH VOD with a single KID per track, on one local
filesystem with hard-link support and normal cleanup, the main peak is roughly
two copies of the selected media plus safety headroom: parts plus their
assembly, encrypted plus clear track, or clear tracks plus the muxed output.
This is not a universal storage limit. Debug/retention settings, repacking,
audio transcoding, hybrid processing, filesystems without hard links and
cross-filesystem copies can need more. Check free space on the temporary
filesystem as well as the final destination; the mux is staged before it is
committed to the downloads directory.

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
