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
