"""Local release information and the reserved remote-update check surface."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from .. import __version__
from ..core.i18n import tr

RELEASE_NOTES = (
    "update.note.downloader",
    "update.note.service_views",
    "update.note.media_types",
    "update.note.documentation",
    "update.note.clipboard",
)


class _CloseMark(Static):
    def on_click(self, event) -> None:
        event.stop()
        closer = getattr(self.screen, "action_close", None)
        if callable(closer):
            closer()


class UpdateScreen(ModalScreen[None]):
    """A truthful update dialog until a signed remote feed is implemented."""

    BINDINGS: ClassVar = [
        Binding("escape", "close", "Close", show=False),
        Binding("x", "close", "Close", show=False),
        Binding("ctrl+b", "close", "Close", show=False),
        Binding("b", "close", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="update-modal"):
            with Horizontal(id="update-modal-head"):
                yield Static(tr("update.title"), id="update-modal-title")
                yield _CloseMark("✕", id="update-modal-close")
            yield Static(
                f"[$muted]{tr('update.current')}[/] [$accent]v{__version__}[/]",
                id="update-current",
            )
            yield Static(tr("update.remote_reserved"), id="update-status")
            yield Static(tr("update.no_remote_check"), id="update-result")
            yield Static(tr("update.release_notes"), id="update-notes-title")
            with VerticalScroll(id="update-notes"):
                for note in RELEASE_NOTES:
                    yield Static(f"[$accent]•[/] {tr(note)}", classes="update-note")
            yield Static(tr("update.close_hint"), id="update-modal-hint")

    def action_close(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


__all__ = ["RELEASE_NOTES", "UpdateScreen"]
