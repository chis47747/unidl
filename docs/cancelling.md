# Pausing and stopping a download

[English](cancelling.md) | [简体中文](zh-CN/cancelling.md)

The delivery chrome **Pause** control holds the native transfer in place. The
download/record screen does not change, workers wait between segments, and the
same control becomes **Resume** so you can continue the same job. It is not a
cancel: verified parts stay in `tmp` and the job remains running.

`^b` is still the way to actually stop. The key bar says which of the two things
`^b` currently means, so you are not guessing:

| State | chrome | `^b` says |
|-------|--------|-----------|
| nothing running | Done / Resume a finished row | `back` |
| downloading | Pause | `stop everything` |
| paused | Resume | `stop everything` |
| stopping | Stopping | `stopping...` |

Pause waits at the next segment or live refresh. Back, the first Quit, and skip
still cancel. The old Stop control cancelled immediately; that is what Pause
replaced.

## Returning after work stops or finishes

Back returns to the immediate preceding page that launched the current step,
not directly to service home. Success and failure use the same parent: restore
the owning picker/input after stopping or dismissing the result. Close a popup
without also leaving its underlying page. Service home is the destination only
when it really is that preceding page.
See [navigation.md](navigation.md) for nested search/season/episode flows,
custom service delegation and the detailed navigation contract.

## Failed titles in a batch

A manifest, licence or selected-track preparation failure finishes that title
as failed and preserves its queue row for retry. The remaining titles continue;
a failed licence (including an empty Widevine/PlayReady key response) does not
leave a hidden track-selection question waiting for an answer. Clear media and
ordinary HLS AES-128 do not require content-key pairs.

After the final title, the queue leaves its busy state even when that title
failed. Back returns normally; it does not try to stop a nonexistent transfer.
Cancelling during preparation also dismisses a pending download/track question
and stops the unstarted remainder of the batch. Service and login prompts are
not dismissed by a download cancellation. An in-flight synchronous provider
request may still need its configured network timeout to return; no message
claims it is waiting for a media segment when no transfer is active.

## Inspecting batch titles

Click any title in the download queue to open its details, including completed,
running, failed and queued titles. The popup does not pause a transfer, change
track selection or request a manifest, licence or media segment. Use the compact
Previous/Next controls or Left/Right to inspect adjacent titles. Esc, Ctrl+B or
the close mark closes only the popup; it does not stop the download.

Completed rows retain the actual reported output paths, confirmed selected
tracks, per-track KIDs, available keys, metadata counts and timing for the latest
attempt. Total elapsed time includes preparation and manual selection; download
and processing time covers the native transfer/decryption/mux run, including
pauses. A failed or cancelled row retains the information collected so far.
Its explicit **Retry** button is enabled only when no job is active.
Clicking a row itself no longer starts a retry.

An ordinary unstarted title says **Queued**: UniDL does not pre-authorize it
just to populate the popup. When available, its label comes from the title
selection; otherwise a numbered placeholder is used. Native batch imports can
show the saved title, manifest references, exported track inventory, keys and
metadata before downloading. The exported inventory is labelled separately
from the current confirmed selection; saved information is not a promise that
an expiring URL or key is still valid. Pending rows never count as already
started tasks and do not enter the retry queue.

These details are lightweight, in-memory session history, not another export or
debug log. No parsed segment lists, licence payloads, cookies or account tokens
are retained for the popup. Content keys are deliberately visible: treat them
and signed manifest links as sensitive, and redact them before sharing a
screenshot. Starting a new batch clears the previous batch's history.

## Delay between batch titles

The global **Download behavior → Delay between titles** setting is a scheduler
delay for automatic multi-title VOD batches. It is off by default. Enter `30` for
a fixed 30-second wait, or `20-40` for a random wait in that range; `off` disables
it. The first title starts immediately, and the wait happens before the next
title begins service preparation, manifest work, or licensing. Tracks belonging
to one title and live recording segments are never delayed, and a retry does not
add a second title wait.

While the delay is shown, **Pause** freezes its countdown and **Resume** carries
it on. **Back**, **Escape**, or **Quit** cancels the remaining batch. The wait is
interruptible, so stopping does not leave a sleeping worker behind.

## How it works

The native delivery call is synchronous, so cancellation is carried by its
explicit `CancellationToken` rather than by hijacking terminal output. The
backend checks the token in transfer workers and live-refresh loops, cancels
pending work, and closes temporary state.

