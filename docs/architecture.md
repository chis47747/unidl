# Architecture

## Three layers

```
┌────────────────────────────────────────────────────────────┐
│  Shell        unidl/tui/       full-screen interface,      │
│                                navigation, settings, queue │
├────────────────────────────────────────────────────────────┤
│  Services     unidl/services/  login, catalogue, manifest  │
│                                discovery, license requests │
├────────────────────────────────────────────────────────────┤
│  Delivery     unidl/downloader/ parse, select, download,   │
│                                decrypt, subtitles, mux     │
└────────────────────────────────────────────────────────────┘
```

The boundary between services and delivery is intentional. The native delivery
core accepts URLs, manifests and raw keys, and contains no account login, no CDM,
no license requests and no key acquisition. Everything that touches an account
or a CDM lives in UniDL's service layer. Keep it that way.

## Where UniDL stops

Every service is a native UniDL service. There is one registration path:
`registry.register` accepts a `Service` subclass from `unidl.services`, validates
its declarations and exposes it to the platform list.

Three things cross it, and only three.

**1. The native delivery core.** Called through `core/delivery.py` and
`NativeDownloaderBackend`, and handed a manifest, a save directory and the keys
it needs. There is no second downloader package and no downloader subprocess.
What it is given is described in [the handoff object](#the-handoff-object).

**2. A helper a service declares.** A binary, a Python module or an asset that a
service names in its `HELPERS` list - `java`, `node`, `adb`, a signer jar, a
client certificate. Resolution is five steps long and every one of them is a
place someone *named*: the service key in `unidl.yaml`, the shared key, `PATH`
for binaries, `<paths.helpers>/<service>/`, then the `extra_paths` in the
declaration itself. There is no search step. A helper that none of those five
found is reported missing, with the hint that says how to install it, before you
browse rather than after you pick an episode. See
[external-helpers.md](external-helpers.md).

**3. The download folder**, `paths.downloads`, with one subdirectory per service.
Beside it sits `paths.subtitles`, split the same way, for services that fetch
subtitle files themselves rather than leaving them to the delivery core.

Everything else stays inside the project folder: logins in `tokens/`, content keys
in `db/`, CDM devices in `cdm/`, cookies in `cookies/<service>/`, exported commands in
`download_commands/`, re-fetchable scratch in `cache/`. No module names a path
into another tool's tree, no login is read from one, and unknown keys under
`paths:` in `unidl.yaml` are ignored rather than honoured - a path that quietly
did nothing would be worse than one that is simply not read.

## The rule that makes many services tractable

Service code never imports a UI framework, never calls `print()`, never calls
`input()`. It describes what it wants to ask; the shell decides how to render
it. See [writing-a-service.md](writing-a-service.md).

One display implementation belongs in the shell; a separate menu implementation
inside every service would make navigation and accessibility inconsistent.

## The Flow protocol

A service entry point is a generator. It yields *asks* and receives answers.

```python
def open_url(self, ctx, target):
    show = self.api.show(target)
    season = yield ctx.pick("Season", [Choice(f"Season {s}", s) for s in show.seasons])
    episodes = yield ctx.pick("Episodes", choices, multi=True)
    for episode in episodes:
        yield ctx.emit(self.get_playback(episode))
```

`core/flow.py` defines the ask types (`Pick`, `TextAsk`, `Confirm`, `TableAsk`,
`Await`, `SettingsAsk`, `Suspend`, `Emit`), the `FlowContext` that builds them, and
`run_flow()` which drives a flow against a *presenter*.

Two presenters exist:

- `tui/session.py:TextualPresenter` renders asks as widgets. The flow runs on a
  thread worker; each ask is marshalled to the event loop and the worker blocks
  on an `Event` until the user answers.
- `core/flow.py:AutoPresenter` answers from a rule table, so a flow can be
  walked without a terminal while a service is being written (see
  `writing-a-service.md`).

`Back` and `Quit` are *thrown into* the generator, which is why a flow can catch
`Back` to pop up one level and otherwise let it bubble.

`PartnerHandoff` is the one non-rendered routing ask. It carries an in-memory,
single-use `PartnerAuthorization` from a producer service to an explicitly
allowlisted consumer. The session driver builds the consumer through the
registry and core erases the URL on every outcome; neither service imports the
other. See [partner-authorization.md](partner-authorization.md) for the contract,
security rules and producer/consumer templates.

### Ask scope decides the screen

The UI is four screens, each answering one question, so an ask carries a scope
saying which one it belongs on rather than the UI inferring it from nesting
depth. See [ui-design.md](ui-design.md).

| Scope | Screen | Set by |
|-------|--------|--------|
| `root` | 2, the service's own menu | `Service.home()` |
| `flow` | 3, the service working | the default, so services need not think about it |
| `delivery` | 4, download or command | `SessionController` for its own two asks |

`tui/session.py:SessionController` owns the worker and does the routing: it
pushes screen 3 when the first flow-scoped ask arrives, pushes screen 4 for a
job, and pops both when the flow comes back to the service menu. Screens are
replaced rather than reused, which throws the old screen's state away on purpose;
whatever has to survive - the log, the failure flag - lives on the controller.

### Navigation levels

| Where | `^b` does |
|-------|-----------|
| Screen 1, platforms | clears the filter, otherwise nothing (it is the base screen) |
| Screens 2 to 4, ask pending | answers `Back`; the flow decides what that means |
| Screen 2, nothing pending | leaves the service, back to the platform list |
| Screen 3, nothing pending | declines: the service is mid-work |
| Screen 4, download running | stops the download; see [cancelling.md](cancelling.md) |
| Settings / search | closes, returns to the caller |

Implemented by `Service.home()` catching `Back` from sub-flows, and by screens
implementing `go_back() -> bool` (`True` = handled internally, `False` = pop).

### What a failure does to a flow

A flow is a generator, and a generator that raised is gone - it cannot be resumed,
only replaced. That single fact decides where a failure has to be handled: inside
the menu loop, or not at all.

`Service.home` catches `RuntimeError` around each menu branch, reports it through
`ctx.problem()` and redisplays the menu. Every service error class in the tree is a
`RuntimeError`, and so are core's `CdmError` and `HelperError`, so "the service said
no" reliably means "this step is over" rather than "this session is over". Anything
else - a `TypeError`, an `AttributeError` - unwinds to the session driver, which
shows the panel and stops: that is a bug, and it stays visible.

A service that writes its own menu loop has to do the same, for the same reason;
the native service contract keeps this behavior explicit.

## The handoff object

A service's only product is a `Playback` (`core/playback.py`):

```python
Playback(
    title=Title(...),
    save_name="Show.S01E02.Episode.Name",
    manifest_url="https://.../stream.mpd",   # or json_manifest=
    headers={"User-Agent": ...},
    is_live=False,
    drm=DrmInfo(license_url=..., headers=..., context=...),
)
```

The model accepts three typed source shapes:

- `manifest_url` — HLS, DASH, ISM, or a local `.json` track-manifest path
- `json_manifest` — a dict reserved for services that already hold per-track
  direct URLs
- inline HLS text — materialized in a private, atomic Core scratch file before
  parsing

All three forms enter the same typed `ParseRequest`; the TUI and the standalone
`unidl list/download` commands do not maintain separate parser paths.

## Native delivery boundary

`core/engine.py` builds a typed `ParseRequest`/`DeliveryPlan` and hands it to
`NativeDownloaderBackend`. It:

1. parses through the native backend's typed `parse()` method; when a service
   explicitly supplies several authorized `Playback` variants, parses each and
   builds one JSON-backed, deduplicated ladder
2. builds a DRM-only inventory from every parsed encrypted track
3. lets a service with a verified rendition-specific seed choose exact init data
   through `prepare_drm`, without making a licence request there
4. by default consults the key vault and requests missing keys before shared
   Track output selection; the opt-in compatibility mode reverses only these two
   steps and scopes KID/init-data discovery to the final selected tracks
5. applies shared Track output selection without allowing it to select a service
   API profile or service-owned licence profile
6. downloads via the backend's typed `run()` method, passing the already-parsed streams so
   there is no second network parse and selection indexes cannot drift
7. renders structured progress, messages, cancellation and final artefacts in
   the TUI without capturing process-global stdout

## DRM

`core/drm.py` registers Widevine, PlayReady and MonaLisa behind one exchange
shape. Widevine's local path illustrates the networked systems:

```
Device.load -> Cdm.from_device -> open -> get_license_challenge
            -> service.get_license(challenge)   <- the only part that varies
            -> parse_license -> get_keys -> filter SIGNING
```

Services that use the core networked path implement their own licence transport.
There is no default raw POST: URL, headers, authentication, request body and
response unwrapping remain inside that service package. The base Widevine and
PlayReady hooks fail closed. PlayReady changes the init data and SOAP exchange.
MonaLisa receives a ticket with playback data and unwraps it locally through a
`.mld` plus wasm module. Remote Widevine and PlayReady CDMs use the same registry
without a local device file. See [drm.md](drm.md).

Default key acquisition order:

```
parse full manifest -> service licence plan/init-data hook -> KIDs known
                    -> vault hit? use it, no license call
                    -> partial? license missing KIDs, merge, then store
                    -> miss? license, then store
                    -> shared output selection -> native delivery core
```

The global **License after final track selection** compatibility switch is the
deliberate exception for HLS-style inputs whose master playlist cannot expose the
selected media playlists' init data:

```
parse full/merged ladder -> shared output selection -> selected encrypted tracks
                         -> service licence plan/init-data hook -> vault/licence
                         -> native delivery core
```

This switch changes when Core supplies the DRM inventory. It does not authorize a
service to read `video_quality`, audio, subtitle or interactive checkbox settings
to choose a provider endpoint. Service-owned `license_*` / `pssh_*` settings remain
separate from shared output selection in both modes.

Parsing has to come first because the MPD or its initialization segment reveals
the KIDs and DRM init data. See [drm.md](drm.md) and
[key-vault.md](key-vault.md).

## Capabilities

`Capabilities(auth, catalog, drm, tracks, naming)` — each is `"core"` or
`"self"`. It records ownership for diagnostics and lets a service explicitly
retain an implementation for a capability. The native runtime dispatches a
service-owned DRM path when `drm="self"`; the other fields describe ownership
without creating alternate plugin APIs.

Native services should use the hooks documented in
[writing-a-service.md](writing-a-service.md). Every registered service is a
native `Service` subclass.

## Module map

```
src/unidl/
  __main__.py            launch + read-only diagnostics
  core/
    flow.py              the Flow protocol - the only service/UI interface
    service.py           Service base, Capabilities, registry
    devreload.py         explicit Debug-only native service reload
    playback.py          Playback, DrmInfo
    titles.py            Title; live channels are peers of episodes
    engine.py            Core orchestration and typed delivery plans
    drm.py               DRM registry and system dispatch
    cdm.py  pssh.py      Widevine device and init-data support
    playready.py         PlayReady device and SOAP support
    monalisa.py          MonaLisa ticket + local wasm support
    remotecdm.py         pywidevine/pyplayready HTTP CDMs
    vault.py             local SQLite key database
    vaults.py            ordered local/API vault collection
    settings.py          global / service / track tiers
    config.py            unidl.yaml
    secureio.py          locked, owner-only atomic state writes
    credentials.py       credential slots
    cookies.py           browser cookie import and storage
    cache.py             token store: one directory, read and written
    exports.py           portable resolved-title documents
    naming.py            output naming
    helpers.py           external binaries, modules, assets
    brands.py            service id -> brand name
    justwatch.py         availability and provider -> service mapping
    appearance.py        persisted theme choice
  tui/
    app.py               global actions, back-level rules
    chrome.py            the persistent top bar
    banner.py            block letters, two sizes
    home.py              screen 1: the platform list
    askhost.py           shared base for screens 2 to 4
    service_screen.py    screen 2: the service's own menu
    flow_screen.py       screen 3: the service working
    download_screen.py   screen 4: download, or the command
    session.py           SessionController: the worker, and ask routing
    asks.py              one widget per ask type
    settings_screen.py   the three-section settings panel
    search.py            services, titles and keys
    justwatch_screen.py  regional availability lookup
    cdm_screen.py        local and remote device picker
    palette.py           command palette
    theme.py             stylesheet
  services/
    bbciplayer/          a native service package
    <service>/           one package per service
```

## Verification

Nothing in this project is considered working because it looks correct. Local
and headless checks cover contracts and screen behaviour; live checks cover
network schemas, accounts, licences and real manifests. The complete grouping,
requirements and commands are in [testing.md](testing.md).
