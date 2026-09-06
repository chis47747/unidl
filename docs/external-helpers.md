# External helpers

Helpers are dependencies a native service explicitly declares. Resolution is
centralised in `unidl.core.helpers`; a service never searches arbitrary host
directories or embeds a user's absolute path.

Services may declare optional helpers such as certificates, binaries or modules:

| Helper | Kind | Purpose |
|---|---|---|
| `helpers/bbc/iplayer.pem` | asset | BBC UHD media-selector client certificate |
| `curl` on `PATH` | binary | BBC UHD selector transport |

If an optional dependency is missing, the service should report the degraded
mode before playback instead of failing halfway through a download.

## Resolution rules

For a declared helper, UniDL checks only explicitly named locations:

1. a per-service override in the selected YAML;
2. a shared helper override;
3. `PATH` for declared binaries;
4. `<paths.home>/helpers/<service>/`;
5. declaration-provided package resources.

There is no recursive search of a developer's filesystem. Keep machine-specific
executables and private assets outside version control, and use relative paths
or documented placeholders in committed configuration and documentation.

Small public assets may be bundled as package data and loaded through
`importlib.resources`. Private certificates, tokens, device identities, and
large native runtimes belong in the ignored runtime helper directory.
