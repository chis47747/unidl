from __future__ import annotations

import base64
import http.client
import io
import math
import re
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from .embedding import current_download_runtime
from .http_client import HttpClientError, get_global_http_client
from .loader import DEFAULT_USER_AGENT
from .models import SegmentInfo, StreamInfo

WIRE_VARINT = 0
WIRE_BYTES = 2
WIRE_32BIT = 5
WIRE_64BIT = 1
HDR10_SABR_VIDEO_ITAGS = {
    330,
    331,
    332,
    333,
    334,
    335,
    336,
    337,
    361,
    362,
    363,
    364,
    365,
    366,
    367,
    368,
}
PREFERRED_SABR_VIDEO_LADDER = (368, 337, 558, 367, 557, 366, 411, 365, 409, 364, 317, 363, 280, 362, 279, 361)
PREFERRED_SABR_AUDIO_ITAGS = (149, 148)
SABR_REDIRECT_PART_TYPE = 43
SABR_DVR_BATCH_SIZE = 8
SABR_DVR_DEFAULT_PARALLEL_BATCHES = 4
SABR_DVR_MAX_PARALLEL_BATCHES = 8
SABR_DVR_MISSING_SEQUENCE_SKIP_LIMIT = 6
SABR_LIVE_DEFAULT_SEGMENT_SECONDS = 5.0
SABR_CONNECTION_DROP_ATTEMPTS = 8
SABR_KEY_PROBE_MAX_BYTES = 768 * 1024
_URL_RE = re.compile(rb"https?://[^\s\"'<>\\\x00-\x1f]+")


def _runtime_sleep(seconds: float) -> None:
    """Make SABR retry backoff wake immediately on embedded shutdown."""
    runtime = current_download_runtime()
    if runtime is None:
        time.sleep(max(0.0, float(seconds)))
    else:
        if runtime.wait(seconds):
            runtime.checkpoint()


class SabrUmpError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FormatId:
    itag: int
    lmt: int = 0
    xtags: str = ""


@dataclass(frozen=True, slots=True)
class ClientInfo:
    client_name: int
    client_version: str
    hl: str = "en_US"
    device_make: str = ""
    device_model: str = ""
    os_name: str = ""
    os_version: str = ""


@dataclass(frozen=True, slots=True)
class ProtoField:
    number: int
    wire_type: int
    value: int | bytes


@dataclass(frozen=True, slots=True)
class UmpPart:
    part_type: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class TimeRange:
    start_ticks: int | None = None
    duration_ticks: int | None = None
    timescale: int | None = None


@dataclass(frozen=True, slots=True)
class SabrSegmentRequest:
    format_id: FormatId | int
    first_sequence: int
    last_sequence: int | None = None
    start_time_ms: int | None = None
    duration_ms: int | None = None
    time_range: TimeRange | None = None
    secondary_time_range: TimeRange | None = None
    android_time_range: TimeRange | None = None


@dataclass(frozen=True, slots=True)
class MediaHeader:
    header_id: int | None
    format_id: FormatId
    sequence_number: int | None = None
    start_ms: int | None = None
    duration_ms: int | None = None
    time_range: TimeRange | None = None
    is_init: bool = False


@dataclass(frozen=True, slots=True)
class SabrUmpDownloadStats:
    media_bytes: int
    chunks: int
    headers: int
    ended: bool
    seen_itags: tuple[int, ...]
    redirect_url: str = ""
    completed_segments: int = 0
    total_segments: int = 0


def download_sabr_ump_stream(
    stream: StreamInfo,
    output_path: Path,
    *,
    headers: dict[str, str] | None = None,
    request_timeout: int = 30,
    retries: int = 3,
    progress: Callable[[int, int, int], None] | None = None,
) -> SabrUmpDownloadStats:
    request = _build_stream_request(stream)
    request_headers = _sabr_request_headers(_merged_sabr_headers(stream, headers))
    attempts = _sabr_retry_loop_attempts(retries)
    last_error: BaseException | None = None
    stats: SabrUmpDownloadStats | None = None
    try:
        for attempt in range(attempts):
            current_url = request.url
            redirect_chain: list[str] = []
            try:
                for _redirect_attempt in range(4):
                    with get_global_http_client().request(
                        "POST",
                        current_url,
                        headers=request_headers,
                        timeout=request_timeout,
                        body=request.body,
                    ) as response:
                        stats = _write_selected_media_from_ump_response(
                            response,
                            output_path,
                            target_itag=request.target_format.itag,
                            progress=progress,
                            base_url=current_url,
                        )
                    if stats.media_bytes or not stats.redirect_url:
                        break
                    current_url = urljoin(current_url, stats.redirect_url)
                    if current_url in redirect_chain:
                        raise SabrUmpError("SABR/UMP redirect loop while following part 43.")
                    redirect_chain.append(current_url)
                else:
                    raise SabrUmpError("SABR/UMP redirect chain was too long.")
                break
            except (HttpClientError, OSError, http.client.HTTPException) as exc:
                last_error = exc
                _unlink_quietly(output_path)
                if not _should_retry_sabr_error(exc, attempt=attempt, retries=retries):
                    raise
                _runtime_sleep(_sabr_retry_delay(attempt))
    except HttpClientError as exc:
        raise SabrUmpError(_http_error_message(exc, request)) from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise SabrUmpError(str(exc) or exc.__class__.__name__) from exc
    if stats is None:
        raise SabrUmpError(str(last_error) if last_error else "SABR/UMP request failed.")

    if not stats.media_bytes:
        _unlink_quietly(output_path)
        seen = ", ".join(str(item) for item in stats.seen_itags) or "none"
        redirect = " redirect frame was present." if stats.redirect_url else ""
        raise SabrUmpError(f"SABR/UMP response did not contain media chunks for itag {request.target_format.itag}; seen itags: {seen}.{redirect}")
    if not stats.ended:
        _unlink_quietly(output_path)
        raise SabrUmpError(
            "SABR/UMP initial response returned media, but follow-up request state is not mapped yet; "
            "refusing to keep a partial file."
        )
    return stats


