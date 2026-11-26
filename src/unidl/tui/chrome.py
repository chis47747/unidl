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
from ..core.terminal_cells import crop

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

    ALLOW_SELECT = False

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
        self._display_label: str | None = None
        self.enabled = enabled
        self._prism_index = prism_index
        self._pressed = False
        self._handled_up = False
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
        text = self._display_label if self._display_label is not None else phrase(label)
        if not enabled:
            return f"[$gutter]{key} {text}[/]"
        return f"[{self._key_colour()}]{key}[/] [bold]{text}[/bold]"

    def full_text_width(self) -> int:
        return cell_width(self._key) + 1 + cell_width(phrase(self._label))

    def set_display_label(self, text: str | None) -> None:
        if self._display_label == text:
            return
        self._display_label = text
        self.update(self._markup(self._key, self._label, self.enabled))

    def on_mount(self) -> None:
        self.update(self._markup(self._key, self._label, self.enabled))

    def relocalize(self) -> None:
        self._display_label = None
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
        self._display_label = None
        self.update(self._markup(key, label, self.enabled))
        parent = self.parent
        fit = getattr(parent, "_fit_width", None)
        if callable(fit):
            fit()

    def on_leave(self) -> None:
        self._pressed = False

    def on_mouse_down(self, event) -> None:
        if getattr(event, "button", 1) == 1 and self.enabled:
            self._pressed = True
            self._handled_up = False
            self.screen.clear_selection()
            event.stop()

    async def on_mouse_up(self, event) -> None:
        if not self._pressed:
            return
        self._pressed = False
        event.stop()
        if self.enabled and self.region.contains_point(event.screen_offset):
            self._handled_up = True
            await self.app.run_action(self._action)

    async def on_click(self, event) -> None:
        if getattr(event, "button", 1) != 1:
            return
        if self._handled_up:
            self._handled_up = False
            return
        if self.enabled:
            await self.app.run_action(self._action)


class CloseMark(Static):
    """Visible ✕ on a see-through overlay card.

    Overlay modals sit on top of another screen's chrome, so the Back / Quit
    labels behind them are not live controls. A control you cannot use is not a
    control; this mark is the click target that actually dismisses the card.
    """

    ALLOW_SELECT = False

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

    ALLOW_SELECT = False

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

    ALLOW_SELECT = False

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
        # Textual's Widget.size already reflects the inner content width
        # (after #keybar's horizontal padding), so do not subtract padding twice.
        available = max(0, width if width > 0 else 76)
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


class _ChromeGap(Static):
    """Non-selectable spacer between the navigation and utility chrome groups."""

    ALLOW_SELECT = False


class Chrome(Horizontal):
    """The persistent top bar."""

    ALLOW_SELECT = False

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
        gap = _ChromeGap("", id="chrome-gap")
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

    def on_mount(self) -> None:
        self._fit_width()

    def on_resize(self) -> None:
        self._fit_width()

    def _fit_width(self) -> None:
        """Keep every Chrome button on screen at 80 cols across all locales."""
        if not self.is_mounted:
            return
        width = self.size.width or (self.app.size.width if self.app else 80)
        if width <= 0:
            return
        available = max(20, width - 2)
        buttons = [button for button in self.query(ChromeButton) if button.display]
        if not buttons:
            return
        for button in buttons:
            button.set_display_label(None)
        widths = [button.full_text_width() for button in buttons]
        if sum(w + 5 for w in widths) <= available:
            self.set_class(False, "tight")
            self.set_class(False, "compact")
            return
        if sum(w + 3 for w in widths) <= available:
            self.set_class(True, "tight")
            self.set_class(False, "compact")
            return
        self.set_class(False, "tight")
        self.set_class(True, "compact")
        total = sum(w + 2 for w in widths)
        if total <= available:
            return
        # Still wider than the bar (e.g. 6 buttons in French/German at 80 cols):
        # trim the longest labels fairly while preserving shortcut keys and core nav.
        excess = total - available
        full_labels = [phrase(button._label) for button in buttons]
        targets = [cell_width(label) for label in full_labels]
        # Prefer trimming extra left actions before core Back/Quit/Search/Settings.
        core_actions = {"app.global_back", "app.global_quit", "app.global_search", "app.global_settings"}
        min_label = [3 if button._action in core_actions else 2 for button in buttons]
        while excess > 0:
            candidates = [
                (0 if buttons[i]._action not in core_actions else 1, -targets[i], i)
                for i in range(len(buttons))
                if targets[i] > min_label[i]
            ]
            if not candidates:
                break
            candidates.sort()
            idx = candidates[0][2]
            targets[idx] -= 1
            excess -= 1
        for button, full_label, target in zip(buttons, full_labels, targets, strict=True):
            if target < cell_width(full_label):
                short = crop(full_label, max(1, target - 1)) + "…" if target >= 2 else crop(full_label, target)
                button.set_display_label(short)
            else:
                button.set_display_label(None)

    def refresh_locale(self) -> None:
        for button in self.query(ChromeButton):
            button.relocalize()
        self._sync_direction()
        self._fit_width()

    def refresh_theme(self) -> None:
        for button in self.query(ChromeButton):
            button.refresh_theme()
        self._fit_width()


