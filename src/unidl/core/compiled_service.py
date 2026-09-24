"""Registration support for source-free, compiled-only service packages."""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import platform
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from .service import Service, registry

_VERSION_RE = re.compile(r"\d+")


class CompiledServiceError(ImportError):
    """A compiled-only service package is invalid or cannot be loaded."""


def runtime_abi() -> str:
    return str(getattr(sys.implementation, "cache_tag", "") or "")


def runtime_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower().replace("amd64", "x86_64")
    if system == "darwin":
        return f"macos-{machine}"
    if system == "windows":
        return f"windows-{machine}"
    if system == "linux":
        return f"linux-{machine}"
    return f"{system}-{machine}"


def _abi_matches(current: str, candidate: object) -> bool:
    value = str(candidate or "").strip().lower().replace("-", "")
    active = current.lower().replace("-", "")
    # Hand-written manifests commonly use ``cp312`` while Python exposes
    # ``cpython-312`` as its cache tag. Accept both spellings (and the bare
    # version) without accepting a neighbouring ABI.
    version = active.removeprefix("cpython").removeprefix("python")
    return value in {active, version, f"cp{version}"}


def _platform_matches(current: str, candidate: object) -> bool:
    value = str(candidate or "").strip().lower().replace("-", "_")
    active = current.lower().replace("-", "_")
    if value in {active, "any"}:
        return True
    if value == "macos_universal2" and active.startswith("macos_"):
        return True
    return value.replace("amd64", "x86_64") == active.replace("amd64", "x86_64")


def _version_tuple(value: object) -> tuple[int, ...]:
    return tuple(int(part) for part in _VERSION_RE.findall(str(value or ""))) or (0,)


def _compatibility_error(metadata: dict[str, Any]) -> str:
    abis = metadata.get("python_abis")
    platforms = metadata.get("platforms")
    if not isinstance(abis, (list, tuple)) or not abis:
        return "service.toml must declare python_abis"
    if not isinstance(platforms, (list, tuple)) or not platforms:
        return "service.toml must declare platforms"
    abi = runtime_abi()
    if not any(_abi_matches(abi, item) for item in abis):
        return f"Python ABI {abi or 'unknown'} is not supported"
    platform_tag = runtime_platform()
    if not any(_platform_matches(platform_tag, item) for item in platforms):
        return f"platform {platform_tag} is not supported"
    from .. import __version__ as unidl_version

    current = _version_tuple(unidl_version)
    minimum = metadata.get("min_unidl")
    maximum = metadata.get("max_unidl")
    if minimum and current < _version_tuple(minimum):
        return f"UniDL {unidl_version} is older than the required {minimum}"
    if maximum and current > _version_tuple(maximum):
        return f"UniDL {unidl_version} is newer than the supported {maximum}"
    return ""


