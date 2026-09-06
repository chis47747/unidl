"""Rendering for stream listings.

`StreamInfo.format_line()` is the canonical single-string description, used for
drop-filter matching and metadata as well as display. Everything here renders
that same string: colorizing its tokens, and measuring/cutting it to the
terminal without counting ANSI escapes as visible width.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from .console import Palette, cell_width, paint

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _range_color(value: str) -> str | None:
    """Resolved per call so the palette can adapt to the detected color level."""
    if value in {"DV+HDR10+", "DV+HDR10", "DV"}:
        return Palette.bold + Palette.magenta
    if value == "HDR10+":
        return Palette.bold + Palette.yellow
    if value == "HDR10":
        return Palette.yellow
    if value == "HLG":
        return Palette.green
    if value == "SDR":
        return Palette.muted
    return None


def visible_len(text: str) -> int:
    """Terminal cells a possibly-colored string occupies."""
    return cell_width(_ANSI_ESCAPE_RE.sub("", text))


def truncate(text: str, width: int) -> str:
    """Cut a possibly-colored string to `width` cells, preserving escapes."""
    if width <= 0:
        return ""
    if visible_len(text) <= width:
        return text
    output: list[str] = []
    used = 0
    position = 0
    had_ansi = False
    for match in _ANSI_ESCAPE_RE.finditer(text):
        if match.start() > position:
            used = _append_chunk(output, text[position : match.start()], width, used)
            if used >= width:
                break
        output.append(match.group(0))
        had_ansi = True
        position = match.end()
    else:
        if position < len(text):
            _append_chunk(output, text[position:], width, used)
    result = "".join(output)
    if had_ansi and result and not result.endswith(Palette.reset):
        result += Palette.reset
    return result


def _append_chunk(output: list[str], chunk: str, width: int, used: int) -> int:
    for char in chunk:
        size = cell_width(char)
        if used + size > width:
            return used
        output.append(char)
        used += size
    return used


def ellipsize(text: str, width: int) -> str:
    if width <= 0 or visible_len(text) <= width:
        return text
    if width <= 3:
        return truncate(text, width)
    return truncate(text, width - 3) + "..."


def pad(text: str, width: int, align: str = "l") -> str:
    """Pad to `width` terminal cells, honouring wide glyphs and escapes."""
    filler = " " * max(0, width - visible_len(text))
    return filler + text if align == "r" else text + filler


def colorize_stream_text(stream, line: str, colors: bool | None = None) -> str:
    """Highlight the meaningful tokens inside a pipe-joined `format_line()`."""
    if not colors:
        return line
    prefix = stream.display_prefix()
    prefix_color = {
        "Vid": Palette.cyan,
        "Aud": Palette.green,
        "Sub": Palette.magenta,
    }.get(prefix, Palette.blue)

    if line.startswith(prefix):
        line = paint(prefix, prefix_color, colors) + line[len(prefix) :]
    if "Encrypted" in line:
        line = line.replace("Encrypted", paint("Encrypted", Palette.yellow, colors))
    if " *" in line:
        scheme_start = line.find(" *")
        scheme_end = line.find(" ", scheme_start + 2)
        if scheme_end > scheme_start:
            scheme = line[scheme_start:scheme_end]
            line = line.replace(scheme, paint(scheme, Palette.yellow, colors), 1)
    if getattr(stream, "media_type", None) == "audio":
        line = re.sub(
            r"(?<![A-Za-z0-9+])E-AC-3 Atmos(?![A-Za-z0-9+])",
            lambda match: paint(match.group(0), Palette.bold + Palette.blue, colors),
            line,
        )
    for label in ("DV+HDR10+", "DV+HDR10", "HDR10+", "HDR10", "HLG", "DV", "SDR"):
        color = _range_color(label)
        if not color:
            continue
        line = re.sub(
            rf"(?<![A-Za-z0-9+]){re.escape(label)}(?![A-Za-z0-9+])",
            lambda match, color=color: paint(match.group(0), color, colors),
            line,
        )
    return line


def render_stream_table(
    streams: Sequence,
    *,
    width: int,
    colors: bool | None = None,
    numbers: dict[int, int] | None = None,
    checked: Iterable | None = None,
    show_checkbox: bool = True,
    indent: str = "  ",
) -> list[str]:
    """Numbered listing, one canonical `format_line()` row per stream.

    Same row shape the interactive picker uses, so a track reads identically
    whether it is listed, picked, or echoed back in a summary.
    """
    if not streams:
        return []
    checked_ids = {id(s) for s in (checked or [])}
    numbers = numbers or {id(stream): position for position, stream in enumerate(streams, start=1)}
    number_width = max(2, len(str(max(numbers.values(), default=len(streams)))))
    prefix_width = len(indent) + number_width + 2 + (4 if show_checkbox else 0)

    lines: list[str] = []
    for stream in streams:
        number = numbers.get(id(stream), 0)
        label = paint(f"{number:>{number_width}}.", Palette.muted, colors)
        row = ellipsize(
            colorize_stream_text(stream, stream.format_line(), colors),
            max(1, width - prefix_width),
        )
        if show_checkbox:
            mark = (
                paint("[x]", Palette.green, colors)
                if id(stream) in checked_ids
                else paint("[ ]", Palette.muted, colors)
            )
            lines.append(f"{indent}{label} {mark} {row}".rstrip())
        else:
            lines.append(f"{indent}{label} {row}".rstrip())
    return lines
