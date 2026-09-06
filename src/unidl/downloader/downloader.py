from __future__ import annotations

import base64
import hashlib
import http.client
import json
import math
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote_to_bytes, urlencode, urljoin, urlparse, urlunparse

from .bbts import bbts_part_is_complete
from .embedding import (
    DownloadCancelled,
    current_download_runtime,
    managed_popen,
    managed_run,
)
from .http_client import HttpClientError, get_global_http_client, http2_download_to_file, httpx_http2_available
from .live_rules import (
    is_yangshipin_catchup_cdn_url,
    should_prefer_yangshipin_catchup_curl,
    yangshipin_catchup_parallelism,
)
from .loader import DEFAULT_USER_AGENT
from .models import SegmentInfo, StreamInfo
from .postprocess import split_fragmented_mp4_init_media
from .qobuz import decrypt_qobuz_file, decrypt_qobuz_segment
from .sabr_ump import (
    SabrUmpError,
    download_sabr_ump_dvr_stream,
    download_sabr_ump_stream,
    probe_sabr_ump_dvr_first_media_sequence,
    probe_sabr_ump_dvr_sequence_window,
)
from .utils import is_url, source_path, unique_path
from .youku import YoukuTsError, decrypt_youku_segment

KNOWN_OUTPUT_SUFFIXES = {
    ".aac",
    ".ac3",
    ".bbts",
    ".eac3",
    ".m4a",
    ".m4s",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".srt",
    ".ts",
    ".ttml",
    ".vtt",
    ".webm",
}

_APPLE_HLS_LOW_SPEED_LIMIT = 64 * 1024
_APPLE_HLS_LOW_SPEED_TIME = 12.0
_APPLE_HLS_LOW_SPEED_MIN_BYTES = 256 * 1024
_APPLE_HLS_READ_CHUNK_SIZE = 256 * 1024
_HEDGED_DOWNLOAD_FACTOR = 3.0
_HEDGED_DOWNLOAD_MIN_WAIT = 5.0
_YANGSHIPIN_CATCHUP_RESUME_ATTEMPTS = 64
_YANGSHIPIN_CATCHUP_STALLED_ATTEMPTS = 8
_YANGSHIPIN_CATCHUP_PROGRESS_RETRY_DELAY = 0.15

_VOLATILE_RESUME_QUERY_KEYS = {
    "accesskey",
    "access_key",
    "auth",
    "e",
    "ei",
    "exp",
    "expire",
    "expires",
    "hash",
    "hdnea",
    "hdntl",
    "hmac",
    "ip",
    "key-pair-id",
    "keypairid",
    "policy",
    "sig",
    "signature",
    "t",
    "token",
    "x-amz-algorithm",
    "x-amz-credential",
    "x-amz-date",
    "x-amz-expires",
    "x-amz-security-token",
    "x-amz-signature",
}


def _runtime_checkpoint() -> None:
    runtime = current_download_runtime()
    if runtime is not None:
        runtime.checkpoint()


def _runtime_sleep(seconds: float) -> None:
    runtime = current_download_runtime()
    if runtime is None:
        time.sleep(max(0.0, float(seconds)))
    else:
        if runtime.wait(seconds):
            runtime.checkpoint()


def _shutdown_pool(pool: ThreadPoolExecutor, *, cancel_futures: bool = True) -> None:
    """Join cancelled workers when running inside UniDL's embedded runtime.

    The standalone command historically returned immediately from its Ctrl+C
    path.  Embedded downloads, however, must not leave executor threads behind
    after the TUI has gone away.  Closing the runtime's HTTP clients first makes
    in-flight requests unwind, after which joining is bounded by their request
    timeout rather than silently leaking a worker.
    """
    pool.shutdown(
        wait=current_download_runtime() is not None,
        cancel_futures=cancel_futures,
    )


@dataclass(slots=True)
class DownloadResult:
    stream: StreamInfo
    path: Path
    temp_dir: Path | None = None
    sections: list[Path] | None = None
    parts: list[Path] | None = None


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    stream: StreamInfo
    completed_segments: int
    total_segments: int
    downloaded_bytes: int
    total_bytes: int | None
    elapsed_seconds: float
    done: bool = False


@dataclass(frozen=True, slots=True)
class HlsCrypto:
    method: str
    key: bytes | None = None
    iv: bytes | None = None
    decryptor: Any | None = None


@dataclass(frozen=True, slots=True)
class _JsonLiveProbeResult:
    ok: bool
    error: str | None = None
    netflix_geo_failed: bool = False


@dataclass(frozen=True, slots=True)
class _RangeProbeResult:
    supported: bool
    size: int | None = None
    url: str | None = None


_HLS_KEY_CACHE: dict[str, bytes] = {}
_HLS_KEY_LOCK = threading.Lock()
_ALLOW_INSECURE_LOCALHOST_HLS_KEYS = False
_DVR_SEQUENCE_WINDOW_CACHE: dict[tuple[str, str], tuple[int, int]] = {}
_DVR_SEQUENCE_WINDOW_LOCK = threading.Lock()
_HLS_SEGMENT_CIPHER_METHODS = {"AES_128", "AES_128_ECB", "CHACHA20", "YOUKU_ECB"}
_HLS_SAMPLE_ENCRYPTION_METHODS = {"CENC", "CBCS", "SAMPLE_AES", "SAMPLE_AES_CENC", "SAMPLE_AES_CTR", "BBTS"}
# Tencent TV HLS-ENC restarts the original 8-byte-nonce ChaCha20 state at each
# 1024-byte transport chunk. It is not one continuous stream over the whole
# HLS segment, so OpenSSL's one-shot ``enc -chacha20`` path cannot represent it.
_TENCENT_CHACHA20_CHUNK_SIZE = 1024
# Some CDN/S3 frontends reject large byte ranges even though they advertise
# Accept-Ranges. Keep generic single-file ranges below the common 10 MiB cap.
_LARGE_SINGLE_FILE_SPLIT_SIZE = 8 * 1024 * 1024
_LARGE_SINGLE_FILE_SPLIT_THRESHOLD = 2 * _LARGE_SINGLE_FILE_SPLIT_SIZE
_JSON_DIRECT_SINGLE_FILE_SPLIT_SIZE = 4 * 1024 * 1024
_STREAM_COPY_CHUNK_SIZE = 1024 * 1024


class DownloadError(RuntimeError):
    def __init__(self, message: str, url: str | None = None):
        super().__init__(message)
        self.url = url


def set_allow_insecure_localhost_hls_keys(enabled: bool) -> None:
    global _ALLOW_INSECURE_LOCALHOST_HLS_KEYS
    _ALLOW_INSECURE_LOCALHOST_HLS_KEYS = bool(enabled)


def _is_sabr_ump_stream(stream: StreamInfo) -> bool:
    return stream.manifest_type == "sabr_ump" or bool(stream.extra.get("sabr_ump"))


def _is_sabr_dvr_vod_stream(stream: StreamInfo) -> bool:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    return _is_sabr_ump_stream(stream) and bool(extra.get("sabr_dvr_as_vod"))


def _is_json_dvr_sequence_stream(stream: StreamInfo) -> bool:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    return stream.manifest_type == "json" and bool(extra.get("json_dvr_sequence"))


def prepare_dvr_sequence_windows(
    streams: list[StreamInfo],
    *,
    headers: dict[str, str] | None = None,
    request_timeout: int = 30,
    retries: int = 3,
    probe_network: bool = True,
) -> None:
    groups: dict[tuple[str, str], list[StreamInfo]] = {}
    for stream in streams:
        key = _dvr_sequence_group_key(stream)
        if key is None:
            continue
        groups.setdefault(key, []).append(stream)

    for group_streams in groups.values():
        if len(group_streams) <= 1:
            continue
        if probe_network:
            _align_sabr_dvr_group_first_media_sequence(
                group_streams,
                headers=headers,
                request_timeout=request_timeout,
                retries=retries,
            )
        if _dvr_group_has_mismatched_starts(group_streams):
            for stream in group_streams:
                extra = stream.extra if isinstance(stream.extra, dict) else {}
                start = _dvr_sequence_start(stream)
                end = _int_extra(extra, _dvr_sequence_end_key(stream))
                if start is None or end is not None or not probe_network:
                    continue
                try:
                    window = _probe_stream_dvr_sequence_window(
                        stream,
                        headers=headers,
                        request_timeout=request_timeout,
                        retries=retries,
                        remember=True,
                    )
                except (DownloadError, SabrUmpError):
                    continue
                if window is not None:
                    _set_dvr_sequence_window(stream, window[0], window[1])
            continue
        cache_key = _dvr_sequence_group_key(group_streams[0])
        if cache_key is not None:
            with _DVR_SEQUENCE_WINDOW_LOCK:
                cached = _DVR_SEQUENCE_WINDOW_CACHE.get(cache_key)
            if cached is not None:
                current_starts = [start for start in (_dvr_sequence_start(stream) for stream in group_streams) if start is not None]
                cached_start = max([cached[0], *current_starts]) if current_starts else cached[0]
                cached_end = cached[1]
                if cached_end < cached_start:
                    continue
                for stream in group_streams:
                    _set_dvr_sequence_window(stream, cached_start, cached_end)
                continue
        if probe_network and _can_probe_json_dvr_group_once(group_streams):
            representative = _json_dvr_group_probe_stream(group_streams)
            try:
                window = _probe_stream_dvr_sequence_window(
                    representative,
                    headers=headers,
                    request_timeout=request_timeout,
                    retries=retries,
                    remember=False,
                )
            except (DownloadError, SabrUmpError):
                window = None
            if window is not None:
                start, end = window
                if cache_key is not None:
                    with _DVR_SEQUENCE_WINDOW_LOCK:
                        _DVR_SEQUENCE_WINDOW_CACHE[cache_key] = (int(start), int(end))
                for stream in group_streams:
                    _set_dvr_sequence_window(stream, start, end)
                continue
        windows: list[tuple[StreamInfo, int, int]] = []
        missing: list[StreamInfo] = []
        for stream in group_streams:
            extra = stream.extra if isinstance(stream.extra, dict) else {}
            start = _dvr_sequence_start(stream)
            end = _int_extra(extra, _dvr_sequence_end_key(stream))
            if start is not None and end is not None and end >= start:
                windows.append((stream, start, end))
                continue
            if _is_sabr_dvr_vod_stream(stream):
                missing.append(stream)
                continue
            if not probe_network:
                missing.append(stream)
                continue
            try:
                window = _probe_stream_dvr_sequence_window(
                    stream,
                    headers=headers,
                    request_timeout=request_timeout,
                    retries=retries,
                    remember=False,
                )
            except (DownloadError, SabrUmpError):
                missing.append(stream)
                continue
            if window is None:
                missing.append(stream)
                continue
            windows.append((stream, window[0], window[1]))
        has_json_window = any(_is_json_dvr_sequence_stream(stream) for stream, _start, _end in windows)
        blocking_missing = [
            stream
            for stream in missing
            if not (has_json_window and _is_sabr_dvr_vod_stream(stream))
        ]
        if blocking_missing and probe_network:
            for stream in list(missing):
                try:
                    window = _probe_stream_dvr_sequence_window(
                        stream,
                        headers=headers,
                        request_timeout=request_timeout,
                        retries=retries,
                        remember=False,
                    )
                except (DownloadError, SabrUmpError):
                    continue
                if window is None:
                    continue
                windows.append((stream, window[0], window[1]))
                missing.remove(stream)
        blocking_missing = [
            stream
            for stream in missing
            if not (has_json_window and _is_sabr_dvr_vod_stream(stream))
        ]
        if blocking_missing or not windows:
            continue
        starts = [start for _stream, start, _end in windows]
        ends = [end for _stream, _start, end in windows]
        start = max(starts)
        end = min(ends)
        if end < start:
            continue
        if cache_key is not None:
            with _DVR_SEQUENCE_WINDOW_LOCK:
                _DVR_SEQUENCE_WINDOW_CACHE[cache_key] = (int(start), int(end))
        for stream in group_streams:
            _set_dvr_sequence_window(stream, start, end)


def _sabr_progress_total_hint(stream: StreamInfo) -> int:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    start = _int_extra(extra, "sabr_dvr_min_sequence")
    end = _int_extra(extra, "sabr_dvr_end_sequence")
    if start is not None and end is not None and end >= start:
        return end - start + 1
    return max(0, stream.segments_count)


def _can_probe_json_dvr_group_once(streams: list[StreamInfo]) -> bool:
    if len(streams) <= 1 or not all(_is_json_dvr_sequence_stream(stream) for stream in streams):
        return False
    if _dvr_group_has_mismatched_starts(streams):
        return False
    return not any(_int_extra(stream.extra if isinstance(stream.extra, dict) else {}, "json_dvr_sequence_end") is not None for stream in streams)


def _json_dvr_group_probe_stream(streams: list[StreamInfo]) -> StreamInfo:
    return sorted(
        streams,
        key=lambda stream: (
            0 if stream.media_type == "audio" else 1,
            int(stream.bandwidth or 0),
            str(stream.id or ""),
        ),
    )[0]


