"""Small, dependency-free adapters for the operating system clipboard.

Textual's ``App.clipboard`` is an in-process cache.  It is useful for content
copied by UniDL itself, but it does not contain text copied from Explorer, a
browser, or another terminal window.  The TUI uses this module to read the
native clipboard when Ctrl+V is pressed and falls back to Textual's cache when
the platform clipboard is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


def _run(command: list[str]) -> str | None:
    """Run one clipboard command, returning ``None`` when it is unavailable."""

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=1.0,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def read_system_clipboard() -> str | None:
    """Read text from the native clipboard for the current desktop platform."""

    if sys.platform == "win32":
        for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
            executable = shutil.which(name)
            if executable is None:
                continue
            value = _run(
                [
                    executable,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-STA",
                    "-Command",
                    "[Console]::Out.Write((Get-Clipboard -Raw))",
                ]
            )
            if value is not None:
                return value
        return None

    if sys.platform == "darwin":
        executable = shutil.which("pbpaste")
        return _run([executable]) if executable else None

    # Wayland is preferred, followed by the two common X11 clipboard tools.
    for command in (
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    ):
        if shutil.which(command[0]) is None:
            continue
        value = _run(command)
        if value is not None:
            return value
    return None


def read_clipboard(app: object) -> str:
    """Read the system clipboard, falling back to Textual's app clipboard."""

    system = read_system_clipboard()
    if system is not None:
        return system
    return str(getattr(app, "clipboard", "") or "")
