# Key vault

An ordered set of content-key stores. With no `key_vaults` configuration this
is one local SQLite database at `paths.keys_db`; it can also include more SQLite
files and HTTP API vaults. **Settings → DRM & vaults** manages the definitions and
enabled state; its **Vault policy** panel chooses exactly which enabled backends
participate in lookup, search and writes.

## Why it is worth having

Keys are reused far more than you would expect. A single title usually issues
**one KID for every encrypted track** — all video renditions and all audio
streams. A real measurement from Paramount: 8 encrypted tracks, 1 KID. So the
first key fetched covers the whole title, and a repeat download needs no license
exchange at all.

This is also true within one run, which is why the vault pays for itself before
you have downloaded anything twice.

## Design, and what was borrowed

Borrowed from unshackle (`unshackle/core/vault.py`,
`unshackle/vaults/SQLite.py`, `docs/guide/vaults.md`):

- **Match by KID, never by PSSH.** A PSSH box can differ between requests for
  the very same content — some services even return a CENC header rather than a
  PSSH box. A KID only changes when the media itself changes.
- KID and key stored as **32 lowercase hex characters, no dashes**, compared
  `COLLATE NOCASE`.
- An **all-zero key is not a key**: lookups skip it, writes reject it, so a
  placeholder can never mask a real key.
- `UNIQUE(kid, key)` so re-adding is a no-op rather than a duplicate.
- SQLite in WAL mode, `synchronous=NORMAL`, 30 second busy timeout, one
  connection per thread — the UI thread and the download worker share the file.

Deliberately **not** borrowed: unshackle gives every service its own table named
after the service tag. unidl uses one table with a `service` column instead:

- global "which title did this KID come from" is one indexed query, rather than
  enumerating `sqlite_master` and querying every table — a limitation unshackle
  documents for its own `kv search`
- provenance is per row, not per table: title, source, cdm, timestamp
- no table name is ever interpolated into SQL (unshackle's source carries three
  `TODO: SQL injection risk` comments about exactly that)

## Schema

```sql
CREATE TABLE keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kid        TEXT NOT NULL COLLATE NOCASE,
    key        TEXT NOT NULL COLLATE NOCASE,
    service    TEXT NOT NULL COLLATE NOCASE,
    title      TEXT,
    pssh       TEXT,
    source     TEXT NOT NULL DEFAULT 'license',
    cdm        TEXT,
    origin     TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(kid, key)
);
CREATE INDEX keys_kid_idx     ON keys(kid);
CREATE INDEX keys_service_idx ON keys(service);
CREATE INDEX keys_title_idx   ON keys(title);
```

| Column | Meaning |
|--------|---------|
| `kid`, `key` | 32 lowercase hex each |
| `service` | the service id, or `unknown` for a deliberately unscoped manual row |
| `title` | the save name, so a key can be traced to a title |
| `pssh` | when known; informational only, never used for matching |
| `source` | `license` (fetched from the service), `vault` (remote backfill), or `manual` |
| `cdm` | which configured device obtained it |
| `origin` | the explicit writer, such as `tui` for manual entry |

## Where it sits in the pipeline

```
parse manifest  ->  KIDs known
                ->  vault lookup
                    all KIDs found?  ->  use them, no license request
                    any missing?     ->  license unresolved KIDs, merge, then store
                ->  shared output track selection
```

Parsing must come first: the manifest is what reveals the KIDs. The order in
`tui/session.py:_process` is `load_tracks`, `resolve_keys`, then output track
selection for this reason. The vault and licence inventory always cover the full
parsed encrypted ladder; changing the later download selection cannot change a
lookup or licence request.

The lookup returns a complete set only when it can skip the licence exchange. A
partial hit is retained on the playback while the service's own licence path
obtains the missing KIDs; core then merges both sets by KID. This matters for
PlayReady, where one PSSH/WRM header can return only its own key. PlayReady
headers already covered by the vault are skipped, but a header carrying another
KID is still sent to that service's licence endpoint. Init-data discovery runs
before the final all-cached decision when a manifest can hide additional KIDs
(for example Disney+ HLS media playlists or a Widevine v1 PSSH).

