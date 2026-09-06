# DRM systems

Widevine, PlayReady and MonaLisa are registered behind one DRM interface. The
global setting is a preference; a service can declare the systems it supports,
pin its only system, or expose a service-specific choice when it supports more
than one. Switch the global preference by clicking the `drm` chip, or in
Settings.

Widevine and PlayReady can often use the same service, account, catalogue call
and manifest while changing the challenge and licence exchange. MonaLisa is a
different shape: iQiyi supplies a ticket with playback data and a local wasm
module unwraps it without a licence-server round trip.

## Widevine and PlayReady verification

Verified against Paramount+ on 2026-07-27, both systems returning the same
content key for the same title from live licence requests:

```
widevine   samsung_l3 (L3, .wvd)                    cd7e...9d7d:74dc...f418
playready  genius_fashion_gae_tv_smart_tv_sl3000    cd7e...9d7d:74dc...f418
           (SL3000, .prd)
```

The key belongs to the content, not to the DRM system, which is why they agree.

## What actually differs

| | Widevine | PlayReady | MonaLisa |
|---|----------|-----------|----------|
| device | `.wvd` | `.prd` | `.mld` plus referenced wasm |
| init data | PSSH box | WRM header (XML) | licence ticket |
| challenge | bytes | string | none |
| licence | raw-byte HTTP exchange | SOAP HTTP exchange | none; unwrap locally |
| implementation | `core/cdm.py` | `core/playready.py` | `core/monalisa.py` |
| remote CDM | yes | yes | no |

`core/drm.py` registers these differences so the engine does not branch on
system names. A service supplies the transport-specific licence call when one
exists; MonaLisa needs no transport at all.

There is deliberately no shared HTTP licence transport. Core may load the CDM,
create a challenge, parse the returned licence and extract keys, but each service
package owns the request URL, headers, authentication, body and response
unwrapping. `Service.get_license` and `Service.get_license_soap` only fail closed;
every service using a networked DRM system must override the corresponding hook.

## Where the line is: the CDM is shared, the licence request is not

**Every service that needs a licence makes its own request.** `Service.get_license`
and `Service.get_license_soap` have no shared implementation - they raise, naming the
service - so a new service cannot end up quietly relying on somebody else's. A
licence request is one of a service's own calls: its URL, its headers, its token, and
its own way of saying no. The one that used to live in core worked for the simple
cases and hid the interesting ones behind code that belonged to nobody.

What core does own is the **CDM**, and it never talks to a service's servers:

| | who does it |
|---|---|
| load the `.wvd` / `.prd` / `.mld`, build the challenge, parse the reply, dedupe PlayReady headers, cover every key id | `core/cdm.py`, `core/playready.py`, `core/drm.py` |
| find the init data in an MPD, an HLS playlist or an init segment | `core/engine.py` |
| **send the challenge and read the response** | the service, in its own api module |
| endpoints, tokens, catalogue, playback request | the service |

`core/drm.py` calls back into the service for every exchange
(`exchange.service.get_license(...)`), so the request always leaves from the
service's own session with the service's own credentials. Shared code takes
capabilities in - a flag on the registered system, a value in `drm.context` - and
never branches on a service name.

How different those requests are is the argument for the rule. Ten ports of TV
Everywhere services, ten gates, and no two of them alike:

