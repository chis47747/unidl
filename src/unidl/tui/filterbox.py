"""The filter field that sits under a long list.

One field, one set of rules, wherever a list is long enough to need narrowing:

* type letters to narrow the list
* type a number to point at that entry, whatever the filter was
* Enter takes whatever is highlighted

That is deliberately the same contract as the main screen's platform filter, so
there is one thing to learn rather than one per list.

Numbers keep their meaning while a filter is on, which is why typing a digit
clears the filter instead of searching for the digit: a number is an entry's
address, and an address that moves is not one.
"""

from __future__ import annotations

from textual.widgets import Input

from ..core.i18n import tr

#: Lists shorter than this do not get a filter. A field under four options is
#: clutter, and the digits already reach all four.
FILTER_MIN = 10

DEFAULT_HINT = "type to narrow the list, or a number, then Enter"


class FilterBox(Input):
    """A one-line filter, styled like every other ask input."""

    def __init__(self, *, hint: str = "", id: str | None = None):
        super().__init__(
            placeholder=hint or tr("filter.hint"),
            id=id,
            classes="ask-input filter-box",
        )


def wanted(needle: str) -> str:
    return (needle or "").strip().lower()


def matches(needle: str, *parts: str) -> bool:
    """True when every whitespace-separated term appears somewhere in ``parts``.

    Terms rather than one substring, so ``s01 party`` finds an episode whose
    number and title are in different columns.
    """
    needle = wanted(needle)
    if not needle:
        return True
    haystack = " ".join(str(part or "") for part in parts).lower()
    return all(term in haystack for term in needle.split())


def as_number(needle: str) -> int | None:
    """The entry a purely numeric filter is pointing at, if it is one.

    ``isascii`` as well as ``isdigit``: superscripts and other numeric forms pass
    ``isdigit`` and then raise out of ``int``, and this runs on every keystroke, so
    pasting "²" into a filter took the app down.
    """
    text = wanted(needle)
    return int(text) if text.isascii() and text.isdigit() and text != "0" else None


__all__ = ["DEFAULT_HINT", "FILTER_MIN", "FilterBox", "as_number", "matches", "wanted"]
