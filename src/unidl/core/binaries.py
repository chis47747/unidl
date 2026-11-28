"""Project-local fallback directory for external command-line tools.

UniDL deliberately prefers tools installed by the operating system.  The
project's ``binaries/`` directory is only appended to ``PATH`` as a fallback,
so existing ``shutil.which`` calls and child processes all use the same policy:
system PATH first, bundled project binaries second.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

_CONFIGURED: set[str] = set()
_REGISTRY_LOCK = threading.Lock()
_VERSION_CACHE_LOCK = threading.Lock()
_VERSION_CACHE: dict[tuple[str, tuple[str, ...]], str] = {}


@dataclass(frozen=True, slots=True)
class BinarySpec:
    """Description of an optional or service-registered external binary."""

    name: str
    candidates: tuple[str, ...] = ()
    version_args: tuple[str, ...] = ("--version",)
    install_hint: str = ""
    service_id: str = ""


_STANDARD_TOOLS: tuple[BinarySpec, ...] = (
    BinarySpec("ffmpeg", ("ffmpeg", "ffmpeg.exe"), ("-version",)),
    BinarySpec("ffprobe", ("ffprobe", "ffprobe.exe"), ("-version",)),
    BinarySpec("mkvmerge", ("mkvmerge", "mkvmerge.exe"), ("--version",)),
    BinarySpec("MP4Box", ("MP4Box", "mp4box", "MP4Box.exe"), ("-version",)),
    BinarySpec("shaka-packager", ("shaka-packager", "packager", "shaka-packager.exe"), ("--version",)),
    BinarySpec("dovi_tool", ("dovi_tool", "dovi_tool.exe"), ("--version",)),
    BinarySpec("hdr10plus_tool", ("hdr10plus_tool", "hdr10plus_tool.exe"), ("--version",)),
    BinarySpec("aria2c", ("aria2c", "aria2c.exe"), ("--version",)),
)

_REGISTERED_BINARIES: dict[str, BinarySpec] = {}


def register_binary(
    name: str,
    *,
    candidates: tuple[str, ...] | list[str] = (),
    version_args: tuple[str, ...] | list[str] = ("--version",),
    install_hint: str = "",
    service_id: str = "",
) -> BinarySpec:
    """Register a custom external binary required or used by a service plugin."""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Binary name must not be empty.")
    cand_tuple = tuple(str(item).strip() for item in (candidates or (clean_name,)) if str(item).strip())
    if not cand_tuple:
        cand_tuple = (clean_name,)
    spec = BinarySpec(
        name=clean_name,
        candidates=cand_tuple,
        version_args=tuple(str(arg) for arg in version_args),
        install_hint=str(install_hint or "").strip(),
        service_id=str(service_id or "").strip(),
    )
    with _REGISTRY_LOCK:
        _REGISTERED_BINARIES[clean_name.casefold()] = spec
    return spec


def registered_binaries() -> tuple[BinarySpec, ...]:
    """Return all service-registered custom external binaries."""
    with _REGISTRY_LOCK:
        return tuple(_REGISTERED_BINARIES.values())


def clear_registered_binaries() -> None:
    """Clear service-registered binaries (primarily for isolated tests)."""
    with _REGISTRY_LOCK:
        _REGISTERED_BINARIES.clear()


def resolve_binary(spec_or_name: BinarySpec | str) -> str | None:
    """Locate an executable on PATH (including configured project binaries/)."""
    if isinstance(spec_or_name, BinarySpec):
        candidates = spec_or_name.candidates or (spec_or_name.name,)
    else:
        key = str(spec_or_name or "").strip().casefold()
        with _REGISTRY_LOCK:
            reg = _REGISTERED_BINARIES.get(key)
        candidates = reg.candidates if reg is not None else (str(spec_or_name or "").strip(),)
    for candidate in candidates:
        if not candidate:
            continue
        found = shutil.which(candidate)
        if found:
            return found
    return None


def probe_binary_version(
    executable: str,
    version_args: tuple[str, ...] = ("--version",),
    *,
    timeout: float = 2.0,
    use_cache: bool = True,
) -> str:
    """Return the concise first non-empty version line for ``executable``."""
    cache_key = (executable, tuple(version_args))
    if use_cache:
        with _VERSION_CACHE_LOCK:
            cached = _VERSION_CACHE.get(cache_key)
        if cached is not None:
            return cached
    version = ""
    try:
        proc = subprocess.run(
            [executable, *version_args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
        for line in combined.splitlines():
            clean = line.strip()
            if clean:
                version = clean[:160]
                break
    except (OSError, subprocess.SubprocessError):
        version = ""
    if use_cache:
        with _VERSION_CACHE_LOCK:
            _VERSION_CACHE[cache_key] = version
    return version


def collect_tool_versions(
    *,
    include_registered: bool = True,
    use_cache: bool = True,
    timeout: float = 2.0,
) -> dict[str, str]:
    """Collect versions of all installed standard and registered CLI tools."""
    specs: list[BinarySpec] = list(_STANDARD_TOOLS)
    if include_registered:
        seen = {spec.name.casefold() for spec in specs}
        for spec in registered_binaries():
            if spec.name.casefold() not in seen:
                specs.append(spec)
                seen.add(spec.name.casefold())
    versions: dict[str, str] = {}
    for spec in specs:
        exe = resolve_binary(spec)
        if not exe:
            continue
        ver = probe_binary_version(exe, spec.version_args, timeout=timeout, use_cache=use_cache)
        versions[spec.name] = ver or exe
    return versions


def binary_directories(project_home: str | Path | None = None) -> tuple[Path, ...]:
    """Return candidate local binary directories in fallback order."""
    candidates: list[Path] = []
    if project_home:
        candidates.append(Path(project_home).expanduser() / "binaries")

    env_home = os.environ.get("UNIDL_HOME", "").strip()
    if env_home:
        candidates.append(Path(env_home).expanduser() / "binaries")

    # Running from a checkout is the normal development/portable layout.
    candidates.append(Path.cwd() / "binaries")

    # Also cover an installed source checkout (src/unidl/core -> repository root).
    try:
        candidates.append(Path(__file__).resolve().parents[3] / "binaries")
    except IndexError:  # pragma: no cover - defensive for unusual loaders
        pass

    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        result.append(resolved)
    return tuple(result)


def configure_binary_path(project_home: str | Path | None = None) -> tuple[Path, ...]:
    """Append local binary directories to PATH without disturbing system tools."""
    directories = binary_directories(project_home)
    existing = os.environ.get("PATH", "")
    parts = existing.split(os.pathsep) if existing else []
    normalized = {os.path.normcase(str(Path(part).expanduser())) for part in parts if part}
    additions: list[str] = []
    for directory in directories:
        key = os.path.normcase(str(directory))
        if key in normalized or key in _CONFIGURED:
            continue
        # Keep nonexistent directories in PATH: a user may place a tool there
        # after UniDL starts, and the next subprocess lookup should see it.
        additions.append(str(directory))
        _CONFIGURED.add(key)
    if additions:
        os.environ["PATH"] = os.pathsep.join([*parts, *additions])
    return directories


__all__ = [
    "BinarySpec",
    "binary_directories",
    "clear_registered_binaries",
    "collect_tool_versions",
    "configure_binary_path",
    "probe_binary_version",
    "register_binary",
    "registered_binaries",
    "resolve_binary",
]
