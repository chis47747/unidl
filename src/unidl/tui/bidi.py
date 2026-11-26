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
from typing import Any, Literal

import arabic_reshaper
from bidi import algorithm as _bidi_alg
from rich.markup import escape
from rich.segment import Segment
from rich.style import Style

RtlTerminalMode = Literal["explicit", "apple_terminal", "off"]

_RTL_CLASSES = frozenset({"R", "AL", "AN"})
_CONTROL_CHARS = frozenset(
    "\u061c\u200e\u200f"  # Arabic/LTR/RTL marks
    "\u202a\u202b\u202c\u202d\u202e"  # embeddings, overrides, pop
    "\u2066\u2067\u2068\u2069"  # directional isolates and pop
)
# U+1160 (HANGUL JUNGSEONG FILLER): non-Cf (category Lo), strong LTR
# (Bidi_Class=L), wcwidth=0, rich cell width=0, zero-width empty glyph in
# macOS SF Mono & Menlo. Unlike U+200E/U+202D/U+2066 (which Terminal.app
# strips from its screen buffer), Terminal.app preserves U+1160 attached to
# its preceding cell, isolating each pre-shaped RTL glyph into a length-1 run
# so Apple CoreText cannot reverse text or drag glyphs out of button/border
# cells.
_RTL_FREEZE_MARK = "\u1160"
_LAM_ALEF_LIGATURES = frozenset("\ufef5\ufef6\ufef7\ufef8\ufef9\ufefa\ufefb\ufefc")
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_MULTIPLEXER_TERM_PROGRAMS = frozenset({"tmux", "screen"})
_NON_APPLE_TERMINAL_ENV = (
    "KITTY_WINDOW_ID",
    "WT_SESSION",
    "WEZTERM_EXECUTABLE",
    "WEZTERM_PANE",
    "ALACRITTY_SOCKET",
    "ALACRITTY_WINDOW_ID",
    "GHOSTTY_RESOURCES_DIR",
    "ITERM_SESSION_ID",
    "KONSOLE_VERSION",
    "VTE_VERSION",
    "VSCODE_PID",
)
_ARABIC_RESHAPER = arabic_reshaper.ArabicReshaper(
    configuration={
        # Keep vowel marks (harakat). Moving them one position before visual
        # reversal leaves each mark attached to its base glyph.
        "delete_harakat": False,
        "shift_harakat_position": True,
    }
)
# Only characters that arabic_reshaper actually transforms into distinct
# presentation forms count as "unshaped". Treating unmapped extended Arabic
# codepoints (e.g. Kurdish/Pashto letters outside the reshaper table) as
# unshaped would trigger a second BiDi pass on already-visualized text.
_UNSHAPED_ARABIC_CHARS = frozenset(
    ch
    for ch, forms in _ARABIC_RESHAPER.letters.items()
    if ch != "\u0640" and any(form and form != ch for form in forms)
)
_TEXTUAL_RTL_INSTALLED = False

# ECMA-48 BDSM Explicit Mode (CSI 8 l) + SCP LTR (CSI 1 SP k) + disable
# implicit BiDi autodetection (CSI ? 2501 l). Tells BiDi-capable terminals
# (iTerm2 3.5+, GNOME VTE, Konsole, WezTerm, SwiftTerm, foot, mlterm) to stop
# running row-wide UAX #9 reordering over full-screen TUI cells; cell-only
# terminals (Kitty, Alacritty, Ghostty, Windows Terminal, xterm) ignore these.
EXPLICIT_BIDI_ENTER = "\x1b[8l\x1b[1 k\x1b[?2501l"
EXPLICIT_BIDI_EXIT = "\x1b[?2501h\x1b[0 k\x1b[8h"


def is_apple_terminal() -> bool:
    """Detect macOS built-in Terminal.app (including inside tmux/screen)."""
    term_program = os.environ.get("TERM_PROGRAM", "").strip()
    if term_program == "Apple_Terminal":
        return True
    if term_program and term_program.lower() not in _MULTIPLEXER_TERM_PROGRAMS:
        return False
    if any(os.environ.get(var) for var in _NON_APPLE_TERMINAL_ENV):
        return False
    return os.environ.get("__CFBundleIdentifier", "").strip() == "com.apple.Terminal"


