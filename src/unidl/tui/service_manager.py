"""Global service registration and homepage visibility controls."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from ..core import exports, service_catalog
from ..core.i18n import setting_value, tr
from ..core.settings import Option as SettingOption
from ..core.settings import Setting, Settings, service_license_settings
from .bidi import visual_markup
from .chrome import Chrome, KeyBar
from .settings_layout import label_width, setting_row


class _ChapterPolicyScreen(ModalScreen[None]):
    """Toggle chapter metadata independently for each registered service."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
    ]

    def __init__(self, app) -> None:
        super().__init__()
        self.services = sorted(app.services, key=lambda cls: cls.NAME.casefold())
        chapter_spec = next(spec for spec in service_license_settings() if spec.key == "fetch_chapters")
        self.scopes = [
            Settings(cls.ID, [chapter_spec], app.settings_store, parent=app.globals, legacy_ids=cls.LEGACY_IDS)
            for cls in self.services
        ]

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        with Vertical(id="modal-body"):
            yield Static(tr("service.chapters.title", default="Chapter metadata by service"), classes="ask-title")
            yield Label(tr("service.chapters.help", default="Select a service to toggle optional chapter metadata."), classes="ask-hint")
            yield OptionList(id="service-chapters-list")
        yield KeyBar(("enter", "toggle"), ("^b", "back"), ("esc", "back"))

    def on_mount(self) -> None:
        self.rebuild()
        self.query_one("#service-chapters-list", OptionList).focus()

    def rebuild(self) -> None:
        options = self.query_one("#service-chapters-list", OptionList)
        previous = options.highlighted
        options.clear_options()
        labels = [cls.NAME for cls in self.services]
        width = label_width(labels)
        for index, (cls, settings) in enumerate(zip(self.services, self.scopes, strict=True)):
            value = setting_value(settings.spec_by_key["fetch_chapters"], settings)
            options.add_option(Option(setting_row(options, cls.NAME, f"[$accent]{value}[/]", width), id=str(index)))
        if self.services:
            options.highlighted = min(previous or 0, len(self.services) - 1)
        else:
            options.add_option(Option(tr("service.export.empty"), disabled=True))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if index < 0 or index >= len(self.services):
            return
        settings = self.scopes[index]
        settings.set("fetch_chapters", not bool(settings.get("fetch_chapters")))
        self.rebuild()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


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


class _ExportManifestEditor(ModalScreen[None]):
    """Choose master/media export policy independently for each service."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
    ]

    def __init__(self, sources, store) -> None:
        super().__init__()
        self.sources = list(sources)
        self.store = store

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        with Vertical(id="modal-body"):
            yield Static(tr("service.export.title"), classes="ask-title")
            yield Label(tr("service.export.help"), classes="ask-hint")
            yield OptionList(id="service-export-list")
        yield KeyBar(("enter", "change"), ("^b", "back"), ("esc", "cancel"))

    def on_mount(self) -> None:
        self.rebuild()
        self.query_one("#service-export-list", OptionList).focus()

    def rebuild(self) -> None:
        options = self.query_one("#service-export-list", OptionList)
        previous = options.highlighted
        options.clear_options()
        for index, source in enumerate(self.sources):
            mode = service_catalog.export_manifest_type(
                self.store,
                source.service_id,
            )
            label = tr(f"service.export.mode.{mode}")
            options.add_option(
                Option(
                    f"  {visual_markup(source.name)}  [$accent]{visual_markup(label)}[/]",
                    id=f"service-{index}",
                )
            )
        if not self.sources:
            options.add_option(
                Option(
                    f"  [$dim]{tr('service.export.empty')}[/]",
                    disabled=True,
                )
            )
        else:
            options.highlighted = min(previous or 0, len(self.sources) - 1)

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        event.stop()
        ident = str(event.option.id or "")
        if not ident.startswith("service-"):
            return
        index = int(ident.partition("-")[2])
        if index < 0 or index >= len(self.sources):
            return
        source = self.sources[index]
        setting = Setting(
            exports.EXPORT_MANIFEST_TYPE_KEY,
            tr("service.export.mode.title", service=source.name),
            kind="choice",
            options=[
                SettingOption(
                    exports.MASTER_MANIFEST,
                    tr("service.export.mode.master"),
                ),
                SettingOption(
                    exports.MEDIA_MANIFEST,
                    tr("service.export.mode.media"),
                ),
            ],
            default=exports.MASTER_MANIFEST,
            help=tr("service.export.mode.help"),
        )
        from .settings_screen import _ChoiceEditor

        def chosen(value: Any) -> None:
            if value is None:
                return
            service_catalog.update_export_manifest_type(
                self.store,
                source.service_id,
                value,
            )
            self.rebuild()
            self.query_one("#service-export-list", OptionList).highlighted = index
            self.app.notify(
                tr(
                    "service.export.changed",
                    service=source.name,
                    mode=tr(
                        f"service.export.mode.{service_catalog.export_manifest_type(self.store, source.service_id)}"
                    ),
                ),
                timeout=5,
            )

        self.app.push_screen(
            _ChoiceEditor(
                setting,
                service_catalog.export_manifest_type(self.store, source.service_id),
            ),
            chosen,
        )

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
        self._rows = ("chapters", "register", "home", "export")
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
        """Give wrapped help all available rows while preserving all choices."""

        found = self.query("#settings-group-help")
        if not found:
            return
        # Chrome, masthead, four visible option rows and the key bar need eight
        # rows. The rest can belong to the selected setting's prose, up to a
        # readable twelve-row panel.
        found.first(Static).styles.max_height = max(2, min(12, self.app.size.height - 8))

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
        media = sum(
            service_catalog.export_manifest_type(self.app.settings_store, service_id)
            == exports.MEDIA_MANIFEST
            for service_id in registered
        )
        rows = [
            ("chapters", tr('service.chapters.action', default='Chapter metadata by service'), f"[$dim]{tr('service.chapters.summary', default='Independent on/off per registered service')}[/]"),
            ("register", tr('service.register.action'), f"[$dim]{tr('service.register.summary', registered=count, available=len(self._sources))}[/]"),
            ("home", tr('service.home.action'), f"[$dim]{tr('service.home.summary', shown=shown, registered=count)}[/]"),
            (
                "export",
                tr('service.export.action'),
                f"[$dim]{tr('service.export.summary', media=media, master=max(0, count - media))}[/]",
            ),
        ]
        longest = label_width(label for _, label, _ in rows)
        for ident, label, value in rows:
            options.add_option(Option(setting_row(options, label, value, longest), id=ident))
        options.highlighted = min(previous or 0, len(rows) - 1)
        self._show_help(self._rows[options.highlighted or 0])

    def _show_help(self, ident: str) -> None:
        keys = {
            "chapters": "service.chapters.help",
            "register": "service.register.help",
            "home": "service.home.help",
            "export": "service.export.help",
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
        if ident == "chapters":
            self.app.push_screen(_ChapterPolicyScreen(self.app), lambda _result: self.rebuild())
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
            return
        if ident == "export":
            registered = service_catalog.registered_ids(self.app.settings_store)
            sources = [item for item in self._sources if item.service_id in registered]
            self.app.push_screen(
                _ExportManifestEditor(sources, self.app.settings_store),
                lambda _value: self.rebuild(),
            )

    def action_back(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


__all__ = ["ServicesManagerScreen"]
