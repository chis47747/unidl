"""Terminal cell widths for Thai combining marks.

Some macOS terminal emulators draw Thai vowels and tone marks in their own
visible cell even though Unicode classifies them as non-spacing marks. Rich
therefore under-counts Thai strings by one cell per mark, leaving unpainted
cells and making Textual rows overlap. Thai base letters remain one cell.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache

_THAI_VISIBLE_MARKS = frozenset(
    [0x0E31, *range(0x0E34, 0x0E3B), *range(0x0E47, 0x0E4F)]
)
_ZERO_WIDTH_CATEGORIES = frozenset({"Mn", "Me", "Cf"})
_WIDE_EAST_ASIAN = frozenset({"W", "F"})
_RICH_INSTALLED = False


def char_width(char: str) -> int:
    if not char:
        return 0
    code = ord(char)
    if code < 32 or 0x7F <= code < 0xA0:
        return 0
    if code in _THAI_VISIBLE_MARKS:
        return 1
    if unicodedata.combining(char) or unicodedata.category(char) in _ZERO_WIDTH_CATEGORIES:
        return 0
    if unicodedata.east_asian_width(char) in _WIDE_EAST_ASIAN:
        return 2
    return 1


def cell_width(text: str) -> int:
    width = 0
    for char in str(text or ""):
        width += 1 if char in "\n\t" else char_width(char)
    return width


def _clusters(text: str) -> list[tuple[str, int]]:
    clusters: list[tuple[str, int]] = []
    for char in str(text or ""):
        size = 1 if char in "\n\t" else char_width(char)
        if clusters and (size == 0 or ord(char) in _THAI_VISIBLE_MARKS):
            previous, previous_size = clusters[-1]
            clusters[-1] = (previous + char, previous_size + size)
        else:
            clusters.append((char, size))
    return clusters


def crop(text: str, width: int) -> str:
    if width <= 0:
        return ""
    used = 0
    parts: list[str] = []
    for cluster, size in _clusters(text):
        if used + size > width:
            break
        parts.append(cluster)
        used += size
    return "".join(parts)


def crop_left(text: str, width: int) -> str:
    if width <= 0:
        return ""
    used = 0
    kept: list[str] = []
    for cluster, size in reversed(_clusters(text)):
        if used + size > width:
            break
        kept.append(cluster)
        used += size
    kept.reverse()
    return "".join(kept)


def fit_middle(text: str, room: int, *, head_share: float = 0.6) -> str:
    text = str(text or "")
    if room <= 0:
        return ""
    if cell_width(text) <= room:
        return text
    if room <= 3:
        return "..."[:room]
    keep = room - 1
    head = max(1, int(keep * head_share))
    tail = keep - head
    return crop(text, keep) + "…" if tail <= 0 else crop(text, head) + "…" + crop_left(text, tail)


def install_rich_cell_widths() -> None:
    global _RICH_INSTALLED
    if _RICH_INSTALLED:
        return
    import rich.cells

    original = rich.cells.get_character_cell_size

    @lru_cache(maxsize=4096)
    def get_character_cell_size(character: str, unicode_version: str = "auto") -> int:
        width = original(character, unicode_version)
        if width == 0 and character and ord(character) in _THAI_VISIBLE_MARKS:
            return 1
        return width

    rich.cells.get_character_cell_size = get_character_cell_size
    cached = getattr(rich.cells, "cached_cell_len", None)
    clear = getattr(cached, "cache_clear", None)
    if callable(clear):
        clear()
    _RICH_INSTALLED = True


__all__ = ["cell_width", "char_width", "crop", "crop_left", "fit_middle", "install_rich_cell_widths"]
