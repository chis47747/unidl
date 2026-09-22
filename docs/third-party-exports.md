# Third-party exports

UniDL has two deliberately separate export paths:

* **UniDL native exports** (`kind: unidl-export`) are read by UniDL's versioned
  parser.  When the source service is installed, the native import may retain
  that service's delivery hooks.
* **Third-party exports** are adapted into Core's neutral playback model and are
  always delivered by the generic downloader. The adapter recognises the shared
  `kind: mediaexport` v1 format, current Unshackle v2 files, and the legacy track
  and series schemas described below. It is intentionally named
  `third_party_exports.py` so other downloader formats can be added without
  weakening the native reader.

## Security and ownership boundary

A third-party file is data, not a service session.  Its service label is retained
for naming and display only. Even if it contains a provider/service identifier,
importing it does **not**:

* instantiate or query that service's code;
* read that service's cookies, token, account or helper state; or
* call that service's licence endpoint or ask UniDL to re-license the title.

Exported `KID:key` pairs are passed directly to the native delivery core.  A
protected track with no exported key fails clearly; it does not fall through to a
service licence request.  Clear media, playlist AES-128 and other non-license
states remain non-license paths.

The export's `region` is informational metadata.  It is shown in the import note
and is never silently converted into a proxy.  Choose a proxy explicitly in
UniDL's settings when a signed manifest still requires the source region.

## Manifest handling

The settled shared `mediaexport` fields are imported without service code:
title metadata, the primary and extra manifests (identified by their complete
URLs), non-sensitive manifest headers, DRM/PSSH hints, KID:key pairs, chapters,
and URL-bearing side-load tracks. `Cookie` and `Authorization` headers are
discarded. A shared export whose service tag happens to match an installed
service still stays in the generic import context.

HLS AES URI-key records and frozen `segments[]` delivery are intentionally not
enabled yet. Their IV, initialization-section and per-segment semantics are still
being finalized in the shared format. UniDL does not silently treat those records
as a normal service manifest or request a new licence; such a title must be
re-exported in a currently supported form.

The adapter keeps the source that was already resolved by the exporter. In
addition to current Unshackle v2 files, it accepts the older exports found in
the wild:

* the flat ``title -> track label -> track`` map used by older movie and
  multi-manifest exports; and
* the ``series -> seasons -> episodes -> tracks`` catalog shape. Each episode
  is imported as its own title, with season/episode numbers and the exported
  media URLs preserved.

The series catalog must contain at least one URL in an episode's track tree.
Metadata-only catalogs are rejected as non-downloadable instead of being
silently turned into an empty job. Legacy track labels are only a fallback for
missing fields: explicit ``type``, codec, language and URL values always win.
In particular, a legacy row that says ``descriptor: DASH`` but points to an
``.mp4`` file is treated as a direct URL, not sent to the DASH parser.

* HLS uses each exported track's media-playlist URL in a typed JSON manifest.  The
  track picker uses the exporter-provided metadata and defers reading those child
  playlists until a track is selected, so an export with dozens of subtitle
  playlists opens quickly.  Download hydration then reads only the selected
  playlist(s).  This optimization is limited to the private third-party marker;
  normal service JSON manifests keep their eager parsing behavior.
* DASH and ISM use the exported manifest URL.  If the export contains additional
  already-authorized manifest URLs (for example, a separate codec/profile MPD),
  UniDL parses and merges those URLs through the generic, inert import context.
  No service API or licence call is involved in that merge.
* Direct URL tracks (including side-loaded subtitles) are represented in the same
  typed JSON source and are not mistaken for a service manifest.

Manifest URLs and request headers can contain short-lived authorization.  Treat a
third-party export like a password: keep it private, avoid committing it, and
re-export when the signed source has expired.

## Keys and DRM

Unshackle's per-track `keys` maps are normalised to lower-case, 32-character KIDs
and 128-bit hexadecimal content keys.  Duplicate KIDs with different values are
rejected instead of choosing one silently.  Widevine, PlayReady and ClearKeyCENC
DRM labels are preserved as Core hints; the hint does not authorize a new
exchange.  PlayReady PSSH/WRM data is retained when present so the generic
downloader can identify the encrypted representation while using the exported
keys.

## What import does not change

Import still uses UniDL's normal output-track selection and downloader settings.
An export containing a large ladder can therefore be downloaded at a different
quality, provided the corresponding KID:key was included.  Track selection is
separate from the exporter's API profile and from licence-track selection.
