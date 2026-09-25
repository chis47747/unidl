# Writing a service

## Dolby Vision hybrid output

The global `dolby_vision_hybrid` setting is an output transformation, not a
service API/profile selector. A service may inherit it or declare the same
boolean in its policy settings to override it. When enabled for VOD, Core keeps
both a selected DV video and its matching HDR10/HDR10+ HEVC base in the licence
inventory; the native downloader combines them after decryption with `dovi_tool`.
The source may be one manifest containing both layers or several service-authorized
profiles merged by Core; multiple profiles are not a prerequisite.
Do not implement a second licence client for the transformation, and do not
enable it for live/replay ladders. If the two files do not have matching
frame rate, duration or frame count, the downloader must fail clearly rather
than produce an incorrectly aligned stream. The DV source is intentionally the
lowest-resolution available DV layer; it does not need to match the HDR base
resolution because only its RPU metadata is used.

Three things to read alongside this:

- `src/unidl/services/example/` — the reference service. Every hook, every
  declaration, and a comment on each saying why it exists and what breaks without
  it. It is not registered, so it never appears in the platform list. Copy the
  folder, rename it, and delete what you do not need. The service tests and
  registry validation keep it aligned with the public API.
- `src/unidl/services/bbciplayer/` — the public native service, including
  optional helper declarations and Widevine playback.
- `src/unidl/services/example/` — the intentionally incomplete scaffold; it is
  not registered and never appears on the platform list.

## Start from the template

    cp -r src/unidl/services/example src/unidl/services/mysvc

Then edit `ID`, `NAME`, `TAG`, and delete the hooks you do not implement. A
service shipped in the build may use the code-level `@registry.register`
decorator; a package imported by a user may omit `registry` and use **Settings →
Services → Register a service** instead. The loader discovers concrete
`Service` subclasses after the user restarts UniDL, so adding a manual import to
`src/unidl/services/__init__.py` is no longer required.

## The one hard rule

Service code does not import a UI framework, does not `print()`, does not
`input()`. It yields *asks* and receives answers. In exchange you get: your
service works in the interface and headlessly, it is unit-testable by feeding
scripted answers, and you never write a menu loop.

## Minimum viable service

```python
from collections.abc import Iterator

from ...core.flow import Ask, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import Service, registry
from ...core.titles import Title, TitleKind


@registry.register
class Example(Service):
    ID = "example"                       # stable, lowercase, used everywhere
    NAME = "Example TV"                  # shown in the interface
    ALIASES = ("ex", "exampletv")         # accepted by search and the palette
    TITLE_RE = r"example\.com"            # a pasted URL routes here
    GEOFENCE = ("US",)

    SUPPORTS_URL = True
    SUPPORTS_SEARCH = False
    SUPPORTS_LIVE = False
    SUPPORTS_LIBRARY = False

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        info = self.api.title(target)
        title = Title(id=info["id"], kind=TitleKind.MOVIE, name=info["name"],
                      year=info["year"], service=self.ID)
        yield ctx.emit(Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=info["manifest"],
            headers={"User-Agent": USER_AGENT},
            drm=DrmInfo(license_url=info["license"], headers={"authorization": info["token"]}),
        ))
```

The `Service` import is required because the class must inherit the UniDL
service contract. Importing `registry` is only required for the optional
code-level decorator; the user-level TUI registration path performs the same
runtime registration automatically after restart.

That is a complete service. You did not write a menu, a login prompt, a track
picker, a CDM call, a filename builder or a download command.

## File layout

One package per service:

```
services/example/
  __init__.py      the Service subclass and its flows
  api.py           HTTP client and response parsing, no UI, no printing
```

Keeping the API client separate matters: it is the part you will debug against
a live service, and it should be usable from a scratch script.

## Entry points

Override the ones you support and set the matching `SUPPORTS_*` flag. The base
`home()` assembles the top menu from those flags, so you normally do not write
`home()` at all.

| Method | When |
|--------|------|
| `open_url(ctx, target)` | a URL or content id was given |
| `search(ctx, query)` | free-text search |
| `live(ctx)` | live channels or EPG |
| `library(ctx)` | the account's own list |
| `home(ctx)` | only if the default menu genuinely does not fit |

## Asks

All built from `ctx`:

