"""A focusable, clickable cell for one service.

The main screen lays these out in a grid whose column count is recomputed from
the real terminal width, so a wide window shows more services at once instead of
one long column. Cells stay dumb: they report a click or an Enter, and the
screen owns navigation because only the screen knows the grid geometry.
"""

from __future__ import annotations

from textual.binding import Binding
from textual.message import Message
from textual.widgets import Static


class ServiceCell(Static):
    """One service. Focusable so the keyboard works, clickable so the mouse does."""

    can_focus = True

    BINDINGS = [Binding("enter", "choose", "Open", show=False)]

    class Chosen(Message):
        def __init__(self, service) -> None:
            super().__init__()
            self.service = service

    class Focused(Message):
        def __init__(self, cell: ServiceCell) -> None:
            super().__init__()
            self.cell = cell

    def __init__(self, service, markup: str, *, index: int, wide: bool = False):
        super().__init__(markup, classes="service-cell" + (" wide" if wide else ""))
        self.service = service
        self.index = index

    def action_choose(self) -> None:
        self.post_message(self.Chosen(self.service))

    async def on_click(self) -> None:
        self.focus()
        self.post_message(self.Chosen(self.service))

    def on_focus(self) -> None:
        self.post_message(self.Focused(self))