def download_stream(
    stream: StreamInfo,
    output_dir: str | Path,
    filename: str | None = None,
    headers: dict[str, str] | None = None,
    workers: int = 16,
    retries: int = 3,
    keep_temp: bool = False,
    downloader: str = "python",
    progress: Callable[[ProgressUpdate], None] | None = None,
    temp_dir: str | Path | None = None,
    resume: bool = True,
    max_speed: int | None = None,
    hls_crypto: HlsCrypto | None = None,
    request_timeout: int = 30,
    check_segments_count: bool = True,
    assemble_output: bool = True,
    host_limiter: _HostAdaptiveLimiter | None = None,
    gate: Callable[[], None] | None = None,
) -> DownloadResult:
    output_path = _output_path(stream, output_dir, filename)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if _is_sabr_ump_stream(stream):
        _apply_cached_dvr_sequence_window(stream)
        start = time.monotonic()
        initial_total = max(1, _sabr_progress_total_hint(stream))
        _emit_progress(progress, stream, 0, initial_total, 0, None, start)
        try:
            if gate is not None:
                gate()

            def sabr_progress(completed_segments: int, total_segments: int, media_bytes: int) -> None:
                if gate is not None:
                    gate()
                _emit_progress(progress, stream, completed_segments, total_segments or initial_total, media_bytes, None, start)

            if _is_sabr_dvr_vod_stream(stream):
                stats = download_sabr_ump_dvr_stream(
                    stream,
                    output_path,
                    headers=headers,
                    request_timeout=request_timeout,
                    retries=retries,
                    workers=workers,
                    progress=sabr_progress,
                )
            else:
                stats = download_sabr_ump_stream(
                    stream,
                    output_path,
                    headers=headers,
                    request_timeout=request_timeout,
                    retries=retries,
                    progress=sabr_progress,
                )
        except SabrUmpError as exc:
            raise DownloadError(f"SABR/UMP download failed: {exc}", url=stream.url) from exc
        if gate is not None:
            gate()
        final_size = output_path.stat().st_size if output_path.exists() else stats.media_bytes
        stream.size_bytes = final_size
        final_total = stats.total_segments or stats.completed_segments or initial_total or 1
        final_completed = stats.completed_segments or final_total
        _emit_progress(progress, stream, final_completed, final_total, final_size, final_size, start, done=True)
        return DownloadResult(stream=stream, path=output_path)

    if _is_json_dvr_sequence_stream(stream):
        _apply_cached_dvr_sequence_window(stream)
        discovery_start = time.monotonic()
        _emit_progress(progress, stream, 0, 1, 0, None, discovery_start)
        stream.segments = _discover_json_dvr_sequence_segments(stream, headers=headers, request_timeout=request_timeout, retries=retries)

    def part(*args, **kwargs):
        if gate is not None:
            gate()
        return _download_part(*args, **kwargs)

    urls = _segment_urls(stream)
    urls = _apply_json_live_base_fallback(stream, urls, headers=headers, request_timeout=request_timeout)
    urls = _split_single_large_remote_segment(stream, urls, headers=headers, hls_crypto=hls_crypto, request_timeout=request_timeout)
    start = time.monotonic()
    completed = 0
    downloaded_bytes = 0
    total_bytes = _known_segments_total_bytes(urls)
    if total_bytes is None and len(urls) == 1 and stream.size_bytes:
        total_bytes = stream.size_bytes
    limiter = RateLimiter(max_speed) if max_speed else None
    qobuz_frame_key = _qobuz_frame_key(stream)
    if qobuz_frame_key and not assemble_output:
        raise DownloadError("Qobuz frame decryption requires assembled output", url=stream.url)
    if qobuz_frame_key:
        downloader = "python"
    elif downloader == "auto":
        downloader = "python"
    prefer_curl = _should_prefer_curl(stream)
    if host_limiter is None:
        host_max_parallel, host_initial_parallel = _host_parallelism_limits(stream, urls, workers)
        host_limiter = _HostAdaptiveLimiter(
            max_parallel=host_max_parallel,
            initial_parallel=host_initial_parallel,
        )
    _emit_progress(progress, stream, completed, len(urls), downloaded_bytes, total_bytes, start)

    if downloader == "aria2c":
        temp_root = _resume_temp_dir(stream, output_path, temp_dir, urls) if resume else _transient_temp_dir(output_path, temp_dir, "aria2")
        sections: list[Path] = []
        try:
            temp_root.mkdir(parents=True, exist_ok=True)
            _write_resume_manifest(temp_root, stream, output_path, len(urls))
            _download_with_aria2c(
                urls,
                output_path,
                headers=headers,
                workers=workers,
                temp_dir=temp_root,
                retries=retries,
                max_speed=max_speed,
                request_timeout=request_timeout,
                gate=gate,
            )
        except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
            if not (keep_temp or resume) and temp_root.exists():
                shutil.rmtree(temp_root, ignore_errors=True)
            raise DownloadError(f"aria2c download failed: {_error_text(exc)}") from exc
        size = output_path.stat().st_size if output_path.exists() else 0
        part_paths = [temp_root / f"{index:08d}.part" for index in range(len(urls))]
        sections = _write_section_outputs(output_path, part_paths, urls)
        _emit_progress(progress, stream, len(urls), len(urls), size, size, start, done=True)
        if not (keep_temp or resume or sections) and temp_root.exists():
            shutil.rmtree(temp_root, ignore_errors=True)
        return DownloadResult(stream=stream, path=output_path, temp_dir=temp_root if keep_temp or resume or sections else None, sections=sections or None, parts=part_paths)
    if downloader != "python":
        raise DownloadError(f"Unsupported downloader: {downloader}")

    if len(urls) == 1 and not resume:
        progress_bytes = _part_progress_callback(progress, stream, len(urls), total_bytes, start)
        size = part(
            urls[0],
            output_path,
            headers,
            retries,
            limiter,
            hls_crypto,
            request_timeout,
            prefer_curl,
            progress_bytes=progress_bytes,
            host_limiter=host_limiter,
        )
        if qobuz_frame_key and urls[0].index != -1:
            decrypt_qobuz_file(output_path, qobuz_frame_key)
        final_size = output_path.stat().st_size if output_path.exists() else size
        _emit_progress(progress, stream, 1, 1, final_size, final_size, start, done=True)
        return DownloadResult(stream=stream, path=output_path)

    if len(urls) == 1:
        temp_root = _resume_temp_dir(stream, output_path, temp_dir, urls) if resume else _transient_temp_dir(output_path, temp_dir, "parts")
        sections: list[Path] = []
        part_paths = [temp_root / "00000000.part"]
        preserve_parts = keep_temp or not assemble_output
        try:
            temp_root.mkdir(parents=True, exist_ok=True)
            _write_resume_manifest(temp_root, stream, output_path, len(urls))
            progress_bytes = _part_progress_callback(progress, stream, len(urls), total_bytes, start)
            cached_size = _cached_part_size(part_paths[0], urls[0]) if resume else None
            if cached_size is not None:
                completed = 1
                if progress_bytes is None:
                    downloaded_bytes = cached_size
                else:
                    progress_bytes.add_cached(cached_size)
                    progress_bytes.set_completed(completed)
                    downloaded_bytes = progress_bytes.total
                _emit_progress(progress, stream, completed, len(urls), downloaded_bytes, total_bytes, start)
            else:
                size = part(
                    urls[0],
                    part_paths[0],
                    headers,
                    retries,
                    limiter,
                    hls_crypto,
                    request_timeout,
                    prefer_curl,
                    progress_bytes=progress_bytes,
                    host_limiter=host_limiter,
                )
                completed = 1
                if progress_bytes:
                    progress_bytes.set_completed(completed)
                downloaded_bytes = progress_bytes.total if progress_bytes is not None else size
                _emit_progress(progress, stream, completed, len(urls), downloaded_bytes, total_bytes, start)
            if assemble_output:
                with output_path.open("wb") as output:
                    _copy_stream_part_to_output(part_paths[0], output, qobuz_frame_key, urls[0])
                final_size = output_path.stat().st_size
            else:
                final_size = _downloaded_part_paths_size(part_paths)
            _emit_progress(progress, stream, 1, 1, final_size, final_size, start, done=True)
            if preserve_parts:
                return DownloadResult(stream=stream, path=output_path, temp_dir=temp_root, parts=part_paths)
        finally:
            if not (preserve_parts or resume or sections) and temp_root.exists():
                shutil.rmtree(temp_root, ignore_errors=True)
        return DownloadResult(stream=stream, path=output_path, temp_dir=temp_root if resume or preserve_parts else None, parts=part_paths)

    temp_root = _resume_temp_dir(stream, output_path, temp_dir, urls) if resume else _transient_temp_dir(output_path, temp_dir, "parts")
    sections: list[Path] = []
    preserve_parts = keep_temp or not assemble_output
    try:
        temp_root.mkdir(parents=True, exist_ok=True)
        _write_resume_manifest(temp_root, stream, output_path, len(urls))
        part_paths: list[Path] = [temp_root / f"{index:08d}.part" for index in range(len(urls))]
        if resume:
            _migrate_inline_init_resume_parts(stream, output_path, temp_dir, temp_root, part_paths)
        progress_bytes = _part_progress_callback(progress, stream, len(urls), total_bytes, start)
        pool = ThreadPoolExecutor(max_workers=max(1, workers))
        pool_wait = True
        futures = {}
        try:
            if _should_hedge_segment_downloads(stream, urls, workers):
                completed, downloaded_bytes = _download_parts_with_hedging(
                    urls=urls,
                    part_paths=part_paths,
                    headers=headers,
                    retries=retries,
                    limiter=limiter,
                    hls_crypto=hls_crypto,
                    request_timeout=request_timeout,
                    prefer_curl=prefer_curl,
                    host_limiter=host_limiter,
                    pool=pool,
                    progress=progress,
                    progress_bytes=progress_bytes,
                    stream=stream,
                    total_bytes=total_bytes,
                    start=start,
                    completed=completed,
                    downloaded_bytes=downloaded_bytes,
                    download_part=part,
                )
                pool_wait = False
            else:
                for index, segment in enumerate(urls):
                    cached_size = _cached_part_size(part_paths[index], segment)
                    if cached_size is not None:
                        completed += 1
                        if progress_bytes is None:
                            downloaded_bytes += cached_size
                        else:
                            progress_bytes.add_cached(cached_size)
                            progress_bytes.set_completed(completed)
                            downloaded_bytes = progress_bytes.total
                        _emit_progress(progress, stream, completed, len(urls), downloaded_bytes, total_bytes, start)
                        continue
                    if gate is not None:
                        gate()
                    futures[
                        pool.submit(
                            part,
                            segment,
                            part_paths[index],
                            headers,
                            retries,
                            limiter,
                            hls_crypto,
                            request_timeout,
                            prefer_curl,
                            progress_bytes=progress_bytes,
                            host_limiter=host_limiter,
                        )
                    ] = index
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        size = future.result()
                    except DownloadError as exc:
                        segment = urls[index]
                        if _should_retry_failed_parallel_part_serially(segment, exc):
                            size = _retry_failed_parallel_part_serially(
                                segment,
                                part_paths[index],
                                headers,
                                retries,
                                limiter,
                                hls_crypto,
                                request_timeout,
                                prefer_curl,
                                progress_bytes,
                                host_limiter,
                                gate=gate,
                            )
                        else:
                            raise
                    except DownloadCancelled:
                        raise
                    except Exception as exc:
                        segment = urls[index]
                        raise DownloadError(
                            f"Failed to download segment {_segment_label(segment)}: {_error_text(exc)}",
                            url=segment.url,
                        ) from exc
                    completed += 1
                    if progress_bytes is None:
                        downloaded_bytes += size
                    else:
                        progress_bytes.set_completed(completed)
                        downloaded_bytes = progress_bytes.total
                    _emit_progress(progress, stream, completed, len(urls), downloaded_bytes, total_bytes, start)
        except KeyboardInterrupt:
            for future in futures:
                future.cancel()
            _shutdown_pool(pool)
            raise
        except BaseException:
            for future in futures:
                future.cancel()
            _shutdown_pool(pool)
            raise
        else:
            pool.shutdown(wait=pool_wait, cancel_futures=not pool_wait)

        if check_segments_count:
            _check_downloaded_parts_count(part_paths, len(urls))
        part_paths = _split_json_dvr_inline_init_parts(stream, urls, part_paths)
        if len(part_paths) != len(urls):
            urls = _segment_urls(stream)
        if assemble_output:
            with output_path.open("wb") as output:
                for part_path, segment in zip(part_paths, urls, strict=False):
                    _copy_stream_part_to_output(part_path, output, qobuz_frame_key, segment)
            sections = _write_section_outputs(output_path, part_paths, urls)
            final_size = output_path.stat().st_size
        else:
            final_size = _downloaded_part_paths_size(part_paths)
        _emit_progress(progress, stream, len(urls), len(urls), final_size, final_size, start, done=True)
        if preserve_parts:
            return DownloadResult(stream=stream, path=output_path, temp_dir=temp_root, sections=sections or None, parts=part_paths)
    finally:
        if not (preserve_parts or resume or sections) and temp_root.exists():
            shutil.rmtree(temp_root, ignore_errors=True)
    return DownloadResult(stream=stream, path=output_path, temp_dir=temp_root if preserve_parts or resume or sections else None, sections=sections or None, parts=part_paths)


def _segment_urls(stream: StreamInfo) -> list[SegmentInfo]:
    if stream.segments:
        return list(stream.segments)
    return [SegmentInfo(url=stream.url, duration=stream.duration, index=0)]


def _qobuz_frame_key(stream: StreamInfo) -> str:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    raw = extra.get("raw")
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("qobuz_frame_key") or "").strip()


def _copy_stream_part_to_output(
    path: Path,
    output,
    qobuz_frame_key: str = "",
    segment: SegmentInfo | None = None,
) -> None:
    if not qobuz_frame_key or (segment is not None and segment.index == -1):
        _copy_file_to_output(path, output)
        return
    output.write(decrypt_qobuz_segment(path.read_bytes(), qobuz_frame_key))


def _downloaded_part_paths_size(part_paths: list[Path]) -> int:
    total = 0
    for path in part_paths:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _split_json_dvr_inline_init_parts(stream: StreamInfo, segments: list[SegmentInfo], part_paths: list[Path]) -> list[Path]:
    if not _json_dvr_uses_inline_init_media(stream):
        return part_paths
    if not segments or not part_paths or any(segment.index == -1 for segment in segments):
        return part_paths
    first_part = part_paths[0]
    if not first_part.exists():
        return part_paths
    init_part = first_part.with_name(f"{first_part.stem}.init{first_part.suffix}")
    media_part = first_part.with_name(f"{first_part.stem}.media{first_part.suffix}")
    try:
        init_path, media_path = split_fragmented_mp4_init_media(first_part, init_part, media_part)
    except (OSError, ValueError):
        init_part.unlink(missing_ok=True)
        media_part.unlink(missing_ok=True)
        return part_paths
    if init_path is None:
        media_part.unlink(missing_ok=True)
        return part_paths
    first_part.unlink(missing_ok=True)
    init_segment = replace(
        segments[0],
        index=-1,
        duration=None,
        byte_range=None,
    )
    stream.segments = [init_segment, *segments]
    return [init_path, media_path, *part_paths[1:]]


def _json_dvr_uses_inline_init_media(stream: StreamInfo) -> bool:
    if not _is_json_dvr_sequence_stream(stream):
        return False
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    raw = extra.get("raw")
    if not isinstance(raw, dict):
        return False
    source = str(raw.get("init_range_source") or raw.get("initRangeSource") or "").strip().lower()
    if source != "live_byte_range_fallback":
        return False
    text = " ".join(str(raw.get(key) or "") for key in ("mime_type", "mimeType", "codec", "codecs")).lower()
    return "mp4" in text or not text


def _apply_json_live_base_fallback(
    stream: StreamInfo,
    segments: list[SegmentInfo],
    headers: dict[str, str] | None = None,
    request_timeout: int = 30,
) -> list[SegmentInfo]:
    if stream.extra.get("range_redirect_url"):
        return segments
    bases = _json_live_all_urls(stream)
    if stream.manifest_type != "json" or len(bases) <= 1 or len(segments) <= 1:
        return segments
    probe_segment = next((segment for segment in segments if segment.index != -1 and is_url(segment.url)), None)
    if not probe_segment:
        return segments

    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for base in bases:
        candidate = _json_live_rebase_segment_url(stream.url, probe_segment.url, base)
        if candidate in seen:
            continue
        seen.add(candidate)
        candidates.append((base, candidate))
    if len(candidates) <= 1:
        return segments

    probe_timeout = max(2, min(3, int(request_timeout or 30)))
    pool = ThreadPoolExecutor(max_workers=min(len(candidates), 4))
    future_to_base = {
        pool.submit(_json_live_probe_url, candidate, headers, probe_timeout): base
        for base, candidate in candidates
    }
    chosen_base: str | None = None
    failed_probes: list[tuple[str, _JsonLiveProbeResult]] = []
    pending = set(future_to_base)
    deadline = time.monotonic() + probe_timeout
    try:
        while pending and chosen_base is None:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                base = future_to_base[future]
                try:
                    result = future.result()
                    if result.ok:
                        chosen_base = base
                        break
                except Exception:
                    result = _JsonLiveProbeResult(False)
                failed_probes.append((base, result))
    finally:
        _shutdown_pool(pool)
    if chosen_base is None and any(result.netflix_geo_failed for _base, result in failed_probes):
        detail = next((result.error for _base, result in failed_probes if result.netflix_geo_failed and result.error), "Netflix geo check failed")
        raise DownloadError(
            f"No usable Netflix JSON live CDN URL: {detail}. Re-export this JSON using the same network/proxy you will download with."
        )
    if not chosen_base or chosen_base == stream.url:
        return segments
    return [replace(segment, url=_json_live_rebase_segment_url(stream.url, segment.url, chosen_base)) for segment in segments]


def _json_live_all_urls(stream: StreamInfo) -> list[str]:
    values = stream.extra.get("all_urls")
    urls: list[str] = []
    if isinstance(values, list):
        urls.extend(value.strip() for value in values if isinstance(value, str) and value.strip())
    elif isinstance(values, dict):
        urls.extend(value.strip() for value in values.values() if isinstance(value, str) and value.strip())
    if stream.url:
        urls.insert(0, stream.url)
    result: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def _json_live_rebase_segment_url(current_base: str, segment_url: str, new_base: str) -> str:
    segment = urlparse(segment_url)
    base = urlparse(current_base)
    replacement = urlparse(new_base)
    base_path = base.path.rstrip("/")
    if base_path and segment.path.startswith(f"{base_path}/"):
        suffix = segment.path[len(base_path) :]
    elif base_path and segment.path.rstrip("/") == base_path:
        suffix = ""
    else:
        suffix = f"/{segment.path.rsplit('/', 1)[-1]}"
    replacement_path = replacement.path.rstrip("/")
    path = f"{replacement_path}{suffix}" if replacement_path else suffix
    return urlunparse((replacement.scheme, replacement.netloc, path or "/", "", replacement.query, ""))


def _json_live_probe_url(url: str, headers: dict[str, str] | None, timeout: int) -> _JsonLiveProbeResult:
    request_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity", "Range": "bytes=0-0"}
    request_headers.update(headers or {})
    try:
        with get_global_http_client().request("GET", url, headers=request_headers, timeout=timeout) as response:
            response.read(1)
        return _JsonLiveProbeResult(True)
    except Exception as exc:
        error = _error_text(exc)
        return _JsonLiveProbeResult(False, error=error, netflix_geo_failed="netflix geo check failed" in error.lower())


