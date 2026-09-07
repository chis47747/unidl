"""Runtime settings.

Two tiers, deliberately kept apart:

* **service settings** - each service's own vocabulary (profiles, regions,
  markets or other provider selectors). Declared by the service, only meaningful
  while that service is active, and they usually change *which manifest you get*.
* **track settings** - one shared vocabulary for every service
  (resolution / codec / range / audio / subs). Applied *after* the manifest is
  parsed, against the real ladder.

Both live in the same store and are editable from the TUI at any time, so a
change takes effect on the next request without restarting.

Static things (CDM paths, credentials) do **not** belong here - they live in
``unidl.yaml``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Imported rather than restated: the default region list belongs to the JustWatch
# code, and two copies of it would drift apart.
from . import cdmrules, naming
from .justwatch import DEFAULT_REGIONS as _JUSTWATCH_DEFAULT_REGIONS
from .secureio import atomic_write_text, locked_path, private_file


@dataclass
class Option:
    value: Any
    label: str = ""
    help: str = ""

    def display(self) -> str:
        return self.label or str(self.value)


@dataclass
class Setting:
    key: str
    label: str
    kind: str = "choice"  # choice | bool | text | int | multi | vaults | action
    options: list[Option] = field(default_factory=list)
    default: Any = None
    help: str = ""
    #: changing this invalidates the cached session/login for the service
    resets_session: bool = False
    #: edit this with a dedicated screen instead of the generic list. ``"cdm"``,
    #: or ``"cdm:<system>"`` to restrict it to one system's devices. The options
    #: are still filled in, so the current value has a label and a check can read
    #: what was on offer without opening anything.
    picker: str = ""
    #: Runtime settings can remain addressable for compatibility while a unified
    #: editor owns their visible UI. Vault policy is the current example: five
    #: persisted keys are one coherent decision rather than five unrelated rows.
    visible: bool = True

    def option_labels(self) -> list[str]:
        return [o.display() for o in self.options]

    def coerce(self, value: Any) -> Any:
        if self.kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
        if self.kind == "int":
            try:
                return int(value)
            except (TypeError, ValueError):
                return self.default
        if self.kind == "multi":
            # Multi-valued service settings are persisted as a tuple so callers
            # never have to guess whether a YAML/string/list representation was
            # loaded.  A comma/newline separated string remains convenient for
            # headless config and the TUI editor supplies a list/tuple directly.
            if isinstance(value, str):
                values = [part.strip() for part in value.replace("\n", ",").split(",")]
            elif isinstance(value, (list, tuple, set, frozenset)):
                values = list(value)
            else:
                values = [] if value in (None, "") else [value]
            allowed = {option.value for option in self.options}
            result = []
            for item in values:
                if item in allowed and item not in result:
                    result.append(item)
            return tuple(result)
        if self.kind == "choice" and self.options and not self.picker:
            # Not for a picker-backed choice. Its options are a snapshot of what
            # existed when the service was built, while the picker reads the folder
            # live - so a device dropped in five minutes ago was offered, accepted,
            # and then coerced back to "" on the way into the store, silently.
            allowed = {o.value for o in self.options}
            return value if value in allowed else self.default
        return value


def _choice(
    key: str,
    label: str,
    options: list[tuple[Any, str]],
    default: Any,
    help: str = "",
    picker: str = "",
    visible: bool = True,
) -> Setting:
    return Setting(
        key=key,
        label=label,
        kind="choice",
        options=[Option(v, lbl) for v, lbl in options],
        default=default,
        help=help,
        picker=picker,
        visible=visible,
    )


def multi_choice_setting(
    key: str,
    label: str,
    options: list[tuple[Any, str]],
    default: tuple[Any, ...] = (),
    help: str = "",
    *,
    resets_session: bool = False,
) -> Setting:
    """Declare a service-owned multi-select API/profile setting.

    This is intended for facts a service sends to its playback API (several
    resolutions or profiles), not for UniDL's shared output-track selector.
    """
    return Setting(
        key=key,
        label=label,
        kind="multi",
        options=[Option(value, option_label) for value, option_label in options],
        default=tuple(default),
        help=help,
        resets_session=resets_session,
    )


#: how each audio container is described, for the ones UniDL knows about
_AUDIO_LABELS = {
    "mp3": "MP3 320 Kbps",
    "flac": "FLAC (lossless)",
    "alac": "ALAC in M4A (lossless)",
    "m4a": "M4A / AAC",
    "opus": "Opus",
    "wav": "WAV (uncompressed)",
}


def _audio_format_setting() -> Setting:
    """Audio container, offering exactly what this UniDL build can produce.

    Read from unidl.downloader rather than listed here, so the day it learns FLAC the
    option appears without a change on this side - and, more importantly, an
    option that cannot work never appears at all.
    """
    try:
        from unidl.downloader import api

        supported = [name for name in api.audio_formats() if name]
    except Exception:  # a broken or older UniDL must not stop the app starting
        supported = ["mp3"]

    options: list[tuple[Any, str]] = [("source", "Keep the original, do not re-encode")]
    options += [(name, _AUDIO_LABELS.get(name, name.upper())) for name in supported]
    return _choice(
        "audio_format",
        "Audio-only format",
        options,
        "mp3" if "mp3" in supported else "source",
        "Applies to audio-only titles - radio, podcasts - and is ignored for "
        "anything with a picture. Re-encoding also writes ID3 tags and cover art.",
    )


def _drm_system_setting(
    system_ids: list[str] | None = None,
    *,
    inheritable: bool = False,
    visible: bool = True,
) -> Setting:
    """The DRM system choice, built from whichever systems are registered.

    Two callers, one shape. The app-wide setting offers everything installed; a
    service that can use more than one system gets its own copy offering only
    those, plus an "app-wide setting" entry - because "this service always needs
    PlayReady" and "I generally prefer PlayReady" are different statements and
    the second should not have to be repeated per service.
    """
    from .drm import all_systems, get

    systems = [s for s in (get(i) for i in (system_ids or [])) if s] if system_ids else all_systems()
    options: list[tuple[Any, str]] = []
    if inheritable:
        options.append(("", "Use the app-wide setting"))
    options += [(system.id, system.option_label()) for system in systems]
    default = "" if inheritable else (systems[0].id if systems else "widevine")
    help_text = (
        "Which system to use where a service offers more than one. Only affects "
        "this service."
        if inheritable
        else "Which CDM to use when a service offers a choice. Each system needs "
        "its own library and its own device file; one that is not installed says so."
    )
    return _choice("drm_system", "DRM system", options, default, help_text, visible=visible)


def drm_system_setting(system_ids: list[str]) -> Setting:
    """A service's own DRM choice, offering only the systems it supports."""
    return _drm_system_setting(system_ids, inheritable=True)


