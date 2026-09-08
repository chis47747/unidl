"""Main screen: the platform list, and nothing else.

Deliberately austere. This screen answers one question - which platform - so it
shows platform names and a number for each. Account state, feature flags and
helper status belong on the service's own screen, once you have chosen one.
Keeping them out also means the screen does not probe every service to draw
itself.

Three ways to choose, all equal:

* click a name
* type part of a name, then Enter
* type its number, then Enter

Numbers are assigned once, over the whole list, and do not move when the list is
filtered. A number is an address, not a position.

Cells are built once too. Filtering hides and shows them rather than rebuilding
the list, which keeps a keystroke from destroying and recreating every widget.

Picking one clears this screen and opens the service, which presents its own
options. The input sits below the list, where what you type lands.
"""

from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from .. import __version__
from ..core import drm as drm_registry
from ..core import service_catalog
from ..core.cdm import WIDEVINE
from ..core.devreload import ReloadError
from ..core.i18n import phrase, tr
from ..core.service import Service
from . import banner
from .askhost import MODE_LABEL as _MODE_LABEL
from .bidi import visual_markup
from .cells import ServiceCell
from .chrome import Chrome, KeyBar, StatusChip
from .input import ClipboardInput as Input

#: target width of one cell: the number, the brand, and the service tag. Wide
#: enough for the longest brand plus a four-character tag, which at every width
#: worth caring about costs no columns compared to leaving the tag out.
CELL_WIDTH = 28
MAX_COLUMNS = 6
SERVICE_VIEWS = ("alphabetical", "country", "type")

# ``GEOFENCE`` is stored as ISO-3166-1 alpha-2 codes in service declarations.
# The short code keeps grouping deterministic; the readable name makes the
# country view useful without probing a service or loading network metadata.
_COUNTRY_NAMES = {
    "AE": "United Arab Emirates", "AR": "Argentina", "AT": "Austria",
    "AU": "Australia", "BE": "Belgium", "BH": "Bahrain", "BR": "Brazil",
    "CA": "Canada", "CH": "Switzerland", "CL": "Chile", "CN": "China",
    "CO": "Colombia", "CZ": "Czechia", "DE": "Germany", "DK": "Denmark",
    "EG": "Egypt", "ES": "Spain", "FI": "Finland", "FR": "France",
    "GB": "United Kingdom", "HK": "Hong Kong", "HU": "Hungary", "ID": "Indonesia",
    "IE": "Ireland", "IL": "Israel", "IN": "India", "IQ": "Iraq",
    "IT": "Italy", "JO": "Jordan", "JP": "Japan", "KR": "South Korea",
    "KW": "Kuwait", "LB": "Lebanon", "MX": "Mexico", "NL": "Netherlands",
    "NO": "Norway", "NZ": "New Zealand", "OM": "Oman", "PE": "Peru",
    "PH": "Philippines", "PL": "Poland", "PT": "Portugal", "QA": "Qatar",
    "RU": "Russia", "SA": "Saudi Arabia", "SE": "Sweden", "SG": "Singapore",
    "TR": "Türkiye", "TW": "Taiwan", "US": "United States", "VN": "Vietnam",
    "ZA": "South Africa",
}


def _is_number(value: str) -> bool:
    """Whether ``int`` will accept this. ``isdigit`` alone will not do.

    Superscripts and other numeric forms answer True to ``isdigit`` and then raise
    out of ``int``, and this is tested on every keystroke.
    """
    return value.isascii() and value.isdigit()