def _known_segments_total_bytes(segments: list[SegmentInfo]) -> int | None:
    if not segments:
        return None
    total = 0
    for segment in segments:
        if segment.data is not None:
            total += len(segment.data)
            continue
        if segment.byte_range:
            start, end = segment.byte_range
            if end < start:
                return None
            total += end - start + 1
            continue
        return None
    return total


def _split_single_large_remote_segment(
    stream: StreamInfo,
    segments: list[SegmentInfo],
    headers: dict[str, str] | None = None,
    hls_crypto: HlsCrypto | None = None,
    request_timeout: int = 30,
) -> list[SegmentInfo]:
    if stream.is_live or len(segments) != 1:
        return segments
    if hls_crypto and _hls_method(hls_crypto.method) != "NONE":
        return segments
    if _stream_contains_hls_segment_cipher(stream):
        return segments
    segment = segments[0]
    if segment.data is not None or not is_url(segment.url):
        return segments
    if segment.byte_range and segment.byte_range[0] != 0:
        return segments

    known_size = (segment.byte_range[1] + 1) if segment.byte_range else stream.size_bytes
    segment_url_changed = False
    probe_result = _probe_range_support(segment.url, headers=headers, timeout=request_timeout, expected_size=known_size)
    if probe_result.url and probe_result.url != segment.url:
        original_url = segment.url
        segment = replace(segment, url=probe_result.url)
        segment_url_changed = True
        if stream.url == original_url:
            stream.url = probe_result.url
        stream.extra["range_redirect_url"] = probe_result.url
    if not probe_result.supported:
        stream.extra["remote_range_supported"] = False
        if _stream_has_trusted_direct_range_size(stream, segment):
            return [replace(segment, byte_range=None)]
        return [segment] if segment_url_changed else segments
    stream.extra["remote_range_supported"] = True
    ranged_size = probe_result.size
    size = ranged_size or known_size
    if ranged_size:
        stream.size_bytes = ranged_size
        stream.extra["remote_content_length_probed"] = True
    split_size = _single_file_split_size(stream, segment)
    split_threshold = _single_file_split_threshold(stream, segment)
    if not size or size < split_threshold:
        return [segment] if segment_url_changed else segments

    duration = segment.duration if segment.duration is not None else stream.duration
    split_segments: list[SegmentInfo] = []
    start = 0
    index = 0
    while start < size:
        end = min(size - 1, start + split_size - 1)
        part_duration = None
        if duration:
            part_duration = duration * ((end - start + 1) / size)
        split_segments.append(
            SegmentInfo(
                url=segment.url,
                duration=part_duration,
                index=index,
                byte_range=(start, end),
                encrypted=segment.encrypted,
                encryption_scheme=segment.encryption_scheme,
                key_id=segment.key_id,
                key_uri=segment.key_uri,
                key_iv=segment.key_iv,
                program_date_time=segment.program_date_time,
                timeline_time=segment.timeline_time,
                timeline_presentation_time=segment.timeline_presentation_time,
            )
        )
        index += 1
        start = end + 1
    return split_segments or segments


def _stream_has_trusted_direct_range_size(stream: StreamInfo, segment: SegmentInfo) -> bool:
    return stream.manifest_type == "json" and bool(segment.byte_range and segment.byte_range[0] == 0 and stream.size_bytes)


def _single_file_split_size(stream: StreamInfo, segment: SegmentInfo) -> int:
    if _stream_has_trusted_direct_range_size(stream, segment):
        return _JSON_DIRECT_SINGLE_FILE_SPLIT_SIZE
    return _LARGE_SINGLE_FILE_SPLIT_SIZE


def _single_file_split_threshold(stream: StreamInfo, segment: SegmentInfo) -> int:
    split_size = _single_file_split_size(stream, segment)
    if _stream_has_trusted_direct_range_size(stream, segment):
        return split_size + 1
    return max(split_size + 1, _LARGE_SINGLE_FILE_SPLIT_THRESHOLD)


def _initial_host_parallelism(stream: StreamInfo, segments: list[SegmentInfo], workers: int) -> int:
    worker_count = max(1, int(workers or 1))
    if _is_single_file_range_download(stream, segments):
        return min(worker_count, 4)
    return worker_count


def _host_parallelism_limits(stream: StreamInfo, segments: list[SegmentInfo], workers: int) -> tuple[int, int]:
    worker_count = max(1, int(workers or 1))
    if segments:
        replay_limits = yangshipin_catchup_parallelism(segments[0].url, worker_count)
        if replay_limits is not None:
            return replay_limits
    return worker_count, _initial_host_parallelism(stream, segments, worker_count)


def _is_single_file_range_download(stream: StreamInfo, segments: list[SegmentInfo]) -> bool:
    if len(segments) <= 1:
        return False
    urls = {segment.url for segment in segments if segment.url}
    return len(urls) == 1 and all(segment.byte_range for segment in segments)


def _is_json_single_file_range_download(stream: StreamInfo, segments: list[SegmentInfo]) -> bool:
    if stream.manifest_type != "json" or len(segments) <= 1:
        return False
    return _is_single_file_range_download(stream, segments)


def _discover_json_dvr_sequence_segments(
    stream: StreamInfo,
    *,
    headers: dict[str, str] | None,
    request_timeout: int,
    retries: int = 3,
) -> list[SegmentInfo]:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    start = _int_extra(extra, "json_dvr_sequence_start")
    if start is None:
        raise DownloadError("DVR sequence stream is missing min_sq.", url=stream.url)
    reliable_end = _int_extra(extra, "json_dvr_sequence_end")
    if reliable_end is not None and reliable_end >= start:
        end = reliable_end
    else:
        end = _probe_json_dvr_sequence_end(stream, start, headers=headers, request_timeout=request_timeout)
    if end < start:
        raise DownloadError("DVR sequence stream has no available media segments.", url=stream.url)
    duration = _float_extra(extra, "json_dvr_segment_duration")
    segments: list[SegmentInfo] = []
    init_segment = _json_dvr_init_segment(stream)
    if init_segment is not None:
        segments.append(init_segment)
    segments.extend(_json_dvr_sequence_segment(stream, sequence, duration=duration) for sequence in range(start, end + 1))
    return segments


def _align_sabr_dvr_group_first_media_sequence(
    streams: list[StreamInfo],
    *,
    headers: dict[str, str] | None,
    request_timeout: int,
    retries: int,
) -> None:
    if not streams or not all(_is_sabr_dvr_vod_stream(stream) for stream in streams):
        return
    starts = [_dvr_sequence_start(stream) for stream in streams]
    if any(start is None for start in starts):
        return
    actual_starts: list[int] = []
    for stream, current_start in zip(streams, starts, strict=False):
        if current_start is None:
            continue
        try:
            actual_start = probe_sabr_ump_dvr_first_media_sequence(
                stream,
                headers=headers,
                request_timeout=request_timeout,
                retries=retries,
            )
        except (DownloadError, SabrUmpError, HttpClientError, OSError, ValueError):
            continue
        if actual_start is not None and actual_start >= current_start:
            actual_starts.append(int(actual_start))
    if not actual_starts:
        return
    aligned_start = max(max(int(start) for start in starts if start is not None), max(actual_starts))
    for stream in streams:
        _set_dvr_sequence_start(stream, aligned_start)


def _probe_stream_dvr_sequence_window(
    stream: StreamInfo,
    *,
    headers: dict[str, str] | None,
    request_timeout: int,
    retries: int,
    remember: bool = True,
) -> tuple[int, int] | None:
    if _is_sabr_dvr_vod_stream(stream):
        return probe_sabr_ump_dvr_sequence_window(stream, headers=headers, request_timeout=request_timeout, retries=retries)
    if _is_json_dvr_sequence_stream(stream):
        extra = stream.extra if isinstance(stream.extra, dict) else {}
        start = _int_extra(extra, "json_dvr_sequence_start")
        if start is None:
            return None
        reliable_end = _int_extra(extra, "json_dvr_sequence_end")
        end = reliable_end if reliable_end is not None and reliable_end >= start else _probe_json_dvr_sequence_end(
            stream,
            start,
            headers=headers,
            request_timeout=request_timeout,
        )
        if end < start:
            return None
        if remember:
            _remember_dvr_sequence_window(stream, start, end)
        return start, end
    return None


def _apply_cached_dvr_sequence_window(stream: StreamInfo) -> None:
    key = _dvr_sequence_group_key(stream)
    if key is None:
        return
    with _DVR_SEQUENCE_WINDOW_LOCK:
        window = _DVR_SEQUENCE_WINDOW_CACHE.get(key)
    if window is None:
        return
    _set_dvr_sequence_window(stream, window[0], window[1])


def _remember_dvr_sequence_window(stream: StreamInfo, start: int, end: int) -> None:
    key = _dvr_sequence_group_key(stream)
    if key is None or end < start:
        return
    with _DVR_SEQUENCE_WINDOW_LOCK:
        cached = _DVR_SEQUENCE_WINDOW_CACHE.get(key)
        if cached is not None:
            start = max(start, cached[0])
            end = min(end, cached[1])
            if end < start:
                return
        _DVR_SEQUENCE_WINDOW_CACHE[key] = (int(start), int(end))
    _set_dvr_sequence_window(stream, int(start), int(end))


def _set_dvr_sequence_window(stream: StreamInfo, start: int, end: int) -> None:
    if end < start:
        return
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    if not isinstance(stream.extra, dict):
        stream.extra = extra
    if _is_sabr_dvr_vod_stream(stream):
        extra["sabr_dvr_min_sequence"] = int(start)
        extra["sabr_dvr_end_sequence"] = int(end)
    elif _is_json_dvr_sequence_stream(stream):
        extra["json_dvr_sequence_start"] = int(start)
        extra["json_dvr_sequence_end"] = int(end)


def _set_dvr_sequence_start(stream: StreamInfo, start: int) -> None:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    if not isinstance(stream.extra, dict):
        stream.extra = extra
    if _is_sabr_dvr_vod_stream(stream):
        extra["sabr_dvr_min_sequence"] = int(start)
    elif _is_json_dvr_sequence_stream(stream):
        extra["json_dvr_sequence_start"] = int(start)


def _dvr_sequence_group_key(stream: StreamInfo) -> tuple[str, str] | None:
    start = _dvr_sequence_start(stream)
    if start is None:
        return None
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    title_id = str(extra.get("title_id") or "").strip()
    source_id = title_id or _video_id_from_url(stream.url) or _video_id_from_url(stream.original_url)
    if not source_id:
        return None
    if _is_sabr_dvr_vod_stream(stream):
        coordinate_space = "sabr_ump"
    elif _is_json_dvr_sequence_stream(stream):
        coordinate_space = "json"
    else:
        coordinate_space = str(stream.manifest_type or "unknown")
    return source_id, coordinate_space


def _dvr_group_has_mismatched_starts(streams: list[StreamInfo]) -> bool:
    starts = {_dvr_sequence_start(stream) for stream in streams}
    starts.discard(None)
    return len(starts) > 1


def _dvr_sequence_start(stream: StreamInfo) -> int | None:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    if _is_sabr_dvr_vod_stream(stream):
        return _int_extra(extra, "sabr_dvr_min_sequence")
    if _is_json_dvr_sequence_stream(stream):
        return _int_extra(extra, "json_dvr_sequence_start")
    return None


def _dvr_sequence_end_key(stream: StreamInfo) -> str:
    return "sabr_dvr_end_sequence" if _is_sabr_dvr_vod_stream(stream) else "json_dvr_sequence_end"


def _video_id_from_url(url: str) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    value = params.get("id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _probe_json_dvr_sequence_end(
    stream: StreamInfo,
    start: int,
    *,
    headers: dict[str, str] | None,
    request_timeout: int,
) -> int:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    probe_timeout = max(0.8, min(1.25, float(request_timeout or 30)))
    confirm_timeout = max(probe_timeout, min(5.0, max(1.0, float(request_timeout or 30) / 4.0)))
    confirmed_misses: set[int] = set()
    availability: dict[int, bool] = {}

    def available(sequence: int) -> bool:
        if sequence not in availability:
            is_available = _json_dvr_sequence_available(stream, sequence, headers=headers, timeout=probe_timeout)
            if not is_available and sequence not in confirmed_misses and confirm_timeout > probe_timeout:
                confirmed_misses.add(sequence)
                is_available = _json_dvr_sequence_available(stream, sequence, headers=headers, timeout=confirm_timeout)
            availability[sequence] = is_available
        return availability[sequence]

    if not available(start):
        raise DownloadError(f"DVR sequence {start} is not available.", url=stream.url)

    lower = start
    hint = _int_extra(extra, "json_dvr_sequence_end_hint")
    if hint is not None and hint >= start and available(hint):
        lower = hint

    step = max(1, lower - start + 1)
    max_count = _int_extra(extra, "json_dvr_sequence_max_count")
    if max_count is None:
        duration = _float_extra(extra, "json_dvr_duration_seconds")
        segment_duration = _float_extra(extra, "json_dvr_segment_duration") or 5.0
        if duration is not None and duration > 0 and segment_duration > 0:
            max_count = max(1, int(math.ceil(duration / segment_duration)) + 12)
    max_probe_span = max(1, _int_extra(extra, "json_dvr_sequence_max_probe_span") or 20000)
    if max_count is not None and max_count > 0:
        max_probe_span = min(max_probe_span, max(1, max_count - 1))
    max_sequence = start + max_probe_span
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


def _json_dvr_sequence_available(
    stream: StreamInfo,
    sequence: int,
    *,
    headers: dict[str, str] | None,
    timeout: int,
) -> bool:
    segment = _json_dvr_sequence_segment(stream, sequence, duration=None)
    request_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity", "Range": "bytes=0-0"}
    request_headers.update(headers or {})
    current_url = segment.url
    redirect_chain: list[str] = []
    try:
        for _ in range(5):
            with get_global_http_client().request("GET", current_url, headers=request_headers, timeout=timeout) as response:
                redirect_url, data = _json_dvr_probe_response(response, current_url)
                if redirect_url:
                    if redirect_url in redirect_chain:
                        return False
                    redirect_chain.append(redirect_url)
                    current_url = redirect_url
                    continue
                return _json_dvr_probe_has_media(response, data)
        return False
    except (TimeoutError, URLError, OSError, http.client.HTTPException) as exc:
        if _is_timeout_like_error(exc):
            return False
        raise DownloadError(f"Failed to probe DVR sequence {sequence}: {_error_text(exc)}", url=current_url) from exc
    except HttpClientError as exc:
        if _is_missing_http_error(exc) or _is_timeout_like_error(exc):
            return False
        raise DownloadError(f"Failed to probe DVR sequence {sequence}: {_error_text(exc)}", url=current_url) from exc


def _json_dvr_probe_response(response, base_url: str) -> tuple[str | None, bytes]:
    response_headers = getattr(response, "headers", {}) or {}
    status = int(getattr(response, "status", 200) or 200)
    content_type = _header_value(response_headers, "Content-Type").lower()
    content_length = _header_content_length(response_headers)
    if status == 206 and ("text/" not in content_type and "url" not in content_type):
        return None, response.read(32)
    if content_type and "text/" not in content_type and "url" not in content_type:
        return None, response.read(32)
    if content_length is not None and content_length > 8192:
        return None, response.read(32)
    data = response.read(8193)
    if data.lstrip().startswith((b"http://", b"https://")):
        redirect_url = _text_url_redirect_from_bytes(data, response_headers, base_url=base_url)
        if redirect_url:
            return redirect_url, b""
    return None, data[:32]


def _json_dvr_probe_has_media(response, data: bytes) -> bool:
    if not data:
        return False
    response_headers = getattr(response, "headers", {}) or {}
    status = int(getattr(response, "status", 200) or 200)
    content_type = _header_value(response_headers, "Content-Type").lower()
    if content_type.startswith(("video/", "audio/")):
        return True
    if content_type in {"application/octet-stream", "binary/octet-stream"}:
        return True
    if status == 206 and "text/" not in content_type:
        return True
    if not content_type:
        return True
    return _looks_like_media_bytes(data)


def _looks_like_media_bytes(data: bytes) -> bool:
    if len(data) >= 8 and data[4:8] in _MP4_TOP_LEVEL_BOXES:
        return True
    if data.startswith((bytes.fromhex("1a45dfa3"), bytes.fromhex("1f43b675"))):
        return True
    return bool(data and data[0] == 0x47)