Lookup prefers a same-service match, then falls back to any service. KIDs are
globally unique in practice, so a key imported under one service name still
serves another.

## Inspecting

From the interface, press `/` and type a KID, a `kid:key` pair, or part of a
title. Key results show the service, title, source and date; `ctrl+y` copies the
pair.

**Search vaults** is a separate home-search multi-select setting. Its
default selects the local SQLite vaults only, so local matches remain immediate
and offline. An empty selection turns key search off. Remote backends appear in
this setting only when their YAML entry explicitly declares `searchable: true`
and the adapter has enough information to search safely.

For an exact KID, **Search remote vault** is the first result whenever at least
one selected remote backend supports it. Nothing remote happens while typing.
Selecting that row queries the sole selected remote directly; with several, a
single-choice picker asks which backend to contact. A service-aware HTTP vault
such as StreamFab then offers **All supported platforms** first, followed by its
declared platforms. Choosing one platform sends only that platform's `GetKey`
request for each candidate KID; **All** intentionally walks the finite
`supported_services` list and stops at the first hit. The result is displayed and
can be copied, but is not silently written into a local database.

### Adding one or more keys

On that same search screen, click **Add keys** or press `ctrl+n`. It is also in
the command palette as **Add keys to vault**. When opened from inside a service,
that service is already selected; from the global screen, type a platform name or
service id and choose a live filtered result. The typed query stays visible. Edit
or delete the selected id to clear it and immediately choose another service;
only the first eight matches are mounted, rather than a 150-service dropdown.

The editor takes one `KID:key` per line. It also accepts copied `--key KID:key`
arguments, several direct pairs in a JSON or command line, dashed UUID-shaped
KIDs, upper-case hex and a full-width colon. Blank lines are ignored. Every other
line must parse, every half must normalize to 32 hex characters, an all-zero key
is refused, identical pairs are de-duplicated, and two different keys for the
same KID in one paste are an error. The entire batch is checked before anything
is written.

Before saving, **Choose vaults** opens a multi-select list of every writable local
and remote backend. Its initial selection comes from **Vaults receiving acquired
keys**; remote destinations are initially omitted while **Use remote key vaults**
is off. This screen is an explicit one-operation override, so deliberately adding
a remote destination here authorizes this batch to leave the machine without
changing the global automatic-playback setting.

Each selected SQLite file writes the whole batch in one transaction. A pair
already present is reported as `already stored`. A KID already mapped to another
key is a conflict: the first press writes nothing and names the database that has
the conflict; only the explicit **Replace conflicts and add** press removes those
conflicting rows and commits the new batch in that database. Transactions are
atomic per database, not across unrelated files or HTTP servers. Destinations are
independent: one failure does not roll back successful destinations, and the
result names every target without printing full key pairs. `no_push: true`
backends are shown as read-only and cannot be selected as write destinations.

From the command line:

```bash
unidl keys                                          # summary by service
unidl keys 96a9ef1af1094578974d4c09ef8fcecc         # one KID
unidl keys 96a9ef... --service paramountplus
```

## API

```python
from unidl.core.vault import KeyVault

vault = KeyVault(config.paths.keys_db)

vault.add("paramountplus", kid, key, title="Show.S01E01", source="license")
vault.add_pairs("paramountplus", ["kid:key", ...], title="Show.S01E01")
preview = vault.preview_pairs(["kid:key", ...])
result = vault.add_many(
    "paramountplus", ["kid:key", ...], title="Show.S01E01", source="manual"
)

vault.get_key(kid, "paramountplus")        # str | None
vault.get_keys([kid, ...], "paramountplus")  # dict, only what was found
vault.find("Big.Brother")              # by KID, kid:key, or title text
vault.by_service("paramountplus")
vault.stats()
```