| service | what the licence request carries |
|---|---|
| `tnt` | `x-isp-token`, to a URL whose company id is chosen by network *and* by live-vs-VOD |
| `discoverygo` | `PreAuthorization`, and the reply is sometimes JSON with the licence base64 inside it - though every stream a provider account is offered today comes back unencrypted, so this waits for a title that needs it |
| `usa`, `nesn` | `X-AxDRM-Message`, from the entitlement answer rather than from the session |
| `lifetime` | nothing - A+E serves HLS AES-128, so there is no licence server to ask |
| `abc`, `disneynow` | the device bearer *and* `x-playback-rights-authorization`, a value the playback answer handed over that ties this licence to that stream - to one of two endpoints, `/widevine/v1/obtain-license` for a recording and `/widevine/v1/channel/obtain-license` for a channel, with an empty `x-request-id` because that is what the player sends |
| `gothamsports` | nothing at all, and the URL is not in the playback answer either - the manifest's own `laurl` is the licence server, so the service fetches its manifest once to find it |
| `fox` | the account's bearer and *nothing else* - no api key, no location header, not even a content type; sending the playback headers here is answered with a 403. Only a final DASH manifest retains FOX's `drm.proxyServiceUrl` and receives Widevine `DrmInfo`; FOX HLS has no License or DRM object at all |
| `nbc` | a signature, and no account at all: `time` in epoch milliseconds and `hash`, an HMAC-SHA256 over `f"{time}widevine"` keyed with a secret out of NBC's own player configuration - in the query string, with the challenge as the whole body. The entitlement happened earlier and somewhere else, so a bearer here is not just unnecessary, it is not what the proxy checks |

One port has no gate at all, and it is worth naming for the opposite reason.
`optimum` is Nagra PRM from end to end: there is no Widevine, no PlayReady and no
licence server, so it declares `drm="self"` and no DRM system. Nagra's client
produces the key and only runs on an Android device or under an ARM emulator, both
declared as [external helpers](external-helpers.md) - and it often does not produce
a key at all, descrambling in place and serving the stream back decrypted instead.
A service can be protected and still have nothing for a CDM to do.

A shared default could not have guessed any of them, and the one that used to exist
would have posted a challenge to whichever URL it was handed and reported the refusal
as a CDM problem.

## Two things that are not obvious

**The client identity decides which DRM you get.** Paramount issues a licence
session for whichever system it thinks the client supports, and it reads that off
the device type in the URL. An `androidtv` session is Widevine-only, and asking
its PlayReady endpoint returns a SOAP 500 `Service Specific Error` - which reads
like a broken request rather than a wrong session. `xboxone` gets a PlayReady
session. So `_session_device_type()` follows the DRM setting; swapping the licence
URL alone is not enough.

This is likely to be true of other services too. When a PlayReady licence request
fails with a server error rather than a 403, suspect the session before the
challenge.

Amazon adds another identity binding: a PlayReady `.prd` and the Fire TV
Device Type ID (DTID) must describe the same provisioned device for UHD/4K.
Security level alone is insufficient. Amazon exposes separate editable
`web_device_type_id` and `firetv_device_type_id` service settings; the latter is
used unchanged for code registration, token refresh, startup, catalogue, PRS and
licence calls. Fire TV tokens are cached per DTID so changing identities cannot
silently reuse a bearer registered for another device. When a `.prd` directory
declares its DTID in a small README, the CDM picker shows it and Amazon rejects a
known mismatch before a 4K request. A matching pair is necessary but does not
override title, account, advertising or region entitlement—the returned ladder
may still be capped by Amazon policy.

Amazon playback resources also return a session handoff and ask the client to
track playback through PES. After PRS, unidl opens `/cdp/playback/pes/StartSession`
immediately before key resolution and always sends `StopSession` afterwards,
including when CDM or licence handling raises. All distinct PlayReady PSSH
exchanges share that one PES session. The Web form carries browser cookies and a
`userWatchSessionId`; the Fire TV form carries its bearer identity. Those request
paths remain separate, and Web live preserves the original rule of not opening a
browser PES session.

**A manifest can carry several PlayReady PSSH objects.** They are deduplicated in
document order and parsed into distinct WRM headers. Because pyplayready exposes
only the content key corresponding to the PSSH/WRM header used for that challenge,
`Service.resolve_keys` performs a separate licence exchange for every distinct
header and merges the returned keys by `kid:key`. It does this for both local and
remote PlayReady CDMs. A failed header does not prevent the remaining distinct
headers from being tried; the exchange fails only when none returns a key.

Two rules keep that from becoming a burst of requests, both in `core/drm.py`:

- `_playready_headers` collapses headers twice - identical text, then identical
  canonical KID set. A v4.1 header and its v4.0 downgrade, or one object repeated
  per rendition, is one licence request.
- `_collect_playready_keys` is driven by *coverage*, not by the length of the
  header list. It skips a header whose KIDs are already answered and stops as soon
  as every KID the download needs has a key, because one licence often returns
  several. A refusal that reads like throttling stops the loop and reports what was
  collected, rather than asking again with the same result. The Disney+ measurement
  on 2026-07-28: three PSSH objects, two distinct headers, two licence requests,
  three keys.

KIDs are compared as `UUID.hex` (`playready.canonical_kid`). The same KID is
written three ways along this path - base64 of a little-endian GUID in a v4.0
header, a dashed GUID in a v4.1 `KID VALUE`, and a UUID in the licence's keys - and
comparing any two of those as text makes every key look like it belongs to a KID
nobody asked for.

During each request `DrmInfo.wrm_header` points at the header currently in flight.
That matters to services such as Amazon whose licence body includes the header's
KID in addition to the SOAP challenge. The normal Widevine path remains a single-
PSSH, single-licence flow; a service whose original Widevine implementation is
track-scoped is an explicit exception, not a reason to change every WV service.

### The default rule, and when not to use it

Before applying either rule, keep three selections distinct:

- provider manifest/source selection belongs to explicit service settings and
  happens before the manifest is parsed;
- shared Track output selection belongs to native delivery core delivery and decides only
  which parsed tracks are downloaded/muxed;
- licence track/profile/PSSH selection belongs to verified DRM service logic and,
  when configurable, to a separate setting in that service's own section.

Shared `video_quality`, `video_codec`, `video_range`, audio, subtitle and
interactive track choices are not a service licence-control surface. By default
Core resolves keys from the complete encrypted inventory before the interactive
output picker is shown. A configurable service licence policy is a separate
`license_*` or `pssh_*` setting and must not alter the output selection in return.

The app-wide **License after final track selection** switch is an explicit Core
compatibility path for HLS/per-media-playlist init data. With it enabled, Core
first obtains the final output tracks and then supplies those encrypted track
objects to vault/init-data/licence resolution. Service code still must not read
shared output settings to choose an endpoint, provider profile or PSSH policy.

For a normal DASH or Smooth Streaming PlayReady service, this is the porting
contract even when the current test title happens to use only one key:

1. Fetch the complete manifest and keep only PlayReady `ContentProtection` data.
2. Deduplicate the PR PSSH objects in manifest order. After pyplayready parses
   them, also collapse WRM headers that declare the same canonical KID set; two
   differently wrapped objects producing the same challenge must not cost two
   licence requests.
3. Send each remaining WRM/PSSH through its own CDM session and licence request.
4. Merge the returned keys by KID and verify that they cover Core's encrypted
   licence inventory (the full ladder in the default mode).

Most services still finish with one request and one key. Paramount is the useful
counterexample: the US title **Scream 7** was measured on 2026-07-28 with six
distinct raw PR PSSH boxes, three distinct WRM/KID identities and three encrypted
track KIDs. Three PlayReady exchanges returned three keys and covered all three
track KIDs. This is now the Paramount multi-KID regression case.

There are two exceptions to the manifest-wide rule:

- Preserve a service's original verified flow when it deliberately uses a
  different DRM model, rather than reshaping it to resemble the DASH default
  above. Disney+ is the HLS form of the same rule rather than an exception to it:
  its key lines live in media playlists, so its verified licence-track plan decides
  which playlists are read, and the master is the fallback - matching the
  verified service flow, one representative playlist per format bucket.
- An HLS master may omit some or all per-track PSSH data and place it only in the
  media playlists. In that case do not walk the whole ladder and request every
  key. Resolve the service's configured/default licence tracks first, fetch those
  video/audio media playlists, deduplicate their PSSH data and request only the
  keys that licence plan needs. Do not source this service policy from shared Track
  output settings. The separate Core compatibility switch may instead scope the
  inventory to the user's already-final selected tracks when that behavior is
  intentionally requested.