```python
choice   = yield ctx.pick("Season", choices)                    # single
choices_ = yield ctx.pick("Episodes", choices, multi=True)      # multi
text     = yield ctx.text("URL or id", placeholder="https://…")
yes      = yield ctx.confirm("Include extras?", default=False)
row      = yield ctx.table("Live TV", columns, rows, values)    # grids, EPG
token    = yield ctx.wait_for(                                  # something done elsewhere
               "Confirm sign-in",
               [("Code", "ABCD-1234"), ("Open", "https://…"), "and nothing to type here"],
               poll,
               qr=QrPresentation(payload="https://…"))          # optional QR in the TUI
yield ctx.emit(playback)                                        # a result
yield ctx.settings_request()                                    # open settings
result   = yield ctx.suspend(callable)                           # release the terminal
```

`Choice(label, value, detail="", tags=(), disabled=False, navigates=False)` —
`detail` renders as a dim second line, `tags` as dim inline markers.
`navigates=True` marks entries such as **Next page**: they move the list and are
excluded when the driver infers a batch total from a multi-select answer.

Multi-picks accept `preselected=[0, 3]` to arrive pre-checked. That is how the
track picker starts on the automatic selection.

`ctx.wait_for()` displays its lines and calls `poll` on the flow worker every
`interval` seconds until it returns something other than `None`. Timeout or Back
raises `Back`. Use it for anything confirmed on another screen; use
`ctx.suspend()` only when another process must temporarily own the terminal.

For a scan-to-sign-in challenge, pass a `QrPresentation` from
`unidl.core.qr`. Use `payload=` when the service returns the text/URL encoded by
the QR; the presenter generates a sharp bitmap locally. Use `image_data=` only
when the service returns an image as bytes or a base64 `data:image/...` URI.
`image_url=` may name an official, public HTTPS QR bitmap returned by the
provider. When both `image_url=` and `payload=` are present, the official bitmap
is rendered first and the payload is the local fallback. `image_data=` remains
the preferred option for protected images: fetch those through the service's own
API session before constructing the presentation so the TUI does not bypass
cookies, headers, or a service proxy. `fallback_url=` is the ordinary HTTP(S)
address opened when the terminal cannot display images. Older service adapters
may put a clearly named QR image endpoint in `fallback_url=`; the public QR
layer recognises those endpoints for compatibility.

```python
challenge = client.start_qr_login()
session = yield ctx.wait_for(
    "Scan to sign in",
    [("Open in a browser", challenge.url), "Confirm on your phone."],
    poll=lambda: client.poll_qr_login(challenge),
    qr=QrPresentation(
        payload=challenge.url,
        image_url=challenge.image_url,
        fallback_url=challenge.image_url,
        alt="Example TV login QR",
    ),
)
```

Never put a QR image or base64 data URI in `lines` or `ctx.log()`. Manual lines
are copied and recorded as text; the dedicated QR field is rendered in memory
and discarded when the wait ends. Fetch a protected image through the service's
own API session before constructing the presentation—the TUI must not bypass the
service's proxy, cookies or headers.

### Something the user has to do by hand

Not only device codes. Any step a person has to carry out somewhere else - a code
to type, a page to sign in on, a name to pick out of a list on a phone - is
written as `(label, value)` pairs, with plain strings for the prose around them:

```python
yield ctx.wait_for(                       # confirmed elsewhere, polled for
    "Activate with your TV provider",
    [("Open", url), ("Code", code), "The page signs you in."],
    poll,
)
answer = yield ctx.text(                  # finished in a browser, pasted back
    "Sign in, then paste the address back",
    placeholder="https://…?code=…",
    lines=[("Open and sign in", url), "Do not press Continue; the address matters."],
)
```

The pairs are what the UI leads with: a bordered card in the middle of the
screen, each value on its own row, bold, numbered so a keypress copies it,
clickable for the same reason, and `o` (or `^o` beside a field) opens the first
one that is a link. Labels are yours - they are what the copy hints are worded
from - so name them for what the person is about to do with them.

Do not put a code or a URL in a `ctx.log()` line or fold it into a title. It is
still written to the log for the scrollback, but the log is where you look for
what already happened, not for what the app is waiting on you to do.

**Your client must not do the waiting.** This is the one way the rule above gets
broken by accident: an `api.py` that takes a `show(code)` callback and then loops
inside itself until the code is used leaves the flow with nothing to yield, so the
only place the code *can* go is the log. Six services were written that way, and on
screen they all looked the same - a blank pane, a code buried in the scrollback and
a Back key that did nothing for ten minutes.

