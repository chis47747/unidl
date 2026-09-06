"""One understandable home for JustWatch's two independent region choices."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ..core import justwatch as jw
from ..core.i18n import tr
from ..core.settings import Settings
from .bidi import visual_markup
from .chrome import Chrome, KeyBar, refresh_locale_widgets
from .justwatch_screen import RegionScreen, SearchRegionScreen, regions_from, search_region_from


class JustWatchSettingsScreen(Screen[None]):
    """Edit catalogue search and availability regions without conflating them."""

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "activate", "Change", show=True),
    ]

    def __init__(self, globals_scope: Settings) -> None:
        super().__init__()
        self.globals = globals_scope

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        yield Static(tr("justwatch.title"), id="masthead")
        yield Static(tr("justwatch.help"), id="justwatch-settings-help")
        with Horizontal(id="statusline"):
            yield Static(f"[$dim]{tr('justwatch.note')}[/]", classes="pill")
        with Vertical(id="justwatch-settings-body"):
            yield OptionList(id="justwatch-settings-list")
        yield KeyBar(("enter", "change"), ("^b", "back"), ("esc", "quit"))

    def on_mount(self) -> None:
        self.rebuild()
        self.query_one("#justwatch-settings-list", OptionList).focus()

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self.query_one("#masthead", Static).update(tr("justwatch.title"))
        self.query_one("#justwatch-settings-help", Static).update(tr("justwatch.help"))
        found = self.query(".pill")
        if found:
            found.first(Static).update(f"[$dim]{tr('justwatch.note')}[/]")
        self.rebuild()

    def rebuild(self) -> None:
        region = search_region_from(self.globals)
        offers = regions_from(self.globals)
        offer_names = [jw.region_label(code) for code in offers]
        if len(offer_names) > 3:
            offer_summary = " + ".join(offer_names[:3]) + tr("justwatch.more", count=len(offer_names) - 3)
        else:
            offer_summary = " + ".join(offer_names)
        rows = [
            (
                tr("justwatch.catalogue"),
                jw.region_label(region),
                tr("justwatch.catalogue_help"),
            ),
            (
                tr("justwatch.availability"),
                offer_summary,
                tr("justwatch.availability_help", count=len(offers)),
            ),
        ]
        options = self.query_one("#justwatch-settings-list", OptionList)
        highlighted = options.highlighted
        options.clear_options()
        for label, value, help_text in rows:
            options.add_option(
                Option(
                    f"  [$foreground]{label}[/]  [$accent]{visual_markup(value)}[/]\n"
                    f"     [$dim]{help_text}[/]"
                )
            )
        options.highlighted = min(highlighted or 0, len(rows) - 1)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_index == 0:
            current = search_region_from(self.globals)

            def selected(code: str | None) -> None:
                if code:
                    self.globals.set("justwatch_search_region", code)
                    self.rebuild()

            self.app.push_screen(SearchRegionScreen(current), selected)
            return

        current = regions_from(self.globals)

        def selected(codes: list[str] | None) -> None:
            if codes:
                self.globals.set("justwatch_regions", ",".join(codes))
                self.rebuild()

        self.app.push_screen(RegionScreen(current), selected)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True
