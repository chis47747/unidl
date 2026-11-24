"""Terminal-safe presentation of Hebrew, Arabic and other RTL text.

Service and core values always stay in Unicode logical order.  Terminals draw
cells from left to right and usually do not run the Unicode Bidirectional
Algorithm, so a Hebrew title can otherwise appear character-reversed and mixed
numbers or Latin words can jump to the wrong side.  Arabic additionally needs
presentation-form shaping because terminal cells do not reliably join glyphs.

Only the render copy passes through :func:`visual_text` or :func:`shape_strip`;
filtering, API calls, file names, cache values, search queries and explicit copy
actions always keep the original logical Unicode string.  This boundary also
strips explicit bidi overrides/isolate controls from remote text: they are
unnecessary after visual reordering, can confuse python-bidi, and must never be
able to rearrange the surrounding menu number or markup.
"""

from __future__ import annotations

import os
import unicodedata
from typing import Any

import arabic_reshaper
from bidi import algorithm as _bidi_alg
from rich.markup import escape
from rich.segment import Segment
from rich.style import Style

_RTL_CLASSES = frozenset({"R", "AL", "AN"})
_CONTROL_CHARS = frozenset(
    "\u061c\u200e\u200f"  # Arabic/LTR/RTL marks
    "\u202a\u202b\u202c\u202d\u202e"  # embeddings, overrides, pop
    "\u2066\u2067\u2068\u2069"  # directional isolates and pop
)
_LAM_ALEF_LIGATURES = frozenset("\ufef5\ufef6\ufef7\ufef8\ufef9\ufefa\ufefb\ufefc")
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_ARABIC_RESHAPER = arabic_reshaper.ArabicReshaper(
    configuration={
        # Keep vowel marks (harakat). Moving them one position before visual
        # reversal leaves each mark attached to its base glyph.
        "delete_harakat": False,
        "shift_harakat_position": True,
    }
)
_TEXTUAL_RTL_INSTALLED = False

# ECMA-48 BDSM Explicit Mode (CSI 8 l) + SCP LTR (CSI 1 SP k) + disable
# implicit BiDi autodetection (CSI ? 2501 l). Tells BiDi-capable terminals
# (iTerm2 3.5+, GNOME VTE, Konsole, WezTerm, SwiftTerm, foot, mlterm) to stop
# running row-wide UAX #9 reordering over full-screen TUI cells; cell-only
# terminals (Kitty, Alacritty, Ghostty, Windows Terminal, xterm) ignore these.
EXPLICIT_BIDI_ENTER = "\x1b[8l\x1b[1 k\x1b[?2501l"
EXPLICIT_BIDI_EXIT = "\x1b[?2501h\x1b[0 k\x1b[8h"


def rtl_shaping_disabled() -> bool:
    """Whether ``UNIDL_DISABLE_RTL_SHAPING=1`` opts out for native-BiDi terminals."""
    return os.environ.get("UNIDL_DISABLE_RTL_SHAPING", "").strip().lower() in _TRUTHY


def _is_unshaped_arabic(char: str) -> bool:
    code = ord(char)
    if code == 0x0640:
        # U+0640 (Arabic tatweel/kashida) has no separate presentation form and
        # remains U+0640 after shaping, so it must not trigger re-shaping.
        return False
    return (
        (0x0621 <= code <= 0x064A)
        or (0x066E <= code <= 0x06D5)
        or (0x0750 <= code <= 0x077F)
        or (0x08A0 <= code <= 0x08FF)
    )


def _is_arabic_run_char(char: str) -> bool:
    code = ord(char)
    return (
        _is_unshaped_arabic(char)
        or (0x064B <= code <= 0x065F)
        or code == 0x0640
        or code == 0x0670
    )


def _is_presentation_arabic(char: str) -> bool:
    code = ord(char)
    return (0xFB50 <= code <= 0xFDFF) or (0xFE70 <= code <= 0xFEFF)


def has_unshaped_arabic(value: Any) -> bool:
    """Whether ``value`` contains logical Arabic letters not yet shaped."""
    return any(_is_unshaped_arabic(char) for char in str(value or ""))


def has_rtl(value: Any) -> bool:
    """Whether ``value`` contains a strong RTL or Arabic-number character."""
    return any(unicodedata.bidirectional(char) in _RTL_CLASSES for char in str(value or ""))


