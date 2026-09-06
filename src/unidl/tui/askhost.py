"""Shared base for the three screens that host a running service.

Screens 2, 3 and 4 differ in what they put above the content - block letters, a
bold line, a delivery header - and are otherwise the same machine: a place to
mount one ask, a log, and the plumbing that lets a worker thread block on the
user's answer.

The log deliberately does *not* belong to the screen. Screens are popped and
pushed as you move between them, which throws their state away by design, but
the record of what the session did has to survive that. So the controller owns
the lines and every screen replays them on mount.
"""

from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING, Any

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from ..core import drm as drm_registry
from ..core.credentials import mask_in
from ..core.flow import SCOPE_ROOT, TextAsk
from ..core.i18n import tr
from ..core.redact import redact_lines
from .asks import BACK, AskWidget, ManualStep, PanelWidget, widget_for
from .bidi import visual_markup
from .chrome import Chrome, KeyBar, StatusChip
from .logline import LIVE_STATE, LogLine, status_line
from .logpane import SelectableLog

if TYPE_CHECKING:
    from textual.timer import Timer

    from ..core.flow import Ask, Panel
    from .session import SessionController

#: what happens once a title is picked, spelled out rather than abbreviated
MODE_LABEL = {
    "download": "download the file",
    "command": "save the command only",
    "list": "list the tracks only",
    "export": "save an export file",
    "ask": "ask me",
}

#: How many lines the session log keeps. The same bound as the ``RichLog`` below,
#: because the controller's copy exists to replay into that widget: keeping more
#: than the widget can hold is memory nobody can read.
LOG_LIMIT = 4000

#: Half a blink of the "work in progress" dot. Slow on purpose: a dot that flickers
#: reads as an error, and this one is only saying "still going".
PULSE_SECONDS = 0.7

#: How long after the last resize the log is laid out again. Dragging a window edge
#: fires a resize per column; a full log is a few hundred milliseconds to redraw, so
#: it is done once the dragging stops rather than on every step of it.
REFLOW_SECONDS = 0.25

#: How the log pane cycles. Collapsed leaves the header visible, so the log is
#: still discoverable and still says how many lines are waiting in it.
LOG_STATES = ("normal", "tall", "collapsed")

LOG_ARROW = {"normal": "▾", "tall": "▾", "collapsed": "▸"}
LOG_NOTE = {
    "normal": "click to expand",
    "tall": "click to collapse",
    "collapsed": "click to show",
}


#: Colour tags, for measuring a string by what it draws rather than by what it says.
_TAGS = re.compile(r"\[/?[^\]]*\]")


def visible_width(markup: str) -> int:
    """How many columns ``markup`` will occupy once its tags are gone."""
    return len(_TAGS.sub("", str(markup or "")))


def join_fitted(
    parts: list[tuple[str, str]], width: int, separator: str, *, padding: int = 4
) -> str:
    """Join ``(markup, plain)`` segments, dropping from the right until they fit.

    The same rule the key bar and the main screen's status line already follow:
    lose the least important thing whole rather than truncating the most important
    one mid-word. Measured on the plain copy, because the markup is mostly colour
    tags and a widget cannot be asked its width before it is drawn.

    The separator is measured too, rather than assumed. It used to be counted as a
    fixed five columns - the width of ``"  ·  "``, which is what both callers pass -
    so the first caller to separate its segments with anything else would have been
    measured against a gap it was not using.
    """
    if not parts:
        return ""
    room = max(20, (width or 80) - padding)
    gap = visible_width(separator)
    kept = list(parts)
    while len(kept) > 1:
        used = sum(len(plain) for _, plain in kept) + gap * (len(kept) - 1)
        if used <= room:
            break
        kept.pop()
    return separator.join(markup for markup, _ in kept)


