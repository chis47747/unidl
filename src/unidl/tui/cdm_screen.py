"""Pick a CDM device.

Reached by clicking ``cdm`` on the main screen. Lists what is actually on disk
under the project's ``cdm/`` folder - ``cdm/widevine/*.wvd`` and
``cdm/playready/*.prd`` - plus anything named in the config file and anything
under ``cdm.search_paths``, so an existing pile of devices does not have to be
copied to become selectable.

Two folders rather than one so a ``.prd`` and a ``.wvd`` are never sitting side
by side wondering which system they belong to.

The choice is not written into the config file. Config is the "set it once"
surface; this is a thing you flip while working, so it goes to settings.json
alongside quality and codec.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import OptionList, Static, Tab, Tabs
from textual.widgets.option_list import Option

from ..core.cdm import PLAYREADY, WIDEVINE, DeviceFile
from ..core.cdmrules import known_devices
from ..core.drm import all_systems, get
from ..core.i18n import tr
from .chrome import Chrome, KeyBar, refresh_locale_widgets
from .filterbox import FilterBox, as_number, matches
from .input import ClipboardInput as Input

SYSTEM_LABEL = {WIDEVINE: "Widevine", PLAYREADY: "PlayReady"}

#: The row that clears a per-service choice. A device with no name and no path is
#: not a device, which is exactly what "no opinion here, follow the app-wide
#: choice" means - and it dismisses as "", the value that setting holds.
APP_WIDE = DeviceFile()


def _system_label(system_id: str) -> str:
    """A system's display name, from the registry rather than a local table."""
    system = get(system_id)
    return system.label if system else SYSTEM_LABEL.get(system_id, system_id)


def _esc(text: str) -> str:
    """Neutralise square brackets in a file name.

    Real device files are called things like ``[L3] OnePlus, CPH2493``, and an
    unescaped ``[`` in Textual markup is a style tag, so the list refuses to
    render at all.
    """
    return str(text).replace("\\", "\\\\").replace("[", r"\[")