def _json_dvr_sequence_segment(stream: StreamInfo, sequence: int, *, duration: float | None) -> SegmentInfo:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    key_id = extra.get("key_id") if isinstance(extra.get("key_id"), str) else None
    return SegmentInfo(
        url=_url_with_query_value(stream.url, "sq", str(int(sequence))),
        duration=duration,
        index=int(sequence),
        encrypted=stream.encrypted,
        encryption_scheme=stream.encryption_scheme,
        key_id=key_id,
    )


def _json_dvr_init_segment(stream: StreamInfo) -> SegmentInfo | None:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    raw = extra.get("raw")
    if not isinstance(raw, dict):
        return None
    if str(raw.get("init_range_source") or raw.get("initRangeSource") or "").strip().lower() == "live_byte_range_fallback":
        return None
    init_range = _parse_json_byte_range(
        raw.get("init_range")
        or raw.get("initRange")
        or raw.get("initialization_range")
        or raw.get("initializationRange")
    )
    if init_range is None:
        return None
    key_id = extra.get("key_id") if isinstance(extra.get("key_id"), str) else None
    return SegmentInfo(
        url=stream.url,
        index=-1,
        byte_range=init_range,
        encrypted=stream.encrypted,
        encryption_scheme=stream.encryption_scheme,
        key_id=key_id,
    )


def _url_with_query_value(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    params = [(name, item) for name, item in parse_qsl(parsed.query, keep_blank_values=True) if name != key]
    params.append((key, value))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(params), parsed.fragment))


def _int_extra(extra: dict[str, object], key: str) -> int | None:
    value = extra.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _float_extra(extra: dict[str, object], key: str) -> float | None:
    value = extra.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _parse_json_byte_range(value: object) -> tuple[int, int] | None:
    if isinstance(value, dict):
        start = _int_value(value.get("start"))
        end = _int_value(value.get("end"))
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        start = _int_value(value[0])
        end = _int_value(value[1])
    elif isinstance(value, str) and "-" in value:
        left, right = value.split("-", 1)
        start = _int_value(left)
        end = _int_value(right)
    else:
        return None
    if start is None or end is None or end < start:
        return None
    return start, end


def _int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _is_timeout_like_error(exc: BaseException | None) -> bool:
    text = _error_text(exc).lower()
    return "timed out" in text or "timeout" in text


def probe_remote_stream_size(
    stream: StreamInfo,
    headers: dict[str, str] | None = None,
    request_timeout: int = 30,
) -> int | None:
    if stream.is_live or len(stream.segments) != 1:
        return None
    segment = stream.segments[0]
    if segment.data is not None or not is_url(segment.url):
        return None
    if segment.byte_range:
        size = segment.byte_range[1] - segment.byte_range[0] + 1
    else:
        size = _probe_range_size(segment.url, headers=headers, timeout=request_timeout)
    if size and size > 0:
        stream.size_bytes = size
        stream.extra["remote_content_length_probed"] = True
        return size
    return None


def prepare_remote_stream_download_plan(
    stream: StreamInfo,
    headers: dict[str, str] | None = None,
    hls_crypto: HlsCrypto | None = None,
    request_timeout: int = 30,
) -> list[SegmentInfo]:
    planned = _split_single_large_remote_segment(
        stream,
        list(stream.segments),
        headers=headers,
        hls_crypto=hls_crypto,
        request_timeout=request_timeout,
    )
    if planned and planned != stream.segments:
        stream.segments = planned
    return planned


def fetch_segment_probe_bytes(
    segment: SegmentInfo,
    headers: dict[str, str] | None = None,
    request_timeout: int = 30,
) -> bytes:
    if segment.byte_range and is_url(segment.url):
        return _fetch_probe_range_bytes(segment.url, segment.byte_range, headers=headers, request_timeout=request_timeout)
    return _fetch_bytes(segment, headers=headers, retries=1, request_timeout=request_timeout)


def _fetch_probe_range_bytes(
    url: str,
    byte_range: tuple[int, int],
    headers: dict[str, str] | None = None,
    request_timeout: int = 30,
    redirects_remaining: int = 2,
) -> bytes:
    start, end = byte_range
    request_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
    request_headers.update(headers or {})
    request_headers["Range"] = f"bytes={start}-{end}"
    try:
        with get_global_http_client().request("GET", url, headers=request_headers, timeout=request_timeout) as response:
            if response.status == 206:
                return response.read()
            redirect_url = _text_url_redirect_from_response(response)
            if redirect_url and redirects_remaining > 0:
                return _fetch_probe_range_bytes(redirect_url, byte_range, headers=headers, request_timeout=request_timeout, redirects_remaining=redirects_remaining - 1)
    except Exception as exc:
        raise DownloadError(f"Failed to fetch init range {start}-{end}: {_error_text(exc)}", url=url) from exc
    raise DownloadError(f"Range probe {start}-{end} was not honored.", url=url)


