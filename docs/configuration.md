# Configuration

## First launch: no YAML required

After installation, run `unidl` (or `python -m unidl`). A missing YAML file
does not prevent startup. On first launch, Home shows the actual download
directory with a clickable link to review or change it.

The default is `~/unidl_downloads/<service>/`, which on Windows normally means
`C:\Users\<username>\unidl_downloads\<service>\`. Directories are created
automatically. Use **Settings → Files & naming → Output locations → Finished
downloads** to change the destination without editing YAML.

Precedence is: the saved **Finished downloads** preference, then YAML
`paths.downloads`, then the built-in `~/unidl_downloads` default. Setting
`paths.home` changes runtime state, not this finished-media default.

Use `--config PATH` only to choose a specific file. For example,
`unidl --config ./unidl.private.yaml` does not read `unidl.yaml`; editing the
latter will not change that run. Keep using the same explicit path on later
launches. Source installs normally use their checkout's `unidl.yaml`; packaged
installs use `unidl.yaml` in the current directory if it exists.

UniDL has two configuration tiers:

- unidl.yaml stores paths, device definitions, credentials, helpers, proxies and
  service-owned static values.
- settings.json under paths.home stores interactive preferences such as quality,
  output tracks, theme, locale, vault policy and the selected CDM.

If you choose to use a custom YAML, pass it explicitly when starting UniDL:

~~~console
python -m unidl --config ./unidl.yaml
~~~

Relative paths are resolved from the directory containing that YAML, not from the
process working directory. This keeps a checkout portable. Absolute paths and
environment variables are supported for private installations.

## Minimal YAML

~~~yaml
paths:
  home: .
  commands: ./download_commands
  exports: ./exports
  keys_db: ./db/keys.db
  downloads: ./downloads
  helpers: ./helpers
  cdm: ./cdm
  cookies: ./cookies
  tokens: ./tokens

cdm:
  default: widevine-test
  devices:
    widevine-test: ./cdm/widevine/device.wvd
    playready-test: ./cdm/playready/device.prd
    monalisa-test: ./cdm/monalisa/device.mld

credentials: {}
helpers: {}
proxies: {}
services: {}
~~~

The exact service settings and credential fields are declared by each service.
Unknown path keys are ignored so a typo cannot silently redirect runtime state.

## Paths and runtime state

paths.home is the root for disposable and account-bearing state. Most runtime
directories below are derived from it; finished downloads are the exception:

| Path | Purpose |
|---|---|
| downloads | Finished media under `~/unidl_downloads`, grouped by service, unless overridden. Not derived from paths.home. |
| commands | Saved command/export text, grouped by service. |
| exports | Re-importable resolved-title JSON documents. |
| cache / temp | Manifest, segment and transient working data. |
| logs | Timestamped session and per-task debug logs. |
| tokens | Service login and refresh state. |
| cookies | Browser cookie profiles, grouped by service. |
| cdm | Local device files, grouped by DRM system. |
| keys_db | Local SQLite content-key vault. |
| subtitles | Service-fetched subtitle sidecars. |
| helpers | Service helper assets and modules. |

Core creates directories on demand and applies owner-only permissions to
credential, device, key and log state where the operating system supports them.

## CDM entries

Each cdm.devices value maps a user-facing name to a .wvd, .prd or .mld file. The
file suffix determines the DRM system; a mismatched system is refused rather than
silently falling back to another device. A MonaLisa device also needs the wasm
module declared by its .mld file.

The TUI's DRM & CDM manager can discover devices under paths.cdm and configured
search paths. A service may offer a service-specific CDM setting; that choice is
kept separate from the app-wide default and from output-track settings.

Remote CDMs are declared under remote_cdm and are selected by name. The endpoint
receives only the challenge/parse operations supported by its DRM system. Use
HTTPS and keep its token in a private configuration file.

## Vault entries

Declare one or more local/remote vaults under key_vaults. The vault manager
supports enable/disable, read/write policy, service filters and manual KID:key
entry. The TUI can edit these resources without hand-editing YAML. Keep vault
tokens and keys private.

For HTTP vaults, keep any operator-required trailing slash in the endpoint URL;
some routers distinguish `/vault` from `/vault/`. A `supported_services` list is
an explicit capability declaration for home-screen remote search. Automatic
lookup and storage use the active service ID directly, while `service_map` aliases
are applied to explicit searches.

## Service-specific quality settings

The public example service exposes a `manifest_profile` (`hd` or `uhd`)
independently of its `license_profile`. A provider can also declare independent
codec, encryption or bitrate-adaptation source choices. The shared
`video_quality` setting is applied only after a returned manifest is parsed; it
does not choose which manifest profile a service requests.

## Credentials and cookies

Credential slots are declared by the service. Values live under that service's
namespace and are never shared implicitly with another service. Cookie profiles
are selected by profile name and read only from <paths.cookies>/<service>/.

Use a private override for real values:

~~~console
python -m unidl --config ./unidl.private.yaml
~~~

Do not commit credentials, refresh tokens, cookies, device files, content keys,
signed URLs or remote-vault/CDM bearer tokens.
