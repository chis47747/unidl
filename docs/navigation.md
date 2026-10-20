# Back and one-level navigation

## The rule

**Back returns to the immediate previous page that opened the current step.**
It is not a shortcut to the service home or platform list. This applies to
keyboard Back, clickable Back, search results, browsing, track/source choices,
download success, download failure, and saved command/export results.

A page is a logical interaction, not necessarily a separate Textual screen.
Search results, seasons and episodes can all share one FlowScreen; moving Back
must still visit each of those pages individually. Keep the owning generator
and page state alive instead of rebuilding the service session.

The service home is a valid destination **only when it really is the preceding
page**, such as Back from its URL/search input. Back on the service home itself
leaves the service for the platform list. Do not create a season, source or
track page that was never shown. A direct URL can have fewer parent pages than
a title reached through search.

## Expected destinations

| Current page/action | Back destination |
| --- | --- |
| Episodes opened from a season picker | That season picker, not search or service home. |
| Seasons opened from search results | Those search results, not the search-input box. |
| Search results | The search-input page that launched them. |
| Audio/source choices opened from an episode picker | The preceding episode or source page, one level at a time. |
| A completed or failed delivery/result | Its immediately preceding live selection/input page. The outcome does not change the parent. |
| A saved command, export or track-only result | The selection/input page that launched that operation. |
| Settings, chapters, attachments, lyrics or a job-details popup | Close that overlay and reveal its launching page; do not also navigate the page underneath. |
| Search/URL input opened from service home | Service home, because it is the actual parent here. |

For example, a service that shows all these choices has this return sequence:

```text
delivery/result → audio/source → episodes → seasons
                → search results → search input → service home → platform list
```

Keep available cursor positions, selected episodes, search text and loaded
results when restoring a page. Back must not re-run an API search or download,
automatically retry a failed title, or discard a valid completed output.

An earlier manifest, license or download failure does not add an error-summary
page to this return path. After returning to the service home, one Back leaves
for Home even when the session's queue still contains failed jobs. Retain their
outcomes and logs; do not clear failure history just to allow navigation. A flow
that actually stops with an error, without an explicit service-home Back, still
keeps its error visible until the user navigates away.

## Flow ownership and exception forwarding

Delegate child flows with `yield from`. It forwards ordinary answers **and**
exceptions into the active child. A child may catch `Back` and yield its own
parent page; the enclosing flow must not consume a second Back or jump again.
Let an unhandled `Back` bubble exactly one owning level.

The shared `Service._did_anything()` search wrapper counts whether the child
produced an ask/playback without changing those delegation rules. It forwards
exceptions to the child as well as answers and closes it on teardown. A custom
service `home()`, direct `yield from` delegation, or existing
`_did_anything()` override remains in control; Core does not add another
navigation wrapper around it.

Do not implement a proxy using only `flow.send(answer)`: a Back raised at the
proxy's `yield` would then close the child before its handler could restore the
previous page. If a custom proxy is necessary, forward exceptions with
`flow.throw(exc)` too. Treat `GeneratorExit` as teardown: close the child and
release resources without yielding another page.

## Keep the closest parent alive

Each page owns recovery from the child it opened. Yield the page outside the
child's recovery block, so Back **on that page** can leave it, while Back from a
deeper child restores it:

```python
def browse(ctx, page, visit):
    while True:
        selected = yield page
        # Update page.cursor/preselected here when appropriate.
        try:
            yield from visit(selected)
        except Back:
            continue  # Child closed: restore this immediate parent.
        except RuntimeError as exc:
            ctx.problem("Could not finish that", str(exc))
            continue  # Recoverable child failure: same parent, not service home.
        if not ctx.interactive:
            return    # Headless rules must not create an endless prompt loop.
```

Catch the service's expected network/preparation failures at the same closest
owner. Do not swallow programming errors or `Quit` with a broad catch. Once an
exception escapes and terminates a generator, an outer page cannot resume its
lost stack. The service home's error handler is a final safety boundary, not the
normal navigation mechanism for failed searches, manifests or downloads.

Download success and failure must preserve the same return target. In the
shared delivery driver, a completed run's `Back` is thrown at the service's
`yield ctx.emit(...)`; keep the picker/input owning that emit alive. The signal
also covers dismissing a finished result and must not be interpreted as a new
request to skip every browsing level.

## Running downloads, batches and Quit

Back during active work still requests the existing stop/finalization behavior
before returning to the previous page; this navigation rule does not remove
cancellation, checkpoint cleanup or live-recording finalization.
See [cancelling.md](cancelling.md) for those lifecycle rules.

An intermediate batch title's success or failure does not create a navigation
event: continue according to the existing batch policy. After the final title
or an explicit batch stop, return to the active page that owns the batch/delivery,
not directly to service home. Keep remaining titles unstarted after an explicit
stop. A history/details popup closes only itself.

Quit is not Back. It keeps the application's existing exit and preservation
policy; it must not reopen a parent picker or trigger extra live finalization.

## Verification

Test one-level return through the real entry point, not only `service.search()`
or `open_url()` in isolation:

- Enter through shared service home, a custom service home, and a direct link.
- Visit results, seasons, episodes and any source/audio choices actually shown.
- Exercise successful delivery, preparation/download failure, command/export
  results and a stopped batch. Back must restore the same actual parent.
- Verify cursor/selection retention and no repeated provider API requests.
- Repeat with a service's existing navigation override enabled; one Back must
  never produce two transitions.
- Check mouse Back, keyboard Back, popup dismissal, Quit and generator cleanup.
