"""Local release notes and public release information."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from .. import __version__
from ..core.i18n import tr
from ..core.update import UpdateInfo
from .chrome import CloseMark, StatusChip

RELEASE_NOTES = (
    "update.note.downloader",
    "update.note.service_views",
    "update.note.media_types",
    "update.note.documentation",
    "update.note.clipboard",
    "update.note.update_checker",
)


class UpdateScreen(ModalScreen[None]):
    """Show local notes and the best-effort public PyPI/GitHub check."""

    def __init__(self, update_info: UpdateInfo | None = None) -> None:
        super().__init__()
        self.update_info = update_info

    BINDINGS: ClassVar = [
        Binding("escape", "close", "Close", show=False),
        Binding("x", "close", "Close", show=False),
        Binding("ctrl+b", "close", "Close", show=False),
        Binding("b", "close", "Close", show=False),
        Binding("up", "scroll(-2)", "Up", show=False),
        Binding("down", "scroll(2)", "Down", show=False),
        Binding("pageup", "scroll(-10)", "Page up", show=False),
        Binding("pagedown", "scroll(10)", "Page down", show=False),
        Binding("home", "edge('top')", "Top", show=False),
        Binding("end", "edge('bottom')", "Bottom", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="update-modal"):
            with Horizontal(id="update-modal-head"):
                yield Static(tr("update.title"), id="update-modal-title")
                yield CloseMark(id="update-modal-close")
            yield Static(
                f"[$muted]{tr('update.current')}[/] [$accent]v{__version__}[/]",
                id="update-current",
            )
            yield Static(tr("update.checking"), id="update-status")
            yield Static(tr("update.checking_result"), id="update-result")
            with Horizontal(id="update-links"):
                yield StatusChip("open_pypi", id="update-pypi")
                yield StatusChip("open_github", id="update-github")
            with VerticalScroll(id="update-notes"):
                yield Static(tr("update.remote_notes"), id="update-remote-notes-title")
                yield Static("", id="update-remote-notes", markup=False)
                yield Static(tr("update.release_notes"), id="update-notes-title")
                for note in RELEASE_NOTES:
                    yield Static(f"[$accent]•[/] {tr(note)}", classes="update-note")
            yield Static(tr("update.close_hint"), id="update-modal-hint")

    def on_mount(self) -> None:
        self._render_remote()
        self.query_one("#update-notes", VerticalScroll).focus()

    def action_scroll(self, delta: int) -> None:
        self.query_one("#update-notes", VerticalScroll).scroll_relative(y=delta, animate=False)

    def action_edge(self, edge: str) -> None:
        notes = self.query_one("#update-notes", VerticalScroll)
        if edge == "top":
            notes.scroll_home(animate=False)
        else:
            notes.scroll_end(animate=False)

    def set_update_info(self, update_info: UpdateInfo | None) -> None:
        """Refresh a dialog that was opened before the background check ended."""
        self.update_info = update_info
        if self.is_mounted:
            self._render_remote()

    def _render_remote(self) -> None:
        status = self.query("#update-status")
        result = self.query("#update-result")
        links = self.query("#update-links")
        notes_title = self.query("#update-remote-notes-title")
        notes = self.query("#update-remote-notes")
        if not status or not result or not links or not notes_title or not notes:
            return
        status_widget = status.first(Static)
        result_widget = result.first(Static)
        links_widget = links.first(Horizontal)
        notes_title_widget = notes_title.first(Static)
        notes_widget = notes.first(Static)
        info = self.update_info
        if info is None:
            status_widget.update(tr("update.checking"))
            result_widget.update(tr("update.checking_result"))
            links_widget.display = False
            notes_title_widget.display = False
            notes_widget.display = False
            return
        if info.error:
            status_widget.update(tr("update.checked_partial" if info.checked else "update.check_failed"))
            if info.update_available:
                result_widget.update(tr("update.available", version=info.latest_version or ""))
            else:
                result_widget.update(
                    tr("update.partial_detail" if info.checked else "update.check_failed_detail")
                )
        elif info.update_available:
            status_widget.update(tr("update.checked"))
            result_widget.update(
                tr("update.available", version=info.latest_version or "")
            )
        else:
            status_widget.update(tr("update.checked"))
            result_widget.update(
                tr("update.current_ok", version=info.current_version)
            )
        pypi = self.query_one("#update-pypi", StatusChip)
        github = self.query_one("#update-github", StatusChip)
        pypi.update(tr("update.open_pypi"))
        github.update(tr("update.open_github"))
        pypi.display = bool(info.pypi_version)
        github.display = bool(info.github_url)
        links_widget.display = pypi.display or github.display
        has_notes = bool(info.release_notes)
        notes_title_widget.display = has_notes
        notes_widget.display = has_notes
        if has_notes:
            title = f"{info.release_title}\n" if info.release_title else ""
            notes_widget.update(Text(title + info.release_notes))

    def action_open_pypi(self) -> None:
        if self.update_info is not None:
            self.app.open_url(self.update_info.pypi_url)

    def action_open_github(self) -> None:
        if self.update_info is not None:
            self.app.open_url(self.update_info.github_url)

    def action_close(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


__all__ = ["RELEASE_NOTES", "UpdateScreen"]
