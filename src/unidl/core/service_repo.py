"""Git-backed external service plugin repository manager.

Manages optional user-configured Git repositories containing UniDL service
packages under ``<home>/service_repos/<slug>``.  Pulls respect a configurable
TTL cache and refuse to overwrite repositories with uncommitted local edits
(dirty-clone guard).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SYNC_MARKER = ".unidl_sync_ts"


@dataclass(frozen=True, slots=True)
class ServiceRepoSpec:
    """Declarative configuration for one Git service repository."""

    url: str
    name: str
    branch: str = ""
    subdir: str = ""
    ttl_hours: float = 24.0
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class RepoSyncResult:
    """Outcome of synchronizing one Git service repository."""

    spec: ServiceRepoSpec
    path: Path
    action: str  # cloned | updated | fresh | dirty | disabled | error
    ok: bool
    detail: str = ""


def service_repos_dir(home: Path | None = None) -> Path:
    """Return the root folder holding cloned external service repositories."""
    if home is None:
        home = Path(os.environ.get("UNIDL_HOME", Path.home() / ".unidl"))
    return Path(home).expanduser() / "service_repos"


def _slug_from_url(url: str) -> str:
    text = str(url or "").strip().rstrip("/")
    parsed = urlsplit(text)
    tail = parsed.path.rstrip("/").rsplit("/", 1)[-1] if parsed.path else text.rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    slug = _SLUG_RE.sub("-", tail).strip("-._")
    return slug or "service-repo"


def parse_service_repos(raw: Any) -> list[ServiceRepoSpec]:
    """Parse ``service_repos`` entries from ``unidl.yaml`` (URL strings or dicts)."""
    if not raw or not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    specs: list[ServiceRepoSpec] = []
    seen_names: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            url = item.strip()
            if not url:
                continue
            base_name = _slug_from_url(url)
            name = _unique_slug(base_name, seen_names)
            specs.append(ServiceRepoSpec(url=url, name=name))
        elif isinstance(item, Mapping):
            url = str(item.get("url") or item.get("repo") or "").strip()
            if not url:
                continue
            raw_name = str(item.get("name") or "").strip()
            base_name = _SLUG_RE.sub("-", raw_name).strip("-._") if raw_name else _slug_from_url(url)
            name = _unique_slug(base_name or "service-repo", seen_names)
            branch = str(item.get("branch") or item.get("ref") or "").strip()
            subdir = str(item.get("subdir") or item.get("path") or "").strip().strip("/\\")
            raw_ttl = item.get("ttl_hours", 24.0)
            try:
                ttl_hours = max(0.0, float(raw_ttl))
            except (TypeError, ValueError):
                ttl_hours = 24.0
            enabled_raw = item.get("enabled", True)
            enabled = (
                enabled_raw.strip().lower() not in {"0", "false", "no", "off"}
                if isinstance(enabled_raw, str)
                else bool(enabled_raw)
            )
            specs.append(
                ServiceRepoSpec(
                    url=url,
                    name=name,
                    branch=branch,
                    subdir=subdir,
                    ttl_hours=ttl_hours,
                    enabled=enabled,
                )
            )
    return specs


def _unique_slug(base: str, seen: set[str]) -> str:
    candidate = base
    idx = 2
    while candidate.casefold() in seen:
        candidate = f"{base}-{idx}"
        idx += 1
    seen.add(candidate.casefold())
    return candidate


def _resolve_repo_service_root(repo_dir: Path, subdir: str = "") -> Path | None:
    if not repo_dir.is_dir():
        return None
    if subdir:
        target = (repo_dir / subdir).resolve()
        try:
            target.relative_to(repo_dir.resolve())
        except ValueError:
            return None
        return target if target.is_dir() else None
    for candidate in (
        repo_dir / "src" / "unidl" / "services",
        repo_dir / "services",
        repo_dir,
    ):
        if candidate.is_dir():
            if candidate != repo_dir or any(
                child.is_dir() and not child.name.startswith((".", "_"))
                for child in candidate.iterdir()
            ):
                return candidate
    return None


def service_repo_roots(
    home: Path | None = None,
    specs: Sequence[ServiceRepoSpec] | None = None,
) -> list[Path]:
    """Return discoverable service directories inside cloned Git repositories."""
    base = service_repos_dir(home)
    if not base.is_dir():
        return []
    roots: list[Path] = []
    if specs is not None:
        for spec in specs:
            if not spec.enabled:
                continue
            resolved = _resolve_repo_service_root(base / spec.name, spec.subdir)
            if resolved is not None and resolved not in roots:
                roots.append(resolved)
        return roots
    for child in sorted(base.iterdir(), key=lambda p: p.name.casefold()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        resolved = _resolve_repo_service_root(child)
        if resolved is not None and resolved not in roots:
            roots.append(resolved)
    return roots


def _run_git(argv: list[str], *, cwd: Path | None = None, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return subprocess.run(
        ["git", *argv],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        env=env,
        check=False,
    )


def is_dirty_git_repo(repo_dir: Path) -> bool:
    """Return True if ``repo_dir`` has tracked modifications or untracked files."""
    if not (repo_dir / ".git").exists():
        return False
    proc = _run_git(["status", "--porcelain"], cwd=repo_dir, timeout=15.0)
    if proc.returncode != 0:
        return True
    lines = [
        line
        for line in proc.stdout.splitlines()
        if line.strip() and not line.strip().endswith(_SYNC_MARKER)
    ]
    return bool(lines)


def _read_sync_ts(repo_dir: Path) -> float | None:
    marker = repo_dir / _SYNC_MARKER
    try:
        return float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_sync_ts(repo_dir: Path, ts: float) -> None:
    marker = repo_dir / _SYNC_MARKER
    try:
        marker.write_text(f"{ts:.3f}\n", encoding="utf-8")
    except OSError:
        pass


def _normalize_git_url(url: str) -> str:
    text = str(url or "").strip().rstrip("/")
    if text.lower().endswith(".git"):
        text = text[:-4]
    return text


def _git_origin_url(repo_dir: Path) -> str:
    try:
        proc = _run_git(["remote", "get-url", "origin"], cwd=repo_dir, timeout=15.0)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def configured_repo_specs(config: Any = None) -> list[ServiceRepoSpec]:
    """Read enabled repository choices from the active configuration only."""
    raw = config.service_repos() if callable(getattr(config, "service_repos", None)) else []
    return parse_service_repos(raw)


def refresh_service_repo(
    spec: ServiceRepoSpec,
    base_dir: Path,
    *,
    force: bool = False,
    now: float | None = None,
) -> RepoSyncResult:
    """Clone or fast-forward pull one external service repository safely."""
    target = Path(base_dir) / spec.name
    if not spec.enabled:
        return RepoSyncResult(spec=spec, path=target, action="disabled", ok=True, detail="disabled")

    ts = time.time() if now is None else float(now)
    try:
        Path(base_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return RepoSyncResult(spec=spec, path=target, action="error", ok=False, detail=str(exc))

    if (target.exists() or target.is_symlink()) and not target.is_dir():
        return RepoSyncResult(
            spec=spec,
            path=target,
            action="error",
            ok=False,
            detail=f"Target path already exists and is not a directory: {target}",
        )

    if not (target / ".git").exists():
        if target.exists():
            try:
                non_empty = any(target.iterdir())
            except OSError as exc:
                return RepoSyncResult(spec=spec, path=target, action="error", ok=False, detail=str(exc))
            if non_empty:
                return RepoSyncResult(
                    spec=spec,
                    path=target,
                    action="error",
                    ok=False,
                    detail=f"Target directory already exists and is not a git repository: {target}",
                )
        cmd = ["clone", "--depth", "1"]
        if spec.branch:
            cmd.extend(["--branch", spec.branch])
        cmd.extend(["--", spec.url, str(target)])
        try:
            proc = _run_git(cmd)
        except (OSError, subprocess.SubprocessError) as exc:
            return RepoSyncResult(spec=spec, path=target, action="error", ok=False, detail=str(exc))
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:400] or f"git clone exited {proc.returncode}"
            return RepoSyncResult(spec=spec, path=target, action="error", ok=False, detail=detail)
        _write_sync_ts(target, ts)
        return RepoSyncResult(spec=spec, path=target, action="cloned", ok=True, detail="cloned")

    actual_origin = _git_origin_url(target)
    if actual_origin and _normalize_git_url(actual_origin) != _normalize_git_url(spec.url):
        return RepoSyncResult(
            spec=spec,
            path=target,
            action="error",
            ok=False,
            detail=(
                f"Existing clone origin ({actual_origin}) does not match configured URL ({spec.url}); "
                f"remove {target} or use a different local folder name."
            ),
        )

    if not force and spec.ttl_hours > 0:
        last_sync = _read_sync_ts(target)
        if last_sync is not None and (ts - last_sync) < spec.ttl_hours * 3600.0:
            return RepoSyncResult(spec=spec, path=target, action="fresh", ok=True, detail="within TTL cache")

    if is_dirty_git_repo(target):
        return RepoSyncResult(
            spec=spec,
            path=target,
            action="dirty",
            ok=False,
            detail="Repository has uncommitted local changes; skipping pull to protect local edits.",
        )

    pull_cmd = ["pull", "--ff-only"]
    if spec.branch:
        pull_cmd.extend(["--", "origin", spec.branch])
    try:
        proc = _run_git(pull_cmd, cwd=target)
    except (OSError, subprocess.SubprocessError) as exc:
        return RepoSyncResult(spec=spec, path=target, action="error", ok=False, detail=str(exc))
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:400] or f"git pull exited {proc.returncode}"
        return RepoSyncResult(spec=spec, path=target, action="error", ok=False, detail=detail)
    _write_sync_ts(target, ts)
    summary = (proc.stdout or "").strip().splitlines()
    return RepoSyncResult(
        spec=spec,
        path=target,
        action="updated",
        ok=True,
        detail=summary[-1][:200] if summary else "updated",
    )


def refresh_service_repos(config: Any, *, force: bool = False) -> list[RepoSyncResult]:
    """Synchronize all Git service repositories declared in ``config``."""
    specs = configured_repo_specs(config)
    base = service_repos_dir(getattr(getattr(config, "paths", None), "home", None))
    return [refresh_service_repo(spec, base, force=force) for spec in specs]


__all__ = [
    "RepoSyncResult",
    "ServiceRepoSpec",
    "configured_repo_specs",
    "is_dirty_git_repo",
    "parse_service_repos",
    "refresh_service_repo",
    "refresh_service_repos",
    "service_repo_roots",
    "service_repos_dir",
]