def _probe_range_size(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> int | None:
    result = _probe_range_support(url, headers=headers, timeout=timeout)
    return result.size if result.supported else None


def _probe_range_support(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
    expected_size: int | None = None,
    redirects_remaining: int = 2,
) -> _RangeProbeResult:
    request_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
    request_headers.update(headers or {})
    range_headers = dict(request_headers)
    range_headers["Range"] = "bytes=0-0"
    try:
        with get_global_http_client().request("GET", url, headers=range_headers, timeout=timeout) as response:
            if response.status == 206:
                response.read(1)
                return _RangeProbeResult(True, _content_range_total(response.get_header("Content-Range")) or expected_size, url)
            redirect_url = _text_url_redirect_from_response(response)
            if redirect_url and redirects_remaining > 0:
                nested = _probe_range_support(redirect_url, headers=headers, timeout=timeout, expected_size=expected_size, redirects_remaining=redirects_remaining - 1)
                if nested.url:
                    return nested
                return _RangeProbeResult(nested.supported, nested.size, redirect_url)
    except Exception:
        return _RangeProbeResult(False)
    return _RangeProbeResult(False)


def _content_range_total(value: str | None) -> int | None:
    if not value or "/" not in value:
        return None
    total = value.rsplit("/", 1)[-1].strip()
    return int(total) if total.isdigit() else None


def _text_url_redirect_from_response(response) -> str | None:
    if _header_content_length(response.headers) is not None and _header_content_length(response.headers) > 8192:
        return None
    data = response.read(8193)
    return _text_url_redirect_from_bytes(data, response.headers, base_url=getattr(response, "url", ""))


def _text_url_redirect_from_bytes(data: bytes, headers: dict[str, str] | None = None, *, base_url: str = "") -> str | None:
    if len(data) > 8192:
        return None
    headers = headers or {}
    content_type = _header_value(headers, "Content-Type").lower()
    content_length = _header_content_length(headers)
    if content_length is not None and content_length > 8192:
        return None
    stripped = data.lstrip()
    looks_like_url = stripped.startswith((b"http://", b"https://"))
    if content_type and "text/" not in content_type and "url" not in content_type and not looks_like_url:
        return None
    text = data.decode("utf-8", "ignore").strip()
    if not text:
        return None
    first = text.splitlines()[0].strip()
    parsed = urlparse(urljoin(base_url, first))
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    return None


def _header_content_length(headers) -> int | None:
    value = headers.get("Content-Length") or headers.get("content-length")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _header_value(headers: dict[str, str], name: str) -> str:
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return ""


def _follow_text_url_redirect(segment: SegmentInfo, redirect_url: str, chain: list[str]) -> SegmentInfo:
    target = urljoin(segment.url, redirect_url)
    if target in chain:
        raise DownloadError("Text URL redirect loop while downloading segment.", url=segment.url)
    chain.append(target)
    if len(chain) > 4:
        raise DownloadError("Text URL redirect chain was too long while downloading segment.", url=segment.url)
    return replace(segment, url=target)


def _segment_may_return_text_url_redirect(segment: SegmentInfo) -> bool:
    if not is_url(segment.url):
        return False
    parsed = urlparse(segment.url)
    host = (parsed.hostname or "").lower()
    if not host.endswith("googlevideo.com"):
        return False
    if not parsed.path.rstrip("/").endswith("/videoplayback"):
        return False
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return bool(segment.byte_range or params.get("sq"))


def _should_prefer_curl(stream: StreamInfo) -> bool:
    candidates = [
        str(getattr(stream, "url", "") or ""),
        str(getattr(stream, "original_url", "") or ""),
        *(str(getattr(segment, "url", "") or "") for segment in getattr(stream, "segments", []) or []),
    ]
    return any(should_prefer_yangshipin_catchup_curl(url) for url in candidates)


def _should_use_aria2c(stream: StreamInfo, segments: list[SegmentInfo], hls_crypto: HlsCrypto | None = None) -> bool:
    return False


def _stream_uses_segmented_webm_media(stream: StreamInfo) -> bool:
    if stream.manifest_type not in {"dash", "ism", "hls"}:
        return False
    segments = _segment_urls(stream)
    if len(segments) <= 1 and not any(segment.byte_range for segment in segments):
        return False
    return any(is_url(segment.url) for segment in segments)


def _is_live_remote_hls_media(stream: StreamInfo) -> bool:
    if stream.manifest_type != "hls" or not stream.is_live:
        return False
    return _is_live_remote_media(stream)


def _stream_uses_webm_container(stream: StreamInfo) -> bool:
    extension = (stream.extension or "").lower()
    if extension:
        return extension == "webm"
    raw = stream.extra.get("raw") if isinstance(stream.extra, dict) else None
    mime_type = ""
    if isinstance(raw, dict):
        mime_type = str(raw.get("mime_type") or raw.get("mimeType") or "").lower()
    if mime_type:
        return "webm" in mime_type
    codecs = (stream.codecs or "").lower()
    return extension == "webm" or any(token in codecs for token in ("vp9", "vp09", "vp8", "vp08"))


def _is_live_remote_media(stream: StreamInfo) -> bool:
    if not stream.is_live:
        return False
    if stream.media_type in {"subtitle", "subtitles", "text"}:
        return False
    return any(is_url(segment.url) for segment in _segment_urls(stream))


def _stream_contains_hls_segment_cipher(stream: StreamInfo) -> bool:
    if stream.manifest_type != "hls":
        return False
    candidates = [stream.encryption_scheme]
    candidates.extend(segment.encryption_scheme for segment in _segment_urls(stream))
    return any(_hls_method(value) in _HLS_SEGMENT_CIPHER_METHODS for value in candidates)


def _write_section_outputs(output_path: Path, part_paths: list[Path], segments: list[SegmentInfo]) -> list[Path]:
    ranges = _init_section_ranges(segments)
    if len(ranges) <= 1:
        return []
    section_dir = part_paths[0].parent / "sections"
    section_dir.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix or ".mp4"
    paths: list[Path] = []
    for section_index, (start, end) in enumerate(ranges, start=1):
        section_path = section_dir / f"{output_path.stem}.{section_index:03d}{suffix}"
        with section_path.open("wb") as output:
            for part_path in part_paths[start:end]:
                _copy_file_to_output(part_path, output)
        paths.append(section_path)
    return paths


def _init_section_ranges(segments: list[SegmentInfo]) -> list[tuple[int, int]]:
    starts = [index for index, segment in enumerate(segments) if segment.index == -1]
    if len(starts) <= 1:
        return [(0, len(segments))] if segments else []
    ranges: list[tuple[int, int]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(segments)
        if end > start:
            ranges.append((start, end))
    return ranges


def _default_tmp_root() -> Path:
    return Path(__file__).resolve().parents[2] / "tmp"


def _transient_temp_dir(output_path: Path, temp_dir: str | Path | None, label: str) -> Path:
    root = Path(temp_dir).expanduser() if temp_dir else output_path.parent
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{output_path.stem}_{label}_", dir=str(root)))


def _resume_temp_dir(
    stream: StreamInfo,
    output_path: Path,
    temp_dir: str | Path | None,
    segments: list[SegmentInfo] | None = None,
    *,
    search_fallback: bool = True,
) -> Path:
    root = Path(temp_dir).expanduser() if temp_dir else _default_tmp_root()
    key = _stream_resume_key(stream, segments=segments)
    label = _safe_name("-".join(part for part in [stream.media_type, stream.resolution, stream.language, str(stream.bandwidth or "")] if part))
    target = root / f"{label or output_path.stem}_{key[:16]}"
    if target.exists() or not search_fallback:
        return target

    # A refreshed manifest can legitimately change its segment list (for
    # example, a service may add a new init segment) while still pointing at the
    # same title.  Older versions keyed the directory by every segment URL, so
    # a new signed playlist could strand otherwise valid ``.part`` files in an
    # unreachable directory.  Look for a prior manifest carrying the stable
    # stream identity and reuse that directory.  The output path check prevents
    # accidentally borrowing parts from a different save target.
    identity = _stream_resume_identity(stream)
    candidate: tuple[float, Path] | None = None
    try:
        entries = root.iterdir()
    except OSError:
        return target
    for entry in entries:
        if not entry.is_dir() or entry == target:
            continue
        marker = entry / "resume.json"
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("output") != str(output_path):
            continue
        marker_identity = data.get("identity")
        if marker_identity != identity:
            # Caches written before the identity field was introduced can still
            # be matched safely by their canonical original manifest URL and
            # the exact output path.  Volatile query parameters are removed by
            # ``_resume_url_key`` just as they are for the current key.
            marker_url = data.get("original_url")
            if (
                marker_identity is not None
                or not marker_url
                or not stream.original_url
                or _resume_url_key(marker_url) != _resume_url_key(stream.original_url)
            ):
                continue
        try:
            updated = float(data.get("updated_at", marker.stat().st_mtime))
        except (TypeError, ValueError, OSError):
            updated = 0.0
        if candidate is None or updated > candidate[0]:
            candidate = (updated, entry)
    return candidate[1] if candidate is not None else target


def _stream_resume_identity(stream: StreamInfo) -> str:
    """Stable identity used to find parts after a manifest refresh.

    Unlike the full resume key this intentionally excludes the segment list.
    Segment URLs and counts may change between two parses of the same VOD, but
    the canonical manifest/track identity remains the same.  It is only used
    for a conservative directory migration after the exact key was not found.
    """

    payload = {
        "manifest_type": stream.manifest_type,
        "media_type": stream.media_type,
        "url": _resume_url_key(stream.url),
        "original_url": _resume_url_key(stream.original_url),
        "id": stream.id,
        "group_id": stream.group_id,
        "name": stream.name,
        "language": stream.language,
        "bandwidth": stream.bandwidth,
        "resolution": stream.resolution,
    }
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(data).hexdigest()


def _stream_resume_key(stream: StreamInfo, segments: list[SegmentInfo] | None = None) -> str:
    segment_list = segments if segments is not None else _segment_urls(stream)
    payload = {
        # Keep the original exact-key payload stable so existing caches remain
        # addressable after an upgrade.  The relaxed identity is only used by
        # the fallback directory scan above.
        "manifest_type": stream.manifest_type,
        "media_type": stream.media_type,
        "url": _resume_url_key(stream.url),
        "original_url": _resume_url_key(stream.original_url),
        "id": stream.id,
        "group_id": stream.group_id,
        "name": stream.name,
        "language": stream.language,
        "bandwidth": stream.bandwidth,
        "resolution": stream.resolution,
        "segments": [
            {
                "url": _resume_url_key(segment.url),
                "index": segment.index,
                "byte_range": segment.byte_range,
                "inline_sha1": hashlib.sha1(segment.data).hexdigest() if segment.data is not None else None,
                "key_id": segment.key_id,
            }
            for segment in segment_list
        ],
    }
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(data).hexdigest()


def _resume_url_key(url: str | None) -> str | None:
    if not url:
        return url
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return str(url)
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_volatile_resume_query_key(key)
    ]
    query = urlencode(sorted(query_items), doseq=True)
    path = _stable_resume_path(parsed.path)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def _is_volatile_resume_query_key(key: str) -> bool:
    normalized = key.strip().lower()
    return normalized in _VOLATILE_RESUME_QUERY_KEYS or normalized.startswith("x-amz-")


def _stable_resume_path(path: str) -> str:
    # Disney-style signed paths can prefix the real media path with a dvt2 token.
    marker = "/grn/"
    position = path.find(marker)
    if position > 0:
        return path[position:]
    return path


def _migrate_inline_init_resume_parts(
    stream: StreamInfo,
    output_path: Path,
    temp_dir: str | Path | None,
    target_dir: Path,
    part_paths: list[Path],
) -> None:
    segments = _segment_urls(stream)
    if not segments or not any(segment.index == -1 and segment.data is not None for segment in segments):
        return
    if any(path.exists() for path in part_paths):
        return
    legacy_segments = [segment for segment in segments if not (segment.index == -1 and segment.data is not None)]
    if len(legacy_segments) == len(segments):
        return
    legacy_dir = _resume_temp_dir(
        stream,
        output_path,
        temp_dir,
        segments=legacy_segments,
        search_fallback=False,
    )
    if legacy_dir == target_dir or not legacy_dir.exists():
        return

    media_index = 0
    migrated = False
    for index, segment in enumerate(segments):
        target = part_paths[index]
        if segment.index == -1 and segment.data is not None:
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(segment.data)
            tmp.replace(target)
            migrated = True
            continue
        source = legacy_dir / f"{media_index:08d}.part"
        media_index += 1
        if not source.exists():
            continue
        shutil.copyfile(source, target)
        migrated = True
    if migrated:
        legacy_manifest = legacy_dir / "resume.json"
        if legacy_manifest.exists():
            shutil.copyfile(legacy_manifest, target_dir / "resume.legacy.json")


def _write_resume_manifest(temp_root: Path, stream: StreamInfo, output_path: Path, total_segments: int) -> None:
    manifest = {
        "output": str(output_path),
        "stream": stream.format_line(),
        "identity": _stream_resume_identity(stream),
        "original_url": stream.original_url,
        "total_segments": total_segments,
        "updated_at": int(time.time()),
    }
    marker = temp_root / "resume.json"
    temporary = marker.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(marker)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def _cached_part_size(path: Path, segment: SegmentInfo | None = None) -> int | None:
    try:
        if not path.exists():
            return None
        size = path.stat().st_size
        if size <= 0:
            return None
        if not _cached_part_is_complete(path, size, segment):
            path.unlink(missing_ok=True)
            return None
        return size
    except OSError:
        return None
    return None


def _cached_part_is_complete(path: Path, size: int, segment: SegmentInfo | None = None) -> bool:
    if segment and segment.byte_range:
        start, end = segment.byte_range
        if size != end - start + 1:
            return False
        if _segment_uses_bbts(segment):
            return bbts_part_is_complete(path, size)
        return True
    if segment and _segment_uses_bbts(segment):
        return bbts_part_is_complete(path, size)
    # iQ .amp4 HLS media URLs partition one top-level mdat across several files.
    if segment and segment.index != -1 and urlparse(segment.url).path.lower().endswith(".amp4"):
        return True
    expected_webm_size = _webm_declared_segment_size(path)
    if expected_webm_size is not None and size < expected_webm_size:
        return False
    mp4_complete = _mp4_part_is_complete(path, size)
    if mp4_complete is False:
        return False
    return True


def _segment_uses_bbts(segment: SegmentInfo) -> bool:
    scheme = (segment.encryption_scheme or "").upper().replace("-", "_")
    return scheme == "BBTS" or urlparse(segment.url).path.lower().endswith(".bbts")


_MP4_TOP_LEVEL_BOXES = {
    b"ftyp",
    b"styp",
    b"sidx",
    b"ssix",
    b"moov",
    b"moof",
    b"mdat",
    b"mfra",
    b"uuid",
    b"pssh",
    b"free",
    b"skip",
    b"wide",
    b"emsg",
    b"prft",
}


def _mp4_part_is_complete(path: Path, file_size: int) -> bool | None:
    try:
        with path.open("rb") as file:
            first = file.read(8)
            if len(first) < 8:
                return None
            first_type = first[4:8]
            if first_type not in _MP4_TOP_LEVEL_BOXES:
                return None
            position = 0
            header = first
            while True:
                if len(header) < 8:
                    return False
                box_size = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                header_size = 8
                if box_type not in _MP4_TOP_LEVEL_BOXES:
                    return False
                if box_size == 1:
                    extended = file.read(8)
                    if len(extended) < 8:
                        return False
                    box_size = int.from_bytes(extended, "big")
                    header_size = 16
                elif box_size == 0:
                    box_size = file_size - position
                if box_size < header_size:
                    return False
                next_position = position + box_size
                if next_position > file_size:
                    return False
                if next_position == file_size:
                    return True
                file.seek(next_position)
                header = file.read(8)
                position = next_position
    except OSError:
        return None


def _webm_declared_segment_size(path: Path) -> int | None:
    try:
        with path.open("rb") as file:
            header = file.read(16)
    except OSError:
        return None
    if not header:
        return None
    parsed_id = _read_ebml_id(header, 0)
    if parsed_id is None:
        return None
    element_id, id_length = parsed_id
    # DASH WebM media chunks usually start with a Cluster element. The init
    # segment commonly uses an unknown-size Segment, so only media chunks can be
    # validated cheaply here.
    if element_id != 0x1F43B675:
        return None
    parsed_size = _read_ebml_size(header, id_length)
    if parsed_size is None:
        return None
    value, size_length, unknown = parsed_size
    if unknown:
        return None
    return id_length + size_length + value


def _read_ebml_id(data: bytes, offset: int) -> tuple[int, int] | None:
    length = _ebml_vint_length(data, offset, max_len=4)
    if length is None or offset + length > len(data):
        return None
    return int.from_bytes(data[offset:offset + length], "big"), length


def _read_ebml_size(data: bytes, offset: int) -> tuple[int, int, bool] | None:
    length = _ebml_vint_length(data, offset, max_len=8)
    if length is None or offset + length > len(data):
        return None
    marker = 1 << (8 - length)
    value = data[offset] & (marker - 1)
    for byte in data[offset + 1:offset + length]:
        value = (value << 8) | byte
    return value, length, value == (1 << (7 * length)) - 1


def _ebml_vint_length(data: bytes, offset: int, max_len: int) -> int | None:
    if offset >= len(data):
        return None
    marker = 0x80
    first = data[offset]
    for length in range(1, max_len + 1):
        if first & marker:
            return length
        marker >>= 1
    return None


def _check_downloaded_parts_count(part_paths: list[Path], expected_count: int) -> None:
    actual_count = sum(1 for path in part_paths if path.exists())
    if actual_count != expected_count:
        raise DownloadError(f"Downloaded segment count mismatch: expected {expected_count}, got {actual_count}.")


def _emit_progress(
    progress: Callable[[ProgressUpdate], None] | None,
    stream: StreamInfo,
    completed_segments: int,
    total_segments: int,
    downloaded_bytes: int,
    total_bytes: int | None,
    start: float,
    done: bool = False,
) -> None:
    if not progress:
        return
    progress(
        ProgressUpdate(
            stream=stream,
            completed_segments=completed_segments,
            total_segments=total_segments,
            downloaded_bytes=downloaded_bytes,
            total_bytes=total_bytes,
            elapsed_seconds=max(0.001, time.monotonic() - start),
            done=done,
        )
    )


class _PartProgress:
    def __init__(
        self,
        callback: Callable[[ProgressUpdate], None],
        stream: StreamInfo,
        total_segments: int,
        total_bytes: int | None,
        start: float,
    ):
        self.callback = callback
        self.stream = stream
        self.total_segments = total_segments
        self.total_bytes = total_bytes
        self.start = start
        self._completed_bytes = 0
        self._active_bytes: dict[str, int] = {}
        self.completed_segments = 0
        self.last_emit = 0.0
        self.lock = threading.Lock()

    @property
    def total(self) -> int:
        with self.lock:
            return self._total_locked()

    def _total_locked(self) -> int:
        return self._completed_bytes + sum(self._active_bytes.values())

    def set_completed(self, completed_segments: int) -> None:
        with self.lock:
            self.completed_segments = max(self.completed_segments, min(self.total_segments, completed_segments))

    def add_cached(self, size: int) -> None:
        if size <= 0:
            return
        with self.lock:
            self._completed_bytes += size

    def remove_cached(self, size: int) -> None:
        """Undo a speculative partial-file contribution.

        A partial file is counted optimistically so a resumed delivery starts
        at its on-disk byte position.  If the server rejects the range and we
        have to discard that file, remove the contribution before retrying from
        zero; otherwise the progress line would permanently over-report bytes.
        """

        if size <= 0:
            return
        with self.lock:
            self._completed_bytes = max(0, self._completed_bytes - size)

    def add(self, size: int, part_id: str | None = None) -> None:
        if size <= 0:
            return
        now = time.monotonic()
        with self.lock:
            if part_id:
                self._active_bytes[part_id] = self._active_bytes.get(part_id, 0) + size
            else:
                self._completed_bytes += size
            downloaded = self._total_locked()
            completed = self.completed_segments
            if now - self.last_emit < 0.25:
                return
            self.last_emit = now
        _emit_progress(
            self.callback,
            self.stream,
            completed,
            self.total_segments,
            downloaded,
            self.total_bytes,
            self.start,
        )

    def complete_part(self, part_id: str) -> None:
        with self.lock:
            self._completed_bytes += self._active_bytes.pop(part_id, 0)

    def discard_part(self, part_id: str) -> None:
        with self.lock:
            self._active_bytes.pop(part_id, None)


def _part_progress_callback(
    progress: Callable[[ProgressUpdate], None] | None,
    stream: StreamInfo,
    total_segments: int,
    total_bytes: int | None,
    start: float,
) -> _PartProgress | None:
    if not progress:
        return None
    return _PartProgress(progress, stream, total_segments, total_bytes, start)


def _should_hedge_segment_downloads(stream: StreamInfo, segments: list[SegmentInfo], workers: int) -> bool:
    if workers <= 1 or len(segments) <= 1:
        return False
    return stream.media_type == "video" and any(_is_apple_hls_video_segment(segment) for segment in segments)


def _download_parts_with_hedging(
    *,
    urls: list[SegmentInfo],
    part_paths: list[Path],
    headers: dict[str, str] | None,
    retries: int,
    limiter: RateLimiter | None,
    hls_crypto: HlsCrypto | None,
    request_timeout: int,
    prefer_curl: bool,
    host_limiter: _HostAdaptiveLimiter | None,
    pool: ThreadPoolExecutor,
    progress: Callable[[ProgressUpdate], None] | None,
    progress_bytes: _PartProgress | None,
    stream: StreamInfo,
    total_bytes: int | None,
    start: float,
    completed: int,
    downloaded_bytes: int,
    download_part: Callable[..., int] | None = None,
) -> tuple[int, int]:
    max_workers = max(1, getattr(pool, "_max_workers", 1))
    futures: dict[object, tuple[int, Path, float, bool]] = {}
    completed_indices: set[int] = set()
    hedged_indices: set[int] = set()
    original_started: dict[int, float] = {}
    original_durations: list[float] = []
    attempts = 0
    _cleanup_hedged_attempt_files(part_paths)
    fetch = download_part or _download_part

    def submit_attempt(index: int, hedge: bool = False) -> None:
        nonlocal attempts
        attempts += 1
        attempt_path = part_paths[index].with_name(f"{part_paths[index].name}.attempt{attempts:04d}")
        started = time.monotonic()
        if not hedge:
            original_started[index] = started
        future = pool.submit(
            fetch,
            urls[index],
            attempt_path,
            headers,
            retries,
            limiter,
            hls_crypto,
            request_timeout,
            prefer_curl,
            progress_bytes=None,
            host_limiter=host_limiter,
            claimed=lambda index=index: index in completed_indices,
        )
        futures[future] = (index, attempt_path, started, hedge)

    for index, segment in enumerate(urls):
        cached_size = _cached_part_size(part_paths[index], segment)
        if cached_size is not None:
            completed_indices.add(index)
            completed += 1
            if progress_bytes is None:
                downloaded_bytes += cached_size
            else:
                progress_bytes.add_cached(cached_size)
                progress_bytes.set_completed(completed)
                downloaded_bytes = progress_bytes.total
            _emit_progress(progress, stream, completed, len(urls), downloaded_bytes, total_bytes, start)
            continue
        submit_attempt(index)

    try:
        while futures:
            _submit_slow_hedges(
                futures=futures,
                original_started=original_started,
                original_durations=original_durations,
                completed_indices=completed_indices,
                hedged_indices=hedged_indices,
                max_workers=max_workers,
                submit_attempt=submit_attempt,
            )
            done, _pending = wait(futures, timeout=0.25, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                index, attempt_path, attempt_started, hedge = futures.pop(future)
                try:
                    size = future.result()
                except DownloadError:
                    attempt_path.unlink(missing_ok=True)
                    if index in completed_indices:
                        continue
                    raise
                except DownloadCancelled:
                    raise
                except Exception as exc:
                    attempt_path.unlink(missing_ok=True)
                    if index in completed_indices:
                        continue
                    segment = urls[index]
                    raise DownloadError(
                        f"Failed to download segment {_segment_label(segment)}: {_error_text(exc)}",
                        url=segment.url,
                    ) from exc
                if index in completed_indices:
                    attempt_path.unlink(missing_ok=True)
                    continue
                completed_indices.add(index)
                if not hedge:
                    original_durations.append(max(0.001, time.monotonic() - attempt_started))
                if attempt_path != part_paths[index]:
                    attempt_path.replace(part_paths[index])
                completed += 1
                if progress_bytes is None:
                    downloaded_bytes += size
                else:
                    progress_bytes.add_cached(size)
                    progress_bytes.set_completed(completed)
                    downloaded_bytes = progress_bytes.total
                _emit_progress(progress, stream, completed, len(urls), downloaded_bytes, total_bytes, start)
                if len(completed_indices) == len(urls):
                    _drain_hedged_losers(futures, timeout=1.5)
                    _cleanup_hedged_attempt_files(part_paths)
                    return completed, downloaded_bytes
    finally:
        for _future, (_index, attempt_path, _started, _hedge) in list(futures.items()):
            attempt_path.unlink(missing_ok=True)

    return completed, downloaded_bytes


def _drain_hedged_losers(futures: dict[object, tuple[int, Path, float, bool]], timeout: float) -> None:
    if not futures:
        return
    done, _pending = wait(futures, timeout=max(0.0, timeout), return_when=FIRST_COMPLETED)
    for future in done:
        _index, attempt_path, _started, _hedge = futures.pop(future)
        try:
            future.result()
        except Exception:
            pass
        attempt_path.unlink(missing_ok=True)


def _cleanup_hedged_attempt_files(part_paths: list[Path]) -> None:
    parents = {path.parent for path in part_paths}
    for parent in parents:
        try:
            candidates = list(parent.glob("*.part.attempt*"))
        except OSError:
            continue
        for path in candidates:
            try:
                path.unlink()
            except OSError:
                pass


def _submit_slow_hedges(
    *,
    futures: dict[object, tuple[int, Path, float, bool]],
    original_started: dict[int, float],
    original_durations: list[float],
    completed_indices: set[int],
    hedged_indices: set[int],
    max_workers: int,
    submit_attempt: Callable[[int, bool], None],
) -> None:
    if not original_durations or len(futures) >= max_workers:
        return
    threshold = max(_HEDGED_DOWNLOAD_MIN_WAIT, _HEDGED_DOWNLOAD_FACTOR * _median(original_durations))
    now = time.monotonic()
    active_indices = {index for index, _path, _started, _hedge in futures.values()}
    spare = max_workers - len(futures)
    for index, started in sorted(original_started.items(), key=lambda item: item[1]):
        if spare <= 0:
            break
        if index in completed_indices or index in hedged_indices or index not in active_indices:
            continue
        if now - started < threshold:
            continue
        hedged_indices.add(index)
        submit_attempt(index, True)
        spare -= 1


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    midpoint = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[midpoint]
    return (sorted_values[midpoint - 1] + sorted_values[midpoint]) / 2


def _should_retry_failed_parallel_part_serially(segment: SegmentInfo, exc: BaseException | None) -> bool:
    if not is_url(segment.url) or _is_declared_hls_gap_segment(segment):
        return False
    if _is_skippable_missing_hls_boundary_segment(segment, exc):
        return False
    return _is_adaptive_retryable_error(exc) or _is_ssl_transport_error(exc)


def _retry_failed_parallel_part_serially(
    segment: SegmentInfo,
    out: Path,
    headers: dict[str, str] | None,
    retries: int,
    limiter: RateLimiter | None,
    hls_crypto: HlsCrypto | None,
    request_timeout: int,
    prefer_curl: bool,
    progress_bytes: _PartProgress | None,
    host_limiter: _HostAdaptiveLimiter | None,
    gate: Callable[[], None] | None = None,
) -> int:
    if progress_bytes:
        progress_bytes.discard_part(str(out))
    extra_retries = max(2, int(retries or 1))
    _runtime_sleep(_retry_delay(0))
    if gate is not None:
        gate()
    return _download_part(
        segment,
        out,
        headers,
        extra_retries,
        limiter,
        hls_crypto,
        request_timeout,
        prefer_curl,
        progress_bytes=progress_bytes,
        host_limiter=host_limiter,
    )


class RateLimiter:
    def __init__(self, bytes_per_second: int):
        self.bytes_per_second = max(1, bytes_per_second)
        self.started = time.monotonic()
        self.consumed = 0
        self.lock = threading.Lock()

    def consume(self, size: int) -> None:
        if size <= 0:
            return
        with self.lock:
            self.consumed += size
            target_elapsed = self.consumed / self.bytes_per_second
            sleep_for = self.started + target_elapsed - time.monotonic()
        if sleep_for > 0:
            _runtime_sleep(sleep_for)


class _HostAdaptiveLimiter:
    def __init__(self, max_parallel: int, initial_parallel: int | None = None):
        self.max_parallel = max(1, int(max_parallel or 1))
        self.initial_parallel = max(1, min(self.max_parallel, int(initial_parallel or self.max_parallel)))
        self._states: dict[str, _HostAdaptiveState] = {}
        self._lock = threading.Lock()

    def acquire(self, url: str) -> _HostPermit | None:
        key = _adaptive_host_key(url)
        if not key:
            return None
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = _HostAdaptiveState(self.max_parallel, self.initial_parallel)
                self._states[key] = state
        state.acquire()
        return _HostPermit(state)


class _HostAdaptiveState:
    def __init__(self, max_parallel: int, initial_parallel: int | None = None):
        self.max_parallel = max(1, max_parallel)
        self.limit = max(1, min(self.max_parallel, int(initial_parallel or self.max_parallel)))
        self.active = 0
        self.failure_streak = 0
        self.success_streak = 0
        self.cooldown_until = 0.0
        self.condition = threading.Condition()

    def acquire(self) -> None:
        with self.condition:
            while True:
                now = time.monotonic()
                wait_for = self.cooldown_until - now
                if wait_for > 0:
                    self.condition.wait(min(wait_for, 1.0))
                    continue
                if self.active < max(1, min(self.limit, self.max_parallel)):
                    self.active += 1
                    return
                self.condition.wait(0.1)

    def release_success(self) -> None:
        with self.condition:
            self.active = max(0, self.active - 1)
            self.success_streak += 1
            if self.success_streak >= max(4, self.limit):
                self.success_streak = 0
                self.failure_streak = 0
                self.limit = min(self.max_parallel, self.limit + 1)
                self.cooldown_until = 0.0
            self.condition.notify_all()

    def release_failure(self, retryable: bool) -> None:
        with self.condition:
            self.active = max(0, self.active - 1)
            if retryable:
                self.failure_streak = min(self.failure_streak + 1, 6)
                self.success_streak = 0
                if self.limit > 1:
                    self.limit = max(1, min(self.limit - 1, self.limit // 2))
                cooldown = min(0.5 * (2 ** (self.failure_streak - 1)), 6.0)
                self.cooldown_until = max(self.cooldown_until, time.monotonic() + cooldown)
            self.condition.notify_all()


class _HostPermit:
    def __init__(self, state: _HostAdaptiveState):
        self.state = state
        self.closed = False

    def success(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.state.release_success()

    def failure(self, retryable: bool) -> None:
        if self.closed:
            return
        self.closed = True
        self.state.release_failure(retryable)


def _adaptive_host_key(url: str) -> str | None:
    if not is_url(url):
        return None
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}:{port}"


def _is_adaptive_retryable_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    text = _error_text(exc).lower()
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
        "ssl",
        "tls",
        "record layer failure",
        "unexpected_eof",
        "unexpected eof",
        "eof occurred",
        "eof while reading",
        "incomplete segment",
        "got ",
    )
    if "range request was not honored" in text:
        return False
    if isinstance(exc, HttpClientError):
        return any(token in text for token in retry_tokens)
    if isinstance(exc, (TimeoutError, URLError, http.client.HTTPException, ssl.SSLError)):
        return True
    return any(token in text for token in retry_tokens)


def _is_ssl_transport_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, ssl.SSLError):
        return True
    text = _error_text(exc).lower()
    ssl_tokens = (
        "ssl",
        "tls",
        "record layer failure",
        "unexpected_eof",
        "unexpected eof",
        "eof occurred",
        "eof while reading",
        "_ssl.c",
    )
    return any(token in text for token in ssl_tokens)


def _download_part(
    segment: SegmentInfo,
    out: Path,
    headers: dict[str, str] | None,
    retries: int,
    limiter: RateLimiter | None = None,
    hls_crypto: HlsCrypto | None = None,
    request_timeout: int = 30,
    prefer_curl: bool = False,
    progress_bytes: _PartProgress | None = None,
    host_limiter: _HostAdaptiveLimiter | None = None,
    claimed: Callable[[], bool] | None = None,
) -> int:
    _runtime_checkpoint()
    if _is_declared_hls_gap_segment(segment):
        return _write_empty_part(out, progress_bytes=progress_bytes, part_id=str(out))
    if _can_stream_part(segment, hls_crypto):
        return _stream_part_to_file(segment, out, headers=headers, retries=retries, limiter=limiter, request_timeout=request_timeout, prefer_curl=prefer_curl, progress_bytes=progress_bytes, host_limiter=host_limiter, claimed=claimed)
    try:
        data = _fetch_bytes(segment, headers=headers, retries=retries, limiter=limiter, hls_crypto=hls_crypto, request_timeout=request_timeout, prefer_curl=prefer_curl, host_limiter=host_limiter)
    except DownloadError as exc:
        if _is_skippable_missing_hls_boundary_segment(segment, exc):
            return _write_empty_part(out, progress_bytes=progress_bytes, part_id=str(out))
        raise
    tmp_out = out.with_suffix(out.suffix + ".tmp")
    tmp_out.write_bytes(data)
    if not _cached_part_is_complete(tmp_out, len(data), segment):
        tmp_out.unlink(missing_ok=True)
        raise DownloadError(f"Incomplete segment {_segment_label(segment)}: got {len(data)} bytes.", url=segment.url)
    tmp_out.replace(out)
    if progress_bytes:
        progress_bytes.add(len(data))
    return len(data)


def _can_stream_part(segment: SegmentInfo, hls_crypto: HlsCrypto | None) -> bool:
    if segment.data is not None:
        return False
    if _segment_may_return_text_url_redirect(segment):
        return False
    segment_method = _hls_method(segment.encryption_scheme)
    if segment.encrypted and segment_method in _HLS_SEGMENT_CIPHER_METHODS:
        return False
    if getattr(segment, "key_uri", None) and segment_method in _HLS_SEGMENT_CIPHER_METHODS:
        return False
    return not (hls_crypto and (hls_crypto.key or hls_crypto.decryptor))


def _stream_part_to_file(
    segment: SegmentInfo,
    out: Path,
    headers: dict[str, str] | None,
    retries: int,
    limiter: RateLimiter | None = None,
    request_timeout: int = 30,
    prefer_curl: bool = False,
    progress_bytes: _PartProgress | None = None,
    host_limiter: _HostAdaptiveLimiter | None = None,
    claimed: Callable[[], bool] | None = None,
) -> int:
    _runtime_checkpoint()
    tmp_out = out.with_suffix(out.suffix + ".tmp")
    tmp_out.parent.mkdir(parents=True, exist_ok=True)
    part_id = str(out)
    if not is_url(segment.url):
        return _stream_local_part_to_file(segment, tmp_out, out, limiter, progress_bytes, part_id=part_id)

    base_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
    base_headers.update(headers or {})

    last_error: Exception | None = None
    if prefer_curl:
        permit = host_limiter.acquire(segment.url) if host_limiter else None
        catchup = is_yangshipin_catchup_cdn_url(segment.url)
        try:
            request_headers = _segment_request_headers(segment, base_headers)
            if catchup:
                size = _curl_download_yangshipin_catchup_part(
                    segment,
                    tmp_out,
                    headers=request_headers,
                    retries=retries,
                    request_timeout=request_timeout,
                    limiter=limiter,
                    progress_bytes=progress_bytes,
                    part_id=part_id,
                )
            else:
                size = _curl_download_to_file(segment, tmp_out, headers=request_headers, retries=retries, request_timeout=request_timeout, limiter=limiter)
            if not _cached_part_is_complete(tmp_out, size, segment):
                raise DownloadError(f"Incomplete segment {_segment_label(segment)}: got {size} bytes.", url=segment.url)
            if progress_bytes:
                if catchup:
                    progress_bytes.complete_part(part_id)
                else:
                    progress_bytes.add(size)
            tmp_out.replace(out)
            if permit:
                permit.success()
            return size
        except DownloadError as exc:
            last_error = exc
            if permit:
                permit.failure(_is_adaptive_retryable_error(exc))
            if catchup:
                raise
            try:
                tmp_out.unlink()
            except OSError:
                pass

    plain_query_fallback = False
    # ``resume_supported`` records whether the origin accepted the latest range
    # probe for an ordinary direct file.  A missing/unsupported partial is
    # always restarted from byte zero; it is never appended blindly.
    resume_supported: bool | None = None
    resume_total: int | None = None
    partial_seeded = 0
    # A pre-existing direct-file prefix may need one extra attempt: the first
    # request can discover that a redirect target does not honour Range, after
    # which we deliberately retry once from byte zero.
    attempt_count = max(1, retries) + (
        1 if segment.allow_range_status_200 or (not segment.byte_range and tmp_out.exists()) else 0
    )
    for attempt in range(attempt_count):
        if claimed and claimed():
            raise DownloadError("download superseded by another attempt", url=segment.url)
        if plain_query_fallback:
            existing_size = 0
        elif segment.byte_range:
            existing_size = _partial_range_size(tmp_out, segment)
        elif not segment.byte_range:
            existing_size, resume_total, resume_supported = _partial_remote_resume_size(
                tmp_out,
                segment,
                headers=base_headers,
                request_timeout=request_timeout,
            )
            if not existing_size:
                # Keep a stale/unsupported partial only until the probe has had
                # a chance to make the decision.  Never append to an origin
                # which ignored the Range capability check.
                resume_supported = False
                try:
                    tmp_out.unlink()
                except OSError:
                    pass
                if partial_seeded and progress_bytes:
                    progress_bytes.remove_cached(partial_seeded)
                partial_seeded = 0
        if existing_size > partial_seeded and progress_bytes:
            progress_bytes.add_cached(existing_size - partial_seeded)
            partial_seeded = existing_size
        expected_range_size = _segment_range_size(segment)
        if (
            not segment.byte_range
            and existing_size > 0
            and resume_total is not None
            and existing_size >= resume_total
            and _cached_part_is_complete(tmp_out, existing_size, segment)
        ):
            tmp_out.replace(out)
            if progress_bytes:
                progress_bytes.complete_part(part_id)
            return existing_size
        if plain_query_fallback:
            if progress_bytes:
                progress_bytes.discard_part(part_id)
                if partial_seeded:
                    progress_bytes.remove_cached(partial_seeded)
            partial_seeded = 0
            try:
                tmp_out.unlink()
            except OSError:
                pass
        if expected_range_size is not None and existing_size >= expected_range_size:
            if _cached_part_is_complete(tmp_out, existing_size, segment):
                tmp_out.replace(out)
                if progress_bytes:
                    progress_bytes.complete_part(part_id)
                return existing_size
            existing_size = 0
            if progress_bytes:
                progress_bytes.discard_part(part_id)
                if partial_seeded:
                    progress_bytes.remove_cached(partial_seeded)
            partial_seeded = 0
            try:
                tmp_out.unlink()
            except OSError:
                pass
        request_headers = (
            dict(base_headers)
            if plain_query_fallback
            else _segment_request_headers(segment, base_headers, resume_offset=existing_size)
        )
        mode = "ab" if existing_size else "wb"
        permit = host_limiter.acquire(segment.url) if host_limiter else None
        try:
            with tmp_out.open(mode, buffering=_STREAM_COPY_CHUNK_SIZE) as file:
                low_speed_limit, low_speed_time, low_speed_min_bytes = _native_low_speed_policy(segment)
                progress_callback = (
                    (lambda chunk_size: progress_bytes.add(chunk_size, part_id=part_id))
                    if progress_bytes and not plain_query_fallback
                    else None
                )
                if _should_use_native_http2(segment):
                    size, response = http2_download_to_file(
                        segment.url,
                        file,
                        headers=request_headers,
                        timeout=request_timeout,
                        chunk_size=_native_read_chunk_size(segment),
                        limiter=limiter,
                        progress=progress_callback,
                        low_speed_limit=low_speed_limit,
                        low_speed_time=low_speed_time,
                        low_speed_min_bytes=low_speed_min_bytes,
                        should_stop=claimed,
                    )
                else:
                    size, response = get_global_http_client().download_to_file(
                        segment.url,
                        file,
                        headers=request_headers,
                        timeout=request_timeout,
                        chunk_size=_native_read_chunk_size(segment),
                        limiter=limiter,
                        progress=progress_callback,
                        low_speed_limit=low_speed_limit,
                        low_speed_time=low_speed_time,
                        low_speed_min_bytes=low_speed_min_bytes,
                        should_stop=claimed,
                        allow_range_status_200=segment.allow_range_status_200 and segment.byte_range is not None,
                    )
            if plain_query_fallback and expected_range_size is not None:
                received_size = tmp_out.stat().st_size if tmp_out.exists() else size
                if received_size < expected_range_size:
                    raise DownloadError(
                        f"Incomplete segment {_segment_label(segment)}: got "
                        f"{received_size} of {expected_range_size} bytes.",
                        url=segment.url,
                    )
                if received_size > expected_range_size:
                    with tmp_out.open("r+b") as file:
                        file.truncate(expected_range_size)
                size = expected_range_size
                if progress_bytes:
                    progress_bytes.add(size, part_id=part_id)
                expected_size = expected_range_size
            else:
                expected_size = _expected_response_size(response) if response is not None else size
            if expected_size is not None and size != expected_size:
                raise DownloadError(f"Incomplete segment {_segment_label(segment)}: got {size} of {expected_size} bytes.", url=segment.url)
            total_size = tmp_out.stat().st_size if tmp_out.exists() else existing_size + size
            if not _cached_part_is_complete(tmp_out, total_size, segment):
                raise DownloadError(f"Incomplete segment {_segment_label(segment)}: got {total_size} bytes.", url=segment.url)
            tmp_out.replace(out)
            if progress_bytes:
                progress_bytes.complete_part(part_id)
            if permit:
                permit.success()
            return total_size
        except (HTTPError, URLError, TimeoutError, OSError, http.client.HTTPException, HttpClientError, DownloadError) as exc:
            last_error = exc
            if permit:
                permit.failure(_is_adaptive_retryable_error(exc))
            invalid_query_bbts = (
                segment.allow_range_status_200
                and not plain_query_fallback
                and _segment_uses_bbts(segment)
                and tmp_out.exists()
                and not bbts_part_is_complete(tmp_out)
            )
            if (
                segment.allow_range_status_200
                and not plain_query_fallback
                and (_is_range_not_satisfiable_error(exc) or invalid_query_bbts)
            ):
                plain_query_fallback = True
                if progress_bytes:
                    progress_bytes.discard_part(part_id)
                try:
                    tmp_out.unlink()
                except OSError:
                    pass
                continue
            if (
                resume_supported
                and existing_size > 0
                and not segment.byte_range
                and (_is_range_not_honored_error(exc) or _is_range_not_satisfiable_error(exc))
            ):
                # The probe succeeded but this request was redirected to an
                # origin which did not honour Range.  Drop the prefix and retry
                # once from zero rather than risking duplicated media bytes.
                resume_supported = False
                if progress_bytes:
                    progress_bytes.discard_part(part_id)
                    if partial_seeded:
                        progress_bytes.remove_cached(partial_seeded)
                partial_seeded = 0
                try:
                    tmp_out.unlink()
                except OSError:
                    pass
                continue
            # Any bytes reported by the active response are now represented by
            # the on-disk temporary.  Remove the in-flight contribution before
            # the next attempt seeds that file size, otherwise a retry would
            # count the same prefix twice.
            if progress_bytes:
                progress_bytes.discard_part(part_id)
            if not _keep_partial_range_tmp(
                tmp_out,
                segment,
                allow_non_range=not segment.byte_range,
            ):
                if progress_bytes:
                    if partial_seeded:
                        progress_bytes.remove_cached(partial_seeded)
                partial_seeded = 0
                try:
                    tmp_out.unlink()
                except OSError:
                    pass
            if attempt + 1 < attempt_count:
                _runtime_sleep(_retry_delay(attempt))
        except BaseException:
            if permit:
                permit.failure(False)
            raise
    if _should_try_curl_fallback(segment, last_error):
        try:
            if progress_bytes:
                progress_bytes.discard_part(part_id)
            try:
                tmp_out.unlink()
            except OSError:
                pass
            request_headers = _segment_request_headers(segment, base_headers)
            size = _curl_download_to_file(segment, tmp_out, headers=request_headers, retries=retries, request_timeout=request_timeout, limiter=limiter)
            if not _cached_part_is_complete(tmp_out, size, segment):
                raise DownloadError(f"Incomplete segment {_segment_label(segment)}: got {size} bytes.", url=segment.url)
            if progress_bytes:
                progress_bytes.add(size)
            tmp_out.replace(out)
            return size
        except DownloadError as exc:
            last_error = exc
            try:
                tmp_out.unlink()
            except OSError:
                pass
    if _is_skippable_missing_hls_boundary_segment(segment, last_error):
        return _write_empty_part(out, progress_bytes=progress_bytes, part_id=part_id)
    if progress_bytes:
        progress_bytes.discard_part(part_id)
    raise DownloadError(
        f"Failed to download segment {_segment_label(segment)} after {max(1, retries)} attempts: {_error_text(last_error)}",
        url=segment.url,
    )


def _write_empty_part(out: Path, progress_bytes: _PartProgress | None = None, part_id: str | None = None) -> int:
    tmp_out = out.with_suffix(out.suffix + ".tmp")
    tmp_out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out.write_bytes(b"")
    tmp_out.replace(out)
    if progress_bytes and part_id:
        progress_bytes.complete_part(part_id)
    return 0


def _content_length_from_headers(headers: dict[str, str] | None) -> int | None:
    if not headers:
        return None
    value = headers.get("Content-Length") or headers.get("content-length")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _is_declared_hls_gap_segment(segment: SegmentInfo) -> bool:
    return bool(getattr(segment, "gap", False))


def _is_skippable_missing_hls_boundary_segment(segment: SegmentInfo, exc: BaseException | None) -> bool:
    if _is_declared_hls_gap_segment(segment):
        return True
    if not _is_missing_http_error(exc):
        return False
    if not getattr(segment, "discontinuity_after", False):
        return False
    duration = segment.duration
    return duration is not None and duration <= 0.5


def _is_missing_http_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, HTTPError):
        return exc.code in {404, 410}
    text = _error_text(exc).lower()
    return "http error 404" in text or "http error 410" in text or "404: not found" in text or "410: gone" in text


def _is_range_not_satisfiable_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, HTTPError):
        return exc.code == 416
    text = _error_text(exc).lower()
    return "http error 416" in text or "range not satisfiable" in text


