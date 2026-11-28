"""Core scheduled download engine, persistence, matching, and headless runner.

Services stay completely unchanged: scheduled jobs drive each service's native
:meth:`Service.open_url` or :meth:`Service.search` generator through
:class:`ScheduledFlowPresenter`, reuse :mod:`unidl.core.ranges` for episode
expressions, and execute resolved playbacks through :class:`unidl.core.engine.Engine`.

Security boundaries:
* Schedules live in ``<paths.home>/schedules.json`` (``0600``, locked, atomic).
* Credentials, cookies, tokens, and API keys are never persisted here.
* Recorded run logs are scrubbed with :func:`unidl.core.redact.redact`.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import exports, ranges, vaults
from .config import Config
from .engine import Engine
from .flow import (
    Ask,
    Await,
    Back,
    Choice,
    Confirm,
    FlowContext,
    Form,
    PartnerHandoff,
    Pick,
    SettingsAsk,
    Suspend,
    TableAsk,
    TextAsk,
    run_flow,
)
from .network import playback_cleanup_scope
from .redact import redact
from .secureio import FILE_MODE, atomic_write_text, locked_path, private_file, set_fd_mode
from .service import Service
from .service import registry as default_registry
from .settings import Settings, SettingsStore, global_settings
from .vault import KeyVault

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
MAX_HISTORY_RECORDS = 200
MAX_LOG_LINES_PER_RUN = 120

INPUT_MODES = ("url", "search")
MEDIA_KINDS = ("auto", "movie", "show")
SELECTION_MODES = ("movie", "episodes", "new_episodes")
ACTIONS = ("download", "export", "command")
REPEAT_MODES = ("once", "daily", "weekly", "watch")
JOB_STATUSES = (
    "scheduled",
    "waiting_release",
    "queued",
    "running",
    "done",
    "failed",
    "needs_auth",
    "needs_review",
    "paused",
)

#: Typical streaming premiere hours (UTC) used when a release date lacks a time.
DEFAULT_PLATFORM_RELEASE_HOURS_UTC: dict[str, int] = {
    "netflix": 8,       # 00:00 PT
    "disney": 8,        # 00:00 PT
    "hulu": 5,          # 00:00 ET
    "max": 8,           # 00:00 PT
    "appletv": 2,       # 21:00 ET previous evening / 02:00 UTC
    "amazon": 0,        # 00:00 UTC
    "paramountplus": 8, # 00:00 PT
    "peacock": 8,       # 00:00 PT
    "crunchyroll": 15,  # midday simulcast window
    "iqiyi": 4,         # 12:00 CST
    "wetv": 10,         # 18:00 CST
    "youku": 4,         # 12:00 CST
    "bilibili": 10,     # 18:00 CST
}

_SEASON_HINT_RE = re.compile(r"\b(?:season|series|staffel|saison|temporada|stagione|シーズン|시즌|第)\s*0*(\d{1,3})\b", re.I)
_S_TOKEN_RE = re.compile(r"\bs0*(\d{1,3})(?:e\d{1,4})?\b", re.I)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2}|21\d{2})\b")
_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")
_REL_DELTA_RE = re.compile(r"^\+\s*(\d{1,4})\s*([mhd])$", re.I)
_AUTH_WORDS = (
    "login",
    "log in",
    "sign in",
    "signin",
    "password",
    "passcode",
    "otp",
    "verification",
    "captcha",
    "cookie",
    "token",
    "activate",
    "device code",
    "credentials",
    "account",
    "登录",
    "密碼",
    "密码",
    "验证码",
)
_MOVIE_WORDS = ("movie", "film", "电影", "電影")
_SHOW_WORDS = ("series", "show", "tv", "season", "episode", "剧集", "劇集", "番剧")


class ScheduleError(RuntimeError):
    """Base error for scheduled execution."""


class ScheduleAuthRequired(ScheduleError):
    """Raised when a service requires interactive login/cookies before running."""


class ScheduleNeedsReview(ScheduleError):
    """Raised when conservative disambiguation cannot safely pick one title."""


class ScheduleNotReleased(ScheduleError):
    """Raised when the title or requested episodes are not available yet."""


class ScheduleCancelled(ScheduleError):
    """Raised when an in-flight scheduled job is stopped by the user or app exit."""


# ----------------------------------------------------------------- time helpers


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def default_release_hour_utc(service_id: str = "") -> int:
    key = str(service_id or "").strip().lower()
    return DEFAULT_PLATFORM_RELEASE_HOURS_UTC.get(key, 8)


def parse_run_at(
    value: str,
    *,
    now: datetime | None = None,
    service_id: str = "",
) -> str:
    """Parse user-entered schedule timestamps into canonical ISO-8601 UTC.

    Supports:
    * ISO-8601 UTC or offset timestamps (``2026-12-04T08:00:00Z``)
    * Local ``YYYY-MM-DD HH:MM`` (and optional ``:SS``)
    * Date-only ``YYYY-MM-DD`` (applies platform default UTC release hour)
    * Relative shorthand: ``+30m``, ``+2h``, ``+1d``
    """
    raw = str(value or "").strip()
    base = (now or utc_now()).astimezone(timezone.utc).replace(microsecond=0)
    if not raw:
        raise ValueError("Start time cannot be empty.")

    rel = _REL_DELTA_RE.match(raw)
    if rel:
        amount = int(rel.group(1))
        unit = rel.group(2).lower()
        if amount <= 0:
            raise ValueError("Relative offset must be positive.")
        if unit == "m":
            return format_iso_utc(base + timedelta(minutes=amount))
        if unit == "h":
            return format_iso_utc(base + timedelta(hours=amount))
        return format_iso_utc(base + timedelta(days=amount))

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        day = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        hour = default_release_hour_utc(service_id)
        return format_iso_utc(day + timedelta(hours=hour))

    # Local time without timezone offset ("2026-12-04 09:30" or "2026-12-04 09:30:00")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            naive = datetime.strptime(raw, fmt)
            # Resolve local UTC offset on the requested date (including DST).
            return format_iso_utc(naive.astimezone(timezone.utc))
        except ValueError:
            continue

    parsed = parse_iso_utc(raw)
    if parsed is not None:
        return format_iso_utc(parsed)
    raise ValueError(f"Invalid date/time: {raw!r}. Use YYYY-MM-DD HH:MM, YYYY-MM-DD, or +2h.")


def format_local_time(iso_utc: str | None) -> str:
    dt = parse_iso_utc(iso_utc)
    if dt is None:
        return "-"
    local_dt = dt.astimezone()
    return local_dt.strftime("%Y-%m-%d %H:%M")


def format_countdown(iso_utc: str | None, *, now: datetime | None = None) -> str:
    dt = parse_iso_utc(iso_utc)
    if dt is None:
        return ""
    ref = (now or utc_now()).astimezone(timezone.utc)
    delta = int((dt - ref).total_seconds())
    if delta <= 0:
        return "now"
    minutes = (delta + 59) // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem_min = minutes % 60
    if hours < 48:
        return f"{hours}h {rem_min}m" if rem_min else f"{hours}h"
    days = hours // 24
    return f"{days}d"


def advance_repeat_time(iso_utc: str, repeat: str, *, now: datetime | None = None) -> str:
    ref = (now or utc_now()).astimezone(timezone.utc)
    anchor = parse_iso_utc(iso_utc) or ref
    mode = str(repeat or "once").strip().lower()
    if mode == "daily":
        step = timedelta(days=1)
    elif mode == "weekly":
        step = timedelta(days=7)
    elif mode == "watch":
        step = timedelta(hours=6)
    else:
        return ""
    nxt = anchor + step
    while nxt <= ref:
        nxt += step
    return format_iso_utc(nxt)


# ------------------------------------------------------------------ data models


@dataclass
class ScheduledJob:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    service_id: str = ""
    input_mode: str = "url"           # url | search
    target: str = ""
    media_kind: str = "auto"          # auto | movie | show
    match_year: str = ""
    selection_mode: str = "movie"     # movie | episodes | new_episodes
    episode_expr: str = ""
    action: str = "download"          # download | export | command
    run_at: str = ""                  # ISO-8601 UTC
    repeat: str = "once"              # once | daily | weekly | watch
    retry_window_hours: int = 12
    retry_interval_minutes: int = 20
    enabled: bool = True
    status: str = "scheduled"
    next_attempt_at: str = ""
    window_started_at: str = ""
    attempts: int = 0
    completed_episodes: list[str] = field(default_factory=list)
    poster_url: str = ""
    source_meta: dict[str, str] = field(default_factory=dict)
    last_error: str = ""
    last_run_at: str = ""
    created_at: str = field(default_factory=lambda: format_iso_utc(utc_now()))

    def __post_init__(self) -> None:
        self.id = str(self.id or uuid.uuid4().hex[:8]).strip()
        self.name = str(self.name or "").strip()
        self.service_id = str(self.service_id or "").strip().lower()
        self.input_mode = self.input_mode if self.input_mode in INPUT_MODES else "url"
        self.target = str(self.target or "").strip()
        self.media_kind = self.media_kind if self.media_kind in MEDIA_KINDS else "auto"
        self.match_year = str(self.match_year or "").strip()
        self.selection_mode = (
            self.selection_mode if self.selection_mode in SELECTION_MODES else "movie"
        )
        self.episode_expr = str(self.episode_expr or "").strip()
        self.action = self.action if self.action in ACTIONS else "download"
        self.repeat = self.repeat if self.repeat in REPEAT_MODES else "once"
        try:
            self.retry_window_hours = max(0, min(168, int(self.retry_window_hours)))
        except (TypeError, ValueError):
            self.retry_window_hours = 12
        try:
            self.retry_interval_minutes = max(1, min(720, int(self.retry_interval_minutes)))
        except (TypeError, ValueError):
            self.retry_interval_minutes = 20
        self.enabled = bool(self.enabled)
        self.status = self.status if self.status in JOB_STATUSES else "scheduled"
        self.attempts = max(0, int(self.attempts or 0))
        self.completed_episodes = [
            str(item).strip()
            for item in (self.completed_episodes or ())
            if str(item).strip()
        ]
        self.poster_url = str(self.poster_url or "").strip()
        self.source_meta = {
            str(k): str(v)
            for k, v in (self.source_meta or {}).items()
            if str(k).strip() and v is not None
        }
        self.last_error = str(self.last_error or "").strip()
        self.last_run_at = str(self.last_run_at or "").strip()
        if not self.name:
            self.name = self.target[:60] or self.service_id or self.id

    def effective_due_at(self) -> datetime | None:
        return parse_iso_utc(self.next_attempt_at) or parse_iso_utc(self.run_at)

    def is_active_pending(self) -> bool:
        return self.enabled and self.status in {"scheduled", "waiting_release", "queued"}

    def needs_attention(self) -> bool:
        return self.status in {"needs_auth", "needs_review", "failed"}

    def is_due(self, now: datetime | None = None) -> bool:
        if not self.is_active_pending():
            return False
        due = self.effective_due_at()
        if due is None:
            return False
        ref = (now or utc_now()).astimezone(timezone.utc)
        return due <= ref

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.service_id:
            errors.append("Target service is required.")
        if not self.target:
            errors.append("Link/ID or search keyword is required.")
        if self.input_mode not in INPUT_MODES:
            errors.append(f"Unsupported input mode: {self.input_mode}")
        if self.match_year and not (len(self.match_year) == 4 and self.match_year.isdigit()):
            errors.append(f"Year must be 4 digits: {self.match_year}")
        if self.episode_expr:
            if ranges.parse(self.episode_expr) is None:
                errors.append(f"Invalid episode expression: {self.episode_expr}")
        if parse_iso_utc(self.run_at) is None:
            errors.append("Start time must be a valid timestamp.")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "service_id": self.service_id,
            "input_mode": self.input_mode,
            "target": self.target,
            "media_kind": self.media_kind,
            "match_year": self.match_year,
            "selection_mode": self.selection_mode,
            "episode_expr": self.episode_expr,
            "action": self.action,
            "run_at": self.run_at,
            "repeat": self.repeat,
            "retry_window_hours": self.retry_window_hours,
            "retry_interval_minutes": self.retry_interval_minutes,
            "enabled": self.enabled,
            "status": self.status,
            "next_attempt_at": self.next_attempt_at,
            "window_started_at": self.window_started_at,
            "attempts": self.attempts,
            "completed_episodes": list(self.completed_episodes),
            "poster_url": self.poster_url,
            "source_meta": dict(self.source_meta),
            "last_error": self.last_error,
            "last_run_at": self.last_run_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledJob:
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            service_id=str(data.get("service_id") or ""),
            input_mode=str(data.get("input_mode") or "url"),
            target=str(data.get("target") or ""),
            media_kind=str(data.get("media_kind") or "auto"),
            match_year=str(data.get("match_year") or ""),
            selection_mode=str(data.get("selection_mode") or "movie"),
            episode_expr=str(data.get("episode_expr") or ""),
            action=str(data.get("action") or "download"),
            run_at=str(data.get("run_at") or ""),
            repeat=str(data.get("repeat") or "once"),
            retry_window_hours=int(data.get("retry_window_hours", 12)),
            retry_interval_minutes=int(data.get("retry_interval_minutes", 20)),
            enabled=bool(data.get("enabled", True)),
            status=str(data.get("status") or "scheduled"),
            next_attempt_at=str(data.get("next_attempt_at") or ""),
            window_started_at=str(data.get("window_started_at") or ""),
            attempts=int(data.get("attempts", 0)),
            completed_episodes=list(data.get("completed_episodes") or ()),
            poster_url=str(data.get("poster_url") or ""),
            source_meta=dict(data.get("source_meta") or {}),
            last_error=str(data.get("last_error") or ""),
            last_run_at=str(data.get("last_run_at") or ""),
            created_at=str(data.get("created_at") or format_iso_utc(utc_now())),
        )


@dataclass
class ScheduleRunRecord:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    job_id: str = ""
    job_name: str = ""
    service_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    status: str = "done"
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    episodes: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "job_name": self.job_name,
            "service_id": self.service_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "summary": redact(self.summary),
            "artifacts": [redact(item) for item in self.artifacts],
            "episodes": list(self.episodes),
            "logs": [redact(line) for line in self.logs[:MAX_LOG_LINES_PER_RUN]],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduleRunRecord:
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:10]),
            job_id=str(data.get("job_id") or ""),
            job_name=str(data.get("job_name") or ""),
            service_id=str(data.get("service_id") or ""),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            status=str(data.get("status") or "done"),
            summary=str(data.get("summary") or ""),
            artifacts=[str(x) for x in (data.get("artifacts") or ())],
            episodes=[str(x) for x in (data.get("episodes") or ())],
            logs=[str(x) for x in (data.get("logs") or ())],
        )


# --------------------------------------------------------------- storage & lock


def schedule_store_path(home: Path) -> Path:
    return Path(home) / "schedules.json"


class ScheduleStore:
    """Owner-only, locked, atomic JSON store for scheduled jobs & run history."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.backup_path = self.path.with_name(self.path.name + ".bak")
        self.last_error: str = ""

    @staticmethod
    def _parse_store_text(text: str) -> tuple[list[ScheduledJob], list[ScheduleRunRecord]]:
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise ScheduleError(f"Invalid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ScheduleError("Root JSON value must be an object.")
        raw_jobs = raw.get("jobs")
        if raw_jobs is not None and not isinstance(raw_jobs, list):
            raise ScheduleError("'jobs' field must be a list.")
        raw_history = raw.get("history")
        if raw_history is not None and not isinstance(raw_history, list):
            raise ScheduleError("'history' field must be a list.")
        jobs: list[ScheduledJob] = []
        for entry in raw_jobs or ():
            if isinstance(entry, dict):
                try:
                    jobs.append(ScheduledJob.from_dict(entry))
                except (TypeError, ValueError):
                    continue
        history: list[ScheduleRunRecord] = []
        for entry in raw_history or ():
            if isinstance(entry, dict):
                try:
                    history.append(ScheduleRunRecord.from_dict(entry))
                except (TypeError, ValueError):
                    continue
        return jobs, history[:MAX_HISTORY_RECORDS]

    def _read_unlocked(
        self,
        *,
        for_write: bool = False,
    ) -> tuple[list[ScheduledJob], list[ScheduleRunRecord]]:
        self.last_error = ""
        if not self.path.is_file():
            if self.backup_path.is_file():
                try:
                    return self._parse_store_text(self.backup_path.read_text("utf-8"))
                except (OSError, ScheduleError):
                    pass
            return [], []
        try:
            return self._parse_store_text(self.path.read_text("utf-8"))
        except (OSError, ScheduleError) as exc:
            if self.backup_path.is_file():
                try:
                    restored = self._parse_store_text(self.backup_path.read_text("utf-8"))
                    self.last_error = f"Recovered schedules from backup after read error: {exc}"
                    return restored
                except (OSError, ScheduleError):
                    pass
            self.last_error = f"Could not read schedules file {self.path}: {exc}"
            if for_write:
                raise ScheduleError(
                    f"Refusing to overwrite corrupt or unreadable schedules file {self.path}: {exc}"
                ) from exc
            return [], []

    def _write_unlocked(
        self,
        jobs: Sequence[ScheduledJob],
        history: Sequence[ScheduleRunRecord],
    ) -> None:
        if self.path.is_file():
            try:
                previous = self.path.read_text("utf-8")
                self._parse_store_text(previous)
            except (OSError, ScheduleError):
                previous = ""
            if previous:
                atomic_write_text(self.backup_path, previous, mode=FILE_MODE)
                private_file(self.backup_path)
        payload = {
            "version": SCHEMA_VERSION,
            "jobs": [job.to_dict() for job in jobs],
            "history": [rec.to_dict() for rec in list(history)[:MAX_HISTORY_RECORDS]],
        }
        atomic_write_text(
            self.path,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            mode=FILE_MODE,
        )
        private_file(self.path)

    def list_jobs(self) -> list[ScheduledJob]:
        with locked_path(self.path):
            jobs, _history = self._read_unlocked()
        return sorted(
            jobs,
            key=lambda j: (
                0 if j.is_active_pending() else (1 if j.needs_attention() else 2),
                j.next_attempt_at or j.run_at or "9999",
                j.name.lower(),
            ),
        )

    def get_job(self, job_id: str) -> ScheduledJob | None:
        wanted = str(job_id or "").strip()
        if not wanted:
            return None
        with locked_path(self.path):
            jobs, _history = self._read_unlocked()
        for job in jobs:
            if job.id == wanted:
                return job
        return None

    def upsert_job(self, job: ScheduledJob) -> ScheduledJob:
        with locked_path(self.path):
            jobs, history = self._read_unlocked(for_write=True)
            replaced = False
            for idx, existing in enumerate(jobs):
                if existing.id == job.id:
                    jobs[idx] = job
                    replaced = True
                    break
            if not replaced:
                jobs.append(job)
            self._write_unlocked(jobs, history)
        return job

    def update_job(
        self,
        job_id: str,
        mutator: Callable[[ScheduledJob], None],
    ) -> ScheduledJob | None:
        """Atomically load, mutate in place, and persist one job under file lock."""
        wanted = str(job_id or "").strip()
        if not wanted:
            return None
        with locked_path(self.path):
            jobs, history = self._read_unlocked(for_write=True)
            updated: ScheduledJob | None = None
            for job in jobs:
                if job.id == wanted:
                    mutator(job)
                    updated = job
                    break
            if updated is not None:
                self._write_unlocked(jobs, history)
        return updated

    def recover_stale_running(self) -> list[ScheduledJob]:
        """Recover jobs left in ``running`` after a crash/unclean exit.

        Callers MUST hold :func:`try_runner_lock` (or otherwise guarantee no
        other scheduler runner is executing) before calling this method.
        """
        recovered: list[ScheduledJob] = []
        with locked_path(self.path):
            jobs, history = self._read_unlocked(for_write=True)
            for job in jobs:
                if job.status != "running":
                    continue
                expected = _expected_episode_keys(job.episode_expr)
                if job.enabled and job.repeat == "once" and expected and not _missing_expected_episodes(
                    expected, job.completed_episodes
                ):
                    job.status, job.enabled, job.last_error = "done", False, ""
                    job.next_attempt_at = job.window_started_at = ""
                    job.last_run_at = format_iso_utc(utc_now())
                else:
                    job.status = "scheduled" if job.enabled else "paused"
                    job.last_error = "Interrupted by previous exit; ready to retry."
                recovered.append(job)
            if recovered:
                self._write_unlocked(jobs, history)
        return recovered

    def save_edits(self, original: ScheduledJob, edited: ScheduledJob) -> ScheduledJob | None:
        """Apply form changes to today's record, never to the old form snapshot."""
        fields = ("name", "service_id", "input_mode", "target", "media_kind", "match_year",
                  "selection_mode", "episode_expr", "action", "run_at", "repeat",
                  "retry_window_hours", "source_meta", "poster_url")
        changes = {key: getattr(edited, key) for key in fields
                   if getattr(edited, key) != getattr(original, key)}
        identity = {"service_id", "input_mode", "target", "match_year", "media_kind", "action"}
        schedule = {"run_at", "selection_mode", "episode_expr", "repeat"} | identity

        def apply(current: ScheduledJob) -> None:
            if current.status == "running" and changes.keys() & (schedule - {"run_at", "repeat"}):
                raise ScheduleError("Stop the running job before changing its target or episode selection.")
            for key, value in changes.items():
                setattr(current, key, value)
            if changes.keys() & identity:
                current.completed_episodes.clear()
            if changes.keys() & schedule:
                current.window_started_at = current.last_error = ""
                current.next_attempt_at = current.run_at
                current.attempts = 0
                if current.status not in {"paused", "running"}:
                    current.status, current.enabled = "scheduled", True

        return self.update_job(original.id, apply)

    def delete_job(self, job_id: str) -> bool:
        wanted = str(job_id or "").strip()
        with locked_path(self.path):
            jobs, history = self._read_unlocked(for_write=True)
            kept = [job for job in jobs if job.id != wanted]
            if len(kept) == len(jobs):
                return False
            self._write_unlocked(kept, history)
        return True

    def toggle_job(self, job_id: str) -> ScheduledJob | None:
        wanted = str(job_id or "").strip()
        with locked_path(self.path):
            jobs, history = self._read_unlocked(for_write=True)
            updated: ScheduledJob | None = None
            for job in jobs:
                if job.id != wanted:
                    continue
                job.enabled = not job.enabled
                if job.enabled:
                    if job.status in {"paused", "failed", "needs_auth", "needs_review", "done"}:
                        job.status = "scheduled"
                        job.window_started_at = ""
                        job.next_attempt_at = ""
                        job.last_error = ""
                else:
                    if job.status in {"scheduled", "waiting_release", "queued"}:
                        job.status = "paused"
                updated = job
                break
            if updated is not None:
                self._write_unlocked(jobs, history)
        return updated

    def list_history(self, *, limit: int = 100) -> list[ScheduleRunRecord]:
        with locked_path(self.path):
            _jobs, history = self._read_unlocked()
        return history[: max(0, int(limit))]

    def append_history(self, record: ScheduleRunRecord) -> None:
        with locked_path(self.path):
            jobs, history = self._read_unlocked(for_write=True)
            history.insert(0, record)
            self._write_unlocked(jobs, history[:MAX_HISTORY_RECORDS])

    def clear_history(self) -> None:
        with locked_path(self.path):
            jobs, _history = self._read_unlocked(for_write=True)
            self._write_unlocked(jobs, [])

    def due_jobs(self, *, now: datetime | None = None) -> list[ScheduledJob]:
        ref = now or utc_now()
        return [job for job in self.list_jobs() if job.is_due(ref)]

    def summary(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Compact aggregate for status chips without touching any service."""
        ref = now or utc_now()
        jobs = self.list_jobs()
        pending = [j for j in jobs if j.is_active_pending()]
        attention = [j for j in jobs if j.needs_attention()]
        running = [j for j in jobs if j.status == "running"]
        next_dt: datetime | None = None
        for job in pending:
            due = job.effective_due_at()
            if due is not None and (next_dt is None or due < next_dt):
                next_dt = due
        return {
            "total": len(jobs),
            "pending": len(pending),
            "attention": len(attention),
            "running": len(running),
            "next_at": format_iso_utc(next_dt) if next_dt else "",
            "next_in": format_countdown(format_iso_utc(next_dt), now=ref) if next_dt else "",
        }


@contextlib.contextmanager
def try_runner_lock(home: Path) -> Iterator[bool]:
    """Non-blocking cross-process lock so TUI and launchd/cron never double-run."""
    lock_file = Path(home) / ".schedules.runner.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(lock_file, flags, FILE_MODE)
    acquired = False
    try:
        set_fd_mode(fd, FILE_MODE)
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                acquired = False
        elif msvcrt is not None:  # pragma: no cover - Windows
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                acquired = False
        else:
            acquired = True
        yield acquired
    finally:
        if acquired:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            elif msvcrt is not None:  # pragma: no cover - Windows
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        os.close(fd)


# -------------------------------------------- matching & non-interactive driver


def _clean_title(text: str) -> str:
    lowered = str(text or "").casefold()
    # Strip bracketed years/tags like "[2026]", "(US)", "(Series)"
    lowered = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", lowered)
    lowered = _NON_WORD_RE.sub(" ", lowered)
    return _SPACES_RE.sub(" ", lowered).strip()


def _extract_years(text: str) -> set[str]:
    return set(_YEAR_RE.findall(str(text or "")))


_BRACKETED_RE = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")


def _candidate_years(choice: Choice, query: str, *, match_year: str = "") -> set[str]:
    """Extract release-year hints from a choice without confusing title numbers."""
    meta_parts = [choice.detail, *choice.tags, *_BRACKETED_RE.findall(str(choice.label or ""))]
    meta_years = _extract_years(" ".join(str(p or "") for p in meta_parts))
    if meta_years:
        return meta_years
    label_years = _extract_years(choice.label)
    if not label_years:
        return set()
    query_years = _extract_years(query)
    if match_year:
        query_years.discard(match_year)
    return label_years - query_years


def _infer_kind(text: str) -> str:
    lowered = str(text or "").casefold()
    has_show = any(word in lowered for word in _SHOW_WORDS)
    has_movie = any(word in lowered for word in _MOVIE_WORDS)
    if has_show and not has_movie:
        return "show"
    if has_movie and not has_show:
        return "movie"
    return "auto"


def _score_choice(
    choice: Choice,
    query: str,
    *,
    match_year: str = "",
    media_kind: str = "auto",
) -> tuple[float, bool]:
    """Return ``(score, exact_title)`` for conservative search disambiguation."""
    q_clean = _clean_title(query)
    if not q_clean:
        return 0.0, False
    label_clean = _clean_title(choice.label)
    combined = f"{choice.label} {choice.detail} {' '.join(choice.tags)}"
    exact = bool(label_clean and label_clean == q_clean)
    if exact:
        score = 1.0
    else:
        ratio = difflib.SequenceMatcher(None, q_clean, label_clean).ratio() if label_clean else 0.0
        q_tokens = set(q_clean.split())
        l_tokens = set(label_clean.split())
        overlap = (len(q_tokens & l_tokens) / max(1, len(q_tokens))) if q_tokens else 0.0
        # Prefix containment helps when service appends subtitles like "Show - Season 1".
        prefix_bonus = 0.08 if label_clean.startswith(q_clean) and len(q_clean) >= 3 else 0.0
        score = min(0.97, max(ratio, 0.55 * ratio + 0.45 * overlap) + prefix_bonus)

    if match_year:
        years = _candidate_years(choice, query, match_year=match_year)
        if years:
            if match_year in years:
                score += 0.12
            else:
                score -= 0.25
    if media_kind in {"movie", "show"}:
        inferred = _infer_kind(combined)
        if inferred == media_kind:
            score += 0.06
        elif inferred != "auto" and inferred != media_kind:
            score -= 0.22
    return score, exact


def match_search_choice(
    choices: Sequence[Choice],
    query: str,
    *,
    match_year: str = "",
    media_kind: str = "auto",
) -> tuple[Choice | None, float, str]:
    """Conservative search-result picker.

    Returns ``(choice, score, reason)``.  When multiple candidates remain
    ambiguous (no clear high-confidence winner), or when every candidate
    explicitly conflicts with the requested year or media kind, returns
    ``None`` so the scheduler pauses with ``needs_review`` instead of
    downloading the wrong item.
    """
    enabled = [c for c in choices if not c.disabled and not c.navigates]
    if not enabled:
        return None, 0.0, "no search results returned"

    evaluated: list[tuple[Choice, float, bool, bool, bool, set[str], str]] = []
    for choice in enabled:
        score, exact = _score_choice(choice, query, match_year=match_year, media_kind=media_kind)
        cand_years = _candidate_years(choice, query, match_year=match_year) if match_year else set()
        year_conflict = bool(match_year and cand_years and match_year not in cand_years)
        combined = f"{choice.label} {choice.detail} {' '.join(choice.tags)}"
        inferred = _infer_kind(combined) if media_kind in {"movie", "show"} else "auto"
        kind_conflict = bool(media_kind in {"movie", "show"} and inferred != "auto" and inferred != media_kind)
        evaluated.append((choice, score, exact, year_conflict, kind_conflict, cand_years, inferred))

    eligible = [(c, sc, ex) for (c, sc, ex, yc, kc, _yrs, _inf) in evaluated if not yc and not kc]
    if not eligible:
        evaluated.sort(key=lambda item: (item[1], item[2]), reverse=True)
        top_choice, top_score, _ex, top_yc, top_kc, top_yrs, top_inf = evaluated[0]
        if top_yc:
            found = ", ".join(sorted(top_yrs))
            return (
                None,
                top_score,
                f"year conflict for '{top_choice.label}' (expected {match_year}, found {found})",
            )
        return (
            None,
            top_score,
            f"media kind mismatch for '{top_choice.label}' (expected {media_kind}, found {top_inf})",
        )

    scored = sorted(eligible, key=lambda item: (item[1], item[2]), reverse=True)
    best_choice, best_score, best_exact = scored[0]

    if len(scored) == 1:
        if best_score >= 0.72:
            return best_choice, best_score, "single confident match"
        return (
            None,
            best_score,
            f"only result '{best_choice.label}' had low similarity ({best_score:.2f})",
        )

    second_choice, second_score, second_exact = scored[1]
    margin = best_score - second_score

    # Exact unique title match (with non-conflicting year/kind) passes cleanly.
    if best_exact and not second_exact and best_score >= 0.85:
        return best_choice, best_score, "exact title match"

    # High-confidence winner with clear lead over runner-up.
    if best_score >= 0.88 and margin >= 0.12:
        return best_choice, best_score, "high-confidence match"

    return (
        None,
        best_score,
        (
            f"ambiguous search match between '{best_choice.label}' ({best_score:.2f}) "
            f"and '{second_choice.label}' ({second_score:.2f}); add year or link"
        ),
    )


def _looks_like_auth_ask(ask: Ask) -> bool:
    if isinstance(ask, (Await, Form)):
        return True
    if isinstance(ask, TextAsk) and ask.password:
        return True
    title = str(ask.title or "").casefold()
    if any(word in title for word in _AUTH_WORDS):
        return True
    if isinstance(ask, TextAsk) and ask.lines:
        return True
    return False


def _extract_season_number(text: str) -> int | None:
    match = _SEASON_HINT_RE.search(str(text or ""))
    if match:
        return int(match.group(1))
    match = _S_TOKEN_RE.search(str(text or ""))
    if match:
        return int(match.group(1))
    return None


def _target_seasons_from_expr(expr: str) -> list[int]:
    found: list[int] = []
    for m in _S_TOKEN_RE.finditer(str(expr or "")):
        num = int(m.group(1))
        if num not in found:
            found.append(num)
    if not found:
        for m in _SEASON_HINT_RE.finditer(str(expr or "")):
            num = int(m.group(1))
            if num not in found:
                found.append(num)
    return found


def _episode_ledger_key(entry: ranges.Entry, choice: Choice) -> str:
    if entry.season is not None and entry.episode is not None:
        return f"S{entry.season:02d}E{entry.episode:02d}"
    if entry.episode is not None:
        return f"E{entry.episode:02d}"
    return _clean_title(choice.label)[:64] or f"row:{entry.row}"


_MAX_EXPECTED_EPISODE_SPAN = 500


def _expected_episode_keys(expr: str) -> list[str] | None:
    """Enumerate explicit bounded episode keys from ``expr``, or ``None`` if open-ended.

    Expressions like ``S01E01-S01E03``, ``S01E01,S01E03``, ``E01-E03``, or
    ``S01E01-S01E04,-S01E02`` name a concrete finite set of episodes so the
    scheduler knows when part of the range has not been released yet.
    Unbounded or season-wide expressions (``all``, ``S01``, ``S01E05-``,
    cross-season ``S01E08-S02E02``, or bare row numbers ``1-3``) return
    ``None`` because the total episode count cannot be inferred up front.
    """
    selection = ranges.parse(expr)
    if selection is None or selection.everything or not selection.spans:
        return None
    positive = [span for span in selection.spans if not span.exclude]
    excludes = [span for span in selection.spans if span.exclude]
    if not positive:
        return None

    synthetic: list[ranges.Entry] = []
    seen_pairs: set[tuple[int | None, int]] = set()
    row_num = 0
    for span in positive:
        if span.start.is_row or span.end.is_row:
            return None
        if span.start.episode is None or span.end.episode is None:
            return None
        if span.end.episode >= ranges._LAST or (span.end.season is not None and span.end.season >= ranges._LAST):
            return None
        if span.start.season != span.end.season:
            return None
        low = min(span.start.episode, span.end.episode)
        high = max(span.start.episode, span.end.episode)
        if high - low > _MAX_EXPECTED_EPISODE_SPAN:
            return None
        for ep_num in range(low, high + 1):
            pair = (span.start.season, ep_num)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            row_num += 1
            synthetic.append(ranges.Entry(row=row_num, season=span.start.season, episode=ep_num))

    keys: list[str] = []
    for entry in synthetic:
        if any(span.holds(entry) for span in excludes):
            continue
        assert entry.episode is not None
        if entry.season is not None:
            keys.append(f"S{entry.season:02d}E{entry.episode:02d}")
        else:
            keys.append(f"E{entry.episode:02d}")
    return keys or None


def _episode_key_satisfied(expected_key: str, completed: set[str]) -> bool:
    if expected_key in completed:
        return True
    if expected_key.startswith("E"):
        suffix = expected_key
        return any(item.startswith("S") and item.endswith(suffix) and item[1 : -len(suffix)].isdigit() for item in completed)
    return False


def _missing_expected_episodes(
    expected: Sequence[str] | None,
    completed: Sequence[str],
) -> list[str]:
    if not expected:
        return []
    done_set = set(completed)
    return [key for key in expected if not _episode_key_satisfied(key, done_set)]


def _is_season_picker(ask: Pick, enabled: Sequence[Choice]) -> bool:
    title = str(ask.title or "").casefold()
    if any(word in title for word in ("season", "series", "季", "saison", "staffel", "temporada")):
        if not any(word in title for word in ("episode", "集")):
            return True
    season_hits = sum(
        1
        for c in enabled
        if _extract_season_number(f"{c.label} {c.detail}") is not None
        and ranges.read_entry(1, c.label, c.detail).episode is None
    )
    return bool(enabled) and season_hits >= max(1, (len(enabled) + 1) // 2)


class _ListSink:
    """Capture service flow logs into a bounded, redacted list."""

    def __init__(self, log_fn: Callable[[str], None] | None = None) -> None:
        self.lines: list[str] = []
        self._log_fn = log_fn

    def log(self, message: str, level: str = "info") -> None:
        text = redact(f"[{level}] {message}" if level != "info" else str(message))
        if len(self.lines) < MAX_LOG_LINES_PER_RUN:
            self.lines.append(text)
        if self._log_fn is not None:
            self._log_fn(text)

    def status(self, message: str) -> None:
        self.log(str(message), "info")

    def batch(self, total: int) -> None:
        self.log(f"batch: {total} title(s)")

    def problem(self, headline: str, detail: str = "", hint: str = "") -> None:
        body = " — ".join(part for part in (headline, detail, hint) if part)
        self.log(body, "error")


class ScheduledFlowPresenter:
    """Non-interactive Flow presenter tailored to a :class:`ScheduledJob`."""

    def __init__(self, job: ScheduledJob, *, log: Callable[[str], None] | None = None) -> None:
        self.job = job
        self._log = log or (lambda _msg: None)
        self._search_matched = job.input_mode != "search"
        self._target_text_used = False
        self.selected_episode_keys: list[str] = []
        self.matched_title_label: str = ""
        self.expected_episode_keys: list[str] | None = _expected_episode_keys(job.episode_expr)
        self._picked_season: int | None = None
        self._partial_range_retry: bool = bool(
            self.expected_episode_keys
            and job.completed_episodes
            and _missing_expected_episodes(self.expected_episode_keys, job.completed_episodes)
        )

    def _read_entries(self, enabled: Sequence[Choice]) -> list[ranges.Entry]:
        entries: list[ranges.Entry] = []
        for idx, choice in enumerate(enabled, 1):
            entry = ranges.read_entry(idx, choice.label, choice.detail)
            if entry.season is None and entry.episode is not None and self._picked_season is not None:
                entry = ranges.Entry(row=entry.row, season=self._picked_season, episode=entry.episode)
            entries.append(entry)
        return entries

    def _already_delivered(self, key: str, completed: set[str]) -> bool:
        # A bounded expression like E01-E03 may match an episode without a
        # season in the picker, while the delivered title records S01E01.
        return _episode_key_satisfied(key, completed) if self._partial_range_retry else key in completed

    def present(self, ask: Ask) -> Any:
        if isinstance(ask, Suspend):
            return ask.work() if callable(ask.work) else None

        if isinstance(ask, (SettingsAsk, PartnerHandoff)):
            raise ScheduleNeedsReview(
                f"Service asked for interactive step '{ask.title or type(ask).__name__}'."
            )

        if _looks_like_auth_ask(ask):
            raise ScheduleAuthRequired(
                f"Service requires sign-in ({ask.title or type(ask).__name__})."
            )

        if isinstance(ask, Confirm):
            title = str(ask.title or "").casefold()
            if any(word in title for word in ("logout", "sign out", "delete", "clear")):
                return False
            return bool(ask.default)

        if isinstance(ask, TextAsk):
            if not self._target_text_used and self.job.target:
                self._target_text_used = True
                return self.job.target
            if ask.default:
                return ask.default
            raise ScheduleNeedsReview(f"Unexpected text prompt: {ask.title}")

        if isinstance(ask, TableAsk):
            if not ask.values:
                raise ScheduleNotReleased(f"No rows available for '{ask.title}'.")
            if len(ask.values) == 1:
                return ask.values[0]
            choices = [
                Choice(" ".join(str(cell) for cell in row), value)
                for row, value in zip(ask.rows, ask.values, strict=False)
            ]
            chosen, _score, reason = match_search_choice(
                choices,
                self.job.target,
                match_year=self.job.match_year,
                media_kind=self.job.media_kind,
            )
            if chosen is None:
                raise ScheduleNeedsReview(reason)
            return chosen.value

        if isinstance(ask, Pick):
            return self._handle_pick(ask)

        return None

    def _handle_pick(self, ask: Pick) -> Any:
        enabled = [c for c in ask.choices if not c.disabled and not c.navigates]
        if not enabled:
            raise ScheduleNotReleased(f"No available items in '{ask.title or 'selection'}'.")

        if ask.multi:
            return self._handle_multi_pick(ask, enabled)
        return self._handle_single_pick(ask, enabled)

    def _handle_single_pick(self, ask: Pick, enabled: list[Choice]) -> Any:
        # 1. First title pick in search mode -> conservative disambiguation.
        if not self._search_matched:
            chosen, score, reason = match_search_choice(
                enabled,
                self.job.target,
                match_year=self.job.match_year,
                media_kind=self.job.media_kind,
            )
            if chosen is None:
                raise ScheduleNeedsReview(reason)
            self._search_matched = True
            self.matched_title_label = chosen.label
            self._log(f"matched '{chosen.label}' (score={score:.2f}, {reason})")
            return chosen.value

        # 2. Season selection step.
        if _is_season_picker(ask, enabled):
            wanted_seasons = _target_seasons_from_expr(self.job.episode_expr)
            if self.expected_episode_keys and self.job.completed_episodes:
                missing = _missing_expected_episodes(self.expected_episode_keys, self.job.completed_episodes)
                pending_seasons = {int(m[1:m.index("E")]) for m in missing if m.startswith("S")}
                if pending_seasons:
                    wanted_seasons = [season for season in wanted_seasons if season in pending_seasons]
            by_num: dict[int, Choice] = {}
            for choice in enabled:
                num = _extract_season_number(f"{choice.label} {choice.detail}")
                if num is not None:
                    by_num.setdefault(num, choice)
            if wanted_seasons:
                for num in wanted_seasons:
                    if num in by_num:
                        picked = by_num[num]
                        self._picked_season = num
                        self._log(f"selected season '{picked.label}'")
                        return picked.value
                raise ScheduleNotReleased(
                    f"Season S{wanted_seasons[0]:02d} is not available yet."
                )
            if self.job.selection_mode == "new_episodes" and by_num:
                latest_num = max(by_num)
                picked = by_num[latest_num]
                self._picked_season = latest_num
                self._log(f"watch mode selected latest season '{picked.label}'")
                return picked.value
            first_num = _extract_season_number(f"{enabled[0].label} {enabled[0].detail}")
            if first_num is not None:
                self._picked_season = first_num
            return enabled[0].value

        # 3. Single-episode picker when episode_expr is set.
        if self.job.episode_expr:
            selection = ranges.parse(self.job.episode_expr)
            if selection is not None:
                entries = self._read_entries(enabled)
                wanted_rows = sorted(selection.rows(entries))
                if wanted_rows:
                    done_set = set(self.job.completed_episodes)
                    if self._partial_range_retry or self.job.selection_mode == "new_episodes":
                        wanted_rows = [
                            row
                            for row in wanted_rows
                            if not self._already_delivered(_episode_ledger_key(entries[row - 1], enabled[row - 1]), done_set)
                        ]
                        if not wanted_rows:
                            missing = _missing_expected_episodes(
                                self.expected_episode_keys, self.job.completed_episodes
                            )
                            if missing:
                                raise ScheduleNotReleased(
                                    f"Waiting for remaining episodes: {', '.join(missing[:8])}."
                                )
                            raise ScheduleNotReleased("No new episodes available yet.")
                    row_idx = wanted_rows[0] - 1
                    picked = enabled[row_idx]
                    key = _episode_ledger_key(entries[row_idx], picked)
                    if key not in self.selected_episode_keys:
                        self.selected_episode_keys.append(key)
                    return picked.value
                if any(e.marked for e in entries):
                    raise ScheduleNotReleased(
                        f"Episode '{self.job.episode_expr}' is not available yet."
                    )

        # 4. Watch mode on a single-episode list: choose first unseen episode.
        if self.job.selection_mode == "new_episodes":
            entries = self._read_entries(enabled)
            if any(e.marked for e in entries):
                done_set = set(self.job.completed_episodes)
                for entry, choice in zip(entries, enabled, strict=True):
                    key = _episode_ledger_key(entry, choice)
                    if key not in done_set:
                        self.selected_episode_keys.append(key)
                        return choice.value
                raise ScheduleNotReleased("No new episodes available yet.")

        # 5. Default fallback for auxiliary pickers (e.g. edition/stream/audio version).
        return enabled[0].value

    def _handle_multi_pick(self, ask: Pick, enabled: list[Choice]) -> list[Any]:
        entries = self._read_entries(enabled)

        # Start with expression-filtered rows when provided.
        candidate_indices: list[int]
        if self.job.episode_expr:
            selection = ranges.parse(self.job.episode_expr)
            if selection is None:
                raise ScheduleNeedsReview(f"Invalid episode expression: {self.job.episode_expr}")
            wanted_rows = sorted(selection.rows(entries))
            if not wanted_rows:
                raise ScheduleNotReleased(
                    f"Episodes matching '{self.job.episode_expr}' are not available yet."
                )
            candidate_indices = [row - 1 for row in wanted_rows if 1 <= row <= len(enabled)]
        elif self.job.selection_mode == "movie":
            if ask.preselected:
                candidate_indices = [i for i in ask.preselected if 0 <= i < len(enabled)]
            else:
                candidate_indices = [0]
        else:
            candidate_indices = list(range(len(enabled)))

        if self.job.selection_mode == "new_episodes" or self._partial_range_retry:
            done_set = set(self.job.completed_episodes)
            fresh: list[int] = []
            for idx in candidate_indices:
                key = _episode_ledger_key(entries[idx], enabled[idx])
                if not self._already_delivered(key, done_set):
                    fresh.append(idx)
            if not fresh:
                if self.job.selection_mode == "new_episodes":
                    raise ScheduleNotReleased("No new episodes available yet.")
                missing = _missing_expected_episodes(
                    self.expected_episode_keys, self.job.completed_episodes
                )
                raise ScheduleNotReleased(
                    f"Waiting for remaining episodes: {', '.join(missing[:8])}."
                    if missing
                    else f"Episodes matching '{self.job.episode_expr}' are not available yet."
                )
            candidate_indices = fresh

        chosen_values: list[Any] = []
        for idx in candidate_indices:
            choice = enabled[idx]
            chosen_values.append(choice.value)
            key = _episode_ledger_key(entries[idx], choice)
            if key not in self.selected_episode_keys:
                self.selected_episode_keys.append(key)
        self._log(f"selected {len(chosen_values)} item(s): {', '.join(self.selected_episode_keys[:8])}")
        return chosen_values


# --------------------------------------------------------------- job execution


@dataclass(frozen=True)
class ScheduleProgress:
    """Live execution snapshot for an active scheduled download job."""

    job_id: str
    stage: str = "starting"
    item_index: int = 0
    item_total: int = 0
    item_name: str = ""
    rows: tuple[str, ...] = ()
    last_log: str = ""
    started_at: float = 0.0


@dataclass
class JobExecutionResult:
    job: ScheduledJob
    record: ScheduleRunRecord
    dry_run: bool = False
    ok: bool = False


def preflight_service(
    service_id: str,
    *,
    config: Config,
    settings_store: SettingsStore,
    globals_scope: Settings,
    service_registry=default_registry,
) -> tuple[bool, str, str]:
    """Quick, non-mutating readiness summary: ``(ready, status_code, message)``.

    ``status_code`` is one of ``ready`` | ``missing_service`` | ``missing_helper``
    | ``needs_auth``.
    """
    service_cls = service_registry.for_id(service_id)
    if service_cls is None:
        return False, "missing_service", f"Service '{service_id}' is not installed."
    try:
        service = service_registry.build(
            service_cls, config, settings_store, globals_scope=globals_scope
        )
        if not service.ctx.helpers.ready:
            return False, "missing_helper", service.ctx.helpers.blocking_message()
        auth = service._auth_status_quietly()
    except Exception as exc:  # noqa: BLE001
        return False, "error", str(exc) or type(exc).__name__
    if not auth.usable:
        return False, "needs_auth", auth.detail or auth.label or "Sign-in required"
    cdm_label = ""
    try:
        name = service.cdm_name()
        level = service.cdm_level()
        if name:
            cdm_label = f"{name} ({level})" if level else name
    except Exception:
        cdm_label = ""
    detail = f"Ready · {cdm_label}" if cdm_label else "Ready"
    return True, "ready", detail


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ScheduleCancelled("Stopped by user.")


def _release_scheduled(service, playback, log) -> None:
    release = getattr(service, "release_playback", None)
    if callable(release) and getattr(release, "__func__", None) is not Service.release_playback:
        try:
            with playback_cleanup_scope():
                release(playback, log)
        except Exception as exc:
            log(f"Could not release scheduled playback: {redact(str(exc))}")


def _deliver_playback(
    engine: Engine,
    service: Service,
    playback: Any,
    *,
    action: str,
    cancel: threading.Event | None,
    log: Callable[[str], None],
    export_path: Path | None,
    batch: bool,
    on_stage: Callable[[str], None] | None = None,
    on_frame: Callable[[Sequence[str]], None] | None = None,
) -> tuple[list[str], Path | None]:
    settings = service.ctx.settings
    artifacts: list[str] = []
    next_export_path = export_path
    prev_frame = getattr(engine, "frame", None)
    prev_cols = getattr(engine, "frame_columns", None)
    if on_frame is not None:
        engine.frame = on_frame
        engine.frame_columns = 60

    def _stage(name: str) -> None:
        _check_cancel(cancel)
        if callable(on_stage):
            try:
                on_stage(name)
            except Exception:
                pass
        _check_cancel(cancel)

    try:
        _stage("tracks")
        tracks = engine.load_tracks(playback, settings, service=service)
        selection_error = str(getattr(tracks, "selection_error", "") or "")
        if selection_error and not getattr(tracks, "selected", None):
            strict_wait = any(
                bool(settings.get(key, False))
                for key in ("require_video", "require_audio", "require_subtitles")
            )
            if strict_wait:
                raise ScheduleNotReleased(f"Waiting for required tracks: {selection_error}")
            raise ScheduleError(selection_error)
        _stage("keys")
        engine.resolve_keys(playback, service, tracks, settings)
        sync_sidecars = getattr(engine, "sync_sidecar_selection", None)
        if callable(sync_sidecars):
            sync_sidecars(playback, tracks)
        if action == "export":
            _stage("exporting")
            written = engine.export_document(
                playback,
                tracks,
                settings=settings,
                service_id=service.ID,
                service_name=service.NAME,
                path=export_path,
                batch=batch,
                export_manifest_type=settings.get(exports.EXPORT_MANIFEST_TYPE_KEY, exports.MASTER_MANIFEST),
            )
            next_export_path = written
            artifacts.append(str(written))
            log(f"exported: {written}")
        elif action == "command":
            _stage("exporting")
            written = engine.export_command(
                playback,
                settings,
                tracks,
                service_id=service.ID,
                service=service,
            )
            artifacts.append(str(written))
            log(f"command saved: {written}")
        else:
            _stage("sidecars")
            prepare_sidecars = getattr(engine, "prepare_download_sidecars", None)
            if callable(prepare_sidecars):
                prepare_sidecars(
                    playback,
                    settings,
                    service,
                    log,
                    tracks,
                    cancel=cancel,
                )
            else:
                service.prepare_download(playback, log)
            if callable(sync_sidecars):
                sync_sidecars(playback, tracks)
            _stage("downloading")
            if cancel is not None and cancel.is_set():
                raise ScheduleCancelled("Stopped by user.")
            result = engine.run(
                playback,
                settings,
                tracks,
                cancel=cancel,
                service=service,
            )
            if not result.ok:
                if cancel is not None and cancel.is_set():
                    raise ScheduleCancelled("Stopped by user.")
                raise ScheduleError(result.failure or f"Download failed (exit {result.exit_code}).")
            if result.artifacts:
                artifacts.extend(str(path) for path in result.artifacts)
            else:
                artifacts.append(str(result.output_dir / playback.save_name))
            log(f"downloaded: {playback.save_name}")
    finally:
        if on_frame is not None:
            engine.frame = prev_frame
            engine.frame_columns = prev_cols
        _release_scheduled(service, playback, log)
    return artifacts, next_export_path


def _delivered_episode_key(playback: Any, presenter: ScheduledFlowPresenter) -> str:
    """Use the delivered item, not the index of a possibly skipped/reordered pick."""
    title = getattr(playback, "title", None)
    name = str(getattr(playback, "save_name", "") or getattr(title, "name", "") or "")
    entry = ranges.read_entry(1, name)
    episode = getattr(title, "episode", None)
    if episode is None:
        episode = entry.episode
    if episode is not None:
        season = getattr(title, "season", None)
        if season is None:
            season = entry.season if entry.season is not None else presenter._picked_season
        if str(episode).isdigit() and (season is None or str(season).isdigit()):
            return (f"S{int(season):02d}" if season is not None else "") + f"E{int(episode):02d}"
    return _clean_title(name)[:64]


def execute_scheduled_job(
    job: ScheduledJob,
    *,
    config: Config,
    settings_store: SettingsStore,
    globals_scope: Settings,
    store: ScheduleStore | None = None,
    vault: KeyVault | None = None,
    vault_collection=None,
    service_registry=default_registry,
    dry_run: bool = False,
    cancel: threading.Event | None = None,
    now: datetime | None = None,
    on_log: Callable[[str], None] | None = None,
    on_start: Callable[[], None] | None = None,
    on_progress: Callable[[ScheduleProgress], None] | None = None,
) -> JobExecutionResult:
    """Run one scheduled job (or dry-run preflight + lookup without downloading)."""
    started = (now or utc_now()).astimezone(timezone.utc)
    started_mono = time.monotonic()
    prog_lock = threading.Lock()
    prog_state: dict[str, Any] = {
        "stage": "starting",
        "item_index": 0,
        "item_total": 0,
        "item_name": "",
        "rows": (),
        "last_log": "",
    }

    def _emit_progress(**updates: Any) -> None:
        if on_progress is None:
            return
        with prog_lock:
            prog_state.update(updates)
            snap = ScheduleProgress(
                job_id=job.id,
                stage=str(prog_state["stage"]),
                item_index=int(prog_state["item_index"]),
                item_total=int(prog_state["item_total"]),
                item_name=str(prog_state["item_name"]),
                rows=tuple(prog_state["rows"]),
                last_log=str(prog_state["last_log"]),
                started_at=started_mono,
            )
        try:
            on_progress(snap)
        except Exception:
            pass

    def _handle_log(line: str) -> None:
        if on_log is not None:
            on_log(line)
        clean = str(line or "").strip()
        if clean:
            _emit_progress(last_log=clean)

    sink = _ListSink(_handle_log)
    owns_vault = vault is None
    if vault is None:
        vault = KeyVault(config.paths.keys_db)
    if vault_collection is None:
        vault_collection = vaults.build(config, local=vault)

    artifacts: list[str] = []
    episodes: list[str] = []
    completed_this_run: list[str] = []
    pending_release: dict[int, Any] = {}
    expected_episodes: list[str] | None = _expected_episode_keys(job.episode_expr)
    initial_enabled = bool(job.enabled)
    initial_run_at = str(job.run_at or "")
    status = "done"
    summary = ""

    def _record_completed_keys(keys: Sequence[str]) -> None:
        fresh_keys: list[str] = []
        for ep in keys:
            if ep and ep not in completed_this_run:
                completed_this_run.append(ep)
            if ep and ep not in job.completed_episodes:
                job.completed_episodes.append(ep)
                fresh_keys.append(ep)
        if fresh_keys and not dry_run and store is not None:
            def _append_eps(current: ScheduledJob, eps: tuple[str, ...] = tuple(fresh_keys)) -> None:
                for ep in eps:
                    if ep and ep not in current.completed_episodes:
                        current.completed_episodes.append(ep)

            store.update_job(job.id, _append_eps)

    try:
        service_cls = service_registry.for_id(job.service_id)
        if service_cls is None:
            raise ScheduleError(f"Service '{job.service_id}' is not installed.")
        if job.input_mode == "url" and not getattr(service_cls, "SUPPORTS_URL", True):
            raise ScheduleError(f"Service '{service_cls.NAME}' does not support direct URL input.")
        if job.input_mode == "search" and not getattr(service_cls, "SUPPORTS_SEARCH", True):
            raise ScheduleError(f"Service '{service_cls.NAME}' does not support search.")

        service = service_registry.build(
            service_cls, config, settings_store, globals_scope=globals_scope
        )
        if not service.ctx.helpers.ready:
            raise ScheduleError(service.ctx.helpers.blocking_message())

        auth = service._auth_status_quietly()
        if not auth.usable:
            raise ScheduleAuthRequired(
                f"{service_cls.NAME} is not signed in ({auth.detail or auth.label})."
            )

        if not dry_run and store is not None:
            job.status = "running"

            def _mark_running(current: ScheduledJob) -> None:
                current.status = "running"

            store.update_job(job.id, _mark_running)
            _emit_progress(stage="lookup")
            if callable(on_start):
                try:
                    on_start()
                except Exception:
                    pass
        else:
            _emit_progress(stage="lookup")

        presenter = ScheduledFlowPresenter(job, log=sink.log)
        expected_episodes = presenter.expected_episode_keys
        flow_ctx = FlowContext(settings=service.ctx.settings, sink=sink, interactive=False)
        if job.input_mode == "url":
            flow = service.open_url(flow_ctx, job.target)
        else:
            flow = service.search(flow_ctx, job.target)

        def _emitted(pb):
            if dry_run:
                _release_scheduled(service, pb, sink.log)
            else:
                pending_release[id(pb)] = pb
            _check_cancel(cancel)

        playbacks = run_flow(flow, presenter, on_emit=_emitted)
        if cancel is not None and cancel.is_set():
            raise ScheduleCancelled("Stopped by user.")
        if not playbacks:
            raise ScheduleNotReleased("No matching playable titles or episodes are available yet.")

        if dry_run:
            for pb in playbacks:
                key = _delivered_episode_key(pb, presenter)
                if key and key not in episodes:
                    episodes.append(key)
            status = "dry_run"
            names = [str(getattr(pb, "save_name", "") or "item") for pb in playbacks[:5]]
            more = f" (+{len(playbacks) - 5} more)" if len(playbacks) > 5 else ""
            summary = f"Dry-run OK: matched {len(playbacks)} item(s) — {', '.join(names)}{more}"
            sink.log(summary)
        else:
            engine = Engine(
                config,
                vault=vault,
                vault_collection=vault_collection,
                log=sink.log,
            )
            export_path: Path | None = None
            batch = len(playbacks) > 1
            total_items = len(playbacks)
            for idx, pb in enumerate(playbacks, 1):
                if cancel is not None and cancel.is_set():
                    raise ScheduleCancelled("Stopped by user.")
                pb_name = str(getattr(pb, "save_name", "") or getattr(getattr(pb, "title", None), "name", "") or job.name)
                _emit_progress(
                    stage="tracks",
                    item_index=idx,
                    item_total=total_items,
                    item_name=pb_name,
                    rows=(),
                )
                # Delivery owns cleanup from here; unvisited items are still ours.
                pending_release.pop(id(pb), None)
                pb_artifacts, export_path = _deliver_playback(
                    engine,
                    service,
                    pb,
                    action=job.action,
                    cancel=cancel,
                    log=sink.log,
                    export_path=export_path,
                    batch=batch,
                    on_stage=(lambda st: _emit_progress(stage=st)) if on_progress is not None else None,
                    on_frame=(
                        lambda rows: _emit_progress(
                            rows=tuple(str(r) for r in rows if str(r).strip())
                        )
                    )
                    if on_progress is not None
                    else None,
                )
                artifacts.extend(pb_artifacts)
                key = _delivered_episode_key(pb, presenter)
                if key:
                    _record_completed_keys([key])
            episodes = list(completed_this_run)
            missing = _missing_expected_episodes(expected_episodes, job.completed_episodes)
            if missing:
                status = "waiting_release"
                more_missing = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
                summary = (
                    f"Completed {len(playbacks)} item(s) via {service_cls.NAME} ({job.action}); "
                    f"waiting for {', '.join(missing[:8])}{more_missing}."
                )
                sink.log(summary, "info")
            else:
                status = "done"
                summary = f"Completed {len(playbacks)} item(s) via {service_cls.NAME} ({job.action})."

    except ScheduleAuthRequired as exc:
        status = "needs_auth"
        summary = str(exc)
        sink.log(summary, "error")
    except ScheduleNeedsReview as exc:
        status = "needs_review"
        summary = str(exc)
        sink.log(summary, "warning")
    except ScheduleNotReleased as exc:
        status = "waiting_release"
        summary = str(exc)
        sink.log(summary, "info")
    except ScheduleCancelled as exc:
        status = "paused"
        summary = str(exc) or "Stopped by user."
        sink.log(summary, "warning")
    except (Back, ScheduleError) as exc:
        if cancel is not None and cancel.is_set():
            status = "paused"
            summary = "Stopped by user."
            sink.log(summary, "warning")
        else:
            status = "failed"
            summary = str(exc) or type(exc).__name__
            sink.log(summary, "error")
    except Exception as exc:  # noqa: BLE001
        if cancel is not None and cancel.is_set():
            status = "paused"
            summary = "Stopped by user."
            sink.log(summary, "warning")
        else:
            status = "failed"
            summary = f"{type(exc).__name__}: {exc}"
            sink.log(summary, "error")
    finally:
        for pb in pending_release.values():
            _release_scheduled(service, pb, sink.log)
        if owns_vault:
            try:
                vault_collection.close()
            except Exception:
                pass

    if not dry_run and not episodes and completed_this_run:
        episodes = list(completed_this_run)

    finished = utc_now()
    final_status = status

    def _apply_outcome(target: ScheduledJob) -> None:
        nonlocal final_status
        target.last_run_at = format_iso_utc(finished)
        for ep in completed_this_run:
            if ep and ep not in target.completed_episodes:
                target.completed_episodes.append(ep)
        paused_by_user = (not target.enabled) and (initial_enabled or target.status == "paused")
        if status == "done":
            target.last_error = ""
            target.window_started_at = ""
            if target.repeat == "once":
                target.attempts += 1
                target.status = "done"
                target.enabled = False
                target.next_attempt_at = ""
            else:
                if target.run_at == initial_run_at:
                    next_ts = advance_repeat_time(
                        target.run_at or format_iso_utc(finished),
                        target.repeat,
                        now=finished,
                    )
                    target.run_at = next_ts
                    target.next_attempt_at = next_ts
                else:
                    target.next_attempt_at = target.run_at
                target.attempts = 0
                if paused_by_user:
                    target.enabled = False
                    target.status = "paused"
                else:
                    target.status = "scheduled"
        elif status == "waiting_release":
            target.attempts += 1
            target.last_error = redact(summary)
            window_start = parse_iso_utc(target.window_started_at) or started
            target.window_started_at = format_iso_utc(window_start)
            window_end = window_start + timedelta(hours=target.retry_window_hours)
            if paused_by_user:
                target.enabled = False
                target.status = "paused"
                target.next_attempt_at = ""
            elif target.repeat == "watch":
                target.status = "waiting_release"
                target.next_attempt_at = format_iso_utc(
                    finished + timedelta(minutes=target.retry_interval_minutes)
                )
            elif target.retry_window_hours > 0 and finished < window_end:
                target.enabled = True
                target.status = "waiting_release"
                next_try = min(
                    window_end,
                    finished + timedelta(minutes=target.retry_interval_minutes),
                )
                target.next_attempt_at = format_iso_utc(next_try)
            else:
                final_status = "failed"
                target.status = "failed"
                target.last_error = redact(f"Release window expired: {summary}")
                target.next_attempt_at = ""
                if target.repeat == "once":
                    target.enabled = False
        elif status == "paused":
            target.attempts += 1
            target.last_error = redact(summary)
            target.next_attempt_at = ""
            target.enabled = False
            target.status = "paused"
        elif status in {"needs_auth", "needs_review"}:
            target.attempts += 1
            target.last_error = redact(summary)
            target.next_attempt_at = ""
            if paused_by_user:
                target.enabled = False
                target.status = "paused"
            else:
                target.status = status
        else:
            target.attempts += 1
            target.last_error = redact(summary)
            target.next_attempt_at = ""
            if paused_by_user:
                target.enabled = False
                target.status = "paused"
            else:
                target.status = "failed"
                if target.repeat == "once":
                    target.enabled = False

    if not dry_run:
        if store is not None:
            merged = store.update_job(job.id, _apply_outcome)
            if merged is not None:
                job.__dict__.update(merged.__dict__)
            else:
                _apply_outcome(job)
        else:
            _apply_outcome(job)
        status = final_status

    record = ScheduleRunRecord(
        job_id=job.id,
        job_name=job.name,
        service_id=job.service_id,
        started_at=format_iso_utc(started),
        finished_at=format_iso_utc(finished),
        status=status,
        summary=redact(summary),
        artifacts=[redact(item) for item in artifacts],
        episodes=episodes,
        logs=sink.lines,
    )
    if store is not None:
        store.append_history(record)

    return JobExecutionResult(
        job=job,
        record=record,
        dry_run=dry_run,
        ok=status in {"done", "dry_run", "waiting_release"},
    )


def run_due_jobs(
    *,
    config: Config,
    job_id: str | None = None,
    dry_run: bool = False,
    service_registry=default_registry,
    now: datetime | None = None,
    on_log: Callable[[str], None] | None = None,
) -> list[JobExecutionResult]:
    """Run all due scheduled jobs (or one explicit ``job_id``) under a runner lock."""
    store = ScheduleStore(schedule_store_path(config.paths.home))
    settings_store = SettingsStore(config.paths.home / "settings.json")
    globals_scope = global_settings(settings_store, config=config)

    with try_runner_lock(config.paths.home) as acquired:
        if not acquired:
            if on_log:
                on_log("Scheduler lock held by another running UniDL process; skipping tick.")
            return []
        store.recover_stale_running()
        if job_id:
            target = store.get_job(job_id)
            jobs = [target] if target is not None else []
        else:
            jobs = store.due_jobs(now=now)
        results: list[JobExecutionResult] = []
        for job in jobs:
            if not job_id:
                fresh = store.get_job(job.id)
                if fresh is None or not fresh.is_due(now):
                    continue
                job = fresh
            results.append(
                execute_scheduled_job(
                    job,
                    config=config,
                    settings_store=settings_store,
                    globals_scope=globals_scope,
                    store=store,
                    service_registry=service_registry,
                    dry_run=dry_run,
                    now=now,
                    on_log=on_log,
                )
            )
        return results


__all__ = [
    "ACTIONS",
    "DEFAULT_PLATFORM_RELEASE_HOURS_UTC",
    "INPUT_MODES",
    "JOB_STATUSES",
    "MEDIA_KINDS",
    "REPEAT_MODES",
    "SELECTION_MODES",
    "JobExecutionResult",
    "ScheduleAuthRequired",
    "ScheduleError",
    "ScheduleNeedsReview",
    "ScheduleNotReleased",
    "ScheduleProgress",
    "ScheduleRunRecord",
    "ScheduleStore",
    "ScheduledFlowPresenter",
    "ScheduledJob",
    "advance_repeat_time",
    "default_release_hour_utc",
    "execute_scheduled_job",
    "format_countdown",
    "format_iso_utc",
    "format_local_time",
    "match_search_choice",
    "parse_iso_utc",
    "parse_run_at",
    "preflight_service",
    "run_due_jobs",
    "schedule_store_path",
    "try_runner_lock",
    "utc_now",
]
