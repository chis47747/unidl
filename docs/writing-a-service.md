# Adding a native service

A service is a small adapter between a provider API and UniDL's typed Core
contracts. It owns authentication, catalogue calls, manifest authorization and
licence transport. Core owns the interaction shell, manifest parsing, output
track selection, download, decryption, subtitles and muxing.

The result is one service that works in the TUI and in headless tests without a
second menu implementation.

## 1. Create and register the package

Create one package under `src/unidl/services/<service_id>/`. The repository
includes [`src/unidl/services/example/`](../src/unidl/services/example/) as a
small, import-safe reference scaffold; copy it, rename the class and IDs, and
replace its provider-specific placeholders before registering the new service:

~~~text
src/unidl/services/example/
  __init__.py        Service subclass, settings and Flow entry points
  api.py             HTTP client, response models and provider parsing
  drm.py             optional service-local licence helpers
  chapters.py        optional chapter parser
  tests/             optional service-focused fixtures
~~~

Use a stable lowercase ID; it is used in configuration, vault rows, token
folders, command exports and JustWatch mappings. NAME is the display name, TAG
is the short output/name tag, and ALIASES contains accepted search and palette
names.

The example package is intentionally not registered, so it does not appear as a
usable platform until its API, authentication and playback contract have been
implemented. Register a completed package once in
`src/unidl/services/__init__.py`:

~~~python
from . import bbciplayer
from . import example
~~~

The registry imports the package, validates its declarations and exposes it to
the platform list. Do not register a service more than once.

### What the reference scaffold demonstrates

`example/__init__.py` is the service-facing side of the contract. It declares
the service identity and capabilities, defines provider-owned settings, creates
a proxy/cookie-aware API client from `ServiceContext`, turns search results into
`Choice` values, and emits a typed `Playback`. Its `_playback()` method is the
place to translate a normalized API item into `Title`, `DrmInfo` and playback
metadata; it is not a second downloader.

`example/api.py` is the provider-facing side. `ExampleApi` owns the requests
session, URL construction, timeout and HTTP error handling. `ExampleItem` is the
small normalized model passed to the Flow, and `from_payload()` is where a real
provider's JSON is validated and converted. Replace the placeholder search and
resolve paths, authentication headers, manifest response fields and
`post_license()` body/response handling with the provider contract. Keep secrets
in `ctx.credential()`, `ctx.tokens` or managed cookies; never put them in this
template or in a URL constant.

The scaffold includes separate `manifest_profile` and `license_profile` settings
to make the timing boundary visible. The first may select which provider
manifest is requested; the second may select a provider-specific licence
context. Neither is the shared Track output selection. Add `drm.py`,
`chapters.py` or helper declarations only when the provider needs those
extension points, and document each setting beside its implementation.

## 2. Declare capabilities

The base Service.home() builds the service menu from capability flags. Override
only the entry points the provider supports:

~~~python
from collections.abc import Iterator

from ...core.flow import Ask, FlowContext
from ...core.service import Service, registry


@registry.register
class Example(Service):
    ID = "example"
    NAME = "Example TV"
    TAG = "EX"
    ALIASES = ("ex", "exampletv")
    TITLE_RE = r"(?:^|\.)example\.com"
    GEOFENCE = ("US",)
    MEDIA_TYPES = ("video",)

    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = False
    SUPPORTS_LIBRARY = False

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        ...

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        ...
~~~

The public signatures are fixed:

| Entry point | Purpose |
|---|---|
| open_url(ctx, target) | Resolve a provider URL or content identifier. |
| search(ctx, query) | Search titles; the query is already supplied by the shared screen. |
| live(ctx) | Browse live channels or an EPG. |
| library(ctx) | Browse the signed-in account library. |
| login(ctx) | Run an account, device-code or QR sign-in flow. |
| home(ctx) | Custom menu only when the inherited menu cannot express the service. |

The SUPPORTS_* flags must match the methods that are genuinely implemented. A
service with MEDIA_TYPES = ("audio",) receives the audio layout and naming path;
use ("audio", "video") for a mixed catalogue.

## 3. Keep API code and Flow code separate

api.py should contain requests, authentication headers, response validation and
provider-specific models. It must not import Textual, call print() or call
input(). Flow methods in __init__.py turn API results into Core asks:

~~~python
from ...core.flow import Choice

def search(self, ctx, query):
    results = self.api.search(query)
    choices = [
        Choice(item.label, item, detail=item.year or "")
        for item in results
    ]
    picked = yield ctx.pick("Search results", choices)
    yield from self.open_url(ctx, picked.id)
