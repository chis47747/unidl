"""Service source discovery and user-controlled registration state.

Service modules still own their normal :class:`~unidl.core.service.Service`
registration.  This module only discovers the metadata needed by the TUI and
stores which already-imported services are enabled for a user.  In particular,
it never imports a service while the registration dialog is open: registering
code that needs a restart is deliberately a persistent, atomic choice.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .settings import SettingsStore

GLOBAL_SCOPE = "@global"
REGISTERED_KEY = "registered_services"
HOME_KEY = "home_services"

# A cheap textual gate avoids compiling large API/helper modules that cannot
# contain a service declaration.  ``__init__.py`` and the conventional
# ``service.py`` are always parsed so multiline base lists remain supported.
_SERVICE_CLASS_HINT = re.compile(
    r"^\s*class\s+\w+\s*\([^)]*\b[A-Za-z_]\w*Service\b[^)]*\)", re.MULTILINE
)
_DISCOVERY_CACHE: dict[Path, tuple[tuple[tuple[str, int, int], ...], tuple[ServiceSource, ...]]] = {}


@dataclass(frozen=True)
class ServiceSource:
    """Metadata read from one service package without executing its code."""

    service_id: str
    name: str
    package: str
    path: Path


def source_root() -> Path:
    """The package's ``services`` directory in a checkout or installation."""

    return Path(__file__).resolve().parents[1] / "services"


def _literal_string(node: ast.AST | None) -> str:
    try:
        value = ast.literal_eval(node) if node is not None else ""
    except (ValueError, TypeError, SyntaxError):
        return ""
    return str(value).strip() if isinstance(value, str) else ""


def _class_metadata(tree: ast.AST) -> list[tuple[str, str]]:
    """Return every statically-declared service identity in *tree*.

    A module may contain more than one registered service (for example a
    shared API module with several brand subclasses).  Returning a list keeps
    discovery lossless; classes whose identity is assigned in
    ``__init_subclass__`` are intentionally left for the registry fallback in
    the TUI.
    """
    found: list[tuple[str, str]] = []
    # Service declarations are module-level classes.  Walking every expression
    # in large API/helper modules made startup disproportionately expensive;
    # inspecting the module body retains all native declarations while avoiding
    # millions of AST node visits.
    for node in getattr(tree, "body", ()):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(base.attr)
        if not any(base == "Service" or base.endswith("Service") for base in bases):
            continue
        values: dict[str, str] = {}
        for statement in node.body:
            if isinstance(statement, ast.Assign):
                targets = statement.targets
            elif isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {"ID", "NAME"}:
                    values[target.id] = _literal_string(
                        statement.value if isinstance(statement, ast.Assign) else statement.value
                    )
        service_id = values.get("ID", "").lower()
        name = values.get("NAME", "")
        if service_id and name:
            pair = (service_id, name)
            if pair not in found:
                found.append(pair)
    return found


def discover_sources(root: Path | None = None) -> list[ServiceSource]:
    """Read service package metadata from disk, never importing the packages."""

    directory = Path(root) if root is not None else source_root()
    if not directory.is_dir():
        return []
    # ``load_all`` discovers once before Textual starts; the registration screen
    # asks for the same catalog again. Reuse it when source mtimes/sizes are
    # unchanged, while still noticing a newly added or edited service package.
    signature_parts: list[tuple[str, int, int]] = []
    for entry in sorted(directory.iterdir(), key=lambda path: path.name.casefold()):
        if entry.name.startswith("_") or entry.name.casefold() == "example":
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if entry.is_dir():
            signature_parts.append((str(entry), stat.st_mtime_ns, stat.st_size))
            candidates = entry.glob("*.py")
        elif entry.is_file() and entry.suffix.casefold() == ".py":
            signature_parts.append((str(entry), stat.st_mtime_ns, stat.st_size))
            candidates = (entry,)
        else:
            continue
        for path in candidates:
            try:
                child = path.stat()
            except OSError:
                continue
            signature_parts.append((str(path), child.st_mtime_ns, child.st_size))
    signature = tuple(signature_parts)
    cached = _DISCOVERY_CACHE.get(directory)
    if cached is not None and cached[0] == signature:
        return list(cached[1])
    found: dict[str, ServiceSource] = {}
    for package in sorted(directory.iterdir(), key=lambda path: path.name.casefold()):
        # Native services are normally packages, but a single ``foo.py`` file
        # is also a valid and useful extension shape. Treat both identically;
        # ``__init__.py`` and private helpers remain excluded.
        if package.name.startswith("_") or package.name.casefold() == "example":
            continue
        if package.is_dir():
            package_name = package.name
            files = sorted(package.glob("*.py"), key=lambda path: path.name.casefold())
        elif package.is_file() and package.suffix.casefold() == ".py":
            package_name = package.stem
            files = [package]
        else:
            continue
        for path in files:
            try:
                source = path.read_text("utf-8")
                if (
                    path.name not in {"__init__.py", "service.py"}
                    and not _SERVICE_CLASS_HINT.search(source)
                ):
                    continue
                tree = ast.parse(source, filename=str(path))
            except (OSError, SyntaxError, UnicodeError):
                continue
            for service_id, name in _class_metadata(tree):
                found.setdefault(service_id, ServiceSource(service_id, name, package_name, path))
    result = tuple(sorted(found.values(), key=lambda item: item.name.casefold()))
    _DISCOVERY_CACHE[directory] = (signature, result)
    return list(result)


