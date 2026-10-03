"""Input widget with native system clipboard support."""

from __future__ import annotations

from textual import events
from textual.widgets import Input as TextualInput

from .clipboard import read_clipboard


class ClipboardInput(TextualInput):
    """Textual Input whose Ctrl+V action also sees the OS clipboard."""

    @staticmethod
    def _single_line(value: str) -> str:
        """Keep one-line fields safe when a terminal pastes a document."""
        return str(value or "").splitlines()[0] if value else ""

    def _insert_paste(self, value: str) -> None:
        value = self._single_line(value)
        if not value:
            return
        start, end = self.selection
        self.replace(value, start, end)

    def on_paste(self, event: events.Paste) -> None:
        """Insert bracketed/native paste events sent by Windows Terminal."""
        event.stop()
        event.prevent_default()
        self._insert_paste(event.text)

    def action_paste(self) -> None:
        clipboard = read_clipboard(self.app)
        self._insert_paste(clipboard)
