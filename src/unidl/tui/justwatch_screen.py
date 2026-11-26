"""Availability lookup: where a title is, and which of our services can get it.

Three screens, each answering one thing, which is the same split the download
flow uses:

1. :class:`JustWatchScreen`   - which title did you mean
2. :class:`AvailabilityScreen` - who has it, in which regions, from which season
3. :class:`RegionScreen`      - which regions to ask about

The last step is the one a browser cannot do: a provider row that corresponds to
one of the 150 installed services opens that service, with the title already
typed into its search. A provider we have nothing for says so instead of
pretending.

Every lookup is one HTTP request per region, so both fetches run on a thread
worker and the screen says which region it is on. Nothing here blocks the loop.
"""

from __future__ import annotations

import json

from rich.cells import set_cell_size
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import OptionList, SelectionList, Static
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from ..core import justwatch as jw
from ..core.i18n import cell_width, tr
from ..core.terminal_cells import crop as crop_cells
from .bidi import escape_markup
from .chrome import Chrome, KeyBar, StatusChip, refresh_locale_widgets
from .filterbox import FILTER_MIN, FilterBox, as_number, matches
from .poster import (
    DEFAULT_POSTER_PREVIEW,
    POSTER_MIN_SCREEN_HEIGHT,
    POSTER_MIN_SCREEN_WIDTH,
    PosterItem,
    PosterPreviewWidget,
    normalize_poster_preview,
    should_show_poster,
)

#: how many search results to ask for
RESULT_COUNT = 12


#: Said on both pickers, because the difference between them is the whole reason
#: there are two. Kept here so the two screens cannot drift into describing it
#: differently.
SEARCH_REGION_NOTE = "one region: whose catalog the title list comes from"
OFFER_REGION_NOTE = "many regions: who carries the title, one request each"


def regions_from(settings) -> list[str]:
    """The regions an availability lookup asks about.

    Many, on purpose: "who has this" is a different question in every country, and
    the answer is worth having for all of them at once.
    """
    configured = jw.parse_regions(settings.get("justwatch_regions", ""))
    return configured or list(jw.DEFAULT_REGIONS)


def search_region_from(settings) -> str:
    """The one region a title search runs in.

    One, on purpose: a search returns one catalogue's titles under that
    catalogue's names, so asking six countries at once would mean six lists of
    near-duplicates to pick a title out of. Which is why this is not the same
    setting as :func:`regions_from` - widening where offers are looked up used to
    silently change which catalogue was searched, because the search took the
    first entry of that list.

    Unset falls back to the first availability region, which is what this used to
    be, so nothing changes for a configuration that never knew there were two.
    """
    chosen = jw.parse_regions(settings.get("justwatch_search_region", ""))
    if chosen:
        return chosen[0]
    return (regions_from(settings) or list(jw.DEFAULT_REGIONS))[0]


class _ConfirmingRegions(SelectionList[str]):
    """SelectionList where space ticks and enter confirms the whole set.

    Textual binds enter to "toggle the highlighted row", and bindings resolve from
    the focused widget upwards - so the screen's own enter binding never fired and
    this picker could not be confirmed at all. Enter appeared to do nothing, or
    worse, silently untick what had just been ticked. ``asks.py`` solves the same
    problem the same way for the episode multi-select.

    The type parameter is bound here rather than at the call site: subscripting a
    non-generic subclass raises at compose time.
    """

    BINDINGS = [Binding("enter", "confirm", "Confirm", show=False)]

    class Confirmed(Message):
        pass

    def action_confirm(self) -> None:
        self.post_message(self.Confirmed())


def _region_entries(needle: str = "") -> list[tuple[str, str]]:
    """``(code, name)`` for every region, by name, narrowed to ``needle``."""
    return [
        (code, name)
        for code, name in sorted(jw.REGIONS.items(), key=lambda kv: kv[1])
        if matches(needle, code, name)
    ]


class SearchRegionScreen(Screen[str]):
    """Pick the one region a title search runs in.

    A single-select, and deliberately not the same screen as
    :class:`RegionScreen`: picking one thing is one click, so there is nothing to
    confirm and no way to end up with none. The multi-select next door needs both.
    """

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("ctrl+r", "reset", "Default region", show=False),
        # the filter has the focus, so list movement is forwarded from here
        Binding("up", "cursor(-1)", "Up", show=False),
        Binding("down", "cursor(1)", "Down", show=False),
        Binding("pageup", "cursor(-10)", "Page up", show=False),
        Binding("pagedown", "cursor(10)", "Page down", show=False),
    ]

    def __init__(self, current: str = ""):
        super().__init__()
        self.current = (current or "").upper()
        #: one code per drawn row, so a filtered list still answers correctly
        self._codes: list[str] = []

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        yield Static(
            f"{tr('justwatch.search_region')}  [$dim]{tr('justwatch.search_note')}[/]",
            id="masthead",
        )
        with Horizontal(id="statusline"):
            yield Static("", id="subhead", classes="pill")
        with Vertical(id="body"):
            yield OptionList(id="region-list")
            yield FilterBox(hint=tr("filter.hint.country"))
        yield KeyBar(
            ("enter", "use this region"), ("↑↓", "move"), ("^r", "default"), ("^b", "back")
        )

    def on_mount(self) -> None:
        # short enough for one row at 100 columns: this is a pill on a one-line
        # status row, so anything longer is simply cut off at the screen edge
        self.query_one("#subhead", Static).update(f"[$dim]{tr('justwatch.search_subhead')}[/]")
        self.rebuild()
        self.query_one(FilterBox).focus()

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self.query_one("#masthead", Static).update(
            f"{tr('justwatch.search_region')}  [$dim]{tr('justwatch.search_note')}[/]"
        )
        self.query_one("#subhead", Static).update(f"[$dim]{tr('justwatch.search_subhead')}[/]")
        box = self.query_one(FilterBox)
        box.placeholder = tr("filter.hint.country")
        self.rebuild(box.value)

    def rebuild(self, needle: str = "") -> None:
        option_list = self.query_one("#region-list", OptionList)
        option_list.clear_options()
        self._codes = []
        entries = _region_entries(needle)
        if not entries:
            option_list.add_option(
                Option(f"  [$dim]{tr('cdm.no_match', query=_safe(needle))}[/]", disabled=True)
            )
            return
        for code, name in entries:
            self._codes.append(code)
            mark = f"  [$ok]{tr('cdm.in_use')}[/]" if code == self.current else ""
            option_list.add_option(Option(f"  [$foreground]{code}[/]  [$dim]{name}[/]{mark}"))
        if self.current in self._codes:
            option_list.highlighted = self._codes.index(self.current)
            # 59 rows: the one in use is usually below the fold, and a highlight
            # you cannot see is not a starting point
            option_list.scroll_to_highlight()
        else:
            option_list.highlighted = 0

    # ------------------------------------------------------------------ events
    def on_input_changed(self, event) -> None:
        event.stop()
        self.rebuild(event.value)

    def on_input_submitted(self, event) -> None:
        event.stop()
        self._take(self.query_one("#region-list", OptionList).highlighted)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._take(event.option_index)

    def _take(self, index: int | None) -> None:
        if index is None or index >= len(self._codes):
            return
        self.dismiss(self._codes[index])

    def action_cursor(self, delta: int) -> None:
        """Move the list while the filter field has the focus."""
        option_list = self.query_one("#region-list", OptionList)
        count = option_list.option_count
        if not count:
            return
        current = option_list.highlighted
        start = current if current is not None else (-1 if delta > 0 else count)
        option_list.highlighted = max(0, min(count - 1, start + delta))

    def action_reset(self) -> None:
        self.dismiss(jw.DEFAULT_REGIONS[0])

    def go_back(self) -> bool:
        self.dismiss("")
        return True