def merge_registry_sources(
    sources: Iterable[ServiceSource], services: Iterable[type[Any]], root: Path | None = None
) -> list[ServiceSource]:
    """Add already-loaded services that cannot be identified by AST alone.

    Some shared service bases assign ``ID``/``NAME`` in ``__init_subclass__``;
    those values do not exist as literals in the source file.  The registry is
    authoritative after startup, so use it only as a metadata fallback while
    retaining the no-import behaviour of :func:`discover_sources`.
    """
    directory = Path(root) if root is not None else source_root()
    merged = {source.service_id: source for source in sources}
    for service in services:
        service_id = str(getattr(service, "ID", "") or "").strip().lower()
        name = str(getattr(service, "NAME", "") or "").strip()
        if not service_id or not name or service_id in merged:
            continue
        module = str(getattr(service, "__module__", "") or "")
        relative = module.removeprefix("unidl.services.")
        parts = relative.split(".") if relative else []
        package = parts[0] if parts else service_id
        module_path = (
            directory / Path(*parts).with_suffix(".py")
            if parts
            else directory / package / "__init__.py"
        )
        if len(parts) == 1:
            module_path = directory / package / "__init__.py"
        merged[service_id] = ServiceSource(service_id, name, package, module_path)
    return sorted(merged.values(), key=lambda item: item.name.casefold())


def _ids(value: Any) -> list[str]:
    if isinstance(value, str):
        values: Iterable[Any] = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        values = ()
    result: list[str] = []
    for item in values:
        service_id = str(item or "").strip().lower()
        if service_id and service_id not in result:
            result.append(service_id)
    return result


def _stored_ids(store: SettingsStore, key: str) -> tuple[list[str], bool]:
    values = store.values_for(GLOBAL_SCOPE)
    return _ids(values.get(key)), key in values


def ensure_state(store: SettingsStore, service_ids: Iterable[str]) -> tuple[set[str], set[str]]:
    """Initialize or sanitize registration/home selections.

    Existing installs have no registration keys yet. Treat all currently loaded
    services as registered once, preserving the pre-2.0.6 experience. Future
    service packages are then absent from the persisted list until explicitly
    registered in the TUI.
    """

    available = {str(value).strip().lower() for value in service_ids if str(value).strip()}
    registered, had_registered = _stored_ids(store, REGISTERED_KEY)
    home, had_home = _stored_ids(store, HOME_KEY)
    changed = False
    if not had_registered:
        registered = sorted(available)
        store.put(GLOBAL_SCOPE, REGISTERED_KEY, registered)
        changed = True
    else:
        clean = sorted(set(registered) & available)
        if clean != sorted(registered):
            registered = clean
            store.put(GLOBAL_SCOPE, REGISTERED_KEY, registered)
            changed = True
    if not had_home:
        home = list(registered)
        store.put(GLOBAL_SCOPE, HOME_KEY, home)
        changed = True
    else:
        clean_home = sorted(set(home) & set(registered))
        if clean_home != sorted(home):
            home = clean_home
            store.put(GLOBAL_SCOPE, HOME_KEY, home)
            changed = True
    if changed:
        store.save()
    return set(registered), set(home)


def registered_ids(store: SettingsStore) -> set[str]:
    return set(_stored_ids(store, REGISTERED_KEY)[0])


def home_ids(store: SettingsStore) -> set[str]:
    return set(_stored_ids(store, HOME_KEY)[0])


def update_registration(store: SettingsStore, service_id: str, enabled: bool) -> None:
    """Persist one service registration without changing other global settings."""

    current = registered_ids(store)
    service_id = str(service_id or "").strip().lower()
    if enabled:
        current.add(service_id)
    else:
        current.discard(service_id)
    store.put(GLOBAL_SCOPE, REGISTERED_KEY, sorted(current))
    home = home_ids(store)
    if enabled:
        home.add(service_id)
    else:
        home.discard(service_id)
    store.put(GLOBAL_SCOPE, HOME_KEY, sorted(home))
    store.save()


def update_home(store: SettingsStore, service_ids: Iterable[str]) -> None:
    """Persist the selected homepage services, constrained to registrations."""

    registered = registered_ids(store)
    chosen = {str(value).strip().lower() for value in service_ids if str(value).strip()}
    store.put(GLOBAL_SCOPE, HOME_KEY, sorted(chosen & registered))
    store.save()


__all__ = [
    "HOME_KEY",
    "REGISTERED_KEY",
    "ServiceSource",
    "discover_sources",
    "merge_registry_sources",
    "ensure_state",
    "home_ids",
    "registered_ids",
    "source_root",
    "update_home",
    "update_registration",
]
