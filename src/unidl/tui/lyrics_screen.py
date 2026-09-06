"""Modal viewer for static, line-timed, and word-timed lyrics."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ..core.i18n import tr
from ..core.lyrics import Lyrics, timestamp
from .bidi import visual_markup


class _CloseMark(Static):
    def on_click(self, event) -> None:
        event.stop()
        closer = getattr(self.screen, "action_close", None)
        if callable(closer):
            closer()


class LyricsScreen(ModalScreen[None]):
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

    def __init__(self, save_name: str, lyrics: Lyrics) -> None:
        super().__init__()
        self.save_name = str(save_name)
        self.lyrics = lyrics

    def compose(self) -> ComposeResult:
        timing = tr(f"lyrics.timing.{self.lyrics.timing}")
        summary = tr(
            "lyrics.summary",
            count=len(self.lyrics.lines),
            timing=timing,
            language=self.lyrics.language or tr("lyrics.language_unknown"),
        )
        with Vertical(id="lyrics-modal"):
            with Horizontal(id="lyrics-modal-head"):
                yield Static(
                    f"{tr('lyrics.title')}  [$dim]{visual_markup(self.save_name)}[/]",
                    id="lyrics-modal-title",
                )
                yield _CloseMark("x", id="lyrics-modal-close")
            yield Static(f"[$muted]{visual_markup(summary)}[/]", id="lyrics-summary")
            with VerticalScroll(id="lyrics-list"):
                for index, line in enumerate(self.lyrics.lines, start=1):
                    timing_text = timestamp(line.start_ms)
                    if line.end_ms is not None:
                        timing_text += f" - {timestamp(line.end_ms)}"
                    section = f"  [$accent]{visual_markup(line.section)}[/]" if line.section else ""
                    yield Static(
                        f"[$gutter]{index:>3}[/]  [$manifest]{timing_text}[/]{section}\n"
                        f"     {visual_markup(line.text)}",
                        classes=f"lyrics-row {'odd' if index % 2 else 'even'}",
                    )
            yield Static(
                f"[$muted]{tr('lyrics.close_hint')}[/]",
                id="lyrics-modal-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#lyrics-list", VerticalScroll).focus()

    def action_scroll(self, delta: int) -> None:
        self.query_one("#lyrics-list", VerticalScroll).scroll_relative(y=delta, animate=False)

    def action_edge(self, edge: str) -> None:
        listing = self.query_one("#lyrics-list", VerticalScroll)
        if edge == "top":
            listing.scroll_home(animate=False)
        else:
            listing.scroll_end(animate=False)

    def action_close(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


__all__ = ["LyricsScreen"]
