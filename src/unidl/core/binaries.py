"""Project-local fallback directory for external command-line tools.

UniDL deliberately prefers tools installed by the operating system.  The
project's ``binaries/`` directory is only appended to ``PATH`` as a fallback,
so existing ``shutil.which`` calls and child processes all use the same policy:
system PATH first, bundled project binaries second.
"""

from __future__ import annotations

import os
from pathlib import Path

_CONFIGURED: set[str] = set()


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


__all__ = ["binary_directories", "configure_binary_path"]