Split the client at the seam instead: one call for everything up to the code, one
that asks *once* whether it has been used.

```python
code = client.begin_provider_sign_in(provider)      # no waiting in here
session = yield ctx.wait_for(
    f"Sign in to {provider.label()}",
    [("Open", code.url), ("Code", code.code), "Nothing is typed here."],
    poll=lambda: client.finish_provider_sign_in(code, provider),   # one check
    timeout=max(60.0, float(code.seconds_left)),
)
```

The driver does the rest: it counts down in the status line, treats a failed poll as
"not yet" rather than as an error, and turns Back or the timeout into `Back`. Keep a
blocking form as well if a scratch script needs one - write it in terms of the two
halves rather than the other way round.

### Progress and messages

Do not yield for these; they are side channels:

```python
ctx.status("Loading season 3")   # the crumb line, transient
ctx.log("found 12 episodes")     # the log pane
ctx.warn("no subtitles")
ctx.error("playback denied")
```

### Back

`Back` is thrown into your generator when the user goes up a level. Catch it to
handle a level yourself, or let it propagate:

```python
while True:
    try:
        season = yield ctx.pick("Season", seasons)
    except Back:
        return                      # leave this sub-flow
    episodes = yield ctx.pick(f"Season {season}", ...)
```

You do not need to catch it at your top level — `Service.home()` already
catches `Back` from sub-flows and redisplays its menu.

## Playback and DRM

Return a `Playback`. Fill `drm` with what core needs; do not run the CDM
yourself.

If the service's playback API also exposes chapter markers, attach
`unidl.core.Chapter` values to `Playback.chapters`; this field is optional and
services without such an endpoint do not need a stub. Use milliseconds (or
`Chapter.from_seconds` only when the API explicitly reports seconds). The full
contract and delivery presentation are in [chapters.md](chapters.md).
Honor the per-service `fetch_chapters` policy (with legacy global fallback) through
`self.fetch_chapters_enabled()`: when it is false, skip the optional endpoint or
selection-set field entirely. Chapter parsing is best effort; catch provider
timeouts, schema changes and malformed optional entries, log a warning when the
flow has a context, and continue returning the normal `Playback` so DRM,
download and mux are unaffected.
Chapter embedding is already a shared **Tracks and output** setting named
`embed_chapters` (on by default); a service should not add a second mux toggle.

Optional posters, thumbnails and artwork use `Playback.attachments` with
`Attachment(url, name, kind, mime_type, filename, headers)` values. Check
`self.fetch_attachments_enabled()` before requesting them. UniDL previews them
beside Chapters/Lyrics and downloads them separately under a title-specific
`.attachments` directory; attachments are never muxed into media.

```python
DrmInfo(
    system=None,                                 # None: service/global selection decides
    pssh=None,                                   # None: core tries MPD, init segment, then KID fallback
    wrm_header=None,                             # PlayReady: core tries MPD and init segment
    init_data=None,                              # registry-specific, e.g. MonaLisa ticket
    service_certificate=None,                    # Widevine privacy-mode certificate bytes
    license_url="https://…/getlicense",
    headers={"authorization": f"Bearer {session}"},
    context={"session": session},                # yours, passed back to get_license
    cdm=None,                                    # override the device for this playback
    hls_key=None, hls_iv=None, hls_method=None,  # HLS AES-128 instead of Widevine
    clear=False,                                 # True: this title is not encrypted
)
```

Two of those fields are about intent rather than data, and both fail safe:

- Leave `system` as `None` unless the service or this playback requires a
  particular registered DRM system. `None` means "no opinion", and the
  service/global selection decides. Naming one pins it, and the setting no
  longer overrides it.
- A `DrmInfo` that exists means the stream is encrypted. If you attach one to a
  title that is *not* — a clear extra in a protected catalogue — say
  `clear=True`. Core will not guess from empty fields, because a service that
  builds its licence URL inside `get_license` legitimately has all of them empty,
  and guessing "no DRM" there downloads encrypted output while reporting that no
  key was needed.

Do not attach a `DrmInfo` at all for a title with no DRM of any kind.