def proxy_setting() -> Setting:
    """A service's own proxy, overriding the app-wide one.

    Its own ``Setting`` object rather than the app-wide one, which is what puts it
    in this service's section of the screen and what lets empty mean "no opinion
    here" - :meth:`Settings.inherited` then falls through to the app-wide value.
    Per service because a proxy is usually needed for *one* geofenced service, and
    routing the rest of the catalogue through it is slower and, for a service that
    checks where the account signed in from, wrong.
    """
    return Setting(
        "proxy",
        "Proxy",
        "text",
        default="",
        help="Empty means whatever is set app-wide - including nothing, which is "
        "the default. A name from the `proxies` section of unidl.yaml, or a full "
        "URI. Applies to this service's API calls, its licence request, the "
        "manifest and the download.",
    )


def cookie_profile_setting(
    service_id: str,
    config: Any = None,
    legacy_ids: tuple[str, ...] | list[str] = (),
) -> Setting:
    """The browser-cookie file this service is allowed to use.

    Values are profile names, not paths. Options come only from direct ``.txt``
    files under ``<cookies>/<service id>/``; this keeps a service setting from
    becoming a general-purpose path field capable of reaching another account.
    The empty value selects this service's ``default.txt``.
    """
    options = [
        Option(
            "",
            "Automatic · default.txt",
        )
    ]
    folder = None
    if config is not None:
        from .cookies import CookieStore

        store = CookieStore(config.paths.cookies)
        folder = store.root / service_id
        paths = []
        for name in (service_id, *legacy_ids):
            paths.extend(store.profile_files(name))
        seen_profiles: set[str] = set()
        for path in paths:
            if path.stem in seen_profiles:
                continue
            seen_profiles.add(path.stem)
            options.append(Option(path.stem, path.name))
    where = str(folder) if folder is not None else f"<cookies>/{service_id}"
    return Setting(
        key="cookie_profile",
        label="Browser cookie file",
        kind="choice",
        options=options,
        default="",
        help=(
            f"Only direct .txt files in {where} are offered. A named selection "
            "uses that exact file with no fallback to another account. Automatic "
            "uses default.txt in the same service folder. Reopen the service after "
            "changing this if it already "
            "created an API session."
        ),
        # The picker marker keeps a persisted file name visible even if that file
        # is temporarily absent; coercing it to automatic could sign into a
        # different account without saying so.
        picker="cookie",
    )


def _device_choices(system_id: str, config: Any) -> list[tuple[Any, str]]:
    """Every CDM that could answer ``system_id`` - local files and remote ones.

    Read from the live config rather than listed here, for the same reason the DRM
    choice is read from the registry: a device that is not installed must not be
    offered, and one that was dropped into the folder five minutes ago must be.
    Remote CDMs sit in the same list because "which CDM does this service use" is
    one question, and splitting it into "local or remote" and then "which"
    produces two settings that can disagree.
    """
    # Devices named in unidl.yaml belong in this list even when they live outside
    # the search paths, and one name means one row: both facts live in
    # `cdmrules.known_devices`, because the rules screen has to see exactly the
    # devices this list offers or a rule could name something unpickable.
    choices: list[tuple[Any, str]] = []
    for device in cdmrules.known_devices(config):
        if system_id and device.system != system_id:
            continue
        # the folder is left out on purpose: names are unique in this list, so it
        # would only lengthen a row that has to fit next to a setting's label. A
        # remote one still says so, because that changes where the licence goes.
        remote = f"remote · {device.where}" if device.is_remote else ""
        bits = (device.name, device.level, remote)
        choices.append((device.name, "  ".join(b for b in bits if b)))
    return choices


def _cdm_device_setting(system_id: str, label: str, config: Any) -> Setting:
    options: list[tuple[Any, str]] = [("", "Use the app-wide choice")]
    found = _device_choices(system_id, config)
    options += found
    help_text = (
        "Which CDM this service uses. Only affects this service: every other one "
        "keeps following the app-wide choice - ^o on the main screen, or "
        "cdm.default in unidl.yaml - and empty here means exactly that, no opinion, "
        "so turning this on for one service never has to be undone for the rest. "
        "Remote CDMs are listed next to the local device files; picking one sends "
        "the challenge to that server instead of using a device on this machine."
    )
    if system_id and not found:
        help_text += (
            f" Nothing was found for {system_id} under the CDM search paths, so "
            "there is nothing to choose here yet."
        )
    return _choice(
        # not `cdm_device`: the main screen's picker already stores the app-wide
        # choice under that key, and two settings with one name that mean different
        # things in different scopes is a bug waiting for a reader to trip over
        f"cdm_{system_id}" if system_id else "cdm_any",
        label,
        options,
        "",
        help_text,
        # the real picker, with its search box: there are over a hundred device
        # files on this machine and a flat option list is not a way to choose one
        picker=f"cdm:{system_id}" if system_id else "cdm",
    )


