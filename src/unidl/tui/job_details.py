"""Lightweight, read-only batch history; never keeps manifests or segment state."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..core.i18n import tr
from .bidi import visual_text
from .chrome import CloseMark

if TYPE_CHECKING:
    from ..core.exports import Entry
    from ..core.playback import Playback


@dataclass(frozen=True)
class JobDetails:
    """Display values, never a Playback, parsed ladder or account credentials."""

    title: str = ""
    save_name: str = ""
    sources: tuple[str, ...] = ()
    drm_system: str = ""
    tracks: tuple[str, ...] = ()
    track_kids: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()
    raw_keys: tuple[str, ...] = ()
    chapters: int = 0
    attachments: int = 0
    from_export: bool = False
    confirmed: bool = False
    outputs: tuple[str, ...] = ()

    @classmethod
    def from_playback(
        cls, playback: Playback, *, previous: JobDetails | None = None,
        tracks: tuple[str, ...] | None = None, track_kids: tuple[str, ...] = (),
        outputs: tuple[str, ...] | None = None,
    ) -> JobDetails:
        old = previous or cls()
        drm = playback.drm
        sources = tuple(dict.fromkeys(
            str(url) for url in (playback.manifest_url, *playback.alternate_manifest_urls) if url
        ))
        if not sources and (playback.json_manifest is not None or playback.inline_manifest):
            sources = old.sources
        return cls(
            title=playback.title.full_label(), save_name=playback.save_name,
            sources=sources, drm_system=str(drm.system or "") if drm is not None else "",
            tracks=old.tracks if tracks is None else tracks,
            track_kids=old.track_kids if tracks is None else track_kids,
            keys=tuple(dict.fromkeys(str(value) for value in (*playback.keys, *playback.display_keys))),
            raw_keys=(str(drm.hls_key),) if drm is not None and drm.hls_key else (),
            chapters=len(playback.chapters), attachments=len(playback.attachments),
            from_export=old.from_export, confirmed=old.confirmed or tracks is not None,
            outputs=old.outputs if outputs is None else outputs,
        )

    @classmethod
    def from_entry(cls, entry: Entry) -> JobDetails:
        """Inspect only saved metadata; do not create a playback or parse JSON."""
        return cls(
            title=entry.title.full_label() if entry.title is not None else entry.label(),
            save_name=entry.save_name,
            sources=tuple(dict.fromkeys(url for url in (
                entry.manifest_url, *entry.alternate_manifest_urls
            ) if url)),
            drm_system=entry.drm_system, tracks=tuple(entry.tracks),
            track_kids=tuple(entry.key_ids()), keys=tuple(entry.keys),
            raw_keys=(str(entry.hls_key),) if entry.hls_key else (),
            chapters=len(entry.chapters), attachments=len(entry.attachments), from_export=True,
        )


def _duration(start: float | None, end: float | None) -> str:
    seconds = max(0, int((time.monotonic() if end is None else end) - start)) if start is not None else 0
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class JobDetailsScreen(ModalScreen[None]):
    """Inspect a job without changing selection, pause, cancellation or licensing."""

    BINDINGS = [
        Binding("escape,x,ctrl+b,b", "close", "Close", show=False),
        Binding("left", "previous", "Previous", show=False),
        Binding("right", "next", "Next", show=False),
        Binding("up", "scroll(-2)", "Up", show=False),
        Binding("down", "scroll(2)", "Down", show=False),
        Binding("pageup", "scroll(-12)", "Page up", show=False),
        Binding("pagedown", "scroll(12)", "Page down", show=False),
    ]

    def __init__(self, controller, job) -> None:
        super().__init__()
        self.controller = controller
        self.job = job
        self._last_title: str | None = None
        self._last_body: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="job-modal"):
            with Horizontal(id="job-modal-head"):
                yield Static("", id="job-modal-title")
                yield CloseMark(id="job-modal-close")
            with VerticalScroll(id="job-modal-scroll"):
                yield Static("", id="job-modal-body", markup=False)
            with Horizontal(id="job-modal-nav"):
                yield Button(f"‹ {tr('modal.previous')}", id="job-previous")
                yield Button(f"{tr('modal.next')} ›", id="job-next")
                yield Button(tr("job.retry"), id="job-retry")
            yield Static(tr("job.close_hint"), id="job-modal-hint", markup=False)

    def on_mount(self) -> None:
        self.refresh_details()
        self.query_one("#job-modal-scroll", VerticalScroll).focus()
        self.set_interval(0.5, self.refresh_details)

    def relocalize(self) -> None:
        self._last_title = None
        self._last_body = None
        self.query_one("#job-previous", Button).label = f"‹ {tr('modal.previous')}"
        self.query_one("#job-next", Button).label = f"{tr('modal.next')} ›"
        self.query_one("#job-retry", Button).label = tr("job.retry")
        self.query_one("#job-modal-hint", Static).update(tr("job.close_hint"))
        self.refresh_details()

    def _jobs(self) -> list:
        queue = getattr(self.controller, "queue_jobs", None)
        return queue() if callable(queue) else list(self.controller.jobs)

    def refresh_details(self) -> None:
        job = self.job
        details = getattr(job, "details", JobDetails())
        jobs = self._jobs()
        index = next((index for index, item in enumerate(jobs) if item is job), -1)
        title = Text(tr("job.title"), style=f"bold {self.app.palette.fg}")
        if index >= 0:
            title.append(f"  {index + 1}/{len(jobs)}", style=self.app.palette.dim)
        title_key = f"{title.plain}:{self.app.palette.fg}"
        if title_key != self._last_title:
            self._last_title = title_key
            self.query_one("#job-modal-title", Static).update(title)
        lines = [details.title or job.name, ""]
        def field(key: str, value: object) -> None:
            if value not in ("", None):
                lines.append(f"{tr(key)}  {value}")
        field("job.status", tr(f"job.state.{job.state}", default=job.state))
        if job.note:
            field("job.note", job.note)
        if job.state == "queued":
            lines.extend(("", tr("job.export_pending") if details.from_export else tr("job.pending")))
        started = getattr(job, "started_at", None)
        if started is not None:
            field("job.elapsed", _duration(started, getattr(job, "finished_at", None)))
            field("job.attempt", getattr(job, "attempts", 1))
        download_started = getattr(job, "download_started_at", None)
        if download_started is not None:
            field("job.download_elapsed", _duration(download_started, getattr(job, "download_finished_at", None)))
        field("job.save_name", details.save_name)
        if details.outputs:
            lines.extend(("", tr("job.outputs"), *details.outputs))
        elif job.state != "queued":
            lines.extend(("", tr("job.no_outputs")))
        if details.sources:
            lines.extend(("", tr("job.sources"), *details.sources))
        field("job.drm", details.drm_system)
        if details.tracks:
            lines.extend(("", tr("job.selected_tracks") if details.confirmed else tr("job.export_tracks"), *details.tracks))
        if details.track_kids:
            lines.extend(("", "KID", *details.track_kids))
        if details.keys or details.raw_keys:
            lines.extend(("", tr("job.sensitive")))
            if details.keys:
                lines.extend(("KID:KEY", *details.keys))
            if details.raw_keys:
                lines.extend(("KEY", *details.raw_keys))
        if details.chapters or details.attachments:
            lines.extend(("", tr("job.metadata", chapters=details.chapters, attachments=details.attachments)))
        body = "\n".join(lines)
        selecting = getattr(self, "text_selection", None) is not None
        if body != self._last_body and (not selecting or self._last_body is None):
            self._last_body = body
            self.query_one("#job-modal-body", Static).update(Text(visual_text(body)))
        self.query_one("#job-previous", Button).disabled = index <= 0
        self.query_one("#job-next", Button).disabled = index < 0 or index >= len(jobs) - 1
        retry = self.query_one("#job-retry", Button)
        retry.display = bool(getattr(job, "retryable", False))
        retry.disabled = bool(
            getattr(self.controller, "batch_busy", False)
            or getattr(self.controller, "job_running", False)
            or getattr(self.controller, "_retry_pending", False)
            or getattr(self.controller, "aborted", False)
        )

    def action_scroll(self, delta: int) -> None:
        self.query_one("#job-modal-scroll", VerticalScroll).scroll_relative(y=delta, animate=False)

    def _move(self, step: int) -> None:
        jobs = self._jobs()
        index = next((index for index, job in enumerate(jobs) if job is self.job), -1)
        if index >= 0 and 0 <= index + step < len(jobs):
            self.job = jobs[index + step]
            self._last_body = None
            self.refresh_details()
            self.query_one("#job-modal-scroll", VerticalScroll).scroll_home(animate=False)

    def action_previous(self) -> None:
        self._move(-1)

    def action_next(self) -> None:
        self._move(1)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "job-previous":
            self.action_previous()
        elif event.button.id == "job-next":
            self.action_next()
        elif event.button.id == "job-retry" and getattr(self.job, "retryable", False):
            if not self.query_one("#job-retry", Button).disabled:
                job = self.job
                self.dismiss(None)
                started = self.controller.retry([job])
                if started:
                    self.app.notify(tr("queue.retrying_name", name=job.name), timeout=3)
                else:
                    self.app.notify(tr("queue.retry_busy"), timeout=3)

    def action_close(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.action_close()
        return True
