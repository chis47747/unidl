"""Widgets that render one :class:`~unidl.core.flow.Ask`.

Each widget posts :class:`AskWidget.Answered` exactly once. Mouse and keyboard
both work because the underlying Textual widgets support both; the custom bit is
making ``enter`` take the highlight when nothing is ticked, or confirm the batch
when something is, rather than toggling the row like Textual normally does.
"""

from __future__ import annotations

import re
import time
from typing import Any

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import DataTable, Label, OptionList, SelectionList, Static
from textual.widgets.option_list import Option

from ..core import ranges
from ..core.flow import Ask, Await, Confirm, Form, Panel, Pick, TableAsk, TextAsk
from ..core.i18n import tr
from .audio import AudioPreviewWidget
from .bidi import visual_markup, visual_text
from .chrome import StatusChip
from .filterbox import FILTER_MIN, FilterBox, as_number, matches, wanted
from .input import ClipboardInput as Input
from .qr import QrWidget

BACK = object()
"""Sentinel answer meaning "the user went back"."""

#: How much of a value fits on one row inside a manual card. A longer one is
#: wrapped instead of sized to its content, because a card is a fixed width and
#: content wider than it is gets clipped - and half a command line is worse than
#: none. Measured against the *narrow* card: it is 78 columns with a `max-width`
#: of 96%, so at an 80-column terminal it is 72 and only 62 characters fit. Sized
#: for the wide card, values of 63 to 68 lost their ends on a narrow one.
VALUE_FIT = 62


class _ConfirmingSelectionList(SelectionList[int]):
    """SelectionList where space toggles and enter submits to its owner.

    The type parameter is bound here rather than at the call site: subscripting
    a non-generic subclass raises at compose time.
    """

    BINDINGS = [
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("a", "toggle_all", "Toggle all", show=False),
        Binding("n", "clear_all", "Clear", show=False),
    ]

    class Confirmed(Message):
        pass

    def action_confirm(self) -> None:
        self.post_message(self.Confirmed())

    def action_toggle_all(self) -> None:
        # option_count is the public way to ask this; _options is Textual's
        if len(self.selected) == self.option_count:
            self.deselect_all()
        else:
            self.select_all()

    def action_clear_all(self) -> None:
        self.deselect_all()


class AskWidget(Vertical):
    """Base for ask renderers."""

    class Answered(Message):
        def __init__(self, value: Any) -> None:
            super().__init__()
            self.value = value

    def __init__(self, ask: Ask) -> None:
        super().__init__()
        self.ask = ask
        self._answered = False

    def answer(self, value: Any) -> None:
        if self._answered:
            return
        self._answered = True
        self.post_message(self.Answered(value))

    def go_back(self) -> None:
        self.answer(BACK)

    def focus_first(self) -> None:
        for child in self.walk_children():
            if getattr(child, "can_focus", False):
                child.focus()
                return

    # subclasses provide the header rows
    def _header(self):
        chapters = tuple(getattr(self.ask, "chapters", ()) or ())
        lyrics = getattr(self.ask, "lyrics", None)
        if self.ask.title:
            if chapters or lyrics is not None:
                from ..core.chapters import count_label

                with Horizontal(classes="ask-title-row"):
                    yield Label(visual_markup(self.ask.title), classes="ask-title")
                    if chapters:
                        chip = StatusChip(
                            "show_chapters", id="ask-chapters", classes="chapter-chip"
                        )
                        chip.update(count_label(chapters))
                        yield chip
                    if lyrics is not None:
                        lyric_chip = StatusChip(
                            "show_lyrics", id="ask-lyrics", classes="chapter-chip"
                        )
                        lyric_chip.update(tr("lyrics.lines_n", count=len(lyrics.lines)))
                        yield lyric_chip
            else:
                yield Label(visual_markup(self.ask.title), classes="ask-title")
        hint = getattr(self.ask, "hint", "")
        if hint:
            yield Label(f"[$dim]{visual_markup(hint)}[/]", classes="ask-hint")


