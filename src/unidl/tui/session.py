"""Driving one service session across screens 2, 3 and 4.

A service flow is a blocking generator, so it runs on a Textual thread worker.
Asks are marshalled to the event loop, the worker blocks on an ``Event`` until
the user answers, and the answer is handed back. Service code stays completely
synchronous and framework-free.

What is new here is routing. The UI has four screens and each answers one
question, so an ask does not just get shown - it gets shown *somewhere*:

* ``scope="root"``     the service's own menu          -> screen 2
* ``scope="flow"``     titles, seasons, channels       -> screen 3
* ``scope="delivery"`` tracks, download or export      -> screen 4

Moving between them pushes and pops screens, which throws the old screen's state
away on purpose. Anything that has to outlive a screen - the log, the failure
flag, the worker itself - lives on the controller.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from unidl.downloader.utils import pretty_codec

from ..core import chapters as chapter_model
from ..core import exports, naming
from ..core.engine import Engine, TrackSet
from ..core.flow import (
    SCOPE_DELIVERY,
    SCOPE_FLOW,
    SCOPE_ROOT,
    Ask,
    Await,
    Back,
    Choice,
    FlowContext,
    Panel,
    PartnerHandoff,
    Pick,
    Quit,
    SettingsAsk,
    Suspend,
    TextAsk,
    run_flow,
)
from ..core.i18n import tr
from ..core.partner import (
    PartnerAuthorization,
    PartnerAuthorizationError,
    PartnerAuthorizationResult,
    deliver_partner_authorization,
)
from ..core.playback import LiveWindow, Playback, normalize_live_record_limit
from ..core.service import Service
from ..core.vault import normalize_hex, split_pair
from . import logline
from .askhost import LOG_LIMIT
from .asks import BACK
from .audio import AudioPreview
from .logline import GUTTER_WIDTH, LogLine

if TYPE_CHECKING:
    from textual.app import App

    from .askhost import AskHost
    from .download_screen import DownloadScreen
    from .flow_screen import FlowScreen
    from .service_screen import ServiceScreen

#: bound on how many screens we will pop looking for one of ours
_POP_GUARD = 12

#: how often a blocked flow wakes up to ask whether the session is still alive
_ANSWER_POLL = 0.25

#: a position in a replay window, as UniDL takes it. Minutes and seconds are
#: enough on their own - "30:00" is half an hour in, which is how people type it.
_POSITION = re.compile(r"^\d{1,3}(:[0-5]?\d){1,2}$")


class _LiveBackToTracks(Exception):
    """Return from the first live-delivery question to track selection."""


#: What a track row is marked with in the listing card. A glyph as well as the
#: order, so the two states are told apart where colour is not.
TAKEN, LEFT = "▸", "·"


def _track_highlights(stream: object) -> tuple[tuple[str, str], ...]:
    """Exceptional format badges to colour inside an otherwise neutral row."""
    highlights: list[tuple[str, str]] = []

    def add(term: str, role: str) -> None:
        item = (term, role)
        if item not in highlights:
            highlights.append(item)

    video_range = str(getattr(stream, "video_range", "") or "").strip()
    upper = video_range.upper().replace("_", " ")
    if "HDR VIVID" in upper or "HDRVIVID" in upper:
        add("HDR Vivid" if "HDR VIVID" in upper else "HDRVIVID", "hdr-vivid")
    if "DOLBY VISION" in upper:
        add("Dolby Vision", "dv")
    elif re.search(r"(?:^|[^A-Z])DV(?:$|[^A-Z])", upper):
        add("DV", "dv")
    if "HDR10+" in upper:
        add("HDR10+", "hdr10plus")
    elif "HDR10PLUS" in upper:
        add("HDR10PLUS", "hdr10plus")
    elif re.search(r"(?:^|[^A-Z0-9])HDR10P(?:$|[^A-Z0-9])", upper):
        add("HDR10P", "hdr10plus")
    elif "HDR10" in upper:
        add("HDR10", "hdr10")
    if "HLG" in upper:
        add("HLG", "hlg")

    extra = getattr(stream, "extra", None)
    extra = extra if isinstance(extra, dict) else {}
    format_line = getattr(stream, "format_line", None)
    line = str(format_line() if callable(format_line) else "")
    audio_text = " ".join(
        str(value or "")
        for value in (
            getattr(stream, "codecs", ""),
            getattr(stream, "name", ""),
            getattr(stream, "group_id", ""),
            line,
        )
    ).upper()
    if extra.get("audio_atmos") or "ATMOS" in audio_text:
        add("Atmos", "atmos")
    elif "ATOMS" in audio_text:
        add("ATOMS", "atmos")
    if (
        "AUDIO VIVID" in audio_text
        or "AV3A" in audio_text
        or extra.get("audio_vivid_policy")
        or extra.get("audio_vivid_detected")
    ):
        add("Audio Vivid", "audio-vivid")
    return tuple(highlights)


def tracks_panel(save_name: str, tracks: TrackSet) -> Panel:
    """Everything the manifest holds, and which of it would have been taken.

    Plain rows rather than numbered values: there is nothing here to lift out one
    at a time, and twenty-eight numbered tracks would offer twenty-eight keys that
    do the same nothing. ``1`` and ``^y`` still copy the whole card, which is the
    form this is useful in - a track list pasted into a note.
    """
    chosen = {id(stream) for stream in tracks.selected}
    rows: list[str | tuple[str, str]] = []
    for stream in tracks.streams:
        mark = TAKEN if id(stream) in chosen else LEFT
        rows.append(f"{mark}  {stream.format_line()}")
    return Panel(
        title=f"tracks  ·  {save_name}",
        lines=rows,
        hint=(
            f"{TAKEN} would be taken · {LEFT} would be left · nothing was downloaded "
            "and no licence was requested  ·  1 copies the list  ·  enter or ^b to go back"
        ),
    )


_AUDIO_TAG_ROWS = (
    ("title", "title"),
    ("artist", "artist"),
    ("album", "album"),
    ("album_artist", "album artist"),
    ("date", "date"),
    ("track", "track"),
    ("disc", "disc"),
    ("genre", "genre"),
    ("publisher", "publisher"),
    ("comment", "comment"),
)


def _metadata_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    if value is None or isinstance(value, (dict, set)):
        return ""
    return str(value).strip()


def _cover_reference(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("url") or value.get("path")
    source = _metadata_value(value)
    if not source:
        return ""
    parsed = urlsplit(source)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        # The sidecar keeps the complete URL; the screen omits query parameters
        # so signed cover URLs are not copied into the visible log by accident.
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if parsed.scheme == "data":
        return "embedded data"
    return source


def _audio_metadata_lines(playback: Playback) -> list[str]:
    """Text-only ID3 preview for the command card; never fetches the cover."""
    if not playback.audio_only:
        return []
    # Keep the command card and the artwork panel on the same normalized,
    # merged metadata path.  In particular, a partial service payload must not
    # hide title-provided ID3 defaults.
    tags = dict(AudioPreview.from_playback(playback).tags)
    rows = [f"  {label}  ·  {value}" for key, label in _AUDIO_TAG_ROWS if (value := _metadata_value(tags.get(key)))]
    cover = _cover_reference(tags.get("cover"))
    rows.append(f"  cover  ·  {'available  ·  ' + cover if cover else 'not available'}")
    return rows


def _subtitle_reference_lines(playback: Playback) -> list[str]:
    rows: list[str] = []
    current_kind = ""
    for subtitle in playback.subtitle_references:
        kind = subtitle.kind or "normal"
        if kind != current_kind:
            current_kind = kind
            rows.append(f"  [{kind.upper()}]")
        flags = [
            value for value, enabled in (("Original", subtitle.original), ("Selected", subtitle.selected)) if enabled
        ]
        suffix = f" ({', '.join(flags)})" if flags else ""
        label = subtitle.language or "und"
        if subtitle.name and subtitle.name.casefold() != label.casefold():
            label += f" · {subtitle.name}"
        rows.append(f"  {label}{suffix}: {subtitle.url}")
    return rows


def _chapter_metadata_lines(playback: Playback, *, limit: int | None = None) -> list[str]:
    """A compact chapter preview for cards; the saved command keeps every row."""
    chapters = list(playback.chapters)
    if not chapters:
        return []

    def row(index: int, chapter) -> str:
        span = chapter_model.timestamp(chapter.start_ms)
        if chapter.end_ms is not None:
            span += f"–{chapter_model.timestamp(chapter.end_ms)}"
        kind = f"  [{chapter.kind}]" if chapter.kind else ""
        return f"  {index:02d}  {span}{kind}  {chapter.title}"

    if limit is None or len(chapters) <= limit:
        return [row(index, chapter) for index, chapter in enumerate(chapters, start=1)]
    head = max(1, limit - 2)
    rows = [row(index, chapter) for index, chapter in enumerate(chapters[:head], start=1)]
    omitted = len(chapters) - head - 1
    rows.append(f"  …  {omitted} more chapter{'s' if omitted != 1 else ''} in the saved command")
    rows.append(row(len(chapters), chapters[-1]))
    return rows


def command_panel(
    entries: list[tuple[str, str]],
    audio_metadata: Sequence[Sequence[str]] | None = None,
    subtitle_metadata: Sequence[Sequence[str]] | None = None,
    chapter_metadata: Sequence[Sequence[str]] | None = None,
) -> Panel:
    """The card that shows finished commands, when nothing is being downloaded.

    Takes the whole run, not one command: ten episodes picked at once produce ten
    commands, and a card that showed only the newest would leave nine of them
    reachable nowhere but the log. Each is labelled with its own save name and
    numbered, so a keypress or a click lifts one out, and ``^y`` takes all of them.

    Module level so the snapshot tool draws the same card the session does, rather
    than its own approximation of one.
    """
    count = len(entries)
    if count < 2:
        titles, copying, saved = "nothing was downloaded", "1 copies it", "saved to a file too"
    else:
        titles = f"{count} of them, nothing was downloaded"
        copying = "1-9 copy one · click any of them · ^y copies all"
        saved = "each saved to a file too"
    lines: list[str | tuple[str, str]] = []
    for index, entry in enumerate(entries):
        metadata = list(audio_metadata[index]) if audio_metadata and index < len(audio_metadata) else []
        if metadata:
            if lines:
                lines.append("")
            lines.append(f"audio metadata  ·  {entry[0]}")
            lines.extend(metadata)
        subtitles = list(subtitle_metadata[index]) if subtitle_metadata and index < len(subtitle_metadata) else []
        if subtitles:
            if lines:
                lines.append("")
            lines.append(f"all available subtitle URLs  ·  {entry[0]}")
            lines.extend(subtitles)
        chapters = list(chapter_metadata[index]) if chapter_metadata and index < len(chapter_metadata) else []
        if chapters:
            if lines:
                lines.append("")
            lines.append(f"chapters  ·  {entry[0]}")
            lines.extend(chapters)
        lines.append(entry)
    return Panel(
        title=f"command only  ·  {titles}",
        lines=lines,
        # Said out loud, because the card is holding the flow until one of them is
        # pressed and a card that waits without saying so reads as a hang. Both
        # keys, because they do the same thing here: go on to whatever the flow was
        # about to ask, which is the list this title was picked from.
        hint=f"{copying}  ·  {saved}  ·  enter or ^b to go back",
    )


def export_panel(path: Path, document: exports.Document) -> Panel:
    """The card that shows a finished export: what is in it, and where it is.

    The path is the one numbered value, because it is the thing you do something
    with - send it, copy it, move it. The titles are plain rows for the same reason
    the track card's are: there is nothing to lift out of them one at a time.
    """
    count = len(document.entries)
    rows: list[str | tuple[str, str]] = []
    for entry in document.entries:
        detail = f"{len(entry.keys)} key(s)"
        if entry.chapters:
            detail += f"  ·  {len(entry.chapters)} chapter(s)"
        rows.append(f"{entry.label()}  ·  {detail}")
    rows.append("")
    rows.append("It holds the content keys and the manifest link, so treat it like a password. Nothing was downloaded.")
    rows.append(("File", str(path)))
    titles = "1 title" if count == 1 else f"{count} titles"
    return Panel(
        title=f"export  ·  {titles}, {document.keys} key(s)",
        lines=rows,
        hint=(
            "1 copies the path · click it · import it from the main screen, here or "
            "anywhere else  ·  enter or ^b to go back"
        ),
    )


def _hms(seconds: float) -> str:
    """Seconds as HH:MM:SS, for talking about a window rather than a timestamp."""
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"


def _split_span(text: str) -> tuple[str, str]:
    """``"30:00"`` or ``"30:00-1:15:00"`` as a start and an optional end.

    One field rather than two prompts: a stretch is one thought, and every extra
    question is another place to get stuck. Anything that is not a position comes
    back empty, and the caller says so rather than passing nonsense to UniDL.
    """
    start, _, end = str(text or "").strip().partition("-")
    start, end = start.strip(), end.strip()
    if not _POSITION.match(start):
        return "", ""
    if end and not _POSITION.match(end):
        return start, ""
    return start, end


def _actual_video_line(tracks: TrackSet) -> str:
    """Describe selected video tracks using manifest-derived fields only."""
    video = [stream for stream in tracks.selected if stream.media_type == "video"]
    if not video:
        return ""

    def dimensions(stream) -> tuple[int, int]:
        match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", str(stream.resolution or ""))
        return (int(match.group(1)), int(match.group(2))) if match else (0, 0)

    best = max(video, key=lambda stream: (*dimensions(stream)[::-1], int(stream.bandwidth or 0)))
    width, height = dimensions(best)
    resolution = f"{width}x{height}" if width and height else str(best.resolution or "").strip()
    codec = str(pretty_codec(best.codecs, "video") or best.codecs or "").strip()
    dynamic_range = str(best.video_range or "").strip()
    return " · ".join(value for value in (resolution, codec, dynamic_range) if value)


#: how long to let something the user opened sit in front of a screen we want to
#: close, and how often to look. Waiting is the point: the alternative is popping
#: their settings screen out from under them.
_OVERLAY_STEP = 0.05
_OVERLAY_WAIT = 30.0


#: how a job in the queue is getting on. There is no "pending": a row appears
#: when its job starts, because the queue is fed one playback at a time by a
#: generator and nothing knows the next title until it arrives.
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"
CANCELLED = "cancelled"

FINISHED_STATES = (DONE, FAILED, SKIPPED, CANCELLED)


@dataclass
class Job:
    """One playback's progress through the delivery screen."""

    name: str
    state: str = RUNNING
    note: str = ""
    #: What produced this row, kept so it can be run again. A failed title used to
    #: be a dead row: the playback that made it was a local in the worker, and
    #: recovering one episode out of twenty-four meant picking all of them again.
    playback: Playback | None = None
    #: how many times it has been run, including the first
    attempts: int = 1

    @property
    def retryable(self) -> bool:
        return self.state in (FAILED, CANCELLED) and self.playback is not None


