"""Working out whether the terminal is light or dark.

There is no portable way to ask, so this tries the sources that exist, cheapest
and most reliable first, and says plainly when it does not know. "Do not know"
matters: guessing light on a dark terminal is worse than staying on the default,
because the default is at least the thing the user last saw.

Nothing here blocks. A terminal *can* be asked directly with an OSC 11 query,
but that means writing to the tty and waiting for a reply, which is a hang
waiting to happen inside a full-screen app - and would have to happen before
Textual takes the terminal over anyway.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

DARK = "dark"
LIGHT = "light"

#: How long the *desktop* answer is trusted for. That question forks a process, and
#: detection is asked once per line written to the log - a screenful of them was a
#: screenful of `defaults read` calls. Still re-asked, because "follow the system"
#: has to notice the system changing; just not thousands of times a minute.
#:
#: Only that question is cached. Caching the whole answer meant an environment that
#: had just changed was not looked at: reading UNIDL_APPEARANCE is a dict lookup,
#: and the cache was in front of it, so setting the override - or switching terminal
#: profile - changed nothing until the cache happened to expire.
_CACHE_SECONDS = 2.0
_CACHED_AT = -_CACHE_SECONDS
_CACHED: str | None = None

#: terminal themes whose names give it away. Checked as substrings, lowercased.
_NAME_HINTS = (
    ("light", LIGHT),
    ("day", LIGHT),
    ("solarized-light", LIGHT),
    ("dark", DARK),
    ("night", DARK),
)


def detect() -> str | None:
    """``"dark"``, ``"light"``, or None when nothing reliable said.

    Order is deliberate: an explicit override beats everything, then what the
    terminal itself reports, then the desktop's own setting. The desktop comes
    last because a dark-mode Mac is routinely used with a light terminal.
    """
    # The three cheap questions are asked every time: they are environment reads,
    # and the whole point of an override is that it takes effect when it is set.
    for source in (_from_override, _from_colorfgbg, _from_profile_name):
        found = source()
        if found in (DARK, LIGHT):
            return found
    return _desktop_cached()


def _desktop_cached() -> str | None:
    """The desktop's own setting, asked at most once every few seconds."""
    global _CACHED_AT, _CACHED
    now = time.monotonic()
    if now - _CACHED_AT < _CACHE_SECONDS:
        return _CACHED
    found = _from_desktop()
    _CACHED_AT, _CACHED = now, found if found in (DARK, LIGHT) else None
    return _CACHED


def _from_override() -> str | None:
    """``UNIDL_APPEARANCE=light`` for when detection is wrong or absent."""
    value = (os.environ.get("UNIDL_APPEARANCE") or "").strip().lower()
    return value if value in (DARK, LIGHT) else None


def _from_colorfgbg() -> str | None:
    """``COLORFGBG`` is set by rxvt, konsole and a few others.

    Format is ``fg;bg`` or ``fg;something;bg`` in ANSI colour numbers. A high
    background number means a light background.
    """
    raw = (os.environ.get("COLORFGBG") or "").strip()
    if not raw:
        return None
    parts = [part for part in raw.split(";") if part.strip()]
    if not parts:
        return None
    try:
        background = int(parts[-1])
    except ValueError:
        return None
    # 0-6 and 8 are the dark end of the 16-colour palette; 7 and 15 are light
    return LIGHT if background in (7, 15) or background > 8 else DARK


def _from_profile_name() -> str | None:
    """Some terminals name their theme in the environment."""
    for key in ("ITERM_PROFILE", "TERMINAL_THEME", "COLORTERM_THEME"):
        name = (os.environ.get(key) or "").strip().lower()
        if not name:
            continue
        for hint, answer in _NAME_HINTS:
            if hint in name:
                return answer
    return None


def _from_desktop() -> str | None:
    """The operating system's own light/dark setting.

    A last resort, because it describes the desktop rather than the terminal, and
    those disagree often enough to matter.
    """
    if os.name == "nt":  # pragma: no cover - no Windows here to check against
        return _from_windows_registry()
    if _looks_like_macos():
        return _from_macos()
    return _from_freedesktop()


def _looks_like_macos() -> bool:
    import sys

    return sys.platform == "darwin"


def _run(argv: list[str]) -> str | None:
    if not shutil.which(argv[0]):
        return None
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no user input
            argv, capture_output=True, text=True, timeout=2, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


def _from_macos() -> str | None:
    """``AppleInterfaceStyle`` exists only while dark mode is on."""
    output = _run(["defaults", "read", "-g", "AppleInterfaceStyle"])
    if output is None:
        # the key being absent is how macOS says "light", but it is also how it
        # says "defaults is not available", so this is not treated as an answer
        return None
    return DARK if "dark" in output.lower() else LIGHT


def _from_freedesktop() -> str | None:
    output = _run(
        ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"]
    ) or _run(["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"])
    if not output:
        return None
    lowered = output.lower()
    for hint, answer in _NAME_HINTS:
        if hint in lowered:
            return answer
    return None


def _from_windows_registry() -> str | None:  # pragma: no cover - not testable here
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            apps_use_light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except OSError:
        return None
    return LIGHT if apps_use_light else DARK


def describe() -> str:
    """A short account of what was found, for the settings screen."""
    answer = detect()
    if answer is None:
        return "could not tell, so dark is used"
    which = {
        _from_override: "UNIDL_APPEARANCE",
        _from_colorfgbg: "the terminal's COLORFGBG",
        _from_profile_name: "the terminal profile name",
        _from_desktop: "the system setting",
    }
    for source, label in which.items():
        if source() == answer:
            return f"{answer}, from {label}"
    return answer


__all__ = ["DARK", "LIGHT", "describe", "detect"]