def download_sabr_ump_dvr_stream(
    stream: StreamInfo,
    output_path: Path,
    *,
    headers: dict[str, str] | None = None,
    request_timeout: int = 30,
    retries: int = 3,
    workers: int = 1,
    progress: Callable[[int, int, int], None] | None = None,
) -> SabrUmpDownloadStats:
    request = _build_stream_request(stream)
    templates = _segment_request_templates(request.body, request)
    start_sequence = _int_or_none((stream.extra or {}).get("sabr_dvr_min_sequence"))
    if start_sequence is None and templates:
        start_sequence = min(template.first_sequence for template in templates)
    if start_sequence is None:
        return download_sabr_ump_stream(
            stream,
            output_path,
            headers=headers,
            request_timeout=request_timeout,
            retries=retries,
            progress=progress,
        )

    segment_duration_ms = _template_segment_duration_ms(templates) or 5000
    needs_timeline_calibration = False
    if templates:
        anchor = min(templates, key=lambda template: template.first_sequence)
        end_hint = max(template.last_sequence if template.last_sequence is not None else template.first_sequence for template in templates)
    else:
        if not bool((stream.extra or {}).get("sabr_dvr_as_vod")):
            return download_sabr_ump_stream(
                stream,
                output_path,
                headers=headers,
                request_timeout=request_timeout,
                retries=retries,
                progress=progress,
            )
        anchor = _sabr_dvr_sequence_anchor(request.target_format, int(start_sequence), segment_duration_ms)
        needs_timeline_calibration = True
        end_hint = int(start_sequence)
    explicit_batch_size = _int_or_none((stream.extra or {}).get("sabr_dvr_batch_size")) is not None
    batch_size = _sabr_dvr_batch_size(stream) if explicit_batch_size else 1
    if end_hint < start_sequence:
        return download_sabr_ump_stream(
            stream,
            output_path,
            headers=headers,
            request_timeout=request_timeout,
            retries=retries,
            progress=progress,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    request_headers = _sabr_request_headers(_merged_sabr_headers(stream, headers))
    sequence_cache: dict[int, tuple[list[SegmentInfo], SabrUmpDownloadStats]] = {}
    if needs_timeline_calibration:
        anchor = _calibrate_sabr_dvr_sequence_anchor(
            stream,
            request,
            anchor,
            start_sequence=int(start_sequence),
            segment_duration_ms=segment_duration_ms,
            headers=request_headers,
            request_timeout=request_timeout,
            retries=retries,
            cache=sequence_cache,
        )
        if anchor.first_sequence > start_sequence:
            start_sequence = int(anchor.first_sequence)
    reliable_end = _int_or_none((stream.extra or {}).get("sabr_dvr_end_sequence"))
    if reliable_end is not None and reliable_end >= start_sequence:
        end_sequence = reliable_end
    else:
        end_sequence = _probe_sabr_dvr_sequence_end(
            stream,
            request,
            anchor,
            start_sequence=int(start_sequence),
            end_hint=int(end_hint),
            segment_duration_ms=segment_duration_ms,
            headers=request_headers,
            request_timeout=request_timeout,
            retries=retries,
            cache=sequence_cache,
        )
    total_segments = max(0, int(end_sequence) - int(start_sequence) + 1)
    if progress:
        progress(0, total_segments, 0)
    seen_sequences: set[int] = set()
    skipped_sequences: set[int] = set()
    chunks = 0
    media_bytes = 0
    header_count = 0
    seen_itags: set[int] = set()
    ended = False
    webm_writer = None
    queued_segments: dict[int, SegmentInfo] = {}
    missing_sequence_skip_limit = _sabr_dvr_missing_sequence_skip_limit(stream)
    consecutive_missing_sequences = 0
    current_anchor = anchor

    with output_path.open("wb") as output:
        if _stream_uses_webm_container(stream):
            from .webm_live import ContinuousWebMWriter, WebMLiveWriterError

            webm_writer = ContinuousWebMWriter(output)

        def collect_result(segments: list[SegmentInfo], stats: SabrUmpDownloadStats) -> None:
            nonlocal chunks, media_bytes, header_count, ended
            chunks += stats.chunks
            media_bytes += stats.media_bytes
            header_count += stats.headers
            seen_itags.update(stats.seen_itags)
            ended = stats.ended or ended
            for segment in segments:
                if (
                    segment.index is not None
                    and segment.index >= start_sequence
                    and segment.index <= end_sequence
                    and segment.index not in seen_sequences
                ):
                    queued_segments.setdefault(int(segment.index), segment)

        def write_ready_segments() -> bool:
            nonlocal pending_sequence, consecutive_missing_sequences, current_anchor
            wrote_any = False
            while pending_sequence in queued_segments:
                segment = queued_segments.pop(pending_sequence)
                data = segment.data or b""
                if webm_writer is not None:
                    try:
                        duration_hint = None if getattr(stream, "media_type", None) == "video" else segment.duration
                        webm_writer.write_fragment(io.BytesIO(data), duration_seconds=duration_hint)
                    except WebMLiveWriterError as exc:
                        raise SabrUmpError(f"SABR/UMP DVR WebM fragment merge failed at sequence {segment.index}: {exc}") from exc
                else:
                    output.write(data)
                if segment.index is not None:
                    seen_sequences.add(segment.index)
                    refreshed_anchor = _sabr_dvr_anchor_from_segment(request.target_format, segment, default_duration_ms=segment_duration_ms)
                    if refreshed_anchor is not None:
                        current_anchor = refreshed_anchor
                if progress:
                    progress(min(total_segments, len(seen_sequences) + len(skipped_sequences)), total_segments, media_bytes)
                pending_sequence += 1
                consecutive_missing_sequences = 0
                wrote_any = True
            return wrote_any

        def skip_missing_sequence_if_future_is_ready() -> bool:
            nonlocal pending_sequence, consecutive_missing_sequences
            if missing_sequence_skip_limit <= 0 or pending_sequence in queued_segments:
                return False
            future_sequences = [sequence for sequence in queued_segments if sequence > pending_sequence]
            if not future_sequences:
                return False
            if consecutive_missing_sequences >= missing_sequence_skip_limit:
                return False
            skipped_sequences.add(pending_sequence)
            pending_sequence += 1
            consecutive_missing_sequences += 1
            if progress:
                progress(min(total_segments, len(seen_sequences) + len(skipped_sequences)), total_segments, media_bytes)
            return True

        def fetch_missing_sequence(sequence: int) -> None:
            request_anchor = current_anchor
            try:
                segments, stats = _fetch_sabr_dvr_sequence(
                    stream,
                    request,
                    request_anchor,
                    sequence=sequence,
                    segment_duration_ms=segment_duration_ms,
                    headers=request_headers,
                    request_timeout=request_timeout,
                    retries=retries,
                )
            except HttpClientError as exc:
                built = _build_sabr_dvr_sequence_request(
                    stream,
                    request,
                    request_anchor,
                    sequence=sequence,
                    segment_duration_ms=segment_duration_ms,
                )
                raise SabrUmpError(_http_error_message(exc, built)) from exc
            except (OSError, ValueError) as exc:
                raise SabrUmpError(str(exc) or exc.__class__.__name__) from exc
            collect_result(segments, stats)

        pending_sequence = int(start_sequence)
        end_sequence = int(end_sequence)
        parallel_batches = _sabr_dvr_parallel_batches(stream, workers)
        if batch_size == 1 and _int_or_none((stream.extra or {}).get("sabr_dvr_parallel_batches")) is None:
            parallel_batches = max(parallel_batches, min(SABR_DVR_MAX_PARALLEL_BATCHES, max(1, int(workers or 1))))

        def fetch_sequence_range(first_sequence: int, last_sequence: int) -> tuple[list[SegmentInfo], SabrUmpDownloadStats]:
            request_anchor = current_anchor
            try:
                return _fetch_sabr_dvr_sequence_range(
                    stream,
                    request,
                    request_anchor,
                    first_sequence=first_sequence,
                    last_sequence=last_sequence,
                    segment_duration_ms=segment_duration_ms,
                    headers=request_headers,
                    request_timeout=request_timeout,
                    retries=retries,
                )
            except HttpClientError as exc:
                built = _build_sabr_dvr_sequence_request(
                    stream,
                    request,
                    request_anchor,
                    sequence=first_sequence,
                    last_sequence=last_sequence,
                    segment_duration_ms=segment_duration_ms,
                )
                raise SabrUmpError(_http_error_message(exc, built)) from exc
            except (OSError, ValueError) as exc:
                raise SabrUmpError(str(exc) or exc.__class__.__name__) from exc

        def fetch_and_write_missing_head() -> None:
            fetch_missing_sequence(pending_sequence)
            if not write_ready_segments():
                if skip_missing_sequence_if_future_is_ready():
                    write_ready_segments()
                    return
                seen = ", ".join(str(item) for item in sorted(seen_itags)) or "none"
                raise SabrUmpError(
                    f"SABR/UMP DVR response did not contain media chunks for sequence {pending_sequence} "
                    f"of itag {request.target_format.itag}; seen itags: {seen}."
                )

        if parallel_batches <= 1:
            while pending_sequence <= end_sequence:
                cached = sequence_cache.pop(pending_sequence, None)
                if cached is not None:
                    segments, stats = cached
                else:
                    fetched_end = min(end_sequence, pending_sequence + batch_size - 1)
                    segments, stats = fetch_sequence_range(pending_sequence, fetched_end)

                collect_result(segments, stats)
                if write_ready_segments():
                    continue

                fetch_and_write_missing_head()
        else:
            next_fetch_sequence = pending_sequence
            executor = ThreadPoolExecutor(max_workers=parallel_batches)
            inflight: dict[Any, tuple[int, int]] = {}

            def submit_more() -> None:
                nonlocal next_fetch_sequence
                if next_fetch_sequence < pending_sequence:
                    next_fetch_sequence = pending_sequence
                while len(inflight) < parallel_batches and next_fetch_sequence <= end_sequence:
                    if next_fetch_sequence in sequence_cache:
                        return
                    first = next_fetch_sequence
                    last = min(end_sequence, first + batch_size - 1)
                    inflight[executor.submit(fetch_sequence_range, first, last)] = (first, last)
                    next_fetch_sequence = last + 1

            def collect_future(future: Any) -> None:
                _range = inflight.pop(future)
                segments, stats = future.result()
                collect_result(segments, stats)

            try:
                submit_more()
                while pending_sequence <= end_sequence:
                    cached = sequence_cache.pop(pending_sequence, None)
                    if cached is not None:
                        segments, stats = cached
                        collect_result(segments, stats)
                        if write_ready_segments():
                            submit_more()
                            continue

                    if write_ready_segments():
                        submit_more()
                        continue

                    submit_more()
                    if any(first <= pending_sequence <= last for first, last in inflight.values()):
                        done, _pending = wait(inflight.keys(), return_when=FIRST_COMPLETED)
                        for future in done:
                            collect_future(future)
                        continue

                    if inflight:
                        fetch_and_write_missing_head()
                        submit_more()
                        continue

                    fetch_and_write_missing_head()
                    submit_more()
            finally:
                for future in list(inflight):
                    future.cancel()
                executor.shutdown(
                    wait=current_download_runtime() is not None,
                    cancel_futures=True,
                )

    if not seen_sequences:
        _unlink_quietly(output_path)
        seen = ", ".join(str(item) for item in sorted(seen_itags)) or "none"
        raise SabrUmpError(f"SABR/UMP DVR response did not contain media chunks for itag {request.target_format.itag}; seen itags: {seen}.")
    return SabrUmpDownloadStats(
        media_bytes=media_bytes,
        chunks=chunks,
        headers=header_count,
        ended=ended,
        seen_itags=tuple(sorted(seen_itags)),
        completed_segments=min(total_segments, len(seen_sequences) + len(skipped_sequences)),
        total_segments=total_segments,
    )


def probe_sabr_ump_dvr_sequence_window(
    stream: StreamInfo,
    *,
    headers: dict[str, str] | None = None,
    request_timeout: int = 30,
    retries: int = 3,
) -> tuple[int, int] | None:
    request = _build_stream_request(stream)
    templates = _segment_request_templates(request.body, request)
    start_sequence = _int_or_none((stream.extra or {}).get("sabr_dvr_min_sequence"))
    if start_sequence is None and templates:
        start_sequence = min(template.first_sequence for template in templates)
    if start_sequence is None:
        return None
    segment_duration_ms = _template_segment_duration_ms(templates) or 5000
    needs_timeline_calibration = False
    if templates:
        anchor = min(templates, key=lambda template: template.first_sequence)
        end_hint = max(template.last_sequence if template.last_sequence is not None else template.first_sequence for template in templates)
    else:
        if not bool((stream.extra or {}).get("sabr_dvr_as_vod")):
            return None
        anchor = _sabr_dvr_sequence_anchor(request.target_format, int(start_sequence), segment_duration_ms)
        needs_timeline_calibration = True
        end_hint = int(start_sequence)
    if end_hint < start_sequence:
        return None
    reliable_end = _int_or_none((stream.extra or {}).get("sabr_dvr_end_sequence"))
    if reliable_end is not None and reliable_end >= start_sequence:
        return int(start_sequence), int(reliable_end)
    request_headers = _sabr_request_headers(_merged_sabr_headers(stream, headers))
    sequence_cache: dict[int, tuple[list[SegmentInfo], SabrUmpDownloadStats]] = {}
    if needs_timeline_calibration:
        anchor = _calibrate_sabr_dvr_sequence_anchor(
            stream,
            request,
            anchor,
            start_sequence=int(start_sequence),
            segment_duration_ms=segment_duration_ms,
            headers=request_headers,
            request_timeout=request_timeout,
            retries=retries,
            cache=sequence_cache,
        )
    end_sequence = _probe_sabr_dvr_sequence_end(
        stream,
        request,
        anchor,
        start_sequence=int(start_sequence),
        end_hint=int(end_hint),
        segment_duration_ms=segment_duration_ms,
        headers=request_headers,
        request_timeout=request_timeout,
        retries=retries,
        cache=sequence_cache,
    )
    return int(start_sequence), int(end_sequence)


def probe_sabr_ump_dvr_first_media_sequence(
    stream: StreamInfo,
    *,
    headers: dict[str, str] | None = None,
    request_timeout: int = 30,
    retries: int = 3,
) -> int | None:
    request = _build_stream_request(stream)
    templates = _segment_request_templates(request.body, request)
    start_sequence = _int_or_none((stream.extra or {}).get("sabr_dvr_min_sequence"))
    if start_sequence is None and templates:
        start_sequence = min(template.first_sequence for template in templates)
    if start_sequence is None:
        return None

    segment_duration_ms = _template_segment_duration_ms(templates) or 5000
    needs_timeline_calibration = False
    if templates:
        anchor = min(templates, key=lambda template: template.first_sequence)
    else:
        if not bool((stream.extra or {}).get("sabr_dvr_as_vod")):
            return None
        anchor = _sabr_dvr_sequence_anchor(request.target_format, int(start_sequence), segment_duration_ms)
        needs_timeline_calibration = True

    request_headers = _sabr_request_headers(_merged_sabr_headers(stream, headers))
    sequence_cache: dict[int, tuple[list[SegmentInfo], SabrUmpDownloadStats]] = {}
    if needs_timeline_calibration:
        anchor = _calibrate_sabr_dvr_sequence_anchor(
            stream,
            request,
            anchor,
            start_sequence=int(start_sequence),
            segment_duration_ms=segment_duration_ms,
            headers=request_headers,
            request_timeout=request_timeout,
            retries=retries,
            cache=sequence_cache,
        )

    candidates: list[int] = []
    for segments, _stats in sequence_cache.values():
        candidates.extend(_sabr_media_sequence_candidates(segments, start_sequence=int(start_sequence)))
    if not candidates:
        segments, _stats = _fetch_sabr_dvr_sequence(
            stream,
            request,
            anchor,
            sequence=int(start_sequence),
            segment_duration_ms=segment_duration_ms,
            headers=request_headers,
            request_timeout=request_timeout,
            retries=retries,
        )
        candidates.extend(_sabr_media_sequence_candidates(segments, start_sequence=int(start_sequence)))
    return min(candidates) if candidates else None


def probe_sabr_ump_key_bytes(
    stream: StreamInfo,
    *,
    headers: dict[str, str] | None = None,
    request_timeout: int = 30,
    retries: int = 2,
    max_bytes: int = SABR_KEY_PROBE_MAX_BYTES,
) -> bytes:
    request = _build_stream_request(stream)
    request_headers = _sabr_request_headers(_merged_sabr_headers(stream, headers))
    probe_request = _build_sabr_key_probe_request(stream, request)
    return _post_sabr_key_probe_bytes(
        probe_request,
        headers=request_headers,
        request_timeout=request_timeout,
        retries=retries,
        max_bytes=max(1, int(max_bytes)),
    )


def _build_sabr_key_probe_request(stream: StreamInfo, request: _SabrStreamRequest) -> _SabrStreamRequest:
    templates = _segment_request_templates(request.body, request)
    start_sequence = _int_or_none((stream.extra or {}).get("sabr_dvr_min_sequence"))
    if start_sequence is None and templates:
        start_sequence = min(template.first_sequence for template in templates)
    if start_sequence is None:
        return request

    segment_duration_ms = _template_segment_duration_ms(templates) or 5000
    if templates:
        anchor = min(templates, key=lambda template: template.first_sequence)
    elif bool((stream.extra or {}).get("sabr_dvr_as_vod")):
        anchor = _sabr_dvr_sequence_anchor(request.target_format, int(start_sequence), segment_duration_ms)
    else:
        return request
    return _build_sabr_dvr_sequence_request(
        stream,
        request,
        anchor,
        sequence=int(start_sequence),
        segment_duration_ms=segment_duration_ms,
    )


def _sabr_media_sequence_candidates(segments: list[SegmentInfo], *, start_sequence: int) -> list[int]:
    return [
        int(segment.index)
        for segment in segments
        if segment.index is not None
        and int(segment.index) >= int(start_sequence)
        and segment.data
    ]


def _stream_uses_webm_container(stream: StreamInfo) -> bool:
    extension = (stream.extension or "").lower().lstrip(".")
    if extension:
        return extension == "webm"
    raw = stream.extra.get("raw") if isinstance(stream.extra, dict) else None
    if isinstance(raw, dict):
        text = " ".join(str(raw.get(key) or "") for key in ("mime_type", "mimeType", "codec", "codecs")).lower()
        if "webm" in text or "vp9" in text or "vp09" in text or "vp8" in text:
            return True
    codecs = (stream.codecs or "").lower()
    return "vp9" in codecs or "vp09" in codecs or "vp8" in codecs


def _probe_sabr_dvr_sequence_end(
    stream: StreamInfo,
    request: _SabrStreamRequest,
    anchor: SabrSegmentRequest,
    *,
    start_sequence: int,
    end_hint: int,
    segment_duration_ms: int,
    headers: dict[str, str],
    request_timeout: int,
    retries: int,
    cache: dict[int, tuple[list[SegmentInfo], SabrUmpDownloadStats]],
) -> int:
    probe_timeout = max(0.8, min(1.25, float(request_timeout or 30)))
    confirm_timeout = max(probe_timeout, min(5.0, max(1.0, float(request_timeout or 30) / 4.0)))
    timeout_misses: set[int] = set()

    def available(sequence: int) -> bool:
        if sequence not in cache:
            try:
                cache[sequence] = _fetch_sabr_dvr_sequence(
                    stream,
                    request,
                    anchor,
                    sequence=sequence,
                    segment_duration_ms=segment_duration_ms,
                    headers=headers,
                    request_timeout=probe_timeout,
                    retries=retries,
                )
            except HttpClientError as exc:
                if _is_timeout_like_error(exc):
                    if sequence not in timeout_misses and confirm_timeout > probe_timeout:
                        timeout_misses.add(sequence)
                        return _confirm_available(sequence)
                    return False
                built = _build_sabr_dvr_sequence_request(stream, request, anchor, sequence=sequence, segment_duration_ms=segment_duration_ms)
                raise SabrUmpError(_http_error_message(exc, built)) from exc
            except (OSError, ValueError) as exc:
                if _is_timeout_like_error(exc):
                    if sequence not in timeout_misses and confirm_timeout > probe_timeout:
                        timeout_misses.add(sequence)
                        return _confirm_available(sequence)
                    return False
                raise SabrUmpError(str(exc) or exc.__class__.__name__) from exc
        segments, _stats = cache[sequence]
        return any(segment.index == sequence and segment.data for segment in segments)

    def _confirm_available(sequence: int) -> bool:
        try:
            cache[sequence] = _fetch_sabr_dvr_sequence(
                stream,
                request,
                anchor,
                sequence=sequence,
                segment_duration_ms=segment_duration_ms,
                headers=headers,
                request_timeout=confirm_timeout,
                retries=max(1, retries),
            )
        except HttpClientError as exc:
            if _is_timeout_like_error(exc):
                return False
            built = _build_sabr_dvr_sequence_request(stream, request, anchor, sequence=sequence, segment_duration_ms=segment_duration_ms)
            raise SabrUmpError(_http_error_message(exc, built)) from exc
        except (OSError, ValueError) as exc:
            if _is_timeout_like_error(exc):
                return False
            raise SabrUmpError(str(exc) or exc.__class__.__name__) from exc
        segments, _stats = cache[sequence]
        return any(segment.index == sequence and segment.data for segment in segments)

    if not available(start_sequence):
        raise SabrUmpError(f"SABR/UMP DVR response did not contain media chunks for the first sequence {start_sequence}.")

    lower = start_sequence
    if end_hint >= start_sequence and available(end_hint):
        lower = end_hint

    step = max(1, lower - start_sequence + 1)
    max_probe_span = _sabr_dvr_max_probe_span(stream, segment_duration_ms, default=20000)
    max_sequence = start_sequence + max_probe_span
    high = lower + step
    while high <= max_sequence and available(high):
        lower = high
        step *= 2
        high = lower + step

    if high > max_sequence:
        high = max_sequence + 1
        if max_sequence > lower and available(max_sequence):
            return max_sequence

    lo = lower + 1
    hi = high - 1
    end = lower
    while lo <= hi:
        mid = (lo + hi) // 2
        if available(mid):
            end = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return end


def _sabr_dvr_max_probe_span(stream: StreamInfo, segment_duration_ms: int, *, default: int) -> int:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    max_count = _int_or_none(extra.get("sabr_dvr_sequence_max_count"))
    if max_count is None:
        duration = _number_or_none(extra.get("sabr_dvr_duration_seconds"))
        segment_seconds = max(0.001, float(segment_duration_ms or 5000) / 1000.0)
        if duration is not None and duration > 0:
            max_count = max(1, int(math.ceil(duration / segment_seconds)) + 12)
    span = max(1, _int_or_none(extra.get("sabr_dvr_sequence_max_probe_span")) or int(default))
    if max_count is not None and max_count > 0:
        span = min(span, max(1, max_count - 1))
    return span


def _sabr_dvr_batch_size(stream: StreamInfo) -> int:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    value = _int_or_none(extra.get("sabr_dvr_batch_size"))
    if value is None:
        value = SABR_DVR_BATCH_SIZE
    return max(1, min(32, int(value)))


def _sabr_dvr_parallel_batches(stream: StreamInfo, workers: int) -> int:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    value = _int_or_none(extra.get("sabr_dvr_parallel_batches"))
    if value is None:
        value = min(SABR_DVR_DEFAULT_PARALLEL_BATCHES, max(1, int(workers or 1)))
    return max(1, min(SABR_DVR_MAX_PARALLEL_BATCHES, int(value), max(1, int(workers or 1))))


def _sabr_dvr_missing_sequence_skip_limit(stream: StreamInfo) -> int:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    value = _int_or_none(extra.get("sabr_dvr_missing_sequence_skip_limit"))
    if value is None:
        value = SABR_DVR_MISSING_SEQUENCE_SKIP_LIMIT
    return max(0, min(100, int(value)))


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_sabr_dvr_sequence(
    stream: StreamInfo,
    request: _SabrStreamRequest,
    anchor: SabrSegmentRequest,
    *,
    sequence: int,
    segment_duration_ms: int,
    headers: dict[str, str],
    request_timeout: int,
    retries: int = 3,
) -> tuple[list[SegmentInfo], SabrUmpDownloadStats]:
    built = _build_sabr_dvr_sequence_request(stream, request, anchor, sequence=sequence, segment_duration_ms=segment_duration_ms)
    return _post_sabr_segments(
        built,
        stream,
        headers=headers,
        request_timeout=request_timeout,
        retries=retries,
    )


def _fetch_sabr_dvr_sequence_range(
    stream: StreamInfo,
    request: _SabrStreamRequest,
    anchor: SabrSegmentRequest,
    *,
    first_sequence: int,
    last_sequence: int,
    segment_duration_ms: int,
    headers: dict[str, str],
    request_timeout: int,
    retries: int = 3,
) -> tuple[list[SegmentInfo], SabrUmpDownloadStats]:
    if last_sequence <= first_sequence:
        return _fetch_sabr_dvr_sequence(
            stream,
            request,
            anchor,
            sequence=first_sequence,
            segment_duration_ms=segment_duration_ms,
            headers=headers,
            request_timeout=request_timeout,
            retries=retries,
        )
    built = _build_sabr_dvr_sequence_request(
        stream,
        request,
        anchor,
        sequence=first_sequence,
        last_sequence=last_sequence,
        segment_duration_ms=segment_duration_ms,
    )
    return _post_sabr_segments(
        built,
        stream,
        headers=headers,
        request_timeout=request_timeout,
        retries=retries,
    )


def _build_sabr_dvr_sequence_request(
    stream: StreamInfo,
    request: _SabrStreamRequest,
    anchor: SabrSegmentRequest,
    *,
    sequence: int,
    last_sequence: int | None = None,
    segment_duration_ms: int,
) -> _SabrStreamRequest:
    segment_request = _dvr_segment_request_for_sequence(
        request.target_format,
        anchor,
        sequence,
        last_sequence,
        segment_duration_ms,
    )
    client_state_playback_ms = None
    if segment_request.start_time_ms is not None:
        client_state_playback_ms = segment_request.start_time_ms + max(1, segment_duration_ms // 2)
    return _build_stream_request(
        stream,
        segment_requests=(segment_request,),
        include_selected_itags=True,
        client_state_playback_ms=client_state_playback_ms,
    )


def _calibrate_sabr_dvr_sequence_anchor(
    stream: StreamInfo,
    request: _SabrStreamRequest,
    anchor: SabrSegmentRequest,
    *,
    start_sequence: int,
    segment_duration_ms: int,
    headers: dict[str, str],
    request_timeout: int,
    retries: int,
    cache: dict[int, tuple[list[SegmentInfo], SabrUmpDownloadStats]],
) -> SabrSegmentRequest:
    try:
        segments, stats = _fetch_sabr_dvr_sequence(
            stream,
            request,
            anchor,
            sequence=int(start_sequence),
            segment_duration_ms=segment_duration_ms,
            headers=headers,
            request_timeout=request_timeout,
            retries=retries,
        )
    except (HttpClientError, OSError, ValueError):
        return anchor
    candidates = [
        segment
        for segment in segments
        if segment.index is not None
        and segment.index >= 0
        and segment.timeline_presentation_time is not None
        and segment.data
    ]
    if not candidates:
        return anchor
    segment = min(candidates, key=lambda item: abs(int(item.index or 0) - int(start_sequence)))
    cache[int(segment.index)] = (segments, stats)
    start_time_ms = int(round(float(segment.timeline_presentation_time or 0.0) * 1000.0))
    duration_ms = int(round(float(segment.duration) * 1000.0)) if segment.duration is not None else int(segment_duration_ms)
    return SabrSegmentRequest(
        format_id=request.target_format,
        first_sequence=int(segment.index),
        last_sequence=int(segment.index),
        start_time_ms=start_time_ms,
        duration_ms=max(1, duration_ms),
    )


def _sabr_dvr_anchor_from_segment(
    video_format: FormatId,
    segment: SegmentInfo,
    *,
    default_duration_ms: int,
) -> SabrSegmentRequest | None:
    if segment.index is None or segment.index < 0 or segment.timeline_presentation_time is None:
        return None
    start_time_ms = int(round(float(segment.timeline_presentation_time) * 1000.0))
    duration_ms = int(round(float(segment.duration) * 1000.0)) if segment.duration is not None else int(default_duration_ms)
    return SabrSegmentRequest(
        format_id=video_format,
        first_sequence=int(segment.index),
        last_sequence=int(segment.index),
        start_time_ms=start_time_ms,
        duration_ms=max(1, duration_ms),
    )


def _is_timeout_like_error(exc: BaseException | None) -> bool:
    text = str(exc or "").lower()
    return "timed out" in text or "timeout" in text


def _is_retryable_sabr_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, http.client.IncompleteRead):
        return True
    text = str(exc).lower()
    retry_tokens = (
        "http error 429",
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
        "temporarily unavailable",
        "service unavailable",
        "too many requests",
        "timed out",
        "timeout",
        "connection reset",
        "connection aborted",
        "remote end closed",
        "broken pipe",
        "unexpected_eof",
        "unexpected eof",
        "eof occurred in violation",
        "ssl.c",
        "incompleteread",
        "incomplete read",
        "chunked",
    )
    if isinstance(exc, HttpClientError):
        return any(token in text for token in retry_tokens)
    if isinstance(exc, (TimeoutError, OSError, http.client.HTTPException)):
        return True
    return any(token in text for token in retry_tokens)


def _is_sabr_connection_drop_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, http.client.IncompleteRead)):
        return True
    text = str(exc).lower()
    drop_tokens = (
        "connection reset",
        "connection aborted",
        "remote end closed",
        "broken pipe",
        "unexpected_eof",
        "unexpected eof",
        "eof occurred in violation",
        "incompleteread",
        "incomplete read",
        "chunked",
    )
    return any(token in text for token in drop_tokens)


