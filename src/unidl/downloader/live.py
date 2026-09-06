from __future__ import annotations

import re
import shutil
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .console import Palette, color_enabled, paint
from .downloader import KNOWN_OUTPUT_SUFFIXES, HlsCrypto, _fetch_bytes, _HostAdaptiveLimiter, _should_prefer_curl
from .embedding import DownloadCancelled, current_download_runtime
from .ism_live import stream_ism_timescale, stream_uses_ism_timeline_index
from .live_rules import (
    live_media_retry_attempts,
    live_media_retry_grace_enabled,
    repeated_live_media_duration_seconds,
    should_repeat_live_media_request,
)
from .loader import LoadError, load_text
from .models import SegmentInfo, StreamInfo
from .parser import parse_source
from .sabr_ump import SabrUmpError, fetch_sabr_ump_live_segments


@dataclass(slots=True)
class LiveRecordOptions:
    limit_seconds: float | None = None
    wait_seconds: int | None = None
    take_count: int = 16
    dvr_from_start: bool = False
    dvr_start_offset: float | None = None
    dvr_end_offset: float | None = None
    keep_segments: bool = True
    real_time_merge: bool = True
    workers: int = 16
    retries: int = 3
    request_timeout: int = 30
    check_segments_count: bool = True
    temp_dir: Path | None = None
    colors: bool | None = None
    progress: Callable[[LiveProgressUpdate], None] | None = None
    cancel_requested: Callable[[], bool] | None = None
    pause_requested: Callable[[], bool] | None = None
    segment_sink: Callable[[LiveSegmentBatch], None] | None = None
    group_segment_sink: Callable[[list[LiveSegmentBatch]], None] | None = None
    segment_key_observer: Callable[[StreamInfo, list[SegmentInfo]], None] | None = None
    stream_transform: Callable[[list[StreamInfo]], None] | None = None
    hls_crypto: HlsCrypto | None = None
    stop_on_interrupt: bool = False
    assemble_output: bool | Callable[[StreamInfo], bool] = True


@dataclass(slots=True)
class LiveRecordResult:
    stream: StreamInfo
    path: Path
    recorded_seconds: float
    segments_count: int
    temp_dir: Path | None = None
    parts: list[Path] | None = None
    segments: list[SegmentInfo] | None = None


@dataclass(frozen=True, slots=True)
class LiveProgressUpdate:
    stream: StreamInfo
    recorded_seconds: float
    limit_seconds: float | None
    segments_count: int
    new_segments: int
    path: Path
    available_seconds: float | None = None
    available_segments: int | None = None
    status: str = "Waiting"
    done: bool = False
    downloaded_bytes: int = 0
    speed_bytes_per_second: float | None = None


@dataclass(frozen=True, slots=True)
class LiveSegmentBatch:
    stream: StreamInfo
    segments: list[SegmentInfo]
    parts: list[Path]
    output_path: Path
    temp_dir: Path


def _runtime_sleep(seconds: float) -> None:
    """Sleep until a retry/refresh deadline, waking when UniDL shuts down."""
    runtime = current_download_runtime()
    if runtime is None:
        time.sleep(max(0.0, float(seconds)))
    else:
        if runtime.wait(seconds):
            runtime.checkpoint()


def _raise_if_cancelled(options: LiveRecordOptions) -> None:
    callback = options.cancel_requested
    if callback is not None and callback():
        raise DownloadCancelled("live recording cancelled")


def _wait_if_paused(options: LiveRecordOptions) -> None:
    paused = options.pause_requested
    if paused is None:
        return
    while paused():
        _raise_if_cancelled(options)
        _runtime_sleep(0.1)


def _checkpoint(options: LiveRecordOptions) -> None:
    _raise_if_cancelled(options)
    _wait_if_paused(options)


def _wait_for_refresh(options: LiveRecordOptions, seconds: float) -> None:
    """Wait for the next manifest refresh while remaining promptly cancellable."""

    deadline = time.monotonic() + max(0.0, float(seconds or 0))
    while True:
        _checkpoint(options)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        _runtime_sleep(min(0.25, remaining))


@dataclass(slots=True)
class _LiveRecordState:
    stream: StreamInfo
    current_stream: StreamInfo
    output_path: Path
    temp_dir: Path
    seen: set[str]
    init_written: set[str]
    edge_index: int | None
    recorded_seconds: float
    segments_count: int
    downloaded_bytes: int
    available_seconds: float
    available_count: int
    parts: list[Path]
    segments: list[SegmentInfo]
    pending_media: list[SegmentInfo]


@dataclass(frozen=True, slots=True)
class _LivePlannedBatch:
    state: _LiveRecordState
    segments: list[SegmentInfo]
    media_segments: list[SegmentInfo]


class _DashPatchNoUpdate(Exception):
    pass