def rtl_terminal_mode() -> RtlTerminalMode:
    """Resolve the active RTL terminal rendering strategy.

    - ``"off"``: ``UNIDL_DISABLE_RTL_SHAPING=1`` or ``UNIDL_RTL_MODE=off``
      (leave logical Unicode untouched for terminals where the user wants raw
      implicit BiDi).
    - ``"apple_terminal"``: macOS ``Terminal.app`` (auto-detected or via
      ``UNIDL_RTL_MODE=apple``), which ignores ``CSI 8 l`` and runs CoreText
      row-wide BiDi; pre-shaped visual strips are frozen at compositor output
      time using zero-width ``U+1160`` LTR anchors.
    - ``"explicit"``: default for all other terminals (iTerm2 3.5+, WezTerm,
      Kitty, Alacritty, Ghostty, Windows Terminal, VS Code, GNOME VTE,
      Konsole, foot, xterm, etc.).
    """
    if os.environ.get("UNIDL_DISABLE_RTL_SHAPING", "").strip().lower() in _TRUTHY:
        return "off"
    override = os.environ.get("UNIDL_RTL_MODE", "").strip().lower()
    if override in {"off", "disabled", "none", "native"}:
        return "off"
    if override in {"explicit", "standard", "cell"}:
        return "explicit"
    if override in {"apple", "apple_terminal", "freeze"}:
        return "apple_terminal"
    if is_apple_terminal():
        return "apple_terminal"
    return "explicit"


def rtl_shaping_disabled() -> bool:
    """Whether RTL pre-shaping is explicitly opted out via environment."""
    return rtl_terminal_mode() == "off"


