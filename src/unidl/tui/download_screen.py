"""Screen 4: UniDL downloading.

One purpose, and it is in the name: this screen exists because something is being
fetched. It is pushed at the moment the download starts and it carries UniDL's
own output and the queue behind it.  Back returns to the page that launched the
delivery; an active transfer is cancelled as part of that navigation.

It used to be "delivery" in a wider sense - either downloading, or showing the
command we would have run instead - so it was opened for every job and headed
"nothing will be downloaded" when there was nothing to download. A saved command
is not a download, and it now stays on the screen the flow is running on, in a
card, next to everything else that run reported. See
:meth:`SessionController._show_command`.

The header is plain bold text, same as screen 3 - the hierarchy of block letters
stops at the service screen.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from ..core.i18n import localize_progress_line, tr
from .askhost import AskHost
from .audio import AudioPreview, AudioPreviewWidget
from .bidi import visual_text
from .chrome import Chrome, ChromeButton, StatusChip
from .logline import safe_terminal_text
from .session import (
    CANCELLED,
    DONE,
    FAILED,
    RUNNING,
    SKIPPED,
    _track_highlights,
)

if TYPE_CHECKING:
    from ..core.engine import TrackSet
    from ..core.playback import Playback

#: state -> (glyph, palette role). A glyph as well as a colour, so the states are
#: still distinguishable where colour is not.
STATE_MARK = {
    RUNNING: ("▸", "accent"),
    DONE: ("✓", "ok"),
    FAILED: ("✗", "error"),
    SKIPPED: ("·", "warn"),
    CANCELLED: ("⊘", "muted"),
}

BAR_WIDTH = 12

# Download progress is state, not a high-frame-rate animation. Five paints per
# second keeps counters current while coalescing bursts from VOD workers and live
# recorders. This also leaves enough UI time for resize and input events.
FRAME_SECONDS = 0.2
SPINNER_FRAMES = "⣾⣽⣻⢿⡿⣟⣯⣷"


def _elapsed_text(seconds: float) -> str:
    """Format the whole delivery clock without tying it to a mux status row."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _styled_track_text(stream: object, palette) -> Text:
    """A default-colour row with colour applied only to format badges."""
    format_line = getattr(stream, "format_line", None)
    value = visual_text(str(format_line() if callable(format_line) else stream))
    result = Text(value, style=palette.fg)
    occupied: list[tuple[int, int]] = []
    for term, role in sorted(
        _track_highlights(stream), key=lambda item: len(item[0]), reverse=True
    ):
        pattern = (
            rf"(?<![A-Za-z0-9_-]){re.escape(term)}"
            rf"(?![A-Za-z0-9_-])"
        )
        for match in re.finditer(pattern, value, flags=re.IGNORECASE):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in occupied):
                continue
            colour = getattr(palette, role.replace("-", "_"), palette.fg)
            result.stylize(f"bold {colour}", *span)
            occupied.append(span)
    return result


def _styled_progress_text(
    rows: list[str], palette, *, spinner_frame: str | None = None
) -> Text:
    """Render progress bars with theme-aware filled and empty segments.

    The native progress contract intentionally contains plain text, so Core does
    not know about Rich or Textual colours.  At this presentation boundary the
    bar glyphs are unambiguous: active work is the manifest colour, completed
    work is green, and the remaining rail is a quiet structural grey.
    """
    rendered: list[Text] = []
    for row in rows:
        clean = safe_terminal_text(row)
        if spinner_frame:
            clean = "".join(
                spinner_frame if character in SPINNER_FRAMES else character
                for character in clean
            )
        # Completion belongs to the state token, not arbitrary provider text.
        # A save name such as ``Done.100%.mkv`` must not paint an active bar green.
        is_done = bool(
            re.search(
                r"(?<![A-Za-z0-9_])Done(?=\s*(?:✓)?$)",
                clean,
            )
        )
        if not is_done and ("━" in clean or "─" in clean):
            is_done = bool(re.search(r"━+.*\b100%\b", clean))
        clean = localize_progress_line(clean)
        line = Text(clean, style=palette.fg)
        filled_style = palette.ok if is_done else palette.manifest
        for match in re.finditer(r"━+", clean):
            line.stylize(f"bold {filled_style}", *match.span())
        for match in re.finditer(r"─+", clean):
            line.stylize(palette.gutter, *match.span())
        rendered.append(line)
    return Text("\n").join(rendered)