Cancellation also releases a paused transfer; it never requires **Resume**
first. Back or Quit cancels the remaining batch even while paused. The stop
signal is immediate, while socket/process cleanup runs outside the TUI thread
so navigation and worker callbacks stay responsive. Native HTTP socket reads
are interrupted before their buffered responses are closed. The download
owner waits for cleanup before it returns, so this does not leave a detached
downloader or resource-cleanup thread behind.

The public result is `DeliveryStatus.CANCELLED` with exit code 130. It is not
reported as a failed download because the user explicitly asked to stop.

`Ctrl+C` follows the same path as Back/Stop: UniDL aborts the active session,
closes the native downloader's sockets and child processes, then asks Textual to
leave its event loop. The terminal is restored and the interrupt is handled
quietly, so an intentional stop does not produce a Python `KeyboardInterrupt`
traceback. A second interrupt during cleanup is ignored rather than recursively
starting another shutdown.

Two things this depends on, both checked:

- **The UI owns rendering.** Structured `TrackProgressEvent` values are painted
  by the delivery screen; no progress thread writes directly to the terminal.
- **The standalone command remains orderly.** `unidl download` keeps its own
  terminal behavior, while an embedded TUI receives the same cancellation and
  status through the Core contract.

### The honest limitation

Cancellation is noticed at the next progress line, not instantly. In practice
that is a fraction of a second. A download stalled on a socket is released by
the embedded runtime closing its transport; an uncooperative worker is allowed a
bounded cleanup window before the process returns.

When `resume_parts` is enabled (the default), completed segment parts remain
under `paths.temp` after a cancellation. A direct, single-file URL is also kept
as a temporary prefix when its origin confirms HTTP Range support; the next run
continues from that byte offset. If the origin does not support Range, UniDL
discards the prefix and safely starts that file from zero rather than producing
duplicated media.

## Resuming completed tracks and processing stages

With `resume_parts` enabled, a VOD retry preserves more than unfinished
segments. Each track publishes a verified checkpoint after download,
decryption, repacking, audio processing, and subtitle preparation. A failed
stage cannot replace the previous valid checkpoint. For example, if audio is
ready but video fails halfway, a retry reuses the prepared audio and continues
the video's verified parts. If muxing fails, prepared tracks can be reused
without downloading or decrypting them again.

Checkpoints live under the task's `paths.temp` cache, separately from the
disposable per-invocation `.work` directory. They are not finished downloads.
Records are replaced atomically and checked against the track identity,
processing options, file size, modification time and bounded content samples.
An absent, changed or incompatible file is not trusted merely because its name
matches. Tasks sharing a cache are serialized through the final commit so one
invocation cannot clean another's active inputs.

The three controls have separate jobs:

- `resume_parts`: reuse verified segments and valid stage checkpoints after
  failure/cancellation. Off bypasses these checkpoints and starts fresh.
- `keep_temp`: retain caches after success for inspection/reuse; debug mode
  also enables retention. It is not required for failure recovery.
- `keep_failed_intermediates`: additionally retain the failed work directory,
  including incomplete outputs, for debugging. Off removes that debris but
  does not remove valid resume checkpoints.

Normal cleanup releases redundant parts once a durable track checkpoint
replaces them. Remaining checkpoints are removed only after the final output
has been committed successfully, subject to the existing success-retention
settings. Failed mux outputs are disposable; valid source tracks are not.
On filesystems supporting hard links, checkpoint media shares the existing
file data rather than duplicating large tracks. Other filesystems use a
bounded-memory copy. Live recordings keep their separate visible-file,
Back-to-finalize and Quit-preservation behavior.

## What it means for a batch

Cancelling one title cancels the rest. You asked to stop, not to skip one and
carry on with nine more.

Every remaining title still gets a queue row, marked `⊘ cancelled`, so the queue
accounts for the whole batch instead of just stopping mid-list.

## Cancelled is not failed

| | Colour | Header | Holds the screen |
|---|--------|--------|------------------|
| failed | red | `finished with errors` | yes |
| cancelled | muted | `stopped` | no |

A cancellation is an outcome you chose, so it is not coloured as a problem, not
counted among the failures, and does not block your way off the screen. A failure
is none of those things, so it does all three.

When a batch has both, the failure wins the headline: it is the part you did not
already know about.

## Checking it

Run the offline test suite and use a deliberately slow local fixture to verify
that cancellation stops promptly:

```bash
python -m pytest -q
```