def _manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CompiledServiceError(f"could not read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise CompiledServiceError(f"{path.name} must contain a TOML table")
    return data


def _variant_manifest(
    package: str,
    root_path: Path,
    root_metadata: dict[str, Any],
) -> tuple[str, Path, dict[str, Any]]:
    """Choose one ABI/platform bundle from a multi-variant service package."""

    raw_dir = str(root_metadata.get("variants_dir", "") or "").strip()
    if not raw_dir:
        return package, root_path, root_metadata
    variants_root = root_path.parent / raw_dir
    if not variants_root.is_dir():
        raise CompiledServiceError(f"compiled service variants directory is missing: {variants_root}")
    candidates: list[tuple[str, Path, dict[str, Any]]] = []
    reasons: list[str] = []
    for child in sorted(variants_root.iterdir(), key=lambda path: path.name.casefold()):
        if not child.is_dir() or not child.name.isidentifier():
            continue
        manifest = child / "service.toml"
        if not manifest.is_file():
            continue
        try:
            metadata = _manifest(manifest)
        except CompiledServiceError as exc:
            reasons.append(f"{child.name}: {exc}")
            continue
        if str(metadata.get("implementation", "")).strip().lower() != "compiled-only":
            reasons.append(f"{child.name}: not compiled-only")
            continue
        compatibility = _compatibility_error(metadata)
        if compatibility:
            reasons.append(f"{child.name}: {compatibility}")
            continue
        candidates.append((package, manifest, metadata))
    if not candidates:
        detail = "; ".join(reasons[:4])
        suffix = f" ({detail})" if detail else ""
        raise CompiledServiceError(
            f"no compiled service variant matches Python ABI {runtime_abi() or 'unknown'} "
            f"and platform {runtime_platform()}{suffix}"
        )
    if len(candidates) > 1:
        names = ", ".join(item[1].parent.name for item in candidates)
        raise CompiledServiceError(f"multiple compiled service variants match this runtime: {names}")
    return candidates[0]


def register_compiled_services(
    package: str,
    manifest_name: str = "service.toml",
    manifest_path: Path | None = None,
) -> tuple[type[Service], ...]:
    """Load and register classes from a compiled-only service package."""

    if manifest_path is None:
        package_module = __import__(package, fromlist=["__path__"])
        locations = list(getattr(package_module, "__path__", ()))
        if not locations:
            raise CompiledServiceError(f"compiled service package {package} has no filesystem path")
        manifest_path = Path(locations[0]) / manifest_name
    metadata = _manifest(Path(manifest_path))
    if str(metadata.get("implementation", "")).strip().lower() != "compiled-only":
        raise CompiledServiceError("service.toml does not declare compiled-only")
    package, selected_manifest, metadata = _variant_manifest(package, Path(manifest_path), metadata)
    compatibility = "" if metadata.get("variants_dir") else _compatibility_error(metadata)
    if compatibility:
        raise CompiledServiceError(compatibility)
    module_name = str(metadata.get("module", "_service_native")).strip()
    entries = metadata.get("entry_classes", metadata.get("entry_class", ()))
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, (list, tuple)) or not entries:
        raise CompiledServiceError("service.toml must declare entry_classes")
    module_name_full = f"{package}.{module_name}"
    variant_dir = selected_manifest.parent
    module_path = next(
        (variant_dir / f"{module_name}{suffix}" for suffix in importlib.machinery.EXTENSION_SUFFIXES
         if (variant_dir / f"{module_name}{suffix}").is_file()),
        None,
    )
    try:
        if module_path is not None:
            # A variant's extension is loaded under the canonical package name.
            # AMC's compiled source uses ``from ...core``; importing it under a
            # deeper ``variants.<name>`` package would resolve that relative
            # import incorrectly. Load its sibling API extension the same way.
            api_path = next(
                (variant_dir / f"api{suffix}" for suffix in importlib.machinery.EXTENSION_SUFFIXES
                 if (variant_dir / f"api{suffix}").is_file()),
                None,
            )
            if api_path is not None and f"{package}.api" not in sys.modules:
                api_spec = importlib.util.spec_from_file_location(f"{package}.api", api_path)
                if api_spec is None or api_spec.loader is None:
                    raise CompiledServiceError(f"compiled API module {package}.api is unavailable")
                api_module = importlib.util.module_from_spec(api_spec)
                sys.modules[f"{package}.api"] = api_module
                api_spec.loader.exec_module(api_module)
            spec = importlib.util.spec_from_file_location(module_name_full, module_path)
            if spec is None or spec.loader is None:
                raise CompiledServiceError(f"compiled module {module_name_full} is unavailable")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name_full] = module
            spec.loader.exec_module(module)
        else:
            module = importlib.import_module(module_name_full)
    except Exception as exc:
        raise CompiledServiceError(f"compiled module {module_name_full} is unavailable: {exc}") from exc
    origin = str(getattr(module, "__file__", "") or "")
    if not any(origin.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES):
        raise CompiledServiceError(f"compiled module {package}.{module_name} is not a Python extension")
    registered: list[type[Service]] = []
    manifest_id = str(metadata.get("id", "") or "").strip().lower()
    manifest_name_value = str(metadata.get("name", "") or "").strip()
    for raw_name in entries:
        name = str(raw_name or "").strip()
        candidate = getattr(module, name, None)
        if not isinstance(candidate, type) or not issubclass(candidate, Service):
            raise CompiledServiceError(f"compiled module does not export Service {name!r}")
        service_id = str(getattr(candidate, "ID", "") or "").strip().lower()
        service_name = str(getattr(candidate, "NAME", "") or "").strip()
        if not service_id or not service_name or (manifest_id and service_id != manifest_id):
            raise CompiledServiceError("compiled Service identity does not match service.toml")
        if manifest_name_value and service_name != manifest_name_value:
            raise CompiledServiceError("compiled Service name does not match service.toml")
        existing = registry.get(service_id)
        if existing is not None and existing is not candidate:
            raise CompiledServiceError(f"service id {service_id!r} is already registered")
        if existing is None:
            registry.register(candidate)
        registered.append(candidate)
    if not registered:
        raise CompiledServiceError("compiled-only package exported no Service")
    return tuple(registered)


__all__ = ["CompiledServiceError", "register_compiled_services", "runtime_abi", "runtime_platform"]
