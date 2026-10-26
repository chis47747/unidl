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
from ..core.i18n import setting_label, setting_value, tr
from ..core.settings import Option as SettingOption
from ..core.settings import Setting, Settings, service_license_settings
from .bidi import visual_markup
from .chrome import Chrome, KeyBar
from .settings_layout import label_width, setting_row

# Static declarations shared by the manager and Home's metadata-only search.
SERVICE_MANAGER_ITEMS = (
    ("chapters", "fetch_chapters"),
    ("attachments", "fetch_attachments"),
    ("bitrate", "fetch_actual_bitrate"),
    ("license", "license_action"),
    ("hybrid", "dolby_vision_hybrid"),
    ("register", "register_service"),
    ("home", "home_services"),
    ("export", "export_manifest_type"),
)


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


class _AttachmentPolicyScreen(ModalScreen[None]):
    """Toggle title attachments independently for each registered service."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False), Binding("ctrl+b", "cancel", "Back", show=False)]

    def __init__(self, app) -> None:
        super().__init__()
        self.services = sorted(app.services, key=lambda cls: cls.NAME.casefold())
        spec = next(spec for spec in service_license_settings() if spec.key == "fetch_attachments")
        self.scopes = [Settings(cls.ID, [spec], app.settings_store, parent=app.globals, legacy_ids=cls.LEGACY_IDS) for cls in self.services]

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        with Vertical(id="modal-body"):
            yield Static(tr("service.attachments.title", default="Title attachments by service"), classes="ask-title")
            yield Label(tr("service.attachments.help", default="Select a service to toggle optional title attachments."), classes="ask-hint")
            yield OptionList(id="service-attachments-list")
        yield KeyBar(("enter", "toggle"), ("^b", "back"), ("esc", "back"))

    def on_mount(self) -> None:
        self.rebuild()
        self.query_one("#service-attachments-list", OptionList).focus()

    def rebuild(self) -> None:
        options = self.query_one("#service-attachments-list", OptionList)
        previous = options.highlighted
        options.clear_options()
        width = label_width(cls.NAME for cls in self.services)
        for index, (cls, settings) in enumerate(zip(self.services, self.scopes, strict=True)):
            value = setting_value(settings.spec_by_key["fetch_attachments"], settings)
            options.add_option(Option(setting_row(options, cls.NAME, f"[$accent]{value}[/]", width), id=str(index)))
        if self.services:
            options.highlighted = min(previous or 0, len(self.services) - 1)
        else:
            options.add_option(Option(tr("service.export.empty"), disabled=True))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if 0 <= index < len(self.services):
            settings = self.scopes[index]
            settings.set("fetch_attachments", not bool(settings.get("fetch_attachments")))
            self.rebuild()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


class _BitratePolicyScreen(ModalScreen[None]):
    """Choose a global probe default and independent service overrides."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("r", "reset", "Reset", show=False),
    ]

    def __init__(self, app) -> None:
        super().__init__()
        self.globals = app.globals
        self.services = sorted(app.services, key=lambda cls: cls.NAME.casefold())
        spec = next(spec for spec in service_license_settings() if spec.key == "fetch_actual_bitrate")
        self.scopes = [
            Settings(cls.ID, [spec], app.settings_store, parent=app.globals, legacy_ids=cls.LEGACY_IDS)
            for cls in self.services
        ]

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        with Vertical(id="modal-body"):
            yield Static(tr("service.bitrate.title"), classes="ask-title")
            yield Label(tr("service.bitrate.help"), id="service-bitrate-help", classes="ask-hint")
            yield OptionList(id="service-bitrate-list")
        yield KeyBar(("enter", "toggle"), ("r", "reset"), ("^b", "back"), ("esc", "back"))

    def on_mount(self) -> None:
        self.rebuild()
        self._fit_help()
        self.query_one("#service-bitrate-list", OptionList).focus()

    def on_resize(self) -> None:
        self._fit_help()

    def _fit_help(self) -> None:
        found = self.query("#service-bitrate-help")
        if found:
            hint = found.first(Label)
            # Override the shared single-line ask hint with responsive prose.
            hint.styles.width = "1fr"
            hint.styles.height = "auto"
            hint.styles.max_height = max(2, min(8, self.app.size.height - 7))

    def rebuild(self) -> None:
        options = self.query_one("#service-bitrate-list", OptionList)
        previous = options.highlighted
        options.clear_options()
        labels = [tr("service.bitrate.global"), *(cls.NAME for cls in self.services)]
        width = label_width(labels)
        spec = self.globals.spec_by_key["fetch_actual_bitrate"]
        value = setting_value(spec, self.globals)
        options.add_option(Option(setting_row(options, labels[0], f"[$accent]{value}[/]", width), id="@global"))
        for cls, settings in zip(self.services, self.scopes, strict=True):
            value = setting_value(settings.spec_by_key["fetch_actual_bitrate"], settings)
            source = "settings.service_override" if settings.has_override("fetch_actual_bitrate") else "settings.follows_global"
            row = setting_row(options, cls.NAME, f"[$accent]{value}[/]  [$dim]{tr(source)}[/]", width)
            options.add_option(Option(row, id=cls.ID))
        if not self.services:
            options.add_option(Option(tr("service.export.empty"), disabled=True))
        options.highlighted = min(previous or 0, len(self.services))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if 0 <= index <= len(self.services):
            settings = self.globals if index == 0 else self.scopes[index - 1]
            settings.set("fetch_actual_bitrate", not bool(settings.get("fetch_actual_bitrate")))
            self.rebuild()

    def action_reset(self) -> None:
        index = self.query_one("#service-bitrate-list", OptionList).highlighted
        if index is not None and 0 <= index <= len(self.services):
            settings = self.globals if index == 0 else self.scopes[index - 1]
            settings.clear("fetch_actual_bitrate")
            self.rebuild()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


