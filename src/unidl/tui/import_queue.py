"""An explicit, in-memory queue of titles from independent export documents."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from ..core import exports
from ..core.diagnostics import activate
from ..core.flow import FlowContext
from ..core.i18n import tr
from ..core.terminal_cells import cell_width, crop
from .bidi import visual_text
from .chrome import Chrome, KeyBar, refresh_locale_widgets
from .job_details import JobDetails
from .session import QUEUED, RUNNING, Job, SessionController, _Sink


@dataclass(frozen=True)
class ImportQueueItem:
    source: Path
    document: exports.Document
    index: int

    @property
    def entry(self) -> exports.Entry:
        return self.document.entries[self.index]

    @property
    def identity(self) -> tuple[Path, int]:
        return self.source.resolve(), self.index

    @property
    def label(self) -> str:
        service = self.document.service_name or self.document.service
        return f"{service} · {self.entry.label()}"


class ImportQueueScreen(Screen[None]):
    """Pick from exports on the left; review and reorder on the right."""

    DEFAULT_CSS = """
    #import-queue-head { height: 2; padding: 1 2 0 2; }
    #import-queue-heading { width: 1fr; height: 1; text-style: bold; }
    #import-queue-count { width: auto; max-width: 45%; height: 1; color: $muted; }
    #import-queue-note { height: 2; padding: 0 2 1 2; color: $muted; }
    #import-queue-body { height: 1fr; min-height: 3; margin: 0 2 1 2; }
    #import-queue-body > Vertical { width: 1fr; min-width: 0; }
    #queue-order-pane { border-left: solid $rule; padding-left: 2; margin-left: 1; }
    #import-queue-body .queue-label {
        height: 2; padding: 0 1 1 1; color: $fg2; text-style: bold;
    }
    #import-queue-files, #import-queue-items {
        height: 1fr; padding: 0; background: transparent; overflow-x: hidden;
    }
    #import-queue-body OptionList > .option-list--option { padding: 0 1 1 1; }
    ImportQueueScreen.compact #import-queue-body OptionList > .option-list--option { padding-bottom: 0; }
    #queue-order-body { height: 1fr; min-height: 0; }
    #import-queue-empty {
        height: 1fr; padding: 1 2; content-align: center middle;
        text-align: center; color: $muted;
    }
    #import-queue-actions {
        height: 2; margin: 0 2; border-top: solid $rule;
    }
    #queue-action-gap { width: 1fr; height: 1; }
    #import-queue-actions Button {
        height: 1; min-width: 3; width: auto; border: none;
        padding: 0 1; margin: 0 1 0 0; background: transparent;
        color: $muted; text-style: none; text-opacity: 1;
    }
    #import-queue-actions Button:hover, #import-queue-actions Button:focus {
        border: none; background: $highlight; color: $foreground; text-style: bold;
    }
    #import-queue-actions Button.-active { border: none; background: $highlight; }
    #import-queue-actions #queue-start {
        margin-right: 0; color: $accent; text-style: bold;
    }
    #import-queue-actions Button:disabled {
        border: none; background: transparent; color: $dim;
        text-opacity: 0.6; text-style: none;
    }
    ImportQueueScreen.compact #import-queue-head { height: 1; padding-top: 0; }
    ImportQueueScreen.compact #import-queue-note { height: 1; padding-bottom: 0; }
    """

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", show=False),
        Binding("escape", "app.global_quit", show=False),
        Binding("ctrl+r", "reload", show=False),
        Binding("ctrl+o", "reveal", show=False),
        Binding("ctrl+enter", "start", show=False),
        Binding("delete", "remove", show=False),
        Binding("ctrl+up", "move_up", show=False),
        Binding("ctrl+down", "move_down", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.items: list[ImportQueueItem] = []
        self._rows: list[tuple[Path, exports.Document | None, str]] = []
        self._drawing = False

    def compose(self) -> ComposeResult:
        yield Chrome(can_go_back=True, show_settings=False)
        with Horizontal(id="import-queue-head"):
            yield Static("", id="import-queue-heading")
            yield Static("", id="import-queue-count")
        yield Static("", id="import-queue-note")
        with Horizontal(id="import-queue-body"):
            with Vertical(id="queue-files-pane"):
                yield Static(tr("import.queue.files"), classes="queue-label", id="queue-files-label")
                yield OptionList(id="import-queue-files")
            with Vertical(id="queue-order-pane"):
                yield Static(tr("import.queue.order"), classes="queue-label", id="queue-order-label")
                with Vertical(id="queue-order-body"):
                    yield OptionList(id="import-queue-items")
                    yield Static("", id="import-queue-empty", markup=False)
        with Horizontal(id="import-queue-actions"):
            yield Button(tr("import.queue.up"), id="queue-up")
            yield Button(tr("import.queue.down"), id="queue-down")
            yield Button(tr("import.queue.remove"), id="queue-remove")
            yield Button(tr("import.queue.clear"), id="queue-clear")
            yield Static("", id="queue-action-gap")
            yield Button("", id="queue-start")
        yield KeyBar(
            ("enter", tr("import.queue.choose")), ("^enter", tr("import.queue.start")),
            ("^↑↓", tr("import.queue.reorder")), ("^r", "look again"),
            ("^o", "open the folder"), ("^b", "back"), ("esc", "quit"),
        )

    def on_mount(self) -> None:
        self.set_class(self.size.height < 28, "compact")
        self.action_reload()
        self._render_queue()
        self.query_one("#import-queue-files", OptionList).focus()
        signal = getattr(self.app, "theme_changed_signal", None)
        if signal is not None:
            signal.subscribe(self, self._refresh_rows)
        self.call_after_refresh(self._refresh_rows)

    def on_resize(self) -> None:
        self.set_class(self.size.height < 28, "compact")
        if self.is_mounted:
            self.call_after_refresh(self._refresh_rows)

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        for selector, key in (
            ("#queue-files-label", "files"), ("#queue-order-label", "order"),
        ):
            self.query_one(selector, Static).update(tr(f"import.queue.{key}"))
        self.action_reload()
        self._render_queue()
        self.query_one(KeyBar).render_pairs(
            ("enter", tr("import.queue.choose")), ("^enter", tr("import.queue.start")),
            ("^↑↓", tr("import.queue.reorder")), ("^r", "look again"),
            ("^o", "open the folder"), ("^b", "back"), ("esc", "quit"),
        )

    def action_reload(self) -> None:
        listing = self.query_one("#import-queue-files", OptionList)
        previous = listing.highlighted
        listing.clear_options()
        self._rows = exports.scan(Path(self.app.config.paths.exports))
        for path, document, problem in self._rows:
            listing.add_option(Option(self._file_prompt(path, document, problem)))
        if not self._rows:
            listing.add_option(Option(tr("import.put"), disabled=True))
        elif previous is not None:
            listing.highlighted = min(previous, len(self._rows) - 1)
        else:
            listing.highlighted = 0
        self._render_head()

    @staticmethod
    def _fit_label(value: object, width: int) -> str:
        shown = visual_text(" ".join(str(value).split()))
        if cell_width(shown) <= width:
            return shown
        return crop(shown, max(0, width - 1)) + "…"

    def _row_width(self, selector: str) -> int:
        listing = self.query_one(selector, OptionList)
        # Reserve both option paddings and a potential scrollbar. Fit each
        # explicit line by terminal cells, never Python character count.
        return max(4, listing.size.width - 3)

    def _file_prompt(self, path: Path, document: exports.Document | None, problem: str) -> Text:
        from .import_screen import _age

        width = self._row_width("#import-queue-files")
        palette = self.app.palette
        title = document.entries[0].label() if document is not None and document.entries else path.stem
        row = Text(self._fit_label(title, width), style=palette.accent if document is not None else palette.warn)
        facts = (
            [document.service_name or document.service,
             tr("delivery.titles_n", count=len(document.entries)), _age(path)]
            if document is not None else [problem]
        )
        row.append("\n" + self._fit_label(" · ".join(fact for fact in facts if fact), width), style=palette.muted)
        return row

    def _item_prompt(self, index: int, item: ImportQueueItem) -> Text:
        width = self._row_width("#import-queue-items")
        palette = self.app.palette
        number = f"{index + 1:02d}  "
        row = Text(number, style=palette.dim)
        row.append(self._fit_label(item.entry.label(), width - len(number)), style=palette.fg)
        source = f"{item.document.service_name or item.document.service} · {item.source.name}"
        row.append("\n    " + self._fit_label(source, max(1, width - 4)), style=palette.muted)
        return row

    def _refresh_rows(self, _theme=None) -> None:
        if not self.is_mounted:
            return
        listing = self.query_one("#import-queue-files", OptionList)
        for index, (path, document, problem) in enumerate(self._rows):
            listing.replace_option_prompt_at_index(index, self._file_prompt(path, document, problem))
        listing = self.query_one("#import-queue-items", OptionList)
        for index, item in enumerate(self.items):
            listing.replace_option_prompt_at_index(index, self._item_prompt(index, item))
        self._render_head()
        self._sync_actions()

    def _render_head(self) -> None:
        self.query_one("#queue-files-label", Static).update(
            Text.assemble(tr("import.queue.files"), (f" · {len(self._rows)}", self.app.palette.dim))
        )
        self.query_one("#import-queue-heading", Static).update(tr("import.queue.title"))
        self.query_one("#import-queue-count", Static).update(tr("import.queue.count", count=len(self.items)))
        note = self.query_one("#import-queue-note", Static)
        note.update(self._fit_label(tr("import.queue.brief"), max(1, note.content_size.width)))
        note.tooltip = tr("import.queue.help")
        empty = Text(tr("import.queue.empty_title"), style=f"bold {self.app.palette.fg2}")
        empty.append("\n\n" + tr("import.queue.empty_help"), style=self.app.palette.muted)
        self.query_one("#import-queue-empty", Static).update(empty)

    def _sync_actions(self) -> None:
        index = self._queue_index()
        for identifier, key, enabled in (
            ("queue-up", "up", index is not None and index > 0),
            ("queue-down", "down", index is not None and index + 1 < len(self.items)),
            ("queue-remove", "remove", index is not None),
            ("queue-clear", "clear", bool(self.items)),
            ("queue-start", "start", bool(self.items)),
        ):
            button = self.query_one(f"#{identifier}", Button)
            full = tr(f"import.queue.{key}")
            # Narrow windows keep all click targets: arrows carry full labels
            # in their tooltips instead of pushing Start off the screen.
            label = ("↑" if key == "up" else "↓") if self.size.width < 110 and key in {"up", "down"} else full
            button.label = f"▶ {label}" if key == "start" else label
            button.tooltip = full
            button.disabled = not enabled

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "import-queue-items":
            event.stop()
            if not self._drawing:
                self._sync_actions()

    def action_reveal(self) -> None:
        folder = Path(self.app.config.paths.exports)
        folder.mkdir(parents=True, exist_ok=True)
        self.app.open_path(folder)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_list.id != "import-queue-files":
            return
        index = event.option_index
        if not 0 <= index < len(self._rows):
            return
        path, document, problem = self._rows[index]
        if document is None:
            self.notify(f"{path.name}: {problem}", title=tr("import.cannot"), severity="error")
            return
        from .import_screen import ExportEntryScreen

        def chosen(indexes: list[int] | None) -> None:
            if indexes is None:
                return
            identities = {item.identity for item in self.items}
            for entry_index in sorted(indexes):
                item = ImportQueueItem(path, document, entry_index)
                if item.identity not in identities:
                    self.items.append(item)
                    identities.add(item.identity)
            self._render_queue()

        self.app.push_screen(
            ExportEntryScreen(document, source_name=path.name, queue_selection=True), chosen
        )

    def _render_queue(self) -> None:
        listing = self.query_one("#import-queue-items", OptionList)
        previous = listing.highlighted
        self._drawing = True
        try:
            listing.clear_options()
            for index, item in enumerate(self.items):
                listing.add_option(Option(self._item_prompt(index, item)))
            if self.items:
                listing.highlighted = min(previous or 0, len(self.items) - 1)
        finally:
            self._drawing = False
        listing.display = bool(self.items)
        self.query_one("#import-queue-empty", Static).display = not self.items
        self._render_head()
        self._sync_actions()
        # A previously empty queue's list was hidden and has no measured width
        # until the next layout. Refit after it becomes visible.
        self.call_after_refresh(self._refresh_rows)

    def _queue_index(self) -> int | None:
        index = self.query_one("#import-queue-items", OptionList).highlighted
        return index if index is not None and 0 <= index < len(self.items) else None

    def action_remove(self) -> None:
        if (index := self._queue_index()) is not None:
            self.items.pop(index)
            self._render_queue()

    def action_clear(self) -> None:
        self.items.clear()
        self._render_queue()

    def _move(self, delta: int) -> None:
        index = self._queue_index()
        if index is not None and 0 <= index + delta < len(self.items):
            other = index + delta
            self.items[index], self.items[other] = self.items[other], self.items[index]
            self._render_queue()
            self.query_one("#import-queue-items", OptionList).highlighted = other
            self._sync_actions()

    def action_move_up(self) -> None:
        self._move(-1)

    def action_move_down(self) -> None:
        self._move(1)

    def action_start(self) -> None:
        if not self.items:
            self.notify(tr("import.queue.empty"), severity="warning")
            return
        self.app.open_import_queue(tuple(self.items))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "queue-start": self.action_start, "queue-up": self.action_move_up,
            "queue-down": self.action_move_down, "queue-remove": self.action_remove,
            "queue-clear": self.action_clear,
        }
        if action := actions.get(event.button.id):
            event.stop()
            action()


def build_import_service(app, document: exports.Document):
    """Same trust boundary as ordinary import, evaluated for each source."""
    from .app import _portable_import_service_class

    installed = app.registry.for_id(document.service) if document.is_native else None
    service_cls = installed or _portable_import_service_class(document)
    service = app.registry.build(service_cls, app.config, app.settings_store, globals_scope=app.globals)
    if installed is not None and not service.ctx.helpers.ready:
        raise RuntimeError(service.ctx.helpers.blocking_message())
    return service


class ImportQueueController(SessionController):
    """One visible serial batch, but independent source service contexts."""

    def __init__(self, app, service, engine, items: tuple[ImportQueueItem, ...]):
        super().__init__(app, service, engine)
        self.import_session = True
        self.mixed_import_session = True
        self.items = items
        self._neutral_service = service
        self._source_services: dict[tuple[Path, str, str], object] = {}
        self._playback_slots: dict[int, int] = {}
        self._job_slots: dict[int, int] = {}
        self._active_slot: int | None = None
        self._context_error: Exception | None = None

    def _drive(self) -> None:
        # A single session-wide recorder would apply the first source's privacy
        # policy to unrelated sources. Engine.run retains per-title debug logs
        # under each active service's own policy instead.
        with activate(None):
            self._drive_impl()

    def _seed_import_jobs(self, document: exports.Document) -> None:
        del document
        self.pending_jobs = [
            Job(item.label, state=QUEUED, attempts=0,
                details=JobDetails.from_entry(item.entry), identity=("mixed-import", index))
            for index, item in enumerate(self.items)
        ]
        self._queue_rows = list(self.pending_jobs)

    def _replay(self, ctx, document):
        del document
        ctx.log(tr("import.queue.running", count=len(self.items)))
        for slot, item in enumerate(self.items):
            if self.aborted or self.leaving or self.batch_cancelled:
                return
            # Download preparation, naming and probing must not mutate the
            # export object retained by the builder or another queued title.
            playback = copy.deepcopy(item.entry).playback()
            self._playback_slots[id(playback)] = slot
            yield ctx.emit(playback)

    def _switch_source(self, slot: int) -> None:
        item = self.items[slot]
        self._active_slot = slot
        self._context_error = None
        try:
            key = (item.source.resolve(), item.document.service, item.document.source_format)
            service = self._source_services.get(key)
            if service is None:
                service = build_import_service(self.app, item.document)
                self._source_services[key] = service
        except Exception as exc:
            service = self._neutral_service
            self._context_error = exc
        self.service = service
        self.settings = service.settings
        self._on_ui(self._refresh_source_hosts)

    def _refresh_source_hosts(self) -> None:
        for host in list(self.hosts):
            host.service, host.settings = self.service, self.settings
            host.refresh_after_settings()
            refresh_banner = getattr(host, "refresh_banner", None)
            if callable(refresh_banner):
                refresh_banner()

    def _source_context(self) -> FlowContext:
        return FlowContext(settings=self.settings, sink=_Sink(self), interactive=True)

    def process(self, playback, presenter, ctx):
        self._switch_source(self._playback_slots[id(playback)])
        return super().process(playback, presenter, self._source_context())

    def _start_job(self, playback):
        slot = self._playback_slots.pop(id(playback))
        job = self._queue_rows[slot]
        self.pending_jobs.remove(job)
        job.state, job.playback, job.attempts = RUNNING, playback, 1
        self.jobs.append(job)
        self._job_slots[id(job)] = slot
        self._snapshot_job(playback)
        self.render_queue()
        return job

    def _run_job(self, job, playback, presenter, ctx):
        # Retrying an earlier failure must not run under the last title's
        # service. Failed context builds are attempted again on an explicit retry.
        slot = self._job_slots[id(job)]
        if self._active_slot != slot or self._context_error is not None:
            self._switch_source(slot)
        playback.title.service = str(
            getattr(self.service, "_EXPORT_SOURCE_ID", self.service.ID) or self.service.ID
        )
        return super()._run_job(job, playback, presenter, self._source_context())

    def _process(self, playback, presenter, ctx):
        if self._context_error is not None:
            raise RuntimeError(str(self._context_error)) from self._context_error
        return super()._process(playback, presenter, ctx)

    def _release_playback(self, playback):
        # A failed source build never acquired that service's lifecycle state.
        if self._context_error is None:
            super()._release_playback(playback)