class RegionScreen(Screen[list]):
    """Tick the regions an availability lookup asks about.

    A filterable multi-select, the same shape as the episode picker, because it
    is the same job: a long list where you want a handful. Dismisses with the
    codes in JustWatch's order of usefulness - the order they were ticked is not
    information, so they come back in the order they are listed.

    Not the region a search runs in. That is one country's catalogue and it has
    its own picker; see :class:`SearchRegionScreen`.
    """

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("ctrl+r", "reset", "Default regions", show=False),
    ]

    def __init__(self, chosen: list[str] | None = None):
        super().__init__()
        self._chosen = [code.upper() for code in (chosen or [])]
        #: what was in force on the way in, so leaving without saving can say so
        self._initial = list(self._chosen)

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        yield Static(
            f"{tr('justwatch.offer_region')}  [$dim]{tr('justwatch.offer_note')}[/]",
            id="masthead",
        )
        with Horizontal(id="statusline"):
            yield Static("", id="subhead", classes="pill")
        with Vertical(id="body"):
            yield _ConfirmingRegions(*self._selections(), id="region-list")
            yield FilterBox(hint=tr("filter.hint.country_confirm"))
        yield KeyBar(
            ("space", "tick"), ("enter", "save"), ("^r", "defaults"), ("^b", "discard")
        )

    def _selections(self, needle: str = "") -> list[Selection]:
        return [
            Selection(f"{code}  [$dim]{name}[/]", code, code in self._chosen)
            for code, name in _region_entries(needle)
        ]

    def on_mount(self) -> None:
        self._render_count()
        self.query_one(SelectionList).focus()

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self.query_one("#masthead", Static).update(
            f"{tr('justwatch.offer_region')}  [$dim]{tr('justwatch.offer_note')}[/]"
        )
        self.query_one(FilterBox).placeholder = tr("filter.hint.country_confirm")
        self._render_count()

    def _render_count(self) -> None:
        changed = self._chosen != self._initial
        note = tr("justwatch.offer_changed") if changed else tr("justwatch.offer_same")
        self.query_one("#subhead", Static).update(
            f"[$dim]{tr('justwatch.regions_count', chosen=len(self._chosen), total=len(jw.REGIONS), note=note)}[/]"
        )

    # ------------------------------------------------------------------ events
    # two underscores after "on": the handler name is derived from the class name,
    # and this class's name starts with one. Same as asks.py's own picker.
    def on__confirming_regions_confirmed(self, event: _ConfirmingRegions.Confirmed) -> None:
        event.stop()
        self.action_confirm()

    def on_selection_list_selected_changed(self, event) -> None:
        event.stop()
        # Only what is on screen is read back; a code the filter is hiding keeps
        # whatever it was, the same rule the episode picker follows.
        shown = {str(option.value) for option in event.selection_list._options}
        picked = {str(value) for value in event.selection_list.selected}
        self._chosen = [
            code
            for code in jw.REGIONS
            if (code in picked) or (code in self._chosen and code not in shown)
        ]
        self._render_count()

    def on_input_changed(self, event) -> None:
        event.stop()
        widget = self.query_one(SelectionList)
        widget.clear_options()
        rows = self._selections(event.value)
        if rows:
            widget.add_options(rows)
            widget.highlighted = 0

    def on_input_submitted(self, event) -> None:
        event.stop()
        self.action_confirm()

    def action_reset(self) -> None:
        self._chosen = list(jw.DEFAULT_REGIONS)
        widget = self.query_one(SelectionList)
        widget.clear_options()
        widget.add_options(self._selections(self.query_one(FilterBox).value))
        self._render_count()

    def action_confirm(self) -> None:
        # An empty set is not a choice, it is a lookup with nowhere to look. It
        # used to dismiss with nothing, which the caller reads as "unchanged" - so
        # unticking everything and pressing enter did nothing, in silence.
        if not self._chosen:
            self.notify(tr("justwatch.need_region"), severity="warning", timeout=4)
            return
        self.dismiss(list(self._chosen))

    def go_back(self) -> bool:
        """Leave without saving - and say so, if there was something to save.

        Back discards here, as it does everywhere else. What it must not do is
        discard in silence: ticking a region is a deliberate act, and coming back
        to the old list with no explanation looks like the app ignored it.
        """
        if self._chosen != self._initial:
            self.notify(tr("justwatch.unchanged"), severity="warning", timeout=6)
        self.dismiss([])
        return True


