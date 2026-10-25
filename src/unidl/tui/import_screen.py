"""Finish what an export file already resolved.

Reached by clicking ``import`` on the main screen. Every file in the exports
folder is listed with what is in it - which service, which titles, how many keys -
and Enter on one runs the ordinary delivery path against it: the manifest is read
again, the tracks are chosen by your own settings, the keys come out of the file.
The source service is matched when installed so its custom download preparation
and lifecycle actions still run. It is not mandatory: without a match, the TUI
uses an inert generic context for the shared delivery screen. That fallback needs
no source-service installation, sign-in, helper, CDM or licence request.

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

from dataclasses import replace
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import OptionList, SelectionList, Static
from textual.widgets.option_list import Option

from ..core import exports
from ..core.i18n import tr
from .askhost import join_fitted
from .asks import _ConfirmingSelectionList
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


class ExportEntryScreen(Screen[None]):
    """Choose which titles in one export file should enter delivery.

    ``SelectionList`` gives the export flow the same keyboard contract as every
    other multi-pick in UniDL: Space toggles one row, ``a`` selects all, ``n``
    clears the list, and Enter confirms. With no boxes ticked, Enter means the
    highlighted row, which keeps the common one-title action a single keypress.
    """

    DEFAULT_CSS = """
    #import-entry-head { height: 2; padding: 1 2 0 2; }
    #import-entry-note { height: 1; padding: 0 2; }
    #import-entry-list { height: 1fr; padding: 0 1; }
    """

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "confirm", "Open selected", show=True),
    ]

    def __init__(self, document: exports.Document, *, source_name: str = "") -> None:
        super().__init__()
        self.document = document
        self.source_name = source_name

    def compose(self) -> ComposeResult:
        yield Chrome(can_go_back=True, show_settings=False)
        yield Static("", id="import-entry-head")
        yield Static("", id="import-entry-note")
        yield _ConfirmingSelectionList(
            *[
                (
                    self._entry_markup(index, entry),
                    index,
                    False,
                )
                for index, entry in enumerate(self.document.entries)
            ],
            id="import-entry-list",
        )
        yield KeyBar(
            ("space", "select / clear"),
            ("a", "select all"),
            ("n", "clear all"),
            ("enter", "open selected"),
            ("^b", "back to exports"),
            ("esc", "quit"),
        )

    @staticmethod
    def _entry_markup(index: int, entry: exports.Entry) -> str:
        detail: list[str] = []
        if entry.summary:
            detail.append(entry.summary)
        if entry.keys:
            detail.append(tr("import.keys_n", count=len(entry.keys)))
        if entry.chapters:
            detail.append(tr(
                "chapters.count.one" if len(entry.chapters) == 1 else "chapters.count.other",
                count=len(entry.chapters),
            ))
        suffix = f"  [$dim]{_esc('  ·  '.join(detail))}[/]" if detail else ""
        return f"  [$accent]{index + 1}.[/]  {_esc(_fit(entry.label()))}{suffix}"

    def on_mount(self) -> None:
        option_list = self.query_one("#import-entry-list", SelectionList)
        if option_list.option_count:
            option_list.highlighted = 0
            option_list.focus()
        self._render_head()

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self._render_head()

    def on_resize(self) -> None:
        # ``join_fitted`` depends on the available width. Rebuild both status
        # lines after a terminal resize just like AskHost re-fits its identity and
        # parameter rows, rather than leaving a line measured for the old width.
        if self.is_mounted:
            self._render_head()

    def _list(self) -> _ConfirmingSelectionList:
        return self.query_one("#import-entry-list", _ConfirmingSelectionList)

    def _render_head(self) -> None:
        # Headings in the service/session screens are fitted as whole segments;
        # doing the same here prevents a translated service name or a narrow
        # terminal from painting past the right edge. The title and count are the
        # useful part, so the separator is dropped first if necessary.
        head = join_fitted(
            [
                (
                    f"[$accent]{_esc(tr('import.head'))}  ·  {_esc(tr('keys.choose'))}[/]",
                    f"{tr('import.head')}  ·  {tr('keys.choose')}",
                ),
                (
                    f"[$foreground]{_esc(tr('delivery.titles_n', count=len(self.document.entries)))}[/]",
                    tr("delivery.titles_n", count=len(self.document.entries)),
                ),
            ],
            self.size.width,
            "  [$dim]·[/]  ",
        )
        self.query_one("#import-entry-head", Static).update(head)
        selected = len(self._list().selected) if self.is_mounted else 0
        source = self.source_name or self.document.service_name or self.document.service
        note = join_fitted(
            [
                (f"[$muted]{_esc(source)}[/]", source),
                (
                    f"[$muted]{_esc(tr('ask.multi.selected', picked=selected, total=len(self.document.entries)))}[/]",
                    tr("ask.multi.selected", picked=selected, total=len(self.document.entries)),
                ),
                (
                    f"[$muted]{_esc(tr('ask.multi.keys', enter=tr('ask.multi.enter_confirm')))}[/]",
                    tr("ask.multi.keys", enter=tr("ask.multi.enter_confirm")),
                ),
            ],
            self.size.width,
            "  [$dim]·[/]  ",
        )
        self.query_one("#import-entry-note", Static).update(note)

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        event.stop()
        self._render_head()

    def on__confirming_selection_list_confirmed(
        self, event: _ConfirmingSelectionList.Confirmed
    ) -> None:
        event.stop()
        self.action_confirm()

    def action_confirm(self) -> None:
        entries = self._list()
        selected = [int(value) for value in entries.selected]
        if not selected:
            highlighted = entries.highlighted
            if highlighted is None:
                self.notify("Select at least one title", severity="warning", timeout=3)
                return
            selected = [int(entries.get_option_at_index(highlighted).value)]
        chosen = [self.document.entries[index] for index in selected]
        if not self.app.open_import(replace(self.document, entries=chosen)):
            return


__all__ = ["ExportEntryScreen", "ImportScreen"]