def _is_range_not_honored_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    text = _error_text(exc).lower()
    return "range request was not honored" in text or "range was not honored" in text


def _segment_request_headers(segment: SegmentInfo, base_headers: dict[str, str], resume_offset: int = 0) -> dict[str, str]:
    request_headers = dict(base_headers)
    if segment.byte_range:
        start, end = segment.byte_range
        start += max(0, resume_offset)
        request_headers["Range"] = f"bytes={start}-{end}"
    elif resume_offset > 0:
        # Ordinary direct files do not have a manifest byte range, but a
        # previous interrupted response can still be resumed when the origin
        # explicitly honours HTTP Range.  The caller probes that capability
        # before passing this offset.
        request_headers["Range"] = f"bytes={resume_offset}-"
    return request_headers


def _segment_range_size(segment: SegmentInfo) -> int | None:
    if not segment.byte_range:
        return None
    start, end = segment.byte_range
    if end < start:
        return None
    return end - start + 1


def _partial_range_size(path: Path, segment: SegmentInfo) -> int:
    expected_size = _segment_range_size(segment)
    if expected_size is None:
        return 0
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= 0:
        return 0
    if size > expected_size:
        try:
            path.unlink()
        except OSError:
            pass
        return 0
    return size


def _partial_remote_resume_size(
    path: Path,
    segment: SegmentInfo,
    *,
    headers: dict[str, str] | None,
    request_timeout: int,
) -> tuple[int, int | None, bool]:
    """Return a safe resume offset for a non-range segment.

    Direct media URLs are often represented as one segment.  They used to lose
    their ``.tmp`` file on cancellation and consequently restarted at byte 0.
    Probe the origin only when a partial file exists; appending is enabled only
    after a real ``206`` response is observed by the probe.  The total size is
    returned when the server supplied it so a complete temporary file can be
    promoted without another full request.
    """

    try:
        size = path.stat().st_size
    except OSError:
        return 0, None, False
    if size <= 0 or not is_url(segment.url):
        return 0, None, False
    probe = _probe_range_support(segment.url, headers=headers, timeout=request_timeout)
    if not probe.supported:
        return 0, probe.size, False
    if probe.size is not None and size > probe.size:
        return 0, probe.size, False
    return size, probe.size, True