def _is_unshaped_arabic(char: str) -> bool:
    return char in _UNSHAPED_ARABIC_CHARS


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
        span = chars[index:end]
        if not any(_is_unshaped_arabic(ch) for ch, _ in span):
            out.extend(span)
            index = end
            continue
        word = "".join(ch for ch, _ in span)
        reshaped = _ARABIC_RESHAPER.reshape(word)
        word_styles = [st for _, st in span]
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
        has_rtl_inner = bool(inner_types & {"R", "AL"})
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
            if not seg.control and seg.text and not seg.text.isascii()
            for ch in seg.text
        )
    else:
        needs_shape = any(
            _is_unshaped_arabic(ch)
            for seg in segments
            if not seg.control and seg.text and not seg.text.isascii()
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


def _strip_has_rtl(strip: Any) -> bool:
    """Whether ``strip`` contains any strong RTL or Arabic-number character."""
    segments = getattr(strip, "_segments", None)
    if not segments:
        return False
    for seg in segments:
        if seg.control or not seg.text or seg.text.isascii():
            continue
        if any(unicodedata.bidirectional(ch) in _RTL_CLASSES for ch in seg.text):
            return True
    return False


def freeze_strip_for_apple_terminal(
    strip: Any,
    *,
    anchor_col0: bool = False,
) -> Any:
    """Freeze pre-shaped RTL visual order for macOS Terminal.app CoreText.

    macOS ``Terminal.app`` ignores ECMA-48 ``CSI 8 l`` / ``CSI ? 2501 l``,
    strips zero-width ``Cf`` BiDi controls (``U+200E``, ``U+202D``, ``U+2066``),
    paints cell backgrounds at fixed LTR columns, and runs CoreText row-wide
    UAX #9 BiDi over the row text.  Appending ``U+1160`` (non-``Cf``,
    ``Bidi_Class=L``, ``wcwidth=0``, zero-width invisible glyph in SF Mono &
    Menlo) after each RTL base+combining cluster turns every RTL glyph into a
    length-1 run that CoreText cannot reverse or drag across borders/buttons,
    while anchoring column 0 (and the cell before the first RTL char) keeps
    CoreText's paragraph base direction strictly LTR.

    Always returns a fresh ``Strip`` when modified so cached widget/compositor
    strips, text selection, and clipboard copy remain pure Unicode.
    """
    segments = getattr(strip, "_segments", None)
    if not segments:
        return strip
    has_any_rtl = _strip_has_rtl(strip)
    if not has_any_rtl and not anchor_col0:
        return strip
    if not has_any_rtl and anchor_col0:
        first_seg_idx = next(
            (i for i, seg in enumerate(segments) if not seg.control and seg.text),
            -1,
        )
        if first_seg_idx == -1:
            return strip
        first_seg = segments[first_seg_idx]
        first_ch = first_seg.text[0]
        if unicodedata.bidirectional(first_ch) == "L":
            return strip
        if len(first_seg.text) >= 2 and first_seg.text[1] == _RTL_FREEZE_MARK:
            return strip
        patched = list(segments)
        patched[first_seg_idx] = Segment(
            first_ch + _RTL_FREEZE_MARK + first_seg.text[1:],
            first_seg.style,
        )
        from textual.strip import Strip

        return Strip(patched, cell_length=strip.cell_length)

    chars: list[tuple[str, Style | None]] = []
    controls: dict[int, list[Segment]] = {}
    for seg in segments:
        if seg.control:
            controls.setdefault(len(chars), []).append(seg)
        elif seg.text:
            style = seg.style
            for ch in seg.text:
                chars.append((ch, style))

    if not chars:
        return strip

    first_rtl_idx = (
        next(
            (i for i, (ch, _) in enumerate(chars) if unicodedata.bidirectional(ch) in _RTL_CLASSES),
            -1,
        )
        if has_any_rtl
        else -1
    )

    if first_rtl_idx == -1 and anchor_col0:
        if unicodedata.bidirectional(chars[0][0]) == "L":
            return strip

    out_segments: list[Segment] = []
    pending_chars: list[tuple[str, Style | None]] = []

    def flush_pending() -> None:
        if pending_chars:
            out_segments.extend(_coalesce_segments(pending_chars))
            pending_chars.clear()

    total = len(chars)
    index = 0
    while index < total:
        if index in controls:
            flush_pending()
            out_segments.extend(controls[index])

        ch, style = chars[index]
        bc = unicodedata.bidirectional(ch)
        is_rtl = bc in _RTL_CLASSES

        if index == 0 and anchor_col0 and is_rtl:
            pending_chars.append((_RTL_FREEZE_MARK, style))

        pending_chars.append((ch, style))
        last_style = style

        # Keep trailing combining marks (harakat, etc.) attached to their base.
        end = index + 1
        while end < total and unicodedata.category(chars[end][0]) in ("Mn", "Me"):
            if end in controls:
                flush_pending()
                out_segments.extend(controls[end])
            mark_ch, mark_style = chars[end]
            pending_chars.append((mark_ch, mark_style))
            last_style = mark_style
            end += 1

        need_mark = False
        if is_rtl:
            need_mark = True
        elif index == 0 and anchor_col0 and bc != "L":
            need_mark = True
        elif first_rtl_idx > 0 and end == first_rtl_idx and bc != "L":
            need_mark = True

        if need_mark and (end >= total or chars[end][0] != _RTL_FREEZE_MARK):
            pending_chars.append((_RTL_FREEZE_MARK, last_style))

        index = end

    if total in controls:
        flush_pending()
        out_segments.extend(controls[total])
    else:
        flush_pending()

    from textual.strip import Strip

    return Strip(out_segments, cell_length=strip.cell_length)


def install_textual_rtl_shaping() -> None:
    """Hook Textual's strip and compositor emission once for cross-terminal RTL."""
    global _TEXTUAL_RTL_INSTALLED
    if _TEXTUAL_RTL_INSTALLED:
        return
    from rich._loop import loop_last
    from rich.control import Control
    from textual._compositor import ChopsUpdate, LayoutUpdate
    from textual.visual import Visual
    from textual.widgets import Input as TextualInput
    from textual.widgets import TextArea as TextualTextArea

    from ..core.i18n import is_rtl_locale

    original_to_strips = Visual.to_strips.__func__  # type: ignore[attr-defined]
    original_layout_render_segments = LayoutUpdate.render_segments
    original_chops_render_segments = ChopsUpdate.render_segments

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

    def _rtl_layout_render_segments(self, console) -> str:
        if rtl_terminal_mode() != "apple_terminal":
            return original_layout_render_segments(self, console)
        sequences: list[str] = []
        append = sequences.append
        x = self.region.x
        move_to = Control.move_to
        rtl_locale = is_rtl_locale()
        for last, (y, line) in loop_last(enumerate(self.strips, self.region.y)):
            append(move_to(x, y).segment.text)
            row_strips = list(line)
            need_anchor = (x == 0) and (
                rtl_locale or any(_strip_has_rtl(strip) for strip in row_strips)
            )
            anchored = False
            for strip in row_strips:
                anchor_this = need_anchor and not anchored and strip.cell_length > 0
                if anchor_this:
                    anchored = True
                append(
                    freeze_strip_for_apple_terminal(
                        strip,
                        anchor_col0=anchor_this,
                    ).render(console)
                )
            if not last:
                append("\n")
        return "".join(sequences)

    def _rtl_chops_render_segments(self, console) -> str:
        if rtl_terminal_mode() != "apple_terminal":
            return original_chops_render_segments(self, console)
        if not self.spans:
            return ""
        sequences: list[str] = []
        append = sequences.append
        move_to = Control.move_to
        chops = self.chops
        chop_ends = self.chop_ends
        last_y = self.spans[-1][0]
        rtl_locale = is_rtl_locale()

        for y, x1, x2 in self.spans:
            line = chops[y]
            ends = chop_ends[y]
            emitted: list[tuple[int, Any]] = []
            for end, (x, strip) in zip(ends, line.items(), strict=False):
                if strip is None:
                    continue
                if x > x2 or end <= x1:
                    continue
                if not (x2 > x >= x1 and end <= x2):
                    strip = strip.crop(0, min(end, x2) - x)
                emitted.append((x, strip))

            row_has_rtl = any(_strip_has_rtl(strip) for _, strip in emitted)
            need_anchor = rtl_locale or row_has_rtl
            anchored = False
            if need_anchor and emitted and not any(x == 0 and strip.cell_length > 0 for x, strip in emitted):
                col0_strip = line.get(0)
                if col0_strip is not None and col0_strip.cell_length > 0:
                    anchor_cell = col0_strip.crop(0, 1)
                    append(move_to(0, y).segment.text)
                    append(
                        freeze_strip_for_apple_terminal(
                            anchor_cell,
                            anchor_col0=True,
                        ).render(console)
                    )
                    anchored = True
            for x, strip in emitted:
                anchor_this = need_anchor and not anchored and x == 0 and strip.cell_length > 0
                if anchor_this:
                    anchored = True
                append(move_to(x, y).segment.text)
                append(
                    freeze_strip_for_apple_terminal(
                        strip,
                        anchor_col0=anchor_this,
                    ).render(console)
                )

            if y != last_y:
                append("\n")

        return "".join(sequences)

    Visual.to_strips = _rtl_to_strips  # type: ignore[assignment]
    LayoutUpdate.render_segments = _rtl_layout_render_segments  # type: ignore[method-assign]
    ChopsUpdate.render_segments = _rtl_chops_render_segments  # type: ignore[method-assign]
    _TEXTUAL_RTL_INSTALLED = True


__all__ = [
    "EXPLICIT_BIDI_ENTER",
    "EXPLICIT_BIDI_EXIT",
    "RtlTerminalMode",
    "escape_markup",
    "freeze_strip_for_apple_terminal",
    "has_rtl",
    "has_unshaped_arabic",
    "install_textual_rtl_shaping",
    "is_apple_terminal",
    "rtl_shaping_disabled",
    "rtl_terminal_mode",
    "shape_strip",
    "visual_markup",
    "visual_text",
]