def _highlight_choice_label(label: str, highlights: tuple[tuple[str, str], ...]) -> str:
    """Add palette markup to non-overlapping pieces of an escaped label."""
    candidates = [
        (visual_markup(term), str(role).strip())
        for term, role in highlights
        if str(term).strip() and str(role).strip()
    ]
    candidates.sort(key=lambda item: len(item[0]), reverse=True)
    if not candidates:
        return label
    pattern = re.compile(
        "|".join(
            (
                f"(?P<t{index}>"
                f"(?<![A-Za-z0-9_-]){re.escape(term)}"
                f"(?![A-Za-z0-9_-]))"
            )
            for index, (term, _role) in enumerate(candidates)
        ),
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        index = int(match.lastgroup[1:]) if match.lastgroup else 0
        return f"[${candidates[index][1]}]{match.group(0)}[/]"

    return pattern.sub(replace, label)


def render_choice(choice, number: int | None = None, *, single_line: bool = False) -> str:
    """Markup for one choice row.

    Numbered to match the main screen, so "type the number" works the same way
    everywhere. Module level rather than a method: a ``_render`` on a Widget
    subclass would shadow Textual's own ``Widget._render``.

    ``single_line`` folds the detail onto the same row. ``SelectionList`` clips
    each option to one line, so a second line there is not wrapped, it is thrown
    away.
    """
    prefix = f"[$gutter]{number:>3}[/]  " if number is not None else ""
    rendered_label = visual_markup(choice.label)
    if choice.style:
        rendered_label = f"[${choice.style}]{rendered_label}[/]"
    elif choice.highlights:
        rendered_label = _highlight_choice_label(rendered_label, choice.highlights)
    label = f"{prefix}{rendered_label}"
    if choice.tags:
        tags = " ".join(visual_markup(tag) for tag in choice.tags)
        label = f"{label}  [$dim]{tags}[/]"
    if choice.detail:
        joiner = "  [$gutter]·[/]  " if single_line else "\n     "
        label = f"{label}{joiner}[$dim]{visual_markup(choice.detail)}[/]"
    return label


class _NumberJump:
    """Shared behaviour: typing digits moves the cursor to that entry."""

    def _typed_number(self, digit: str) -> int | None:
        """The number being typed, treating a pause as the end of the last one."""
        now = time.monotonic()
        if now - getattr(self, "_typed_at", 0.0) > 1.0:
            self._typed = ""
        self._typed = (getattr(self, "_typed", "") + digit)[-3:]
        self._typed_at = now
        try:
            return int(self._typed)
        except ValueError:
            return None


class _Filterable(AskWidget):
    """An ask whose list is long enough to be worth narrowing.

    Option ids stay the *original* index, so a filtered list still answers with
    the right choice and a typed number still means what it says. The visible
    subset is tracked separately.
    """

    #: bound here rather than on each subclass: with the filter focused these
    #: keys have to be forwarded to the list, which does not have focus
    BINDINGS = [
        Binding("up", "cursor(-1)", "Up", show=False),
        Binding("down", "cursor(1)", "Down", show=False),
        Binding("pageup", "cursor(-10)", "Page up", show=False),
        Binding("pagedown", "cursor(10)", "Page down", show=False),
    ]

    def __init__(self, ask: Ask) -> None:
        super().__init__(ask)
        self._visible: list[int] = list(range(len(ask.choices)))
        self._filtered = False

    # the concrete list widget, which differs between single and multi select
    def _list(self) -> OptionList:
        raise NotImplementedError

    @property
    def filterable(self) -> bool:
        return len(self.ask.choices) >= FILTER_MIN

    def _filter_hint(self) -> str:
        return tr("ask.filter.entries", count=len(self.ask.choices))

    def compose_filter(self):
        if self.filterable:
            yield FilterBox(hint=self._filter_hint())

    def apply_filter(self, needle: str) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------ events
    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        number = as_number(event.value)
        if number is not None:
            # a number addresses an entry; it does not describe one
            self.apply_filter("")
            self.highlight_original(number - 1)
            return
        self.apply_filter(event.value)

    def highlight_original(self, original: int) -> None:
        if original in self._visible:
            self._list().highlighted = self._visible.index(original)

    def action_cursor(self, delta: int) -> None:
        """Move the list while the filter field has the focus."""
        widget = self._list()
        count = widget.option_count
        if not count:
            return
        current = widget.highlighted if widget.highlighted is not None else -1 if delta > 0 else count
        widget.highlighted = max(0, min(count - 1, current + delta))

    def _focus_default(self) -> None:
        """The filter when there is one, the list otherwise."""
        found = self.query(FilterBox)
        target = found.first() if found else None
        if target is None:
            lists = self.query(OptionList)
            target = lists.first() if lists else None
        if target is not None:
            target.focus()

    def focus_first(self) -> None:
        # AskHost calls this straight after mounting, before compose has
        # necessarily run; on_mount calls it again once children exist
        self._focus_default()


class PickWidget(_Filterable, _NumberJump):
    """Single choice."""

    BINDINGS = [Binding(str(digit), f"digit('{digit}')", show=False) for digit in range(10)]

    def compose(self):
        yield from self._header()
        preview = getattr(self.ask, "preview", None)
        if preview is not None:
            from textual.containers import Horizontal, Vertical

            with Horizontal(classes="audio-track-picker"):
                yield AudioPreviewWidget(preview, classes="audio-track-preview")
                with Vertical(classes="audio-track-controls"):
                    yield OptionList(*self._options(self._visible))
                    yield from self.compose_filter()
            return
        yield OptionList(*self._options(self._visible))
        yield from self.compose_filter()

    def _options(self, indexes: list[int]) -> list[Option]:
        return [
            Option(
                render_choice(self.ask.choices[index], index + 1),
                id=str(index),
                disabled=self.ask.choices[index].disabled,
            )
            for index in indexes
        ]

    def _list(self) -> OptionList:
        return self.query_one(OptionList)

    def on_mount(self) -> None:
        option_list = self._list()
        if self.ask.choices:
            option_list.highlighted = max(0, min(self.ask.cursor, len(self.ask.choices) - 1))
        self._focus_default()

    def apply_filter(self, needle: str) -> None:
        keep = [
            index
            for index, choice in enumerate(self.ask.choices)
            if matches(needle, choice.label, choice.detail, *choice.tags)
        ]
        if keep == self._visible:
            return
        self._visible = keep
        widget = self._list()
        widget.clear_options()
        if keep:
            widget.add_options(self._options(keep))
            widget.highlighted = 0
        self._filtered = bool(wanted(needle))

    def action_digit(self, digit: str) -> None:
        """Digits reach here only when the list itself has the focus.

        Resolved against the *original* index, like everywhere else in the app: the
        number printed on a row is the choice's address, not its position. Treating
        it as a position meant that with a filter on, typing 2 highlighted the
        second visible row - whose printed number was something else entirely, and
        there was no row numbered 2 on screen at all.
        """
        wanted = self._typed_number(digit)
        if wanted is not None and 1 <= wanted <= len(self.ask.choices):
            self.highlight_original(wanted - 1)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter takes whatever is highlighted."""
        event.stop()
        widget = self._list()
        index = widget.highlighted
        if index is None or index >= len(self._visible):
            return
        original = self._visible[index]
        if not self.ask.choices[original].disabled:
            self.answer(self.ask.choices[original].value)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = int(event.option.id or 0)
        self.answer(self.ask.choices[index].value)


class MultiPickWidget(_Filterable):
    """Multi choice with checkboxes.

    What is ticked is remembered here, not read off the widget, because filtering
    rebuilds the list. Narrowing the view must not silently un-tick the episodes
    that scrolled out of it.
    """

    def __init__(self, ask: Ask) -> None:
        super().__init__(ask)
        self._chosen: set[int] = {
            index for index in ask.preselected if 0 <= index < len(ask.choices)
        }
        #: rows a half-typed expression would select, or None when the field holds
        #: an ordinary filter. Kept rather than applied on every keystroke, because
        #: "S01" on the way to "S01E05" is a different set and ticking both in turn
        #: leaves the union behind.
        self._pending: set[int] | None = None

    def compose(self):
        yield from self._header()
        preview = getattr(self.ask, "preview", None)
        if preview is not None:
            # Audio-only delivery gets a deliberate two-column card: the artwork
            # and ID3 facts stay visible while the right column is navigated.  The
            # ordinary track picker has no preview and keeps its existing layout.
            from textual.containers import Horizontal, Vertical

            with Horizontal(classes="audio-track-picker"):
                yield AudioPreviewWidget(preview, classes="audio-track-preview")
                with Vertical(classes="audio-track-controls"):
                    yield Label("", id="multi-hint", classes="ask-hint")
                    yield _ConfirmingSelectionList(*self._selections(self._visible))
                    yield from self.compose_filter()
            return
        yield Label("", id="multi-hint", classes="ask-hint")
        yield _ConfirmingSelectionList(*self._selections(self._visible))
        yield from self.compose_filter()

    def _selections(self, indexes: list[int]) -> list[tuple[str, int, bool]]:
        return [
            (
                render_choice(self.ask.choices[index], index + 1, single_line=True),
                index,
                index in self._chosen,
            )
            for index in indexes
        ]

    def _list(self) -> OptionList:
        return self.query_one(_ConfirmingSelectionList)

    def _filter_hint(self) -> str:
        return tr("filter.hint.multi", count=len(self.ask.choices))

    # ------------------------------------------------------------- expressions
    def _entries(self) -> list[ranges.Entry]:
        """Every row, with whatever season and episode it says it is."""
        return [
            ranges.read_entry(index + 1, choice.label, choice.detail)
            for index, choice in enumerate(self.ask.choices)
        ]

    def _expression_rows(self, text: str) -> set[int] | None:
        """The rows ``text`` asks for, or None when it is not an expression."""
        if not ranges.is_expression(text):
            return None
        selection = ranges.parse(text)
        return None if selection is None else selection.rows(self._entries())

    def _focus_default(self) -> None:
        """The list, not the filter.

        Opposite of a single-select on purpose. Here the keyboard's main job is
        ticking, and space has to mean toggle; in a focused text field it would
        mean "type a space", which a multi-word filter legitimately needs. So the
        list keeps the focus and tab reaches the filter.
        """
        lists = self.query(_ConfirmingSelectionList)
        if lists:
            lists.first().focus()

    def on_mount(self) -> None:
        widget = self._list()
        if self.ask.choices:
            widget.highlighted = max(0, min(self.ask.cursor, len(self.ask.choices) - 1))
        self._render_hint()
        self._focus_default()

    def _render_hint(self) -> None:
        picked = len(self._chosen)
        total = len(self.ask.choices)
        if self._pending is not None:
            # An expression is being typed. It says what it would select and how to
            # take it, rather than the keys for ticking rows one at a time - the
            # point of writing a set down is not having to.
            wanted = len(self._pending)
            counted = (
                f"[$accent]{tr('ask.multi.would', wanted=wanted, total=total)}[/]"
                if wanted
                else f"[$warn]{tr('ask.multi.nothing')}[/]"
            )
            self.query_one("#multi-hint", Label).update(
                f"[$dim]{tr('ask.multi.pending')}[/]   {counted}"
            )
            return
        enter = tr("ask.multi.enter_open") if not picked else tr("ask.multi.enter_confirm")
        keys = tr("ask.multi.keys", enter=enter)
        if self.filterable:
            keys += tr("ask.multi.tab_filter")
        counted = f"[$accent]{tr('ask.multi.selected', picked=picked, total=total)}[/]"
        if self._filtered:
            counted += f"[$dim]{tr('ask.multi.hidden')}[/]"
        self.query_one("#multi-hint", Label).update(f"[$dim]{keys}[/]   {counted}")

    # ------------------------------------------------------------------ filter
    def apply_filter(self, needle: str) -> None:
        rows = self._expression_rows(needle)
        if rows is not None:
            # A set is not a filter: it states what you want, so the list stays as
            # it is and the hint says what would be taken. Narrowing here was the
            # first thing I tried and it read as "S01 hides season two".
            self._pending = {row - 1 for row in rows}
            self._render_hint()
            return
        if self._pending is not None:
            self._pending = None
            self._render_hint()
        keep = [
            index
            for index, choice in enumerate(self.ask.choices)
            if matches(needle, choice.label, choice.detail, *choice.tags)
        ]
        if keep == self._visible:
            return
        self._visible = keep
        self._filtered = bool(wanted(needle))
        widget = self._list()
        # No guard around the rebuild. There was a `_syncing` flag here, but the
        # events this raises are posted, not dispatched, so by the time the
        # handler ran the flag was always back to False - and it did not need to
        # be true, because the rebuild leaves the widget in a state that already
        # agrees with `_chosen`, which makes the fold below a no-op.
        widget.clear_options()
        if keep:
            widget.add_options(self._selections(keep))
            widget.highlighted = 0
        self._render_hint()

    def on_selection_list_selected_changed(self, event) -> None:
        """Fold the visible widget's state back into what we remember.

        Only the visible entries are taken from the widget; the hidden ones keep
        whatever they were. That makes `a` and `n` mean "all/none of what I can
        see", which is what they look like they mean.
        """
        event.stop()
        shown = set(self._visible)
        selected = {int(value) for value in self._list().selected}
        self._chosen = (self._chosen - shown) | selected
        self._render_hint()

    # ----------------------------------------------------------------- confirm
    def on__confirming_selection_list_confirmed(self, event: _ConfirmingSelectionList.Confirmed) -> None:
        event.stop()
        self._confirm()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter hands over to the list; it does not confirm.

        Confirming from the filter would mean typing a filter and pressing Enter
        starts the download with whatever happened to be ticked. So the first
        Enter moves to the list, and the second one confirms.

        A set expression is the exception: it already says what it wants, so Enter
        takes it. The list is left showing the ticks rather than confirming them,
        because a mistyped span should be visible before it downloads twenty files.
        """
        event.stop()
        if self._pending is not None:
            self._take_pending()
            return
        self._list().focus()

    def _take_pending(self) -> None:
        """Tick exactly what the expression asked for and hand over to the list."""
        wanted = self._pending or set()
        self._pending = None
        self._chosen = {index for index in wanted if 0 <= index < len(self.ask.choices)}
        found = self.query(FilterBox)
        if found:
            # clear without re-entering apply_filter through the changed event
            with found.first().prevent(Input.Changed):
                found.first().value = ""
        self._visible = list(range(len(self.ask.choices)))
        self._filtered = False
        widget = self._list()
        widget.clear_options()
        widget.add_options(self._selections(self._visible))
        first = min(wanted, default=None)
        widget.highlighted = first if first is not None else 0
        self._render_hint()
        widget.focus()

    def _confirm(self) -> None:
        chosen = sorted(index for index in self._chosen if 0 <= index < len(self.ask.choices))
        if not chosen:
            # Episode lists are multi-select so batches remain possible, but the
            # rest of the browsing path is highlight + Enter. Treat Enter with
            # nothing ticked as the same single-item action instead of answering
            # with [], which silently ended the flow and returned to the service
            # home. Once anything is ticked, Enter keeps its batch-confirm meaning.
            highlighted = self._list().highlighted
            if highlighted is None or not (0 <= highlighted < len(self._visible)):
                return
            original = self._visible[highlighted]
            if not (0 <= original < len(self.ask.choices)):
                return
            if self.ask.choices[original].disabled:
                return
            chosen = [original]
        self.answer([self.ask.choices[index].value for index in chosen])


class AwaitValue(Static):
    """One thing the user has to go and act on.

    Its own row, bold, in a colour nothing else on the screen uses, because this
    is the whole reason the panel is up. Clicking it copies it: a value you can
    read but not lift out of the terminal is only half shown.
    """

    def __init__(self, label: str, value: str, number: int) -> None:
        link = value.startswith(("http://", "https://"))
        classes = ["await-value"]
        if link:
            classes.append("await-link")
        if len(value) > VALUE_FIT:
            classes.append("await-wrap")
        # visual_markup, not visual_text: this is a markup-bearing widget, and the
        # value is a save name, a URL or a whole command line. A file name like
        # "Barbarella.[Uncut]" lost the bracketed part - on the one row the CSS
        # comment calls "the thing that has to be read exactly" - and a value
        # containing "[/]" took the app down with a MarkupError.
        super().__init__(visual_markup(value), classes=" ".join(classes))
        self.field_label = label
        self.field_value = value
        self.number = number
        self.is_link = link

    def on_click(self, event) -> None:
        event.stop()
        # the screen owns the clipboard and the notification; asked for by name so
        # this widget still renders on a host that does not offer copying
        copy = getattr(self.screen, "copy_await_value", None)
        if callable(copy):
            copy(self.number)


class ManualStep:
    """Shared by the asks that show something to go and do elsewhere.

    Both halves of a manual step draw the same rows - a numbered label, the value
    on its own line, prose between them - and both have to let those values be
    lifted off the screen. What differs is only how the step ends: confirmed on
    another device, or pasted back into a field here.
    """

    def manual_rows(self):
        """The label/value rows and the prose, in the order the service gave them."""
        number = 0
        for item in self.ask.lines:
            if isinstance(item, tuple) and len(item) == 2:
                number += 1
                label, value = str(item[0]), str(item[1])
                yield Label(
                    f"[$dim]{number}[/] [$muted]{visual_markup(label)}[/]",
                    classes="await-label",
                )
                yield AwaitValue(label, value, number)
            else:
                yield Static(visual_markup(item), classes="await-line")

    def copy_keys(self) -> list[str]:
        """How to lift each value off, in the words of whoever named it."""
        parts: list[str] = []
        for number, (label, value) in enumerate(self.ask.actions, 1):
            what = (
                tr("ask.link")
                if value.startswith(("http://", "https://"))
                else label.strip().rstrip(":").lower()
            )
            parts.append(tr("ask.copy_what", number=number, what=what))
        return parts

    def value_at(self, number: int) -> tuple[str, str] | None:
        """The ``number``-th ``(label, value)`` pair, 1-based."""
        actions = self.ask.actions
        if 1 <= number <= len(actions):
            return actions[number - 1]
        return None

    def link(self) -> str | None:
        """The first value that is a URL, if any."""
        for _, value in self.ask.actions:
            if value.startswith(("http://", "https://")):
                return value
        qr = getattr(self.ask, "qr", None)
        if qr is not None and qr.open_url:
            return qr.open_url
        return None

    def copyable(self) -> str:
        """What to copy when nothing is addressed by number.

        A panel built out of prose has no pairs to copy, so the whole thing is the
        answer rather than nothing at all.
        """
        actions = self.ask.actions
        if actions:
            return "\n".join(value for _, value in actions)
        return "\n".join(self.ask.as_text())


class TextWidget(ManualStep, AskWidget):
    """One field to type into, and whatever has to be done before there is
    anything to type in it.

    The field is given a visible placeholder and an accent bar down its left
    edge: an empty input with no border is indistinguishable from empty space,
    which is how you end up staring at a prompt wondering where to type.

    When the ask carries manual rows - the page to open and sign in on, whose
    address is then pasted here - they are drawn above the field the same way the
    waiting panel draws them, and the whole thing moves to the middle of the
    screen. A sign-in that starts in a browser used to put its link in the log and
    leave an unlabelled field behind, which is a puzzle, not a prompt.
    """

    #: used when the ask does not supply one of its own
    DEFAULT_PLACEHOLDER = ""

    #: ``ctrl+o`` rather than ``o``: the field has the focus here, so a plain
    #: letter is something the user is typing. Bound on the widget because the
    #: focused Input is inside it, and bindings resolve upwards from the focus.
    BINDINGS = [Binding("ctrl+o", "open_link", "Open link", show=False)]

    def __init__(self, ask: Ask) -> None:
        super().__init__(ask)
        if self.manual:
            # the same card as the waiting panel: this is the same step, and the
            # only difference is that its answer arrives here
            self.add_class("manual-card")

    @property
    def manual(self) -> bool:
        return bool(self.ask.lines)

    def compose(self):
        if self.manual:
            if self.ask.title:
                yield Label(visual_markup(self.ask.title), classes="await-title")
            yield from self.manual_rows()
            # no digit keys here: the field has the focus, so a digit is a digit
            keys = [tr("ask.copy_value")] if self.ask.actions else []
            if self.link() is not None:
                keys.append(tr("ask.open_link"))
            rows = [" · ".join(keys), tr("ask.paste_below")]
            for index, row in enumerate([row for row in rows if row]):
                yield Label(
                    f"[$dim]{row}[/]",
                    classes="await-keys" if index == 0 else "await-keys thin",
                )
        else:
            yield from self._header()
            yield Label(
                f"[$dim]{tr('ask.enter_accept')}[/]",
                classes="ask-hint",
            )
        yield Input(
            value=self.ask.default,
            placeholder=visual_text(self.ask.placeholder or tr("ask.placeholder")),
            password=self.ask.password,
            classes="ask-input",
        )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def action_open_link(self) -> None:
        opener = getattr(self.screen, "open_manual_link", None)
        if callable(opener):
            opener()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.answer(event.value.strip())


class ConfirmWidget(AskWidget):
    BINDINGS = [
        Binding("y", "yes", "Yes", show=False),
        Binding("n", "no", "No", show=False),
    ]

    def compose(self):
        yield from self._header()
        default = "Y/n" if self.ask.default else "y/N"
        yield Label(f"[dim]{tr('ask.confirm_hint', default=default)}[/dim]", classes="ask-hint")
        with Horizontal():
            yield OptionList(
                Option(tr("ask.yes"), id="yes"),
                Option(tr("ask.no"), id="no"),
            )

    def on_mount(self) -> None:
        option_list = self.query_one(OptionList)
        option_list.highlighted = 0 if self.ask.default else 1
        option_list.focus()

    def action_yes(self) -> None:
        self.answer(True)

    def action_no(self) -> None:
        self.answer(False)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.answer(event.option.id == "yes")


class AwaitWidget(ManualStep, AskWidget):
    """What the user has to do somewhere else, and nothing to type here.

    Deliberately not a prompt: there is no field, because the answer arrives from
    another device or another window. What it does have is a bordered card in the
    middle of the screen with each value on its own row - drawn to be read across
    a room, numbered so a keypress copies it, clickable for the same reason - plus
    a way out, because something you have given up on has to be abandonable or the
    flow is stuck until it times out.

    Not device-code specific. Anything a service needs a person to do by hand goes
    here, and the service decides what the rows are called.
    """

    def __init__(self, ask: Ask) -> None:
        super().__init__(ask)
        self.add_class("manual-card")
        if self.ask.qr is not None:
            self.add_class("qr-card")

    def compose(self):
        if self.ask.title:
            yield Label(visual_markup(self.ask.title), classes="await-title")
        if self.ask.qr is not None:
            yield QrWidget(self.ask.qr)
        yield from self.manual_rows()
        # one short line each rather than one long one: these are built from labels
        # a service chose, so any single line can turn out too long for the card
        keys = self.copy_keys()
        if self.link() is not None:
            keys.append(tr("ask.open_plain"))
        rows = [self.ask.hint.strip(), " · ".join(keys), tr("ask.give_up")]
        # only the first of them gets the row of air above it
        for index, row in enumerate([row for row in rows if row]):
            yield Label(
                f"[$dim]{row}[/]",
                classes="await-keys" if index == 0 else "await-keys thin",
            )

    def focus_first(self) -> None:
        # nothing to focus: there is no input, and stealing focus from the screen
        # would take ^b with it
        return


class FormWidget(ManualStep, AskWidget):
    """Several fields in one card, answered together.

    The shape a login is: an account and a password are one thought, and asking
    them as two prompts in the bottom bar meant the first had scrolled off by the
    time the second arrived - with nothing on screen saying which service either
    of them was for. This is the same card a device code gets, for the same
    reason: it is a thing the person has to do, so it goes where they are looking.

    The password is masked and can be shown, because a password typed blind into
    the wrong field is a sign-in failure that looks like a rejected account.
    """

    BINDINGS = [
        # ctrl, because a field has the focus here and a letter is a letter.
        # Not bound by Input, checked.
        Binding("ctrl+r", "reveal", "Show the password", show=False),
    ]

    def __init__(self, ask: Ask) -> None:
        super().__init__(ask)
        self.add_class("manual-card")
        self._revealed = False

    def compose(self):
        if self.ask.title:
            yield Label(visual_markup(self.ask.title), classes="await-title")
        yield from self.manual_rows()
        for spec in self.ask.fields:
            yield Label(
                f"[$muted]{visual_markup(spec.label)}[/]", classes="await-label form-label"
            )
            yield Input(
                value=spec.default,
                placeholder=visual_text(spec.placeholder),
                password=spec.password,
                classes="ask-input form-input",
                id=f"field-{spec.key}",
            )
        keys = [tr("ask.tab_fields"), tr("ask.enter_accepts")]
        if any(spec.password for spec in self.ask.fields):
            keys.insert(0, tr("ask.reveal"))
        rows = [self.ask.hint.strip(), " · ".join(keys), tr("ask.back")]
        for index, row in enumerate([row for row in rows if row]):
            yield Label(
                f"[$dim]{row}[/]",
                classes="await-keys" if index == 0 else "await-keys thin",
            )

    def on_mount(self) -> None:
        self.focus_first()

    def focus_first(self) -> None:
        """The first field with nothing in it, so a known account is not retyped."""
        inputs = list(self.query(Input))
        target = next((box for box in inputs if not box.value), None)
        if target is None:
            target = inputs[0] if inputs else None
        if target is not None:
            target.focus()

    def action_reveal(self) -> None:
        self._revealed = not self._revealed
        for spec, box in zip(self.ask.fields, self.query(Input), strict=False):
            if spec.password:
                box.password = not self._revealed

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter anywhere submits the card.

        Not "enter moves to the next field": every field is visible at once, so
        Tab is the move and Enter is the answer - and a form that could only be
        submitted from its last field is a form that looks stuck.
        """
        event.stop()
        self.answer(self.values())

    def values(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for spec, box in zip(self.ask.fields, self.query(Input), strict=False):
            found[spec.key] = box.value
        return found


class PanelWidget(ManualStep, Vertical):
    """Something the run produced, drawn like a manual step but asking nothing.

    The same card as :class:`AwaitWidget` - numbered rows, click to copy, a keys
    row - and deliberately not an :class:`AskWidget`: there is no answer to post
    and nothing is waiting for one, so it has no ``Answered`` message and no way
    to be resolved. That is the difference between "here is what I did" and "tell
    me what to do".

    ``ask`` holds the :class:`~unidl.core.flow.Panel`, because that is the
    attribute :class:`ManualStep` draws from. The name is the renderer's; it is not
    a claim that this is a question.
    """

    def __init__(self, panel: Panel) -> None:
        super().__init__()
        self.ask = panel
        self.add_class("manual-card")

    def compose(self):
        if self.ask.title:
            yield Label(visual_markup(self.ask.title), classes="await-title")
        yield from self.manual_rows()
        # No "1 copy <label>" row: unlike a manual step, a panel writes its own
        # hint, and the labels here are file names - "1 copy example.show.s01e02"
        # is the row above it read back in lower case.
        keys = []
        if self.link() is not None:
            keys.append(tr("ask.open_plain"))
        # no "give up on this" row: there is nothing to give up on. What Back
        # means here belongs to the screen, which says so through the hint.
        rows = [self.ask.hint.strip(), " · ".join(keys)]
        for index, row in enumerate([row for row in rows if row]):
            yield Label(
                f"[$dim]{row}[/]",
                classes="await-keys" if index == 0 else "await-keys thin",
            )


class TableWidget(AskWidget):
    """Grid selection, used for channel lists and EPG."""

    def compose(self):
        yield from self._header()
        table = DataTable(cursor_type="row", zebra_stripes=True)
        yield table

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for column in self.ask.columns:
            table.add_column(Text(visual_text(column)))
        for index, row in enumerate(self.ask.rows):
            style = self.ask.row_styles[index] if index < len(self.ask.row_styles) else ""
            cells = [Text(visual_text(cell), style=style) for cell in row]
            table.add_row(*cells, key=str(index))
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        index = int(str(event.row_key.value or 0))
        if 0 <= index < len(self.ask.values):
            self.answer(self.ask.values[index])


def widget_for(ask: Ask) -> AskWidget:
    if isinstance(ask, Await):
        return AwaitWidget(ask)
    if isinstance(ask, Form):
        return FormWidget(ask)
    if isinstance(ask, Pick):
        return MultiPickWidget(ask) if ask.multi else PickWidget(ask)
    if isinstance(ask, TextAsk):
        return TextWidget(ask)
    if isinstance(ask, Confirm):
        return ConfirmWidget(ask)
    if isinstance(ask, TableAsk):
        return TableWidget(ask)
    raise TypeError(f"No widget for {type(ask).__name__}")