def _poster_mode_for(app, override: str | None = None) -> str:
    if override is not None:
        return normalize_poster_preview(override)
    return normalize_poster_preview(
        app.globals.get("poster_preview", DEFAULT_POSTER_PREVIEW)
    )


def _poster_chip_text(mode: str, visible: bool, renderer: str) -> str:
    label = tr(f"poster.mode.{mode}", default=mode)
    if not visible:
        return tr("poster.chip_hidden", mode=label, default=f"posters: {label} (hidden)")
    if renderer == "protocol":
        return tr("poster.chip_on", mode=label, default=f"posters: {label}")
    return tr("poster.chip_fallback", mode=label, default=f"posters: {label} (cells)")


def _cycle_poster_override(app, current_override: str | None, width: int, height: int) -> tuple[str | None, str]:
    """Toggle poster visibility in-screen without breaking macOS Terminal.app defaults."""
    if width < POSTER_MIN_SCREEN_WIDTH:
        return current_override, tr(
            "poster.notify_narrow",
            default=f"Terminal is too narrow for the poster rail (need >= {POSTER_MIN_SCREEN_WIDTH} cols).",
        )
    if height < POSTER_MIN_SCREEN_HEIGHT:
        return current_override, tr(
            "poster.notify_short",
            default=f"Terminal is too short for the poster rail (need >= {POSTER_MIN_SCREEN_HEIGHT} rows).",
        )
    mode = _poster_mode_for(app, current_override)
    visible, _renderer, reason = should_show_poster(mode, width=width, height=height)
    if visible:
        return "off", ""
    if reason == "dumb":
        return current_override, tr("poster.notify_unsupported", default="Posters are unavailable in a dumb terminal.")
    # Currently hidden due to off or auto-no-protocol -> enable (auto if native works, else always fallback).
    auto_ok, _auto_r, _auto_reason = should_show_poster("auto", width=width, height=height)
    if auto_ok:
        return "auto", ""
    return "always", tr(
        "poster.notify_fallback",
        default="Terminal has no native image protocol; enabled text-cell poster preview (press p to hide).",
    )


def _title_poster_item(title: jw.Title, *, footer: str = "") -> PosterItem:
    meta: list[tuple[str, str]] = []
    if title.original_name and title.original_name != title.name:
        meta.append((tr("poster.meta.original", default="Original"), title.original_name))
    if title.genres:
        meta.append((tr("poster.meta.genres", default="Genres"), ", ".join(title.genres[:4])))
    if title.imdb_id:
        meta.append(("IMDb", title.imdb_id))
    subtitle_bits = [part for part in (title.year, title.kind_label) if part]
    return PosterItem(
        title=title.name,
        subtitle=" · ".join(subtitle_bits),
        poster_url=title.poster_url,
        meta_lines=tuple(meta),
        synopsis=title.synopsis,
        footer_hint=footer,
    )


