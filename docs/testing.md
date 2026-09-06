# Testing UniDL

UniDL uses fast offline checks for contracts and deterministic behavior, then
focused live checks for provider schemas, accounts, DRM and manifests. Keep live
fixtures and credentials outside the repository.

## Fast checks

Run these from the project root:

~~~console
python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
python -m unidl --config ./unidl.yaml services
python -m unidl --config ./unidl.yaml cdm --check
~~~

The first three commands are safe for continuous integration. The CLI commands
verify registration and local device configuration without starting a download.

## Service tests

A service test suite should cover:

1. import and registry validation;
2. every declared URL, search, live, library and login entry point;
3. Flow answers, paging, multi-select batches and Back navigation;
4. credential, cookie, token, refresh and logout behavior;
5. manifest/profile settings independently from output-track settings;
6. service-local licence transport for every declared DRM system;
7. multiple PSSH/KID behavior and live key rotation where applicable;
8. chapter, audio metadata, helper and playback-session cleanup paths;
9. one authorized title reaching a typed Playback.

Use recorded response fixtures with redacted URLs and keys. Unit tests should
replace network clients with deterministic fakes. A headless Flow test can use
AutoPresenter:

~~~python
from unidl.core.flow import AutoPresenter, FlowContext, run_flow

ctx = FlowContext(settings=service.settings)
emitted = run_flow(
    service.open_url(ctx, "https://example.invalid/title"),
    AutoPresenter({"Season": 1}),
)
assert emitted
~~~

## Downloader tests

The native delivery layer should be tested with local manifests and loopback
HTTP fixtures. Cover DASH, HLS, ISM, JSON and direct-media parsing as applicable,
then verify:

- selected tracks are exactly the tracks passed to delivery;
- KID/key inventories contain every required key;
- retries and cancellation stop promptly;
- live duration zero means unlimited;
- output naming uses the selected video/audio codec and range tags;
- subtitles, chapters, cover art and ID3 metadata survive muxing;
- a failed optional metadata request does not prevent download.

Avoid real signed manifests and content keys in fixtures.

## Live checks

Live recordings should use a local playlist with a moving media sequence or a
deterministic fake refresh loop. Verify track selection occurs before recording,
DVR/replay choices return to the previous question on Back, Stop/Back/Esc stops
the job, and rotating KIDs trigger only the service's proven init-data path.

## Packaging checks

Build both artifacts before publishing:

~~~console
uv build
~~~

Inspect the wheel and source archive for accidental credentials, device files,
tokens, cookies, logs, local absolute paths and generated caches. Test an
editable install and a clean virtual environment on each supported OS/CPU target.

## Reporting failures

Include the UniDL version, OS, service ID, selected profile, manifest type,
DRM system, and the smallest redacted log excerpt that reproduces the issue.
Never include usernames, passwords, refresh tokens, cookies, KID:key pairs,
license bodies, signed URLs or private CDM material.