class _Sink:
    """The side channel handed to service code."""

    def __init__(self, controller: SessionController):
        self._controller = controller

    def log(self, message: str, level: str = "info") -> None:
        self._controller.post_log(message, level)

    def status(self, message: str) -> None:
        self._controller.post_status(message)

    def batch(self, total: int) -> None:
        self._controller.begin_batch(total)

    def problem(self, headline: str, detail: str = "", hint: str = "") -> None:
        self._controller.post_error(headline, detail, hint)


def _selections(ask: Pick, value: Any) -> list[Any]:
    """The answer with list-navigation entries taken out.

    A "next page" entry is ticked in the same multi-select as the episodes, and
    the driver reads that answer to work out how many titles are coming. Counting
    the paging entry inflated the total by one, and the total is what the queue
    uses to decide it has finished - so the delivery screen went on saying
    "downloading" after the last title had landed, and offering to stop it.

    Matched by identity, not equality: a choice's value is whatever the service
    put there, and comparing arbitrary objects with == is its business, not ours.
    """
    if not isinstance(value, list):
        return [] if value is None else [value]
    skip = [choice.value for choice in ask.choices if choice.navigates]
    if not skip:
        return list(value)
    return [item for item in value if not any(item is entry for entry in skip)]


class TextualPresenter:
    """Blocking presenter, called from the flow worker thread."""

    def __init__(self, controller: SessionController):
        self._controller = controller

    def present(self, ask: Ask) -> Any:
        controller = self._controller
        if controller.aborted:
            raise Quit()

        if isinstance(ask, SettingsAsk):
            controller.app.call_from_thread(controller.app.action_global_settings)
            return None
        if isinstance(ask, Suspend):
            return controller.app.call_from_thread(controller.run_suspended, ask)
        if isinstance(ask, Await):
            return controller.run_await(ask)
        if isinstance(ask, PartnerHandoff):
            return controller.route_partner_authorization(ask.authorization)

        event = threading.Event()
        box: dict[str, Any] = {}
        controller.app.call_from_thread(controller.mount_ask, ask, box, event)
        # Polled rather than waited on for ever. The screens release a pending ask
        # when they go away, but there is a window either side of that - while the
        # mount is still being marshalled, or if the loop stops before the answer
        # arrives - where nothing will ever set this event, and an unbounded wait
        # there is a thread that keeps the process from exiting.
        while not event.wait(_ANSWER_POLL):
            if controller.aborted:
                raise Quit()
        controller.awaiting_answer = False

        if box.get("quit") or controller.aborted:
            raise Quit()
        value = box.get("value")
        if value is BACK:
            raise Back()

        # A multi-select that returned several things is a batch about to happen.
        # Reading it here means every service gets a queue and a confirmation
        # without having to say so, which matters when there are 149 of them and
        # most will never be edited again. ctx.batch() overrides this for the
        # cases where the service knows a total before a picker can reveal it.
        if isinstance(ask, Pick) and ask.multi and ask.scope != SCOPE_DELIVERY:
            wanted = _selections(ask, value)
            if len(wanted) > 1:
                controller.hint_batch(len(wanted))
        return value


