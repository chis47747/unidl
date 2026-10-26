"""Opt-in search-first Home; the classic service-grid Home remains the default."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from ..core import exports
from ..core.i18n import cell_width, load_catalogue, setting_help, setting_label, tr
from ..core.service import Service
from ..core.settings import BETA_AUTO_RESULT_TYPES, TRACK_SETTINGS
from ..core.vault import KeyRecord, hex_ids, normalize_hex, split_pair
from .chrome import CloseMark, KeyBar, StatusChip
from .filterbox import matches
from .home import HomeScreen
from .input import ClipboardInput
from .search import copy_key_record, local_search_vault_names

MODES = ("auto", "services", "justwatch", "catalog", "url", "open", "exports", "vault", "add_keys", "settings")
COMMANDS = {
    "auto": "auto", "service": "services", "services": "services",
    "justwatch": "justwatch", "catalog": "catalog", "url": "url", "open": "open",
    "vault": "vault", "settings": "settings",
    "addkeys": "add_keys", "add": "add_keys",
    "exports": "exports", "export": "exports",
}
PAGE_SIZE = 8
AUTO_EXPORT_LIMIT = 3
AUTO_KEY_LIMIT = 3
LOCAL_KEY_LIMIT = 40
MAX_QUERY_CHARS = 64 * 1024
MAX_AUTO_URL_CHARS = 4096


def service_rank(service: type[Service], query: str) -> int:
    names = [service.ID, service.NAME, service.tag(), *service.ALIASES]
    names = [str(name).casefold() for name in names]
    query = query.casefold()
    if query in names:
        return 0
    if any(name.startswith(query) for name in names):
        return 1
    return 2 if matches(query, *names) else 3


def link_matches(services: list[type[Service]], query: str) -> list[type[Service]]:
    """Only real HTTP(S) links are offered to declared URL-capable services."""
    # Service matchers may be third-party regular expressions. Do not run
    # them on unbounded signed URLs on the UI thread; use explicit routing.
    if len(query) > MAX_AUTO_URL_CHARS:
        return []
    try:
        parsed = urlsplit(query)
    except ValueError:
        return []
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return []
    result = []
    for service in services:
        if not service.SUPPORTS_URL:
            continue
        try:
            if service.matches_url(query):
                result.append(service)
        except (TypeError, ValueError):
            continue
    return result


@dataclass(frozen=True)
class SettingHit:
    key: str
    label: str
    help: str
    scope: str
    service: type[Service] | None = None
    aliases: tuple[str, ...] = ()
    target: tuple[str, str] | None = None

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.scope, self.service.ID if self.service else "", self.key


def setting_index(app) -> list[SettingHit]:
    """Declaration metadata only: no account, setting value or client access."""
    from .justwatch_settings import JUSTWATCH_SETTING_ROWS
    from .proxy_manager import PROXY_ROUTING_ROWS
    from .resource_manager import VAULT_POLICY_FIELDS
    from .service_manager import SERVICE_MANAGER_ITEMS
    from .settings_group import SETTINGS_GROUPS
    from .storage_manager import STORAGE_TABS

    english = load_catalogue("en")

    def aliases(*texts: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(text for text in texts if text))

    def spec_hit(spec, scope: str, service=None) -> SettingHit:
        return SettingHit(spec.key, setting_label(spec), setting_help(spec), scope, service,
                          aliases(spec.label, english.get(f"setting.{spec.key}", "")))

    hits = []
    for spec in app.globals.specs:
        if spec.key not in {"download_location_confirmed", "license_after_tracks"}:
            hits.append(spec_hit(spec, "global"))

    def manager_hit(key: str, label_id: str, help_text: str, target: tuple[str, str], *names: str) -> None:
        # Replace the same global setting, preserving its old UI/English names.
        # Page-only actions receive an entry without inventing a persisted setting.
        previous = next((hit for hit in hits if hit.scope == "global" and hit.key == key), None)
        label = tr(label_id, default=english.get(label_id, key))
        hit = SettingHit(
            key, label, help_text or (previous.help if previous else ""), "global",
            aliases=aliases(label, english.get(label_id, ""), *names,
                            previous.label if previous else "", *(previous.aliases if previous else ())),
            target=target,
        )
        if previous is not None:
            hits[hits.index(previous)] = hit
        else:
            hits.append(hit)

    for ident, key in SERVICE_MANAGER_ITEMS:
        title_id = f"service.{ident}.title"
        manager_hit(key, f"service.{ident}.action", tr(f"service.{ident}.help"),
                    ("services", ident), tr(title_id, default=""), english.get(title_id, ""))
    for rows in STORAGE_TABS.values():
        for row in rows:
            manager_hit(row.key, f"storage.row.{row.key}",
                        tr(f"storage.row.{row.key}.help", default=row.help),
                        ("storage", row.key), row.label)
    for key, label_id in JUSTWATCH_SETTING_ROWS:
        manager_hit(key, label_id, "", ("justwatch", key))
    for key, _kind, label_id in PROXY_ROUTING_ROWS:
        manager_hit(key, label_id, tr("proxy.help.routing"), ("proxy", key))
    for tab in ("endpoints", "providers"):
        manager_hit(f"proxy_{tab}", f"proxy.tab.{tab}", tr(f"proxy.help.{tab}"), ("proxy", tab))
    for key, label_id, target in (
        ("drm_system", "resource.row.drm", "drm-system"),
        ("cdm_rules", "resource.row.rules", "cdm-rules"),
        ("cdm_device", "setting.cdm_any", "cdm-choice"),
        ("local_cdm_files", "resource.row.local_files", "cdm-local"),
        ("cdm_devices", "resource.tab.cdm", "cdm"),
        ("key_vaults", "resource.tab.vault", "vault"),
        ("vault_policy", "resource.policy.title", "policy"),
    ):
        help_id = "resource.policy.help" if target == "policy" else f"resource.help.{'vault' if target == 'vault' else 'cdm'}"
        manager_hit(key, label_id, tr(help_id), ("resources", target))

    for key, label_id, _widget in VAULT_POLICY_FIELDS:
        manager_hit(key, label_id, tr("resource.policy.help"), ("vault_policy", key))

    for group, (_title, _help, keys) in SETTINGS_GROUPS.items():
        for key in keys:
            previous = next((hit for hit in hits if hit.scope == "global" and hit.key == key), None)
            if previous is not None:
                manager_hit(key, f"setting.{key}", previous.help, (group, key))

    shared = {spec.key for spec in TRACK_SETTINGS}
    for spec in TRACK_SETTINGS:
        hits.append(spec_hit(spec, "shared"))
    for service in app.services:
        # Use the framework's declaration builder without a Config or a client.
        # Include generated CDM/cookie/proxy/licence fields without enumerating
        # real devices, cookie files, vault credentials or provider API state.
        for spec in Service.setting_specs.__func__(service):
            if spec.key == "license_after_tracks":
                continue
            # Shared declarations have one result followed by a service picker.
            # Provider-specific overrides retain their own labelled result.
            if spec.key in shared and any(spec is item for item in TRACK_SETTINGS):
                continue
            hits.append(spec_hit(spec, "service", service))
    return hits


def find_settings(index: list[SettingHit], query: str) -> list[SettingHit]:
    def normalize(text: str) -> str:
        return re.sub(r"[\W_]+", " ", text.casefold()).strip()

    needle = normalize(query)
    if not needle:
        return []
    terms = needle.split()

    def rank(hit: SettingHit) -> int:
        label, key = normalize(hit.label), normalize(hit.key)
        names = [normalize(name) for name in hit.aliases]
        if needle in {label, key}:
            return 0
        if needle in names:
            return 1
        if any(all(term in name for term in terms) for name in (label, key, *names)):
            return 2
        haystack = normalize(" ".join((hit.label, hit.key, *hit.aliases, hit.help,
                                      hit.service.NAME if hit.service else "",
                                      hit.service.ID if hit.service else "")))
        return 3 if all(term in haystack for term in terms) else 4

    ranked = [(rank(hit), hit) for hit in index]
    ranked.sort(key=lambda item: (item[0], item[1].scope != "global", item[1].label.casefold(), item[1].key))
    return [hit for score, hit in ranked if score < 4]


class _ChoicePicker(ModalScreen[object | None]):
    """Small mouse/keyboard picker shared by modes and service scopes."""

    DEFAULT_CSS = """
    _ChoicePicker { align: center middle; background: $base 75%; padding: 2 0; }
    #beta-picker-card {
        width: 64; max-width: 94%; max-height: 100%;
        border: round $gutter; background: $recessed; padding: 1 2;
    }
    _ChoicePicker.service-picker #beta-picker-card { width: 52; }
    #beta-picker-head { height: 1; }
    #beta-picker-title { width: 1fr; text-style: bold; }
    #beta-picker-filter { height: 1; min-height: 1; padding: 0 1; margin-top: 1; }
    #beta-picker-list { height: 1fr; min-height: 1; max-height: 14; margin-top: 1; }
    _ChoicePicker.service-picker #beta-picker-list { max-height: 8; }
    """
    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("ctrl+b", "cancel", show=False),
        Binding("up", "cursor(-1)", show=False),
        Binding("down", "cursor(1)", show=False),
    ]

    def __init__(self, title: str, choices: list[tuple[object, str, str]], *, filterable: bool = False):
        super().__init__()
        self.heading = title
        self.choices = choices
        self.filterable = filterable
        self.set_class(filterable, "service-picker")
        self._visible: list[object] = []

    def compose(self) -> ComposeResult:
        card = Vertical(id="beta-picker-card")
        # Set the content-sized height before the first layout. Viewport
        # padding and max-height handle resizing without a second fitted frame.
        overhead = 8 if self.filterable else 6
        visible_rows = 8 if self.filterable else 14
        card.styles.height = overhead + min(visible_rows, max(2, len(self.choices)))
        with card:
            with Horizontal(id="beta-picker-head"):
                yield Label(self.heading, id="beta-picker-title")
                yield CloseMark(id="beta-picker-close")
            if self.filterable:
                yield ClipboardInput(placeholder=tr("beta.service_filter"), id="beta-picker-filter")
            yield OptionList(id="beta-picker-list")

    def on_mount(self) -> None:
        self.rebuild("")
        self.query_one("#beta-picker-filter" if self.filterable else "#beta-picker-list").focus()

    def rebuild(self, query: str) -> None:
        options = self.query_one("#beta-picker-list", OptionList)
        options.clear_options()
        self._visible = []
        for value, label, detail in self.choices:
            if not matches(query, label, detail):
                continue
            self._visible.append(value)
            text = Text(label, no_wrap=True, overflow="ellipsis")
            if detail:
                text.append(f"  {detail}", style="dim")
            options.add_option(Option(text))
        if self._visible:
            options.highlighted = 0
        else:
            options.add_option(Option(Text(tr("beta.none")), disabled=True))

    def on_input_changed(self, event: ClipboardInput.Changed) -> None:
        event.stop()
        self.rebuild(event.value)

    def on_input_submitted(self, event: ClipboardInput.Submitted) -> None:
        event.stop()
        self.take()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.take()

    def take(self) -> None:
        index = self.query_one("#beta-picker-list", OptionList).highlighted
        if index is not None and index < len(self._visible):
            self.dismiss(self._visible[index])

    def action_cursor(self, delta: int) -> None:
        options = self.query_one("#beta-picker-list", OptionList)
        (options.action_cursor_down if delta > 0 else options.action_cursor_up)()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.action_cancel()
        return True


class _SearchInput(ClipboardInput):
    BINDINGS = [
        Binding("ctrl+u", "clear_query", show=False),
        Binding("backspace", "delete_query(-1)", show=False),
        Binding("delete", "delete_query(1)", show=False),
    ]

    def replace(self, text: str, start: int, end: int) -> None:
        start, end = sorted((max(0, start), min(len(self.value), end)))
        if len(self.value) - (end - start) + len(text) > MAX_QUERY_CHARS:
            self.notify(tr("beta.input_too_long", limit=MAX_QUERY_CHARS), severity="warning")
            return
        super().replace(text, start, end)

    def action_clear_query(self) -> None:
        self.value = ""

    def action_delete_query(self, direction: int) -> None:
        # Only a new deletion while already empty clears the mode. Deleting
        # the final character, a selection, or text at a boundary edits normally.
        if not self.value:
            self.post_message(self.ModeResetRequested())
        elif direction < 0:
            self.action_delete_left()
        else:
            self.action_delete_right()

    class ModeResetRequested(Message):
        pass

    class ModesRequested(Message):
        pass

    class MoveResult(Message):
        def __init__(self, delta: int):
            super().__init__()
            self.delta = delta

    def on_key(self, event: events.Key) -> None:
        if event.character == "/" and not self.value:
            event.stop()
            event.prevent_default()
            # Retain a literal slash on cancel, so manually typed absolute paths
            # remain possible. Paste events never go through this handler.
            self.value = "/"
            self.cursor_position = 1
            self.post_message(self.ModesRequested())
        elif event.key in {"up", "down"}:
            event.stop()
            event.prevent_default()
            self.post_message(self.MoveResult(1 if event.key == "down" else -1))


@dataclass(frozen=True)
class _ExportHit:
    path: Path
    label: str
    relative: str
    service: str
    count: int
    search_text: str
    readable: bool


@dataclass(frozen=True)
class _Result:
    kind: str
    label: str
    detail: str = ""
    service: type[Service] | None = None
    setting: SettingHit | None = None
    export_path: Path | None = None
    record: KeyRecord | None = None

    @property
    def category(self) -> str | None:
        """Shared provenance for Auto visibility checks and result badges."""
        return {
            "service": "services", "export": "exports", "exports": "exports",
            "key": "vault", "vault": "vault", "setting": "settings",
            "justwatch": "justwatch", "catalog": "catalog",
            "url": "url", "long_url": "url",
        }.get(self.kind)

    @property
    def identity(self):
        if self.record is not None:
            record = self.record
            return self.kind, record.service, record.kid, record.key, record.title, record.created_at
        identity = self.setting.identity if self.setting else str(self.export_path) if self.export_path else self.label
        return self.kind, self.service.ID if self.service else "", identity


class BetaHomeScreen(HomeScreen, inherit_bindings=False):
    USES_SERVICE_GRID = False
    BINDINGS = [
        Binding("ctrl+b", "app.global_back", show=False),
        Binding("ctrl+s", "app.global_settings", show=False),
        Binding("ctrl+f", "app.global_search", show=False),
        Binding("escape", "app.global_quit", show=False),
        Binding("up", "move_result(-1)", show=False),
        Binding("down", "move_result(1)", show=False),
        Binding("ctrl+r", "reload_service", show=False),
        Binding("ctrl+y", "copy_key", show=False),
    ]

    class ExportsIndexed(Message):
        def __init__(self, generation: int, hits: list[_ExportHit], failed: bool):
            super().__init__()
            self.generation, self.hits, self.failed = generation, hits, failed

    class LocalKeysFound(Message):
        def __init__(self, generation: int, records: list[KeyRecord], failed: bool):
            super().__init__()
            self.generation, self.records, self.failed = generation, records, failed

    def __init__(self):
        super().__init__()
        self.search_mode = "auto"
        self.catalog_service: type[Service] | None = None
        self.target_service: type[Service] | None = None
        self._results: list[_Result] = []
        self._index: list[SettingHit] = []
        self._index_signature = None
        self._page_size = PAGE_SIZE
        self._navigated = False
        self._export_hits: list[_ExportHit] = []
        self._export_folder: Path | None = None
        self._export_generation = 0
        self._exports_loading = False
        self._exports_failed = False
        self._local_key_signature = None
        self._local_key_generation = 0
        self._local_key_records: list[KeyRecord] = []
        self._local_keys_loading = False
        self._local_keys_failed = False

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with Vertical(id="beta-body"):
            with VerticalScroll(id="setup-notes"):
                yield StatusChip("open_download_location", id="download-setup-note")
                yield StatusChip("pick_cdm", id="setup-note")
                yield StatusChip("app.global_settings", id="service-setup-note")
            with Horizontal(id="beta-content"):
                with Vertical(id="beta-column"):
                    yield Static(tr("beta.title"), id="beta-heading")
                    with Horizontal(id="beta-card"):
                        yield Button("", id="beta-mode")
                        yield _SearchInput(id="filter", max_length=MAX_QUERY_CHARS)
                        yield Button("×", id="beta-clear", tooltip=tr("beta.clear"))
                        yield Button(tr("beta.submit"), id="beta-submit")
                    yield Static(tr("beta.hint"), id="beta-hint")
                    yield Static("", id="beta-summary")
                    yield Static("", id="beta-empty")
                    yield OptionList(id="beta-results")
        yield KeyBar(*self._key_pairs())

    def _key_pairs(self) -> tuple[tuple[str, str], ...]:
        return (
            ("enter", tr("beta.submit")), ("/", tr("beta.commands")),
            ("↑↓", "move"), ("^f", "search"), ("^s", "settings"), ("^p", "commands"),
        ) + ((("^r", "look again"),) if self.search_mode in {"exports", "vault"} else ()) + (
            (("^y", "copy key"),) if self.search_mode in {"auto", "vault"} else ()
        )

    def on_mount(self, event: events.Mount) -> None:
        # Rich prompts carry literal inks, so repaint them on a live theme
        # change as well as on query/locale updates.
        self.app.theme_changed_signal.subscribe(self, lambda _theme: self.rebuild())
        self._render_caption()
        self.refresh_banner()
        self.refresh_env()
        self._fit_beta()
        self._render_mode()
        self.rebuild()
        self.focus_search()
        self.check_updates()

    def on_resize(self, event: events.Resize) -> None:
        self._fit_beta()

    def _fit_beta(self) -> None:
        self.set_class(self.size.width < 80, "narrow")
        self.set_class(self.size.height < 32, "compact")

    def _fit_statusline(self) -> None:
        super()._fit_statusline()
        columns = self.query("#beta-column")
        rows = self.query("#statusline")
        # Readiness/import workers may finish while the screen is unmounting:
        # the header can already be gone even if the body is still attached.
        if not columns or not rows:
            return
        available = max(1, (self.size.width or self.app.size.width) - 4)
        visible = [child for child in rows.first().children if child.display]
        width = available
        if available >= 76 and visible:
            # Match the visible CDM-to-import text span, not the full-width
            # status container. Measure before layout so the first paint fits.
            width = sum(cell_width(child.render().plain) + child.styles.gutter.width
                        + child.styles.margin.width for child in visible)
            width -= visible[0].styles.padding.left + visible[-1].styles.padding.right
            width = min(available, max(72, width))
        columns.first().styles.width = width

    def on_screen_resume(self, event) -> None:
        self.relocalize()

    def refresh_after_settings(self) -> None:
        self.refresh_env()
        self._refresh_local_keys(force=True)
        self.rebuild()

    def relocalize(self) -> None:
        from .chrome import refresh_locale_widgets

        refresh_locale_widgets(self)
        self._render_caption()
        self.refresh_env()
        self.query_one("#beta-heading", Static).update(tr("beta.title"))
        self.query_one("#beta-hint", Static).update(tr("beta.hint"))
        submit = self.query_one("#beta-submit", Button)
        submit.label = tr("beta.submit")
        submit.refresh(layout=True)
        self.query_one("#beta-clear", Button).tooltip = tr("beta.clear")
        self.query_one(KeyBar).render_pairs(*self._key_pairs())
        self._render_mode()
        if self.search_mode == "exports" or self.search_mode == "auto" and self._auto_shows("exports") and self._export_folder is not None:
            self._refresh_exports(force=True)
        self._refresh_local_keys(force=True)
        self.rebuild()

    def _service_scope(self) -> type[Service] | None:
        attribute = {"catalog": "catalog_service", "open": "target_service"}.get(self.search_mode)
        if attribute is None:
            return None
        previous = getattr(self, attribute)
        if previous is None:
            return None
        # A setting change can unregister a service, and a code reload can
        # replace its class. Never keep routing to a stale or disabled scope.
        service = next((item for item in self.app.services if item.ID == previous.ID), None)
        supported = service is not None and (
            service.SUPPORTS_URL if self.search_mode == "open" else service.SUPPORTS_SEARCH
        )
        setattr(self, attribute, service if supported else None)
        return getattr(self, attribute)

    def _render_mode(self) -> None:
        label = tr(f"beta.mode.{self.search_mode}")
        caption = label
        service = self._service_scope()
        if service is not None:
            label += f" · {service.NAME}"
            # Keep the explicitly chosen service visible in the narrower button.
            caption = f"{tr('keys.open')} · {service.NAME}" if self.search_mode == "open" else label
        button = self.query_one("#beta-mode", Button)
        button.label = Text(caption + " ▾")
        button.tooltip = label
        # Button.label repaints but does not request new auto-width layout.
        # A chosen scope must not stay clipped to the old, shorter "Auto" width.
        button.refresh(layout=True)
        self.query_one("#filter", _SearchInput).placeholder = tr(f"beta.placeholder.{self.search_mode}")

    def action_focus_search(self) -> None:
        self.focus_search()

    def focus_search(self) -> None:
        self.query_one("#filter", _SearchInput).focus()

    def on__search_input_modes_requested(self, event: _SearchInput.ModesRequested) -> None:
        event.stop()
        self.choose_mode()

    def on__search_input_mode_reset_requested(self, event: _SearchInput.ModeResetRequested) -> None:
        event.stop()
        if self.query_one("#filter", _SearchInput).value or self.search_mode == "auto":
            return
        if self.search_mode == "catalog":
            self.catalog_service = None
        elif self.search_mode == "open":
            self.target_service = None
        self.set_mode("auto")

    def on__search_input_move_result(self, event: _SearchInput.MoveResult) -> None:
        event.stop()
        self.action_move_result(event.delta)

    def choose_mode(self) -> None:
        def chosen(value):
            if value not in MODES:
                self.focus_search()
                return
            if self.query_one("#filter", _SearchInput).value == "/":
                self.query_one("#filter", _SearchInput).value = ""
            self.set_mode(value)

        self.app.push_screen(_ChoicePicker(tr("beta.commands"), [
            (mode, tr(f"beta.mode.{mode}"), "/" + {"services": "service", "add_keys": "addkeys"}.get(mode, mode))
            for mode in MODES
        ]), chosen)

    def set_mode(self, mode: str) -> None:
        self.search_mode = mode
        self._navigated = False
        self._page_size = PAGE_SIZE
        self._render_mode()
        if mode == "exports":
            self._refresh_exports(force=True)
        self.query_one(KeyBar).render_pairs(*self._key_pairs())
        self.rebuild(preserve_selection=False)
        self.focus_search()
        if mode in {"catalog", "open"}:
            self.choose_service(for_open=mode == "open")

    def choose_service(
        self, *, run_query: str | None = None, setting: SettingHit | None = None,
        for_open: bool = False,
    ) -> None:
        candidates = [
            service for service in self.app.services
            if setting is not None or (service.SUPPORTS_URL if for_open else service.SUPPORTS_SEARCH)
        ]

        def chosen(service):
            if service is None:
                self.focus_search()
                return
            if setting is not None:
                self._open_setting(setting, service)
                return
            if for_open:
                self.target_service = service
            else:
                self.catalog_service = service
            self._render_mode()
            self.rebuild()
            self.focus_search()
            if run_query:
                if for_open:
                    self.app.open_service(service, target=run_query)
                else:
                    self.app.open_service(service, search=run_query)

        self.app.push_screen(_ChoicePicker(
            tr("beta.choose_service"),
            [(service, service.NAME, service.ID) for service in candidates],
            filterable=True,
        ), chosen)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "beta-mode":
            self.choose_mode()
        elif event.button.id == "beta-submit":
            self.submit()
        elif event.button.id == "beta-clear":
            self.query_one("#filter", _SearchInput).action_clear_query()
            self.focus_search()

    def on_input_changed(self, event: _SearchInput.Changed) -> None:
        event.stop()
        event.prevent_default()
        # A burst of paste/edit events can queue stale values. Rebuild only
        # for the current value, without delaying Enter or the clear control.
        if event.value != self.query_one("#filter", _SearchInput).value:
            return
        self._navigated = False
        self._page_size = PAGE_SIZE
        self.rebuild(preserve_selection=False)

    def on_input_submitted(self, event: _SearchInput.Submitted) -> None:
        event.stop()
        event.prevent_default()
        self.submit()

    def _query(self) -> str:
        return self.query_one("#filter", _SearchInput).value.strip()

    @property
    def key_candidate(self) -> str:
        """Only the explicit writer mode may seed manual entry from Home."""
        return self._query() if self.search_mode == "add_keys" and self.is_mounted else ""

    def _setting_hits(self, query: str) -> list[SettingHit]:
        signature = (tuple((service.ID, id(service)) for service in self.app.services),
                     str(self.app.globals.get("interface_locale")))
        if signature != self._index_signature:
            self._index = setting_index(self.app)
            self._index_signature = signature
        return find_settings(self._index, query)

    def _setting_row(self, hit: SettingHit) -> _Result:
        manager_labels = {
            "services": "settings.services.title", "storage": "storage.title",
            "justwatch": "justwatch.title", "proxy": "proxy.title", "resources": "resource.title",
            "download_behavior": "settings.group.download", "interface": "settings.group.interface",
            "vault_policy": "resource.policy.title",
        }
        scope = hit.service.NAME if hit.service else tr(
            manager_labels[hit.target[0]] if hit.target else
            "beta.shared" if hit.scope == "shared" else "settings.global",
        )
        return _Result("setting", hit.label, f"{scope} · {hit.key}", setting=hit)

    def _key_query(self, query: str) -> bool:
        return bool(normalize_hex(query) or split_pair(query) or hex_ids(query))

    def _auto_shows(self, kind: str) -> bool:
        return kind in (self.app.globals.get("beta_auto_results", BETA_AUTO_RESULT_TYPES) or ())

    def _refresh_local_keys(self, *, force: bool = False) -> None:
        query = self._query()
        eligible = bool(query and query != "/" and (
            self.search_mode == "vault" or self.search_mode == "auto" and self._auto_shows("vault")
            and not query.startswith(("/", "~", "\\"))
            and not query[:8].lower().startswith(("http://", "https://"))
        ))
        names = local_search_vault_names(self.app) if eligible else ()
        signature = (query, names, id(self.app.vaults)) if eligible else None
        if not force and signature == self._local_key_signature:
            return
        self._local_key_signature = signature
        self._local_key_generation += 1
        self._local_key_records = []
        self._local_keys_loading, self._local_keys_failed = bool(eligible and names != ()), False
        if self._local_keys_loading:
            self._search_local_keys(query, names, self._local_key_generation)
        else:
            self.workers.cancel_group(self, "beta-local-key-search")

    @work(exclusive=True, group="beta-local-key-search", description="Search selected local vaults")
    async def _search_local_keys(self, query: str, names: tuple[str, ...] | None, generation: int) -> None:
        # Coalesce rapid edits, then perform only local SQLite work off the UI
        # thread. Generation checks discard results from an earlier query/mode.
        await asyncio.sleep(0.1)
        failed = False
        try:
            records = await asyncio.to_thread(
                self.app.vaults.find_local, query, limit=LOCAL_KEY_LIMIT, local_names=names,
            )
        except Exception:  # noqa: BLE001 - a local database error belongs in the UI, not a crash
            records, failed = [], True
        self.post_message(self.LocalKeysFound(generation, records, failed))

    def on_beta_home_screen_local_keys_found(self, event: LocalKeysFound) -> None:
        event.stop()
        if event.generation != self._local_key_generation:
            return
        self._local_key_records = event.records
        self._local_keys_loading, self._local_keys_failed = False, event.failed
        results = self.query("#beta-results")
        if self.search_mode in {"auto", "vault"} and results:
            self.rebuild(preserve_selection=self._navigated or results.first().has_focus)

    def _local_key_rows(self) -> list[_Result]:
        return [_Result("key", record.pair(),
                        " · ".join(part for part in (
                            tr("search.section.local_keys"), record.service,
                            record.title or tr("search.unknown_title"), record.created_at[:10],
                        ) if part), record=record) for record in self._local_key_records]

    def _refresh_exports(self, *, force: bool = False) -> None:
        folder = Path(self.app.config.paths.exports)
        if folder == self._export_folder and (self._exports_loading or not force):
            return
        if folder != self._export_folder:
            self._export_hits = []
        self._export_folder = folder
        self._export_generation += 1
        self._exports_loading, self._exports_failed = True, False
        self._index_exports(folder, self._export_generation)

    @work(thread=True, exclusive=True, group="beta-export-index")
    def _index_exports(self, folder: Path, generation: int) -> None:
        hits: list[_ExportHit] = []
        failed = False
        worker = get_current_worker()
        try:
            for path, document, _problem in exports.scan(folder):
                if worker.is_cancelled:
                    return
                relative = str(path.relative_to(folder))
                service = (document.service_name or document.service) if document else ""
                labels = [entry.label() for entry in document.entries] if document else []
                titles = [entry.title.full_label() for entry in document.entries if entry.title] if document else []
                # Keep only display/search metadata here, never keys, headers,
                # cookies or manifest URLs. Selection re-reads the source file.
                hits.append(_ExportHit(
                    path, labels[0] if labels else path.stem, relative, service,
                    len(labels), " ".join([relative, service, document.service if document else "",
                                          *labels, *titles]), document is not None,
                ))
        except Exception:  # noqa: BLE001 - a failed folder scan must not take down the TUI
            failed = True
        if not worker.is_cancelled:
            self.post_message(self.ExportsIndexed(generation, hits, failed))

    def on_beta_home_screen_exports_indexed(self, event: ExportsIndexed) -> None:
        event.stop()
        if event.generation != self._export_generation:
            return
        self._export_hits = event.hits
        self._exports_loading, self._exports_failed = False, event.failed
        results = self.query("#beta-results")
        if self.search_mode in {"auto", "exports"} and results:
            # A cold Auto scan initially shows fallback search actions. Prefer
            # the new first match unless the user has already chosen a result.
            preserve = self.search_mode == "exports" or self._navigated or results.first().has_focus
            self.rebuild(preserve_selection=preserve)

    def _export_rows(self, query: str) -> list[_Result]:
        return [
            _Result("export", hit.label,
                    " · ".join(part for part in (
                        hit.service, tr("delivery.titles_n", count=hit.count) if hit.readable else tr("import.cannot"),
                        hit.relative,
                    ) if part), export_path=hit.path)
            for hit in self._export_hits if matches(query, hit.search_text)
        ]

    async def action_reload_service(self) -> None:
        if self.search_mode == "exports":
            self._refresh_exports(force=True)
            self.rebuild()
        elif self.search_mode == "vault":
            self._refresh_local_keys(force=True)
            self.rebuild()
        else:
            await super().action_reload_service()

    def rebuild(self, *, preserve_selection: bool = True) -> None:
        found = self.query("#beta-results")
        if not found:
            return
        options = found.first(OptionList)
        field = self.query_one("#filter", _SearchInput)
        self.query_one("#beta-clear", Button).set_class(bool(field.value), "has-query")
        self._refresh_local_keys()
        previous = options.highlighted if preserve_selection else None
        wanted = self._results[previous].identity if previous is not None and previous < len(self._results) else None
        scroll_y = options.scroll_y if preserve_selection else 0
        query = self._query()
        mode = self.search_mode
        rows = []
        if mode == "add_keys":
            rows = [_Result("add_keys", tr("beta.action.add_keys"))]
        elif mode == "exports":
            self._refresh_exports()
            rows = self._export_rows(query)
        elif query and query != "/":
            if mode == "url" or mode == "auto" and query[:8].lower().startswith(("http://", "https://")):
                if mode == "auto" and not self._auto_shows("url"):
                    rows = []
                elif len(query) > MAX_AUTO_URL_CHARS and query[:8].lower().startswith(("http://", "https://")):
                    rows = [_Result("long_url", tr("beta.action.open"), tr("beta.long_url"))]
                else:
                    rows = [_Result("url", service.NAME, service.ID, service=service)
                            for service in link_matches(self.app.services, query)]
            elif mode == "vault" or mode == "auto" and self._key_query(query):
                if mode == "vault" or self._auto_shows("vault"):
                    key_rows = self._local_key_rows()
                    if mode == "auto":
                        key_rows = key_rows[:AUTO_KEY_LIMIT]
                    rows = [_Result("vault", tr("beta.action.vault")), *key_rows]
            elif mode == "justwatch":
                rows = [_Result("justwatch", tr("beta.action.justwatch"))]
            elif mode in {"catalog", "open"}:
                service = self._service_scope()
                label = service.NAME if service else tr("beta.choose_service")
                rows = [_Result(mode, tr(f"beta.action.{mode}"), label)]
            elif mode == "settings":
                rows = [self._setting_row(hit) for hit in self._setting_hits(query)]
            elif mode in {"auto", "services"}:
                services = [service for service in self.app.home_services if service_rank(service, query) < 3
                            and (mode == "services" or self._auto_shows("services"))]
                services.sort(key=lambda service: (service_rank(service, query), service.NAME.casefold()))
                rows = [_Result("service", service.NAME, service.ID, service=service) for service in services]
                if mode == "auto" and not query.startswith(("/", "~", "\\")):
                    key_rows = self._local_key_rows()[:AUTO_KEY_LIMIT] if self._auto_shows("vault") else []
                    rows = rows[:3 if key_rows else 5]
                    rows.extend(key_rows)
                    if key_rows:
                        rows.append(_Result("vault", tr("beta.action.vault")))
                    if self._auto_shows("exports"):
                        self._refresh_exports()
                        export_rows = self._export_rows(query)
                        rows.extend(export_rows[:AUTO_EXPORT_LIMIT])
                        if len(export_rows) > AUTO_EXPORT_LIMIT:
                            rows.append(_Result("exports", tr("beta.mode.exports"),
                                                tr("beta.results", count=len(export_rows))))
                    if self._auto_shows("settings"):
                        rows.extend(self._setting_row(hit) for hit in self._setting_hits(query)[:3])
                    for kind in ("justwatch", "catalog"):
                        if self._auto_shows(kind):
                            rows.append(_Result(kind, tr(f"beta.action.{kind}")))
        visible = rows[:self._page_size]
        if len(rows) > len(visible):
            visible.append(_Result("more", tr("search.load_more"), str(len(rows) - len(visible))))
        self._results = visible
        options.clear_options()
        for row in visible:
            options.add_option(Option(self._result_prompt(row)))
        options.display = bool(visible)
        empty = self.query_one("#beta-empty", Static)
        empty.display = bool(not visible and (mode == "exports" or query and query != "/"))
        if empty.display:
            message = "beta.none"
            if mode == "exports":
                if self._exports_loading:
                    message = "beta.exports_loading"
                elif self._exports_failed:
                    message = "beta.exports_failed"
                elif not query:
                    message = "import.empty"
            elif mode == "url" and query:
                message = "beta.no_url_match"
            empty.update(tr(message))
        if visible:
            options.highlighted = next((i for i, row in enumerate(visible) if row.identity == wanted), 0)
            options.scroll_to(y=scroll_y, animate=False)
        summary = self.query_one("#beta-summary", Static)
        summary.display = bool(visible or mode == "vault" and query)
        status = tr("beta.results", count=len(rows)) if visible else ""
        if mode == "vault" and query:
            if self._local_keys_loading:
                status = tr("beta.local_keys_loading")
            elif self._local_keys_failed:
                status = tr("beta.local_keys_failed")
            elif self._local_key_signature and self._local_key_signature[1] == ():
                status = tr("search.vault_off")
            elif not self._local_key_records:
                status = tr("search.no_key_matches")
        summary.update(status)

    def _result_prompt(self, row: _Result) -> Text:
        text = Text(no_wrap=True, overflow="ellipsis") if row.kind in {"export", "key"} else Text()
        if self.search_mode == "auto" and row.category is not None:
            text.append(
                f"[{tr(f'beta.category.{row.category}')}] ",
                style=getattr(self.app.palette, f"search_{row.category}"),
            )
        label_start = len(text)
        # Provider titles and filenames are plain text, never Rich markup.
        text.append(row.label)
        if row.kind == "key" and row.record is not None:
            text.stylize(self.app.palette.accent, label_start + len(row.record.kid) + 1, len(text))
        if row.detail:
            text.append(f"\n  {row.detail}", style="dim")
        return text

    def build(self) -> None:
        self.rebuild()

    def action_move_result(self, delta: int) -> None:
        options = self.query_one("#beta-results", OptionList)
        if not self._results:
            return
        if not self._navigated:
            options.highlighted = 0 if delta > 0 else len(self._results) - 1
        else:
            (options.action_cursor_down if delta > 0 else options.action_cursor_up)()
        self._navigated = True

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_index < len(self._results):
            self.activate(self._results[event.option_index])

    def submit(self) -> None:
        query = self._query()
        if query.startswith("/"):
            token, _, rest = query.partition(" ")
            mode = COMMANDS.get(token[1:].lower())
            if mode is not None:
                self.query_one("#filter", _SearchInput).value = rest.lstrip()
                self.set_mode(mode)
                return
        if self.search_mode == "add_keys":
            self.activate(_Result("add_keys", ""))
            return
        if self.search_mode == "exports":
            if self._navigated:
                index = self.query_one("#beta-results", OptionList).highlighted
                if index is not None and index < len(self._results):
                    self.activate(self._results[index])
            elif self._results:
                self.query_one("#beta-results").focus()
            return
        if not query or query == "/":
            return
        if self.search_mode == "url" or self.search_mode == "auto" and self._auto_shows("exports"):
            raw_path = query[1:-1] if len(query) > 1 and query[0] == query[-1] and query[0] in "\"'" else query
            if self._import_path(raw_path, clear_input=False):
                return
        if self._navigated:
            index = self.query_one("#beta-results", OptionList).highlighted
            if index is not None and index < len(self._results):
                self.activate(self._results[index])
            return
        mode = self.search_mode
        # Match rebuild's URL-first precedence: a signed link can contain a
        # KID, but it must never become a vault query on Enter.
        if mode == "url" or mode == "auto" and query[:8].lower().startswith(("http://", "https://")):
            if mode == "auto" and not self._auto_shows("url"):
                return
            if len(query) > MAX_AUTO_URL_CHARS and query[:8].lower().startswith(("http://", "https://")):
                self.activate(_Result("long_url", ""))
                return
            candidates = [row for row in self._results if row.kind == "url"]
            if len(candidates) == 1:
                self.activate(candidates[0])
            elif not candidates:
                self.notify(tr("beta.no_url_match"), severity="warning")
            else:
                self.query_one("#beta-results").focus()
            return
        if mode == "vault" or mode == "auto" and self._key_query(query):
            if mode == "vault" or self._auto_shows("vault"):
                self.activate(_Result("vault", ""))
            return
        if mode == "justwatch":
            self.activate(_Result("justwatch", ""))
            return
        if mode in {"catalog", "open"}:
            self.activate(_Result(mode, ""))
            return
        exact = [row for row in self._results if row.kind == "service" and service_rank(row.service, query) == 0]
        if mode in {"auto", "services"} and len(exact) == 1:
            self.activate(exact[0])
            return
        if self._results:
            self.query_one("#beta-results").focus()

    def activate(self, row: _Result) -> None:
        if self.search_mode == "auto":
            if row.category is not None and not self._auto_shows(row.category):
                self.rebuild()
                return
        query = self._query()
        if row.kind == "more":
            first_new = len(self._results) - 1
            self._page_size += PAGE_SIZE
            self.rebuild()
            options = self.query_one("#beta-results", OptionList)
            options.highlighted = first_new
            options.scroll_to_highlight()
        elif row.kind == "service":
            self.app.open_service(row.service)
        elif row.kind == "exports":
            self.set_mode("exports")
        elif row.kind == "export" and row.export_path is not None:
            # Reuse the path-import boundary: batches get the existing title
            # picker, single titles enter the existing delivery session.
            if not self._import_path(str(row.export_path), clear_input=False):
                self.notify(tr("import.cannot"), severity="error")
                self._refresh_exports(force=True)
        elif row.kind == "url":
            self.app.open_service(row.service, target=query)
        elif row.kind == "long_url":
            self.choose_service(run_query=query, for_open=True)
        elif row.kind == "justwatch":
            from .justwatch_screen import JustWatchScreen

            self.app.push_screen(JustWatchScreen(query))
        elif row.kind == "vault":
            from .search import GlobalSearchScreen

            self.app.push_screen(GlobalSearchScreen(query, vault_only=True))
        elif row.kind == "key" and row.record is not None:
            copy_key_record(self.app, row.record, self.notify)
        elif row.kind == "add_keys":
            self.app.action_add_keys()
        elif row.kind in {"catalog", "open"}:
            service = self._service_scope()
            if service is not None:
                if row.kind == "open":
                    self.app.open_service(service, target=query)
                else:
                    self.app.open_service(service, search=query)
            else:
                self.choose_service(run_query=query, for_open=row.kind == "open")
        elif row.kind == "setting":
            if row.setting.scope == "shared":
                self.choose_service(setting=row.setting)
            else:
                self._open_setting(row.setting, row.setting.service)

    def _open_setting(self, hit: SettingHit, service_cls: type[Service] | None = None) -> None:
        from .settings_screen import SettingsScreen

        if service_cls is None and hit.target is not None:
            manager, key = hit.target
            if manager == "services":
                from .service_manager import ServicesManagerScreen

                screen = ServicesManagerScreen(self.app.globals, focus_key=key)
            elif manager == "storage":
                from .storage_manager import StorageManagerScreen

                screen = StorageManagerScreen(self.app.globals)
            elif manager == "justwatch":
                from .justwatch_settings import JustWatchSettingsScreen

                screen = JustWatchSettingsScreen(self.app.globals)
            elif manager == "proxy":
                from .proxy_manager import ProxyManagerScreen

                screen = ProxyManagerScreen(self.app.globals)
            elif manager in {"download_behavior", "interface"}:
                from .settings_group import SETTINGS_GROUPS, SettingsGroupScreen

                title, description, keys = SETTINGS_GROUPS[manager]
                screen = SettingsGroupScreen(self.app.globals, title, description, keys)
            elif manager == "vault_policy":
                from .resource_manager import VaultPolicyScreen

                screen = VaultPolicyScreen(self.app.globals)
            else:
                from .resource_manager import ResourceManagerScreen

                screen = ResourceManagerScreen()
            self.app.push_screen(screen)
            if manager != "services":
                screen.call_after_refresh(screen.focus_setting, key)
            return
        service = None
        if service_cls is not None:
            try:
                service = self.app.registry.build(
                    service_cls, self.app.config, self.app.settings_store,
                    globals_scope=self.app.globals,
                )
            except Exception as exc:
                self.notify(str(exc), severity="error")
                return
        self.app.push_screen(SettingsScreen(service, self.app.globals, focus_key=hit.key))

    def _reload_target(self) -> type[Service] | None:
        index = self.query_one("#beta-results", OptionList).highlighted
        if index is not None and index < len(self._results):
            return self._results[index].service or self._service_scope()
        return self._service_scope()

    def action_copy_key(self) -> None:
        if self.search_mode == "auto" and not self._auto_shows("vault"):
            return
        index = self.query_one("#beta-results", OptionList).highlighted
        if index is not None and index < len(self._results):
            row = self._results[index]
            if row.kind == "key" and row.record is not None:
                copy_key_record(self.app, row.record, self.notify)

    async def _rebuild_services(self, needle: str) -> None:
        self._index_signature = None
        self._render_mode()
        self.rebuild()

    def go_back(self) -> bool:
        options = self.query_one("#beta-results", OptionList)
        if options.has_focus:
            self.focus_search()
        elif self._query():
            self.query_one("#filter", _SearchInput).value = ""
        elif self.search_mode != "auto":
            self.set_mode("auto")
        self.focus_search()
        return True