Helpers: `normalize_hex()` accepts dashed, uppercase and quoted forms;
`split_pair()` parses `kid:key`; `is_null_key()` identifies the all-zero key.

## Turning reuse off

Reusing a stored key is most of the value here, but it has to be possible to switch
off — otherwise there is no way to make a run actually talk to the licence server.

The unified **Vault policy** panel contains the two safety switches:

- **Use the local key vault** — on by default
- **Use remote key vaults** — off by default

Three target pickers in the same panel narrow those switches and control search:

- **Vaults used for key lookup** — all configured backends by default
- **Vaults receiving acquired keys** — all writable backends by default
- **Search vaults** — local backends by default; remote
  search is always an explicit click

The first controls automatic playback lookup. The second controls licence writes,
live-key rotation writes and which local databases receive a remote lookup
backfill. The third is independent: it controls which local databases appear in
home search and which capability-declared remote backends may be queried there.
Choosing no targets is valid in every list.

Two rather than one because they are two different decisions. Reading a local file
is free and private. Reaching a remote vault is a network call that also sends your
keys to somebody else's server.

With both target lists left at their `all` default:

| | Local read | Local write | Remote read | Remote write |
|---|---|---|---|---|
| local on, remote off *(default)* | yes | yes | no | no |
| local on, remote on | first | yes | only the KIDs local missed | yes |
| local off, remote on | no | yes | yes | yes |
| both off | no | yes | no | no |

Two things in that table are deliberate and worth stating:

**A selected local write still happens.** Switching local reuse off means you do not
trust what is stored — a suspect key, a service that re-encrypted something under
the same key IDs, a CDM or licence server that is what you are testing. It does not
mean you want to stop recording what a licence request just returned. Remove a
database from **Vaults receiving acquired keys** when it should not be written.

**The remote switch governs writes too.** That is the asymmetry: not reading a
local file is a preference, but pushing keys to a server is an action, and a switch
that is off should not be taking it.

### With both on

The local vault is asked **first**, always, whatever order `key_vaults` lists — a
local hit is free and offline, and a config file should not be able to make every
lookup a round trip. Only the key IDs the local store did not have reach the
network.

A key that comes back from a remote vault is then **copied into the selected
writable local target(s)**, so
the second download of the same title needs no network at all. The copy only ever
goes inward: a lookup never sends anything out as a side effect.

`Engine.vault_targets(settings)` combines the two safety switches with both name
lists at the single automatic-playback decision point. No settings scope at all —
a check script, a caller with no scope — means local only and all local names,
which is the safe reading of "no opinion": nothing leaves the machine because
nobody asked for it to. The log names the backend that answered:

```
vault: all 1 keys served from local, no license request
vault: both vault switches are off, going to the licence server
vault local: cached 1 key(s) from the remote vault
```

## More than one vault

`paths.keys_db` is the default and, with no configuration, the only one.
`key_vaults` in `unidl.yaml` replaces that default with an ordered list:

```yaml
key_vaults:
  - type: sqlite
    name: local
    enabled: true
  - type: sqlite
    name: archive
    path: db/archive.db
  - type: api
    name: shared
    uri: https://keys.example.com
    token: your-token
    no_push: true
```

Every entry needs a case-insensitively unique `name`, because that name is the
stable identifier stored by the two target settings. Every additional SQLite
entry should have its own `path`; a relative path is resolved beside
`unidl.yaml`, and its parent directory is created privately. The TUI can scan
the project `db/` folder (and the configured home `db/` folder when different)
for SQLite files with a UniDL `keys` table. It shows those files as
**discovered** candidates; a user must explicitly import the selected one(s)
before they become configured vaults, so a backup or unrelated database never
joins licence lookups or writes by accident.

Three backends, because three is what the shapes are:

| Type | Where | Interface |
|------|-------|-----------|
| `sqlite`, `local` | a file on disk | the `KeyVault` above |
| `HTTP`, `httpapi` | an HTTP service | one URL, the operation in the body: `{"method": "GetKey"\|"InsertKey", "params": {...}, "token": ...}`. `api_mode: query` puts the same fields in a GET query string and needs a `username` too |
| `api` | an HTTP service | `GET`/`POST {uri}/{service}/{kid}`, `Authorization: Bearer` |

