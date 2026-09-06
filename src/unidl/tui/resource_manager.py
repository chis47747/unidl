"""One place to manage CDM endpoints and key-vault backends.

The original settings split one practical job across a device picker, YAML, two
vault safety switches and three target lists. This screen owns the *resources*
themselves: remote CDMs and remote/local vault definitions are edited here and
saved to the project YAML; the vault policy is available from the same screen.

It intentionally does not edit a local CDM file. A device file is credential
material, not configuration, and deleting or modifying it from a generic form
would be surprising. The standard CDM picker remains the safe place to choose
which local or remote device is active.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    Label,
    OptionList,
    Select,
    Static,
    Tab,
    Tabs,
    TextArea,
)
from textual.widgets.option_list import Option

from ..core import cdmrules, vaults
from ..core.drm import all_systems
from ..core.i18n import phrase, tr
from ..core.settings import Settings
from .bidi import visual_markup
from .chrome import Chrome, KeyBar, refresh_locale_widgets
from .input import ClipboardInput as Input
from .vault_targets import VaultCheckbox, VaultTargetResult, VaultTargetScreen

_NAME_RE = re.compile(r"^[^\s][^\n\r]*$")


def _text(value: object) -> str:
    return str(value or "").strip()


def _bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _normalised_entries(value: object) -> list[dict[str, Any]]:
    """Return a mutable list for either documented YAML list/mapping spelling."""
    if isinstance(value, dict):
        return [
            {**dict(entry), "name": name}
            for name, entry in value.items()
            if isinstance(entry, dict)
        ]
    if isinstance(value, list):
        return [dict(entry) for entry in value if isinstance(entry, dict)]
    return []


def _pairs(value: object) -> dict[str, str]:
    """Parse a forgiving service-map field without accepting ambiguous lines."""
    found: dict[str, str] = {}
    for part in re.split(r"[,\n]+", _text(value)):
        item = part.strip()
        if not item:
            continue
        pieces = re.split(r"\s*[:=]\s*", item, maxsplit=1)
        if len(pieces) != 2 or not pieces[0].strip() or not pieces[1].strip():
            raise ValueError(f"service map entry {item!r} must look like source=destination")
        source, target = pieces
        found[source.strip().lower()] = target.strip().lower()
    return found


def _map_text(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return "\n".join(
        f"{source}={target}"
        for source, target in value.items()
        if _text(source) and _text(target)
    )


def _service_list(value: object) -> list[str]:
    found: list[str] = []
    for part in re.split(r"[,\s]+", _text(value)):
        item = part.strip().lower()
        if item and item not in found:
            found.append(item)
    return found


def _masked(value: object) -> str:
    text = _text(value)
    return f"{text[:4]}…{text[-4:]}" if len(text) > 9 else ("stored" if text else "missing")


@dataclass(frozen=True)
class ResourceRow:
    """One selectable manager row; headings are represented by ``None``."""

    kind: str
    name: str
    detail: str
    enabled: bool = True
    entry: dict[str, Any] | None = None

    @property
    def identity(self) -> tuple[str, str]:
        return self.kind, self.name.casefold()


class _DeleteConfirm(ModalScreen[bool]):
    """A deliberately small confirmation before removing a configured backend."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Cancel", show=False),
    ]

    def __init__(self, noun: str, name: str):
        super().__init__()
        self.noun = noun
        self.name = name

    def compose(self) -> ComposeResult:
        with Vertical(id="resource-confirm-card"):
            yield Label(tr("resource.delete_title", noun=self.noun), id="resource-confirm-title")
            yield Static(
                tr("resource.delete_help", name=f"[$warn]{visual_markup(self.name)}[/]"),
                id="resource-confirm-help",
            )
            with Horizontal(id="resource-confirm-actions"):
                yield Button(phrase("Delete"), variant="error", id="resource-confirm-delete")
                yield Button(phrase("Cancel"), id="resource-confirm-cancel")

    def on_mount(self) -> None:
        self.query_one("#resource-confirm-cancel", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "resource-confirm-delete")


