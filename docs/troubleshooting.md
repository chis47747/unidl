# Troubleshooting

Turn on `debug` first (`s` -> Interface & diagnostics -> Debug mode). It logs full URLs, the
license exchange, the resolved UniDL command, keeps temp files, and writes a
per-task log under `paths.logs`.

## Importing and registering a service package

The package must be copied into the installed `unidl/services` directory. Open
**Settings → Services → Register a service** and select it; registered packages
are dimmed, while available packages can be selected. Restart UniDL after the
confirmation. The restart is required because service code is imported at
startup and the persisted registration choice is applied when the new process
builds Home and global search. A single-file service module is accepted as well
as a package directory.

On a minimal distribution with no packages, Home shows the directory path and
the same Settings route. A CDM warning can appear at the same time; it is
independent, and only becomes relevant when a service requests DRM keys.

## Only native services appear on the main screen

Only services that are both loaded and user-registered appear on the main
screen. To inspect the loaded runtime registry, use `unidl services`; this is a
diagnostic list and may include packages that were loaded for the current
process. The TUI Home and global search always apply the persisted registration
and homepage-visibility choices.

## A service says "no credentials"

Open the service and read the Sign in row or identity band; Home has no account
column. If the detail names a slot such as `slot 'us'`, add it under
`credentials.<service>.<slot>` in `unidl.yaml`. Field names are whatever that
service reads — usually `username` and `password`, sometimes cookies or an API
key.

## Entering a service is refused with "needs Java runtime"

A declared helper is missing. Install it, or point at it explicitly:

```yaml
helpers:
  java: /opt/homebrew/opt/openjdk/bin/java
```

Required helpers are checked when you try to enter the service; the setup
notification contains its install hints. See
[external-helpers.md](external-helpers.md).

## "No CDM device configured" or a wrong-system device

Click the `cdm` chip on Home and choose a device for the active DRM system, or
add one under `cdm.devices` and set `cdm.default`. Verify configured local files:

```bash
unidl cdm --check
```

`missing` means the path is wrong. `unusable (...)` means the file could not be
loaded by the implementation for its extension. Widevine reports its level and
system id; PlayReady reports its level when the library exposes one; MonaLisa
checks the `.mld` and its referenced wasm module.

If the error says the selected file belongs to another system, choose a `.wvd`,
`.prd` or `.mld` matching Widevine, PlayReady or MonaLisa. A remote CDM is
selected by name in the same picker; it is not listed by the current diagnostic
command because that command checks configured local files.

## "License server returned 403" / no keys

In order of likelihood:

1. **The login expired.** Sign out from the service's own menu, or delete its
   file under `paths.tokens`, and let it sign in again. That file is the only
   place a session is read from, so there is no second copy to hunt for.
2. **Wrong CDM level.** 4K generally needs an L1 device. Set
   `cdm.by_service.<service>` to an L1 entry.
3. **Region.** The account or the IP does not have rights to that title. Check
   the service's own region setting and `proxies`.
4. **The service changed its API.** With `debug` on, the log shows the license
   URL and the response body preview.

## Keys come back but the file will not play

The keys were wrong rather than absent. Verify independently instead of trusting
the "Decrypted" message:

```bash
ffprobe -v error -show_entries stream=codec_name,codec_type,width,height \
  -of csv=p=0 "output.mkv"
ffmpeg -v error -i "output.mkv" -t 5 -f null -
```

The second command decoding without errors is the real proof. If it reports
corrupt frames, the key did not match the track.

## "best" quality picked something odd

Almost always a trick-play or thumbnail ladder winning on height — a real case
had a `1280x1440` thumbnail track beat genuine `1920x1080` video. The
`drop_video` track setting uses boundary-aware `trick`, `trickplay`, `thumbnail`
and `image` terms to prevent it without matching those letters inside a title
such as `Strickland`. If a service uses different wording, extend the regex.

## The track picker is empty or missing tracks

- HLS masters are parsed shallowly by default for speed. native delivery core's `--details`
  equivalent is not yet exposed as a setting; the manifest may need it.
- `drop_video` may be filtering too aggressively. Check the regex.
- Some CDNs need the manifest's query parameters copied onto child playlists.
  native delivery core does this automatically for hosts it recognises.

## Downloads stall near the end

CDN throttling under concurrency. Lower `workers` in the track settings. The
segment cache under `paths.temp` is reused, so restarting the same title and
track resumes rather than re-downloading. This includes completed HLS/DASH
parts, and direct-file prefixes when the CDN advertises working HTTP Range
support. Keep `resume_parts` enabled. UniDL also normalizes rotating signed URL
parameters and can find a prior cache directory when a refreshed manifest has a
different exact segment list; changing the title, track, output path, or service
identity intentionally does not reuse another task's parts.

After pressing `Ctrl+C`, the native runtime closes active sockets and downloader
children before the TUI exits. The stop is handled as a cancellation (not an
error traceback), while already completed parts remain on disk for the next
attempt. A currently open network request may lose only its in-flight chunk; the
next attempt resumes from the last safe temporary boundary.

## Esc does not quit

By design — it needs a second press within 1.5 seconds, so a stray keystroke
cannot end a session with work in flight. A notification says so after the first
press.

## Plain letter shortcuts do not work while typing

Also by design. A focused text input consumes printable keys, so `s`, `d` and
`b` type instead of triggering. Move focus out of the input, or use the click
targets in the top bar.

## Where things are

| | |
|---|---|
| config | `<source checkout>/unidl.yaml`, or the explicit `--config PATH` |
| settings | `<paths.home>/settings.json` |
| tokens | `<paths.tokens>/<service>/` |
| key vault | `paths.keys_db` |
| exported commands | `<paths.commands>/<service>/` |
| task logs | `paths.logs` |
| segment cache | `paths.temp` |
| finished media | `~/unidl_downloads/<service>/` |

## Reproducing outside the interface

Reproduce a failure with the same service and configuration in a clean terminal,
then run the project checks:

```bash
python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
```

## Filing something as a bug

Include: the service, `debug` log output, `unidl cdm --check`, and whether
`ffmpeg -f null -` decodes the output. "It did not work" without the decode
result cannot be distinguished from a wrong key.
