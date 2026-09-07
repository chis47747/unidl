"""One compact editor for output locations and file-name rules.

The global settings list used to spend five rows on one practical question:
where a file goes and what it is called.  This screen keeps the existing
``settings.json`` and ``unidl.yaml`` keys intact, but gives them one entry point.

Only ordinary output locations are writable here.  Token, cookie, helper, CDM
and vault paths are identity-bearing state; phase one shows those paths so they
can be found without turning a folder edit into an implicit migration.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Label, OptionList, Static, Tab, Tabs
from textual.widgets.option_list import Option

from ..core import naming, template
from ..core.i18n import tr
from ..core.settings import Settings
from .bidi import visual_markup
from .chrome import Chrome, KeyBar, refresh_locale_widgets
from .input import ClipboardInput as Input


@dataclass(frozen=True)
class StorageRow:
    key: str
    label: str
    kind: str  # setting-path | config-path | template | tag | advanced
    help: str

    @property
    def editable(self) -> bool:
        return self.kind != "advanced"


_OUTPUT_ROWS = (
    StorageRow(
        "download_dir",
        "Finished downloads",
        "setting-path",
        "Downloads and live recordings. Empty uses paths.downloads from unidl.yaml.",
    ),
    StorageRow(
        "subtitles",
        "Service subtitles",
        "config-path",
        "Sidecar subtitles fetched by a service before UniDL muxes them.",
    ),
    StorageRow(
        "commands",
        "Saved commands",
        "config-path",
        "Generated UniDL commands, separated into one folder per service.",
    ),
    StorageRow(
        "exports",
        "Portable exports",
        "config-path",
        "Resolved-title JSON exports containing manifests, tracks and keys.",
    ),
)

_NAMING_ROWS = (
    StorageRow(
        "name_template_episode",
        "Episode file name",
        "template",
        "Fields: title, year, season, episode, season_episode and episode_name.",
    ),
    StorageRow(
        "name_template_movie",
        "Film file name",
        "template",
        "Fields: title, year and episode_name.",
    ),
    StorageRow(
        "release_template",
        "Release suffix",
        "template",
        "Fields: quality, platform, source, audio/audio_full, atmos, video, range and tag.",
    ),
    StorageRow(
        "release_tag",
        "Release tag",
        "tag",
        "Optional group tag written after the final dash.",
    ),
)

_ADVANCED_ROWS = (
    StorageRow("home", "Runtime home", "advanced", "Parent for runtime state whose path is not overridden."),
    StorageRow("cache", "Cache", "advanced", "Disposable service cache."),
    StorageRow("temp", "Temporary files", "advanced", "Segment and resume scratch space."),
    StorageRow("logs", "Logs", "advanced", "Debug and session logs."),
    StorageRow("tokens", "Account sessions", "advanced", "Tokens, device identities and login sessions."),
    StorageRow("cookies", "Browser cookies", "advanced", "Service-scoped browser cookie files."),
    StorageRow("helpers", "External helpers", "advanced", "Machine-local helper modules, binaries and assets."),
    StorageRow("cdm", "Local CDMs", "advanced", "Widevine, PlayReady and other DRM device files."),
    StorageRow("keys_db", "Default key vault", "advanced", "The default local SQLite key vault."),
)

_TEMPLATE_FIELDS = {
    "name_template_episode": naming.TITLE_FIELDS,
    "name_template_movie": naming.TITLE_FIELDS,
    "release_template": naming.RELEASE_FIELDS,
}


class _StorageEditor(ModalScreen[str | None]):
    """Single-line editor with a live resolved-path or file-name preview."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
    ]

    def __init__(
        self,
        row: StorageRow,
        value: str,
        preview: Callable[[str], str],
    ) -> None:
        super().__init__()
        self.row = row
        self.value = value
        self.preview = preview
        self.add_class("editor")

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        with Vertical(id="modal-body"):
            with Vertical(id="modal-card"):
                yield Label(tr(f"storage.row.{self.row.key}", default=self.row.label), classes="ask-title")
                yield Label(tr(f"storage.row.{self.row.key}.help", default=self.row.help), classes="ask-hint")
                yield Input(
                    value=self.value,
                    placeholder=(
                        tr("storage.placeholder.path")
                        if self.row.kind.endswith("path")
                        else tr("storage.placeholder.template")
                    ),
                    id="storage-editor-input",
                    classes="ask-input",
                )
                yield Static("", id="storage-editor-preview")
        yield KeyBar(("enter", "save"), ("^b", "cancel"), ("esc", "cancel"))

    def on_mount(self) -> None:
        field = self.query_one("#storage-editor-input", Input)
        field.focus()
        self._show_preview(field.value)

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._show_preview(event.value)

    def _show_preview(self, value: str) -> None:
        self.query_one("#storage-editor-preview", Static).update(self.preview(value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


class StorageManagerScreen(Screen[None]):
    """Edit output paths and naming templates without widening global Settings."""

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "activate", "Edit", show=True),
        Binding("r", "reset", "Use default", show=True),
    ]

    def __init__(self, globals_scope: Settings) -> None:
        super().__init__()
        self.globals = globals_scope
        self._tab = "output"
        self._rows: list[StorageRow | None] = []

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        yield Static(tr("storage.title"), id="masthead")
        yield Static("", id="storage-manager-help")
        yield Tabs(
            Tab(tr("storage.tab.output"), id="storage-tab-output"),
            Tab(tr("storage.tab.naming"), id="storage-tab-naming"),
            Tab(tr("storage.tab.advanced"), id="storage-tab-advanced"),
            id="storage-tabs",
        )
        with Horizontal(id="statusline"):
            yield Static("", id="crumbs-pill", classes="pill")
        with Vertical(id="storage-manager-body"):
            yield Static("", id="storage-preview")
            yield OptionList(id="storage-list")
        yield KeyBar(("enter", "edit"), ("r", "use default"), ("^b", "back"), ("esc", "quit"))

    def on_mount(self) -> None:
        self.rebuild()
        self.query_one("#storage-list", OptionList).focus()

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self.query_one("#masthead", Static).update(tr("storage.title"))
        self.query_one("#storage-tab-output", Tab).label = tr("storage.tab.output")
        self.query_one("#storage-tab-naming", Tab).label = tr("storage.tab.naming")
        self.query_one("#storage-tab-advanced", Tab).label = tr("storage.tab.advanced")
        self.rebuild()

    def _tab_rows(self) -> tuple[StorageRow, ...]:
        if self._tab == "naming":
            return _NAMING_ROWS
        if self._tab == "advanced":
            return _ADVANCED_ROWS
        return _OUTPUT_ROWS

    def rebuild(self) -> None:
        option_list = self.query_one("#storage-list", OptionList)
        selected = self._selected()
        wanted = selected.key if selected is not None else ""
        option_list.clear_options()
        rows = self._tab_rows()
        self._rows = list(rows)

        help_text = tr(f"storage.help.{self._tab}")
        self.query_one("#storage-manager-help", Static).update(f"[$muted]{help_text}[/]")
        tab_label = {
            "output": tr("storage.tab.output"),
            "naming": tr("storage.tab.naming"),
            "advanced": tr("storage.tab.advanced"),
        }[self._tab]
        self.query_one("#crumbs-pill", Static).update(
            f"[$dim]{tr('storage.entries', tab=tab_label, count=len(rows))}[/]"
        )

        preview = self.query_one("#storage-preview", Static)
        preview.display = self._tab == "naming"
        if preview.display:
            preview.update(self._name_preview())

        for row in rows:
            option_list.add_option(Option(self._row_markup(row)))
        target = next((index for index, row in enumerate(rows) if row.key == wanted), 0)
        option_list.highlighted = target

    def _row_markup(self, row: StorageRow) -> str:
        value, source = self._display_value(row)
        compact = self.app.has_class("short") or self.size.width < 96
        state_text = tr("storage.read_only") if not row.editable else source
        # Every naming row has the same source, so repeating "settings" is the
        # first thing to give up in a short window.  The value is the useful part,
        # and keeping it on one line matters more than restating where it lives.
        show_state = not (compact and row.editable)
        state = f"  [$dim]{state_text}[/]" if show_state else ""
        label = tr(f"storage.row.{row.key}", default=row.label)
        available = max(
            18,
            self.size.width
            - len(label)
            - (len(state_text) if show_state else 0)
            - 12,
        )
        return (
            f"    [$foreground]{visual_markup(label)}[/]  "
            f"[$accent]{visual_markup(self._short(value, available))}[/]{state}"
        )

    @staticmethod
    def _short(value: object, limit: int = 76) -> str:
        text = str(value or "")
        if len(text) <= limit:
            return text
        left = max(1, (limit - 1) // 2)
        right = max(1, limit - left - 1)
        return f"{text[:left]}…{text[-right:]}"

    def _display_value(self, row: StorageRow) -> tuple[str, str]:
        if row.kind == "setting-path":
            selected = str(self.globals.get(row.key) or "").strip()
            if selected:
                return str(Path(selected).expanduser()), tr("storage.source.override")
            return str(self.app.config.paths.downloads), tr("storage.source.yaml_default")
        if row.kind == "config-path":
            value = getattr(self.app.config.paths, row.key)
            raw = self.app.config.raw.get("paths") or {}
            explicit = isinstance(raw, dict) and bool(str(raw.get(row.key) or "").strip())
            return str(value), tr("storage.source.yaml") if explicit else tr("storage.source.home_default")
        if row.kind in {"template", "tag"}:
            value = str(self.globals.get(row.key) or "")
            return value or tr("value.none"), tr("storage.source.settings")
        return str(getattr(self.app.config.paths, row.key)), tr("storage.read_only")

    def _editor_value(self, row: StorageRow) -> str:
        if row.kind == "config-path":
            raw = self.app.config.raw.get("paths") or {}
            return str(raw.get(row.key) or "") if isinstance(raw, dict) else ""
        return str(self.globals.get(row.key) or "")

    def _selected(self) -> StorageRow | None:
        index = self.query_one("#storage-list", OptionList).highlighted
        if index is None or index >= len(self._rows):
            return None
        return self._rows[index]

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        event.stop()
        self._tab = str(event.tab.id or "storage-tab-output").removeprefix("storage-tab-")
        self.rebuild()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_activate()

    def action_activate(self) -> None:
        row = self._selected()
        if row is None:
            return
        if not row.editable:
            self.notify(
                tr("storage.notify.readonly"),
                severity="warning",
                timeout=5,
            )
            return

        def finished(value: str | None) -> None:
            if value is not None:
                self._save_value(row, value)

        self.app.push_screen(
            _StorageEditor(row, self._editor_value(row), lambda value: self._edit_preview(row, value)),
            finished,
        )

    def action_reset(self) -> None:
        row = self._selected()
        if row is None or not row.editable:
            return
        if row.kind in {"setting-path", "config-path"}:
            value = ""
        else:
            spec = self.globals.spec_by_key.get(row.key)
            value = str(spec.default if spec is not None and spec.default is not None else "")
        self._save_value(row, value)

    def _save_value(self, row: StorageRow, value: str) -> bool:
        text = str(value or "").strip()
        if row.kind == "template":
            problems = template.validate(text, _TEMPLATE_FIELDS[row.key])
            if problems:
                self.notify(
                    "  ·  ".join(problems),
                    title=tr("storage.notify.not_saved", label=tr(f"storage.row.{row.key}", default=row.label)),
                    severity="error",
                    timeout=10,
                )
                return False
        if row.kind in {"setting-path", "config-path"} and text:
            try:
                text = self._normalise_path(text)
                Path(text).mkdir(parents=True, exist_ok=True)
            except (OSError, ValueError) as exc:
                self.notify(tr("storage.notify.folder", error=exc), severity="error", timeout=8)
                return False
        try:
            if row.kind == "config-path":
                self.app.config.save_path_overrides({row.key: text or None})
                self.app.config.paths.ensure()
            else:
                self.globals.set(row.key, text)
        except (OSError, ValueError) as exc:
            self.notify(
                tr(
                    "storage.notify.save_fail",
                    label=tr(f"storage.row.{row.key}", default=row.label),
                    error=exc,
                ),
                severity="error",
                timeout=8,
            )
            return False
        self.notify(tr("storage.notify.saved", label=tr(f"storage.row.{row.key}", default=row.label)), timeout=4)
        self.rebuild()
        return True

    def _normalise_path(self, value: str) -> str:
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError("a folder path must be one line")
        path = Path(value).expanduser()
        if not path.is_absolute():
            base = self.app.config.source.parent if self.app.config.source else Path.cwd()
            path = base / path
        return str(path.resolve(strict=False))

    def _edit_preview(self, row: StorageRow, value: str) -> str:
        if row.kind in {"setting-path", "config-path"}:
            if not str(value or "").strip():
                fallback, _source = self._display_value(row)
                return f"[$dim]{tr('storage.preview.default')}[/]  {visual_markup(fallback)}"
            try:
                resolved = self._normalise_path(value)
            except ValueError as exc:
                return f"[$warn]{visual_markup(str(exc))}[/]"
            return f"[$dim]{tr('storage.preview.resolved')}[/]  {visual_markup(resolved)}"
        if row.kind == "template":
            problems = template.validate(str(value or "").strip(), _TEMPLATE_FIELDS[row.key])
            if problems:
                return f"[$warn]{visual_markup('  ·  '.join(problems))}[/]"
        return self._name_preview({row.key: value})

    def _name_preview(self, changes: dict[str, str] | None = None) -> str:
        values = {
            key: str(self.globals.get(key) or "")
            for key in (
                "name_template_episode",
                "name_template_movie",
                "release_template",
                "release_tag",
            )
        }
        values.update(changes or {})
        episode = template.render(
            values["name_template_episode"],
            {
                "title": "Example.Show",
                "year": "2026",
                "season": "01",
                "episode": "02",
                "season_episode": "S01E02",
                "episode_name": "The.Example",
            },
        )
        movie = template.render(
            values["name_template_movie"],
            {
                "title": "Example.Movie",
                "year": "2026",
                "season": "",
                "episode": "",
                "season_episode": "",
                "episode_name": "",
            },
        )
        suffix = naming.release_suffix(
            quality="1080p",
            platform="SERVICE",
            audio="DDP",
            audio_channels="5.1",
            audio_full="DDP5.1",
            atmos="Atmos",
            video="H.265",
            dynamic_range="DV",
            tag=values["release_tag"] or "YOURTAG",
            layout=values["release_template"],
        )
        return (
            f"[$dim]{tr('storage.preview.episode')}[/]  {visual_markup(episode + suffix)}\n"
            f"[$dim]{tr('storage.preview.film')}[/]     {visual_markup(movie + suffix)}"
        )

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


__all__ = ["StorageManagerScreen"]
