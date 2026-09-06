from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..embedding import managed_run
from ..loader import content_length, head_url, local_file_size
from ..models import SegmentInfo, StreamInfo
from ..utils import is_url, source_path, uri_basename


def parse_direct(source: str, headers: dict[str, str] | None = None, probe: bool = True) -> list[StreamInfo]:
    size = None
    final_url = source
    content_type = None
    if is_url(source):
        head = head_url(source, headers=headers)
        size = content_length(head)
        final_url = head.get("Final-Url", source) if head else source
        content_type = (head.get("Content-Type") or head.get("content-type") or "").split(";", 1)[0]
    else:
        size = local_file_size(source)
        final_url = str(source_path(source).resolve()) if Path(source).expanduser().exists() else source

    probe_data = _ffprobe(final_url if is_url(final_url) else source, headers=headers) if probe else None
    stream = _stream_from_probe(source, final_url, probe_data)
    if stream is None:
        stream = StreamInfo(
            manifest_type="direct",
            media_type=_media_type_from_source(source, content_type),
            url=final_url,
            original_url=source,
            id=uri_basename(source),
            name=uri_basename(source),
            extension=_extension(source),
        )
    stream.size_bytes = stream.size_bytes or size
    if not stream.segments:
        range_size = stream.size_bytes
        byte_range = (0, range_size - 1) if range_size and range_size > 0 else None
        stream.segments = [SegmentInfo(url=final_url, duration=stream.duration, index=0, byte_range=byte_range)]
    return [stream]


def _stream_from_probe(source: str, final_url: str, data: dict | None) -> StreamInfo | None:
    if not data:
        return None
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    primary = video or audio
    if primary is None:
        return None
    media_type = "video" if video else "audio"
    duration = _float_or_none(primary.get("duration")) or _float_or_none(fmt.get("duration"))
    bitrate = _int_or_none(primary.get("bit_rate")) or _int_or_none(fmt.get("bit_rate"))
    width = primary.get("width")
    height = primary.get("height")
    resolution = f"{width}x{height}" if width and height else None
    frame_rate = _frame_rate(primary.get("avg_frame_rate") or primary.get("r_frame_rate"))
    size = _int_or_none(fmt.get("size"))
    codec = primary.get("codec_name")

    return StreamInfo(
        manifest_type="direct",
        media_type=media_type,
        url=final_url,
        original_url=source,
        id=uri_basename(source),
        name=uri_basename(source),
        bandwidth=bitrate,
        codecs=codec,
        resolution=resolution,
        frame_rate=frame_rate,
        channels=str(primary.get("channels")) if primary.get("channels") else None,
        extension=_extension(source) or (fmt.get("format_name") or "").split(",", 1)[0] or None,
        duration=duration,
        size_bytes=size,
    )


def _ffprobe(source: str, headers: dict[str, str] | None = None) -> dict | None:
    executable = shutil.which("ffprobe")
    if not executable:
        return None
    command = [
        executable,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
    ]
    if is_url(source) and headers:
        header_blob = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        command.extend(["-headers", header_blob])
    command.append(source)
    try:
        proc = managed_run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _media_type_from_source(source: str, content_type: str | None = None) -> str:
    content_type = (content_type or "").lower()
    ext = _extension(source)
    if content_type.startswith("audio/") or ext in {"mp3", "m4a", "aac", "flac", "wav"}:
        return "audio"
    if content_type.startswith("text/") or ext in {"vtt", "srt", "ttml"}:
        return "subtitle"
    return "video"


def _extension(source: str) -> str | None:
    name = uri_basename(source).split("?", 1)[0]
    if "." not in name:
        return None
    return name.rsplit(".", 1)[-1].lower()


def _frame_rate(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            denominator_float = float(denominator)
            if denominator_float == 0:
                return None
            return float(numerator) / denominator_float
        return float(value)
    except ValueError:
        return None


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value) -> float | None:
    try:
        return float(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None