def cdm_device_settings(system_ids: list[str] | None = None, config: Any = None) -> list[Setting]:
    """A service's own CDM, one setting per DRM system it can use.

    One per system rather than one field, because the two are different files
    holding different provisioning: a ``.prd`` cannot answer a Widevine challenge,
    so a single field would have to be re-picked every time the DRM choice above
    it changed - and the point of a per-service setting is that it is set once and
    left alone. With one each, "this service uses that PlayReady device and that
    Widevine device" is stated once and both halves stay true.

    A service that declares no systems gets a single system-less field instead:
    it follows the app-wide DRM choice, so which system its device must serve is
    not known here.
    """
    from .drm import get

    systems = [s for s in (get(i) for i in (system_ids or [])) if s]
    if not systems:
        return [_cdm_device_setting("", "CDM device", config)]
    return [_cdm_device_setting(system.id, f"{system.label} CDM", config) for system in systems]


def live_settings() -> list[Setting]:
    """How this service's live channels are recorded.

    Only given to services that declare ``SUPPORTS_LIVE``. A recording length on a
    service that has no live streams is a setting that can be changed and never
    read, and the whole point of the per-service section is that everything in it
    does something.

    Length is per service rather than app-wide because it is a property of what is
    being recorded: a football match, a news channel and a radio station want three
    different numbers, and having to re-type one before every run is how a recording
    ends up truncated.
    """
    return [
        Setting(
            key="live_record_limit",
            label="Live recording length",
            kind="text",
            default="00:00:00",
            help="How long a recording runs before UniDL stops it: HH:MM:SS, or "
            "seconds, or 1h20m. The default 00:00:00 means no length limit: keep "
            "recording until Stop, Back or Esc. This is the default offered after "
            "final track selection and the value used by headless runs; one "
            "interactive recording can override it.",
        ),
        Setting(
            key="live_replay",
            label="Offer the replay window",
            kind="bool",
            default=False,
            help="Many live streams keep the last stretch available to rewind into - "
            "a replay or DVR window. This chooses the default after final track "
            "selection. When a window is requested and actually there, the delivery "
            "screen asks what to take from it: the edge, the whole window "
            "as a file, or a stretch measured from the window's start. Asked at that "
            "point rather than here because how much is available is only known once "
            "the manifest has been read.",
        ),
    ]


def cdm_choice(settings: Settings, system: str = "") -> str:
    """The CDM this scope's own settings name, or "" when they name none.

    The system-specific key first, then the system-less one, so a service that
    declares its systems and one that does not are read the same way by callers
    that only know they want "this service's device".
    """
    keys = [f"cdm_{system.strip().lower()}"] if system else []
    keys.append("cdm_any")
    for key in keys:
        if key in settings.spec_by_key:
            value = str(settings.get(key) or "").strip()
            if value:
                return value
    return ""


#: Reserved store key for app-wide settings.
GLOBAL_SCOPE = "@global"


def _vault_target_settings(config: Any = None) -> list[Setting]:
    """Build the multi-vault settings from the current YAML entries."""
    from . import vaults

    descriptors = vaults.configured_vaults(config) if config is not None else []
    all_options = [
        Option(
            descriptor.name,
            f"{descriptor.name} · {'remote' if descriptor.remote else 'local'}"
            + (" · disabled" if not descriptor.enabled else ""),
        )
        for descriptor in descriptors
    ]
    writable_options = [
        option
        for option, descriptor in zip(all_options, descriptors, strict=True)
        if descriptor.writable
    ]
    searchable_descriptors = [descriptor for descriptor in descriptors if descriptor.searchable]
    searchable_options = [
        Option(
            descriptor.name,
            f"{descriptor.name} · {'remote key search' if descriptor.remote else 'local'}"
            + (" · disabled" if not descriptor.enabled else ""),
        )
        for descriptor in searchable_descriptors
    ]
    default_search_targets = vaults.serialize_targets(
        tuple(
            descriptor.name
            for descriptor in searchable_descriptors
            if not descriptor.remote and descriptor.enabled
        )
    )
    return [
        Setting(
            "vault_read_targets",
            "Vaults used for key lookup",
            kind="vaults",
            options=all_options,
            default="",
            picker="vaults:read",
            visible=False,
            help=(
                "Choose one or more configured vaults. Empty means all vaults in the "
                "local/remote categories enabled above; use the explicit empty "
                "selection to disable every destination. Read-only remote vaults "
                "remain available here."
            ),
        ),
        Setting(
            "vault_search_targets",
            "Search vaults",
            kind="vaults",
            options=searchable_options,
            default=default_search_targets,
            picker="vaults:search",
            visible=False,
            help=(
                "Choose local and explicitly searchable remote vaults. Local keys "
                "appear immediately; remote keys are queried only after you select "
                "Search remote vault from the results. Empty selection turns vault "
                "search off."
            ),
        ),
        Setting(
            "vault_write_targets",
            "Vaults receiving acquired keys",
            kind="vaults",
            options=writable_options,
            default="",
            picker="vaults:write",
            visible=False,
            help=(
                "Choose one or more writable vaults for licence, rotation and "
                "manual keys. Empty means all writable configured vaults; a remote "
                "vault is still subject to the remote-vault safety switch during "
                "automatic playback."
            ),
        ),
    ]


