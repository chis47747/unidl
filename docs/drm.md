# DRM systems

UniDL supports Widevine, PlayReady and MonaLisa through one typed DRM
boundary. A service declares the systems it can use and owns the complete
licence request. Core loads a CDM, creates a challenge, parses the response and
returns content keys; it never guesses a provider endpoint or sends a licence
request on a service's behalf.

## Systems

| | Widevine | PlayReady | MonaLisa |
|---|---|---|---|
| device | `.wvd` | `.prd` | `.mld` plus its referenced wasm |
| init data | PSSH box | WRM header/XML | service ticket |
| challenge | bytes | SOAP challenge | none |
| response | licence bytes | SOAP licence | local unwrap |
| implementation | `core/cdm.py` | `core/playready.py` | `core/monalisa.py` |
| remote CDM | optional | optional | no |

The selected device must match the requested system. A Widevine file cannot
answer a PlayReady request, and a PlayReady file cannot answer Widevine. The
picker validates the file extension and the device metadata; a filename such as
`l3` is only a label and does not change the reported security level.

## Service-owned licence transport

Every networked DRM exchange is implemented by the service package. The base
hooks fail closed, so a service cannot silently fall back to a shared POST:

```text
core: load device -> build challenge -> call service hook -> parse licence -> keys
service: choose endpoint, headers, authentication, body and response wrapper
```

The service hook may use its own cookies, token, proxy, device identity and
session context. Core may call it more than once when the service's verified
policy requires more than one init-data object, but each request still passes
through that same service-owned hook. Remote CDMs only replace the challenge
and parsing step; they do not move the licence URL or credentials into Core.

## Keep three selections separate

There are three independent choices:

1. **Manifest/source profile** — a service setting that selects the provider
   resource (for example a codec, colour range or API profile) before parsing.
2. **Licence/PSSH plan** — service-owned DRM policy that decides which init data
   is authorized. If configurable, expose it as a separate `license_*` or
   `pssh_*` setting.
3. **Final output tracks** — shared Track settings and the interactive picker,
   applied after the manifest has been parsed.

Changing output quality, codec, range or language must not rewrite a service's
endpoint, manifest profile or licence plan. Conversely, a service licence plan
must not alter the tracks the user finally downloads. This distinction is
especially important when a service offers several ladders with similar
resolution labels.

## License action

Choose a global default in **Settings → Services → License action**, or override
it in a service's **License and vaults** section.

| Action | Timing and scope |
| --- | --- |
| License before final track selection | Before the output picker. Widevine normally uses the default/first top-level PSSH; PlayReady collects and deduplicates the full encrypted inventory's headers, including missing init-segment data. |
| License after final track selection | After automatic/manual final selection. Both Widevine and PlayReady use only selected encrypted tracks, including their media playlists and initialization segments. |
| Custom license | Offered only when the service explicitly declares support. The service chooses the scope, init data and request organization; it can explicitly defer its plan until after selection. |

The global choice offers Before/After, not Custom. A service declaring Custom
keeps its own custom default until an explicit global or service policy is saved.
An explicit service policy takes priority. Press `r` on the service row to clear
it and restore the explicit global policy, or the service default if none exists.
Old `license_after_tracks=true/false` settings remain readable as After/Before,
including explicit false values and renamed service scopes. Saving/resetting the
new choice removes the obsolete override; opening Settings changes nothing.

After applies to DASH, HLS and ISM. Core maps DASH representations, reads exact
selected HLS media playlists, and scopes stale full-ladder init data before
querying vaults or opening a local/remote CDM. It never follows an unselected HLS
variant just because it was listed first. A selected shared multi-KID header is
sent unchanged: unrelated returned keys are retained, but do not expand required
vault coverage or cause extra requests. ISM may declare only one shared
`ProtectionHeader`; without per-track ownership information Core preserves that
shared scope rather than inventing a finer mapping.

A merged multi-period DASH track retains the exact adaptation/representation
identity for every period. After-selection Widevine and PlayReady extraction use
those period-local identities, so renumbering cannot omit a later selected part
or include a different rendition merely because it shares a KID.