~~~

Use the shared asks rather than writing a menu loop:

~~~python
season = yield ctx.pick("Season", season_choices)
episodes = yield ctx.pick("Episodes", episode_choices, multi=True)
url = yield ctx.text("URL or ID", placeholder="https://…")
confirmed = yield ctx.confirm("Start download now?", default=True)
row = yield ctx.table("Live TV", columns, rows, values)
yield ctx.emit(playback)
~~~

Use ctx.status() for transient progress, ctx.log() for durable diagnostics,
ctx.warn() for recoverable problems and ctx.error() for a visible error. A
provider failure should raise a service-specific RuntimeError subclass with a
clear recovery hint. A programming error such as TypeError or AttributeError
must remain visible instead of being swallowed.

### Back, paging and batch results

The driver sends Back into the generator. Catch it only when the service owns an
intermediate level; otherwise let it bubble to the parent menu. Mark paging rows
with Choice(..., navigates=True) so they are not counted as titles in a
multi-select batch.

For QR, TV-code or browser authorization, yield ctx.wait_for() and keep the
polling operation in api.py as one check per call:

~~~python
challenge = self.api.begin_login()
answer = yield ctx.wait_for(
    "Scan to sign in",
    [("Code", challenge.code), ("Open", challenge.url)],
    poll=lambda: self.api.poll_login(challenge),
    qr=QrPresentation(payload=challenge.url, alt="Example login QR"),
)
~~~

Do not put QR image data, bearer URLs, cookies or tokens in log lines. Use the
dedicated qr field so the TUI renders it without recording it in scrollback.

## 4. Produce a Playback

A successful title flow ends by yielding a Playback. Core then performs the
native parse/select/download/decrypt/mux sequence:

~~~python
from ...core.playback import DrmInfo, Playback
from ...core.titles import Title, TitleKind

title = Title(
    id=item.id,
    kind=TitleKind.MOVIE,
    name=item.name,
    year=item.year,
    service=self.ID,
)
yield ctx.emit(Playback(
    title=title,
    save_name=self.save_name(title),
    manifest_url=item.manifest_url,
    headers=item.headers,
    proxy=self.ctx.proxy,
    is_live=False,
    drm=DrmInfo(
        system=None,
        license_url=item.license_url,
        headers=item.license_headers,
        context={"account": item.account_id},
    ),
))
~~~

Always call self.save_name(title). Do not call a nonexistent title.save_name()
method or construct a filename independently. Put service metadata in the Title,
Playback and track models rather than in display strings.

A clear title has no drm object. A protected catalogue item that is explicitly
clear may use DrmInfo(clear=True). An existing DrmInfo means encrypted media
unless it declares a service-owned HLS key/decryptor.

Supported playback fields include:

- manifest_url and explicitly authorized alternate_manifest_urls;
- inline_manifest or json_manifest for structured provider responses;
- headers, proxy, is_live, live_window and live_record_limit;
- chapters, lyrics, subtitle_references and mux_imports;
- keys for keys already obtained by a service-owned DRM path.

## 5. Integrate native manifest and track handling

The native downloader accepts DASH/MPD, HLS, ISM/Smooth Streaming, JSON manifest,
SABR and direct media sources. Services should pass the authorized source and
metadata; they should not parse representations or download segments themselves.

Keep these three decisions separate:

| Decision | Owner | Example |
|---|---|---|
| Provider source/profile | Service setting and API | manifest_profile, region or codec family used to request an MPD. |
| Final output tracks | Shared Core settings | Video quality/codec/range, audio and subtitles selected in the track picker. |
| Licence track/profile/seed | Service DRM setting | license_profile, license_tracks or pssh_video_profile. |

Shared Track output selection controls what is downloaded and muxed. It is not a
licence selector and must not be read by get_license, prepare_drm, or a
service-local DRM helper. If a provider needs a particular video track or media
playlist to obtain a licence, expose that choice as a separate service setting.

For multiple authorized ladders, return one Playback with merge_manifests=True and
implement manifest_variants():

~~~python
def manifest_variants(self, playback, log):
    return [
        replace(
            playback,
            manifest_url=self.api.manifest(profile),
            merge_manifests=False,
        )
        for profile in self.settings.get("manifest_profiles")
    ]
~~~

Every variant must already be authorized and carry its own headers/proxy. Core
parses and deduplicates the variants before showing one track picker. Never
concatenate raw stream objects in the service.

## 6. Integrate DRM safely

Declare only systems the service actually supports:

~~~python
DRM_SYSTEMS = ("widevine", "playready")
~~~

