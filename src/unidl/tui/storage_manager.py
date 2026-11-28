"""One compact editor for output locations and file-name rules.

The global settings list used to spend five rows on one practical question:
where a file goes and what it is called.  This screen keeps the existing
``settings.json`` and ``unidl.yaml`` keys intact, but gives them one entry point.

Only ordinary output locations are writable here. Service locations and runtime
state paths can be opened without turning inspection into a path migration.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Label, OptionList, Static, Tab, Tabs
from textual.widgets.option_list import Option

from ..core import naming, service_catalog, template
from ..core.i18n import cell_width, phrase, tr
from ..core.output_paths import finished_download_root, render_episode_folder, validate_episode_folder_template
from ..core.settings import Settings
from ..core.terminal_cells import fit_middle
from .bidi import escape_markup, visual_markup
from .chrome import Chrome, CloseMark, KeyBar, fit_modal_card, refresh_locale_widgets
from .input import ClipboardInput as Input
from .settings_layout import SettingRow, label_width, setting_row, stable_help


@dataclass(frozen=True)
class StorageRow:
    key: str
    label: str
    kind: str  # setting-path | config-path | template | folder-template | tag | choice | advanced
    help: str

    @property
    def editable(self) -> bool:
        return self.kind != "advanced"


_OUTPUT_ROWS = (
    StorageRow(
        "download_dir",
        "Finished downloads",
        "setting-path",
        "No YAML editing is required. Empty uses paths.downloads when configured, "
        "otherwise the default output folder. Files are grouped by service.",
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
        "filename_style",
        "Filename format",
        "choice",
        "Choose a ready-made dots, spaces, or hyphens layout. Templates below remain available for fine control.",
    ),
    StorageRow(
        "name_template_episode",
        "Episode file name",
        "template",
        "Fields: title, year, season, episode, season_episode and episode_name.",
    ),
    StorageRow(
        "episode_folder_template",
        "Episode folder layout",
        "folder-template",
        "Empty keeps the flat layout. Examples: {series} or {series}/{season}. "
        "Fields: series, season, season_number, year, season_episode and service.",
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
    StorageRow(
        "unicode_filenames",
        "Preserve Unicode characters",
        "bool",
        "On keeps accents and non-ASCII scripts. Off transliterates filenames to ASCII.",
    ),
    StorageRow(
        "repack_tag",
        "REPACK tag",
        "bool",
        "Include REPACK in the release suffix for films and episodes.",
    ),
    StorageRow(
        "audio_lang_tag",
        "Multi-audio language tag",
        "choice",
        "Automatically include DUAL or MULTI in release names when multiple audio languages are selected.",
    ),
)

_ADVANCED_ROWS = (
    StorageRow("services", "Service directory", "advanced",
               "The services directory of the UniDL installation running now, not another checkout or Python environment."),
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

STORAGE_TABS = {"output": _OUTPUT_ROWS, "naming": _NAMING_ROWS, "advanced": _ADVANCED_ROWS}


class _StorageEditor(ModalScreen[str | None]):
    """Single-line editor with a live resolved-path or file-name preview."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def __init__(
        self,
        row: StorageRow,
        value: str,
        preview: Callable[[str], str],
        validator: Callable[[str], list[str]] | None = None,
    ) -> None:
        super().__init__()
        self.row = row
        self.value = value
        self.preview = preview
        self.validator = validator
        self._discard_armed_at = 0.0

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-card"):
            with Horizontal(classes="modal-head"):
                yield Label(tr(f"storage.row.{self.row.key}", default=self.row.label), classes="modal-card-title")
                yield CloseMark(id="modal-close")
            yield Label(tr(f"storage.row.{self.row.key}.help", default=self.row.help), classes="modal-card-help")
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
            with Horizontal(classes="modal-card-actions"):
                yield Button(phrase("Save"), variant="primary", id="storage-editor-save")
                yield Button(phrase("Cancel"), id="storage-editor-cancel")

    def on_mount(self) -> None:
        field = self.query_one("#storage-editor-input", Input)
        field.focus()
        self._show_preview(field.value)
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)

    def on_resize(self) -> None:
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)

    def _fit_modal(self) -> None:
        fit_modal_card(self, "#modal-card")

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._discard_armed_at = 0.0
        self._show_preview(event.value)
        self.call_after_refresh(self._fit_modal)

    def _show_preview(self, value: str) -> None:
        self.query_one("#storage-editor-preview", Static).update(self.preview(value))

    def _submit(self, value: str) -> None:
        if self.validator is not None:
            problems = self.validator(value)
            if problems:
                self._discard_armed_at = 0.0
                self.notify(
                    "  ·  ".join(problems),
                    title=tr(
                        "storage.notify.not_saved",
                        label=tr(f"storage.row.{self.row.key}", default=self.row.label),
                    ),
                    severity="warning",
                    timeout=8,
                )
                return
        self.dismiss(value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit(event.value)

    def action_save(self) -> None:
        self._submit(self.query_one("#storage-editor-input", Input).value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "storage-editor-save":
            self.action_save()
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        field = self.query_one("#storage-editor-input", Input)
        if field.value != self.value:
            now = time.monotonic()
            if now - self._discard_armed_at > 4.0:
                self._discard_armed_at = now
                self.notify(
                    tr("confirm.discard_unsaved", default="Unsaved changes — press Esc or Cancel again to discard"),
                    severity="warning",
                    timeout=4,
                )
                return
        if self.app.screen is self:
            self.dismiss(None)

    def go_back(self) -> bool:
        self.action_cancel()
        return True


class StorageManagerScreen(Screen[None]):
    """Edit output paths and naming templates without widening global Settings."""

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("b", "app.global_back", "Back", show=False),
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
        yield Tabs(
            Tab(tr("storage.tab.output"), id="storage-tab-output"),
            Tab(tr("storage.tab.naming"), id="storage-tab-naming"),
            Tab(tr("storage.tab.advanced"), id="storage-tab-advanced"),
            id="storage-tabs",
        )
        yield Static("", id="storage-manager-help")
        with Horizontal(id="statusline"):
            yield Static("", id="crumbs-pill", classes="pill")
        with Vertical(id="storage-manager-body"):
            yield Static("", id="storage-preview")
            yield OptionList(id="storage-list")
        yield KeyBar(("enter", "edit"), ("r", "use default"), ("^b", "back"), ("esc", "quit"))

    def on_mount(self) -> None:
        self.rebuild()
        self.call_after_refresh(self.rebuild)
        self.query_one("#storage-list", OptionList).focus()

    def on_resize(self) -> None:
        if self.is_attached:
            self.rebuild()

    def focus_setting(self, key: str) -> None:
        """Reveal a searched setting using this manager's existing validation."""
        for tab, rows in STORAGE_TABS.items():
            if any(row.key == key for row in rows):
                self._tab = tab
                self.query_one("#storage-tabs", Tabs).active = f"storage-tab-{tab}"
                self.rebuild()
                self.query_one("#storage-list", OptionList).highlighted = next(
                    index for index, row in enumerate(self._rows) if row.key == key
                )
                self.query_one("#storage-list", OptionList).scroll_to_highlight()
                return

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self.query_one("#masthead", Static).update(tr("storage.title"))
        self.query_one("#storage-tab-output", Tab).label = tr("storage.tab.output")
        self.query_one("#storage-tab-naming", Tab).label = tr("storage.tab.naming")
        self.query_one("#storage-tab-advanced", Tab).label = tr("storage.tab.advanced")
        self.rebuild()

    def _tab_rows(self) -> tuple[StorageRow, ...]:
        return STORAGE_TABS.get(self._tab, _OUTPUT_ROWS)

    def rebuild(self) -> None:
        option_list = self.query_one("#storage-list", OptionList)
        selected = self._selected()
        wanted = selected.key if selected is not None else ""
        option_list.clear_options()
        rows = self._tab_rows()
        self._rows = list(rows)

        help_by_tab = {
            "output": f"[$muted]{tr('storage.help.output')}[/]",
            "naming": f"[$muted]{tr('storage.help.naming')}[/]",
            "advanced": f"[$muted]{tr('storage.help.advanced')}[/]",
        }
        self.query_one("#storage-manager-help", Static).update(
            stable_help(help_by_tab.get(self._tab, help_by_tab["output"]), help_by_tab.values())
        )
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
        self.query_one(KeyBar).render_pairs(
            *([("enter", "open the folder")] if self._tab == "advanced"
              else [("enter", "edit"), ("r", "use default")]),
            ("^b", "back"), ("esc", "quit"),
        )

    def _row_markup(self, row: StorageRow) -> SettingRow:
        option_list = self.query_one("#storage-list", OptionList)
        value, source = self._display_value(row)
        compact = self.app.has_class("short") or self.size.width < 96
        state_text = tr("storage.read_only") if not row.editable else source
        # Every naming row has the same source, so repeating "settings" is the
        # first thing to give up in a short window.  The value is the useful part,
        # and keeping it on one line matters more than restating where it lives.
        show_state = not (compact and row.editable)
        state = f"  [$dim]{state_text}[/]" if show_state else ""
        label = tr(f"storage.row.{row.key}", default=row.label)
        longest = label_width(
            tr(f"storage.row.{item.key}", default=item.label) for item in self._rows
        )
        # Mirror SettingRow._columns() exactly so shortened editable paths fit on
        # a single row alongside the source badge without wrapping onto line 2.
        list_width = option_list.content_region.width or max(40, (self.size.width or 80) - 6)
        usable_width = max(1, (list_width - 2) - option_list.styles.scrollbar_size_vertical)
        indent = min(4, max(0, usable_width - 5))
        usable = usable_width - indent - 2
        label_col = max(1, min(longest, usable // 2))
        value_col = max(1, usable - label_col)
        state_cost = (2 + cell_width(state_text)) if show_state else 0
        available = max(12, value_col - state_cost)
        shown_value = self._short(value, available) if row.editable else value
        return setting_row(
            option_list,
            label,
            f"[$accent]{escape_markup(shown_value)}[/]{state}",
            longest,
            indent=4,
        )

    @staticmethod
    def _short(value: object, limit: int = 76) -> str:
        text = str(value or "")
        if cell_width(text) <= limit:
            return text
        if limit <= 1:
            return "…"
        return fit_middle(text, limit, head_share=0.5)

    def _display_value(self, row: StorageRow) -> tuple[str, str]:
        if row.kind == "setting-path":
            config = self.app.config
            selected = str(self.globals.get(row.key) or "").strip()
            if selected:
                return str(finished_download_root(config, self.globals)), tr("storage.source.override")
            raw = config.raw.get("paths") or {}
            explicit = config.missing_config is None and isinstance(raw, dict) and "downloads" in raw
            source = "storage.source.yaml" if explicit else "storage.source.builtin"
            return str(config.paths.downloads), tr(source)
        if row.kind == "config-path":
            value = getattr(self.app.config.paths, row.key)
            raw = self.app.config.raw.get("paths") or {}
            explicit = self.app.config.missing_config is None and isinstance(raw, dict) and bool(str(raw.get(row.key) or "").strip())
            return str(value), tr("storage.source.yaml") if explicit else tr("storage.source.home_default")
        if row.kind == "bool":
            value = bool(self.globals.get(row.key))
            return (tr("value.on") if value else tr("value.off")), tr("storage.source.settings")
        if row.kind == "choice":
            value = self.globals.get(row.key)
            spec = self.globals.spec_by_key.get(row.key)
            if spec is not None:
                for option in spec.options:
                    if option.value == value:
                        return option.display(), tr("storage.source.settings")
            return str(value or ""), tr("storage.source.settings")
        if row.kind in {"template", "folder-template", "tag"}:
            value = str(self.globals.get(row.key) or "")
            return value or tr("value.none"), tr("storage.source.settings")
        return str(self._advanced_path(row)), tr("storage.read_only")

    def _advanced_path(self, row: StorageRow) -> Path:
        if row.key == "services":
            return service_catalog.source_root()
        return Path(getattr(self.app.config.paths, row.key)).expanduser().absolute()

    def _open_advanced(self, row: StorageRow) -> None:
        path = self._advanced_path(row)
        # Open a vault's containing folder, never launch SQLite in an editor.
        folder = path.parent if row.key == "keys_db" else path
        try:
            if not folder.is_dir():
                self.notify(tr("storage.notify.path_missing", path=folder), severity="warning", timeout=8)
                return
            self.app.open_path(folder)
        except (OSError, ValueError) as exc:
            self.notify(tr("notify.open_fail", path=folder, error=exc), severity="error", timeout=8)

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
            self._open_advanced(row)
            return

        def finished(value: str | None) -> None:
            if value is not None:
                self._save_value(row, value)

        if row.kind == "bool":
            self._save_value(row, not bool(self.globals.get(row.key)))
            return
        if row.kind == "choice":
            from .settings_screen import _ChoiceEditor

            spec = self.globals.spec_by_key.get(row.key)
            if spec is None:
                return
            self.app.push_screen(_ChoiceEditor(spec, self.globals.get(row.key)), finished)
            return
        self.app.push_screen(
            _StorageEditor(
                row,
                self._editor_value(row),
                lambda value: self._edit_preview(row, value),
                validator=lambda value: self._validate_value(row, value),
            ),
            finished,
        )

    def _validate_value(self, row: StorageRow, value: str) -> list[str]:
        text = str(value or "").strip()
        if row.kind == "template":
            return template.validate(text, _TEMPLATE_FIELDS[row.key])
        if row.kind == "folder-template":
            return validate_episode_folder_template(text)
        if row.kind in {"setting-path", "config-path"} and text:
            try:
                self._normalise_path(text)
            except ValueError as exc:
                return [str(exc)]
        return []

    def action_reset(self) -> None:
        row = self._selected()
        if row is None or not row.editable:
            return
        if row.kind in {"setting-path", "config-path"}:
            value: object = ""
        elif row.kind == "bool":
            spec = self.globals.spec_by_key.get(row.key)
            value = bool(spec.default) if spec is not None else False
        else:
            spec = self.globals.spec_by_key.get(row.key)
            value = str(spec.default if spec is not None and spec.default is not None else "")
        self._save_value(row, value)

    def _save_value(self, row: StorageRow, value: object) -> bool:
        if not row.editable:
            return False
        if row.kind == "bool":
            try:
                self.globals.set(row.key, bool(value))
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
        if row.kind == "folder-template":
            problems = validate_episode_folder_template(text)
            if problems:
                self.notify(
                    "  ·  ".join(problems),
                    title=tr(
                        "storage.notify.not_saved",
                        label=tr(f"storage.row.{row.key}", default=row.label),
                    ),
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
            base = self.app.config.config_path.parent
            path = base / path
        return str(path.resolve(strict=False))

    def _edit_preview(self, row: StorageRow, value: str) -> str:
        if row.kind in {"setting-path", "config-path"}:
            if not str(value or "").strip():
                if row.kind == "setting-path":
                    fallback = str(self.app.config.paths.downloads)
                else:
                    fallback = str(self.app.config.paths.home / row.key)
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
        if row.kind == "folder-template":
            problems = validate_episode_folder_template(str(value or "").strip())
            if problems:
                return f"[$warn]{visual_markup('  ·  '.join(problems))}[/]"
        return self._name_preview({row.key: value}, focus_key=row.key)

    def _name_preview(self, changes: dict[str, str] | None = None, *, focus_key: str = "") -> str:
        values = {
            key: str(self.globals.get(key) or "")
            for key in (
                "name_template_episode",
                "name_template_movie",
                "release_template",
                "release_tag",
                "filename_style",
                "episode_folder_template",
            )
        }
        values.update(changes or {})
        from ..core.titles import Title, TitleKind

        templates = {
            "episode": values["name_template_episode"],
            "movie": values["name_template_movie"],
            "style": values["filename_style"],
        }
        episode = naming.save_name_for(
            Title(
                "example-episode",
                TitleKind.EPISODE,
                "Example Show",
                year="2026",
                season=1,
                episode=2,
                episode_name="The Example",
            ),
            templates,
        )
        movie = naming.save_name_for(
            Title("example-movie", TitleKind.MOVIE, "Example Movie", year="2026"),
            templates,
        )
        try:
            folder = "/".join(
                render_episode_folder(
                    values["episode_folder_template"],
                    Title(
                        "example-episode",
                        TitleKind.EPISODE,
                        "Example Show",
                        year="2026",
                        season=1,
                        episode=2,
                        episode_name="The Example",
                    ),
                    "example-service",
                )
            ) or tr("storage.preview.flat")
        except ValueError as exc:
            folder = str(exc)
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
            style=values["filename_style"],
        )
        episode_line = f"[$dim]{tr('storage.preview.episode')}[/]  {visual_markup(episode + suffix)}"
        film_line = f"[$dim]{tr('storage.preview.film')}[/]     {visual_markup(movie + suffix)}"
        folder_line = f"[$dim]{tr('storage.preview.folder')}[/]  {visual_markup(folder)}"
        if focus_key == "name_template_movie":
            return f"{film_line}\n{episode_line}\n{folder_line}"
        if focus_key == "episode_folder_template":
            return f"{folder_line}\n{episode_line}\n{film_line}"
        return f"{episode_line}\n{film_line}\n{folder_line}"

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


__all__ = ["StorageManagerScreen"]
