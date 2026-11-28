"""Global service registration and homepage visibility controls."""

from __future__ import annotations

from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..core import exports, service_catalog
from ..core.i18n import phrase, setting_label, setting_value, tr
from ..core.service_repo import (
    RepoSyncResult,
    ServiceRepoSpec,
    parse_service_repos,
    refresh_service_repos,
)
from ..core.settings import Option as SettingOption
from ..core.settings import Setting, Settings, service_license_settings
from .bidi import escape_markup, visual_markup
from .chrome import Chrome, CloseMark, KeyBar, fit_modal_card
from .input import ClipboardInput as Input
from .settings_layout import label_width, setting_row
from .vault_targets import VaultCheckbox

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
    ("repos", "service_repos"),
    ("cookies", "browser_cookies"),
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
        with Vertical(classes="service-policy-card"):
            with Horizontal(classes="modal-head"):
                yield Label(tr("service.chapters.title", default="Chapter metadata by service"), classes="modal-card-title")
                yield CloseMark(id="service-modal-close")
            yield Label(tr("service.chapters.help", default="Select a service to toggle optional chapter metadata."), classes="modal-card-help")
            yield OptionList(id="service-chapters-list")
            with Horizontal(classes="modal-card-actions"):
                yield Button(phrase("Close"), id="service-modal-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    def on_mount(self) -> None:
        self.rebuild()
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        self.query_one("#service-chapters-list", OptionList).focus()

    def on_resize(self) -> None:
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)

    def _fit_modal(self) -> None:
        fit_modal_card(self, ".service-policy-card", "#service-chapters-list")

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
        with Vertical(classes="service-policy-card"):
            with Horizontal(classes="modal-head"):
                yield Label(tr("service.attachments.title", default="Title attachments by service"), classes="modal-card-title")
                yield CloseMark(id="service-modal-close")
            yield Label(tr("service.attachments.help", default="Select a service to toggle optional title attachments."), classes="modal-card-help")
            yield OptionList(id="service-attachments-list")
            with Horizontal(classes="modal-card-actions"):
                yield Button(phrase("Close"), id="service-modal-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    def on_mount(self) -> None:
        self.rebuild()
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        self.query_one("#service-attachments-list", OptionList).focus()

    def on_resize(self) -> None:
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)

    def _fit_modal(self) -> None:
        fit_modal_card(self, ".service-policy-card", "#service-attachments-list")

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
        with Vertical(classes="service-policy-card"):
            with Horizontal(classes="modal-head"):
                yield Label(tr("service.bitrate.title"), classes="modal-card-title")
                yield CloseMark(id="service-modal-close")
            yield Label(tr("service.bitrate.help"), id="service-bitrate-help", classes="modal-card-help")
            yield OptionList(id="service-bitrate-list")
            with Horizontal(classes="modal-card-actions"):
                yield Button(phrase("reset"), id="service-bitrate-reset")
                yield Button(phrase("Close"), id="service-modal-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "service-bitrate-reset":
            self.action_reset()
        else:
            self.action_cancel()

    def on_mount(self) -> None:
        self.rebuild()
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        self.query_one("#service-bitrate-list", OptionList).focus()

    def on_resize(self) -> None:
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)

    def _fit_modal(self) -> None:
        fit_modal_card(self, ".service-policy-card", "#service-bitrate-list")

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
        with Vertical(classes="service-policy-card"):
            with Horizontal(classes="modal-head"):
                yield Label(tr("service.register.title"), classes="modal-card-title")
                yield CloseMark(id="service-modal-close")
            yield Label(tr("service.register.help"), classes="modal-card-help")
            yield OptionList(id="service-registration-list")
            with Horizontal(classes="modal-card-actions"):
                yield Button(phrase("Cancel"), id="service-modal-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    def on_mount(self) -> None:
        options = self.query_one("#service-registration-list", OptionList)
        for index, source in enumerate(self.sources):
            name = visual_markup(source.name)
            sid = visual_markup(source.service_id)
            if source.service_id in self.registered:
                text = f"  [$dim]✓  {name}  ({sid})[/]"
                options.add_option(Option(text, id=f"registered-{index}", disabled=True))
            else:
                text = f"  [$accent]＋  {name}  ({sid})[/]"
                options.add_option(Option(text, id=f"available-{index}"))
        if not self.sources:
            options.add_option(Option(f"  [$dim]{tr('service.register.empty')}[/]", disabled=True))
        else:
            for index, source in enumerate(self.sources):
                if source.service_id not in self.registered:
                    options.highlighted = index
                    break
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        options.focus()

    def on_resize(self) -> None:
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)

    def _fit_modal(self) -> None:
        fit_modal_card(self, ".service-policy-card", "#service-registration-list")

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
        with Vertical(classes="service-policy-card"):
            with Horizontal(classes="modal-head"):
                yield Label(tr("service.export.title"), classes="modal-card-title")
                yield CloseMark(id="service-modal-close")
            yield Label(tr("service.export.help"), classes="modal-card-help")
            yield OptionList(id="service-export-list")
            with Horizontal(classes="modal-card-actions"):
                yield Button(phrase("Close"), id="service-modal-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    def on_mount(self) -> None:
        self.rebuild()
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        self.query_one("#service-export-list", OptionList).focus()

    def on_resize(self) -> None:
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)

    def _fit_modal(self) -> None:
        fit_modal_card(self, ".service-policy-card", "#service-export-list")

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


