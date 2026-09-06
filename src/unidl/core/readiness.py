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
"""

from __future__ import annotations

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
    #: a missing one of these stops something dead rather than degrading it
    required: bool = False

    @property
    def blocking(self) -> bool:
        return self.required and not self.ok


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

    def summary(self) -> str:
        """One line for the status chip: what is wrong, or that nothing is."""
        blocking = len(self.blocking)
        if blocking:
            return f"{blocking} missing" if blocking > 1 else "1 missing"
        optional = len([item for item in self.missing if item.group != "folders"])
        if optional:
            return f"{optional} optional missing"
        return "ready"

    @property
    def ready(self) -> bool:
        return not self.blocking


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
            )
        )
    return items


def _writable(path: Path) -> bool:
    import os

    try:
        return os.access(path, os.W_OK)
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
        _drm_items() + _helper_items(config, registry, resolver) + _folder_items(config)
    )
    order = {name: index for index, name in enumerate(GROUPS)}
    items.sort(key=lambda item: (order.get(item.group, 99), not item.blocking, item.label.lower()))
    return Report(items=items)


__all__ = ["GROUPS", "GROUP_LABEL", "Item", "Report", "forget", "survey"]
