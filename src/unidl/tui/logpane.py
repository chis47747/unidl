"""The log widget, made selectable like every other panel.

Everything else on screen can be dragged over and copied: the app copies the
selection on mouse-up, which is why a KID or a manifest URL can go straight into
another window. The log could not, and it is the one place those values actually
appear.

The reason is not a missing binding. ``Widget.get_selection`` reads the widget's
own render and only understands a ``Text`` or ``Content``; ``RichLog`` renders
pre-composed ``Strip``s, so selection returned ``None`` and the copy silently did
nothing. Textual's plain ``Log`` implements both halves for itself, and this does
the same for the rich one:

* :meth:`get_selection` - the plain text behind the strips, so a selection can be
  extracted at all
* :meth:`_render_line` - the highlight, so you can see what you are about to copy

Switching to ``Log`` instead would have been less code and would have thrown away
the colour, which is the whole point of that pane: title, manifest and key are
told apart by colour.
"""

from __future__ import annotations

from rich.style import Style
from textual import events
from textual.geometry import Offset
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import RichLog

__all__ = ["SelectableLog"]


class SelectableLog(RichLog):
    """A ``RichLog`` whose contents can be selected and copied."""

    # Selection belongs to this widget, not to Screen. Textual's screen-wide
    # selector changes to SELECT_ALL as soon as a drag leaves the log and enters
    # its header (or any other widget), which turned an upward log selection into
    # "copy the entire log". We still publish the finished range through
    # ``screen.selections`` so the app's normal clipboard path and
    # ``Screen.clear_selection`` continue to work.
    ALLOW_SELECT = False

    #: How many rows the last :meth:`write_counted` produced. One renderable can
    #: become several rows, because the pane wraps.
    _last_rows = 0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._drag_anchor: Offset | None = None
        self._drag_pointer: Offset | None = None
        self._drag_moved = False
        self._drag_scroll_direction = 0
        self._drag_scroll_timer = None
        #: Set by a wheel/trackpad scroll away from the newest line. This is
        #: separate from a text selection: live logs should respect either kind
        #: of inspection, and resume only when the user scrolls back to the end.
        self._follow_paused = False

    @property
    def has_selection(self) -> bool:
        """Whether a persistent range or an in-progress drag owns the viewport."""
        return self.text_selection is not None or self._drag_anchor is not None

    # ------------------------------------------------------------- last line
    def write_counted(self, renderable: object) -> int:
        """Write at the pane's own width, and say how many rows it took.

        The width is stated rather than left to be worked out. ``RichLog`` measures
        a renderable against the *app's* console and then clamps the result to
        ``min_width``, neither of which is this pane: the effect was a long line
        rendered at some other width and then cropped, so the tail of a manifest URL
        or a save name sat off the right edge with only a sideways scroll to reach
        it. Given a width, none of that machinery runs and the line folds where the
        pane ends.

        The row count is for :meth:`redraw_last`, which needs to know how much of
        the log the newest line occupies.
        """
        room = self.scrollable_content_region.width
        before = len(self.lines)
        follow_tail = self.auto_scroll and not self._follow_paused and not self.has_selection
        # RichLog normally queues a scroll-to-end without checking again when it
        # runs. A mouse selection can begin in between, and that stale callback is
        # what made the viewport snap back to the newest line on mouse-up. Queue
        # our own guarded follow instead.
        self.write(renderable, width=room if room > 0 else None, scroll_end=False)
        if follow_tail:
            self.call_after_refresh(self._follow_tail)
        self._last_rows = max(0, len(self.lines) - before)
        return self._last_rows

    def _follow_tail(self) -> None:
        """Follow new output only if no selection was made in the meantime."""
        if self.auto_scroll and not self._follow_paused and not self.has_selection:
            self.scroll_end(animate=False, immediate=True, x_axis=False)

    def redraw_last(self, renderable: object) -> bool:
        """Draw ``renderable`` over the last line, in place.

        For the one line whose state can still change: the newest one, while the
        work it describes is still going on. Only ever the last line, and only when
        the rows it produced are still all there - a trimmed or cleared pane has
        nothing to redraw, and cutting the wrong rows out of a log is worse than a
        dot that stopped pulsing.
        """
        rows = self._last_rows
        if rows <= 0 or rows > len(self.lines):
            return False
        del self.lines[-rows:]
        # The cache is keyed by row, and the rows from here down have just changed
        # meaning. Cheaper than it looks: the pane re-renders what is visible.
        self._line_cache.clear()
        self.write_counted(renderable)
        return True

    # ---------------------------------------------------------------- copying
    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """The text under the selection.

        Read off the strips rather than from the source renderables: what a person
        dragged over is what is on screen, including the wrapping, and the source
        objects no longer know where the line breaks ended up.
        """
        if not self.lines:
            return None
        # Right-trimmed: a line is laid out in columns so that a wrapped one hangs
        # under its own text, and a column pads its cell to the full width. Those
        # spaces are layout, not content, and copying them means a pasted manifest
        # URL arrives with a tail of blanks.
        text = "\n".join(strip.text.rstrip() for strip in self.lines)
        return selection.extract(text), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        # The strips are cached per line, and a cached line has no idea part of it
        # is now highlighted, so the cache goes when the selection moves.
        self._line_cache.clear()
        # A service may keep logging while the pointer is down. Hold the viewport
        # still until the selection is cleared, otherwise each arriving line can
        # move different text underneath the same drag.
        # Clearing an old selection and starting a new drag can happen in the same
        # event turn. The reactive clear callback may arrive after mouse-down; a
        # live drag must still win and keep the viewport pinned.
        self.auto_scroll = (
            selection is None
            and self._drag_anchor is None
            and not self._follow_paused
        )
        self.refresh()

    # -------------------------------------------------------------- follow tail
    def _pause_follow_tail(self) -> None:
        """Keep live output from undoing a user's manual scroll."""
        self._follow_paused = True
        self.auto_scroll = False
        self.scroll_target_y = self.scroll_y

    def _resume_follow_tail_at_end(self) -> None:
        """Resume following once the user has manually returned to the bottom."""
        if self.scroll_y < self.max_scroll_y:
            return
        self._follow_paused = False
        if not self.has_selection:
            self.auto_scroll = True

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        # Shift/Ctrl-wheel is horizontal in Textual and does not mean the user is
        # inspecting older log rows.
        if not event.shift and not event.ctrl:
            self._pause_follow_tail()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if event.shift or event.ctrl:
            return
        self._pause_follow_tail()
        # Widget's built-in scroll handler runs after this public handler. Check
        # the resulting position on the next refresh, then resume only at the end.
        self.call_after_refresh(self._resume_follow_tail_at_end)

    # ---------------------------------------------------------- mouse selection
    def _content_offset(self, pointer: Offset) -> Offset:
        """Translate a captured pointer to a safe offset in the rendered log."""
        if not self.lines:
            return Offset(0, 0)
        height = max(1, self.scrollable_content_region.height)
        width = max(1, self.scrollable_content_region.width)
        viewport_y = min(max(pointer.y, 0), height - 1)
        viewport_x = min(max(pointer.x, 0), width - 1)
        y = min(max(int(self.scroll_y) + viewport_y, 0), len(self.lines) - 1)
        x = max(0, int(self.scroll_x) + viewport_x)
        return Offset(x, y)

    @staticmethod
    def _inclusive_selection(anchor: Offset, pointer: Offset) -> Selection:
        """Make the cell under the pointer part of the selected range."""
        start, end = sorted((anchor, pointer), key=lambda offset: offset.transpose)
        return Selection(start, end + (1, 0))

    def _publish_drag_selection(self) -> None:
        anchor = self._drag_anchor
        pointer = self._drag_pointer
        if anchor is None or pointer is None:
            return
        end = self._content_offset(pointer)
        if end != anchor:
            self._drag_moved = True
        if self._drag_moved:
            self.screen.selections = {self: self._inclusive_selection(anchor, end)}

    def _stop_drag_scroll(self) -> None:
        if self._drag_scroll_timer is not None:
            self._drag_scroll_timer.stop()
            self._drag_scroll_timer = None
        self._drag_scroll_direction = 0

    def _set_drag_scroll(self, direction: int) -> None:
        """Auto-scroll while a captured drag rests at either vertical edge."""
        if direction == self._drag_scroll_direction:
            return
        self._stop_drag_scroll()
        self._drag_scroll_direction = direction
        if direction:
            self._drag_scroll_tick()
            self._drag_scroll_timer = self.set_interval(0.06, self._drag_scroll_tick)

    def _drag_scroll_tick(self) -> None:
        direction = self._drag_scroll_direction
        if not direction or self._drag_anchor is None:
            self._stop_drag_scroll()
            return
        before = self.scroll_y
        self.scroll_relative(y=direction, animate=False, immediate=True)
        self._publish_drag_selection()
        if self.scroll_y == before:
            self._stop_drag_scroll()

    async def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1 or not self.lines:
            return
        self.screen.clear_selection()
        self.auto_scroll = False
        # Cancel both an active animation and its target. New log writes use the
        # guarded path above, so nothing else may move this viewport during a drag.
        await self.stop_animation("scroll_y", complete=False)
        self.scroll_target_y = self.scroll_y
        self._drag_pointer = Offset(event.x, event.y)
        self._drag_anchor = self._content_offset(self._drag_pointer)
        self._drag_moved = False
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_anchor is None:
            return
        self._drag_pointer = Offset(event.x, event.y)
        height = max(1, self.scrollable_content_region.height)
        if event.y <= 0 and self.scroll_y > 0:
            self._set_drag_scroll(-1)
        elif event.y >= height - 1 and self.scroll_y < self.max_scroll_y:
            self._set_drag_scroll(1)
        else:
            self._set_drag_scroll(0)
        self._publish_drag_selection()
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._drag_anchor is None:
            return
        self._drag_pointer = Offset(event.x, event.y)
        self._publish_drag_selection()
        self._stop_drag_scroll()
        self._drag_anchor = None
        self._drag_pointer = None
        self.release_mouse()
        # A click is not a one-character selection. Clearing here also resumes
        # follow-tail; a real drag keeps its range and its viewport in place.
        if not self._drag_moved:
            self.screen.clear_selection()
        self._drag_moved = False
        # Deliberately let MouseUp bubble: UniDL's app-level handler copies the
        # exact text exposed through ``screen.selections``.

    def on_unmount(self) -> None:
        self._stop_drag_scroll()
        if self.app.mouse_captured is self:
            self.release_mouse()

    # --------------------------------------------------------------- painting
    def _render_line(self, y: int, scroll_x: int, width: int) -> Strip:
        selection = self.text_selection
        if selection is None:
            # ``offset`` metadata is how Screen turns a mouse cell back into a
            # character and line inside this widget. RichLog omits it because it
            # is not selectable; without adding it here a drag can only mean
            # "the whole widget", never the portion the pointer crossed.
            return super()._render_line(y, scroll_x, width).apply_offsets(scroll_x, y)
        if y >= len(self.lines):
            return Strip.blank(width, self.rich_style)

        line = self.lines[y]
        span = selection.get_span(y)
        if span is not None:
            start, end = span
            if end == -1:
                end = len(line.text)
            if end > start:
                palette = self.app.palette
                # Textual's inherited screen selection style can resolve to the
                # same foreground and background colour in this theme. That draws
                # an opaque purple bar and hides the selected characters. Use the
                # app palette directly so the range is unmistakable in both themes.
                # A selection is a reading aid, not the current action. The accent
                # colour is intentionally vivid for buttons and focused controls;
                # filling several log rows with it is glaring. ``visual`` is the
                # palette's theme-specific selection surface: charcoal-purple in
                # dark mode and pale lavender in light mode. Preserve the log's
                # semantic foreground colours (URL blue, key green, warning gold)
                # rather than flattening everything to one ink colour.
                style = Style(bgcolor=palette.visual)
                # ``Strip.divide([start, end])`` returns only the pieces ending at
                # those cuts; it does *not* include the tail after ``end``. The old
                # three-piece assumption therefore never painted a selection at
                # all. Crop the three ranges explicitly, including selections that
                # begin at column zero or end at the line boundary.
                length = line.cell_length
                start = min(max(start, 0), length)
                end = min(max(end, start), length)
                line = Strip.join(
                    [
                        line.crop(0, start),
                        line.crop(start, end).apply_style(style),
                        line.crop(end, length),
                    ]
                )
        line = line.crop_extend(scroll_x, scroll_x + width, self.rich_style)
        return line.apply_offsets(scroll_x, y)
