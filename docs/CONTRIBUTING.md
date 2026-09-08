# Contributing to unidl

UniDL services are the account, catalogue and key-acquisition layer; the native
delivery core is part of the same application. Keep that boundary intact:
services may log in, discover playback and acquire keys; the native core owns
manifest parsing, track selection, download, decrypt, subtitles and muxing.

## Before changing code

1. Create a virtual environment and install UniDL plus the development extras
   as described in [docs/testing.md](testing.md).
2. Read [docs/architecture.md](architecture.md) and, for service work,
   [docs/writing-a-service.md](writing-a-service.md).
3. Keep credentials, cookies, tokens, device files, content keys, command
   exports and debug logs out of version control. The visible project-local
   runtime layout is supported, but its paths must remain ignored.

## Project rules

- Service code never imports Textual, calls `print()` or calls `input()`. It
  yields Flow asks and returns a `Playback`.
- Put HTTP clients and response parsing in the service's `api.py`; keep the flow
  in `__init__.py`.
- Add each provider as one native package under `src/unidl/services/`. A
  code-shipped service may use one `@registry.register` decorator; a user
  imported package may rely on the loader's automatic class registration after
  TUI registration. In either case, do not add a second manual import to
  `src/unidl/services/__init__.py`. Follow
  [writing-a-service.md](writing-a-service.md). For a package delivered to an
  end user, the user must copy it into the installed services directory, choose
  **Settings → Services → Register a service**, and restart UniDL before it is
  available on Home or in global search.
- Do not duplicate manifest parsing, selection or download logic from the native delivery core.
- Do not hardcode credentials, local absolute paths or helper locations.
- Declare settings, credentials, helpers and `DRM_SYSTEMS`; do not make the UI
  infer them from service internals.
- Shared Track output selection controls only what the native delivery core downloads and muxes.
  Never reuse its quality/codec/range/audio/subtitle settings as licence-track
  policy; configurable licence tracks/profiles/PSSH seeds require a separate
  setting in that service's own section. The app-wide post-selection licence
  compatibility mode is implemented by Core and is not permission for service
  code to read those shared settings.
- Read and write session state only through `ctx.tokens`, which is rooted at this
  service's own folder under `paths.tokens`. Name the file, never the directory:
  path separators, absolute paths and `..` are rejected by `TokenStore`.
- If a service's file is renamed, move it rather than copy it: leaving the old
  name behind leaves a session that signing out does not remove and the next read
  adopts again.
- Keep narrative documentation free of fixed service totals. The porting record
  may quote a measured snapshot; nothing else should.

## Documentation with code changes

Update the matching reference in the same change:

- a config key: `docs/configuration.md` and `unidl.example.yaml`
- a global or track setting: `docs/settings.md`
- a Flow ask or service hook: `docs/writing-a-service.md` and the reference
  service comments
- a screen, binding or snapshot target: `docs/interface.md` and
  `docs/ui-design.md`
- DRM or vault behaviour: `docs/drm.md` or `docs/key-vault.md`
- a new verification command or fixture: `docs/testing.md`

Link new user-facing documents from `README.md` and use relative Markdown links
inside the repository.

## Verification

Run `ruff` and `compileall`, then the smallest relevant offline checks. Run live
checks only when you have the required account, region, device and authority;
state exactly which commands ran and which were not run. Never paste live
credentials, full content keys, CDM secrets or unredacted debug logs into a bug
report or review.

For a service port, exercise every declared entry point against the real
service. Importing successfully is not proof that login, catalogue parsing,
licensing or the current third-party schema works.

## Reloading service code during development

unidl does not watch source files. To adopt an edit without quitting, turn on
global **Debug mode**, return to the bare Home screen, highlight one service (or
filter to exactly one), and press `ctrl+r`. The action compiles the selected
service's loaded source first, reloads its multi-file package in dependency
order, and also reloads native service packages that share those modules. Core,
TUI and unrelated service modules are outside the reload graph.

The registry receives new classes, so only the **next** service session uses the
new code. No active service instance, download, licence exchange, heartbeat or
playback-session cleanup is patched. Reload is deliberately unavailable while a
service or overlay is on the screen.

This is a development convenience, not a transactional production hot patch.
A syntax error is rejected before any module is touched and the old registry is
restored after an import error, but Python cannot fully undo module globals that
an already-started import changed. Restart unidl after a failed reload, or after
adding/removing/renaming modules or symbols whose old names may remain in a
module namespace. Changing a service id also requires a restart.

## Change description

Explain:

- what user-visible behaviour changed;
- which contract or bug motivated it;
- the checks run and their environment (offline, loopback or live);
- any account, region, helper, CDM or external-service limitation;
- any migration or configuration step users must take.

Use the software only with accounts and content you are authorised to access.