Core loads the selected local or remote CDM, creates the challenge, parses the
response and extracts content keys. The service owns the licence endpoint,
request body, headers, authentication and response transport. Keep those methods
in the service package (a local drm.py is recommended):

~~~python
def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
    response = self.api.post_license(
        drm.license_url,
        challenge=challenge,
        headers=drm.headers,
    )
    return response.license_bytes


def get_license_soap(self, challenge: str, drm: DrmInfo) -> str:
    return self.api.post_playready_license(
        drm.license_url,
        soap=challenge,
        headers=drm.headers,
    )
~~~

Never call a shared default HTTP licence implementation and never invoke a CDM
directly from service code. Keep service context in DrmInfo.context; do not
depend on a later settings change to route an active playback.

### PlayReady and multiple PSSH values

The normal PlayReady Core path reads the complete manifest, keeps PlayReady
protection data, deduplicates raw PSSH/WRM identities and performs one request per
remaining identity. It merges every returned content key. A service may override
this only when its verified provider behavior requires a selected media playlist
or a service-specific licence seed. HLS services whose master playlist omits PSSH
should use a separate license_tracks/pssh_* setting and fetch the matching media
playlist.

For live rotation, implement live_key_pssh() only when the service can map the new
KID to exact init data. Core tries the vault first, then this service hook and
transport, and finally an explicit masked KID:key prompt. Never invent a generic
KID-only request shape.

## 7. Settings, credentials, cookies and tokens

Declare provider-owned settings with Setting/Option; shared output settings are
appended by Core:

~~~python
from ...core.settings import Option, Setting

SETTINGS = [
    Setting(
        key="manifest_profile",
        label="Manifest profile",
        kind="choice",
        options=[Option("hd", "HD"), Option("uhd", "UHD")],
        default="hd",
    ),
    Setting(
        key="license_track",
        label="Licence track",
        kind="choice",
        options=[Option("720p", "720p seed"), Option("1080p", "1080p seed")],
        default="720p",
    ),
]
~~~

Keep API credentials in declared CredentialSlots and read them through the
service context:

~~~python
credential = self.ctx.credential("default")
username = credential.username
password = credential.password
~~~

Use self.ctx.tokens for service-owned refresh/session state. Use
USES_COOKIES = True when browser cookie profiles are a supported login path; Core
then scopes files to <paths.cookies>/<service>/. Never accept an arbitrary
cookie path from a setting.

If a service has multiple independent login methods, give each method its own
token/cookie file and route refresh/logout only for the selected method. Changing
a profile must not delete another profile's state. auth_status() must be cheap
and offline; it should inspect local state and never make a network call.

## 8. Helpers, chapters, audio and live

Declare every binary, module or asset through the helper contract. Resolve it with
self.ctx.helper("name"); never hardcode a developer path or scan the filesystem.
See external-helpers.md.

Optional provider chapters are converted to Core Chapter values in milliseconds
and attached to Playback.chapters. Honor the global chapter policy and treat a
chapter endpoint failure as a warning: return the normal playback and continue
to DRM/download.

For audio services set MEDIA_TYPES appropriately and populate title-level audio
metadata. Core handles the audio layout, selected audio tracks, cover art and MP3
ID3 export. See audio.md and downloader/mp3-audio-format.md.

For live services set SUPPORTS_LIVE = True, return Playback(is_live=True), and let
Core ask for recording, replay/DVR mode and duration after track selection.
00:00:00 is unlimited; Stop/Back/Esc cancels the active recording. Only add a
service-local session/heartbeat lifecycle when the provider actually requires it,
and release it in an outer finally around the emitted playback.

## 9. Testing checklist

Before calling a service ready, verify:

- import and registry validation succeed;
- inspect.signature() matches every base entry point;
- search, URL, live, library and login paths match SUPPORTS_* declarations;
- Back returns to the preceding question, and paging is marked navigates=True;
- login, refresh, expiry, logout and independent login profiles are covered;
- auth_status() performs no network request;
- at least one authorized title reaches a Playback;
- manifest/profile, output-track and licence-track settings remain independent;
- service-local licence transport works for each declared DRM system;
- multiple PSSH/KID and live-key rotation behavior follows the provider contract;
- chapters, audio metadata, helpers and session cleanup are failure-safe;
- tests pass with ruff, compileall and the project's offline test suite;
- live tests use authorized accounts and never print tokens, keys, PSSH, cookies,
  signed URLs or licence bodies.

Document the service's settings and any provider-specific DRM/profile behavior in
the same change. Keep the public service ID stable after release; use aliases or
a migration when a display name changes.