Apple is the native example of the second shape, and its original code applies
the track-scoped rule to both PlayReady **and Widevine**. The `license_tracks`
service setting accepts `ALL` or combinations such as `UHD_HDR,AUDIO`; only one
representative media playlist for each selected tier is fetched. The PSSH stays
paired with that playlist's complete signed `keyInfo URI` (including its
watermarking token), each pair gets an independent CDM/licence exchange, and the
playback stop URL is called after the selected set completes. This deliberately
uses `drm='self'` for the outer loop while reusing core for every local or remote
WV/PR CDM exchange. A live check on 2026-07-28 licensed `AUDIO` and `UHD_HDR`
through PlayReady as two distinct PSSH requests and returned two keys. The tested
Widevine device files reached Apple's licence server but were refused with
`-1021` (device security/revocation), which is a device result rather than a
request-shape fallback to scanning another track.

YouTube is the byte-range form of the same selected-track exception. Its
adaptive JSON puts each representation's DRM PSSH in that representation's
`initRange`; there is no complete MPD whose entire PlayReady inventory should be
walked. Its separate `license_tracks` setting first declares whether VIDEO,
AUDIO or both are eligible. Within those classes, the verified protocol binds
the request to core's already-resolved output representation, so the service
fetches only those exact bounded init ranges without rereading shared quality,
codec, range or language settings. It keeps PlayReady PSSH identities distinct
by `(PSSH, HDR/SDR licence mode)`, deduplicates repeats and performs one
independent CDM/licence exchange per survivor. It never scans the unselected
adaptive ladder. Widevine retains the source script's different behavior:
prefer `streamingData.wv_pssh`, otherwise use the eligible resolved video init,
and use the YouTube SDR/HDR feature modes required by that player session.

### A configured licence seed and live key rotation

DIRECTV is neither a manifest-wide scan nor a selected-download-track scan. Its
verified client has a separate licence-profile choice: by default it opens the
highest-bandwidth exact 1280x720 video media playlist, takes the first paired
`skd://KID` / data-PSSH declaration and makes one Widevine request. That licence
may return video and audio keys beyond the seed KID. All non-signing content keys
are preserved and passed to native delivery core; the seed is never used to filter the CDM
answer or to pretend that the 720p rendition was selected for download. Other
profiles are explicit service settings, and an absent configured profile is an
error rather than a fallback to a lower tier.

Once recording has started, a newly observed KID follows one bounded path:

1. Ask enabled vaults for that exact KID.
2. Ask `service.live_key_pssh` for init data bound to the triggering segment and
   that exact KID; if the callback has none, refresh the selected stream's media
   playlist and match the KID there.
3. Run a fresh CDM exchange through that same service's `resolve_keys` /
   `get_license`; there is no shared licence HTTP transport.
4. Merge every key returned by the licence into the playback and vault, while
   returning only the requested KID:key to the paused native delivery core stream.
5. If any automatic step cannot answer, ask in the TUI for a masked `KEY` when
   the KID is known, or `KID:KEY` when it is not.

There is deliberately no common KID-only PSSH guess. An already-held KID that
fails fragment validation is not treated as a newly rotated KID and does not
silently spend another licence; it reaches the explicit replacement prompt.
unidl temporarily replaces native delivery core's standalone terminal prompt only for the
bounded embedded live call, keeps Textual's real stdin detached, and restores
both on exit. VOD never enters that adapter.

YouTube uses the project's installed latest `pyplayready` and the same
local/remote CDM selection as every other native service; there is no downgraded
runtime or helper environment. Current YouTube Movies SOAP replies do include an
outer XML-DSIG `Signature` that pyplayready 0.8.5 attempts to verify but rejects
because its XML canonicalisation does not match YouTube's. The YouTube transport
removes only that outer optional signature node before handing the unchanged XMR
licence to pyplayready. Challenge creation, licence decryption and content-key
parsing remain in the latest library, and signature verification for every other
service is untouched.