When one authorization explicitly returns several equivalent manifest URLs,
put the first in `manifest_url` and the remaining URLs in
`alternate_manifest_urls`. Core tries only those declared candidates, in order,
and updates `manifest_url` to the one that loaded. `manifest_attempts` is an
opt-in retry count for a platform whose verified client retries transient
manifest failures; it is not a host- or schema-discovery fallback.

Declare the systems the service can actually use:

```python
DRM_SYSTEMS = ("widevine", "playready")  # service setting chooses between them
# DRM_SYSTEMS = ("monalisa",)             # one system is pinned, with no setting
```

An empty tuple means the app-wide DRM choice applies. One item is a fact about
the service and overrides the app-wide preference. Several items add a
service-scoped `drm_system` setting. The order is the service's preference.

There is no default licence POST. Every networked DRM service implements its own
transport, even when the endpoint accepts a raw challenge. Core owns CDM loading,
challenge creation, licence parsing and key extraction; the service owns the URL,
headers, authentication, request body and response unwrapping. Both base hooks
fail closed so an incomplete port cannot silently inherit another HTTP contract:

```python
def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
    response = self.http.post(
        drm.license_url,
        json={"payload": base64.b64encode(challenge).decode()},
        headers=drm.headers,
    )
    return base64.b64decode(response.json()["license"])
```

PlayReady services likewise implement `get_license_soap`. Keep both methods in
the service package (splitting them into a service-local `drm.py` is fine); never
call `super().get_license(...)` or `super().get_license_soap(...)`.

For Widevine privacy mode, fetch the service certificate before emitting the
playback and put its decoded bytes in `service_certificate`. Core applies it
before creating the challenge for both local and remote CDMs.

If the verified client chooses its licence seed independently of the tracks that
will be downloaded, override `prepare_drm(playback, tracks, log)`. It runs on the
full encrypted licence inventory before vault/CDM resolution and before shared
output selection. The hook may set `drm.pssh` or the registered system's
equivalent init data; it must not make a licence request or rewrite
`license_tracks` / `license_track_kids` with output preferences.
DIRECTV is the reference: its service setting defaults to the legacy 720p video
profile, the exact highest-bandwidth 1280x720 media playlist supplies one PSSH,
and the one Widevine licence response keeps every returned content key even when
the user chose another video/audio combination.

For encrypted live recording, override
`live_key_pssh(playback, stream, segment, kid, log)` only when the service can map
the newly observed KID to exact init data from the selected stream/segment.
Prefer init data already bound to the triggering segment; for a rolling HLS
playlist this also covers an older DVR-window segment after its key line has
fallen out of the current playlist. Core tries the vault first, then this hook and
the same service's own licence transport, then a password-masked TUI `KEY` /
`KID:KEY` prompt. Returning `None` declines automatic rotation. Never manufacture
a generic KID-only PSSH here: a service must prove that request shape itself. Core
retains and stores every content key returned by the licence but hands the
delivery backend only the pair matching the KID that paused the recording. A KID already present
in the recording key set is not an automatic rotation event; validation failures
for that KID go to explicit user entry.

### Multiple PSSH objects

The default PlayReady port must hand core the full DASH/ISM manifest, not extract
only its first PSSH in the service. Core filters PlayReady protection data,
deduplicates raw PSSH and equivalent WRM/KID identities, performs one independent
licence exchange per remaining identity and merges the keys. Most services happen
to produce one key; that observation is not permission to hard-code one PSSH.

