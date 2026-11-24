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

from ..core.i18n import cell_width, is_rtl_locale, phrase

__all__ = [
    "Chrome",
    "ChromeButton",
    "CloseMark",
    "KeyBar",
    "StatusChip",
    "fit_modal_card",
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
        prism_index: int = 0,
    ):
        self._action = action
        self._key = key
        self._label = label
        self.enabled = enabled
        self._prism_index = prism_index
        super().__init__(self._markup(key, label, enabled), id=id)
        self.add_class("chrome-button")
        if not enabled:
            self.add_class("chrome-disabled")

    def _key_colour(self) -> str:
        if getattr(self, "_is_mounted", False):
            prism = getattr(getattr(self.app, "palette", None), "prism", ())
            if prism:
                return prism[self._prism_index % len(prism)]
        return "$accent"

    def _markup(self, key: str, label: str, enabled: bool) -> str:
        text = phrase(label)
        if not enabled:
            return f"[$gutter]{key} {text}[/]"
        return f"[{self._key_colour()}]{key}[/] [bold]{text}[/bold]"

    def on_mount(self) -> None:
        self.update(self._markup(self._key, self._label, self.enabled))

    def relocalize(self) -> None:
        self.update(self._markup(self._key, self._label, self.enabled))

    def refresh_theme(self) -> None:
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

    def on_mount(self) -> None:
        self.update(self._fit(self.size.width or 9999))

    def on_resize(self) -> None:
        if self.is_mounted:
            self.update(self._fit(self.size.width))

    def render_pairs(self, *pairs: tuple[str, str]) -> None:
        self._pairs = list(pairs) or self._pairs
        self.update(self._fit(self.size.width if self.is_mounted else 9999))

    def refresh_theme(self) -> None:
        if self.is_mounted:
            self.render_pairs()

    def _fit(self, width: int) -> str:
        """As many hints as fit, in declared order of usefulness."""
        available = max(0, (width or 80) - 4)
        prism = (
            getattr(getattr(self.app, "palette", None), "prism", ())
            if getattr(self, "_is_mounted", False)
            else ()
        )
        shown: list[str] = []
        used = 0
        for index, (key, label) in enumerate(self._pairs):
            text = phrase(label)
            cost = cell_width(key) + 1 + cell_width(text) + (2 if shown else 0)
            if used + cost > available:
                break
            colour = prism[index % len(prism)] if prism else "$accent"
            shown.append(f"[{colour}]{key}[/] {text}")
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
        nav: list[ChromeButton] = []
        prism_index = 0
        if self._can_go_back:
            nav.append(ChromeButton("^b", "Back", "app.global_back", prism_index=prism_index))
            prism_index += 1
        for key, label, action, widget_id in self._left_actions:
            nav.append(ChromeButton(key, label, action, id=widget_id, prism_index=prism_index))
            prism_index += 1
        nav.append(ChromeButton("esc", "Quit", "app.global_quit", prism_index=prism_index))
        prism_index += 1
        for button in nav:
            button._chrome_group = "nav"  # type: ignore[attr-defined]

        util: list[ChromeButton] = []
        if self._show_search:
            util.append(ChromeButton("^f", "Search", "app.global_search", prism_index=prism_index))
            prism_index += 1
        if self._show_settings:
            util.append(ChromeButton("^s", "Settings", "app.global_settings", prism_index=prism_index))
            prism_index += 1
        for button in util:
            button._chrome_group = "util"  # type: ignore[attr-defined]

        self._nav_buttons = nav
        self._util_buttons = util
        gap = Static("", id="chrome-gap")
        self._gap = gap
        ordered = (
            [*reversed(util), gap, *reversed(nav)]
            if is_rtl_locale()
            else [*nav, gap, *util]
        )
        yield from ordered

    def _sync_direction(self) -> None:
        nav = getattr(self, "_nav_buttons", None)
        util = getattr(self, "_util_buttons", None)
        gap = getattr(self, "_gap", None)
        if nav is None or util is None or gap is None or not self.is_mounted:
            return
        wanted = (
            [*reversed(util), gap, *reversed(nav)]
            if is_rtl_locale()
            else [*nav, gap, *util]
        )
        if list(self.children) == wanted:
            return
        for index, child in enumerate(wanted):
            if child.parent is self:
                self.move_child(child, before=index)

    def refresh_locale(self) -> None:
        for button in self.query(ChromeButton):
            button.relocalize()
        self._sync_direction()

    def refresh_theme(self) -> None:
        for button in self.query(ChromeButton):
            button.refresh_theme()


