"""Finish what an export file already resolved.

Reached by clicking ``import`` on the main screen. Every file in the exports
folder is listed with what is in it - which service, which titles, how many keys -
and Enter on one runs the ordinary delivery path against it: the manifest is read
again, the tracks are chosen by your own settings, the keys come out of the file.
No sign-in, no CDM, no licence request.

That is also why this is a list of a folder rather than a box to type a path into.
A file arrives here the way any file arrives anywhere - it is put in the folder -
and ``^o`` opens that folder so the drag is one step.

A file that landed somewhere else does not have to be moved first: the box on the
main screen, the one that already takes a platform name or a video URL, takes the
path to an export too. That is the same act as pasting a link, and it is the route
that still works when the status line is too narrow for this screen's chip.

A file that cannot be read is listed anyway, with the reason. A folder that
silently ignores what you just put in it is a folder you stop trusting.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ..core import exports
from ..core.i18n import tr
from .bidi import visual_text
from .chrome import Chrome, KeyBar, refresh_locale_widgets

#: How much of a title's name a row shows before it is cut. The rest of the row -
#: the service, the age, the key count - is what tells two similar exports apart.
NAME_LIMIT = 46


def _esc(text: object) -> str:
    """Neutralise square brackets: release names are full of them."""
    return str(text).replace("\\", "\\\\").replace("[", r"\[")


def _fit(text: str, limit: int = NAME_LIMIT) -> str:
    shown = visual_text(text)
    return shown if len(shown) <= limit else f"{shown[: limit - 1]}…"


def _age(path: Path) -> str:
    """How long ago the file was written, in the roughest useful unit.

    An export's value decays: the manifest link in it is usually signed and expires
    in hours. "3 days ago" is the difference between "try it" and "re-resolve it",
    and an ISO timestamp makes the reader do that subtraction themselves.
    """
    try:
        when = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return ""
    seconds = max(0, int((datetime.now() - when).total_seconds()))
    if seconds < 90:
        return tr("import.just_now")
    if seconds < 5400:
        return tr("import.minutes", count=seconds // 60)
    if seconds < 172800:
        return tr("import.hours", count=seconds // 3600)
    return tr("import.days", count=seconds // 86400)


class ImportScreen(Screen[None]):
    """The exports folder, and one keypress to finish any of them."""

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "noop", "Import this one", show=True),
        Binding("ctrl+r", "reload", "Look again", show=True),
        Binding("ctrl+o", "reveal", "Open the folder", show=True),
    ]

    def action_noop(self) -> None:
        """Key-bar hint only; Enter is handled by the focused list."""

    def __init__(self) -> None:
        super().__init__()
        #: one entry per row: the file, what it holds, and why it cannot be read
        self._rows: list[tuple[Path, exports.Document | None, str]] = []

    def compose(self) -> ComposeResult:
        yield Chrome(can_go_back=True, show_settings=False)
        yield Static("", id="import-head")
        yield Static("", id="import-note")
        yield OptionList(id="import-list")
        yield KeyBar(
            ("enter", "import this one"),
            ("↑↓", "move"),
            ("^r", "look again"),
            ("^o", "open the folder"),
            ("^b", "back"),
            ("esc", "quit"),
        )

    def on_mount(self) -> None:
        self.rebuild()
        self.query_one("#import-list", OptionList).focus()

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self.rebuild()

    @property
    def folder(self) -> Path:
        return Path(self.app.config.paths.exports)

    # -------------------------------------------------------------------- render
    def action_reload(self) -> None:
        self.rebuild()
        self.notify(tr("import.looked"), timeout=2)

    def rebuild(self) -> None:
        option_list = self.query_one("#import-list", OptionList)
        previous = option_list.highlighted
        option_list.clear_options()
        palette = self.app.palette
        self._rows = exports.scan(self.folder)

        head = Text()
        head.append(tr("import.head"), style=f"bold {palette.accent}")
        head.append("  ·  ", style=palette.dim)
        readable = [row for row in self._rows if row[1] is not None]
        if self._rows:
            keys = sum(row[1].keys for row in readable)
            head.append(
                tr("import.summary", files=len(readable), keys=keys),
                style=palette.fg,
            )
        else:
            head.append(tr("import.empty"), style=palette.muted)
        self.query_one("#import-head", Static).update(head)

        note = Text()
        note.append(f"{self.folder}", style=palette.dim)
        note.append("  ·  ", style=palette.gutter)
        note.append(
            tr("import.put") if not self._rows else tr("import.open_hint"),
            style=palette.muted,
        )
        self.query_one("#import-note", Static).update(note)

        for path, document, problem in self._rows:
            option_list.add_option(Option(self._row_markup(path, document, problem)))
        if not self._rows:
            option_list.add_option(
                Option(
                    f"    [$dim]{tr('import.how')}[/]",
                    disabled=True,
                )
            )

        if previous is not None and self._rows:
            option_list.highlighted = min(previous, len(self._rows) - 1)
        elif self._rows:
            option_list.highlighted = 0

    def _row_markup(
        self, path: Path, document: exports.Document | None, problem: str
    ) -> str:
        if document is None:
            return f"    [$warn]{_esc(_fit(path.name))}[/]  [$dim]{_esc(problem)}[/]"
        first = document.entries[0].label() if document.entries else path.stem
        more = (
            f"  [$dim]{tr('import.more', count=len(document.entries) - 1)}[/]"
            if len(document.entries) > 1
            else ""
        )
        facts = [
            document.service_name or document.service,
            tr("import.keys_n", count=document.keys),
            _age(path),
        ]
        return (
            f"    [$accent]{_esc(_fit(first))}[/]{more}"
            f"  [$dim]{_esc('  ·  '.join(fact for fact in facts if fact))}[/]"
        )

    def _selected(self) -> tuple[Path, exports.Document | None, str] | None:
        option_list = self.query_one("#import-list", OptionList)
        index = option_list.highlighted
        if index is None or index >= len(self._rows):
            return None
        return self._rows[index]

    # --------------------------------------------------------------------- doing
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        chosen = self._selected()
        if chosen is None:
            return
        path, document, problem = chosen
        if document is None:
            self.notify(
                f"{path.name}: {problem}",
                title=tr("import.cannot"),
                severity="error",
                timeout=8,
            )
            return
        # This screen stays underneath, like the platform list does under a
        # service: it is where this download came from, so it is where Back should
        # land and where the next one is picked. An import that cannot start says
        # so with the list still in front of you rather than dropping you home.
        self.app.open_import(document)

    def action_reveal(self) -> None:
        """Open the exports folder through the app's desktop boundary."""
        folder = self.folder
        folder.mkdir(parents=True, exist_ok=True)
        self.app.open_path(folder)


__all__ = ["ImportScreen"]