class QueueRow(Static):
    """One queue row, which a failed one can be clicked to run again.

    Clickable rather than focusable: the queue is a report, and giving it focus
    would put a second cursor on a screen that already has one in the log and one
    in whatever is being asked. A row that can be acted on says so on the row.
    """

    def __init__(self, line, *, job, index: int) -> None:
        super().__init__(line, classes="queue-row" + (" retryable" if job.retryable else ""))
        self.job = job
        self.index = index

    def on_click(self, event) -> None:
        if not self.job.retryable:
            return
        event.stop()
        retry = getattr(self.screen, "retry_job", None)
        if callable(retry):
            retry(self.job)

#: How much of a shortened name is worth keeping at the front. The rest of the
#: room goes to the tail, because that is where the release half lives - the
#: resolution and the range are the two things this header is glanced at for, and
#: they are the part a plain end-truncation would throw away.
_HEAD_SHARE = 0.6


def fit(name: str, room: int) -> str:
    """``name`` shortened from the middle to at most ``room`` columns.

    A save name is one unbreakable word, so a header that cannot fit it wraps it
    to a second line - and this header is one line tall, which means the name
    disappears entirely. Rather than let that happen, or give the header a second
    row it only needs on a narrow terminal, drop the middle: both ends stay
    readable and the full name is still on the log line below.
    """
    if room <= 0 or len(name) <= room:
        return name
    if room <= 3:
        return "..."
    keep = room - 1  # the ellipsis is one character
    head = max(1, int(keep * _HEAD_SHARE))
    tail = keep - head
    return f"{name[:head]}…{name[len(name) - tail:]}" if tail else f"{name[:keep]}…"


def _bar(done: int, total: int) -> str:
    """A fixed-width progress bar, because a percentage alone reads as noise."""
    if total <= 0:
        return "░" * BAR_WIDTH
    filled = max(0, min(BAR_WIDTH, round(BAR_WIDTH * done / total)))
    return "█" * filled + "░" * (BAR_WIDTH - filled)