def _keep_partial_range_tmp(path: Path, segment: SegmentInfo, *, allow_non_range: bool = False) -> bool:
    expected_size = _segment_range_size(segment)
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if expected_size is None:
        # A non-range temporary is retained for a later capability probe.  It
        # is never appended blindly: the next attempt first verifies that the
        # origin supports Range and deletes it when it does not.
        return bool(allow_non_range and size > 0)
    return 0 < size < expected_size


def _stream_local_part_to_file(segment: SegmentInfo, tmp_out: Path, out: Path, limiter: RateLimiter | None, progress_bytes: _PartProgress | None = None, part_id: str | None = None) -> int:
    path = source_path(segment.url)
    try:
        with path.open("rb") as source, tmp_out.open("wb", buffering=_STREAM_COPY_CHUNK_SIZE) as target:
            if segment.byte_range:
                start, end = segment.byte_range
                source.seek(start)
                size = _copy_limited_stream(source, target, end - start + 1, limiter, progress_bytes, part_id=part_id)
            else:
                size = _copy_stream(source, target, limiter, progress_bytes, part_id=part_id)
        tmp_out.replace(out)
        if progress_bytes and part_id:
            progress_bytes.complete_part(part_id)
        return size
    except OSError as exc:
        if progress_bytes and part_id:
            progress_bytes.discard_part(part_id)
        try:
            tmp_out.unlink()
        except OSError:
            pass
        raise DownloadError(f"Failed to read segment {_segment_label(segment)}: {_error_text(exc)}", url=segment.url) from exc


def _copy_stream(source, target, limiter: RateLimiter | None, progress_bytes: _PartProgress | None = None, part_id: str | None = None) -> int:
    total = 0
    while True:
        chunk = source.read(_STREAM_COPY_CHUNK_SIZE)
        if not chunk:
            break
        if limiter:
            limiter.consume(len(chunk))
        target.write(chunk)
        total += len(chunk)
        if progress_bytes:
            progress_bytes.add(len(chunk), part_id=part_id)
    return total


def _expected_response_size(response) -> int | None:
    content_range_size = _content_range_response_size(response.headers.get("Content-Range") or response.headers.get("content-range"))
    if content_range_size is not None:
        return content_range_size
    content_length = response.headers.get("Content-Length")
    if content_length and content_length.isdigit():
        return int(content_length)
    return None


def _content_range_response_size(value: str | None) -> int | None:
    if not value:
        return None
    try:
        unit, range_and_total = value.strip().split(None, 1)
        if unit.lower() != "bytes":
            return None
        range_text = range_and_total.split("/", 1)[0]
        start_text, end_text = range_text.split("-", 1)
        start = int(start_text)
        end = int(end_text)
    except (ValueError, TypeError):
        return None
    if end < start:
        return None
    return end - start + 1


def _copy_limited_stream(source, target, length: int, limiter: RateLimiter | None, progress_bytes: _PartProgress | None = None, part_id: str | None = None) -> int:
    total = 0
    remaining = max(0, length)
    while remaining:
        chunk = source.read(min(_STREAM_COPY_CHUNK_SIZE, remaining))
        if not chunk:
            break
        if limiter:
            limiter.consume(len(chunk))
        target.write(chunk)
        total += len(chunk)
        remaining -= len(chunk)
        if progress_bytes:
            progress_bytes.add(len(chunk), part_id=part_id)
    return total


def _fetch_bytes(
    segment: SegmentInfo,
    headers: dict[str, str] | None,
    retries: int,
    limiter: RateLimiter | None = None,
    hls_crypto: HlsCrypto | None = None,
    request_timeout: int = 30,
    prefer_curl: bool = False,
    host_limiter: _HostAdaptiveLimiter | None = None,
) -> bytes:
    _runtime_checkpoint()
    if segment.data is not None:
        return _apply_hls_crypto(segment.data, segment, hls_crypto, headers=headers, retries=retries, request_timeout=request_timeout)

    if not is_url(segment.url):
        path = source_path(segment.url)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise DownloadError(f"Failed to read segment {_segment_label(segment)}: {_error_text(exc)}", url=segment.url) from exc
        if segment.byte_range:
            start, end = segment.byte_range
            return _apply_hls_crypto(data[start : end + 1], segment, hls_crypto, headers=headers, retries=retries, request_timeout=request_timeout)
        return _apply_hls_crypto(data, segment, hls_crypto, headers=headers, retries=retries, request_timeout=request_timeout)

    base_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
    base_headers.update(headers or {})
    current_segment = segment
    redirect_chain: list[str] = []

    last_error: Exception | None = None
    if prefer_curl:
        permit = host_limiter.acquire(current_segment.url) if host_limiter else None
        catchup = is_yangshipin_catchup_cdn_url(current_segment.url)
        try:
            request_headers = _segment_request_headers(current_segment, base_headers)
            data = _curl_fetch_bytes(current_segment, headers=request_headers, retries=retries, request_timeout=request_timeout, limiter=limiter)
            if permit:
                permit.success()
            return _apply_hls_crypto(data, current_segment, hls_crypto, headers=request_headers, retries=retries, request_timeout=request_timeout)
        except DownloadError as exc:
            last_error = exc
            if permit:
                permit.failure(_is_adaptive_retryable_error(exc))
            if catchup:
                raise

    for attempt in range(max(1, retries)):
        while True:
            request_headers = _segment_request_headers(current_segment, base_headers)
            permit = host_limiter.acquire(current_segment.url) if host_limiter else None
            try:
                data, response = _native_fetch_bytes(current_segment.url, request_headers, request_timeout, limiter)
                redirect_url = (
                    _text_url_redirect_from_bytes(data, response.headers, base_url=current_segment.url)
                    if _segment_may_return_text_url_redirect(current_segment)
                    else None
                )
                if redirect_url:
                    if permit:
                        permit.success()
                    current_segment = _follow_text_url_redirect(current_segment, redirect_url, redirect_chain)
                    continue
                if (
                    current_segment.byte_range
                    and response.status != 206
                    and not (current_segment.allow_range_status_200 and response.status == 200)
                ):
                    raise DownloadError(f"Range request was not honored: HTTP {response.status} {response.reason}", url=current_segment.url)
                expected_size = _expected_response_size(response)
                if expected_size is not None and len(data) != expected_size:
                    raise DownloadError(f"Incomplete segment {_segment_label(current_segment)}: got {len(data)} of {expected_size} bytes.", url=current_segment.url)
                try:
                    output = _apply_hls_crypto(data, current_segment, hls_crypto, headers=request_headers, retries=retries, request_timeout=request_timeout)
                    if permit:
                        permit.success()
                    return output
                except DownloadError as exc:
                    if not _is_retryable_hls_crypto_error(current_segment, exc):
                        raise
                    last_error = exc
                    if permit:
                        permit.failure(_is_adaptive_retryable_error(exc))
                    _runtime_checkpoint()
                    if attempt + 1 < retries:
                        _runtime_sleep(_retry_delay(attempt))
                        break
                    break
            except (HTTPError, URLError, TimeoutError, OSError, http.client.HTTPException, HttpClientError, DownloadError) as exc:
                last_error = exc
                if permit:
                    permit.failure(_is_adaptive_retryable_error(exc))
                _runtime_checkpoint()
                if attempt + 1 < retries:
                    _runtime_sleep(_retry_delay(attempt))
                break
            except BaseException:
                if permit:
                    permit.failure(False)
                raise
    if _should_try_curl_fallback(segment, last_error):
        request_headers = _segment_request_headers(current_segment, base_headers)
        data = _curl_fetch_bytes(current_segment, headers=request_headers, retries=retries, request_timeout=request_timeout, limiter=limiter)
        return _apply_hls_crypto(data, current_segment, hls_crypto, headers=request_headers, retries=retries, request_timeout=request_timeout)
    raise DownloadError(
        f"Failed to download segment {_segment_label(segment)} after {max(1, retries)} attempts: {_error_text(last_error)}",
        url=segment.url,
    )


def _should_try_curl_fallback(segment: SegmentInfo, exc: BaseException | None) -> bool:
    return False


def _native_low_speed_policy(segment: SegmentInfo) -> tuple[int | None, float | None, int]:
    if _is_apple_hls_video_segment(segment):
        return _APPLE_HLS_LOW_SPEED_LIMIT, _APPLE_HLS_LOW_SPEED_TIME, _APPLE_HLS_LOW_SPEED_MIN_BYTES
    return None, None, 0


def _native_read_chunk_size(segment: SegmentInfo) -> int:
    if _is_apple_hls_video_segment(segment):
        return _APPLE_HLS_READ_CHUNK_SIZE
    return _STREAM_COPY_CHUNK_SIZE


def _should_use_native_http2(segment: SegmentInfo) -> bool:
    return httpx_http2_available() and _is_apple_hls_video_segment(segment)


