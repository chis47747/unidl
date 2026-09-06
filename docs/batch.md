# Batches

Picking ten episodes is one action with ten outcomes. The delivery screen can
describe one title in its header; it cannot describe ten, and a serial run that
says nothing until it is over is a run you cannot supervise.

So screen 4 grows a queue.

```
finished with errors  ·  1 of 2 failed  ·  ^b to go back

████████████  2 / 2 done  ·  1 failed

    1 ✓ Batch.Show.S01E01
    2 ✗ Batch.Show.S01E02  native delivery exit 1
```

## How the total is known

Only the flow knows how many playbacks are coming. The driver sees them one at a
time, through `on_emit`, so without help it can count what has finished but not
what it is counting towards.

Two ways it finds out, in order of preference:

**Inferred, and free.** The driver sees the answer to every ask. A multi-select
that returned six things is six emits about to happen, so
`tui/session.py:TextualPresenter.present` reads the count off the answer and
opens a queue. No service has to opt in.

Choices that navigate rather than emit a playback, such as **Next page**, must
set `Choice(..., navigates=True)`. The driver removes them before inferring the
total, so a paging row cannot leave the queue waiting for a title that will
never arrive.

The seam is deliberately narrow. Keep paging choices distinct from choices that
emit a playback:

| Ask | Inferred as a batch? |
|-----|----------------------|
| single-select | no |
| multi-select answering with one item | no |
| track picker (`scope="delivery"`) | no |
| multi-select answering with several | yes |

**Declared, for what cannot be inferred.** `ctx.batch(n)` states it outright.
Needed when the count does not come from a selection at all - a service
harvests however many command files a script happened to write, and there was no
multi-select involved. It overrides the inferred value.

`ctx.batch()` is not fire-and-forget like `ctx.log()` and `ctx.status()`. It can
raise `Back`, so treat it as a suspension point:

```python
ctx.batch(len(content_ids))   # may raise Back
for content_id in content_ids:
    yield ctx.emit(self._playback(content_id))
```

## Job states

| State | Glyph | Means |
|-------|-------|-------|
| running | `▸` | in flight |
| done | `✓` | downloaded, or the command was saved in command-only mode |
| failed | `✗` | native delivery core returned non-zero, or the manifest could not be read |
| skipped | `·` | nothing to do, or you declined: no tracks selected, no manifest URL |
| cancelled | `⊘` | the current job and the rest of its batch were stopped by the user |

There is deliberately no pending state. A generator feeds playbacks one at a
time, so the controller does not know the next title until that title starts and
its row appears.

A glyph as well as a colour, so the states stay distinguishable where colour does
not survive.

Skipped is not failed. A title with no downloadable manifest has not gone wrong,
it is simply not something native delivery core can fetch yet, and colouring that red would
teach you to ignore red.

## Confirming

`confirm_batch` (off by default) asks once for the whole batch, on screen 3 where
the selection was made - there is no job yet, so there is nothing for screen 4 to
be about. Declining raises `Back`, which unwinds to the list you chose from.

## A failure stays on your screen

Normally screen 4 is popped when the flow returns to the service menu: screens
replace, they do not accumulate. A batch that had failures is the exception. `8 of
10 done, 2 failed` is exactly the thing you must not have taken off your screen,
so the menu is mounted *underneath* screen 4 instead, and `^b` reveals a menu
that is already waiting.

The hold is released as soon as the screen is dismissed, and a new batch
supersedes the previous one's report.

## Checking it

Run the project test suite and exercise a multi-select flow with a local
AutoPresenter or TUI pilot:

```bash
python -m pytest -q
```

The queue should account for every emitted playback, including cancelled rows.