class DownloadScreen(AskHost):
    """What UniDL is doing, while it is doing it."""

    #: the log is visible here, but its compact default still leaves progress
    #: and the resolved stream details readable while work is active
    LOG_CLASSES = "delivery"
    #: the delivery header and the status row already say what is happening, so
    #: a third note in the middle of the screen would just be repetition
    WAITING_TEXT = ""
    BINDINGS = [
        Binding("c", "show_chapters", "Chapters", show=False),
        Binding("l", "show_lyrics", "Lyrics", show=False),
    ]

    def __init__(self, controller) -> None:
        super().__init__(controller)
        #: what the header is currently about, kept so a resize can re-shorten the
        #: name for the new width. Cleared once the run is over, because from then
        #: on the header says how it went rather than what is being fetched.
        self._described: Playback | None = None
        self._described_tracks: TrackSet | None = None
        #: Kept after completion so the Chapters button still opens the result
        #: while the finished screen is being inspected.
        self._chapter_playback: Playback | None = None
        #: Version sampled from SessionController's latest-frame slot. Idle
        #: timer ticks compare this before parsing ANSI or touching a widget.
        self._shown_frame_version = -1
        self._frame_rows = 0
        #: The elapsed clock belongs to the delivery screen, not the transient
        #: mux/decrypt status row. It is reset for each title and frozen on done.
        self._download_started_at: float | None = None
        self._download_finished_at: float | None = None
        self._paused_total = 0.0
        self._pause_started_at: float | None = None
        self._shown_elapsed = ""
        self._animation_frame = 0

    def compose_identity(self) -> ComposeResult:
        yield Static("", id="ident-line")
        yield Static("", id="param-line")
        with Horizontal(id="delivery-head-row"):
            yield Static("", id="delivery-head")
            yield StatusChip(
                "show_chapters", id="delivery-chapters", classes="chapter-chip"
            )
            yield StatusChip(
                "show_lyrics", id="delivery-lyrics", classes="chapter-chip"
            )
        yield Static("", id="delivery-elapsed")
        yield Static("", id="queue-summary")
        yield Static("", id="status-line")

    def chrome_widget(self) -> Chrome:
        """Make stopping a visible control, not only a meaning hidden behind Back."""
        return Chrome(
            can_go_back=self.CAN_GO_BACK,
            left_actions=(
                ("■", "Pause", "screen.stop_resume", "chrome-stop"),
                ("c", "Chapters", "screen.show_chapters", "chrome-chapters"),
                ("l", "Lyrics", "screen.show_lyrics", "chrome-lyrics"),
            ),
        )

    def compose_content(self) -> ComposeResult:
        with Horizontal(id="delivery-main"):
            with Vertical(id="delivery-audio-side"):
                yield AudioPreviewWidget(id="delivery-audio-preview")
            with Vertical(id="delivery-main-left"):
                # The resolved download contract is the main body of this screen.
                # It owns the flexible space above the live progress card, and
                # gives that space back when the log is explicitly expanded.
                with VerticalScroll(id="delivery-details"):
                    yield Static("", id="delivery-details-body")
                yield Static("", id="delivery-frame")
                yield VerticalScroll(id="queue-list")
                yield from self.compose_ask_area()

    def key_hints(self) -> list[tuple[str, str]]:
        """Show Back as navigation; stopping is a side effect while a job runs.

        Back always means one page up.  If a native transfer is still active the
        controller receives a cancellation signal before this screen is popped,
        but the key itself is not repurposed as a second stop/confirm action.

        The two queue keys are only offered when there is something for them to do:
        skipping while nothing runs, or retrying with nothing failed, would be keys
        that answer nothing.
        """
        back = "stopping..." if self.controller.cancel_requested else "back"
        chapters = self.chapter_hints()
        lyrics = self.lyrics_hints()
        log_action = {
            "normal": "expand the log",
            "tall": "collapse the log",
            "collapsed": "show the log",
        }.get(self._log_state, "toggle the log")
        return [("^b", back), *self.queue_hints(), *chapters, *lyrics, ("^l", log_action),
                ("drag", "select text to copy it"), ("^s", "settings")]

    def refresh_keys(self) -> None:
        super().refresh_keys()
        chapters = self.query("#chrome-chapters")
        if chapters:
            chapter_button = chapters.first(ChromeButton)
            count = len(self._chapter_playback.chapters) if self._chapter_playback else 0
            chapter_button.display = count > 0
            chapter_button.set_label(
                "c",
                tr("chrome.chapters_n", count=count) if count else "Chapters",
            )
            chapter_button.set_enabled(count > 0)
        lyrics = self.query("#chrome-lyrics")
        if lyrics:
            lyric_button = lyrics.first(ChromeButton)
            count = (
                len(self._chapter_playback.lyrics.lines)
                if self._chapter_playback is not None and self._chapter_playback.lyrics is not None
                else 0
            )
            lyric_button.display = count > 0
            lyric_button.set_label("l", tr("lyrics.lines_n", count=count) if count else "Lyrics")
            lyric_button.set_enabled(count > 0)
        found = self.query("#chrome-stop")
        if not found:
            return
        button = found.first(ChromeButton)
        if self.controller.job_running:
            if self.controller.cancel_requested:
                button.set_label("…", "Stopping")
                button.set_enabled(False)
            elif self.controller.pause_requested:
                button.set_label("▶", "Resume")
                button.set_enabled(True)
            else:
                button.set_label("■", "Pause")
                button.set_enabled(True)
            return
        if self.controller._retry_pending:
            button.set_label("…", "Starting")
            button.set_enabled(False)
            return
        retryable = any(job.retryable for job in self.controller.jobs)
        button.set_label("▶", "Resume" if retryable else "Done")
        button.set_enabled(retryable)

    def action_stop_resume(self) -> None:
        """Pause the active job in place, resume it, or retry a finished row."""
        if self.controller.job_running:
            if self.controller.pause_requested:
                if self.controller.request_resume_pause():
                    self.notify(tr("delivery.notify.resumed"), timeout=4)
                else:
                    self.notify(tr("delivery.notify.already_stopping"), timeout=3)
                self.refresh_keys()
                return
            if self.controller.request_pause():
                self.notify(tr("delivery.notify.paused"), timeout=5)
            else:
                self.notify(tr("delivery.notify.already_stopping"), timeout=3)
            self.refresh_keys()
            return
        job = next(
            (job for job in reversed(self.controller.jobs) if job.retryable),
            None,
        )
        if job is None:
            self.notify(tr("delivery.notify.nothing_resume"), timeout=3)
            return
        if self.controller.retry([job]):
            self.notify(tr("delivery.notify.resuming", name=job.name), timeout=4)
        else:
            self.notify(tr("delivery.notify.wait_job"), timeout=3)
        self.refresh_keys()

    def prepare_quit(self) -> None:
        """A Quit click/escape is also an explicit stop signal for delivery."""
        if self.controller.job_running and not self.controller.cancel_requested:
            self.controller.request_cancel()
            self.notify(tr("delivery.notify.stopping_quit"), timeout=5)
            self.refresh_keys()

    def on_mount(self) -> None:
        super().on_mount()
        chapters_found = self.query("#delivery-chapters")
        if chapters_found:
            chapters_found.first(StatusChip).display = False
        lyrics_found = self.query("#delivery-lyrics")
        if lyrics_found:
            lyrics_found.first(StatusChip).display = False
        self.refresh_keys()
        self.render_queue(self.controller.jobs, self.controller.batch_total)
        # a screen that appears while something is already downloading shows it
        version, rows = self.controller.frame_snapshot()
        self.show_frame(rows)
        self._shown_frame_version = version
        self._frame_rows = len(rows)
        self._render_elapsed()
        self.set_interval(FRAME_SECONDS, self._flush_frame)

    def _theme_changed(self, theme=None) -> None:
        """Repaint the in-place progress card with the new theme colours."""
        super()._theme_changed(theme)
        version, rows = self.controller.frame_snapshot()
        self.show_frame(rows, layout=False)
        self._shown_frame_version = version
        self._frame_rows = len(rows)
        self._render_elapsed()

    # ------------------------------------------------------------- UniDL's own
    def _flush_frame(self) -> None:
        """Paint progress, post-processing animation and elapsed time; UI thread."""
        self._sync_pause_clock()
        version, rows = self.controller.frame_snapshot()
        self._render_elapsed()
        animated = bool(rows) and any(character in SPINNER_FRAMES for row in rows for character in row)
        if version == self._shown_frame_version and not animated:
            return
        # Width is fixed by the panel. With the same row count the live counter
        # text cannot change this widget's geometry, so skip Textual's full layout
        # pass. A track appearing or disappearing still gets a normal layout.
        layout = len(rows) != self._frame_rows
        if animated:
            self._animation_frame = (
                getattr(self, "_animation_frame", 0) + 1
            ) % len(SPINNER_FRAMES)
            self.show_frame(
                rows,
                layout=layout,
                spinner_frame=SPINNER_FRAMES[self._animation_frame],
            )
        else:
            self.show_frame(rows, layout=layout)
        self._shown_frame_version = version
        self._frame_rows = len(rows)

    def show_frame(
        self,
        rows: list[str],
        *,
        layout: bool = True,
        spinner_frame: str | None = None,
    ) -> None:
        """Paint Core's plain structured display, replacing the prior picture."""
        found = self.query("#delivery-frame")
        if not found:
            return
        panel = found.first(Static)
        if not rows:
            panel.display = False
            panel.update("", layout=True)
            return
        body = _styled_progress_text(rows, self.app.palette, spinner_frame=spinner_frame)
        # Make the widget visible before replacing its renderable. This matters
        # when the first frame arrives after the delivery screen mounted hidden,
        # and avoids a blank bordered box on Textual versions that defer refreshes
        # for hidden widgets. Progress text is cheap and must repaint even when
        # the number of rows stays the same but the percentage changes.
        panel.display = True
        panel.update(body, layout=True)

    def _sync_pause_clock(self) -> None:
        """Freeze the elapsed clock while a transfer is paused in place."""
        paused = bool(
            getattr(self.controller, "job_running", False)
            and getattr(self.controller, "pause_requested", False)
        )
        if paused:
            if getattr(self, "_pause_started_at", None) is None:
                self._pause_started_at = time.monotonic()
            return
        started = getattr(self, "_pause_started_at", None)
        if started is None:
            return
        self._paused_total = float(getattr(self, "_paused_total", 0.0) or 0.0)
        self._paused_total += max(0.0, time.monotonic() - started)
        self._pause_started_at = None

    def _render_elapsed(self) -> None:
        # A few framework-level tests exercise the frame sampler with a tiny
        # object created via ``object.__new__``.  There is no widget tree in that
        # mode, and the elapsed decoration must remain presentation-only.
        if not hasattr(self, "_nodes"):
            return
        try:
            found = self.query("#delivery-elapsed")
        except Exception:
            return
        if not found:
            return
        widget = found.first(Static)
        started_at = getattr(self, "_download_started_at", None)
        if started_at is None:
            widget.update("")
            widget.display = False
            self._shown_elapsed = ""
            return
        finished_at = getattr(self, "_download_finished_at", None) or time.monotonic()
        paused_total = float(getattr(self, "_paused_total", 0.0) or 0.0)
        pause_started = getattr(self, "_pause_started_at", None)
        if pause_started is not None:
            paused_total += max(0.0, time.monotonic() - pause_started)
        value = _elapsed_text(finished_at - started_at - paused_total)
        paused = bool(
            getattr(self.controller, "job_running", False)
            and getattr(self.controller, "pause_requested", False)
        )
        shown = f"{'paused' if paused else 'elapsed'}:{value}"
        if shown == getattr(self, "_shown_elapsed", ""):
            return
        self._shown_elapsed = shown
        line = Text()
        line.append(
            f"{tr('delivery.paused')}   " if paused else f"{tr('delivery.elapsed')}  ",
            style=self.app.palette.warn if paused else self.app.palette.dim,
        )
        line.append(value, style=self.app.palette.fg2)
        widget.update(line)
        widget.display = True

    def clear_frame(self) -> None:
        self.show_frame([])
        # A terminal clear can be delivered immediately rather than by the
        # sampler. Mark that version consumed so the next timer tick does not
        # perform the same hide/layout operation a second time.
        version, _rows = self.controller.frame_snapshot()
        self._shown_frame_version = version
        self._frame_rows = 0

    # ------------------------------------------------------------------- queue
    def render_queue(self, jobs: list, total: int | None) -> None:
        """Show where a batch has got to.

        Hidden for a single title: a one-line queue over a one-line job is
        restating the header. It appears once there is more than one thing to
        keep track of, which is exactly when you stop being able to.
        """
        summary = self.query("#queue-summary")
        listing = self.query("#queue-list")
        if not summary or not listing:
            return

        show = len(jobs) > 1 or (total or 0) > 1
        summary.first().display = show
        listing.first().display = show
        if not show:
            return

        palette = self.app.palette
        counts = {state: sum(1 for job in jobs if job.state == state) for state in STATE_MARK}
        finished = counts[DONE] + counts[FAILED] + counts[SKIPPED] + counts[CANCELLED]
        expected = total if total else len(jobs)

        head = Text()
        head.append(_bar(finished, expected), style=palette.accent)
        head.append("  ")
        head.append(
            tr("delivery.queue_done", finished=finished, expected=expected),
            style=palette.fg,
        )
        for state, ident, role in (
            (FAILED, "delivery.queue_failed", "error"),
            (SKIPPED, "delivery.queue_skipped", "warn"),
            (CANCELLED, "delivery.queue_cancelled", "muted"),
        ):
            if counts[state]:
                head.append("  ·  ", style=palette.dim)
                head.append(tr(ident, count=counts[state]), style=getattr(palette, role))
        summary.first(Static).update(head)

        target = listing.first(VerticalScroll)
        target.remove_children()
        rows = []
        for index, job in enumerate(jobs, start=1):
            mark, role = STATE_MARK[job.state]
            line = Text()
            line.append(f"{index:>3} ", style=palette.gutter)
            line.append(f"{mark} ", style=getattr(palette, role, palette.fg))
            line.append(
                visual_text(job.name),
                style=palette.fg if job.state == RUNNING else palette.fg2,
            )
            if job.note:
                line.append(f"  {visual_text(job.note)}", style=palette.dim)
            if job.retryable:
                line.append(f"   {tr('delivery.retry_hint')}", style=palette.accent)
            rows.append(QueueRow(line, job=job, index=index))
        if rows:
            target.mount_all(rows)
            rows[-1].scroll_visible(animate=False)

    # ---------------------------------------------------------------- headline
    def describe(
        self,
        playback: Playback,
        tracks: TrackSet | None = None,
        *,
        reset_clock: bool = True,
    ) -> None:
        """Describe the file, selected streams and DRM material being delivered."""
        found = self.query("#delivery-head")
        if not found:
            return
        palette = self.app.palette
        audio_side = self.query("#delivery-audio-side")
        if audio_side:
            audio_side.first().display = playback.audio_only
            preview = self.query("#delivery-audio-preview")
            if preview:
                preview.first(AudioPreviewWidget).set_preview(
                    AudioPreview.from_playback(playback) if playback.audio_only else None
                )
        widget = found.first(Static)
        lead = tr("delivery.downloading")
        separator = "  ·  "
        chapter_text = ""
        if playback.chapters:
            from ..core.chapters import count_label

            chapter_text = count_label(playback.chapters)
        lyrics_text = (
            tr("lyrics.lines_n", count=len(playback.lyrics.lines))
            if playback.lyrics is not None
            else ""
        )
        chip_room = sum(len(value) + 3 for value in (chapter_text, lyrics_text) if value)
        # content_size is already inside the padding; the fallback is not, so it
        # has to give the 2+2 columns back
        room = widget.content_size.width or max(0, self.size.width - 4)
        name = fit(
            visual_text(playback.save_name),
            room - len(lead) - len(separator) - chip_room,
        )
        head = Text(no_wrap=True, overflow="ellipsis")
        head.append(lead, style=f"bold {palette.ok}")
        head.append(separator, style=palette.dim)
        head.append(name, style=palette.fg)
        widget.update(head)
        chapters_found = self.query("#delivery-chapters")
        if chapters_found:
            chip = chapters_found.first(StatusChip)
            if chapter_text:
                chip.update(chapter_text)
                chip.display = True
            else:
                chip.update("")
                chip.display = False
        lyrics_found = self.query("#delivery-lyrics")
        if lyrics_found:
            chip = lyrics_found.first(StatusChip)
            if lyrics_text:
                chip.update(lyrics_text)
                chip.display = True
            else:
                chip.update("")
                chip.display = False
        details = self.query("#delivery-details-body")
        if details:
            container = self.query("#delivery-details")
            if container:
                container.first(VerticalScroll).display = True
            body = Text()

            def field(label: str, value: str | Text, style) -> None:
                if body:
                    body.append("\n")
                body.append(f"{label:<10}", style=palette.dim)
                if isinstance(value, Text):
                    body.append_text(value)
                else:
                    body.append(value, style=style)

            engine = getattr(self.controller, "engine", None)
            try:
                downloads = engine.save_dir(
                    self.controller.settings,
                    getattr(self.controller.service, "ID", ""),
                )
            except Exception:
                paths = getattr(getattr(engine, "config", None), "paths", None)
                downloads = getattr(paths, "downloads", None)
            output = (
                str(downloads / playback.save_name)
                if downloads is not None
                else playback.save_name
            )
            field(tr("delivery.field.file"), visual_text(output), palette.fg)
            source = playback.manifest_url or tr("delivery.json_generated")
            if tracks is not None:
                manifest = getattr(tracks, "manifest", None)
                request = getattr(manifest, "request", None)
                parsed_source = getattr(request, "source", None)
                if getattr(parsed_source, "json_document", None) is not None:
                    source = (
                        tr("delivery.json_merged")
                        if playback.merge_manifests
                        else tr("delivery.json_generated")
                    )
                elif getattr(parsed_source, "reference", None):
                    source = str(parsed_source.reference)
            field(tr("delivery.field.source"), visual_text(str(source)), palette.manifest)

            if tracks is not None:
                field(tr("delivery.field.selected"), tracks.summary(), palette.fg2)
                for index, stream in enumerate(tracks.selected, start=1):
                    field(
                        tr("delivery.field.stream", index=index),
                        _styled_track_text(stream, palette),
                        palette.fg,
                    )
                    kids = self.controller.engine.downloader.key_ids([stream])
                    if kids:
                        field("KID", ", ".join(kids), palette.warn)
            for pair in playback.keys:
                field("KID:KEY", pair, palette.ok)
            if playback.chapters:
                from ..core.chapters import summary as chapter_summary
                from ..core.chapters import timestamp as chapter_timestamp

                field(tr("delivery.field.chapters"), chapter_summary(playback.chapters), palette.muted)
                if len(playback.chapters) <= 3:
                    for chapter in playback.chapters:
                        label = chapter_timestamp(chapter.start_ms)
                        if chapter.kind:
                            label += f" [{chapter.kind}]"
                        field("", f"{label}  {visual_text(chapter.title)}", palette.fg2)
                else:
                    field(
                        "",
                        tr("chapters.details_hint"),
                        palette.accent,
                    )
            if playback.lyrics is not None:
                timing = tr(f"lyrics.timing.{playback.lyrics.timing}")
                field(
                    tr("lyrics.title"),
                    tr(
                        "lyrics.summary",
                        count=len(playback.lyrics.lines),
                        timing=timing,
                        language=playback.lyrics.language or tr("lyrics.language_unknown"),
                    ),
                    palette.muted,
                )
            details.first(Static).update(body)
        # so a later resize is not left with a name cut for the old width
        self._described = playback
        self._described_tracks = tracks
        self._chapter_playback = playback
        if reset_clock:
            self._download_started_at = time.monotonic()
            self._download_finished_at = None
            self._paused_total = 0.0
            self._pause_started_at = None
            self._shown_elapsed = ""
            self._animation_frame = 0
        self._render_elapsed()
        self.refresh_keys()

    def mark_done(self) -> None:
        """Everything queued has been dealt with. Say so, and how to leave."""
        if self._download_started_at is not None:
            self._download_finished_at = time.monotonic()
            self._render_elapsed()
        self.clear_frame()
        found = self.query("#delivery-head")
        if not found:
            return
        palette = self.app.palette
        jobs = self.controller.jobs
        failed = sum(1 for job in jobs if job.state == FAILED)
        stopped = sum(1 for job in jobs if job.state == CANCELLED)

        head = Text()
        if failed:
            head.append(tr("delivery.finished_errors"), style=f"bold {palette.error}")
            head.append("  ·  ", style=palette.dim)
            head.append(tr("delivery.failed_n", failed=failed, total=len(jobs)), style=palette.fg)
        elif stopped:
            # stopping on request is not an error, and must not be dressed as one
            head.append(tr("delivery.stopped"), style=f"bold {palette.warn}")
            head.append("  ·  ", style=palette.dim)
            head.append(tr("delivery.cancelled_n", stopped=stopped, total=len(jobs)), style=palette.fg)
        else:
            head.append(tr("delivery.finished"), style=f"bold {palette.manifest}")
            if len(jobs) > 1:
                head.append("  ·  ", style=palette.dim)
                head.append(tr("delivery.titles_n", count=len(jobs)), style=palette.fg)
        head.append("  ·  ", style=palette.dim)
        head.append(tr("delivery.back_hint"), style=palette.muted)
        found.first(Static).update(head)
        chapters_found = self.query("#delivery-chapters")
        if chapters_found:
            chapters_found.first(StatusChip).display = False
        lyrics_found = self.query("#delivery-lyrics")
        if lyrics_found:
            lyrics_found.first(StatusChip).display = False
        # the header is no longer about one title, so a resize must leave it alone
        self._described = None
        self._described_tracks = None
        self.refresh_keys()

    def on_resize(self) -> None:
        """Re-shorten the name for the width the window now has."""
        super().on_resize()
        self.controller.update_frame_columns(self.size.width)
        # Repaint the latest picture at the new width even if a transfer happens
        # to be stalled between segment callbacks.
        self._shown_frame_version = -1
        if self._described is not None:
            self.describe(self._described, self._described_tracks, reset_clock=False)

    # -------------------------------------------------------------- navigation
    def back_without_ask(self) -> bool:
        """Cancel an active transfer, then return to the immediate parent page.

        Back is navigation, not a two-stage stop/confirm control.  A running
        native job is asked to stop before this screen is popped; the controller
        keeps the worker and service flow alive long enough to finish cleanup and
        route the next ask to the page underneath.
        """
        if self.controller.job_running:
            if self.controller.request_cancel():
                self.notify(tr("delivery.notify.stopping"), timeout=4)
            else:
                self.notify(tr("delivery.notify.already_stopping"), timeout=3)
        return False  # let the app pop us onto the previous page


__all__ = ["DownloadScreen"]