def _reshape_styled_chars(
    chars: list[tuple[str, Style | None]],
) -> list[tuple[str, Style | None]]:
    """Reshape unshaped Arabic runs while preserving per-character Rich styles."""
    out: list[tuple[str, Style | None]] = []
    total = len(chars)
    index = 0
    while index < total:
        char, style = chars[index]
        if not _is_arabic_run_char(char):
            out.append((char, style))
            index += 1
            continue
        end = index + 1
        while end < total and _is_arabic_run_char(chars[end][0]):
            end += 1
        word = "".join(ch for ch, _ in chars[index:end])
        reshaped = _ARABIC_RESHAPER.reshape(word)
        word_styles = [st for _, st in chars[index:end]]
        if len(reshaped) == len(word):
            out.extend((rc, word_styles[offset]) for offset, rc in enumerate(reshaped))
        else:
            src_idx = 0
            limit = len(word_styles) - 1
            for rc in reshaped:
                out.append((rc, word_styles[min(src_idx, limit)]))
                src_idx += 2 if rc in _LAM_ALEF_LIGATURES else 1
        index = end
    return out


def _apply_bidi_to_styled(
    shaped: list[tuple[str, Style | None]],
    *,
    base_dir: str | None = None,
) -> list[tuple[str, Style | None]]:
    """Reorder a shaped single-line styled character sequence into visual order."""
    if not shaped:
        return []
    text = "".join(ch for ch, _ in shaped)
    storage = _bidi_alg.get_empty_storage()
    if base_dir in _bidi_alg.PARAGRAPH_LEVELS:
        base_level = _bidi_alg.PARAGRAPH_LEVELS[base_dir]
    else:
        base_level = _bidi_alg.get_base_level(text, False)
    storage["base_level"] = base_level
    storage["base_dir"] = ("L", "R")[base_level]

    _bidi_alg.get_embedding_levels(text, storage, False, False)
    chars_info = storage["chars"]
    for idx, entry in enumerate(chars_info):
        entry["style"] = shaped[idx][1]

    # Keep technical LTR tokens atomic even in RTL paragraphs:
    # - control chords (^b, ^s, ^f, ^p, ...)
    # - slash commands and POSIX paths (/auto, /Users/..., /var/...)
    # - dotfiles / extensions (.wvd, .prd, .mkv, .json)
    # - home-relative paths (~/...)
    # - CLI flags (--help, -v)
    # - arrow pairs (↑↓)
    total_chars = len(chars_info)
    for idx in range(total_chars):
        ch = chars_info[idx]["ch"]
        prev_ch = chars_info[idx - 1]["ch"] if idx > 0 else ""
        prev_type = chars_info[idx - 1]["type"] if idx > 0 else ""
        nxt_ch = chars_info[idx + 1]["ch"] if idx + 1 < total_chars else ""
        make_ltr = False
        if ch == "^" and nxt_ch.isascii() and nxt_ch.isalpha():
            make_ltr = True
        elif ch == "/" and nxt_ch.isascii() and (nxt_ch.isalnum() or nxt_ch in "._~"):
            make_ltr = True
        elif ch == "~" and nxt_ch == "/":
            make_ltr = True
        elif (
            ch == "."
            and nxt_ch.isascii()
            and nxt_ch.isalnum()
            and prev_type not in _RTL_CLASSES
        ):
            make_ltr = True
        elif (
            ch == "-"
            and (idx == 0 or prev_ch.isspace() or prev_ch == "-")
            and nxt_ch.isascii()
            and (nxt_ch.isalnum() or nxt_ch == "-")
        ):
            make_ltr = True
        elif ch == "↑" and nxt_ch == "↓":
            chars_info[idx]["type"] = "L"
            chars_info[idx]["orig"] = "L"
            chars_info[idx + 1]["type"] = "L"
            chars_info[idx + 1]["orig"] = "L"
            continue
        if make_ltr:
            chars_info[idx]["type"] = "L"
            chars_info[idx]["orig"] = "L"

    _bidi_alg.explicit_embed_and_overrides(storage, False)
    _bidi_alg.resolve_weak_types(storage, False)

    # Lightweight UAX #9 rule N0 for balanced ASCII () and [] wrapping pure LTR
    # runs (such as "(12.3 MB/s)" or "[x]" / "[✓]") so closing brackets are not
    # asymmetrically mirrored at an RTL line boundary.
    stack: list[tuple[int, str]] = []
    pairs: list[tuple[int, int]] = []
    close_for = {"(": ")", "[": "]"}
    for idx, entry in enumerate(chars_info):
        ch = entry["ch"]
        if ch in close_for:
            stack.append((idx, close_for[ch]))
        elif stack and ch == stack[-1][1]:
            open_idx, _ = stack.pop()
            pairs.append((open_idx, idx))
    for open_idx, close_idx in pairs:
        inner_types = {chars_info[pos]["type"] for pos in range(open_idx + 1, close_idx)}
        has_ltr = bool(inner_types & {"L", "EN"})
        has_rtl_inner = bool(inner_types & _RTL_CLASSES)
        if has_ltr and not has_rtl_inner:
            chars_info[open_idx]["type"] = "L"
            chars_info[close_idx]["type"] = "L"

    _bidi_alg.resolve_neutral_types(storage, False)
    _bidi_alg.resolve_implicit_levels(storage, False)
    _bidi_alg.reorder_resolved_levels(storage, False)
    _bidi_alg.apply_mirroring(storage, False)
    return [(entry["ch"], entry["style"]) for entry in storage["chars"]]


