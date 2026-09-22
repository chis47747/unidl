"""What is installed, what is missing, and what stops working without it.

Every piece of this was already known and none of it was ever shown. A service
declares the tools it needs, :class:`~unidl.core.helpers.HelperResolver` finds
them or does not, and the DRM registry knows whether a system's library is
importable - but all three only spoke up at the moment they failed, which is
halfway through a download, in a log, as an exception. So a missing Java was
discovered by a service dying rather than by looking.

This assembles the same facts into one report, ahead of time. It is deliberately
read-only and installs nothing: the answer to a missing tool is a package manager,
and the report says which command to run.

Grouped, because the groups have different consequences:

* **drm** - a system whose library is absent cannot be selected at all
* **tools** - an executable some service shells out to
* **assets** - a file or module a service needs alongside its code
* **folders** - where things are written, and whether they can be

Each item also has an impact level independent of its group: ``core`` (red and
blocking), ``global`` (yellow, such as DRM libraries/devices), or
``enhancement`` (blue, such as service helpers and Dolby Vision Hybrid tools).
Only a report with no missing item at any level is fully ``ready``.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import drm as drm_registry
from .helpers import Helper, HelperKind, HelperResolver

#: The order groups are shown in, worst consequence first.
GROUPS = ("drm", "tools", "assets", "folders")

GROUP_LABEL = {
    "drm": "DRM systems",
    "tools": "External tools",
    "assets": "Files and modules services need",
    "folders": "Where things are kept",
}

# These are the small set of imports the application itself needs before a
# service can even be opened.  Service-specific imports are deliberately not in
# this list: a missing service helper is an incomplete optional capability, not
# a broken UniDL installation.
CORE_IMPORTS = (
    ("textual", "Textual TUI"),
    ("yaml", "PyYAML"),
    ("requests", "Requests HTTP client"),
    ("cryptography", "Cryptography backend"),
    ("Crypto", "PyCryptodome backend"),
)

CORE = "core"
GLOBAL = "global"
ENHANCEMENT = "enhancement"
LEVELS = (CORE, GLOBAL, ENHANCEMENT)


@dataclass(frozen=True)
class Item:
    """One line of the report."""

    group: str
    label: str
    ok: bool
    #: what was found, or why it was not
    detail: str = ""
    #: what to do about it, when there is something to do
    hint: str = ""
    #: what goes wrong while it is missing
    without: str = ""
    #: the services that asked for it, for a helper nothing else explains
    needed_by: tuple[str, ...] = ()
    #: whether the declaring service considers this helper mandatory for its own
    #: feature (does not change the application-wide dependency level)
    required: bool = False
    #: impact scope, independent from whether a service declares a helper as
    #: required for its own feature.  This is what drives the readiness colours.
    level: str = GLOBAL

    @property
    def blocking(self) -> bool:
        # ``required=True`` was the pre-level API.  Keep it meaningful for
        # callers constructing a plain Item, while all built-in service helpers
        # explicitly use ENHANCEMENT and therefore stay blue.
        return (self.level == CORE or (self.level == GLOBAL and self.required)) and not self.ok

    @property
    def severity(self) -> int:
        """The severity used by the home chip (higher is worse)."""
        if self.ok:
            return 0
        if self.blocking:
            return 3
        return {ENHANCEMENT: 1, GLOBAL: 2, CORE: 3}.get(self.level, 2)


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)

    def group(self, name: str) -> list[Item]:
        return [item for item in self.items if item.group == name]

    @property
    def missing(self) -> list[Item]:
        return [item for item in self.items if not item.ok]

    @property
    def blocking(self) -> list[Item]:
        return [item for item in self.items if item.blocking]

    def missing_at(self, level: str) -> list[Item]:
        return [item for item in self.items if item.level == level and not item.ok]

    @property
    def global_missing(self) -> list[Item]:
        return self.missing_at(GLOBAL)

    @property
    def enhancement_missing(self) -> list[Item]:
        return self.missing_at(ENHANCEMENT)

    @property
    def status(self) -> str:
        """``missing``, ``global``, ``partial`` or ``ready`` for UI clients."""
        if self.blocking:
            return "missing"
        if self.global_missing:
            return "global"
        if self.enhancement_missing:
            return "partial"
        return "ready"

    def summary(self) -> str:
        """One line for the status chip: what is wrong, or that nothing is."""
        if self.blocking:
            count = len(self.blocking)
            return f"{count} missing" if count > 1 else "1 missing"
        if self.global_missing:
            count = len(self.global_missing)
            return f"{count} global missing"
        if self.enhancement_missing:
            return "partial ready"
        return "ready"

    @property
    def ready(self) -> bool:
        return self.status == "ready"


def _declared(registry) -> dict[str, tuple[Helper, list[str]]]:
    """Every helper any registered service asks for, and which ones ask.

    Keyed by helper key, first declaration wins. Two services declaring the same
    key with different labels is a naming slip rather than two different tools, and
    resolving it here would hide it; the report shows the first and lists both
    services.
    """
    found: dict[str, tuple[Helper, list[str]]] = {}
    for service_cls in registry.all():
        for helper in getattr(service_cls, "HELPERS", ()) or ():
            entry = found.setdefault(helper.key, (helper, []))
            entry[1].append(service_cls.ID)
    return found


def _drm_items() -> list[Item]:
    items = []
    for system in drm_registry.all_systems():
        items.append(
            Item(
                group="drm",
                label=system.label,
                ok=system.available,
                detail=f"{system.suffix} devices" if system.available else "library not installed",
                hint="" if system.available else system.install_hint,
                without="cannot be selected as the DRM system",
                level=GLOBAL,
            )
        )
    return items


def _module_items() -> list[Item]:
    """Check imports needed by UniDL itself, without importing service code."""
    items: list[Item] = []
    for module, label in CORE_IMPORTS:
        try:
            available = importlib.util.find_spec(module) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            available = False
        items.append(
            Item(
                group="tools",
                label=label,
                ok=available,
                detail="installed" if available else f"Python module {module!r} is not importable",
                hint=f"python -m pip install {module}",
                without="the UniDL application cannot provide its normal TUI or downloader runtime",
                level=CORE,
            )
        )
    return items


def _cdm_items(config) -> list[Item]:
    """Show the global DRM device capability separately from its Python library.

    A remote CDM counts as a device.  This keeps a machine that intentionally
    uses a remote CDM from being reported as incomplete just because its local
    ``cdm/`` folder is empty.
    """
    try:
        devices = [
            *drm_registry.discover(config.cdm_roots()),
            *drm_registry.remote_devices(config.remote_cdms),
        ]
    except Exception:  # noqa: BLE001 - a malformed optional config is not fatal
        devices = []
    by_system = {system.id: 0 for system in drm_registry.all_systems()}
    for device in devices:
        if device.system in by_system and device.usable:
            by_system[device.system] += 1

    items: list[Item] = []
    for system in drm_registry.all_systems():
        count = by_system.get(system.id, 0)
        # A device row is useful once the local library can consume it, or when
        # a remote device is configured.  Otherwise the library row already
        # explains the missing global capability and a second row would only
        # repeat the same problem.
        has_remote = any(device.system == system.id and device.is_remote for device in devices)
        if not system.available and not has_remote:
            continue
        items.append(
            Item(
                group="drm",
                label=f"{system.label} CDM device",
                ok=count > 0,
                detail=f"{count} local/remote device(s) configured" if count else "no local or remote device configured",
                hint=f"Put a {system.suffix} device in {getattr(config.paths, 'cdm', '~/.unidl/cdm')} or configure a remote CDM",
                without=f"{system.label} protected playback and licensing",
                level=GLOBAL,
            )
        )
    return items


def _helper_items(config, registry, resolver: HelperResolver) -> list[Item]:
    items = []
    for key, (helper, services) in sorted(_declared(registry).items()):
        # Resolved as the first service that asks for it, so a path that service
        # names in the config file is honoured. A helper several services share is
        # normally shared in the config too, and the alternative - resolving it once
        # per service - runs a version probe per service for no new information.
        report = resolver.report(services[0], (helper,))
        found = report.resolved.get(key)
        group = "tools" if helper.kind is HelperKind.BINARY else "assets"
        items.append(
            Item(
                group=group,
                label=helper.label or key,
                ok=bool(found and found.ok),
                detail=found.describe() if found else "not resolved",
                hint=helper.install_hint,
                without=helper.degrades_to,
                needed_by=tuple(sorted(services)),
                required=helper.required,
                level=ENHANCEMENT,
            )
        )
    return items


def _which(names: tuple[str, ...]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _hybrid_items() -> list[Item]:
    """Optional Hybrid prerequisites, kept visible without making them global."""
    candidates = {
        "dovi_tool": ("dovi_tool", "dovi_tool.exe"),
        "ffmpeg (Hybrid)": ("ffmpeg",),
        "ffprobe (Hybrid)": ("ffprobe",),
        "mkvmerge (Hybrid)": ("mkvmerge",),
    }
    items: list[Item] = []
    for label, names in candidates.items():
        found = _which(names)
        if not found and label == "dovi_tool":
            for env_name in ("UNIDL_DOVI_TOOL", "DOVI_TOOL"):
                value = os.environ.get(env_name)
                if value and Path(value).is_file():
                    found = value
                    break
            if not found:
                for value in (Path.home() / "dovi_tool", Path.home() / "dovi_tool.exe"):
                    if value.is_file():
                        found = str(value)
                        break
        items.append(
            Item(
                group="tools",
                label=label,
                ok=bool(found),
                detail=str(found) if found else "not found",
                hint="Install dovi_tool and its ffmpeg/mkvmerge companions for Hybrid output"
                if label == "dovi_tool"
                else "Install the tool to enable Dolby Vision Hybrid output",
                without="Dolby Vision + HDR10 Hybrid output",
                level=ENHANCEMENT,
            )
        )
    return items


def _folder_items(config) -> list[Item]:
    paths = config.paths
    wanted = [
        ("Downloads", paths.downloads, "where finished files land"),
        ("Commands", paths.commands, "the exported UniDL commands"),
        ("Exports", paths.exports, "the portable manifest-and-keys files"),
        ("Tokens", paths.tokens, "what a sign-in produced"),
        ("CDM devices", paths.cdm, "the .wvd and .prd files"),
        ("Helpers", paths.helpers, "tools and assets kept with the project"),
        ("Key vault", paths.keys_db, "the local KID:key database"),
    ]
    items = []
    for label, path, what in wanted:
        target = Path(path)
        # A folder that does not exist yet is not a problem: it is made on demand.
        # One that exists and cannot be written to is.
        exists = target.exists()
        writable = _writable(target if exists else target.parent)
        items.append(
            Item(
                group="folders",
                label=label,
                ok=writable,
                detail=str(target) + ("" if exists else "  (not created yet)"),
                without=what if writable else "cannot be written to",
                # Finished downloads and command files are part of the basic
                # delivery path.  Token/export/CDM/helper/vault locations are
                # setup capabilities and remain yellow when unavailable.
                level=CORE if label in {"Downloads", "Commands"} else GLOBAL,
            )
        )
    return items


def _writable(path: Path) -> bool:
    import os

    try:
        # Paths are created lazily.  Check the nearest existing ancestor rather
        # than declaring a fresh install broken simply because ``downloads/``
        # has not been created yet.
        candidate = path
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return os.access(candidate, os.W_OK)
    except OSError:
        return False


#: The resolver the last survey used, and which install it was for. Kept so the
#: chip on the main screen and the screen it opens do not each shell out to every
#: tool on the machine to learn the same thing: a survey runs a version probe per
#: helper, and the chip alone would repeat that on every repaint of the main screen.
_kept: tuple[tuple[str, str], HelperResolver] | None = None


def _resolver_for(config, fresh: bool) -> HelperResolver:
    """A resolver for this install, reused unless the caller wants a fresh look.

    Keyed by what identifies the install rather than by the object, so two Config
    objects for the same home share the answers and a different home never does.
    """
    global _kept
    key = (
        str(getattr(getattr(config, "paths", None), "home", "")),
        str(getattr(config, "source", "") or ""),
    )
    if not fresh and _kept is not None and _kept[0] == key:
        return _kept[1]
    resolver = HelperResolver(config)
    _kept = (key, resolver)
    return resolver


def forget() -> None:
    """Drop what was found last time. The next survey looks again."""
    global _kept
    _kept = None


def survey(config, registry=None, *, fresh: bool = False) -> Report:
    """The whole report, in the order it should be read.

    ``fresh`` re-runs every probe. That is what "check again" means, and it is the
    only thing that does: everything else - the chip, opening the screen, coming
    back to it - is answered from what the last survey found, because a tool does
    not appear on the machine while you are looking at a list of it.
    """
    if registry is None:
        from .service import registry as default_registry

        registry = default_registry
    resolver = _resolver_for(config, fresh)
    items = (
        _module_items()
        + _drm_items()
        + _cdm_items(config)
        + _helper_items(config, registry, resolver)
        + _hybrid_items()
        + _folder_items(config)
    )
    order = {name: index for index, name in enumerate(GROUPS)}
    items.sort(key=lambda item: (order.get(item.group, 99), -item.severity, item.label.lower()))
    return Report(items=items)


__all__ = [
    "CORE",
    "CORE_IMPORTS",
    "ENHANCEMENT",
    "GLOBAL",
    "GROUPS",
    "GROUP_LABEL",
    "LEVELS",
    "Item",
    "Report",
    "forget",
    "survey",
]
