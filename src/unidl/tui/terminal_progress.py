"""Terminal native progress bar (OSC 9;4), window title (OSC 0), and speed sparkline."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from .bidi import is_apple_terminal

OSC_PROGRESS_CLEAR = "\x1b]9;4;0;0\x07"
DEFAULT_TERMINAL_TITLE = "UniDL"

PROGRESS_HIDDEN = 0
PROGRESS_NORMAL = 1
PROGRESS_ERROR = 2
PROGRESS_INDETERMINATE = 3
PROGRESS_PAUSED = 4

DEFAULT_COMPLETION_ALERT = "off"
COMPLETION_ALERT_OPTIONS: tuple[str, ...] = ("off", "auto", "bell")

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
_OSC_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3})%(?!\d)")
_SPEED_RE = re.compile(r"(?<!\w)(\d+(?:\.\d+)?)\s*([KMGT])?(?:i)?B/s\b", re.IGNORECASE)
_UNIT_SCALE = {
    "": 1.0,
    "K": 1024.0,
    "M": 1024.0**2,
    "G": 1024.0**3,
    "T": 1024.0**4,
}


def sanitize_osc_text(text: str, *, max_chars: int = 72) -> str:
    """Strip control bytes so remote titles can never inject terminal escapes."""
    clean = " ".join(_OSC_FORBIDDEN.sub(" ", str(text or "")).split())
    if len(clean) > max_chars:
        return clean[: max_chars - 1].rstrip() + "…"
    return clean


def supports_osc_progress() -> bool:
    """Whether the active terminal likely handles ConEmu/OSC 9;4 progress bars."""
    if os.environ.get("TERM", "").strip().lower() == "dumb":
        return False
    # macOS Terminal.app ignores OSC 9;4 while supporting OSC 0 window titles.
    if is_apple_terminal():
        return False
    return True


def supports_osc_title() -> bool:
    """Whether the active terminal supports OSC 0 window/tab titles."""
    return os.environ.get("TERM", "").strip().lower() != "dumb"


def osc_progress_sequence(state: int, percent: int = 0) -> str:
    """Build an OSC 9;4 terminal progress sequence."""
    if state == PROGRESS_HIDDEN:
        return OSC_PROGRESS_CLEAR
    if state == PROGRESS_INDETERMINATE:
        return "\x1b]9;4;3;0\x07"
    clamped = max(0, min(100, int(percent)))
    return f"\x1b]9;4;{int(state)};{clamped}\x07"


def normalize_completion_alert(value: str | None) -> str:
    key = str(value or "").strip().lower()
    return key if key in COMPLETION_ALERT_OPTIONS else DEFAULT_COMPLETION_ALERT


def osc_notify_sequence(title: str, body: str, *, mode: str = DEFAULT_COMPLETION_ALERT) -> str:
    """Build a terminal completion alert sequence (OSC 9 / 99 / 777 or BEL)."""
    normalized = normalize_completion_alert(mode)
    if normalized == "off" or os.environ.get("TERM", "").strip().lower() == "dumb":
        return ""
    if normalized == "bell":
        return "\x07"
    if is_apple_terminal():
        # macOS Terminal.app ignores OSC 9 / 777 popups; BEL triggers visual bell / dock bounce.
        return "\x07"
    safe_title = sanitize_osc_text(title, max_chars=48).replace(";", ",") or DEFAULT_TERMINAL_TITLE
    safe_body = sanitize_osc_text(body, max_chars=96).replace(";", ",")
    term = os.environ.get("TERM", "").strip().lower()
    if os.environ.get("KITTY_WINDOW_ID") or term == "xterm-kitty":
        msg = f"{safe_title}: {safe_body}" if safe_body else safe_title
        return f"\x1b]99;;{msg}\x1b\\"
    if os.environ.get("GHOSTTY_RESOURCES_DIR") or os.environ.get("VTE_VERSION"):
        return f"\x1b]777;notify;{safe_title};{safe_body}\x07"
    msg = f"{safe_title}: {safe_body}" if safe_body else safe_title
    return f"\x1b]9;{msg}\x07"


def osc_title_sequence(title: str) -> str:
    """Build an OSC 0 window/tab title sequence."""
    safe = sanitize_osc_text(title) or DEFAULT_TERMINAL_TITLE
    return f"\x1b]0;{safe}\x07"


def parse_frame_metrics(rows: Sequence[str], *, spinner_chars: str = "") -> tuple[int | None, float | None, bool]:
    """Extract aggregate ``(percent, total_bytes_per_sec, spinning)`` from progress rows."""
    percents: list[int] = []
    total_speed = 0.0
    found_speed = False
    spinning = False
    for raw in rows:
        row = str(raw or "")
        if spinner_chars and any(ch in spinner_chars for ch in row):
            spinning = True
        if "━" in row or "─" in row or "%" in row:
            pct_match = _PERCENT_RE.search(row)
            if pct_match:
                pct = int(pct_match.group(1))
                if 0 <= pct <= 100:
                    percents.append(pct)
        speed_match = _SPEED_RE.search(row)
        if speed_match:
            val = float(speed_match.group(1))
            unit = (speed_match.group(2) or "").upper()
            total_speed += val * _UNIT_SCALE.get(unit, 1.0)
            found_speed = True
    percent = round(sum(percents) / len(percents)) if percents else None
    speed = total_speed if found_speed else None
    return percent, speed, spinning


def format_speed(bps: float | None) -> str:
    """Format bytes/second concisely for status bars and window titles."""
    if bps is None or bps < 0:
        return ""
    value = float(bps)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s", "TB/s"):
        if value < 1024.0 or unit == "TB/s":
            if unit == "B/s":
                return f"{int(round(value))} B/s"
            text = f"{value:.1f}" if value < 100 else f"{value:.0f}"
            return f"{text} {unit}"
        value /= 1024.0
    return ""


def render_sparkline(samples: Sequence[float], *, width: int = 12) -> str:
    """Render a compact Unicode waveform from recent speed samples (bytes/sec)."""
    if not samples or width <= 0:
        return ""
    tail = [max(0.0, float(v)) for v in samples[-width:]]
    peak = max(tail)
    if peak <= 0.0:
        return _SPARK_BLOCKS[0] * len(tail)
    levels = len(_SPARK_BLOCKS) - 1
    out: list[str] = []
    for sample in tail:
        if sample <= 0.0:
            out.append(_SPARK_BLOCKS[0])
        else:
            idx = max(1, min(levels, int(round(sample / peak * levels))))
            out.append(_SPARK_BLOCKS[idx])
    return "".join(out)


def format_terminal_title(
    *,
    percent: int | None = None,
    speed_bps: float | None = None,
    paused: bool = False,
    batch_pos: tuple[int, int] | None = None,
    name: str = "",
    status_tag: str = "",
) -> str:
    """Format a concise window/tab title for active or completed downloads."""
    bits: list[str] = []
    if status_tag:
        bits.append(status_tag)
    if batch_pos is not None and batch_pos[1] > 1:
        bits.append(f"{batch_pos[0]}/{batch_pos[1]}")
    if paused:
        bits.append("Paused")
    if percent is not None:
        bits.append(f"{max(0, min(100, int(percent)))}%")
    speed_text = format_speed(speed_bps) if (speed_bps and not paused) else ""
    if speed_text:
        bits.append(speed_text)
    prefix = f"[{' · '.join(bits)}] " if bits else ""
    clean_name = sanitize_osc_text(name, max_chars=36)
    if clean_name:
        return f"{prefix}{clean_name} — {DEFAULT_TERMINAL_TITLE}"
    if prefix:
        return f"{prefix}{DEFAULT_TERMINAL_TITLE}"
    return DEFAULT_TERMINAL_TITLE


__all__ = [
    "COMPLETION_ALERT_OPTIONS",
    "DEFAULT_COMPLETION_ALERT",
    "DEFAULT_TERMINAL_TITLE",
    "OSC_PROGRESS_CLEAR",
    "PROGRESS_ERROR",
    "PROGRESS_HIDDEN",
    "PROGRESS_INDETERMINATE",
    "PROGRESS_NORMAL",
    "PROGRESS_PAUSED",
    "format_speed",
    "format_terminal_title",
    "normalize_completion_alert",
    "osc_notify_sequence",
    "osc_progress_sequence",
    "osc_title_sequence",
    "parse_frame_metrics",
    "render_sparkline",
    "sanitize_osc_text",
    "supports_osc_progress",
    "supports_osc_title",
]
