"""Small, dependency-free adapters for the operating system clipboard.

Textual's ``App.clipboard`` is an in-process cache.  It is useful for content
copied by UniDL itself, but it does not contain text copied from Explorer, a
browser, or another terminal window. The TUI reads and writes native clipboard
text through this module. Ctrl+V falls back to Textual's cache when native
reading is unavailable; an OSC 52 write alone is not confirmed OS clipboard
access.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time


def _windows_clipboard_apis():
    """Declare pointer-sized Win32 signatures before using any handles."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = (
        (user32.CreateWindowExW, [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                 wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                 wintypes.HINSTANCE, wintypes.LPVOID], wintypes.HWND),
        (user32.DestroyWindow, [wintypes.HWND], wintypes.BOOL),
        (user32.OpenClipboard, [wintypes.HWND], wintypes.BOOL),
        (user32.EmptyClipboard, [], wintypes.BOOL),
        (user32.SetClipboardData, [wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE),
        (user32.CloseClipboard, [], wintypes.BOOL),
        (kernel32.GlobalAlloc, [wintypes.UINT, ctypes.c_size_t], wintypes.HANDLE),
        (kernel32.GlobalLock, [wintypes.HANDLE], wintypes.LPVOID),
        (kernel32.GlobalUnlock, [wintypes.HANDLE], wintypes.BOOL),
        (kernel32.GlobalFree, [wintypes.HANDLE], wintypes.HANDLE),
    )
    for function, arguments, result in signatures:
        function.argtypes = arguments
        function.restype = result
    return user32, kernel32


def _write_windows_clipboard(text: str) -> bool:
    """Transfer an owned UTF-16 buffer to Windows, not to the terminal cache."""
    import ctypes

    # Embedded NUL would silently truncate CF_UNICODETEXT.
    if "\0" in text:
        return False
    try:
        payload = (text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
                   .encode("utf-16-le") + b"\0\0")
        user32, kernel32 = _windows_clipboard_apis()
    except (AttributeError, OSError, UnicodeError):
        return False
    window = memory = None
    opened = False
    try:
        # OpenClipboard(NULL) followed by EmptyClipboard has no owner and can
        # make SetClipboardData fail. A hidden built-in window also works when
        # Windows Terminal does not expose a console window to this process.
        window = user32.CreateWindowExW(
            0, "STATIC", "UniDL Clipboard", 0x80000000,
            0, 0, 0, 0, None, None, None, None,
        )
        if not window:
            return False
        memory = kernel32.GlobalAlloc(0x0002, len(payload))  # GMEM_MOVEABLE
        if not memory:
            return False
        address = kernel32.GlobalLock(memory)
        if not address:
            return False
        try:
            ctypes.memmove(address, payload, len(payload))
        finally:
            kernel32.GlobalUnlock(memory)
        for attempt in range(5):
            if user32.OpenClipboard(window):
                opened = True
                break
            if attempt < 4:
                time.sleep(0.02)
        if not opened or not user32.EmptyClipboard():
            return False
        if not user32.SetClipboardData(13, memory):  # CF_UNICODETEXT
            return False
        memory = None  # Windows owns the allocation after SetClipboardData.
        return True
    except OSError:
        return False
    finally:
        if opened:
            user32.CloseClipboard()
        if memory:
            kernel32.GlobalFree(memory)
        if window:
            user32.DestroyWindow(window)


def write_system_clipboard(text: str) -> bool:
    """Return true only when an OS clipboard backend accepted the text."""
    if sys.platform == "win32":
        return _write_windows_clipboard(text)
    commands = (
        (["pbcopy"],)
        if sys.platform == "darwin"
        else (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"])
    )
    for command in commands:
        executable = shutil.which(command[0])
        if executable is None:
            continue
        try:
            result = subprocess.run(
                [executable, *command[1:]], input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=1.0, check=False,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError):
            continue
        if result.returncode == 0:
            return True
    return False


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
