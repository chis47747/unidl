"""Screen 2: the service's own home.

One question: what do you want to do on this platform? VOD, live, search,
library, settings - whatever the service declares.

The service name is drawn in block letters one size down from the main screen's
``unidl``, which is the whole point of having two sizes: the smaller title says
you are a level in.

Search and Settings mean something narrower here than on the main screen. Search
covers this service's keys rather than the whole vault; Settings covers this
service's own options - resolution, codec, login method - and applies to it
alone. Both are handled by the app's global actions, which look at the current
screen's ``service``.
"""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.widgets import Static

from . import banner
from .askhost import AskHost


class ServiceScreen(AskHost):
    """The service's top menu, and the owner of the flow worker."""

    WAITING_TEXT = "getting the service ready - its options will appear here"
    WAITING_TEXT_ID = "ask.waiting.service"

    def compose_identity(self) -> ComposeResult:
        yield Static("", id="service-banner")
        yield Static("", id="ident-line")
        yield Static("", id="param-line")
        yield Static("", id="status-line")

    def key_hints(self) -> list[tuple[str, str]]:
        return [
            ("enter", "open"),
            ("↑↓", "move"),
            ("^b", "back to platforms"),
            ("^f", "search this service's keys"),
            ("^s", "settings for this service"),
            ("^l", "log"),
        ]

    def on_mount(self) -> None:
        super().on_mount()
        self.refresh_banner()
        self.run_service_flow()

    def on_resize(self) -> None:
        # super() as well as the banner: the host re-wraps the log and re-fits the
        # identity rows, and this override used to replace all of that with a
        # banner redraw - so a resized service screen kept every log line wrapped
        # for the width it no longer had.
        super().on_resize()
        self.refresh_banner()

    def refresh_banner(self) -> None:
        found = self.query("#service-banner")
        if not found:
            return
        # the qualifier ("/ CBS", "(DASH)") stays on the identity row below
        name = banner.title_for(self.service.NAME)
        found.first(Static).update(banner.small_art(name, self.size.width, "$accent"))

    # ------------------------------------------------------------------ worker
    @work(thread=True, exclusive=True, group="service-flow")
    def run_service_flow(self) -> None:
        self.controller.drive()

    # -------------------------------------------------------------- navigation
    def back_without_ask(self) -> bool:
        """Nothing pending: leave the service and go back to the platform list."""
        self.controller.abort()
        return False


__all__ = ["ServiceScreen"]
