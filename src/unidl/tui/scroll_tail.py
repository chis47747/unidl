"""Small, viewport-local controls for returning to the newest content."""

from __future__ import annotations

from textual import events
from textual.containers import Vertical, VerticalScroll
from textual.errors import NoWidget
from textual.geometry import Size
from textual.widget import Widget
from textual.widgets import Static

from ..core.i18n import tr


class JumpToBottom(Static):
    """A five-cell overlay, not a new row taken from the content viewport."""

    ALLOW_SELECT = False
    DEFAULT_CSS = """
    JumpToBottom {
        layer: scroll-tail;
        dock: bottom;
        width: 5;
        height: 1;
        padding: 0;
        margin: 0;
        content-align: center middle;
        color: $muted;
        background: $recessed;
        visibility: hidden;
    }
    JumpToBottom:hover {
        color: $foreground;
        background: $highlight;
    }
    """

    def __init__(self, target: Widget) -> None:
        super().__init__("▼", markup=False)
        self.target = target
        self._sync_pending = False

    def on_mount(self) -> None:
        self.watch(self.target, "scroll_y", self.request_sync, init=False)
        self.watch(self.target, "virtual_size", self.request_sync, init=False)
        self.request_sync()

    def request_sync(self) -> None:
        if not self.is_attached or self._sync_pending:
            return
        self._sync_pending = True
        self.call_after_refresh(self._sync)

    def _sync(self) -> None:
        self._sync_pending = False
        if not self.is_attached or not self.target.is_attached:
            return
        parent = self.parent
        viewport = self.target.scrollable_content_region
        try:
            viewport = viewport.intersection(self.screen.find_widget(self.target).clip)
        except NoWidget:
            self.visible = False
            return
        if isinstance(parent, Widget):
            self.styles.offset = (
                max(0, viewport.x - parent.content_region.x + (viewport.width - 5) // 2),
                min(0, viewport.bottom - parent.content_region.bottom),
            )
        self.tooltip = tr("scroll.jump_bottom")
        self.visible = (
            self.target.display
            and viewport.height > 0
            and self.target.max_scroll_y > 0
            and not self.target.is_vertical_scroll_end
        )

    def on_mouse_down(self, event: events.MouseDown) -> None:
        # Do not start screen-wide text selection or toggle a row underneath.
        event.stop()
        event.prevent_default()

    def on_enter(self) -> None:
        self.tooltip = tr("scroll.jump_bottom")

    def on_mouse_up(self, event: events.MouseUp) -> None:
        event.stop()
        event.prevent_default()

    async def on_click(self, event: events.Click) -> None:
        event.stop()
        event.prevent_default()
        if event.button != 1 or not self.target.is_attached:
            return
        await self.target.stop_animation("scroll_y", complete=False)
        if not self.is_attached or not self.target.is_attached:
            return
        jump = getattr(self.target, "jump_to_bottom", None)
        if callable(jump):
            jump()
        else:
            self.target.scroll_end(animate=False, immediate=True, x_axis=False)
        self.request_sync()


class ScrollTail(Vertical):
    """Wrap a line-rendered viewport without changing its internal scrolling."""

    DEFAULT_CSS = """
    ScrollTail {
        width: 1fr;
        height: 1fr;
        min-height: 0;
        min-width: 0;
        layers: content scroll-tail;
    }
    """

    def __init__(self, target: Widget, *, classes: str = "") -> None:
        self.target = target
        super().__init__(target, JumpToBottom(target), classes=classes)

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        # Auto-height logs must measure their lines, not a flexible child plus
        # an overlay. In particular an empty log must not reserve eight rows.
        return self.target.get_content_height(container, viewport, width)

    def on_resize(self) -> None:
        for mark in self.query(JumpToBottom):
            mark.request_sync()


class TailVerticalScroll(VerticalScroll):
    """A normal scrolling container with a fixed, non-scrolling tail mark."""

    DEFAULT_CSS = """
    TailVerticalScroll { layers: content scroll-tail; }
    """

    async def on_mount(self) -> None:
        await self.mount(JumpToBottom(self))

    def on_resize(self) -> None:
        for mark in self.query(JumpToBottom):
            mark.request_sync()
