"""Search & scheduled download settings: JustWatch regions, upcoming sources, TMDB key, and host scheduler."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from ..core import justwatch as jw
from ..core import schedule_host
from ..core.i18n import phrase, tr
from ..core.settings import Settings
from ..core.upcoming import (
    DEFAULT_UPCOMING_SOURCES,
    SOURCE_LABELS,
    UPCOMING_SOURCES,
    normalize_upcoming_sources,
)
from .bidi import escape_markup, visual_markup
from .chrome import Chrome, CloseMark, KeyBar, refresh_locale_widgets
from .input import ClipboardInput as Input
from .justwatch_screen import RegionScreen, SearchRegionScreen, regions_from, search_region_from
from .settings_layout import label_width, setting_row

JUSTWATCH_SETTING_ROWS = (
    ("justwatch_search_region", "justwatch.catalogue"),
    ("justwatch_regions", "justwatch.availability"),
    ("upcoming_sources", "setting.upcoming_sources"),
    ("tmdb_api_key", "setting.tmdb_api_key"),
    ("upcoming_installed_only", "schedule.setting.installed_only"),
    ("schedule_host", "schedule.setting.host"),
)


def mask_api_key(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return tr("value.not_set", default="not set")
    tail = raw[-4:] if len(raw) >= 6 else ""
    return f"••••••{tail}" if tail else "••••••"


class TmdbApiKeyModal(ModalScreen[str | None]):
    """Masked prompt to enter, update, or clear the optional TMDB API key."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def __init__(self, current: str = "") -> None:
        super().__init__()
        self._current = str(current or "").strip()

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-card", classes="tmdb-key-modal"):
            with Horizontal(classes="modal-head"):
                yield Static(
                    tr("tmdb.key_modal.title", default="TMDB API Key"),
                    classes="modal-card-title",
                )
                yield CloseMark(id="modal-close")
            yield Static(
                f"[$muted]{escape_markup(tr('tmdb.key_modal.help', default='Enter a TMDB v3 API key or v4 Read Access Token. Saved privately (0600) and masked in logs.'))}[/]",
                classes="modal-card-help",
            )
            yield Input(
                value=self._current,
                placeholder=tr("tmdb.key_modal.placeholder", default="v3 API key or v4 Bearer token"),
                password=True,
                id="tmdb-key-input",
                classes="ask-input",
            )
            with Horizontal(classes="modal-card-actions"):
                yield Button(phrase("Save"), variant="primary", id="tmdb-save")
                yield Button(
                    tr("tmdb.key_modal.toggle_show", default="Show"),
                    id="tmdb-reveal",
                )
                if self._current:
                    yield Button(tr("tmdb.key_modal.clear", default="Clear"), id="tmdb-clear")
                yield Button(phrase("Cancel"), id="tmdb-cancel")

    def on_mount(self) -> None:
        self.query_one("#tmdb-key-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id or ""
        if bid == "tmdb-save":
            self.action_save()
        elif bid == "tmdb-clear":
            self.dismiss("")
        elif bid == "tmdb-reveal":
            field = self.query_one("#tmdb-key-input", Input)
            field.password = not field.password
            event.button.label = (
                tr("tmdb.key_modal.toggle_hide", default="Hide")
                if not field.password
                else tr("tmdb.key_modal.toggle_show", default="Show")
            )
        else:
            self.action_cancel()

    def action_save(self) -> None:
        value = self.query_one("#tmdb-key-input", Input).value.strip()
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.action_cancel()
        return True


class UpcomingSourcesModal(ModalScreen[tuple[str, ...] | None]):
    """Multi-select / single-select picker for Upcoming Radar sources.

    Selecting TMDB when no ``tmdb_api_key`` is configured immediately prompts
    the user for their TMDB API key via :class:`TmdbApiKeyModal`.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("ctrl+s", "apply", "Apply", show=False, priority=True),
        Binding("space", "toggle", "Toggle", show=False),
        Binding("o", "only_highlighted", "Only this", show=False),
        Binding("k", "edit_tmdb_key", "TMDB key", show=False),
    ]

    def __init__(self, globals_scope: Settings) -> None:
        super().__init__()
        self.globals = globals_scope
        self.selected: list[str] = list(
            normalize_upcoming_sources(
                globals_scope.get("upcoming_sources", DEFAULT_UPCOMING_SOURCES)
            )
        )

    def _has_tmdb_key(self) -> bool:
        return bool(str(self.globals.get("tmdb_api_key", "") or "").strip())

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-card", classes="sources-modal"):
            with Horizontal(classes="modal-head"):
                yield Static(
                    tr("upcoming.sources.title", default="Upcoming release sources"),
                    classes="modal-card-title",
                )
                yield CloseMark(id="modal-close")
            yield Static(
                f"[$muted]{escape_markup(tr('upcoming.sources.help', default='Space toggles sources (multi-select), o selects only the highlighted source (single-select), k edits the TMDB API key.'))}[/]",
                classes="modal-card-help",
            )
            yield OptionList(id="upcoming-source-options")
            with Horizontal(classes="modal-card-actions"):
                yield Button(
                    f"{phrase('Apply')} ({len(self.selected)})",
                    variant="primary",
                    id="sources-apply",
                )
                yield Button(
                    tr("upcoming.sources.tmdb_key_btn", default="TMDB key"),
                    id="sources-tmdb-key",
                )
                yield Button(phrase("Cancel"), id="sources-cancel")

    def on_mount(self) -> None:
        self._rebuild()
        self.query_one("#upcoming-source-options", OptionList).focus()

    def _source_note(self, source: str) -> str:
        if source == "justwatch":
            return tr("upcoming.source_note.justwatch", default="Global streaming releases & platforms")
        if source == "tvmaze":
            return tr("upcoming.source_note.tvmaze", default="Exact UTC episode air times")
        if source == "anilist":
            return tr("upcoming.source_note.anilist", default="Exact anime airing schedules")
        if source == "tmdb":
            if self._has_tmdb_key():
                return tr(
                    "upcoming.source_note.tmdb_ready",
                    key=mask_api_key(self.globals.get("tmdb_api_key", "")),
                    default="API key configured",
                )
            return tr("upcoming.source_note.tmdb_needs_key", default="Requires TMDB API key (prompted when selected)")
        return ""

    def _rebuild(self) -> None:
        options = self.query_one("#upcoming-source-options", OptionList)
        prev = options.highlighted
        options.clear_options()
        for src in UPCOMING_SOURCES:
            checked = src in self.selected
            mark = "[x]" if checked else "[ ]"
            role = "accent" if checked else "muted"
            label = SOURCE_LABELS.get(src, src)
            note = self._source_note(src)
            options.add_option(
                Option(
                    f"  [${role}]{escape_markup(mark)}[/]  "
                    f"[$foreground]{escape_markup(label)}[/]  "
                    f"[$dim]{escape_markup(note)}[/]",
                    id=src,
                )
            )
        if prev is not None and 0 <= prev < len(UPCOMING_SOURCES):
            options.highlighted = prev
        else:
            options.highlighted = 0
        apply_btn = self.query("#sources-apply")
        if apply_btn:
            apply_btn.first(Button).label = f"{phrase('Apply')} ({len(self.selected)})"

    def _highlighted_source(self) -> str | None:
        idx = self.query_one("#upcoming-source-options", OptionList).highlighted
        if idx is not None and 0 <= idx < len(UPCOMING_SOURCES):
            return UPCOMING_SOURCES[idx]
        return None

    def _prompt_tmdb_key(self, *, enable_on_save: bool, only_on_save: bool = False) -> None:
        current = str(self.globals.get("tmdb_api_key", "") or "")

        def _done(value: str | None) -> None:
            if value is None:
                if enable_on_save and not self._has_tmdb_key():
                    if "tmdb" in self.selected:
                        self.selected.remove("tmdb")
                    self.notify(
                        tr("tmdb.key_modal.required_notice", default="TMDB requires an API key and was not enabled."),
                        severity="warning",
                        timeout=4,
                    )
                self._rebuild()
                return
            cleaned = value.strip()
            self.globals.set("tmdb_api_key", cleaned)
            if cleaned and only_on_save:
                self.selected = ["tmdb"]
            elif cleaned and enable_on_save and "tmdb" not in self.selected:
                self.selected.append("tmdb")
            elif not cleaned:
                if "tmdb" in self.selected:
                    self.selected.remove("tmdb")
                existing = list(
                    normalize_upcoming_sources(
                        self.globals.get("upcoming_sources", DEFAULT_UPCOMING_SOURCES)
                    )
                )
                if "tmdb" in existing:
                    existing.remove("tmdb")
                    self.globals.set(
                        "upcoming_sources",
                        tuple(existing) if existing else DEFAULT_UPCOMING_SOURCES,
                    )
            self._rebuild()

        self.app.push_screen(TmdbApiKeyModal(current), _done)

    def _toggle_source(self, src: str) -> None:
        if src in self.selected:
            self.selected.remove(src)
            self._rebuild()
            return
        if src == "tmdb" and not self._has_tmdb_key():
            self._prompt_tmdb_key(enable_on_save=True)
            return
        self.selected.append(src)
        self._rebuild()

    def action_toggle(self) -> None:
        if isinstance(self.focused, Button):
            self.focused.press()
            return
        if self.focused is not self.query_one("#upcoming-source-options", OptionList):
            return
        src = self._highlighted_source()
        if src is not None:
            self._toggle_source(src)

    def action_only_highlighted(self) -> None:
        if self.focused is not self.query_one("#upcoming-source-options", OptionList):
            return
        src = self._highlighted_source()
        if src is None:
            return
        if src == "tmdb" and not self._has_tmdb_key():
            self._prompt_tmdb_key(enable_on_save=True, only_on_save=True)
            return
        self.selected = [src]
        self._rebuild()

    def action_edit_tmdb_key(self) -> None:
        self._prompt_tmdb_key(enable_on_save=False)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id:
            self._toggle_source(str(event.option.id))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id or ""
        if bid == "sources-apply":
            self.action_apply()
        elif bid == "sources-tmdb-key":
            self.action_edit_tmdb_key()
        else:
            self.action_cancel()

    def action_apply(self) -> None:
        if not self.selected:
            self.notify(
                tr("upcoming.sources.need_one", default="Select at least one data source."),
                severity="warning",
                timeout=4,
            )
            return
        if "tmdb" in self.selected and not self._has_tmdb_key():
            self._prompt_tmdb_key(enable_on_save=True)
            return
        ordered = tuple(s for s in UPCOMING_SOURCES if s in self.selected)
        self.dismiss(ordered)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.action_cancel()
        return True


class JustWatchSettingsScreen(Screen[None]):
    """Edit JustWatch search regions and scheduled-download / upcoming settings."""

    BINDINGS = [
        Binding("b", "app.global_back", "Back", show=False),
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "activate", "Change", show=True),
    ]

    def __init__(self, globals_scope: Settings) -> None:
        super().__init__()
        self.globals = globals_scope
        self._row_keys: list[str | None] = []
        self._host_status: schedule_host.HostScheduleStatus | None = None

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
        self._load_host_status()
        self.query_one("#justwatch-settings-list", OptionList).focus()

    def on_screen_resume(self) -> None:
        self._load_host_status()
        self.rebuild()

    @work(thread=True, exclusive=True, group="jw-sched-host", exit_on_error=False)
    def _load_host_status(self) -> None:
        try:
            status = schedule_host.get_host_status(self.app.config, probe_runtime=False)
        except Exception:  # noqa: BLE001
            return
        try:
            self.app.call_from_thread(self._apply_host_status, status)
        except RuntimeError:
            pass

    def _apply_host_status(self, status: schedule_host.HostScheduleStatus) -> None:
        if not self.is_mounted:
            return
        self._host_status = status
        self.rebuild()

    def _host_summary_text(self) -> str:
        st = self._host_status
        if st is not None and st.installed:
            return tr(
                "schedule.setting.host_on",
                backend=st.backend,
                minutes=st.interval_minutes,
                default=f"{st.backend} · every {st.interval_minutes}m",
            )
        return tr("schedule.setting.host_off", default="Not installed · in-app only")

    def selected_key(self) -> str | None:
        options = self.query_one("#justwatch-settings-list", OptionList)
        index = options.highlighted
        if index is None or not (0 <= index < len(self._row_keys)):
            return None
        return self._row_keys[index]

    def focus_setting(self, key: str) -> None:
        options = self.query_one("#justwatch-settings-list", OptionList)
        for index, row_key in enumerate(self._row_keys):
            if row_key == key:
                options.highlighted = index
                options.scroll_to_highlight()
                return

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

        active_sources = normalize_upcoming_sources(
            self.globals.get("upcoming_sources", DEFAULT_UPCOMING_SOURCES)
        )
        sources_summary = (
            " + ".join(SOURCE_LABELS.get(s, s) for s in active_sources)
            if active_sources
            else tr("value.none")
        )
        tmdb_summary = mask_api_key(self.globals.get("tmdb_api_key", ""))
        installed_only = bool(self.globals.get("upcoming_installed_only", False))
        installed_only_val = tr("value.on") if installed_only else tr("value.off")
        st = self._host_status
        host_val = self._host_summary_text()
        host_role = "ok" if (st is not None and st.installed) else "muted"

        search_rows = [
            (
                "justwatch_search_region",
                tr("justwatch.catalogue"),
                f"[$accent]{escape_markup(jw.region_label(region))}[/]",
                tr("justwatch.catalogue_help"),
            ),
            (
                "justwatch_regions",
                tr("justwatch.availability"),
                f"[$accent]{escape_markup(offer_summary)}[/]",
                tr("justwatch.availability_help", count=len(offers)),
            ),
        ]
        schedule_rows = [
            (
                "upcoming_sources",
                tr("setting.upcoming_sources", default="Upcoming release sources"),
                f"[$accent]{escape_markup(sources_summary)}[/]",
                tr(
                    "setting.upcoming_sources.help",
                    default="Pick one or combine JustWatch, TVMaze, AniList, and TMDB for the Upcoming Radar.",
                ),
            ),
            (
                "tmdb_api_key",
                tr("setting.tmdb_api_key", default="TMDB API key"),
                f"[$accent]{escape_markup(tmdb_summary)}[/]",
                tr(
                    "setting.tmdb_api_key.help",
                    default="Optional TMDB v3 API key or v4 token (stored privately and masked in logs).",
                ),
            ),
            (
                "upcoming_installed_only",
                tr("schedule.setting.installed_only", default="Installed services only"),
                f"[$accent]{escape_markup(installed_only_val)}[/]",
                tr(
                    "schedule.setting.installed_only_help",
                    default="Filter Upcoming radar to titles matching a service currently installed in UniDL.",
                ),
            ),
            (
                "schedule_host",
                tr("schedule.setting.host", default="Background host scheduler"),
                f"[${host_role}]{escape_markup(host_val)}[/]",
                tr(
                    "schedule.setting.host_help",
                    default="Run due jobs automatically when UniDL is closed via macOS launchd, Linux systemd, or Windows Task Scheduler.",
                ),
            ),
        ]

        options = self.query_one("#justwatch-settings-list", OptionList)
        wanted = self.selected_key()
        options.clear_options()
        self._row_keys = []
        longest = label_width(label for _key, label, _val, _help in (*search_rows, *schedule_rows))

        def _add_section(title: str, rows: list[tuple[str, str, str, str]]) -> None:
            self._row_keys.append(None)
            options.add_option(Option(f"  [$dim]{visual_markup(title.upper())}[/]", disabled=True))
            for key, label, val_markup, help_text in rows:
                self._row_keys.append(key)
                options.add_option(
                    Option(
                        setting_row(
                            options,
                            label,
                            f"{val_markup}\n[$dim]{escape_markup(help_text)}[/]",
                            longest,
                            indent=4,
                        )
                    )
                )

        _add_section(tr("justwatch.section.search", default="JustWatch search"), search_rows)
        _add_section(tr("justwatch.section.schedule", default="Scheduled downloads"), schedule_rows)

        target_index = next(
            (i for i, k in enumerate(self._row_keys) if wanted is not None and k == wanted),
            next((i for i, k in enumerate(self._row_keys) if k is not None), 0),
        )
        options.highlighted = target_index

    def action_activate(self) -> None:
        options = self.query_one("#justwatch-settings-list", OptionList)
        index = options.highlighted
        if index is not None:
            self._open_row(index)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._open_row(event.option_index)

    def _open_row(self, index: int) -> None:
        if not (0 <= index < len(self._row_keys)):
            return
        key = self._row_keys[index]
        if key is None:
            return

        if key == "justwatch_search_region":
            current = search_region_from(self.globals)

            def selected_region(code: str | None) -> None:
                if code:
                    self.globals.set("justwatch_search_region", code)
                    self.rebuild()

            self.app.push_screen(SearchRegionScreen(current), selected_region)
            return

        if key == "justwatch_regions":
            current_regions = regions_from(self.globals)

            def selected_regions(codes: list[str] | None) -> None:
                if codes:
                    self.globals.set("justwatch_regions", ",".join(codes))
                    self.rebuild()

            self.app.push_screen(RegionScreen(current_regions), selected_regions)
            return

        if key == "upcoming_sources":
            def selected_sources(chosen: tuple[str, ...] | None) -> None:
                if chosen is not None:
                    self.globals.set("upcoming_sources", chosen)
                self.rebuild()

            self.app.push_screen(UpcomingSourcesModal(self.globals), selected_sources)
            return

        if key == "tmdb_api_key":
            current_key = str(self.globals.get("tmdb_api_key", "") or "")

            def saved_key(value: str | None) -> None:
                if value is not None:
                    cleaned = value.strip()
                    self.globals.set("tmdb_api_key", cleaned)
                    if not cleaned:
                        existing = list(
                            normalize_upcoming_sources(
                                self.globals.get("upcoming_sources", DEFAULT_UPCOMING_SOURCES)
                            )
                        )
                        if "tmdb" in existing:
                            existing.remove("tmdb")
                            self.globals.set(
                                "upcoming_sources",
                                tuple(existing) if existing else DEFAULT_UPCOMING_SOURCES,
                            )
                    self.rebuild()

            self.app.push_screen(TmdbApiKeyModal(current_key), saved_key)
            return

        if key == "upcoming_installed_only":
            current_only = bool(self.globals.get("upcoming_installed_only", False))
            self.globals.set("upcoming_installed_only", not current_only)
            self.rebuild()
            return

        if key == "schedule_host":
            from .schedule_screen import ScheduleHostModal

            self.app.push_screen(ScheduleHostModal(), lambda _res: self._load_host_status())

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


__all__ = [
    "JUSTWATCH_SETTING_ROWS",
    "JustWatchSettingsScreen",
    "TmdbApiKeyModal",
    "UpcomingSourcesModal",
    "mask_api_key",
]
