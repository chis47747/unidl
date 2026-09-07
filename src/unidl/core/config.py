"""Static configuration: one YAML file for paths, CDM devices and credentials.

Anything a human sets once lives here. Anything a human flips while browsing
(quality, codec, profile) lives in :mod:`unidl.core.settings` instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .credentials import Credential
from .secureio import (
    atomic_write_text,
    locked_path,
    private_directory,
    private_file,
    secure_tree,
)

DEFAULT_HOME = Path(os.environ.get("UNIDL_HOME", Path.home() / ".unidl"))
CONFIG_NAME = "unidl.yaml"
# This used to be the implicit fallback. It is kept only as a denied path so an
# old launcher or an agent passing it explicitly gets a useful error instead of
# quietly reviving a second configuration source. ``~/.unidl`` remains the
# default *state* home; only the YAML file there is retired.
LEGACY_CONFIG_PATH = Path.home() / ".unidl" / CONFIG_NAME

# Canonical service id -> ids used by earlier releases. This compatibility table
# lets direct callers using a renamed id read the same namespace as the service.
RENAMED_SERVICE_IDS: dict[str, tuple[str, ...]] = {
    "appletv": ("apple",),
    "paramountplus": ("paramount",),
}


def _service_keys(service_id: str, legacy_ids: tuple[str, ...] | list[str] = ()) -> tuple[str, ...]:
    """Return the canonical service id followed by accepted legacy ids.

    Service ids are also configuration namespaces.  A renamed service must be
    able to read the namespace it used before the rename, while every new write
    goes to the canonical id.  Keeping this small helper here lets Config apply
    that rule consistently to credentials, CDM pins and service options.
    """
    found: list[str] = []
    aliases = RENAMED_SERVICE_IDS.get(str(service_id or "").strip().lower(), ())
    for value in (service_id, *legacy_ids, *aliases):
        key = str(value or "").strip()
        if key and key not in found:
            found.append(key)
    return tuple(found)


def _service_entry(
    section: Any, service_id: str, legacy_ids: tuple[str, ...] | list[str] = ()
) -> Any:
    if not isinstance(section, dict):
        return None
    for key in _service_keys(service_id, legacy_ids):
        if key in section:
            return section[key]
    return None


def _expand(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _source_checkout_root() -> Path | None:
    """The checkout this imported package belongs to, when running from source."""
    root = Path(__file__).resolve().parents[3]
    if (root / "pyproject.toml").is_file() and (
        root / "src" / "unidl" / "core" / "config.py"
    ).is_file():
        return root
    return None


def default_config_path() -> Path:
    """The one implicit config path, independent of the process working directory.

    An editable/source checkout always owns its root ``unidl.yaml``. A packaged
    installation has no checkout to own one, so its explicit local convention is
    the current directory. Neither route ever consults ``~/.unidl/unidl.yaml``.
    """
    root = _source_checkout_root()
    return (root if root is not None else Path.cwd()) / CONFIG_NAME


def _absolute(path: Path) -> Path:
    """Lexically absolute without following a symlink to a permitted target."""
    return Path(os.path.abspath(path))


def _resolved(path: Path) -> Path:
    """Absolute path with existing symlinks resolved for policy checks."""
    try:
        return path.resolve(strict=False)
    except OSError:
        # A broken link must still be compared lexically; Config.load will
        # report it as a missing config after this policy check.
        return _absolute(path)


def _reject_legacy_config(path: Path) -> None:
    # Check both spellings. The lexical comparison keeps a normal explicit
    # path cheap, while the resolved comparison closes a symlink escape hatch
    # that could otherwise revive the retired file indirectly.
    if _absolute(path) == _absolute(LEGACY_CONFIG_PATH) or _resolved(path) == _resolved(LEGACY_CONFIG_PATH):
        raise ValueError(
            f"{LEGACY_CONFIG_PATH} is a retired config location and is never read; "
            f"use {default_config_path()} or pass --config with another path"
        )


def _quote(value: str) -> str:
    """One scalar, quoted the way YAML needs it.

    Through ``yaml.safe_dump`` rather than by hand: a password is arbitrary text
    and the rules for when it needs quoting - leading ``@``, a trailing colon, a
    string that looks like a number or like ``yes`` - are not worth reimplementing.
    """
    dumped = yaml.safe_dump(str(value), default_flow_style=True, allow_unicode=True).strip()
    return dumped.removesuffix("...").strip() or "''"


def _block(service_id: str, slot: str, values: dict[str, Any], indent: str) -> list[str]:
    """The lines for one service's credentials, indented to sit under ``credentials:``."""
    step = indent + "  "
    out = [f"{indent}{service_id}:", f"{step}{slot}:"]
    out.extend(f"{step}  {name}: {_quote(value)}" for name, value in values.items() if value)
    return out


