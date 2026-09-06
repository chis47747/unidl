"""Optional chapter metadata shared by services, exports and delivery.

A provider's chapter endpoint is service-owned: authentication, identifiers and
field names stay in that service's API module.  The handoff to Core is one small,
unit-explicit value.  Milliseconds are used deliberately; an unlabelled integer
such as ``90`` is otherwise impossible to distinguish as seconds or milliseconds.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


def _one_line(value: object) -> str:
    """Collapse provider text to one safe display/mux title line."""
    return " ".join(str(value or "").split())


def _milliseconds(value: object, *, field: str, optional: bool = False) -> int | None:
    if optional and value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"chapter {field} must be milliseconds, not a boolean")
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise ValueError(f"chapter {field} must be an integer number of milliseconds")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"chapter {field} must be an integer number of milliseconds") from exc
    if number < 0:
        raise ValueError(f"chapter {field} cannot be negative")
    return number


@dataclass(frozen=True, slots=True)
class Chapter:
    """One navigation chapter supplied by a service.

    ``end_ms`` may be omitted when the provider publishes start markers only.
    :func:`normalize_chapters` infers it from the next distinct start, and from
    the media duration for the final chapter when that duration is known.

    ``kind`` is a display hint such as ``intro``, ``recap`` or ``credits``.  It
    does not control skipping and is not interpreted as an advertisement rule.
    """

    start_ms: int
    title: str
    end_ms: int | None = None
    kind: str = ""

    def __post_init__(self) -> None:
        start = _milliseconds(self.start_ms, field="start_ms")
        end = _milliseconds(self.end_ms, field="end_ms", optional=True)
        if end is not None and end <= start:
            raise ValueError("chapter end_ms must be greater than start_ms")
        title = _one_line(self.title)
        kind = _one_line(self.kind).casefold()
        if not title and not kind:
            raise ValueError("chapter needs a title or kind")
        object.__setattr__(self, "start_ms", start)
        object.__setattr__(self, "end_ms", end)
        object.__setattr__(self, "title", title or kind.replace("_", " ").title())
        object.__setattr__(self, "kind", kind)

    @classmethod
    def from_seconds(
        cls,
        start: int | float,
        title: str,
        end: int | float | None = None,
        *,
        kind: str = "",
    ) -> Chapter:
        """Build a chapter from an API that explicitly reports seconds."""
        def convert(value: int | float, field: str) -> int:
            if isinstance(value, bool):
                raise ValueError(f"chapter {field} must be seconds, not a boolean")
            try:
                seconds = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"chapter {field} must be a finite number of seconds") from exc
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError(f"chapter {field} must be a finite, non-negative number of seconds")
            return round(seconds * 1000)

        start_ms = convert(start, "start")
        end_ms = convert(end, "end") if end is not None else None
        return cls(start_ms=start_ms, title=title, end_ms=end_ms, kind=kind)

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "start_ms": self.start_ms,
            "title": self.title,
        }
        if self.end_ms is not None:
            document["end_ms"] = self.end_ms
        if self.kind:
            document["kind"] = self.kind
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> Chapter:
        if not isinstance(document, Mapping):
            raise ValueError("chapter document must be an object")
        return cls(
            start_ms=document.get("start_ms"),
            title=str(document.get("title") or ""),
            end_ms=document.get("end_ms"),
            kind=str(document.get("kind") or ""),
        )


def normalize_chapters(
    chapters: Iterable[Chapter],
    *,
    duration_ms: int | None = None,
) -> tuple[Chapter, ...]:
    """Return a stable, deduplicated timeline with missing ends inferred.

    Exact duplicates are dropped.  Different labels at the same timestamp are
    retained: providers sometimes deliberately publish an ``intro`` marker and a
    named scene at the same point, and Core must not silently decide one is false.
    """
    duration = _milliseconds(duration_ms, field="duration_ms", optional=True)
    indexed: list[tuple[int, Chapter]] = []
    seen: set[tuple[int, int | None, str, str]] = set()
    for index, chapter in enumerate(chapters or ()):
        if not isinstance(chapter, Chapter):
            raise TypeError("Playback.chapters entries must be Chapter values")
        identity = (chapter.start_ms, chapter.end_ms, chapter.title, chapter.kind)
        if identity in seen:
            continue
        seen.add(identity)
        indexed.append((index, chapter))
    indexed.sort(key=lambda item: (item[1].start_ms, item[0]))
    ordered = [chapter for _index, chapter in indexed]

    result: list[Chapter] = []
    for index, chapter in enumerate(ordered):
        end = chapter.end_ms
        if end is None:
            end = next(
                (
                    following.start_ms
                    for following in ordered[index + 1 :]
                    if following.start_ms > chapter.start_ms
                ),
                None,
            )
            if end is None and duration is not None and duration > chapter.start_ms:
                end = duration
        result.append(
            Chapter(
                start_ms=chapter.start_ms,
                end_ms=end,
                title=chapter.title,
                kind=chapter.kind,
            )
        )
    return tuple(result)


def timestamp(milliseconds: int | None, *, precise: bool = False) -> str:
    """Format a chapter position as ``HH:MM:SS`` (optionally milliseconds)."""
    if milliseconds is None:
        return "--:--:--"
    total = max(0, int(milliseconds))
    seconds, millis = divmod(total, 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    base = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    # OGM/simple chapter files accepted by mkvmerge require the fractional part
    # even for an exact second.  ``precise`` therefore describes the output
    # shape, not whether this particular value happens to have non-zero millis.
    return f"{base}.{millis:03d}" if precise else base


def summary(chapters: Iterable[Chapter]) -> str:
    """One compact line for logs and the delivery details card."""
    values = tuple(chapters or ())
    if not values:
        return "none"
    span_end = values[-1].end_ms
    span = f"{timestamp(values[0].start_ms)}–{timestamp(span_end)}" if span_end is not None else f"from {timestamp(values[0].start_ms)}"
    return f"{count_label(values)} · {span}"


def count_label(chapters: Iterable[Chapter]) -> str:
    """Clickable badge text next to a track or delivery title."""
    from .i18n import tr

    count = len(tuple(chapters or ()))
    return tr("chapters.count", count=count)


def length(chapter: Chapter) -> str:
    """Duration of one chapter when both ends are known."""
    if chapter.end_ms is None:
        return ""
    return timestamp(chapter.end_ms - chapter.start_ms)


__all__ = ["Chapter", "count_label", "length", "normalize_chapters", "summary", "timestamp"]
