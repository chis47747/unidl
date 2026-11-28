"""User-defined post-download hook scripts with metadata environment variables.

Hooks run after a delivery finishes (on success, and optionally on failure).
Metadata is passed via ``UNIDL_*`` environment variables and optional
``{variable}`` placeholders in the configured command template. Hooks inherit
the UniDL process environment.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .diagnostics import current_recorder

DEFAULT_HOOK_TIMEOUT = 120.0
_MAX_OUTPUT_CHARS = 2000


@dataclass(frozen=True, slots=True)
class HookResult:
    """Outcome of running a post-download hook command."""

    executed: bool
    ok: bool
    returncode: int | None = None
    timed_out: bool = False
    elapsed_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    command: tuple[str, ...] = ()


def build_hook_metadata(
    *,
    status: str,
    exit_code: int,
    service: str = "",
    title: Any = None,
    save_name: str = "",
    output_dir: Path | str | None = None,
    artifacts: Sequence[Path | str] = (),
    quality: str = "",
    failure: str = "",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Assemble safe, stringified metadata variables for a post-download hook."""
    artifact_paths = [str(Path(item)) for item in artifacts if item]
    primary_file = artifact_paths[0] if artifact_paths else ""
    out_dir = str(Path(output_dir)) if output_dir else (str(Path(primary_file).parent) if primary_file else "")

    title_name = str(getattr(title, "name", "") or "")
    episode_name = str(getattr(title, "episode_name", "") or "")
    kind_raw = getattr(title, "kind", "")
    title_kind = getattr(kind_raw, "value", str(kind_raw or ""))
    season = getattr(title, "season", None)
    episode = getattr(title, "episode", None)
    year = str(getattr(title, "year", "") or "")
    air_date = str(getattr(title, "air_date", "") or "")
    edition = str(getattr(title, "edition", "") or "")
    svc = str(service or getattr(title, "service", "") or "")

    meta: dict[str, str] = {
        "status": str(status or ""),
        "exit_code": str(int(exit_code)),
        "service": svc,
        "title": title_name,
        "title_kind": str(title_kind),
        "episode_name": episode_name,
        "season": f"{int(season):02d}" if season is not None else "",
        "episode": f"{int(episode):02d}" if episode is not None else "",
        "year": year,
        "air_date": air_date,
        "edition": edition,
        "save_name": str(save_name or ""),
        "quality": str(quality or ""),
        "output_dir": out_dir,
        "output_file": primary_file,
        "output_path": primary_file,
        "artifacts_json": json.dumps(artifact_paths, ensure_ascii=False),
        "artifact_count": str(len(artifact_paths)),
        "failure": str(failure or ""),
    }
    if extra:
        for key, value in extra.items():
            clean_key = str(key or "").strip().lower()
            if clean_key and clean_key not in meta:
                meta[clean_key] = str(value if value is not None else "")
    return meta


def _render_command_tokens(command_template: str, metadata: Mapping[str, str]) -> list[str]:
    """Parse ``command_template`` into argv tokens and expand ``{var}`` safely.

    Tokenising *before* placeholder expansion ensures spaces or shell
    metacharacters in filenames/titles never split arguments or inject shell
    syntax (no ``shell=True`` is used).
    """
    raw = str(command_template or "").strip()
    if not raw:
        return []
    tokens = shlex.split(raw, posix=(os.name != "nt"))
    expanded: list[str] = []
    for token in tokens:
        rendered = token
        for key, value in metadata.items():
            placeholder = "{" + key + "}"
            if placeholder in rendered:
                rendered = rendered.replace(placeholder, value)
        expanded.append(rendered)
    return expanded


def run_post_download_hook(
    command_template: str | None,
    metadata: Mapping[str, str],
    *,
    timeout: float = DEFAULT_HOOK_TIMEOUT,
    cwd: Path | str | None = None,
    log: Callable[[str], None] | None = None,
) -> HookResult:
    """Execute a configured post-download script/command with ``UNIDL_*`` env vars."""
    raw = str(command_template or "").strip()
    if not raw:
        return HookResult(executed=False, ok=True)

    try:
        argv = _render_command_tokens(raw, metadata)
    except ValueError as exc:
        msg = f"post-download hook syntax error: {exc}"
        if log:
            log(f"warning: {msg}")
        return HookResult(executed=False, ok=False, error=msg)

    if not argv:
        return HookResult(executed=False, ok=True)

    # Expand ~ on executable path when a user enters ~/scripts/post.sh
    argv[0] = str(Path(argv[0]).expanduser()) if argv[0].startswith("~") else argv[0]

    env = dict(os.environ)
    for key, value in metadata.items():
        env[f"UNIDL_{key.upper()}"] = value

    bounded_timeout = max(1.0, min(3600.0, float(timeout or DEFAULT_HOOK_TIMEOUT)))
    work_dir = str(Path(cwd)) if cwd and Path(cwd).is_dir() else None
    started = time.monotonic()

    try:
        proc = subprocess.run(
            argv,
            env=env,
            cwd=work_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=bounded_timeout,
            check=False,
        )
        elapsed = time.monotonic() - started
        stdout = (proc.stdout or "").strip()[-_MAX_OUTPUT_CHARS:]
        stderr = (proc.stderr or "").strip()[-_MAX_OUTPUT_CHARS:]
        ok = proc.returncode == 0
        result = HookResult(
            executed=True,
            ok=ok,
            returncode=proc.returncode,
            elapsed_seconds=round(elapsed, 3),
            stdout=stdout,
            stderr=stderr,
            error="" if ok else (stderr.splitlines()[-1] if stderr else f"exit code {proc.returncode}"),
            command=tuple(argv),
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        result = HookResult(
            executed=True,
            ok=False,
            timed_out=True,
            elapsed_seconds=round(elapsed, 3),
            error=f"timed out after {bounded_timeout:.0f}s",
            command=tuple(argv),
        )
    except OSError as exc:
        elapsed = time.monotonic() - started
        result = HookResult(
            executed=True,
            ok=False,
            elapsed_seconds=round(elapsed, 3),
            error=str(exc),
            command=tuple(argv),
        )

    recorder = current_recorder()
    if recorder is not None:
        recorder.record_event(
            "hook.post_download",
            ok=result.ok,
            returncode=result.returncode,
            timed_out=result.timed_out,
            elapsed_seconds=result.elapsed_seconds,
            error=result.error,
            status=metadata.get("status", ""),
            output_file=metadata.get("output_file", ""),
        )

    if log is not None:
        if result.ok:
            log(f"post-download hook finished ({result.elapsed_seconds:.1f}s)")
            if result.stdout:
                for line in result.stdout.splitlines()[:6]:
                    log(f"  hook: {line}")
        else:
            log(f"warning: post-download hook failed: {result.error}")
            if result.stderr:
                for line in result.stderr.splitlines()[:4]:
                    log(f"  hook stderr: {line}")
    return result


__all__ = [
    "DEFAULT_HOOK_TIMEOUT",
    "HookResult",
    "build_hook_metadata",
    "run_post_download_hook",
]
