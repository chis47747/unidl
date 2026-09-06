from __future__ import annotations

import os
import sys
import unicodedata
from typing import IO


class ColorLevel:
    NONE = 0
    ANSI16 = 1
    ANSI256 = 2
    TRUECOLOR = 3


_TRUECOLOR_TERMS = {"truecolor", "24bit"}

_LEVEL_OVERRIDE: int | None = None
_WINDOWS_VT_READY: bool | None = None


def _enable_windows_vt() -> bool:
    """Turn on ANSI escape processing for legacy Windows consoles.

    Windows Terminal and PowerShell 7 already handle VT sequences, but conhost
    needs ENABLE_VIRTUAL_TERMINAL_PROCESSING or every escape prints literally.
    """
    global _WINDOWS_VT_READY
    if _WINDOWS_VT_READY is not None:
        return _WINDOWS_VT_READY
    if sys.platform != "win32":
        _WINDOWS_VT_READY = True
        return True
    _WINDOWS_VT_READY = False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        enable_vt = 0x0004
        for handle_id in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(handle_id)
            if handle in (0, -1):
                continue
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            if kernel32.SetConsoleMode(handle, mode.value | enable_vt):
                _WINDOWS_VT_READY = True
    except Exception:
        _WINDOWS_VT_READY = False
    return _WINDOWS_VT_READY


_BASIC_TERMS = {"xterm", "screen", "vt100", "vt220", "ansi", "linux", "rxvt", "cygwin"}


def detect_color_level() -> int:
    """How rich the escape codes may be, independent of whether we emit any.

    Capability and enablement are deliberately separate: a caller that forces
    color on (``--force-ansi-console``, a captured render, a test) still needs
    real sequences, so this never returns NONE just because stdout is a pipe.
    """
    if _LEVEL_OVERRIDE is not None:
        return _LEVEL_OVERRIDE

    term = os.environ.get("TERM", "").lower()
    if term == "dumb":
        return ColorLevel.NONE
    if not _enable_windows_vt():
        return ColorLevel.NONE
    if os.environ.get("COLORTERM", "").lower() in _TRUECOLOR_TERMS:
        return ColorLevel.TRUECOLOR
    if "256color" in term or "truecolor" in term:
        return ColorLevel.ANSI256
    if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
        return ColorLevel.TRUECOLOR
    if term.split("-", 1)[0] in _BASIC_TERMS:
        return ColorLevel.ANSI16
    return ColorLevel.ANSI256


def set_color_level(level: int | None) -> None:
    """Pin the palette to a level. Pass None to return to auto-detection."""
    global _LEVEL_OVERRIDE
    _LEVEL_OVERRIDE = level
    _PaletteMeta.invalidate()


# name -> (truecolor rgb, 256-color index, basic SGR)
_COLORS: dict[str, tuple[tuple[int, int, int], int, int]] = {
    "muted": ((128, 134, 145), 245, 90),
    "red": ((233, 86, 78), 160, 31),
    "green": ((87, 187, 112), 35, 32),
    "yellow": ((214, 165, 68), 178, 33),
    "blue": ((78, 146, 226), 33, 34),
    "magenta": ((176, 118, 205), 133, 35),
    "cyan": ((72, 173, 178), 37, 36),
}

_ATTRS = {"bold": "\033[1m", "dim": "\033[2m", "reset": "\033[0m"}


def _sequence(name: str, level: int) -> str:
    # ``colors=True`` is an explicit request from callers such as
    # ``--force-ansi-console`` and captured render tests.  Keep a usable
    # 256-colour palette even when TERM/NO_COLOR reports no automatic
    # capability; ``paint(..., enabled=None)`` still suppresses it normally.
    if level <= ColorLevel.NONE:
        level = ColorLevel.ANSI256
    rgb, index, basic = _COLORS[name]
    if level >= ColorLevel.TRUECOLOR:
        return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    if level >= ColorLevel.ANSI256:
        return f"\033[38;5;{index}m"
    return f"\033[{basic}m"


class _PaletteMeta(type):
    _cache: dict[str, str] = {}
    _cached_level: int | None = None

    @classmethod
    def invalidate(mcs) -> None:
        mcs._cache = {}
        mcs._cached_level = None

    def __getattr__(cls, name: str) -> str:
        if name in _ATTRS:
            return _ATTRS[name]
        if name not in _COLORS:
            raise AttributeError(name)
        level = detect_color_level()
        if level != _PaletteMeta._cached_level:
            _PaletteMeta._cache = {}
            _PaletteMeta._cached_level = level
        cached = _PaletteMeta._cache.get(name)
        if cached is None:
            cached = _sequence(name, level)
            _PaletteMeta._cache[name] = cached
        return cached


class Palette(metaclass=_PaletteMeta):
    """Semantic colors that degrade from truecolor down to basic ANSI."""

    bold = "\033[1m"
    dim = "\033[2m"
    reset = "\033[0m"


def color_enabled(stream: IO[str] | None = None) -> bool:
    """Whether color should actually be written to `stream` (default stdout)."""
    value = os.environ.get("UNIDOWN_COLOR", "").lower()
    if value in {"1", "true", "yes", "always"}:
        return detect_color_level() > ColorLevel.NONE
    if value in {"0", "false", "no", "never"} or os.environ.get("NO_COLOR"):
        return False
    if detect_color_level() <= ColorLevel.NONE:
        return False
    try:
        return bool((stream or sys.stdout).isatty())
    except (AttributeError, ValueError):
        return False


def stderr_color_enabled() -> bool:
    """Color for the diagnostics channel, decided independently of stdout.

    Keeps `unidl ... > out.txt` colorful on screen and `2> err.log` clean.
    """
    return color_enabled(sys.stderr)


def paint(text: str, color: str | None = None, enabled: bool | None = None) -> str:
    if not color:
        return text
    if enabled is None:
        enabled = color_enabled()
    if not enabled:
        return text
    return f"{color}{text}{Palette.reset}"


_ZERO_WIDTH_CATEGORIES = {"Mn", "Me", "Cf"}


def char_width(char: str) -> int:
    """Terminal cells occupied by one character."""
    code = ord(char)
    if code < 32 or 0x7F <= code < 0xA0:
        return 0
    if unicodedata.combining(char):
        return 0
    if unicodedata.category(char) in _ZERO_WIDTH_CATEGORIES:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def cell_width(text: str) -> int:
    """Terminal cells occupied by a string, counting CJK glyphs as two."""
    return sum(char_width(char) for char in text)
