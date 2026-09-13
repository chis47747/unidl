"""Global chrome: the bar present on every screen.

Layout is fixed so muscle memory works everywhere:

    ^b Back   esc Quit                            ^f Search   ^s Settings

Every item is both a keyboard shortcut and a click target. Search and Settings
are always in the same place; Back appears on every screen except the first,
which has nothing above it.
"""

from __future__ import annotations

from textual.containers import Horizontal
from textual.widgets import Static

from ..core.i18n import cell_width, phrase

__all__ = [
    "Chrome",
    "ChromeButton",
    "CloseMark",
    "KeyBar",
    "StatusChip",
    "refresh_locale_widgets",
]


class ChromeButton(Static):
    """A clickable shortcut label, drawn to look like a control.

    A terminal has one font at one size, so "make it bigger" is not on offer.
    What is: weight, contrast, a background panel and room around the text. The
    label is bold, the key is in the accent colour, and the CSS gives it two
    columns of padding so it reads as a button rather than as a stray word.
    """

    def __init__(
        self,
        key: str,
        label: str,
        action: str,
        *,
        enabled: bool = True,
        id: str | None = None,
    ):
        super().__init__(self._markup(key, label, enabled), id=id)
        self._action = action
        self._key = key
        self._label = label
        self.enabled = enabled
        self.add_class("chrome-button")
        if not enabled:
            self.add_class("chrome-disabled")

    @staticmethod
    def _markup(key: str, label: str, enabled: bool) -> str:
        text = phrase(label)
        if not enabled:
            return f"[dim]{key}[/dim] [dim]{text}[/dim]"
        return f"[$accent]{key}[/] [bold]{text}[/bold]"

    def relocalize(self) -> None:
        self.update(self._markup(self._key, self._label, self.enabled))

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self.enabled:
            return
        self.enabled = enabled
        self.set_class(not enabled, "chrome-disabled")
        self.update(self._markup(self._key, self._label, enabled))

    def set_label(self, key: str, label: str) -> None:
        """Change a stateful control without replacing the whole chrome bar."""
        self._key = key
        self._label = label
        self.update(self._markup(key, label, self.enabled))

    async def on_click(self) -> None:
        if self.enabled:
            await self.app.run_action(self._action)


class CloseMark(Static):
    """Visible ✕ on a see-through overlay card.

    Overlay modals sit on top of another screen's chrome, so the Back / Quit
    labels behind them are not live controls. A control you cannot use is not a
    control; this mark is the click target that actually dismisses the card.
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("✕", id=id)
        self.add_class("modal-close")

    def on_click(self, event) -> None:
        event.stop()
        screen = self.screen
        for name in ("action_close", "action_cancel"):
            closer = getattr(screen, name, None)
            if callable(closer):
                closer()
                return


class StatusChip(Static):
    """One clickable item on a status line.

    A separate widget per item rather than one Static full of ``@click`` spans,
    because a widget can have a ``:hover`` style. Without that there is nothing
    telling you the text is a control, and an invisible control is not one.

    The action is resolved against the *screen*, so screens own their own status
    actions instead of everything having to be an app action.
    """

    def __init__(self, action: str = "", *, id: str | None = None, classes: str = ""):
        super().__init__("", id=id, classes=f"chip {classes}".strip())
        self._action = action
        if not action:
            self.add_class("chip-static")

    async def on_click(self) -> None:
        if self._action:
            await self.app.run_action(self._action, default_namespace=self.screen)


class KeyBar(Static):
    """The contextual shortcut bar along the bottom.

    Textual's own ``Footer`` hides a binding whenever the focused widget would
    swallow the key, so with a text input focused it collapses to almost
    nothing. This bar always states what is available in the current context,
    so it is written explicitly per screen.
    """

    def __init__(self, *pairs: tuple[str, str]):
        self._pairs = list(pairs)
        # content has to exist before the first render, so build it up front
        super().__init__(self._fit(9999), id="keybar")

    def on_resize(self) -> None:
        if self.is_mounted:
            self.update(self._fit(self.size.width))

    def render_pairs(self, *pairs: tuple[str, str]) -> None:
        self._pairs = list(pairs) or self._pairs
        self.update(self._fit(self.size.width if self.is_mounted else 9999))

    def _fit(self, width: int) -> str:
        """As many hints as fit, in declared order of usefulness."""
        available = max(0, (width or 80) - 4)
        shown: list[str] = []
        used = 0
        for key, label in self._pairs:
            text = phrase(label)
            cost = cell_width(key) + 1 + cell_width(text) + (2 if shown else 0)
            if used + cost > available:
                break
            shown.append(f"[$accent]{key}[/] {text}")
            used += cost
        return "  ".join(shown)


class Chrome(Horizontal):
    """The persistent top bar."""

    def __init__(
        self,
        *,
        can_go_back: bool = True,
        show_search: bool = True,
        show_settings: bool = True,
        left_actions: tuple[tuple[str, str, str, str], ...] = (),
    ):
        super().__init__(id="chrome")
        self._can_go_back = can_go_back
        self._show_search = show_search
        self._show_settings = show_settings
        self._left_actions = left_actions

    def compose(self):
        # The advertised keys are the control chords, because those work even
        # while a text input has focus. The plain-letter equivalents are also
        # bound and simply give way to whatever you are typing.
        #
        # Screen 1 has nowhere to go back to, so Back is left out rather than
        # shown greyed: an inert control is worse than no control.
        if self._can_go_back:
            yield ChromeButton("^b", "Back", "app.global_back")
        for key, label, action, widget_id in self._left_actions:
            yield ChromeButton(key, label, action, id=widget_id)
        yield ChromeButton("esc", "Quit", "app.global_quit")
        yield Static("", id="chrome-gap")
        if self._show_search:
            yield ChromeButton("^f", "Search", "app.global_search")
        if self._show_settings:
            yield ChromeButton("^s", "Settings", "app.global_settings")

    def refresh_locale(self) -> None:
        for button in self.query(ChromeButton):
            button.relocalize()


def refresh_locale_widgets(widget) -> None:
    """Redraw chrome and key-bar labels already mounted on ``widget``."""
    for chrome in widget.query(Chrome):
        chrome.refresh_locale()
    for bar in widget.query(KeyBar):
        bar.render_pairs()
