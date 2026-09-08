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
from .bidi import visual_markup
from .chrome import Chrome, KeyBar


class SettingsGroupScreen(Screen[None]):
    """Edit a small, named group while keeping each stored setting independent."""

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "activate", "Change", show=True),
        Binding("r", "reset", "Reset", show=True),
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

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        yield Static(tr(self.title, default=self.title), id="masthead")
        yield Static(tr(self.description, default=self.description), id="settings-group-help")
        with Vertical(id="body"):
            yield OptionList(id="settings-group-list")
        yield KeyBar(("enter", "change"), ("r", "reset"), ("^b", "back"), ("esc", "quit"))

    def on_mount(self) -> None:
        self.rebuild()
        self._fit_help()
        self.query_one("#settings-group-list", OptionList).focus()

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

    def rebuild(self) -> None:
        options = self.query_one("#settings-group-list", OptionList)
        previous = options.highlighted
        options.clear_options()
        for spec in self.specs:
            options.add_option(Option(self._row_markup(spec)))
        if self.specs:
            options.highlighted = min(previous or 0, len(self.specs) - 1)
            self._show_help(self.specs[options.highlighted or 0])
        else:
            options.add_option(
                Option(f"  [$dim]{tr('settings.group.empty', default='No settings in this group')}[/]", disabled=True)
            )
        masthead = self.query("#masthead")
        if masthead:
            masthead.first(Static).update(tr(self.title, default=self.title))

    def _row_markup(self, spec: Setting) -> str:
        value = setting_value(spec, self.scope)
        return (
            f"  [$foreground]{visual_markup(setting_label(spec))}[/]  "
            f"[$accent]{visual_markup(value)}[/]"
        )

    def _selected(self) -> Setting | None:
        index = self.query_one("#settings-group-list", OptionList).highlighted
        return self.specs[index] if index is not None and index < len(self.specs) else None

    def _show_help(self, spec: Setting | None) -> None:
        if spec is not None:
            self.query_one("#settings-group-help", Static).update(
                f"[$muted]{visual_markup(setting_help(spec) or tr(self.description, default=self.description))}[/]"
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
        from .settings_screen import _ChoiceEditor, _MultiChoiceEditor, _TextEditor

        if spec.kind == "multi":
            editor = _MultiChoiceEditor(spec, self.scope.get(spec.key))
        elif spec.kind == "choice":
            editor = _ChoiceEditor(spec, self.scope.get(spec.key))
        else:
            editor = _TextEditor(spec, self.scope.get(spec.key))
        self.app.push_screen(editor, lambda value: self._apply(spec, value) if value is not None else None)

    def _apply(self, spec: Setting, value: Any) -> None:
        self.scope.set(spec.key, value)
        if spec.key == "theme":
            self.app.apply_theme()
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
