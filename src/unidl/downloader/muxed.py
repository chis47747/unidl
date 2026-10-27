"""Service-opted-in, lossless separation of finite muxed A/V sources."""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from .models import StreamInfo
from .utils import pretty_codec

SOURCE = "_unidl_muxed_source"
MEDIA = "_unidl_demux_media"
_SOURCE_FIELDS = (
    "id",
    "group_id",
    "name",
    "language",
    "role",
    "bandwidth",
    "codecs",
    "resolution",
    "frame_rate",
    "channels",
    "extension",
    "video_range",
    "size_bytes",
)
_AUDIO_CODECS = {"AAC", "HE-AAC", "AC-3", "E-AC-3", "E-AC-3 Atmos", "AC-4", "Opus", "FLAC", "DTS", "DTS-HD", "DTS:X"}


def is_demux_stream(stream: StreamInfo) -> bool:
    return stream.extra.get(MEDIA) in {"video", "audio"} and isinstance(stream.extra.get(SOURCE), dict)


def _codec(value: str | None, media: str) -> str | None:
    for token in str(value or "").split(","):
        token = token.strip()
        if token and ((pretty_codec(token, media) in _AUDIO_CODECS) == (media == "audio")):
            return token
    return None


def split_muxed_stream(stream: StreamInfo) -> list[StreamInfo]:
    """Project metadata only: no media, key or init-segment requests."""
    if is_demux_stream(stream) or stream.media_type != "video" or not stream.extra.get("muxed_audio") or stream.is_live:
        return [stream]
    check_source(stream)
    shape = {key: getattr(stream, key) for key in _SOURCE_FIELDS}
    extra = {**stream.extra, SOURCE: shape, "muxed_audio": False}
    video = replace(
        stream,
        codecs=_codec(stream.codecs, "video"),
        channels=None,
        extension="mp4",
        size_bytes=None,
        segments=list(stream.segments),
        extra={**extra, MEDIA: "video"},
    )
    audio_codec = stream.extra.get("muxed_audio_codec") or _codec(stream.codecs, "audio")
    audio_extension = (
        "m4a"
        if pretty_codec(audio_codec, "audio")
        in {
            "AAC",
            "HE-AAC",
            "AC-3",
            "E-AC-3",
            "E-AC-3 Atmos",
            "AC-4",
        }
        else "mka"
    )
    audio = replace(
        stream,
        media_type="audio",
        id=f"{stream.id or 'muxed'}:audio",
        group_id=f"embedded-{stream.resolution or stream.id or 'audio'}",
        language=stream.extra.get("audio_language") or stream.language,
        role=stream.role or "Main",
        bandwidth=stream.extra.get("muxed_audio_bitrate"),
        codecs=audio_codec,
        channels=stream.extra.get("muxed_audio_channels"),
        resolution=None,
        frame_rate=None,
        video_range=None,
        size_bytes=None,
        extension=audio_extension,
        segments=list(stream.segments),
        extra={
            key: value
            for key, value in {**extra, MEDIA: "audio"}.items()
            if key not in {"actual_bitrate", "actual_bitrate_samples"}
        },
    )
    return [video, audio]


def check_source(stream: StreamInfo) -> None:
    # A sample-encrypted source may carry different audio/video KIDs. Do not
    # pretend that the video-only decryptor guarantees a clear audio output.
    if stream.encrypted and str(stream.encryption_scheme or "").upper().replace("_", "-") not in {"AES-128"}:
        raise ValueError(
            "Muxed A/V separation currently requires clear or HLS AES-128 VOD; sample-encrypted sources are not supported."
        )


def projection_source(stream: StreamInfo) -> StreamInfo:
    extra = {key: value for key, value in stream.extra.items() if key not in {SOURCE, MEDIA}}
    extra["muxed_audio"] = True
    return replace(stream, media_type="video", extra=extra, **stream.extra[SOURCE])


def demux_file(source: Path, output: Path, stream: StreamInfo) -> Path:
    from .postprocess import _run_external

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to separate muxed audio and video.")
    media = stream.extra[MEDIA]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + ".demuxing" + output.suffix)
    command = [
        ffmpeg,
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-copyts",
        "-start_at_zero",
        "-i",
        str(source),
        "-map",
        f"0:{'a' if media == 'audio' else 'v'}:0",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "disabled",
        str(temporary),
    ]
    try:
        _run_external(command, f"separating muxed {media}")
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError(f"Separating muxed {media} produced no output.")
        temporary.replace(output)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
    return output


class MuxedSourceCache:
    """Share a physical download across projections, with cancellable waits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[threading.Lock, list[tuple[bool, Any]]]] = {}

    @staticmethod
    def key(stream: StreamInfo) -> str:
        # Full source identity, not a language/codec guess: distinct renditions
        # may contain different audio, even when their labels look identical.
        from .downloader import _stream_resume_key

        return hashlib.sha256(
            json.dumps(
                [
                    _stream_resume_key(stream),
                    stream.codecs,
                    stream.encryption_scheme,
                    stream.extra.get("_unidl_source_headers"),
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()[:24]

    def get(self, key: str, download: Callable[[], Any], gate: Callable[[], None]):
        with self._lock:
            lock, values = self._entries.setdefault(key, (threading.Lock(), []))
        while not lock.acquire(timeout=0.1):
            gate()
        try:
            gate()
            if not values:
                try:
                    values.append((True, download()))
                except BaseException as exc:
                    values.append((False, exc))
                    raise
            succeeded, value = values[0]
            if not succeeded:
                raise value
            return value
        finally:
            lock.release()
