"""Native UniDL service registry.

Each provider package exposes one Service subclass when imported. Packages may
register it explicitly with ``@registry.register`` or let the loader register
the class automatically for a user-enabled package. Keeping the registry in one
module makes service discovery deterministic for the TUI, CLI and headless
tests.
"""

import logging
import sys
from collections import defaultdict
from importlib import import_module
from types import ModuleType

from ..core.compiled_service import register_compiled_services
from ..core.diagnostics import safe_log_text
from ..core.service import RegistrationChecks, Service, registry
from ..core.service_catalog import COMPILED_ONLY_IMPLEMENTATION, ServiceSource, discover_sources, service_roots

__all__ = ["compiled_service_errors", "service_load_errors"]

_COMPILED_SERVICE_ERRORS: dict[str, str] = {}
_SERVICE_LOAD_ERRORS: dict[str, str] = {}
_LOG = logging.getLogger(__name__)


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
    home = getattr(getattr(config, "paths", None), "home", None)
    package_paths = getattr(sys.modules[__name__], "__path__", ())
    roots = service_roots(home, config=config) if config is not None else service_roots(home)
    for root in roots:
        text = str(root)
        if text not in package_paths and hasattr(package_paths, "append"):
            package_paths.append(text)
    packages: dict[str, list[ServiceSource]] = defaultdict(list)
    _SERVICE_LOAD_ERRORS.clear()
    _COMPILED_SERVICE_ERRORS.clear()
    discovered = discover_sources(home=home, config=config) if config is not None else discover_sources(home=home)
    for source in discovered:
        packages[source.package].append(source)
    checks = RegistrationChecks()
    for package, sources in packages.items():
        declared = next((source for source in sources if source.manifest is not None), sources[0])
        if declared.error:
            _SERVICE_LOAD_ERRORS[declared.service_id] = declared.error
            _LOG.warning("service %s is unavailable: %s", declared.service_id, declared.error)
            continue
        compiled = declared.implementation == COMPILED_ONLY_IMPLEMENTATION
        fullname = f"{__name__}.{package}"
        prefix = fullname + "."
        existing = {name for name in sys.modules.copy() if name == fullname or name.startswith(prefix)}
        try:
            with registry.registration_scope(checks=checks):
                if compiled:
                    register_compiled_services(fullname, manifest_path=declared.manifest or declared.path)
                else:
                    _register_module_services(import_module(fullname))
        except (Exception, SystemExit) as exc:
            # A submodule might have loaded successfully and registered a
            # class before its parent failed. Retry must rerun that decorator.
            for name in sys.modules.copy():
                if name not in existing and (name == fullname or name.startswith(prefix)):
                    sys.modules.pop(name, None)
            reason = safe_log_text(f"{type(exc).__name__}: {exc}").splitlines()[0]
            errors = _COMPILED_SERVICE_ERRORS if compiled else _SERVICE_LOAD_ERRORS
            for source in sources:
                errors[source.service_id] = reason
            _LOG.warning("service %s is unavailable: %s", package, reason)
    return len(registry.all())


def compiled_service_errors() -> dict[str, str]:
    """Return compiled-only services that failed without stopping UniDL."""

    return dict(_COMPILED_SERVICE_ERRORS)


def service_load_errors() -> dict[str, str]:
    """Return source and manifest failures without exposing unusable services."""

    return dict(_SERVICE_LOAD_ERRORS)