def fit_modal_card(
    screen,
    card_selector: str,
    body_selector: str = "",
) -> None:
    """Keep a modal card's trailing actions visible when its body exceeds max-height.

    Textual's ``height: auto; max-height: X%`` caps a card's outer box without
    shrinking ``height: auto`` children inside it, clipping trailing buttons off
    the bottom on long lists or compact terminals. Adding ``.modal-card-flex``
    only when the natural content height exceeds the vertical budget switches
    the scrollable body to ``height: 1fr`` while keeping short modals snug.
    For non-scrollable text/template modals without a matching body widget,
    clamps tall help/preview blocks so Save/Cancel actions stay inside the card.
    """
    if not getattr(screen, "is_mounted", False):
        return
    cards = screen.query(card_selector)
    if not cards:
        return
    card = cards.first()
    bodies = screen.query(body_selector) if body_selector else None
    body = bodies.first() if bodies else None
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
    clampable: list[object] = []
    help_child = None
    for child in visible_children:
        if child is body:
            continue
        if (
            child.has_class("modal-card-help")
            or child.has_class("proxy-editor-help")
        ):
            help_child = child
            clampable.append(child)
        elif child.id == "storage-editor-preview":
            clampable.append(child)

    inner_width = card.content_size.width
    if inner_width <= 0:
        w_budget = viewport.width
        if card.styles.width is not None:
            w_budget = min(w_budget, int(card.styles.width.resolve(viewport, viewport)))
        if card.styles.max_width is not None:
            w_budget = min(w_budget, int(card.styles.max_width.resolve(viewport, viewport)))
        inner_width = max(1, w_budget - card.styles.gutter.width)

    container_size = type(viewport)(inner_width, inner_budget)
    clampable_ids = {id(child) for child in clampable}

    def _measure_child(child) -> int:
        style_h = child.styles.height
        if style_h is not None and style_h.is_cells:
            return max(1, int(style_h.value))
        if id(child) in clampable_ids or child.region.height <= 0:
            child_w = max(1, inner_width - child.styles.gutter.width - child.styles.margin.width)
            try:
                content_h = child.get_content_height(container_size, viewport, child_w)
                return max(1, int(content_h) + child.styles.gutter.height)
            except Exception:
                pass
        return max(1, child.region.height) if child.region.height > 0 else 1

    if body is None:
        needed = 0
        prev_bottom = 0
        heights: dict[int, int] = {}
        for index, child in enumerate(visible_children):
            margin = child.styles.margin
            gap = margin.top if index == 0 else max(prev_bottom, margin.top)
            child_h = _measure_child(child)
            heights[id(child)] = child_h
            needed += gap + child_h
            prev_bottom = margin.bottom
        needed += prev_bottom
        excess = needed - inner_budget
        if clampable:
            # Shrink help and preview blocks down to at least 1 row each so
            # Input and Save/Cancel action rows stay visible inside the card,
            # without oscillating between clamped and unclamped heights.
            for child in clampable:
                cur = heights.get(id(child), 1)
                reducible = max(0, cur - 1)
                if excess > 0 and reducible > 0:
                    take = min(reducible, excess)
                    child.styles.max_height = max(1, cur - take)
                    excess -= take
                else:
                    child.styles.max_height = None
        return

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
            child_h = _measure_child(child)
            needed += child_h
            non_body_needed += gap + child_h
            if child is help_child:
                help_h = child_h
        prev_bottom = margin.bottom
    needed += prev_bottom
    non_body_needed += prev_bottom

    # Only clamp a long help label when it would starve the scrollable body
    # below its minimum height or push the action bar out of the card.
    for child in clampable:
        if child is not help_child:
            child.styles.max_height = None
    if help_child is not None and help_h > 0:
        other_chrome = non_body_needed - help_h
        max_help = inner_budget - other_chrome - min_body
        if help_h > max_help:
            clamped_help = max(2, max_help)
            help_child.styles.max_height = clamped_help
            needed -= max(0, help_h - clamped_help)
        else:
            help_child.styles.max_height = None

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
