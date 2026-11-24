"""Screen 3: the service doing its work.

Everything interactive a service asks for between "I picked this platform" and
"download it" happens here: a URL, a show, a season, an episode, a channel, a
login code.

No block letters. The name, the account and the parameters are plain bold text on
one row each, because at this depth the content is what matters and the header is
just orientation.

Pushed the first time a flow-scoped ask arrives and popped when the flow returns
to the service menu, so backing out of a season list lands you exactly where you
would expect.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Static

from .askhost import AskHost


class FlowScreen(AskHost):
    """Hosts the asks a service raises while it is working."""

    WAITING_TEXT = "working - the next prompt will appear here"
    WAITING_TEXT_ID = "ask.waiting.flow"

    def _on_host_resume(self, _event) -> None:
        if (
            getattr(self.controller, "_close_flow_on_delivery_dismiss", False)
            and self.controller.delivery is None
            and not self.waiting
            and not self.panel_showing
            and self.app.screen is self
        ):
            self.controller._close_flow_on_delivery_dismiss = False
            self.call_after_refresh(self._pop_stale_flow)

    def _pop_stale_flow(self) -> None:
        if (
            self.is_attached
            and self.app.screen is self
            and not self.waiting
            and not self.panel_showing
            and len(self.app.screen_stack) > 1
        ):
            self.app.pop_screen()

    def compose_identity(self) -> ComposeResult:
        yield Static("", id="ident-line")
        yield Static("", id="param-line")
        yield Static("", id="status-line")

    def key_hints(self) -> list[tuple[str, str]]:
        if self.panel_showing:
            # what Back does here is different, and the card in the middle of the
            # screen is not a prompt to move through
            if self.panel_values > 1:
                return [
                    ("1-9", "copy one"),
                    ("^y", "copy all"),
                    ("↑↓", "scroll"),
                    ("^b", "back to the menu"),
                    ("^l", "log"),
                ]
            return [
                ("1", "copy the command"),
                ("^b", "back to the menu"),
                ("^l", "log"),
                ("^s", "settings"),
            ]
        return [
            ("enter", "confirm"),
            ("↑↓", "move"),
            ("digits", "jump to a number"),
            ("^b", "back one step"),
            # a title that failed earlier in this session is rescued from here as
            # well: this is the screen the flow comes back to, and by then the queue
            # has been closed behind it
            *self.queue_hints(),
            *self.chapter_hints(),
            *self.lyrics_hints(),
            *self.attachments_hints(),
            ("^l", "log"),
            ("^s", "settings"),
        ]

    def back_without_ask(self) -> bool:
        """Nothing to answer is three different situations, and they need
        different answers.

        Saved-result cards are handled by AskHost before this fallback: Back
        releases their read gate and the next service ask owns navigation. It
        reuses this page for the previous input/picker or closes it to show the
        root menu. Popping at the same time would unmount the restored prompt.
        Other informational panels may simply be dismissed with their page.

        While the flow is running it means the service is between prompts - busy.
        Back cancels that in-flight flow and pops this page, revealing the service
        menu.  The controller's abort path releases the worker and closes any
        service-owned transport before the page disappears, so it cannot resume
        into a screen that is no longer mounted.

        Once the flow has stopped it means the page can be popped normally.  The
        service menu underneath is still the immediate parent, so Back must reveal
        it rather than tearing down the complete session.  A second Back from that
        service menu then leaves the service and returns to the platform list.
        """
        if self.panel_showing:
            self.clear_panel()
            return False  # let the app pop us, onto whatever was underneath
        if not self.controller.finished:
            self.controller.abort()
            return False  # cancel the flow and pop to the service menu
        self.controller.abort()
        return False  # let the app pop this page onto the service menu


__all__ = ["FlowScreen"]
