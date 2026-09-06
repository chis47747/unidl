"""Input widget with native system clipboard support."""

from __future__ import annotations

from textual.widgets import Input as TextualInput

from .clipboard import read_clipboard


class ClipboardInput(TextualInput):
    """Textual Input whose Ctrl+V action also sees the OS clipboard."""

    def action_paste(self) -> None:
        clipboard = read_clipboard(self.app)
        # Input is a one-line widget.  Pasting the first line keeps a copied
        # multi-line document from corrupting the field or triggering submit.
        line = clipboard.splitlines()[0] if clipboard else ""
        if not line:
            return
        start, end = self.selection
        self.replace(line, start, end)
