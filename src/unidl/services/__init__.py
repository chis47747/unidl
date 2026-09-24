"""Native UniDL service registry.

Each provider package exposes one Service subclass when imported. Packages may
register it explicitly with ``@registry.register`` or let the loader register
the class automatically for a user-enabled package. Keeping the registry in one
module makes service discovery deterministic for the TUI, CLI and headless
tests.
"""

import logging
from importlib import import_module
from types import ModuleType

from ..core.compiled_service import CompiledServiceError, register_compiled_services
from ..core.service import Service, registry
from ..core.service_catalog import COMPILED_ONLY_IMPLEMENTATION, discover_sources

__all__ = ["compiled_service_errors"]

_COMPILED_SERVICE_ERRORS: dict[str, str] = {}


def _register_module_services(module: ModuleType) -> None:
    """Register concrete service classes exported by *module*.

    A package may use the explicit ``@registry.register`` decorator, which is
    the code-level registration path for built-in distributions.  Imported
    service packages supplied through the TUI may omit that decorator: the
    loader registers their concrete ``Service`` subclasses after import so
    user-level registration has the same runtime effect.  Already registered
    classes are left untouched, making both styles safe to mix.
    """
    for candidate in vars(module).values():
        if not isinstance(candidate, type) or candidate is Service:
            continue
        try:
            is_service = issubclass(candidate, Service)
        except TypeError:
            continue
        if not is_service or candidate.__module__ != module.__name__:
            continue
        service_id = str(getattr(candidate, "ID", "") or "").strip()
        name = str(getattr(candidate, "NAME", "") or "").strip()
        if not service_id or not name or registry.get(service_id) is not None:
            continue
        registry.register(candidate)


def load_all(config=None) -> int:
    """Import service packages found on disk, validate, and return the count.

    The scan is the extension seam used by the TUI registration manager: a
    newly added service package no longer needs another edit to this module.
    Packages that use ``@registry.register`` keep that code-level path; packages
    registered from the TUI may rely on the loader's class discovery instead.
    """
    for source in discover_sources():
        if source.implementation == COMPILED_ONLY_IMPLEMENTATION:
            _COMPILED_SERVICE_ERRORS.pop(source.service_id, None)
            try:
                register_compiled_services(
                    f"{__name__}.{source.package}",
                    manifest_path=source.manifest or source.path,
                )
            except CompiledServiceError as exc:
                _COMPILED_SERVICE_ERRORS[source.service_id] = str(exc)
                logging.getLogger(__name__).warning(
                    "compiled-only service %s is unavailable: %s", source.service_id, exc
                )
        else:
            module = import_module(f"{__name__}.{source.package}")
            _register_module_services(module)
    # Declaration validation only needs class metadata and setting keys.  The
    # config-dependent device option lists are built lazily when a service is
    # opened; resolving every local WVD/PRD here made startup needlessly slow.
    registry.validate()
    return len(registry.all())


def compiled_service_errors() -> dict[str, str]:
    """Return compiled-only services that failed without stopping UniDL."""

    return dict(_COMPILED_SERVICE_ERRORS)