def _safe_line(line: str, *, base_dir: str | None = None) -> str:
    clean = "".join(
        char
        for char in unicodedata.normalize("NFC", line)
        if char not in _CONTROL_CHARS
    )
    if not has_rtl(clean) or rtl_shaping_disabled():
        return clean
    try:
        shaped = _reshape_styled_chars([(ch, None) for ch in clean])
        ordered = _apply_bidi_to_styled(shaped, base_dir=base_dir)
        return "".join(ch for ch, _ in ordered)
    except (AssertionError, TypeError, ValueError):
        # Remote titles are allowed to be odd, but never to take down the UI.
        return clean


def visual_text(value: Any, *, base_dir: str | None = None) -> str:
    """Return a terminal visual-order copy while preserving line boundaries."""
    text = str(value or "")
    if text.isascii():
        return text
    has_controls = any(char in _CONTROL_CHARS for char in text)
    if not has_rtl(text) and not has_controls:
        return unicodedata.normalize("NFC", text)
    return "\n".join(_safe_line(line, base_dir=base_dir) for line in text.split("\n"))


def escape_markup(value: Any) -> str:
    """Escape Rich/Textual markup brackets without pre-reversing RTL text.

    Use this for multi-line or wrapped UI strings (setting values, help prose,
    setup notes) so Textual wraps lines in logical reading order first and
    :func:`shape_strip` shapes each visual line afterward.
    """
    escaped = escape(str(value or ""))
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


def visual_markup(value: Any, *, base_dir: str | None = None) -> str:
    """Visual-order text escaped for a Textual/Rich markup-bearing widget."""
    return escape_markup(visual_text(value, base_dir=base_dir))


def _coalesce_segments(chars: list[tuple[str, Style | None]]) -> list[Segment]:
    out: list[Segment] = []
    for ch, style in chars:
        if out and out[-1].style == style:
            out[-1] = Segment(out[-1].text + ch, style)
        else:
            out.append(Segment(ch, style))
    return out


