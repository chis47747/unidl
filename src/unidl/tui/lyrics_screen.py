"""Modal viewer for static, line-timed, and word-timed lyrics."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..core.i18n import tr
from ..core.lyrics import Lyrics, timestamp
from .bidi import visual_markup
from .chrome import CloseMark


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
        timing = tr(
            f"lyrics.timing.{self.lyrics.timing}",
            default=str(self.lyrics.timing),
        )
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
                yield CloseMark(id="lyrics-modal-close")
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
            with Horizontal(id="lyrics-modal-nav"):
                yield Button(f"‹ {tr('modal.previous')}", id="lyrics-previous")
                yield Button(f"{tr('modal.next')} ›", id="lyrics-next")
            yield Static(
                f"[$muted]{tr('lyrics.close_hint')}[/]",
                id="lyrics-modal-hint",
            )

    def on_mount(self) -> None:
        listing = self.query_one("#lyrics-list", VerticalScroll)
        listing.focus()
        self.watch(listing, "scroll_y", self._sync_nav, init=False)
        self.watch(listing, "virtual_size", self._sync_nav, init=False)
        self.call_after_refresh(self._sync_nav)

    def on_resize(self) -> None:
        self.call_after_refresh(self._sync_nav)

    def relocalize(self) -> None:
        timing = tr(
            f"lyrics.timing.{self.lyrics.timing}",
            default=str(self.lyrics.timing),
        )
        summary = tr(
            "lyrics.summary",
            count=len(self.lyrics.lines),
            timing=timing,
            language=self.lyrics.language or tr("lyrics.language_unknown"),
        )
        self.query_one("#lyrics-modal-title", Static).update(
            f"{tr('lyrics.title')}  [$dim]{visual_markup(self.save_name)}[/]"
        )
        self.query_one("#lyrics-summary", Static).update(f"[$muted]{visual_markup(summary)}[/]")
        self.query_one("#lyrics-previous", Button).label = f"‹ {tr('modal.previous')}"
        self.query_one("#lyrics-next", Button).label = f"{tr('modal.next')} ›"
        self.query_one("#lyrics-modal-hint", Static).update(f"[$muted]{tr('lyrics.close_hint')}[/]")

    def _sync_nav(self) -> None:
        if not self.is_attached:
            return
        listing = self.query_one("#lyrics-list", VerticalScroll)
        was_focused = self.focused
        prev_btn = self.query_one("#lyrics-previous", Button)
        next_btn = self.query_one("#lyrics-next", Button)
        prev_btn.disabled = listing.scroll_y <= 0
        next_btn.disabled = (
            listing.max_scroll_y <= 0 or listing.scroll_y >= listing.max_scroll_y
        )
        if was_focused is not None and getattr(was_focused, "disabled", False):
            for candidate in (next_btn, prev_btn, listing):
                if not getattr(candidate, "disabled", False):
                    candidate.focus()
                    break

    def action_scroll(self, delta: int) -> None:
        self.query_one("#lyrics-list", VerticalScroll).scroll_relative(y=delta, animate=False)

    def action_edge(self, edge: str) -> None:
        listing = self.query_one("#lyrics-list", VerticalScroll)
        if edge == "top":
            listing.scroll_home(animate=False)
        else:
            listing.scroll_end(animate=False)

    def action_previous(self) -> None:
        self.action_scroll(-12)

    def action_next(self) -> None:
        self.action_scroll(12)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "lyrics-previous":
            self.action_previous()
        elif event.button.id == "lyrics-next":
            self.action_next()

    def action_close(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.dismiss(None)
        return True


__all__ = ["LyricsScreen"]