Init data is also read with pyplayready's own parser rather than by regex: a
manifest holds a base64 PlayReady object, not the header verbatim, and unpacking
that by hand is how you get a header the CDM will not accept.
`playready_pssh_from_mpd` filters on the PlayReady scheme id, so a manifest that
carries both systems does not hand a Widevine PSSH to a PlayReady CDM.

**HLS carries init data too, in the playlist.** An `EXT-X-KEY` or
`EXT-X-SESSION-KEY` line whose `KEYFORMAT` names a DRM system holds that system's
init data as base64 after `data:...;base64,` - a PSSH box for
`urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed`, a PlayReady object for
`com.microsoft.playready`. `pssh.from_hls`, `pssh.all_from_hls` and
`playready.playready_objects_from_hls` read those lines, `METHOD=NONE` and plain
`METHOD=AES-128` are skipped, and `engine.resolve_init_data` fetches the playlist
to run them. For per-key PlayReady, the media playlists belonging to the
configured/default selected encrypted tracks are read as well as the master, and
their key lines take precedence over it - the master is fetched once and extracted
last. The first couple of variants are a metadata-only fallback for when there is
no parsed selection to work from. Widevine never fans out over the selection: one
licence carries every key, so one init data is the whole title.

This is why one Disney+ playback request can serve both systems when its master
carries a key line per system: choosing PlayReady means reading the other line
rather than asking for a different stream. That does not override the HLS
track-scoped exception above.

**Init data is not always in the MPD.** Some valid DASH manifests, including
ITVX, declare encryption and their default KID in XML but put the actual
Widevine/PlayReady `pssh` only in each representation's initialization segment.
The shared resolver therefore uses this order:

1. Parse system-filtered init data embedded in the MPD.
2. Ask native delivery core's parsed encrypted tracks for their init URL/range, read at most
   64 KiB, scan validated ISO-BMFF boxes, and keep only the selected DRM system.
   Widevine stops after the first usable init segment; PlayReady inspects every
   distinct encrypted representation init segment and deduplicates its PSSH
   objects before making licence requests.
3. For Widevine only, construct a standard KID-only PSSH if the MPD/init had no
   PSSH but exposed trustworthy non-zero KIDs.

The init read is bounded even if a CDN ignores `Range`, so resolving DRM metadata
cannot accidentally turn into a media download. Both Widevine and PlayReady use
this public path; services should not grow their own init-segment parsers.

## pyplayready version

`>=0.8.5`. Earlier releases pinned a `cryptography` that conflicts with
pywidevine, so the two could not be installed together. 0.8.5 is also the first
project baseline used for signed PlayReady challenge `CustomData`, and is what
this was built and tested against.

One API change between the old line and the new one is absorbed in
`core/playready.py`: `PSSH.get_wrm_headers(downgrade_to_v4=False)` became a
`wrm_headers` attribute holding `WRMHeader` objects. Both shapes are accepted,
because which version is installed is not something this code gets to decide.

## Devices

```
<paths.cdm>/widevine/*.wvd
<paths.cdm>/playready/*.prd
<paths.cdm>/monalisa/*.mld
```

One folder per system, so a file in the wrong place is obvious. Switching system
takes the device with it, and picking a file switches to the system identified
by its extension. A MonaLisa `.mld` is JSON that points to a wasm module; keep
both together. MonaLisa also requires `pymonalisa`, installed separately:

```bash
python -m pip install pymonalisa
```

See [configuration.md](configuration.md).

Not every device works with every title. An SL2000 PlayReady device and an L3
Widevine device are both limited in what the licence server will release to them,
and a refusal at that level is a licence response, not a bug here.

Two refusals worth telling apart, both seen from Disney+:

- `security-level.insufficient: capability-hd` - the *ladder* is too big for the
  device. Ask for a smaller one. Disney+ exposes this as the service's
  `stream_ladder` setting, whose default reads the CDM's level and asks for the
  720p H.264 ladder when it is not L1 or SL3000.