def _is_top_level(line: str) -> bool:
    """A line that starts a new top-level key, so the block before it has ended."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    return not line[:1].isspace()


def _section(lines: list[str]) -> tuple[int, int] | None:
    """Where the top-level ``credentials:`` block starts and ends."""
    head = next(
        (index for index, line in enumerate(lines) if line.rstrip() == "credentials:"),
        None,
    )
    if head is None:
        return None
    end = len(lines)
    for index in range(head + 1, len(lines)):
        if _is_top_level(lines[index]):
            end = index
            break
    return head, end


def _top_level_sections(lines: list[str], names: set[str]) -> list[tuple[int, int]]:
    """Return spans for named top-level YAML sections.

    Resource management edits one owned section at a time. Re-serialising the
    whole document would erase the comments people keep beside credentials and
    service settings, so this small line scanner leaves every unrelated section
    byte-for-byte intact. A comment immediately before the next top-level key is
    considered part of the preceding section, which is harmless: the managed
    section is the only one replaced.
    """
    def mapping_key(line: str) -> str:
        """A top-level mapping key, never an unindented YAML list item."""
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")) or line[:1].isspace():
            return ""
        key, separator, _rest = stripped.partition(":")
        return key.strip() if separator else ""

    starts: list[int] = []
    for index, line in enumerate(lines):
        key = mapping_key(line)
        if key in names:
            starts.append(index)
    if not starts:
        return []
    top_level = [index for index, line in enumerate(lines) if mapping_key(line)]
    spans: list[tuple[int, int]] = []
    for start in starts:
        next_start = next((index for index in top_level if index > start), len(lines))
        spans.append((start, next_start))
    return spans


def _replace_top_level_section(
    text: str,
    name: str,
    value: Any,
    *,
    aliases: tuple[str, ...] = (),
) -> str:
    """Replace one managed YAML section while retaining all other sections."""
    lines = text.splitlines()
    encoded = yaml.safe_dump(
        {name: value},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).rstrip()
    replacement = encoded.splitlines()
    spans = _top_level_sections(lines, {name, *aliases})
    if not spans:
        if lines and any(line.strip() for line in lines):
            lines.extend(["", *replacement])
        else:
            lines = replacement
        return "\n".join(lines).rstrip() + "\n"

    # Replace the first spelling and remove duplicate canonical/legacy sections.
    # Having both ``remote_cdm`` and ``remote_cdms`` present made the old loader
    # silently choose one; one managed section is much safer after an edit.
    out: list[str] = []
    cursor = 0
    for index, (start, end) in enumerate(spans):
        out.extend(lines[cursor:start])
        if index == 0:
            out.extend(replacement)
        cursor = end
    out.extend(lines[cursor:])
    return "\n".join(out).rstrip() + "\n"


def _entry(lines: list[str], start: int, end: int, name: str) -> tuple[int, int, str] | None:
    """``(first, last, indent)`` for ``name:`` and everything nested under it."""
    for index in range(start, end):
        line = lines[index]
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = line[: len(line) - len(line.lstrip())]
        if line.strip().startswith(f"{name}:"):
            stop = end
            for inner in range(index + 1, end):
                nested = lines[inner]
                if not nested.strip() or nested.strip().startswith("#"):
                    continue
                if len(nested) - len(nested.lstrip()) <= len(indent):
                    stop = inner
                    break
            return index, stop, indent
    return None


def remove_credential(text: str, service_id: str, slot: str = "") -> str:
    """Return ``text`` with one saved login taken out, and nothing else changed.

    The mirror of :func:`write_credential`, and the same reasoning: edited as text,
    so the rest of a hand-written file - comments included - is left alone.

    A named ``slot`` takes out just that slot, unless it is the only one under the
    service, in which case the service entry goes with it - an empty
    ``service:`` key left behind is not a login, it is litter. An unnamed slot
    removes the service entry whole. An empty ``credentials:`` heading is kept:
    the comments under it are the user's, and YAML reads it as nothing either way.
    """
    lines = text.splitlines()
    span = _section(lines)
    if span is None:
        return text
    head, end = span
    found = _entry(lines, head + 1, end, service_id)
    if found is None:
        return text
    start, stop, indent = found

    cut = (start, stop)
    if slot:
        inner = _entry(lines, start + 1, stop, slot)
        if inner is None:
            return text
        first, last, deeper = inner
        siblings = sum(
            1
            for index in range(start + 1, stop)
            if lines[index].strip()
            and not lines[index].strip().startswith("#")
            and lines[index][: len(lines[index]) - len(lines[index].lstrip())] == deeper
        )
        if siblings > 1:
            cut = (first, last)

    kept = [*lines[: cut[0]], *lines[cut[1] :]]
    return ("\n".join(kept) + "\n") if kept else ""


def write_credential(text: str, service_id: str, slot: str, values: dict[str, Any]) -> str:
    """Return ``text`` with one service's credentials replaced or appended.

    Edited as text, not re-serialised: this file is hand-written and full of
    comments, and ``yaml.safe_dump`` of a parsed copy would hand it back stripped
    of every one of them. So the only bytes that change are the ones inside the
    block being written.

    The service's whole block is replaced rather than merged field by field. That
    only happens when the login was incomplete - a complete one is never asked
    about - so there is nothing in there worth keeping that the form has not been
    pre-filled with.
    """
    lines = text.splitlines()
    span = _section(lines)
    if span is None:
        # No section, or one written inline (`credentials: {...}`), which is not a
        # shape to edit blind - either way the answer is a new block at the end.
        tail = [*lines]
        while tail and not tail[-1].strip():
            tail.pop()
        tail.extend(["", "credentials:", *_block(service_id, slot, values, "  ")])
        return "\n".join(tail) + "\n"

    head, end = span
    found = _entry(lines, head + 1, end, service_id)
    if found is None:
        body = _block(service_id, slot, values, "  ")
        cut = end
        while cut > head + 1 and not lines[cut - 1].strip():
            cut -= 1  # keep trailing blank lines after the section, not inside it
        return "\n".join([*lines[:cut], *body, *lines[cut:]]) + "\n"

    start, stop, indent = found
    body = _block(service_id, slot, values, indent)
    return "\n".join([*lines[:start], *body, *lines[stop:]]) + "\n"


@dataclass
class Paths:
    home: Path = DEFAULT_HOME
    #: Where finished files land, one folder per service inside it. Named
    #: ``unidl_downloads`` and put in the home directory rather than inside
    #: ``Downloads``: it is a folder you go to on purpose, not one thing among a
    #: hundred browser downloads.
    downloads: Path = field(default_factory=lambda: Path.home() / "unidl_downloads")
    cache: Path = field(default_factory=lambda: DEFAULT_HOME / "cache")
    temp: Path = field(default_factory=lambda: DEFAULT_HOME / "tmp")
    logs: Path = field(default_factory=lambda: DEFAULT_HOME / "logs")
    #: exported UniDL commands, mirroring the old download_commands/ layout
    commands: Path = field(default_factory=lambda: DEFAULT_HOME / "commands")
    #: portable exports: a resolved title's manifest, tracks and keys as one JSON
    #: file. Its own folder rather than a corner of ``commands`` because these are
    #: read back in - a file you drop in here appears on the import screen - and
    #: because they hold content keys, which a folder of shell commands does not.
    exports: Path = field(default_factory=lambda: DEFAULT_HOME / "exports")
    #: external helper binaries, assets and modules
    helpers: Path = field(default_factory=lambda: DEFAULT_HOME / "helpers")
    #: CDM device files, split by system so .wvd and .prd cannot be confused
    cdm: Path = field(default_factory=lambda: DEFAULT_HOME / "cdm")
    #: exported browser cookies, one file or folder per service
    cookies: Path = field(default_factory=lambda: DEFAULT_HOME / "cookies")
    #: what a sign-in produced: tokens, session keys, device ids, one file per
    #: service. Its own directory rather than a corner of ``cache`` because these
    #: are the files you open when a login misbehaves - and because a cache is
    #: something you delete without thinking, which for these is a sign-out.
    #: Point it somewhere visible; ``~/.unidl`` is hidden on macOS.
    tokens: Path = field(default_factory=lambda: DEFAULT_HOME / "tokens")
    #: Subtitles a service fetches itself, one folder per service. Some services
    #: hand UniDL a sidecar file to mux rather than a track in the manifest, and
    #: those files were living in a corner of ``cache`` - which is a folder people
    #: delete. These are inputs to a download, so they get their own place.
    subtitles: Path = field(default_factory=lambda: DEFAULT_HOME / "subtitles")
    #: local content key vault
    keys_db: Path = field(default_factory=lambda: DEFAULT_HOME / "keys.db")

    def ensure(self) -> None:
        for path in (
            self.home,
            self.downloads,
            self.cache,
            self.temp,
            self.logs,
            self.commands,
            self.exports,
            self.cookies,
            self.tokens,
            self.subtitles,
        ):
            path.mkdir(parents=True, exist_ok=True)
        # Tokens are credentials, not disposable cache. Tighten both a fresh
        # directory and any existing per-service state before anything reads it.
        from .cache import secure_token_tree  # local: permission work belongs to ensure()

        secure_token_tree(self.tokens)

        # one folder per registered DRM system: a .wvd in the playready folder is
        # then obviously in the wrong place, which is the point. Read from the
        # registry, so a system added later gets its folder without an edit here.
        from .drm import all_systems  # noqa: PLC0415 - optional at import time

        for system in all_systems():
            (self.cdm / system.id).mkdir(parents=True, exist_ok=True)

        # These trees can all carry credentials, signed URLs, device private
        # material or content keys. Tighten existing files as well as new ones so
        # upgrading fixes an old permissive umask rather than only future writes.
        for root in (self.logs, self.commands, self.exports, self.cookies, self.cdm):
            secure_tree(root)
        if self.keys_db.parent != self.home:
            private_directory(self.keys_db.parent)
        for path in (
            self.keys_db,
            self.keys_db.with_name(self.keys_db.name + "-wal"),
            self.keys_db.with_name(self.keys_db.name + "-shm"),
        ):
            private_file(path)


class Config:
    def __init__(self, data: dict[str, Any] | None = None, source: Path | None = None):
        self.raw = data or {}
        self.source = source
        self.paths = self._read_paths()
        self._devices = self._read_devices()
        #: chosen in the UI rather than written in the file. The YAML stays the
        #: "set it once" surface; this is the "flipped while browsing" surface,
        #: and it is persisted in settings.json, not here.
        self.device_override: str = ""

    def _resolve_path(self, value: str | Path) -> Path:
        """Expand a configured path relative to the YAML that owns it.

        Relative paths make a checked-in UniDL configuration portable between
        clones. Absolute paths and ``~`` keep their normal meaning; when a
        caller constructs an in-memory config there is no YAML base, so the
        process working directory remains the natural fallback.
        """
        path = _expand(value)
        if path.is_absolute() or self.source is None:
            return path
        return self.source.parent.resolve() / path

    # ------------------------------------------------------------- loading
    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        candidate = _expand(path) if path is not None else default_config_path()
        _reject_legacy_config(candidate)
        if candidate.exists():
            try:
                data = yaml.safe_load(candidate.read_text("utf-8")) or {}
            except yaml.YAMLError as exc:
                raise ValueError(f"{candidate} is not valid YAML: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"{candidate} must contain a mapping at the top level")
            return cls(data, candidate)
        # An explicit path remains authoritative even before its first write.
        # Dropping it here made a later sign-in save into the canonical project
        # file instead of the path supplied with --config.
        return cls({}, candidate if path is not None else None)

    def reload(self) -> None:
        """Reload the YAML in place, preserving the device chosen in the UI.

        Screens, services and the engine all hold this ``Config`` instance, so
        replacing it would leave half the application on the old remote-CDM
        list. Updating the instance means a remote entry edited while the CDM
        picker is open becomes available everywhere after one refresh.
        """
        if self.source is None:
            raise ValueError("no unidl.yaml is configured")
        selected = self.device_override
        refreshed = type(self).load(self.source)
        self.raw = refreshed.raw
        self.source = refreshed.source
        self.paths = refreshed.paths
        self._devices = refreshed._devices
        self.device_override = selected

    def save_managed_section(
        self,
        name: str,
        value: Any,
        *,
        aliases: tuple[str, ...] = (),
    ) -> Path:
        """Persist one UI-managed top-level section and refresh this instance.

        CDM endpoints and vault definitions contain secrets, but they are still
        configuration rather than runtime settings. The manager therefore writes
        them to the canonical YAML with the same owner-only, locked, atomic path
        used by credential storage. Only the named section is replaced, so a
        simultaneous edit to service settings or credentials is not overwritten.
        """
        path = self.source or default_config_path()
        _reject_legacy_config(path)
        if path.is_symlink():
            path = Path(os.path.realpath(path))
        with locked_path(path):
            text = path.read_text("utf-8") if path.is_file() else ""
            updated = _replace_top_level_section(
                text,
                str(name).strip(),
                value,
                aliases=tuple(str(alias).strip() for alias in aliases if str(alias).strip()),
            )
            atomic_write_text(path, updated)

        try:
            parsed = yaml.safe_load(updated) or {}
        except yaml.YAMLError as exc:  # pragma: no cover - safe_dump just produced it
            raise ValueError(f"could not read the saved configuration: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("the saved configuration must contain a mapping at the top level")
        self.raw = parsed
        self.source = path
        self.paths = self._read_paths()
        self._devices = self._read_devices()
        return path

    def save_remote_cdms(self, entries: list[dict[str, Any]]) -> Path:
        """Write the canonical remote CDM list from the resource manager."""
        return self.save_managed_section("remote_cdm", entries, aliases=("remote_cdms",))

    def save_vault_specs(self, entries: list[dict[str, Any]]) -> Path:
        """Write the canonical key-vault list from the resource manager."""
        return self.save_managed_section("key_vaults", entries, aliases=("vaults",))

    def save_path_overrides(self, updates: dict[str, str | Path | None]) -> Path:
        """Merge UI-managed output paths into ``paths`` without touching peers.

        The Files & naming screen owns only ordinary output locations.  Runtime
        identity paths (tokens, cookies, CDMs, helpers and the vault) remain
        read-only there, so a typo cannot silently sign the user out or point the
        process at a different device tree.  An empty value removes just that
        override and lets ``home``/the built-in default answer again.

        Read and merge under the same lock.  This is narrower than passing the
        screen's in-memory ``paths`` mapping to :meth:`save_managed_section`: a
        second process may have changed another path since the screen opened, and
        editing ``commands`` must not put its older ``downloads`` value back.
        """
        allowed = {"downloads", "subtitles", "commands", "exports"}
        unknown = sorted(set(updates) - allowed)
        if unknown:
            raise ValueError(f"paths cannot be managed here: {', '.join(unknown)}")

        path = self.source or default_config_path()
        _reject_legacy_config(path)
        if path.is_symlink():
            path = Path(os.path.realpath(path))
        with locked_path(path):
            text = path.read_text("utf-8") if path.is_file() else ""
            try:
                current = yaml.safe_load(text) or {}
            except yaml.YAMLError as exc:
                raise ValueError(f"could not read the configuration: {exc}") from exc
            if not isinstance(current, dict):
                raise ValueError("the configuration must contain a mapping at the top level")
            section = current.get("paths") or {}
            if not isinstance(section, dict):
                raise ValueError("paths must be a mapping")
            merged = dict(section)
            for key, value in updates.items():
                rendered = str(value or "").strip()
                if rendered:
                    merged[key] = rendered
                else:
                    merged.pop(key, None)
            updated = _replace_top_level_section(text, "paths", merged)
            atomic_write_text(path, updated)

        try:
            parsed = yaml.safe_load(updated) or {}
        except yaml.YAMLError as exc:  # pragma: no cover - safe_dump produced it
            raise ValueError(f"could not read the saved configuration: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("the saved configuration must contain a mapping at the top level")
        self.raw = parsed
        self.source = path
        self.paths = self._read_paths()
        self._devices = self._read_devices()
        return path

    def _read_paths(self) -> Paths:
        section = self.raw.get("paths") or {}
        paths = Paths()
        if "home" in section:
            paths.home = self._resolve_path(section["home"])
            paths.cache = paths.home / "cache"
            paths.temp = paths.home / "tmp"
            paths.logs = paths.home / "logs"
            paths.commands = paths.home / "commands"
            paths.exports = paths.home / "exports"
            paths.helpers = paths.home / "helpers"
            paths.keys_db = paths.home / "keys.db"
            paths.cdm = paths.home / "cdm"
            paths.cookies = paths.home / "cookies"
            paths.tokens = paths.home / "tokens"
            paths.subtitles = paths.home / "subtitles"
        for key in (
            "downloads",
            "cache",
            "temp",
            "logs",
            "commands",
            "exports",
            "helpers",
            "keys_db",
            "cdm",
            "cookies",
            "tokens",
            "subtitles",
        ):
            if key in section:
                setattr(paths, key, self._resolve_path(section[key]))
        # Anything else under `paths:` is ignored rather than honoured. The keys
        # that used to point at the standalone-script tree - legacy_scripts,
        # legacy_python, legacy_cache, legacy_commands - are among them, and so is
        # every service branch that read them. A path that quietly did nothing
        # would be worse than one that is simply not read.
        return paths

    def _read_devices(self) -> dict[str, Path]:
        section = self.raw.get("cdm") or {}
        devices = section.get("devices") or {}
        return {str(name): self._resolve_path(path) for name, path in devices.items()}

    # ----------------------------------------------------------------- cdm
    @property
    def devices(self) -> dict[str, Path]:
        return dict(self._devices)

    # ---------------------------------------------------------- remote cdm
    @property
    def remote_cdms(self) -> list[Any]:
        """Every CDM that lives on a server, from the ``remote_cdm`` section.

        Parsed on each call rather than cached: the list is short, and a config
        reloaded while the app is open should take effect the same way a changed
        device path does.
        """
        from .remotecdm import parse_entry

        entries = self.raw.get("remote_cdm") or self.raw.get("remote_cdms") or []
        if isinstance(entries, dict):
            # a mapping of name -> settings is the other way people write this
            entries = [dict(value, name=key) for key, value in entries.items()]
        found = []
        for entry in entries:
            parsed = parse_entry(entry)
            if parsed is not None:
                found.append(parsed)
        return found

    def remote_cdm(self, name: str, *, include_disabled: bool = False) -> Any | None:
        """One enabled remote CDM by name (or any entry for the manager)."""
        wanted = str(name or "").strip().lower()
        if not wanted:
            return None
        for entry in self.remote_cdms:
            if not include_disabled and not getattr(entry, "enabled", True):
                continue
            if wanted in (entry.name.lower(), entry.device_name.lower()):
                return entry
        return None

    def remote_cdm_for(
        self,
        service_id: str,
        requested: str | None = None,
        system: str | None = None,
        *,
        legacy_ids: tuple[str, ...] | list[str] = (),
    ) -> Any | None:
        """The remote CDM the current choice resolves to, if it is one.

        The same name resolution as :meth:`device_for`, asked first: a remote CDM
        has no path, so letting the local resolver run first would either fail
        with "does not exist" or, worse, silently fall through to some other
        device that happens to be on disk.

        ``system`` filters rather than overrides. A remote PlayReady CDM selected
        while Widevine is active is not usable for this request, and saying so
        beats sending a Widevine challenge to a PlayReady server.
        """
        section = self.raw.get("cdm") or {}
        name = (
            requested
            or _service_entry(section.get("by_service"), service_id, legacy_ids)
            or self.device_override
            or section.get("default")
        )
        entry = self.remote_cdm(str(name)) if name else None
        if entry is None:
            return None
        if system and entry.system != system:
            return None
        return entry

    # -------------------------------------------------------------- vaults
    def vault_specs(self) -> list[dict[str, Any]]:
        """The ``key_vaults`` section, normalised to a list of mappings.

        An empty list means "no opinion", which :func:`unidl.core.vaults.build`
        turns into the single local SQLite vault - the behaviour that existed
        before vaults were configurable.
        """
        entries = self.raw.get("key_vaults") or self.raw.get("vaults") or []
        if isinstance(entries, dict):
            entries = [dict(value, name=key) for key, value in entries.items()]
        specs: list[dict[str, Any]] = []
        for entry in entries:
            if isinstance(entry, dict):
                specs.append({str(k).strip().lower(): v for k, v in entry.items()})
        return specs

    def cdm_roots(self) -> list[Path]:
        """Where to look for device files, project folder first.

        ``cdm.search_paths`` lets an existing pile of ``.wvd`` files be picked up
        where it already lives, instead of copying seventy files into the project
        folder to make them selectable.
        """
        roots = [self.paths.cdm]
        section = self.raw.get("cdm") or {}
        extra = section.get("search_paths") or []
        if isinstance(extra, (str, Path)):
            extra = [extra]
        roots.extend(self._resolve_path(path) for path in extra)
        # the directories named devices live in are worth searching too
        for path in self._devices.values():
            if path.parent not in roots:
                roots.append(path.parent)
        return roots

    def _named_or_path(self, name: str) -> Path | None:
        if name in self._devices:
            return self._devices[name]
        # allow a bare path in place of a device name
        candidate = self._resolve_path(name)
        from .cdm import discover, system_of

        if system_of(candidate):
            return candidate
        # a bare stem: look for it among the discovered files
        for device in discover(self.cdm_roots()):
            if device.name == name:
                return device.path
        return None

    def device_for(
        self,
        service_id: str,
        requested: str | None = None,
        system: str | None = None,
        *,
        pinned: bool = False,
        legacy_ids: tuple[str, ...] | list[str] = (),
    ) -> Path | None:
        """Resolve a device path.

        Order: what the caller asked for, then a per-service pin, then whatever
        was chosen in the UI, then the file's default.

        ``system`` narrows the last resort - "any device we know about" - to one
        that can answer the request being made. It does not override a name
        someone chose: a Widevine device selected while PlayReady is active is a
        configuration mistake worth reporting, not one to paper over.

        ``pinned`` says the system was not chosen by anyone - either the service
        only speaks that one, or the playback itself identifies it unambiguously.
        Then a name that belongs to a different system is not a mistake to report,
        it is a choice that was never about this playback, so a matching device is
        looked for instead. Without this, a Widevine device selected on the main
        screen blocks a MonaLisa playback with an error the user can do nothing
        sensible about.
        """
        section = self.raw.get("cdm") or {}
        name = (
            requested
            or _service_entry(section.get("by_service"), service_id, legacy_ids)
            or self.device_override
            or section.get("default")
        )
        if not name:
            return self._any_device(system)
        remote = self.remote_cdm(str(name), include_disabled=True)
        if remote is not None:
            if not getattr(remote, "enabled", True):
                # Keep a disabled endpoint configured for later, but never let a
                # stale default/override turn into a missing-device failure.
                return self._any_device(system)
            if pinned and system and remote.system != system:
                # A system-pinned request cannot use an app-wide remote CDM for
                # another system. Resolve a matching local device just as we do
                # for a mismatched local default below.
                return self._any_device(system)
            # Enabled remote devices have no local path. The caller resolves the
            # remote endpoint before asking for a device file.
            return None
        found = self._named_or_path(str(name))
        if pinned and system and found is not None:
            from .cdm import system_of

            if system_of(found) != system:
                return self._any_device(system) or found
        return found

    def _any_device(self, system: str | None = None) -> Path | None:
        """Any usable device, preferring one that matches ``system``.

        Declared devices first, then whatever is on disk. Discovery matters here:
        ``cdm.devices`` in the config file is a list someone wrote by hand, and a
        device dropped into an unconfigured directory is not on it - which is how
        "no MonaLisa device" got reported about a folder that had one in it.
        """
        from .cdm import discover, system_of

        declared = list(self._devices.values())
        if system:
            for path in declared:
                if system_of(path) == system:
                    return path
            for device in discover(self.cdm_roots()):
                if device.system == system:
                    return device.path
            return None
        return next(iter(declared), None)

    def device_name_for(
        self,
        service_id: str,
        requested: str | None = None,
        *,
        legacy_ids: tuple[str, ...] | list[str] = (),
    ) -> str:
        """Short label for the resolved device, never a full path."""
        section = self.raw.get("cdm") or {}
        name = str(
            requested
            or _service_entry(section.get("by_service"), service_id, legacy_ids)
            or self.device_override
            or section.get("default")
            or ""
        )
        if not name:
            path = self.device_for(service_id, requested, legacy_ids=legacy_ids)
            return path.stem if path is not None else ""
        return Path(name).stem if ("/" in name or "\\" in name) else name

    # --------------------------------------------------------- credentials
    def credential(
        self,
        service_id: str,
        slot: str = "default",
        *,
        legacy_ids: tuple[str, ...] | list[str] = (),
    ) -> Credential:
        section = _service_entry(self.raw.get("credentials") or {}, service_id, legacy_ids)
        if section is None:
            return Credential(slot=slot)
        if isinstance(section, dict) and slot in section and isinstance(section[slot], dict):
            return Credential(slot=slot, values=dict(section[slot]))
        # a flat mapping means the service has a single unnamed login
        if isinstance(section, dict) and not any(isinstance(v, dict) for v in section.values()):
            return Credential(slot=slot, values=dict(section))
        return Credential(slot=slot)

    def save_credential(
        self, service_id: str, slot: str, values: dict[str, Any]
    ) -> Path:
        """Write one login into ``unidl.yaml`` and into this instance.

        So a sign-in typed on screen is asked once rather than every session, which
        is the whole point of the file. Both halves matter: the file for next time,
        and ``self.raw`` for right now - every screen and every service holds this
        same instance, and a login that only reached the disk would still read as
        missing until the app was restarted.

        Written through a temporary file and ``os.replace`` so an interrupted write
        cannot leave a half a config behind, and mode 600 because the file now holds
        a password. Creates it at the canonical project config path if there is
        none; runtime state under ``paths.home`` is a separate location.
        """
        path = self.source or default_config_path()
        _reject_legacy_config(path)
        if path.is_symlink():
            # Written through the link, not over it. `os.replace` onto a symlink
            # replaces the *link* with a regular file, which would quietly
            # disconnect a config kept somewhere else and symlinked into place -
            # and leave every later hand edit to the real file unread.
            path = Path(os.path.realpath(path))
        with locked_path(path):
            text = path.read_text("utf-8") if path.is_file() else ""
            updated = write_credential(text, service_id, slot, values)
            atomic_write_text(path, updated)

        section = self.raw.setdefault("credentials", {})
        if isinstance(section, dict):
            entry = section.get(service_id)
            if not isinstance(entry, dict) or any(
                isinstance(inner, dict) for inner in entry.values()
            ):
                entry = entry if isinstance(entry, dict) else {}
                section[service_id] = entry
                entry[slot] = dict(values)
            else:
                # a flat block: it holds one unnamed login, which is this one
                section[service_id] = {slot: dict(values)}
        self.source = path
        return path

    def forget_credential(
        self,
        service_id: str,
        slot: str = "",
        *,
        legacy_ids: tuple[str, ...] | list[str] = (),
    ) -> Path | None:
        """Take one saved login out of ``unidl.yaml``, and out of this instance.

        The other half of :meth:`save_credential`. Deleting the block by hand in an
        editor does the same thing and always will - the file is the record - but
        something the interface wrote, the interface should be able to unwrite.

        Returns the file it edited, or None when there was no file to edit.
        """
        path = self.source
        if path is None:
            return None
        if path.is_symlink():
            path = Path(os.path.realpath(path))
        if not path.is_file():
            return None

        with locked_path(path):
            if not path.is_file():
                return None
            updated = path.read_text("utf-8")
            section = self.raw.get("credentials")
            for name in _service_keys(service_id, legacy_ids):
                entry = section.get(name) if isinstance(section, dict) else None
                nested = isinstance(entry, dict) and any(
                    isinstance(inner, dict) for inner in entry.values()
                )
                # A flat service block is the unnamed/default login. Asking the
                # text editor to remove a literal ``default:`` child would find
                # nothing and leave the password on disk even though memory was
                # cleared below.
                updated = remove_credential(updated, name, slot if nested else "")
            atomic_write_text(path, updated)

        section = self.raw.get("credentials")
        if isinstance(section, dict):
            for name in _service_keys(service_id, legacy_ids):
                entry = section.get(name)
                nested = isinstance(entry, dict) and any(
                    isinstance(inner, dict) for inner in entry.values()
                )
                if slot and nested and len(entry) > 1:
                    entry.pop(slot, None)
                else:
                    section.pop(name, None)
        return path

    def credential_slots(
        self,
        service_id: str,
        *,
        legacy_ids: tuple[str, ...] | list[str] = (),
    ) -> list[str]:
        section = _service_entry(self.raw.get("credentials") or {}, service_id, legacy_ids)
        if isinstance(section, dict):
            nested = [k for k, v in section.items() if isinstance(v, dict)]
            return nested or ["default"]
        return []

    # --------------------------------------------------------------- misc
    def proxy(self, name: str | None) -> str | None:
        """Resolve a named endpoint, direct URI or configured provider query."""
        from .proxy import resolve_proxy

        return resolve_proxy(
            name,
            self.raw.get("proxies") or {},
            self.raw.get("proxy_providers") or {},
            self.paths.tokens / "vpn",
        )

    def save_proxies(self, entries: dict[str, Any]) -> Path:
        """Write named proxy endpoints without touching provider credentials."""
        return self.save_managed_section("proxies", entries)

    def save_proxy_providers(self, entries: dict[str, Any]) -> Path:
        """Write supported VPN proxy-provider definitions."""
        return self.save_managed_section("proxy_providers", entries)

    def service_options(
        self,
        service_id: str,
        *,
        legacy_ids: tuple[str, ...] | list[str] = (),
    ) -> dict[str, Any]:
        return dict(_service_entry(self.raw.get("services") or {}, service_id, legacy_ids) or {})