class CdmScreen(Screen[str | None]):
    """Returns the chosen device name, or None if nothing changed.

    Two callers, and the difference between them is only what the answer is
    *for*: the main screen sets the application's device, a service's settings
    set that one service's. Both are "find one device among a hundred and twenty",
    which is a search box and a numbered list - reproducing that as a plain
    option list in the settings screen would have been a hundred-row scroll.
    """

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "noop", "Use this device", show=True),
        Binding("ctrl+o", "reveal", "Open the folder", show=True),
        Binding("ctrl+e", "edit_remote", "Manage CDMs", show=True),
        Binding("ctrl+r", "reload", "Reload devices", show=True),
        # the filter has the focus, so list movement is forwarded from here
        Binding("up", "cursor(-1)", "Up", show=False),
        Binding("down", "cursor(1)", "Down", show=False),
        Binding("pageup", "cursor(-10)", "Page up", show=False),
        Binding("pagedown", "cursor(10)", "Page down", show=False),
    ]

    def action_noop(self) -> None:
        """Key-bar hint only; Enter is handled by the focused list."""

    def __init__(
        self,
        current: str = "",
        system: str = WIDEVINE,
        *,
        only: str = "",
        allow_default: bool = False,
        scope: str = "",
        note: str = "",
    ):
        super().__init__()
        self.current = current
        self.system = system
        #: what the masthead says after "CDM device", when neither "<service> only"
        #: nor "<system> is active" is what this choice is for. A rule about
        #: resolution is picking a device for a *condition*, and saying which
        #: condition is the only thing that tells two identical-looking visits apart.
        self.note = note
        #: restrict the list to one system's devices. Set when the choice being
        #: made is "the PlayReady device for this service" - offering a .wvd there
        #: would be offering a device that cannot answer the question.
        self.only = only
        #: show the row that means "no opinion here, follow the app-wide choice"
        self.allow_default = allow_default
        #: what this choice applies to, for the masthead
        self.scope = scope
        #: one entry per list row; None marks a section heading
        self._rows: list[DeviceFile | None] = []
        self._devices: list[DeviceFile] = []
        self._source = "all"
        self._needle = ""
        #: device name -> its stable number, so a number keeps its meaning
        self._numbers: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        yield Static("", id="masthead")
        yield Static("", id="subhead")
        yield Tabs(
            Tab(tr("cdm.tab.all"), id="source-all"),
            Tab(tr("cdm.tab.local"), id="source-local"),
            Tab(tr("cdm.tab.remote"), id="source-remote"),
            id="cdm-source",
        )
        with Horizontal(id="statusline"):
            yield Static("", id="crumbs-pill", classes="pill")
        with Vertical(id="body"):
            yield OptionList(id="cdm-list")
            yield FilterBox(hint=tr("filter.hint.device"), id="cdm-filter")
        yield KeyBar(
            ("enter", "use this device"),
            ("↑↓", "move"),
            ("tab", "local / remote"),
            ("^e", "manage CDMs"),
            ("^r", "reload"),
            ("^o", "open the cdm folder"),
            ("^b", "back"),
            ("esc", "quit"),
        )

    def on_mount(self) -> None:
        self._render_masthead()
        self._load_devices()
        self.rebuild()
        self.query_one("#cdm-filter", FilterBox).focus()

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self.query_one("#source-all", Tab).label = tr("cdm.tab.all")
        self.query_one("#source-local", Tab).label = tr("cdm.tab.local")
        self.query_one("#source-remote", Tab).label = tr("cdm.tab.remote")
        self.query_one("#cdm-filter", FilterBox).placeholder = tr("filter.hint.device")
        self._render_masthead()
        self._load_devices()
        self.rebuild(self._needle)

    def _render_masthead(self) -> None:
        note = self.note or (
            tr("cdm.scope_only", scope=self.scope)
            if self.scope
            else tr("cdm.system_active", system=_system_label(self.system))
        )
        self.query_one("#masthead", Static).update(f"{tr('cdm.title')}  [$dim]{_esc(note)}[/]")

    def _load_devices(self) -> None:
        """Read local files and configured remote endpoints into one picker."""
        folder = self.app.config.paths.cdm
        config_name = self.app.config.source.name if self.app.config.source else "unidl.yaml"
        # Through the one function that knows how a device becomes visible, which is
        # what the settings list and the quality rules already use. This screen built
        # its own list - the search paths and the remote endpoints - and so left out
        # the devices named in unidl.yaml that live somewhere else: `cdm.default`
        # resolves those by name, so the app would use a device this picker could not
        # offer.
        everything = known_devices(self.app.config)
        if self.only:
            everything = [d for d in everything if d.system == self.only]
        # Remote CDMs first, then the files. Both are things you pick, and a
        # remote one is often the only device for its system.
        self._devices = [d for d in everything if d.is_remote] + [
            d for d in everything if not d.is_remote
        ]
        remote_count = sum(device.is_remote for device in self._devices)
        # numbered once, over every device, so filtering never moves a number
        self._numbers = {device.name: index + 1 for index, device in enumerate(self._devices)}
        self.query_one("#subhead", Static).update(
            f"[$muted]{tr('cdm.subhead.local')}[/] [$accent]{_esc(folder)}[/]  "
            f"[$muted]{tr('cdm.subhead.remote')}[/] "
            f"[$accent]{tr('cdm.subhead.remote_n', count=remote_count, config=_esc(config_name))}[/]"
        )

    def _source_devices(self) -> list[DeviceFile]:
        if self._source == "remote":
            return [device for device in self._devices if device.is_remote]
        if self._source == "local":
            return [device for device in self._devices if not device.is_remote]
        return list(self._devices)

    # ------------------------------------------------------------------ render
    def rebuild(self, needle: str = "") -> None:
        """Draw the list, narrowed to ``needle``.

        There are over a hundred device files on a machine that has been
        collecting them, and paging through that to find one is not a way to
        choose anything.
        """
        option_list = self.query_one("#cdm-list", OptionList)
        option_list.clear_options()
        self._rows = []

        if self.allow_default:
            # first, and always visible: it is how a per-service choice is undone,
            # and filtering it away would leave no way back to the default
            self._rows.append(APP_WIDE)
            mark = "" if self.current else f"  [$ok]{tr('cdm.in_use')}[/]"
            option_list.add_option(Option(f"       [$muted]{tr('cdm.app_wide')}[/]{mark}"))

        devices = self._source_devices()
        if not devices:
            detail = tr("cdm.hint.remote") if self._source == "remote" else tr("cdm.hint.local")
            # "all" is the name of a tab, not a kind of device: on that tab this
            # said "no all devices found", which reads as a bug in the sentence.
            kind = "" if self._source == "all" else f"{self._source} "
            if self.only:
                missing = tr(
                    "cdm.no_system_devices",
                    system=_system_label(self.only),
                    kind=kind,
                )
            elif kind:
                missing = tr("cdm.no_kind_devices", kind=kind)
            else:
                missing = tr("cdm.no_devices")
            self.query_one("#crumbs-pill", Static).update(f"[$warn]{missing} - {detail}[/]")
            option_list.add_option(Option(f"  [$dim]{tr('cdm.nothing')}[/]", disabled=True))
            return

        shown = [
            device
            for device in devices
            if matches(
                needle,
                device.name,
                device.level,
                device.problem,
                device.where,
            )
        ]
        self._render_count(shown, devices, needle)

        if not shown:
            option_list.add_option(
                Option(f"  [$dim]{tr('cdm.no_match', query=_esc(needle))}[/]", disabled=True)
            )
            return

        # every registered system, not a hardcoded pair: a MonaLisa device was
        # discovered, counted and then never drawn, so iq's device was unpickable
        for system in [s.id for s in all_systems()]:
            group = [device for device in shown if device.system == system]
            if not group:
                continue
            self._rows.append(None)
            note = "" if system == self.system or self.only else tr("cdm.not_active")
            option_list.add_option(
                Option(
                    f"  [$muted]{_system_label(system).upper()}[/][$dim]{note}[/]",
                    disabled=True,
                )
            )
            for device in group:
                self._rows.append(device)
                option_list.add_option(
                    Option(
                        self._row_markup(device, self._numbers[device.name]),
                        disabled=not device.usable,
                    )
                )

        self._highlight(self.current)

    def _render_count(
        self, shown: list[DeviceFile], devices: list[DeviceFile], needle: str
    ) -> None:
        total = len(devices)
        counts = [
            (system.id, sum(1 for d in devices if d.system == system.id))
            for system in all_systems()
        ]
        summary = " · ".join(
            f"{count} {_system_label(system)}" for system, count in counts if count
        )
        local_count = sum(not device.is_remote for device in devices)
        remote_count = total - local_count
        source_summary = tr("cdm.local_remote", local=local_count, remote=remote_count)
        summary = f"{source_summary}  ·  {summary}" if summary else source_summary
        if needle.strip():
            summary = tr("cdm.shown", shown=len(shown), total=total, summary=summary)
        self.query_one("#crumbs-pill", Static).update(f"[$dim]{summary}[/]")

    def _highlight(self, name: str) -> None:
        option_list = self.query_one("#cdm-list", OptionList)
        for index, row in enumerate(self._rows):
            if row is not None and row.name == name:
                option_list.highlighted = index
                option_list.scroll_to_highlight()
                return
        option_list.highlighted = next(
            (index for index, row in enumerate(self._rows) if row is not None), 0
        )

    def _row_markup(self, device: DeviceFile, number: int) -> str:
        active = device.name == self.current
        # theme variables rather than the dark palette's literals, which drew this
        # list in near-white on a white terminal
        colour = "$accent" if active else "$fg2"
        mark = f"  [$ok]{tr('cdm.in_use')}[/]" if active else ""
        if device.problem:
            level = f"[$warn]{tr('cdm.unusable')}[/]"
        else:
            level = f"[$muted]{device.level}[/]" if device.level else ""
        # where it came from matters: two devices can share a stem, and a remote
        # CDM is worth telling apart from a file at a glance
        where = _esc(
            tr("cdmrules.remote", where=device.where) if device.is_remote else device.where
        )
        return (
            f"  [$gutter]{number:>3}[/]  "
            f"[{colour}]{_esc(device.name)}[/]  {level}  "
            f"[$dim]{where}[/]{mark}"
        )

    # ------------------------------------------------------------------ events
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # `option_index`, not `index`: the constructor's parameter is called
        # index but the attribute it sets is option_index
        event.stop()
        self._take(event.option_index)

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        number = as_number(event.value)
        if number is not None:
            # numbers address a device, so they clear the filter rather than
            # being searched for
            self._needle = ""
            self.rebuild("")
            for name, assigned in self._numbers.items():
                if assigned == number:
                    self._highlight(name)
                    break
            return
        self._needle = event.value
        self.rebuild(self._needle)

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        event.stop()
        self._source = str(event.tab.id or "source-all").removeprefix("source-")
        self.rebuild(self._needle)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._take(self.query_one("#cdm-list", OptionList).highlighted)

    def _take(self, index: int | None) -> None:
        if index is None or index >= len(self._rows):
            return
        device = self._rows[index]
        if device is None:
            return
        if not device.usable:
            self.notify(
                tr("cdm.unusable_detail", error=device.problem),
                severity="error",
                timeout=7,
            )
            return
        self.dismiss(device.name)

    def action_cursor(self, delta: int) -> None:
        """Move the list while the filter field has the focus."""
        option_list = self.query_one("#cdm-list", OptionList)
        count = option_list.option_count
        if not count:
            return
        current = option_list.highlighted
        start = current if current is not None else (-1 if delta > 0 else count)
        option_list.highlighted = max(0, min(count - 1, start + delta))

    def action_reveal(self) -> None:
        self.app.open_path(self.app.config.paths.cdm)

    def action_edit_remote(self) -> None:
        from .resource_manager import ResourceManagerScreen

        def refreshed(_result) -> None:
            self._load_devices()
            self.rebuild(self._needle)

        self.app.push_screen(ResourceManagerScreen(), refreshed)

    def action_reload(self) -> None:
        try:
            self.app.config.reload()
            self.app.apply_device_choice()
            self._load_devices()
            self.rebuild(self._needle)
        except (OSError, ValueError) as exc:
            self.notify(tr("cdm.reload_fail", error=exc), severity="error", timeout=7)
            return
        self.notify(tr("cdm.reloaded"), timeout=3)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


__all__ = ["APP_WIDE", "CdmScreen"]