- `drm-serial-number-has-been-individually-revoked`, or
  `device-certificate-revoked` - that *file* is finished for that service. No
  request shape helps; another device is the only answer.
- an error with **nothing in it** - Vudu answers ``status=error`` with no
  description for a device that may not have the quality that was asked for. Its
  ``stream_quality`` setting exists for that, and its default reads the level too.

`Service.cdm_level()` is where that reading lives, in core rather than in a
service: more than one service has to know the level *before* asking for a stream,
because both Disney+ and Vudu hand over a manifest for a ladder they will then
refuse to licence, and neither refusal names the device or the ladder.

## MonaLisa

A service pins MonaLisa with `DRM_SYSTEMS = ("monalisa",)`. It puts the ticket
in `DrmInfo.init_data`; core loads the selected `.mld`, resolves its wasm path
relative to that file, unwraps the ticket and returns ordinary `KID:key` pairs.
Those pairs then use the same vault and command-export path as every other DRM
system.

There is no remote MonaLisa form because there is no licence exchange to move
to a server. A missing wasm file is reported as a device configuration error
before the module is asked to parse the ticket.

## A challenge before there is content

Most services describe a title first and ask for a licence second. Netflix does the
opposite: it will not describe a title at all until a CDM has answered, so the
**manifest request itself carries a licence challenge** — generated before any key
id is known, over a fixed and meaningless one.

That is a separate capability from an exchange, and it is declared as one:
`DrmSystem.make_challenge`. Widevine and PlayReady have it; MonaLisa does not,
because it makes no licence request to challenge with.

It exists as its own field rather than being faked by aborting an exchange. Both
were tried, and aborting does not work: PlayReady's exchange loops over every WRM
header in a manifest and reports "none were accepted", so an exception raised to
stop it early is caught and reported as a failure of the thing that actually
succeeded.

The same CDM — local or remote — produces the challenge and the later content
licence, by construction. A device Netflix will not accept therefore fails at the
first request rather than after a manifest that looked fine.

The app and TV identities are the exception: their manifest requests carry **no**
challenge. They ask for the manifest, then open a DRM session. Which would mean
they need no CDM to browse — except that the app's *session* is opened through the
CDM, so it needs one even earlier. See below.

## Three ways to open a Netflix session

Netflix's key exchange depends on what the client can prove, and each identity can
prove something different. All three are in `services/netflix/msl.py`.

| identity | exchange | what authenticates it |
| --- | --- | --- |
| browser, both systems | `ASYMMETRIC_WRAPPED` | nothing — the session keys come back wrapped to an RSA key generated on the spot, and the *user* auth carries the account |
| Android app, Android TV Widevine | `WIDEVINE` | the CDM. Netflix runs a licence exchange as the key exchange: the reply is a licence whose content keys **are** the MSL session keys, named by `encryptionkeyid` and `hmackeyid` |
| Android TV PlayReady | `AUTHENTICATED_DH` + `MGK` entity auth | the device's pre-shared KPE/KPH pair. Header and payload are encrypted and signed with the device's own keys before any session exists — this is the only identity that uses a pair |

Two consequences worth stating, both found by running it:

* the app needs a readable `.wvd` **to browse**, not only to fetch a key, and the
  ESN has to name that device's Widevine system id — `NFANDROID1-PRV-P-SAMSUSM-F711N-<system_id>-…`.
  Netflix compares the two and refuses the pair with a message about the account.
* the PlayReady TV's session keys are derived from the DH shared secret with a
  *wrapping* key mixed in, and that key is not stored anywhere: it is derived from
  the KPE/KPH pair (`trunc128(HMAC-SHA256(HMAC-SHA256(salt, KPE‖KPH), info))`). So
  a pair is enough to open a session, and nothing less is.

