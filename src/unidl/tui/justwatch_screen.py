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
from ..core.i18n import tr
from .chrome import Chrome, KeyBar, StatusChip
from .filterbox import FILTER_MIN, FilterBox, as_number, matches

#: how many search results to ask for
RESULT_COUNT = 12


#: Said on both pickers, because the difference between them is the whole reason
#: there are two. Kept here so the two screens cannot drift into describing it
#: differently.
SEARCH_REGION_NOTE = "one region: whose catalogue the title list comes from"
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
        Binding("b", "app.global_back", "Back", show=False),
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

    def rebuild(self, needle: str = "") -> None:
        option_list = self.query_one("#region-list", OptionList)
        option_list.clear_options()
        self._codes = []
        entries = _region_entries(needle)
        if not entries:
            option_list.add_option(Option(f"  [$dim]nothing matches '{needle}'[/]", disabled=True))
            return
        for code, name in entries:
            self._codes.append(code)
            mark = "  [$ok]in use[/]" if code == self.current else ""
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
        Binding("b", "app.global_back", "Back", show=False),
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


class JustWatchScreen(Screen):
    """Search results from JustWatch, for one term."""

    BINDINGS = [
        Binding("b", "app.global_back", "Back", show=False),
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("ctrl+f", "app.global_search", "Search", show=False),
        Binding("ctrl+s", "app.global_settings", "Settings", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("ctrl+r", "regions", "Regions", show=False),
    ]

    def __init__(self, query: str):
        super().__init__()
        self._query = query
        self._titles: list[jw.Title] = []
        self._error = ""

    def compose(self) -> ComposeResult:
        yield Chrome()
        yield Static(f"{tr('justwatch.availability_head')}  [$dim]{self._query}[/]", id="masthead")
        with Horizontal(id="statusline"):
            yield StatusChip("regions", id="region-chip")
            yield Static("·", classes="chip-sep")
            yield Static("", id="subhead", classes="pill chip-static")
        with Vertical(id="body"):
            yield OptionList(id="result-list")
            yield FilterBox(hint=tr("filter.hint.results"))
        yield KeyBar(("enter", "open"), ("^r", "regions"), ("^b", "back"), ("esc", "quit"))

    def on_mount(self) -> None:
        self._render_chips()
        self.query_one("#result-list", OptionList).add_option(
            Option(f"  [$dim]{tr('justwatch.searching')}[/]", disabled=True)
        )
        self.query_one(FilterBox).display = False
        self.search()

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

    # ------------------------------------------------------------------ search
    @work(thread=True, exclusive=True, group="justwatch-search")
    def search(self) -> None:
        client = jw.JustWatch()
        region = self.search_region
        try:
            found = client.search(self._query, region=region, count=RESULT_COUNT)
        except jw.JustWatchError as exc:
            self.app.call_from_thread(self._failed, str(exc))
            return
        self.app.call_from_thread(self._show, found)

    def _failed(self, message: str) -> None:
        self._error = message
        self._titles = []
        self._render_rows()

    def _show(self, titles: list[jw.Title]) -> None:
        self._error = ""
        self._titles = titles
        self._render_rows()

    def _render_rows(self, needle: str = "") -> None:
        option_list = self.query_one("#result-list", OptionList)
        option_list.clear_options()

        if self._error:
            option_list.add_option(Option(f"  [$bad]{self._error}[/]", disabled=True))
            return
        if not self._titles:
            option_list.add_option(
                Option(f"  [$dim]{tr('justwatch.nothing', query=self._query)}[/]", disabled=True)
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
            return
        for position, index in enumerate(self._visible, 1):
            option_list.add_option(Option(self._row(position, self._titles[index])))
        option_list.highlighted = 0
        # Both regions again, because this is the moment they differ: these titles
        # came out of one catalogue, and picking one asks a different, longer list
        # of regions who carries it.
        self.query_one("#subhead", Static).update(
            f"[$dim]{tr('justwatch.results', count=len(self._titles), region=self.search_region, count2=len(self.regions))}[/]"
        )

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
            bits.append(_safe(title.synopsis[:96] + ("..." if len(title.synopsis) > 96 else "")))
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
        Binding("b", "app.global_back", "Back", show=False),
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("ctrl+f", "app.global_search", "Search", show=False),
        Binding("ctrl+s", "app.global_settings", "Settings", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("ctrl+r", "regions", "Regions", show=False),
        Binding("ctrl+y", "copy_link", "Copy link", show=False),
    ]

    def __init__(self, title: jw.Title):
        super().__init__()
        self.title_ref = title
        self._found: list[jw.Availability] = []
        #: one entry per rendered row: an offer and the service that can get it
        self._rows: list[tuple[jw.Offer, str | None] | None] = []
        self._error = ""

    def compose(self) -> ComposeResult:
        yield Chrome()
        yield Static("", id="masthead")
        with Horizontal(id="statusline"):
            yield StatusChip("regions", id="region-chip")
            yield Static("·", classes="chip-sep")
            yield Static("", id="subhead", classes="pill chip-static")
        with Vertical(id="body"):
            yield OptionList(id="offer-list")
        yield KeyBar(
            ("enter", "open in a service"), ("^y", "copy link"), ("^r", "regions"), ("^b", "back")
        )

    def on_mount(self) -> None:
        title = self.title_ref
        head = f"{_safe(title.name)}"
        if title.year:
            head += f"  [$gutter]{title.year}[/]"
        self.query_one("#masthead", Static).update(f"{head}  [$dim]{title.kind_label}[/]")
        self._render_chips()
        self.query_one("#offer-list", OptionList).add_option(
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
        shown = ", ".join(regions[:6]) + ("..." if len(regions) > 6 else "")
        self.query_one("#region-chip", StatusChip).update(
            tr("justwatch.chip_availability", count=len(regions), shown=shown)
        )

    def _status(self, text: str) -> None:
        self.query_one("#subhead", Static).update(f"[$dim]{text}[/]")

    # ------------------------------------------------------------------- fetch
    @work(thread=True, exclusive=True, group="justwatch-offers")
    def fetch(self) -> None:
        client = jw.JustWatch()
        regions = self.regions
        try:
            found = client.availability(
                self.title_ref,
                regions,
                on_region=lambda code: self.app.call_from_thread(
                    self._status, tr("justwatch.looking_up", region=jw.region_label(code))
                ),
            )
        except jw.JustWatchError as exc:
            self.app.call_from_thread(self._failed, str(exc))
            return
        self.app.call_from_thread(self._show, found)

    def _failed(self, message: str) -> None:
        self._error = message
        self._render_rows()

    def _show(self, found: list[jw.Availability]) -> None:
        self._error = ""
        self._found = found
        self._render_rows()

    # ------------------------------------------------------------------ render
    def _render_rows(self) -> None:
        option_list = self.query_one("#offer-list", OptionList)
        option_list.clear_options()
        self._rows = []

        if self._error:
            option_list.add_option(Option(f"  [$bad]{self._error}[/]", disabled=True))
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
                        f"{jw.MONETIZATION.get(monetization, monetization)}[/]",
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
                    option_list.add_option(Option(self._offer_row(offer, service_id)))

        if not self._found:
            option_list.add_option(Option(f"  [$dim]{tr('justwatch.nothing_back')}[/]", disabled=True))
        elif not carried:
            option_list.add_option(
                Option(f"  [$dim]{tr('justwatch.not_in_regions')}[/]", disabled=True)
            )
        self._focus_first(option_list)
        link = self.title_ref.web_url
        extra = tr("justwatch.copies", link=link) if link else ""
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
            body = f"[$dim]{availability.summary()}[/]"
        option_list.add_option(
            Option(f"  [$accent]{availability.label}[/]  {body}", disabled=True)
        )

    #: plain-text column widths. Padding has to be applied before the markup
    #: goes on: `f"{markup:<34}"` counts the tag characters, which is how a long
    #: price ended up welded to the service name next to it.
    PROVIDER_WIDTH = 30
    DETAIL_WIDTH = 32

    @classmethod
    def _offer_row(cls, offer: jw.Offer, service_id: str | None) -> str:
        bits = [offer.quality] if offer.quality else []
        seasons = offer.season_summary()
        if seasons:
            bits.append(seasons)
        if offer.price:
            # already carries its own currency symbol - "$3.99", "£3.49" - so
            # prefixing one turned every price into "$$3.99"
            bits.append(offer.price)
        provider = _fit(offer.provider, cls.PROVIDER_WIDTH)
        detail = _fit("  ·  ".join(bits), cls.DETAIL_WIDTH)
        if service_id:
            target = f"[$ok]{service_id}[/]"
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
        self.app.copy_text(url)
        self.notify(tr("justwatch.copied", url=url), timeout=4)

    def action_regions(self) -> None:
        def _chosen(codes: list | None) -> None:
            if codes:
                self.app.globals.set("justwatch_regions", ",".join(codes))
                self._render_chips()
                self.query_one("#offer-list", OptionList).clear_options()
                self.query_one("#offer-list", OptionList).add_option(
                    Option(f"  [$dim]{tr('justwatch.asking')}[/]", disabled=True)
                )
                self.fetch()

        self.app.push_screen(RegionScreen(self.regions), _chosen)

    def go_back(self) -> bool:
        return False


def _is_home(screen) -> bool:
    return type(screen).__name__ == "HomeScreen"


def _fit(text: str, width: int) -> str:
    """Pad or truncate to an exact *cell* width, not an exact character count.

    Provider names here are Japanese, Korean and Chinese as often as not, and a
    CJK character occupies two cells while ``len`` counts it once - so padding by
    length pushed the next column three to twenty cells out of line, and a name
    wide enough to need truncating was not truncated at all.
    """
    return set_cell_size(str(text or ""), width)


def _safe(text: str) -> str:
    """Markup-proof a value that came off the network.

    Backslashes first: escaping only the bracket turned ``foo\\[bar`` into an
    escaped backslash followed by an *opening tag*, which is the thing this is
    supposed to prevent.
    """
    return str(text or "").replace("\\", "\\\\").replace("[", "\\[")


__all__ = [
    "AvailabilityScreen",
    "JustWatchScreen",
    "RegionScreen",
    "SearchRegionScreen",
    "regions_from",
    "search_region_from",
]
