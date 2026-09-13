"""Modal inspection popup for one playback's chapter timeline."""

from __future__ import annotations

from collections.abc import Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ..core.chapters import Chapter, count_label, length, summary, timestamp
from ..core.i18n import tr
from .bidi import visual_markup
from .chrome import CloseMark


class ChaptersScreen(ModalScreen[None]):
    """Show every chapter in a popup that leaves download/record running."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("x", "close", "Close", show=False),
        Binding("ctrl+b", "close", "Close", show=False),
        Binding("b", "close", "Close", show=False),
        Binding("up", "scroll(-2)", "Up", show=False),
        Binding("down", "scroll(2)", "Down", show=False),
        Binding("pageup", "scroll(-12)", "Page up", show=False),
        Binding("pagedown", "scroll(12)", "Page down", show=False),
        Binding("home", "edge('top')", "First", show=False),
        Binding("end", "edge('bottom')", "Last", show=False),
    ]

    def __init__(self, save_name: str, chapters: Sequence[Chapter]) -> None:
        super().__init__()
        self.save_name = str(save_name)
        self.chapters = tuple(chapters)

    def compose(self) -> ComposeResult:
        with Vertical(id="chapter-modal"):
            with Horizontal(id="chapter-modal-head"):
                yield Static(
                    f"{tr('chapters.title')}  [$dim]{visual_markup(self.save_name)}[/]",
                    id="chapter-modal-title",
                )
                yield CloseMark(id="chapter-modal-close")
            yield Static(
                f"[$muted]{visual_markup(summary(self.chapters))}  ·  "
                f"{tr('chapters.meta')}[/]",
                id="chapter-summary",
            )
            with VerticalScroll(id="chapter-list"):
                for index, chapter in enumerate(self.chapters, start=1):
                    span = timestamp(chapter.start_ms, precise=True)
                    if chapter.end_ms is not None:
                        span += f" – {timestamp(chapter.end_ms, precise=True)}"
                    duration = length(chapter)
                    duration_markup = (
                        f"  [$ok]{duration}[/]" if duration else ""
                    )
                    kind = (
                        f"  [$accent]{visual_markup(chapter.kind)}[/]"
                        if chapter.kind
                        else ""
                    )
                    yield Static(
                        f"[$gutter]{index:>3}[/]  [$manifest]{span}[/]"
                        f"{duration_markup}{kind}\n"
                        f"     {visual_markup(chapter.title)}",
                        classes=f"chapter-row {'odd' if index % 2 else 'even'}",
                    )
            yield Static(
                f"[$muted]{tr('chapters.close_hint', summary=count_label(self.chapters))}[/]",
                id="chapter-modal-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#chapter-list", VerticalScroll).focus()

    def action_scroll(self, delta: int) -> None:
        self.query_one("#chapter-list", VerticalScroll).scroll_relative(
            y=delta,
            animate=False,
        )

    def action_edge(self, edge: str) -> None:
        listing = self.query_one("#chapter-list", VerticalScroll)
        if edge == "top":
            listing.scroll_home(animate=False)
        else:
            listing.scroll_end(animate=False)

    def action_close(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


__all__ = ["ChaptersScreen"]