Because no two identities share a session, none of them share a cached one either:
`tokens/netflix_msl_session.json` holds **one entry per identity**, keyed by the
identity and — where the ESN is built from the CDM — by that device too. The same
goes for `tokens/netflix_esn.json`. Before that, using the browser identity threw
the phone's session and its user token away, so every switch cost a fresh handshake
and, for the identities that sign in with a password, a fresh sign-in. Netflix
rate-limits those.

A TV's *session* is not its *sign-in*. Two tempting shortcuts are refused, both
measured rather than assumed:

* an account through MSL — refused with `Email or password is incorrect`, using
  credentials that sign the phone identity in seconds earlier.
* a browser's cookies — refused with `User authentication data does not match
  entity identity`. Cookies are bound to the entity that minted them, and that
  entity is the browser.

The native service therefore drives the TV's own CLCS screens, either with the
configured account or with the eight-digit code confirmed at `netflix.com/tv2`.
It preserves the server-encrypted `serverState` and `serverScreenUpdate`, waits for
`CURRENT_MEMBER`, and then uses the minted device cookies only as transport
authority for `getProfilesNoMsl`. The selected profile id is opaque, not assumed
to be a UUID. An ensureProfile-style ping with that real id mints the
`USER_ID_TOKEN` bound to the current MasterToken. PlayReady TV sessions include an
`AUTHENTICATED_DH/WRAP` renewal in that step and adopt the replacement session
keys before decrypting the response payload.

## A title with two keys

Some titles split their renditions across two content keys — Netflix marks the
streams `SEGMENT_MAP_2KEY` — one for the low segment map and one from 720p up. The
licence is issued against the `playbackContextId` in the manifest's licence URL,
*not* against the key id in the challenge, and the context of a manifest that
reaches the top level covers only the low key.

Nothing reports this. The exchange succeeds and answers with a key for a key id
nobody asked for, and it surfaces much later as a file that will not play. The fix
is Netflix's own client's, and measured: ask for a second manifest with the top
level capped and take *its* licence URL, which covers both keys. One extra request,
only for titles marked this way — `NetflixApi.ensure_license_context`.

## Remote CDMs

A device can live on a server instead of on disk. Configured under `remote_cdm`
in `unidl.yaml` (see [configuration.md](configuration.md)) and selected by name
exactly like a file — remote CDMs appear in the `^o` picker alongside the files,
marked `remote · host`.

The protocol is pywidevine's `serve` API, which pyplayready's server copies:

```
GET  {host}/{device}/open                     -> session id, and the device it opened
POST {host}/{device}/set_service_certificate  -> Widevine privacy mode, optional
POST {host}/{device}/get_license_challenge    -> init data in, challenge out
POST {host}/{device}/parse_license            -> the licence response in
POST {host}/{device}/get_keys                 -> the content keys out
GET  {host}/{device}/close/{session}
```

Three details are not optional:

- **The challenge path differs by system.** PlayReady servers answer
  `get_license_challenge`; Widevine servers answer
  `get_license_challenge/STREAMING`. Both are tried, in the order that is right
  for the system, and a `not found` for the other spelling is normal.
- **The licence body differs by system.** PlayReady exchanges the SOAP XML as
  text; Widevine exchanges bytes as base64. Sending base64 to a PlayReady server
  produces a parse error from deep inside its CDM.
- **The licence request is still made locally.** Only the challenge and the
  parsing happen remotely, so the service's headers, cookies and proxy still apply
  and the licence server never sees the remote CDM's address.

`DrmSystem.remote_keys` is what declares that a system has a remote form.
Widevine and PlayReady do; MonaLisa does not, because it decrypts locally against
a wasm module and never makes a licence request at all — asking for a remote one
says so rather than failing halfway through an exchange.

A remote CDM needs no CDM library installed locally, which is usually the point.
It answers for one system only: selecting a PlayReady one for a Widevine playback
is refused with a message naming both.

## Checking it

The DRM contract is covered by offline tests and can be exercised with local
manifest/device fixtures:

```bash
python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
```

Live licence checks require an authorized account, a matching device and a
redacted test plan. Never place licence responses or content keys in fixtures.
