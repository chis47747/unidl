"""Terminal cell widths and grapheme-safe cropping.

Standard Unicode and modern terminal emulators across macOS, Windows and Linux
render combining marks (Thai vowels and tone marks, Lao, Khmer, Myanmar,
Devanagari, Arabic harakat, Hebrew niqqud, NFD diacritics) as zero-width marks
attached to their base cell.  Counting them as 1 cell makes Rich/Textual
over-measure every row containing those scripts, pulling modal borders, close
buttons and right-aligned chips leftward.

For unusual legacy terminal/font setups that truly paint Thai combining marks
in their own cell, users can explicitly opt in with ``UNIDL_THAI_MARK_WIDTH=1``.

This module also upgrades Rich's ``Segment._split_cells`` to split on grapheme
cluster boundaries via ``rich.cells.split_graphemes`` so modal overlays and
widget crops never sever combining marks or Emoji VS16/ZWJ sequences from their
base character.
"""

from __future__ import annotations

import os
import unicodedata
from functools import lru_cache

_THAI_VISIBLE_MARKS = frozenset(
    [0x0E31, *range(0x0E34, 0x0E3B), *range(0x0E47, 0x0E4F)]
)

_ZERO_WIDTH_CATEGORIES = frozenset({"Mn", "Me", "Mc", "Cf"})
_WIDE_EAST_ASIAN = frozenset({"W", "F"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_RICH_INSTALLED = False


def thai_marks_wide() -> bool:
    """Whether ``UNIDL_THAI_MARK_WIDTH=1`` explicitly requests 1-cell Thai marks."""
    return os.environ.get("UNIDL_THAI_MARK_WIDTH", "").strip().lower() in _TRUTHY


def _fallback_char_width(char: str) -> int:
    if not char:
        return 0
    code = ord(char)
    if code < 32 or 0x7F <= code < 0xA0:
        return 0
    if thai_marks_wide() and code in _THAI_VISIBLE_MARKS:
        return 1
    if unicodedata.combining(char) or unicodedata.category(char) in _ZERO_WIDTH_CATEGORIES:
        return 0
    if unicodedata.east_asian_width(char) in _WIDE_EAST_ASIAN:
        return 2
    return 1


def char_width(char: str) -> int:
    """Terminal cells occupied by one character."""
    if not char:
        return 0
    code = ord(char)
    if code < 32 or 0x7F <= code < 0xA0:
        return 0
    if thai_marks_wide() and code in _THAI_VISIBLE_MARKS:
        return 1
    try:
        import rich.cells

        return rich.cells.get_character_cell_size(char)
    except Exception:
        return _fallback_char_width(char)


def cell_width(text: str) -> int:
    """Terminal cells occupied by ``text``.

    Newlines and tabs count as one column so callers that fold labels stay in
    line with :func:`unidl.core.i18n.cell_width`.
    """
    raw = str(text or "")
    if not raw:
        return 0
    try:
        import rich.cells

        if "\n" not in raw and "\t" not in raw:
            if not thai_marks_wide():
                return rich.cells.cell_len(raw)
        width = 0
        start = 0
        for index, char in enumerate(raw):
            if char in "\n\t":
                if index > start:
                    width += _piece_width(raw[start:index], rich.cells)
                width += 1
                start = index + 1
        if start < len(raw):
            width += _piece_width(raw[start:], rich.cells)
        return width
    except Exception:
        width = 0
        for char in raw:
            width += 1 if char in "\n\t" else _fallback_char_width(char)
        return width


def _piece_width(piece: str, rich_cells) -> int:
    if not thai_marks_wide():
        return rich_cells.cell_len(piece)
    width = 0
    for char in piece:
        code = ord(char)
        if code in _THAI_VISIBLE_MARKS:
            width += 1
        else:
            width += rich_cells.get_character_cell_size(char)
    return width


def _clusters(text: str) -> list[tuple[str, int]]:
    """Grapheme-like clusters: combining marks and emoji modifiers stay attached."""
    raw = str(text or "")
    if not raw:
        return []
    wide_thai = thai_marks_wide()
    if not wide_thai:
        try:
            import rich.cells

            clusters: list[tuple[str, int]] = []
            start = 0
            for index, char in enumerate(raw):
                if char in "\n\t":
                    if index > start:
                        piece = raw[start:index]
                        spans, _ = rich.cells.split_graphemes(piece)
                        clusters.extend((piece[s:e], w) for s, e, w in spans)
                    clusters.append((char, 1))
                    start = index + 1
            if start < len(raw):
                piece = raw[start:]
                spans, _ = rich.cells.split_graphemes(piece)
                clusters.extend((piece[s:e], w) for s, e, w in spans)
            return clusters
        except Exception:
            pass
    clusters = []
    for char in raw:
        size = 1 if char in "\n\t" else char_width(char)
        if clusters and (size == 0 or (wide_thai and ord(char) in _THAI_VISIBLE_MARKS)):
            previous, previous_size = clusters[-1]
            clusters[-1] = (previous + char, previous_size + size)
        else:
            clusters.append((char, size))
    return clusters


def crop(text: str, width: int) -> str:
    """Prefix of ``text`` occupying at most ``width`` cells."""
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
    """Suffix of ``text`` occupying at most ``width`` cells."""
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
    """Shorten ``text`` from the middle to at most ``room`` cells."""
    text = str(text or "")
    if room <= 0:
        return ""
    if cell_width(text) <= room:
        return text
    if room <= 3:
        return "..."[:room] if room < 3 else "..."
    keep = room - 1
    head = max(1, int(keep * head_share))
    tail = keep - head
    if tail <= 0:
        return crop(text, keep) + "…"
    return crop(text, head) + "…" + crop_left(text, tail)


def install_rich_cell_widths() -> None:
    """Install grapheme-safe Rich segment splitting and optional Thai override.

    Rich's ``Segment._split_cells`` stops as soon as ``cell_len(text[:pos])``
    matches ``cut``, which can sever trailing combining marks (Thai, Lao,
    Khmer, Myanmar, Vietnamese NFD, Hebrew, Arabic) or Emoji VS16/ZWJ modifiers
    from their base character whenever Textual crops a strip behind a modal.
    Delegating to ``rich.cells.split_graphemes`` keeps every cluster intact.

    Thai combining marks keep their standard Unicode width of ``0`` unless the
    user explicitly sets ``UNIDL_THAI_MARK_WIDTH=1``.
    """
    global _RICH_INSTALLED
    if _RICH_INSTALLED:
        return
    import rich.cells
    from rich.segment import Segment

    if thai_marks_wide():
        original_char_size = rich.cells.get_character_cell_size

        @lru_cache(maxsize=4096)
        def get_character_cell_size(character: str, unicode_version: str = "auto") -> int:
            width = original_char_size(character, unicode_version)
            if width == 0 and character and ord(character) in _THAI_VISIBLE_MARKS:
                return 1
            return width

        rich.cells.get_character_cell_size = get_character_cell_size

    split_graphemes = getattr(rich.cells, "split_graphemes", None)
    if callable(split_graphemes):
        original_split_cells = Segment._split_cells

        @classmethod  # type: ignore[misc]
        @lru_cache(1024 * 16)
        def _grapheme_split_cells(
            cls: type[Segment], segment: Segment, cut: int
        ) -> tuple[Segment, Segment]:
            text, style, control = segment
            if cut <= 0:
                return cls("", style, control), segment
            cell_length = segment.cell_length
            if cut >= cell_length:
                return segment, cls("", style, control)
            spans, total = split_graphemes(text)
            if total != cell_length:
                return original_split_cells(segment, cut)
            used = 0
            span_count = len(spans)
            for idx, (start, end, span_width) in enumerate(spans):
                next_used = used + span_width
                if next_used == cut:
                    split_at = end
                    next_idx = idx + 1
                    while next_idx < span_count and spans[next_idx][2] == 0:
                        split_at = spans[next_idx][1]
                        next_idx += 1
                    return (
                        cls(text[:split_at], style, control),
                        cls(text[split_at:], style, control),
                    )
                if next_used > cut:
                    return (
                        cls(text[:start] + " ", style, control),
                        cls(" " + text[end:], style, control),
                    )
                used = next_used
            return segment, cls("", style, control)

        Segment._split_cells = _grapheme_split_cells  # type: ignore[method-assign]

    cached = getattr(rich.cells, "cached_cell_len", None)
    cache_clear = getattr(cached, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()
    _RICH_INSTALLED = True


__all__ = [
    "cell_width",
    "char_width",
    "crop",
    "crop_left",
    "fit_middle",
    "install_rich_cell_widths",
    "thai_marks_wide",
]
