"""Explicit, development-only reload of one native service package.

This is deliberately not a watcher. A person chooses the moment after an edit
is complete, from the home screen where no service instance is running. Existing
instances are never patched: the registry receives new classes and the next
service session is built from them.
"""

from __future__ import annotations

import ast
import importlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from .service import Service, ServiceRegistry

_SERVICE_PREFIX = "unidl.services."


class ReloadError(RuntimeError):
    """The requested service could not be safely reloaded."""


@dataclass(frozen=True)
class ReloadResult:
    requested: str
    services: tuple[str, ...]
    modules: tuple[str, ...]
    generation: int
    seconds: float


class ServiceReloader:
    """Reload a service's loaded module graph at an explicit safe point."""

    def __init__(self, registry: ServiceRegistry):
        self.registry = registry
        self.generation = 0

    def reload(self, service_cls: type[Service]) -> ReloadResult:
        started = time.monotonic()
        requested = str(service_cls.ID or "").strip()
        root = _service_root(service_cls)
        loaded = _loaded_service_modules()
        selected = {
            name for name in loaded if name == root or name.startswith(f"{root}.")
        }
        if not selected:
            raise ReloadError(f"no loaded Python modules belong to {requested or root}")

        dependencies = {
            name: _module_dependencies(module, set(loaded))
            for name, module in loaded.items()
        }
        # A few service families deliberately share native Python modules. Reload
        # both directions inside ``unidl.services``: a requested wrapper needs
        # its edited shared dependency, and an edited shared implementation
        # needs every wrapper that imported names from it refreshed as well.
        # Core/TUI modules are outside the graph and are never touched here.
        selected = _service_closure(selected, dependencies)
        order = _dependency_order(selected, dependencies)
        sources = _compile_sources(order, loaded)

        # An editor may still be replacing files while the key is pressed. Catch
        # that before the first module is mutated; the action can simply be tried
        # again once the files have settled.
        changed = [
            str(path)
            for path, expected in sources.values()
            if _read(path) != expected
        ]
        if changed:
            raise ReloadError(
                "service files changed while reload was preparing: "
                + ", ".join(changed[:3])
            )

        with self.registry._lock:  # one transaction against readiness/account workers
            previous = dict(self.registry._services)
            try:
                importlib.invalidate_caches()
                with self.registry.replacement_scope(selected):
                    for name in order:
                        module = loaded[name]
                        cached = getattr(module, "__cached__", None)
                        if cached:
                            try:
                                Path(cached).unlink(missing_ok=True)
                            except OSError:
                                pass
                        importlib.reload(module)
                replacement = self.registry._services.get(requested)
                if replacement is None or replacement is previous.get(requested):
                    raise ReloadError(
                        f"{requested} did not register a replacement class after its "
                        "modules reloaded; service id changes require a restart"
                    )
                stale = [
                    service_id
                    for service_id, old_class in previous.items()
                    if old_class.__module__ in selected
                    and self.registry._services.get(service_id) is old_class
                ]
                if stale:
                    raise ReloadError(
                        "shared service modules did not replace: " + ", ".join(stale)
                    )
            except Exception as exc:
                self.registry._services.clear()
                self.registry._services.update(previous)
                if isinstance(exc, ReloadError):
                    raise
                raise ReloadError(
                    f"{requested} reload failed: {type(exc).__name__}: {exc}"
                ) from exc

            affected = {
                service_id
                for service_id, cls in {**previous, **self.registry._services}.items()
                if cls.__module__ in selected
            }
            affected.add(requested)

        self.generation += 1
        return ReloadResult(
            requested=requested,
            services=tuple(sorted(affected)),
            modules=tuple(order),
            generation=self.generation,
            seconds=time.monotonic() - started,
        )


def _service_root(service_cls: type[Service]) -> str:
    module = str(getattr(service_cls, "__module__", ""))
    parts = module.split(".")
    if len(parts) < 3 or parts[:2] != ["unidl", "services"]:
        raise ReloadError(
            f"{service_cls.ID or service_cls.__name__} is not a native unidl service module"
        )
    return ".".join(parts[:3])