def _sabr_base_attempts(retries: int) -> int:
    return max(1, int(retries or 1))


def _sabr_retry_loop_attempts(retries: int) -> int:
    return max(_sabr_base_attempts(retries), SABR_CONNECTION_DROP_ATTEMPTS)


def _sabr_attempt_limit_for_error(retries: int, exc: BaseException) -> int:
    base = _sabr_base_attempts(retries)
    if _is_sabr_connection_drop_error(exc):
        return max(base, SABR_CONNECTION_DROP_ATTEMPTS)
    return base


def _should_retry_sabr_error(exc: BaseException, *, attempt: int, retries: int) -> bool:
    if not _is_retryable_sabr_error(exc):
        return False
    return attempt + 1 < _sabr_attempt_limit_for_error(retries, exc)


def _sabr_retry_delay(attempt: int) -> float:
    return min(3.0, 0.5 * (2 ** max(0, int(attempt))))


@dataclass(frozen=True, slots=True)
class _SabrStreamRequest:
    url: str
    body: bytes
    video_format: FormatId
    target_format: FormatId
    target_media_type: str
    audio_formats: tuple[FormatId, ...]
    po_token_present: bool = False
    po_token_source: str = ""
    po_token_status: str = ""


def _build_stream_request(
    stream: StreamInfo,
    *,
    segment_requests: Iterable[SabrSegmentRequest | bytes] = (),
    include_selected_itags: bool = False,
    client_state_playback_ms: int | None = None,
) -> _SabrStreamRequest:
    extra = stream.extra or {}
    url = str(extra.get("sabr_url") or stream.url or "").strip()
    config_b64 = str(extra.get("sabr_config") or "").strip()
    request_body_b64 = str(extra.get("sabr_request_body") or "").strip()
    target_itag = _int_or_none(extra.get("sabr_itag") or stream.id)
    target_media_type = str(extra.get("sabr_media_type") or getattr(stream, "media_type", "") or "video").lower()
    if target_media_type != "audio":
        target_media_type = "video"
    if not url or not target_itag or (not config_b64 and not request_body_b64):
        raise SabrUmpError("SABR/UMP stream is missing request metadata from server_abr.")

    target_format = FormatId(
        itag=target_itag,
        lmt=_int_or_none(extra.get("sabr_lmt")) or 0,
        xtags=str(extra.get("sabr_xtags") or ""),
    )
    request_video_formats = tuple(_request_video_formats(extra))
    if target_media_type == "video":
        video_format = target_format
        request_video_formats = request_video_formats or (video_format,)
    else:
        video_format = request_video_formats[0] if request_video_formats else _fallback_video_format(extra)
    audio_formats = tuple(_selected_audio_formats(extra))
    if target_media_type == "audio":
        audio_formats = (target_format,)
    po_token = extra.get("sabr_po_token")
    po_token_status = str(extra.get("sabr_po_token_status") or "").strip()
    force_rebuild_body = bool(po_token) and po_token_status == "override"
    segment_requests = tuple(segment_requests)
    selected_audio_format = target_format if target_media_type == "audio" else (audio_formats[0] if audio_formats else None)
    selected_video_itag = video_format.itag if include_selected_itags and target_media_type == "video" else (0 if include_selected_itags else None)
    selected_audio_itag = selected_audio_format.itag if include_selected_itags and selected_audio_format else None
    if request_body_b64 and not force_rebuild_body:
        body = _b64_any_decode(request_body_b64)
        body = _retarget_embedded_request_body(
            body,
            video_format,
            audio_format=selected_audio_format,
            audio_formats=audio_formats,
            segment_requests=segment_requests,
            include_selected_itags=include_selected_itags,
            replace_video_format=target_media_type == "video",
            selected_video_itag=selected_video_itag,
            selected_audio_itag=selected_audio_itag,
            client_state_playback_ms=client_state_playback_ms,
        )
    else:
        if not config_b64:
            raise SabrUmpError(
                "SABR/UMP override PO token requires sabr_config because the saved request body already contains a token."
            )
        config = _b64url_decode(config_b64)
        client = _client_info(extra)
        body = build_sabr_request(
            video_playback_ustreamer_config=config,
            video_formats=request_video_formats,
            audio_formats=audio_formats,
            client_info=client,
            po_token=po_token,
            playback_cookie=extra.get("sabr_playback_cookie"),
            fk=extra.get("sabr_fk"),
            segment_requests=segment_requests,
            selected_video_itag=selected_video_itag,
            selected_audio_itag=selected_audio_itag,
            client_state=_retarget_client_state_playback(
                build_default_client_state(max_height=_height(stream) or 2160),
                client_state_playback_ms,
            ),
            player_time_ms=client_state_playback_ms,
            max_height=_height(stream) or 2160,
        )
    return _SabrStreamRequest(
        url=url,
        body=body,
        video_format=video_format,
        target_format=target_format,
        target_media_type=target_media_type,
        audio_formats=audio_formats,
        po_token_present=bool(
            po_token
            or extra.get("sabr_request_body_po_token_present")
            or extra.get("sabr_request_body_playback_cookie_present")
            or extra.get("sabr_playback_cookie")
        ),
        po_token_source=str(extra.get("sabr_po_token_source") or extra.get("sabr_request_body_source") or "").strip(),
        po_token_status=po_token_status,
    )