def fit_modal_card(
    screen,
    card_selector: str,
    body_selector: str,
) -> None:
    """Keep a modal card's trailing actions visible when its body exceeds max-height.

    Textual's ``height: auto; max-height: X%`` caps a card's outer box without
    shrinking ``height: auto`` children inside it, clipping trailing buttons off
    the bottom on long lists or compact terminals. Adding ``.modal-card-flex``
    only when the natural content height exceeds the vertical budget switches
    the scrollable body to ``height: 1fr`` while keeping short modals snug.
    """
    if not getattr(screen, "is_mounted", False):
        return
    cards = screen.query(card_selector)
    bodies = screen.query(body_selector)
    if not cards or not bodies:
        return
    card = cards.first()
    body = bodies.first()
    viewport = screen.size
    if viewport.height <= 0:
        return
    max_height = card.styles.max_height
    budget = (
        int(max_height.resolve(viewport, viewport))
        if max_height is not None
        else viewport.height
    )
    inner_budget = max(1, budget - card.styles.gutter.height)

    visible_children = [child for child in card.children if child.display]
    help_child = next(
        (
            child
            for child in visible_children
            if child is not body
            and (
                child.has_class("modal-card-help")
                or child.has_class("proxy-editor-help")
            )
        ),
        None,
    )
    if help_child is not None:
        help_child.styles.max_height = None

    min_h = body.styles.min_height
    min_body = int(min_h.value) if min_h is not None and min_h.is_cells else 3

    needed = 0
    non_body_needed = 0
    help_h = 0
    prev_bottom = 0
    for index, child in enumerate(visible_children):
        margin = child.styles.margin
        gap = margin.top if index == 0 else max(prev_bottom, margin.top)
        needed += gap
        if child is body:
            non_body_needed += gap
            content_rows = body.virtual_size.height
            if content_rows <= 0:
                option_count = getattr(body, "option_count", None)
                if isinstance(option_count, int):
                    content_rows = max(1, option_count)
                else:
                    content_rows = max(1, len([c for c in body.children if c.display]) * 2)
            natural = max(min_body, content_rows + body.styles.gutter.height)
            if not card.has_class("modal-card-flex"):
                max_h = body.styles.max_height
                if max_h is not None and max_h.is_cells:
                    natural = min(natural, int(max_h.value))
            needed += natural
        else:
            child_h = child.region.height
            if child_h <= 0:
                style_h = child.styles.height
                if style_h is not None and style_h.is_cells:
                    child_h = int(style_h.value)
                else:
                    child_h = 1
            needed += child_h
            non_body_needed += gap + child_h
            if child is help_child:
                help_h = child_h
        prev_bottom = margin.bottom
    needed += prev_bottom
    non_body_needed += prev_bottom

    # Only clamp a long help label when it would starve the scrollable body
    # below its minimum height or push the action bar out of the card.
    if help_child is not None and help_h > 0:
        other_chrome = non_body_needed - help_h
        max_help = inner_budget - other_chrome - min_body
        if help_h > max_help:
            help_child.styles.max_height = max(2, max_help)

    clipped_now = (
        not card.has_class("modal-card-flex")
        and card.content_region.height > 0
        and any(child.region.bottom > card.content_region.bottom for child in visible_children)
    )
    card.set_class(needed > inner_budget or clipped_now, "modal-card-flex")


def refresh_locale_widgets(widget) -> None:
    """Redraw chrome and key-bar labels already mounted on ``widget``."""
    for chrome in widget.query(Chrome):
        chrome.refresh_locale()
    for bar in widget.query(KeyBar):
        bar.render_pairs()