def shape_strip(
    strip: Any,
    *,
    rtl_base: bool = False,
    logical: bool = False,
    align_right: bool = False,
) -> Any:
    """Shape and visually reorder unshaped Arabic inside a rendered Textual Strip.

    Leaves non-Arabic strips and already-visualized Hebrew/Presentation-Form
    strips completely untouched (unless ``logical=True`` explicitly marks the
    strip as un-reordered logical text), preserves outer alignment padding and
    exact strip cell length (even when ``لا`` ligatures shrink the cell count by
    1), and prevents double-reversal when a logical Arabic UI label shares a
    line with an already-visualized Arabic title.
    """
    if rtl_shaping_disabled():
        return strip
    segments = getattr(strip, "_segments", None)
    if not segments:
        return strip
    if logical:
        needs_shape = any(
            _is_unshaped_arabic(ch) or (0x0590 <= ord(ch) <= 0x05FF)
            for seg in segments
            if not seg.control
            for ch in seg.text
        )
    else:
        needs_shape = any(
            _is_unshaped_arabic(ch)
            for seg in segments
            if not seg.control
            for ch in seg.text
        )
    if not needs_shape:
        return strip

    original_width = strip.cell_length
    raw: list[tuple[str, Style | None]] = []
    for seg in segments:
        if seg.control:
            continue
        text = unicodedata.normalize("NFC", seg.text)
        clean = [ch for ch in text if ch not in _CONTROL_CHARS]
        if any(_is_presentation_arabic(ch) for ch in clean) and not any(
            _is_unshaped_arabic(ch) for ch in clean
        ):
            # Already passed through visual_text() -> temporarily revert visual
            # order so the full-line BiDi pass leaves it right-side-up.
            try:
                clean = list(_bidi_alg.get_display("".join(clean), base_dir="R"))
            except (AssertionError, TypeError, ValueError):
                pass
        for ch in clean:
            raw.append((ch, seg.style))

    if not raw:
        return strip

    # Preserve outer alignment spaces added by text-align: right / center / left
    # so BiDi reordering never moves right-alignment left-padding to the right.
    lead_end = 0
    while lead_end < len(raw) and raw[lead_end][0] == " ":
        lead_end += 1
    trail_start = len(raw)
    while trail_start > lead_end and raw[trail_start - 1][0] == " ":
        trail_start -= 1

    leading = raw[:lead_end]
    core = raw[lead_end:trail_start]
    trailing = raw[trail_start:]
    if not core:
        return strip

    try:
        shaped_core = _reshape_styled_chars(core)
        ordered_core = _apply_bidi_to_styled(
            shaped_core,
            base_dir="R" if rtl_base else None,
        )
    except (AssertionError, TypeError, ValueError):
        return strip

    from textual.strip import Strip

    if align_right and trailing:
        combined = [*trailing, *leading, *ordered_core]
        leading = [*trailing, *leading]
        trailing = []
    else:
        combined = [*leading, *ordered_core, *trailing]
    result = Strip(_coalesce_segments(combined))
    delta = original_width - result.cell_length
    if delta > 0:
        # Ligatures (e.g. لا -> ﻻ) can shorten the visual line by 1 cell per
        # ligature; return the freed cell(s) to the alignment padding side so
        # right-aligned rows stay flush and compositor strip widths stay exact.
        if (leading and not trailing) or (rtl_base and not trailing):
            pad_style = leading[0][1] if leading else (ordered_core[0][1] if ordered_core else None)
            combined = [(" ", pad_style)] * delta + combined
        elif leading and trailing:
            left_add = delta // 2
            right_add = delta - left_add
            combined = (
                [(" ", leading[0][1])] * left_add
                + combined
                + [(" ", trailing[-1][1])] * right_add
            )
        else:
            pad_style = trailing[-1][1] if trailing else (ordered_core[-1][1] if ordered_core else None)
            combined = combined + [(" ", pad_style)] * delta
        result = Strip(_coalesce_segments(combined))
    return result


def install_textual_rtl_shaping() -> None:
    """Hook Textual's ``Visual.to_strips`` once so all Arabic UI renders shaped."""
    global _TEXTUAL_RTL_INSTALLED
    if _TEXTUAL_RTL_INSTALLED:
        return
    from textual.visual import Visual
    from textual.widgets import Input as TextualInput
    from textual.widgets import TextArea as TextualTextArea

    from ..core.i18n import is_rtl_locale

    original_to_strips = Visual.to_strips.__func__  # type: ignore[attr-defined]

    @classmethod  # type: ignore[misc]
    def _rtl_to_strips(
        cls,
        widget,
        visual,
        width: int,
        height: int | None,
        style,
        *,
        apply_selection: bool = True,
        pad: bool = False,
        post_style=None,
    ):
        strips = original_to_strips(
            cls,
            widget,
            visual,
            width,
            height,
            style,
            apply_selection=apply_selection,
            pad=pad,
            post_style=post_style,
        )
        if rtl_shaping_disabled() or isinstance(widget, (TextualInput, TextualTextArea)):
            return strips
        rtl_base = is_rtl_locale()
        return [shape_strip(strip, rtl_base=rtl_base) for strip in strips]

    Visual.to_strips = _rtl_to_strips  # type: ignore[assignment]
    _TEXTUAL_RTL_INSTALLED = True


__all__ = [
    "EXPLICIT_BIDI_ENTER",
    "EXPLICIT_BIDI_EXIT",
    "escape_markup",
    "has_rtl",
    "has_unshaped_arabic",
    "install_textual_rtl_shaping",
    "rtl_shaping_disabled",
    "shape_strip",
    "visual_markup",
    "visual_text",
]