def _is_apple_hls_video_segment(segment: SegmentInfo) -> bool:
    if not is_url(segment.url):
        return False
    parsed = urlparse(segment.url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host != "hls.itunes.apple.com":
        return False
    path = parsed.path.lower()
    return "/itunes-assets/" in path and ("hlssportsvodvideo" in path or "_video_" in path)


def _native_fetch_bytes(
    url: str,
    headers: dict[str, str],
    request_timeout: int,
    limiter: RateLimiter | None,
):
    _runtime_checkpoint()
    with get_global_http_client().request("GET", url, headers=headers, timeout=request_timeout) as response:
        chunks: list[bytes] = []
        while True:
            _runtime_checkpoint()
            chunk = response.read(_STREAM_COPY_CHUNK_SIZE)
            if not chunk:
                break
            if limiter:
                limiter.consume(len(chunk))
            chunks.append(chunk)
        return b"".join(chunks), response


def _curl_fetch_bytes(
    segment: SegmentInfo,
    headers: dict[str, str] | None,
    retries: int,
    request_timeout: int,
    limiter: RateLimiter | None,
) -> bytes:
    result = _run_curl(segment, headers=headers, retries=retries, request_timeout=request_timeout, output_path=None)
    data = result.stdout
    if limiter:
        limiter.consume(len(data))
    return data


def _curl_download_to_file(
    segment: SegmentInfo,
    output_path: Path,
    headers: dict[str, str] | None,
    retries: int,
    request_timeout: int,
    limiter: RateLimiter | None,
    resume: bool = False,
) -> int:
    _run_curl(
        segment,
        headers=headers,
        retries=retries,
        request_timeout=request_timeout,
        output_path=output_path,
        resume=resume,
    )
    size = output_path.stat().st_size if output_path.exists() else 0
    if limiter:
        limiter.consume(size)
    return size


def _curl_download_yangshipin_catchup_part(
    segment: SegmentInfo,
    output_path: Path,
    *,
    headers: dict[str, str] | None,
    retries: int,
    request_timeout: int,
    limiter: RateLimiter | None,
    progress_bytes: _PartProgress | None = None,
    part_id: str | None = None,
) -> int:
    """Resume a replay TS part after this CDN closes a partial response."""
    last_error: DownloadError | None = None
    attempts = max(_YANGSHIPIN_CATCHUP_RESUME_ATTEMPTS, int(retries or 1))
    stalled_attempts = 0
    try:
        initial_size = output_path.stat().st_size
    except OSError:
        initial_size = 0
    if progress_bytes and initial_size:
        progress_bytes.add(initial_size, part_id=part_id)
    for attempt in range(attempts):
        _runtime_checkpoint()
        try:
            before_size = output_path.stat().st_size
        except OSError:
            before_size = 0
        try:
            resume = not segment.byte_range and output_path.exists() and output_path.stat().st_size > 0
            size = _curl_download_to_file(
                segment,
                output_path,
                headers=headers,
                retries=1,
                request_timeout=request_timeout,
                limiter=None,
                resume=resume,
            )
            added = max(0, size - before_size)
            if limiter and added:
                limiter.consume(added)
            if progress_bytes and added:
                progress_bytes.add(added, part_id=part_id)
            return size
        except DownloadError as exc:
            last_error = exc
            try:
                after_size = output_path.stat().st_size
            except OSError:
                after_size = 0
            added = max(0, after_size - before_size)
            if limiter and added:
                limiter.consume(added)
            if progress_bytes and added:
                progress_bytes.add(added, part_id=part_id)
            stalled_attempts = 0 if added else stalled_attempts + 1
            if stalled_attempts >= max(
                _YANGSHIPIN_CATCHUP_STALLED_ATTEMPTS,
                int(retries or 1),
            ):
                break
            if attempt + 1 < attempts:
                if added:
                    delay = _YANGSHIPIN_CATCHUP_PROGRESS_RETRY_DELAY
                else:
                    delay = min(1.0, 0.25 * stalled_attempts)
                _runtime_sleep(delay)
    raise last_error or DownloadError("Yangshipin catch-up segment download failed.", url=segment.url)


def _run_curl(
    segment: SegmentInfo,
    headers: dict[str, str] | None,
    retries: int,
    request_timeout: int,
    output_path: Path | None,
    resume: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    executable = shutil.which("curl")
    if not executable:
        raise DownloadError("curl fallback is unavailable.", url=segment.url)
    catchup = is_yangshipin_catchup_cdn_url(segment.url)
    args = [
        executable,
        "-L",
        "--fail",
        "--silent",
        "--show-error",
        *(["--noproxy", "*"] if catchup else []),
        "--max-time",
        str(min(max(1, request_timeout), 20) if catchup else max(1, request_timeout)),
        "--connect-timeout",
        str(min(max(1, request_timeout), 10) if catchup else max(1, request_timeout)),
        "--retry",
        str(max(0, retries - 1)),
        "--retry-delay",
        "1",
        "--retry-all-errors",
    ]
    if catchup:
        args.extend(["--speed-limit", "16384", "--speed-time", "10"])
    if resume:
        if output_path is None:
            raise DownloadError("curl resume needs a file target.", url=segment.url)
        args.extend(["--continue-at", "-"])
    if segment.byte_range:
        start, end = segment.byte_range
        args.extend(["-r", f"{start}-{end}"])
    for key, value in (headers or {}).items():
        if key.lower() in {"accept-encoding", "range"}:
            continue
        args.extend(["-H", f"{key}: {value}"])
    if output_path is not None:
        args.extend(["-o", str(output_path)])
    args.append(segment.url)
    result = managed_run(args, capture_output=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise DownloadError(f"curl fallback failed: {detail or f'exit {result.returncode}'}", url=segment.url)
    return result


def _apply_hls_crypto(
    data: bytes,
    segment: SegmentInfo,
    hls_crypto: HlsCrypto | None,
    headers: dict[str, str] | None = None,
    retries: int = 3,
    request_timeout: int = 30,
) -> bytes:
    if hls_crypto and _hls_method(hls_crypto.method) == "NONE":
        return data
    segment_method = _hls_method(segment.encryption_scheme)
    custom_method = _hls_method(hls_crypto.method) if hls_crypto and hls_crypto.method else None
    forced_custom = bool(custom_method and custom_method in _HLS_SEGMENT_CIPHER_METHODS and not segment.encryption_scheme)
    if segment_method in _HLS_SEGMENT_CIPHER_METHODS:
        method = custom_method if custom_method in _HLS_SEGMENT_CIPHER_METHODS else segment_method
    elif forced_custom:
        method = custom_method
    elif not segment.encryption_scheme and hls_crypto and hls_crypto.key:
        method = custom_method or "AES_128"
    else:
        method = segment_method
    if method in {"NONE", "UNKNOWN", "SABR"} or method in _HLS_SAMPLE_ENCRYPTION_METHODS:
        return data
    if method == "ENC":
        return data
    if method not in _HLS_SEGMENT_CIPHER_METHODS:
        raise DownloadError(f"Unsupported custom HLS method: {hls_crypto.method if hls_crypto else segment.encryption_scheme}", url=segment.url)
    if not segment.encrypted and not forced_custom:
        return data
    if method == "YOUKU_ECB":
        key = hls_crypto.key if hls_crypto and hls_crypto.key else None
        if not key:
            raise DownloadError("Youku copyrightDRM key was not supplied.", url=segment.url)
        try:
            return decrypt_youku_segment(data, key)
        except YoukuTsError as exc:
            raise DownloadError(f"Youku copyrightDRM decryption failed: {exc}", url=segment.url) from exc
    if method in {"AES_128", "AES_128_ECB"} and len(data) % 16:
        raise DownloadError(f"HLS AES segment is incomplete or not block aligned ({len(data)} bytes).", url=segment.url)

    key = hls_crypto.key if hls_crypto and hls_crypto.key else _load_hls_key(segment, headers, retries, request_timeout)
    if not key:
        raise DownloadError("AES-128 HLS key not found in playlist; use --custom-hls-key for this stream.", url=segment.url)
    iv = hls_crypto.iv if hls_crypto and hls_crypto.iv else segment.key_iv
    if method == "AES_128" and iv is None:
        sequence = segment.index if segment.index is not None and segment.index >= 0 else 0
        iv = int(sequence).to_bytes(16, "big")
    return _openssl_decrypt(data, method, key, iv)


def _hls_method(value: str | None) -> str:
    return (value or "NONE").upper().replace("-", "_")


def _is_retryable_hls_crypto_error(segment: SegmentInfo, exc: DownloadError) -> bool:
    if _hls_method(segment.encryption_scheme) not in _HLS_SEGMENT_CIPHER_METHODS:
        return False
    message = str(exc).lower()
    return "block aligned" in message or "wrong final block length" in message


def _load_hls_key(segment: SegmentInfo, headers: dict[str, str] | None, retries: int, request_timeout: int) -> bytes | None:
    key_uri = segment.key_uri
    if not key_uri:
        return None
    cached = _cached_hls_key(key_uri)
    if cached is not None:
        return cached
    data = _fetch_hls_key(key_uri, headers, retries, request_timeout)
    _store_hls_key(key_uri, data)
    return data


def _cached_hls_key(key_uri: str) -> bytes | None:
    with _HLS_KEY_LOCK:
        return _HLS_KEY_CACHE.get(key_uri)


def _store_hls_key(key_uri: str, data: bytes) -> None:
    with _HLS_KEY_LOCK:
        _HLS_KEY_CACHE[key_uri] = data


def _fetch_hls_key(key_uri: str, headers: dict[str, str] | None, retries: int, request_timeout: int) -> bytes:
    if key_uri.lower().startswith("data:"):
        return _decode_data_uri(key_uri)
    parsed = urlparse(key_uri)
    if parsed.scheme in {"http", "https"}:
        request_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
        request_headers.update({key: value for key, value in (headers or {}).items() if key.lower() != "range"})
        last_error: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                if _should_fetch_hls_key_with_insecure_localhost_tls(parsed):
                    return _fetch_https_localhost_hls_key_unverified(key_uri, request_headers, request_timeout)
                return get_global_http_client().fetch_bytes(key_uri, headers=request_headers, timeout=request_timeout)
            except (HTTPError, URLError, TimeoutError, OSError, http.client.HTTPException, HttpClientError) as exc:
                last_error = exc
                if attempt + 1 < retries:
                    _runtime_sleep(_retry_delay(attempt))
        raise DownloadError(f"Failed to fetch HLS key after {max(1, retries)} attempts: {_error_text(last_error)}", url=key_uri)
    try:
        return source_path(key_uri).read_bytes()
    except OSError as exc:
        raise DownloadError(f"Failed to read HLS key: {_error_text(exc)}", url=key_uri) from exc


def _should_fetch_hls_key_with_insecure_localhost_tls(parsed) -> bool:
    if not _ALLOW_INSECURE_LOCALHOST_HLS_KEYS or parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _fetch_https_localhost_hls_key_unverified(key_uri: str, headers: dict[str, str], request_timeout: int) -> bytes:
    parsed = urlparse(key_uri)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HttpClientError(f"Invalid local HTTPS key URL: {key_uri}")
    context = ssl._create_unverified_context()
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=request_timeout, context=context)
    path = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, parsed.fragment))
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        data = response.read()
        if response.status >= 400:
            raise HttpClientError(f"HTTP Error {response.status}: {response.reason}")
        return data
    finally:
        connection.close()


def _decode_data_uri(value: str) -> bytes:
    if "," not in value:
        raise DownloadError("Invalid HLS data URI key.")
    metadata, payload = value.split(",", 1)
    if ";base64" in metadata.lower():
        return base64.b64decode(payload)
    return unquote_to_bytes(payload)


def _openssl_decrypt(data: bytes, method: str, key: bytes, iv: bytes | None) -> bytes:
    executable = shutil.which("openssl")
    if not executable:
        raise DownloadError("openssl not found; custom HLS segment decryption needs openssl.")
    if method == "AES_128":
        if len(key) != 16:
            raise DownloadError("AES_128 custom HLS key must be 16 bytes.")
        if iv is None or len(iv) != 16:
            raise DownloadError("AES_128 custom HLS IV must be 16 bytes.")
        cipher_args = ["-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex(), "-nopad"]
        unpad = True
    elif method == "AES_128_ECB":
        if len(key) != 16:
            raise DownloadError("AES_128_ECB custom HLS key must be 16 bytes.")
        cipher_args = ["-aes-128-ecb", "-K", key.hex(), "-nopad"]
        unpad = True
    else:
        if len(key) != 32:
            raise DownloadError("CHACHA20 custom HLS key must be 32 bytes.")
        if iv is None or len(iv) not in {8, 12, 16}:
            raise DownloadError("CHACHA20 custom HLS IV must be 8, 12, or 16 bytes.")
        if len(iv) == 8:
            try:
                from Crypto.Cipher import ChaCha20
            except ImportError as exc:
                raise DownloadError(
                    "8-byte-nonce CHACHA20 HLS decryption needs pycryptodome."
                ) from exc
            clear = bytearray()
            for offset in range(0, len(data), _TENCENT_CHACHA20_CHUNK_SIZE):
                cipher = ChaCha20.new(key=key, nonce=iv)
                clear.extend(cipher.decrypt(data[offset : offset + _TENCENT_CHACHA20_CHUNK_SIZE]))
            return bytes(clear)
        cipher_args = ["-chacha20", "-K", key.hex(), "-iv", iv.hex()]
        unpad = False
    result = managed_run([executable, "enc", "-d", *cipher_args], input=data, capture_output=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise DownloadError(f"custom HLS decryption failed: {detail or 'openssl failed'}")
    output = result.stdout
    return _pkcs7_unpad(output) if unpad else output


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > 16 or data[-pad:] != bytes([pad]) * pad:
        return data
    return data[:-pad]


def _segment_label(segment: SegmentInfo) -> str:
    if segment.index is None:
        return "unknown"
    if segment.index == -1:
        return "init"
    return str(segment.index)


def _error_text(exc: BaseException | None) -> str:
    if exc is None:
        return "unknown error"
    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__


def _retry_delay(attempt: int) -> float:
    return min(2 * (2**attempt), 12)


def _download_with_aria2c(
    segments: list[SegmentInfo],
    output_path: Path,
    headers: dict[str, str] | None,
    workers: int,
    temp_dir: Path,
    retries: int,
    max_speed: int | None = None,
    request_timeout: int = 30,
    gate: Callable[[], None] | None = None,
) -> None:
    executable = shutil.which("aria2c") or shutil.which("aria2")
    if not executable:
        raise RuntimeError("aria2c not found. Install aria2c or use --downloader python.")
    if any(segment.byte_range for segment in segments):
        raise RuntimeError("aria2c mode does not support byte-range segments yet; use --downloader python.")

    temp_dir.mkdir(parents=True, exist_ok=True)
    part_paths = [temp_dir / f"{index:08d}.part" for index in range(len(segments))]
    input_lines: list[str] = []
    for index, segment in enumerate(segments):
        if gate is not None:
            gate()
        if _cached_part_size(part_paths[index]) is not None:
            continue
        if segment.data is not None or not is_url(segment.url):
            _download_part(segment, part_paths[index], headers, retries, request_timeout=request_timeout)
            continue
        input_lines.append(f"{segment.url}\n\tdir={temp_dir}\n\tout={index:08d}.part")

    if input_lines:
        input_text = "\n".join(input_lines)
        input_path = temp_dir / ".aria2.input"
        input_path.write_text(input_text, encoding="utf-8")
        args = [
            executable,
            "-c",
            "-x",
            str(workers),
            "-j",
            str(workers),
            "-s",
            str(workers),
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "--file-allocation=none",
            "--summary-interval=0",
            "--console-log-level=error",
            "--show-console-readout=false",
            "--download-result=hide",
            "--timeout",
            str(request_timeout),
            "--connect-timeout",
            str(request_timeout),
            "-i",
            str(input_path),
        ]
        if max_speed:
            args.extend(["--max-download-limit", str(max_speed)])
        for key, value in (headers or {}).items():
            if key.lower() == "accept-encoding":
                continue
            args.extend(["--header", f"{key}: {value}"])
        process = None
        try:
            process = managed_popen(args)  # noqa: S603 - executable is resolved from PATH
            while process.poll() is None:
                if gate is not None:
                    gate()
                _runtime_sleep(0.1)
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, args)
        except BaseException:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
        finally:
            try:
                input_path.unlink()
            except OSError:
                pass

    if gate is not None:
        gate()
    with output_path.open("wb") as output:
        for part in part_paths:
            _copy_file_to_output(part, output)


def _output_path(stream: StreamInfo, output_dir: str | Path, filename: str | None) -> Path:
    output_dir = Path(output_dir).expanduser()
    if filename:
        path = Path(filename)
        if _has_known_output_suffix(path):
            return unique_path(path if path.is_absolute() else output_dir / path)
        return unique_path(output_dir / f"{filename}.{_default_extension(stream)}")
    stem = _safe_name(stream.name or stream.id or stream.display_prefix().lower())
    return unique_path(output_dir / f"{stem}.{_default_extension(stream)}")


def _copy_file_to_output(path: Path, output) -> None:
    with path.open("rb") as source:
        shutil.copyfileobj(source, output, length=1024 * 1024)


def _default_extension(stream: StreamInfo) -> str:
    extension = (stream.extension or "").lower()
    if extension == "bbts":
        return "ts"
    if extension in {"ts", "mp3", "m4a", "mp4", "vtt", "srt", "ttml", "webm"}:
        return extension
    if stream.media_type == "subtitle":
        codec = (stream.codecs or "").lower()
        if extension in {"m4s", "m4v", "mov"} and any(token in codec for token in ("wvtt", "stpp", "ttml")):
            return "mp4"
        return "vtt" if extension == "webvtt" else "ttml"
    return "mp4"


def _has_known_output_suffix(path: Path) -> bool:
    return path.suffix.lower() in KNOWN_OUTPUT_SUFFIXES


def _safe_name(value: str) -> str:
    keep = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            keep.append(char)
        elif char.isspace():
            keep.append("_")
    result = "".join(keep).strip("._")
    return result or "stream"