class JustWatchScreen(Screen):
    """Search results from JustWatch, for one term."""

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("ctrl+f", "app.global_search", "Search", show=False),
        Binding("ctrl+s", "app.global_settings", "Settings", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("ctrl+r", "regions", "Regions", show=False),
        Binding("p", "toggle_poster", "Toggle poster", show=False),
        Binding("t", "schedule", "Schedule", show=False),
        Binding("up", "cursor(-1)", "Up", show=False),
        Binding("down", "cursor(1)", "Down", show=False),
        Binding("pageup", "cursor(-10)", "Page up", show=False),
        Binding("pagedown", "cursor(10)", "Page down", show=False),
    ]

    def __init__(self, query: str):
        super().__init__()
        self._query = query
        self._titles: list[jw.Title] = []
        self._error = ""
        self._generation = 0
        self._last_search_region = ""
        self._last_regions: tuple[str, ...] = ()
        self._poster_override: str | None = None

    def compose(self) -> ComposeResult:
        yield Chrome()
        yield Static(f"{tr('justwatch.availability_head')}  [$dim]{_safe(self._query)}[/]", id="masthead")
        with Horizontal(id="statusline"):
            yield StatusChip("regions", id="region-chip")
            yield Static("·", classes="chip-sep")
            yield StatusChip("toggle_poster", id="poster-chip")
            yield Static("·", classes="chip-sep")
            yield Static("", id="subhead", classes="pill chip-static")
        with Vertical(id="body"):
            with Horizontal(id="jw-split", classes="poster-split"):
                yield OptionList(id="result-list")
                yield PosterPreviewWidget(id="poster-rail")
            yield FilterBox(hint=tr("filter.hint.results"))
        yield KeyBar(
            ("enter", "open"),
            ("t", "schedule.action.schedule"),
            ("p", "poster.key_toggle"),
            ("^r", "regions"),
            ("^b", "back"),
            ("esc", "quit"),
        )

    def on_mount(self) -> None:
        self._last_search_region = self.search_region
        self._last_regions = tuple(self.regions)
        self._render_chips()
        self._sync_poster_layout()
        self.query_one("#result-list", OptionList).add_option(
            Option(f"  [$dim]{tr('justwatch.searching')}[/]", disabled=True)
        )
        self.query_one(FilterBox).display = False
        self.search()

    def on_resize(self) -> None:
        self._render_chips()
        self._sync_poster_layout()

    def on_screen_suspend(self, _event) -> None:
        found = self.query("#poster-rail")
        if found:
            found.first(PosterPreviewWidget).suspend_graphics()

    def on_screen_resume(self, _event) -> None:
        current_search = self.search_region
        current_regions = tuple(self.regions)
        self._sync_poster_layout()
        found = self.query("#poster-rail")
        if found:
            found.first(PosterPreviewWidget).resume_graphics()
        if current_search != self._last_search_region:
            self._last_search_region = current_search
            self._last_regions = current_regions
            self._clear_filter_and_poster()
            self._render_chips()
            option_list = self.query_one("#result-list", OptionList)
            option_list.clear_options()
            option_list.add_option(
                Option(f"  [$dim]{tr('justwatch.searching')}[/]", disabled=True)
            )
            self.search()
        elif current_regions != self._last_regions:
            self._last_regions = current_regions
            self._render_chips()
            if self._titles or self._error:
                filter_box = self.query(FilterBox)
                needle = filter_box.first(FilterBox).value if filter_box else ""
                self._render_rows(needle)

    # ------------------------------------------------------------------ status
    @property
    def regions(self) -> list[str]:
        """Where offers will be looked up, once a title is picked. Not this search."""
        return regions_from(self.app.globals)

    @property
    def search_region(self) -> str:
        return search_region_from(self.app.globals)

    def _render_chips(self) -> None:
        # The control on this screen changes the *search* region, because that is
        # what this screen does. The other list is named too, so it is clear which
        # of the two ^r is about, and that the other one exists.
        region = self.search_region
        self.query_one("#region-chip", StatusChip).update(
            tr("justwatch.chip_region", region=jw.region_label(region))
        )
        self.query_one("#subhead", Static).update(
            f"[$dim]{tr('justwatch.searching_catalogue', region=jw.REGIONS.get(region, region), count=len(self.regions))}[/]"
        )

    def on_unmount(self) -> None:
        self._generation += 1
        self.workers.cancel_group(self, "justwatch-search")

    def _clear_filter_and_poster(self) -> None:
        filter_box = self.query(FilterBox)
        if filter_box:
            fb = filter_box.first(FilterBox)
            with fb.prevent(FilterBox.Changed):
                fb.value = ""
            fb.display = False
        self._update_poster_for_index(None)

    def relocalize(self) -> None:
        from .chrome import refresh_locale_widgets

        refresh_locale_widgets(self)
        self.query_one("#masthead", Static).update(
            f"{tr('justwatch.availability_head')}  [$dim]{_safe(self._query)}[/]"
        )
        filter_box = self.query(FilterBox)
        if filter_box:
            filter_box.first(FilterBox).placeholder = tr("filter.hint.results")
        self._render_chips()
        self._sync_poster_layout()
        if self._titles or self._error:
            needle = filter_box.first(FilterBox).value if filter_box else ""
            self._render_rows(needle)

    def action_cursor(self, delta: int) -> None:
        option_list = self.query_one("#result-list", OptionList)
        count = option_list.option_count
        if not count:
            return
        enabled = [i for i in range(count) if not option_list.get_option_at_index(i).disabled]
        if not enabled:
            return
        current = option_list.highlighted
        if current is None:
            option_list.highlighted = enabled[0] if delta > 0 else enabled[-1]
            return
        target = max(0, min(count - 1, current + delta))
        option_list.highlighted = min(enabled, key=lambda i: (abs(i - target), i))

    # ------------------------------------------------------------------ search
    @work(thread=True, exclusive=True, group="justwatch-search")
    def search(self) -> None:
        self._generation += 1
        generation = self._generation
        client = jw.JustWatch()
        region = self.search_region
        try:
            found = client.search(self._query, region=region, count=RESULT_COUNT)
        except jw.JustWatchError as exc:
            try:
                self.app.call_from_thread(self._failed, str(exc), generation)
            except RuntimeError:
                pass
            return
        try:
            self.app.call_from_thread(self._show, found, generation)
        except RuntimeError:
            pass

    def _failed(self, message: str, generation: int | None = None) -> None:
        if not self.is_attached or (generation is not None and generation != self._generation):
            return
        self._error = message
        self._titles = []
        self._render_rows()

    def _show(self, titles: list[jw.Title], generation: int | None = None) -> None:
        if not self.is_attached or (generation is not None and generation != self._generation):
            return
        self._error = ""
        self._titles = titles
        self._render_rows()

    def _render_rows(self, needle: str = "") -> None:
        option_list = self.query_one("#result-list", OptionList)
        option_list.clear_options()

        if self._error:
            option_list.add_option(Option(f"  [$bad]{_safe(self._error)}[/]", disabled=True))
            self.query_one(FilterBox).display = False
            self._update_poster_for_index(None)
            self.query_one("#subhead", Static).update(
                f"[$bad]{tr('justwatch.fetch_failed')}[/]"
            )
            return
        if not self._titles:
            option_list.add_option(
                Option(f"  [$dim]{tr('justwatch.nothing', query=_safe(self._query))}[/]", disabled=True)
            )
            self.query_one(FilterBox).display = False
            self._update_poster_for_index(None)
            self.query_one("#subhead", Static).update(
                f"[$dim]{tr('justwatch.results', count=0, region=self.search_region, count2=len(self.regions))}[/]"
            )
            return

        self._visible = [
            index
            for index, title in enumerate(self._titles)
            if matches(needle, title.name, title.original_name, title.year, *title.genres)
        ]
        filter_box = self.query_one(FilterBox)
        filter_box.display = len(self._titles) >= FILTER_MIN
        if not self._visible:
            option_list.add_option(Option(f"  [$dim]{tr('justwatch.no_match')}[/]", disabled=True))
            self._update_poster_for_index(None)
            return
        for position, index in enumerate(self._visible, 1):
            option_list.add_option(Option(self._row(position, self._titles[index])))
        option_list.highlighted = 0
        self._update_poster_for_index(0)
        # Both regions again, because this is the moment they differ: these titles
        # came out of one catalogue, and picking one asks a different, longer list
        # of regions who carries it.
        self.query_one("#subhead", Static).update(
            f"[$dim]{tr('justwatch.results', count=len(self._titles), region=self.search_region, count2=len(self.regions))}[/]"
        )

    def _highlighted_title(self, index: int | None = None) -> jw.Title | None:
        if not self._titles:
            return None
        visible = getattr(self, "_visible", list(range(len(self._titles))))
        idx = index if index is not None else self.query_one("#result-list", OptionList).highlighted
        if idx is None or not (0 <= idx < len(visible)):
            return None
        return self._titles[visible[idx]]

    def _sync_poster_layout(self) -> None:
        found = self.query("#poster-rail")
        if not found:
            return
        rail = found.first(PosterPreviewWidget)
        width = int(self.size.width or self.app.size.width or 80)
        height = int(self.size.height or self.app.size.height or 24)
        mode = _poster_mode_for(self.app, self._poster_override)
        visible, renderer, _reason = should_show_poster(mode, width=width, height=height)
        chip = self.query("#poster-chip")
        if chip:
            chip.first(StatusChip).update(_poster_chip_text(mode, visible, renderer))
        if not visible:
            focused = self.focused
            if focused is rail or (focused is not None and rail in focused.ancestors):
                self.query_one("#result-list", OptionList).focus()
            rail.clear_graphics()
            rail.display = False
            return
        rail.display = True
        self._update_poster_for_index(self.query_one("#result-list", OptionList).highlighted, renderer=renderer)

    def _update_poster_for_index(self, index: int | None, *, renderer: str | None = None) -> None:
        found = self.query("#poster-rail")
        if not found:
            return
        rail = found.first(PosterPreviewWidget)
        if not rail.display:
            return
        if renderer is None:
            width = int(self.size.width or self.app.size.width or 80)
            height = int(self.size.height or self.app.size.height or 24)
            mode = _poster_mode_for(self.app, self._poster_override)
            visible, renderer, _reason = should_show_poster(mode, width=width, height=height)
            if not visible:
                rail.clear_graphics()
                rail.display = False
                return
        title = self._highlighted_title(index)
        proxy = self.app.config.proxy_url(self.app.globals.get("proxy", "")) if hasattr(self.app.config, "proxy_url") else None
        if title is None:
            rail.set_item(None, renderer=renderer, proxy=proxy)
            return
        footer = tr("poster.hint.justwatch", default="Enter: availability · t: schedule")
        rail.set_item(_title_poster_item(title, footer=footer), renderer=renderer, proxy=proxy)

    def action_toggle_poster(self) -> None:
        width = int(self.size.width or self.app.size.width or 80)
        height = int(self.size.height or self.app.size.height or 24)
        nxt, notice = _cycle_poster_override(self.app, self._poster_override, width, height)
        self._poster_override = nxt
        self._sync_poster_layout()
        if notice:
            self.notify(notice, timeout=4)

    def action_schedule(self) -> None:
        title = self._highlighted_title()
        if title is None:
            return
        svc_index = jw.service_index(getattr(self.app, "services", ()) or ())
        platform_urls: dict[str, str] = {}
        first_service_id = ""
        first_direct_url = ""
        for tech, clear, url in getattr(title, "offer_links", ()) or ():
            if not url:
                continue
            if not first_direct_url:
                first_direct_url = url
            sid = jw.resolve_service(tech, clear, svc_index) if svc_index else None
            if sid:
                platform_urls.setdefault(sid, url)
                if not first_service_id:
                    first_service_id = sid
                    first_direct_url = url
        source_meta: dict[str, str] = {
            "source": "justwatch",
            "justwatch_id": title.node_id,
            "search_target": title.name,
            "synopsis": title.synopsis[:240],
        }
        if first_direct_url:
            source_meta["direct_url"] = first_direct_url
        if platform_urls:
            source_meta["platform_urls_json"] = json.dumps(platform_urls, ensure_ascii=False)
        self.app.open_schedule_editor(
            prefill={
                "name": title.name,
                "service_id": first_service_id,
                "input_mode": "search",
                "target": title.name,
                "direct_url": first_direct_url,
                "search_target": title.name,
                "platform_urls": platform_urls,
                "media_kind": "show" if title.is_show else "movie",
                "match_year": title.year,
                "selection_mode": "episodes" if title.is_show else "movie",
                "poster_url": title.poster_url,
                "source_meta": source_meta,
            }
        )

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        self._update_poster_for_index(event.option_index)

    @staticmethod
    def _row(position: int, title: jw.Title) -> str:
        """Number, kind, name, then what it is - one entry, two lines.

        Built with markup escaped where the data goes: a synopsis with a square
        bracket in it would otherwise be read as a style tag.
        """
        head = f"  [$gutter]{position:>3}[/]  [$fg2]{_safe(title.name)}[/]"
        if title.original_name and title.original_name != title.name:
            head += f" [$dim]({_safe(title.original_name)})[/]"
        if title.year:
            head += f"  [$gutter]{title.year}[/]"
        head += f"  [$dim]{title.kind_label}[/]"
        bits = []
        if title.genres:
            bits.append(", ".join(_safe(g) for g in title.genres[:3]))
        if title.synopsis:
            summary = (
                f"{crop_cells(title.synopsis, 96)}..."
                if cell_width(title.synopsis) > 96
                else title.synopsis
            )
            bits.append(_safe(summary))
        detail = f"\n       [$dim]{'  ·  '.join(bits)}[/]" if bits else ""
        return head + detail

    # ------------------------------------------------------------------ events
    def on_input_changed(self, event) -> None:
        event.stop()
        number = as_number(event.value)
        if number is not None:
            self._render_rows()
            option_list = self.query_one("#result-list", OptionList)
            if 1 <= number <= option_list.option_count:
                option_list.highlighted = number - 1
            return
        self._render_rows(event.value)

    def on_input_submitted(self, event) -> None:
        event.stop()
        option_list = self.query_one("#result-list", OptionList)
        self._take(option_list.highlighted)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._take(event.option_index)

    def _take(self, index: int | None) -> None:
        if index is None or not self._titles:
            return
        visible = getattr(self, "_visible", list(range(len(self._titles))))
        if index >= len(visible):
            return
        self.app.push_screen(AvailabilityScreen(self._titles[visible[index]]))

    def action_regions(self) -> None:
        """Change the one region this search runs in, and run it again."""

        def _chosen(code: str | None) -> None:
            if not code or code == self.search_region:
                return
            self.app.globals.set("justwatch_search_region", code)
            self._last_search_region = self.search_region
            self._last_regions = tuple(self.regions)
            self._clear_filter_and_poster()
            self._render_chips()
            self.query_one("#result-list", OptionList).clear_options()
            self.query_one("#result-list", OptionList).add_option(
                Option(f"  [$dim]{tr('justwatch.searching')}[/]", disabled=True)
            )
            self.search()

        self.app.push_screen(SearchRegionScreen(self.search_region), _chosen)

    def go_back(self) -> bool:
        return False