class SessionController:
    """Owns the worker, the log, and which screen an ask belongs on."""

    def __init__(self, app: App, service: Service, engine: Engine):
        self.app = app
        self.service = service
        self.engine = engine
        self.settings = service.settings

        #: the session's log, replayed by every screen that appears
        self.log_lines: list[Any] = []
        self.hosts: list[AskHost] = []
        #: the last frame UniDL drew, so a screen that appears mid-download shows
        #: the download rather than an empty panel
        self.last_frame: list[str] = []
        #: Progress is produced in bursts, often from several track workers. A
        #: lock-protected latest-frame slot lets producers carry on without
        #: waiting for Textual to lay out every intermediate VOD/live picture.
        #: DownloadScreen samples this slot at a bounded rate.
        self._frame_lock = threading.Lock()
        self._frame_version = 0
        self._live_frame_mode = False

        self.root: ServiceScreen | None = None
        self.flow_screen: FlowScreen | None = None
        self.delivery: DownloadScreen | None = None

        self.job_running = False
        #: Textual's thread workers are backed by asyncio's executor.  Merely
        #: cancelling the Textual Worker does not join a synchronous service or
        #: downloader call, so the application needs an explicit completion
        #: signal before it gives the event loop back to Python shutdown.
        self._worker_lock = threading.Lock()
        self._worker_count = 0
        self._workers_done = threading.Event()
        self._workers_done.set()
        self._flow_worker_reserved = False
        #: Set before Textual has had a chance to start a retry worker. Without a
        #: pending state, two quick keypresses both observed job_running=False and
        #: put two workers through the same Engine and queue.
        self._retry_pending = False
        self.finished = False
        #: Set when Back is pressed on the service's root menu.  A root Back is
        #: the one navigation action that should finish the whole service session;
        #: result/download pages deliberately use one-level popping instead.
        self.root_back_requested = False
        self.aborted = False
        #: the flow itself stopped with an error - the one failure no queue row
        #: can record, because it happened outside a job
        self.flow_error = False
        #: true while the flow is blocked on an ask instead of producing
        #: playbacks. Being asked something is the only reliable sign that the
        #: flow has stopped emitting for now, which is what tells the queue it is
        #: idle when the expected count was never accurate.
        self.awaiting_answer = False
        #: the flow has asked something since the last playback, so the next one
        #: begins a new run rather than continuing the one that just drained.
        #: True to begin with, because the first playback of a session is a new
        #: run by definition.
        self.run_boundary = True

        #: one entry per playback the flow has handed over, in order
        self.jobs: list[Job] = []
        #: ``(save name, command)`` for every command-only job of the current run,
        #: so the card can show a whole batch rather than only its last title.
        #: Cleared with the queue, because that is what a run is.
        self.commands: list[tuple[str, str]] = []
        #: Metadata is parallel to ``commands`` so command rows remain the only
        #: numbered/copyable values on the card.
        self.command_audio_metadata: list[list[str]] = []
        self.command_subtitle_metadata: list[list[str]] = []
        self.command_chapter_metadata: list[list[str]] = []
        #: the export file this run is writing into, once there is one. Per run for
        #: the same reason the commands above are: a season resolved in one go is one
        #: thing, and it belongs in one file.
        self.export_path: Path | None = None
        #: how many the flow said were coming, when it said so
        self.batch_total: int | None = None
        #: set when the user declined a batch, so the rest are skipped cheaply
        self.batch_declined = False
        #: Polled through UniDL's public cancellation hook. Replaced per job
        #: rather than reused, because a cancellation belongs to the download it
        #: stopped: a single long-lived event stays set afterwards and silently discards
        #: everything picked later in the session.
        self.cancel_event = threading.Event()
        #: Set while transfer workers wait in place. Distinct from cancel: the
        #: delivery screen stays up and Resume continues the same native job.
        self.pause_event = threading.Event()
        #: true while the *remaining* queue is being abandoned, which does
        #: outlive one job but not the batch
        self.batch_cancelled = False
        #: set when the session is being torn down, so a download in flight is
        #: told to stop rather than left running with nowhere to report
        self.leaving = False
        #: the cancellation now in flight is for this one title, not the queue, so
        #: the event is replaced once it has taken effect
        self._skip_one = False

        #: Open except while a command card is waiting to be read. A command is
        #: the whole result of a command-only run, and the flow carries on the
        #: moment it has handed one over - so without this the next question, or
        #: the end of the session, painted over the answer within a second of it
        #: appearing. Cleared when a card goes up, set again when the user is done
        #: with it. An asyncio event because the waiter is the UI thread routing
        #: the next ask; the worker stays blocked behind it, which is what an ask
        #: does anyway.
        self._card_read = asyncio.Event()
        self._card_read.set()
        #: the session ended while a card was still up, so leaving is what happens
        #: once the user is done reading rather than immediately
        self._leave_when_read = False
        #: the session ended while Settings/Search was in front. The overlay is
        #: never popped for the user; the next one of our screens to resume
        #: completes the deferred teardown instead.
        self._leave_pending = False

    # ------------------------------------------------------------------ start
    def start_screen(self) -> ServiceScreen:
        """Build screen 2, the entry point into the service."""
        from .service_screen import ServiceScreen

        # Reserve the worker before the screen is mounted.  This closes the
        # small race where Esc is pressed after ``push_screen`` but before
        # Textual has scheduled the thread worker.
        self._worker_started()
        self._flow_worker_reserved = True
        self.root = ServiceScreen(self)
        return self.root

    def route_partner_authorization(self, authorization: PartnerAuthorization | None) -> PartnerAuthorizationResult:
        """Resolve a receiver and let core deliver a one-time service handoff."""
        if authorization is None:
            return PartnerAuthorizationResult("", False, "authorization was missing")
        try:
            if authorization.source_service_id != self.service.ID:
                raise PartnerAuthorizationError(
                    f"authorization says it came from {authorization.source_service_id}, not {self.service.ID}"
                )
            target_cls = self.app.registry.get(authorization.service_id)
            if target_cls is None:
                raise PartnerAuthorizationError(f"target service {authorization.service_id} is not installed")
            receiver = self.app.registry.build(
                target_cls,
                self.app.config,
                self.app.settings_store,
                globals_scope=self.app.globals,
            )
            result = deliver_partner_authorization(authorization, receiver)
            self.app.invalidate_hints(result.service_id)
            level = "ok" if result.authenticated else "warning"
            self.post_log(result.label or f"{target_cls.NAME} authorization finished", level)
            if result.detail:
                self.post_log(result.detail, "info" if result.authenticated else "warning")
            return result
        except Exception as exc:  # noqa: BLE001 - receiver failures return to the producer menu
            authorization.discard()
            label = f"{authorization.provider} authorization failed"
            self.post_log(f"{label}: {exc}", "error")
            return PartnerAuthorizationResult(
                authorization.service_id,
                False,
                label=label,
                detail=str(exc),
            )

    # ---------------------------------------------------------- host bookkeeping
    def attach(self, host: AskHost) -> None:
        if host not in self.hosts:
            self.hosts.append(host)
        host.replay(self.log_lines)

    def detach(self, host: AskHost) -> None:
        if host in self.hosts:
            self.hosts.remove(host)
        if host is self.flow_screen:
            self.flow_screen = None
        if host is self.delivery:
            self.delivery = None
        if host is self.root:
            # All three, not two. Keeping a reference to an unmounted screen 2
            # meant `host_for` handed the flow a screen with no widgets left, which
            # answers every later question with None instead of ending the session
            # - and it kept the whole controller alive in a reference cycle.
            self.root = None

    def abort(self) -> None:
        """Leaving the service: stop the download and unblock the worker.

        Releasing the hosts only frees a worker that is waiting on an ask. One
        inside ``Engine.run`` carries on downloading into a session whose screens
        have all been popped, so it is told to stop as well - otherwise leaving a
        service, or quitting, leaves a download running with nowhere to report
        and the process waiting on it at shutdown.
        """
        self.aborted = True
        self.leaving = True
        self.batch_cancelled = True
        self.cancel_event.set()
        # The event is the cooperative path; the engine owns the hard-stop path
        # for sockets and external downloader processes.  Keep this call
        # idempotent because both Back and application shutdown may reach it.
        self._shutdown_active_download()
        # and a worker held behind a command card is waiting on something the user
        # has just walked away from
        self._leave_when_read = False
        self._card_read.set()
        for host in list(self.hosts):
            host.release()

    def mark_root_back(self) -> None:
        """Remember a Back answered on the service menu itself.

        The presenter resolves the pending root ask before the flow worker returns,
        so the worker needs a side-channel to distinguish that intentional session
        exit from a normal flow completion after a download.  This flag is consumed
        by :meth:`_drive` on the UI thread.
        """
        self.root_back_requested = True

    def _shutdown_active_download(self) -> None:
        """Close the native downloader's sockets/processes, if one is active."""
        shutdown = getattr(getattr(self, "engine", None), "shutdown_active_download", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception as exc:  # noqa: BLE001 - cancellation must continue
                self.post_log(f"download cleanup requested: {exc}", "warning")

    def _worker_started(self) -> None:
        """Record a controller-owned executor worker entering user code."""
        self._ensure_worker_state()
        with self._worker_lock:
            self._worker_count += 1
            self._workers_done.clear()

    def _worker_finished(self) -> None:
        """Release application shutdown after the last owned worker returns."""
        self._ensure_worker_state()
        with self._worker_lock:
            self._worker_count = max(0, self._worker_count - 1)
            if self._worker_count == 0:
                self._workers_done.set()

    def wait_for_workers(self, timeout: float | None = None) -> bool:
        """Wait a bounded time for service/retry workers to finish cleanup."""
        self._ensure_worker_state()
        return self._workers_done.wait(timeout)

    def _ensure_worker_state(self) -> None:
        """Lazily support lightweight controller doubles used by headless tests."""
        if hasattr(self, "_worker_lock"):
            return
        self._worker_lock = threading.Lock()
        self._worker_count = 0
        self._workers_done = threading.Event()
        self._workers_done.set()

    # -------------------------------------------------------------- log/status
    #: three columns of gutter - a state dot and two spaces - shared by plain
    #: lines, field rows and bare artefacts, so every line in the log starts at the
    #: same column. See :mod:`unidl.tui.logline`.
    GUTTER_WIDTH = GUTTER_WIDTH

    @property
    def palette(self):
        """Live colour values.

        The log holds Rich ``Text``, and Rich resolves styles itself without
        knowing about Textual's design tokens, so these lines need real values.
        Read per call rather than cached, so a theme change applies to whatever
        is written next.
        """
        return self.app.palette

    def post_log(self, message: str, level: str = "info") -> None:
        """One line in the log, with a dot saying what state it is in.

        A caller that names a level gets it. A caller that does not is reporting
        whatever it is up to right now, so while a job is in flight that line is
        the current activity and its dot pulses until the next line lands - which
        is what "in progress" means here, without every call site in core having to
        say so.
        """
        state = logline.normalise(level)
        if state == "info" and self.job_running:
            state = logline.LIVE_STATE
        self.emit_line(LogLine(body=str(message), state=state))

    def post_field(self, label: str, value: str, role: str) -> None:
        """A coloured ``label: value`` row.

        Kept as parts rather than as finished text, so a bracket in a title or a
        URL cannot be read back as markup and so the row can be drawn again.
        ``role`` names a palette field, not a colour, so it survives a theme
        change - and it is why ``title``, ``manifest`` and ``key`` keep the colours
        they have always had while the dot in front of them stays plain: a field
        states a value, it does not report progress.
        """
        self.emit_line(LogLine(body=str(value), label=str(label), role=role))

    def emit_line(self, renderable: Any) -> None:
        """Append to the session log and show it on every live screen.

        Anything that is not already a line becomes one, so a bare artefact - the
        command text, a blank spacer - is written through the same gutter as
        everything else instead of against the left edge.
        """
        if not isinstance(renderable, LogLine):
            text = str(renderable if renderable is not None else "")
            renderable = LogLine(blank=not text.strip(), body=text)
        self.log_lines.append(renderable)
        if len(self.log_lines) > LOG_LIMIT:
            # Bounded like the widget it feeds. Unbounded, a long live capture
            # grew this for ever while the screen could only ever show the last
            # LOG_LIMIT lines anyway, so the extra was memory nobody could read.
            del self.log_lines[: len(self.log_lines) - LOG_LIMIT]
        self._on_ui(self._write_all, renderable)

    def _write_all(self, renderable: Any) -> None:
        for host in list(self.hosts):
            try:
                host.write(renderable)
            except Exception:
                pass

    def post_frame(self, rows: list[str]) -> None:
        """UniDL's own display, as rows to repaint. Worker thread.

        Not the log: these rows are one picture being redrawn, so appending them
        would produce a wall of near-identical lines - which is exactly what it used
        to do. An empty list means there is nothing running to show.

        VOD and live frames use the same latest-picture slot. Textual's
        ``call_from_thread`` waits for the UI callback, so marshalling every
        segment or byte update stalls workers behind layout and visibly flashes
        the terminal. :class:`DownloadScreen` samples this slot at a bounded rate.

        An empty VOD frame is a terminal state and is delivered immediately. A
        live recorder may briefly clear while rebuilding its picture, so that
        transient clear remains suppressed until live mode is explicitly ended.
        """
        snapshot = list(rows)
        with self._frame_lock:
            if self._live_frame_mode and not snapshot:
                return
            if snapshot == self.last_frame:
                return
            self.last_frame = snapshot
            self._frame_version += 1
        if not snapshot:
            self._on_ui(self._frame_all, snapshot)

    def update_frame_columns(self, width: int) -> None:
        """Keep embedded UniDL's progress width aligned with the TUI panel."""
        if width > 0:
            self.engine.frame_columns = max(40, int(width) - 10)

    def frame_snapshot(self) -> tuple[int, list[str]]:
        """A consistent copy of the newest UniDL picture for the UI timer."""
        with self._frame_lock:
            return self._frame_version, list(self.last_frame)

    @property
    def live_frame_mode(self) -> bool:
        """Whether frames currently belong to a live recording."""
        with self._frame_lock:
            return self._live_frame_mode

    def _set_live_frame_mode(self, enabled: bool) -> None:
        """Select buffered live painting without changing ordinary downloads."""
        with self._frame_lock:
            self._live_frame_mode = bool(enabled)

    def _frame_all(self, rows: list[str]) -> None:
        for host in list(self.hosts):
            if not rows:
                clear = getattr(host, "clear_frame", None)
                if callable(clear):
                    try:
                        clear()
                    except Exception:
                        pass
                    continue
            show = getattr(host, "show_frame", None)
            if callable(show):
                try:
                    show(rows)
                except Exception:
                    pass

    def post_status(self, message: str, state: str = logline.LIVE_STATE) -> None:
        """What is happening right now, on one line that replaces the last one.

        In progress by default, because that is what a status message is for: it is
        set when something starts and cleared when it stops.
        """
        self._on_ui(self._status_all, message, state)

    def _status_all(self, message: str, state: str = logline.LIVE_STATE) -> None:
        for host in list(self.hosts):
            try:
                host.set_status(message, state)
            except Exception:
                pass

    def _on_ui(self, callback, *args) -> bool:
        """Run on the UI thread, whichever thread we happen to be on.

        Returns whether it got there, for the one caller that keeps state the
        callback is responsible for clearing.

        The decision is made by thread identity, not by catching an exception.
        ``call_from_thread`` raises for two unrelated reasons - it cannot marshal,
        or the callback itself raised, which it re-raises out of
        ``future.result()`` - and treating the second as the first meant running
        the same widget mutation a second time on this worker thread, against
        state the first attempt had already half-changed, and then swallowing
        whatever happened.
        """
        if self._on_ui_thread():
            return self._guarded(callback, *args)
        try:
            self.app.call_from_thread(callback, *args)
        except Exception as exc:
            # a genuine failure; it is not retried, but it is not hidden either.
            # Do not send this line through emit_line: that method marshals to the
            # same UI which has just refused the callback, recursively repeating
            # the failure until the worker overflows its stack. This matters most
            # while a live recorder is being torn down, because a trapped worker
            # also leaves its pipe-mux child waiting on a FIFO.
            self._remember_interface_failure(exc)
            return False
        return True

    def _on_ui_thread(self) -> bool:
        """True when this is already the thread Textual runs its loop on."""
        thread_id = getattr(self.app, "_thread_id", None)
        return thread_id is not None and thread_id == threading.get_ident()

    def _guarded(self, callback, *args) -> bool:
        try:
            callback(*args)
        except Exception as exc:
            # Already on the UI thread, so there is nothing to marshal to - but it
            # is still recorded. Silence here is what let a call to a method that
            # did not exist survive unnoticed.
            self._remember_interface_failure(exc)
            return False
        return True

    def _remember_interface_failure(self, exc: Exception) -> None:
        """Record a UI failure without trying that failed UI route again."""
        self.log_lines.append(
            LogLine(
                body=f"interface update failed: {type(exc).__name__}: {exc}",
                state="error",
            )
        )
        if len(self.log_lines) > LOG_LIMIT:
            del self.log_lines[: len(self.log_lines) - LOG_LIMIT]

    def refresh_after_settings(self) -> None:
        for host in list(self.hosts):
            try:
                host.refresh_after_settings()
            except Exception:
                pass

    # -------------------------------------------------------------- the queue
    def begin_batch(self, total: int) -> None:
        """A flow declared that ``total`` playbacks are coming. Worker thread.

        Raises :class:`Back` when the user says no, which unwinds the flow to
        wherever the selection was made. The service is mid-loop at this point,
        so declining has to travel the same way backing out of a list does.
        """
        self._open_queue(total)
        self._confirm_batch()

    def hint_batch(self, total: int) -> None:
        """A multi-select answered with ``total`` things, so infer a batch.

        Same effect as :meth:`begin_batch`, from the other direction: the driver
        can see the answer even when the service never says how many emits are
        coming.
        """
        self._open_queue(total)
        self._confirm_batch()

    def _open_queue(self, total: int) -> None:
        wanted = max(0, int(total))
        if self.queue_complete:
            # the last batch is over and has been reported: start clean
            self.jobs = []
            self.clear_cancel()  # a new batch is not born cancelled
        # Otherwise this pick happened *inside* a run - a page of episodes after
        # an earlier page, seasons then their episodes - so the rows already on
        # screen still belong to it. Wiping them lost the record of titles that
        # had already downloaded, in the one situation where you were most likely
        # to be watching the queue.
        self.batch_declined = False
        done = len([job for job in self.jobs if job.state in FINISHED_STATES])
        self.batch_total = done + wanted
        self.render_queue()
        if wanted > 1:
            self.post_log(f"{wanted} titles queued")

    def _confirm_batch(self) -> None:
        """Ask once for the whole batch, not once per title."""
        if (self.batch_total or 0) < 2 or not self.settings.get("confirm_batch"):
            return
        presenter = TextualPresenter(self)
        ctx = FlowContext(settings=self.settings, sink=_Sink(self))
        answer = presenter.present(
            ctx.confirm(
                f"Download all {self.batch_total} titles?",
                default=True,
                # asked on screen 3, where the selection was made: no job exists
                # yet, so there is nothing for screen 4 to be about
                scope=SCOPE_FLOW,
            )
        )
        if answer is False:
            self.batch_declined = True
            # The batch is over before it began, so the queue must stop waiting
            # for titles that are never coming. Leaving the total set left it
            # permanently short of its own expectation, which is a state nothing
            # can get out of: the refusal never expired, and the next title
            # picked - a different title, from the menu - was skipped as though
            # it had been part of the batch that was turned down.
            self.batch_total = None
            self.post_log("batch cancelled", "warning")
            raise Back()

    # ---------------------------------------------------------- cancellation
    @property
    def cancel_requested(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def pause_requested(self) -> bool:
        return self.pause_event.is_set()

    def request_pause(self) -> bool:
        """Hold the running transfer without cancelling it. Returns False if already paused."""
        if not self.job_running or self.cancel_event.is_set() or self.pause_event.is_set():
            return False
        self.pause_event.set()
        self.post_log("paused; Resume continues from this point", "warning")
        self.post_status(tr("delivery.status.paused"))
        if self.delivery is not None:
            self._on_ui(self.delivery.refresh_keys)
        return True

    def request_resume_pause(self) -> bool:
        """Continue a paused transfer. Returns False if nothing is paused."""
        if not self.pause_event.is_set() or self.cancel_event.is_set():
            return False
        self.pause_event.clear()
        self.post_log("resumed", "ok")
        if self.job_running:
            self.post_status(tr("delivery.status.downloading"))
        if self.delivery is not None:
            self._on_ui(self.delivery.refresh_keys)
        return True

    def clear_pause(self) -> None:
        self.pause_event.clear()

    def request_cancel(self) -> bool:
        """Ask the running download to stop. Returns False if already asked.

        Cancelling one title cancels the rest of the batch too: you asked to
        stop, not to skip one and carry on with nine more. It does *not* cancel
        what you pick afterwards, which is why the queue-level flag is separate
        from the per-job event. To stop only what is in flight, see
        :meth:`request_skip`.
        """
        if self.cancel_event.is_set():
            return False
        self.cancel_event.set()
        self.pause_event.clear()
        self.batch_cancelled = True
        self._shutdown_active_download()
        self.post_log("stopping at the next segment", "warning")
        if self.job_running:
            # only while there is something to stop: a cancellation asked for
            # between two titles has no job whose ending would take this line down
            # again, so it would sit there saying "Cancelling" for ever
            self.post_status(tr("delivery.status.cancelling"))
        if self.delivery is not None:
            self._on_ui(self.delivery.refresh_keys)
        return True

    def request_skip(self) -> bool:
        """Stop the title in flight and carry on with the rest of the batch.

        The other half of Back: "not this one" rather than "not any of them". The
        queue-level flag is left alone, and the event is replaced once the job it
        stopped has ended - otherwise the next title in the batch would start with
        a cancellation already asked for and end before it began.
        """
        if not self.job_running or self.cancel_event.is_set():
            return False
        self.cancel_event.set()
        self.pause_event.clear()
        self._skip_one = True
        self._shutdown_active_download()
        self.post_log("skipping this one; the rest of the queue carries on", "warning")
        self.post_status(tr("delivery.status.skipping"))
        if self.delivery is not None:
            self._on_ui(self.delivery.refresh_keys)
        return True

    def clear_cancel(self) -> None:
        """Forget a cancellation, so the next thing you start is not born dead."""
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.batch_cancelled = False

    def _release_playback(self, playback: Playback) -> None:
        """Release service-owned playback state on every exit path."""
        release = getattr(self.service, "release_playback", None)
        if not callable(release):
            return
        try:
            release(playback, self.post_log)
        except Exception as exc:  # noqa: BLE001 - cleanup must not strand a job
            self.post_log(f"could not release playback transport: {exc}", "warning")

    def _start_job(self, playback: Playback) -> Job:
        job = Job(name=playback.save_name, state=RUNNING, playback=playback)
        self.jobs.append(job)
        self.render_queue()
        return job

    def _finish_job(self, job: Job, state: str, note: str = "") -> None:
        job.state = state
        job.note = note
        if state not in (FAILED, CANCELLED):
            # The playback is kept for one purpose - running the row again - and a
            # row that finished or was skipped is not one you can run again. Letting
            # it go matters at batch size: it holds the manifest headers, the keys
            # and, for a service that hands over an inventory rather than a URL, the
            # whole parsed track list. Twenty-four of those outlived their download
            # for as long as the session did.
            job.playback = None
        self.render_queue()
        # A row that failed is a row that can be run again, and the key for that is
        # offered on whichever screen you are on. Nothing else was going to redraw
        # the bar, so the key existed and never appeared.
        self._on_ui(self._refresh_keys_all)

    def _refresh_keys_all(self) -> None:
        for host in list(self.hosts):
            try:
                host.refresh_keys()
            except Exception as exc:  # noqa: BLE001
                self.post_log(f"could not redraw the key bar: {exc}", "warning")

    @property
    def queue_complete(self) -> bool:
        """True when nothing is running and nothing more is expected.

        The expected count is a hint from the flow, not a contract: a service can
        put a "next page" entry in the same multi-select the driver counts, which
        inflates it, and then the count is never reached. So the count is not the
        only way out. A flow that is blocked on an ask, or has ended, has stopped
        emitting whatever it was going to emit - which is what the delivery screen
        needs to know, and what it used to miss when it kept saying "downloading"
        long after the last title had landed.
        """
        if any(job.state == RUNNING for job in self.jobs):
            return False
        if self.batch_total is None or self.finished or self.awaiting_answer:
            return True
        return len([j for j in self.jobs if j.state in FINISHED_STATES]) >= self.batch_total

    def queue_counts(self) -> dict[str, int]:
        counts = {state: 0 for state in (DONE, FAILED, SKIPPED, CANCELLED, RUNNING)}
        for job in self.jobs:
            counts[job.state] = counts.get(job.state, 0) + 1
        return counts

    def render_queue(self) -> None:
        self._on_ui(self._render_queue_ui)

    def _render_queue_ui(self) -> None:
        if self.delivery is not None:
            self.delivery.render_queue(self.jobs, self.batch_total)

    # ------------------------------------------------------------------- errors
    @property
    def session_failed(self) -> bool:
        """Did this session actually end badly?

        Read from the queue, not latched by whoever last showed an error panel.
        Those are different questions: a licence that could not be fetched is
        worth a panel and still leaves a download worth attempting, and treating
        the two as one meant a session where the first title's key failed and the
        next nine downloaded cleanly still refused to close itself.
        """
        return self.flow_error or any(job.state == FAILED for job in self.jobs)

    def post_error(self, headline: str, detail: str = "", hint: str = "") -> None:
        """Report a failure on screen as well as in the log.

        The log still gets it, because that is the record. The panel is so the
        failure is visible without having to go looking for it. Neither decides
        anything: what went wrong is recorded where the outcome lives - a queue
        row for a job, :attr:`flow_error` for the flow itself.
        """
        self.post_log(f"{headline}: {detail}" if detail else headline, "error")
        self._on_ui(self._error_all, headline, detail, hint)

    def _error_all(self, headline: str, detail: str, hint: str) -> None:
        # only the screen in front: an error panel on a covered screen would
        # reappear when you went back to it, long after it stopped being true
        for host in list(self.hosts):
            if host is self.app.screen:
                try:
                    host.show_error(headline, detail, hint)
                except Exception:
                    pass

    def clear_errors(self) -> None:
        self._on_ui(self._clear_error_all)

    def _clear_error_all(self) -> None:
        for host in list(self.hosts):
            try:
                host.clear_error()
            except Exception:
                pass

    # ------------------------------------------------------------------ routing
    async def mount_ask(self, ask: Ask, box: dict[str, Any], event: threading.Event) -> None:
        """Put ``ask`` on the screen it belongs to. Runs on the UI thread.

        A question means the waiting is over. The status line says what is being
        done *while you wait for it*, so the moment something is being asked, there
        is nothing for it to say - and whatever it said last must not outlive the
        step that set it.

        Cleared here rather than by whoever set it, because most of what sets it is
        service code: a hundred-odd services call ``ctx.status`` before a request
        and none of them can be expected to take it down again on every path out,
        including the ones that raise. A live channel that failed to resolve is the
        case that showed it - "Resolving <channel> (widevine)" sat on the service
        menu for the rest of the session, with the menu itself proving nothing was
        being resolved.

        Every scope, delivery included. A track picker now comes before the licence
        so the final selection can inform DRM coverage; either way nothing is lost
        by clearing the status - it is transient by design, and what a step actually
        produced is logged by whoever produced it.
        """
        self._status_all("")
        # Being asked something means the flow is not producing playbacks any
        # more, which is the queue's other way of knowing it is idle when the
        # count it was given turned out to be wrong. A delivery-scope ask does not
        # count: the track picker and the download/save prompt are part of the job
        # already in flight, not a pause between jobs.
        if getattr(ask, "scope", SCOPE_FLOW) != SCOPE_DELIVERY:
            self.awaiting_answer = True
            self.run_boundary = True
            self._after_job()
            # A flow-scope ask is the boundary: the run that produced a command
            # card is over and something new is being asked. That is exactly the
            # moment the card would be cleared, so wait for the user to be done
            # with it first. A delivery-scope ask - the track picker, the
            # download-or-save prompt - belongs to the job in flight and never
            # arrives while a card for that same job is up.
            await self.hold_for_card()
        try:
            host = await self.host_for(getattr(ask, "scope", SCOPE_FLOW))
        except Exception as exc:  # a routing failure must not hang the worker
            self.post_log(f"could not show a prompt: {exc}", "error")
            host = None
        # Routing can take a while - it waits out a screen the user opened in
        # front - and the session can be abandoned during it. Mounting a question
        # on the way out would ask something nobody is there to answer.
        if self.aborted:
            host = None
        if host is None:
            box["quit"] = True
            event.set()
            return
        host.show_ask(ask, box, event)

    @property
    def hold_flow(self) -> bool:
        """Screen 3 is showing a result that must not be popped out from under it.

        Derived rather than stored: it is true exactly while that screen has a
        panel up, so there is no flag to reset and no way for it to be left set
        after the panel has gone. The panel disappears when the next ask lands on
        the screen, or when the user dismisses it, and the hold goes with it.
        """
        screen = self.flow_screen
        return screen is not None and screen.panel_showing

    async def host_for(self, scope: str) -> AskHost | None:
        """The screen that hosts asks of this scope, pushing or popping to suit."""
        if scope == SCOPE_ROOT:
            # A command card you must not have taken off your screen is left up and
            # the menu is mounted underneath it. Download results follow the flow
            # back to its picker first, so they are closed before a root ask.
            await self._close(self.delivery)
            if not self.hold_flow:
                await self._close(self.flow_screen)
            return self.root
        if scope == SCOPE_DELIVERY:
            # Only when there is one. Nothing downloads in command-only mode, so
            # screen 4 is never opened for it, and a track picker must not be the
            # thing that summons a download screen the user said they did not want.
            if self.delivery is not None:
                return self.delivery

        # A flow ask means the previous job, if any, is behind us. Both successful
        # and failed jobs signal Back at their emit point; the service can then
        # yield the exact picker that produced the title before this ask is shown.
        await self._close(self.delivery)
        if self.flow_screen is None:
            from .flow_screen import FlowScreen

            await self._wait_for_front()
            screen = FlowScreen(self)
            self.flow_screen = screen
            await self.app.push_screen(screen)
        return self.flow_screen

    async def delivery_screen(self) -> DownloadScreen:
        if self.delivery is None:
            from .download_screen import DownloadScreen

            await self._wait_for_front()
            screen = DownloadScreen(self)
            self.delivery = screen
            await self.app.push_screen(screen)
        return self.delivery

    async def _wait_for_front(self) -> None:
        """Hold off pushing while a screen the user opened is in front.

        :meth:`_close` already waits a foreign screen out rather than destroying
        it; this is the same courtesy from the other side. Without it a flow that
        needed a new screen mid-run put it straight on top of the settings panel
        the user was typing into - their screen was still there, underneath, but
        they were no longer looking at it.

        Bounded by the same deadline, and for the same reason: mounting behind an
        overlay is a poor outcome, waiting for ever is a worse one.
        """
        ours = [screen for screen in (self.root, self.flow_screen, self.delivery) if screen is not None]
        waited = 0.0
        while waited < _OVERLAY_WAIT:
            current = self.app.screen
            if len(self.app.screen_stack) <= 1 or any(current is screen for screen in ours):
                return
            await asyncio.sleep(_OVERLAY_STEP)
            waited += _OVERLAY_STEP
        self.post_log("still waiting for the screen in front", "warning")

    async def _close(self, screen: AskHost | None) -> None:
        """Take ``screen`` off the stack, once it is the one in front.

        Only ever pops ``screen`` itself. Settings and search are reachable with
        ^s and ^f from screens 3 and 4 while the worker is still running, and they
        sit on top of ours, so popping whatever happens to be there took the
        user's own screen with it. Worse, Textual's ``pop_screen`` drops the
        callback a screen was pushed with rather than calling it, so the theme
        change or login refresh that screen was going to apply on the way out
        vanished too.

        So something in front is waited out, not destroyed. The wait is bounded:
        mounting an ask on a covered screen is a poor outcome, but it is a better
        one than tearing down a screen the user is typing into.

        The pop is awaited because unmounting is what clears our reference to the
        screen, and the caller is about to test that reference.
        """
        if screen is None:
            return
        waited = 0.0
        while screen in self.app.screen_stack and len(self.app.screen_stack) > 1:
            if self.app.screen is screen:
                await self.app.pop_screen()
                return
            if waited >= _OVERLAY_WAIT:
                self.post_log("still waiting for the screen in front", "warning")
                return
            await asyncio.sleep(_OVERLAY_STEP)
            waited += _OVERLAY_STEP

    # ------------------------------------------------------------------- await
    def request_live_key(
        self,
        kid: str | None,
        stream: object,
        segment: object,
        reason: str,
        automatic_error: str,
    ) -> str:
        """Ask on DownloadScreen when automatic live-key resolution is exhausted.

        Called from whichever UniDL worker detected the rotation. The UI owns
        stdin, so the worker marshals a normal delivery-scope ask to Textual and
        waits on an Event; no terminal mode or input file descriptor is touched.
        """
        expected = normalize_hex(kid)
        format_line = getattr(stream, "format_line", None)
        stream_label = (
            str(format_line()) if callable(format_line) else str(getattr(stream, "media_type", "media") or "media")
        )
        problem = ""
        while True:
            lines: list[str | tuple[str, str]] = [
                f"{reason}. Automatic vault and service licence lookup could not continue.",
                ("Stream", stream_label[:220]),
                ("KID", expected or "not exposed by the fragment"),
            ]
            if automatic_error:
                lines.append(f"Automatic lookup: {automatic_error}")
            if problem:
                lines.append(f"That value was not accepted: {problem}")
            ask = TextAsk(
                title="Live recording needs a new content key",
                placeholder=("32 hexadecimal KEY, or KID:KEY" if expected else "32 hexadecimal KID:KEY"),
                password=True,
                lines=lines,
                scope=SCOPE_DELIVERY,
            )
            event = threading.Event()
            box: dict[str, Any] = {}
            self.app.call_from_thread(self.mount_ask, ask, box, event)
            while not event.wait(_ANSWER_POLL):
                if self.aborted or self.cancel_requested:
                    raise KeyboardInterrupt("live key entry cancelled")
            value = box.get("value")
            if box.get("quit") or value is BACK or self.aborted:
                self.request_cancel()
                raise KeyboardInterrupt("live key entry cancelled")
            raw = str(value or "").strip()
            pair = split_pair(raw) if ":" in raw or "=" in raw else None
            if pair is None and expected:
                key = normalize_hex(raw)
                pair = (expected, key) if key else None
            if pair is None:
                problem = "enter KID:KEY" if not expected else "enter KEY or KID:KEY"
                continue
            if expected and pair[0] != expected:
                problem = f"the KID must be {expected}"
                continue
            self.post_log(
                f"manual live key accepted for {pair[0]}",
                "ok",
            )
            return f"{pair[0]}:{pair[1]}"

    def run_await(self, ask: Await) -> Any:
        """Show a code, then poll for it until something happens. Worker thread.

        The polling runs here, on the flow's own thread, for the same reason
        everything else about a service does: it is a network call, and the event
        loop is not the place for one. The screen only holds the panel and a way
        out.

        Three ways this ends, and they are not the same:

        * ``poll`` returns something  - that is the answer
        * the user backs out         - :class:`Back`, so it lands where backing
          out of the previous question would
        * the deadline passes        - also Back, but said out loud first, because
          a code that expired silently looks like the app stopped working
        """
        if not callable(ask.poll):
            return None

        event = threading.Event()
        box: dict[str, Any] = {}
        self.app.call_from_thread(self.mount_ask, ask, box, event)
        # the panel is where this is read; the log keeps a copy because scrollback
        # is where you go looking for a code the panel has already cleared
        for line in ask.as_text():
            self.post_log(line)

        deadline = time.monotonic() + max(float(ask.timeout), 5.0)
        interval = max(float(ask.interval), 0.5)
        try:
            while True:
                if box.get("quit") or self.aborted:
                    raise Quit()
                if box.get("value") is not None or event.is_set():
                    # the widget answered, which for this ask only ever means Back
                    raise Back()
                left = int(deadline - time.monotonic())
                if left <= 0:
                    self.post_log("that code expired before it was confirmed", "warning")
                    raise Back()
                self.post_status(f"{ask.waiting_note} · {left}s left")
                try:
                    result = ask.poll()
                except Exception as exc:  # noqa: BLE001 - one failed poll is not fatal
                    self.post_log(f"still waiting ({type(exc).__name__}: {exc})", "warning")
                    result = None
                if result is not None:
                    self.post_status("")
                    return result
                # Waits on the event rather than sleeping, so backing out is acted
                # on immediately instead of after the rest of the interval.
                event.wait(interval)
        finally:
            self.awaiting_answer = False
            self.post_status("")
            self._on_ui(self._clear_await)

    def _clear_await(self) -> None:
        for host in list(self.hosts):
            if host is self.app.screen:
                try:
                    host.clear_ask()
                except Exception:
                    pass

    # ----------------------------------------------------------------- suspend
    async def run_suspended(self, ask: Suspend) -> Any:
        """Leave application mode, run the work, then restore the UI.

        For an external helper that owns the terminal: a tool drawing its own
        prompts, or asking for a password on the tty, cannot do that while a
        full-screen app owns it. The work runs in a thread so the event loop is not
        blocked for the whole subprocess.
        """
        if not callable(ask.work):
            return None
        self.post_log(ask.note or "handing over the terminal")
        try:
            with self.app.suspend():
                if ask.note:
                    print(f"\n{ask.note}\n", flush=True)
                return await asyncio.to_thread(ask.work)
        except Exception as exc:
            self.post_log(f"suspended run failed: {exc}", "error")
            return None

    # ------------------------------------------------------------------ driving
    def drive(self) -> None:
        """Run the service flow while publishing its worker lifetime."""
        if getattr(self, "_flow_worker_reserved", False):
            self._flow_worker_reserved = False
        else:
            # Keep direct/headless callers safe even when they do not build a
            # ServiceScreen first.
            self._worker_started()
        try:
            self._drive()
        finally:
            self._worker_finished()

    def _drive(self) -> None:
        """Run the service flow to completion. Worker thread."""
        presenter = TextualPresenter(self)
        # interactive: a person is answering these, which is what lets an entry
        # point return to its own prompt rather than to the menu above it
        ctx = FlowContext(settings=self.settings, sink=_Sink(self), interactive=True)
        self.engine.log = self.post_log
        # and UniDL's own display goes to the screen, not into the log, laid out
        # for the panel it lands in rather than for a terminal that is not there.
        # The chrome around it - a margin, a border, padding - is ten columns.
        self.engine.frame = self.post_frame
        self.update_frame_columns(int(getattr(self.app.size, "width", 100)))
        self.engine.live_key_input = self.request_live_key

        extras = self.service.ctx.extras
        target = extras.pop("initial_target", None)
        query = extras.pop("initial_search", None)
        document = extras.pop("initial_import", None)

        if document is not None:
            flow = self._replay(ctx, document)
        elif target:
            flow = self._deep_link(ctx, self.service.open_url, str(target))
        elif query:
            flow = self._deep_link(ctx, self.service.search, str(query))
        else:
            flow = self.service.home(ctx)

        try:
            run_flow(flow, presenter, on_emit=lambda pb: self.deliver(pb, presenter, ctx))
        except Quit:
            return
        except Exception as exc:
            self.flow_error = True
            self.post_error(
                f"{self.service.NAME} stopped with an error",
                f"{type(exc).__name__}: {exc}",
                "^b goes back one page. Turn on debug in settings for a traceback.",
            )
            if self.settings.get("debug"):
                import traceback

                for line in traceback.format_exc().splitlines():
                    self.post_log(line)
        finally:
            self.engine.live_key_input = None

        self.finished = True
        # nothing more is coming, so a batch that was abandoned part-way stops
        # claiming to be in progress - and neither does the status line, for the
        # flow that died between two jobs with one still on it
        self.batch_total = None
        self.post_status("")
        self._on_ui(self._after_job)

        if self.session_failed:
            self.post_log("Session ended with errors. ^b to go back.", "warning")
        elif self.root_back_requested:
            # Back on screen 2 is the bottom of the service stack.  Preserve the
            # traditional one-press exit here; only result/download completion is
            # kept mounted for one-level navigation.
            self._on_ui(self.leave_service)
        # Keep the screens that led to a successful result mounted.  Navigation is
        # a one-level stack: the next Back reveals the immediate parent (the flow
        # picker or service menu), not the platform list.  The old eager
        # ``leave_service`` call collapsed the whole session as soon as the worker
        # returned, making a completed download jump home before it could be
        # inspected.  The normal screen Back handlers perform the final teardown
        # one screen at a time.

    def _replay(self, ctx: FlowContext, document: exports.Document):
        """Hand the session what an export already knows, and stop. Worker thread.

        Not a service flow: nothing here asks the service anything, because the two
        things a service is for - finding the title and getting the key - have both
        already happened and are in the file. What follows is the ordinary delivery
        path, and it needs no help to work: a playback that arrives carrying keys
        never reaches a licence request.

        No menu afterwards, unlike a deep link. A deep link falls into the service's
        own menu because you are *in* that service and the next thing you do is
        probably there too; an import is a file being finished, and offering a
        search that would need the account this file exists to avoid is an invitation
        to a failure.
        """
        ctx.log(
            f"importing {len(document.entries)} title(s) from an export"
            f"{f' made by {document.app}' if document.app else ''}"
            f"{f', {document.created}' if document.created else ''}"
        )
        ctx.log("no sign-in, no licence request: the keys came with the file")
        for entry in document.entries:
            if entry.summary:
                ctx.log(f"exported as: {entry.summary}")
            yield ctx.emit(entry.playback())

    def _deep_link(self, ctx: FlowContext, entry, argument: str):
        """Run a URL or search straight away, then fall into the service's menu.

        A deep link used to *replace* the menu, and the menu is where ``Back``
        from a sub-flow is caught and turned into "return to the platform's
        options". So pasting a URL on the platform list, or arriving from global
        search, produced a session where backing out of the first prompt ended it,
        and where finishing one download tore all four screens down - while the
        very same title picked from the menu left you on the menu. Running the
        link first and then handing over to ``home`` gives all three entry points
        the same navigation, and does not ask 150 services to know about it.
        """
        try:
            yield from entry(ctx, argument)
        except Back:
            pass
        except NotImplementedError as exc:
            ctx.warn(str(exc))
        except RuntimeError as exc:
            # Same rule as the menu it is about to hand over to: a URL that could
            # not be opened is a failed step, not a failed session.
            ctx.problem(
                f"{self.service.NAME} could not open that",
                f"{type(exc).__name__}: {exc}",
                "Its menu is next - sign in again, or try something else.",
            )
        yield from self.service.home(ctx)

    def leave_service(self) -> None:
        """Tear the whole session down. UI thread.

        Pops this session's own screens and stops at anything else. If the user
        has settings or search open when the last download lands, that screen is
        theirs, and it stays until they close it.

        Public because two different things ask for it: a flow that ended cleanly,
        which does it for the user, and a flow that died, where the screen it left
        behind is the only thing still able to act on Back.

        A command card still waiting to be read defers this rather than cancelling
        it: a flow that ends right after saving a command used to pop the screen
        out from under the answer it had just put there.
        """
        if self.card_waiting:
            # A result card belongs to the current flow page.  It is not a reason
            # to tear down the service: dismissing it should reveal the page that
            # launched the result, and a later Back from the service menu can then
            # leave the service normally.
            self._leave_when_read = False
            return
        ours = [s for s in (self.delivery, self.flow_screen, self.root) if s is not None]
        if not any(screen in self.app.screen_stack for screen in ours):
            self._leave_pending = False
            return
        if not any(self.app.screen is screen for screen in ours):
            self._leave_pending = True
            return
        self._leave_pending = False
        for _ in range(_POP_GUARD):
            if len(self.app.screen_stack) <= 1:
                return
            if not any(self.app.screen is screen for screen in ours):
                return
            self.app.pop_screen()

    def resume_deferred_leave(self) -> None:
        """Finish a teardown that waited for a user-owned overlay to close."""
        if self._leave_pending:
            self.leave_service()

    # ----------------------------------------------------------------- one job
    def deliver(self, playback: Playback, presenter: TextualPresenter, ctx: FlowContext) -> None:
        """Process one emitted title, then navigate back after a completed run.

        ``run_flow`` turns this :class:`Back` into ``flow.throw(Back())`` at the
        service's ``yield ctx.emit(...)``. Browsing flows can therefore restore
        the picker that produced the title. A batch stays intact because the
        signal is sent only after its last queue item. The signal is deliberately
        identical for success and failure: the result screen is a report, while
        Back always means the page that launched this title.
        """
        self.process(playback, presenter, ctx)
        if self.queue_complete:
            raise Back()

    def process(self, playback: Playback, presenter: TextualPresenter, ctx: FlowContext) -> str:
        """Resolve keys, choose tracks, then download or export. Worker thread."""
        # A playback that arrives after the queue drained *and* after the flow
        # asked something is a new run, and starts clean. Three things were
        # following the user around without it: a cancellation asked for once
        # applied to everything picked afterwards, a batch declined once poisoned
        # the rest of the session, and two unrelated single downloads rendered as
        # a batch of two.
        #
        # Both halves matter. A flow that keeps emitting without asking is still
        # the same run, however wrong its declared total turned out to be, so a
        # cancellation still applies to the rest of it.
        if self.run_boundary and self.queue_complete:
            self.jobs = []
            self.commands = []
            self.command_audio_metadata = []
            self.command_subtitle_metadata = []
            self.command_chapter_metadata = []
            self.export_path = None
            self.batch_total = None
            self.batch_declined = False
            self.clear_cancel()
        self.run_boundary = False

        try:
            job = self._start_job(playback)
        except BaseException:
            # A playback is authorized before it reaches this method. If the
            # queue row cannot be mounted, it still owns a service-side lease.
            self._release_playback(playback)
            raise
        if self.batch_declined or self.batch_cancelled or self.leaving:
            # The rest of an abandoned batch. Recorded and said out loud: a job
            # that disappears without a screen, a log line or a queue row is
            # indistinguishable from a download that silently did nothing.
            state = SKIPPED if self.batch_declined else CANCELLED
            note = "batch declined" if self.batch_declined else "cancelled"
            self._release_playback(playback)
            self._finish_job(job, state, note)
            self.post_log(f"{note}: {playback.save_name}", "warning")
            self._on_ui(self._after_job)
            return state

        return self._run_job(job, playback, presenter, ctx)

    def _run_job(self, job: Job, playback: Playback, presenter: TextualPresenter, ctx: FlowContext) -> str:
        """One title, start to finish, with the queue kept honest either way.

        Shared by the first attempt and by a retry, so a retried row goes through
        exactly the path it did the first time rather than a second copy of it.
        """
        self.job_running = True
        self.clear_pause()
        state, note = FAILED, "stopped unexpectedly"
        try:
            state, note = self._process(playback, presenter, ctx)
        finally:
            self._release_playback(playback)
            self.job_running = False
            # Whatever happened, nothing is happening now. The status line says
            # what a job is *doing*, and it used to be cleared at each of the
            # places a job can end well - which left every other exit stating a
            # step that was over: back out of the track picker and the service menu
            # underneath went on saying "Resolving keys", for the rest of the
            # session. One line here covers all of them, including the ones added
            # later and the ones that raise.
            self.post_status("")
            if self._skip_one:
                # the cancellation belonged to this job only; the next one in the
                # batch has to start from a clean event
                self._skip_one = False
                self.clear_cancel()
            self._finish_job(job, state, note)
            self._on_ui(self._after_job)
        return state

    # -------------------------------------------------------------- retrying
    def retry(self, wanted: list[Job] | None = None) -> int:
        """Run failed or cancelled rows again. Returns how many were started.

        Only while nothing is in flight, which is when a person is looking at a
        finished queue deciding what to rescue. Allowing it mid-batch would put two
        workers through one engine, and the second would inherit the first's
        cancellation event and its screens.
        """
        if self.job_running or self._retry_pending or self.aborted:
            return 0
        jobs = [job for job in (wanted if wanted is not None else self.jobs) if job.retryable]
        if not jobs:
            return 0
        # a queue that was cancelled is being deliberately restarted
        self.clear_cancel()
        self.batch_declined = False
        self._retry_pending = True
        self._worker_started()
        try:
            self.app.run_worker(
                lambda: self._retry_worker(jobs),
                thread=True,
                group="queue-retry",
                exclusive=True,
                exit_on_error=False,
            )
        except Exception:
            self._retry_pending = False
            self._worker_finished()
            raise
        return len(jobs)

    def _retry_worker(self, jobs: list[Job]) -> None:
        """Re-run the given rows, one after another. Worker thread."""
        presenter = TextualPresenter(self)
        ctx = FlowContext(settings=self.settings, sink=_Sink(self), interactive=True)
        try:
            for job in jobs:
                if self.aborted or self.leaving or self.batch_cancelled:
                    return
                if job.playback is None:
                    continue
                job.state = RUNNING
                job.note = f"attempt {job.attempts + 1}"
                job.attempts += 1
                self.render_queue()  # marshals itself
                self.post_log(f"retrying {job.name} (attempt {job.attempts})", "warning")
                self._run_job(job, job.playback, presenter, ctx)
        finally:
            self._retry_pending = False
            self._worker_finished()

    def _after_job(self) -> None:
        """Only call the screen finished when the whole queue is."""
        if not self.queue_complete:
            return
        if self.delivery is not None:
            self.delivery.mark_done()

    def _process(self, playback: Playback, presenter: TextualPresenter, ctx: FlowContext) -> tuple[str, str]:
        """Run one job. Returns its outcome for the queue.

        Screen 4 is UniDL's screen, and it is opened at the one moment that
        earns it: a download is about to start. Everything before that - the
        report, the tracks, the delivery question - happens where the user already
        is, and a run that ends at the command never leaves that screen at all.
        Opening it up front meant "save the command only" put up a download
        screen, headed it "nothing will be downloaded", and asked about tracks on
        it, which is the whole download interface for a job with no download in it.
        """
        mode = str(self.settings.get("after_resolve", "download"))
        self.post_status(tr("delivery.status.preparing", name=playback.save_name))

        # Title and manifest are always reported, whatever the service, whatever
        # the mode, VOD or live. Keys follow once they are known. Colour-coded so
        # the three are distinguishable at a glance, and selectable with the
        # mouse to copy them out.
        self.emit_line("")
        self.post_field("title", playback.title.full_label() or playback.save_name, "accent")
        self.post_field("save as", playback.save_name, "fg")
        if playback.manifest_url:
            self.post_field("manifest", playback.manifest_url, "manifest")
        elif playback.inline_manifest:
            kind = (
                "inline HLS manifest"
                if str(playback.inline_manifest).lstrip().startswith("#EXTM3U")
                else "inline DASH manifest"
            )
            self.post_field("manifest", kind, "manifest")
        elif playback.json_manifest is not None:
            self.post_field("manifest", "generated JSON track manifest", "manifest")
        if playback.audio_only:
            # say it, because "no video track" and "the video failed" look the
            # same in a track list otherwise
            requested_audio_format = str(self.settings.get("audio_format", "") or "").casefold()
            if requested_audio_format == "auto":
                shape = "audio only"
            else:
                audio_format = self.engine.audio_format_for(playback, self.settings)
                shape = f"audio only, as {audio_format.upper()}" if audio_format else "audio only"
            self.post_field("kind", f"{shape}{' · live' if playback.is_live else ''}", "warn")
        elif playback.is_live:
            self.post_field("kind", "live stream", "warn")
        if playback.requested:
            self.post_field("requested", playback.requested, "muted")
        if playback.returned:
            self.post_field("returned", playback.returned, "manifest")
        if playback.note:
            self.post_field("note", playback.note, "muted")
        if playback.chapters:
            self.post_field("chapters", chapter_model.summary(playback.chapters), "muted")

        if not playback.manifest_url and not playback.inline_manifest and playback.json_manifest is None:
            self.post_log("playback has no manifest input", "warning")
            return SKIPPED, "no manifest input"

        # Parse before licensing: the manifest is what reveals the KIDs, and
        # knowing them lets the vault answer without a license exchange.
        try:
            self.post_status(tr("delivery.status.parsing_manifest"))
            tracks = (
                self.engine.parse_tracks(
                    playback,
                    self.settings,
                    service=self.service,
                )
                if playback.merge_manifests or playback.merge_manifest_segments
                else self.engine.parse_tracks(playback, self.settings)
            )
        except Exception as exc:
            self.post_error(
                "Could not read the manifest",
                str(exc),
                "Often a region block or an expired session. Check the proxy "
                "setting, or sign in again from this service's settings.",
            )
            return FAILED, "manifest unreadable"

        # A manifest that cannot be read does not always raise: an input UniDL
        # does not recognise parses to an empty ladder instead. Stopping here is
        # not tidiness - the track picker below would put up a list with nothing
        # in it, which cannot be answered, and the worker would wait on it for
        # ever with the rest of the batch queued behind it.
        if not tracks.streams:
            # Skipped rather than failed, like every other "there is nothing here
            # to fetch" outcome around it: the request worked, the answer was
            # empty. Usually a region block or an expired session handing back a
            # placeholder, which is worth saying out loud.
            self.post_log(
                "the manifest was read and had no tracks in it - often a region "
                "block, or an expired session answering with a placeholder. Sign "
                "in again from this service's menu, or check the proxy setting.",
                "warning",
            )
            return SKIPPED, "manifest had no tracks"

        if self.cancel_requested:
            # asked to stop while the manifest was being read: nothing below this
            # is worth asking about, least of all which tracks to fetch
            self.post_log("cancelled", "warning")
            return CANCELLED, "cancelled"

        # Asked before the licence, not after it. Two of the three answers need a
        # key and one of them needs nothing at all, so asking first is what makes
        # "just show me what is in here" free: no licence request, no vault write,
        # nothing on the service's records. It also means the question arrives while
        # the manifest is fresh rather than after a wait nobody asked for.
        if mode == "ask":
            try:
                wants = presenter.present(
                    ctx.pick(
                        playback.save_name,
                        [
                            Choice("Download it now", "download"),
                            Choice("Just save the command, do not download", "command"),
                            Choice(
                                "Just list the tracks",
                                "list",
                                detail="no licence request, nothing saved",
                            ),
                            Choice(
                                "Save an export file",
                                "export",
                                detail="the manifest, the tracks and the keys, importable without an account or a CDM",
                            ),
                        ],
                        scope=SCOPE_DELIVERY,
                    )
                )
            except Back:
                self.post_log("skipped", "warning")
                return SKIPPED, "declined"
            mode = str(wants or "download")

        if mode == "list":
            # The whole point is that nothing else happens: no licence, no command
            # file, no download. Apply output selection only for the list itself,
            # so the card can say what would have been taken and what the file
            # would have been called.
            try:
                self.engine.select_tracks(playback, self.settings, tracks)
            except Exception as exc:
                self.post_error("Could not select output tracks", str(exc), "Check Track settings.")
                return FAILED, "track selection failed"
            self.post_log(f"tracks: {tracks.summary()}")
            if actual := _actual_video_line(tracks):
                self.post_field("actual", actual, "ok")
            self._name_release(playback, tracks)
            self.app.call_from_thread(self._show_tracks, playback, tracks)
            self.post_status("")
            return DONE, "tracks listed"

        # The normal path licenses the complete parsed inventory before output
        # selection.  A deliberately opt-in compatibility setting defers it to
        # the confirmed output tracks; this is useful for HLS services whose PSSH
        # only appears in selected media playlists.  It is not the meaning of
        # Track output selection and never changes the default behavior.
        inherited = getattr(self.settings, "inherited", None)
        defer_license = bool(
            inherited("license_after_tracks", False)
            if callable(inherited)
            else self.settings.get("license_after_tracks", False)
        )
        if not defer_license:
            try:
                self.post_status(tr("delivery.status.resolving_keys"))
                self.engine.resolve_keys(playback, self.service, tracks, self.settings)
            except Exception as exc:
                # not fatal: the download can still be attempted, and the report
                # below will show that no key was obtained
                self.post_error(
                    "Could not get the content key",
                    str(exc),
                    "Check the CDM on the main screen. An L3 device cannot open some "
                    "streams, and an expired session cannot request a licence.",
                )

        def report_keys() -> None:
            for key in playback.display_keys:
                self.post_field("key", key, "ok")
            if playback.keys:
                for key in playback.keys:
                    self.post_field("key", key, "ok")
            elif playback.drm is not None and playback.drm.hls_key:
                method = (playback.drm.hls_method or "AES_128").replace("_", "-")
                self.post_field("key", f"{method} ready", "ok")
            elif playback.drm is not None and (playback.drm.context.get("apple_music") or {}).get(
                "foothill_context_keys"
            ):
                self.post_field("key", "Apple Music FootHill ready", "ok")
            elif playback.drm is not None and playback.drm.needs_license:
                self.post_field("key", "unavailable", "error")
            elif playback.display_keys:
                return
            else:
                self.post_field("key", "none needed", "dim")

        if not defer_license:
            report_keys()
        else:
            self.post_field("key", "deferred until final track selection", "muted")

        if self.cancel_requested:
            self.post_log("cancelled", "warning")
            return CANCELLED, "cancelled"

        # Only now settle the tracks that UniDL will download/export. This
        # selection is deliberately downstream of DRM and never enters the
        # licence context.
        try:
            self.engine.select_tracks(playback, self.settings, tracks)
        except Exception as exc:
            self.post_error("Could not select output tracks", str(exc), "Check Track settings.")
            return FAILED, "track selection failed"
        self.post_log(f"tracks: {tracks.summary()}")

        interactive = self.settings.get("track_mode") == "interactive"
        if interactive and mode != "command":
            chosen = self._ask_tracks(tracks, presenter, ctx, playback)
            if chosen is None:
                self.post_log("skipped", "warning")
                return SKIPPED, "tracks not chosen"
            tracks.selected = chosen
        elif interactive:
            # said out loud, because the setting was asked for and is not being
            # obeyed here, and silence would look like it had been forgotten
            self.post_log(
                "tracks picked automatically - nothing is being downloaded, so the "
                "command carries the automatic selection. Edit it there, or pick "
                "'download the file' to choose tracks yourself."
            )

        if not tracks.selected:
            self.post_log("nothing selected", "warning")
            return SKIPPED, "no tracks selected"

        if defer_license:
            try:
                self.post_status(tr("delivery.status.resolving_selected_keys"))
                selected_inventory = self.engine.license_inventory(tracks, selected_only=True)
                self.engine.resolve_keys(
                    playback,
                    self.service,
                    tracks,
                    self.settings,
                    license_tracks=selected_inventory,
                )
            except Exception as exc:
                # Keep the existing non-fatal key behavior: the native downloader
                # still receives the plan and its result explains a missing key.
                self.post_error(
                    "Could not get the content key",
                    str(exc),
                    "Check the CDM on the main screen. An L3 device cannot open some "
                    "streams, and an expired session cannot request a licence.",
                )
            report_keys()
            self.post_status("")

        if actual := _actual_video_line(tracks):
            self.post_field("actual", actual, "ok")

        # Now that the selection is settled, the name can say what is in the file.
        # Here rather than where the title was resolved, because resolution and
        # dynamic range are properties of the tracks that were chosen - which is
        # not known until a manifest has been read and, if the user is choosing
        # them, until they have chosen.
        self._name_release(playback, tracks)

        if playback.is_live:
            # Decided here, after the manifest has been read: whether to record at
            # all is an app-wide switch, and what to take out of the replay window
            # can only be asked once it is known how much of one there is.
            while True:
                try:
                    mode = self._live_plan(playback, tracks, mode, presenter, ctx)
                    break
                except _LiveBackToTracks:
                    if not interactive:
                        self.post_log("skipped", "warning")
                        return SKIPPED, "live delivery backed out"
                    # The first live question follows track selection, so its Back
                    # answer must reveal that picker rather than skip the whole job.
                    chosen = self._ask_tracks(tracks, presenter, ctx, playback)
                    if chosen is None:
                        self.post_log("skipped", "warning")
                        return SKIPPED, "tracks not chosen"
                    tracks.selected = chosen
                    if not tracks.selected:
                        self.post_log("nothing selected", "warning")
                        return SKIPPED, "no tracks selected"
                    self.post_log(f"tracks: {tracks.summary()}")
                    if defer_license:
                        # The selected-only compatibility path may already have
                        # populated keys for the old selection.  Remove them before
                        # resolving the newly selected inventory, or resolve_keys
                        # will correctly (but wrongly for this new choice) short-cut.
                        playback.keys = []
                        if playback.drm is not None:
                            playback.drm.context.pop("vault_keys", None)
                        try:
                            self.post_status(tr("delivery.status.resolving_selected_keys"))
                            selected_inventory = self.engine.license_inventory(tracks, selected_only=True)
                            self.engine.resolve_keys(
                                playback,
                                self.service,
                                tracks,
                                self.settings,
                                license_tracks=selected_inventory,
                            )
                        except Exception as exc:
                            self.post_error(
                                "Could not get the content key",
                                str(exc),
                                "Check the CDM on the main screen. An L3 device cannot open some "
                                "streams, and an expired session cannot request a licence.",
                            )
                        report_keys()
                        self.post_status("")
                    if actual := _actual_video_line(tracks):
                        self.post_field("actual", actual, "ok")
                    self._name_release(playback, tracks)
                    # Re-enter the local live stack with the new final track choice.
                    continue
            if mode is None:
                self.post_log("skipped", "warning")
                return SKIPPED, "live window not chosen"

        if mode == "download":
            try:
                self.post_status(tr("delivery.status.preparing_sidecars"))
                self.service.prepare_download(playback, self.post_log)
            except Exception as exc:
                self.post_error(
                    "Could not prepare download sidecars",
                    str(exc),
                    "No media was downloaded. Check the service session or subtitle settings.",
                )
                return FAILED, "sidecar preparation failed"

        # Every run leaves the same two artefacts behind, regardless of mode:
        # the command text file, and the KID:key pairs in the vault.
        command = self.engine.command_for(playback, self.settings, tracks)
        try:
            path = self.engine.export_command(playback, self.settings, tracks, service_id=self.service.ID)
            self.post_log(f"command saved -> {path}", "ok")
        except Exception as exc:
            self.post_log(f"could not save command: {exc}", "warning")

        if mode == "command":
            self.app.call_from_thread(self._show_command, playback, command)
            self.post_status("")
            return DONE, "command saved"

        if mode == "export":
            # After the command file, not instead of it: an export is the portable
            # copy, and the command file is still the local record of this run.
            try:
                written = self.engine.export_document(
                    playback,
                    tracks,
                    service_id=self.service.ID,
                    service_name=self.service.NAME,
                    path=self.export_path,
                )
            except Exception as exc:
                self.post_error(
                    "Could not write the export",
                    str(exc),
                    "The keys are in the log and the vault either way - nothing about this title has been lost.",
                )
                return FAILED, "export not written"
            self.export_path = written
            self.post_log(f"export saved -> {written}", "ok")
            self.app.call_from_thread(self._show_export, written)
            self.post_status("")
            return DONE, "exported"

        # Downloading, so now the native delivery screen: it is where structured
        # progress is painted and where the queue lives.  Back remains navigation;
        # an active transfer is cancelled as the screen returns to its parent.
        self.app.call_from_thread(self._open_delivery, playback, tracks)
        self._set_live_frame_mode(playback.is_live)
        self.post_status(tr("delivery.status.downloading_name", name=playback.save_name))
        if self.delivery is not None:
            self._on_ui(self.delivery.refresh_keys)
        try:
            result = self.engine.run(
                playback,
                self.settings,
                tracks,
                cancel=self.cancel_event,
                pause=self.pause_event,
                service=self.service,
            )
        finally:
            was_live = playback.is_live
            self._set_live_frame_mode(False)
            if was_live:
                # Deliver the clear immediately now that the buffered phase is
                # over, so the final timer interval cannot leave an old recording
                # picture over the finished result.
                self.post_frame([])
        self.post_status("")
        if result.cancelled:
            # not a failure: the user asked for this, so it is not coloured or
            # counted as something that went wrong
            self.post_log("cancelled", "warning")
            self._notify(f"Stopped {playback.save_name}")
            return CANCELLED, "cancelled"
        if result.ok:
            completed = tuple(dict.fromkeys(result.artifacts)) or (result.output_dir,)
            for path in completed:
                self.post_log(f"done -> {path}", "ok")
            self._notify(f"Downloaded {playback.save_name}")
            return DONE, ""

        self.post_error(
            f"Download failed: {playback.save_name}",
            result.failure or f"native downloader exited with code {result.exit_code}.",
            "The log above has the native downloader details. ^l expands it.",
        )
        self._notify(f"{playback.save_name} failed", severity="error")
        return FAILED, f"native downloader exit {result.exit_code}"

    # live ------------------------------------------------------------------
    def _live_plan(
        self,
        playback: Playback,
        tracks,
        mode: str,
        presenter: TextualPresenter,
        ctx: FlowContext,
    ) -> str | None:
        """What "deliver this" means for a live stream. Returns the mode, or None to skip.

        All recording decisions live here, after final track selection and before
        the native recorder starts. App/service settings supply defaults for
        headless runs; the interactive TUI confirms record-vs-command, duration
        and replay-window use against the actual selected stream.
        """
        if mode != "download":
            # An outcome that was never going to run UniDL is untouched by any of
            # this: a command and an export are the same artefact for a live stream
            # as for a film, and only a recording needs to know how much of the
            # stream to take. This used to answer "command" for every mode, so
            # asking for an export of a live channel wrote a command file instead -
            # the one thing that was not asked for.
            return mode
        recording = bool(self.settings.get("live_record", False))
        limit = normalize_live_record_limit(
            playback.live_record_limit or self.settings.get("live_record_limit", "") or ""
        )
        wants_replay = bool(self.settings.get("live_replay", False))
        interactive = bool(getattr(ctx, "interactive", False))
        window: float | None = None

        if not interactive and not recording:
            self.post_field("live", "not recording, saving the command instead", "warn")
            self.post_log("live recording was not selected; saving the command", "warning")
            return "command"

        if interactive:
            # These nested loops are the live-delivery navigation stack.  Back
            # moves one level at a time: duration -> record choice, replay
            # inspection -> duration, replay mode -> inspection, and offset ->
            # replay mode.  Only the outermost choice leaves this plan.
            while True:  # record-vs-command
                try:
                    answer = presenter.present(
                        ctx.pick(
                            f"{playback.save_name}: record this live stream?",
                            [
                                Choice(
                                    "Record it now",
                                    "record",
                                    detail="track selection is final; recording starts after the next choices",
                                ),
                                Choice(
                                    "Save the command only",
                                    "command",
                                    detail="resolve the stream without starting a recorder",
                                ),
                            ],
                            cursor=0 if recording else 1,
                            scope=SCOPE_DELIVERY,
                        )
                    )
                except Back as exc:
                    # The track picker is the previous delivery step.  The caller
                    # catches this private signal and re-opens it instead of
                    # treating the live plan as a completed/declined job.
                    raise _LiveBackToTracks from exc
                recording = answer == "record"
                if not recording:
                    self.post_field("live", "not recording, saving the command instead", "warn")
                    self.post_log("live recording was not selected; saving the command", "warning")
                    return "command"
                duration_answered = False
                while True:  # recording duration
                    try:
                        typed_limit = presenter.present(
                            ctx.text(
                                "How long should this live recording run?",
                                placeholder="00:00:00 for no limit, 3600, or 1h",
                                default=limit or "00:00:00",
                                scope=SCOPE_DELIVERY,
                            )
                        )
                    except Back:
                        # Return to the already answered record-vs-command pick.
                        break
                    duration_answered = True
                    raw_limit = "00:00:00" if typed_limit is None else str(typed_limit)
                    limit = normalize_live_record_limit(raw_limit)
                    # This value is per delivery: do not silently rewrite the
                    # service's saved default just because one match or programme
                    # needed longer.
                    # Keep an explicit zero spelling on Playback.  An empty field
                    # means "inherit the service setting" at the engine boundary;
                    # storing the canonical zero here is what lets a user override
                    # an older non-zero service default with unlimited recording.
                    playback.live_record_limit = limit or "00:00:00"

                    begin_answered = False
                    while True:  # live edge vs replay inspection
                        try:
                            replay_answer = presenter.present(
                                ctx.pick(
                                    "Where should the recording begin?",
                                    [
                                        Choice(
                                            "At the live edge",
                                            "edge",
                                            detail="start with the next available segment",
                                        ),
                                        Choice(
                                            "Inspect the replay / DVR window",
                                            "replay",
                                            detail="measure what the selected tracks still expose",
                                        ),
                                    ],
                                    cursor=1 if wants_replay else 0,
                                    scope=SCOPE_DELIVERY,
                                )
                            )
                        except Back:
                            # Return to the duration field.
                            begin_answered = False
                            break
                        begin_answered = True
                        wants_replay = replay_answer == "replay"
                        if not wants_replay:
                            # An explicit edge choice supersedes a stale replay
                            # mode left on a retried Playback.
                            playback.live_window = None
                            window = None
                            break
                        # Only measured when requested: finding the exact HLS
                        # window can cost a request per selected rendition.
                        window = self.engine.measure_live_window(playback, self.settings, tracks)
                        replay = window >= self.engine.LIVE_WINDOW_FLOOR
                        if not replay:
                            break

                        replay_mode_back = False
                        while True:  # replay mode / optional offset
                            choices = [
                                Choice(
                                    "Record from now, the live edge",
                                    "edge",
                                    detail="what a recorder normally does",
                                ),
                                Choice(
                                    "Record from the start of the replay window",
                                    "start",
                                    detail=f"{_hms(window)} further back, then keep going",
                                ),
                                Choice(
                                    "Take the replay window as it stands, once",
                                    "vod",
                                    detail="finishes on its own, so the length limit does not apply",
                                ),
                                Choice(
                                    "Take a stretch of the window",
                                    "offset",
                                    detail="measured from the window's start",
                                ),
                            ]
                            try:
                                answer = presenter.present(
                                    ctx.pick(
                                        f"{playback.save_name}: {_hms(window)} of this stream is still "
                                        "available to rewind into",
                                        choices,
                                        scope=SCOPE_DELIVERY,
                                    )
                                )
                            except Back:
                                # Return to edge-vs-replay inspection.
                                replay_mode_back = True
                                break
                            chosen = str(answer or "edge")

                            if chosen == "offset":
                                try:
                                    typed = presenter.present(
                                        ctx.text(
                                            "How far into the replay window, measured from its start?",
                                            placeholder="00:30:00, or 00:30:00-01:15:00 for a stretch",
                                            default="00:00:00",
                                            scope=SCOPE_DELIVERY,
                                        )
                                    )
                                except Back:
                                    # Return to the replay-mode picker.
                                    continue
                                start_at, end_at = _split_span(str(typed or ""))
                                if not start_at:
                                    self.post_log(
                                        f"{typed!r} is not a position in the window; recording from the "
                                        "live edge instead",
                                        "warning",
                                    )
                                    chosen = "edge"
                                else:
                                    playback.live_window = LiveWindow("offset", start_at, end_at)
                            if chosen != "offset":
                                playback.live_window = LiveWindow(chosen)
                            self.post_field("live window", playback.live_window.describe(), "warn")
                            return mode
                        if replay_mode_back:
                            # The replay-mode picker was backed out.  Continue the
                            # edge-vs-replay loop, rather than leaving delivery.
                            continue
                        break

                    if not begin_answered:
                        # The begin picker was backed out; ask for the duration
                        # again, retaining the value already typed.
                        continue
                    break
                if not duration_answered:
                    # The duration field was backed out; ask record-vs-command
                    # again instead of returning to the service menu.
                    continue
                break

        # Only measured when requested: finding the exact HLS window can cost a
        # request per selected rendition.
        if window is None:
            window = (
                self.engine.measure_live_window(playback, self.settings, tracks)
                if wants_replay
                else self.engine.live_window_seconds(tracks)
            )
        window = float(window or 0.0)
        replay = window >= self.engine.LIVE_WINDOW_FLOOR

        shape = f"recording up to {limit}" if limit else "recording with no length limit"
        if replay:
            shape += f"  ·  {_hms(window)} of replay available"
        self.post_field("live", shape, "warn")

        if not replay or not wants_replay:
            if wants_replay and not replay:
                self.post_log(
                    "the selected tracks do not expose a usable replay window; recording from the live edge",
                    "warning",
                )
            return mode

        choices = [
            Choice(
                "Record from now, the live edge",
                "edge",
                detail="what a recorder normally does",
            ),
            Choice(
                "Record from the start of the replay window",
                "start",
                detail=f"{_hms(window)} further back, then keep going",
            ),
            Choice(
                "Take the replay window as it stands, once",
                "vod",
                detail="finishes on its own, so the length limit does not apply",
            ),
            Choice(
                "Take a stretch of the window",
                "offset",
                detail="measured from the window's start",
            ),
        ]
        try:
            answer = presenter.present(
                ctx.pick(
                    f"{playback.save_name}: {_hms(window)} of this stream is still available to rewind into",
                    choices,
                    scope=SCOPE_DELIVERY,
                )
            )
        except Back:
            return None
        chosen = str(answer or "edge")

        if chosen == "offset":
            try:
                typed = presenter.present(
                    ctx.text(
                        "How far into the replay window, measured from its start?",
                        placeholder="00:30:00, or 00:30:00-01:15:00 for a stretch",
                        default="00:00:00",
                        scope=SCOPE_DELIVERY,
                    )
                )
            except Back:
                return None
            start_at, end_at = _split_span(str(typed or ""))
            if not start_at:
                self.post_log(
                    f"{typed!r} is not a position in the window; recording from the live edge instead",
                    "warning",
                )
                chosen = "edge"
            else:
                playback.live_window = LiveWindow("offset", start_at, end_at)
        if chosen != "offset":
            playback.live_window = LiveWindow(chosen)

        self.post_field("live window", playback.live_window.describe(), "warn")
        return mode

    def _name_release(self, playback: Playback, tracks: TrackSet) -> None:
        """Add the release half to the save name: ``.1080p.DSNP.WEB-DL.DV-TAG``.

        The queue row is renamed with it, so the row, the log, the command file and
        the file on disk are all one string rather than four nearly-identical ones.
        """
        before = playback.save_name
        release_tag = getattr(self.service, "release_tag", None)
        platform = release_tag() if callable(release_tag) else self.service.tag()
        playback.save_name = naming.with_release(
            before,
            playback.title,
            streams=tracks.selected,
            platform=platform,
            tag=str(self.settings.get("release_tag", "") or ""),
            layout=str(self.settings.inherited("release_template", "") or ""),
        )
        if playback.save_name == before:
            return
        self.post_field("save as", playback.save_name, "fg")
        job = self.jobs[-1] if self.jobs else None
        if job is not None and job.name == before:
            job.name = playback.save_name
            self.render_queue()

    # UI-thread helpers used by _process ------------------------------------
    async def _open_delivery(self, playback: Playback, tracks: TrackSet) -> None:
        """Bring UniDL's screen up, just before it has something to show."""
        screen = await self.delivery_screen()
        screen.describe(playback, tracks)

    async def _present_card(self, panel: Panel) -> None:
        """Put a finished artefact on screen 3 and hold the flow at it. UI thread.

        Shared by the two things a run can end with instead of a file - a command
        and a track listing - because holding is the same act for both: the card is
        the result, and the flow's next question would paint over it.
        """
        await self._close(self.delivery)
        try:
            host = await self.host_for(SCOPE_FLOW)
        except Exception as exc:  # noqa: BLE001 - the log copy still stands
            self.post_log(f"could not show the result: {exc}", "warning")
            return
        if host is None:
            return
        host.show_panel(panel)
        self._card_read.clear()

    async def _show_tracks(self, playback: Playback, tracks: TrackSet) -> None:
        """The track listing, for a run that was only ever going to look."""
        panel = tracks_panel(playback.save_name, tracks)
        for row in panel.as_text():
            self.emit_line(row)
        await self._present_card(panel)

    async def _show_export(self, path: Path) -> None:
        """The export card: what went into the file, and where the file is.

        Read back from disk rather than described from memory, so the card is a
        statement about the file that exists rather than about what was meant to be
        written to it.
        """
        try:
            document = exports.read(path)
        except exports.ExportError as exc:
            self.post_log(f"the export was written but cannot be read back: {exc}", "warning")
            return
        for row in export_panel(path, document).as_text():
            self.emit_line(row)
        await self._present_card(export_panel(path, document))

    async def _show_command(self, playback: Playback, command: str) -> None:
        """Add the finished command to the card, where it will be read. UI thread.

        In the middle of the screen the flow is running on, in a card that can be
        copied out of - because when this is the outcome, the command *is* the
        result. UniDL's screen is closed if a previous job in this session left
        it up: nothing is downloading, so a download screen in front of the answer
        is the thing the user asked us to stop doing.

        One card for the whole run, one numbered row per title. The log keeps a
        copy of each as well, and every one is written to its own file.
        """
        self.emit_line(command)
        if any(
            len(metadata) != len(self.commands)
            for metadata in (
                self.command_audio_metadata,
                self.command_subtitle_metadata,
                self.command_chapter_metadata,
            )
        ):
            # Keep test/retry callers that replace ``commands`` directly from
            # inheriting stale metadata from an earlier run.
            self.command_audio_metadata = []
            self.command_subtitle_metadata = []
            self.command_chapter_metadata = []
        self.commands.append((playback.save_name, command))
        self.command_audio_metadata.append(_audio_metadata_lines(playback))
        self.command_subtitle_metadata.append(_subtitle_reference_lines(playback))
        # A command card must stay bounded for a season whose episodes each have
        # dozens of markers. The command text file itself contains the full list.
        self.command_chapter_metadata.append(_chapter_metadata_lines(playback, limit=6))
        # the whole run, redrawn: a batch adds to this card rather than replacing
        # what it said last time
        await self._present_card(
            command_panel(
                self.commands,
                self.command_audio_metadata,
                self.command_subtitle_metadata,
                self.command_chapter_metadata,
            )
        )

    # -------------------------------------------------------- the command card
    async def hold_for_card(self) -> None:
        """Wait until the user is done with a command card. UI thread.

        Returns immediately when there is no card, which is every other run.
        """
        await self._card_read.wait()

    @property
    def card_waiting(self) -> bool:
        """True while a command card is up and has not been finished with."""
        return not self._card_read.is_set()

    def card_gone(self) -> None:
        """The card left the screen, so nothing is waiting to be read. UI thread.

        Only releases the gate. Leaving, if it was deferred, still waits for the
        user - a card that was taken off the screen by the next step of the same
        run is not the user saying they are finished with the session.
        """
        self._card_read.set()

    def card_read(self) -> bool:
        """The user is done with the card: let the flow move on. UI thread.

        Takes the card off the screen as well as opening the gate, because those are
        the same act. Leaving it up meant the flow's next question was mounted
        *behind* it - a menu on screen 2 with the card still in front - so saying
        "done" appeared to do nothing and took a second Back to get anywhere.

        Returns whether there was a card, so a key binding can tell whether it did
        anything and let the keypress mean what it usually means otherwise.
        """
        if self._card_read.is_set():
            return False
        self._card_read.set()
        for host in list(self.hosts):
            try:
                host.clear_panel()
            except Exception:
                pass
        # Do not collapse the navigation stack here.  The card is one page in the
        # flow; the caller's next Back (or the flow's next ask) decides which
        # immediate parent to reveal.
        self._leave_when_read = False
        return True

    def _notify(self, message: str, severity: str = "information") -> None:
        self._on_ui(lambda: self.app.notify(message, severity=severity, timeout=5))

    def _ask_tracks(
        self,
        tracks: TrackSet,
        presenter: TextualPresenter,
        ctx: FlowContext,
        playback: Playback,
    ) -> list | None:
        chosen_ids = {id(stream) for stream in tracks.selected}
        choices = [
            Choice(
                stream.format_line(),
                stream,
                highlights=_track_highlights(stream),
            )
            for stream in tracks.streams
        ]
        preselected = [index for index, stream in enumerate(tracks.streams) if id(stream) in chosen_ids]
        ask = ctx.pick(
            f"Tracks · {playback.save_name}",
            choices,
            multi=True,
            preselected=preselected,
            hint=tracks.summary(),
            scope=SCOPE_DELIVERY,
            chapters=tuple(playback.chapters),
            lyrics=playback.lyrics,
            preview=AudioPreview.from_playback(playback) if playback.audio_only else None,
        )
        try:
            return presenter.present(ask)
        except Back:
            return None


__all__ = ["SessionController", "TextualPresenter"]
