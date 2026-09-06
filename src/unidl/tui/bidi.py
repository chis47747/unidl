"""Terminal-safe presentation of Hebrew, Arabic and other RTL text.

Service and core values always stay in Unicode logical order.  Terminals draw
cells from left to right and usually do not run the Unicode Bidirectional
Algorithm, so a Hebrew title can otherwise appear character-reversed and mixed
numbers or Latin words can jump to the wrong side.  Arabic additionally needs
presentation-form shaping because terminal cells do not reliably join glyphs.

Only the render copy passes through :func:`visual_text`; filtering, API calls,
file names, cache values and explicit copy actions keep the original string.
This boundary also strips explicit bidi overrides/isolate controls from remote text:
they are unnecessary after visual reordering, can confuse python-bidi, and must
never be able to rearrange the surrounding menu number or markup.
"""

from __future__ import annotations

import unicodedata
from typing import Any

import arabic_reshaper
from bidi.algorithm import get_display
from rich.markup import escape

_RTL_CLASSES = frozenset({"R", "AL", "AN"})
_CONTROL_CHARS = frozenset(
    "\u061c\u200e\u200f"  # Arabic/LTR/RTL marks
    "\u202a\u202b\u202c\u202d\u202e"  # embeddings, overrides, pop
    "\u2066\u2067\u2068\u2069"  # directional isolates and pop
)
_ARABIC_RESHAPER = arabic_reshaper.ArabicReshaper(
    configuration={
        # Titles are data, not decoration: keep vowel marks. Moving them one
        # position before visual reversal leaves each mark on its base glyph.
        "delete_harakat": False,
        "shift_harakat_position": True,
    }
)


def has_rtl(value: Any) -> bool:
    """Whether ``value`` contains a strong RTL or Arabic-number character."""
    return any(unicodedata.bidirectional(char) in _RTL_CLASSES for char in str(value or ""))


def _safe_line(line: str) -> str:
    clean = "".join(
        char
        for char in unicodedata.normalize("NFC", line)
        if char not in _CONTROL_CHARS
    )
    if not has_rtl(clean):
        return clean
    try:
        # Reshaping is a no-op for Hebrew and non-Arabic scripts.  get_display
        # chooses the paragraph direction from the first strong character and
        # preserves LTR number/Latin runs within the visual RTL result.
        return get_display(_ARABIC_RESHAPER.reshape(clean))
    except (AssertionError, TypeError, ValueError):
        # Remote titles are allowed to be odd, but never to take down the UI.
        return clean


def visual_text(value: Any) -> str:
    """Return a terminal visual-order copy while preserving line boundaries."""
    text = str(value or "")
    has_controls = any(char in _CONTROL_CHARS for char in text)
    if not has_rtl(text) and not has_controls:
        return text
    return "\n".join(_safe_line(line) for line in text.split("\n"))


def visual_markup(value: Any) -> str:
    """Visual-order text escaped for a Textual/Rich markup-bearing widget."""
    escaped = escape(visual_text(value))
    # Rich's escape helper intentionally recognises only tag-looking brackets.
    # A Unicode word in brackets is left untouched, but when followed by our
    # own ``[/]`` style closer Rich may then treat it as an opening style and
    # fail the whole option. Escape every still-unescaped opening bracket.
    out: list[str] = []
    for char in escaped:
        if char == "[":
            slashes = 0
            for previous in reversed(out):
                if previous != "\\":
                    break
                slashes += 1
            if slashes % 2 == 0:
                out.append("\\")
        out.append(char)
    return "".join(out)


__all__ = ["has_rtl", "visual_markup", "visual_text"]