def _selected_audio_formats(extra: dict[str, Any]) -> list[FormatId]:
    raw_values = extra.get("sabr_request_audio_formats") or extra.get("sabr_audio_formats") or []
    if not isinstance(raw_values, list):
        return []
    preferred: list[dict[str, Any]] = []
    for item in raw_values:
        if not isinstance(item, dict):
            continue
        itag = _int_or_none(item.get("itag") or item.get("id"))
        if not itag:
            continue
        preferred.append(item)
    if not preferred:
        return []
    for preferred_itag in PREFERRED_SABR_AUDIO_ITAGS:
        matches = [item for item in preferred if _int_or_none(item.get("itag")) == preferred_itag]
        if matches:
            return [
                FormatId(
                    itag=int(item.get("itag")),
                    lmt=_int_or_none(item.get("lmt")) or 0,
                    xtags=str(item.get("xtags") or ""),
                )
                for item in matches
            ]
    selected = preferred[0]
    return [FormatId(itag=int(selected.get("itag")), lmt=_int_or_none(selected.get("lmt")) or 0, xtags=str(selected.get("xtags") or ""))]


def _request_video_formats(extra: dict[str, Any]) -> list[FormatId]:
    raw_values = extra.get("sabr_request_video_formats") or []
    if not isinstance(raw_values, list):
        return []
    formats: list[FormatId] = []
    seen: set[tuple[int, str]] = set()
    for item in raw_values:
        if not isinstance(item, dict):
            continue
        itag = _int_or_none(item.get("itag") or item.get("id"))
        if not itag:
            continue
        xtags = str(item.get("xtags") or "")
        key = (itag, xtags)
        if key in seen:
            continue
        seen.add(key)
        formats.append(FormatId(itag=itag, lmt=_int_or_none(item.get("lmt")) or 0, xtags=xtags))
    by_itag = {fmt.itag: fmt for fmt in formats}
    preferred = [by_itag[itag] for itag in PREFERRED_SABR_VIDEO_LADDER if itag in by_itag]
    return preferred or formats


def _fallback_video_format(extra: dict[str, Any]) -> FormatId:
    raw_values = extra.get("sabr_request_video_formats") or []
    if isinstance(raw_values, list):
        for item in raw_values:
            if not isinstance(item, dict):
                continue
            itag = _int_or_none(item.get("itag") or item.get("id"))
            if itag:
                return FormatId(itag=itag, lmt=_int_or_none(item.get("lmt")) or 0, xtags=str(item.get("xtags") or ""))
    return FormatId(itag=PREFERRED_SABR_VIDEO_LADDER[0])