`HTTP` and `api` are both HTTP and are still separate backends, because they are
separate protocols — one names the operation in the body against a single URL, the
other puts the service and key id in the path.

The StreamFab vault is an `HTTP` backend. Its JSON calls are the same shape as
the existing adapter:

```json
{
  "method": "GetKey",
  "params": {"kid": "<kid>", "service": "<service>", "title": null, "session_id": null},
  "token": "<vault-token>"
}
```

Writes use `InsertKey` and add `key` to `params`; `title` and `session_id` remain
explicit fields. The token belongs in the secret-bearing project `unidl.yaml`,
never in a service module, test fixture, log or command export. The configured
entry only makes the backend available; the global **Use remote key vaults**
switch controls whether reads and writes reach it. If a backend declares
`supported_services`, unidl maps only the configured `service_map` aliases and
skips unsupported local service IDs without sending them to the server.

The configured StreamFab endpoint currently accepts these remote service tags:
`abema`, `amazon`, `amazonmusic`, `applemusic`, `appletv`, `ard`, `canal`,
`channel4`, `crackle`, `crunchyroll`, `cw`, `danime`, `dazn`, `discovery`,
`disney`, `dmm`, `espn`, `familyclub`, `fandango`, `fanza`, `fod`, `foxtel`,
`gyao`, `hulu`, `itv`, `joyn`, `lemino`, `linemusic`, `m6`, `max`, `mgstage`,
`netflix`, `nhk`, `now`, `onlyfans`, `paramountplus`, `paravi`, `peacock`,
`plex`, `pluto`, `rakuten`, `roku`, `rtl`, `shahid`, `skyshowtime`, `sokmil`,
`spotify`, `stan`, `starzon`, `telasa`, `tidal`, `tubi`, `udemy`, `unext`,
`viki`, `vix`, `waipu`, `wowow`, `wowtv` and `youtubemovie`. The project maps
local IDs to those tags only for this backend: legacy `apple` → `appletv`,
`canalplus` → `canal`, `discoveryca`/`discoverygo`/`discoveryplus` → `discovery`,
`nowtv` → `wowtv`, legacy `paramount` → `paramountplus`, `rtlplus` → `rtl`,
`starz`/`starzplay` → `starzon`, and `vudu` → `fandango`. Other local services
are skipped by this backend rather than sent with an unsupported tag.

Its project entry also declares `searchable: true`. That does not make typing a
KID issue network requests; it only makes StreamFab selectable under **Search
vaults** and enables the explicit result-row action.

- reads take the first key offered, local before remote
- writes go to every selected writable vault with `no_push` unset
- a vault that errors or cannot be reached is **logged and skipped**. A key store
  being down means "no cached key", never a failed download

`Engine` keeps both: `engine.vaults` is the collection used for lookups and
writes, while `engine.vault` remains the primary SQLite compatibility handle.
The key screen aggregates the selected local members of the collection for KID,
title and provenance search. HTTP vaults are never asked for fuzzy title or
provenance search; a capability-declared backend may only receive the exact KID
after the explicit remote-search action.

An existing unshackle vault of either kind works without a server-side change; the
request and response shapes are the same, including the `api` backend's numeric
`code` table and the `HTTP` backend's in-body `status_code`.

Three details of the `HTTP` backend come from a live server rather than from the
reference implementation, and each would otherwise be a silent misread:

- `status_code` lives **in the body**, so an HTTP 200 can still carry a refusal —
  the body is what decides
- a rejected password is an HTTP **401** with a plain message; an unknown method is
  a **400** carrying `error`/`type` instead of `message`, so both field names are
  read when reporting why
- `InsertKey` answering `{"status": "cached"}` without `inserted` means the vault
  already held that key, which counts as success: the point was for it to have it
