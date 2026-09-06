"""Unified Proxy & VPN settings.

Only providers that return an HTTP(S) proxy endpoint are exposed here. ExpressVPN
uses an explicit device-authorization screen and stores its refreshable session in
the project token directory. No provider changes the machine's default route.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import HorizontalScroll, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, Input, Label, OptionList, Static, Tab, Tabs
from textual.widgets.option_list import Option

from ..core.i18n import phrase, tr
from ..core.proxy import PROVIDER_LABELS, PROVIDER_ORDER, safe_proxy_label
from ..core.proxy_express import DeviceAuthorization, ExpressVPNClient
from ..core.settings import Settings
from .bidi import visual_markup
from .chrome import Chrome, KeyBar, refresh_locale_widgets


@dataclass(frozen=True)
class ProxyRow:
    kind: str
    name: str
    value: Any = None


def _format_map(value: Any) -> str:
    return ", ".join(f"{key}={item}" for key, item in value.items()) if isinstance(value, dict) else ""


def _parse_map(value: str, *, allow_empty: bool = False) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in value.split(","):
        if "=" not in item:
            continue
        key, mapped = item.split("=", 1)
        key, mapped = key.strip().lower(), mapped.strip()
        if key and (mapped or allow_empty):
            found[key] = mapped
    return found


class _RouteEditor(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False), Binding("ctrl+b", "cancel", "Back", show=False)]

    def __init__(self, current: str) -> None:
        super().__init__()
        self.current = current
        self.add_class("editor")

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        with Vertical(id="modal-body"):
            with Vertical(id="proxy-route-card", classes="proxy-editor-card"):
                yield Label(tr("proxy.route.title"), classes="ask-title")
                yield Label(tr("proxy.route.help"), classes="ask-hint")
                yield Input(value=self.current, placeholder=tr("proxy.route.placeholder"), id="proxy-route-input")
        yield KeyBar(("enter", "save"), ("^b", "cancel"), ("esc", "cancel"))

    def on_mount(self) -> None:
        self.query_one("#proxy-route-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


class _NamedProxyEditor(ModalScreen[dict[str, Any] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False), Binding("ctrl+b", "cancel", "Back", show=False)]

    def __init__(self, current: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.current = current or {}
        self.add_class("editor")

    def compose(self) -> ComposeResult:
        urls = self.current.get("urls", self.current.get("url", self.current.get("uri", "")))
        if isinstance(urls, list):
            urls = ", ".join(str(item) for item in urls)
        yield Chrome(show_search=False, show_settings=False)
        with Vertical(id="modal-body"):
            with Vertical(id="proxy-named-card", classes="proxy-editor-card"):
                yield Label(tr("proxy.named.title"), classes="ask-title")
                yield Label(tr("proxy.named.help"), classes="ask-hint")
                yield Label(tr("proxy.field.name"), classes="proxy-field-label")
                yield Input(value=str(self.current.get("name", "")), id="proxy-name")
                yield Label(tr("proxy.field.uris"), classes="proxy-field-label")
                yield Input(
                    value=str(urls or ""),
                    placeholder=tr("proxy.placeholder.uris"),
                    id="proxy-urls",
                )
                yield Checkbox(
                    tr("proxy.enabled"),
                    value=bool(self.current.get("enabled", self.current.get("enable", True))),
                    id="proxy-enabled",
                )
        yield KeyBar(("enter", "save"), ("^b", "cancel"), ("esc", "cancel"))

    def on_mount(self) -> None:
        self.query_one("#proxy-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "proxy-name":
            self.query_one("#proxy-urls", Input).focus()
            return
        self._save()

    def _save(self) -> None:
        name = self.query_one("#proxy-name", Input).value.strip()
        urls = [value.strip() for value in self.query_one("#proxy-urls", Input).value.split(",") if value.strip()]
        if not name or not urls:
            self.notify(tr("proxy.need_named"), severity="error", timeout=5)
            return
        self.dismiss({"name": name, "urls": urls, "enabled": self.query_one("#proxy-enabled", Checkbox).value})

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


class _ProviderEditor(ModalScreen[dict[str, Any] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False), Binding("ctrl+b", "cancel", "Back", show=False)]

    def __init__(self, provider: str, current: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.provider = provider
        self.current = current or {}
        self.add_class("editor")

    def compose(self) -> ComposeResult:
        server_map = self.current.get("server_map") or {}
        mapping = ", ".join(f"{key}={value}" for key, value in server_map.items()) if isinstance(server_map, dict) else ""
        label = PROVIDER_LABELS[self.provider]
        yield Chrome(show_search=False, show_settings=False)
        with Vertical(id="modal-body"):
            with Vertical(id="proxy-provider-card", classes="proxy-editor-card"):
                yield Label(tr("proxy.provider.title", label=label), classes="ask-title")
                yield Label(tr("proxy.provider.help"), classes="ask-hint")
                yield Label(tr("proxy.field.username"), classes="proxy-field-label")
                yield Input(value=str(self.current.get("username", "")), id="provider-username")
                yield Label(tr("proxy.field.password"), classes="proxy-field-label")
                yield Input(value=str(self.current.get("password", "")), password=True, id="provider-password")
                yield Label(tr("proxy.field.pinned"), classes="proxy-field-label")
                yield Input(value=mapping, placeholder=tr("proxy.placeholder.map"), id="provider-map")
                yield Checkbox(
                    tr("proxy.enabled"),
                    value=bool(self.current.get("enabled", self.current.get("enable", True))),
                    id="provider-enabled",
                )
        yield KeyBar(("enter", "save"), ("^b", "cancel"), ("esc", "cancel"))

    def on_mount(self) -> None:
        self.query_one("#provider-username", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        ids = ["provider-username", "provider-password", "provider-map"]
        if event.input.id in ids[:2]:
            self.query_one(f"#{ids[ids.index(event.input.id) + 1]}", Input).focus()
            return
        self._save()

    def _save(self) -> None:
        username = self.query_one("#provider-username", Input).value.strip()
        password = self.query_one("#provider-password", Input).value
        if not username or not password:
            self.notify(tr("proxy.need_creds"), severity="error", timeout=5)
            return
        server_map: dict[str, str] = {}
        for item in self.query_one("#provider-map", Input).value.split(","):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            if key.strip() and value.strip():
                server_map[key.strip().lower()] = value.strip()
        self.dismiss(
            {
                "username": username,
                "password": password,
                "server_map": server_map,
                "enabled": self.query_one("#provider-enabled", Checkbox).value,
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


class _ExpressProviderEditor(ModalScreen[dict[str, Any] | None]):
    """ExpressVPN has a device login, not manually entered service credentials."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False), Binding("ctrl+b", "cancel", "Back", show=False)]

    def __init__(self, current: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.current = current or {}
        self.add_class("editor")

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        with Vertical(id="modal-body"):
            with Vertical(id="proxy-provider-card", classes="proxy-editor-card"):
                yield Label(tr("proxy.express.title"), classes="ask-title")
                yield Label(tr("proxy.express.help"), classes="ask-hint")
                yield Label(tr("proxy.field.presets"), classes="proxy-field-label")
                yield Input(
                    value=_format_map(self.current.get("region_map")),
                    placeholder=tr("proxy.placeholder.presets"),
                    id="express-region-map",
                )
                yield Label(tr("proxy.field.aliases"), classes="proxy-field-label")
                yield Input(
                    value=_format_map(self.current.get("server_map")),
                    placeholder=tr("proxy.placeholder.aliases"),
                    id="express-server-map",
                )
                yield Label(tr("proxy.field.account_json"), classes="proxy-field-label")
                yield Input(
                    value=str(self.current.get("account_json", "")),
                    placeholder=tr("proxy.placeholder.account_json"),
                    id="express-account-json",
                )
                yield Checkbox(
                    tr("proxy.enabled"),
                    value=bool(self.current.get("enabled", self.current.get("enable", True))),
                    id="provider-enabled",
                )
        yield KeyBar(("enter", "save"), ("^b", "cancel"), ("esc", "cancel"))

    def on_mount(self) -> None:
        self.query_one("#express-region-map", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        ids = ["express-region-map", "express-server-map", "express-account-json"]
        if event.input.id in ids[:2]:
            self.query_one(f"#{ids[ids.index(event.input.id) + 1]}", Input).focus()
            return
        self._save()

    def _save(self) -> None:
        # Preserve imported token/cache overrides that this deliberately
        # credential-free editor does not expose, while replacing its own fields.
        value = dict(self.current)
        value.pop("enable", None)
        value["enabled"] = self.query_one("#provider-enabled", Checkbox).value
        value["region_map"] = _parse_map(
            self.query_one("#express-region-map", Input).value,
            allow_empty=True,
        )
        value["server_map"] = _parse_map(self.query_one("#express-server-map", Input).value)
        account_json = self.query_one("#express-account-json", Input).value.strip()
        if account_json:
            value["account_json"] = account_json
        else:
            value.pop("account_json", None)
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


class ExpressLoginScreen(Screen[bool]):
    """Run ExpressVPN's OAuth device grant without blocking Textual's event loop."""

    BINDINGS = [
        Binding("ctrl+b", "cancel", "Cancel", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("o", "open", "Open browser", show=True),
        Binding("c", "copy", "Copy code", show=True),
    ]

    def __init__(self, client: ExpressVPNClient) -> None:
        super().__init__()
        self.client = client
        self.authorization: DeviceAuthorization | None = None
        self.cancelled = threading.Event()

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        yield Static(tr("proxy.login.title"), id="masthead")
        with Vertical(id="body"):
            yield Static(tr("proxy.login.requesting"), id="express-login-status")
            yield Static("", id="express-login-code")
            yield Static("", id="express-login-url")
        yield KeyBar(("o", "open browser"), ("c", "copy code"), ("^b", "cancel"), ("esc", "cancel"))

    def on_mount(self) -> None:
        self._authorize()

    @work(thread=True, exclusive=True, group="expressvpn-login")
    def _authorize(self) -> None:
        try:
            authorization = self.client.request_device_authorization()
            if self.cancelled.is_set():
                return
            self.app.call_from_thread(self._show_authorization, authorization)
            while time.time() < authorization.expires_at:
                if self.cancelled.wait(authorization.interval):
                    return
                tokens = self.client.poll_device_authorization(authorization)
                if tokens is None:
                    continue
                self.client.save_device_tokens(tokens)
                self.app.call_from_thread(self._finish, True, tr("proxy.login.saved"))
                return
            self.app.call_from_thread(self._finish, False, tr("proxy.login.timeout"))
        except Exception as exc:
            if not self.cancelled.is_set():
                self.app.call_from_thread(self._finish, False, tr("proxy.login.failed", error=exc))

    def _show_authorization(self, authorization: DeviceAuthorization) -> None:
        self.authorization = authorization
        self.query_one("#express-login-status", Static).update(tr("proxy.login.prompt"))
        self.query_one("#express-login-code", Static).update(
            f"[$muted]{tr('proxy.login.code')}[/]  [$accent]{visual_markup(authorization.user_code)}[/]"
        )
        self.query_one("#express-login-url", Static).update(
            f"[$muted]{tr('proxy.login.open')}[/]  [$foreground]{visual_markup(authorization.verification_uri)}[/]"
        )

    def _finish(self, success: bool, message: str) -> None:
        if self.cancelled.is_set():
            return
        self.notify(message, severity="information" if success else "error", timeout=8)
        self.dismiss(success)

    def action_open(self) -> None:
        if self.authorization is not None:
            self.app.open_url(self.authorization.verification_uri)

    def action_copy(self) -> None:
        if self.authorization is not None:
            self.app.copy_text(self.authorization.user_code)
            self.notify(tr("proxy.login.copied"), timeout=3)

    def action_cancel(self) -> None:
        self.cancelled.set()
        self.dismiss(False)

    def on_unmount(self) -> None:
        self.cancelled.set()

    def go_back(self) -> bool:
        self.action_cancel()
        return True


class _ProviderTypeEditor(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False), Binding("ctrl+b", "cancel", "Back", show=False)]

    def __init__(self, providers: list[str]) -> None:
        super().__init__()
        self.providers = providers

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        with Vertical(id="modal-body"):
            with Vertical(id="proxy-provider-type-card", classes="proxy-editor-card"):
                yield Label(tr("proxy.add_provider"), classes="ask-title")
                yield Label(tr("proxy.add_provider_help"), classes="ask-hint")
                yield OptionList(
                    *[Option(PROVIDER_LABELS[name], id=name) for name in self.providers],
                    id="proxy-provider-types",
                )
        yield KeyBar(("enter", "choose"), ("^b", "cancel"), ("esc", "cancel"))

    def on_mount(self) -> None:
        self.query_one("#proxy-provider-types", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(str(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


class ProxyManagerScreen(Screen[None]):
    """Manage routing, static endpoints and provider credentials in one place."""

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "activate", "Edit", show=True),
        Binding("a", "add", "Add", show=True),
        Binding("e", "activate", "Edit", show=False),
        Binding("d", "delete", "Delete", show=True),
        Binding("space", "toggle", "Enable/disable", show=True),
        Binding("t", "test", "Test route", show=True),
        Binding("l", "authorize", "Authorize", show=True),
    ]

    def __init__(self, globals_scope: Settings) -> None:
        super().__init__()
        self.globals = globals_scope
        self._tab = "routing"
        self._rows: list[ProxyRow | None] = []

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        yield Static(tr("proxy.title"), id="masthead")
        yield Static("", id="proxy-manager-help")
        yield Tabs(
            Tab(tr("proxy.tab.routing"), id="proxy-tab-routing"),
            Tab(tr("proxy.tab.endpoints"), id="proxy-tab-endpoints"),
            Tab(tr("proxy.tab.providers"), id="proxy-tab-providers"),
            id="proxy-tabs",
        )
        with Vertical(id="proxy-manager-body"):
            yield Static("", id="proxy-preview")
            yield OptionList(id="proxy-list")
        with HorizontalScroll(id="proxy-manager-actions"):
            yield Button(tr("common.edit"), id="proxy-edit")
            yield Button(tr("common.add"), id="proxy-add")
            yield Button(tr("common.toggle"), id="proxy-toggle")
            yield Button(phrase("Delete"), id="proxy-delete")
            yield Button(tr("proxy.test_route"), id="proxy-test")
            yield Button(tr("common.authorize"), id="proxy-authorize")
        yield KeyBar(
            ("enter", "edit"),
            ("a", "add"),
            ("l", "authorize"),
            ("d", "delete"),
            ("space", "toggle"),
            ("t", "test"),
            ("^b", "back"),
        )

    def on_mount(self) -> None:
        self.rebuild()
        self.query_one("#proxy-list", OptionList).focus()

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self.query_one("#masthead", Static).update(tr("proxy.title"))
        self.query_one("#proxy-tab-routing", Tab).label = tr("proxy.tab.routing")
        self.query_one("#proxy-tab-endpoints", Tab).label = tr("proxy.tab.endpoints")
        self.query_one("#proxy-tab-providers", Tab).label = tr("proxy.tab.providers")
        self.query_one("#proxy-add", Button).label = tr("common.add")
        self.query_one("#proxy-delete", Button).label = phrase("Delete")
        self.query_one("#proxy-test", Button).label = tr("proxy.test_route")
        self.query_one("#proxy-authorize", Button).label = tr("common.authorize")
        self.rebuild()

    def _tab_rows(self) -> list[ProxyRow]:
        if self._tab == "routing":
            return [
                ProxyRow("route", tr("proxy.row.route")),
                ProxyRow("download", tr("proxy.row.downloads")),
            ]
        if self._tab == "endpoints":
            entries = self.app.config.raw.get("proxies") or {}
            return [ProxyRow("endpoint", str(name), value) for name, value in entries.items()] if isinstance(entries, dict) else []
        entries = self.app.config.raw.get("proxy_providers") or {}
        return [
            ProxyRow("provider", str(name).lower(), value)
            for name, value in entries.items()
            if str(name).lower() in PROVIDER_LABELS and str(name).lower() != "basic"
        ]

    def rebuild(self) -> None:
        option_list = self.query_one("#proxy-list", OptionList)
        selected = option_list.highlighted
        self._rows = self._tab_rows()
        option_list.clear_options()
        if self._tab == "routing":
            help_text = tr("proxy.help.routing")
        elif self._tab == "endpoints":
            help_text = tr("proxy.help.endpoints")
        else:
            help_text = tr("proxy.help.providers")
        self.query_one("#proxy-manager-help", Static).update(f"[$muted]{help_text}[/]")
        for row in self._rows:
            option_list.add_option(Option(self._row_markup(row)))
        option_list.highlighted = min(selected or 0, max(0, len(self._rows) - 1)) if self._rows else None
        self._preview()
        self._refresh_actions()

    def _row_markup(self, row: ProxyRow) -> str:
        if row.kind == "route":
            value = str(self.globals.get("proxy") or "") or tr("proxy.direct")
            return f"  [$foreground]{tr('proxy.row.route')}[/]  [$accent]{visual_markup(value)}[/]"
        if row.kind == "download":
            value = tr("value.on") if self.globals.get("proxy_downloads") else tr("value.off")
            return f"  [$foreground]{tr('proxy.row.downloads')}[/]  [$accent]{value}[/]"
        if row.kind == "endpoint":
            enabled = not isinstance(row.value, dict) or bool(row.value.get("enabled", True))
            state = tr("value.enabled") if enabled else tr("value.disabled")
            return f"  [$foreground]{visual_markup(row.name)}[/]  [$accent]{state}[/]  [$dim]{tr('proxy.static')}[/]"
        enabled = not isinstance(row.value, dict) or bool(row.value.get("enabled", row.value.get("enable", True)))
        state = tr("value.enabled") if enabled else tr("value.disabled")
        return (
            f"  [$foreground]{PROVIDER_LABELS.get(row.name, row.name)}[/]  "
            f"[$accent]{state}[/]  [$dim]{tr('proxy.provider')}[/]"
        )

    def _preview(self) -> None:
        row = self._selected()
        if row is None:
            self.query_one("#proxy-preview", Static).update("")
            return
        if row.kind == "route":
            route = str(self.globals.get("proxy") or "")
            self.query_one("#proxy-preview", Static).update(
                f"[$dim]{tr('proxy.preview.route')}[/] {visual_markup(route or tr('proxy.direct'))}"
            )
        elif row.kind == "endpoint":
            urls = row.value.get("urls", row.value.get("url", "")) if isinstance(row.value, dict) else row.value
            if isinstance(urls, list):
                urls = urls[0] if urls else ""
            self.query_one("#proxy-preview", Static).update(
                f"[$dim]{tr('proxy.preview.endpoint')}[/] {safe_proxy_label(str(urls or ''))}"
            )
        elif row.name == "expressvpn":
            client = ExpressVPNClient(
                dict(row.value) if isinstance(row.value, dict) else {},
                self.app.config.paths.tokens / "vpn",
            )
            state = tr("proxy.preview.authorized") if client.has_silent_session() else tr("proxy.preview.need_auth")
            self.query_one("#proxy-preview", Static).update(f"[$dim]{tr('proxy.preview.express')}[/] {state}")
        else:
            self.query_one("#proxy-preview", Static).update(f"[$dim]{tr('proxy.preview.hidden')}[/]")

    def _selected(self) -> ProxyRow | None:
        index = self.query_one("#proxy-list", OptionList).highlighted
        return self._rows[index] if index is not None and index < len(self._rows) else None

    def on_option_list_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        self._preview()
        self._refresh_actions()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_activate()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        action = (event.button.id or "").removeprefix("proxy-")
        {
            "edit": self.action_activate,
            "add": self.action_add,
            "toggle": self.action_toggle,
            "delete": self.action_delete,
            "test": self.action_test,
            "authorize": self.action_authorize,
        }.get(action, lambda: None)()

    def _refresh_actions(self) -> None:
        """Keep mouse buttons in the same context as the keyboard shortcuts."""
        actions = {
            key: self.query_one(f"#proxy-{key}", Button)
            for key in ("edit", "add", "toggle", "delete", "test", "authorize")
        }
        row = self._selected()
        if self._tab == "routing":
            actions["edit"].label = tr("proxy.edit_route")
            actions["edit"].display = True
            actions["add"].display = False
            actions["toggle"].label = (
                tr("proxy.downloads_on") if self.globals.get("proxy_downloads") else tr("proxy.downloads_off")
            )
            actions["toggle"].display = True
            actions["toggle"].disabled = False
            actions["delete"].display = False
            actions["authorize"].display = False
        else:
            is_endpoint = bool(row and row.kind == "endpoint")
            is_provider = bool(row and row.kind == "provider")
            actions["edit"].label = tr("common.edit")
            actions["edit"].display = True
            actions["edit"].disabled = not (is_endpoint or is_provider)
            actions["add"].display = True
            actions["add"].disabled = False
            actions["toggle"].display = True
            actions["toggle"].disabled = not (is_endpoint or is_provider)
            actions["delete"].display = True
            actions["delete"].disabled = not (is_endpoint or is_provider)
            actions["authorize"].display = self._tab == "providers"
            actions["authorize"].disabled = not (is_provider and row.name == "expressvpn")
        actions["test"].display = True
        actions["test"].disabled = not bool(self.globals.get("proxy") or "")

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        event.stop()
        self._tab = str(event.tab.id or "proxy-tab-routing").removeprefix("proxy-tab-")
        self.rebuild()
        self.query_one("#proxy-list", OptionList).focus()

    def action_activate(self) -> None:
        row = self._selected()
        if row is None:
            return
        if row.kind == "route":
            self.app.push_screen(_RouteEditor(str(self.globals.get("proxy") or "")), self._save_route)
        elif row.kind == "endpoint":
            current = dict(row.value) if isinstance(row.value, dict) else {"url": row.value, "name": row.name}
            current["name"] = row.name
            self.app.push_screen(_NamedProxyEditor(current), lambda value: self._save_endpoint(value, row.name) if value else None)
        elif row.kind == "provider":
            self._edit_provider(row.name, dict(row.value) if isinstance(row.value, dict) else {})

    def _edit_provider(self, name: str, current: dict[str, Any] | None = None) -> None:
        editor: ModalScreen
        if name == "expressvpn":
            editor = _ExpressProviderEditor(current)
        else:
            editor = _ProviderEditor(name, current)
        self.app.push_screen(
            editor,
            lambda value: self._save_provider(value, name) if value else None,
        )

    def _save_route(self, value: str | None) -> None:
        if value is not None:
            self.globals.set("proxy", value)
            self.rebuild()

    def action_add(self) -> None:
        if self._tab == "routing":
            self.action_activate()
        elif self._tab == "endpoints":
            self.app.push_screen(_NamedProxyEditor(), lambda value: self._save_endpoint(value) if value else None)
        else:
            configured = {row.name for row in self._tab_rows()}
            available = [name for name in PROVIDER_ORDER if name != "basic" and name not in configured]
            if not available:
                self.notify(tr("proxy.notify.all_providers"), timeout=4)
                return
            self.app.push_screen(
                _ProviderTypeEditor(available),
                lambda provider: self._edit_provider(provider) if provider else None,
            )

    def _save_endpoint(self, value: dict[str, Any] | None, old_name: str = "") -> None:
        if not value:
            return
        entries = dict(self.app.config.raw.get("proxies") or {})
        name = str(value.pop("name", "")).strip()
        if old_name and old_name != name:
            entries.pop(old_name, None)
        entries[name] = value
        try:
            self.app.config.save_proxies(entries)
            self.rebuild()
        except Exception as exc:
            self.notify(tr("proxy.notify.save_endpoint", error=exc), severity="error", timeout=8)

    def _save_provider(self, value: dict[str, Any] | None, name: str) -> None:
        if not value:
            return
        entries = dict(self.app.config.raw.get("proxy_providers") or {})
        entries[name] = value
        try:
            self.app.config.save_proxy_providers(entries)
            self.rebuild()
        except Exception as exc:
            self.notify(tr("proxy.notify.save_provider", error=exc), severity="error", timeout=8)

    def action_delete(self) -> None:
        row = self._selected()
        if row is None or row.kind not in {"endpoint", "provider"}:
            self.notify(tr("proxy.notify.delete_kind"), timeout=4)
            return
        entries = dict(self.app.config.raw.get("proxies" if row.kind == "endpoint" else "proxy_providers") or {})
        entries.pop(row.name, None)
        try:
            if row.kind == "endpoint":
                self.app.config.save_proxies(entries)
            else:
                self.app.config.save_proxy_providers(entries)
            self.rebuild()
        except Exception as exc:
            self.notify(tr("proxy.notify.delete_fail", name=row.name, error=exc), severity="error", timeout=8)

    def action_toggle(self) -> None:
        row = self._selected()
        if row is None or row.kind not in {"endpoint", "provider"}:
            if row and row.kind == "download":
                self.globals.set("proxy_downloads", not bool(self.globals.get("proxy_downloads")))
                self.rebuild()
            return
        entries = dict(self.app.config.raw.get("proxies" if row.kind == "endpoint" else "proxy_providers") or {})
        current = entries.get(row.name)
        if isinstance(current, dict):
            updated = dict(current)
            updated["enabled"] = not bool(updated.get("enabled", updated.get("enable", True)))
            updated.pop("enable", None)
        else:
            updated = {"urls": current if isinstance(current, list) else [current], "enabled": False}
        entries[row.name] = updated
        try:
            (self.app.config.save_proxies if row.kind == "endpoint" else self.app.config.save_proxy_providers)(entries)
            self.rebuild()
        except Exception as exc:
            self.notify(tr("proxy.notify.toggle_fail", name=row.name, error=exc), severity="error", timeout=8)

    def action_test(self) -> None:
        route = str(self.globals.get("proxy") or "").strip()
        if not route:
            self.notify(tr("proxy.notify.direct"), timeout=4)
            return
        self._test_route(route)

    def action_authorize(self) -> None:
        row = self._selected()
        if row is None or row.kind != "provider" or row.name != "expressvpn":
            self.notify(tr("proxy.notify.select_express"), timeout=4)
            return
        spec = dict(row.value) if isinstance(row.value, dict) else {}
        client = ExpressVPNClient(spec, self.app.config.paths.tokens / "vpn")
        self.app.push_screen(ExpressLoginScreen(client), lambda _success: self.rebuild())

    @work(thread=True, exclusive=True)
    def _test_route(self, route: str) -> None:
        try:
            resolved = self.app.config.proxy(route)
            self.app.call_from_thread(
                self.notify, tr("proxy.notify.resolves", route=safe_proxy_label(resolved)), timeout=6
            )
        except Exception as exc:
            self.app.call_from_thread(
                self.notify, tr("proxy.notify.test_fail", error=exc), severity="error", timeout=8
            )

    def go_back(self) -> bool:
        self.dismiss(None)
        return True