class AskHost(Screen):
    """A screen that can host one ask at a time on behalf of a flow worker."""

    #: shown in the chrome; only the main screen has nothing to go back to
    CAN_GO_BACK = True
    #: extra classes for the log pane, so screen 4 can start it expanded
    LOG_CLASSES = "empty"
    #: what the content area says when there is nothing to answer. Empty means
    #: the screen states it some other way, so do not add a second voice.
    WAITING_TEXT = ""
    #: how many values a panel can carry and still be centred rather than listed
    CENTRE_ROWS = 2

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("b", "app.global_back", "Back", show=False),
        Binding("ctrl+f", "app.global_search", "Search", show=False),
        Binding("ctrl+s", "app.global_settings", "Settings", show=False),
        Binding("s", "app.global_settings", "Settings", show=False),
        Binding("slash", "app.global_search", "Search", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("ctrl+l", "toggle_log", "Log", show=True),
        Binding("ctrl+p", "command_palette", "Commands", show=True),
        Binding("space", "noop", "Toggle", show=True),
        Binding("enter", "confirm", "Confirm", show=True),
        # Copying what a manual step is waiting on. Bound here rather than on the
        # panel because that panel deliberately takes no focus, and a binding on
        # an unfocused widget never fires. Both actions do nothing unless such a
        # panel is up, so a digit still means "jump to that entry" in a list -
        # the focused list is asked first either way.
        Binding("o", "open_await_link", "Open link", show=False),
        *[Binding(str(digit), f"copy_await({digit})", show=False) for digit in range(1, 10)],
        # a whole card at once, for the run that produced more rows than there are
        # digit keys. ctrl, because y is a letter someone might be typing.
        Binding("ctrl+y", "copy_all", "Copy all", show=False),
        # the queue, from wherever the session has got to
        Binding("ctrl+r", "retry_failed", "Run the failed ones again", show=False),
        Binding("ctrl+x", "skip_one", "Skip this one", show=False),
        # A card can be taller than the area it sits in - ten commands is fifty
        # rows - and it takes no focus, so there is nothing else for these keys to
        # reach. A focused list is asked first, so they still move the list.
        Binding("up", "scroll_panel(-2)", show=False),
        Binding("down", "scroll_panel(2)", show=False),
        Binding("pageup", "scroll_panel(-12)", show=False),
        Binding("pagedown", "scroll_panel(12)", show=False),
    ]

    def __init__(self, controller: SessionController):
        super().__init__()
        self.controller = controller
        self.service = controller.service
        self.settings = controller.settings
        self._ask_box: dict[str, Any] | None = None
        self._ask_event: threading.Event | None = None
        # Scope of the currently mounted ask.  Back needs this small piece of
        # navigation context before ``_resolve`` clears the widget: a Back on the
        # service's root menu ends the session, while Back on a flow/delivery ask
        # returns to that ask's parent page.
        self._ask_scope: str | None = None
        self._current_ask: AskWidget | None = None
        #: a finished artefact left in the middle of the screen. Its own slot
        #: rather than the ask's, because nothing is waiting on it: everything
        #: that reasons about "is the flow blocked" must not see it.
        self._panel: PanelWidget | None = None
        self._log_state = "normal"
        self._log_lines = 0
        self._error: Static | None = None
        #: The newest log line, kept because it is the only one whose state can
        #: still change: while its work is running its dot pulses, and it settles
        #: the moment another line lands on top of it.
        self._last_line: Any = None
        #: what the status row is currently saying, and in what state
        self._status: LogLine | None = None
        #: which half of the pulse the dots are on
        self._pulse_dim = False
        self._pulse_timer: Timer | None = None
        #: the pane width the log was last laid out for. A line is rendered into
        #: fixed rows when it is written, so a pane that changes width - including
        #: the first line, which is what reveals the pane and gives it a width at
        #: all - has to be laid out again or its long lines keep the wrapping of a
        #: size the pane no longer is.
        self._drawn_width = 0
        self._reflow_timer: Timer | None = None
        #: The identity row's segments, as (markup, plain) pairs. Kept because
        #: building them asks the service for its login state, which reads the token
        #: files, while *fitting* them is arithmetic on the width - and a window
        #: being dragged re-fits many times for one state. Dropped whenever that
        #: state can have changed; see :meth:`refresh_after_settings`.
        self._ident_bits: list[tuple[str, str]] | None = None

    # ------------------------------------------------------------- composition
    def compose(self) -> ComposeResult:
        yield self.chrome_widget()
        yield from self.compose_identity()
        with Vertical(id="body"):
            # a sibling of the content, not a child of it: inside #ask-area it
            # would compete with a height:1fr list and lose
            yield Static("", id="error-panel")
            yield from self.compose_content()
            with Vertical(id="log-pane", classes=self.LOG_CLASSES):
                with Horizontal(id="log-bar"):
                    # a clickable header, because ctrl+l is not something you find
                    # by looking at the screen
                    yield StatusChip("toggle_log", id="log-header")
                    # and beside it, the one thing people do with a whole log:
                    # take it somewhere else to ask about it
                    yield StatusChip("share_log", id="log-share")
                yield SelectableLog(
                    highlight=False,
                    markup=False,
                    wrap=True,
                    max_lines=LOG_LIMIT,
                    # RichLog renders at 78 columns unless told otherwise, so on a
                    # narrower pane the far end of a long line was cropped and left
                    # reachable only by scrolling sideways - which is how a save
                    # name wrapped onto a second row while the manifest URL beside
                    # it silently lost its tail. Zero means "the width there is",
                    # and then a long value folds like everything else.
                    min_width=0,
                )
        yield KeyBar(*self.key_hints())

    def chrome_widget(self) -> Chrome:
        """Top bar for this screen; download delivery adds Stop/Resume here."""
        return Chrome(can_go_back=self.CAN_GO_BACK)

    def compose_identity(self) -> ComposeResult:
        """Rows above the content: who and where you are. Subclasses add to it."""
        yield Static("", id="ident-line")
        yield Static("", id="param-line")

    def compose_content(self) -> ComposeResult:
        yield from self.compose_ask_area()

    def compose_ask_area(self) -> ComposeResult:
        """Where asks are mounted, plus what it says while there are none."""
        with VerticalScroll(id="ask-area"):
            yield Static("", id="waiting")

    def key_hints(self) -> list[tuple[str, str]]:
        return [
            ("enter", "confirm"),
            ("↑↓", "move"),
            ("^b", "back"),
            ("^l", "log"),
            ("^s", "settings"),
        ]

    def refresh_keys(self) -> None:
        """Re-render the key bar, for a screen whose hints depend on its state.

        The bar is built once at compose time, so a screen that answers
        ``key_hints`` differently as things change has to say when they have.
        """
        found = self.query(KeyBar)
        if found:
            found.first(KeyBar).render_pairs(*self.key_hints())

    def on_mount(self) -> None:
        self.refresh_after_settings()
        self._render_waiting()
        self._render_log_header()
        self.controller.attach(self)
        # A log line and the status row are Rich text, and Rich bakes the colour in
        # when it is built. Switching theme repaints the stylesheet but cannot reach
        # inside something already rendered, so a log written on the dark theme
        # stayed near-white after a switch to the light one - white on white. This
        # is what redraws them from their parts. The signal unsubscribes itself when
        # this screen goes away.
        signal = getattr(self.app, "theme_changed_signal", None)
        if signal is not None:
            signal.subscribe(self, self._theme_changed)

    def on_screen_resume(self, _event: events.ScreenResume) -> None:
        self.call_after_refresh(self.controller.resume_deferred_leave)

    def _theme_changed(self, _theme=None) -> None:
        """Draw everything that carries a baked-in colour again. UI thread."""
        self._drawn_width = 0  # force the reflow to run rather than short-circuit
        self._ident_bits = None  # the segments carry palette variables of their own
        self._reflow_log()
        self._render_status()
        self._render_log_header()
        self.refresh_after_settings()

    def on_unmount(self) -> None:
        self.controller.detach(self)
        self.release()

    # ------------------------------------------------------------- identity band
    def relocalize(self) -> None:
        from .chrome import Chrome

        for chrome in self.query(Chrome):
            chrome.refresh_locale()
        self.refresh_keys()
        self._render_waiting()
        # These two rows are already mounted Rich text rather than widgets that
        # rebuild themselves. Redraw them as well, otherwise changing the
        # interface language while a service/download screen is open leaves the
        # old-language log header and transient status behind.
        self._render_log_header()
        self._render_status()
        self.refresh_after_settings()

    def refresh_after_settings(self) -> None:
        # The device may have been changed from the main screen while this session
        # was open, or in this service's own settings, and the context holds a
        # snapshot of it. Re-resolve before drawing, so the row states the device
        # the next licence request will actually use. Through the service rather
        # than the context: the service knows which DRM system is active, and
        # therefore which of its own CDM choices applies.
        try:
            self.service.refresh_device()
            # The proxy is the same kind of snapshot, and changing it mid-session
            # otherwise took effect only the next time the service was opened.
            self.service.ctx.refresh_proxy()
        except Exception:  # noqa: BLE001 - a stale row is better than a dead screen
            pass
        # the state behind the row has just been re-resolved, so what was built from
        # the old state is no longer what this row says
        self._ident_bits = None
        self._render_identity()
        self._render_params()

    def _identity_bits(self) -> list[tuple[str, str]]:
        """The identity row's segments, built once per state rather than per paint.

        Asking the service for its login state means reading whatever it keeps on
        disk, and this row is rebuilt on every re-fit - which, while a window is
        being dragged, is once every quarter second for a state that has not
        changed. Building is cached; fitting is not, because that is the part that
        depends on the width.
        """
        if self._ident_bits is not None:
            return self._ident_bits
        status = self.service.auth_status()
        account = mask_in(status.label)
        device = self.service.ctx.device_name or "no cdm"
        self._ident_bits = [
            # theme variables, not literals: this used to name the dark palette's
            # own foreground, which on a white terminal is white text on white
            (f"[bold $foreground]{visual_markup(self.service.NAME)}[/]", self.service.NAME),
            (f"[$muted]{visual_markup(account)}[/]", account),
            (f"[$dim]{visual_markup(device)}[/]", device),
        ]
        return self._ident_bits

    def _render_identity(self) -> None:
        """Service name, account and CDM, at normal size.

        Escaped and fitted rather than joined: the account label is a service's own
        sentence and the device is a file name, both of which have contained square
        brackets, and this row is one line high - so anything past the edge was cut
        mid-word, leaving a separator with nothing after it. The CDM in particular
        must not be the part that silently disappears: it says which device the next
        licence request will use.
        """
        line = self.query("#ident-line")
        if not line:
            return
        line.first(Static).update(
            join_fitted(self._identity_bits(), self.size.width, "  [$gutter]·[/]  ")
        )

    def _render_params(self) -> None:
        """The settings that will shape whatever this screen does next."""
        line = self.query("#param-line")
        if not line:
            return
        mode = str(self.settings.get("after_resolve", "download"))
        quality = self.settings.get("video_quality", "best")
        quality_label = "best available" if quality in ("best", "worst") else f"{quality}p"
        tracks_label = (
            "choose tracks myself"
            if self.settings.get("track_mode") == "interactive"
            else "tracks picked automatically"
        )
        mode_label = MODE_LABEL.get(mode, mode)
        drm_label = self._drm_label()
        # Same treatment as the row above: whole segments are dropped from the
        # right when they will not fit, because "quality best available" clipped
        # to "quality best" is a different claim, and "tracks picked" is not one
        # at all. The keyboard hint leads, so it is the last thing to go.
        parts = [
            (
                f"[$accent]^s[/] [$muted]after picking[/] "
                f"[$foreground]{visual_markup(mode_label)}[/]",
                f"^s after picking {mode_label}",
            ),
            (f"[$muted]drm[/] [$foreground]{visual_markup(drm_label)}[/]", f"drm {drm_label}"),
            (
                f"[$muted]quality[/] [$foreground]{visual_markup(quality_label)}[/]",
                f"quality {quality_label}",
            ),
            (f"[$muted]{tracks_label}[/]", tracks_label),
        ]
        if self.settings.get("debug"):
            parts.append(("[$warn]debug[/]", "debug"))
        line.first(Static).update(join_fitted(parts, self.size.width, "  [$dim]·[/]  "))

    def _drm_label(self) -> str:
        system_id = self.service.drm_system()
        system = drm_registry.get(system_id)
        return system.label if system is not None else str(system_id).title()

    def set_status(self, text: str, state: str = LIVE_STATE) -> None:
        """One line of "what is happening right now", if the screen shows one.

        Dotted like a log line and bold unlike one: the log is a record to scan
        back through, this is the single thing happening now, and it replaces
        itself rather than accumulating.
        """
        self._status = LogLine(body=str(text), state=state) if text else None
        self._render_status()
        self._sync_pulse()

    def _render_status(self) -> None:
        found = self.query("#status-line")
        if not found:
            return
        status = self._status
        found.first(Static).update(
            status_line(status.body, status.state, self.app.palette, dim=self._pulse_dim)
            if status is not None
            else ""
        )

    # --------------------------------------------------------------- empty state
    def _render_waiting(self) -> None:
        """Say what happens next, rather than leaving the area blank.

        An empty content area and a broken one look the same. Screens that state
        it elsewhere - the delivery header, the status row - set WAITING_TEXT to
        empty so there is only ever one voice.
        """
        found = self.query("#waiting")
        if not found:
            return
        quiet = self.waiting or self._error is not None or self._panel is not None
        ident = getattr(self, "WAITING_TEXT_ID", "")
        note = "" if quiet else (tr(ident) if ident else self.WAITING_TEXT)
        widget = found.first(Static)
        widget.update(f"[$dim]{note}[/]" if note else "")
        widget.display = bool(note)

    # --------------------------------------------------------------------- error
    def show_error(self, headline: str, detail: str = "", hint: str = "") -> None:
        """Put a failure in the content area, where you are already looking.

        A red line in a log the user may have collapsed is not a report. This
        states what broke and what to do about it, in the middle of the screen.
        """
        found = self.query("#error-panel")
        if not found:
            return
        body = f"[bold $bad]{visual_markup(headline)}[/]"
        if detail:
            body += f"\n[$foreground]{visual_markup(detail)}[/]"
        if hint:
            body += f"\n[$muted]{visual_markup(hint)}[/]"
        panel = found.first(Static)
        panel.update(body)
        panel.display = True
        self._error = panel
        self._render_waiting()

    def clear_error(self) -> None:
        found = self.query("#error-panel")
        if found:
            panel = found.first(Static)
            panel.update("")
            panel.display = False
        self._error = None

    # --------------------------------------------------------------------- log
    def _drawn(self, line: Any) -> Any:
        """A log line as text. Anything already renderable is passed through."""
        render = getattr(line, "render", None)
        if callable(render):
            return render(self.app.palette, dim=self._pulse_dim and bool(line.live))
        return line

    def replay(self, lines: list[Any]) -> None:
        """Draw the session's log so far. Called when the screen appears."""
        if not lines:
            return
        log = self.query(SelectableLog)
        if not log:
            return
        widget = log.first(SelectableLog)
        for line in lines:
            widget.write_counted(self._drawn(line))
        self._last_line = lines[-1]
        self._log_lines = len(lines)
        self._reveal_log()
        self._sync_pulse()
        self._reflow_log()

    def write(self, renderable: Any) -> None:
        log = self.query(SelectableLog)
        if not log:
            return
        widget = log.first(SelectableLog)
        # The line that was newest is not any more, so whatever it was doing has
        # been overtaken: settle it before the new one goes under it.
        self._settle_last(widget)
        widget.write_counted(self._drawn(renderable))
        self._last_line = renderable
        self._log_lines += 1
        self._reveal_log()
        self._sync_pulse()
        # The pane had no width while it was hidden, so the line that revealed it
        # was wrapped for a pane that was not there yet.
        self._reflow_log()

    # ------------------------------------------------------------------ reflow
    def on_resize(self) -> None:
        """Lay out everything whose shape is a function of the width.

        Debounced through one timer, because a drag arrives as a stream of resizes
        and one of the things below reads the service's login state.
        """
        if self._reflow_timer is not None:
            self._reflow_timer.stop()
        self._reflow_timer = self.set_timer(REFLOW_SECONDS, self._refit)

    def _refit(self) -> None:
        """The log's wrapping, and the two rows that drop segments to fit.

        Those rows were laid out on mount and after a settings change only, so a
        window that changed size kept a row measured for the old one: widened, it
        stayed short of the CDM it exists to name; narrowed, it ran off the edge it
        was supposed to stop at. They are cheap to rebuild and this is the one place
        that knows the width changed.
        """
        self._reflow_timer = None
        self._reflow_log()
        self._render_identity()
        self._render_params()

    def _reflow_log(self) -> None:
        """Redraw every line, if the pane is a different width than they were.

        A written line is a fixed set of rows: the pane keeps rendered strips, not
        renderables, so it cannot re-wrap anything by itself. Redrawing from the
        session's own list is what makes a narrowed window fold a manifest URL
        instead of hiding its tail, and it is why a line is kept as its parts.
        """
        self._reflow_timer = None
        log = self.query(SelectableLog)
        if not log:
            return
        widget = log.first(SelectableLog)
        width = widget.scrollable_content_region.width
        if width <= 0 or width == self._drawn_width:
            return
        lines = list(getattr(self.controller, "log_lines", None) or ())
        # Not while a drag is in progress: the selection is a range of rows, and
        # re-wrapping moves the text under it. Do not record the new width until
        # the redraw really happened; otherwise a resize during a selection is
        # considered handled and the old wrapping survives after mouse-up.
        if widget.has_selection:
            return
        self._drawn_width = width
        if not lines:
            return
        widget.clear()
        for line in lines:
            widget.write_counted(self._drawn(line))
        self._last_line = lines[-1]
        self._log_lines = len(lines)
        self._render_log_header()

    # ------------------------------------------------------------------- pulse
    def _settle_last(self, widget: SelectableLog) -> None:
        """Redraw the newest line as finished, if it was drawn mid-pulse."""
        last = self._last_line
        if last is None or not getattr(last, "live", False) or not self._pulse_dim:
            return
        self._pulse_dim = False
        if not widget.has_selection:
            widget.redraw_last(self._drawn(last))

    def _pulsing(self) -> bool:
        """Whether anything on this screen is still reporting work in progress."""
        return bool(
            getattr(self._last_line, "live", False) or getattr(self._status, "live", False)
        )

    def _sync_pulse(self) -> None:
        """Start the pulse when there is something live, stop it when there is not."""
        if self._pulsing():
            if self._pulse_timer is None:
                self._pulse_timer = self.set_interval(PULSE_SECONDS, self._pulse)
            return
        if self._pulse_timer is not None:
            self._pulse_timer.stop()
            self._pulse_timer = None
        if self._pulse_dim:
            self._pulse_dim = False
            self._repaint_live()

    def _pulse(self) -> None:
        """Half a blink: the dot fades, then comes back. Slow, and only the dot."""
        if not self._pulsing():
            self._sync_pulse()
            return
        self._pulse_dim = not self._pulse_dim
        self._repaint_live()

    def _repaint_live(self) -> None:
        """Draw the live status line and the live log line again."""
        if getattr(self._status, "live", False):
            self._render_status()
        last = self._last_line
        if last is None or not getattr(last, "live", False):
            return
        # Never while a selection is up: redrawing a line moves text under the
        # pointer, and a pulsing dot is not worth breaking a drag for.
        log = self.query(SelectableLog)
        if not log:
            return
        widget = log.first(SelectableLog)
        if widget.has_selection:
            return
        widget.redraw_last(self._drawn(last))

    def _reveal_log(self) -> None:
        pane = self.query("#log-pane")
        if pane:
            pane.first().remove_class("empty")
        self._render_log_header()

    def _render_log_header(self) -> None:
        share = self.query("#log-share")
        if share:
            # hidden, not blanked: an empty label left a chip that still hovered and
            # still answered a click, by copying nothing
            chip = share.first()
            chip.display = bool(self._log_lines)
            chip.update("[$dim]copy for a report[/]" if self._log_lines else "")
        found = self.query("#log-header")
        if not found:
            return
        state = self._log_state
        # clamped: the pane holds at most LOG_LIMIT, so counting every line ever
        # written had the header claiming 6000 over a pane with 4000 in it
        count = min(self._log_lines, LOG_LIMIT)
        label = "1 line" if count == 1 else f"{count} lines"
        found.first(Static).update(
            f"[$accent]{LOG_ARROW[state]}[/] [$muted]log[/]  "
            f"[$muted]{label}[/]  [$dim]^l · {LOG_NOTE[state]}[/]"
        )

    def action_toggle_log(self) -> None:
        """Cycle the log: normal height, tall, header only."""
        pane = self.query("#log-pane")
        if not pane:
            return
        target = pane.first()
        if target.has_class("empty"):
            return
        self._log_state = LOG_STATES[(LOG_STATES.index(self._log_state) + 1) % len(LOG_STATES)]
        target.set_class(self._log_state == "tall", "tall")
        target.set_class(self._log_state == "collapsed", "collapsed")
        self._render_log_header()
        self.refresh_keys()

    def action_share_log(self) -> None:
        """Put the whole log on the clipboard, with the secrets taken out.

        The log itself is left exactly as it is. A manifest URL is copied out of it
        in order to be *used*, and it stops working the moment its signature is
        masked; the same goes for the command file. So the masking happens here, on
        the way to the clipboard, and the notification says which half was masked so
        nobody has to guess before pasting it into an issue.
        """
        lines = getattr(self.controller, "log_lines", None) or ()
        text = redact_lines(str(line) for line in lines)
        if not text.strip():
            self.notify("The log is empty", timeout=3)
            return
        try:
            self.app.copy_text(text)
        except Exception:  # noqa: BLE001 - a terminal that refuses the clipboard
            self.notify("This terminal would not take the clipboard", timeout=4)
            return
        self.notify(
            f"Copied {len(text.splitlines())} lines. Passwords, tokens, signed URLs "
            "and your home path are masked; content keys and titles are not.",
            title="Log copied",
            timeout=6,
        )

    def action_noop(self) -> None:
        """Key-bar hint only; the focused ask widget handles these keys."""

    def chapter_payload(self) -> tuple[str, tuple]:
        """Title and chapters for the current ask, or the delivery playback."""
        current = getattr(self, "_current_ask", None)
        ask = getattr(current, "ask", None)
        chapters = tuple(getattr(ask, "chapters", ()) or ())
        if chapters:
            title = str(getattr(ask, "title", "") or "")
            name = title.split("·", 1)[-1].strip() if "·" in title else title
            return name, chapters
        playback = getattr(self, "_chapter_playback", None)
        if playback is not None and getattr(playback, "chapters", ()):
            return str(playback.save_name), tuple(playback.chapters)
        return "", ()

    def chapter_hints(self) -> list[tuple[str, str]]:
        _name, chapters = self.chapter_payload()
        return [("c", "chapters")] if chapters else []

    def lyrics_payload(self) -> tuple[str, object | None]:
        current = getattr(self, "_current_ask", None)
        ask = getattr(current, "ask", None)
        lyrics = getattr(ask, "lyrics", None)
        if lyrics is not None:
            title = str(getattr(ask, "title", "") or "")
            name = title.split("·", 1)[-1].strip() if "·" in title else title
            return name, lyrics
        playback = getattr(self, "_chapter_playback", None)
        if playback is not None and getattr(playback, "lyrics", None) is not None:
            return str(playback.save_name), playback.lyrics
        return "", None

    def lyrics_hints(self) -> list[tuple[str, str]]:
        _name, lyrics = self.lyrics_payload()
        return [("l", "lyrics")] if lyrics is not None else []

    def action_show_lyrics(self) -> None:
        name, lyrics = self.lyrics_payload()
        if lyrics is None:
            self.notify(tr("lyrics.none"), timeout=3)
            return
        from .lyrics_screen import LyricsScreen

        self.app.push_screen(LyricsScreen(name, lyrics))

    def action_show_chapters(self) -> None:
        name, chapters = self.chapter_payload()
        if not chapters:
            self.notify(tr("chapters.none"), timeout=3)
            return
        from .chapters_screen import ChaptersScreen

        self.app.push_screen(ChaptersScreen(name, chapters))

    # ------------------------------------------------------------ queue actions
    # On every screen of a session, not only on the one that draws the queue. The
    # download screen is closed the moment the flow asks its next question, so a
    # key that only lived there was gone by the time anybody looked at the failure
    # it was for - which is exactly when you want it.
    def queue_hints(self) -> list[tuple[str, str]]:
        """The two queue keys, offered only when they would do something."""
        controller = self.controller
        hints: list[tuple[str, str]] = []
        if controller.job_running and not controller.cancel_requested:
            hints.append(("^x", "skip just this one"))
        rescuable = sum(1 for job in controller.jobs if job.retryable)
        if rescuable and not controller.job_running and not controller._retry_pending:
            hints.append(
                (
                    "^r",
                    tr("keys.retry_n", count=rescuable) if rescuable > 1 else "run it again",
                )
            )
        return hints

    def action_retry_failed(self) -> None:
        """Run every row that failed or was stopped, in queue order."""
        started = self.controller.retry()
        if not started:
            self.notify(
                "Wait for the one in flight to finish first"
                if self.controller.job_running or self.controller._retry_pending
                else "Nothing to run again",
                timeout=3,
            )
            return
        self.notify(f"Running {started} again", timeout=3)
        self.refresh_keys()

    def retry_job(self, job) -> None:
        """Run one row again, from a click on it."""
        if self.controller.retry([job]):
            self.notify(f"Running {job.name} again", timeout=3)
            self.refresh_keys()
        else:
            self.notify("Wait for the one in flight to finish first", timeout=3)

    def action_skip_one(self) -> None:
        """Stop what is in flight and let the rest of the queue carry on."""
        if self.controller.request_skip():
            self.notify("Skipping this one. The rest of the queue carries on.", timeout=4)
            self.refresh_keys()

    def action_confirm(self) -> None:
        """Enter. Finishes with a card that is holding the flow, if one is.

        Nothing else here answers Enter: an ask widget has focus and handles its
        own, so this only ever fires when the middle of the screen is showing
        something to read rather than something to answer.
        """
        controller = getattr(self, "controller", None)
        if controller is not None and getattr(controller, "card_waiting", False):
            controller.card_read()

    # -------------------------------------------------------------------- asks
    @property
    def waiting(self) -> bool:
        return self._ask_event is not None

    def show_ask(self, ask: Ask, box: dict[str, Any], event: threading.Event) -> None:
        """Mount the widget for ``ask``. The worker is waiting on ``event``."""
        self._clear_ask()
        # a question takes the middle of the screen back: it is what the user has
        # to deal with now, and the artefact it replaces is in the log
        self.clear_panel()
        self.clear_error()
        self._ask_box = box
        self._ask_event = event
        self._ask_scope = getattr(ask, "scope", None)
        try:
            widget = widget_for(ask)
        except TypeError:
            box["value"] = None
            event.set()
            self._ask_box = None
            self._ask_event = None
            self._ask_scope = None
            return
        self._current_ask = widget
        area = self.query("#ask-area")
        if not area:
            box["value"] = None
            event.set()
            self._ask_box = None
            self._ask_event = None
            self._ask_scope = None
            return
        target = area.first(VerticalScroll)
        # Something to go and do by hand is not a list to work down: it is one
        # thing, and it belongs in the middle where the eye already is. Everything
        # else keeps its old place - a bare field at the bottom, next to where what
        # you type lands, and lists from the top, where reading starts.
        #
        # Asked of the widget rather than the ask: "is this drawn as a card" is the
        # widget's own answer, and three different asks can be.
        card = widget.has_class("manual-card")
        target.set_class(isinstance(ask, TextAsk) and not card, "bottom")
        target.set_class(card, "centre")
        target.mount(widget)
        self._render_waiting()
        widget.focus_first()
        self.refresh_keys()

    def _clear_ask(self) -> None:
        if self._current_ask is not None:
            self._current_ask.remove()
            self._current_ask = None
        self._ask_scope = None
        self._render_waiting()

    def clear_ask(self) -> None:
        """Take the current ask off the screen, and the question with it.

        Public because the controller does this from the outside: a device-code
        panel is answered by polling, not by the widget, so when the poll comes
        back there is nobody on this screen to remove it. Without this the card
        stayed up - "open this page and enter 85731187" - long after the code had
        been accepted, until some later ask happened to replace it.

        The pending box goes too. Removing only the widget left :attr:`waiting`
        true with nothing waiting, which is what ``go_back`` consults - so the
        first Back after a device-code sign-in answered a box nobody was reading
        and reported itself as handled, and the screen would not close.
        """
        self._ask_box = None
        self._ask_event = None
        self._clear_ask()

    # ------------------------------------------------------------------ panels
    def show_panel(self, panel: Panel) -> None:
        """Put a finished artefact in the middle of the screen and leave it there.

        Not an ask: nothing blocks on it, so a batch of ten produces ten of these
        without stopping ten times. Same card as a manual step, and the same keys
        copy out of it.
        """
        self.clear_panel()
        area = self.query("#ask-area")
        if not area:
            return
        widget = PanelWidget(panel)
        self._panel = widget
        target = area.first(VerticalScroll)
        # the middle of the screen, and never the bottom: the bottom is where a
        # field goes, and there is nothing to type here.
        #
        # Except when the card is long. A centred card taller than the area it is
        # centred in loses its first rows off the top, where nothing can scroll
        # back to them - so a batch's worth of commands starts at the top and
        # scrolls down like the list it is.
        target.remove_class("bottom")
        short = len(panel.actions) <= self.CENTRE_ROWS and len(panel.lines) <= self.CENTRE_ROWS + 4
        target.set_class(short, "centre")
        target.set_class(not short, "centre-top")
        target.mount(widget)
        self._render_waiting()
        self.refresh_keys()

    def clear_panel(self) -> None:
        if self._panel is not None:
            self._panel.remove()
            self._panel = None
            # Whatever the reason it went, there is nothing left to read, so a gate
            # still held for it would hold the flow on an empty screen. The usual
            # way here is a flow-scope ask, which has already waited its turn; the
            # one that has not is a delivery-scope ask for the *next* title of a
            # batch, whose own command is appended to a fresh card anyway.
            controller = getattr(self, "controller", None)
            if controller is not None:
                release = getattr(controller, "card_gone", None)
                if callable(release):
                    release()
            area = self.query("#ask-area")
            if area:
                area.first(VerticalScroll).remove_class("centre-top")
            self._render_waiting()
            self.refresh_keys()

    @property
    def panel_showing(self) -> bool:
        """True while an artefact is on screen waiting to be read, not answered."""
        return self._panel is not None

    @property
    def panel_values(self) -> int:
        """How many numbered values the card is showing, if there is one."""
        return len(self._panel.ask.actions) if self._panel is not None else 0

    # ------------------------------------------------------- manual steps
    @property
    def manual_step(self) -> ManualStep | None:
        """What is on screen showing something to act on, if anything.

        Three things qualify: an ask confirmed on another device, an ask whose
        answer is pasted back here, and a panel that is not an ask at all. A text
        field with nothing to act on does not, so the copy keys do nothing while an
        ordinary prompt is up.

        The ask wins when both are up, because it is the live one.
        """
        for widget in (self._current_ask, self._panel):
            if isinstance(widget, ManualStep) and widget.ask.lines:
                return widget
        return None

    def copy_await_value(self, number: int) -> None:
        """Put the ``number``-th value on the clipboard, and say which one.

        A panel built only out of prose has nothing numbered, so ``1`` there means
        the whole block. A number nothing is labelled with copies nothing: quietly
        taking the wrong value is worse than the keypress doing nothing.
        """
        panel = self.manual_step
        if panel is None:
            return
        found = panel.value_at(number)
        if found is not None:
            label, text = found[0], found[1]
        elif not panel.value_at(1) and number == 1:
            label, text = "", panel.copyable()
        else:
            return
        if not text:
            return
        try:
            self.app.copy_text(text)
        except Exception:
            # a terminal that refuses the clipboard is not a failure worth an
            # error: show the value instead, so it can still be read off
            self.notify(text, title=label or "Copy", timeout=10)
            return
        shown = text if len(text) <= 60 else f"{text[:57]}..."
        self.notify(f"Copied {shown}", title=label or "Copy", timeout=3)

    def action_copy_await(self, number: int) -> None:
        self.copy_await_value(number)

    def action_copy_all(self) -> None:
        """Everything the card is showing, one value per line.

        For the run that produced more rows than there are digit keys: ten commands
        pasted into a file is a script, which is what a batch of saved commands is
        for.
        """
        panel = self.manual_step
        if panel is None:
            return
        text = panel.copyable()
        if not text:
            return
        count = len(panel.ask.actions) or 1
        try:
            self.app.copy_text(text)
        except Exception:
            self.notify(text, title="Copy", timeout=10)
            return
        self.notify(f"Copied {count} value(s)", title="Copy", timeout=3)

    def action_scroll_panel(self, delta: int) -> None:
        """Move a card that is taller than the space it is in."""
        if self._panel is None:
            return
        area = self.query("#ask-area")
        if area:
            area.first(VerticalScroll).scroll_relative(y=delta, animate=False)

    def open_manual_link(self) -> None:
        """Hand the activation link to the browser, since typing it is the chore."""
        panel = self.manual_step
        if panel is None:
            return
        link = panel.link()
        if link:
            self.app.open_url(link)

    def action_open_await_link(self) -> None:
        self.open_manual_link()

    def _resolve(self, value: Any) -> None:
        if self._ask_box is None or self._ask_event is None:
            return
        self._ask_box["value"] = value
        event, self._ask_event = self._ask_event, None
        self._ask_box = None
        self._clear_ask()
        event.set()

    def on_ask_widget_answered(self, event: AskWidget.Answered) -> None:
        event.stop()
        self._resolve(event.value)

    def release(self) -> None:
        """Unblock a waiting worker so teardown cannot deadlock.

        The flow runs on a thread waiting on an Event. If this screen goes away
        first - the app quits, an error unwinds the UI - nothing would ever set
        that Event and shutdown would hang.
        """
        if self._ask_box is None or self._ask_event is None:
            return
        self._ask_box["quit"] = True
        event, self._ask_event = self._ask_event, None
        self._ask_box = None
        self._ask_scope = None
        event.set()

    # -------------------------------------------------------------- navigation
    def go_back(self) -> bool:
        """Answer the pending ask with Back; the flow decides what that means."""
        if self.waiting:
            root_back = getattr(self, "_ask_scope", None) == SCOPE_ROOT
            controller = getattr(self, "controller", None)
            if root_back and controller is not None:
                mark = getattr(controller, "mark_root_back", None)
                if callable(mark):
                    mark()
            self._resolve(BACK)
            return True
        # Back on a command/result card means "I have read it" and then one normal
        # navigation step.  The card lives on the current flow page; after it is
        # dismissed, popping that page reveals the immediate parent.  Keeping the
        # handler as ``False`` here is important: returning ``True`` consumed the
        # key and left the user on a blank/finished page, while the old deferred
        # teardown could jump two levels to the platform list.
        controller = getattr(self, "controller", None)
        if controller is not None and getattr(controller, "card_waiting", False):
            controller.card_read()
            return False
        return self.back_without_ask()

    def back_without_ask(self) -> bool:
        """Nothing is being asked. Subclasses say whether the screen may close."""
        return False


__all__ = ["LOG_STATES", "MODE_LABEL", "AskHost"]