Widevine After performs independent exchanges for distinct required PSSH values,
preserving original payloads. Vault-covered requests are skipped; a multi-key
response can satisfy later objects without another request. Missing selected
keys are reported as a licensing error. Clear/subtitle-only selections,
pre-supplied keys, and MonaLisa's local ticket path do not gain new CDM exchanges.

Custom hooks must describe their actual license coverage, not leave an unrelated
full ladder as the vault requirement. Core uses the custom init plan's KIDs when
available. Standard Before/After bypass an explicitly declared Custom planner and
custom resolver, while retaining the service's authenticated license transport.
Older undeclared hooks retain their compatibility path.

## PlayReady PSSH and KID handling

For a normal DASH or Smooth Streaming PlayReady service, the default contract
is manifest-wide key coverage:

1. Parse the complete authorized manifest and collect PlayReady
   `ContentProtection` entries.
2. Deduplicate raw PSSH objects in document order.
3. Parse them into WRM headers and collapse headers that declare the same
   canonical KID set. Compare KIDs as `UUID.hex`, never as their differently
   encoded source strings.
4. Send each remaining header through its own CDM session and the service's
   licence hook.
5. Merge returned keys by KID and verify that the encrypted inventory is
   covered. A header already covered by the vault is skipped.

The installed pyplayready API can return only the key associated with the
header used for a challenge. Therefore one request per distinct uncovered
header is intentional. A refusal that is clearly a throttle or policy error
stops further retries and reports the keys already collected. A failure for one
header does not hide successful keys from other headers; the exchange fails only
when the required inventory remains uncovered.

An HLS master may omit PSSH data and place it in media playlists. A service
with that shape must use its verified licence-track plan first, fetch only the
authorized playlists, deduplicate their init data and request those keys. Do
not walk every rendition merely because it is visible in the master.

Services whose original implementation deliberately uses a different DRM
model keep that implementation. The rule is about preserving a verified
contract, not forcing every service into the same request count.

## Widevine requests

Before normally sends one default Widevine challenge. After may send several
distinct selected PSSH objects, stopping when the required selected KIDs are
covered. Custom services own their request plan. Every license call still uses
the service's authenticated transport; a remote CDM does not replace it.

For live playback, a newly observed KID follows the service's declared init-data
path. Core reuses a cached key when possible and never invents a second
endpoint. If a genuinely new key needs user input, the live flow pauses at the
explicit replacement prompt rather than silently downloading with an unrelated
key.

## Identity, sessions and cleanup

Some providers bind a licence to a client identity, device type, account or
playback session. Services must select that identity before creating the
challenge. Swapping only the URL after a session is opened is not valid.

If a playback API opens a monitor, concurrency slot or heartbeat session, the
service owns that lifecycle: start immediately before the authorized playback
work and close it in an outer `finally` block after manifest, licence, command,
download, cancellation and error paths. Core never invents a heartbeat call.
Changing a service/API/profile setting is not an authentication invalidation;
it must preserve the other variant's cookies and tokens. Only explicit sign-out
or a provider response that definitively invalidates the selected session may
delete credentials.

## Key vault interaction

The vault is a cache keyed by normalized KID. Core may satisfy an exchange from
the selected vaults, then asks the service only for missing KIDs. Newly returned
keys are written with the service and title provenance. Vault reads and writes
never change the service's licence URL or bypass its transport.

Remote vaults are opt-in and are independent of remote CDMs. A remote vault
stores keys; a remote CDM performs CDM operations. Neither is a shared licence
transport.

## Diagnosing failures

Read errors in this order:

1. **Device mismatch** — the selected file extension/system is wrong, or the
   device security level cannot satisfy the service profile.
2. **Init-data mismatch** — the PSSH/WRM header is absent, malformed or from a
   different manifest/profile.
3. **Service refusal** — authentication, entitlement, region, identity or
   request shape was rejected by the service-owned endpoint.
4. **CDM/parser failure** — the challenge or returned licence could not be
   processed after the service response was accepted.

Keep logs redacted. Never commit device files, cookies, tokens, licence bodies,
signed URLs or KID:key pairs. A deterministic offline test should cover the
service hook, the selected profile, PSSH/KID deduplication and cleanup without
contacting a real provider.
