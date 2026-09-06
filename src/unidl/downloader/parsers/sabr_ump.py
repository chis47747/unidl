from __future__ import annotations

import base64
import math
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..models import StreamInfo

SABR_MANIFEST_TYPE = "sabr_ump"
HDR10_VIDEO_ITAGS = {
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
WEBM_VIDEO_ITAGS = {
    242,
    243,
    244,
    247,
    248,
    271,
    272,
    278,
    280,
    302,
    303,
    308,
    313,
    315,
    317,
    318,
    409,
    411,
    557,
    558,
} | HDR10_VIDEO_ITAGS
DEFAULT_DVR_SEGMENT_SECONDS = 5.0
DVR_DURATION_MARGIN_SEGMENTS = 12


def parse_sabr_ump_streams(
    uri: str,
    title: dict[str, Any],
    title_meta: dict[str, Any],
    *,
    existing_streams: list[StreamInfo] | None = None,
) -> list[StreamInfo]:
    server_abr = title.get("server_abr")
    if not isinstance(server_abr, dict):
        return []
    if str(server_abr.get("transport") or "").lower() != "sabr_ump":
        return []
    if server_abr.get("sabr_usable") is False:
        return []
    validation = server_abr.get("sabr_validation") if isinstance(server_abr.get("sabr_validation"), dict) else {}
    if server_abr.get("request_body_is_heap_prefix") or validation.get("usable") is False:
        return []
    if server_abr.get("request_body_live_validated") is False:
        return []

    url = _server_abr_url(server_abr)
    if not url:
        return []

    existing_streams = existing_streams or []
    force_primary = bool(server_abr.get("force_sabr_primary"))
    existing_videos = [stream for stream in existing_streams if stream.media_type == "video"]
    existing_video_ids = set() if force_primary else {
        str(stream.id) for stream in existing_videos if stream.id
    }
    direct_max_height = 0 if force_primary else max(
        (_height(stream) or 0 for stream in existing_videos),
        default=0,
    )
    selected_max_height = 0 if force_primary else (_int_or_none(server_abr.get("selected_max_height")) or direct_max_height)
    min_sabr_height = max(direct_max_height, selected_max_height)
    dvr_window = _sabr_dvr_window(title_meta, server_abr, url) if _title_is_dvr_vod(title) else (None, None)

    streams: list[StreamInfo] = []
    for fmt in _unique_formats(server_abr.get("advertised_video_formats")):
        itag = _format_itag(fmt)
        width = _int_or_none(fmt.get("width"))
        height = _int_or_none(fmt.get("height"))
        if not itag or not width or not height:
            continue
        if str(itag) in existing_video_ids:
            continue
        if not force_primary and not _sabr_video_adds_playback_option(
            fmt,
            existing_videos,
            server_abr,
            min_sabr_height=min_sabr_height,
        ):
            continue
        streams.append(_video_stream(uri, title, title_meta, server_abr, fmt, url, width, height, dvr_window=dvr_window))
    streams.extend(_audio_streams(uri, title, title_meta, server_abr, url, existing_streams=existing_streams, dvr_window=dvr_window))
    return streams


def is_sabr_ump_stream(stream: StreamInfo) -> bool:
    return stream.manifest_type == SABR_MANIFEST_TYPE or bool(stream.extra.get("sabr_ump"))


def _video_stream(
    uri: str,
    title: dict[str, Any],
    title_meta: dict[str, Any],
    server_abr: dict[str, Any],
    fmt: dict[str, Any],
    url: str,
    width: int,
    height: int,
    dvr_window: tuple[int | None, int | None] = (None, None),
) -> StreamInfo:
    itag = str(_format_itag(fmt) or "")
    kid = _normalize_kid(fmt.get("kid"))
    drm = server_abr.get("drm") if isinstance(server_abr.get("drm"), dict) else {}
    key_ids = _normalize_kids([kid])
    encrypted = bool(
        kid
        or (isinstance(drm, dict) and drm.get("key_count"))
        or str(fmt.get("key_status") or "").startswith("kid_")
        or str(fmt.get("kid_source") or "").startswith("pending_")
    )
    duration = _duration_seconds(title) or _positive_float_or_none(title_meta.get("duration_seconds"))
    extra = _sabr_extra(title_meta, server_abr, fmt, media_type="video", dvr_window=dvr_window)
    if key_ids:
        extra["key_ids"] = key_ids
        extra["key_id"] = key_ids[0]
    return StreamInfo(
        manifest_type=SABR_MANIFEST_TYPE,
        media_type="video",
        url=url,
        original_url=uri,
        id=itag or None,
        role=f"SABR itag {itag}" if itag else "SABR Advertised",
        bandwidth=_int_or_none(fmt.get("bitrate") or fmt.get("bandwidth")),
        codecs=_format_codec(fmt),
        resolution=f"{width}x{height}",
        frame_rate=_float_or_none(fmt.get("fps") or fmt.get("frame_rate") or fmt.get("frameRate")),
        extension=_video_extension(fmt),
        video_range=_video_range(fmt),
        duration=duration,
        encrypted=encrypted,
        encryption_scheme="SABR" if encrypted else None,
        is_live=_title_is_live(title),
        segments=[],
        extra=extra,
    )


def _sabr_video_adds_playback_option(
    fmt: dict[str, Any],
    existing_videos: list[StreamInfo],
    server_abr: dict[str, Any],
    *,
    min_sabr_height: int,
) -> bool:
    height = _int_or_none(fmt.get("height")) or 0
    if height > min_sabr_height:
        return True

    same_height = [stream for stream in existing_videos if _height(stream) == height]
    comparison_streams = same_height or existing_videos
    baseline_fps = max((stream.frame_rate or 0.0 for stream in comparison_streams), default=0.0)
    if not comparison_streams:
        baseline_fps = _float_or_none(server_abr.get("selected_max_fps")) or 0.0
    candidate_fps = _float_or_none(fmt.get("fps") or fmt.get("frame_rate") or fmt.get("frameRate")) or 0.0
    if candidate_fps > baseline_fps + 0.01:
        return True

    candidate_range = _normalize_video_range(_video_range(fmt))
    if candidate_range == "SDR":
        return False
    existing_ranges = {
        _normalize_video_range(stream.video_range)
        for stream in comparison_streams
    }
    return candidate_range not in existing_ranges


def _matching_direct_audio(candidates: list[StreamInfo], fmt: dict[str, Any]) -> StreamInfo | None:
    itag = str(_format_itag(fmt) or "")
    role = _audio_role(fmt)
    matches = [stream for stream in candidates if str(stream.id or stream.group_id or "") == itag]
    if role:
        role_matches = [stream for stream in matches if (stream.role or "").lower() == role.lower()]
        if role_matches:
            return role_matches[0]
    return matches[0] if matches else None


def _audio_role(fmt: dict[str, Any]) -> str | None:
    for key in ("role", "audio_descriptor", "audioDescriptor", "audio_description"):
        explicit = _str_or_none(fmt.get(key))
        if explicit:
            return _normalize_audio_role(explicit)
    xtags = _str_or_none(fmt.get("xtags"))
    if xtags:
        raw_lowered = xtags.strip().lower()
        lowered = f"{xtags} {_decode_xtags_text(xtags)}".lower()
        if "acont" in lowered and "primary" in lowered:
            return "Primary"
        if "acont" in lowered and "secondary" in lowered:
            return "Secondary"
        if raw_lowered == "primary":
            return "Primary"
        if raw_lowered == "secondary":
            return "Secondary"
    return None


def _decode_xtags_text(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    try:
        padded = raw + "=" * ((4 - len(raw) % 4) % 4)
        return base64.b64decode(padded).decode("utf-8", "ignore")
    except Exception:
        return raw


def _normalize_audio_role(value: str) -> str:
    normalized = value.strip().replace("_", " ").replace("-", " ")
    lowered = normalized.lower()
    if lowered == "primary":
        return "Primary"
    if lowered == "secondary":
        return "Secondary"
    if lowered in {"main", "default"}:
        return lowered.title()
    return normalized


def _audio_default_bandwidth(itag: int | None) -> int | None:
    return {
        148: 64_000,
        149: 144_000,
        381: 400_000,
    }.get(int(itag) if itag is not None else 0)


def _audio_default_codec(itag: int | None) -> str | None:
    if itag in {148, 149}:
        return "aac.mp4a.40.2"
    if itag == 381:
        return "ac-3"
    return None


def _audio_default_channels(itag: int | None) -> str | None:
    if itag in {148, 149}:
        return "2.0"
    if itag == 381:
        return "5.1"
    return None


def _audio_extension(item: dict[str, Any]) -> str:
    text = " ".join(str(item.get(key) or "") for key in ("codec", "codecs", "mime_type", "mimeType")).lower()
    if "webm" in text or "opus" in text:
        return "webm"
    return "mp4"


def _sabr_extra(
    title_meta: dict[str, Any],
    server_abr: dict[str, Any],
    fmt: dict[str, Any],
    *,
    media_type: str,
    dvr_window: tuple[int | None, int | None] = (None, None),
) -> dict[str, Any]:
    client = server_abr.get("client") if isinstance(server_abr.get("client"), dict) else {}
    drm = server_abr.get("drm") if isinstance(server_abr.get("drm"), dict) else {}
    extra = dict(title_meta)
    dvr_min_sequence, dvr_end_sequence = dvr_window
    dvr_duration = _positive_float_or_none(title_meta.get("duration_seconds"))
    extra.update(
        {
            "source": "json_sabr_ump",
            "sabr_ump": True,
            "sabr_media_type": media_type,
            "sabr_transport": "sabr_ump",
            "sabr_dvr_as_vod": title_meta.get("title_source") == "dvr",
            "sabr_dvr_min_sequence": dvr_min_sequence,
            "sabr_dvr_end_sequence": dvr_end_sequence,
            "sabr_dvr_duration_seconds": dvr_duration,
            "sabr_url": _server_abr_url(server_abr),
            "sabr_itag": _format_itag(fmt),
            "sabr_lmt": _int_or_none(fmt.get("lmt")),
            "sabr_xtags": _str_or_none(fmt.get("xtags")),
            "sabr_format_source": _str_or_none(fmt.get("source")),
            "sabr_kid_source": _str_or_none(fmt.get("kid_source")),
            "sabr_key_status": _str_or_none(fmt.get("key_status")),
            "sabr_config": server_abr.get("video_playback_ustreamer_config"),
            "sabr_request_body": _str_or_none(server_abr.get("request_body_b64")),
            "sabr_request_body_source": _str_or_none(server_abr.get("request_body_source")),
            "sabr_request_body_bytes": _int_or_none(server_abr.get("request_body_bytes")),
            "sabr_request_body_po_token_present": bool(server_abr.get("request_body_po_token_present")),
            "sabr_request_body_playback_cookie_present": bool(server_abr.get("request_body_playback_cookie_present")),
            "sabr_request_body_playback_cookie_bytes": _int_or_none(server_abr.get("request_body_playback_cookie_bytes")),
            "sabr_request_body_is_tv_sabr_request": bool(server_abr.get("request_body_is_tv_sabr_request")),
            "sabr_usable": bool(server_abr.get("sabr_usable")),
            "sabr_request_headers": _request_headers(server_abr.get("sabr_request_headers")),
            "sabr_request_video_formats": _compact_formats(server_abr.get("request_video_formats")),
            "sabr_request_audio_formats": _compact_formats(server_abr.get("request_audio_formats")),
            "sabr_client": {
                "client_name": _str_or_none(client.get("client_name")),
                "client_number": _str_or_none(client.get("client_number")),
                "client_version": _str_or_none(client.get("client_version")),
            },
            "sabr_po_token": _str_or_none(server_abr.get("po_token")),
            "sabr_po_token_source": _str_or_none(server_abr.get("po_token_source")),
            "sabr_po_token_status": _str_or_none(server_abr.get("po_token_status")),
            "sabr_po_token_required": bool(server_abr.get("po_token_required_for_high_renditions")),
            "sabr_audio_formats": _compact_formats(server_abr.get("selected_audio_formats")),
            "sabr_available_key_ids": _normalize_kids(drm.get("key_ids_available") if isinstance(drm, dict) else None),
            "raw": _compact_format(fmt),
        }
    )
    if title_meta.get("title_source") == "dvr" and dvr_duration is not None:
        max_count = _dvr_expected_sequence_count(dvr_duration, DEFAULT_DVR_SEGMENT_SECONDS)
        if max_count is not None:
            extra["sabr_dvr_sequence_max_count"] = max_count
            extra["sabr_dvr_sequence_max_probe_span"] = max(1, max_count - 1)
    return extra


def _audio_streams(
    uri: str,
    title: dict[str, Any],
    title_meta: dict[str, Any],
    server_abr: dict[str, Any],
    url: str,
    *,
    existing_streams: list[StreamInfo],
    dvr_window: tuple[int | None, int | None] = (None, None),
) -> list[StreamInfo]:
    result: list[StreamInfo] = []
    existing_audio = [stream for stream in existing_streams if stream.media_type == "audio"]
    for fmt in _unique_formats(server_abr.get("selected_audio_formats")):
        itag = _format_itag(fmt)
        if not itag:
            continue
        matched = _matching_direct_audio(existing_audio, fmt)
        result.append(_audio_stream(uri, title, title_meta, server_abr, fmt, url, matched, dvr_window=dvr_window))
    return result


def _audio_stream(
    uri: str,
    title: dict[str, Any],
    title_meta: dict[str, Any],
    server_abr: dict[str, Any],
    fmt: dict[str, Any],
    url: str,
    matched: StreamInfo | None,
    *,
    dvr_window: tuple[int | None, int | None] = (None, None),
) -> StreamInfo:
    itag = str(_format_itag(fmt) or "")
    kid = _normalize_kid(fmt.get("kid"))
    drm = server_abr.get("drm") if isinstance(server_abr.get("drm"), dict) else {}
    key_ids = _normalize_kids([kid])
    encrypted = bool(
        kid
        or (isinstance(drm, dict) and drm.get("key_count"))
        or str(fmt.get("key_status") or "").startswith("kid_")
        or str(fmt.get("kid_source") or "").startswith("pending_")
        or (matched.encrypted if matched else False)
    )
    duration = _duration_seconds(title) or _positive_float_or_none(title_meta.get("duration_seconds"))
    extra = _sabr_extra(title_meta, server_abr, fmt, media_type="audio", dvr_window=dvr_window)
    target_audio = [_compact_format(fmt)]
    extra["sabr_request_audio_formats"] = target_audio
    extra["sabr_audio_formats"] = target_audio
    if key_ids:
        extra["key_ids"] = key_ids
        extra["key_id"] = key_ids[0]
    return StreamInfo(
        manifest_type=SABR_MANIFEST_TYPE,
        media_type="audio",
        url=url,
        original_url=uri,
        id=itag or None,
        group_id=matched.group_id if matched else None,
        name=matched.name if matched else _str_or_none(fmt.get("name")),
        language=matched.language if matched else _str_or_none(fmt.get("language") or fmt.get("lang") or fmt.get("locale")) or "und",
        role=_audio_role(fmt) or (matched.role if matched else None),
        bandwidth=_int_or_none(fmt.get("bitrate") or fmt.get("bandwidth")) or (matched.bandwidth if matched else _audio_default_bandwidth(_format_itag(fmt))),
        codecs=_format_codec(fmt) or (matched.codecs if matched else _audio_default_codec(_format_itag(fmt))),
        channels=_str_or_none(fmt.get("channels")) or (matched.channels if matched else _audio_default_channels(_format_itag(fmt))),
        extension=matched.extension if matched and matched.extension else _audio_extension(fmt),
        duration=duration,
        size_bytes=matched.size_bytes if matched else None,
        encrypted=encrypted,
        encryption_scheme="SABR" if encrypted else None,
        is_live=_title_is_live(title),
        segments=[],
        extra=extra,
    )


def _unique_formats(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        itag = _format_itag(item)
        if not itag:
            continue
        key = (itag, str(item.get("xtags") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _compact_formats(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_compact_format(item) for item in value if isinstance(item, dict)]


def _compact_format(item: dict[str, Any]) -> dict[str, Any]:
    skipped = {"playback"}
    return {key: value for key, value in item.items() if key not in skipped}


def _request_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, header_value in value.items():
        key_text = str(key or "").strip()
        if not key_text or key_text.lower() in {"host", "connection", "content-length"}:
            continue
        out[key_text] = str(header_value)
    return out


def _sabr_dvr_window(title_meta: dict[str, Any], server_abr: dict[str, Any], url: str) -> tuple[int | None, int | None]:
    url_start, url_end = _sabr_dvr_window_from_url(url)
    inspection_start, inspection_end = _sabr_dvr_window_from_inspection(server_abr)
    start = url_start if url_start is not None else inspection_start
    end = url_end if url_end is not None else inspection_end
    if end is None:
        end = _int_or_none(title_meta.get("json_dvr_sequence_end"))
    return start, end


def _sabr_dvr_window_from_url(url: str) -> tuple[int | None, int | None]:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    start = _first_int_param(params, "mindsq")
    if start is None:
        start = _first_int_param(params, "min_sq")
    end = _first_int_param(params, "maxdsq")
    if end is None:
        end = _first_int_param(params, "max_sq")
    return start, end


def _sabr_dvr_window_from_inspection(server_abr: dict[str, Any]) -> tuple[int | None, int | None]:
    starts: list[int] = []
    ends: list[int] = []
    inspection = server_abr.get("request_body_inspection")
    inspection = inspection if isinstance(inspection, dict) else {}
    native = server_abr.get("native_initplayback")
    native = native if isinstance(native, dict) else {}
    windows = (
        inspection.get("sequence_window") or inspection.get("sequenceWindow"),
        native.get("sequence_window") or native.get("sequenceWindow"),
    )
    for window in windows:
        if not isinstance(window, dict):
            continue
        for key in ("start", "first", "first_sequence", "firstSequence", "min", "min_sequence", "minSequence"):
            value = _int_or_none(window.get(key))
            if value is not None:
                starts.append(value)
        for key in ("end", "last", "last_sequence", "lastSequence", "max", "max_sequence", "maxSequence"):
            value = _int_or_none(window.get(key))
            if value is not None:
                ends.append(value)
    records = inspection.get("segment_records")
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            first = _int_or_none(record.get("first_sequence") or record.get("firstSequence") or record.get("sequence"))
            if first is not None:
                starts.append(first)
    return (min(starts) if starts else None, max(ends) if ends else None)


def _first_int_param(params: dict[str, list[str]], key: str) -> int | None:
    for raw in params.get(key, []):
        value = _int_or_none(raw)
        if value is not None:
            return value
    return None


def _server_abr_url(server_abr: dict[str, Any]) -> str | None:
    url = _str_or_none(server_abr.get("server_abr_streaming_url"))
    if url:
        return url
    for values_key in ("advertised_video_formats", "selected_video_formats", "selected_audio_formats"):
        values = server_abr.get(values_key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            playback = item.get("playback")
            if isinstance(playback, dict):
                url = _str_or_none(playback.get("url"))
                if url:
                    return url
    return None


def _format_itag(item: dict[str, Any]) -> int | None:
    return _int_or_none(item.get("itag") or item.get("id"))


def _format_codec(item: dict[str, Any]) -> str | None:
    codec = _str_or_none(item.get("codec") or item.get("codecs") or item.get("mime_type") or item.get("mimeType"))
    if codec:
        return codec
    itag = _format_itag(item)
    if itag in HDR10_VIDEO_ITAGS:
        return "vp09.02"
    if itag in WEBM_VIDEO_ITAGS:
        return "vp09"
    return None


def _video_extension(item: dict[str, Any]) -> str:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("codec", "codecs", "mime_type", "mimeType")
    ).lower()
    if "webm" in text or "vp9" in text or "vp09" in text or "vp8" in text:
        return "webm"
    itag = _format_itag(item)
    if itag in WEBM_VIDEO_ITAGS:
        return "webm"
    return "mp4"


def _video_range(item: dict[str, Any]) -> str:
    if _format_itag(item) in HDR10_VIDEO_ITAGS:
        return "HDR10"
    explicit = _str_or_none(item.get("video_range") or item.get("videoRange") or item.get("dynamic_range") or item.get("range"))
    if explicit:
        return explicit.upper()
    text = " ".join(str(item.get(key) or "") for key in ("hdr", "hdr10plus", "hdr10_plus", "xtags")).lower()
    if any(token in text for token in ("hdr10+", "hdr10plus")):
        return "HDR10+"
    if any(token in text for token in ("hdr", "pq", "hdr10")):
        return "HDR10"
    if "hlg" in text:
        return "HLG"
    return "SDR"


def _normalize_video_range(value: Any) -> str:
    text = str(value or "SDR").strip().upper().replace(" ", "").replace("_", "")
    if text in {"", "SDR", "SDR8", "SDR10"}:
        return "SDR"
    if text in {"DOLBYVISION", "DOLBY-VISION"}:
        return "DV"
    return text


def _duration_seconds(title: dict[str, Any]) -> float | None:
    seconds = _positive_float_or_none(title.get("duration") or title.get("duration_seconds") or title.get("durationSeconds"))
    if seconds is not None:
        return seconds
    milliseconds = _positive_float_or_none(title.get("duration_ms") or title.get("durationMs"))
    return milliseconds / 1000.0 if milliseconds is not None else None


def _title_is_live(title: dict[str, Any]) -> bool:
    if _title_is_dvr_vod(title):
        return False
    metadata = title.get("liveMetadata")
    if not isinstance(metadata, dict):
        return False
    event_end = _parse_json_time(metadata.get("eventEndTime"))
    if event_end is None:
        return True
    offset_seconds = max(0.0, _float_or_none(metadata.get("eventAvailabilityOffsetMs")) or 0.0) / 1000.0
    return event_end.timestamp() > datetime.now(timezone.utc).timestamp() - offset_seconds


def _title_is_dvr_vod(title: dict[str, Any]) -> bool:
    source = _str_or_none(title.get("source") or title.get("playback_source") or title.get("playbackSource"))
    return bool(source and source.lower() == "dvr")


def _parse_json_time(value: Any) -> datetime | None:
    text = _str_or_none(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _height(stream: StreamInfo) -> int | None:
    if not stream.resolution or "x" not in stream.resolution:
        return None
    _width, height = stream.resolution.lower().split("x", 1)
    return _int_or_none(height)


def _normalize_kids(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values:
        kid = _normalize_kid(item)
        if kid and kid not in result:
            result.append(kid)
    return result


def _normalize_kid(value: Any) -> str | None:
    text = _str_or_none(value)
    if not text:
        return None
    text = text.lower().replace("-", "").replace(" ", "")
    if text.startswith("0x"):
        text = text[2:]
    if len(text) == 32 and all(ch in "0123456789abcdef" for ch in text):
        return text
    return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float_or_none(value: Any) -> float | None:
    number = _float_or_none(value)
    return number if number is not None and number > 0 else None


def _dvr_expected_sequence_count(duration_seconds: float | None, segment_seconds: float | None) -> int | None:
    if duration_seconds is None or duration_seconds <= 0:
        return None
    segment_seconds = segment_seconds or DEFAULT_DVR_SEGMENT_SECONDS
    if segment_seconds <= 0:
        segment_seconds = DEFAULT_DVR_SEGMENT_SECONDS
    return max(1, int(math.ceil(duration_seconds / segment_seconds)) + DVR_DURATION_MARGIN_SEGMENTS)