def _segment_request_templates(body: bytes, request: _SabrStreamRequest) -> list[SabrSegmentRequest]:
    if request.target_media_type == "audio":
        audio_itags = {fmt.itag for fmt in request.audio_formats} or {request.target_format.itag}
        records = _segment_requests_from_body(body)
        audio_records = [record for record in records if _format_itag_value(record.format_id) in audio_itags]
        return audio_records or records
    return _video_segment_request_templates(body, request)


def _video_segment_request_templates(body: bytes, request: _SabrStreamRequest) -> list[SabrSegmentRequest]:
    audio_itags = {fmt.itag for fmt in request.audio_formats}
    records = _segment_requests_from_body(body)
    video_records = [record for record in records if _format_itag_value(record.format_id) not in audio_itags]
    return video_records or records


def _segment_requests_from_body(body: bytes) -> list[SabrSegmentRequest]:
    records: list[SabrSegmentRequest] = []
    try:
        fields = list(iter_proto_fields(body))
    except Exception:
        return records
    for field in fields:
        if field.number != 3 or field.wire_type != WIRE_BYTES or not isinstance(field.value, bytes):
            continue
        record = _parse_sabr_segment_request(field.value)
        if record is not None:
            records.append(record)
    return records


def _parse_sabr_segment_request(payload: bytes) -> SabrSegmentRequest | None:
    try:
        fields = list(iter_proto_fields(payload))
    except Exception:
        return None
    by_num = {field.number: field for field in fields}
    format_bytes = _field_bytes(by_num, 1)
    first_sequence = _field_varint(by_num, 4)
    if not format_bytes or first_sequence is None:
        return None
    last_sequence = _field_varint(by_num, 5)
    time_range_b = _field_bytes(by_num, 11)
    secondary_time_range_b = _field_bytes(by_num, 12)
    android_time_range_b = _field_bytes(by_num, 6)
    return SabrSegmentRequest(
        format_id=parse_format_id(format_bytes),
        first_sequence=first_sequence,
        last_sequence=last_sequence,
        start_time_ms=_field_varint(by_num, 2),
        duration_ms=_field_varint(by_num, 3),
        android_time_range=parse_time_range(android_time_range_b) if android_time_range_b else None,
        time_range=parse_time_range(time_range_b) if time_range_b else None,
        secondary_time_range=parse_time_range(secondary_time_range_b) if secondary_time_range_b else None,
    )


def _format_itag_value(fmt: FormatId | int) -> int:
    return fmt.itag if isinstance(fmt, FormatId) else int(fmt)