#: App-wide settings, editable from the main screen and inside any service.
GLOBAL_SETTINGS: list[Setting] = [
    Setting(
        "download_manager",
        "Download behavior",
        "action",
        default="",
        picker="download_behavior",
        help=(
            "Choose what happens after a title is resolved, native transfer options "
            "that are not per-service track settings, whether live streams are "
            "recorded, whether DRM waits for the final selected tracks, and whether "
            "batch downloads ask for confirmation."
        ),
    ),
    Setting(
        "fetch_chapters",
        "Fetch chapter metadata",
        "bool",
        default=True,
        help=(
            "When enabled, services may request and parse optional chapter metadata. "
            "When disabled, chapter endpoints and optional chapter fields are skipped. "
            "A chapter failure is auxiliary and never stops playback, DRM, downloading "
            "or final muxing. This controls acquisition globally; the per-service "
            "'Embed chapters in the final file' setting controls only container muxing."
        ),
    ),
    _choice(
        "after_resolve",
        "After you pick a title",
        [
            ("download", "Download the file now"),
            ("command", "Don't download, just save the command"),
            ("list", "Just list the tracks, ask for no keys"),
            ("export", "Save an export file: the manifest, the tracks and the keys"),
            ("ask", "Ask me each time"),
        ],
        "download",
        "Downloading and saving the command both need a licence, and both leave the "
        "command file and any keys behind. Listing asks for nothing: it reads the "
        "manifest, shows what is in it, and stops. An export is the licence written "
        "down: one file that can be imported here or by someone else, and finished "
        "without an account, a CDM or a second licence request. It holds the content "
        "keys, so treat it like one.",
        visible=False,
    ),
    Setting(
        "live_record",
        "Record live streams",
        "bool",
        default=False,
        help="Off, a live channel is resolved, parsed, keyed and its command saved - "
        "everything except the recording, which is what you want when you are "
        "collecting streams rather than sitting through them. On, UniDL records, "
        "for as long as the service's own 'Live recording length' default says "
        "(00:00:00 means until you stop it). In "
        "the TUI both choices are confirmed after tracks are selected; these values "
        "are the preselected defaults and remain authoritative for headless runs. Off "
        "by default because a recording does not end on its own.",
        visible=False,
    ),
    Setting(
        "license_after_tracks",
        "License after final track selection",
        "bool",
        default=False,
        help=(
            "Compatibility mode for services whose licence init data is only "
            "available only on selected media playlists. Off keeps UniDL's default "
            "full-manifest inventory licensing; "
            "on waits until you confirm the output tracks, then asks for keys only "
            "from those encrypted tracks. It uses the final selection only as the "
            "source of KIDs/init data; it never turns a shared output preference into "
            "a service API profile or changes which tracks are downloaded."
        ),
        visible=False,
    ),
    _drm_system_setting(visible=False),
    Setting(
        key="cdm_rules",
        label="CDM rules by quality",
        kind="text",
        default="",
        help="Which device answers, decided by the resolution being taken - an L1 "
        "for 1080p and above, an L3 for the rest, say. Built by picking a "
        "threshold and then a device, and shown in the order they are tried, so "
        "the first one that matches is the one you can see. A rule naming a device "
        "for the other DRM system is skipped rather than tried, which is how one "
        "Widevine rule and one PlayReady rule live at the same threshold. Empty "
        "means what it always meant: one device for everything. The resolution "
        "read here is the automatic selection's, since that is what is settled "
        "when the licence is asked for.",
        # a screen, not a text field: these are device names, and typing one is
        # how you get a rule that points at nothing
        picker="cdm_rules",
        visible=False,
    ),
    _choice(
        "theme",
        "Colour theme",
        [
            ("dark", "Dark"),
            ("light", "Light"),
        ],
        "dark",
        "Takes effect immediately and repaints the current screen.",
        visible=False,
    ),
    _choice(
        "interface_locale",
        "Interface language",
        [
            ("system", "Follow the system"),
            ("en", "English"),
            ("zh-Hans", "Simplified Chinese"),
            ("zh-Hant", "Traditional Chinese"),
            ("es", "Español"),
            ("fr", "Français"),
            ("pt", "Português"),
        ],
        "system",
        "Language of UniDL's own labels, prompts and settings. Service names, "
        "titles, URLs and API locales are not translated. Follow the system reads "
        "LC_ALL, LC_MESSAGES, then LANG.",
        visible=False,
    ),
    _choice(
        "service_list_view",
        "Service list view",
        [
            ("alphabetical", "Alphabetical"),
            ("country", "By country"),
            ("type", "By media type"),
        ],
        "alphabetical",
        "How the home screen groups services. Country uses the first declared "
        "GEOFENCE code as the primary market; empty GEOFENCE means International. "
        "Media type is declared by each service and may be audio, video or both.",
        visible=False,
    ),
    Setting(
        "resource_manager",
        "DRM & vaults",
        "action",
        default="",
        picker="resources",
        help=(
            "Manage app-wide DRM/CDM choices, local and remote CDM devices, and "
            "key-vault backends in one place. "
            "Remote definitions are saved to the project unidl.yaml; enabled state "
            "and lookup/write/search policy remain in the app settings."
        ),
    ),
    Setting(
        "storage_manager",
        "Files & naming",
        "action",
        default="",
        picker="storage",
        help=(
            "Manage output folders and file-name templates in one place. "
            "Sensitive runtime paths are shown there for reference but remain read-only."
        ),
    ),
    Setting(
        "local_vault",
        "Use the local key vault",
        "bool",
        default=True,
        help="On, a title whose key IDs are all already in the local vault is "
        "served from there and no licence request is made - which is why a second "
        "download of the same title is instant. Off, every title goes through a "
        "real licence exchange, which is what you want when a stored key is "
        "suspect, when the CDM or the licence server is what is being tested, or "
        "when a service has re-encrypted something under the same key IDs. Keys "
        "are still written to selected local write targets either way: switching "
        "this off means you do not trust what is stored, not that you want to "
        "stop recording it.",
        visible=False,
    ),
    Setting(
        "remote_vault",
        "Use remote key vaults",
        "bool",
        default=False,
        help="The same thing for the vaults configured under `key_vaults` that "
        "live on a server. Off by default because it is a network call and because "
        "it sends your keys somewhere: unlike the local switch, this one governs "
        "**writes as well as reads** - a switch that is off should not be pushing "
        "anything. With both on, selected local vaults are always asked first and "
        "only the key IDs they did not have reach the network; a key that comes "
        "back is copied into selected writable local targets.",
        visible=False,
    ),
    # Options are filled with the configured vault names by global_settings().
    # Keeping the declarations here means service scopes know these are truly
    # app-wide settings and existing checks can inspect the stable vocabulary.
    *_vault_target_settings(),
    Setting(
        "proxy_manager",
        "Proxy & VPN",
        "action",
        default="",
        picker="proxy_manager",
        help=(
            "Choose the app-wide route, decide whether segment downloads use it, "
            "and manage named proxy endpoints and supported VPN proxy providers. "
            "A service can still override this route in its own settings."
        ),
    ),
    Setting(
        "proxy",
        "Proxy",
        "text",
        default="",
        help="Empty means direct, which is the default: a proxy is offered, never "
        "imposed. A value is either a name from the `proxies` section of unidl.yaml "
        "or a full URI - http://host:port, socks5://host:port, with credentials in "
        "it if the proxy needs them. It applies to everything a service does: its "
        "API calls, the manifest, the licence request and the download. A service "
        "can name its own in its own settings, which is what a single geofenced "
        "service wants without routing the other 34 through it.",
        visible=False,
    ),
    Setting(
        "proxy_downloads",
        "Send downloads through the proxy",
        "bool",
        default=True,
        help="Only read when a proxy is set. On, the UniDL run uses it too. Off, "
        "UniDL goes direct - which is what you want when the proxy is slow and "
        "the CDN is open worldwide, and wrong when the manifest host is the thing "
        "that is geofenced, because UniDL fetches the manifest again itself. "
        "unidl's own reads - the manifest it inspects for DRM init data, the "
        "playlists, and the licence request - stay on the proxy either way, so the "
        "keys always come from the same region the session was opened in.",
        visible=False,
    ),
    Setting(
        "download_dir",
        "Where files are saved",
        "text",
        default="",
        help="Downloads and live recordings both land here. Empty means the folder "
        "configured in unidl.yaml, which is what the row shows when nothing is set. "
        "The folder is created if it does not exist; ~ works.",
        visible=False,
    ),
    Setting(
        "debug",
        "Debug mode",
        "bool",
        default=False,
        help="Verbose logging, keeps temp files, writes stream metadata and a session log. "
        "It also enables the explicit Ctrl+R service-code reload on the main screen; "
        "nothing is watched or reloaded automatically.",
        visible=False,
    ),
    Setting(
        "confirm_batch",
        "Confirm before batch downloads",
        "bool",
        default=False,
        help="Ask once before processing a multi-episode selection.",
        visible=False,
    ),
    Setting(
        "retries",
        "Segment retries",
        "int",
        default=5,
        help="How many times a failed segment is retried before the track fails.",
        visible=False,
    ),
    Setting(
        "http_timeout",
        "HTTP timeout (seconds)",
        "int",
        default=30,
        help="Timeout for each native downloader HTTP request.",
        visible=False,
    ),
    Setting(
        "max_speed",
        "Speed limit",
        "text",
        default="",
        help="Cap segment download speed, e.g. 15M, 100K, 2Mbps. Empty means unlimited.",
        visible=False,
    ),
    _choice(
        "muxer",
        "Muxer",
        [("auto", "Auto"), ("ffmpeg", "FFmpeg"), ("mkvmerge", "mkvmerge")],
        "auto",
        "Which tool writes the final container. Auto prefers mkvmerge for MKV when it recognises the inputs. Per-service Container still chooses mkv vs mp4.",
        visible=False,
    ),
    _choice(
        "segment_downloader",
        "Segment downloader",
        [("python", "Built-in Python"), ("aria2c", "aria2c")],
        "python",
        "The native engine's segment backend. aria2c is used only when chosen and installed.",
        visible=False,
    ),
    Setting(
        "resume_parts",
        "Reuse downloaded segments",
        "bool",
        default=True,
        help="On, verified parts in tmp are reused after a pause or a retry. Off starts every track from scratch.",
        visible=False,
    ),
    Setting(
        "check_segments_count",
        "Verify segment count",
        "bool",
        default=True,
        help="Fail a track when the number of downloaded parts does not match the manifest.",
        visible=False,
    ),
    Setting(
        "keep_temp",
        "Keep temporary files",
        "bool",
        default=False,
        help="Keep segment caches after a successful download. Debug mode also keeps them.",
        visible=False,
    ),
    Setting(
        "delete_temp_after_done",
        "Delete temp after success",
        "bool",
        default=True,
        help="Remove temporary segment directories when a title finishes successfully.",
        visible=False,
    ),
    Setting(
        "auto_subtitle_fix",
        "Clean converted subtitles",
        "bool",
        default=True,
        help="De-duplicate and tidy text cues while converting subtitles.",
        visible=False,
    ),
    Setting(
        "live_real_time_merge",
        "Merge live segments while recording",
        "bool",
        default=True,
        help="Append live segments to the output as they arrive instead of waiting until stop.",
        visible=False,
    ),
    Setting(
        "live_keep_segments",
        "Keep live segment files",
        "bool",
        default=False,
        help="Keep per-segment live files in tmp after recording.",
        visible=False,
    ),
    Setting(
        "live_pipe_mux",
        "Live pipe mux",
        "bool",
        default=True,
        help="Mux live audio/video after recording. Ignored for audio-only titles.",
        visible=False,
    ),
    Setting(
        "name_template_episode",
        "Episode file name",
        "text",
        default=naming.TITLE_TEMPLATES["episode"],
        help="The shape of an episode's name. {title} {season_episode} {episode_name} "
        "{season} {episode} {year}, and a ? on a field - {episode_name?} - drops it "
        "and one separator when it is empty. The resolution, platform and tag are "
        "added afterwards, by the release template below.",
        visible=False,
    ),
    Setting(
        "name_template_movie",
        "Film file name",
        "text",
        default=naming.TITLE_TEMPLATES["movie"],
        help="The same fields, for something with no episode number: {title} {year} "
        "{episode_name}. A ? on a field drops it and one separator when it is empty.",
        visible=False,
    ),
    Setting(
        "release_template",
        "What is added after the name",
        "text",
        default=naming.RELEASE_TEMPLATE,
        help="{quality} {platform} {source} {audio} {audio_channels} {audio_full} "
        "{atmos} {video} {range} {tag}. Audio and video fields come from the tracks "
        "finally selected for download: for example AAC2.0, DDP5.1.Atmos or H.265. This is where the order "
        "lives; {source} is always WEB-DL. Only films and episodes get this half.",
        visible=False,
    ),
    Setting(
        "release_tag",
        "Release tag",
        "text",
        default="",
        help="What goes after the dash at the end of a file name - "
        "Show.S01E01.1080p.SERVICE.WEB-DL-YOURTAG. Yours to choose, and left off "
        "entirely when empty. Films and episodes only.",
        visible=False,
    ),
    Setting(
        "justwatch_manager",
        "JustWatch search",
        "action",
        default="",
        picker="justwatch",
        help=(
            "Set the one catalogue used to find titles and the separate list of "
            "countries checked for streaming availability. Both choices remain "
            "available from the JustWatch search and results screens too."
        ),
    ),
    Setting(
        "interface_manager",
        "Interface & diagnostics",
        "action",
        default="",
        picker="interface",
        help=(
            "Choose the colour theme, interface language and diagnostic logging. "
            "Debug mode also enables the explicit service-code reload shortcut; "
            "it never watches files."
        ),
    ),
    # Two region settings, and they answer different questions. Searching for a
    # title happens in one country's catalogue - that is what decides which titles
    # come back and what they are called. Asking who carries a title happens in as
    # many countries as you like, one request each. Sharing a single setting for
    # both meant widening the second silently changed the first.
    Setting(
        "justwatch_search_region",
        "Title search region",
        "text",
        default=_JUSTWATCH_DEFAULT_REGIONS[0],
        help="Whose catalogue the title list comes from - one ISO country code. "
        "It decides which titles a search finds and what they are called, not "
        "where they can be watched. Editable from the search screen too.",
        visible=False,
    ),
    Setting(
        "justwatch_regions",
        "Availability regions",
        "text",
        default=",".join(_JUSTWATCH_DEFAULT_REGIONS),
        help="Which regions are asked who carries a title, in order. Comma "
        "separated ISO country codes. Each one is a separate request, so a long "
        "list is a slow lookup. Editable from the results screen too.",
        visible=False,
    ),
]


