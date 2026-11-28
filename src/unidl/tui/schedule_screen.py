"""Scheduled downloads manager, Upcoming Radar, Settings tab, editor, host setup, and run logs."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, OptionList, Select, Static, Tab, Tabs
from textual.widgets.option_list import Option, OptionDoesNotExist
from textual.worker import get_current_worker

from ..core import justwatch as jw
from ..core import schedule_host
from ..core.i18n import cell_width, phrase, setting_value, tr
from ..core.scheduler import (
    ScheduledJob,
    ScheduleProgress,
    ScheduleRunRecord,
    execute_scheduled_job,
    format_countdown,
    format_iso_utc,
    format_local_time,
    parse_run_at,
    preflight_service,
    utc_now,
)
from ..core.terminal_cells import crop as crop_cells
from ..core.upcoming import (
    DEFAULT_UPCOMING_SOURCES,
    SOURCE_LABELS,
    UpcomingItem,
    UpcomingReport,
    fetch_upcoming,
    normalize_upcoming_sources,
)
from .bidi import escape_markup
from .chrome import Chrome, CloseMark, KeyBar, refresh_locale_widgets
from .filterbox import FilterBox, matches
from .input import ClipboardInput as Input
from .justwatch_screen import (
    SearchRegionScreen,
    _cycle_poster_override,
    _poster_mode_for,
    search_region_from,
)
from .justwatch_settings import TmdbApiKeyModal, UpcomingSourcesModal, mask_api_key
from .poster import (
    PosterItem,
    PosterPreviewWidget,
    poster_capability_summary,
    should_show_poster,
)
from .settings_layout import label_width, setting_row, stable_help
from .settings_screen import _ChoiceEditor
from .terminal_progress import format_speed, parse_frame_metrics

_STATUS_BADGE: dict[str, tuple[str, str]] = {
    "scheduled": ("○", "accent"),
    "waiting_release": ("●", "warn"),
    "queued": ("⋯", "manifest"),
    "running": ("▸", "ok"),
    "done": ("✓", "ok"),
    "failed": ("✗", "bad"),
    "needs_auth": ("⚠", "warn"),
    "needs_review": ("?", "warn"),
    "paused": ("Ⅱ", "muted"),
}

_SETTING_KEYS: tuple[str, ...] = (
    "host",
    "sources",
    "tmdb_key",
    "region",
    "installed_only",
    "posters",
)


def _status_label(status: str) -> str:
    return tr(f"schedule.status.{status}", default=status.replace("_", " "))


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


def _format_elapsed(started_at: float) -> str:
    if not started_at or started_at <= 0:
        return ""
    total = max(0, int(time.monotonic() - started_at))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _progress_bar(percent: int, width: int = 16) -> str:
    clamped = max(0, min(100, int(percent)))
    filled = round((clamped / 100.0) * width)
    return "━" * filled + "─" * (width - filled)


def _safe(text: object) -> str:
    return escape_markup(str(text or ""))


def _clip(text: str, limit: int) -> str:
    raw = str(text or "")
    if limit <= 3 or cell_width(raw) <= limit:
        return raw
    return f"{crop_cells(raw, max(1, limit - 1)).rstrip()}…"


def _app_proxy(app: Any) -> str | None:
    config = getattr(app, "config", None)
    globals_scope = getattr(app, "globals", None)
    if config is None or globals_scope is None:
        return None
    raw = str(globals_scope.get("proxy", "") or "").strip()
    if not raw:
        return None
    resolver = getattr(config, "proxy_url", None)
    if callable(resolver):
        return resolver(raw) or None
    return raw


# ----------------------------------------------------------- editor / host / log modals


class _ConfirmScheduleActionModal(ModalScreen[bool]):
    """Confirm destructive schedule actions (delete job / clear history) with Cancel focused."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
    ]

    def __init__(self, title: str, detail: str, confirm_label: str) -> None:
        super().__init__()
        self._title = title
        self._detail = detail
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="resource-confirm-card"):
            with Horizontal(classes="modal-head"):
                yield Static(_safe(self._title), id="resource-confirm-title")
                yield CloseMark(id="resource-confirm-close")
            yield Static(_safe(self._detail), id="resource-confirm-help")
            with Horizontal(id="resource-confirm-actions"):
                yield Button(phrase("Cancel"), id="sched-confirm-cancel")
                yield Button(self._confirm_label, variant="error", id="sched-confirm-ok")

    def on_mount(self) -> None:
        self.query_one("#sched-confirm-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "sched-confirm-ok")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def go_back(self) -> bool:
        self.action_cancel()
        return True


class ScheduleEditorModal(ModalScreen[ScheduledJob | None]):
    """Create or edit one scheduled download job, with video summary banner and Dry-run check."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        job: ScheduledJob | None = None,
        prefill: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._editing = job
        self._prefill = dict(prefill or {})
        self._dry_running = False
        self._preflight_gen = 0

        meta = dict(self._initial("source_meta", {}) or {})
        raw_urls: Any = self._prefill.get("platform_urls")
        if not isinstance(raw_urls, dict):
            raw_json = str(meta.get("platform_urls_json") or "").strip()
            if raw_json:
                try:
                    parsed = json.loads(raw_json)
                    if isinstance(parsed, dict):
                        raw_urls = parsed
                except (ValueError, TypeError):
                    raw_urls = {}
        self._platform_urls: dict[str, str] = (
            {
                str(k).strip().lower(): str(v).strip()
                for k, v in raw_urls.items()
                if str(k).strip() and str(v).strip()
            }
            if isinstance(raw_urls, dict)
            else {}
        )
        init_mode = str(self._initial("input_mode", "url") or "url")
        if init_mode not in {"url", "search"}:
            init_mode = "url"
        init_service = str(self._initial("service_id", "") or "").strip().lower()
        init_target = str(self._initial("target", "") or "").strip()
        prefill_url = str(
            self._prefill.get("direct_url")
            or meta.get("direct_url")
            or (self._platform_urls.get(init_service, "") if init_service else "")
            or ""
        ).strip()
        prefill_search = str(
            self._prefill.get("search_target")
            or meta.get("search_target")
            or self._initial("name", "")
            or ""
        ).strip()
        self._current_input_mode = init_mode
        self._mode_targets: dict[str, str] = {
            "url": init_target if init_mode == "url" else prefill_url,
            "search": init_target if init_mode == "search" else prefill_search,
        }
        self._auto_url = self._mode_targets["url"]

    def _initial(self, key: str, default: Any = "") -> Any:
        if self._editing is not None:
            return getattr(self._editing, key, default)
        return self._prefill.get(key, default)

    def _service_options(self) -> list[tuple[str, str]]:
        services = list(getattr(self.app, "services", ()) or ())
        opts = [(_safe(f"{svc.NAME} ({svc.ID})"), svc.ID) for svc in services]
        current_id = str(self._initial("service_id", "") or "").strip().lower()
        if current_id and not any(sid == current_id for _lbl, sid in opts):
            not_installed = tr("schedule.editor.not_installed", default="not installed")
            opts.insert(0, (_safe(f"{current_id} ({not_installed})"), current_id))
        if not opts:
            opts.append((_safe(tr("schedule.editor.no_services", default="(no services installed)")), ""))
        return opts

    def _context_banner_markup(self) -> str:
        name = str(self._initial("name", "") or "").strip()
        target = str(self._initial("target", "") or "").strip()
        meta = dict(self._initial("source_meta", {}) or {})
        if not (name or target):
            return (
                f"[$muted]{_safe(tr('schedule.editor.new_help', default='Schedule any service by direct URL or search title with automatic episode matching and release retries.'))}[/]"
            )
        media_kind = str(self._initial("media_kind", "auto") or "auto")
        kind_label = (
            tr("schedule.media.show", default="Series")
            if media_kind == "show"
            else (tr("schedule.media.movie", default="Movie") if media_kind == "movie" else "")
        )
        year = str(self._initial("match_year", "") or "").strip()
        platform = str(meta.get("platform_label") or "").strip()
        source = str(meta.get("source") or "").strip()
        ep_expr = str(self._initial("episode_expr", "") or "").strip()
        badges = [bit for bit in (year, kind_label, ep_expr, platform, source) if bit]
        headline = f"[bold $fg2]{_safe(name or target)}[/]"
        if badges:
            headline += f"   [$accent]{_safe(' · '.join(badges))}[/]"
        synopsis = " ".join(str(meta.get("synopsis") or "").strip().split())
        if synopsis:
            if len(synopsis) > 190:
                synopsis = synopsis[:187].rstrip() + "…"
            return f"{headline}\n[$dim]{_safe(synopsis)}[/]"
        return headline

    def compose(self) -> ComposeResult:
        service_opts = self._service_options()
        init_service = str(self._initial("service_id", "") or "").strip().lower()
        if not init_service or not any(v == init_service for _l, v in service_opts):
            init_service = service_opts[0][1]

        init_run_at = str(self._initial("run_at", "") or "")
        if init_run_at:
            init_time_str = format_local_time(init_run_at)
        else:
            init_time_str = format_local_time(format_iso_utc(utc_now() + timedelta(hours=1)))

        title_text = (
            tr("schedule.editor.edit_title", default="Edit scheduled download")
            if self._editing is not None
            else tr("schedule.editor.new_title", default="New scheduled download")
        )

        with Vertical(id="schedule-editor-card"):
            with Horizontal(classes="modal-head"):
                yield Static(title_text, classes="modal-card-title")
                yield CloseMark(id="modal-close")
            with VerticalScroll(id="schedule-editor-scroll"):
                yield Static(self._context_banner_markup(), id="sched-context-banner")

                yield Static(
                    _safe(tr("schedule.editor.section_source", default="1. Service & target")),
                    classes="sched-sec-head sched-sec-first",
                )
                with Horizontal(classes="sched-two-col"):
                    with Vertical(classes="sched-col"):
                        yield Static(tr("schedule.editor.service", default="Service"), classes="sched-field-label sched-label-tight")
                        yield Select(service_opts, value=init_service, allow_blank=False, id="sched-service")
                    with Vertical(classes="sched-col sched-col-last"):
                        yield Static(tr("schedule.editor.input_mode", default="Lookup mode"), classes="sched-field-label sched-label-tight")
                        yield Select(
                            [
                                (tr("schedule.input_mode.url", default="Direct URL / ID"), "url"),
                                (tr("schedule.input_mode.search", default="In-service search"), "search"),
                            ],
                            value=str(self._initial("input_mode", "url") or "url"),
                            allow_blank=False,
                            id="sched-input-mode",
                        )
                yield Static("", id="sched-preflight", classes="sched-preflight")

                yield Static(tr("schedule.editor.target", default="URL or search title"), classes="sched-field-label sched-label-tight")
                yield Input(
                    value=str(self._initial("target", "") or ""),
                    placeholder=tr("schedule.editor.target_ph", default="https://... or title keyword"),
                    id="sched-target",
                )

                with Horizontal(classes="sched-three-col"):
                    with Vertical(classes="sched-col sched-col-wide"):
                        yield Static(tr("schedule.editor.name", default="Display name"), classes="sched-field-label")
                        yield Input(
                            value=str(self._initial("name", "") or ""),
                            placeholder=tr("schedule.editor.name_ph", default="e.g. Nocturne S01E06"),
                            id="sched-name",
                        )
                    with Vertical(classes="sched-col"):
                        yield Static(tr("schedule.editor.media_kind", default="Media type"), classes="sched-field-label")
                        yield Select(
                            [
                                (tr("schedule.media.auto", default="Auto"), "auto"),
                                (tr("schedule.media.movie", default="Movie"), "movie"),
                                (tr("schedule.media.show", default="Series"), "show"),
                            ],
                            value=str(self._initial("media_kind", "auto") or "auto"),
                            allow_blank=False,
                            id="sched-media-kind",
                        )
                    with Vertical(classes="sched-col sched-col-last"):
                        yield Static(tr("schedule.editor.year", default="Year"), classes="sched-field-label")
                        yield Input(
                            value=str(self._initial("match_year", "") or ""),
                            placeholder="2026",
                            id="sched-year",
                        )

                yield Static(
                    _safe(tr("schedule.editor.section_selection", default="2. Content & episodes")),
                    classes="sched-sec-head",
                )
                with Horizontal(classes="sched-two-col"):
                    with Vertical(classes="sched-col"):
                        yield Static(tr("schedule.editor.selection_mode", default="Selection mode"), classes="sched-field-label sched-label-tight")
                        yield Select(
                            [
                                (tr("schedule.selection.movie", default="Movie / single"), "movie"),
                                (tr("schedule.selection.episodes", default="Specific episodes"), "episodes"),
                                (tr("schedule.selection.new_episodes", default="Watch new episodes"), "new_episodes"),
                            ],
                            value=str(self._initial("selection_mode", "movie") or "movie"),
                            allow_blank=False,
                            id="sched-selection-mode",
                        )
                    with Vertical(classes="sched-col sched-col-last"):
                        yield Static(tr("schedule.editor.episodes", default="Episode expression"), classes="sched-field-label sched-label-tight")
                        yield Input(
                            value=str(self._initial("episode_expr", "") or ""),
                            placeholder="S01E06 / S01E01-E05 / all",
                            id="sched-episodes",
                        )

                yield Static(
                    _safe(tr("schedule.editor.section_timing", default="3. Timing, retry window & action")),
                    classes="sched-sec-head",
                )
                yield Static(
                    tr("schedule.editor.run_at", default="Start time (local YYYY-MM-DD HH:MM or +2h)"),
                    classes="sched-field-label sched-label-tight",
                )
                yield Input(value=init_time_str, placeholder="2026-12-04 09:00 or +1h", id="sched-run-at")
                with Horizontal(id="sched-time-presets"):
                    yield Button("+1h", id="preset-1h")
                    yield Button(tr("schedule.preset.tonight", default="Tonight 20:00"), id="preset-tonight")
                    yield Button(tr("schedule.preset.tomorrow", default="Tomorrow 09:00"), id="preset-tomorrow")

                with Horizontal(classes="sched-three-col"):
                    with Vertical(classes="sched-col"):
                        yield Static(tr("schedule.editor.repeat", default="Repeat"), classes="sched-field-label sched-label-tight")
                        yield Select(
                            [
                                (tr("schedule.repeat.once", default="Once"), "once"),
                                (tr("schedule.repeat.daily", default="Daily"), "daily"),
                                (tr("schedule.repeat.weekly", default="Weekly"), "weekly"),
                                (tr("schedule.repeat.watch", default="Watch ongoing"), "watch"),
                            ],
                            value=str(self._initial("repeat", "once") or "once"),
                            allow_blank=False,
                            id="sched-repeat",
                        )
                    with Vertical(classes="sched-col"):
                        yield Static(tr("schedule.editor.retry_window", default="Release retry window"), classes="sched-field-label sched-label-tight")
                        init_win = int(self._initial("retry_window_hours", 12) or 0)
                        win_choices = [0, 6, 12, 24, 48]
                        if init_win not in win_choices:
                            win_choices.append(init_win)
                            win_choices.sort()
                        yield Select(
                            [
                                (
                                    tr("schedule.retry.none", default="No retries")
                                    if h == 0
                                    else tr("schedule.retry.hours", hours=h, default=f"{h} hours"),
                                    h,
                                )
                                for h in win_choices
                            ],
                            value=init_win,
                            allow_blank=False,
                            id="sched-retry-window",
                        )
                    with Vertical(classes="sched-col sched-col-last"):
                        yield Static(tr("schedule.editor.action", default="After resolving"), classes="sched-field-label sched-label-tight")
                        yield Select(
                            [
                                (tr("schedule.action.download", default="Download"), "download"),
                                (tr("schedule.action.export", default="Export portable JSON"), "export"),
                                (tr("schedule.action.command", default="Save command only"), "command"),
                            ],
                            value=str(self._initial("action", "download") or "download"),
                            allow_blank=False,
                            id="sched-action",
                        )

                yield Static("", id="sched-dry-run-box", classes="sched-dry-run-box")

            with Horizontal(classes="modal-card-actions"):
                yield Button(tr("schedule.editor.dry_run", default="Test lookup (Dry-run)"), id="sched-dry-run")
                yield Button(phrase("Save"), variant="primary", id="sched-save")
                yield Button(phrase("Cancel"), id="sched-cancel")

    def on_mount(self) -> None:
        self.query_one("#sched-dry-run-box", Static).display = False
        self._refresh_preflight()
        self.query_one("#sched-target", Input).focus()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "sched-service":
            self._refresh_preflight()
            self._sync_url_for_service(str(event.value or "").strip().lower())
        elif event.select.id == "sched-input-mode":
            self._switch_input_mode(str(event.value or "url"))

    def _switch_input_mode(self, new_mode: str) -> None:
        if new_mode not in {"url", "search"} or new_mode == self._current_input_mode:
            return
        try:
            target_input = self.query_one("#sched-target", Input)
        except Exception:
            self._current_input_mode = new_mode
            return
        self._mode_targets[self._current_input_mode] = target_input.value.strip()
        self._current_input_mode = new_mode
        if new_mode == "url" and not self._mode_targets.get("url"):
            try:
                service_id = str(self.query_one("#sched-service", Select).value or "").strip().lower()
            except Exception:
                service_id = ""
            if service_id and service_id in self._platform_urls:
                self._mode_targets["url"] = self._platform_urls[service_id]
                self._auto_url = self._mode_targets["url"]
        target_input.value = self._mode_targets.get(new_mode, "")

    def _sync_url_for_service(self, service_id: str) -> None:
        if not service_id:
            return
        svc_url = self._platform_urls.get(service_id, "")
        if not svc_url:
            return
        try:
            target_input = self.query_one("#sched-target", Input)
        except Exception:
            target_input = None
        current_url = (
            target_input.value.strip()
            if (self._current_input_mode == "url" and target_input is not None)
            else self._mode_targets.get("url", "").strip()
        )
        # Update only when the user has not manually typed a custom URL.
        if not current_url or current_url == self._auto_url:
            self._mode_targets["url"] = svc_url
            self._auto_url = svc_url
            if self._current_input_mode == "url" and target_input is not None:
                target_input.value = svc_url

    def _refresh_preflight(self) -> None:
        try:
            service_id = str(self.query_one("#sched-service", Select).value or "").strip()
        except Exception:
            return
        self._preflight_gen += 1
        gen = self._preflight_gen
        if not service_id:
            found = self.query("#sched-preflight")
            if found:
                found.first(Static).update("")
            return
        self._run_preflight_worker(service_id, gen)

    @work(thread=True, exclusive=True, group="sched-preflight", exit_on_error=False)
    def _run_preflight_worker(self, service_id: str, gen: int) -> None:
        try:
            ready, code, detail = preflight_service(
                service_id,
                config=self.app.config,
                settings_store=self.app.settings_store,
                globals_scope=self.app.globals,
                service_registry=self.app.registry,
            )
            role = "ok" if ready else ("warn" if code == "needs_auth" else "bad")
            glyph = "✓" if ready else ("⚠" if code == "needs_auth" else "✗")
            text = f"{glyph} {detail}"
        except Exception as exc:  # noqa: BLE001
            role = "bad"
            text = f"✗ {exc}"
        if get_current_worker().is_cancelled:
            return
        self._set_preflight(role, text, gen, service_id)

    def _set_preflight(self, role: str, text: str, gen: int, service_id: str) -> None:
        def _apply() -> None:
            if not self.is_mounted or gen != self._preflight_gen:
                return
            sel = self.query("#sched-service")
            if sel and str(sel.first(Select).value or "").strip() != service_id:
                return
            found = self.query("#sched-preflight")
            if found:
                found.first(Static).update(f"[${role}]{_safe(text)}[/]")

        try:
            self.app.call_from_thread(_apply)
        except RuntimeError:
            pass

    def _build_job(self) -> tuple[ScheduledJob | None, str]:
        service_id = str(self.query_one("#sched-service", Select).value or "").strip().lower()
        name = self.query_one("#sched-name", Input).value.strip()
        input_mode = str(self.query_one("#sched-input-mode", Select).value or "url")
        target = self.query_one("#sched-target", Input).value.strip()
        media_kind = str(self.query_one("#sched-media-kind", Select).value or "auto")
        match_year = self.query_one("#sched-year", Input).value.strip()
        selection_mode = str(self.query_one("#sched-selection-mode", Select).value or "movie")
        episode_expr = self.query_one("#sched-episodes", Input).value.strip()
        run_at_raw = self.query_one("#sched-run-at", Input).value.strip()
        repeat = str(self.query_one("#sched-repeat", Select).value or "once")
        retry_hours = int(self.query_one("#sched-retry-window", Select).value or 0)
        action = str(self.query_one("#sched-action", Select).value or "download")

        try:
            run_at_iso = parse_run_at(run_at_raw, service_id=service_id)
        except ValueError as exc:
            return None, str(exc)

        if not name:
            name = f"{target[:48]} {episode_expr}".strip() if episode_expr else target[:56]

        service_cls = self.app.registry.for_id(service_id) if service_id else None
        if service_cls is not None:
            if input_mode == "url" and not getattr(service_cls, "SUPPORTS_URL", True):
                return None, f"Service '{service_cls.NAME}' does not support direct URL input."
            if input_mode == "search" and not getattr(service_cls, "SUPPORTS_SEARCH", True):
                return None, f"Service '{service_cls.NAME}' does not support in-service search."

        existing = self._editing
        if existing is not None and run_at_raw == format_local_time(existing.run_at):
            # The form displays minutes; an untouched field retains seconds
            # and the original occurrence of an ambiguous local time.
            run_at_iso = existing.run_at
        keep_paused = bool(
            existing is not None and not existing.enabled and existing.status == "paused"
        )
        source_meta = dict(self._initial("source_meta", {}) or {})
        url_candidate = target if input_mode == "url" else self._mode_targets.get("url", "").strip()
        search_candidate = target if input_mode == "search" else self._mode_targets.get("search", "").strip()
        if url_candidate:
            source_meta["direct_url"] = url_candidate
        if search_candidate:
            source_meta["search_target"] = search_candidate
        if self._platform_urls:
            source_meta["platform_urls_json"] = json.dumps(self._platform_urls, ensure_ascii=False)
        job = ScheduledJob(
            id=existing.id if existing is not None else "",
            name=name,
            service_id=service_id,
            input_mode=input_mode,
            target=target,
            media_kind=media_kind,
            match_year=match_year,
            selection_mode=selection_mode,
            episode_expr=episode_expr,
            action=action,
            run_at=run_at_iso,
            repeat=repeat,
            retry_window_hours=retry_hours,
            retry_interval_minutes=existing.retry_interval_minutes if existing is not None else 20,
            enabled=False if keep_paused else True,
            status="paused" if keep_paused else "scheduled",
            next_attempt_at=run_at_iso,
            window_started_at="",
            attempts=0,
            completed_episodes=list(existing.completed_episodes) if existing is not None else [],
            poster_url=str(self._initial("poster_url", "") or ""),
            source_meta=source_meta,
            last_error="",
            last_run_at=existing.last_run_at if existing is not None else "",
            created_at=existing.created_at if existing is not None and existing.created_at else format_iso_utc(utc_now()),
        )
        errors = job.validate()
        if errors:
            return None, " · ".join(errors)
        return job, ""

    def _apply_preset(self, preset_id: str) -> None:
        now_local = datetime.now().astimezone()
        if preset_id == "preset-1h":
            target = now_local + timedelta(hours=1)
        elif preset_id == "preset-tonight":
            target = now_local.replace(hour=20, minute=0, second=0, microsecond=0)
            if target <= now_local:
                target += timedelta(days=1)
        else:
            target = (now_local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        self.query_one("#sched-run-at", Input).value = target.strftime("%Y-%m-%d %H:%M")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id or ""
        if bid in {"preset-1h", "preset-tonight", "preset-tomorrow"}:
            self._apply_preset(bid)
        elif bid == "sched-dry-run":
            self._start_dry_run()
        elif bid == "sched-save":
            self.action_save()
        else:
            self.action_cancel()

    def _start_dry_run(self) -> None:
        if self._dry_running:
            return
        job, err = self._build_job()
        if job is None:
            self.notify(err, severity="warning", timeout=5)
            return
        self._dry_running = True
        box = self.query_one("#sched-dry-run-box", Static)
        box.display = True
        box.update(f"[$dim]{_safe(tr('schedule.editor.dry_running', default='Running test lookup…'))}[/]")
        self._run_dry_worker(job)

    @work(thread=True, exclusive=True, group="sched-dry-run", exit_on_error=False)
    def _run_dry_worker(self, job: ScheduledJob) -> None:
        role = "bad"
        summary = ""
        try:
            result = execute_scheduled_job(
                job,
                config=self.app.config,
                settings_store=self.app.settings_store,
                globals_scope=self.app.globals,
                store=None,
                vault=self.app.vault,
                vault_collection=self.app.vaults,
                service_registry=self.app.registry,
                dry_run=True,
            )
            role = (
                "ok"
                if result.record.status == "dry_run"
                else ("warn" if result.record.status in {"waiting_release", "needs_auth", "needs_review"} else "bad")
            )
            summary = result.record.summary
        except Exception as exc:  # noqa: BLE001
            summary = str(exc) or type(exc).__name__

        def _finish() -> None:
            self._dry_running = False
            if not self.is_mounted:
                return
            box = self.query_one("#sched-dry-run-box", Static)
            box.display = True
            box.update(f"[${role}]{_safe(summary)}[/]")

        try:
            self.app.call_from_thread(_finish)
        except RuntimeError:
            self._dry_running = False

    def action_save(self) -> None:
        job, err = self._build_job()
        if job is None:
            self.notify(err, severity="warning", timeout=5)
            return
        self.dismiss(job)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.action_cancel()
        return True


class ScheduleHostModal(ModalScreen[None]):
    """Install, inspect, or remove OS background scheduler (launchd / systemd / schtasks)."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("ctrl+b", "close", "Back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._status: schedule_host.HostScheduleStatus | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-card", classes="host-modal"):
            with Horizontal(classes="modal-head"):
                yield Static(
                    tr("schedule.host.title", default="Background host scheduler"),
                    classes="modal-card-title",
                )
                yield CloseMark(id="modal-close")
            yield Static(
                f"[$muted]{_safe(tr('schedule.host.help', default='Run scheduled downloads automatically even when UniDL is closed using macOS launchd, Linux systemd, or Windows Task Scheduler.'))}[/]",
                classes="modal-card-help",
            )
            with VerticalScroll(id="host-status-scroll", classes="host-status-box"):
                yield Static(
                    f"[$dim]{_safe(tr('schedule.host.checking', default='Checking background scheduler…'))}[/]",
                    id="host-status-box",
                )
            yield Static(tr("schedule.host.interval", default="Check interval"), classes="sched-field-label")
            yield Select(
                [
                    (tr("schedule.host.every_m", minutes=5, default="Every 5 minutes"), 5),
                    (tr("schedule.host.every_m", minutes=10, default="Every 10 minutes"), 10),
                    (tr("schedule.host.every_m", minutes=15, default="Every 15 minutes"), 15),
                    (tr("schedule.host.every_m", minutes=30, default="Every 30 minutes"), 30),
                ],
                value=5,
                allow_blank=False,
                id="host-interval",
            )
            with Horizontal(classes="modal-card-actions"):
                yield Button(
                    tr("schedule.host.install", default="Install / Update"),
                    variant="primary",
                    id="host-install",
                    disabled=True,
                )
                yield Button(
                    tr("schedule.host.uninstall", default="Uninstall"),
                    id="host-uninstall",
                    disabled=True,
                )
                yield Button(phrase("Close"), id="host-close")

    def on_mount(self) -> None:
        self._probe_status()

    @work(thread=True, exclusive=True, group="sched-host", exit_on_error=False)
    def _probe_status(self) -> None:
        try:
            status = schedule_host.get_host_status(self.app.config)
        except Exception as exc:  # noqa: BLE001
            status = schedule_host.HostScheduleStatus(
                platform="", backend="error", supported=False, installed=False,
                active=False, interval_minutes=5, detail=str(exc),
            )
        try:
            self.app.call_from_thread(self._apply_status, status)
        except RuntimeError:
            pass

    @work(thread=True, exclusive=True, group="sched-host", exit_on_error=False)
    def _install_worker(self, minutes: int) -> None:
        try:
            status = schedule_host.install_host_scheduler(self.app.config, interval_minutes=minutes)
        except Exception as exc:  # noqa: BLE001
            status = schedule_host.HostScheduleStatus(
                platform="", backend="error", supported=True, installed=False,
                active=False, interval_minutes=minutes, detail=str(exc),
            )
        try:
            self.app.call_from_thread(self._apply_status, status, True)
        except RuntimeError:
            pass

    @work(thread=True, exclusive=True, group="sched-host", exit_on_error=False)
    def _uninstall_worker(self) -> None:
        try:
            status = schedule_host.uninstall_host_scheduler(self.app.config)
        except Exception as exc:  # noqa: BLE001
            status = schedule_host.HostScheduleStatus(
                platform="", backend="error", supported=True, installed=True,
                active=False, interval_minutes=5, detail=str(exc),
            )
        try:
            self.app.call_from_thread(self._apply_status, status, True)
        except RuntimeError:
            pass

    def _apply_status(
        self,
        status: schedule_host.HostScheduleStatus,
        notify: bool = False,
    ) -> None:
        if not self.is_mounted:
            return
        self._status = status
        role = "ok" if status.active else ("warn" if status.installed else "muted")
        state_word = (
            tr("schedule.host.active", default="Active")
            if status.active
            else (
                tr("schedule.host.installed", default="Installed")
                if status.installed
                else tr("schedule.host.off", default="Not installed")
            )
        )
        lines = [
            f"[$muted]{_safe(tr('schedule.host.backend', default='Backend'))}:[/] [$fg2]{_safe(status.backend)}[/]  ·  [${role}]{_safe(state_word)}[/]",
        ]
        if status.target_path:
            lines.append(f"[$muted]{_safe(tr('schedule.host.path', default='Path'))}:[/] [$dim]{_safe(status.target_path)}[/]")
        if status.detail:
            lines.append(f"[$muted]{_safe(tr('schedule.host.detail', default='Detail'))}:[/] [$dim]{_safe(status.detail)}[/]")
        self.query_one("#host-status-box", Static).update("\n".join(lines))
        interval_sel = self.query_one("#host-interval", Select)
        if status.installed and status.interval_minutes > 0:
            known = [5, 10, 15, 30]
            if status.interval_minutes not in known:
                known = sorted({*known, status.interval_minutes})
                interval_sel.set_options(
                    [(tr("schedule.host.every_m", minutes=m, default=f"Every {m} minutes"), m) for m in known]
                )
            interval_sel.value = status.interval_minutes
        self.query_one("#host-install", Button).disabled = not status.supported
        self.query_one("#host-uninstall", Button).disabled = not (status.supported and status.installed)
        if notify:
            self.notify(f"{status.backend}: {status.detail}", timeout=4)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id or ""
        if bid == "host-install":
            minutes = int(self.query_one("#host-interval", Select).value or 5)
            self._install_worker(minutes)
        elif bid == "host-uninstall":
            self._uninstall_worker()
        else:
            self.action_close()

    def action_close(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.action_close()
        return True


class ScheduleRunLogModal(ModalScreen[None]):
    """Read-only redacted execution log viewer for one schedule run record."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("ctrl+b", "close", "Back", show=False),
        Binding("ctrl+y", "copy_logs", "Copy", show=False),
    ]

    def __init__(self, record: ScheduleRunRecord) -> None:
        super().__init__()
        self._record = record

    def compose(self) -> ComposeResult:
        rec = self._record
        with Vertical(id="modal-card", classes="run-log-modal"):
            with Horizontal(classes="modal-head"):
                yield Static(
                    f"{_safe(rec.job_name)}  [$dim]{_safe(format_local_time(rec.started_at))}[/]",
                    classes="modal-card-title",
                )
                yield CloseMark(id="modal-close")
            body_lines = [
                f"{tr('schedule.meta.status', default='Status')}: {_status_label(rec.status)}",
                f"{tr('schedule.meta.service', default='Service')}: {rec.service_id}",
                f"{tr('schedule.log.summary', default='Summary')}: {rec.summary}",
            ]
            if rec.episodes:
                body_lines.append(f"{tr('schedule.meta.episodes', default='Episodes')}: {', '.join(rec.episodes)}")
            if rec.artifacts:
                body_lines.append(f"{tr('schedule.log.artifacts', default='Artifacts')}:")
                body_lines.extend(f"  - {item}" for item in rec.artifacts)
            if rec.logs:
                body_lines.append("")
                body_lines.extend(rec.logs)
            with VerticalScroll(id="run-log-scroll"):
                yield Static(_safe("\n".join(body_lines)), id="run-log-text")
            with Horizontal(classes="modal-card-actions"):
                yield Button(tr("schedule.log.copy", default="Copy log"), id="log-copy")
                yield Button(phrase("Close"), variant="primary", id="log-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "log-copy":
            self.action_copy_logs()
        else:
            self.action_close()

    def action_copy_logs(self) -> None:
        text = "\n".join([self._record.summary, *self._record.logs])
        if self.app.copy_text(text) is not False:
            self.notify(tr("schedule.log.copied", default="Execution log copied."), timeout=3)

    def action_close(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.action_close()
        return True


# ------------------------------------------------------------ main manager screen


class ScheduleManagerScreen(Screen[None]):
    """Four-tab scheduled downloads, Upcoming Radar, history, and Settings manager."""

    BINDINGS = [
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("ctrl+f", "app.global_search", "Search", show=False),
        Binding("ctrl+s", "app.global_settings", "Settings", show=False),
        Binding("n", "new_job", "New job", show=False),
        Binding("e", "edit_job", "Edit", show=False),
        Binding("r", "run_now", "Run now", show=False),
        Binding("space", "toggle_enabled", "Enable/Pause", show=False),
        Binding("d", "delete_job", "Delete", show=False),
        Binding("p", "toggle_poster", "Toggle poster", show=False),
        Binding("u", "edit_sources", "Sources", show=False),
        Binding("f", "change_region", "Region", show=False),
        Binding("o", "toggle_supported_only", "Installed only", show=False),
        Binding("g", "open_host", "Background host", show=False),
        Binding("c", "clear_history", "Clear history", show=False),
        Binding("1", "switch_tab('tab-jobs')", "Scheduled", show=False),
        Binding("2", "switch_tab('tab-radar')", "Upcoming", show=False),
        Binding("3", "switch_tab('tab-history')", "History", show=False),
        Binding("4", "switch_tab('tab-settings')", "Settings", show=False),
        Binding("up", "cursor(-1)", "Up", show=False),
        Binding("down", "cursor(1)", "Down", show=False),
        Binding("pageup", "cursor(-10)", "Page up", show=False),
        Binding("pagedown", "cursor(10)", "Page down", show=False),
    ]

    _VALID_TABS = ("tab-jobs", "tab-radar", "tab-history", "tab-settings")

    def __init__(
        self,
        *,
        initial_tab: str = "tab-jobs",
        focus_job_id: str | None = None,
    ) -> None:
        super().__init__()
        self._active_tab = initial_tab if initial_tab in self._VALID_TABS else "tab-jobs"
        self._pending_focus_job_id = str(focus_job_id or "").strip() or None
        self._jobs: list[ScheduledJob] = []
        self._history: list[ScheduleRunRecord] = []
        self._radar_report: UpcomingReport | None = None
        self._radar_rows: list[UpcomingItem | None] = []
        self._radar_loading = False
        self._radar_generation = 0
        self._radar_signature: tuple[tuple[str, ...], str, str] | None = None
        self._tab_highlights: dict[str, int] = {}
        self._selected_job_id: str | None = None
        self._supported_only = False
        self._poster_override: str | None = None
        self._host_status: schedule_host.HostScheduleStatus | None = None

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=True, show_settings=True)
        yield Static(tr("schedule.title", default="Scheduled downloads"), id="masthead")
        yield Tabs(
            Tab(tr("schedule.tab.jobs", default="Scheduled"), id="tab-jobs"),
            Tab(tr("schedule.tab.radar", default="Upcoming radar"), id="tab-radar"),
            Tab(tr("schedule.tab.history", default="History"), id="tab-history"),
            Tab(tr("schedule.tab.settings", default="Settings"), id="tab-settings"),
            id="schedule-tabs",
            active=self._active_tab,
        )
        yield Static("", id="schedule-help")
        with Horizontal(id="statusline"):
            yield Static("", id="crumbs-pill", classes="pill")
        with Vertical(id="schedule-manager-body"):
            with Horizontal(id="schedule-split", classes="poster-split"):
                yield OptionList(id="schedule-list")
                yield PosterPreviewWidget(id="poster-rail")
            yield FilterBox(hint=tr("schedule.radar.filter_hint", default="Filter upcoming titles or platforms…"))
            with HorizontalScroll(id="schedule-manager-actions"):
                yield Button(tr("schedule.btn.new", default="+ New job"), variant="primary", id="sched-act-new")
                yield Button(tr("schedule.btn.edit", default="Edit"), id="sched-act-edit")
                yield Button(tr("schedule.btn.run", default="Run now"), id="sched-act-run")
                yield Button(tr("schedule.btn.pause", default="Pause"), id="sched-act-toggle")
                yield Button(phrase("Delete"), id="sched-act-delete")
                yield Button(tr("schedule.btn.schedule", default="+ Schedule selected"), variant="primary", id="sched-act-schedule")
                yield Button(tr("schedule.btn.refresh", default="Refresh"), id="sched-act-refresh")
                yield Button(tr("schedule.btn.installed_only_off", default="Installed only: Off"), id="sched-act-only")
                yield Button(tr("schedule.btn.view_log", default="View log"), variant="primary", id="sched-act-log")
                yield Button(tr("schedule.btn.rerun", default="Rerun"), id="sched-act-rerun")
                yield Button(tr("schedule.btn.clear_history", default="Clear history"), id="sched-act-clear")
                yield Button(tr("schedule.btn.change", default="Change"), variant="primary", id="sched-act-change")
        yield KeyBar()

    def on_mount(self) -> None:
        self._supported_only = bool(self.app.globals.get("upcoming_installed_only", False))
        self._load_host_status()
        self.refresh_data()
        self.call_after_refresh(self._refresh_on_layout)
        self.set_interval(15.0, self._tick_jobs)
        self.set_interval(1.0, self._tick_live_progress)
        self.query_one("#schedule-list", OptionList).focus()

    def focus_job(self, job_id: str) -> bool:
        """Switch to the Scheduled jobs tab and highlight ``job_id`` if present."""
        wanted = str(job_id or "").strip()
        if not self.is_mounted:
            self._pending_focus_job_id = wanted or None
            return False
        if wanted:
            self._pending_focus_job_id = wanted
        if self._active_tab != "tab-jobs":
            self._active_tab = "tab-jobs"
            tabs = self.query("#schedule-tabs")
            if tabs:
                tabs.first(Tabs).active = "tab-jobs"
        self.refresh_data()
        options = self.query_one("#schedule-list", OptionList)
        options.focus()
        idx = options.highlighted
        return bool(
            wanted
            and idx is not None
            and 0 <= idx < len(self._jobs)
            and self._jobs[idx].id == wanted
        )

    def _tick_jobs(self) -> None:
        """Keep relative countdowns and external host updates fresh while visible."""
        if not self.is_mounted or self.app.screen is not self or self._active_tab != "tab-jobs":
            return
        self.refresh_data()

    def _tick_live_progress(self) -> None:
        """Keep elapsed clocks moving while an in-app scheduled job is actively running."""
        if not self.is_mounted or self.app.screen is not self or self._active_tab != "tab-jobs":
            return
        active_id = getattr(self.app, "_scheduler_active_job_id", None)
        if active_id:
            self.refresh_live_progress(active_id)

    def _refresh_on_layout(self) -> None:
        if not self.is_mounted:
            return
        if self._active_tab == "tab-jobs":
            self._render_jobs()
        elif self._active_tab == "tab-radar":
            self._render_radar(self.query_one(FilterBox).value)
        elif self._active_tab == "tab-history":
            self._render_history()
        else:
            self._render_settings()
        self._sync_poster_layout()
        self._refresh_actions()

    def on_resize(self) -> None:
        if self.is_mounted:
            self._refresh_on_layout()

    def on_screen_suspend(self, _event) -> None:
        found = self.query("#poster-rail")
        if found:
            found.first(PosterPreviewWidget).suspend_graphics()

    def on_screen_resume(self, _event) -> None:
        self._supported_only = bool(self.app.globals.get("upcoming_installed_only", False))
        self._load_host_status()
        self.refresh_data()
        found = self.query("#poster-rail")
        if found:
            found.first(PosterPreviewWidget).resume_graphics()

    def on_unmount(self) -> None:
        self._radar_generation += 1
        self.workers.cancel_group(self, "upcoming-radar")

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self.query_one("#masthead", Static).update(
            tr("schedule.title", default="Scheduled downloads")
        )
        self.query_one("#sched-act-new", Button).label = tr("schedule.btn.new", default="+ New job")
        self.query_one("#sched-act-edit", Button).label = tr("schedule.btn.edit", default="Edit")
        self.query_one("#sched-act-run", Button).label = tr("schedule.btn.run", default="Run now")
        self.query_one("#sched-act-delete", Button).label = phrase("Delete")
        self.query_one("#sched-act-schedule", Button).label = tr("schedule.btn.schedule", default="+ Schedule selected")
        self.query_one("#sched-act-refresh", Button).label = tr("schedule.btn.refresh", default="Refresh")
        self.query_one("#sched-act-log", Button).label = tr("schedule.btn.view_log", default="View log")
        self.query_one("#sched-act-rerun", Button).label = tr("schedule.btn.rerun", default="Rerun")
        self.query_one("#sched-act-clear", Button).label = tr("schedule.btn.clear_history", default="Clear history")
        self.query_one("#sched-act-change", Button).label = tr("schedule.btn.change", default="Change")
        self.query_one(FilterBox).placeholder = tr("schedule.radar.filter_hint", default="Filter upcoming titles or platforms…")
        self.refresh_data()

    def _help_peers(self) -> tuple[str, str, str, str]:
        return (
            f"[$muted]{_safe(tr('schedule.help.jobs', default='Schedule any service by direct URL or in-service search with automatic episode selection and release-window retries.'))}[/]",
            f"[$muted]{_safe(tr('schedule.help.radar', default='Discover upcoming streaming movies, series, and anime from your enabled sources and schedule them in one press.'))}[/]",
            f"[$muted]{_safe(tr('schedule.help.history', default='Recent scheduled runs with redacted logs, matched episodes, and output paths. Press Enter to inspect or r to rerun.'))}[/]",
            f"[$muted]{_safe(tr('schedule.help.settings', default='Configure background host scheduling, upcoming data sources, TMDB API key, region, and poster previews.'))}[/]",
        )

    @work(thread=True, exclusive=True, group="sched-host-probe", exit_on_error=False)
    def _load_host_status(self) -> None:
        try:
            status = schedule_host.get_host_status(self.app.config, probe_runtime=False)
        except Exception:  # noqa: BLE001
            return
        try:
            self.app.call_from_thread(self._apply_host_status, status)
        except RuntimeError:
            pass

    def _apply_host_status(self, status: schedule_host.HostScheduleStatus) -> None:
        if not self.is_mounted:
            return
        self._host_status = status
        self._render_crumbs()
        if self._active_tab == "tab-settings":
            self._render_settings()

    def _current_radar_signature(self) -> tuple[tuple[str, ...], str, str]:
        sources = tuple(
            normalize_upcoming_sources(
                self.app.globals.get("upcoming_sources", DEFAULT_UPCOMING_SOURCES)
            )
        )
        region = search_region_from(self.app.globals)
        tmdb_key = str(self.app.globals.get("tmdb_api_key", "") or "").strip()
        return (sources, region, tmdb_key)

    def refresh_data(self) -> None:
        prev_job_id = self._selected_job_id
        if self._active_tab == "tab-jobs" and self._jobs:
            cur_idx = self.query_one("#schedule-list", OptionList).highlighted
            if cur_idx is not None and 0 <= cur_idx < len(self._jobs):
                prev_job_id = self._jobs[cur_idx].id
        store = self.app.schedule_store
        self._jobs = store.list_jobs()
        if self._pending_focus_job_id and self._active_tab == "tab-jobs":
            wanted = self._pending_focus_job_id
            self._pending_focus_job_id = None
            self._selected_job_id = wanted
            for idx, job in enumerate(self._jobs):
                if job.id == wanted:
                    self._tab_highlights["tab-jobs"] = idx
                    break
        elif prev_job_id and self._active_tab == "tab-jobs":
            self._selected_job_id = prev_job_id
        self._history = store.list_history(limit=100)
        self._update_tab_labels()
        self._render_help_text()
        filter_box = self.query_one(FilterBox)
        filter_box.display = self._active_tab == "tab-radar"
        if self._active_tab == "tab-jobs":
            self._render_jobs()
        elif self._active_tab == "tab-radar":
            sig = self._current_radar_signature()
            if self._radar_report is not None and self._radar_signature != sig:
                self._radar_report = None
            if self._radar_report is None and not self._radar_loading:
                self._start_radar_fetch(render=False)
            self._render_radar(filter_box.value)
        elif self._active_tab == "tab-history":
            self._render_history()
        else:
            self._render_settings()
        self._render_crumbs()
        self._sync_poster_layout()
        self._refresh_actions()
        self._render_keybar()

    def _update_tab_labels(self) -> None:
        pending = sum(1 for j in self._jobs if j.is_active_pending() or j.status == "running")
        tab_jobs = self.query("#tab-jobs")
        if tab_jobs:
            tab_jobs.first(Tab).label = f"{tr('schedule.tab.jobs', default='Scheduled')} ({pending})"
        tab_radar = self.query("#tab-radar")
        if tab_radar:
            tab_radar.first(Tab).label = tr("schedule.tab.radar", default="Upcoming radar")
        tab_hist = self.query("#tab-history")
        if tab_hist:
            tab_hist.first(Tab).label = f"{tr('schedule.tab.history', default='History')} ({len(self._history)})"
        tab_set = self.query("#tab-settings")
        if tab_set:
            tab_set.first(Tab).label = tr("schedule.tab.settings", default="Settings")

    def _render_help_text(self) -> None:
        peers = self._help_peers()
        idx = {
            "tab-jobs": 0,
            "tab-radar": 1,
            "tab-history": 2,
            "tab-settings": 3,
        }.get(self._active_tab, 0)
        self.query_one("#schedule-help", Static).update(stable_help(peers[idx], peers))

    def _host_summary_text(self) -> str:
        st = self._host_status
        if st is not None and st.installed:
            return tr(
                "schedule.setting.host_on",
                backend=st.backend,
                minutes=st.interval_minutes,
                default=f"{st.backend} · every {st.interval_minutes}m",
            )
        return tr("schedule.setting.host_off", default="Not installed · in-app only")

    def _render_crumbs(self) -> None:
        """Render one quiet read-only summary pill matching UniDL's native manager screens."""
        pill = self.query_one("#crumbs-pill", Static)
        if self._active_tab == "tab-jobs":
            text = tr(
                "schedule.crumbs.jobs",
                section=tr("schedule.tab.jobs", default="Scheduled"),
                count=len(self._jobs),
                host=self._host_summary_text(),
                default=f"Scheduled · {len(self._jobs)} jobs · {self._host_summary_text()}",
            )
        elif self._active_tab == "tab-radar":
            sources = normalize_upcoming_sources(
                self.app.globals.get("upcoming_sources", DEFAULT_UPCOMING_SOURCES)
            )
            src_names = " + ".join(SOURCE_LABELS.get(s, s) for s in sources) if sources else tr("value.none")
            region = search_region_from(self.app.globals)
            visible_count = sum(1 for row in self._radar_rows if row is not None)
            text = tr(
                "schedule.crumbs.radar",
                section=tr("schedule.tab.radar", default="Upcoming radar"),
                count=visible_count,
                sources=src_names,
                region=region,
                default=f"Upcoming radar · {visible_count} titles · {src_names} · {region}",
            )
        elif self._active_tab == "tab-history":
            text = tr(
                "schedule.crumbs.history",
                section=tr("schedule.tab.history", default="History"),
                count=len(self._history),
                default=f"History · {len(self._history)} runs",
            )
        else:
            text = tr(
                "schedule.crumbs.settings",
                section=tr("schedule.tab.settings", default="Settings"),
                count=len(_SETTING_KEYS),
                default=f"Settings · {len(_SETTING_KEYS)} options",
            )
        pill.update(f"[$dim]{_safe(text)}[/]")

    def _refresh_actions(self) -> None:
        """Sync the bottom clickable action bar with the active tab and highlighted row."""
        is_jobs = self._active_tab == "tab-jobs"
        is_radar = self._active_tab == "tab-radar"
        is_history = self._active_tab == "tab-history"
        is_settings = self._active_tab == "tab-settings"

        selected_job = self._selected_job() if is_jobs else None
        selected_radar = self._selected_radar_item() if is_radar else None
        has_history = is_history and bool(self._history)

        btn_new = self.query_one("#sched-act-new", Button)
        btn_edit = self.query_one("#sched-act-edit", Button)
        btn_run = self.query_one("#sched-act-run", Button)
        btn_toggle = self.query_one("#sched-act-toggle", Button)
        btn_delete = self.query_one("#sched-act-delete", Button)
        btn_schedule = self.query_one("#sched-act-schedule", Button)
        btn_refresh = self.query_one("#sched-act-refresh", Button)
        btn_only = self.query_one("#sched-act-only", Button)
        btn_log = self.query_one("#sched-act-log", Button)
        btn_rerun = self.query_one("#sched-act-rerun", Button)
        btn_clear = self.query_one("#sched-act-clear", Button)
        btn_change = self.query_one("#sched-act-change", Button)

        for btn in (btn_new, btn_edit, btn_run, btn_toggle, btn_delete):
            btn.display = is_jobs
        btn_new.disabled = False
        btn_edit.disabled = selected_job is None
        btn_run.disabled = selected_job is None
        btn_toggle.disabled = selected_job is None
        btn_delete.disabled = selected_job is None
        if selected_job is not None:
            btn_toggle.label = (
                tr("schedule.btn.pause", default="Pause")
                if selected_job.enabled
                else tr("schedule.btn.enable", default="Enable")
            )
        else:
            btn_toggle.label = tr("schedule.btn.pause", default="Pause")

        for btn in (btn_schedule, btn_refresh, btn_only):
            btn.display = is_radar
        btn_schedule.disabled = selected_radar is None
        btn_refresh.disabled = self._radar_loading
        btn_only.label = (
            tr("schedule.btn.installed_only_on", default="Installed only: On")
            if self._supported_only
            else tr("schedule.btn.installed_only_off", default="Installed only: Off")
        )

        was_focused = self.focused
        for btn in (btn_log, btn_rerun, btn_clear):
            btn.display = is_history
            btn.disabled = not has_history
        if has_history:
            idx = self.query_one("#schedule-list", OptionList).highlighted
            rec = self._history[idx] if (idx is not None and 0 <= idx < len(self._history)) else None
            btn_rerun.disabled = rec is None or self.app.schedule_store.get_job(rec.job_id) is None

        btn_change.display = is_settings
        btn_change.disabled = False
        if isinstance(was_focused, Button) and (was_focused.disabled or not was_focused.display):
            self.query_one("#schedule-list", OptionList).focus()

    def _render_keybar(self) -> None:
        bar = self.query_one(KeyBar)
        if self._active_tab == "tab-jobs":
            bar.render_pairs(
                ("n", tr("schedule.key.new", default="new")),
                ("enter", tr("schedule.key.edit", default="edit")),
                ("r", tr("schedule.key.run", default="run now")),
                ("space", tr("schedule.key.toggle", default="enable/pause")),
                ("d", tr("schedule.key.delete", default="delete")),
                ("p", tr("poster.key_toggle", default="posters")),
                ("^b", "back"),
            )
        elif self._active_tab == "tab-radar":
            bar.render_pairs(
                ("enter", tr("schedule.action.schedule", default="schedule")),
                ("o", tr("schedule.key.installed", default="installed only")),
                ("r", tr("schedule.key.refresh", default="refresh")),
                ("p", tr("poster.key_toggle", default="posters")),
                ("4", tr("schedule.tab.settings", default="settings")),
                ("^b", "back"),
            )
        elif self._active_tab == "tab-history":
            bar.render_pairs(
                ("enter", tr("schedule.key.inspect", default="view log")),
                ("r", tr("schedule.key.rerun", default="rerun")),
                ("c", tr("schedule.key.clear", default="clear")),
                ("^b", "back"),
            )
        else:
            bar.render_pairs(
                ("enter", tr("schedule.btn.change", default="change")),
                ("^b", "back"),
                ("esc", "quit"),
            )

    # -------------------------------------------------------------- list renders

    def _list_inner_width(self) -> int:
        options = self.query_one("#schedule-list", OptionList)
        measured = int(options.content_region.width or 0)
        if measured > 24:
            return max(36, measured - 4)
        total = int(self.size.width or self.app.size.width or 100)
        rail = self.query("#poster-rail")
        rail_w = 44 if (rail and rail.first(PosterPreviewWidget).display and total >= 126) else (
            36 if (rail and rail.first(PosterPreviewWidget).display) else 0
        )
        return max(36, total - rail_w - 8)

    def _job_live_progress(self, job_id: str) -> ScheduleProgress | None:
        getter = getattr(self.app, "get_schedule_progress", None)
        if not callable(getter):
            return None
        return getter(job_id)

    def _format_job_option_prompt(
        self,
        job: ScheduledJob,
        *,
        now: datetime,
        avail: int,
    ) -> str:
        snap = self._job_live_progress(job.id)
        is_running = job.status == "running" or snap is not None
        status_key = (
            "running"
            if is_running
            else (job.status if job.enabled or job.status in {"done", "failed", "needs_auth", "needs_review"} else "paused")
        )
        glyph, role = _STATUS_BADGE.get(status_key, ("○", "muted"))
        status_word = _status_label(status_key)
        if job.episode_expr and job.episode_expr.lower() not in job.name.lower():
            ep_part = f" · {job.episode_expr}"
        elif job.selection_mode == "new_episodes":
            ep_part = f" · {tr('schedule.selection.new_episodes', default='Watch new')}"
        else:
            ep_part = ""
        fixed_cost = 6 + cell_width(glyph) + cell_width(status_word) + cell_width(job.service_id) + cell_width(ep_part)
        title_budget = max(16, avail - fixed_cost)
        title_shown = _clip(job.name, title_budget)
        line1 = (
            f"  [${role}]{glyph} {_safe(status_word)}[/]  "
            f"[bold $fg2]{_safe(title_shown)}[/][$dim]{_safe(ep_part)}[/]  "
            f"[$accent]{_safe(job.service_id)}[/]"
        )

        if is_running and snap is not None:
            percent, speed_bps, _ = parse_frame_metrics(snap.rows)
            elapsed = _format_elapsed(snap.started_at)
            item_prefix = f"{snap.item_index}/{snap.item_total}" if snap.item_total > 1 else ""
            if percent is not None:
                bar = _progress_bar(percent, 12)
                prog_bits = [
                    bit
                    for bit in (item_prefix, f"[{bar}] {percent}%", format_speed(speed_bps), elapsed)
                    if bit
                ]
                line2_text = _clip(" · ".join(prog_bits), max(24, avail - 5))
                line2 = f"     [$ok]{_safe(line2_text)}[/]"
            else:
                stage_txt = _stage_label(snap.stage)
                detail = snap.rows[-1] if snap.rows else snap.last_log
                prog_bits = [bit for bit in (item_prefix, stage_txt, detail, elapsed) if bit]
                line2_text = _clip(" · ".join(prog_bits), max(24, avail - 5))
                line2 = f"     [$accent]{_safe(line2_text)}[/]"
            return f"{line1}\n{line2}"

        due_iso = job.next_attempt_at or job.run_at
        due_str = format_local_time(due_iso)
        countdown = format_countdown(due_iso, now=now) if job.is_active_pending() else ""
        bits = [due_str]
        if countdown:
            bits.append(countdown)
        if job.repeat != "once":
            bits.append(tr(f"schedule.repeat.{job.repeat}", default=job.repeat))
        if job.completed_episodes:
            bits.append(tr("schedule.done_eps", count=len(job.completed_episodes), default=f"{len(job.completed_episodes)} done"))
        if job.last_error:
            bits.append(job.last_error[:60])
        line2_text = _clip(" · ".join(bits), max(24, avail - 5))
        line2 = f"     [$dim]{_safe(line2_text)}[/]"
        return f"{line1}\n{line2}"

    def _build_job_poster_item(self, job: ScheduledJob, *, rail_width: int = 38) -> PosterItem:
        snap = self._job_live_progress(job.id)
        is_running = job.status == "running" or snap is not None
        status_key = (
            "running"
            if is_running
            else (job.status if job.enabled or job.status in {"done", "failed", "needs_auth", "needs_review"} else "paused")
        )
        glyph, role = _STATUS_BADGE.get(status_key, ("○", "muted"))
        mode_label = tr(f"schedule.input_mode.{job.input_mode}", default=job.input_mode)
        action_label = tr(f"schedule.action.{job.action}", default=job.action)

        meta: list[tuple[str, str]] = []
        if is_running and snap is not None:
            stage_txt = _stage_label(snap.stage)
            elapsed = _format_elapsed(snap.started_at)
            item_tag = f" ({snap.item_index}/{snap.item_total})" if snap.item_total > 1 else ""
            status_line = f"{glyph} {_status_label(status_key)} · {stage_txt}{item_tag}"
            if elapsed:
                status_line += f" · {elapsed}"
            meta.append((role, status_line))

            percent, speed_bps, _ = parse_frame_metrics(snap.rows)
            inner_w = max(24, rail_width)
            if percent is not None:
                bar_w = min(18, max(10, inner_w - 18))
                bar_line = f"{_progress_bar(percent, bar_w)}  {percent}%"
                spd = format_speed(speed_bps)
                if spd:
                    bar_line += f" · {spd}"
                meta.append(("accent", bar_line))
            if snap.rows:
                for row in snap.rows[:4]:
                    meta.append(("dim", _clip(row, inner_w)))
            elif snap.last_log:
                meta.append((tr("schedule.meta.progress", default="Progress"), _clip(snap.last_log, 96)))
        else:
            meta.append((role, f"{glyph} {_status_label(status_key)}"))

        meta.extend(
            [
                (tr("schedule.meta.service", default="Service"), job.service_id),
                (tr("schedule.meta.next", default="Next"), format_local_time(job.next_attempt_at or job.run_at)),
                (tr("schedule.meta.mode", default="Lookup"), f"{mode_label} · {action_label}"),
            ]
        )
        if job.episode_expr:
            meta.append((tr("schedule.meta.episodes", default="Episodes"), job.episode_expr))
        if job.completed_episodes:
            meta.append((tr("schedule.meta.completed", default="Downloaded"), ", ".join(job.completed_episodes[-4:])))
        if job.last_error:
            meta.append(("warn", job.last_error[:140]))
        return PosterItem(
            title=job.name,
            subtitle=f"{job.service_id} · {tr(f'schedule.repeat.{job.repeat}', default=job.repeat)}",
            poster_url=job.poster_url,
            meta_lines=tuple(meta),
            synopsis=job.source_meta.get("synopsis", ""),
            footer_hint=tr("poster.hint.job", default="Enter: edit · r: run now"),
        )

    def refresh_live_progress(self, job_id: str | None = None) -> None:
        """Update running job row and inspector metadata in-place with zero flicker."""
        if not self.is_mounted or self._active_tab != "tab-jobs":
            return
        target_id = job_id or getattr(self.app, "_scheduler_active_job_id", None)
        if not target_id:
            return
        job = next((item for item in self._jobs if item.id == target_id), None)
        if job is None:
            return
        options = self.query_one("#schedule-list", OptionList)
        avail = self._list_inner_width()
        prompt = self._format_job_option_prompt(job, now=utc_now(), avail=avail)
        try:
            options.replace_option_prompt(f"job:{job.id}", prompt)
        except OptionDoesNotExist:
            pass
        idx = options.highlighted
        if idx is not None and 0 <= idx < len(self._jobs) and self._jobs[idx].id == target_id:
            found = self.query("#poster-rail")
            if found:
                rail = found.first(PosterPreviewWidget)
                if rail.display:
                    rail_w = max(24, int(rail.size.width or 44) - 6)
                    rail.update_meta(self._build_job_poster_item(job, rail_width=rail_w))

    def _render_jobs(self) -> None:
        options = self.query_one("#schedule-list", OptionList)
        prev = self._tab_highlights.get("tab-jobs")
        wanted_id = self._selected_job_id
        options.clear_options()
        if not self._jobs:
            self._selected_job_id = None
            options.add_option(
                Option(
                    f"  [$dim]{_safe(tr('schedule.empty_jobs', default='No scheduled downloads yet. Click + New job (or press n), or switch to Upcoming radar (2).'))}[/]",
                    disabled=True,
                )
            )
            return
        now = utc_now()
        avail = self._list_inner_width()
        target_idx: int | None = None
        for idx, job in enumerate(self._jobs):
            prompt = self._format_job_option_prompt(job, now=now, avail=avail)
            options.add_option(Option(prompt, id=f"job:{job.id}"))
            if wanted_id and job.id == wanted_id:
                target_idx = idx
        if target_idx is None:
            target_idx = min(prev or 0, len(self._jobs) - 1)
        options.highlighted = target_idx
        self._tab_highlights["tab-jobs"] = target_idx
        self._selected_job_id = self._jobs[target_idx].id

    def _render_radar(self, needle: str = "") -> None:
        options = self.query_one("#schedule-list", OptionList)
        prev = self._tab_highlights.get("tab-radar")
        options.clear_options()
        self._radar_rows = []

        if self._radar_loading and self._radar_report is None:
            options.add_option(
                Option(
                    f"  [$dim]{_safe(tr('schedule.radar.loading', default='Fetching upcoming releases from enabled sources…'))}[/]",
                    disabled=True,
                )
            )
            return

        report = self._radar_report
        if report is None:
            return

        if report.errors:
            for src, err in report.errors.items():
                self._radar_rows.append(None)
                options.add_option(
                    Option(
                        f"  [$warn]⚠ {_safe(SOURCE_LABELS.get(src, src))}: {_safe(err[:120])}[/]",
                        disabled=True,
                    )
                )

        filtered: list[UpcomingItem] = []
        for item in report.items:
            if self._supported_only and not item.service_id:
                continue
            if needle and not matches(
                needle,
                item.title,
                item.original_title,
                item.platform_name,
                item.service_id or "",
                item.source_badge,
            ):
                continue
            filtered.append(item)

        if not filtered:
            self._radar_rows.append(None)
            msg = (
                tr("schedule.radar.no_match", default="No upcoming items match the current filter.")
                if report.items
                else tr("schedule.radar.empty", default="No upcoming items returned by the enabled sources.")
            )
            options.add_option(Option(f"  [$dim]{_safe(msg)}[/]", disabled=True))
            return

        avail = self._list_inner_width()
        current_day = ""
        first_selectable: int | None = None
        for item in filtered:
            day = item.release_date or tr("schedule.radar.tba", default="TBA")
            if day != current_day:
                current_day = day
                self._radar_rows.append(None)
                rule_len = max(6, min(36, avail - cell_width(day) - 8))
                options.add_option(
                    Option(
                        f"  [$accent]◆ {_safe(day)}[/]  [$gutter]{'─' * rule_len}[/]",
                        disabled=True,
                    )
                )
            kind_str = (
                tr("schedule.media.show", default="Series")
                if item.media_kind == "show"
                else tr("schedule.media.movie", default="Movie")
            )
            ep_str = f" · {item.episode_code}" if item.episode_code else ""
            plat_raw = item.platform_name or item.service_id or "-"
            plat = _clip(plat_raw, 18)
            svc_text = f"✓ {item.service_id}" if item.service_id else tr("justwatch.no_service", default="no service")
            svc_badge = f"[$ok]{_safe(svc_text)}[/]" if item.service_id else f"[$gutter]{_safe(svc_text)}[/]"

            fixed_cost = 10 + cell_width(kind_str) + cell_width(ep_str) + cell_width(plat) + cell_width(svc_text)
            title_shown = _clip(item.title, max(14, avail - fixed_cost))

            time_note = (
                f"{format_local_time(item.release_at)} · {item.source_badge}"
                if item.release_at
                else f"{tr('schedule.radar.date_only', default='date only · retry window')} · {item.source_badge}"
            )
            time_note_shown = _clip(time_note, max(20, avail - 9))

            line1 = (
                f"    [$dim]{_safe(kind_str)}[/]  "
                f"[bold $fg2]{_safe(title_shown)}[/][$accent]{_safe(ep_str)}[/]  "
                f"[$muted]{_safe(plat)}[/]  {svc_badge}"
            )
            line2 = f"         [$dim]{_safe(time_note_shown)}[/]"
            if first_selectable is None:
                first_selectable = len(self._radar_rows)
            self._radar_rows.append(item)
            options.add_option(Option(f"{line1}\n{line2}"))

        if prev is not None and 0 <= prev < len(self._radar_rows) and self._radar_rows[prev] is not None:
            options.highlighted = prev
        elif first_selectable is not None:
            options.highlighted = first_selectable

    def _render_history(self) -> None:
        options = self.query_one("#schedule-list", OptionList)
        prev = self._tab_highlights.get("tab-history")
        options.clear_options()
        if not self._history:
            options.add_option(
                Option(
                    f"  [$dim]{_safe(tr('schedule.empty_history', default='No scheduled runs recorded yet.'))}[/]",
                    disabled=True,
                )
            )
            return
        avail = self._list_inner_width()
        for rec in self._history:
            glyph, role = _STATUS_BADGE.get(rec.status, ("·", "muted"))
            status_word = _status_label(rec.status)
            when = format_local_time(rec.started_at)
            fixed_cost = 8 + cell_width(glyph) + cell_width(status_word) + cell_width(rec.service_id) + cell_width(when)
            name_shown = _clip(rec.job_name, max(14, avail - fixed_cost))
            summary_shown = _clip(rec.summary, max(24, avail - 5))
            line1 = (
                f"  [${role}]{glyph} {_safe(status_word)}[/]  "
                f"[bold $fg2]{_safe(name_shown)}[/]  "
                f"[$accent]{_safe(rec.service_id)}[/]  "
                f"[$dim]{_safe(when)}[/]"
            )
            line2 = f"     [$dim]{_safe(summary_shown)}[/]"
            options.add_option(Option(f"{line1}\n{line2}", id=f"hist:{rec.id}"))
        options.highlighted = min(prev or 0, len(self._history) - 1)

    def _render_settings(self) -> None:
        """Render scheduled-download & radar settings using UniDL's native setting_row() layout."""
        options = self.query_one("#schedule-list", OptionList)
        prev = self._tab_highlights.get("tab-settings")
        options.clear_options()

        st = self._host_status
        host_val = self._host_summary_text()
        host_role = "ok" if (st is not None and st.installed) else "muted"

        sources = normalize_upcoming_sources(
            self.app.globals.get("upcoming_sources", DEFAULT_UPCOMING_SOURCES)
        )
        sources_val = (
            " + ".join(SOURCE_LABELS.get(s, s) for s in sources)
            if sources
            else tr("value.none")
        )
        tmdb_val = mask_api_key(self.app.globals.get("tmdb_api_key", ""))
        region = search_region_from(self.app.globals)
        region_val = jw.region_label(region)
        only_val = tr("value.on") if self._supported_only else tr("value.off")

        poster_spec = self.app.globals.spec_by_key.get("poster_preview")
        poster_val = (
            setting_value(poster_spec, self.app.globals)
            if poster_spec is not None
            else str(self.app.globals.get("poster_preview", "auto"))
        )
        poster_help = poster_capability_summary()

        rows = [
            (
                "host",
                tr("schedule.setting.host", default="Background host scheduler"),
                f"[${host_role}]{_safe(host_val)}[/]",
                tr(
                    "schedule.setting.host_help",
                    default="Run due jobs automatically when UniDL is closed via macOS launchd, Linux systemd, or Windows Task Scheduler.",
                ),
            ),
            (
                "sources",
                tr("schedule.setting.sources", default="Upcoming release sources"),
                f"[$accent]{_safe(sources_val)}[/]",
                tr(
                    "schedule.setting.sources_help",
                    default="Choose one or combine JustWatch, TVMaze, AniList, and TMDB for Upcoming radar.",
                ),
            ),
            (
                "tmdb_key",
                tr("schedule.setting.tmdb_key", default="TMDB API key"),
                f"[$accent]{_safe(tmdb_val)}[/]",
                tr(
                    "schedule.setting.tmdb_key_help",
                    default="Optional TMDB v3 API key or v4 token. Saved privately (0600) and masked in logs.",
                ),
            ),
            (
                "region",
                tr("schedule.setting.region", default="Upcoming radar region"),
                f"[$accent]{_safe(region_val)}[/]",
                tr(
                    "schedule.setting.region_help",
                    default="Catalog region used for JustWatch and TMDB upcoming streaming availability.",
                ),
            ),
            (
                "installed_only",
                tr("schedule.setting.installed_only", default="Installed services only"),
                f"[$accent]{_safe(only_val)}[/]",
                tr(
                    "schedule.setting.installed_only_help",
                    default="Filter Upcoming radar to titles matching a service currently installed in UniDL.",
                ),
            ),
            (
                "posters",
                tr("schedule.setting.posters", default="Poster preview"),
                f"[$accent]{_safe(poster_val)}[/]",
                poster_help,
            ),
        ]

        longest = label_width(label for _key, label, _val, _help in rows)
        for key, label, val_markup, help_text in rows:
            options.add_option(
                Option(
                    setting_row(
                        options,
                        label,
                        f"{val_markup}\n[$dim]{_safe(help_text)}[/]",
                        longest,
                        indent=4,
                    ),
                    id=f"setting:{key}",
                )
            )
        options.highlighted = min(prev or 0, len(rows) - 1)

    # ------------------------------------------------------------ radar worker

    def _start_radar_fetch(self, *, render: bool = True) -> None:
        sig = self._current_radar_signature()
        self._radar_signature = sig
        self._radar_loading = True
        self._radar_generation += 1
        gen = self._radar_generation
        sources, region, tmdb_key = sig
        proxy = _app_proxy(self.app)
        services = list(getattr(self.app, "services", ()) or ())
        self._fetch_radar_worker(gen, sources, region, tmdb_key, services, proxy)
        if render and self._active_tab == "tab-radar":
            self._render_radar(self.query_one(FilterBox).value)
            self._render_crumbs()
            self._sync_poster_layout()
            self._refresh_actions()

    @work(thread=True, exclusive=True, group="upcoming-radar", exit_on_error=False)
    def _fetch_radar_worker(
        self,
        gen: int,
        sources: tuple[str, ...],
        region: str,
        tmdb_key: str,
        services: list[Any],
        proxy: str | None,
    ) -> None:
        try:
            report = fetch_upcoming(
                sources=sources,
                region=region,
                tmdb_api_key=tmdb_key,
                services=services,
                proxy=proxy,
            )
        except Exception as exc:  # noqa: BLE001
            report = UpcomingReport(items=[], errors={"radar": str(exc) or type(exc).__name__})
        if get_current_worker().is_cancelled:
            return
        try:
            self.app.call_from_thread(self._accept_radar, gen, report)
        except RuntimeError:
            pass

    def _accept_radar(self, generation: int, report: UpcomingReport) -> None:
        if generation != self._radar_generation or not self.is_mounted:
            return
        self._radar_loading = False
        self._radar_report = report
        if self._active_tab == "tab-radar":
            needle = self.query_one(FilterBox).value
            self._render_radar(needle)
            self._render_crumbs()
            self._sync_poster_layout()
            self._refresh_actions()

    # -------------------------------------------------------------- poster rail

    def _sync_poster_layout(self) -> None:
        found = self.query("#poster-rail")
        if not found:
            return
        rail = found.first(PosterPreviewWidget)
        width = int(self.size.width or self.app.size.width or 80)
        height = int(self.size.height or self.app.size.height or 24)
        mode = _poster_mode_for(self.app, self._poster_override)
        visible, renderer, _reason = should_show_poster(mode, width=width, height=height)

        if self._active_tab not in {"tab-jobs", "tab-radar"} or not visible:
            rail.clear_graphics()
            rail.display = False
            return

        rail.display = True
        idx = self.query_one("#schedule-list", OptionList).highlighted
        self._update_poster_for_index(idx, renderer=renderer)

    def _update_poster_for_index(self, index: int | None, *, renderer: str | None = None) -> None:
        found = self.query("#poster-rail")
        if not found:
            return
        rail = found.first(PosterPreviewWidget)
        if not rail.display or self._active_tab not in {"tab-jobs", "tab-radar"}:
            return
        if renderer is None:
            width = int(self.size.width or self.app.size.width or 80)
            height = int(self.size.height or self.app.size.height or 24)
            mode = _poster_mode_for(self.app, self._poster_override)
            visible, renderer, _reason = should_show_poster(mode, width=width, height=height)
            if not visible:
                rail.clear_graphics()
                rail.display = False
                return

        proxy = _app_proxy(self.app)
        if self._active_tab == "tab-jobs":
            if index is None or not (0 <= index < len(self._jobs)):
                rail.set_item(None, renderer=renderer, proxy=proxy)
                return
            job = self._jobs[index]
            rail_w = max(24, int(rail.size.width or 44) - 6)
            rail.set_item(
                self._build_job_poster_item(job, rail_width=rail_w),
                renderer=renderer,
                proxy=proxy,
            )
            return

        if self._active_tab == "tab-radar":
            item = (
                self._radar_rows[index]
                if (index is not None and 0 <= index < len(self._radar_rows))
                else None
            )
            if item is None:
                rail.set_item(None, renderer=renderer, proxy=proxy)
                return
            meta = [
                (
                    "ok" if item.service_id else "muted",
                    f"✓ {item.service_id}" if item.service_id else tr("justwatch.no_service", default="no service"),
                ),
                (tr("schedule.meta.platform", default="Platform"), item.platform_name or "-"),
                (
                    tr("schedule.meta.release", default="Release"),
                    format_local_time(item.release_at) if item.release_at else item.release_date,
                ),
                (tr("schedule.meta.sources", default="Sources"), item.source_badge),
            ]
            if item.episode_code:
                ep_val = f"{item.episode_code} {item.episode_title}".strip()
                meta.append((tr("schedule.meta.episodes", default="Episode"), ep_val))
            kind_label = (
                tr("schedule.media.show", default="Series")
                if item.media_kind == "show"
                else tr("schedule.media.movie", default="Movie")
            )
            sub_bits = [p for p in (item.year, kind_label, item.region) if p]
            rail.set_item(
                PosterItem(
                    title=item.title,
                    subtitle=" · ".join(sub_bits),
                    poster_url=item.poster_url,
                    meta_lines=tuple(meta),
                    synopsis=item.synopsis,
                    footer_hint=tr("poster.hint.radar", default="Enter: schedule download"),
                ),
                renderer=renderer,
                proxy=proxy,
            )

    # ------------------------------------------------------------------ events

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        event.stop()
        tab_id = str(event.tab.id or "tab-jobs")
        if tab_id == self._active_tab:
            return
        cur_idx = self.query_one("#schedule-list", OptionList).highlighted
        if cur_idx is not None:
            self._tab_highlights[self._active_tab] = cur_idx
            if self._active_tab == "tab-jobs" and 0 <= cur_idx < len(self._jobs):
                self._selected_job_id = self._jobs[cur_idx].id
        found = self.query("#poster-rail")
        if found:
            found.first(PosterPreviewWidget).clear_graphics()
        self._active_tab = tab_id
        self.refresh_data()
        self.query_one("#schedule-list", OptionList).focus()

    def on_input_changed(self, event) -> None:
        if self._active_tab != "tab-radar":
            return
        event.stop()
        self._render_radar(event.value)
        self._render_crumbs()
        self._sync_poster_layout()
        self._refresh_actions()

    def on_input_submitted(self, event) -> None:
        if self._active_tab != "tab-radar":
            return
        event.stop()
        idx = self.query_one("#schedule-list", OptionList).highlighted
        self._schedule_from_radar(idx)

    def action_cursor(self, delta: int) -> None:
        if self._active_tab != "tab-radar":
            return
        fbox = self.query_one(FilterBox)
        if self.focused is not fbox:
            return
        options = self.query_one("#schedule-list", OptionList)
        if not options.option_count:
            return
        if delta < 0:
            for _ in range(abs(delta)):
                options.action_cursor_up()
        else:
            for _ in range(delta):
                options.action_cursor_down()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        self._tab_highlights[self._active_tab] = event.option_index
        if self._active_tab == "tab-jobs" and 0 <= event.option_index < len(self._jobs):
            self._selected_job_id = self._jobs[event.option_index].id
        self._update_poster_for_index(event.option_index)
        self._refresh_actions()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if self._active_tab == "tab-jobs":
            self.action_edit_job()
        elif self._active_tab == "tab-radar":
            self._schedule_from_radar(event.option_index)
        elif self._active_tab == "tab-history":
            self._inspect_history(event.option_index)
        else:
            self._activate_setting(event.option_index)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id or ""
        idx = self.query_one("#schedule-list", OptionList).highlighted
        if bid == "sched-act-new":
            self.action_new_job()
        elif bid == "sched-act-edit":
            self.action_edit_job()
        elif bid == "sched-act-run":
            self.action_run_now()
        elif bid == "sched-act-toggle":
            self.action_toggle_enabled()
        elif bid == "sched-act-delete":
            self.action_delete_job()
        elif bid == "sched-act-schedule":
            self._schedule_from_radar(idx)
        elif bid == "sched-act-refresh":
            self.action_run_now()
        elif bid == "sched-act-only":
            self.action_toggle_supported_only()
        elif bid == "sched-act-log":
            self._inspect_history(idx)
        elif bid == "sched-act-rerun":
            self.action_run_now()
        elif bid == "sched-act-clear":
            self.action_clear_history()
        elif bid == "sched-act-change":
            self._activate_setting(idx)

    # ----------------------------------------------------------------- actions

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one("#schedule-tabs", Tabs).active = tab_id

    def action_toggle_poster(self) -> None:
        if self._active_tab not in {"tab-jobs", "tab-radar"}:
            return
        width = int(self.size.width or self.app.size.width or 80)
        height = int(self.size.height or self.app.size.height or 24)
        nxt, notice = _cycle_poster_override(self.app, self._poster_override, width, height)
        self._poster_override = nxt
        self._sync_poster_layout()
        if self._active_tab == "tab-jobs":
            self._render_jobs()
        elif self._active_tab == "tab-radar":
            self._render_radar(self.query_one(FilterBox).value)
        self.call_after_refresh(self._refresh_on_layout)
        if notice:
            self.notify(notice, timeout=4)

    def action_new_job(self) -> None:
        self.app.open_schedule_editor(on_saved=self._on_job_saved)

    def _selected_job(self) -> ScheduledJob | None:
        if self._active_tab != "tab-jobs":
            return None
        idx = self.query_one("#schedule-list", OptionList).highlighted
        if idx is None or not (0 <= idx < len(self._jobs)):
            return None
        return self._jobs[idx]

    def _selected_radar_item(self) -> UpcomingItem | None:
        if self._active_tab != "tab-radar":
            return None
        idx = self.query_one("#schedule-list", OptionList).highlighted
        if idx is None or not (0 <= idx < len(self._radar_rows)):
            return None
        return self._radar_rows[idx]

    def action_edit_job(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        self.app.open_schedule_editor(job=job, on_saved=self._on_job_saved)

    def _on_job_saved(self, job: ScheduledJob) -> None:
        self.refresh_data()
        self.notify(tr("schedule.notify.saved", name=job.name, default=f"Scheduled: {job.name}"), timeout=4)

    def action_toggle_enabled(self) -> None:
        if self._active_tab == "tab-settings":
            self._activate_setting(self.query_one("#schedule-list", OptionList).highlighted)
            return
        job = self._selected_job()
        if job is None:
            return
        updated = self.app.schedule_store.toggle_job(job.id)
        if updated is not None:
            if not updated.enabled:
                unqueue = getattr(self.app, "unqueue_scheduled_job", None)
                if callable(unqueue):
                    unqueue(job.id)
            self.refresh_data()
            self.app.refresh_schedule_indicators()

    def action_delete_job(self) -> None:
        job = self._selected_job()
        if job is None:
            return

        def _confirmed(ok: bool | None) -> None:
            if not ok:
                return
            unqueue = getattr(self.app, "unqueue_scheduled_job", None)
            if callable(unqueue):
                unqueue(job.id)
            if self.app.schedule_store.delete_job(job.id):
                self.refresh_data()
                self.app.refresh_schedule_indicators()
                self.notify(tr("schedule.notify.deleted", name=job.name, default=f"Deleted: {job.name}"), timeout=3)

        self.app.push_screen(
            _ConfirmScheduleActionModal(
                f"{phrase('Delete')} {job.name}?",
                tr(
                    "schedule.confirm.delete",
                    name=job.name,
                    default=f"Permanently delete scheduled job '{job.name}' and its watch history?",
                ),
                phrase("Delete"),
            ),
            _confirmed,
        )

    def action_run_now(self) -> None:
        if self._active_tab == "tab-radar":
            self._radar_report = None
            self._start_radar_fetch(render=True)
            return
        if self._active_tab == "tab-history":
            idx = self.query_one("#schedule-list", OptionList).highlighted
            if idx is not None and 0 <= idx < len(self._history):
                rec = self._history[idx]
                job = self.app.schedule_store.get_job(rec.job_id)
                if job is not None:
                    self.app.trigger_scheduled_job(job.id)
                    self.refresh_data()
                else:
                    self.notify(
                        tr("schedule.notify.job_deleted", name=rec.job_name, default=f"Job '{rec.job_name}' no longer exists."),
                        severity="warning",
                        timeout=4,
                    )
            return
        job = self._selected_job()
        if job is not None:
            self.app.trigger_scheduled_job(job.id)
            self.refresh_data()

    def action_open_host(self) -> None:
        self.app.push_screen(ScheduleHostModal(), lambda _res: self._load_host_status())

    def action_edit_sources(self) -> None:
        def _done(chosen: tuple[str, ...] | None) -> None:
            if chosen is None:
                return
            self.app.globals.set("upcoming_sources", chosen)
            self._radar_report = None
            self.refresh_data()

        self.app.push_screen(UpcomingSourcesModal(self.app.globals), _done)

    def action_edit_tmdb_key(self) -> None:
        current_key = str(self.app.globals.get("tmdb_api_key", "") or "")

        def _done(value: str | None) -> None:
            if value is None:
                return
            cleaned = value.strip()
            self.app.globals.set("tmdb_api_key", cleaned)
            if not cleaned:
                existing = list(
                    normalize_upcoming_sources(
                        self.app.globals.get("upcoming_sources", DEFAULT_UPCOMING_SOURCES)
                    )
                )
                if "tmdb" in existing:
                    existing.remove("tmdb")
                    self.app.globals.set(
                        "upcoming_sources",
                        tuple(existing) if existing else DEFAULT_UPCOMING_SOURCES,
                    )
            self._radar_report = None
            self.refresh_data()

        self.app.push_screen(TmdbApiKeyModal(current_key), _done)

    def action_change_region(self) -> None:
        current = search_region_from(self.app.globals)

        def _done(code: str | None) -> None:
            if not code or code == current:
                return
            self.app.globals.set("justwatch_search_region", code)
            self._radar_report = None
            self.refresh_data()

        self.app.push_screen(SearchRegionScreen(current), _done)

    def action_toggle_supported_only(self) -> None:
        self._supported_only = not self._supported_only
        self.app.globals.set("upcoming_installed_only", self._supported_only)
        self.refresh_data()

    def action_edit_poster_mode(self) -> None:
        spec = self.app.globals.spec_by_key.get("poster_preview")
        if spec is None:
            self.action_toggle_poster()
            return

        def _done(value: Any) -> None:
            if value is None:
                return
            self.app.globals.set("poster_preview", value)
            self._poster_override = None
            self.refresh_data()

        self.app.push_screen(_ChoiceEditor(spec, self.app.globals.get("poster_preview")), _done)

    def _activate_setting(self, index: int | None) -> None:
        if index is None or not (0 <= index < len(_SETTING_KEYS)):
            return
        key = _SETTING_KEYS[index]
        if key == "host":
            self.action_open_host()
        elif key == "sources":
            self.action_edit_sources()
        elif key == "tmdb_key":
            self.action_edit_tmdb_key()
        elif key == "region":
            self.action_change_region()
        elif key == "installed_only":
            self.action_toggle_supported_only()
        elif key == "posters":
            self.action_edit_poster_mode()

    def action_clear_history(self) -> None:
        if self._active_tab != "tab-history" or not self._history:
            return

        def _confirmed(ok: bool | None) -> None:
            if not ok:
                return
            self.app.schedule_store.clear_history()
            self.refresh_data()
            self.query_one("#schedule-list", OptionList).focus()
            self.notify(
                tr("schedule.notify.history_cleared", default="Scheduled run history cleared."),
                timeout=3,
            )

        self.app.push_screen(
            _ConfirmScheduleActionModal(
                tr("schedule.btn.clear_history", default="Clear history"),
                tr(
                    "schedule.confirm.clear_history",
                    count=len(self._history),
                    default=f"Remove all {len(self._history)} recorded run log(s)?",
                ),
                tr("schedule.btn.clear_history", default="Clear history"),
            ),
            _confirmed,
        )

    def _schedule_from_radar(self, index: int | None) -> None:
        if index is None or not (0 <= index < len(self._radar_rows)):
            return
        item = self._radar_rows[index]
        if item is None:
            return
        is_show = item.media_kind == "show"
        ep_expr = item.episode_code or ""
        display_name = f"{item.title} {ep_expr}".strip()
        service_id = item.service_id or ""
        platform_urls = dict(item.platform_urls)
        direct_url = item.direct_url or (platform_urls.get(service_id, "") if service_id else "")
        svc_cls = self.app.registry.for_id(service_id) if service_id else None
        prefer_url = bool(
            direct_url
            and svc_cls is not None
            and getattr(svc_cls, "SUPPORTS_URL", True)
            and not getattr(svc_cls, "SUPPORTS_SEARCH", True)
        )
        source_meta: dict[str, str] = {
            "source": item.source_badge,
            "synopsis": item.synopsis[:240],
            "platform_label": item.platform_name,
            "search_target": item.title,
            **item.source_ids,
        }
        if direct_url:
            source_meta["direct_url"] = direct_url
        if platform_urls:
            source_meta["platform_urls_json"] = json.dumps(platform_urls, ensure_ascii=False)
        prefill: dict[str, Any] = {
            "name": display_name,
            "service_id": service_id,
            "input_mode": "url" if prefer_url else "search",
            "target": direct_url if prefer_url else item.title,
            "direct_url": direct_url,
            "search_target": item.title,
            "platform_urls": platform_urls,
            "media_kind": item.media_kind,
            "match_year": item.year,
            "selection_mode": "episodes" if (is_show and ep_expr) else ("new_episodes" if is_show else "movie"),
            "episode_expr": ep_expr,
            "run_at": item.suggested_run_at(),
            "repeat": "watch" if (is_show and not ep_expr) else "once",
            "retry_window_hours": 12,
            "poster_url": item.poster_url,
            "source_meta": source_meta,
        }
        self.app.open_schedule_editor(prefill=prefill, on_saved=self._on_job_saved)

    def _inspect_history(self, index: int | None) -> None:
        if index is None or not (0 <= index < len(self._history)):
            return
        self.app.push_screen(ScheduleRunLogModal(self._history[index]))

    def go_back(self) -> bool:
        if self._active_tab == "tab-radar":
            fbox = self.query_one(FilterBox)
            if fbox.value:
                fbox.value = ""
                self.query_one("#schedule-list", OptionList).focus()
                return True
        return False


__all__ = [
    "ScheduleEditorModal",
    "ScheduleHostModal",
    "ScheduleManagerScreen",
    "ScheduleRunLogModal",
]
