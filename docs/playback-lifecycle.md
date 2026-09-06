# Playback session lifecycle

Service code owns any provider playback/concurrency session. The native
downloader and shared DRM layer must not invent, refresh, or close a provider
session.

When a service opens a session before it emits a playback, keep the complete
flow inside one `try/finally`:

```python
opened = client.open_playback_session(source)
try:
    yield ctx.emit(playback)
finally:
    if opened:
        client.close_playback_session(source)
```

The `finally` must cover manifest parsing, key acquisition, command export,
download, cancellation, Back/Quit, and normal completion. If the source code
has no provider session or heartbeat, do not add one merely because the service
uses DRM. Local CDM sessions are separate and are always closed by the DRM
implementation itself.

This document is the framework contract for any service that opens a provider
playback or concurrency session. A service that has no such lifecycle should
simply emit its Playback without adding one.
