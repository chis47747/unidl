"""Contextual keyboard shortcut cheat sheet opened via ``?`` or Command Palette."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Static

from ..core.i18n import cell_width, phrase, tr
from .bidi import escape_markup
from .chrome import CloseMark, KeyBar

_KEY_DISPLAY = {
    "ctrl+b": "^b",
    "ctrl+d": "^d",
    "ctrl+e": "^e",
    "ctrl+f": "^f",
    "ctrl+l": "^l",
    "ctrl+n": "^n",
    "ctrl+p": "^p",
    "ctrl+q": "^q",
    "ctrl+r": "^r",
    "ctrl+s": "^s",
    "ctrl+t": "^t",
    "ctrl+x": "^x",
    "escape": "esc",
    "question_mark": "?",
    "slash": "/",
    "space": "space",
    "enter": "enter",
    "tab": "tab",
    "pageup": "PgUp",
    "pagedown": "PgDn",
    "up": "↑",
    "down": "↓",
    "left": "←",
    "right": "→",
}

_GLOBAL_KEYS = frozenset({"^p", "^f", "^s", "^b", "^t", "^q", "esc", "?", "f1"})


def _format_binding_key(raw: str) -> str:
    first = raw.split(",", 1)[0].strip()
    lower = first.lower()
    if lower in _KEY_DISPLAY:
        return _KEY_DISPLAY[lower]
    if lower.startswith("ctrl+") and len(lower) == 6:
        return f"^{lower[-1]}"
    return first


def _translate_label(label: str) -> str:
    translated = phrase(label)
    if translated != label:
        return translated
    lowered = label.lower()
    translated_lower = phrase(lowered)
    if translated_lower != lowered:
        return translated_lower
    return label


def collect_screen_shortcuts(screen: Screen | None) -> list[tuple[str, str]]:
    """Collect active, contextual ``(key, label)`` pairs for ``screen``."""
    if screen is None:
        return []
    pairs: list[tuple[str, str]] = []
    seen_keys: set[str] = set()

    def add(key: str, label: str) -> None:
        norm = key.strip()
        text = label.strip()
        if not norm or not text:
            return
        lower = norm.lower()
        if lower in seen_keys or lower in _GLOBAL_KEYS:
            return
        if norm in {"↑", "↓"} and ("↑↓" in seen_keys or "↑↓←→" in seen_keys):
            return
        if norm in {"←", "→"} and "↑↓←→" in seen_keys:
            return
        seen_keys.add(lower)
        pairs.append((norm, _translate_label(text)))

    for bar in screen.query(KeyBar):
        for key, label in getattr(bar, "_pairs", ()):
            add(str(key), str(label))

    for binding in getattr(screen, "BINDINGS", ()):
        if isinstance(binding, Binding):
            key_raw = binding.key
            desc = binding.description
            action = binding.action
        elif isinstance(binding, tuple) and len(binding) >= 3:
            key_raw, action, desc = str(binding[0]), str(binding[1]), str(binding[2])
        else:
            continue
        if not desc or action in {"noop", "close"} or action.startswith("app.global_"):
            continue
        add(_format_binding_key(key_raw), desc)

    return pairs


def _global_shortcuts() -> list[tuple[str, str]]:
    return [
        ("^p", tr("shortcuts.global.palette")),
        ("^f", tr("shortcuts.global.search")),
        ("^s", tr("shortcuts.global.settings")),
        ("^b", tr("shortcuts.global.back")),
        ("^t", tr("shortcuts.global.theme")),
        ("? / F1", tr("shortcuts.global.help")),
        ("esc / ^q", tr("shortcuts.global.quit")),
    ]


def _slash_commands() -> list[tuple[str, str]]:
    return [
        ("/auto", tr("beta.mode.auto")),
        ("/service", tr("beta.mode.services")),
        ("/justwatch", tr("beta.mode.justwatch")),
        ("/url", tr("beta.mode.url")),
        ("/open", tr("beta.mode.open")),
        ("/exports", tr("beta.mode.exports")),
        ("/vault", tr("beta.mode.vault")),
        ("/addkeys", tr("beta.mode.add_keys")),
        ("/settings", tr("beta.mode.settings")),
        ("/schedule", tr("schedule.title", default="Scheduled downloads")),
        ("/catalog", tr("beta.mode.catalog")),
    ]


def _format_rows(rows: list[tuple[str, str]]) -> str:
    if not rows:
        return ""
    key_col = max(10, *(cell_width(key) for key, _ in rows)) + 2
    lines: list[str] = []
    for key, desc in rows:
        pad = " " * max(1, key_col - cell_width(key))
        lines.append(f"  [bold $accent]{escape_markup(key)}[/]{pad}[$foreground]{escape_markup(desc)}[/]")
    return "\n".join(lines)


class ShortcutsScreen(ModalScreen[None]):
    """Read-only modal reference of active screen, global, and slash shortcuts."""

    BINDINGS: ClassVar = [
        Binding("escape", "close", "Close", show=False),
        Binding("question_mark", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
        Binding("x", "close", "Close", show=False),
        Binding("ctrl+b", "close", "Close", show=False),
        Binding("b", "close", "Close", show=False),
        Binding("up", "scroll(-2)", "Up", show=False),
        Binding("down", "scroll(2)", "Down", show=False),
        Binding("pageup", "scroll(-12)", "Page up", show=False),
        Binding("pagedown", "scroll(12)", "Page down", show=False),
    ]

    def __init__(self, source_screen: Screen | None = None) -> None:
        super().__init__()
        self._source_screen = source_screen

    def compose(self) -> ComposeResult:
        with Vertical(id="shortcuts-modal"):
            with Horizontal(id="shortcuts-modal-head"):
                yield Static(tr("shortcuts.title"), id="shortcuts-modal-title")
                yield CloseMark(id="shortcuts-modal-close")
            with VerticalScroll(id="shortcuts-list"):
                yield Static("", id="shortcuts-body")
            yield Static(tr("shortcuts.close_hint"), id="shortcuts-modal-hint")

    def on_mount(self) -> None:
        self._render_body()
        self.query_one("#shortcuts-list", VerticalScroll).focus()

    def relocalize(self) -> None:
        self.query_one("#shortcuts-modal-title", Static).update(tr("shortcuts.title"))
        self.query_one("#shortcuts-modal-hint", Static).update(tr("shortcuts.close_hint"))
        self._render_body()

    def _render_body(self) -> None:
        sections: list[str] = []
        screen_rows = collect_screen_shortcuts(self._source_screen)
        if screen_rows:
            sections.append(
                f"[$dim]{escape_markup(tr('shortcuts.section.screen').upper())}[/]\n"
                f"{_format_rows(screen_rows)}"
            )
        sections.append(
            f"[$dim]{escape_markup(tr('shortcuts.section.global').upper())}[/]\n"
            f"{_format_rows(_global_shortcuts())}"
        )
        sections.append(
            f"[$dim]{escape_markup(tr('shortcuts.section.slash').upper())}[/]\n"
            f"{_format_rows(_slash_commands())}"
        )
        self.query_one("#shortcuts-body", Static).update("\n\n".join(sections))

    def action_scroll(self, delta: int) -> None:
        self.query_one("#shortcuts-list", VerticalScroll).scroll_relative(y=delta, animate=False)

    def action_close(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


__all__ = ["ShortcutsScreen", "collect_screen_shortcuts"]