class HomeScreen(Screen):
    BINDINGS = [
        Binding("b", "app.global_back", "Back", show=False),
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("ctrl+f", "app.global_search", "Search", show=False),
        Binding("ctrl+s", "app.global_settings", "Settings", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("ctrl+v", "cycle_service_view", "Service view", show=False),
        Binding("v", "cycle_service_view", "Service view", show=False),
        Binding("slash", "app.global_search", "Search", show=False),
        Binding("s", "app.global_settings", "Settings", show=False),
        Binding("d", "cycle_mode", "After picking", show=False),
        Binding("ctrl+r", "reload_service", "Dev reload", show=False),
        Binding("up", "move(-1, 0)", "Up", show=False),
        Binding("down", "move(1, 0)", "Down", show=False),
        Binding("left", "move(0, -1)", "Left", show=False),
        Binding("right", "move(0, 1)", "Right", show=False),
        Binding("home", "jump(0)", "First", show=False),
        Binding("end", "jump(-1)", "Last", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._sections: list[list[ServiceCell]] = []
        self._heads: list[Static | None] = []
        self._grids: list[Grid] = []
        self._numbered: dict[int, type[Service]] = {}
        self._no_match: Static | None = None
        self._chip_widths: dict[str, int] = {}
        self._columns = 1
        self._update_info = None

    # -------------------------------------------------------------- composition
    def compose(self) -> ComposeResult:
        yield Chrome(can_go_back=False)
        yield Static("", id="banner")
        with Horizontal(id="caption"):
            yield StatusChip("show_update_info", id="caption-version", classes="caption-link")
            yield Static("·", id="caption-separator", classes="caption-separator")
            yield StatusChip("show_about", id="caption-copyright", classes="caption-link")
        # every item on this row is a control: click the CDM to change it, the
        # DRM system to switch it, the action to cycle it, the config to open it
        with Horizontal(id="statusline"):
            yield StatusChip("pick_cdm", id="chip-cdm")
            yield Static("[$gutter]·[/]", id="sep-drm", classes="chip-sep")
            yield StatusChip("toggle_drm", id="chip-drm")
            yield Static("[$gutter]·[/]", id="sep-mode", classes="chip-sep")
            yield StatusChip("cycle_mode", id="chip-mode")
            yield Static("[$gutter]·[/]", id="sep-config", classes="chip-sep")
            yield StatusChip("open_config", id="chip-config")
            yield Static("[$gutter]·[/]", id="sep-ready", classes="chip-sep")
            yield StatusChip("open_readiness", id="chip-ready")
            yield Static("[$gutter]·[/]", id="sep-import", classes="chip-sep")
            yield StatusChip("open_imports", id="chip-import")
        with Vertical(id="body"):
            # what is not set up yet, above the list rather than in it: a new
            # install is not a broken one, and the platforms are still all usable
            yield StatusChip("pick_cdm", id="setup-note")
            yield StatusChip("app.global_settings", id="service-setup-note")
            with Horizontal(id="service-list-head", classes="section-head"):
                yield Static("", id="service-list-summary")
                yield StatusChip(
                    "cycle_service_view",
                    id="service-view",
                    classes="service-view",
                )
            yield VerticalScroll(id="grid-area")
            yield Static("", id="prompt-hint")
            yield Input(
                placeholder=tr("home.filter_placeholder"),
                id="filter",
            )
        yield KeyBar(
            ("enter", "open the highlighted platform"),
            ("↑↓←→", "move"),
            ("d", "after picking"),
            ("^f", "search"),
            ("^s", "settings"),
            ("^v", "service view"),
            ("^r", "dev reload"),
            ("^p", "commands"),
        )

    def on_mount(self) -> None:
        self._render_caption()
        self.query_one("#prompt-hint", Static).update(
            f"[$dim]{tr('home.prompt_hint')}[/]"
        )
        self.refresh_banner()
        self.refresh_env()
        self.build()
        self.query_one("#filter", Input).focus()
        self.check_updates()

    def refresh_after_settings(self) -> None:
        self.refresh_env()

    def relocalize(self) -> None:
        for chrome in self.query(Chrome):
            chrome.refresh_locale()
        for bar in self.query(KeyBar):
            bar.render_pairs()
        hint = self.query("#prompt-hint")
        if hint:
            hint.first(Static).update(f"[$dim]{tr('home.prompt_hint')}[/]")
        found = self.query("#filter")
        if found:
            found.first(Input).placeholder = tr("home.filter_placeholder")
        self._render_caption()
        self._render_services_heading(len(self.app.home_services))
        self.refresh_env()
        self._render_setup_note()

    def refresh_banner(self) -> None:
        self.query_one("#banner", Static).update(banner.art(self.size.width, "$accent"))

    def _render_caption(self) -> None:
        version, copyright_text = banner.caption_parts("$muted", "$gutter")
        if self._update_info is not None and self._update_info.update_available:
            version = (
                f"[$muted]v{__version__}[/] [$warn]↑ v{self._update_info.latest_version}[/]"
            )
        found = self.query("#caption-version")
        if found:
            found.first(StatusChip).update(version)
            if self._update_info is not None and self._update_info.update_available:
                found.first(StatusChip).tooltip = tr(
                    "home.caption.update_available",
                    version=self._update_info.latest_version,
                )
            else:
                found.first(StatusChip).tooltip = tr("home.caption.version_hint")
        found = self.query("#caption-copyright")
        if found:
            found.first(StatusChip).update(copyright_text)
            found.first(StatusChip).tooltip = tr("home.caption.about_hint")

    # ---------------------------------------------------------- status line
    def refresh_env(self) -> None:
        config = self.app.config
        device = config.device_name_for("") or "none"
        remote = config.remote_cdm(device)
        if device == "none":
            device_source = ""
            device_shown = tr("home.device.none")
        elif remote is not None:
            device_source = tr("home.device.remote")
            device_shown = device
        else:
            device_source = tr("home.device.local")
            device_shown = device
        system = str(self.app.globals.get("drm_system", WIDEVINE))
        # Asked of the registry rather than spelled out: this said "Widevine" for
        # anything that was not PlayReady, so selecting MonaLisa left the chip
        # claiming a system that was not going to run.
        declared = drm_registry.get(system)
        system_label = declared.label if declared else system
        # amber when the selected system's library is not installed
        system_colour = "$ok" if (declared and declared.available) else "$warn"
        mode = str(self.app.globals.get("after_resolve", "download"))
        debug = f"  {tr('home.chip.debug')}" if self.app.globals.get("debug") else ""
        cdm_word = tr("home.chip.cdm")
        drm_word = tr("home.chip.drm")
        after_word = tr("home.chip.after_picking")
        config_word = tr("home.chip.config")

        source_colour = "$manifest" if remote is not None else "$muted"
        source = f"[{source_colour}]{device_source}[/] " if device_source else ""
        # amber when there is nothing chosen: "none" in the same colour as a device
        # name reads like the name of a device
        name_colour = "$foreground" if device != "none" else "$warn"
        self._set_chip(
            "chip-cdm",
            f"[$muted]{cdm_word}[/] {source}[{name_colour}]{device_shown}[/]",
            f"{cdm_word} {device_source} {device_shown}".replace("  ", " "),
        )
        self._set_chip(
            "chip-drm",
            f"[$muted]{drm_word}[/] [{system_colour}]{system_label}[/]",
            f"{drm_word} {system_label}",
        )
        action = phrase(_MODE_LABEL.get(mode, mode))
        self._set_chip(
            "chip-mode",
            f"[$muted]{after_word}[/] [$foreground]{action}[/]"
            + (f"  [$warn]{tr('home.chip.debug')}[/]" if debug else ""),
            f"{after_word} {action}{debug}",
        )
        if config.source is not None:
            self._set_chip(
                "chip-config",
                f"[$muted]{config_word}[/] [$manifest]{config.source.name}[/]",
                f"{config_word} {config.source.name}",
            )
        else:
            self._set_chip(
                "chip-config",
                # Keep this control short enough to remain visible on a normal
                # 120-column terminal.  A long sentence here used to make the
                # responsive status row hide the very control that creates the
                # first config file, so it was impossible to click in the
                # smallest supported layout.  The full explanation remains in
                # the tooltip for terminals that can show it.
                f"[$warn]{config_word}[/] [$muted]{tr('home.chip.config_create')}[/]",
                f"{config_word} {tr('home.chip.config_create')}",
            )
            chip = self.query_one("#chip-config", StatusChip)
            chip.tooltip = tr("home.chip.config_create_tip")
        self._refresh_ready_chip()
        self._refresh_import_chip()
        self._fit_statusline()
        self._render_setup_note()

    def _refresh_import_chip(self) -> None:
        """How many resolved titles are sitting in the exports folder.

        Shown even when it is none, because a chip that only appears once you have
        used the feature is a feature nobody finds. Counted on a worker with the
        readiness survey's reasoning: it reads and parses every file in the folder,
        which is not something the first paint should wait for.
        """
        word = tr("home.chip.import")
        self._set_chip("chip-import", f"[$muted]{word}[/]", word)
        self.count_imports()

    @work(thread=True, exclusive=True, group="imports")
    def count_imports(self) -> None:
        from ..core import exports

        try:
            found = exports.scan(self.app.config.paths.exports)
        except Exception:  # noqa: BLE001 - an unreadable folder is not fatal
            found = []
        readable = [row for row in found if row[1] is not None]
        word = tr("home.chip.import")
        if not found:
            markup, plain = f"[$muted]{word}[/]", word
        else:
            colour = "$foreground" if readable else "$warn"
            markup = f"[$muted]{word}[/] [{colour}]{len(readable)}[/]"
            plain = f"{word} {len(readable)}"
        self.app.call_from_thread(self._set_chip, "chip-import", markup, plain)
        self.app.call_from_thread(self._fit_statusline)

    def _refresh_ready_chip(self) -> None:
        """Say whether anything external is missing, without stopping to find out.

        The survey runs version probes - a subprocess per tool - so it happens on a
        worker and the chip says "checking" until it answers. Doing it inline held
        the first paint of the main screen for the best part of a second.
        """
        checking = tr("home.chip.checking")
        self._set_chip("chip-ready", f"[$muted]{checking}[/]", checking)
        self.survey_environment()

    @work(thread=True, exclusive=True, group="readiness")
    def survey_environment(self) -> None:
        from ..core import readiness

        try:
            report = readiness.survey(self.app.config, self.app.registry)
        except Exception:  # noqa: BLE001 - a report that cannot be built is not fatal
            unknown = tr("home.chip.ready_unknown")
            self.app.call_from_thread(
                self._set_chip, "chip-ready", f"[$muted]{unknown}[/]", unknown
            )
            return
        colour = "$bad" if not report.ready else ("$warn" if report.missing else "$ok")
        summary = report.summary()
        self.app.call_from_thread(
            self._set_chip, "chip-ready", f"[{colour}]{summary}[/]", summary
        )
        self.app.call_from_thread(self._fit_statusline)

    def _render_setup_note(self) -> None:
        """Say what a new install still needs, where a new install starts.

        The CDM and service catalog are separate setup concerns. Most installs
        include services, but a deliberately minimal distribution may not; that
        case gets an import/register instruction alongside the CDM note.

        A missing CDM is different in kind. Nothing on screen fails until a licence
        is requested, and then it fails deep inside one title's report - by which
        point the user has picked a platform, signed in and chosen an episode. Said
        here, it costs two rows once, and it goes away the moment a device exists.
        """
        found = self.query("#setup-note")
        service_found = self.query("#service-setup-note")
        if not found or not service_found:
            return
        note = found.first(StatusChip)
        service_note = service_found.first(StatusChip)
        if not self.app.registry.all():
            folder = service_catalog.source_root()
            service_note.update(
                f"[$warn]{tr('home.setup_services')}[/]\n"
                f"[$dim]{tr('home.setup_services_hint', folder=f'[$accent]{folder}[/][$dim]')}[/]"
            )
            service_note.display = True
        else:
            service_note.display = False
        if self.app.devices():
            note.display = False
        else:
            folder = self.app.config.paths.cdm
            note.update(
                f"[$warn]{tr('home.setup_cdm')}[/] [$muted]· {tr('home.setup_cdm_detail')}[/]\n"
                f"[$dim]{tr('home.setup_cdm_hint', folder=f'[$accent]{folder}[/][$dim]')}[/]"
            )
            note.display = True

    def _set_chip(self, chip_id: str, markup: str, plain: str) -> None:
        """Update a chip, remembering how wide its text really is.

        The plain length is tracked by hand because the markup is full of colour
        tags: measuring the widget after the fact would need a layout pass, and
        this row has to decide what fits *before* it is drawn.
        """
        found = self.query(f"#{chip_id}")
        if not found:
            return
        found.first(StatusChip).update(markup)
        self._chip_widths[chip_id] = len(plain)

    @staticmethod
    def _fit_order() -> list[str]:
        """The order status items are given up in when the row will not fit.

        Named rather than inlined because it is a policy, not an implementation
        detail: the import count goes first because it is the only item on the row
        that is not about *this* run - everything else is state the next download
        will use, and trading one of those for a folder count is the wrong way
        round. The import screen is still reachable with the row at its narrowest,
        by pasting the file's path into the box below.
        """
        return ["chip-import", "chip-config", "chip-mode", "chip-ready", "chip-drm"]

    def _fit_statusline(self) -> None:
        """Drop items from the right until the row fits.

        Same rule as the key bar: lose the least important thing whole, rather
        than truncating the most important one mid-word.
        """
        if not self._chip_widths:
            return
        available = max(0, (self.size.width or 80) - 4)
        # least important first, so this is the order they go
        droppable = self._fit_order()
        keep = [
            "chip-cdm", "chip-drm", "chip-mode", "chip-config", "chip-ready", "chip-import",
        ]

        hidden: set[str] = set()
        while True:
            used = sum(
                self._chip_widths.get(chip, 0) + 4 for chip in keep if chip not in hidden
            )
            if used <= available or not [c for c in droppable if c not in hidden]:
                break
            hidden.add(next(c for c in droppable if c not in hidden))

        for chip in keep:
            found = self.query(f"#{chip}")
            if found:
                found.first().display = chip not in hidden
        # a separator with nothing after it is just a dot
        for chip, sep in (("chip-drm", "sep-drm"), ("chip-mode", "sep-mode"),
                          ("chip-config", "sep-config"), ("chip-ready", "sep-ready"),
                          ("chip-import", "sep-import")):
            found = self.query(f"#{sep}")
            if found:
                found.first().display = chip not in hidden

    # ------------------------------------------------------------ chip actions
    def action_open_config(self) -> None:
        self.app.open_config()

    def action_open_readiness(self) -> None:
        """The whole report, from the chip that summarises it."""
        from .readiness_screen import ReadinessScreen

        def _done(_result) -> None:
            # something may have been installed while it was open
            self._refresh_ready_chip()

        self.app.push_screen(ReadinessScreen(), _done)

    def action_open_imports(self) -> None:
        """The exports folder, from the chip that counts it."""
        from .import_screen import ImportScreen

        def _done(_result) -> None:
            # a file may have been imported, or dropped in, while it was open
            self._refresh_import_chip()

        self.app.push_screen(ImportScreen(), _done)

    def action_cycle_mode(self) -> None:
        """Step through every outcome, in the order the setting offers them.

        All of them, not the three it used to: listing and exporting are outcomes
        like the others, and a chip that cycles past two of the five is a chip that
        cannot say what the setting says.
        """
        order = ["download", "command", "list", "export", "ask"]
        current = str(self.app.globals.get("after_resolve", "download"))
        nxt = order[(order.index(current) + 1) % len(order)] if current in order else order[0]
        self.app.globals.set("after_resolve", nxt)
        self.refresh_env()
        self.notify(f"After picking a title: {_MODE_LABEL[nxt]}", timeout=3)

    def _service_view(self) -> str:
        value = str(self.app.globals.get("service_list_view", SERVICE_VIEWS[0]) or "")
        return value if value in SERVICE_VIEWS else SERVICE_VIEWS[0]

    def _service_view_content(self) -> tuple[str, str]:
        view_label = tr(f"home.view.{self._service_view()}")
        plain = f"{tr('home.chip.view')} {view_label}"
        return (
            f"[$muted]{tr('home.chip.view')}[/] [$foreground]{view_label}[/]",
            plain,
        )

    def _render_service_view(self) -> None:
        """Render the list view in the Services heading where it belongs."""
        found = self.query("#service-view")
        if not found:
            return
        markup, plain = self._service_view_content()
        chip = found.first(StatusChip)
        chip.update(markup)
        chip.tooltip = plain

    async def action_cycle_service_view(self) -> None:
        """Cycle the home list between name, country and media-type groupings."""
        current = self._service_view()
        view = SERVICE_VIEWS[(SERVICE_VIEWS.index(current) + 1) % len(SERVICE_VIEWS)]
        self.app.globals.set("service_list_view", view)
        needle = self.query_one("#filter", Input).value
        await self._rebuild_services(needle)
        self.query_one("#filter", Input).focus()
        self._render_service_view()
        self.notify(f"Service list: {tr(f'home.view.{view}')}", timeout=3)

    def action_show_about(self) -> None:
        from .about_screen import AboutScreen

        self.app.push_screen(AboutScreen())

    def action_show_update_info(self) -> None:
        from .update_screen import UpdateScreen

        self.app.push_screen(UpdateScreen(self._update_info))

    @work(thread=True, exclusive=True, group="update-check")
    def check_updates(self) -> None:
        """Check public release metadata without delaying the first Home paint."""
        from ..core.update import check_for_updates

        try:
            result = check_for_updates()
        except Exception:  # noqa: BLE001 - update discovery must never affect startup
            result = None
        try:
            self.app.call_from_thread(self._set_update_info, result)
        except (RuntimeError, AttributeError):
            # The app may be closing while the bounded network worker returns.
            pass

    def _set_update_info(self, result) -> None:
        if not self.is_attached:
            return
        self._update_info = result
        self._render_caption()
        screen = self.app.screen
        if screen is not self:
            refresh = getattr(screen, "set_update_info", None)
            if callable(refresh):
                refresh(result)

    def action_toggle_drm(self) -> None:
        """Step to the next registered DRM system, taking the device with it.

        A cycle rather than a toggle now that there are three, and it steps
        through the registry so a fourth joins in without a change here. The
        device follows the system: a ``.wvd`` left selected while PlayReady is
        active would be silently wrong, so the first device of the new kind is
        adopted, or the user is told where to put one.

        This is one of the two global controls. A service that can use more than
        one system also has its own choice, in its own settings, which wins for
        that service.
        """
        systems = drm_registry.all_systems()
        if not systems:
            return
        current = str(self.app.globals.get("drm_system", WIDEVINE))
        order = [system.id for system in systems]
        position = order.index(current) if current in order else -1
        system = systems[(position + 1) % len(systems)]
        self.app.globals.set("drm_system", system.id)

        if self.app.active_device_system() != system.id:
            match = next((d for d in self.app.devices() if d.system == system.id), None)
            if match is not None:
                self.app.set_device(match.name)
                self.notify(f"{system.label}, using {match.name}", timeout=4)
            else:
                folder = self.app.config.paths.cdm / system.id
                self.notify(
                    f"{system.label} selected, but no {system.suffix} device found. "
                    f"Put one in {folder}",
                    severity="warning",
                    timeout=8,
                )
        else:
            self.notify(f"DRM system: {system.label}", timeout=3)

        if not system.available:
            self.notify(system.install_hint, severity="warning", timeout=8)
        self.refresh_env()

    def action_pick_cdm(self) -> None:
        """Open the device picker and adopt whatever comes back."""
        from .cdm_screen import CdmScreen

        current = self.app.config.device_name_for("")
        system = str(self.app.globals.get("drm_system", WIDEVINE))

        def _chosen(name: str | None) -> None:
            if not name:
                return
            self.app.set_device(name)
            # the file's extension is the truth about which system it is for
            found = next((d for d in self.app.devices() if d.name == name), None)
            if found is not None and found.system != str(self.app.globals.get("drm_system")):
                self.app.globals.set("drm_system", found.system)
                self.notify(f"{name} is a {found.system} device; switched to it", timeout=5)
            else:
                self.notify(f"CDM: {name}", timeout=3)
            self.refresh_env()

        self.app.push_screen(CdmScreen(current=current, system=system), _chosen)

    # -------------------------------------------------------------- responsive
    def _column_count(self) -> int:
        width = self.query_one("#grid-area").size.width or self.size.width
        return max(1, min(MAX_COLUMNS, (width - 1) // CELL_WIDTH))

    def on_resize(self) -> None:
        self.refresh_banner()
        self._fit_statusline()
        columns = self._column_count()
        if columns == self._columns:
            return
        self._columns = columns
        for grid in self._grids:
            grid.styles.grid_size_columns = columns

    # ----------------------------------------------------------------- listing
    def build(self) -> None:
        """Create every cell for the current registry snapshot.

        Numbers are handed out here, over the *whole* list, and never change.
        That is the point: a number is a platform's address, so `42` has to mean
        the same thing before and after you type in the filter. Numbering the
        filtered list instead - which is what this used to do - meant the digit
        you were about to press changed meaning as you typed.
        """
        area = self.query_one("#grid-area", VerticalScroll)
        area.remove_children()
        self._populate(area, "")

    def _populate(self, area: VerticalScroll, needle: str) -> None:
        """Mount a fresh registry snapshot after the old cells are gone."""
        self._sections = []
        self._heads = []
        self._grids = []
        self._numbered = {}
        self._columns = self._column_count()

        services = list(self.app.home_services)
        self._render_services_heading(len(services))
        groups = self._groups_for_view(services)
        show_group_heading = self._service_view() != "alphabetical"

        number = 1
        for title, note, group in groups:
            number = self._add_section(
                area,
                title,
                note,
                group,
                number,
                show_heading=show_group_heading,
            )

        self._no_match = Static("", classes="empty-note")
        self._no_match.display = False
        area.mount(self._no_match)
        if _is_number(needle.strip()):
            # Numeric input addresses a stable service number rather than
            # filtering names; retain that meaning when a view rebuilds.
            self.apply_filter("")
            self._highlight_number(int(needle.strip()))
        else:
            self.apply_filter(needle)

    def _groups_for_view(
        self, services: list[type[Service]]
    ) -> list[tuple[str, str, list[type[Service]]]]:
        """Return deterministic sections for the selected home-list view.

        A service with several geofences is assigned to its first declared
        territory, which is the service's primary/default market. It appears
        once only; the complete tuple remains available in the service metadata.
        Services without a geofence go in the final International section.
        """
        view = self._service_view()

        def alphabetical(group: list[type[Service]]) -> list[type[Service]]:
            return sorted(group, key=lambda service: service.NAME.lower())

        if view == "alphabetical":
            return [(tr("home.section.services"), f"{len(services)} available", alphabetical(services))]

        buckets: dict[str, list[type[Service]]] = {}
        for service in services:
            if view == "country":
                codes = getattr(service, "GEOFENCE", ()) or ()
                code = next(
                    (str(value).strip().upper() for value in codes if str(value).strip()),
                    "INTL",
                )
            else:
                kinds = set(service.media_types())
                code = "audio_video" if kinds == {"audio", "video"} else next(iter(kinds), "video")
            buckets.setdefault(code, []).append(service)

        if view == "country":
            order = sorted(buckets, key=lambda code: (code == "INTL", code))
            return [
                (
                    self._country_label(code),
                    f"{len(buckets[code])} available",
                    alphabetical(buckets[code]),
                )
                for code in order
            ]

        order = ("video", "audio", "audio_video")
        labels = {
            "video": tr("home.media.video"),
            "audio": tr("home.media.audio"),
            "audio_video": tr("home.media.audio_video"),
        }
        return [
            (labels[code], f"{len(buckets[code])} available", alphabetical(buckets[code]))
            for code in order
            if code in buckets
        ]

    @staticmethod
    def _country_label(code: str) -> str:
        if code == "INTL":
            return tr("home.country.international")
        return f"{_COUNTRY_NAMES.get(code, code)} ({code})"

    async def _rebuild_services(self, needle: str) -> None:
        """Replace cells so none of them retains a pre-reload service class."""
        area = self.query_one("#grid-area", VerticalScroll)
        await area.remove_children()
        self._populate(area, needle)

    def _render_services_heading(self, count: int) -> None:
        """Keep the list summary and its far-right view control current."""
        summary = self.query_one("#service-list-summary", Static)
        summary.update(
            f"[$muted]{tr('home.section.services').upper()}[/]  "
            f"[$dim]{count} available[/]"
        )
        self._render_service_view()

    def _add_section(
        self,
        area: VerticalScroll,
        title: str,
        note: str,
        services: list[type[Service]],
        start: int,
        *,
        show_heading: bool = True,
    ) -> int:
        if not services:
            return start
        head: Static | None = None
        if show_heading:
            head = Static(
                f"[$muted]{title.upper()}[/]  [$dim]{note}[/]", classes="section-head"
            )
            area.mount(head)

        grid = Grid(classes="service-grid")
        area.mount(grid)
        grid.styles.grid_size_columns = self._columns

        cells: list[ServiceCell] = []
        number = start
        for service in services:
            self._numbered[number] = service
            cells.append(ServiceCell(service, self._label(service, number), index=len(cells)))
            number += 1
        grid.mount_all(cells)

        self._sections.append(cells)
        self._heads.append(head)
        self._grids.append(grid)
        return number

    def rebuild(self, needle: str | None = None) -> None:
        """Re-apply the filter. Cells exist already; this only hides and shows."""
        if needle is None:
            found = self.query("#filter")
            needle = found.first(Input).value if found else ""
        self.apply_filter(needle)

    def apply_filter(self, needle: str) -> None:
        """Show the cells that match, hide the rest.

        Hiding rather than rebuilding: `display: none` drops a cell out of the
        grid layout entirely, so the remaining ones reflow with no holes, and
        Existing widgets do not get destroyed and recreated on every keystroke.
        """
        needle = needle.strip().lower()
        by_url = self.app.registry.for_url(needle) if needle else None
        visible = 0

        with self.app.batch_update():
            for index, cells in enumerate(self._sections):
                here = 0
                for cell in cells:
                    match = self._matches(cell.service, needle, by_url)
                    cell.display = match
                    cell.remove_class("targeted")
                    here += match
                # an empty section is a heading over nothing
                head = self._heads[index]
                if head is not None:
                    head.display = here > 0
                self._grids[index].display = here > 0
                visible += here

            if needle and visible == 0:
                # say what to do about it, not just that it happened
                # escaped: the needle is whatever was typed, and "[" made the
                # message erase the very thing it was reporting - "[/]" crashed it
                self._no_match.update(
                    f"[$foreground]Nothing matches '{visual_markup(needle)}'[/]\n"
                    f"[$dim]Try fewer letters, or paste a video URL and unidl will "
                    f"work out which platform it belongs to.[/]"
                )
                self._no_match.display = True
            else:
                self._no_match.display = False

        # a hidden widget must not keep the focus, or the arrow keys have
        # nowhere to go from
        focused = self.app.focused
        if isinstance(focused, ServiceCell) and not focused.display:
            self.query_one("#filter", Input).focus()

    @staticmethod
    def _matches(service: type[Service], needle: str, by_url: type[Service] | None) -> bool:
        if not needle:
            return True
        if by_url is not None:
            return service is by_url
        return (
            needle in service.NAME.lower()
            or needle in service.ID.lower()
            or needle in service.tag().lower()
            or any(needle in alias.lower() for alias in service.ALIASES)
        )

    @staticmethod
    def _label(service: type[Service], number: int) -> str:
        """Number, brand name, and the service tag.

        The tag is shown because it is what a shared command or a note from
        someone using unshackle will call this platform, and typing it here has
        to find the same thing.
        """
        return f"[$gutter]{number:>3}[/]  {service.NAME}  [$gutter]{service.tag()}[/]"

    # -------------------------------------------------------------- navigation
    def _shown(self) -> list[list[ServiceCell]]:
        """Sections as they currently look, hidden cells left out.

        Navigation walks this rather than the full lists, so a filtered grid
        moves the way it looks like it should: right from the last visible cell
        in a row goes to the next visible one, not into a hidden gap.
        """
        return [cells for cells in ([c for c in s if c.display] for s in self._sections) if cells]

    def _locate(self, shown: list[list[ServiceCell]]) -> tuple[int, int] | None:
        focused = self.app.focused
        if not isinstance(focused, ServiceCell):
            return None
        for section_index, cells in enumerate(shown):
            if focused in cells:
                return section_index, cells.index(focused)
        return None

    def action_move(self, rows: int, cols: int) -> None:
        shown = self._shown()
        if not shown:
            return
        located = self._locate(shown)
        if located is None:
            shown[0][0].focus()
            return
        section, index = located
        cells = shown[section]

        if cols:
            target = index + cols
            if 0 <= target < len(cells):
                cells[target].focus()
            return

        target = index + rows * self._columns
        if 0 <= target < len(cells):
            cells[target].focus()
            return

        if rows < 0:
            if section == 0:
                self.query_one("#filter", Input).focus()
            else:
                previous = shown[section - 1]
                previous[min(index, len(previous) - 1)].focus()
        elif section + 1 < len(shown):
            following = shown[section + 1]
            following[min(index % self._columns, len(following) - 1)].focus()
        else:
            self.query_one("#filter", Input).focus()

    def action_jump(self, where: int) -> None:
        shown = self._shown()
        if not shown:
            return
        (shown[0][0] if where == 0 else shown[-1][-1]).focus()

    def _reload_target(self) -> type[Service] | None:
        """The service explicitly selected by focus, address, or filtering."""
        focused = self.app.focused
        if isinstance(focused, ServiceCell) and focused.display:
            return focused.service

        value = self.query_one("#filter", Input).value.strip()
        if _is_number(value):
            numbered = self._numbered.get(int(value))
            if numbered is not None:
                return numbered
        exact = self.app.registry.get(value) if value else None
        if exact is not None:
            return exact

        shown = [cell for section in self._shown() for cell in section]
        return shown[0].service if len(shown) == 1 else None

    async def action_reload_service(self) -> None:
        """Explicitly reload one service package; never watch or patch sessions."""
        if not bool(self.app.globals.get("debug", False)):
            self.notify(
                "Turn on Debug mode in global Settings before reloading service code.",
                title="Developer reload is off",
                severity="warning",
                timeout=6,
            )
            return
        service = self._reload_target()
        if service is None:
            self.notify(
                "Highlight one platform, enter its exact id, or filter to one result first.",
                title="Choose a service to reload",
                severity="warning",
                timeout=6,
            )
            return

        needle = self.query_one("#filter", Input).value
        try:
            result = self.app.reload_service_code(service)
        except ReloadError as exc:
            self.notify(
                str(exc), title=f"Could not reload {service.NAME}", severity="error", timeout=9
            )
            return

        await self._rebuild_services(needle)
        self.query_one("#filter", Input).focus()
        self.refresh_env()
        count = len(result.modules)
        self.notify(
            f"Reloaded {service.NAME}: {count} module{'s' if count != 1 else ''}, "
            f"generation {result.generation}, {result.seconds:.2f}s. "
            "The next session uses the new code.",
            title="Service code reloaded",
            timeout=7,
        )

    # ------------------------------------------------------------------ events
    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        value = event.value.strip()
        if _is_number(value):
            # digits address a platform rather than describe one, so they point
            # at a cell instead of filtering the list
            self.apply_filter("")
            self._highlight_number(int(value))
            return
        self.apply_filter(value)

    def _highlight_number(self, number: int) -> None:
        wanted = self._numbered.get(number)
        for cells in self._sections:
            for cell in cells:
                if wanted is not None and cell.service is wanted:
                    cell.add_class("targeted")
                    cell.scroll_visible(animate=False)
                else:
                    cell.remove_class("targeted")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        value = event.value.strip()
        if self._import_path(value):
            return
        if _is_number(value):
            service = self._numbered.get(int(value))
            if service is not None:
                self._open(service)
            else:
                self.notify(f"No platform numbered {value}", severity="warning", timeout=3)
            return
        shown = self._shown()
        if shown:
            self._open(shown[0][0].service)

    def _import_path(self, value: str) -> bool:
        """An export file pasted into the box. Returns whether it was one.

        The same box already takes a URL and opens the platform that can deal with
        it, so it is the screen's "here is a thing, handle it" field - and a file
        somebody sent you is exactly that. It also means the import route does not
        depend on the status line having room for its chip, and that a file can be
        finished from wherever it landed instead of being moved first.

        Only a path that exists and reads as an export. Anything else falls through
        to filtering the list, because ".json" in a platform name is not a claim
        about the file system.
        """
        from ..core import exports

        if not value.lower().endswith(".json"):
            return False
        path = Path(value).expanduser()
        if not path.is_file():
            return False
        try:
            document = exports.read(path)
        except exports.ExportError as exc:
            self.notify(f"{path.name}: {exc}", title="Not an export", severity="error", timeout=8)
            return True
        self.query_one("#filter", Input).value = ""
        if self.app.open_import(document):
            self.notify(f"Importing {document.label()}", timeout=4)
        return True

    def on_service_cell_chosen(self, event: ServiceCell.Chosen) -> None:
        event.stop()
        self._open(event.service)

    def on_service_cell_focused(self, event: ServiceCell.Focused) -> None:
        event.stop()
        event.cell.scroll_visible(animate=False)

    def _open(self, service: type[Service]) -> None:
        value = self.query_one("#filter", Input).value.strip()
        target = value if value.lower().startswith(("http://", "https://")) else None
        self.app.open_service(service, target=target)

    # -------------------------------------------------------------- back level
    def go_back(self) -> bool:
        """The bottom of the stack: clear the filter, never pop."""
        filter_input = self.query_one("#filter", Input)
        if filter_input.value:
            filter_input.value = ""
        filter_input.focus()
        return True