def _template_segment_duration_ms(templates: list[SabrSegmentRequest]) -> int | None:
    values: list[int] = []
    for template in templates:
        if template.duration_ms is None:
            continue
        first = int(template.first_sequence)
        last = int(template.last_sequence if template.last_sequence is not None else template.first_sequence)
        count = max(1, last - first + 1)
        duration = max(1, int(round(template.duration_ms / count)))
        values.append(duration)
    if not values:
        return None
    values.sort()
    return values[len(values) // 2]


def _dvr_segment_request_for_sequence(
    video_format: FormatId,
    anchor: SabrSegmentRequest,
    sequence: int,
    last_sequence: int | None,
    segment_duration_ms: int,
) -> SabrSegmentRequest:
    last_sequence = int(last_sequence if last_sequence is not None else sequence)
    count = max(1, last_sequence - int(sequence) + 1)
    start_time_ms = None
    if anchor.start_time_ms is not None:
        start_time_ms = int(anchor.start_time_ms) + (int(sequence) - int(anchor.first_sequence)) * int(segment_duration_ms)
    return SabrSegmentRequest(
        format_id=video_format,
        first_sequence=int(sequence),
        last_sequence=last_sequence,
        start_time_ms=start_time_ms,
        duration_ms=int(segment_duration_ms) * count,
    )


def _sabr_dvr_sequence_anchor(video_format: FormatId, start_sequence: int, segment_duration_ms: int) -> SabrSegmentRequest:
    return SabrSegmentRequest(
        format_id=video_format,
        first_sequence=int(start_sequence),
        last_sequence=int(start_sequence),
        start_time_ms=0,
        duration_ms=int(segment_duration_ms),
    )


def _encode_proto_field(field: ProtoField) -> bytes:
    if field.wire_type == WIRE_VARINT:
        return field_varint(field.number, int(field.value))
    if field.wire_type == WIRE_BYTES and isinstance(field.value, bytes):
        return field_bytes(field.number, field.value)
    if field.wire_type == WIRE_32BIT:
        value = field.value if isinstance(field.value, bytes) else int(field.value).to_bytes(4, "little", signed=False)
        return encode_varint((field.number << 3) | WIRE_32BIT) + value
    if field.wire_type == WIRE_64BIT:
        value = field.value if isinstance(field.value, bytes) else int(field.value).to_bytes(8, "little", signed=False)
        return encode_varint((field.number << 3) | WIRE_64BIT) + value
    raise ValueError(f"unsupported protobuf wire type {field.wire_type}")


def _retarget_client_state_playback(client_state: bytes, playback_ms: int | None) -> bytes:
    if playback_ms is None:
        return client_state
    try:
        fields = list(iter_proto_fields(client_state))
    except Exception:
        return client_state
    out = bytearray()
    replaced = False
    for field in fields:
        if field.number == 28:
            out += field_varint(28, int(playback_ms))
            replaced = True
            continue
        out += _encode_proto_field(field)
    if not replaced:
        out += field_varint(28, int(playback_ms))
    return bytes(out)


def _retarget_client_state_hdr_authorization(client_state: bytes) -> bytes:
    try:
        fields = list(iter_proto_fields(client_state))
    except Exception:
        return client_state
    if not fields:
        return client_state
    out = bytearray()
    replaced = False
    for field in fields:
        if field.number == 79 and field.wire_type == WIRE_BYTES:
            if not replaced:
                out += field_bytes(79, build_authorized_formats(include_hdr=True))
                replaced = True
            continue
        try:
            out += _encode_proto_field(field)
        except Exception:
            return client_state
    if not replaced:
        out += field_bytes(79, build_authorized_formats(include_hdr=True))
    return bytes(out)


def _is_hdr10_sabr_video_itag(itag: int | None) -> bool:
    return bool(itag and int(itag) in HDR10_SABR_VIDEO_ITAGS)


def _retarget_embedded_request_body(
    body: bytes,
    video_format: FormatId,
    *,
    audio_format: FormatId | None = None,
    audio_formats: Iterable[FormatId | int] = (),
    segment_requests: Iterable[SabrSegmentRequest | bytes] = (),
    include_selected_itags: bool = False,
    replace_video_format: bool = True,
    selected_video_itag: int | None = None,
    selected_audio_itag: int | None = None,
    client_state_playback_ms: int | None = None,
) -> bytes:
    """Reuse a captured TV request body while asking SABR for the selected video itag."""

    needs_hdr_authorization = replace_video_format and _is_hdr10_sabr_video_itag(video_format.itag)
    try:
        fields = list(iter_proto_fields(body))
    except Exception:
        return body
    segment_records = [
        request if isinstance(request, bytes) else build_sabr_segment_request(request)
        for request in segment_requests
    ]
    audio_format_records = [field_bytes(16, build_format_id(fmt, include_defaults=True)) for fmt in audio_formats]
    has_format_field = any(field.number == 17 for field in fields)
    has_audio_format_field = any(field.number == 16 for field in fields)
    if not fields or (
        not (has_format_field and replace_video_format)
        and not has_audio_format_field
        and not audio_format_records
        and not segment_records
        and not include_selected_itags
        and not needs_hdr_authorization
    ):
        return body
    replacement = field_bytes(17, build_format_id(video_format, include_defaults=True))
    out = bytearray()
    inserted_audio_formats = False
    inserted_format = False
    inserted_segments = False
    inserted_player_time = False

    def maybe_insert_audio_formats() -> None:
        nonlocal inserted_audio_formats
        if inserted_audio_formats:
            return
        for record in audio_format_records:
            out.extend(record)
        inserted_audio_formats = True

    def maybe_insert_segments() -> None:
        nonlocal inserted_segments
        if inserted_segments:
            return
        for record in segment_records:
            out.extend(field_bytes(3, record))
        inserted_segments = True

    def maybe_insert_player_time() -> None:
        nonlocal inserted_player_time
        if inserted_player_time or client_state_playback_ms is None:
            return
        out.extend(field_varint(4, int(client_state_playback_ms)))
        inserted_player_time = True

    for field in fields:
        if segment_records and not inserted_segments and field.number > 1:
            maybe_insert_segments()
        if not inserted_player_time and client_state_playback_ms is not None and field.number > 4:
            maybe_insert_player_time()
        if segment_records and field.number == 3:
            continue
        if field.number == 4 and client_state_playback_ms is not None:
            if not inserted_player_time:
                maybe_insert_player_time()
            continue
        if audio_format_records and field.number == 16:
            if not inserted_audio_formats:
                maybe_insert_audio_formats()
            continue
        if field.number == 17:
            if not replace_video_format:
                out += _encode_proto_field(field)
                continue
            if not inserted_format:
                out += replacement
                inserted_format = True
            continue
        if field.number in {22, 23}:
            continue
        try:
            if field.number == 1 and isinstance(field.value, bytes):
                client_state = field.value
                if client_state_playback_ms is not None:
                    client_state = _retarget_client_state_playback(client_state, client_state_playback_ms)
                if needs_hdr_authorization:
                    client_state = _retarget_client_state_hdr_authorization(client_state)
                out += field_bytes(1, client_state)
                continue
            out += _encode_proto_field(field)
        except Exception:
            return body
    maybe_insert_segments()
    if audio_format_records and not inserted_audio_formats:
        maybe_insert_audio_formats()
    maybe_insert_player_time()
    if replace_video_format and not inserted_format:
        out += replacement
    if include_selected_itags:
        if selected_video_itag is None:
            selected_video_itag = video_format.itag
        if selected_audio_itag is None and audio_format:
            selected_audio_itag = audio_format.itag
        if selected_video_itag:
            out += field_varint(22, int(selected_video_itag))
        if selected_audio_itag:
            out += field_varint(23, int(selected_audio_itag))
    return bytes(out)


def _client_info(extra: dict[str, Any]) -> ClientInfo | None:
    raw = extra.get("sabr_client")
    if not isinstance(raw, dict):
        return None
    client_number = _int_or_none(raw.get("client_number") or raw.get("client_name"))
    client_version = str(raw.get("client_version") or "").strip()
    if not client_number or not client_version:
        return None
    return ClientInfo(client_name=client_number, client_version=client_version)


def build_sabr_request(
    *,
    video_playback_ustreamer_config: bytes,
    video_formats: Iterable[FormatId | int],
    audio_formats: Iterable[FormatId | int] = (),
    segment_requests: Iterable[SabrSegmentRequest | bytes] = (),
    client_info: ClientInfo | None = None,
    client_state: bytes | None = None,
    player_time_ms: int | None = None,
    po_token: bytes | str | None = None,
    playback_cookie: bytes | str | None = None,
    fk: bytes | str | None = None,
    selected_video_itag: int | None = None,
    selected_audio_itag: int | None = None,
    max_height: int = 2160,
) -> bytes:
    out = bytearray()
    out += field_bytes(1, client_state or build_default_client_state(max_height=max_height))
    for request in segment_requests:
        segment_record = request if isinstance(request, bytes) else build_sabr_segment_request(request)
        out += field_bytes(3, segment_record)
    if player_time_ms is not None:
        out += field_varint(4, int(player_time_ms))
    out += field_bytes(5, video_playback_ustreamer_config)
    for fmt in audio_formats:
        out += field_bytes(16, build_format_id(fmt, include_defaults=True))
    for fmt in video_formats:
        out += field_bytes(17, build_format_id(fmt, include_defaults=True))
    if client_info:
        out += field_bytes(19, build_w9(client_info, po_token=po_token, playback_cookie=playback_cookie, fk=fk))
    if selected_video_itag:
        out += field_varint(22, int(selected_video_itag))
    if selected_audio_itag:
        out += field_varint(23, int(selected_audio_itag))
    return bytes(out)


def build_default_client_state(max_height: int = 2160) -> bytes:
    max_height = max(144, int(max_height or 2160))
    out = bytearray()
    out += field_varint(18, 2220)
    out += field_varint(19, 1248)
    out += field_varint(21, 0)
    out += field_varint(23, 2_233_380)
    out += field_varint(28, 9_007_199_254_740_991)
    out += field_varint(29, 0)
    out += field_varint(34, 0)
    out += field_varint(36, 0)
    out += field_varint(39, 749)
    out += field_varint(57, 445)
    out += field_bool(58, False)
    out += field_varint(59, max_height)
    out += field_varint(68, 1_209)
    out += field_bool(71, True)
    out += field_bytes(72, build_playback_policy_state(max_height=max_height))
    out += field_bytes(79, build_authorized_formats())
    out += field_varint(80, 1)
    return bytes(out)


def build_video_format_capability(
    video_codec: int,
    *,
    max_width: int,
    max_height: int,
    max_framerate: int = 60,
    max_bitrate_bps: int = 0,
    efficient: bool = True,
    is_10_bit_supported: bool = False,
) -> bytes:
    out = bytearray()
    out += field_varint(1, int(video_codec))
    out += field_bool(2, efficient)
    out += field_varint(3, int(max_height))
    out += field_varint(4, int(max_width))
    out += field_varint(11, int(max_framerate))
    if max_bitrate_bps:
        out += field_varint(12, int(max_bitrate_bps))
    out += field_bool(15, is_10_bit_supported)
    return bytes(out)


def build_audio_format_capability(
    audio_codec: int,
    *,
    num_channels: int,
    max_bitrate_bps: int = 0,
    spatial_capability_bitmask: int = 0,
) -> bytes:
    out = bytearray()
    out += field_varint(1, int(audio_codec))
    out += field_varint(2, int(num_channels))
    if max_bitrate_bps:
        out += field_varint(3, int(max_bitrate_bps))
    if spatial_capability_bitmask:
        out += field_varint(6, int(spatial_capability_bitmask))
    return bytes(out)


def build_media_capabilities(*, max_width: int = 3840, max_height: int = 2160, max_framerate: int = 60) -> bytes:
    h264_width = min(max_width, 1920)
    h264_height = min(max_height, 1080)
    out = bytearray()
    for cap in (
        build_video_format_capability(4, max_width=max_width, max_height=max_height, max_framerate=max_framerate, max_bitrate_bps=35_000_000),
        build_video_format_capability(2, max_width=h264_width, max_height=h264_height, max_framerate=max_framerate, max_bitrate_bps=18_000_000),
        build_video_format_capability(8, max_width=max_width, max_height=max_height, max_framerate=max_framerate, max_bitrate_bps=25_000_000, is_10_bit_supported=True),
    ):
        out += field_bytes(1, cap)
    for cap in (
        build_audio_format_capability(1, num_channels=6, max_bitrate_bps=512_000),
        build_audio_format_capability(3, num_channels=2, max_bitrate_bps=256_000),
        build_audio_format_capability(7, num_channels=6, max_bitrate_bps=640_000, spatial_capability_bitmask=1),
        build_audio_format_capability(5, num_channels=6, max_bitrate_bps=768_000, spatial_capability_bitmask=1),
    ):
        out += field_bytes(2, cap)
    out += field_varint(5, 3)
    return bytes(out)


def build_playback_policy_state(max_height: int = 2160) -> bytes:
    out = bytearray()
    out += field_varint(1, 0)
    out += field_varint(2, int(max_height))
    out += field_varint(3, 0)
    out += field_varint(4, 0)
    out += field_varint(5, int(max_height))
    out += field_varint(6, 0)
    return bytes(out)


def build_authorized_formats(track_types: Iterable[int] = (1, 2, 3, 4, 5), include_hdr: bool = True) -> bytes:
    out = bytearray()
    for track_type in track_types:
        hdr_values = (0, 1) if include_hdr and track_type != 1 else (0,)
        for is_hdr in hdr_values:
            out += field_bytes(1, field_varint(1, int(track_type)) + field_bool(2, bool(is_hdr)))
    return bytes(out)


def build_w9(
    client_info: ClientInfo,
    *,
    po_token: bytes | str | None = None,
    playback_cookie: bytes | str | None = None,
    fk: bytes | str | None = None,
) -> bytes:
    out = bytearray()
    out += field_bytes(1, build_client_info(client_info))
    po_token_bytes = decode_po_token_bytes(po_token)
    if po_token_bytes:
        out += field_bytes(2, po_token_bytes)
    if playback_cookie:
        out += field_bytes(3, playback_cookie)
    if fk:
        out += field_bytes(4, fk)
    return bytes(out)


def build_client_info(info: ClientInfo) -> bytes:
    out = bytearray()
    if info.hl:
        out += field_bytes(1, info.hl)
    if info.device_make:
        out += field_bytes(12, info.device_make)
    if info.device_model:
        out += field_bytes(13, info.device_model)
    out += field_varint(16, int(info.client_name))
    if info.client_version:
        out += field_bytes(17, info.client_version)
    if info.os_name:
        out += field_bytes(18, info.os_name)
    if info.os_version:
        out += field_bytes(19, info.os_version)
    return bytes(out)


def build_format_id(fmt: FormatId | int, *, include_defaults: bool = False) -> bytes:
    if isinstance(fmt, FormatId):
        itag, lmt, xtags = fmt.itag, fmt.lmt, fmt.xtags
    else:
        itag, lmt, xtags = int(fmt), 0, ""
    out = bytearray()
    out += field_varint(1, int(itag))
    if lmt or include_defaults:
        out += field_varint(2, int(lmt))
    if xtags or include_defaults:
        out += field_bytes(3, xtags)
    return bytes(out)


def parse_format_id(payload: bytes) -> FormatId:
    fields = list(iter_proto_fields(payload))
    by_num = {field.number: field for field in fields}
    xtags_b = _field_bytes(by_num, 3) or b""
    return FormatId(
        itag=_field_varint(by_num, 1) or 0,
        lmt=_field_varint(by_num, 2) or 0,
        xtags=xtags_b.decode("utf-8", "ignore"),
    )


def build_time_range(time_range: TimeRange) -> bytes:
    out = bytearray()
    if time_range.start_ticks is not None:
        out += field_varint(1, int(time_range.start_ticks))
    if time_range.duration_ticks is not None:
        out += field_varint(2, int(time_range.duration_ticks))
    if time_range.timescale is not None:
        out += field_varint(3, int(time_range.timescale))
    return bytes(out)


def build_sabr_segment_request(request: SabrSegmentRequest) -> bytes:
    out = bytearray()
    out += field_bytes(1, build_format_id(request.format_id, include_defaults=True))
    if request.start_time_ms is not None:
        out += field_varint(2, int(request.start_time_ms))
    if request.duration_ms is not None:
        out += field_varint(3, int(request.duration_ms))
    out += field_varint(4, int(request.first_sequence))
    out += field_varint(5, int(request.last_sequence if request.last_sequence is not None else request.first_sequence))
    if request.android_time_range:
        out += field_bytes(6, build_time_range(request.android_time_range))
    if request.time_range:
        out += field_bytes(11, build_time_range(request.time_range))
    if request.secondary_time_range:
        out += field_bytes(12, build_time_range(request.secondary_time_range))
    return bytes(out)


def _write_selected_media_from_ump_response(
    response,
    output_path: Path,
    *,
    target_itag: int,
    progress: Callable[[int, int, int], None] | None = None,
    base_url: str = "",
) -> SabrUmpDownloadStats:
    headers_by_id: dict[int, MediaHeader] = {}
    seen_itags: set[int] = set()
    selected_headers: set[int] = set()
    ended_headers: set[int] = set()
    media_bytes = 0
    chunks = 0
    header_count = 0
    redirect_url = ""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as output:
        try:
            parts = iter_ump_parts_from_reader(response)
            for part in parts:
                if part.part_type == SABR_REDIRECT_PART_TYPE:
                    redirect_url = _sabr_redirect_url_from_payload(part.payload, base_url=base_url) or redirect_url
                    continue
                if part.part_type == 20:
                    header = parse_media_header(part.payload)
                    header_count += 1
                    if header.header_id is None:
                        continue
                    headers_by_id[header.header_id] = header
                    if header.format_id.itag:
                        seen_itags.add(header.format_id.itag)
                    if header.format_id.itag == target_itag:
                        selected_headers.add(header.header_id)
                    continue
                if part.part_type == 21 and part.payload:
                    header_id = part.payload[0]
                    header = headers_by_id.get(header_id)
                    if header and header.format_id.itag:
                        seen_itags.add(header.format_id.itag)
                    if header_id not in selected_headers:
                        continue
                    payload = part.payload[1:]
                    output.write(payload)
                    media_bytes += len(payload)
                    chunks += 1
                    if progress:
                        progress(chunks, 0, media_bytes)
                    continue
                if part.part_type == 22 and part.payload:
                    ended_headers.add(part.payload[0])
        except (OSError, http.client.HTTPException) as exc:
            raise HttpClientError(str(exc) or exc.__class__.__name__) from exc
    ended = bool(selected_headers) and selected_headers.issubset(ended_headers)
    return SabrUmpDownloadStats(
        media_bytes=media_bytes,
        chunks=chunks,
        headers=header_count,
        ended=ended,
        seen_itags=tuple(sorted(seen_itags)),
        redirect_url=redirect_url,
        completed_segments=chunks,
        total_segments=0,
    )


def fetch_sabr_ump_live_segments(
    stream: StreamInfo,
    *,
    first_sequence: int | None = None,
    take_count: int = 1,
    headers: dict[str, str] | None = None,
    request_timeout: int = 30,
) -> list[SegmentInfo]:
    segment_requests: tuple[SabrSegmentRequest, ...] = ()
    if first_sequence is not None and first_sequence >= 0:
        count = max(1, int(take_count or 1))
        video_itag = _int_or_none((stream.extra or {}).get("sabr_itag") or stream.id)
        if not video_itag:
            raise SabrUmpError("SABR/UMP stream is missing selected live video itag.")
        segment_requests = (
            SabrSegmentRequest(
                format_id=FormatId(
                    itag=video_itag,
                    lmt=_int_or_none((stream.extra or {}).get("sabr_lmt")) or 0,
                    xtags=str((stream.extra or {}).get("sabr_xtags") or ""),
                ),
                first_sequence=int(first_sequence),
                last_sequence=int(first_sequence) + count - 1,
            ),
        )
    request = _build_stream_request(
        stream,
        segment_requests=segment_requests,
        include_selected_itags=bool(segment_requests),
    )
    request_headers = _sabr_request_headers(_merged_sabr_headers(stream, headers))
    try:
        segments, _stats = _post_sabr_segments(
            request,
            stream,
            headers=request_headers,
            request_timeout=request_timeout,
        )
        return segments
    except HttpClientError as exc:
        raise SabrUmpError(_http_error_message(exc, request)) from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise SabrUmpError(str(exc) or exc.__class__.__name__) from exc


def _post_sabr_segments(
    request: _SabrStreamRequest,
    stream: StreamInfo,
    *,
    headers: dict[str, str],
    request_timeout: int,
    retries: int = 3,
) -> tuple[list[SegmentInfo], SabrUmpDownloadStats]:
    attempts = _sabr_retry_loop_attempts(retries)
    last_error: BaseException | None = None
    for attempt in range(attempts):
        current_url = request.url
        redirect_chain: list[str] = []
        try:
            for _redirect_attempt in range(4):
                with get_global_http_client().request(
                    "POST",
                    current_url,
                    headers=headers,
                    timeout=request_timeout,
                    body=request.body,
                ) as response:
                    segments, stats = _segments_from_ump_response(
                        response,
                        stream,
                        target_itag=request.target_format.itag,
                        base_url=current_url,
                    )
                if segments or not stats.redirect_url:
                    return segments, stats
                current_url = urljoin(current_url, stats.redirect_url)
                if current_url in redirect_chain:
                    raise SabrUmpError("SABR/UMP redirect loop while following part 43.")
                redirect_chain.append(current_url)
            raise SabrUmpError("SABR/UMP redirect chain was too long.")
        except (HttpClientError, OSError, http.client.HTTPException) as exc:
            last_error = exc
            if not _should_retry_sabr_error(exc, attempt=attempt, retries=retries):
                raise
            _runtime_sleep(_sabr_retry_delay(attempt))
    if last_error is not None:
        raise last_error
    raise SabrUmpError("SABR/UMP request failed.")


def _post_sabr_key_probe_bytes(
    request: _SabrStreamRequest,
    *,
    headers: dict[str, str],
    request_timeout: int,
    retries: int,
    max_bytes: int,
) -> bytes:
    attempts = _sabr_retry_loop_attempts(retries)
    last_error: BaseException | None = None
    for attempt in range(attempts):
        current_url = request.url
        redirect_chain: list[str] = []
        try:
            for _redirect_attempt in range(4):
                with get_global_http_client().request(
                    "POST",
                    current_url,
                    headers=headers,
                    timeout=request_timeout,
                    body=request.body,
                ) as response:
                    data, redirect_url = _selected_media_probe_bytes_from_ump_response(
                        response,
                        target_itag=request.target_format.itag,
                        max_bytes=max_bytes,
                        base_url=current_url,
                    )
                if data or not redirect_url:
                    return data
                current_url = urljoin(current_url, redirect_url)
                if current_url in redirect_chain:
                    raise SabrUmpError("SABR/UMP redirect loop while following part 43.")
                redirect_chain.append(current_url)
            raise SabrUmpError("SABR/UMP redirect chain was too long.")
        except (HttpClientError, OSError, http.client.HTTPException) as exc:
            last_error = exc
            if not _should_retry_sabr_error(exc, attempt=attempt, retries=retries):
                raise
            _runtime_sleep(_sabr_retry_delay(attempt))
    if last_error is not None:
        raise last_error
    return b""


def _selected_media_probe_bytes_from_ump_response(
    reader,
    *,
    target_itag: int,
    max_bytes: int,
    base_url: str = "",
) -> tuple[bytes, str]:
    selected_headers: set[int] = set()
    collected = bytearray()
    redirect_url = ""
    max_bytes = max(1, int(max_bytes))
    while True:
        part_type = _read_ump_int_from_reader(reader)
        if part_type is None:
            return bytes(collected), redirect_url
        size = _read_ump_int_from_reader(reader)
        if size is None:
            raise ValueError("truncated SABR/UMP response")
        if size < 0:
            raise ValueError("invalid SABR/UMP part size")

        if part_type == SABR_REDIRECT_PART_TYPE:
            payload = _read_exact_from_reader(reader, size)
            redirect_url = _sabr_redirect_url_from_payload(payload, base_url=base_url) or redirect_url
            continue

        if part_type == 20:
            payload = _read_exact_from_reader(reader, size)
            header = parse_media_header(payload)
            if header.header_id is None:
                continue
            if header.format_id.itag == target_itag:
                selected_headers.add(header.header_id)
            continue

        if part_type != 21:
            _discard_reader_bytes(reader, size)
            continue

        if size <= 0:
            continue
        header_id = _read_exact_from_reader(reader, 1)[0]
        remaining = size - 1
        if header_id not in selected_headers:
            _discard_reader_bytes(reader, remaining)
            continue
        take = min(remaining, max_bytes - len(collected))
        if take > 0:
            collected.extend(_read_exact_from_reader(reader, take))
            remaining -= take
        if len(collected) >= max_bytes or collected:
            return bytes(collected), redirect_url
        _discard_reader_bytes(reader, remaining)


@dataclass(slots=True)
class _SelectedMedia:
    header: MediaHeader
    data: bytearray


def _segments_from_ump_response(
    response,
    stream: StreamInfo,
    *,
    target_itag: int,
    base_url: str = "",
) -> tuple[list[SegmentInfo], SabrUmpDownloadStats]:
    headers_by_id: dict[int, MediaHeader] = {}
    selected: list[_SelectedMedia] = []
    selected_by_header_id: dict[int, _SelectedMedia] = {}
    selected_headers: set[int] = set()
    ended_headers: set[int] = set()
    seen_itags: set[int] = set()
    redirect_url = ""
    chunks = 0
    media_bytes = 0
    header_count = 0
    try:
        parts = iter_ump_parts_from_reader(response)
        for part in parts:
            if part.part_type == SABR_REDIRECT_PART_TYPE:
                redirect_url = _sabr_redirect_url_from_payload(part.payload, base_url=base_url) or redirect_url
                continue
            if part.part_type == 20:
                header = parse_media_header(part.payload)
                header_count += 1
                if header.header_id is None:
                    continue
                headers_by_id[header.header_id] = header
                if header.format_id.itag:
                    seen_itags.add(header.format_id.itag)
                if header.format_id.itag == target_itag:
                    selected_headers.add(header.header_id)
                    item = _SelectedMedia(header=header, data=bytearray())
                    selected.append(item)
                    selected_by_header_id[header.header_id] = item
                continue
            if part.part_type == 21 and part.payload:
                header_id = part.payload[0]
                header = headers_by_id.get(header_id)
                if header and header.format_id.itag:
                    seen_itags.add(header.format_id.itag)
                if header_id not in selected_headers:
                    continue
                payload = part.payload[1:]
                if not payload:
                    continue
                item = selected_by_header_id.get(header_id)
                if item is None:
                    item = _SelectedMedia(header=header or MediaHeader(header_id=header_id, format_id=FormatId(target_itag)), data=bytearray())
                    selected.append(item)
                    selected_by_header_id[header_id] = item
                item.data.extend(payload)
                chunks += 1
                media_bytes += len(payload)
                continue
            if part.part_type == 22 and part.payload:
                ended_headers.add(part.payload[0])
    except (OSError, http.client.HTTPException) as exc:
        raise HttpClientError(str(exc) or exc.__class__.__name__) from exc

    segments = [
        segment
        for segment in (_live_segment_from_selected_media(stream, media) for media in selected)
        if segment is not None
    ]
    segments.sort(key=lambda segment: (segment.index != -1, segment.index if segment.index is not None else 0, segment.url))
    _fill_sabr_live_segment_durations(segments)
    stats = SabrUmpDownloadStats(
        media_bytes=media_bytes,
        chunks=chunks,
        headers=header_count,
        ended=bool(selected_headers) and selected_headers.issubset(ended_headers),
        seen_itags=tuple(sorted(seen_itags)),
        redirect_url=redirect_url,
    )
    return segments, stats


def _live_segment_from_selected_media(stream: StreamInfo, media: _SelectedMedia) -> SegmentInfo | None:
    if not media.data:
        return None
    header = media.header
    index = -1 if header.is_init else header.sequence_number
    if index is None:
        index = header.header_id
    if index is None:
        return None
    time_range = header.time_range
    duration = None if header.is_init else _duration_seconds_from_header(header)
    timeline_time = time_range.start_ticks if time_range and time_range.start_ticks is not None else None
    timeline_presentation_time = None
    if header.start_ms is not None:
        timeline_presentation_time = float(header.start_ms) / 1000.0
    return SegmentInfo(
        url=f"sabr://{header.format_id.itag}/{index}",
        duration=duration,
        index=index,
        data=bytes(media.data),
        encrypted=stream.encrypted,
        encryption_scheme=stream.encryption_scheme,
        key_id=_stream_primary_key_id(stream),
        timeline_time=timeline_time,
        timeline_presentation_time=timeline_presentation_time,
    )


def _duration_seconds_from_header(header: MediaHeader) -> float | None:
    if header.duration_ms is not None:
        return max(0.0, float(header.duration_ms) / 1000.0)
    time_range = header.time_range
    if time_range and time_range.duration_ticks is not None and time_range.timescale:
        return max(0.0, float(time_range.duration_ticks) / max(1, time_range.timescale))
    return None


def _fill_sabr_live_segment_durations(segments: list[SegmentInfo]) -> None:
    media = [segment for segment in segments if segment.index is not None and segment.index >= 0]
    if not media:
        return
    by_index = {int(segment.index): segment for segment in media}
    for segment in media:
        if segment.duration and segment.duration > 0:
            continue
        duration = _sabr_live_duration_from_neighbor(segment, by_index)
        segment.duration = duration if duration and duration > 0 else SABR_LIVE_DEFAULT_SEGMENT_SECONDS


def _sabr_live_duration_from_neighbor(segment: SegmentInfo, by_index: dict[int, SegmentInfo]) -> float | None:
    if segment.index is None or segment.timeline_presentation_time is None:
        return None
    next_segment = by_index.get(int(segment.index) + 1)
    if next_segment is None or next_segment.timeline_presentation_time is None:
        return None
    try:
        delta = float(next_segment.timeline_presentation_time) - float(segment.timeline_presentation_time)
    except (TypeError, ValueError):
        return None
    return delta if 0.05 <= delta <= 60.0 else None


def _stream_primary_key_id(stream: StreamInfo) -> str | None:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    value = extra.get("key_id")
    if isinstance(value, str) and value:
        return value
    key_ids = extra.get("key_ids")
    if isinstance(key_ids, list):
        for key_id in key_ids:
            if isinstance(key_id, str) and key_id:
                return key_id
    return None


def _sabr_redirect_url_from_payload(payload: bytes, *, base_url: str = "") -> str:
    candidates: list[str] = []

    def collect_from_bytes(value: bytes, depth: int = 0) -> None:
        if not value or depth > 3:
            return
        for match in _URL_RE.finditer(value):
            raw = match.group(0).rstrip(b".,);]")
            text = raw.decode("utf-8", "ignore").strip()
            if text:
                candidates.append(text)
        try:
            fields = list(iter_proto_fields(value))
        except Exception:
            return
        for field in fields:
            if field.wire_type == WIRE_BYTES and isinstance(field.value, bytes):
                collect_from_bytes(field.value, depth + 1)

    collect_from_bytes(payload)
    if not candidates:
        return ""
    return urljoin(base_url, candidates[0]) if base_url else candidates[0]


def iter_ump_parts_from_reader(reader, *, chunk_size: int = 1024 * 1024) -> Iterator[UmpPart]:
    buffer = bytearray()
    eof = False
    while True:
        part = _pop_ump_part(buffer)
        if part is not None:
            yield part
            continue
        if eof:
            if buffer:
                raise ValueError("truncated SABR/UMP response")
            return
        chunk = reader.read(chunk_size)
        if chunk:
            buffer.extend(chunk)
        else:
            eof = True


def _read_ump_int_from_reader(reader) -> int | None:
    first = reader.read(1)
    if not first:
        return None
    first_value = first[0]
    if first_value < 128:
        return first_value
    if first_value < 192:
        tail = _read_exact_from_reader(reader, 1)
        return (first_value & 63) + 64 * tail[0]
    if first_value < 224:
        tail = _read_exact_from_reader(reader, 2)
        return (first_value & 31) + 32 * (tail[0] + 256 * tail[1])
    if first_value < 240:
        tail = _read_exact_from_reader(reader, 3)
        return (first_value & 15) + 16 * (tail[0] + 256 * (tail[1] + 256 * tail[2]))
    tail = _read_exact_from_reader(reader, 4)
    return int.from_bytes(tail, "little")


def _read_exact_from_reader(reader, size: int) -> bytes:
    if size <= 0:
        return b""
    chunks: list[bytes] = []
    remaining = int(size)
    while remaining > 0:
        chunk = reader.read(remaining)
        if not chunk:
            raise ValueError("truncated SABR/UMP response")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _discard_reader_bytes(reader, size: int, *, chunk_size: int = 64 * 1024) -> None:
    remaining = int(size)
    while remaining > 0:
        chunk = reader.read(min(remaining, chunk_size))
        if not chunk:
            raise ValueError("truncated SABR/UMP response")
        remaining -= len(chunk)


def encode_ump_part(part_type: int, payload: bytes) -> bytes:
    return write_ump_int(part_type) + write_ump_int(len(payload)) + payload


def write_ump_int(value: int) -> bytes:
    if value < 0:
        raise ValueError("UMP integer cannot be negative")
    if value < 128:
        return bytes([value])
    if value < 4096:
        return bytes([0x80 | (value & 63), value >> 6])
    if value < 1 << 21:
        return bytes([0xC0 | (value & 31), (value >> 5) & 0xFF, (value >> 13) & 0xFF])
    if value < 1 << 28:
        return bytes([0xE0 | (value & 15), (value >> 4) & 0xFF, (value >> 12) & 0xFF, (value >> 20) & 0xFF])
    if value < 1 << 32:
        return b"\xF0" + value.to_bytes(4, "little")
    raise ValueError("UMP integer is too large")


def _pop_ump_part(buffer: bytearray) -> UmpPart | None:
    parsed_type = _try_read_ump_int(buffer, 0)
    if parsed_type is None:
        return None
    part_type, pos = parsed_type
    parsed_size = _try_read_ump_int(buffer, pos)
    if parsed_size is None:
        return None
    size, pos = parsed_size
    end = pos + size
    if len(buffer) < end:
        return None
    payload = bytes(buffer[pos:end])
    del buffer[:end]
    return UmpPart(part_type=part_type, payload=payload)


def _try_read_ump_int(buf: bytearray, pos: int = 0) -> tuple[int, int] | None:
    if pos >= len(buf):
        return None
    first = buf[pos]
    if first < 128:
        return first, pos + 1
    if first < 192:
        if pos + 2 > len(buf):
            return None
        return (first & 63) + 64 * buf[pos + 1], pos + 2
    if first < 224:
        if pos + 3 > len(buf):
            return None
        return (first & 31) + 32 * (buf[pos + 1] + 256 * buf[pos + 2]), pos + 3
    if first < 240:
        if pos + 4 > len(buf):
            return None
        return (first & 15) + 16 * (buf[pos + 1] + 256 * (buf[pos + 2] + 256 * buf[pos + 3])), pos + 4
    if pos + 5 > len(buf):
        return None
    return int.from_bytes(buf[pos + 1 : pos + 5], "little"), pos + 5


def read_varint(buf: bytes, pos: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 70:
            raise ValueError("protobuf varint is too long")
    raise ValueError("truncated protobuf varint")


def encode_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(out)


def field_varint(number: int, value: int) -> bytes:
    return encode_varint((number << 3) | WIRE_VARINT) + encode_varint(value)


def field_bool(number: int, value: bool) -> bytes:
    return field_varint(number, 1 if value else 0)


def field_bytes(number: int, value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, str):
        value = value.encode("utf-8")
    return encode_varint((number << 3) | WIRE_BYTES) + encode_varint(len(value)) + value


def decode_po_token_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    token = value.strip()
    if not token:
        return b""
    raw = token.encode("ascii", "ignore")
    try:
        decoded = base64.urlsafe_b64decode(raw + b"=" * ((4 - len(raw) % 4) % 4))
        if len(decoded) >= 32 and decoded[:1] == b"2":
            return decoded
    except Exception:
        pass
    return token.encode("utf-8")


def iter_proto_fields(buf: bytes) -> Iterator[ProtoField]:
    pos = 0
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        number = key >> 3
        wire_type = key & 7
        if wire_type == WIRE_VARINT:
            value, pos = read_varint(buf, pos)
        elif wire_type == WIRE_BYTES:
            size, pos = read_varint(buf, pos)
            if pos + size > len(buf):
                raise ValueError("truncated protobuf bytes field")
            value = buf[pos : pos + size]
            pos += size
        elif wire_type == WIRE_32BIT:
            if pos + 4 > len(buf):
                raise ValueError("truncated protobuf fixed32 field")
            value = buf[pos : pos + 4]
            pos += 4
        elif wire_type == WIRE_64BIT:
            if pos + 8 > len(buf):
                raise ValueError("truncated protobuf fixed64 field")
            value = buf[pos : pos + 8]
            pos += 8
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        yield ProtoField(number=number, wire_type=wire_type, value=value)


def parse_media_header(payload: bytes) -> MediaHeader:
    fields = list(iter_proto_fields(payload))
    by_num = {field.number: field for field in fields}
    xtags_b = _field_bytes(by_num, 5) or b""
    time_range_b = _field_bytes(by_num, 15)
    return MediaHeader(
        header_id=_field_varint(by_num, 1),
        format_id=FormatId(
            itag=_field_varint(by_num, 3) or 0,
            lmt=_field_varint(by_num, 4) or 0,
            xtags=xtags_b.decode("utf-8", "ignore"),
        ),
        sequence_number=_field_varint(by_num, 9),
        start_ms=_field_varint(by_num, 11),
        duration_ms=_field_varint(by_num, 12),
        time_range=parse_time_range(time_range_b) if time_range_b else None,
        is_init=bool(_field_varint(by_num, 8) or 0),
    )


def parse_time_range(buf: bytes) -> TimeRange:
    fields = list(iter_proto_fields(buf))
    by_num = {field.number: field for field in fields}
    return TimeRange(
        start_ticks=_field_varint(by_num, 1),
        duration_ticks=_field_varint(by_num, 2),
        timescale=_field_varint(by_num, 3),
    )


def _field_varint(fields: dict[int, ProtoField], number: int) -> int | None:
    field = fields.get(number)
    if not field or field.wire_type != WIRE_VARINT:
        return None
    assert isinstance(field.value, int)
    return field.value


def _field_bytes(fields: dict[int, ProtoField], number: int) -> bytes | None:
    field = fields.get(number)
    if not field or field.wire_type != WIRE_BYTES:
        return None
    assert isinstance(field.value, bytes)
    return field.value


def _sabr_request_headers(headers: dict[str, str] | None) -> dict[str, str]:
    result = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/vnd.yt-ump",
        "Accept-Encoding": "identity",
    }
    result.update(headers or {})
    return result


def _merged_sabr_headers(stream: StreamInfo, overrides: dict[str, str] | None) -> dict[str, str]:
    merged: dict[str, str] = {}
    for key, value in (overrides or {}).items():
        key_text = str(key or "").strip()
        if not key_text or key_text.lower() in {"host", "connection", "content-length"}:
            continue
        merged[key_text] = str(value)
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    embedded = extra.get("sabr_request_headers")
    if isinstance(embedded, dict):
        for key, value in embedded.items():
            key_text = str(key or "").strip()
            if not key_text or key_text.lower() in {"host", "connection", "content-length"}:
                continue
            merged[key_text] = str(value)
    return merged


def _http_error_message(exc: HttpClientError, request: _SabrStreamRequest | None = None) -> str:
    text = str(exc) or exc.__class__.__name__
    if "HTTP Error 403" in text or "Forbidden" in text:
        if request and request.po_token_present:
            source = f" ({request.po_token_source})" if request.po_token_source else ""
            status = f", status={request.po_token_status}" if request.po_token_status else ""
            return (
                f"{text}. SABR/UMP PO token was sent{source}{status}, but the server still rejected the request. "
                "Regenerate the JSON/PO token in the same network session, or try a session/living-room PO token binding."
            )
        return (
            f"{text}. SABR/UMP high-rendition requests usually require the TV player's content/session PO token; "
            "pass --sabr-po-token or --sabr-po-token-file once that token is available."
        )
    return text


def _b64url_decode(value: str) -> bytes:
    value = value.strip()
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def _b64_any_decode(value: str) -> bytes:
    value = value.strip()
    if not value:
        return b""
    padded = value + "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except Exception:
        return base64.urlsafe_b64decode(padded)


def _height(stream: StreamInfo) -> int | None:
    if not stream.resolution or "x" not in stream.resolution:
        return None
    return _int_or_none(stream.resolution.rsplit("x", 1)[-1])


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
