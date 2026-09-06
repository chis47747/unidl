"""Picking a set of episodes by writing it down instead of ticking it.

A twenty-four episode season is twenty-four keystrokes and a scroll; two seasons
of a long-running show is not a list anybody wants to tick. So the field under a
list takes an expression as well as a filter:

    S02              the whole of season two
    S01-S03          three seasons
    S01E05-S02E03    from one episode to another, across the season break
    E05              episode five, whatever season the list is showing
    3-8              by row number, the same numbers the list already shows
    1-20,-7,-13      a span with two holes in it
    all              everything

This module only turns that text into a set of row numbers. It knows nothing
about widgets, and it is deliberately separate from the filter: a filter narrows
what you can see, an expression states what you want, and conflating them meant
"S01" hid season two rather than selecting season one.

The grammar is unshackle's ``-w/--wanted`` (``S01-S05,S07``, ``S01E01-S02E03``,
``-S03`` to exclude), which is the convention people already type, with row
numbers added because our lists are numbered and that is what the digits on the
same field already mean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: ``S01``, ``S01E05``, ``E05``. Either half may be missing, but not both.
_MARKER = re.compile(r"^(?:s(?P<season>\d{1,3}))?(?:e(?P<episode>\d{1,4}))?$", re.I)
#: a row number, as printed beside the entry
_ROW = re.compile(r"^\d{1,5}$")
#: what an entry says about itself, read off its label and detail
_LABELLED = re.compile(r"\bs(?P<season>\d{1,3})\s*e(?P<episode>\d{1,4})\b", re.I)
_EPISODE_ONLY = re.compile(r"\be(?:p(?:isode)?)?\.?\s*(?P<episode>\d{1,4})\b", re.I)

#: what "everything" may be written as
EVERYTHING = ("all", "*", "a")

#: An episode number this side of absurd. A span that ends at a season rather than
#: an episode runs to here, which is cheaper than carrying "unbounded" through
#: every comparison.
_LAST = 10**6


@dataclass(frozen=True)
class Entry:
    """One row of a list, as far as an expression is concerned.

    ``season`` and ``episode`` are what the row says about itself; either may be
    None for a row that is not an episode at all, and such a row can then only be
    addressed by its number.
    """

    row: int
    season: int | None = None
    episode: int | None = None

    @property
    def marked(self) -> bool:
        return self.season is not None or self.episode is not None


def read_entry(row: int, *parts: str) -> Entry:
    """What row ``row`` says it is, read off the text a service wrote for it.

    Structured season and episode numbers would be better than a regex over a
    label, but they would have to come from a hundred and fifty services; the text
    is already there and already says it. ``S01E02`` first, then a bare episode
    number, and nothing at all is a legitimate answer.
    """
    text = " ".join(str(part or "") for part in parts)
    found = _LABELLED.search(text)
    if found is not None:
        return Entry(row, int(found.group("season")), int(found.group("episode")))
    found = _EPISODE_ONLY.search(text)
    if found is not None:
        return Entry(row, None, int(found.group("episode")))
    return Entry(row)


@dataclass(frozen=True)
class _Point:
    """One end of a span: a row number, or a season/episode marker."""

    row: int | None = None
    season: int | None = None
    episode: int | None = None

    @property
    def is_row(self) -> bool:
        return self.row is not None


def _point(token: str) -> _Point | None:
    token = token.strip()
    if not token:
        return None
    if _ROW.match(token):
        number = int(token)
        return _Point(row=number) if number > 0 else None
    found = _MARKER.match(token)
    if found is None:
        return None
    season = found.group("season")
    episode = found.group("episode")
    if season is None and episode is None:
        return None
    return _Point(
        season=int(season) if season is not None else None,
        episode=int(episode) if episode is not None else None,
    )


def _open_end(start: _Point) -> _Point:
    """The far end of ``13-``: everything from there on.

    Written as a point past the end rather than as a flag on the span, so the
    comparisons below stay one shape. Which *kind* of point matters: a bare episode
    marker (``E05-``) must stay a bare episode marker, or the span stops being "the
    fifth episode onwards in any season" and becomes "from the very beginning".
    """
    if start.is_row:
        return _Point(row=_LAST)
    if start.season is None:
        return _Point(episode=_LAST)
    return _Point(season=_LAST, episode=_LAST)


def _sortable(point: _Point, *, end: bool) -> tuple[int, int]:
    """A season/episode marker as a comparable pair.

    A span that names a season without an episode covers the whole season, so the
    missing half is the first episode at the start of a span and the last at the
    end of one. ``E05`` with no season is any season's fifth episode, which is
    handled by the caller rather than here.
    """
    season = point.season if point.season is not None else (0 if not end else _LAST)
    episode = point.episode if point.episode is not None else (0 if not end else _LAST)
    return season, episode


@dataclass(frozen=True)
class _Span:
    """One comma-separated part of an expression."""

    start: _Point
    end: _Point
    exclude: bool = False

    def holds(self, entry: Entry) -> bool:
        if self.start.is_row:
            if entry.row <= 0:
                return False
            low, high = self.start.row or 0, self.end.row or 0
            return min(low, high) <= entry.row <= max(low, high)
        # a bare E05 - or E05-E09 - is about episode numbers in any season
        if self.start.season is None and self.end.season is None:
            if entry.episode is None:
                return False
            low = self.start.episode or 0
            high = self.end.episode if self.end.episode is not None else low
            return min(low, high) <= entry.episode <= max(low, high)
        if not entry.marked:
            return False
        low = _sortable(self.start, end=False)
        high = _sortable(self.end, end=True)
        if high < low:
            low, high = high, low
        where = (entry.season or 0, entry.episode or 0)
        return low <= where <= high


@dataclass(frozen=True)
class Selection:
    """A parsed expression, ready to be asked which rows it wants."""

    spans: tuple[_Span, ...]
    everything: bool = False

    def rows(self, entries: list[Entry]) -> set[int]:
        """The row numbers this expression asks for, out of ``entries``."""
        if self.everything:
            wanted = {entry.row for entry in entries}
        else:
            wanted = {
                entry.row
                for entry in entries
                for span in self.spans
                if not span.exclude and span.holds(entry)
            }
        for entry in entries:
            if any(span.exclude and span.holds(entry) for span in self.spans):
                wanted.discard(entry.row)
        return wanted

    @property
    def only_excludes(self) -> bool:
        """True for ``-S03`` on its own, which means "everything but that".

        Such an expression is built with ``everything`` set, because that is how it
        is evaluated - start from all the rows and take the holes out - so this asks
        about the spans rather than about that flag.
        """
        return bool(self.spans) and all(span.exclude for span in self.spans)


def parse(expression: str) -> Selection | None:
    """``"S01E05-S02E03,-S02E01"`` as a :class:`Selection`, or None.

    None means "this is not an expression", and the caller should treat the text as
    an ordinary filter. That is the whole reason this returns None rather than an
    empty selection: an empty selection is a valid answer that happens to match
    nothing, and a filter is not an answer at all.
    """
    text = (expression or "").strip()
    if not text:
        return None
    if text.lower() in EVERYTHING:
        return Selection(spans=(), everything=True)

    spans: list[_Span] = []
    for raw in text.split(","):
        part = raw.strip()
        if not part:
            continue
        exclude = part.startswith(("-", "!"))
        if exclude:
            part = part[1:].strip()
        # A part that ends in a dash names no far end, and means everything from its
        # start onwards: "13-" is the rest of the list, "-13-" drops it. Read the
        # same way while it is being typed as when it is taken, because the hint
        # under the list says how many it would select and that must not turn out to
        # have been a different number.
        open_ended = part.endswith("-")
        halves = [half for half in part.split("-") if half.strip()]
        if len(halves) > 2 or not halves:
            return None
        start = _point(halves[0])
        if start is None:
            return None
        if len(halves) == 2:
            end = _point(halves[1])
        elif open_ended:
            end = _open_end(start)
        else:
            end = start
        if end is None:
            return None
        if start.is_row != end.is_row:
            # "3-S02" mixes a row number with a season marker; there is no sensible
            # reading of that, and guessing one would select the wrong episodes.
            return None
        spans.append(_Span(start=start, end=end, exclude=exclude))

    if not spans:
        return None
    only_excludes = all(span.exclude for span in spans)
    return Selection(spans=tuple(spans), everything=only_excludes)


def is_expression(text: str) -> bool:
    """Whether this text should be read as a set of episodes rather than a filter.

    A single number is deliberately *not* an expression: on this same field a bare
    number already means "point at that entry", and an address that changes meaning
    depending on a mode is not an address.
    """
    stripped = (text or "").strip()
    if not stripped or _ROW.match(stripped):
        return False
    return parse(stripped) is not None


__all__ = ["EVERYTHING", "Entry", "Selection", "is_expression", "parse", "read_entry"]