#: The first spelling shipped as the default and may already be persisted in a
#: settings file. Its bare ``trick`` alternative also matched ordinary title
#: text such as ``Strickland`` when UniDL considered a stream URL.
LEGACY_DROP_VIDEO_PATTERN = "trick|thumbnail|image"
DEFAULT_DROP_VIDEO_PATTERN = (
    r"(?:^|[^a-z])(?:trick(?:play|mode)?|thumbnails?|images?)(?:[^a-z]|$)"
)


def normalize_drop_video_pattern(value: object) -> str | None:
    """Return the video-drop regex, upgrading the unsafe original default."""
    pattern = str(value or "").strip()
    if not pattern:
        return None
    if pattern == LEGACY_DROP_VIDEO_PATTERN:
        return DEFAULT_DROP_VIDEO_PATTERN
    return pattern


#: Shared track vocabulary, identical for every service.
TRACK_SETTINGS: list[Setting] = [
    _choice(
        "track_mode",
        "Track selection",
        [("interactive", "Interactive - pick from the ladder"), ("auto", "Automatic - apply rules below")],
        "interactive",
        "Whether to show the track picker or select silently using these rules.",
    ),
    _choice(
        "video_quality",
        "Video quality",
        [
            ("best", "Best available"),
            ("4320", "4320p / 8K"),
            ("2160", "2160p / 4K"),
            ("1440", "1440p"),
            ("1080", "1080p"),
            ("720", "720p"),
            ("480", "480p"),
            ("worst", "Worst available"),
        ],
        "best",
    ),
    _choice(
        "video_codec",
        "Video codec",
        [("any", "Any"), ("h264", "H.264"), ("h265", "H.265 / HEVC"), ("av1", "AV1"), ("vp9", "VP9")],
        "any",
    ),
    _choice(
        "video_range",
        "Dynamic range",
        [("any", "Any"), ("sdr", "SDR"), ("hdr10", "HDR10"), ("hdr10+", "HDR10+"), ("hlg", "HLG"), ("dv", "Dolby Vision")],
        "any",
    ),
    _choice(
        "audio_codec",
        "Audio codec",
        [("any", "Any"), ("aac", "AAC"), ("ac3", "AC-3"), ("eac3", "E-AC-3"), ("atmos", "Atmos")],
        "any",
    ),
    _choice(
        "audio_channels",
        "Audio channels",
        [("any", "Any"), ("2", "Stereo"), ("6", "5.1"), ("8", "7.1")],
        "any",
    ),
    Setting("audio_langs", "Audio languages", "text", default="", help="Comma separated, e.g. en,es. Empty = best only."),
    Setting("sub_langs", "Subtitle languages", "text", default="all", help="Comma separated, 'all', or empty to skip."),
    Setting(
        "drop_video",
        "Ignore video tracks matching",
        "text",
        default=DEFAULT_DROP_VIDEO_PATTERN,
        help=(
            "Regex. Keeps trick-play and thumbnail ladders out of quality selection "
            "without matching those letters inside an ordinary title."
        ),
    ),
    _choice("sub_format", "Subtitle format", [("srt", "SRT"), ("vtt", "WebVTT"), ("raw", "Original")], "srt"),
    _choice("mux_format", "Container", [("mkv", "MKV"), ("mp4", "MP4")], "mkv"),
    Setting(
        "embed_chapters",
        "Embed chapters in the final file",
        "bool",
        default=True,
        help=(
            "When the service provides chapter metadata, write it into the final "
            "MKV/MP4 container. Turning this off keeps chapters in the report, "
            "saved command metadata and export, but does not mux them into media."
        ),
    ),
    Setting(
        "embed_lyrics",
        "Embed lyrics in the final audio file",
        "bool",
        default=True,
        help=(
            "When a service provides lyrics, write a plain-text lyrics tag into "
            "compatible audio containers. Timed lyrics remain available for display "
            "and export when this is turned off."
        ),
    ),
    _audio_format_setting(),
    Setting("workers", "Download workers", "int", default=16),
    Setting("concurrent_tracks", "Download tracks in parallel", "bool", default=True),
]