Keep verified service-specific behaviour instead of mechanically applying that
default. If an HLS master omits per-track PSSH data, use the service's separately
configured/default **licence tracks** and their media playlists rather than
scanning every rendition. A port such as Apple, whose original PlayReady and
Widevine paths are both controlled by its own `license_tracks`, must preserve that
separation for both systems. No service licence helper reads shared output
settings; only Core's explicit post-selection compatibility mode may supply a
selected-only DRM inventory. See
[drm.md](drm.md#the-default-rule-and-when-not-to-use-it).

Clear streams: leave `drm` as `None` when the service never uses DRM. Inside a
protected catalogue use `DrmInfo(clear=True)`, so the absence of a licence is an
explicit fact rather than an incomplete encrypted playback.

## Settings

Declare whatever knobs your service actually has. There is no fixed set.

```python
from ...core.settings import Option, Setting, multi_choice_setting

# Use this only when every value maps to a real, service-owned API request.
LADDERS = multi_choice_setting(
    "ladders",
    "Requested ladders",
    [("hd", "1080p"), ("uhd", "2160p")],
    default=("hd",),
)

SETTINGS = [
    Setting(
        key="profile",
        label="Stream profile",
        kind="choice",                       # choice | bool | text | int | multi
        options=[Option("hd", "1080p H.264"), Option("uhd", "4K Dolby Vision")],
        default="hd",
        help="4K needs a TV login and an L1 device.",
        resets_session=True,                 # changing it signs the user out
    ),
    LADDERS,
]
```

These appear in a section named after your service. The shared track settings
(quality, codec, range, audio, subtitles) are appended automatically — do not
redeclare them. See [settings.md](settings.md) for which tier a knob belongs in.

Read them with `self.settings.get("profile")`.

There are three separate decisions. Do not collapse any two because their values
happen to look alike:

| Decision | Configuration owner | What it may change |
|----------|---------------------|--------------------|
| Provider source/manifest profile | service setting such as `manifest_*`, `source_*` or `profile_*` | API payload and returned manifest URL |
| Track output selection | shared `video_*`, audio, subtitle and `track_mode` settings | tracks handed to native delivery core for download/mux |
| Licence track/profile/seed | separate service setting such as `license_tracks`, `license_profile` or `pssh_video_profile` | init data and service-owned licence plan only |

The shared Track output selection settings are **never licence settings**.
`get_keys`, `get_license`, `get_license_soap`, `prepare_drm` and service-local DRM
helpers must not read `video_quality`, `video_codec`, `video_range`, audio or
subtitle output preferences to decide what to licence. If a service needs a
special licence track, profile or PSSH seed, declare a separate setting in that
service's own `SETTINGS`, even when it has the same choices and default as an
output setting.

By default Core supplies `license_tracks` as the complete encrypted parsed
inventory before shared output selection. A service may narrow that inventory
only through its own explicit `license_*` / `pssh_*` policy. Service code must
never read shared output settings to decide its API/profile or licence seed.

The per-service **License after final track selection** compatibility mode changes
the inventory Core passes to the same hooks: it is the final selected encrypted
tracks rather than the complete ladder. This exists for HLS/per-media-playlist
init data. The service does not branch on the global switch and does not inspect
checkbox settings itself; it simply receives the inventory Core selected for the
current mode.

Keep all three phases separate in both the setting names and the code. A
service-level `manifest_*`, `source_*` or `profile_*` setting may select the
provider's manifest profile before the MPD/playlist request. The shared
`video_quality`, `video_codec` and `video_range` settings must be passed to core's
track selection after that manifest has been parsed. They must not be reused to
silently select a different provider URL or a licence track. A service-specific
`license_*` / `pssh_*` setting may choose licence inputs but must not rewrite the
final `TrackSet.selected` handed to native delivery core.

### Several authorized manifests, one picker

A service may offer a multi-valued provider profile setting when its verified
client really requires one playback request per resolution/codec family. Declare
the value with `multi_choice_setting`, request the first profile normally, set
`Playback.merge_manifests=True`, and override:

```python
def manifest_variants(self, playback: Playback, log) -> list[Playback]:
    return [
        replace(playback, manifest_url=self.client.playback(profile),
                merge_manifests=False)
        for profile in self.remaining_profiles(playback)
    ]
```

Every returned object must already be authorized and must carry the exact
headers, proxy and service state needed for that URL. Core neither guesses profile
names nor rewrites URLs. It parses each variant independently, skips an unavailable
variant with a log message, then constructs one typed JSON manifest containing the
deduplicated streams. Deduplication uses the track properties exposed to the
picker (media kind, language/role, resolution, codec, frame rate, dynamic range,
channels, bitrate, container and KIDs), not signed URLs or provider representation
IDs. This matters when each profile request returns a different expiring URL.
Live playback must remain a single refreshable manifest, so
Core deliberately ignores merging for `Playback.is_live`.

The merged source is also what command/export paths receive; do not concatenate
`TrackSet` objects inside the service or return raw stream objects from this hook.
Disney is the reference implementation.

### Licensing a merged profile set

The merged ladder is only a display and delivery view; it does not give the
primary profile authority over tracks returned by another profile. Core keeps an
origin for every merged stream and groups the final licence inventory by that
origin. A service-owned `resolve_keys` implementation must therefore keep each
profile's licence context, endpoint and session separate, request every needed
profile/PSSH once, and return the union of the resulting keys. When
`license_after_tracks` is enabled, narrow that work to the origins and encrypted
tracks represented by the final selection; do not use the full profile list just
because it was requested earlier. Clear duplicate `playback.keys`/cached-key
state between independent exchanges so one successful profile cannot suppress
the next one.

For services that expose source choices, `manifest_resolution`,
`manifest_codec` and `manifest_color` choose the source family, while the shared
track settings choose the representations inside the selected MPD. Tests should
set deliberately different values for all applicable phases and assert the
source URL/profile, final output tracks and licence inputs independently, so a
future port cannot collapse them back into one knob. DIRECTV is the three-way
example: manifest authorization returns the ladder, shared output settings choose
what unidl downloads, and `pssh_video_profile` independently chooses the video
media playlist used as the Widevine licence seed.

## Credentials

Declare the logins you need; `unidl.yaml` fills them.

```python
from ...core.credentials import CredentialSlot

CREDENTIALS = [
    CredentialSlot("us", "Example US account"),
    CredentialSlot("intl", "Example international account"),
]
```

```python
cred = self.ctx.credential("us")
cred.username, cred.password, cred.cookies
```

Field names are yours: `unidl.yaml` passes through whatever keys you put under
the slot, so a service wanting `device_id` or `api_key` just reads
`cred.get("device_id")`.

### Partner authorization from another service

When one installed service obtains a short-lived SSO URL for another, use
core's `PartnerAuthorization` contract. The producer yields
`ctx.partner_handoff()`; the consumer declares
`PARTNER_AUTHORIZATION_SOURCES`, claims the URL once and saves only its own final
session. Services do not import one another, and the consumer never calls the
producer's authorization endpoint.

Do not treat this as a login fallback or a general browser-link wrapper. A
consumer that also accepts account credentials exposes an explicit
`login_method` setting with `resets_session=True`, and isolates the two token and
refresh domains. The complete producer and consumer templates, persistence
rules and tests are in
[partner-authorization.md](partner-authorization.md).

## Failing

Raise. Do not catch your own error just to return quietly, and do not print.

What happens next depends on the type, and the line is between *the situation is
wrong* and *this code is wrong*:

| You raise | The session |
|-----------|-------------|
| any `RuntimeError` - which every service error class in this tree is | shows a panel and returns to your menu; the rest of the session is untouched |
| anything else - `TypeError`, `AttributeError`, `KeyError` | ends, with a panel and a traceback in debug mode |

So an expired login, a licence the CDM was refused, an episode this region does not
carry: raise your own error and say what a person can do about it. The advice is
worth writing because the menu is where they end up, so "sign in again from this
service's menu" can actually be followed.

A `TypeError` is a bug, and it stays loud on purpose. Catching it behind a friendly
menu is how a bug becomes a mystery.

Two consequences worth knowing:

- **Derive your error from `RuntimeError`.** `class ExampleError(RuntimeError)` -
  the template does this and the service tests assert the rule holds.
- **If you write your own `home()` menu loop** instead of using the inherited one,
  you take on the recovery too, because a generator that raised cannot be resumed
  from outside. Put it beside the `except Back: continue` you already have:

  ```python
  except RuntimeError as exc:
      ctx.problem(
          f"{self.NAME} could not finish that",
          f"{type(exc).__name__}: {exc}",
          "The service menu is still open - the rest of the session is fine.",
      )
      continue
  ```

  `ctx.problem()` is the loud channel: a panel on screen as well as a log line.
  `ctx.warn()` and `ctx.log()` stay for the running commentary.

## Tokens

`self.ctx.tokens` is a `TokenStore` rooted at **this service's own folder**,
`paths.tokens/<your id>/`. Pass it a file name, never a path: a name with a `/` in
it makes a folder inside your folder, and there is nothing to disambiguate from
because no other service can reach in here.

```python
cached = self.ctx.tokens.read("example_token.json")
self.ctx.tokens.write("example_token.json", {"token": ..., "expires": ...})
```

There is no second place to look. If your service's file was called something else
in an earlier version, read the old name once, write the new one and **remove the
old one** - a copy leaves a session that signing out does not delete and the next
read adopts again, which reads to the user as a sign-out that did not work.

When one service exposes independent API families, the client selector is not an
authorization selector. Give each family its own file or managed cookie profile,
set the selector's `resets_session=False`, and route read, refresh and logout from
the selected family without inspecting or clearing the other family. Playback
must carry its family in `DrmInfo.context`; licence routing follows that recorded
context rather than whichever setting happens to be selected later. A service
with multiple independent client families should keep those profiles separate.

This rule also applies when the setting is labelled **platform**, **API version**,
**region**, **delivery** or **profile**. Selecting another route is not signing
out. Never call `logout()`, delete a token/cookie file, overwrite another
variant's cache, or mark another variant signed-out from a settings-change
callback. Keep each route's token/cookie state independently addressable and
refresh only the route selected for the current request. An explicit Sign out
action may clear the selected service session according to that service's
documented policy; it must not be used as a side effect of changing a selector.

Sign out must write an empty or tombstone state, rather than only deleting, when a
missing file would be read as "never signed in" and silently re-enter a flow the
user just left. Keep the selected route's state independently addressable.

## Cookies

For a lot of services the sign-in cannot be reproduced from a script at all — a
captcha, a device attestation, an SSO redirect — and an exported cookies.txt is
not a shortcut but the only route. Declare it and core supplies the rest:

```python
class Example(Service):
    USES_COOKIES = True
```

That alone gives you:

- the sign-in entry in the service menu, which verifies the managed cookie profile
- a **Browser cookie file** service setting populated only from direct `.txt`
  files in `~/.unidl/cookies/example/`
- an explicitly selected `example/<profile>.txt` used exactly, with no fallback
  to another account; an empty/automatic choice uses only `example/default.txt`
- **the jar attached to every session** `self.ctx.session()` builds
- `logout()` removing the selected file
- `self.cookie_status()` for `auth_status()`

Most services need no cookie code beyond the declaration, because cookies are not
a different code path — they are the same requests with an account behind them.

The sign-in flow does not ask for a path. Put the browser export directly under the
service's cookie folder and choose its profile in Settings. This is intentional:
an arbitrary path would bypass the per-service boundary.

```python
def auth_status(self) -> AuthStatus:
    status = self.cookie_status()          # logged_in + "17 cookie(s) for .example.com"
    if status.logged_in:
        return status
    return AuthStatus(logged_in=False, anonymous_ok=True, label="free tier needs no sign-in")
```

Three things worth knowing:

- **Expiry is ignored deliberately.** A browser export is usually already stale,
  and `requests` silently drops an expired cookie *at send time* — a jar that
  loaded fine would send nothing and the service would answer as if signed out. The
  in-memory jar is neutralised; the user's file is never rewritten.
- `self.ctx.session(cookies=False)` for the request that must be anonymous, like
  minting a guest token.
- `self.ctx.cookie_header(url)` builds a `Cookie:` header for a `Playback`, since
  the downloader takes headers and not a jar. It is narrowed to the URL's host, so
  an account's whole jar is not sent to a CDN.
- Cookie choices are profile names rather than paths. Do not add a service-owned
  free-text cookie path setting: it bypasses the per-service directory boundary
  and prevents core from showing the available accounts consistently.

A service with its own sign-in *and* cookies gets both offered in the menu.
They are not abstract alternatives: the real sign-in is better when it works, and
cookies are what you reach for when it does not.

## Reporting login state

`auth_status()` must be cheap and must not touch the network. It is used while
building the service menu and identity band, and may be called again after
login, logout or settings changes. Home deliberately does not call it for every
platform row.

```python
def auth_status(self) -> AuthStatus:
    cached = self.ctx.tokens.read("example_token.json")
    if cached and cached.get("token"):
        return AuthStatus(True, f"Example · {cached.get('username', 'signed in')}")
    if self.ctx.credential("us").complete:
        return AuthStatus(False, "credentials ready", detail="will sign in on demand")
    return AuthStatus(False, "no credentials", detail="slot 'us'")
```

`logout()` should drop whatever you cached in memory; core calls it when a
`resets_session=True` setting changes.

## Naming

`self.save_name(title)` produces the project convention:
`Title.S01E02.Episode.Name` / `Title.YEAR`. Override only if your service needs
something genuinely different, and declare `naming="self"`.

## Capabilities

Default is everything `core`. The only native runtime override currently wired
through this declaration is DRM:

```python
USES = Capabilities().with_self("drm")     # I fetch my own keys
```

With `drm="self"`, the engine calls
`get_keys(playback) -> ["kid:key", ...]` instead of the registered DRM system.
The `auth`, `catalog`, `tracks` and `naming` fields currently describe ownership
for diagnostics and for the platform list; they do not select alternate
hooks. Use `login()`/`auth_status()`, the flow entry points, `Playback`, and
`save_name()` directly regardless of those fields.

There is no `get_tracks()` hook that lets a service replace Core's parser. A
service may, however, implement `filter_tracks(playback, tracks, log)` after
the manifest has been parsed. Return an iterable containing only the existing
`StreamInfo` objects that should be exposed to automatic selection and the
interactive track picker, or return `None` to keep the complete ladder:

```python
def filter_tracks(self, playback, tracks, log):
    del playback, log
    return [
        stream
        for stream in tracks.streams
        if not (
            stream.media_type == "audio"
            and str(stream.role or "").casefold() == "audio description"
        )
    ]
```

This is an output filter only. Core keeps `tracks.streams` unchanged for DRM,
vault lookup and export provenance; the returned subset is available as
`tracks.selectable_streams`. Do not create replacement stream objects, perform
licensing, or remove entries from `tracks.streams`. Use `role`, `name`,
`language` and `extra` together when a provider's Audio Description metadata
is inconsistent. `name` is a display label, not a language field, so changing
it to `und` is not a reliable way to affect automatic selection.

A local `.json` manifest path may be supplied as `manifest_url`, because native
delivery core can parse that input. The `Playback.json_manifest` dictionary
field exists in the model but the current delivery controller skips it; do not
use it for a native service yet.

## Playback and concurrency sessions

Before porting any server-side playback, watch, concurrency, heartbeat or helper
session, trace the source script's call sites in both WV and PR variants. A response
field or unused method is not proof that the old flow used that lifecycle.

When it did, keep the transport service-local and put stop/release in an outer
`finally` around the synchronous `yield ctx.emit(playback)` (or around the whole
service-owned multi-PSSH key loop when that is where the old script opened it).
This is what covers manifest errors, licence errors, cancellation and Back/Quit.
See [playback-lifecycle.md](playback-lifecycle.md) for the current audit matrix,
the non-session lookalikes and the exact review checklist.

## External helpers

If you need java, node, adb, a certificate or a Python module loaded by path,
declare it. Do not hardcode a path and crash mid-flow. See
[external-helpers.md](external-helpers.md).

## Testing a service without the interface

```python
from unidl.core.flow import AutoPresenter, FlowContext, run_flow

ctx = FlowContext(settings=service.settings)
emitted = run_flow(
    service.open_url(ctx, "https://example.com/show/x"),
    AutoPresenter({"Season": 2}),          # match by ask title
)
assert emitted[0].save_name == "Show.S02E01.Pilot"
```

`AutoPresenter` picks the first enabled choice by default, honours
`preselected` for multi-picks, and takes overrides keyed by ask title. For an
`Await`, it calls `poll` once and raises `Back` if the result is still `None`, so
a headless walk never waits for the full device-code timeout.

## Checklist for a port

- [ ] `ID`, `NAME`, `ALIASES`, `TITLE_RE`, `GEOFENCE`, `MEDIA_TYPES`
      (`("video",)` by default; use `("audio",)` for audio-only services and
      `("audio", "video")` when the platform offers both). The home screen's
      country view uses the first `GEOFENCE` code as the primary market and puts
      an empty declaration under International; its type view keeps audio/video
      platforms in a separate combined group.
- [ ] `SUPPORTS_*` flags match the methods you implemented
- [ ] `DRM_SYSTEMS` names only systems the service really supports
- [ ] API client in `api.py`, no printing
- [ ] `auth_status()` is cheap and offline
- [ ] token file names match; external legacy state is explicitly validated/copied once when the login must carry over
- [ ] settings declared, not hardcoded constants
- [ ] credentials read from slots, never hardcoded
- [ ] `Playback` has `save_name` from `self.save_name(title)`
- [ ] `DrmInfo` filled; CDM not called directly
- [ ] old WV/PR playback-session call sites audited; no lifecycle inferred from names alone
- [ ] start/heartbeat/stop stays service-local and cleanup runs in an outer `finally`
- [ ] success, manifest/licence failure, cancellation and Back/Quit cleanup counts tested
- [ ] paging choices use `navigates=True`
- [ ] helpers declared if any external tool is used
- [ ] verified against the live service, not just imported