def _source_path(module: ModuleType) -> Path | None:
    raw = str(getattr(module, "__file__", "") or "")
    if not raw:
        return None
    path = Path(raw)
    if path.suffix in {".pyc", ".pyo"}:
        try:
            path = Path(importlib.util.source_from_cache(str(path)))
        except ValueError:
            return None
    return path if path.suffix == ".py" and path.is_file() else None


def _loaded_service_modules() -> dict[str, ModuleType]:
    return {
        name: module
        for name, module in list(sys.modules.items())
        if name.startswith(_SERVICE_PREFIX)
        and isinstance(module, ModuleType)
        and _source_path(module) is not None
    }


def _module_dependencies(module: ModuleType, loaded: set[str]) -> set[str]:
    # Runtime references cover dynamic/imported aliases and remain usable while
    # an editor has temporarily left an unselected consumer with invalid syntax.
    # The AST pass below adds imports whose names are not retained as globals.
    found: set[str] = set()
    for value in vars(module).values():
        if isinstance(value, ModuleType):
            owner = value.__name__
        else:
            owner = str(getattr(value, "__module__", "") or "")
        if owner in loaded:
            found.add(owner)

    path = _source_path(module)
    if path is None:
        return found
    try:
        tree = ast.parse(_read(path), filename=str(path))
    except (OSError, SyntaxError):
        # A selected module is compiled separately and will report the real
        # syntax error. An unrelated half-written service must not prevent the
        # dependency graph for the requested one from being built.
        return found

    package = str(getattr(module, "__package__", "") or "")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name in loaded)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            relative = "." * node.level + (node.module or "")
            try:
                base = importlib.util.resolve_name(relative, package)
            except (ImportError, ValueError):
                continue
        else:
            base = str(node.module or "")
        if base in loaded:
            found.add(base)
        found.update(
            candidate
            for alias in node.names
            if (candidate := f"{base}.{alias.name}") in loaded
        )
    found.discard(module.__name__)
    return found


def _service_closure(selected: set[str], dependencies: dict[str, set[str]]) -> set[str]:
    """Include shared service dependencies and everything that imports them."""
    chosen = set(selected)
    while True:
        dependencies_of_chosen = set().union(
            *(dependencies.get(name, set()) for name in chosen)
        )
        dependents = {
            name
            for name, needs in dependencies.items()
            if name.startswith(_SERVICE_PREFIX) and name not in chosen and needs & chosen
        }
        added = (dependencies_of_chosen | dependents) - chosen
        if not added:
            return chosen
        chosen.update(added)


def _dependency_order(selected: set[str], dependencies: dict[str, set[str]]) -> list[str]:
    remaining = set(selected)
    ordered: list[str] = []
    while remaining:
        ready = [
            name for name in remaining if not (dependencies.get(name, set()) & remaining)
        ]
        if not ready:
            # Circular relative imports are legal after the first import. Reload
            # the deepest module first and leave package __init__ modules last.
            ready = [max(remaining, key=lambda name: (name.count("."), name))]
        for name in sorted(ready, key=lambda value: (-value.count("."), value)):
            ordered.append(name)
            remaining.remove(name)
    return ordered


def _compile_sources(
    order: list[str], loaded: dict[str, ModuleType]
) -> dict[str, tuple[Path, bytes]]:
    sources: dict[str, tuple[Path, bytes]] = {}
    for name in order:
        path = _source_path(loaded[name])
        if path is None:
            continue
        try:
            source = _read(path)
            compile(source, str(path), "exec", dont_inherit=True)
        except (OSError, SyntaxError) as exc:
            line = f" line {exc.lineno}" if isinstance(exc, SyntaxError) and exc.lineno else ""
            raise ReloadError(f"{path.name}{line} cannot be reloaded: {exc}") from exc
        sources[name] = (path, source)
    return sources


def _read(path: Path) -> bytes:
    return path.read_bytes()


__all__ = ["ReloadError", "ReloadResult", "ServiceReloader"]