def parse_live_limit(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip().lower()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    if ":" in value:
        parts = [float(part) for part in value.split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
    total = 0.0
    number = ""
    for char in value:
        if char.isdigit() or char == ".":
            number += char
            continue
        if not number:
            continue
        amount = float(number)
        if char == "h":
            total += amount * 3600
        elif char == "m":
            total += amount * 60
        elif char == "s":
            total += amount
        number = ""
    if number:
        total += float(number)
    return total or None


def record_live_stream_group(
    streams: list[StreamInfo],
    output_dir: str | Path,
    filenames: list[str | None] | None = None,
    headers: dict[str, str] | None = None,
    options: LiveRecordOptions | None = None,
) -> list[LiveRecordResult]:
    options = options or LiveRecordOptions()
    _ensure_live_json_init_segments(streams)
    names = filenames or []
    states = [_new_live_state(stream, output_dir, names[index] if index < len(names) else None, options) for index, stream in enumerate(streams)]
    _refresh_sabr_live_states(states, options, headers=headers)
    wait_seconds = options.wait_seconds or _default_group_wait_seconds(states)
    record_limit = _effective_record_limit(options)
    failed = False
    interrupted = False
    refresh_failed = False
    stagnant_refreshes = 0
    host_limiter = _HostAdaptiveLimiter(max_parallel=max(1, int(options.workers or 1)))

    for state in states:
        _emit_state_progress(options, state, 0, "Waiting")

    try:
        while True:
            _checkpoint(options)
            planned = _plan_group_batches(states, options)
            if planned:
                stagnant_refreshes = 0
                workers = max(1, int(options.workers or 1))
                if options.segment_key_observer:
                    for plan in planned:
                        options.segment_key_observer(plan.state.stream, plan.segments)
                for plan in planned:
                    _emit_state_progress(options, plan.state, len(plan.media_segments), "Recording")
                pool = ThreadPoolExecutor(max_workers=len(planned))
                futures = {}
                try:
                    futures = {
                        pool.submit(
                            _append_segments,
                            plan.segments,
                            plan.state.output_path,
                            plan.state.temp_dir,
                            headers,
                            workers,
                            _should_prefer_curl(plan.state.stream),
                            options.hls_crypto,
                            live_media_retry_attempts(plan.state.stream, options.retries),
                            max(1, int(options.request_timeout or 30)),
                            _batch_progress_callback(
                                options,
                                plan.state.stream,
                                plan.state.output_path,
                                plan.state.recorded_seconds,
                                plan.state.segments_count,
                                plan.state.downloaded_bytes,
                                plan.media_segments,
                                plan.state.available_seconds,
                                plan.state.available_count,
                            ),
                            options.check_segments_count,
                            host_limiter,
                            _should_assemble_live_output(options, plan.state.stream),
                            live_media_retry_grace_enabled(plan.state.stream),
                        ): plan
                        for plan in planned
                    }
                    completed_parts: dict[int, list[Path]] = {}
                    for future in as_completed(futures):
                        plan = futures[future]
                        completed_parts[id(plan.state)] = future.result()
                except BaseException:
                    _cancel_futures_now(pool, futures)
                    raise
                else:
                    pool.shutdown(wait=True)
                group_batches: list[LiveSegmentBatch] = []
                for plan in planned:
                    parts = completed_parts[id(plan.state)]
                    _apply_group_batch(options, plan, parts, emit_segment_sink=options.group_segment_sink is None)
                    group_batches.append(_live_segment_batch(plan, parts))
                if options.group_segment_sink:
                    options.group_segment_sink(group_batches)

            if record_limit is not None and all(state.recorded_seconds >= record_limit for state in states):
                break
            if all(not state.current_stream.is_live and not _has_unseen(state.current_stream, state.seen) for state in states):
                break

            wait_status = "AdBreak" if refresh_failed else "Waiting"
            for state in states:
                _emit_state_progress(options, state, 0, wait_status)
            _wait_for_refresh(options, wait_seconds)
            refresh_failed = not _refresh_group_states(states, options, headers=headers)
            if refresh_failed:
                stagnant_refreshes += 1
            elif any(_state_has_new_live_media(state, options) for state in states):
                stagnant_refreshes = 0
            elif any(state.segments_count for state in states):
                stagnant_refreshes += 1
            wait_seconds = options.wait_seconds or _default_group_wait_seconds(states)
    except DownloadCancelled:
        interrupted = True
        raise
    except KeyboardInterrupt:
        interrupted = True
        if not options.stop_on_interrupt:
            raise
    except Exception:
        failed = True
        raise
    finally:
        status = "Error" if failed else ("Stopped" if interrupted else "Done")
        for state in states:
            _emit_state_progress(options, state, 0, status, done=not failed and not interrupted)

    return [
        LiveRecordResult(
            stream=state.stream,
            path=state.output_path,
            recorded_seconds=state.recorded_seconds,
            segments_count=state.segments_count,
            temp_dir=state.temp_dir,
            parts=state.parts,
            segments=state.segments,
        )
        for state in states
    ]


def record_live_stream(
    stream: StreamInfo,
    output_dir: str | Path,
    filename: str | None = None,
    headers: dict[str, str] | None = None,
    options: LiveRecordOptions | None = None,
) -> LiveRecordResult:
    options = options or LiveRecordOptions()
    _ensure_live_json_init_segments([stream])
    output_path = _live_output_path(stream, output_dir, filename)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = _live_temp_dir(output_path, options)

    seen: set[str] = set()
    init_written: set[str] = set()
    recorded_seconds = 0.0
    recorded_count = 0
    recorded_bytes = 0
    available_seconds = 0.0
    available_count = 0
    part_paths: list[Path] = []
    downloaded_segments: list[SegmentInfo] = []
    current_stream = stream
    _refresh_sabr_live_stream_segments(
        current_stream,
        first_sequence=None,
        take_count=max(1, int(options.take_count or 1)),
        headers=headers,
    )
    edge_index: int | None = None
    wait_seconds = options.wait_seconds or _default_wait_seconds(current_stream)
    record_limit = _effective_record_limit(options)
    colors = color_enabled() if options.colors is None else options.colors
    failed = False
    interrupted = False
    refresh_failed = False
    stagnant_refreshes = 0
    host_limiter = _HostAdaptiveLimiter(max_parallel=max(1, int(options.workers or 1)))
    initial_media_segments = [segment for segment in current_stream.segments if segment.index != -1]
    initial_display_segments = _live_display_segments(initial_media_segments, recorded_count, options)
    available_seconds = sum(segment.duration or 0 for segment in initial_display_segments)
    available_count = len(initial_display_segments)

    if options.progress:
        _emit_live_progress(options, stream, recorded_seconds, recorded_count, 0, output_path, "Waiting", available_seconds, available_count, downloaded_bytes=recorded_bytes)
    else:
        print(f"{paint('Live recording:', Palette.green, colors)} {stream.format_line()}")
        print(f"{paint('Refresh interval:', Palette.blue, colors)} {wait_seconds}s")
        if record_limit:
            print(f"{paint('Record limit:', Palette.blue, colors)} {_format_seconds(record_limit)}")

    try:
        while True:
            _checkpoint(options)
            media_segments = [segment for segment in current_stream.segments if segment.index != -1]
            init_segments = [segment for segment in current_stream.segments if segment.index == -1]
            new_segments = _new_live_media_segments(media_segments, seen, edge_index, recorded_count, options, stream=current_stream)
            new_segments = _cap_segments_for_limit(new_segments, recorded_seconds, record_limit)
            available_seconds, available_count = _live_available_totals(
                media_segments,
                new_segments,
                recorded_seconds,
                recorded_count,
                options,
            )

            batch: list[SegmentInfo] = []
            if new_segments:
                for init_segment in init_segments:
                    key = _segment_key(init_segment)
                    if key not in init_written:
                        batch.append(init_segment)
                        init_written.add(key)
                batch.extend(new_segments)

            if batch:
                stagnant_refreshes = 0
                if options.segment_key_observer:
                    options.segment_key_observer(stream, batch)
                _emit_live_progress(options, stream, recorded_seconds, recorded_count, len(new_segments), output_path, "Recording", available_seconds, available_count, downloaded_bytes=recorded_bytes)
                batch_parts = _append_segments(
                    batch,
                    output_path,
                    temp_dir,
                    headers=headers,
                    workers=options.workers,
                    prefer_curl=_should_prefer_curl(stream),
                    hls_crypto=options.hls_crypto,
                    retries=live_media_retry_attempts(stream, options.retries),
                    request_timeout=max(1, int(options.request_timeout or 30)),
                    progress=_batch_progress_callback(
                        options,
                        stream,
                        output_path,
                        recorded_seconds,
                        recorded_count,
                        recorded_bytes,
                        new_segments,
                        available_seconds,
                        available_count,
                    ),
                    check_segments_count=options.check_segments_count,
                    host_limiter=host_limiter,
                    assemble_output=_should_assemble_live_output(options, stream),
                    retry_grace=live_media_retry_grace_enabled(stream),
                )
                part_paths.extend(batch_parts)
                downloaded_segments.extend(batch)
                recorded_bytes += _paths_total_size(batch_parts)
                for segment in new_segments:
                    seen.add(_segment_key(segment))
                    edge_index = _max_segment_index(edge_index, segment)
                    recorded_seconds += segment.duration or 0
                    recorded_count += 1
                if options.segment_sink:
                    options.segment_sink(
                        LiveSegmentBatch(
                            stream=stream,
                            segments=list(batch),
                            parts=list(batch_parts),
                            output_path=output_path,
                            temp_dir=temp_dir,
                        )
                    )
                available_seconds, available_count = _live_available_totals(
                    media_segments,
                    [],
                    recorded_seconds,
                    recorded_count,
                    options,
                )
                if options.progress:
                    _emit_live_progress(options, stream, recorded_seconds, recorded_count, len(new_segments), output_path, "Waiting", available_seconds, available_count, downloaded_bytes=recorded_bytes)
                else:
                    print(f"{paint('+', Palette.green, colors)} {len(new_segments)} segments | {_format_seconds(recorded_seconds)} | {output_path.name}")

            if record_limit is not None and recorded_seconds >= record_limit:
                break

            wait_status = "AdBreak" if refresh_failed else "Waiting"
            _emit_live_progress(options, stream, recorded_seconds, recorded_count, 0, output_path, wait_status, available_seconds, available_count, downloaded_bytes=recorded_bytes)
            _wait_for_refresh(options, wait_seconds)
            if _is_sabr_ump_stream(current_stream):
                first_sequence = edge_index + 1 if edge_index is not None else None
                refresh_failed = not _refresh_sabr_live_stream_segments(
                    current_stream,
                    first_sequence=first_sequence,
                    take_count=1,
                    headers=headers,
                )
            else:
                patched = _refresh_dash_patch_stream(current_stream, headers=headers)
                if isinstance(patched, StreamInfo):
                    refresh_failed = False
                    current_stream = patched
                else:
                    refreshed_stream = _refresh_stream_from_candidates(current_stream, options, headers=headers)
                    if refreshed_stream is None:
                        refresh_failed = True
                        stagnant_refreshes += 1
                        continue
                    refresh_failed = False
                    current_stream = refreshed_stream
            if _stream_has_new_live_media(current_stream, seen, edge_index, recorded_count, options):
                stagnant_refreshes = 0
            elif recorded_count:
                stagnant_refreshes += 1
            wait_seconds = options.wait_seconds or _default_wait_seconds(current_stream)
            if not current_stream.is_live and not _has_unseen(current_stream, seen):
                break
    except DownloadCancelled:
        interrupted = True
        raise
    except KeyboardInterrupt:
        interrupted = True
        if not options.progress:
            print("\n" + paint("Live recording stopped by user.", Palette.yellow, colors))
        if not options.stop_on_interrupt:
            raise
    except Exception:
        failed = True
        raise
    finally:
        status = "Error" if failed else ("Stopped" if interrupted else "Done")
        _emit_live_progress(
            options,
            stream,
            recorded_seconds,
            recorded_count,
            0,
            output_path,
            status,
            available_seconds,
            available_count,
            done=not failed and not interrupted,
            downloaded_bytes=recorded_bytes,
        )

    return LiveRecordResult(
        stream=stream,
        path=output_path,
        recorded_seconds=recorded_seconds,
        segments_count=recorded_count,
        temp_dir=temp_dir,
        parts=part_paths,
        segments=downloaded_segments,
    )


def _new_live_state(stream: StreamInfo, output_dir: str | Path, filename: str | None, options: LiveRecordOptions) -> _LiveRecordState:
    output_path = _live_output_path(stream, output_dir, filename)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = _live_temp_dir(output_path, options)
    media_segments = [segment for segment in stream.segments if segment.index != -1]
    display_segments = _live_display_segments(media_segments, 0, options)
    return _LiveRecordState(
        stream=stream,
        current_stream=stream,
        output_path=output_path,
        temp_dir=temp_dir,
        seen=set(),
        init_written=set(),
        edge_index=None,
        recorded_seconds=0.0,
        segments_count=0,
        downloaded_bytes=0,
        available_seconds=sum(segment.duration or 0 for segment in display_segments),
        available_count=len(display_segments),
        parts=[],
        segments=[],
        pending_media=[],
    )


def _ensure_live_json_init_segments(streams: list[StreamInfo]) -> None:
    for stream in streams:
        _ensure_live_json_init_segment(stream)


def _ensure_live_json_init_segment(stream: StreamInfo) -> bool:
    if getattr(stream, "manifest_type", None) != "json":
        return False
    if not getattr(stream, "is_live", False):
        return False
    primary_kid = _stream_primary_key_id(stream)
    if any(getattr(segment, "index", None) == -1 for segment in getattr(stream, "segments", []) or []):
        if primary_kid:
            for segment in getattr(stream, "segments", []) or []:
                if getattr(segment, "index", None) == -1 and not getattr(segment, "key_id", None):
                    segment.key_id = primary_kid
        return False
    init_range = _json_init_range(stream)
    if not init_range:
        return False
    source_url = _json_probe_url(stream)
    if not source_url:
        return False
    stream.segments = [
        SegmentInfo(
            url=source_url,
            index=-1,
            byte_range=init_range,
            encrypted=stream.encrypted,
            encryption_scheme=stream.encryption_scheme,
            key_id=primary_kid,
        ),
        *stream.segments,
    ]
    return True


def _json_probe_url(stream: StreamInfo) -> str | None:
    if stream.url:
        return stream.url
    for segment in getattr(stream, "segments", []) or []:
        if segment.url:
            return segment.url
    return None


def _json_init_range(stream: StreamInfo) -> tuple[int, int] | None:
    raw = getattr(stream, "extra", {}).get("raw") if isinstance(getattr(stream, "extra", None), dict) else None
    if not isinstance(raw, dict):
        return None
    return _parse_json_byte_range(raw.get("init_range") or raw.get("initRange") or raw.get("initialization_range") or raw.get("initializationRange"))


def _parse_json_byte_range(value) -> tuple[int, int] | None:
    if isinstance(value, dict):
        start = _int_like(value.get("start"))
        end = _int_like(value.get("end"))
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        start = _int_like(value[0])
        end = _int_like(value[1])
    elif isinstance(value, str) and "-" in value:
        left, right = value.split("-", 1)
        start = _int_like(left)
        end = _int_like(right)
    else:
        return None
    if start is None or end is None or end < start:
        return None
    return start, end


def _int_like(value) -> int | None:
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _live_temp_dir(output_path: Path, options: LiveRecordOptions) -> Path:
    parent = Path(options.temp_dir).expanduser() if options.temp_dir else output_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{_safe_name(output_path.stem)}_live_", dir=str(parent)))


def _plan_group_batches(states: list[_LiveRecordState], options: LiveRecordOptions) -> list[_LivePlannedBatch]:
    media_by_state: dict[int, list[SegmentInfo]] = {}
    init_by_state: dict[int, list[SegmentInfo]] = {}
    sync_dvr_window = _should_synchronize_group_dvr_window(states, options)
    for state in states:
        media_segments = [segment for segment in state.current_stream.segments if segment.index != -1]
        init_by_state[id(state)] = [segment for segment in state.current_stream.segments if segment.index == -1]
        new_segments = _new_live_media_segments(
            media_segments,
            state.seen,
            state.edge_index,
            state.segments_count,
            options,
            apply_dvr_window=not sync_dvr_window,
            stream=state.current_stream,
        )
        _merge_pending_media_segments(state, new_segments)
        state.available_seconds, state.available_count = _live_available_totals(
            media_segments,
            state.pending_media,
            state.recorded_seconds,
            state.segments_count,
            options,
        )
        media_by_state[id(state)] = list(state.pending_media)

    if sync_dvr_window:
        media_by_state = _synchronized_group_dvr_media_segments(states, media_by_state, options)
        for state in states:
            selected = list(media_by_state.get(id(state), []))
            state.pending_media = selected
            state.available_seconds = sum(segment.duration or 0 for segment in selected)
            state.available_count = len(selected)

    aligned = _aligned_group_media_segments(states, media_by_state)
    for state in states:
        if should_repeat_live_media_request(state.current_stream):
            state.pending_media = list(aligned.get(id(state), []))
    planned: list[_LivePlannedBatch] = []
    for state in states:
        media_segments = aligned.get(id(state), [])
        media_segments = _cap_media_segments_for_limit(media_segments, state, _effective_record_limit(options))
        if not media_segments:
            continue
        batch: list[SegmentInfo] = []
        for init_segment in init_by_state[id(state)]:
            key = _segment_key(init_segment)
            if key not in state.init_written:
                batch.append(init_segment)
                state.init_written.add(key)
        batch.extend(media_segments)
        planned.append(_LivePlannedBatch(state=state, segments=batch, media_segments=media_segments))
    return planned


def _should_synchronize_group_dvr_window(states: list[_LiveRecordState], options: LiveRecordOptions) -> bool:
    return _live_starts_from_dvr_window(options) and not any(state.segments_count for state in states)


def _synchronized_group_dvr_media_segments(
    states: list[_LiveRecordState],
    media_by_state: dict[int, list[SegmentInfo]],
    options: LiveRecordOptions,
) -> dict[int, list[SegmentInfo]]:
    sync_states = [state for state in states if state.stream.media_type in {"video", "audio"}] or states
    for method in ("timeline", "program", "elapsed"):
        timed_by_state = _group_dvr_timed_segments(states, media_by_state, method)
        if not all(timed_by_state.get(id(state)) for state in sync_states):
            continue
        selected = _select_timed_group_dvr_window(states, media_by_state, sync_states, timed_by_state, options)
        if selected is not None:
            return selected
    return {
        id(state): _segments_overlapping_elapsed_window(media_by_state.get(id(state), []), options)
        for state in states
    }


def _group_dvr_timed_segments(
    states: list[_LiveRecordState],
    media_by_state: dict[int, list[SegmentInfo]],
    method: str,
) -> dict[int, list[tuple[float, SegmentInfo]]]:
    timed_by_state: dict[int, list[tuple[float, SegmentInfo]]] = {}
    for state in states:
        segments = media_by_state.get(id(state), [])
        if method == "timeline":
            timed = _timeline_indexed_segments(getattr(state, "current_stream", state.stream), segments)
        elif method == "program":
            timed = _timestamped_segments(segments)
        else:
            timed = _elapsed_duration_segments(segments)
        if timed:
            timed_by_state[id(state)] = timed
    return timed_by_state


def _select_timed_group_dvr_window(
    states: list[_LiveRecordState],
    media_by_state: dict[int, list[SegmentInfo]],
    sync_states: list[_LiveRecordState],
    timed_by_state: dict[int, list[tuple[float, SegmentInfo]]],
    options: LiveRecordOptions,
) -> dict[int, list[SegmentInfo]] | None:
    intervals_by_state = {state_id: _timed_segment_intervals(timed) for state_id, timed in timed_by_state.items()}
    sync_intervals = [intervals_by_state.get(id(state), []) for state in sync_states]
    if not all(sync_intervals):
        return None
    common_start = max(intervals[0][0] for intervals in sync_intervals)
    common_end = min(intervals[-1][1] for intervals in sync_intervals)
    selected_start = common_start + max(0.0, float(options.dvr_start_offset or 0.0))
    selected_end = common_end
    if options.dvr_end_offset is not None:
        selected_end = min(selected_end, common_start + max(0.0, float(options.dvr_end_offset)))
    if options.limit_seconds is not None:
        selected_end = min(selected_end, selected_start + max(0.0, float(options.limit_seconds)))
    if selected_end <= selected_start:
        return {id(state): [] for state in states}

    selected: dict[int, list[SegmentInfo]] = {}
    for state in states:
        intervals = intervals_by_state.get(id(state), [])
        if intervals:
            selected[id(state)] = _segments_overlapping_interval(intervals, selected_start, selected_end, 0.0)
            continue
        selected[id(state)] = _segments_overlapping_elapsed_window(media_by_state.get(id(state), []), options)
    return selected


def _elapsed_duration_segments(segments: list[SegmentInfo]) -> list[tuple[float, SegmentInfo]]:
    elapsed = 0.0
    timed: list[tuple[float, SegmentInfo]] = []
    for segment in segments:
        timed.append((elapsed, segment))
        duration = float(segment.duration or 0.0)
        if duration > 0:
            elapsed += duration
    return timed if elapsed > 0 else []


def _segments_overlapping_elapsed_window(segments: list[SegmentInfo], options: LiveRecordOptions) -> list[SegmentInfo]:
    timed = _elapsed_duration_segments(segments)
    intervals = _timed_segment_intervals(timed)
    if not intervals:
        return _segments_from_dvr_offset(segments, options.dvr_start_offset)
    start = max(0.0, float(options.dvr_start_offset or 0.0))
    end = intervals[-1][1]
    if options.dvr_end_offset is not None:
        end = min(end, max(start, float(options.dvr_end_offset)))
    if options.limit_seconds is not None:
        end = min(end, start + max(0.0, float(options.limit_seconds)))
    return _segments_overlapping_interval(intervals, start, end, 0.0)


def _aligned_group_media_segments(
    states: list[_LiveRecordState],
    media_by_state: dict[int, list[SegmentInfo]],
) -> dict[int, list[SegmentInfo]]:
    sync_states = [state for state in states if state.stream.media_type in {"video", "audio"}] or states
    sync_state_ids = {id(state) for state in sync_states}
    if _has_explicit_dash_timeline(sync_states):
        aligned_by_timeline = _align_by_timeline_indexes(states, media_by_state, sync_states, sync_state_ids)
        if aligned_by_timeline:
            return aligned_by_timeline

    common_indexes = _common_media_indexes(sync_states, media_by_state)
    if common_indexes:
        return _align_by_indexes(states, media_by_state, sync_state_ids, common_indexes)

    aligned_by_timeline = _align_by_timeline_indexes(states, media_by_state, sync_states, sync_state_ids)
    if aligned_by_timeline:
        return aligned_by_timeline

    aligned_by_time = _align_by_program_time(states, media_by_state, sync_states, sync_state_ids)
    if aligned_by_time:
        return aligned_by_time

    aligned_by_repeatable = _align_repeatable_live_by_reference_indexes(states, media_by_state, sync_states, sync_state_ids)
    if aligned_by_repeatable:
        return aligned_by_repeatable

    return {id(state): [] for state in states}


def _has_explicit_dash_timeline(states: list[_LiveRecordState]) -> bool:
    for state in states:
        stream = getattr(state, "current_stream", state.stream)
        extra = getattr(stream, "extra", {}) or {}
        if extra.get("dash_index_is_timeline") or stream_uses_ism_timeline_index(stream):
            return True
    return False


def _common_media_indexes(
    sync_states: list[_LiveRecordState],
    media_by_state: dict[int, list[SegmentInfo]],
) -> set[int]:
    sync_index_sets: list[set[int]] = []
    for state in sync_states:
        segments = media_by_state.get(id(state), [])
        indexes = {segment.index for segment in segments if segment.index is not None and segment.index >= 0}
        if not indexes:
            return set()
        sync_index_sets.append(indexes)
    return set.intersection(*sync_index_sets) if sync_index_sets else set()


def _align_by_indexes(
    states: list[_LiveRecordState],
    media_by_state: dict[int, list[SegmentInfo]],
    sync_state_ids: set[int],
    common_indexes: set[int],
) -> dict[int, list[SegmentInfo]]:
    aligned: dict[int, list[SegmentInfo]] = {}
    for state in states:
        segments = media_by_state.get(id(state), [])
        with_common_index = [segment for segment in segments if segment.index in common_indexes]
        if id(state) in sync_state_ids:
            aligned[id(state)] = with_common_index
            continue
        state_indexes = {segment.index for segment in segments if segment.index is not None and segment.index >= 0}
        aligned[id(state)] = with_common_index if state_indexes.intersection(common_indexes) else segments
    return aligned


def _align_repeatable_live_by_reference_indexes(
    states: list[_LiveRecordState],
    media_by_state: dict[int, list[SegmentInfo]],
    sync_states: list[_LiveRecordState],
    sync_state_ids: set[int],
) -> dict[int, list[SegmentInfo]] | None:
    repeatable_ids = {id(state) for state in sync_states if should_repeat_live_media_request(state.current_stream)}
    if not repeatable_ids or len(repeatable_ids) == len(sync_states):
        return None
    reference_states = [state for state in sync_states if id(state) not in repeatable_ids]
    reference_indexes = _common_media_indexes(reference_states, media_by_state)
    if not reference_indexes:
        first_reference = next((state for state in reference_states if _segment_indexes(media_by_state.get(id(state), []))), None)
        if first_reference is None:
            return None
        reference_indexes = _segment_indexes(media_by_state.get(id(first_reference), []))
    if not reference_indexes:
        return None

    aligned: dict[int, list[SegmentInfo]] = {}
    for state in states:
        state_id = id(state)
        segments = media_by_state.get(state_id, [])
        if state_id in repeatable_ids:
            aligned[state_id] = _repeatable_live_segments_for_indexes(state, segments, reference_indexes)
        elif state_id in sync_state_ids:
            aligned[state_id] = [segment for segment in segments if segment.index in reference_indexes]
        else:
            state_indexes = _segment_indexes(segments)
            aligned[state_id] = [segment for segment in segments if segment.index in reference_indexes] if state_indexes.intersection(reference_indexes) else segments

    if any(not aligned.get(id(state)) for state in sync_states):
        return None
    return aligned


def _repeatable_live_segments_for_indexes(
    state: _LiveRecordState,
    segments: list[SegmentInfo],
    indexes: set[int],
) -> list[SegmentInfo]:
    media_segments = [segment for segment in segments if segment.index != -1]
    if not media_segments:
        return []
    by_index = {segment.index: segment for segment in media_segments if segment.index is not None and segment.index >= 0}
    source = media_segments[-1]
    duration = source.duration or repeated_live_media_duration_seconds(state.current_stream)
    result: list[SegmentInfo] = []
    for index in sorted(indexes):
        if str(index) in state.seen:
            continue
        result.append(by_index.get(index) or replace(source, index=index, duration=duration))
    return result


def _segment_indexes(segments: list[SegmentInfo]) -> set[int]:
    return {int(segment.index) for segment in segments if segment.index is not None and segment.index >= 0}


def _align_by_program_time(
    states: list[_LiveRecordState],
    media_by_state: dict[int, list[SegmentInfo]],
    sync_states: list[_LiveRecordState],
    sync_state_ids: set[int],
) -> dict[int, list[SegmentInfo]] | None:
    timed_by_state: dict[int, list[tuple[float, SegmentInfo]]] = {}
    for state in sync_states:
        timed = _timestamped_segments(media_by_state.get(id(state), []))
        if not timed:
            return None
        timed_by_state[id(state)] = timed

    return _align_by_timed_segments(
        states,
        media_by_state,
        sync_states,
        sync_state_ids,
        timed_by_state,
        _program_time_alignment_tolerance(sync_states, media_by_state),
    )


def _align_by_timeline_indexes(
    states: list[_LiveRecordState],
    media_by_state: dict[int, list[SegmentInfo]],
    sync_states: list[_LiveRecordState],
    sync_state_ids: set[int],
) -> dict[int, list[SegmentInfo]] | None:
    timed_by_state: dict[int, list[tuple[float, SegmentInfo]]] = {}
    for state in sync_states:
        timed = _timeline_indexed_segments(getattr(state, "current_stream", state.stream), media_by_state.get(id(state), []))
        if not timed:
            return None
        timed_by_state[id(state)] = timed
    return _align_by_timed_segments(
        states,
        media_by_state,
        sync_states,
        sync_state_ids,
        timed_by_state,
        _program_time_alignment_tolerance(sync_states, media_by_state),
    )


def _align_by_timed_segments(
    states: list[_LiveRecordState],
    media_by_state: dict[int, list[SegmentInfo]],
    sync_states: list[_LiveRecordState],
    sync_state_ids: set[int],
    timed_by_state: dict[int, list[tuple[float, SegmentInfo]]],
    tolerance: float,
) -> dict[int, list[SegmentInfo]] | None:
    intervals_by_state = {state_id: _timed_segment_intervals(timed) for state_id, timed in timed_by_state.items()}
    if not all(intervals_by_state.get(id(state)) for state in sync_states):
        return None
    common_start = max(intervals_by_state[id(state)][0][0] for state in sync_states)
    common_end = min(intervals_by_state[id(state)][-1][1] for state in sync_states)
    if common_end <= common_start:
        return None

    aligned: dict[int, list[SegmentInfo]] = {}
    for state in states:
        state_id = id(state)
        if state_id in sync_state_ids:
            aligned[state_id] = _segments_overlapping_interval(intervals_by_state.get(state_id, []), common_start, common_end, tolerance)
            continue
        timed = _timestamped_segments(media_by_state.get(state_id, []))
        sidecar = _segments_overlapping_interval(_timed_segment_intervals(timed), common_start, common_end, tolerance)
        aligned[state_id] = sidecar if sidecar else media_by_state.get(state_id, [])
    return aligned


def _timed_segment_intervals(timed: list[tuple[float, SegmentInfo]]) -> list[tuple[float, float, SegmentInfo]]:
    intervals: list[tuple[float, float, SegmentInfo]] = []
    for timestamp, segment in timed:
        duration = float(segment.duration or 0)
        if duration <= 0:
            duration = _next_timed_delta(timestamp, timed)
        if duration <= 0:
            duration = 0.001
        intervals.append((timestamp, timestamp + duration, segment))
    return intervals


def _next_timed_delta(timestamp: float, timed: list[tuple[float, SegmentInfo]]) -> float:
    for next_timestamp, _segment in timed:
        delta = next_timestamp - timestamp
        if delta > 0:
            return delta
    return 0.0


def _segments_overlapping_interval(
    intervals: list[tuple[float, float, SegmentInfo]],
    start: float,
    end: float,
    tolerance: float,
) -> list[SegmentInfo]:
    edge_slack = min(max(tolerance, 0.0), 0.25)
    return [
        segment
        for segment_start, segment_end, segment in intervals
        if segment_end > start - edge_slack and segment_start < end + edge_slack
    ]


def _timestamped_segments(segments: list[SegmentInfo]) -> list[tuple[float, SegmentInfo]]:
    timed = []
    for segment in segments:
        timestamp = _segment_program_timestamp(segment)
        if timestamp is not None:
            timed.append((timestamp, segment))
    return sorted(timed, key=lambda item: item[0])


def _timeline_indexed_segments(stream: StreamInfo, segments: list[SegmentInfo]) -> list[tuple[float, SegmentInfo]]:
    timescale = _stream_timeline_index_timescale(stream) or _infer_timeline_index_timescale(segments)
    if not timescale:
        return []
    timed: list[tuple[float, SegmentInfo]] = []
    for segment in segments:
        presentation_time = _segment_timeline_presentation_time(stream, segment, timescale)
        if presentation_time is None:
            continue
        timed.append((presentation_time, segment))
    return sorted(timed, key=lambda item: item[0])


def _segment_timeline_presentation_time(stream: StreamInfo, segment: SegmentInfo, timescale: float) -> float | None:
    value = getattr(segment, "timeline_presentation_time", None)
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    timeline_time = _segment_timeline_time(segment)
    if timeline_time is None:
        return None
    return _stream_period_start(stream) + float(timeline_time) / timescale - _stream_timeline_presentation_offset(stream)


def _segment_timeline_time(segment: SegmentInfo) -> int | None:
    value = getattr(segment, "timeline_time", None)
    if value is None:
        value = getattr(segment, "index", None)
    if value is None or value < 0:
        return None
    return int(value)


def _stream_timeline_index_timescale(stream: StreamInfo) -> float | None:
    extra = getattr(stream, "extra", {}) or {}
    if stream_uses_ism_timeline_index(stream):
        timescale = stream_ism_timescale(stream)
        return float(timescale) if timescale else None
    if not extra.get("dash_index_is_timeline"):
        return None
    value = extra.get("dash_timescale")
    try:
        timescale = float(value)
    except (TypeError, ValueError):
        return None
    return timescale if timescale > 0 else None


def _stream_timeline_presentation_offset(stream: StreamInfo) -> float:
    extra = getattr(stream, "extra", {}) or {}
    value = extra.get("dash_presentation_time_offset")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _stream_period_start(stream: StreamInfo) -> float:
    extra = getattr(stream, "extra", {}) or {}
    value = extra.get("period_start")
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _infer_timeline_index_timescale(segments: list[SegmentInfo]) -> float | None:
    ordered = sorted(
        [segment for segment in segments if segment.index is not None and segment.index >= 0 and (segment.duration or 0) > 0],
        key=lambda segment: segment.index or 0,
    )
    ratios: list[float] = []
    for previous, current in zip(ordered, ordered[1:], strict=False):
        index_delta = (current.index or 0) - (previous.index or 0)
        duration = previous.duration or current.duration or 0
        if index_delta <= 0 or duration <= 0:
            continue
        ratio = index_delta / duration
        if ratio > 10:
            ratios.append(ratio)
    if not ratios:
        return None
    ratios.sort()
    return ratios[len(ratios) // 2]


def _nearest_program_time_position(
    timed: list[tuple[float, SegmentInfo]],
    target: float,
    used_positions: set[int],
    tolerance: float,
) -> int | None:
    best_position: int | None = None
    best_delta = tolerance
    for position, (timestamp, _segment) in enumerate(timed):
        if position in used_positions:
            continue
        delta = abs(timestamp - target)
        if delta <= best_delta:
            best_delta = delta
            best_position = position
    return best_position


def _program_time_alignment_tolerance(
    sync_states: list[_LiveRecordState],
    media_by_state: dict[int, list[SegmentInfo]],
) -> float:
    durations = [
        segment.duration or 0
        for state in sync_states
        for segment in media_by_state.get(id(state), [])
        if (segment.duration or 0) > 0
    ]
    if not durations:
        return 1.5
    average = sum(durations) / len(durations)
    return max(1.5, min(3.0, average * 0.35))


def _segment_program_timestamp(segment: SegmentInfo) -> float | None:
    value = getattr(segment, "program_date_time", None)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _merge_pending_media_segments(state: _LiveRecordState, segments: list[SegmentInfo]) -> None:
    if not segments:
        state.pending_media = [segment for segment in state.pending_media if _segment_key(segment) not in state.seen]
        return
    pending_by_key = {_segment_key(segment): segment for segment in state.pending_media}
    for segment in segments:
        key = _segment_key(segment)
        if key in state.seen or key in pending_by_key:
            continue
        pending_by_key[key] = segment
    state.pending_media = sorted(
        pending_by_key.values(),
        key=lambda segment: (segment.index is None, segment.index if segment.index is not None else 0, segment.url),
    )


def _cap_media_segments_for_limit(
    segments: list[SegmentInfo],
    state: _LiveRecordState,
    limit_seconds: float | None,
) -> list[SegmentInfo]:
    return _cap_segments_for_limit(segments, state.recorded_seconds, limit_seconds)


def _cap_segments_for_limit(
    segments: list[SegmentInfo],
    recorded_seconds: float,
    limit_seconds: float | None,
) -> list[SegmentInfo]:
    if limit_seconds is None:
        return segments
    remaining = limit_seconds - recorded_seconds
    if remaining <= 0:
        return []
    selected: list[SegmentInfo] = []
    selected_seconds = 0.0
    for segment in segments:
        duration = segment.duration or 0
        if selected and duration and selected_seconds + duration > remaining + 0.001:
            break
        selected.append(segment)
        selected_seconds += duration
        if selected_seconds >= remaining - 0.001:
            break
    return selected


def _apply_group_batch(
    options: LiveRecordOptions,
    plan: _LivePlannedBatch,
    parts: list[Path],
    emit_segment_sink: bool = True,
) -> None:
    state = plan.state
    state.parts.extend(parts)
    state.segments.extend(plan.segments)
    state.downloaded_bytes += _paths_total_size(parts)
    for segment in plan.media_segments:
        state.seen.add(_segment_key(segment))
        state.edge_index = _max_segment_index(state.edge_index, segment)
        state.recorded_seconds += segment.duration or 0
        state.segments_count += 1
    planned_keys = {_segment_key(segment) for segment in plan.media_segments}
    state.pending_media = [segment for segment in state.pending_media if _segment_key(segment) not in planned_keys]
    media_segments = [segment for segment in state.current_stream.segments if segment.index != -1]
    state.available_seconds, state.available_count = _live_available_totals(
        media_segments,
        state.pending_media,
        state.recorded_seconds,
        state.segments_count,
        options,
    )
    if emit_segment_sink and options.segment_sink:
        options.segment_sink(_live_segment_batch(plan, parts))
    _emit_state_progress(options, state, len(plan.media_segments), "Waiting")


def _should_assemble_live_output(options: LiveRecordOptions, stream: StreamInfo) -> bool:
    assemble_output = options.assemble_output
    if callable(assemble_output):
        return bool(assemble_output(stream))
    return bool(assemble_output)


def _live_segment_batch(plan: _LivePlannedBatch, parts: list[Path]) -> LiveSegmentBatch:
    state = plan.state
    return LiveSegmentBatch(
        stream=state.stream,
        segments=list(plan.segments),
        parts=list(parts),
        output_path=state.output_path,
        temp_dir=state.temp_dir,
    )


def _refresh_dash_patch_states(states: list[_LiveRecordState], headers: dict[str, str] | None = None) -> bool | None:
    patch_url = _common_dash_patch_url([state.current_stream for state in states])
    if not patch_url:
        return None
    try:
        root, final_url = _load_dash_patch_root(patch_url, headers=headers)
    except _DashPatchNoUpdate:
        return True
    except (LoadError, ET.ParseError, ValueError):
        return False
    if not _is_dash_patch_root(root):
        return None
    next_url = _dash_patch_location(root, final_url)
    patched_any = False
    for state in states:
        patched_any = _apply_dash_patch_to_stream(state.current_stream, root, next_url) or patched_any
    return patched_any


def _refresh_dash_patch_stream(stream: StreamInfo, headers: dict[str, str] | None = None) -> StreamInfo | bool | None:
    patch_url = _dash_patch_url(stream)
    if not patch_url:
        return None
    try:
        root, final_url = _load_dash_patch_root(patch_url, headers=headers)
    except _DashPatchNoUpdate:
        return stream
    except (LoadError, ET.ParseError, ValueError):
        return False
    if not _is_dash_patch_root(root):
        return None
    next_url = _dash_patch_location(root, final_url)
    return stream if _apply_dash_patch_to_stream(stream, root, next_url) else False


def _common_dash_patch_url(streams: list[StreamInfo]) -> str | None:
    urls = {_dash_patch_url(stream) for stream in streams if _dash_patch_url(stream)}
    if len(urls) == 1:
        return next(iter(urls))
    return None


def _dash_patch_url(stream: StreamInfo) -> str | None:
    if stream.manifest_type != "dash":
        return None
    value = (stream.extra or {}).get("dash_refresh_url")
    return value if isinstance(value, str) and value else None


def _load_dash_patch_root(url: str, headers: dict[str, str] | None = None) -> tuple[ET.Element, str]:
    resource = load_text(url, headers=headers)
    text = (resource.text or "").lstrip("\ufeff \t\r\n")
    if not text:
        raise _DashPatchNoUpdate
    root = ET.fromstring(text)
    return root, resource.uri


def _is_dash_patch_root(root: ET.Element) -> bool:
    if _xml_local(root.tag) == "Patch":
        return True
    for child in _xml_descendants(root):
        local = _xml_local(child.tag)
        if local in {"add", "replace", "remove"}:
            return True
        if local != "EssentialProperty":
            continue
        scheme = (child.attrib.get("schemeIdUri") or "").lower()
        if "hulu:schema:mpd:2017:patch" in scheme or "mpd:patch" in scheme:
            return True
    return False


def _dash_patch_location(root: ET.Element, base_url: str) -> str | None:
    from .utils import join_uri

    for child in list(root):
        if _xml_local(child.tag) == "Location" and (child.text or "").strip():
            return join_uri(base_url, child.text.strip())
    for operation in _dash_patch_operations(root):
        selector = (operation.attrib.get("sel") or "").lower()
        if "location" not in selector:
            continue
        if (operation.text or "").strip():
            return join_uri(base_url, operation.text.strip())
        location = _first_xml_descendant(operation, "Location")
        if location is not None and (location.text or "").strip():
            return join_uri(base_url, location.text.strip())
    return None


def _apply_dash_patch_to_stream(stream: StreamInfo, root: ET.Element, next_url: str | None) -> bool:
    if next_url:
        stream.extra["dash_refresh_url"] = next_url
    adaptation_id = str(stream.extra.get("dash_adaptation_id") or stream.group_id or "")
    if not adaptation_id:
        return False
    media_template = stream.extra.get("dash_media_template")
    base_uri = stream.extra.get("dash_template_base_uri")
    if not isinstance(media_template, str) or not isinstance(base_uri, str):
        return False
    timescale = _positive_int(stream.extra.get("dash_timescale")) or 1
    next_index = _next_media_index(stream.segments)
    added = False
    for time_value, duration_ticks in _dash_patch_entries_for_stream(root, stream, adaptation_id):
        segment = _dash_patch_segment(stream, base_uri, media_template, time_value, duration_ticks, timescale, next_index)
        next_index += 1
        if _has_segment_url(stream.segments, segment.url):
            continue
        stream.segments.append(segment)
        added = True
    if added:
        stream.url = stream.segments[0].url
    return added


def _dash_patch_segment(
    stream: StreamInfo,
    base_uri: str,
    media_template: str,
    time_value: int,
    duration_ticks: int,
    timescale: int,
    index: int,
) -> SegmentInfo:
    from .utils import join_uri

    representation_id = str(stream.extra.get("dash_representation_id") or stream.id or "")
    bandwidth = str(stream.extra.get("dash_bandwidth") or stream.bandwidth or "")
    url = join_uri(
        base_uri,
        _replace_dash_template_vars(
            media_template,
            {
                "RepresentationID": representation_id,
                "Bandwidth": bandwidth,
                "Number": str(index),
                "Time": str(time_value),
            },
        ),
    )
    return SegmentInfo(
        url=url,
        duration=duration_ticks / max(1, timescale),
        index=time_value,
        encrypted=stream.encrypted,
        encryption_scheme=stream.encryption_scheme,
        key_id=_stream_primary_key_id(stream),
        timeline_time=time_value,
        timeline_presentation_time=_dash_patch_presentation_time(stream, time_value, timescale),
    )


def _dash_patch_presentation_time(stream: StreamInfo, time_value: int, timescale: int) -> float:
    return _stream_period_start(stream) + float(time_value) / max(1, timescale) - _stream_timeline_presentation_offset(stream)


def _dash_patch_timeline_entries(timeline: ET.Element) -> list[tuple[int, int]]:
    return _dash_patch_timeline_entries_from_nodes(_xml_children(timeline, "S"))


def _dash_patch_timeline_entries_from_nodes(nodes: list[ET.Element]) -> list[tuple[int, int]]:
    entries: list[tuple[int, int]] = []
    current_time = 0
    for node in nodes:
        if node.attrib.get("t") is not None:
            current_time = int(node.attrib["t"])
        duration = int(node.attrib.get("d", "0"))
        if duration <= 0:
            continue
        repeat = int(node.attrib.get("r", "0"))
        for _ in range(max(0, repeat) + 1):
            entries.append((current_time, duration))
            current_time += duration
    return entries


def _dash_patch_entries_for_stream(root: ET.Element, stream: StreamInfo, adaptation_id: str) -> list[tuple[int, int]]:
    entries = _dash_patch_entries_from_periods(_xml_children(root, "Period"), adaptation_id)
    for operation in _dash_patch_operations(root):
        entries.extend(_dash_patch_entries_from_periods(_xml_children(operation, "Period"), adaptation_id))
        selector = operation.attrib.get("sel") or ""
        if not _dash_patch_selector_matches_stream(selector, stream, adaptation_id):
            continue
        timeline = _first_xml_descendant(operation, "SegmentTimeline")
        if timeline is not None:
            entries.extend(_dash_patch_timeline_entries(timeline))
            continue
        entries.extend(_dash_patch_timeline_entries_from_nodes(_xml_children(operation, "S")))
    return entries


def _dash_patch_entries_from_periods(periods: list[ET.Element], adaptation_id: str) -> list[tuple[int, int]]:
    entries: list[tuple[int, int]] = []
    for period in periods:
        for adaptation in _xml_children(period, "AdaptationSet"):
            if adaptation.attrib.get("id") != adaptation_id:
                continue
            timeline = _first_xml_descendant(adaptation, "SegmentTimeline")
            if timeline is None:
                continue
            entries.extend(_dash_patch_timeline_entries(timeline))
    return entries


def _dash_patch_operations(root: ET.Element) -> list[ET.Element]:
    return [
        node
        for node in _xml_descendants(root)
        if _xml_local(node.tag) in {"add", "replace"}
    ]


def _dash_patch_selector_matches_stream(selector: str, stream: StreamInfo, adaptation_id: str) -> bool:
    if not selector:
        return False
    values = [
        adaptation_id,
        stream.extra.get("dash_representation_id") if stream.extra else None,
        stream.id,
        stream.group_id,
    ]
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if f"'{value}'" in selector or f'"{value}"' in selector:
            return True
        if len(value) >= 4 and value in selector:
            return True
    return False


def _replace_dash_template_vars(template: str, values: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group("name")
        fmt = match.group("fmt")
        value = values.get(name, "")
        if fmt:
            try:
                return fmt % int(value)
            except (TypeError, ValueError):
                return value
        return value

    return re.sub(r"\$(?P<name>[A-Za-z]+)(?P<fmt>%0?\d+d)?\$", repl, template)


def _xml_children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _xml_local(child.tag) == name]


def _first_xml_descendant(element: ET.Element, name: str) -> ET.Element | None:
    for child in list(element):
        if _xml_local(child.tag) == name:
            return child
        found = _first_xml_descendant(child, name)
        if found is not None:
            return found
    return None


def _xml_descendants(element: ET.Element):
    for child in list(element):
        yield child
        yield from _xml_descendants(child)


def _xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _positive_int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _has_segment_url(segments: list[SegmentInfo], url: str) -> bool:
    return any(segment.url == url for segment in segments)


def _next_media_index(segments: list[SegmentInfo]) -> int:
    indexes = [segment.index for segment in segments if segment.index is not None and segment.index >= 0]
    return max(indexes, default=-1) + 1


def _stream_primary_key_id(stream: StreamInfo) -> str | None:
    value = stream.extra.get("key_id") if stream.extra else None
    if isinstance(value, str) and value:
        return value
    key_ids = stream.extra.get("key_ids") if stream.extra else None
    if isinstance(key_ids, list):
        for key_id in key_ids:
            if isinstance(key_id, str) and key_id:
                return key_id
    raw = stream.extra.get("raw") if stream.extra else None
    if isinstance(raw, dict):
        for key in ("kid", "key_id", "keyId"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                return value
    for segment in getattr(stream, "segments", []) or []:
        value = getattr(segment, "key_id", None)
        if isinstance(value, str) and value:
            return value
    return None


def _refresh_group_states(states: list[_LiveRecordState], options: LiveRecordOptions, headers: dict[str, str] | None = None) -> bool:
    patched = _refresh_dash_patch_states(states, headers=headers)
    if patched is True:
        return patched
    sabr_refreshed = _refresh_sabr_live_states(states, options, headers=headers)
    common_source = _common_live_refresh_source([state.current_stream for state in states])
    if common_source:
        refreshed = _load_refreshed_streams(common_source, options, headers=headers)
        if refreshed is not None:
            for state in states:
                state.current_stream = _match_live_state_refreshed_stream(state, refreshed, options) or state.current_stream
            return True

    skipped_sources = {common_source} if common_source else set()
    refreshed_any = sabr_refreshed
    for state in states:
        if _is_sabr_ump_stream(state.current_stream):
            continue
        refreshed_stream = _refresh_live_state_from_candidates(state, options, headers=headers, skip_sources=skipped_sources)
        if refreshed_stream is None:
            continue
        refreshed_any = True
        state.current_stream = refreshed_stream
    return refreshed_any


def _refresh_sabr_live_states(states: list[_LiveRecordState], options: LiveRecordOptions, headers: dict[str, str] | None = None) -> bool:
    refreshed_any = False
    for state in states:
        if not _is_sabr_ump_stream(state.current_stream):
            continue
        first_sequence = state.edge_index + 1 if state.edge_index is not None else None
        take_count = 1 if state.segments_count else max(1, int(options.take_count or 1))
        refreshed_any = _refresh_sabr_live_stream_segments(
            state.current_stream,
            first_sequence=first_sequence,
            take_count=take_count,
            headers=headers,
        ) or refreshed_any
        media_segments = [segment for segment in state.current_stream.segments if segment.index != -1]
        state.available_seconds, state.available_count = _live_available_totals(
            media_segments,
            state.pending_media,
            state.recorded_seconds,
            state.segments_count,
            options,
        )
    return refreshed_any


def _refresh_sabr_live_stream_segments(
    stream: StreamInfo,
    *,
    first_sequence: int | None,
    take_count: int,
    headers: dict[str, str] | None = None,
) -> bool:
    if not _is_sabr_ump_stream(stream):
        return False
    try:
        segments = fetch_sabr_ump_live_segments(
            stream,
            first_sequence=first_sequence,
            take_count=take_count,
            headers=headers,
        )
    except SabrUmpError:
        return False
    if not segments:
        return False
    stream.is_live = True
    existing = {_segment_key(segment) for segment in stream.segments}
    new_segments = [segment for segment in segments if _segment_key(segment) not in existing]
    if not new_segments:
        return False
    stream.segments = sorted(
        [*stream.segments, *new_segments],
        key=lambda segment: (segment.index != -1, segment.index if segment.index is not None else 0, segment.url),
    )
    return True


def _is_sabr_ump_stream(stream: StreamInfo) -> bool:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    return stream.manifest_type == "sabr_ump" or bool(extra.get("sabr_ump"))


def _common_live_refresh_source(streams: list[StreamInfo]) -> str | None:
    refresh_sources = {_live_refresh_source(stream) for stream in streams if _live_refresh_source(stream)}
    if len(refresh_sources) == 1:
        return next(iter(refresh_sources))
    original_sources = {stream.original_url for stream in streams if stream.original_url}
    if len(original_sources) == 1 and all(stream.manifest_type in {"dash", "ism"} for stream in streams):
        return next(iter(original_sources))
    return None


def _emit_state_progress(options: LiveRecordOptions, state: _LiveRecordState, new_segments: int, status: str, done: bool = False) -> None:
    _emit_live_progress(
        options,
        state.stream,
        state.recorded_seconds,
        state.segments_count,
        new_segments,
        state.output_path,
        status,
        state.available_seconds,
        state.available_count,
        done=done,
        downloaded_bytes=state.downloaded_bytes,
    )


def _batch_progress_callback(
    options: LiveRecordOptions,
    stream: StreamInfo,
    output_path: Path,
    base_seconds: float,
    base_count: int,
    base_bytes: int,
    media_segments: list[SegmentInfo],
    available_seconds: float | None = None,
    available_count: int | None = None,
) -> Callable[[SegmentInfo, Path], None] | None:
    if not options.progress:
        return None
    tracked_keys = {_segment_key(segment) for segment in media_segments}
    if not tracked_keys:
        return None
    lock = threading.Lock()
    completed_count = 0
    completed_seconds = 0.0
    completed_bytes = 0

    def _progress(segment: SegmentInfo, _path: Path) -> None:
        nonlocal completed_count, completed_seconds, completed_bytes
        if segment.index == -1 or _segment_key(segment) not in tracked_keys:
            return
        with lock:
            completed_count += 1
            completed_seconds += segment.duration or 0
            completed_bytes += _path_size(_path)
            remaining = max(0, len(tracked_keys) - completed_count)
            _emit_live_progress(
                options,
                stream,
                float(base_seconds or 0) + completed_seconds,
                int(base_count or 0) + completed_count,
                remaining,
                output_path,
                "Recording",
                available_seconds,
                available_count,
                downloaded_bytes=int(base_bytes or 0) + completed_bytes,
            )

    return _progress


def _transform_refreshed_streams(options: LiveRecordOptions, streams: list[StreamInfo]) -> None:
    if options.stream_transform and streams:
        options.stream_transform(streams)


def _default_group_wait_seconds(states: list[_LiveRecordState]) -> int:
    values = [_default_wait_seconds(state.current_stream) for state in states if state.current_stream]
    return min(values) if values else 3


def _live_display_segments(
    segments: list[SegmentInfo],
    recorded_count: int,
    options: LiveRecordOptions,
) -> list[SegmentInfo]:
    if recorded_count:
        return segments
    if _live_starts_from_dvr_window(options):
        return _segments_from_dvr_offset(segments, options.dvr_start_offset)
    take_count = int(options.take_count or 0)
    if take_count > 0 and len(segments) > take_count:
        return segments[-take_count:]
    return segments


def _live_starts_from_dvr_window(options: LiveRecordOptions) -> bool:
    return bool(options.dvr_from_start or options.dvr_start_offset is not None)


def _effective_record_limit(options: LiveRecordOptions) -> float | None:
    if options.dvr_start_offset is not None and options.dvr_end_offset is not None:
        dvr_limit = max(0.0, options.dvr_end_offset - options.dvr_start_offset)
        if options.limit_seconds is None:
            return dvr_limit
        return min(options.limit_seconds, dvr_limit)
    return options.limit_seconds


def _segments_from_dvr_offset(segments: list[SegmentInfo], offset: float | None) -> list[SegmentInfo]:
    if not offset or offset <= 0:
        return segments
    elapsed = 0.0
    for index, segment in enumerate(segments):
        duration = float(segment.duration or 0)
        if elapsed + duration > offset:
            return segments[index:]
        elapsed += duration
    return []


def _new_live_media_segments(
    media_segments: list[SegmentInfo],
    seen: set[str],
    edge_index: int | None,
    recorded_count: int,
    options: LiveRecordOptions,
    apply_dvr_window: bool = True,
    stream: StreamInfo | None = None,
) -> list[SegmentInfo]:
    new_segments = [segment for segment in media_segments if _segment_key(segment) not in seen]
    if edge_index is not None and (recorded_count > 0 or not _live_starts_from_dvr_window(options)):
        new_segments = [
            segment
            for segment in new_segments
            if segment.index is None or segment.index > edge_index
        ]
    if not apply_dvr_window and recorded_count == 0 and _live_starts_from_dvr_window(options):
        return new_segments
    if not new_segments and stream is not None:
        repeated = _repeat_live_media_request_segment(stream, media_segments, seen, edge_index, recorded_count, options)
        if repeated is not None:
            new_segments = [repeated]
    return _live_display_segments(new_segments, recorded_count, options)


def _repeat_live_media_request_segment(
    stream: StreamInfo,
    media_segments: list[SegmentInfo],
    seen: set[str],
    edge_index: int | None,
    recorded_count: int,
    options: LiveRecordOptions,
) -> SegmentInfo | None:
    if not recorded_count and not seen:
        return None
    if not media_segments or not should_repeat_live_media_request(stream):
        return None
    next_index = edge_index + 1 if edge_index is not None else recorded_count
    while str(next_index) in seen:
        next_index += 1
    source = media_segments[-1]
    duration = source.duration or repeated_live_media_duration_seconds(stream, options.wait_seconds)
    return replace(source, index=next_index, duration=duration)


def _state_has_new_live_media(state: _LiveRecordState, options: LiveRecordOptions) -> bool:
    return _stream_has_new_live_media(
        state.current_stream,
        state.seen,
        state.edge_index,
        state.segments_count,
        options,
    )


def _stream_has_new_live_media(
    stream: StreamInfo,
    seen: set[str],
    edge_index: int | None,
    recorded_count: int,
    options: LiveRecordOptions,
) -> bool:
    media_segments = [segment for segment in stream.segments if segment.index != -1]
    return bool(_new_live_media_segments(media_segments, seen, edge_index, recorded_count, options, stream=stream))


def _max_segment_index(current: int | None, segment: SegmentInfo) -> int | None:
    if segment.index is None or segment.index < 0:
        return current
    if current is None:
        return segment.index
    return max(current, segment.index)


def _live_available_totals(
    media_segments: list[SegmentInfo],
    pending_segments: list[SegmentInfo],
    recorded_seconds: float,
    recorded_count: int,
    options: LiveRecordOptions,
) -> tuple[float, int]:
    if _live_starts_from_dvr_window(options):
        display_segments = _live_display_segments(media_segments, 0, options)
        return (
            max(sum(segment.duration or 0 for segment in display_segments), recorded_seconds + sum(segment.duration or 0 for segment in pending_segments)),
            max(len(display_segments), recorded_count + len(pending_segments)),
        )
    if recorded_count == 0:
        display_segments = _live_display_segments(media_segments, recorded_count, options)
        return (sum(segment.duration or 0 for segment in display_segments), len(display_segments))
    return (
        max(recorded_seconds + sum(segment.duration or 0 for segment in pending_segments), recorded_seconds),
        max(recorded_count + len(pending_segments), recorded_count),
    )


def _emit_live_progress(
    options: LiveRecordOptions,
    stream: StreamInfo,
    recorded_seconds: float,
    segments_count: int,
    new_segments: int,
    path: Path,
    status: str,
    available_seconds: float | None = None,
    available_segments: int | None = None,
    done: bool = False,
    downloaded_bytes: int = 0,
) -> None:
    if not options.progress:
        return
    options.progress(
        LiveProgressUpdate(
            stream=stream,
            recorded_seconds=recorded_seconds,
            limit_seconds=_effective_record_limit(options),
            segments_count=segments_count,
            new_segments=new_segments,
            path=path,
            available_seconds=available_seconds,
            available_segments=available_segments,
            status=status,
            done=done,
            downloaded_bytes=max(0, int(downloaded_bytes or 0)),
        )
    )


def _append_segments(
    segments: list[SegmentInfo],
    output_path: Path,
    temp_dir: Path,
    headers: dict[str, str] | None,
    workers: int,
    prefer_curl: bool = False,
    hls_crypto: HlsCrypto | None = None,
    retries: int = 3,
    request_timeout: int = 30,
    progress: Callable[[SegmentInfo, Path], None] | None = None,
    check_segments_count: bool = True,
    host_limiter: _HostAdaptiveLimiter | None = None,
    assemble_output: bool = True,
    retry_grace: bool = True,
) -> list[Path]:
    batch_id = time.time_ns()
    part_paths = [temp_dir / f"{batch_id}_{index:08d}.part" for index in range(len(segments))]
    failures = _download_live_segment_parts(
        segments,
        part_paths,
        range(len(segments)),
        headers=headers,
        workers=max(1, workers),
        prefer_curl=prefer_curl,
        hls_crypto=hls_crypto,
        retries=retries,
        request_timeout=request_timeout,
        progress=progress,
        host_limiter=host_limiter,
    )
    if retry_grace and failures and workers > 1:
        _runtime_sleep(0.75)
        failures = _download_live_segment_parts(
            segments,
            part_paths,
            sorted(failures),
            headers=headers,
            workers=1,
            prefer_curl=prefer_curl,
            hls_crypto=hls_crypto,
            retries=max(retries, 8),
            request_timeout=request_timeout,
            progress=progress,
            host_limiter=host_limiter,
        )
    if retry_grace and failures:
        _runtime_sleep(2.5)
        failures = _download_live_segment_parts(
            segments,
            part_paths,
            sorted(failures),
            headers=headers,
            workers=1,
            prefer_curl=prefer_curl,
            hls_crypto=hls_crypto,
            retries=max(retries, 12),
            request_timeout=request_timeout,
            progress=progress,
            host_limiter=host_limiter,
        )
    if failures:
        index = min(failures)
        raise failures[index]
    if check_segments_count:
        _check_parts_count(part_paths, len(segments))
    if assemble_output:
        with output_path.open("ab") as output:
            for part_path in part_paths:
                with part_path.open("rb") as part:
                    shutil.copyfileobj(part, output, length=1024 * 1024)
    return part_paths


def _download_live_segment_parts(
    segments: list[SegmentInfo],
    part_paths: list[Path],
    indexes,
    headers: dict[str, str] | None,
    workers: int,
    prefer_curl: bool,
    hls_crypto: HlsCrypto | None,
    retries: int,
    request_timeout: int,
    progress: Callable[[SegmentInfo, Path], None] | None,
    host_limiter: _HostAdaptiveLimiter | None = None,
) -> dict[int, Exception]:
    failures: dict[int, Exception] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = {}
    try:
        for index in indexes:
            args = (segments[index], part_paths[index], headers, prefer_curl, hls_crypto, retries)
            kwargs = {}
            if request_timeout != 30:
                kwargs["request_timeout"] = request_timeout
            if host_limiter is not None:
                kwargs["host_limiter"] = host_limiter
            future = pool.submit(_download_segment_to_path, *args, **kwargs)
            futures[future] = index
        for future in as_completed(futures):
            index = futures[future]
            try:
                future.result()
            except Exception as exc:
                failures[index] = exc
                try:
                    part_paths[index].unlink()
                except OSError:
                    pass
                continue
            if progress:
                progress(segments[index], part_paths[index])
    except BaseException:
        _cancel_futures_now(pool, futures)
        raise
    else:
        pool.shutdown(wait=True)
    return failures


def _cancel_futures_now(pool: ThreadPoolExecutor, futures) -> None:
    for future in futures:
        future.cancel()
    pool.shutdown(wait=current_download_runtime() is not None, cancel_futures=True)


def _download_segment_to_path(
    segment: SegmentInfo,
    path: Path,
    headers: dict[str, str] | None,
    prefer_curl: bool = False,
    hls_crypto: HlsCrypto | None = None,
    retries: int = 3,
    request_timeout: int = 30,
    host_limiter: _HostAdaptiveLimiter | None = None,
) -> None:
    path.write_bytes(_fetch_bytes(segment, headers=headers, retries=retries, prefer_curl=prefer_curl, hls_crypto=hls_crypto, request_timeout=request_timeout, host_limiter=host_limiter))


def _paths_total_size(paths: list[Path]) -> int:
    return sum(_path_size(path) for path in paths)


def _path_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _check_parts_count(part_paths: list[Path], expected_count: int) -> None:
    actual_count = sum(1 for path in part_paths if path.exists())
    if actual_count != expected_count:
        raise RuntimeError(f"Downloaded segment count mismatch: expected {expected_count}, got {actual_count}.")


def _segment_key(segment: SegmentInfo) -> str:
    if segment.index is not None and segment.index >= 0:
        return str(segment.index)
    return segment.url


def _live_refresh_source(stream: StreamInfo) -> str:
    if _is_sabr_ump_stream(stream):
        return ""
    if stream.manifest_type == "json":
        if stream.original_url:
            return stream.original_url
        if should_repeat_live_media_request(stream):
            return ""
    if stream.manifest_type in {"dash", "ism"} and stream.original_url:
        if stream.manifest_type == "dash":
            refresh_url = (stream.extra or {}).get("dash_refresh_url")
            if isinstance(refresh_url, str) and refresh_url:
                return refresh_url
        return stream.original_url
    return stream.url or stream.original_url


def _live_refresh_sources(stream: StreamInfo) -> list[str]:
    if _is_sabr_ump_stream(stream):
        return []
    sources: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in sources:
            sources.append(value)

    if stream.manifest_type == "json" and should_repeat_live_media_request(stream):
        add(stream.original_url)
        return sources

    add(_live_refresh_source(stream))
    if stream.manifest_type == "dash":
        refresh_url = (stream.extra or {}).get("dash_refresh_url") if stream.extra else None
        if isinstance(refresh_url, str):
            add(refresh_url)
    add(stream.original_url)
    add(stream.url)
    return sources


def _load_refreshed_streams(source: str, options: LiveRecordOptions, headers: dict[str, str] | None = None) -> list[StreamInfo] | None:
    try:
        refreshed = parse_source(source, headers=headers, fetch_child_playlists=True)
    except (LoadError, ValueError, SyntaxError, ET.ParseError):
        return None
    _transform_refreshed_streams(options, refreshed)
    _ensure_live_json_init_segments(refreshed)
    return refreshed


def _refresh_stream_from_candidates(stream: StreamInfo, options: LiveRecordOptions, headers: dict[str, str] | None = None) -> StreamInfo | None:
    for source in _live_refresh_sources(stream):
        refreshed = _load_refreshed_streams(source, options, headers=headers)
        if refreshed is None:
            continue
        if not refreshed:
            return stream
        matched = _match_refreshed_stream(stream, refreshed)
        if matched:
            return matched
        if len(refreshed) == 1:
            return refreshed[0]
    return None


def _refresh_live_state_from_candidates(
    state: _LiveRecordState,
    options: LiveRecordOptions,
    headers: dict[str, str] | None = None,
    skip_sources: set[str] | None = None,
) -> StreamInfo | None:
    last_refreshed: list[StreamInfo] | None = None
    for source in _live_refresh_sources(state.current_stream):
        if skip_sources and source in skip_sources:
            continue
        refreshed = _load_refreshed_streams(source, options, headers=headers)
        if refreshed is None:
            continue
        last_refreshed = refreshed
        matched = _match_live_state_refreshed_stream(state, refreshed, options)
        if matched:
            return matched
    if last_refreshed:
        return _match_refreshed_stream(state.current_stream, last_refreshed) or (last_refreshed[0] if len(last_refreshed) == 1 else None)
    return None


def _match_refreshed_stream(source: StreamInfo, candidates: list[StreamInfo]) -> StreamInfo | None:
    scored = [(_stream_match_score(source, candidate), candidate) for candidate in candidates]
    scored = [(score, candidate) for score, candidate in scored if score >= 0]
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def _match_live_state_refreshed_stream(
    state: _LiveRecordState,
    candidates: list[StreamInfo],
    options: LiveRecordOptions,
) -> StreamInfo | None:
    exact = _match_refreshed_stream(state.current_stream, candidates)
    if exact and _stream_has_new_live_media(exact, state.seen, state.edge_index, state.segments_count, options):
        return exact
    alternatives = [
        candidate
        for candidate in candidates
        if _is_equivalent_live_rendition(state.current_stream, candidate)
        and _stream_has_new_live_media(candidate, state.seen, state.edge_index, state.segments_count, options)
    ]
    if alternatives:
        alternatives.sort(
            key=lambda candidate: (
                _stream_match_score(state.current_stream, candidate),
                _latest_media_index(candidate),
            ),
            reverse=True,
        )
        return alternatives[0]
    return exact


def _stream_match_score(source: StreamInfo, candidate: StreamInfo) -> int:
    if source.media_type != candidate.media_type:
        return -1
    score = 0
    for attr, weight in (
        ("id", 40),
        ("group_id", 20),
        ("language", 10),
        ("resolution", 10),
        ("bandwidth", 10),
        ("codecs", 8),
        ("channels", 6),
        ("role", 4),
        ("name", 4),
    ):
        left = getattr(source, attr, None)
        right = getattr(candidate, attr, None)
        if left and right and left == right:
            score += weight
    return score


def _is_equivalent_live_rendition(source: StreamInfo, candidate: StreamInfo) -> bool:
    if source.media_type != candidate.media_type:
        return False
    if source.media_type == "video":
        for attr in ("resolution", "frame_rate", "video_range"):
            left = getattr(source, attr, None)
            right = getattr(candidate, attr, None)
            if left and right and left != right:
                return False
        if not _close_bandwidth(source.bandwidth, candidate.bandwidth):
            return False
        return _codec_family(source.codecs, source.media_type) == _codec_family(candidate.codecs, candidate.media_type)
    if source.media_type == "audio":
        for attr in ("language", "name", "channels", "role"):
            left = getattr(source, attr, None)
            right = getattr(candidate, attr, None)
            if left and right and left != right:
                return False
        if not _close_bandwidth(source.bandwidth, candidate.bandwidth):
            return False
        return _codec_family(source.codecs, source.media_type) == _codec_family(candidate.codecs, candidate.media_type)
    return False


def _close_bandwidth(left: int | None, right: int | None) -> bool:
    if not left or not right:
        return True
    return abs(left - right) <= max(8_000, int(max(left, right) * 0.08))


def _codec_family(codecs: str | None, media_type: str) -> str:
    text = (codecs or "").lower()
    if media_type == "audio":
        if "ec-3" in text or "eac3" in text:
            return "eac3"
        if "ac-3" in text or "ac3" in text:
            return "ac3"
        if "mp4a" in text or "aac" in text:
            return "aac"
        if "opus" in text:
            return "opus"
    if any(token in text for token in ("vvc1", "vvi1", "h266", "h.266", "vvc")):
        return "vvc"
    if any(token in text for token in ("dvh1", "dvhe", "hvc1", "hev1", "hevc", "h265")):
        return "hevc"
    if any(token in text for token in ("avc1", "avc3", "h264")):
        return "h264"
    if any(token in text for token in ("av01", "av1")):
        return "av1"
    if any(token in text for token in ("vp09", "vp9")):
        return "vp9"
    return text


def _latest_media_index(stream: StreamInfo) -> int:
    indexes = [segment.index for segment in stream.segments if segment.index is not None and segment.index >= 0]
    return max(indexes, default=-1)


def _has_unseen(stream: StreamInfo, seen: set[str]) -> bool:
    return any(_segment_key(segment) not in seen for segment in stream.segments if segment.index != -1)


def _default_wait_seconds(stream: StreamInfo) -> int:
    target_duration = stream.extra.get("target_duration") if stream.extra else None
    if target_duration:
        return max(1, int(float(target_duration)))
    media_durations = [segment.duration or 0 for segment in stream.segments if segment.index != -1]
    media_durations = [duration for duration in media_durations if duration > 0]
    if stream.is_live and media_durations:
        sample = media_durations[-min(len(media_durations), 10) :]
        average = sum(sample) / len(sample)
        return max(1, min(30, int(max(average / 2, 1))))
    total = sum(media_durations)
    if total > 0:
        return max(1, int(total / 2) - 2)
    return 3


def _live_output_path(stream: StreamInfo, output_dir: str | Path, filename: str | None) -> Path:
    output_dir = Path(output_dir).expanduser()
    if filename:
        path = Path(filename)
        if path.suffix.lower() in KNOWN_OUTPUT_SUFFIXES:
            return path if path.is_absolute() else output_dir / path
        return output_dir / f"{filename}.{_live_extension(stream)}"
    name = stream.name or stream.id or stream.display_prefix().lower()
    return output_dir / f"{_safe_name(name)}.{_live_extension(stream)}"


def _live_extension(stream: StreamInfo) -> str:
    extension = (stream.extension or "").lower()
    if extension in {"ts", "mp4", "m4a", "vtt", "ttml", "srt", "aac", "mp3", "webm"}:
        return extension
    if stream.media_type == "audio":
        return "m4a" if extension == "m4s" else "aac"
    if stream.media_type in {"subtitle", "subtitles", "text"}:
        codec = (stream.codecs or "").lower()
        return "ttml" if "ttml" in codec or "stpp" in codec else "vtt"
    return "mp4" if extension == "m4s" else "ts"


def _safe_name(value: str) -> str:
    chars = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            chars.append(char)
        elif char.isspace():
            chars.append("_")
    return "".join(chars).strip("._") or "live"


def _format_seconds(seconds: float) -> str:
    seconds_int = int(seconds)
    hours, remainder = divmod(seconds_int, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