class AvailabilityScreen(Screen):
    """Who carries one title, region by region."""

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("ctrl+f", "app.global_search", "Search", show=False),
        Binding("ctrl+s", "app.global_settings", "Settings", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("ctrl+r", "regions", "Regions", show=False),
        Binding("ctrl+y", "copy_link", "Copy link", show=False),
        Binding("p", "toggle_poster", "Toggle poster", show=False),
        Binding("t", "schedule", "Schedule", show=False),
    ]

    def __init__(self, title: jw.Title):
        super().__init__()
        self.title_ref = title
        self._found: list[jw.Availability] = []
        #: one entry per rendered row: an offer and the service that can get it
        self._rows: list[tuple[jw.Offer, str | None] | None] = []
        self._error = ""
        self._generation = 0
        self._last_regions: tuple[str, ...] = ()
        self._poster_override: str | None = None

    def compose(self) -> ComposeResult:
        yield Chrome()
        yield Static("", id="masthead")
        with Horizontal(id="statusline"):
            yield StatusChip("regions", id="region-chip")
            yield Static("·", classes="chip-sep")
            yield StatusChip("toggle_poster", id="poster-chip")
            yield Static("·", classes="chip-sep")
            yield Static("", id="subhead", classes="pill chip-static")
        with Vertical(id="body"):
            with Horizontal(id="avail-split", classes="poster-split"):
                yield OptionList(id="offer-list")
                yield PosterPreviewWidget(id="poster-rail")
        yield KeyBar(
            ("enter", "open in a service"),
            ("t", "schedule.action.schedule"),
            ("p", "poster.key_toggle"),
            ("^y", "copy link"),
            ("^r", "regions"),
            ("^b", "back"),
        )

    def on_mount(self) -> None:
        title = self.title_ref
        head = f"{_safe(title.name)}"
        if title.year:
            head += f"  [$gutter]{title.year}[/]"
        self.query_one("#masthead", Static).update(f"{head}  [$dim]{title.kind_label}[/]")
        self._last_regions = tuple(self.regions)
        self._render_chips()
        self._sync_poster_layout()
        self.query_one("#offer-list", OptionList).add_option(
            Option(f"  [$dim]{tr('justwatch.asking')}[/]", disabled=True)
        )
        self.fetch()

    def on_resize(self) -> None:
        self._render_chips()
        self._sync_poster_layout()
        if self._found or self._error:
            self._render_rows()

    def on_screen_suspend(self, _event) -> None:
        found = self.query("#poster-rail")
        if found:
            found.first(PosterPreviewWidget).suspend_graphics()

    def on_screen_resume(self, _event) -> None:
        current_regions = tuple(self.regions)
        self._sync_poster_layout()
        found = self.query("#poster-rail")
        if found:
            found.first(PosterPreviewWidget).resume_graphics()
        if current_regions != self._last_regions:
            self._last_regions = current_regions
            self._render_chips()
            option_list = self.query_one("#offer-list", OptionList)
            option_list.clear_options()
            option_list.add_option(
                Option(f"  [$dim]{tr('justwatch.asking')}[/]", disabled=True)
            )
            self.fetch()

    @property
    def regions(self) -> list[str]:
        return regions_from(self.app.globals)

    def _render_chips(self) -> None:
        # "availability regions", not "regions": the search that found this title
        # ran in one region of its own, and two controls both called ^r regions
        # would look like the same setting seen twice.
        regions = self.regions
        width = int(getattr(self.size, "width", 0) or getattr(self.app.size, "width", 0) or 100)
        limit = 2 if width < 96 else 6
        shown = ", ".join(regions[:limit]) + ("..." if len(regions) > limit else "")
        self.query_one("#region-chip", StatusChip).update(
            tr("justwatch.chip_availability", count=len(regions), shown=shown)
        )

    def on_unmount(self) -> None:
        self._generation += 1
        self.workers.cancel_group(self, "justwatch-offers")

    def relocalize(self) -> None:
        from .chrome import refresh_locale_widgets

        refresh_locale_widgets(self)
        self._render_chips()
        self._sync_poster_layout()
        if self._found or self._error:
            self._render_rows()

    def _status(self, text: str, generation: int | None = None) -> None:
        if not self.is_attached or (generation is not None and generation != self._generation):
            return
        self.query_one("#subhead", Static).update(f"[$dim]{text}[/]")

    # ------------------------------------------------------------------- fetch
    @work(thread=True, exclusive=True, group="justwatch-offers")
    def fetch(self) -> None:
        self._generation += 1
        generation = self._generation
        client = jw.JustWatch()
        regions = self.regions

        def report_region(code: str) -> None:
            if generation != self._generation:
                return
            try:
                self.app.call_from_thread(
                    self._status,
                    tr("justwatch.looking_up", region=jw.region_label(code)),
                    generation,
                )
            except RuntimeError:
                pass

        try:
            found = client.availability(
                self.title_ref,
                regions,
                on_region=report_region,
            )
        except jw.JustWatchError as exc:
            try:
                self.app.call_from_thread(self._failed, str(exc), generation)
            except RuntimeError:
                pass
            return
        try:
            self.app.call_from_thread(self._show, found, generation)
        except RuntimeError:
            pass

    def _failed(self, message: str, generation: int | None = None) -> None:
        if not self.is_attached or (generation is not None and generation != self._generation):
            return
        self._error = message
        self._render_rows()

    def _show(self, found: list[jw.Availability], generation: int | None = None) -> None:
        if not self.is_attached or (generation is not None and generation != self._generation):
            return
        self._error = ""
        self._found = found
        self._render_rows()

    # ------------------------------------------------------------------ render
    def _render_rows(self) -> None:
        option_list = self.query_one("#offer-list", OptionList)
        option_list.clear_options()
        self._rows = []

        if self._error:
            option_list.add_option(Option(f"  [$bad]{_safe(self._error)}[/]", disabled=True))
            self._status(tr("justwatch.fetch_failed"))
            return

        index = jw.service_index(self.app.services)
        carried = 0
        openable = 0

        for availability in self._found:
            self._add_header(option_list, availability)
            if availability.error or not availability.offers:
                continue
            carried += 1
            for monetization, offers in availability.by_kind():
                self._rows.append(None)
                option_list.add_option(
                    Option(
                        f"     [${jw.MONETIZATION_ROLE.get(monetization, 'muted')}]"
                        f"{_safe(jw.MONETIZATION.get(monetization, monetization))}[/]",
                        disabled=True,
                    )
                )
                for offer in offers:
                    service_id = (
                        None if offer.physical
                        else jw.resolve_service(offer.technical_name, offer.provider, index)
                    )
                    if service_id:
                        openable += 1
                    self._rows.append((offer, service_id))
                    option_list.add_option(Option(self._offer_row_for_width(offer, service_id, option_list)))

        if not self._found:
            option_list.add_option(Option(f"  [$dim]{tr('justwatch.nothing_back')}[/]", disabled=True))
        elif not carried:
            option_list.add_option(
                Option(f"  [$dim]{tr('justwatch.not_in_regions')}[/]", disabled=True)
            )
        self._focus_first(option_list)
        link = self.title_ref.web_url
        extra = tr("justwatch.copies", link=_safe(link)) if link else ""
        self._status(
            tr(
                "justwatch.status_summary",
                carried=carried,
                total=len(self._found),
                openable=openable,
            )
            + extra
        )

    def _add_header(self, option_list: OptionList, availability: jw.Availability) -> None:
        if self._rows:
            # a blank row between regions; without it sixteen of these run
            # together into one wall
            self._rows.append(None)
            option_list.add_option(Option("", disabled=True))
        self._rows.append(None)
        if availability.error:
            body = f"[$warn]{_safe(availability.error)}[/]"
        elif not availability.offers:
            body = f"[$gutter]{tr('justwatch.nothing_here')}[/]"
        else:
            body = f"[$dim]{_safe(availability.summary())}[/]"
        option_list.add_option(
            Option(f"  [$accent]{_safe(availability.label)}[/]  {body}", disabled=True)
        )

    #: plain-text column widths. Padding has to be applied before the markup
    #: goes on: `f"{markup:<34}"` counts the tag characters, which is how a long
    #: price ended up welded to the service name next to it.
    PROVIDER_WIDTH = 30
    DETAIL_WIDTH = 32

    def _offer_row_for_width(
        self, offer: jw.Offer, service_id: str | None, option_list: OptionList
    ) -> str:
        avail = int(option_list.content_region.width or 0)
        if avail <= 0:
            total = int(self.size.width or self.app.size.width or 80)
            rail = self.query("#poster-rail")
            rail_w = 36 if (rail and rail.first(PosterPreviewWidget).display) else 0
            avail = max(48, total - rail_w - 6)
        # Indent (8) + gaps (4) + target column (~14) = 26 cells fixed overhead.
        budget = max(24, avail - 26)
        prov_w = min(self.PROVIDER_WIDTH, max(14, int(budget * 0.48)))
        det_w = min(self.DETAIL_WIDTH, max(10, budget - prov_w))
        return self._offer_row(offer, service_id, provider_width=prov_w, detail_width=det_w)

    @classmethod
    def _offer_row(
        cls,
        offer: jw.Offer,
        service_id: str | None,
        *,
        provider_width: int | None = None,
        detail_width: int | None = None,
    ) -> str:
        bits = [offer.quality] if offer.quality else []
        seasons = offer.season_summary()
        if seasons:
            bits.append(seasons)
        if offer.price:
            # already carries its own currency symbol - "$3.99", "£3.49" - so
            # prefixing one turned every price into "$$3.99"
            bits.append(offer.price)
        provider = _fit(offer.provider, provider_width or cls.PROVIDER_WIDTH)
        detail = _fit("  ·  ".join(bits), detail_width or cls.DETAIL_WIDTH)
        if service_id:
            target = f"[$ok]{_safe(service_id)}[/]"
        elif offer.physical:
            target = f"[$gutter]{tr('justwatch.disc')}[/]"
        else:
            target = f"[$gutter]{tr('justwatch.no_service')}[/]"
        return f"        {_safe(provider)}  [$dim]{_safe(detail)}[/]  {target}"

    def _focus_first(self, option_list: OptionList) -> None:
        for position, row in enumerate(self._rows):
            if row is not None and row[1]:
                option_list.highlighted = position
                return
        for position in range(option_list.option_count):
            if not option_list.get_option_at_index(position).disabled:
                option_list.highlighted = position
                return

    # ------------------------------------------------------------------ events
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._take(event.option_index)

    def _take(self, index: int | None) -> None:
        if index is None or index >= len(self._rows):
            return
        row = self._rows[index]
        if row is None:
            return
        offer, service_id = row
        if not service_id:
            message = (
                tr("justwatch.disc_sale", provider=offer.provider)
                if offer.physical
                else tr("justwatch.no_handler", provider=offer.provider)
            )
            self.notify(message, severity="warning", timeout=5)
            return
        service = next((s for s in self.app.services if s.ID == service_id), None)
        if service is None:
            self.notify(tr("justwatch.not_installed", service=service_id), severity="error", timeout=5)
            return

        # Out of the lookup and into the service, with the title already typed:
        # the availability screens have done their job at this point, and coming
        # back to them from a download would be a stack nobody asked for.
        while len(self.app.screen_stack) > 1 and not _is_home(self.app.screen):
            self.app.pop_screen()
        if service.SUPPORTS_SEARCH:
            self.app.open_service(service, search=self.title_ref.name)
        else:
            self.app.notify(
                tr("justwatch.no_search", name=service.NAME), timeout=6
            )
            self.app.open_service(service)

    def action_copy_link(self) -> None:
        row = self._rows[self.query_one("#offer-list", OptionList).highlighted or 0] if self._rows else None
        url = (row[0].url if row else "") or self.title_ref.web_url
        if not url:
            self.notify(tr("justwatch.no_link"), severity="warning", timeout=3)
            return
        if self.app.copy_text(url) is False:
            return
        self.notify(tr("justwatch.copied", url=url), timeout=4)

    def action_regions(self) -> None:
        def _chosen(codes: list | None) -> None:
            if codes:
                self.app.globals.set("justwatch_regions", ",".join(codes))
                self._last_regions = tuple(self.regions)
                self._render_chips()
                self.query_one("#offer-list", OptionList).clear_options()
                self.query_one("#offer-list", OptionList).add_option(
                    Option(f"  [$dim]{tr('justwatch.asking')}[/]", disabled=True)
                )
                self.fetch()

        self.app.push_screen(RegionScreen(self.regions), _chosen)

    def _sync_poster_layout(self) -> None:
        found = self.query("#poster-rail")
        if not found:
            return
        rail = found.first(PosterPreviewWidget)
        width = int(self.size.width or self.app.size.width or 80)
        height = int(self.size.height or self.app.size.height or 24)
        mode = _poster_mode_for(self.app, self._poster_override)
        visible, renderer, _reason = should_show_poster(mode, width=width, height=height)
        chip = self.query("#poster-chip")
        if chip:
            chip.first(StatusChip).update(_poster_chip_text(mode, visible, renderer))
        if not visible:
            focused = self.focused
            if focused is rail or (focused is not None and rail in focused.ancestors):
                self.query_one("#offer-list", OptionList).focus()
            rail.clear_graphics()
            rail.display = False
            return
        rail.display = True
        proxy = self.app.config.proxy_url(self.app.globals.get("proxy", "")) if hasattr(self.app.config, "proxy_url") else None
        footer = tr("poster.hint.availability", default="Enter: open service · t: schedule")
        rail.set_item(_title_poster_item(self.title_ref, footer=footer), renderer=renderer, proxy=proxy)

    def action_toggle_poster(self) -> None:
        width = int(self.size.width or self.app.size.width or 80)
        height = int(self.size.height or self.app.size.height or 24)
        nxt, notice = _cycle_poster_override(self.app, self._poster_override, width, height)
        self._poster_override = nxt
        self._sync_poster_layout()
        if notice:
            self.notify(notice, timeout=4)

    def action_schedule(self) -> None:
        idx = self.query_one("#offer-list", OptionList).highlighted
        row = self._rows[idx] if (idx is not None and 0 <= idx < len(self._rows)) else None
        service_id = row[1] if row is not None else ""
        offer = row[0] if row is not None else None
        platform_urls: dict[str, str] = {}
        for item in self._rows:
            if item is None:
                continue
            row_offer, row_sid = item[0], item[1]
            if row_sid and row_offer.url and row_sid not in platform_urls:
                platform_urls[row_sid] = row_offer.url
        direct_url = offer.url if (offer and offer.url) else platform_urls.get(service_id or "", "")
        source_meta: dict[str, str] = {
            "source": "justwatch",
            "justwatch_id": self.title_ref.node_id,
            "platform_label": offer.provider if offer else "",
            "search_target": self.title_ref.name,
            "synopsis": self.title_ref.synopsis[:240],
        }
        if direct_url:
            source_meta["direct_url"] = direct_url
        if platform_urls:
            source_meta["platform_urls_json"] = json.dumps(platform_urls, ensure_ascii=False)
        prefill: dict[str, object] = {
            "name": self.title_ref.name,
            "service_id": service_id or "",
            "input_mode": "url" if (direct_url and service_id) else "search",
            "target": (direct_url if (direct_url and service_id) else self.title_ref.name),
            "direct_url": direct_url,
            "search_target": self.title_ref.name,
            "platform_urls": platform_urls,
            "media_kind": "show" if self.title_ref.is_show else "movie",
            "match_year": self.title_ref.year,
            "selection_mode": "episodes" if self.title_ref.is_show else "movie",
            "poster_url": self.title_ref.poster_url,
            "source_meta": source_meta,
        }
        self.app.open_schedule_editor(prefill=prefill)

    def go_back(self) -> bool:
        return False


def _is_home(screen) -> bool:
    return type(screen).__name__ in {"HomeScreen", "BetaHomeScreen"}


def _fit(text: str, width: int) -> str:
    """Pad or truncate to an exact *cell* width, not an exact character count.

    Provider names here are Japanese, Korean and Chinese as often as not, and a
    CJK character occupies two cells while ``len`` counts it once - so padding by
    length pushed the next column three to twenty cells out of line, and a name
    wide enough to need truncating was not truncated at all.
    """
    return set_cell_size(str(text or ""), width)


def _safe(text: str) -> str:
    """Markup-proof a value that came off the network."""
    return escape_markup(str(text or ""))


__all__ = [
    "AvailabilityScreen",
    "JustWatchScreen",
    "RegionScreen",
    "SearchRegionScreen",
    "regions_from",
    "search_region_from",
]
