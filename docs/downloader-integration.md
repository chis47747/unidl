# Native downloader integration

UniDL's downloader is an in-process engine behind the Core delivery contract.
Services prepare an authorized Playback; Core parses the source, presents track
selection, resolves keys, downloads segments, decrypts, writes sidecars and
muxes the final file.

## Service-staged XML subtitles

External subtitles in `Playback.mux_imports` bypass downloaded-track conversion.
The common mux preparation step converts `.xml`, `.ttml` and `.dfxp` files to
temporary SRT using UniDL's native converter before FFmpeg/mkvmerge sees them.
This handles BBC EBU-TT/IMSC subtitles without requiring the optional `subby`.

Original subtitles remain unchanged. Timing, text and input metadata are
preserved, but SRT does not retain TTML styling or positioning. Short per-job
temporary names avoid repeating long release names in Windows paths. Temporary
files are cleaned up after success, failure or cancellation. Invalid subtitles
produce a preparation error rather than being silently omitted; downloaded
media remains available. Existing SRT/VTT and audio/video inputs are unchanged.

## Supported source families

The native parser accepts:

- MPEG-DASH MPD, including CENC and PlayReady protection data;
- HLS master and media playlists, including AES-128, CENC/CBCS and live reload;
- ISM/Smooth Streaming manifests and initialization ranges;
- UniDL JSON track manifests;
- direct MP4, WebM, MP3/AAC and other supported media URLs;
- SABR/UMP sources exposed by compatible service adapters.

A service should pass the source URL or inline manifest and the headers required
for that source. It must not download segments or duplicate parser logic.

## Delivery contract

The host-facing contract is in unidl.core.delivery. It accepts a typed
DeliveryPlan, selected output settings and a structured event receiver. Events
cover:

- manifest and track discovery;
- selected tracks and encrypted KID inventory;
- vault hits, licence requests and returned keys;
- segment progress, estimated size and retry state;
- live refresh, key rotation, pause/resume and cancellation;
- sidecar, decrypt and mux progress;
- final output paths and errors.

The TUI renders these events. A headless caller can supply its own receiver or
save the same data in a command/export artifact.

## VOD pipeline

1. Service authorizes one or more manifests and yields Playback.
2. Core parses all authorized variants and deduplicates tracks when requested.
3. Core builds the encrypted inventory and runs the service DRM plan.
4. The key vault is consulted; missing keys use the service's own licence path.
5. The shared track picker selects video, audio and subtitle output tracks.
6. Native delivery downloads, decrypts, writes sidecars and muxes.
7. Optional chapters, lyrics, cover art and ID3 metadata are applied.

Output naming and metadata are derived from the tracks selected for download, not
from the provider profile used to obtain the manifest or licence seed.

## Live pipeline

1. Service yields Playback(is_live=True) with an authorized refreshable source.
2. Core parses the master and asks for output tracks.
3. Core asks for recording mode, replay/DVR window and duration.
4. The live engine refreshes playlists, downloads new segments and reports
   structured progress.
5. A service-local live_key_pssh hook may resolve a genuinely new KID.
6. Stop, Back or Esc cancels promptly; duration 00:00:00 means unlimited.
7. Core finalizes the container and performs optional post-processing.

Real-time merge and pipe-mux are selected by delivery options and source
capabilities. A service should not force a mux mode by changing output tracks.

## Keys and DRM ownership

The native engine never invents a licence endpoint. Core creates CDM challenges,
parses licence responses and normalizes content keys; the service provides the
transport and authentication. Every service that needs a licence implements its
own transport in its package.

For PlayReady, Core can deduplicate all distinct PSSH/WRM identities and merge
all returned content keys. HLS services whose master omits init data should expose
a service-owned licence-track/profile setting and provide the matching media
playlist or init data.

The downloader receives only the keys for the current Playback. Vault writes,
remote-CDM calls and service token handling remain explicit and auditable.

## Native API boundary

The public boundary is the DownloaderBackend protocol in
unidl.core.delivery. Core constructs a DeliveryPlan, calls backend.parse() once,
then calls backend.run(plan, hooks). Service code should yield Playback and let
the session controller build that plan; it should not import downloader
internals or start a second worker.

## Verification

Run the project checks:

~~~console
python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
~~~

For a provider integration, add local manifest fixtures and a redacted service
test that verifies the complete Playback-to-output path.