class SettingsStore:
    """Persists setting values per service under one JSON file.

    Every change is written the moment it is made, and written in a way that
    survives the process not coming back: a temporary file replaced into place, and
    the previous file kept beside it. The three ways a settings file gets lost are
    all handled here rather than accepted:

    * **a half-written file.** ``write_text`` truncates first, so being killed
      mid-write leaves invalid JSON - and the loader used to answer that by
      starting from an empty dict, which reads as "everything went back to
      default". Now the write is atomic and the last good file is a fallback.
    * **a second writer.** The store holds the whole file in memory and rewrites
      it, so anything another process wrote in between was overwritten. Now disk is
      re-read and merged at save time, with this process's own changes on top.
    * **a write that fails.** A read-only home or a full disk raised out of a
      keypress handler. Now it is recorded in :attr:`problem` so the interface can
      say the setting was not kept, which is the one thing worse to get wrong
      silently.
    """

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict[str, Any]] = {}
        #: values changed by this instance since its last load/save. Keeping the
        #: delta separate prevents a stale in-memory value from overwriting a
        #: different key another process changed.
        self._dirty: set[tuple[str, str]] = set()
        #: keys removed since the last save, so a merge does not resurrect them
        self._removed: set[tuple[str, str]] = set()
        #: empty when the file is healthy; a sentence for the interface when not
        self.problem = ""
        private_file(self.path)
        private_file(self.backup_path)
        self.load()

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def load(self) -> None:
        self.problem = ""
        self._data = {}
        self._dirty.clear()
        self._removed.clear()
        if not self.path.exists():
            # No file is not the same as a broken file. Deleting settings.json is how
            # a reset is done - by a person and by a check - so the backup must not
            # quietly undo it. It exists for the case below.
            return
        current = self._read(self.path)
        if current is not None:
            self._data = current
            return
        previous = self._read(self.backup_path)
        if previous:
            self._data = previous
            self.problem = (
                "the settings file could not be read, so the previous one was used"
            )
            return
        self.problem = f"{self.path.name} could not be read and there is no backup"

    @staticmethod
    def _read(path: Path) -> dict[str, dict[str, Any]] | None:
        """The file as a dict, or None when it is missing or not readable as one."""
        try:
            if not path.exists():
                return {}
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        return {k: dict(v) for k, v in raw.items() if isinstance(v, dict)}

    def save(self) -> None:
        try:
            # The merge and the replace are one transaction. Re-reading before a
            # lock only narrows a race; re-reading *under* the lock closes it.
            with locked_path(self.path):
                current = self._read(self.path)
                base = current
                if base is None:
                    # Preserve the same recovery used by load(): a damaged current
                    # file must not turn the next one-key edit into a reset.
                    base = self._read(self.backup_path) or self._data
                merged = {service: dict(values) for service, values in base.items()}
                for service_id, key in self._dirty:
                    if key in self._data.get(service_id, {}):
                        merged.setdefault(service_id, {})[key] = self._data[service_id][key]
                for service_id, key in self._removed:
                    merged.get(service_id, {}).pop(key, None)

                text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
                if current is not None and self.path.is_file():
                    # Keep the last good bytes, atomically and owner-only. A damaged
                    # current file is never copied over the usable backup.
                    previous = self.path.read_text("utf-8")
                    atomic_write_text(self.backup_path, previous)
                atomic_write_text(self.path, text)
        except OSError as exc:
            self.problem = f"could not save settings to {self.path}: {exc}"
            return
        self.problem = ""
        self._data = merged
        self._dirty.clear()
        self._removed.clear()

    def values_for(self, service_id: str) -> dict[str, Any]:
        return dict(self._data.get(service_id, {}))

    def put(self, service_id: str, key: str, value: Any) -> None:
        self._data.setdefault(service_id, {})[key] = value
        self._dirty.add((service_id, key))
        self._removed.discard((service_id, key))

    def clear(self, service_id: str, key: str) -> None:
        self._data.get(service_id, {}).pop(key, None)
        self._dirty.discard((service_id, key))
        self._removed.add((service_id, key))

    def rename_cdm_device_references(self, old_name: str, new_name: str) -> int:
        """Update saved *service* CDM choices after a managed endpoint rename.

        A remote CDM is named in several independent places: the app-wide picker,
        the optional YAML ``cdm.by_service`` map, and a service's own
        ``cdm_widevine`` / ``cdm_playready`` setting.  The resource manager owns
        the endpoint name, so it must not leave the last kind pointing at a name
        that no longer exists.

        This intentionally handles only direct device-choice settings.  ``cdm``
        quality rules have their own editor and syntax, while ``cdm_device`` is
        the app-wide choice and is updated by ``UnidlApp.set_device``.  On delete
        the value is kept as an explicit empty override rather than removed: that
        prevents a renamed service from falling back to a stale legacy namespace.
        """
        old = str(old_name or "").strip().casefold()
        if not old:
            return 0
        replacement = str(new_name or "").strip()
        changed = 0
        for service_id, values in tuple(self._data.items()):
            for key, value in tuple(values.items()):
                is_service_cdm_choice = (
                    key == "cdm_any"
                    or (
                        key.startswith("cdm_")
                        and key not in {"cdm_device", "cdm_rules"}
                    )
                )
                if not is_service_cdm_choice:
                    continue
                if str(value or "").strip().casefold() != old:
                    continue
                self.put(service_id, key, replacement)
                changed += 1
        if changed:
            self.save()
        return changed