class _RemoteCdmEditor(ModalScreen[dict[str, Any] | None]):
    """Add or edit one remote pywidevine/pyplayready CDM endpoint."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    _KNOWN = {
        "name", "system", "device_name", "device", "device_type", "system_id",
        "security_level", "host", "secret", "token", "key", "timeout", "enabled",
    }

    def __init__(self, existing: dict[str, Any] | None = None):
        super().__init__()
        self.existing = dict(existing or {})
        self.editing = bool(existing)

    def compose(self) -> ComposeResult:
        systems = [(system.label, system.id) for system in all_systems() if system.remote_capable]
        default_system = _text(self.existing.get("system")) or "widevine"
        if default_system not in {value for _label, value in systems}:
            systems.append((tr("resource.configured", system=default_system), default_system))
        title = tr("resource.edit_cdm") if self.editing else tr("resource.add_cdm")
        with Vertical(id="resource-editor-card"):
            yield Label(title, id="resource-editor-title")
            yield Label(tr("resource.cdm_help"), id="resource-editor-help")
            with VerticalScroll(id="resource-editor-fields"):
                yield Label(tr("resource.field.name"), classes="resource-field-label")
                yield Input(value=_text(self.existing.get("name")), id="resource-name")
                yield Label(tr("resource.field.system"), classes="resource-field-label")
                yield Select(
                    systems,
                    value=default_system,
                    allow_blank=False,
                    compact=True,
                    id="resource-cdm-system",
                )
                yield Label(tr("resource.field.device_name"), classes="resource-field-label")
                yield Input(
                    value=_text(self.existing.get("device_name") or self.existing.get("device")),
                    placeholder=tr("resource.placeholder.device"),
                    id="resource-device-name",
                )
                yield Label(tr("resource.field.address"), classes="resource-field-label")
                yield Input(
                    value=_text(self.existing.get("host")),
                    placeholder=tr("resource.placeholder.address"),
                    id="resource-address",
                )
                yield Label(
                    tr("resource.field.secret_cdm")
                    + (tr("resource.field.secret_keep") if self.editing else ""),
                    classes="resource-field-label",
                )
                yield Input(password=True, placeholder=tr("resource.placeholder.secret"), id="resource-secret")
                yield Label(tr("resource.field.device_type"), classes="resource-field-label")
                yield Input(
                    value=_text(self.existing.get("device_type")),
                    placeholder=tr("resource.placeholder.device_type"),
                    id="resource-device-type",
                )
                yield Label(tr("resource.field.system_id"), classes="resource-field-label")
                yield Input(value=_text(self.existing.get("system_id")), id="resource-system-id")
                yield Label(tr("resource.field.security"), classes="resource-field-label")
                yield Input(
                    value=_text(self.existing.get("security_level")),
                    placeholder=tr("resource.placeholder.level"),
                    id="resource-security-level",
                )
                yield Label(tr("resource.field.timeout"), classes="resource-field-label")
                yield Input(value=_text(self.existing.get("timeout") or "30"), id="resource-timeout")
            yield Static("", id="resource-editor-error")
            with Horizontal(id="resource-editor-actions"):
                yield Button(phrase("Save"), variant="primary", id="resource-editor-save")
                yield Button(phrase("Cancel"), id="resource-editor-cancel")

    def on_mount(self) -> None:
        self.query_one("#resource-name", Input).focus()

    def _field(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _fail(self, message: str, selector: str = "#resource-name") -> None:
        self.query_one("#resource-editor-error", Static).update(f"[$bad]{visual_markup(message)}[/]")
        self.query_one(selector, Input).focus()

    def action_save(self) -> None:
        name = self._field("#resource-name")
        device_name = self._field("#resource-device-name")
        address = self._field("#resource-address").rstrip("/")
        secret = self._field("#resource-secret")
        if not _NAME_RE.match(name):
            self._fail(tr("resource.need_name_cdm"))
            return
        if not device_name:
            self._fail(tr("resource.need_device"), "#resource-device-name")
            return
        if not _valid_url(address):
            self._fail(tr("resource.need_url"), "#resource-address")
            return
        old_secret = _text(self.existing.get("secret") or self.existing.get("token") or self.existing.get("key"))
        secret = secret or old_secret
        if not secret:
            self._fail(tr("resource.need_secret_cdm"), "#resource-secret")
            return
        try:
            system_id = _optional_int(self._field("#resource-system-id"), tr("resource.label.system_id"))
            level = _optional_int(self._field("#resource-security-level"), tr("resource.label.security"))
            timeout = _optional_float(self._field("#resource-timeout"), tr("resource.label.timeout"))
        except ValueError as exc:
            self._fail(str(exc))
            return
        result = {key: value for key, value in self.existing.items() if key not in self._KNOWN}
        result.update(
            {
                "name": name,
                "system": str(self.query_one("#resource-cdm-system", Select).value),
                "device_name": device_name,
                "host": address,
                "secret": secret,
                # Availability is toggled by the manager row, so the form has
                # one unambiguous enable/disable control rather than two.
                "enabled": _bool(self.existing.get("enabled"), True),
            }
        )
        device_type = self._field("#resource-device-type")
        if device_type:
            result["device_type"] = device_type
        if system_id is not None:
            result["system_id"] = system_id
        if level is not None:
            result["security_level"] = level
        if timeout is not None:
            result["timeout"] = timeout
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "resource-editor-save":
            self.action_save()
        else:
            self.action_cancel()


class _RemoteVaultEditor(ModalScreen[dict[str, Any] | None]):
    """Add or edit one HTTP/API vault without ever rendering its token again."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    _KNOWN = {
        "type", "name", "host", "uri", "password", "api_key", "token", "secret", "username",
        "api_mode", "searchable", "search_keys", "supported_services", "service_map", "no_push",
        "timeout", "enabled",
    }

    def __init__(self, existing: dict[str, Any] | None = None):
        super().__init__()
        self.existing = dict(existing or {})
        self.editing = bool(existing)

    def compose(self) -> ComposeResult:
        raw_type = _text(self.existing.get("type")).lower()
        vault_type = "api" if raw_type == "api" else "http"
        title = tr("resource.edit_vault") if self.editing else tr("resource.add_vault")
        services = self.existing.get("supported_services") or []
        services_text = ", ".join(str(value) for value in services) if isinstance(services, list) else _text(services)
        with Vertical(id="resource-editor-card"):
            yield Label(title, id="resource-editor-title")
            yield Label(tr("resource.vault_help"), id="resource-editor-help")
            with VerticalScroll(id="resource-editor-fields"):
                yield Label(tr("resource.field.name"), classes="resource-field-label")
                yield Input(value=_text(self.existing.get("name")), id="resource-name")
                yield Label(tr("resource.field.backend"), classes="resource-field-label")
                yield Select(
                    [(tr("resource.backend.http"), "http"), (tr("resource.backend.api"), "api")],
                    value=vault_type,
                    allow_blank=False,
                    compact=True,
                    id="resource-vault-type",
                )
                yield Label(tr("resource.field.address"), classes="resource-field-label")
                yield Input(
                    value=_text(self.existing.get("host") or self.existing.get("uri")),
                    placeholder=tr("resource.placeholder.vault_url"),
                    id="resource-address",
                )
                yield Label(
                    tr("resource.field.secret_vault")
                    + (tr("resource.field.secret_keep") if self.editing else ""),
                    classes="resource-field-label",
                )
                yield Input(password=True, placeholder=tr("resource.placeholder.token"), id="resource-secret")
                yield Label(tr("resource.field.api_mode"), classes="resource-field-label")
                yield Select(
                    [(tr("resource.api.json"), "json"), (tr("resource.api.query"), "query")],
                    value=_text(self.existing.get("api_mode")) or "json",
                    allow_blank=False,
                    compact=True,
                    id="resource-api-mode",
                )
                yield Label(tr("resource.field.username"), classes="resource-field-label")
                yield Input(value=_text(self.existing.get("username")), id="resource-username")
                yield VaultCheckbox(
                    tr("resource.searchable"),
                    value=_bool(self.existing.get("searchable") or self.existing.get("search_keys"), False),
                    id="resource-searchable",
                )
                yield Label(tr("resource.field.services"), classes="resource-field-label")
                yield Input(
                    value=services_text,
                    placeholder=tr("resource.placeholder.services"),
                    id="resource-supported-services",
                )
                yield Label(tr("resource.field.map"), classes="resource-field-label")
                yield TextArea(_map_text(self.existing.get("service_map")), soft_wrap=False, tab_behavior="focus", id="resource-service-map")
                yield Label(tr("resource.field.timeout"), classes="resource-field-label")
                yield Input(value=_text(self.existing.get("timeout") or "15"), id="resource-timeout")
                yield VaultCheckbox(
                    tr("resource.read_only"),
                    value=_bool(self.existing.get("no_push"), False),
                    id="resource-read-only",
                )
            yield Static("", id="resource-editor-error")
            with Horizontal(id="resource-editor-actions"):
                yield Button(phrase("Save"), variant="primary", id="resource-editor-save")
                yield Button(phrase("Cancel"), id="resource-editor-cancel")

    def on_mount(self) -> None:
        self.query_one("#resource-name", Input).focus()

    def _field(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _fail(self, message: str, selector: str = "#resource-name") -> None:
        self.query_one("#resource-editor-error", Static).update(f"[$bad]{visual_markup(message)}[/]")
        self.query_one(selector, Input).focus()

    def action_save(self) -> None:
        name = self._field("#resource-name")
        address = self._field("#resource-address").rstrip("/")
        kind = str(self.query_one("#resource-vault-type", Select).value)
        mode = str(self.query_one("#resource-api-mode", Select).value)
        secret = self._field("#resource-secret")
        if not _NAME_RE.match(name):
            self._fail(tr("resource.need_name_vault"))
            return
        if not _valid_url(address):
            self._fail(tr("resource.need_url"), "#resource-address")
            return
        old_secret = _text(
            self.existing.get("password")
            or self.existing.get("api_key")
            or self.existing.get("token")
            or self.existing.get("secret")
        )
        secret = secret or old_secret
        if not secret:
            self._fail(tr("resource.need_secret_vault"), "#resource-secret")
            return
        username = self._field("#resource-username")
        if kind == "http" and mode == "query" and not username:
            self._fail(tr("resource.need_username"), "#resource-username")
            return
        searchable = self.query_one("#resource-searchable", Checkbox).value
        services = _service_list(self._field("#resource-supported-services"))
        if searchable and kind != "http":
            self._fail(tr("resource.search_http_only"))
            return
        if searchable and not services:
            self._fail(tr("resource.need_services"), "#resource-supported-services")
            return
        try:
            mapping = _pairs(self.query_one("#resource-service-map", TextArea).text)
            timeout = _optional_float(self._field("#resource-timeout"), tr("resource.label.timeout"))
        except ValueError as exc:
            self._fail(str(exc), "#resource-service-map")
            return
        result = {key: value for key, value in self.existing.items() if key not in self._KNOWN}
        result.update(
            {
                "type": "api" if kind == "api" else "HTTP",
                "name": name,
                # The list's Toggle action is the single enable/disable control.
                "enabled": _bool(self.existing.get("enabled"), True),
                "no_push": self.query_one("#resource-read-only", Checkbox).value,
            }
        )
        if kind == "api":
            result.update({"uri": address, "token": secret})
        else:
            result.update({"host": address, "password": secret, "api_mode": mode})
            if username:
                result["username"] = username
            if searchable:
                result["searchable"] = True
                result["supported_services"] = services
            elif services:
                # A service list can still restrict normal playback lookup even
                # when home search is off, so retain it rather than throwing it
                # away just because the search toggle is not checked.
                result["supported_services"] = services
        if mapping:
            result["service_map"] = mapping
        if timeout is not None:
            result["timeout"] = timeout
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "resource-editor-save":
            self.action_save()
        else:
            self.action_cancel()


class VaultPolicyScreen(ModalScreen[bool]):
    """The old vault switches/target pickers, made one coherent policy panel."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "apply", "Apply", show=False, priority=True),
    ]

    def __init__(self, globals_scope: Settings):
        super().__init__()
        self.globals = globals_scope
        self.values = {
            "local_vault": bool(globals_scope.get("local_vault", True)),
            "remote_vault": bool(globals_scope.get("remote_vault", False)),
            "vault_read_targets": str(globals_scope.get("vault_read_targets", "") or ""),
            "vault_write_targets": str(globals_scope.get("vault_write_targets", "") or ""),
            "vault_search_targets": str(globals_scope.get("vault_search_targets", "") or ""),
        }

    def compose(self) -> ComposeResult:
        with Vertical(id="resource-policy-card"):
            yield Label(tr("resource.policy.title"), id="resource-policy-title")
            yield Label(tr("resource.policy.help"), id="resource-policy-help")
            yield Static(tr("resource.policy.gates"), classes="resource-policy-section")
            with Horizontal(id="resource-policy-gates"):
                with Vertical(classes="resource-policy-gate"):
                    yield VaultCheckbox(
                        tr("resource.policy.local"),
                        value=self.values["local_vault"],
                        id="resource-policy-local",
                    )
                    yield Static(tr("resource.policy.local_note"), classes="resource-policy-gate-note")
                with Vertical(classes="resource-policy-gate"):
                    yield VaultCheckbox(
                        tr("resource.policy.remote"),
                        value=self.values["remote_vault"],
                        id="resource-policy-remote",
                    )
                    yield Static(tr("resource.policy.remote_note"), classes="resource-policy-gate-note")
            yield Static(tr("resource.policy.destinations"), classes="resource-policy-section")
            with Horizontal(classes="resource-policy-row"):
                with Vertical(classes="resource-policy-target-copy"):
                    yield Label(tr("resource.policy.read"), classes="resource-policy-target-title")
                    yield Static("", id="resource-policy-read")
                yield Button(phrase("Select"), id="resource-policy-read-button")
            with Horizontal(classes="resource-policy-row"):
                with Vertical(classes="resource-policy-target-copy"):
                    yield Label(tr("resource.policy.write"), classes="resource-policy-target-title")
                    yield Static("", id="resource-policy-write")
                yield Button(phrase("Select"), id="resource-policy-write-button")
            with Horizontal(classes="resource-policy-row"):
                with Vertical(classes="resource-policy-target-copy"):
                    yield Label(tr("resource.policy.search"), classes="resource-policy-target-title")
                    yield Static("", id="resource-policy-search")
                yield Button(phrase("Select"), id="resource-policy-search-button")
            with Horizontal(id="resource-policy-actions"):
                yield Button(phrase("Apply"), variant="primary", id="resource-policy-apply")
                yield Button(phrase("Cancel"), id="resource-policy-cancel")

    def on_mount(self) -> None:
        self._refresh_labels()
        self.query_one("#resource-policy-local", Checkbox).focus()

    def _descriptors(self, kind: str):
        descriptors = vaults.configured_vaults(self.app.config)
        if kind == "write":
            return [descriptor for descriptor in descriptors if descriptor.writable]
        if kind == "search":
            return [descriptor for descriptor in descriptors if descriptor.searchable]
        return descriptors

    def _summary(self, key: str, kind: str) -> str:
        descriptors = self._descriptors(kind)
        selected = vaults.parse_targets(self.values[key])
        if selected is None:
            return tr("resource.policy.all", count=sum(descriptor.enabled for descriptor in descriptors))
        if not selected:
            return tr("resource.policy.disabled")
        names = [descriptor.name for descriptor in descriptors if descriptor.name.casefold() in {name.casefold() for name in selected}]
        extra = tr("resource.policy.more", count=len(names) - 2) if len(names) > 2 else ""
        return " + ".join(names[:2]) + extra or tr("resource.policy.disabled")

    def _refresh_labels(self) -> None:
        self.query_one("#resource-policy-read", Static).update(self._summary("vault_read_targets", "read"))
        self.query_one("#resource-policy-write", Static).update(self._summary("vault_write_targets", "write"))
        self.query_one("#resource-policy-search", Static).update(self._summary("vault_search_targets", "search"))

    def _pick(self, key: str, kind: str) -> None:
        def done(result: VaultTargetResult | None) -> None:
            if result is not None:
                self.values[key] = vaults.serialize_targets(result.selected)
                self._refresh_labels()

        self.app.push_screen(
            VaultTargetScreen(
                self._descriptors(kind),
                vaults.parse_targets(self.values[key]),
                title={
                    "read": tr("resource.pick.read"),
                    "write": tr("resource.pick.write"),
                    "search": tr("resource.pick.search"),
                }[kind],
                writable_only=kind == "write",
            ),
            done,
        )

    def action_apply(self) -> None:
        self.values["local_vault"] = self.query_one("#resource-policy-local", Checkbox).value
        self.values["remote_vault"] = self.query_one("#resource-policy-remote", Checkbox).value
        for key, value in self.values.items():
            self.globals.set(key, value)
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button = event.button.id or ""
        if button == "resource-policy-read-button":
            self._pick("vault_read_targets", "read")
        elif button == "resource-policy-write-button":
            self._pick("vault_write_targets", "write")
        elif button == "resource-policy-search-button":
            self._pick("vault_search_targets", "search")
        elif button == "resource-policy-apply":
            self.action_apply()
        else:
            self.action_cancel()


class ResourceManagerScreen(Screen[None]):
    """Manage remote CDMs plus local/remote vault backends from global settings."""

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "activate", "Use / edit", show=True),
        Binding("ctrl+n", "add", "Add remote", show=True),
        Binding("ctrl+i", "add_local", "Import local DBs", show=True),
        Binding("ctrl+e", "edit", "Edit", show=True),
        Binding("ctrl+d", "toggle", "Enable / disable", show=True),
        Binding("ctrl+x", "delete", "Delete", show=True),
        Binding("ctrl+p", "policy", "Vault policy", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._tab = "cdm"
        self._rows: list[ResourceRow | None] = []

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        yield Static(tr("resource.title"), id="masthead")
        yield Static("", id="resource-manager-help")
        yield Tabs(
            Tab(tr("resource.tab.cdm"), id="resource-tab-cdm"),
            Tab(tr("resource.tab.vault"), id="resource-tab-vault"),
            id="resource-tabs",
        )
        with Horizontal(id="statusline"):
            yield Static("", id="crumbs-pill", classes="pill")
        with Vertical(id="resource-manager-body"):
            yield OptionList(id="resource-list")
            with HorizontalScroll(id="resource-manager-actions"):
                yield Button(tr("resource.use_cdm"), id="resource-use")
                yield Button(tr("resource.add_remote"), id="resource-add")
                yield Button(tr("resource.import_local"), id="resource-add-local")
                yield Button(tr("resource.edit"), id="resource-edit")
                yield Button(tr("resource.toggle"), id="resource-toggle")
                yield Button(tr("resource.delete"), id="resource-delete")
                yield Button(tr("resource.policy_button"), id="resource-policy")
        yield KeyBar(
            ("enter", "use / edit"),
            ("^n", "add remote"),
            ("^i", "import local DBs"),
            ("^e", "edit"),
            ("^d", "enable / disable"),
            ("^x", "delete"),
            ("^p", "vault policy"),
            ("^b", "back"),
            ("esc", "quit"),
        )

    def on_mount(self) -> None:
        self.rebuild()
        self.query_one("#resource-list", OptionList).focus()

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self.query_one("#masthead", Static).update(tr("resource.title"))
        self.query_one("#resource-tab-cdm", Tab).label = tr("resource.tab.cdm")
        self.query_one("#resource-tab-vault", Tab).label = tr("resource.tab.vault")
        self.query_one("#resource-add", Button).label = tr("resource.add_remote")
        self.query_one("#resource-add-local", Button).label = tr("resource.import_local")
        self.query_one("#resource-edit", Button).label = tr("resource.edit")
        self.query_one("#resource-toggle", Button).label = tr("resource.toggle")
        self.query_one("#resource-delete", Button).label = tr("resource.delete")
        self.query_one("#resource-policy", Button).label = tr("resource.policy_button")
        self.rebuild()

    # --------------------------------------------------------------- data rows
    def _remote_cdms(self) -> list[dict[str, Any]]:
        raw = self.app.config.raw
        return _normalised_entries(raw.get("remote_cdm") if "remote_cdm" in raw else raw.get("remote_cdms"))

    def _vault_entries(self) -> list[dict[str, Any]]:
        raw = self.app.config.raw
        return _normalised_entries(raw.get("key_vaults") if "key_vaults" in raw else raw.get("vaults"))

    def _cdm_rows(self) -> list[ResourceRow | None]:
        current = self.app.config.device_name_for("") or tr("resource.automatic")
        rows: list[ResourceRow | None] = [
            None,
            ResourceRow(
                "drm-system",
                tr("resource.row.drm"),
                self.app.globals.label_for("drm_system"),
                True,
            ),
            ResourceRow(
                "cdm-rules",
                tr("resource.row.rules"),
                cdmrules.summary(cdmrules.parse(self.app.globals.get("cdm_rules"))),
                True,
            ),
            ResourceRow("cdm-choice", current, tr("resource.row.choose"), True),
        ]
        local_count = sum(not device.is_remote for device in self.app.devices())
        rows.append(
            ResourceRow(
                "cdm-local",
                tr("resource.row.local_files"),
                tr("resource.row.local_detail", count=local_count, path=self.app.config.paths.cdm),
                True,
            )
        )
        remote = self._remote_cdms()
        if remote:
            rows.append(None)
            for entry in remote:
                name = _text(entry.get("name") or entry.get("device_name") or "remote cdm")
                system = _text(entry.get("system")) or tr("resource.automatic_system")
                level = _text(entry.get("security_level"))
                host = urlparse(_text(entry.get("host"))).netloc or _text(entry.get("host"))
                detail = " · ".join(
                    bit for bit in (system, tr("resource.level", level=level) if level else "", host) if bit
                )
                rows.append(ResourceRow("cdm-remote", name, detail, _bool(entry.get("enabled"), True), entry))
        return rows

    def _vault_rows(self) -> list[ResourceRow | None]:
        entries = self._vault_entries()
        if not entries:
            entries = [{"type": "sqlite", "name": "local"}]
        local: list[ResourceRow] = []
        remote: list[ResourceRow] = []
        for entry in entries:
            kind = _text(entry.get("type")).lower() or "sqlite"
            name = _text(entry.get("name")) or kind
            enabled = _bool(entry.get("enabled"), True)
            is_remote = kind in {"http", "httpapi", "api"}
            if is_remote:
                address = _text(entry.get("host") or entry.get("uri"))
                host = urlparse(address).netloc or address
                flags = [kind.upper(), host]
                if _bool(entry.get("searchable") or entry.get("search_keys"), False):
                    flags.append(tr("targets.flag.search"))
                if _bool(entry.get("no_push"), False):
                    flags.append(tr("targets.flag.readonly"))
                remote.append(ResourceRow("vault-remote", name, " · ".join(flag for flag in flags if flag), enabled, entry))
            else:
                path = _text(entry.get("path")) or str(self.app.config.paths.keys_db)
                extra = f" · {tr('targets.flag.readonly')}" if _bool(entry.get("no_push"), False) else ""
                local.append(
                    ResourceRow(
                        "vault-local",
                        name,
                        f"SQLite · {Path(path).name}{extra}",
                        enabled,
                        entry,
                    )
                )
        # Discovery is intentionally read-only until the user imports a file.
        # Showing these rows makes every project database visible without silently
        # adding it to the runtime vault chain.
        for candidate in vaults.discovered_local_vaults(self.app.config):
            local.append(
                ResourceRow(
                    "vault-discovered",
                    candidate.name,
                    f"{candidate.detail} · {tr('resource.not_imported')}",
                    False,
                    {
                        "type": "sqlite",
                        "name": candidate.name,
                        "path": str(candidate.path),
                        "enabled": True,
                        "_discovered": True,
                    },
                )
            )
        rows: list[ResourceRow | None] = []
        if local:
            rows.append(None)
            rows.extend(local)
        if remote:
            rows.append(None)
            rows.extend(remote)
        return rows

    # ------------------------------------------------------------------ render
    def rebuild(self) -> None:
        option_list = self.query_one("#resource-list", OptionList)
        previous = self._selected()
        wanted = previous.identity if previous is not None else None
        option_list.clear_options()
        self._rows = self._cdm_rows() if self._tab == "cdm" else self._vault_rows()
        section = tr("resource.section.cdm") if self._tab == "cdm" else tr("resource.section.vault")
        help_text = tr("resource.help.cdm") if self._tab == "cdm" else tr("resource.help.vault")
        self.query_one("#resource-manager-help", Static).update(f"[$muted]{help_text}[/]")
        self.query_one("#crumbs-pill", Static).update(
            f"[$dim]{tr('resource.entries', section=section, count=sum(row is not None for row in self._rows))}[/]"
        )

        if self._tab == "cdm":
            headers = [tr("resource.section.drm")]
            if any(row is not None and row.kind == "cdm-remote" for row in self._rows):
                headers.append(tr("resource.section.remote_cdms"))
        else:
            headers = []
            if any(row is not None and row.kind == "vault-local" for row in self._rows):
                headers.append(tr("resource.section.local_vaults"))
            if any(row is not None and row.kind == "vault-remote" for row in self._rows):
                headers.append(tr("resource.section.remote_vaults"))
        for row in self._rows:
            if row is None:
                title = headers.pop(0) if headers else section
                option_list.add_option(Option(f"  [$dim]{title}[/]", disabled=True))
                continue
            option_list.add_option(Option(self._row_markup(row)))
        self._highlight(wanted)
        self._refresh_actions()

    def _row_markup(self, row: ResourceRow) -> str:
        if row.kind == "vault-discovered":
            enabled = f"[$accent]{tr('resource.discovered')}[/]"
        elif row.kind in {"drm-system", "cdm-rules"}:
            enabled = ""
        else:
            enabled = f"[$ok]{tr('resource.enabled')}[/]" if row.enabled else f"[$warn]{tr('resource.disabled')}[/]"
        active = f"  [$accent]{tr('resource.active')}[/]" if row.kind == "cdm-choice" else ""
        return (
            f"    [$foreground]{visual_markup(row.name)}[/]  "
            f"[$dim]{visual_markup(row.detail)}[/]  {enabled}{active}"
        )

    def _highlight(self, wanted: tuple[str, str] | None) -> None:
        option_list = self.query_one("#resource-list", OptionList)
        selected = next(
            (index for index, row in enumerate(self._rows) if row is not None and row.identity == wanted),
            None,
        )
        if selected is None:
            selected = next((index for index, row in enumerate(self._rows) if row is not None), 0)
        option_list.highlighted = selected

    def _selected(self) -> ResourceRow | None:
        index = self.query_one("#resource-list", OptionList).highlighted
        if index is None or index >= len(self._rows):
            return None
        return self._rows[index]

    def _refresh_actions(self) -> None:
        vault_tab = self._tab == "vault"
        selected = self._selected()
        use = self.query_one("#resource-use", Button)
        use.label = {
            "drm-system": tr("resource.change_drm"),
            "cdm-rules": tr("resource.edit_rules"),
        }.get(selected.kind if selected is not None else "", tr("resource.use_cdm"))
        use.display = not vault_tab
        self.query_one("#resource-policy", Button).display = vault_tab
        self.query_one("#resource-add", Button).display = True
        self.query_one("#resource-add-local", Button).display = vault_tab
        self.query_one("#resource-toggle", Button).display = True
        self.query_one("#resource-edit", Button).disabled = selected is None or selected.kind in {"drm-system", "cdm-rules", "cdm-choice", "cdm-local", "vault-local", "vault-discovered"}
        self.query_one("#resource-toggle", Button).disabled = selected is None or selected.kind in {"drm-system", "cdm-rules", "cdm-choice", "cdm-local"}
        self.query_one("#resource-delete", Button).disabled = selected is None or selected.kind not in {"cdm-remote", "vault-remote"}

    # ---------------------------------------------------------------- events
    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        event.stop()
        self._tab = str(event.tab.id or "resource-tab-cdm").removeprefix("resource-tab-")
        self.rebuild()

    def on_option_list_option_highlighted(self, _event: OptionList.OptionHighlighted) -> None:
        self._refresh_actions()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_activate()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        action = (event.button.id or "").removeprefix("resource-")
        {
            "use": self.action_activate,
            "add": self.action_add,
            "add-local": self.action_add_local,
            "edit": self.action_edit,
            "toggle": self.action_toggle,
            "delete": self.action_delete,
            "policy": self.action_policy,
        }.get(action, lambda: None)()

    def action_drm_system(self) -> None:
        """Change the global DRM preference beside the device/vault choices."""
        from .settings_screen import _ChoiceEditor

        spec = self.app.globals.spec_by_key.get("drm_system")
        if spec is None:
            return

        def chosen(value: str | None) -> None:
            if value is None:
                return
            self.app.globals.set("drm_system", value)
            if self.app.active_device_system() != value:
                match = next((device for device in self.app.devices() if device.system == value), None)
                if match is not None:
                    self.app.set_device(match.name)
                    self.notify(tr("resource.notify.drm_cdm", system=value, device=match.name), timeout=4)
                else:
                    self.notify(tr("resource.notify.drm_none", system=value), severity="warning", timeout=6)
            self.rebuild()

        self.app.push_screen(_ChoiceEditor(spec, self.app.globals.get("drm_system")), chosen)

    def action_cdm_rules(self) -> None:
        from .cdm_rules_screen import CdmRulesScreen

        self.app.push_screen(
            CdmRulesScreen(str(self.app.globals.get("cdm_rules") or "")),
            lambda value: self._save_cdm_rules(value),
        )

    def _save_cdm_rules(self, value: str | None) -> None:
        if value is not None:
            self.app.globals.set("cdm_rules", value)
            self.rebuild()

    def action_activate(self) -> None:
        row = self._selected()
        if row is None:
            return
        if self._tab == "vault":
            self.action_toggle()
            return
        if row.kind == "drm-system":
            self.action_drm_system()
        elif row.kind == "cdm-rules":
            self.action_cdm_rules()
        elif row.kind in {"cdm-choice", "cdm-local"}:
            self._choose_cdm()
        elif row.kind == "cdm-remote":
            self.action_edit()

    def _choose_cdm(self) -> None:
        from .cdm_screen import CdmScreen

        current = str(self.app.globals.get("cdm_device", "") or "")

        def chosen(name: str | None) -> None:
            if name is not None:
                self.app.set_device(name)
                # The manager's "Choose CDM" is the app-wide picker, so it must
                # have the same system-pairing behavior as the home-screen chip.
                # Otherwise picking a .prd while Widevine was active would leave
                # a globally mismatched DRM/CDM pair.  This does not touch any
                # service-specific cdm_widevine/cdm_playready setting.
                found = next((device for device in self.app.devices() if device.name == name), None)
                if found is not None and found.system != str(
                    self.app.globals.get("drm_system", "widevine")
                ):
                    self.app.globals.set("drm_system", found.system)
                    self.notify(
                        tr("resource.notify.switched", name=name, system=found.system),
                        timeout=5,
                    )
                self.rebuild()

        self.app.push_screen(
            CdmScreen(current=current, system=str(self.app.globals.get("drm_system", "widevine"))),
            chosen,
        )

    def action_add(self) -> None:
        if self._tab == "cdm":
            self.app.push_screen(_RemoteCdmEditor(), self._save_remote_cdm)
        else:
            self.app.push_screen(_RemoteVaultEditor(), self._save_remote_vault)

    def action_add_local(self) -> None:
        """Import one or more discovered SQLite key vaults in one confirmation."""
        if self._tab != "vault":
            self.notify(tr("resource.notify.open_vaults"), timeout=4)
            return
        candidates = vaults.discovered_local_vaults(self.app.config)
        if not candidates:
            self.notify(
                tr("resource.notify.no_sqlite", path=self.app.config.paths.home / "db"),
                timeout=6,
            )
            return

        def done(result: VaultTargetResult | None) -> None:
            if result is None:
                return
            if result.selected is None:
                selected = candidates
            else:
                wanted = {name.casefold() for name in result.selected}
                selected = [candidate for candidate in candidates if candidate.name.casefold() in wanted]
            self._import_local_candidates(selected)

        self.app.push_screen(
            VaultTargetScreen(
                candidates,
                (),
                title=tr("resource.import_title"),
                writable_only=False,
            ),
            done,
        )

    def action_edit(self) -> None:
        row = self._selected()
        if row is None or row.entry is None:
            self.notify(tr("resource.notify.local_edit"), timeout=5)
            return
        if row.kind == "cdm-remote":
            self.app.push_screen(_RemoteCdmEditor(row.entry), lambda value: self._save_remote_cdm(value, row.name))
        elif row.kind == "vault-remote":
            self.app.push_screen(_RemoteVaultEditor(row.entry), lambda value: self._save_remote_vault(value, row.name))

    def action_toggle(self) -> None:
        row = self._selected()
        if row is None or row.kind in {"drm-system", "cdm-rules", "cdm-choice", "cdm-local"}:
            self.notify(tr("resource.notify.local_toggle"), timeout=5)
            return
        if row.kind == "vault-discovered":
            self._import_local_candidates([row])
            return
        entries = self._remote_cdms() if row.kind == "cdm-remote" else self._vault_entries()
        if not entries and row.kind == "vault-local":
            entries = [{"type": "sqlite", "name": "local"}]
        index = self._entry_index(entries, row.name)
        if index is None:
            self.notify(tr("resource.notify.missing"), severity="warning", timeout=5)
            self.rebuild()
            return
        entries[index]["enabled"] = not _bool(entries[index].get("enabled"), True)
        try:
            if row.kind == "cdm-remote":
                self.app.config.save_remote_cdms(entries)
                if not entries[index]["enabled"] and _text(
                    self.app.globals.get("cdm_device", "")
                ).casefold() == row.name.casefold():
                    # A disabled endpoint is intentionally not selectable; do
                    # not leave the status line pointing at it until restart.
                    self.app.set_device("")
            else:
                self.app.config.save_vault_specs(entries)
            self.app.refresh_resources()
        except (OSError, ValueError) as exc:
            self.notify(tr("resource.notify.update_fail", name=row.name, error=exc), severity="error", timeout=7)
            return
        state = tr("resource.enabled") if entries[index]["enabled"] else tr("resource.disabled")
        self.notify(tr("resource.notify.state", name=row.name, state=state), timeout=3)
        self.rebuild()

    def action_delete(self) -> None:
        row = self._selected()
        if row is None or row.kind not in {"cdm-remote", "vault-remote"}:
            self.notify(tr("resource.notify.delete_local"), timeout=5)
            return

        def confirmed(ok: bool) -> None:
            if ok:
                self._delete_row(row)

        self.app.push_screen(
            _DeleteConfirm(
                tr("resource.noun.cdm") if row.kind == "cdm-remote" else tr("resource.noun.vault"),
                row.name,
            ),
            confirmed,
        )

    def action_policy(self) -> None:
        if self._tab != "vault":
            self.notify(tr("resource.notify.open_policy"), timeout=4)
            return
        self.app.push_screen(VaultPolicyScreen(self.app.globals), lambda _changed: self.rebuild())

    # --------------------------------------------------------------- mutation
    def _import_local_candidates(self, candidates: list[Any]) -> None:
        """Persist selected discovery rows as explicit local vault entries."""
        selected = [candidate for candidate in candidates if candidate]
        if not selected:
            self.notify(tr("resource.notify.none_selected"), timeout=4)
            return
        entries = self._vault_entries()
        if not entries:
            entries.append(
                {
                    "type": "sqlite",
                    "name": "local",
                    "path": str(self.app.config.paths.keys_db),
                    "enabled": True,
                }
            )
        existing_paths: set[Path] = set()
        for entry in entries:
            if str(entry.get("type") or "sqlite").strip().lower() not in {"", "sqlite", "local"}:
                continue
            raw_path = entry.get("path") or self.app.config.paths.keys_db
            path = Path(str(raw_path)).expanduser()
            if not path.is_absolute() and getattr(self.app.config, "source", None) is not None:
                path = Path(self.app.config.source).parent / path
            try:
                existing_paths.add(path.resolve())
            except OSError:
                existing_paths.add(Path(os.path.abspath(path)))
        added = 0
        for candidate in selected:
            if isinstance(candidate, ResourceRow):
                candidate = candidate.entry or {}
            if isinstance(candidate, vaults.VaultDescriptor):
                name = candidate.name
                path = candidate.path
            elif isinstance(candidate, dict):
                name = _text(candidate.get("name"))
                raw_path = _text(candidate.get("path"))
                path = Path(raw_path) if raw_path else None
            else:
                continue
            if not name or path is None:
                continue
            path = path.expanduser()
            if not path.is_absolute() and getattr(self.app.config, "source", None) is not None:
                path = Path(self.app.config.source).parent / path
            try:
                resolved_path = path.resolve()
            except OSError:
                resolved_path = Path(os.path.abspath(path))
            if resolved_path in existing_paths:
                continue
            try:
                self._check_name(entries, name)
            except ValueError as exc:
                self.notify(str(exc), severity="warning", timeout=6)
                continue
            entries.append(
                {
                    "type": "sqlite",
                    "name": name,
                    "path": str(resolved_path),
                    "enabled": True,
                }
            )
            existing_paths.add(resolved_path)
            added += 1
        if not added:
            self.notify(tr("resource.notify.already"), timeout=5)
            return
        try:
            self.app.config.save_vault_specs(entries)
            self.app.refresh_resources()
        except (OSError, ValueError) as exc:
            self.notify(tr("resource.notify.import_fail", error=exc), severity="error", timeout=8)
            return
        ident = "resource.notify.imported" if added == 1 else "resource.notify.imported_plural"
        self.notify(tr(ident, count=added), timeout=4)
        self.rebuild()

    @staticmethod
    def _entry_index(entries: list[dict[str, Any]], name: str) -> int | None:
        wanted = name.casefold()
        return next((index for index, entry in enumerate(entries) if _text(entry.get("name")).casefold() == wanted), None)

    @staticmethod
    def _check_name(entries: list[dict[str, Any]], name: str, old_name: str = "") -> None:
        wanted = name.casefold()
        old = old_name.casefold()
        if any(_text(entry.get("name")).casefold() == wanted and wanted != old for entry in entries):
            raise ValueError(tr("resource.notify.exists", name=name))

    def _save_remote_cdm(self, value: dict[str, Any] | None, old_name: str = "") -> None:
        if value is None:
            return
        entries = self._remote_cdms()
        try:
            self._check_name(entries, _text(value.get("name")), old_name)
            index = self._entry_index(entries, old_name) if old_name else None
            if index is None:
                entries.append(value)
            else:
                entries[index] = value
            self.app.config.save_remote_cdms(entries)
            if old_name and old_name.casefold() != _text(value.get("name")).casefold():
                self._rename_cdm_references(old_name, _text(value.get("name")))
            self.app.refresh_resources()
        except (OSError, ValueError) as exc:
            self.notify(tr("resource.notify.save_cdm_fail", error=exc), severity="error", timeout=8)
            return
        self.notify(tr("resource.notify.saved_cdm", name=_text(value.get("name"))), timeout=4)
        self.rebuild()

    def _save_remote_vault(self, value: dict[str, Any] | None, old_name: str = "") -> None:
        if value is None:
            return
        entries = self._vault_entries()
        try:
            # An omitted key_vaults section means one implicit local database.
            # Adding the first remote entry must make that implicit backend
            # explicit, otherwise the new YAML list would accidentally replace
            # it and a freshly configured remote vault would remove local cache.
            if not entries:
                entries.append({"type": "sqlite", "name": "local"})
            self._check_name(entries, _text(value.get("name")), old_name)
            index = self._entry_index(entries, old_name) if old_name else None
            if index is None:
                entries.append(value)
            else:
                entries[index] = value
            self.app.config.save_vault_specs(entries)
            if old_name and old_name.casefold() != _text(value.get("name")).casefold():
                self._rename_vault_targets(old_name, _text(value.get("name")))
            self.app.refresh_resources()
        except (OSError, ValueError) as exc:
            self.notify(tr("resource.notify.save_vault_fail", error=exc), severity="error", timeout=8)
            return
        self.notify(tr("resource.notify.saved_vault", name=_text(value.get("name"))), timeout=4)
        self.rebuild()

    def _delete_row(self, row: ResourceRow) -> None:
        entries = self._remote_cdms() if row.kind == "cdm-remote" else self._vault_entries()
        index = self._entry_index(entries, row.name)
        if index is None:
            self.notify(tr("resource.notify.missing"), severity="warning", timeout=5)
            self.rebuild()
            return
        entries.pop(index)
        try:
            if row.kind == "cdm-remote":
                self.app.config.save_remote_cdms(entries)
                self._rename_cdm_references(row.name, "")
            else:
                self.app.config.save_vault_specs(entries)
                self._rename_vault_targets(row.name, "")
            self.app.refresh_resources()
        except (OSError, ValueError) as exc:
            self.notify(tr("resource.notify.delete_fail", name=row.name, error=exc), severity="error", timeout=8)
            return
        self.notify(tr("resource.notify.removed", name=row.name), timeout=4)
        self.rebuild()

    def _rename_cdm_references(self, old_name: str, new_name: str) -> None:
        """Keep every CDM selection surface in step with an endpoint rename.

        ``cdm.default`` and ``cdm.by_service`` live in YAML, the active picker
        lives in the global settings scope, and service-specific CDM choices live
        in ``settings.json``.  The last category is deliberately independent of
        the global choice, so it must be migrated rather than cleared merely
        because the endpoint was edited from the shared manager.
        """
        raw = self.app.config.raw
        cdm = dict(raw.get("cdm") or {})
        changed = False
        if _text(cdm.get("default")).casefold() == old_name.casefold():
            if new_name:
                cdm["default"] = new_name
            else:
                cdm.pop("default", None)
            changed = True
        by_service = cdm.get("by_service")
        if isinstance(by_service, dict):
            copied = dict(by_service)
            for service, device in tuple(copied.items()):
                if _text(device).casefold() == old_name.casefold():
                    if new_name:
                        copied[service] = new_name
                    else:
                        copied.pop(service, None)
                    changed = True
            cdm["by_service"] = copied
        current = _text(self.app.globals.get("cdm_device", ""))
        if current.casefold() == old_name.casefold():
            self.app.set_device(new_name)
        updated = self.app.settings_store.rename_cdm_device_references(old_name, new_name)
        if self.app.settings_store.problem:
            self.notify(
                tr("resource.notify.choice_fail", error=self.app.settings_store.problem),
                severity="warning",
                timeout=9,
            )
        elif updated:
            if not new_name:
                ident = "resource.notify.choice_cleared" if updated == 1 else "resource.notify.choice_cleared_plural"
            else:
                ident = "resource.notify.choice_updated" if updated == 1 else "resource.notify.choice_updated_plural"
            self.notify(tr(ident, count=updated), timeout=3)
        if changed:
            self.app.config.save_managed_section("cdm", cdm)

    def _rename_vault_targets(self, old_name: str, new_name: str) -> None:
        for key in ("vault_read_targets", "vault_write_targets", "vault_search_targets"):
            selected = vaults.parse_targets(self.app.globals.get(key, ""))
            if selected is None:
                continue
            changed = False
            rewritten: list[str] = []
            for name in selected:
                if name.casefold() == old_name.casefold():
                    changed = True
                    if new_name:
                        rewritten.append(new_name)
                else:
                    rewritten.append(name)
            if changed:
                self.app.globals.set(key, vaults.serialize_targets(tuple(rewritten)))

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


def _valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _optional_int(value: str, label: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(tr("resource.int_required", label=label)) from exc


def _optional_float(value: str, label: str) -> float | None:
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(tr("resource.number_required", label=label)) from exc
    if parsed <= 0:
        raise ValueError(tr("resource.number_positive", label=label))
    return parsed


__all__ = ["ResourceManagerScreen", "VaultPolicyScreen"]
