"""Global service registration and homepage visibility controls."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from ..core import service_catalog
from ..core.i18n import setting_value, tr
from ..core.settings import Option as SettingOption
from ..core.settings import Setting, Settings
from .bidi import visual_markup
from .chrome import Chrome, KeyBar


class _RegistrationEditor(ModalScreen[str | None]):
    """Show source packages and allow one unregistered package to be enabled."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
    ]

    def __init__(self, sources, registered: set[str]) -> None:
        super().__init__()
        self.sources = list(sources)
        self.registered = set(registered)

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        with Vertical(id="modal-body"):
            yield Static(tr("service.register.title"), classes="ask-title")
            yield Label(tr("service.register.help"), classes="ask-hint")
            yield OptionList(id="service-registration-list")
        yield KeyBar(("enter", "register"), ("^b", "back"), ("esc", "cancel"))

    def on_mount(self) -> None:
        options = self.query_one("#service-registration-list", OptionList)
        for index, source in enumerate(self.sources):
            if source.service_id in self.registered:
                text = f"  [$dim]✓  {visual_markup(source.name)}  ({source.service_id})[/]"
                options.add_option(Option(text, id=f"registered-{index}", disabled=True))
            else:
                text = f"  [$accent]＋  {visual_markup(source.name)}  ({source.service_id})[/]"
                options.add_option(Option(text, id=f"available-{index}"))
        if not self.sources:
            options.add_option(Option(f"  [$dim]{tr('service.register.empty')}[/]", disabled=True))
        else:
            for index, source in enumerate(self.sources):
                if source.service_id not in self.registered:
                    options.highlighted = index
                    break
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        ident = str(event.option.id or "")
        if not ident.startswith("available-"):
            return
        index = int(ident.partition("-")[2])
        if 0 <= index < len(self.sources):
            self.dismiss(self.sources[index].service_id)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


class ServicesManagerScreen(Screen[None]):
    """The Services section under global Settings."""

    BINDINGS = [
        Binding("ctrl+b", "back", "Back", show=False),
        Binding("escape", "back", "Quit", show=False),
        Binding("enter", "activate", "Change", show=True),
    ]

    def __init__(self, globals_scope: Settings):
        super().__init__()
        self.globals = globals_scope
        self._rows = ("fetch", "register", "home")
        self._sources = []

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        yield Static(tr("settings.services.title"), id="masthead")
        yield Static(tr("settings.services.help"), id="settings-group-help")
        yield OptionList(id="settings-group-list")
        yield KeyBar(("enter", "change"), ("^b", "back"), ("esc", "back"))

    def on_mount(self) -> None:
        self._refresh_sources()
        self.rebuild()
        self._fit_help()
        self.query_one("#settings-group-list", OptionList).focus()

    def on_resize(self) -> None:
        self._fit_help()

    def _fit_help(self) -> None:
        """Give wrapped help all available rows while preserving the three choices."""

        found = self.query("#settings-group-help")
        if not found:
            return
        # Chrome, masthead, three visible option rows and the key bar need seven
        # rows. The rest can belong to the selected setting's prose, up to a
        # readable twelve-row panel.
        found.first(Static).styles.max_height = max(2, min(12, self.app.size.height - 7))

    def _refresh_sources(self) -> None:
        self._sources = service_catalog.merge_registry_sources(
            service_catalog.discover_sources(), self.app.registry.all()
        )

    def rebuild(self) -> None:
        options = self.query_one("#settings-group-list", OptionList)
        previous = options.highlighted
        options.clear_options()
        registered = service_catalog.registered_ids(self.app.settings_store)
        home = service_catalog.home_ids(self.app.settings_store)
        count = len(registered)
        shown = len(home & registered)
        rows = [
            ("fetch", f"  {tr('setting.fetch_chapters')}  [$accent]{setting_value(self.globals.spec_by_key['fetch_chapters'], self.globals)}[/]"),
            ("register", f"  {tr('service.register.action')}  [$dim]{tr('service.register.summary', registered=count, available=len(self._sources))}[/]"),
            ("home", f"  {tr('service.home.action')}  [$dim]{tr('service.home.summary', shown=shown, registered=count)}[/]"),
        ]
        for ident, text in rows:
            options.add_option(Option(text, id=ident))
        options.highlighted = min(previous or 0, len(rows) - 1)
        self._show_help(self._rows[options.highlighted or 0])

    def _show_help(self, ident: str) -> None:
        keys = {
            "fetch": "setting.fetch_chapters.help",
            "register": "service.register.help",
            "home": "service.home.help",
        }
        self.query_one("#settings-group-help", Static).update(tr(keys[ident]))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        if event.option.id in self._rows:
            self._show_help(str(event.option.id))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_activate()

    def action_activate(self) -> None:
        options = self.query_one("#settings-group-list", OptionList)
        ident = str(options.get_option_at_index(options.highlighted or 0).id or "")
        if ident == "fetch":
            self.globals.set("fetch_chapters", not bool(self.globals.get("fetch_chapters")))
            self.rebuild()
            self.app.notify(tr("settings.changed", label=tr("setting.fetch_chapters"), value=setting_value(self.globals.spec_by_key["fetch_chapters"], self.globals)), timeout=4)
            return
        self._refresh_sources()
        if ident == "register":
            registered = service_catalog.registered_ids(self.app.settings_store)

            def registered_done(service_id: str | None) -> None:
                if not service_id:
                    return
                source = next((item for item in self._sources if item.service_id == service_id), None)
                service_catalog.update_registration(self.app.settings_store, service_id, True)
                name = source.name if source else service_id
                self.app.notify(
                    tr("service.register.done", name=name),
                    title=tr("service.register.title"),
                    timeout=10,
                )
                self.rebuild()

            self.app.push_screen(_RegistrationEditor(self._sources, registered), registered_done)
            return
        if ident == "home":
            registered = service_catalog.registered_ids(self.app.settings_store)
            sources = [item for item in self._sources if item.service_id in registered]
            setting = Setting(
                "home_services",
                tr("service.home.title"),
                kind="multi",
                options=[SettingOption(item.service_id, item.name) for item in sources],
                default=(),
                help=tr("service.home.help"),
            )
            from .settings_screen import _MultiChoiceEditor

            def home_done(value: Any) -> None:
                if value is None:
                    return
                service_catalog.update_home(self.app.settings_store, value)
                self.app.set_home_services(value)
                self.rebuild()

            self.app.push_screen(_MultiChoiceEditor(setting, service_catalog.home_ids(self.app.settings_store)), home_done)

    def action_back(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


__all__ = ["ServicesManagerScreen"]
