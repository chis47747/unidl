"""Compact managers for the few global settings that form one user decision."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ..core.i18n import setting_help, setting_label, setting_value, tr
from ..core.settings import Setting, Settings
from .bidi import escape_markup
from .chrome import Chrome, KeyBar
from .settings_layout import SettingRow, is_setting_modified, label_width, setting_row

SETTINGS_GROUPS = {
    "download_behavior": (
        "settings.group.download", "settings.group.download_help",
        (
            "after_resolve", "live_record", "confirm_batch", "retries", "http_timeout",
            "max_speed", "title_delay", "muxer", "subtitle_output", "segment_downloader", "resume_parts",
            "check_segments_count", "verify_content_keys", "keep_temp",
            "keep_failed_intermediates", "delete_temp_after_done", "auto_subtitle_fix",
            "live_real_time_merge", "live_keep_segments", "live_pipe_mux",
        ),
    ),
    "interface": (
        "settings.group.interface", "settings.group.interface_help",
        (
            "theme",
            "accent_color",
            "title_color",
            "layout_density",
            "semantic_colors",
            "completion_alert",
            "poster_preview",
            "interface_locale",
            "beta_interface",
            "beta_auto_results",
            "debug",
        ),
    ),
}


class SettingsGroupScreen(Screen[None]):
    """Edit a small, named group while keeping each stored setting independent."""

    BINDINGS = [
        Binding("b", "app.global_back", "Back", show=False),
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "activate", "Change", show=True),
        Binding("r", "reset", "Reset", show=True),
        Binding("m", "toggle_modified", "Modified only", show=True),
    ]

    def __init__(
        self,
        scope: Settings,
        title: str,
        description: str,
        keys: tuple[str, ...],
    ) -> None:
        super().__init__()
        self.scope = scope
        self.title = title
        self.description = description
        self.specs = [scope.spec_by_key[key] for key in keys if key in scope.spec_by_key]
        self._visible_specs: list[Setting] = list(self.specs)
        self._modified_only = False
        self._label_width = 30

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        yield Static(tr(self.title, default=self.title), id="masthead")
        yield Static(tr(self.description, default=self.description), id="settings-group-help")
        with Vertical(id="body"):
            yield OptionList(id="settings-group-list")
        yield KeyBar(("enter", "change"), ("r", "reset"), ("m", tr("settings.modified_only")), ("^b", "back"), ("esc", "quit"))

    def on_mount(self) -> None:
        self.rebuild()
        self._fit_help()
        self.query_one("#settings-group-list", OptionList).focus()

    def action_toggle_modified(self) -> None:
        self._modified_only = not self._modified_only
        self.rebuild()

    def focus_setting(self, key: str) -> None:
        if self._modified_only and all(spec.key != key for spec in self._visible_specs):
            self._modified_only = False
            self.rebuild()
        options = self.query_one("#settings-group-list", OptionList)
        index = next((index for index, spec in enumerate(self._visible_specs) if spec.key == key), None)
        if index is not None:
            options.highlighted = index
            options.scroll_to_highlight()
        options.focus()

    def on_resize(self) -> None:
        self._fit_help()

    def _fit_help(self) -> None:
        """Reserve enough rows for wrapped setting help at the current size."""

        found = self.query("#settings-group-help")
        if not found:
            return
        # Keep at least one row for the list; the list itself remains scrollable
        # when a narrow terminal needs more room for prose.
        found.first(Static).styles.max_height = max(2, min(12, self.app.size.height - 5))

    def relocalize(self) -> None:
        for chrome in self.query(Chrome):
            chrome.refresh_locale()
        for bar in self.query(KeyBar):
            bar.render_pairs()
        self.rebuild()

    def refresh_after_settings(self) -> None:
        self.rebuild()

    def rebuild(self) -> None:
        options = self.query_one("#settings-group-list", OptionList)
        previous_spec = self._selected()
        wanted_key = previous_spec.key if previous_spec is not None else ""
        options.clear_options()
        modified_count = sum(1 for spec in self.specs if is_setting_modified(spec, self.scope))
        self._visible_specs = (
            [spec for spec in self.specs if is_setting_modified(spec, self.scope)]
            if self._modified_only
            else list(self.specs)
        )
        self._label_width = label_width(setting_label(spec) for spec in self.specs)
        for spec in self._visible_specs:
            options.add_option(Option(self._row_markup(spec)))
        if self._visible_specs:
            target = next(
                (i for i, spec in enumerate(self._visible_specs) if spec.key == wanted_key),
                0,
            )
            options.highlighted = target
            self._show_help(self._visible_specs[target])
        else:
            empty_msg = (
                tr("settings.modified_empty")
                if self._modified_only
                else tr("settings.group.empty", default="No settings in this group")
            )
            options.add_option(Option(f"  [$dim]{empty_msg}[/]", disabled=True))
            self.query_one("#settings-group-help", Static).update(
                f"[$muted]{escape_markup(tr(self.description, default=self.description))}[/]"
            )
        masthead = self.query("#masthead")
        if masthead:
            title = tr(self.title, default=self.title)
            if self._modified_only:
                title += f"  [$accent]{tr('settings.modified_filter_on', count=modified_count)}[/]"
            masthead.first(Static).update(title)
        for bar in self.query(KeyBar):
            mod_label = (
                tr("settings.show_all")
                if self._modified_only
                else (
                    tr("settings.modified_count", count=modified_count)
                    if modified_count > 0
                    else tr("settings.modified_only")
                )
            )
            bar.render_pairs(
                ("enter", "change"),
                ("r", "reset"),
                ("m", mod_label),
                ("^b", "back"),
                ("esc", "quit"),
            )

    def _row_markup(self, spec: Setting) -> SettingRow:
        from .settings_screen import APPEARANCE_SETTING_KEYS, appearance_swatch_markup

        value = setting_value(spec, self.scope)
        label = setting_label(spec)
        value_markup = f"[$accent]{escape_markup(value)}[/]"
        if spec.key in APPEARANCE_SETTING_KEYS:
            palette = getattr(self.app, "palette", None)
            dark = True if palette is None else palette.dark
            accent = getattr(self.app, "accent_mode", "violet")
            swatch = appearance_swatch_markup(
                spec.key,
                self.scope.get(spec.key),
                dark=dark,
                accent=accent,
                theme_mode=getattr(self.app, "theme_mode", ""),
            )
            if swatch:
                value_markup = f"{swatch}  {value_markup}"
        return setting_row(
            self.query_one("#settings-group-list", OptionList),
            label, value_markup, self._label_width,
        )

    def _selected(self) -> Setting | None:
        index = self.query_one("#settings-group-list", OptionList).highlighted
        return self._visible_specs[index] if index is not None and index < len(self._visible_specs) else None

    def _show_help(self, spec: Setting | None) -> None:
        if spec is not None:
            text = setting_help(spec) or tr(self.description, default=self.description)
            if spec.key == "poster_preview":
                from .poster import poster_capability_summary

                cap_line = poster_capability_summary()
                self.query_one("#settings-group-help", Static).update(
                    f"[$muted]{escape_markup(text)}[/]\n[$accent]{escape_markup(cap_line)}[/]"
                )
                return
            self.query_one("#settings-group-help", Static).update(
                f"[$muted]{escape_markup(text)}[/]"
            )

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        self._show_help(self._selected())

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_activate()

    def action_activate(self) -> None:
        spec = self._selected()
        if spec is None:
            return
        if spec.kind == "bool":
            self._apply(spec, not bool(self.scope.get(spec.key)))
            return

        # Reuse the established editors so choice rows keep the same keyboard and
        # focus semantics as the ordinary Settings screen.
        from .settings_screen import (
            APPEARANCE_SETTING_KEYS,
            _AppearanceChoiceEditor,
            _ChoiceEditor,
            _DefaultAudioEditor,
            _DefaultSubtitleEditor,
            _MultiChoiceEditor,
            _TextEditor,
            _TitleDelayEditor,
        )

        if spec.key == "title_delay":
            editor = _TitleDelayEditor(spec, self.scope.get(spec.key))
        elif spec.key == "default_audio":
            editor = _DefaultAudioEditor(spec, self.scope.get(spec.key))
        elif spec.key == "default_subtitle":
            editor = _DefaultSubtitleEditor(spec, self.scope.get(spec.key))
        elif spec.key in APPEARANCE_SETTING_KEYS and spec.options:
            editor = _AppearanceChoiceEditor(spec, self.scope.get(spec.key))
        elif spec.kind == "multi":
            editor = _MultiChoiceEditor(spec, self.scope.get(spec.key))
        elif spec.kind == "choice":
            editor = _ChoiceEditor(spec, self.scope.get(spec.key))
        else:
            editor = _TextEditor(spec, self.scope.get(spec.key))
        self.app.push_screen(editor, lambda value: self._apply(spec, value) if value is not None else None)

    def _apply(self, spec: Setting, value: Any) -> None:
        from .settings_screen import APPEARANCE_SETTING_KEYS, validate_setting_value

        problems = validate_setting_value(spec, value)
        if problems:
            if spec.key in APPEARANCE_SETTING_KEYS:
                clear_fn = getattr(self.app, "clear_appearance_preview", None)
                if callable(clear_fn):
                    clear_fn()
            self.notify(
                "  ·  ".join(problems),
                title=tr("settings.not_saved", label=setting_label(spec)),
                severity="error",
                timeout=10,
            )
            return
        self.scope.set(spec.key, value)
        store_problem = getattr(self.scope._store, "problem", "")
        if store_problem:
            if spec.key in APPEARANCE_SETTING_KEYS:
                clear_fn = getattr(self.app, "clear_appearance_preview", None)
                if callable(clear_fn):
                    clear_fn()
            self.notify(
                store_problem,
                title=tr("settings.not_saved", label=setting_label(spec)),
                severity="error",
                timeout=10,
            )
            self.rebuild()
            return
        if spec.key in APPEARANCE_SETTING_KEYS:
            clear_fn = getattr(self.app, "clear_appearance_preview", None)
            if callable(clear_fn):
                clear_fn(apply=False)
            self.app.apply_theme()
            density_fn = getattr(self.app, "apply_layout_density", None)
            if callable(density_fn):
                density_fn()
        if spec.key == "interface_locale":
            self.app.apply_locale()
        self.rebuild()
        value_label = setting_value(spec, self.scope)
        self.notify(
            tr(
                "settings.changed",
                label=setting_label(spec),
                value=value_label,
            ),
            timeout=4,
        )

    def action_reset(self) -> None:
        spec = self._selected()
        if spec is not None:
            self._apply(spec, spec.default)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True