class _RegistrationEditor(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False), Binding("ctrl+b", "cancel", "Back", show=False)]

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
                text = f"  [$dim]✓  {source.name}  ({source.service_id})[/]"
                options.add_option(Option(text, id=f"registered-{index}", disabled=True))
            else:
                text = f"  [$accent]＋  {source.name}  ({source.service_id})[/]"
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
        if ident.startswith("available-"):
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
    BINDINGS = [Binding("ctrl+b", "back", "Back", show=False), Binding("escape", "back", "Quit", show=False), Binding("enter", "activate", "Change", show=True)]

    def __init__(self, globals_scope: Settings, *, focus_key: str = ""):
        super().__init__()
        self.globals = globals_scope
        self._rows = tuple(ident for ident, _key in SERVICE_MANAGER_ITEMS)
        self._focus_key = focus_key
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
        if self._focus_key in self._rows:
            options = self.query_one("#settings-group-list", OptionList)
            options.highlighted = self._rows.index(self._focus_key)
            options.scroll_to_highlight()

    def on_resize(self) -> None:
        self._fit_help()

    def _fit_help(self) -> None:
        """Give wrapped help all available rows while preserving all choices."""

        found = self.query("#settings-group-help")
        if not found:
            return
        # Reserve room for Chrome, masthead, option rows and the key bar.
        # The rest can belong to the selected setting's prose, up to a
        # readable twelve-row panel.
        found.first(Static).styles.max_height = max(2, min(12, self.app.size.height - len(self._rows) - 4))

    def _refresh_sources(self) -> None:
        self._sources = service_catalog.merge_registry_sources(
            service_catalog.discover_sources(home=self.app.config.paths.home), self.app.registry.all()
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
        summaries = {
            "chapters": tr("service.chapters.summary"),
            "attachments": tr("service.attachments.summary"),
            "bitrate": tr("service.bitrate.summary"),
            "license": setting_value(self.globals.spec_by_key["license_action"], self.globals),
            "hybrid": setting_value(self.globals.spec_by_key["dolby_vision_hybrid"], self.globals),
            "register": tr("service.register.summary", registered=count, available=len(self._sources)),
            "home": tr("service.home.summary", shown=shown, registered=count),
            "export": tr("service.export.summary", media=media, master=max(0, count - media)),
        }
        rows = [(ident, tr(f"service.{ident}.action"), f"[$dim]{summaries[ident]}[/]")
                for ident, _key in SERVICE_MANAGER_ITEMS]
        longest = label_width(label for _, label, _ in rows)
        for ident, label, value in rows:
            options.add_option(Option(setting_row(options, label, value, longest), id=ident))
        options.highlighted = min(previous or 0, len(rows) - 1)
        self._show_help(self._rows[options.highlighted or 0])

    def _show_help(self, ident: str) -> None:
        self.query_one("#settings-group-help", Static).update(tr(f"service.{ident}.help"))

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
        if ident == "attachments":
            self.app.push_screen(_AttachmentPolicyScreen(self.app), lambda _result: self.rebuild())
            return
        if ident == "bitrate":
            self.app.push_screen(_BitratePolicyScreen(self.app), lambda _result: self.rebuild())
            return
        if ident == "license":
            from .settings_screen import _ChoiceEditor

            spec = self.globals.spec_by_key["license_action"]

            def license_done(value) -> None:
                if value is not None:
                    self.globals.set(spec.key, value)
                    self.rebuild()

            self.app.push_screen(_ChoiceEditor(spec, self.globals.get(spec.key)), license_done)
            return
        if ident == "hybrid":
            spec = self.globals.spec_by_key["dolby_vision_hybrid"]
            self.globals.set("dolby_vision_hybrid", not bool(self.globals.get("dolby_vision_hybrid")))
            self.app.notify(
                tr("settings.changed", label=setting_label(spec), value=setting_value(spec, self.globals)),
                timeout=4,
            )
            self.rebuild()
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
                self.app.notify(tr("service.register.done", name=name), title=tr("service.register.title"), timeout=10)
                self.rebuild()

            self.app.push_screen(_RegistrationEditor(self._sources, registered), registered_done)
            return
        if ident == "home":
            registered = service_catalog.registered_ids(self.app.settings_store)
            sources = [item for item in self._sources if item.service_id in registered]
            setting = Setting("home_services", tr("service.home.title"), kind="multi", options=[SettingOption(item.service_id, item.name) for item in sources], default=(), help=tr("service.home.help"))
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
