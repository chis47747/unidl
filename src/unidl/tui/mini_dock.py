"""Floating PiP mini-window dock for background downloads (minimized & scheduled).

Floats on Textual's ``dock`` overlay layer in the bottom-right corner on wide
terminals (with a one-click collapse to a compact corner pill), and degrades to
a single-row bottom strip above KeyBar on narrow/short terminals (< 90x24).
Supports multiple concurrent background downloads — both manually minimized
:class:`SessionController` transfers and in-app scheduled downloads — with
``‹ 1/N ›`` switching, in-place pause/resume for interactive transfers,
attention alerts, and two-click stop protection for active transfers.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from ..core.i18n import cell_width, localize_progress_line, tr
from ..core.terminal_cells import crop as crop_cells
from .bidi import escape_markup
from .session import CANCELLED, DONE, FAILED, RUNNING
from .terminal_progress import format_speed, parse_frame_metrics

DOCK_WIDE_MIN_WIDTH = 90
DOCK_WIDE_MIN_HEIGHT = 24
DOCK_CARD_WIDTH = 48
DOCK_STOP_CONFIRM_WINDOW = 3.5
_SPINNER_FRAMES = "⣾⣽⣻⢿⡿⣟⣯⣷"

_RE_BAR_GLYPHS = re.compile(r"[━─]+")
_RE_SPINNER_GLYPHS = re.compile(f"[{_SPINNER_FRAMES}]+")
_RE_PCT_TOKEN = re.compile(r"(?<!\d)\d{1,3}%")
_RE_SPEED_TOKEN = re.compile(r"\d+(?:\.\d+)?\s*(?:[KMG]?B/s|bps)", re.IGNORECASE)


@dataclass
class ScheduledDockItem:
    """Dock entry representing an active or recently finished scheduled download."""

    job_id: str
    job_name: str
    service_id: str
    started_at: float
    finished_at: float | None = None
    state: str = "running"  # running | done | failed | attention | waiting | stopped
    summary: str = ""
    stage: str = "starting"
    detail: str = ""
    rows: tuple[str, ...] = ()
    item_index: int = 0
    item_total: int = 0
    item_name: str = ""
    version: int = 0
    record_id: str = ""
    attention_code: str = ""  # needs_auth | needs_review | ""


def _safe(text: object) -> str:
    return escape_markup(str(text or ""))


def _clip(text: str, limit: int) -> str:
    raw = str(text or "")
    if limit <= 3 or cell_width(raw) <= limit:
        return raw
    return f"{crop_cells(raw, max(1, limit - 1)).rstrip()}…"


def _bar(percent: int, width: int = 14) -> str:
    clamped = max(0, min(100, int(percent)))
    filled = round((clamped / 100.0) * width)
    return "━" * filled + "─" * (width - filled)


def _compact_row_snippet(row: str, *, strip_metrics: bool) -> str:
    """Strip inline bars/spinners (and duplicate %/speed) for the compact subline."""
    text = localize_progress_line(str(row or "").strip())
    text = _RE_BAR_GLYPHS.sub("  ", text)
    text = _RE_SPINNER_GLYPHS.sub("", text)
    if strip_metrics:
        text = _RE_PCT_TOKEN.sub("  ", text)
        text = _RE_SPEED_TOKEN.sub("  ", text)
    parts = [chunk.strip() for chunk in re.split(r"\s{2,}", text) if chunk.strip()]
    return " · ".join(parts)


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _stage_label(stage: str) -> str:
    defaults = {
        "starting": "Starting…",
        "lookup": "Searching & matching episodes…",
        "tracks": "Parsing tracks…",
        "keys": "Resolving decryption keys…",
        "sidecars": "Preparing subtitles & sidecars…",
        "downloading": "Downloading…",
        "exporting": "Exporting…",
    }
    key = str(stage or "starting").strip().lower()
    return tr(f"schedule.stage.{key}", default=defaults.get(key, key))


def _elapsed_for(item: Any) -> str:
    if isinstance(item, ScheduledDockItem):
        if not item.started_at:
            return ""
        end = item.finished_at if item.finished_at is not None else time.monotonic()
        return _format_duration(end - item.started_at)
    started = getattr(item, "_delivery_started_at", None)
    if started is None:
        return ""
    finished = getattr(item, "_delivery_finished_at", None)
    end = finished if finished is not None else time.monotonic()
    paused_total = float(getattr(item, "_delivery_paused_total", 0.0) or 0.0)
    pause_started = getattr(item, "_delivery_pause_started_at", None)
    if pause_started is not None and finished is None:
        paused_total += max(0.0, end - pause_started)
    return _format_duration(end - started - paused_total)


def _controller_title(item: Any) -> str:
    if isinstance(item, ScheduledDockItem):
        return item.job_name or item.service_id or tr("schedule.title", default="Scheduled")
    pb = getattr(item, "_delivery_playback", None)
    if pb is not None:
        name = str(getattr(pb, "save_name", "") or "").strip()
        if name:
            return name
        title = getattr(pb, "title", None)
        if title is not None and getattr(title, "name", None):
            return str(title.name).strip()
    jobs = item.queue_jobs() if hasattr(item, "queue_jobs") else list(getattr(item, "jobs", ()))
    running = next((j for j in jobs if j.state == RUNNING), None)
    if running is not None and running.name:
        return running.name
    if jobs and jobs[-1].name:
        return jobs[-1].name
    return str(getattr(getattr(item, "service", None), "NAME", "") or "Download")


def _controller_paused(controller: Any) -> bool:
    job_running = bool(getattr(controller, "job_running", False))
    delay_active = bool(getattr(controller, "title_delay_active", False))
    pause_req = bool(getattr(controller, "pause_requested", False))
    batch_paused = bool(getattr(controller, "batch_paused", False))
    return bool(((job_running or delay_active) and pause_req) or (batch_paused and not job_running))


def _controller_state(item: Any) -> str:
    if isinstance(item, ScheduledDockItem):
        return item.state
    if getattr(item, "_pending_minimized_ask", None) is not None:
        return "attention"
    outcome = str(getattr(item, "minimized_outcome", "") or "")
    if outcome in {"done", "failed", "stopped"}:
        return outcome
    if getattr(item, "aborted", False):
        return "stopped"
    if getattr(item, "finished", False):
        jobs = item.queue_jobs() if hasattr(item, "queue_jobs") else list(getattr(item, "jobs", ()))
        if any(j.state == FAILED for j in jobs) or getattr(item, "flow_error", False):
            return "failed"
        if jobs and all(j.state == CANCELLED for j in jobs):
            return "stopped"
        return "done"
    if _controller_paused(item):
        return "paused"
    return "running"


def _dock_item_key(item: Any) -> tuple[str, object]:
    if isinstance(item, ScheduledDockItem):
        return ("sched", item.job_id)
    return ("ctrl", id(item))


class _DockButton(Static):
    """Single-line clickable icon/action control inside MiniDownloadDock."""

    ALLOW_SELECT = False

    def __init__(self, label: str, action: str, *, id: str | None = None, classes: str = "") -> None:
        super().__init__(label, id=id, classes=f"mini-dock-btn {classes}".strip())
        self._dock_action = action

    def on_click(self, event) -> None:
        event.stop()
        for ancestor in self.ancestors_with_self:
            if isinstance(ancestor, MiniDownloadDock):
                ancestor.handle_dock_action(self._dock_action)
                return


class MiniDownloadDock(Vertical):
    """Floating PiP card (wide) / corner pill (folded) / compact bottom strip (narrow)."""

    ALLOW_SELECT = False

    def __init__(self) -> None:
        super().__init__(id="mini-download-dock")
        self._stop_armed: tuple[tuple[str, object], float] | None = None
        self._last_signature: tuple[Any, ...] | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="mini-dock-head"):
            yield Static("", id="mini-dock-title")
            yield _DockButton("‹", "prev", id="mini-dock-prev", classes="mini-dock-nav")
            yield Static("", id="mini-dock-count")
            yield _DockButton("›", "next", id="mini-dock-next", classes="mini-dock-nav")
            yield _DockButton("—", "fold", id="mini-dock-fold")
            yield _DockButton("↗", "restore", id="mini-dock-restore")
            yield _DockButton("‖", "pause", id="mini-dock-pause")
            yield _DockButton("✕", "close", id="mini-dock-close")
        with Vertical(id="mini-dock-body"):
            yield Static("", id="mini-dock-progress")
            yield Static("", id="mini-dock-sub")

    def on_mount(self) -> None:
        self.refresh_dock(force=True)
        self.set_interval(0.25, self._tick)

    def on_resize(self) -> None:
        if self.is_mounted:
            self.refresh_dock(force=True)

    def _tick(self) -> None:
        if not self.is_mounted:
            return
        self.refresh_dock()

    def on_click(self, event) -> None:
        """Clicking the card body restores/opens (or expands when folded into a pill)."""
        event.stop()
        if getattr(self.app, "_mini_dock_collapsed", False):
            self.handle_dock_action("fold")
        else:
            self.handle_dock_action("restore")

    def _items(self) -> list[Any]:
        dock_items = getattr(self.app, "dock_items", None)
        if dock_items is not None:
            return list(dock_items)
        return list(getattr(self.app, "minimized_sessions", ()) or ())

    def _active_item(self, items: list[Any]) -> tuple[Any | None, int]:
        if not items:
            return None, 0
        raw_idx = int(getattr(self.app, "_mini_dock_index", 0) or 0)
        idx = max(0, min(raw_idx, len(items) - 1))
        self.app._mini_dock_index = idx
        return items[idx], idx

    def handle_dock_action(self, action: str) -> None:
        items = self._items()
        if not items:
            self.display = False
            return
        item, idx = self._active_item(items)
        if item is None:
            return
        is_sched = isinstance(item, ScheduledDockItem)
        if action == "prev":
            self.app._mini_dock_index = (idx - 1) % len(items)
            self._stop_armed = None
            self.refresh_dock(force=True)
            return
        if action == "next":
            self.app._mini_dock_index = (idx + 1) % len(items)
            self._stop_armed = None
            self.refresh_dock(force=True)
            return
        if action == "fold":
            self.app._mini_dock_collapsed = not bool(getattr(self.app, "_mini_dock_collapsed", False))
            self.refresh_dock(force=True)
            return
        if action == "restore":
            if is_sched:
                opener = getattr(self.app, "open_scheduled_dock_item", None)
                if callable(opener):
                    opener(item)
            else:
                restore = getattr(self.app, "restore_minimized_session", None)
                if callable(restore):
                    restore(item)
            return
        if action == "pause":
            if not is_sched:
                self._toggle_pause(item)
                self.refresh_dock(force=True)
            return
        if action == "close":
            state = _controller_state(item)
            if is_sched:
                if state != "running":
                    dismiss = getattr(self.app, "dismiss_scheduled_dock_item", None)
                    if callable(dismiss):
                        dismiss(item)
                    return
                key = _dock_item_key(item)
                now = time.monotonic()
                if (
                    self._stop_armed is not None
                    and self._stop_armed[0] == key
                    and (now - self._stop_armed[1]) <= DOCK_STOP_CONFIRM_WINDOW
                ):
                    self._stop_armed = None
                    stop_fn = getattr(self.app, "stop_and_dismiss_scheduled_dock_item", None)
                    if callable(stop_fn):
                        stop_fn(item)
                    return
                self._stop_armed = (key, now)
                name = _clip(_controller_title(item), 32)
                self.app.notify(
                    tr(
                        "mini_dock.confirm_stop",
                        name=name,
                        default=f"Click ✕ again within 3s to stop '{name}'.",
                    ),
                    severity="warning",
                    timeout=3,
                )
                return

            if state in {"done", "failed", "stopped"}:
                dismiss = getattr(self.app, "dismiss_minimized_session", None)
                if callable(dismiss):
                    dismiss(item)
                return
            key = _dock_item_key(item)
            now = time.monotonic()
            if (
                self._stop_armed is not None
                and self._stop_armed[0] == key
                and (now - self._stop_armed[1]) <= DOCK_STOP_CONFIRM_WINDOW
            ):
                self._stop_armed = None
                stop_fn = getattr(self.app, "stop_and_dismiss_minimized_session", None)
                if callable(stop_fn):
                    stop_fn(item)
                return
            self._stop_armed = (key, now)
            name = _clip(_controller_title(item), 32)
            self.app.notify(
                tr(
                    "mini_dock.confirm_stop",
                    name=name,
                    default=f"Click ✕ again within 3s to stop '{name}'.",
                ),
                severity="warning",
                timeout=3,
            )

    def _toggle_pause(self, ctrl: Any) -> None:
        if getattr(ctrl, "batch_paused", False) and not getattr(ctrl, "job_running", False):
            resume_batch = getattr(ctrl, "request_resume_batch", None)
            if callable(resume_batch):
                resume_batch()
            return
        if getattr(ctrl, "job_running", False) or getattr(ctrl, "title_delay_active", False):
            if getattr(ctrl, "pause_requested", False):
                resume_pause = getattr(ctrl, "request_resume_pause", None)
                if callable(resume_pause) and resume_pause():
                    ps = getattr(ctrl, "_delivery_pause_started_at", None)
                    if ps is not None:
                        ctrl._delivery_paused_total = float(
                            getattr(ctrl, "_delivery_paused_total", 0.0) or 0.0
                        ) + max(0.0, time.monotonic() - ps)
                        ctrl._delivery_pause_started_at = None
            else:
                req_pause = getattr(ctrl, "request_pause", None)
                if callable(req_pause) and req_pause():
                    if getattr(ctrl, "_delivery_pause_started_at", None) is None:
                        ctrl._delivery_pause_started_at = time.monotonic()

    def _viewport(self) -> tuple[int, int]:
        scr_size = getattr(self.screen, "size", None)
        app_size = getattr(self.app, "size", None)
        w = int(getattr(scr_size, "width", 0) or getattr(app_size, "width", 0) or 100)
        h = int(getattr(scr_size, "height", 0) or getattr(app_size, "height", 0) or 30)
        return w, h

    def refresh_dock(self, *, force: bool = False) -> None:
        if not self.is_mounted:
            return
        items = self._items()
        if not items:
            self.display = False
            self._last_signature = None
            return
        self.display = True
        item, idx = self._active_item(items)
        assert item is not None
        is_sched = isinstance(item, ScheduledDockItem)
        vw, vh = self._viewport()
        narrow = vw < DOCK_WIDE_MIN_WIDTH or vh < DOCK_WIDE_MIN_HEIGHT
        folded = (not narrow) and bool(getattr(self.app, "_mini_dock_collapsed", False))

        if is_sched:
            version = item.version
            rows = list(item.rows)
            svc_id = item.service_id or "sched"
            total_jobs = int(item.item_total or 0)
            done_jobs = max(0, int(item.item_index or 0) - 1) if total_jobs > 1 else 0
            failed_jobs = 1 if item.state == "failed" else 0
            attention_text = item.summary
            stage = item.stage
            detail = item.detail
        else:
            version, rows = item.frame_snapshot() if hasattr(item, "frame_snapshot") else (0, [])
            svc_id = str(getattr(getattr(item, "service", None), "ID", "") or "dl")
            jobs = item.queue_jobs() if hasattr(item, "queue_jobs") else list(getattr(item, "jobs", ()))
            total_jobs = max(len(jobs), int(getattr(item, "batch_total", 0) or 0))
            done_jobs = sum(1 for j in jobs if j.state == DONE)
            failed_jobs = sum(1 for j in jobs if j.state == FAILED)
            attention_text = str(getattr(item, "needs_attention_text", "") or "")
            stage = ""
            detail = str(getattr(item, "last_status", "") or "")

        percent, speed_bps, _ = parse_frame_metrics(rows, spinner_chars=_SPINNER_FRAMES)
        state = _controller_state(item)
        title_raw = _controller_title(item)
        elapsed = _elapsed_for(item)
        other_attention = any(
            i != idx and _controller_state(other) == "attention"
            for i, other in enumerate(items)
        )
        other_done = any(
            i != idx and _controller_state(other) in {"done", "failed"}
            for i, other in enumerate(items)
        )

        sig = (
            _dock_item_key(item),
            idx,
            len(items),
            vw,
            vh,
            narrow,
            folded,
            version,
            state,
            stage,
            detail,
            title_raw,
            elapsed,
            done_jobs,
            failed_jobs,
            total_jobs,
            attention_text,
            other_attention,
            other_done,
        )
        if not force and sig == self._last_signature:
            return
        self._last_signature = sig

        self.set_class(narrow, "mini-dock-narrow")
        self.set_class(folded, "mini-dock-folded")
        self.set_class(state == "attention", "mini-dock-attention")
        self.set_class(state == "done", "mini-dock-done")
        self.set_class(state == "failed", "mini-dock-failed")
        self.set_class(state in {"paused", "waiting"}, "mini-dock-paused")

        glyph_role = {
            "running": ("▸", "ok"),
            "paused": ("‖", "warn"),
            "waiting": ("●", "warn"),
            "attention": ("⚠", "warn"),
            "done": ("✓", "ok"),
            "failed": ("✗", "bad"),
            "stopped": ("⊘", "muted"),
        }.get(state, ("▸", "accent"))
        glyph, role = glyph_role

        multi = len(items) > 1
        btn_prev = self.query_one("#mini-dock-prev", _DockButton)
        btn_count = self.query_one("#mini-dock-count", Static)
        btn_next = self.query_one("#mini-dock-next", _DockButton)
        btn_fold = self.query_one("#mini-dock-fold", _DockButton)
        btn_restore = self.query_one("#mini-dock-restore", _DockButton)
        btn_pause = self.query_one("#mini-dock-pause", _DockButton)
        body = self.query_one("#mini-dock-body", Vertical)
        title_w = self.query_one("#mini-dock-title", Static)
        prog_w = self.query_one("#mini-dock-progress", Static)
        sub_w = self.query_one("#mini-dock-sub", Static)

        can_pause = (not is_sched) and state in {"running", "paused"}
        btn_pause.display = can_pause and not folded
        btn_pause.update("▶" if state == "paused" else "‖")
        btn_fold.display = not narrow
        btn_fold.update("▢" if folded else "—")
        btn_restore.display = True

        show_pager = multi and not folded
        btn_prev.display = show_pager
        btn_count.display = show_pager
        btn_next.display = show_pager
        if show_pager:
            dot = "[$warn]●[/]" if other_attention else ("[$ok]●[/]" if other_done else "")
            btn_count.update(f"[$dim]{idx + 1}/{len(items)}[/]{dot}")

        sched_prefix = "[$dim]◷[/] " if is_sched else ""
        sched_prefix_w = 2 if is_sched else 0

        if narrow:
            body.display = False
            self.styles.width = "1fr"
            self.styles.offset = (0, 0)
            pct_str = f"{percent}%" if percent is not None else ""
            spd_str = format_speed(speed_bps)
            if state == "attention":
                if is_sched and item.attention_code:
                    att_lbl = tr(f"schedule.status.{item.attention_code}", default=tr("mini_dock.action_short", default="Action needed"))
                else:
                    att_lbl = tr("mini_dock.action_short", default="Action needed")
                status_bit = f"[$warn]{_safe(att_lbl)}[/]"
            elif state == "waiting":
                status_bit = f"[$warn]{_safe(tr('schedule.status.waiting_release', default='Waiting release'))}[/]"
            elif state == "done":
                status_bit = f"[$ok]{_safe(tr('mini_dock.done', default='Done'))}[/]"
            elif state == "failed":
                status_bit = f"[$bad]{_safe(tr('mini_dock.failed', default='Failed'))}[/]"
            elif state == "stopped":
                status_bit = f"[$muted]{_safe(tr('mini_dock.stopped', default='Stopped'))}[/]"
            elif state == "paused":
                status_bit = f"[$warn]{_safe(tr('mini_dock.paused', default='Paused'))} {pct_str}[/]".strip()
            elif percent is not None:
                status_bit = f"[$accent]{_bar(percent, 8)}[/] {pct_str}" + (f" · {spd_str}" if spd_str else "")
            elif is_sched:
                status_bit = f"[$dim]{_safe(_stage_label(stage))}[/]"
            else:
                status_bit = f"[$dim]{_safe(tr('mini_dock.running', default='Downloading…'))}[/]"
            controls_w = (18 if can_pause else 15) + (8 if show_pager else 0) + sched_prefix_w
            name_budget = max(10, vw - controls_w - 30)
            shown_name = _clip(title_raw, name_budget)
            title_w.update(
                f"[${role}]{glyph}[/] {sched_prefix}[$accent]{_safe(svc_id)}[/] [bold $fg2]{_safe(shown_name)}[/]  {status_bit}"
            )
            return

        if folded:
            body.display = False
            count_note = f" ({len(items)})" if multi else ""
            if state == "attention" or other_attention:
                pill_text = f"[$warn]⚠ {_safe(tr('mini_dock.action_short', default='Action needed'))}{count_note}[/]"
                plain_len = cell_width(tr("mini_dock.action_short", default="Action needed")) + len(count_note) + 3
            elif state == "waiting":
                w_lbl = tr("schedule.status.waiting_release", default="Waiting release")
                pill_text = f"[$warn]● {_safe(w_lbl)}{count_note}[/]"
                plain_len = cell_width(w_lbl) + len(count_note) + 3
            elif state == "done":
                pill_text = f"[$ok]✓ {_safe(tr('mini_dock.done', default='Done'))}{count_note}[/]"
                plain_len = cell_width(tr("mini_dock.done", default="Done")) + len(count_note) + 3
            elif state == "failed":
                pill_text = f"[$bad]✗ {_safe(tr('mini_dock.failed', default='Failed'))}{count_note}[/]"
                plain_len = cell_width(tr("mini_dock.failed", default="Failed")) + len(count_note) + 3
            else:
                pct_bit = f" · {percent}%" if percent is not None else ""
                spd = format_speed(speed_bps)
                spd_bit = f" · {spd}" if spd else ""
                base_lbl = tr("mini_dock.pill_label", default="Downloads")
                if not pct_bit and is_sched:
                    stage_short = _clip(_stage_label(stage), 16)
                    extra_bit = f" · {stage_short}"
                    pill_text = f"[${role}]{glyph}[/] [bold $fg2]{_safe(base_lbl)}{count_note}[/][$dim]{_safe(extra_bit)}[/]"
                    plain_len = cell_width(base_lbl) + len(count_note) + cell_width(extra_bit) + 3
                else:
                    pill_text = f"[${role}]{glyph}[/] [bold $fg2]{_safe(base_lbl)}{count_note}[/][$accent]{pct_bit}[/][$dim]{spd_bit}[/]"
                    plain_len = cell_width(base_lbl) + len(count_note) + len(pct_bit) + len(spd_bit) + 3
            title_w.update(pill_text)
            pill_w = max(24, min(48, plain_len + 14))
            self.styles.width = pill_w
            self.styles.offset = (max(0, vw - pill_w - 2), 0)
            return

        # Expanded PiP floating card (wide terminal)
        body.display = True
        card_w = min(DOCK_CARD_WIDTH, max(36, vw - 4))
        self.styles.width = card_w
        self.styles.offset = (max(0, vw - card_w - 2), 0)
        inner_w = max(24, card_w - 4)

        controls_w = (12 if can_pause else 9) + (9 if show_pager else 0) + sched_prefix_w
        head_budget = max(12, inner_w - controls_w - cell_width(svc_id) - 5)
        shown_title = _clip(title_raw, head_budget)
        title_w.update(
            f"[${role}]{glyph}[/] {sched_prefix}[$accent]{_safe(svc_id)}[/] [bold $fg2]{_safe(shown_title)}[/]"
        )

        # Row 1: main progress / status line
        if state == "attention":
            if is_sched and item.attention_code:
                att_head = tr(f"schedule.status.{item.attention_code}", default=tr("mini_dock.action_needed", default="Action needed"))
                att_msg = _clip(f"{att_head} · {attention_text}" if attention_text else att_head, max(14, inner_w - 4))
                prog_w.update(f"[bold $warn]⚠ {_safe(att_msg)}[/]")
                sub_w.update(f"[$dim]{_safe(tr('mini_dock.schedule_click_open', default='Click card or ↗ to open scheduled downloads'))}[/]")
            else:
                att_msg = _clip(attention_text or tr("mini_dock.action_needed", default="Action needed"), max(14, inner_w - 4))
                prog_w.update(f"[bold $warn]⚠ {_safe(att_msg)}[/]")
                sub_w.update(f"[$dim]{_safe(tr('mini_dock.click_to_handle', default='Click card or ↗ to respond'))}[/]")
            return

        if state == "waiting":
            wait_lbl = tr("schedule.status.waiting_release", default="Waiting release")
            el_bit = f" · {elapsed}" if elapsed else ""
            prog_w.update(f"[bold $warn]● {_safe(wait_lbl)}{_safe(el_bit)}[/]")
            hint = _clip(item.summary or tr("mini_dock.click_done_hint", default="Click ↗ to view details · ✕ to close"), inner_w)
            sub_w.update(f"[$dim]{_safe(hint)}[/]")
            return

        if state == "done":
            done_lbl = tr("mini_dock.done", default="Download complete")
            tail_bits = [b for b in (f"{done_jobs}/{total_jobs}" if (not is_sched and total_jobs > 1) else "", elapsed) if b]
            tail = f" · {' · '.join(tail_bits)}" if tail_bits else ""
            prog_w.update(f"[bold $ok]✓ {_safe(done_lbl)}{_safe(tail)}[/]")
            sub_w.update(f"[$dim]{_safe(tr('mini_dock.click_done_hint', default='Click ↗ to view details · ✕ to close'))}[/]")
            return

        if state == "failed":
            if is_sched:
                fail_lbl = tr("mini_dock.failed", default="Failed")
                el_bit = f" · {elapsed}" if elapsed else ""
                prog_w.update(f"[bold $bad]✗ {_safe(fail_lbl)}{_safe(el_bit)}[/]")
                sub_msg = _clip(item.summary or tr("mini_dock.click_failed_hint", default="Click ↗ to inspect or retry · ✕ to close"), inner_w)
                sub_w.update(f"[$dim]{_safe(sub_msg)}[/]")
            else:
                fail_lbl = tr("mini_dock.failed_count", failed=max(1, failed_jobs), total=max(1, total_jobs), default=f"Failed ({max(1, failed_jobs)}/{max(1, total_jobs)})")
                prog_w.update(f"[bold $bad]✗ {_safe(fail_lbl)}[/]")
                sub_w.update(f"[$dim]{_safe(tr('mini_dock.click_failed_hint', default='Click ↗ to inspect or retry · ✕ to close'))}[/]")
            return

        if state == "stopped":
            prog_w.update(f"[$muted]⊘ {_safe(tr('mini_dock.stopped', default='Stopped'))}[/]")
            sub_w.update(f"[$dim]{_safe(tr('mini_dock.click_done_hint', default='Click ↗ to view details · ✕ to close'))}[/]")
            return

        if state == "paused":
            pct_bit = f" · {percent}%" if percent is not None else ""
            el_bit = f" · {elapsed}" if elapsed else ""
            prog_w.update(f"[bold $warn]‖ {_safe(tr('mini_dock.paused', default='Paused'))}{pct_bit}{el_bit}[/]")
        elif percent is not None:
            bar_w = max(10, min(16, inner_w - 22))
            spd = format_speed(speed_bps)
            bits = [f"{percent}%", spd, elapsed]
            tail = " · ".join(b for b in bits if b)
            prog_w.update(f"[$accent]{_bar(percent, bar_w)}[/] [bold $fg2]{_safe(tail)}[/]")
        else:
            stage_lbl = _stage_label(stage) if is_sched else tr("mini_dock.running", default="Downloading…")
            el_bit = f" · {elapsed}" if elapsed else ""
            prog_w.update(f"[$accent]▸ {_safe(stage_lbl)}{_safe(el_bit)}[/]")

        # Row 2: queue progress + compact track / status snippet
        sub_parts: list[str] = []
        if total_jobs > 1:
            if is_sched:
                current_pos = max(1, min(total_jobs, int(item.item_index or 1)))
            else:
                current_pos = min(total_jobs, done_jobs + (1 if getattr(item, "job_running", False) else 0))
            sub_parts.append(
                tr("mini_dock.queue_pos", current=max(1, current_pos), total=total_jobs, default=f"Queue {max(1, current_pos)}/{total_jobs}")
            )
        if rows:
            snippet = _compact_row_snippet(rows[0], strip_metrics=percent is not None)
            if snippet:
                sub_parts.append(snippet)
        elif is_sched:
            if item.item_name and item.item_name != item.job_name:
                sub_parts.append(item.item_name)
            elif detail:
                sub_parts.append(detail)
        elif detail:
            sub_parts.append(detail)
        default_hint = (
            tr("mini_dock.schedule_click_open", default="Click card or ↗ to open scheduled downloads")
            if is_sched
            else tr("mini_dock.click_restore", default="Click card or ↗ to expand")
        )
        sub_text = _clip(" · ".join(sub_parts) if sub_parts else default_hint, inner_w)
        sub_w.update(f"[$dim]{_safe(sub_text)}[/]")


__all__ = ["MiniDownloadDock", "ScheduledDockItem"]