class Settings:
    """Live view of one service's settings. Reads and writes go to the store."""

    def __init__(
        self,
        service_id: str,
        specs: list[Setting],
        store: SettingsStore,
        overrides: dict[str, Any] | None = None,
        parent: Settings | None = None,
        legacy_ids: tuple[str, ...] | list[str] = (),
    ):
        self.service_id = service_id
        self.legacy_ids = tuple(
            key for key in (str(value).strip() for value in legacy_ids) if key and key != service_id
        )
        self.specs = specs
        keys = [spec.key for spec in specs]
        repeated = sorted({key for key in keys if keys.count(key) > 1})
        if repeated:
            raise ValueError(
                f"{service_id}: duplicate setting key(s): {', '.join(repeated)}"
            )
        self.spec_by_key = {s.key: s for s in specs}
        self._store = store
        self._overrides = dict(overrides or {})
        #: app-wide settings, consulted for keys this scope does not define
        self.parent = parent

    def replace_specs(self, specs: list[Setting]) -> None:
        """Replace dynamic declarations without discarding stored values.

        Vault names come from YAML, so saving a resource from the TUI changes the
        option lists while this same global Settings object is still referenced by
        open screens and service scopes. Updating its declarations in place keeps
        those references coherent.
        """
        keys = [spec.key for spec in specs]
        repeated = sorted({key for key in keys if keys.count(key) > 1})
        if repeated:
            raise ValueError(
                f"{self.service_id}: duplicate setting key(s): {', '.join(repeated)}"
            )
        self.specs = specs
        self.spec_by_key = {spec.key: spec for spec in specs}

    # ------------------------------------------------------------------ read
    def get(self, key: str, fallback: Any = None) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        stored = self._store.values_for(self.service_id)
        if key not in stored:
            for legacy_id in self.legacy_ids:
                legacy = self._store.values_for(legacy_id)
                if key in legacy:
                    stored = legacy
                    break
        if key in stored:
            spec = self.spec_by_key.get(key)
            return spec.coerce(stored[key]) if spec else stored[key]
        spec = self.spec_by_key.get(key)
        if spec is not None:
            return spec.default
        if self.parent is not None and (key in self.parent.spec_by_key or key in self.parent._overrides):
            return self.parent.get(key, fallback)
        return fallback

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def as_dict(self) -> dict[str, Any]:
        return {s.key: self.get(s.key) for s in self.specs}

    def label_for(self, key: str) -> str:
        spec = self.spec_by_key.get(key)
        if spec is None:
            return str(self.get(key))
        value = self.get(key)
        if spec.kind == "multi":
            chosen = tuple(value or ())
            labels = [
                option.display()
                for option in spec.options
                if option.value in chosen
            ]
            return ", ".join(labels) if labels else "none"
        for option in spec.options:
            if option.value == value:
                return option.display()
        return str(value)

    # ----------------------------------------------------------------- write
    def set(self, key: str, value: Any, *, persist: bool = True) -> bool:
        """Store a value. Returns True when the change should reset the session."""
        spec = self.spec_by_key.get(key)
        coerced = spec.coerce(value) if spec else value
        self._overrides.pop(key, None)
        self._store.put(self.service_id, key, coerced)
        if persist:
            self._store.save()
        return bool(spec and spec.resets_session)

    def override(self, key: str, value: Any) -> None:
        """Set a value for this run only (used by non-interactive CLI flags)."""
        spec = self.spec_by_key.get(key)
        self._overrides[key] = spec.coerce(value) if spec else value

    def inherited(self, key: str, fallback: Any = None) -> Any:
        """This scope's value if it states one, otherwise the app-wide value.

        Different from :meth:`get`, which stops as soon as this scope *declares*
        the key - which is right for a normal setting and wrong for one that can
        mean "no opinion here". An empty value is how a per-service override says
        that, and it has to fall through rather than resolve to "".
        """
        value = self.get(key)
        if value not in (None, ""):
            return value
        if self.parent is not None:
            return self.parent.get(key, fallback)
        return fallback

    # ------------------------------------------------------------- grouping
    def service_specs(self) -> list[Setting]:
        """This service's own settings.

        Compared by identity, not by key: a service may declare its own version
        of an app-wide key - a DRM choice that applies to it alone - and that is
        a different ``Setting`` object with its own options. Excluding by key
        would have hidden it, leaving a setting that could be read and never
        changed.
        """
        shared = {id(s) for s in TRACK_SETTINGS} | {id(s) for s in GLOBAL_SETTINGS}
        return [s for s in self.specs if id(s) not in shared]

    def track_specs(self) -> list[Setting]:
        track_keys = {s.key for s in TRACK_SETTINGS}
        return [s for s in self.specs if s.key in track_keys]


def global_settings(
    store: SettingsStore,
    overrides: dict[str, Any] | None = None,
    config: Any = None,
) -> Settings:
    """The app-wide settings scope."""
    dynamic = {setting.key: setting for setting in _vault_target_settings(config)}
    specs = [dynamic.get(setting.key, setting) for setting in GLOBAL_SETTINGS]
    return Settings(GLOBAL_SCOPE, specs, store, overrides)