def _repo_spec_to_entry(spec: ServiceRepoSpec) -> dict[str, Any]:
    entry: dict[str, Any] = {"url": spec.url, "enabled": spec.enabled}
    if spec.name:
        entry["name"] = spec.name
    if spec.branch:
        entry["branch"] = spec.branch
    if spec.subdir:
        entry["subdir"] = spec.subdir
    if spec.ttl_hours != 24.0:
        entry["ttl_hours"] = spec.ttl_hours
    return entry


class _ServiceRepoEditorModal(ModalScreen[dict[str, Any] | None]):
    """Add or edit one Git-backed external service repository."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def __init__(self, spec: ServiceRepoSpec | None = None) -> None:
        super().__init__()
        self.spec = spec

    def compose(self) -> ComposeResult:
        spec = self.spec
        ttl_value = (
            str(int(spec.ttl_hours) if float(spec.ttl_hours).is_integer() else spec.ttl_hours)
            if spec is not None
            else "24"
        )
        title = (
            tr("service.repos.edit_title", default="Edit Git service repository")
            if spec is not None
            else tr("service.repos.add_title", default="Add Git service repository")
        )
        with Vertical(id="repo-editor-card", classes="proxy-editor-card"):
            with Horizontal(classes="modal-head"):
                yield Label(title, classes="proxy-editor-title")
                yield CloseMark(id="repo-editor-close")
            yield Label(
                tr(
                    "service.repos.edit_help",
                    default="Clones under <home>/service_repos/<name> and skips pull if local edits exist.",
                ),
                classes="proxy-editor-help",
            )
            with VerticalScroll(classes="proxy-editor-fields"):
                yield Label(tr("service.repos.field.url", default="Repository URL"), classes="proxy-field-label")
                yield Input(
                    value=spec.url if spec is not None else "",
                    placeholder="https://github.com/org/unidl-services.git",
                    id="repo-url",
                )
                yield Label(
                    tr("service.repos.field.name", default="Local folder name (optional)"),
                    classes="proxy-field-label",
                )
                yield Input(
                    value=spec.name if spec is not None else "",
                    placeholder="auto from URL",
                    id="repo-name",
                )
                yield Label(
                    tr("service.repos.field.branch", default="Branch or tag (optional)"),
                    classes="proxy-field-label",
                )
                yield Input(
                    value=spec.branch if spec is not None else "",
                    placeholder="main",
                    id="repo-branch",
                )
                yield Label(
                    tr("service.repos.field.subdir", default="Subdirectory inside repo (optional)"),
                    classes="proxy-field-label",
                )
                yield Input(
                    value=spec.subdir if spec is not None else "",
                    placeholder="services",
                    id="repo-subdir",
                )
                yield Label(
                    tr("service.repos.field.ttl", default="Refresh cache TTL (hours)"),
                    classes="proxy-field-label",
                )
                yield Input(value=ttl_value, placeholder="24", id="repo-ttl")
                yield VaultCheckbox(
                    tr("service.repos.field.enabled", default="Enabled"),
                    value=spec.enabled if spec is not None else True,
                    id="repo-enabled",
                )
            yield Static("", id="repo-editor-error")
            with Horizontal(classes="proxy-editor-actions"):
                yield Button(phrase("Save"), variant="primary", id="repo-editor-save")
                yield Button(phrase("Cancel"), id="repo-editor-cancel")

    def on_mount(self) -> None:
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        self.query_one("#repo-url", Input).focus()

    def on_resize(self) -> None:
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)

    def _fit_modal(self) -> None:
        fit_modal_card(self, ".proxy-editor-card", ".proxy-editor-fields")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "repo-editor-save":
            self.action_save()
        else:
            self.action_cancel()

    def _fail(self, message: str, selector: str) -> None:
        self.query_one("#repo-editor-error", Static).update(f"[$bad]{visual_markup(message)}[/]")
        self.query_one(selector, Input).focus()

    def action_save(self) -> None:
        url = self.query_one("#repo-url", Input).value.strip()
        if not url:
            self._fail(tr("service.repos.need_url", default="Enter a Git repository URL."), "#repo-url")
            return
        raw_ttl = self.query_one("#repo-ttl", Input).value.strip()
        if not raw_ttl:
            ttl_hours = 24.0
        else:
            try:
                ttl_hours = float(raw_ttl)
            except ValueError:
                self._fail(
                    tr("service.repos.bad_ttl", default="TTL hours must be a non-negative number."),
                    "#repo-ttl",
                )
                return
            if ttl_hours < 0:
                self._fail(
                    tr("service.repos.bad_ttl", default="TTL hours must be a non-negative number."),
                    "#repo-ttl",
                )
                return
        entry: dict[str, Any] = {
            "url": url,
            "enabled": bool(self.query_one("#repo-enabled", Checkbox).value),
        }
        name = self.query_one("#repo-name", Input).value.strip()
        branch = self.query_one("#repo-branch", Input).value.strip()
        subdir = self.query_one("#repo-subdir", Input).value.strip().strip("/\\")
        if name:
            entry["name"] = name
        if branch:
            entry["branch"] = branch
        if subdir:
            entry["subdir"] = subdir
        if ttl_hours != 24.0:
            entry["ttl_hours"] = int(ttl_hours) if ttl_hours.is_integer() else ttl_hours
        self.dismiss(entry)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.action_cancel()
        return True


class _ServiceReposModal(ModalScreen[None]):
    """Manage Git-backed external service repositories and trigger safe syncs."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("a", "add", "Add", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("space", "toggle", "Toggle", show=False),
        Binding("d", "delete", "Delete", show=False),
        Binding("r", "refresh_repos", "Refresh", show=False),
        Binding("f", "force_refresh_repos", "Force refresh", show=False),
    ]

    def __init__(self, app) -> None:
        super().__init__()
        self._app_ref = app
        self._specs: list[ServiceRepoSpec] = []
        self._syncing = False

    def compose(self) -> ComposeResult:
        with Vertical(id="service-repos-card", classes="service-policy-card"):
            with Horizontal(classes="modal-head"):
                yield Label(
                    tr("service.repos.title", default="Git service repositories"),
                    classes="modal-card-title",
                )
                yield CloseMark(id="service-modal-close")
            yield Label(
                tr(
                    "service.repos.help",
                    default="Add external Git repositories containing UniDL service packages, toggle them, or sync updates safely.",
                ),
                classes="modal-card-help",
            )
            yield OptionList(id="service-repos-list")
            yield Static("", id="service-repos-status", classes="modal-card-help")
            with Horizontal(classes="modal-card-actions"):
                yield Button(tr("common.add", default="Add"), variant="primary", id="repo-add")
                yield Button(phrase("Edit"), id="repo-edit")
                yield Button(tr("common.toggle", default="Toggle"), id="repo-toggle")
                yield Button(phrase("Delete"), id="repo-delete")
            with Horizontal(classes="modal-card-actions"):
                yield Button(tr("service.repos.refresh", default="Refresh"), id="repo-refresh")
                yield Button(tr("service.repos.force_refresh", default="Force refresh"), id="repo-force")
                yield Button(phrase("Close"), id="repo-close")

    def on_mount(self) -> None:
        self._reload_specs()
        self.rebuild()
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        self.query_one("#service-repos-list", OptionList).focus()

    def on_resize(self) -> None:
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)

    def _fit_modal(self) -> None:
        fit_modal_card(self, ".service-policy-card", "#service-repos-list")

    def _reload_specs(self) -> None:
        raw = (
            self.app.config.service_repos()
            if callable(getattr(self.app.config, "service_repos", None))
            else []
        )
        self._specs = parse_service_repos(raw)

    def _sync_buttons(self) -> None:
        has_items = bool(self._specs)
        for ident in ("#repo-edit", "#repo-toggle", "#repo-delete"):
            self.query_one(ident, Button).disabled = not has_items or self._syncing
        for ident in ("#repo-refresh", "#repo-force"):
            self.query_one(ident, Button).disabled = not has_items or self._syncing
        self.query_one("#repo-add", Button).disabled = self._syncing

    def rebuild(self, *, highlight: int | None = None) -> None:
        options = self.query_one("#service-repos-list", OptionList)
        previous = options.highlighted if highlight is None else highlight
        options.clear_options()
        if not self._specs:
            options.add_option(
                Option(
                    f"  [$dim]{escape_markup(tr('service.repos.empty', default='No Git service repositories configured yet. Click Add (a) to add one.'))}[/]",
                    disabled=True,
                )
            )
        else:
            for index, spec in enumerate(self._specs):
                glyph = "[$ok]✓[/]" if spec.enabled else "[$dim]○[/]"
                meta_parts: list[str] = []
                if spec.branch:
                    meta_parts.append(spec.branch)
                if spec.subdir:
                    meta_parts.append(spec.subdir)
                ttl_label = int(spec.ttl_hours) if float(spec.ttl_hours).is_integer() else spec.ttl_hours
                meta_parts.append(f"TTL {ttl_label}h")
                meta = " · ".join(meta_parts)
                row = (
                    f"  {glyph}  [$accent]{escape_markup(spec.name)}[/]"
                    f"  [$dim]{escape_markup(meta)}[/]  {escape_markup(spec.url)}"
                )
                options.add_option(Option(row, id=f"repo-{index}"))
            options.highlighted = min(max(0, previous or 0), len(self._specs) - 1)
        self._sync_buttons()

    def _selected_index(self) -> int | None:
        if not self._specs:
            return None
        idx = self.query_one("#service-repos-list", OptionList).highlighted
        if idx is None or idx < 0 or idx >= len(self._specs):
            return None
        return idx

    def _save_entries(self, entries: list[dict[str, Any]], *, highlight: int | None = None) -> None:
        self.app.config.save_service_repos(entries)
        self._reload_specs()
        self.rebuild(highlight=highlight)
        self.notify(tr("service.repos.saved", default="Saved Git service repositories"), timeout=4)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if self._specs:
            self.action_edit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        ident = event.button.id or ""
        if ident == "repo-add":
            self.action_add()
        elif ident == "repo-edit":
            self.action_edit()
        elif ident == "repo-toggle":
            self.action_toggle()
        elif ident == "repo-delete":
            self.action_delete()
        elif ident == "repo-refresh":
            self.action_refresh_repos()
        elif ident == "repo-force":
            self.action_force_refresh_repos()
        else:
            self.action_cancel()

    def action_add(self) -> None:
        if self._syncing:
            return

        def _added(entry: dict[str, Any] | None) -> None:
            if not entry:
                return
            entries = [_repo_spec_to_entry(spec) for spec in self._specs]
            entries.append(entry)
            self._save_entries(entries, highlight=len(entries) - 1)

        self.app.push_screen(_ServiceRepoEditorModal(), _added)

    def action_edit(self) -> None:
        if self._syncing:
            return
        index = self._selected_index()
        if index is None:
            return

        def _edited(entry: dict[str, Any] | None) -> None:
            if not entry:
                return
            entries = [_repo_spec_to_entry(spec) for spec in self._specs]
            if 0 <= index < len(entries):
                entries[index] = entry
                self._save_entries(entries, highlight=index)

        self.app.push_screen(_ServiceRepoEditorModal(self._specs[index]), _edited)

    def action_toggle(self) -> None:
        if self._syncing:
            return
        index = self._selected_index()
        if index is None:
            return
        entries = [_repo_spec_to_entry(spec) for spec in self._specs]
        entries[index]["enabled"] = not bool(entries[index].get("enabled", True))
        self._save_entries(entries, highlight=index)

    def action_delete(self) -> None:
        if self._syncing:
            return
        index = self._selected_index()
        if index is None:
            return
        entries = [_repo_spec_to_entry(spec) for spec in self._specs]
        entries.pop(index)
        self._save_entries(entries, highlight=max(0, index - 1))

    def action_refresh_repos(self) -> None:
        self._start_sync(force=False)

    def action_force_refresh_repos(self) -> None:
        self._start_sync(force=True)

    def _start_sync(self, *, force: bool) -> None:
        if self._syncing or not self._specs:
            return
        self._syncing = True
        self._sync_buttons()
        self.query_one("#service-repos-status", Static).update(
            f"[$dim]{escape_markup(tr('service.repos.syncing', default='Syncing Git service repositories…'))}[/]"
        )
        self._sync_worker(force)

    @work(thread=True, exclusive=True)
    def _sync_worker(self, force: bool) -> None:
        try:
            results = refresh_service_repos(self.app.config, force=force)
        except Exception as exc:  # noqa: BLE001
            try:
                self.app.call_from_thread(self._on_sync_error, str(exc))
            except RuntimeError:
                pass
            return
        try:
            self.app.call_from_thread(self._on_sync_done, results)
        except RuntimeError:
            pass

    def _on_sync_error(self, message: str) -> None:
        if not self.is_attached:
            return
        self._syncing = False
        self._sync_buttons()
        self.query_one("#service-repos-status", Static).update(
            f"[$bad]{escape_markup(message)}[/]"
        )
        self.notify(message, severity="error", timeout=8)

    def _on_sync_done(self, results: list[RepoSyncResult]) -> None:
        if not self.is_attached:
            return
        self._syncing = False
        self._reload_specs()
        self.rebuild()
        ok_count = sum(1 for item in results if item.ok)
        fail_count = len(results) - ok_count
        changed = any(item.ok and item.action in {"cloned", "updated"} for item in results)
        lines = [
            f"[{'$ok' if item.ok else '$warn'}][{escape_markup(item.action)}][/] "
            f"{escape_markup(item.spec.name)} — {escape_markup(item.detail)}"
            for item in results[:4]
        ]
        hint = ""
        if changed:
            hint = tr(
                "service.repos.restart_hint",
                default="New services can now be registered under 'Register a service'; restart UniDL to load updated packages.",
            )
            lines.append(f"[$dim]{escape_markup(hint)}[/]")
        self.query_one("#service-repos-status", Static).update("\n".join(lines))
        summary = tr(
            "service.repos.sync_done",
            total=len(results),
            ok=ok_count,
            failed=fail_count,
            default=f"Synced {len(results)} repo(s): {ok_count} ok, {fail_count} issue(s)",
        )
        if hint:
            summary = f"{summary}. {hint}"
        self.notify(
            summary,
            severity="warning" if fail_count else "information",
            timeout=8 if hint else 6,
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


class ServicesManagerScreen(Screen[None]):
    BINDINGS = [
        Binding("b", "back", "Back", show=False),
        Binding("ctrl+b", "back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "activate", "Change", show=True),
    ]

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
        yield KeyBar(("enter", "change"), ("^b", "back"), ("esc", "quit"))

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

    def relocalize(self) -> None:
        from .chrome import refresh_locale_widgets

        refresh_locale_widgets(self)
        self.query_one("#masthead", Static).update(tr("settings.services.title"))
        self.rebuild()

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
            service_catalog.discover_sources(
                home=self.app.config.paths.home,
                config=self.app.config,
            ),
            self.app.registry.all(),
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
        repo_specs = (
            parse_service_repos(self.app.config.service_repos())
            if callable(getattr(self.app.config, "service_repos", None))
            else []
        )
        enabled_repos = sum(1 for spec in repo_specs if spec.enabled)
        summaries = {
            "chapters": tr("service.chapters.summary"),
            "attachments": tr("service.attachments.summary"),
            "bitrate": tr("service.bitrate.summary"),
            "license": setting_value(self.globals.spec_by_key["license_action"], self.globals),
            "hybrid": setting_value(self.globals.spec_by_key["dolby_vision_hybrid"], self.globals),
            "register": tr("service.register.summary", registered=count, available=len(self._sources)),
            "home": tr("service.home.summary", shown=shown, registered=count),
            "export": tr("service.export.summary", media=media, master=max(0, count - media)),
            "repos": tr(
                "service.repos.summary",
                enabled=enabled_repos,
                total=len(repo_specs),
                default=f"{enabled_repos} / {len(repo_specs)} enabled",
            ),
            "cookies": tr("service.cookies.summary", default="Import from Firefox"),
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
            return
        if ident == "repos":
            def repos_done(_value: Any) -> None:
                self._refresh_sources()
                self.rebuild()

            self.app.push_screen(_ServiceReposModal(self.app), repos_done)
            return
        if ident == "cookies":
            from .cookie_extract_modal import FirefoxCookieExtractModal

            registered = service_catalog.registered_ids(self.app.settings_store)
            cookie_ids = {
                cls.ID: (cls.COOKIE_SERVICE_ID or cls.ID)
                for cls in getattr(self.app, "services", ())
            }
            ordered = sorted(
                self._sources,
                key=lambda item: (item.service_id not in registered, item.name.casefold()),
            )
            choices: list[tuple[str, str]] = []
            seen_cookie_ids: set[str] = set()
            for item in ordered:
                cid = cookie_ids.get(item.service_id, item.service_id)
                if cid in seen_cookie_ids:
                    continue
                seen_cookie_ids.add(cid)
                choices.append((cid, item.name))
            self.app.push_screen(
                FirefoxCookieExtractModal(service_choices=choices),
                lambda _value: self.rebuild(),
            )

    def action_back(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


__all__ = ["ServicesManagerScreen"]
