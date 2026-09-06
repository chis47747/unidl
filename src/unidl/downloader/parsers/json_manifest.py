from __future__ import annotations

import base64
import json
import math
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from ..audio import audio_metadata_from_title
from ..models import SegmentInfo, StreamInfo
from ..utils import join_uri
from .sabr_ump import parse_sabr_ump_streams

_DEFAULT_DVR_SEGMENT_SECONDS = 5.0
_DVR_DURATION_MARGIN_SEGMENTS = 12
_DURATION_TEXT_UNITS = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)\b",
    re.IGNORECASE,
)
_DURATION_TEXT_CLOCK = re.compile(r"\b(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\b")


def parse_json(uri: str, text: str, headers: dict[str, str] | None = None) -> list[StreamInfo]:
    data = json.loads(text)
    streams: list[StreamInfo] = []
    for title in _titles(data):
        streams.extend(_parse_title(uri, title, headers=headers))
    if streams:
        return streams
    return _parse_generic_url_tracks(uri, data)


def _titles(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _parse_title(
    uri: str,
    title: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> list[StreamInfo]:
    streams: list[StreamInfo] = []
    title_meta = _title_meta(title)
    for item in _track_items(title, "video_tracks", "videos", "video"):
        nested = _nested_hls_streams(uri, item, "video", title_meta, headers)
        if nested is not None:
            streams.extend(nested)
            continue
        stream = _video_stream(uri, title, item, title_meta)
        if stream:
            streams.append(stream)
    for item in _track_items(title, "audio_tracks", "audios", "audio"):
        nested = _nested_hls_streams(uri, item, "audio", title_meta, headers)
        if nested is not None:
            streams.extend(nested)
            continue
        stream = _audio_stream(uri, title, item, title_meta)
        if stream:
            streams.append(stream)
    for item in _track_items(title, "subtitle_tracks", "subtitles", "text_tracks", "texts"):
        stream = _subtitle_stream(uri, title, item, title_meta)
        if stream:
            streams.append(stream)
    if any(stream.media_type == "audio" for stream in streams):
        for stream in streams:
            if stream.media_type == "video":
                stream.extra["muxed_audio"] = False
    streams.extend(parse_sabr_ump_streams(uri, title, title_meta, existing_streams=streams))
    return streams


def _nested_hls_streams(
    uri: str,
    item: dict[str, Any],
    media_type: str,
    title_meta: dict[str, Any],
    headers: dict[str, str] | None,
) -> list[StreamInfo] | None:
    manifest_url = _str_or_none(item.get("manifest_url") or item.get("manifestUrl"))
    if not manifest_url:
        return None
    manifest_url = join_uri(uri, manifest_url)
    from ..loader import LoadError, load_text
    from .hls import parse_hls

    try:
        resource = load_text(manifest_url, headers=headers)
        nested = parse_hls(
            resource.uri,
            resource.text or "",
            headers=headers,
            fetch_child_playlists=True,
        )
    except (LoadError, ValueError) as exc:
        raise ValueError(f"Failed to parse nested JSON {media_type} manifest: {exc}") from exc
    expected = [stream for stream in nested if stream.media_type == media_type]
    if not expected and len(nested) == 1:
        nested[0].media_type = media_type
        expected = nested
    for stream in expected:
        stream.extra = {**title_meta, **(stream.extra or {}), "source": "json"}
        if not stream.id:
            stream.id = _track_id(item)
        if media_type == "video":
            stream.codecs = stream.codecs or _str_or_none(item.get("codec") or item.get("codecs"))
            explicit_range = _video_range(item, stream.codecs)
            if explicit_range:
                stream.video_range = explicit_range
        elif media_type == "audio":
            stream.language = stream.language or _language(item)
            stream.channels = stream.channels or _str_or_none(item.get("channels"))
            stream.codecs = stream.codecs or _str_or_none(item.get("codec") or item.get("codecs"))
    return expected


def _track_items(title: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key in keys:
        value = title.get(key)
        if isinstance(value, list):
            result.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            result.extend(item for item in value.values() if isinstance(item, dict))
    return result


def _video_stream(uri: str, title: dict[str, Any], item: dict[str, Any], title_meta: dict[str, Any]) -> StreamInfo | None:
    url = _primary_url(item)
    if not url:
        return None
    url = join_uri(uri, url)
    codec = _str_or_none(item.get("codec") or item.get("codecs"))
    kid = _normalize_kid(item.get("kid") or item.get("key_id") or item.get("keyId"))
    encrypted = _encrypted(item, codec, kid)
    scheme = _encryption_scheme(item, codec, encrypted)
    dvr_sequence = _dvr_sequence_info(title, url)
    segments = _track_segments_from_live_template(title, item, url, encrypted, scheme, kid)
    size_bytes = _item_size_bytes(item, url)
    if segments is None:
        segments = _track_segments_from_dvr_sequence_window(title, url, encrypted, scheme, kid)
    if segments is None and dvr_sequence:
        segments = []
    if segments is None:
        segments = _track_segments_from_item(item, url, encrypted, scheme, kid, size_bytes)
    width = _int_or_none(item.get("width"))
    height = _int_or_none(item.get("height"))
    duration = _item_duration(title, item, url) or _segments_duration(segments)
    return StreamInfo(
        manifest_type="json",
        media_type="video",
        url=url,
        original_url=uri,
        id=_track_id(item),
        group_id=_str_or_none(item.get("group_id") or item.get("groupId")),
        role=_video_role(item, codec),
        bandwidth=_bitrate(item),
        codecs=codec,
        resolution=f"{width}x{height}" if width and height else _str_or_none(item.get("resolution")),
        frame_rate=_float_or_none(item.get("fps") or item.get("frame_rate") or item.get("frameRate")),
        extension=_extension(item, url, "video"),
        video_range=_video_range(item, codec),
        duration=duration,
        size_bytes=size_bytes,
        encrypted=encrypted,
        encryption_scheme=scheme,
        is_live=_json_track_is_live(title),
        segments=segments,
        extra=_extra(title_meta, item, dvr_sequence=dvr_sequence),
    )


def _audio_stream(uri: str, title: dict[str, Any], item: dict[str, Any], title_meta: dict[str, Any]) -> StreamInfo | None:
    url = _primary_url(item)
    if not url:
        return None
    url = join_uri(uri, url)
    codec = _str_or_none(item.get("codec") or item.get("codecs"))
    kid = _normalize_kid(item.get("kid") or item.get("key_id") or item.get("keyId"))
    encrypted = _encrypted(item, codec, kid)
    scheme = _encryption_scheme(item, codec, encrypted)
    dvr_sequence = _dvr_sequence_info(title, url)
    segments = _track_segments_from_live_template(title, item, url, encrypted, scheme, kid)
    size_bytes = _item_size_bytes(item, url)
    if segments is None:
        segments = _track_segments_from_dvr_sequence_window(title, url, encrypted, scheme, kid)
    if segments is None and dvr_sequence:
        segments = []
    if segments is None:
        segments = _track_segments_from_item(item, url, encrypted, scheme, kid, size_bytes)
    duration = _item_duration(title, item, url) or _segments_duration(segments)
    return StreamInfo(
        manifest_type="json",
        media_type="audio",
        url=url,
        original_url=uri,
        id=_track_id(item),
        group_id=_str_or_none(item.get("group_id") or item.get("groupId")),
        language=_language(item),
        role=_audio_role(item),
        bandwidth=_bitrate(item),
        codecs=codec,
        channels=_str_or_none(item.get("channels")),
        extension=_extension(item, url, "audio"),
        duration=duration,
        size_bytes=size_bytes,
        encrypted=encrypted,
        encryption_scheme=scheme,
        is_live=_json_track_is_live(title),
        segments=segments,
        extra=_extra(title_meta, item, dvr_sequence=dvr_sequence),
    )


def _subtitle_stream(uri: str, title: dict[str, Any], item: dict[str, Any], title_meta: dict[str, Any]) -> StreamInfo | None:
    url = _primary_url(item)
    if not url:
        return None
    url = join_uri(uri, url)
    codec = _str_or_none(item.get("codec") or item.get("codecs") or item.get("format"))
    kid = _normalize_kid(item.get("kid") or item.get("key_id") or item.get("keyId"))
    encrypted = _encrypted(item, codec, kid)
    scheme = _encryption_scheme(item, codec, encrypted)
    segments = _track_segments_from_live_template(title, item, url, encrypted, scheme, kid)
    size_bytes = _item_size_bytes(item, url)
    if segments is None:
        segments = _track_segments_from_item(item, url, encrypted, scheme, kid, size_bytes)
    duration = _item_duration(title, item, url) or _segments_duration(segments)
    return StreamInfo(
        manifest_type="json",
        media_type="subtitle",
        url=url,
        original_url=uri,
        id=_track_id(item),
        group_id=_str_or_none(item.get("group_id") or item.get("groupId")),
        language=_language(item),
        role=_subtitle_role(item),
        codecs=codec,
        extension=_extension(item, url, "subtitle"),
        duration=duration,
        size_bytes=size_bytes,
        encrypted=encrypted,
        encryption_scheme=scheme,
        is_live=_json_track_is_live(title),
        segments=segments,
        extra=_extra(title_meta, item),
    )


def _parse_generic_url_tracks(uri: str, data: Any) -> list[StreamInfo]:
    streams: list[StreamInfo] = []
    for index, item in enumerate(_walk_dicts(data), start=1):
        url = _primary_url(item)
        if not url:
            continue
        media_type = _infer_media_type(item)
        codec = _str_or_none(item.get("codec") or item.get("codecs") or item.get("format") or item.get("profile"))
        kid = _normalize_kid(item.get("kid") or item.get("key_id") or item.get("keyId"))
        encrypted = _encrypted(item, codec, kid)
        scheme = _encryption_scheme(item, codec, encrypted)
        full_url = join_uri(uri, url)
        size_bytes = _item_size_bytes(item, full_url)
        width = _int_or_none(item.get("width"))
        height = _int_or_none(item.get("height"))
        segments = _track_segments_from_item(item, full_url, encrypted, scheme, kid, size_bytes)
        streams.append(
            StreamInfo(
                manifest_type="json",
                media_type=media_type,
                url=full_url,
                original_url=uri,
                id=_track_id(item) or str(index),
                language=_language(item),
                role=(
                    _audio_role(item)
                    if media_type == "audio"
                    else _subtitle_role(item)
                    if media_type == "subtitle"
                    else _video_role(item, codec)
                ),
                bandwidth=_bitrate(item),
                codecs=codec,
                resolution=f"{width}x{height}" if width and height else _str_or_none(item.get("resolution")),
                frame_rate=_float_or_none(item.get("fps") or item.get("frame_rate") or item.get("frameRate")),
                channels=_str_or_none(item.get("channels")),
                extension=_extension(item, full_url, media_type),
                video_range=_video_range(item, codec) if media_type == "video" else None,
                duration=_item_duration({}, item, full_url) or _segments_duration(segments),
                size_bytes=size_bytes,
                encrypted=encrypted,
                encryption_scheme=scheme,
                segments=segments,
                extra={"source": "json", "raw": _compact_raw(item)},
            )
        )
    return streams


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _primary_url(item: dict[str, Any]) -> str | None:
    for key in ("url", "uri", "href"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("all_urls", "urls"):
        values = item.get(key)
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and value.strip():
                    return value.strip()
        elif isinstance(values, dict):
            for value in values.values():
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _track_segments_from_live_template(
    title: dict[str, Any],
    item: dict[str, Any],
    base_url: str,
    encrypted: bool,
    scheme: str | None,
    kid: str | None,
) -> list[SegmentInfo] | None:
    metadata = title.get("liveMetadata")
    if not isinstance(metadata, dict):
        return None
    downloadable_id = _track_id(item)
    template_id_map = metadata.get("downloadableIdToSegmentTemplateId")
    templates = metadata.get("segmentTemplateIdToSegmentTemplate")
    if not downloadable_id or not isinstance(template_id_map, dict) or not isinstance(templates, dict):
        return None
    template_id = template_id_map.get(str(downloadable_id))
    template = templates.get(str(template_id)) if template_id is not None else None
    if not isinstance(template, dict):
        return None
    media = _str_or_none(template.get("media"))
    if not media or "$Number$" not in media:
        return None
    timescale = _int_or_none(template.get("timescale"))
    duration_units = _int_or_none(template.get("duration"))
    start_number = _int_or_none(template.get("startNumber"))
    availability_start = _parse_json_time(template.get("availabilityStartTime"))
    if not timescale or not duration_units or start_number is None or availability_start is None:
        return None
    segment_seconds = duration_units / timescale
    if segment_seconds <= 0:
        return None

    first_number, last_number = _live_template_number_range(metadata, template, start_number, segment_seconds)
    if first_number is None or last_number is None or last_number < first_number:
        return None

    segments: list[SegmentInfo] = []
    initialization = _str_or_none(template.get("initialization"))
    if initialization:
        segments.append(
            SegmentInfo(
                url=_live_template_url(base_url, initialization),
                index=-1,
                encrypted=encrypted,
                encryption_scheme=scheme,
                key_id=kid,
            )
        )
    for number in range(first_number, last_number + 1):
        segments.append(
            SegmentInfo(
                url=_live_template_url(base_url, media.replace("$Number$", str(number))),
                duration=segment_seconds,
                index=number,
                encrypted=encrypted,
                encryption_scheme=scheme,
                key_id=kid,
            )
        )
    return segments


def _track_segments_from_item(
    item: dict[str, Any],
    base_url: str,
    encrypted: bool,
    scheme: str | None,
    kid: str | None,
    size_bytes: int | None,
) -> list[SegmentInfo]:
    explicit = item.get("segments")
    if isinstance(explicit, list):
        segments = _explicit_track_segments(explicit, base_url, encrypted, scheme, kid)
        if segments:
            return segments
    byte_range = (0, size_bytes - 1) if size_bytes and size_bytes > 0 and _track_declares_direct_size(item, base_url) else None
    return [SegmentInfo(url=base_url, index=0, byte_range=byte_range, encrypted=encrypted, encryption_scheme=scheme, key_id=kid)]


def _track_segments_from_dvr_sequence_window(
    title: dict[str, Any],
    base_url: str,
    encrypted: bool,
    scheme: str | None,
    kid: str | None,
) -> list[SegmentInfo] | None:
    if not _title_is_dvr_vod(title):
        return None
    first_sequence = _min_sq_from_url(base_url)
    last_sequence = _dvr_window_end_sequence(title)
    if first_sequence is None or last_sequence is None or last_sequence < first_sequence:
        return None
    duration = _dvr_segment_duration_seconds(title)
    return [
        SegmentInfo(
            url=_url_with_sq(base_url, sequence),
            duration=duration,
            index=sequence,
            encrypted=encrypted,
            encryption_scheme=scheme,
            key_id=kid,
        )
        for sequence in range(first_sequence, last_sequence + 1)
    ]


def _min_sq_from_url(url: str) -> int | None:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    for raw in params.get("min_sq", []):
        value = _int_or_none(raw)
        if value is not None:
            return value
    return None


def _url_with_sq(url: str, sequence: int) -> str:
    parsed = urlparse(url)
    params = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "sq"]
    params.append(("sq", str(int(sequence))))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(params), parsed.fragment))


def _dvr_sequence_info(title: dict[str, Any], base_url: str) -> dict[str, Any] | None:
    if not _title_is_dvr_vod(title):
        return None
    first_sequence = _min_sq_from_url(base_url)
    if first_sequence is None:
        return None
    info: dict[str, Any] = {
        "json_dvr_sequence": True,
        "json_dvr_sequence_start": first_sequence,
    }
    duration = _dvr_segment_duration_seconds(title)
    title_duration = _title_duration_seconds(title)
    if title_duration is not None:
        info["json_dvr_duration_seconds"] = title_duration
    if title_duration is not None:
        segment_duration = duration or _DEFAULT_DVR_SEGMENT_SECONDS
        max_count = _dvr_expected_sequence_count(title_duration, segment_duration)
        if max_count is not None:
            info["json_dvr_sequence_max_count"] = max_count
            info["json_dvr_sequence_max_probe_span"] = max(1, max_count - 1)
    if duration is None and title_duration is not None:
        duration = _DEFAULT_DVR_SEGMENT_SECONDS
    if duration is not None:
        info["json_dvr_segment_duration"] = duration
    end_hint = _dvr_end_sequence_hint(title)
    if end_hint is not None and end_hint >= first_sequence:
        info["json_dvr_sequence_end_hint"] = end_hint
    reliable_end = _dvr_window_end_sequence(title)
    if reliable_end is not None and reliable_end >= first_sequence:
        info["json_dvr_sequence_end"] = reliable_end
    return info


def _dvr_window_end_sequence(title: dict[str, Any]) -> int | None:
    server_abr = title.get("server_abr")
    if isinstance(server_abr, dict):
        for key in ("dvr_end_sequence", "dvrEndSequence", "sequence_end", "sequenceEnd"):
            value = _int_or_none(server_abr.get(key))
            if value is not None:
                return value
        inspection = server_abr.get("request_body_inspection")
        if isinstance(inspection, dict):
            window = inspection.get("sequence_window") or inspection.get("sequenceWindow")
            if isinstance(window, dict):
                for key in ("end", "last", "last_sequence", "lastSequence", "max", "max_sequence", "maxSequence"):
                    value = _int_or_none(window.get(key))
                    if value is not None:
                        return value
    for key in ("dvr_end_sequence", "dvrEndSequence", "sequence_end", "sequenceEnd"):
        value = _int_or_none(title.get(key))
        if value is not None:
            return value
    return None


def _dvr_end_sequence_hint(title: dict[str, Any]) -> int | None:
    records = _dvr_segment_records(title)
    values = [_int_or_none(record.get("last_sequence") or record.get("lastSequence") or record.get("first_sequence") or record.get("firstSequence")) for record in records]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _dvr_segment_duration_seconds(title: dict[str, Any]) -> float | None:
    durations: list[float] = []
    for record in _dvr_segment_records(title):
        duration_ms = _float_or_none(record.get("duration_ms") or record.get("durationMs"))
        first = _int_or_none(record.get("first_sequence") or record.get("firstSequence"))
        last = _int_or_none(record.get("last_sequence") or record.get("lastSequence")) or first
        if duration_ms is None or first is None or last is None:
            continue
        count = max(1, last - first + 1)
        durations.append(duration_ms / count / 1000.0)
    if not durations:
        return None
    durations.sort()
    return durations[len(durations) // 2]


def _dvr_segment_records(title: dict[str, Any]) -> list[dict[str, Any]]:
    server_abr = title.get("server_abr")
    if not isinstance(server_abr, dict):
        return []
    inspection = server_abr.get("request_body_inspection")
    if not isinstance(inspection, dict):
        return []
    records = inspection.get("segment_records")
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def _explicit_track_segments(
    values: list[Any],
    base_url: str,
    encrypted: bool,
    scheme: str | None,
    kid: str | None,
) -> list[SegmentInfo]:
    segments: list[SegmentInfo] = []
    for index, value in enumerate(values):
        if isinstance(value, str):
            segments.append(SegmentInfo(url=join_uri(base_url, value), index=index, encrypted=encrypted, encryption_scheme=scheme, key_id=kid))
            continue
        if not isinstance(value, dict):
            continue
        url = _primary_url(value) or base_url
        segment_kid = _normalize_kid(value.get("kid") or value.get("key_id") or value.get("keyId")) or kid
        segment_encrypted = _encrypted(value, None, segment_kid) or encrypted
        segment_scheme = _explicit_encryption_scheme(value) or scheme or ("ENC" if segment_encrypted else None)
        segments.append(
            SegmentInfo(
                url=join_uri(base_url, url),
                duration=_float_or_none(value.get("duration") or value.get("duration_seconds") or value.get("durationSeconds")),
                index=_int_or_none(value.get("index")) if value.get("index") not in {None, ""} else index,
                byte_range=_parse_byte_range_value(value.get("byte_range") or value.get("byteRange") or value.get("range")),
                encrypted=segment_encrypted,
                encryption_scheme=segment_scheme,
                key_id=segment_kid,
            )
        )
    return segments


def _explicit_encryption_scheme(item: dict[str, Any]) -> str | None:
    explicit = _str_or_none(item.get("encryption_scheme") or item.get("encryptionScheme") or item.get("scheme"))
    return explicit.upper() if explicit else None


def _live_template_number_range(
    metadata: dict[str, Any],
    template: dict[str, Any],
    start_number: int,
    segment_seconds: float,
) -> tuple[int | None, int | None]:
    availability_start = _parse_json_time(template.get("availabilityStartTime"))
    if availability_start is None:
        return None, None
    event_start = _parse_json_time(metadata.get("eventStartTime"))
    event_end = _parse_json_time(metadata.get("eventEndTime"))
    offset_seconds = max(0.0, _float_or_none(metadata.get("eventAvailabilityOffsetMs")) or 0.0) / 1000.0
    now_available_end = datetime.now(timezone.utc).timestamp() - offset_seconds
    if event_end is None or event_end.timestamp() > now_available_end:
        end = now_available_end
        window = _float_or_none(metadata.get("ocLiveWindowDurationSeconds"))
        start_base = event_start.timestamp() if event_start else availability_start.timestamp()
        start = max(start_base, end - window) if window else start_base
    else:
        start = event_start.timestamp() if event_start else availability_start.timestamp()
        end = event_end.timestamp()
    available = availability_start.timestamp()
    first = start_number + math.floor(max(0.0, start - available) / segment_seconds)
    last = start_number + math.ceil(max(0.0, end - available) / segment_seconds) - 1
    return first, last


def _live_template_url(base_url: str, relative_path: str) -> str:
    parsed = urlparse(base_url)
    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/{relative_path.lstrip('/')}" if base_path else f"/{relative_path.lstrip('/')}"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


def _json_track_is_live(title: dict[str, Any]) -> bool:
    if title.get("is_live") is not None or title.get("isLive") is not None:
        return bool(title.get("is_live") if title.get("is_live") is not None else title.get("isLive"))
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
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _segments_duration(segments: list[SegmentInfo]) -> float | None:
    total = sum(segment.duration or 0 for segment in segments if segment.index != -1)
    return total or None


def _item_duration(title: dict[str, Any], item: dict[str, Any], url: str) -> float | None:
    seconds = _positive_float_or_none(item.get("duration") or item.get("duration_seconds") or item.get("durationSeconds"))
    if seconds is not None:
        return seconds
    milliseconds = _positive_float_or_none(item.get("duration_ms") or item.get("durationMs"))
    if milliseconds is not None:
        return milliseconds / 1000.0
    query_duration = _query_float(url, "dur")
    if query_duration is not None and query_duration > 0:
        return query_duration
    return _title_duration_seconds(title)


def _item_size_bytes(item: dict[str, Any], url: str) -> int | None:
    return (
        _int_or_none(
            item.get("size")
            or item.get("size_bytes")
            or item.get("sizeBytes")
            or item.get("content_length")
            or item.get("contentLength")
            or item.get("clen")
        )
        or _query_int(url, "clen")
    )


def _track_declares_direct_size(item: dict[str, Any], url: str) -> bool:
    if any(key in item for key in ("size", "size_bytes", "sizeBytes", "content_length", "contentLength", "clen")):
        return True
    return _query_int(url, "clen") is not None


def _query_int(url: str, key: str) -> int | None:
    values = parse_qs(urlparse(url).query).get(key)
    return _int_or_none(values[0]) if values else None


def _query_float(url: str, key: str) -> float | None:
    values = parse_qs(urlparse(url).query).get(key)
    return _float_or_none(values[0]) if values else None


def _title_duration_seconds(title: dict[str, Any]) -> float | None:
    seconds = _positive_float_or_none(title.get("duration") or title.get("duration_seconds") or title.get("durationSeconds"))
    if seconds is not None:
        return seconds
    milliseconds = _positive_float_or_none(title.get("duration_ms") or title.get("durationMs"))
    if milliseconds is not None:
        return milliseconds / 1000.0
    for key in ("version_secondary", "versionSecondary", "duration_text", "durationText", "subtitle", "secondary"):
        parsed = _parse_duration_text(title.get(key))
        if parsed is not None:
            return parsed
    return _youtube_player_params_duration_seconds(title)


def _youtube_player_params_duration_seconds(title: dict[str, Any]) -> float | None:
    if not _title_is_dvr_vod(title):
        return None
    for key in ("params", "player_params", "playerParams"):
        data = _decode_youtube_params_blob(title.get(key))
        if not data:
            continue
        duration = _youtube_params_duration_from_proto(data)
        if duration is not None:
            return duration
    return None


def _decode_youtube_params_blob(value: Any) -> bytes | None:
    text = _str_or_none(value)
    if not text:
        return None
    unquoted = text.replace("%3D", "=").replace("%3d", "=")
    padding = "=" * (-len(unquoted) % 4)
    try:
        return base64.urlsafe_b64decode((unquoted + padding).encode("ascii"))
    except Exception:
        return None


def _youtube_params_duration_from_proto(data: bytes) -> float | None:
    values = _proto_positive_varints(data, max_depth=3)
    epochish = [value for _field, value in values if 1_000_000_000 <= value <= 4_000_000_000]
    if not epochish:
        return None
    durations = [value for field, value in values if field == 2 and 30 <= value <= 24 * 3600]
    if not durations:
        return None
    return float(max(durations))


def _proto_positive_varints(data: bytes, *, max_depth: int, _depth: int = 0) -> list[tuple[int, int]]:
    values: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(data):
        key, cursor = _read_proto_varint(data, cursor)
        if key is None:
            break
        field = key >> 3
        wire_type = key & 0x07
        if field <= 0:
            break
        if wire_type == 0:
            value, cursor = _read_proto_varint(data, cursor)
            if value is None:
                break
            if value > 0:
                values.append((field, value))
        elif wire_type == 1:
            cursor += 8
        elif wire_type == 2:
            length, cursor = _read_proto_varint(data, cursor)
            if length is None or length < 0 or cursor + length > len(data):
                break
            nested = data[cursor : cursor + length]
            cursor += length
            if _depth < max_depth:
                values.extend(_proto_positive_varints(nested, max_depth=max_depth, _depth=_depth + 1))
        elif wire_type == 5:
            cursor += 4
        else:
            break
    return values


def _read_proto_varint(data: bytes, offset: int) -> tuple[int | None, int]:
    value = 0
    shift = 0
    cursor = offset
    while cursor < len(data) and shift <= 63:
        byte = data[cursor]
        cursor += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, cursor
        shift += 7
    return None, offset


def _parse_duration_text(value: Any) -> float | None:
    text = _str_or_none(value)
    if not text:
        return None
    prefix = text.split("•", 1)[0].strip()
    if not prefix:
        prefix = text.strip()
    clock = _DURATION_TEXT_CLOCK.search(prefix)
    if clock:
        hours_text, minutes_text, seconds_text = clock.groups()
        hours = int(hours_text or 0)
        minutes = int(minutes_text or 0)
        seconds = int(seconds_text or 0)
        total = hours * 3600 + minutes * 60 + seconds
        return float(total) if total > 0 else None
    total = 0.0
    matched = False
    for match in _DURATION_TEXT_UNITS.finditer(prefix):
        amount = float(match.group("value"))
        unit = match.group("unit").lower()
        if unit.startswith("h"):
            total += amount * 3600.0
        elif unit.startswith("m"):
            total += amount * 60.0
        elif unit.startswith("s"):
            total += amount
        matched = True
    return total if matched and total > 0 else None


def _dvr_expected_sequence_count(duration_seconds: float | None, segment_seconds: float | None) -> int | None:
    if duration_seconds is None or duration_seconds <= 0:
        return None
    segment_seconds = segment_seconds or _DEFAULT_DVR_SEGMENT_SECONDS
    if segment_seconds <= 0:
        segment_seconds = _DEFAULT_DVR_SEGMENT_SECONDS
    return max(1, int(math.ceil(duration_seconds / segment_seconds)) + _DVR_DURATION_MARGIN_SEGMENTS)


def _positive_float_or_none(value: Any) -> float | None:
    number = _float_or_none(value)
    return number if number is not None and number > 0 else None


def _parse_byte_range_value(value: Any) -> tuple[int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        start = _int_or_none(value[0])
        end = _int_or_none(value[1])
    elif isinstance(value, dict):
        start = _int_or_none(value.get("start"))
        end = _int_or_none(value.get("end"))
    elif isinstance(value, str):
        text = value.strip()
        if "-" not in text:
            return None
        left, right = text.split("-", 1)
        start = _int_or_none(left)
        end = _int_or_none(right)
    else:
        return None
    if start is None or end is None or end < start:
        return None
    return start, end


def _track_id(item: dict[str, Any]) -> str | None:
    return _str_or_none(item.get("id") or item.get("track_id") or item.get("trackId") or item.get("downloadable_id"))


def _title_meta(title: dict[str, Any]) -> dict[str, Any]:
    season = _int_or_none(title.get("season"))
    episode = _int_or_none(title.get("episode"))
    show = _str_or_none(title.get("name") or title.get("title"))
    episode_name = _str_or_none(title.get("episode_name") or title.get("episodeName"))
    duration = _title_duration_seconds(title)
    live_metadata = title.get("liveMetadata")
    hls_manifest_url = _str_or_none(live_metadata.get("hls_manifest_url") or live_metadata.get("hlsManifestUrl")) if isinstance(live_metadata, dict) else None
    parts = [show]
    if season is not None and episode is not None:
        parts.append(f"S{season:02d}E{episode:02d}")
    elif episode is not None:
        parts.append(f"E{episode:02d}")
    if episode_name:
        parts.append(episode_name)
    metadata = {
        "title_id": _str_or_none(title.get("id")),
        "title": " ".join(part for part in parts if part),
        "name": show,
        "season": season,
        "episode": episode,
        "episode_name": episode_name,
        "duration_seconds": duration,
        "original_language": _str_or_none(title.get("original_language") or title.get("originalLanguage")),
        "title_source": _str_or_none(title.get("source") or title.get("playback_source") or title.get("playbackSource")),
        "hls_manifest_url": hls_manifest_url,
        "source": "json",
    }
    audio_metadata = audio_metadata_from_title(title)
    if audio_metadata:
        metadata["audio_metadata"] = audio_metadata
    return metadata


def _extra(title_meta: dict[str, Any], item: dict[str, Any], *, dvr_sequence: dict[str, Any] | None = None) -> dict[str, Any]:
    extra = dict(title_meta)
    if dvr_sequence:
        extra.update(dvr_sequence)
    all_urls = item.get("all_urls") or item.get("urls")
    if all_urls:
        extra["all_urls"] = all_urls
    raw = _compact_raw(item)
    if raw:
        extra["raw"] = raw
    return extra


def _compact_raw(item: dict[str, Any]) -> dict[str, Any]:
    skipped = {"url", "uri", "href", "all_urls", "urls"}
    return {key: value for key, value in item.items() if key not in skipped}


def _infer_media_type(item: dict[str, Any]) -> str:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("kind", "type", "media_type", "mediaType", "codec", "codecs", "format", "profile")
    ).lower()
    if item.get("width") or item.get("height") or any(token in text for token in ("video", "hevc", "h264", "avc", "vp9", "av1")):
        return "video"
    if item.get("channels") or any(token in text for token in ("audio", "aac", "ddplus", "eac3", "ac3", "opus", "av3a", "audio-vivid")):
        return "audio"
    if any(token in text for token in ("subtitle", "text", "vtt", "webvtt", "ttml", "srt")):
        return "subtitle"
    return "video"


def _language(item: dict[str, Any]) -> str | None:
    return _str_or_none(item.get("language") or item.get("lang") or item.get("locale"))


def _video_role(item: dict[str, Any], codec: str | None) -> str | None:
    role = _str_or_none(item.get("role") or item.get("profile"))
    if role:
        return role
    text = (codec or "").lower()
    if "main" in text:
        return "Main"
    return None


def _audio_role(item: dict[str, Any]) -> str | None:
    roles = []
    descriptor = _audio_descriptor(item)
    if descriptor:
        roles.append(descriptor)
    if _bool(item.get("is_original") or item.get("original")):
        roles.append("Original")
    if _bool(item.get("descriptive") or item.get("description")):
        roles.append("Descriptive")
    explicit_role = _str_or_none(item.get("role"))
    if roles:
        return " ".join(roles)
    if explicit_role:
        return explicit_role
    if _bool(item.get("is_default") or item.get("isDefault") or item.get("default")):
        return "Default"
    return None


def _audio_descriptor(item: dict[str, Any]) -> str | None:
    explicit = _str_or_none(item.get("audio_descriptor") or item.get("audioDescriptor") or item.get("audio_description"))
    if explicit:
        return _normalize_audio_descriptor(explicit)
    url = _primary_url(item) or ""
    if url:
        params = parse_qs(urlparse(url).query)
        xtags = " ".join(params.get("xtags", []))
        match = re.search(r"(?:^|[;&,\s])acont=([A-Za-z0-9_-]+)", xtags)
        if match:
            return _normalize_audio_descriptor(match.group(1))
    return None


def _normalize_audio_descriptor(value: str) -> str:
    normalized = value.strip().replace("_", " ").replace("-", " ")
    lowered = normalized.lower()
    if lowered == "primary":
        return "Primary"
    if lowered == "secondary":
        return "Secondary"
    if lowered in {"main", "default"}:
        return lowered.title()
    return normalized


def _subtitle_role(item: dict[str, Any]) -> str | None:
    roles = []
    if _bool(item.get("forced")):
        roles.append("Forced")
    if _bool(item.get("sdh") or item.get("cc")):
        roles.append("SDH")
    if _bool(item.get("is_original") or item.get("original")):
        roles.append("Original")
    return " ".join(roles) or _str_or_none(item.get("role"))


def _video_range(item: dict[str, Any], codec: str | None) -> str | None:
    explicit = _str_or_none(item.get("video_range") or item.get("videoRange") or item.get("range") or item.get("dynamic_range"))
    if explicit:
        explicit = _normalize_video_range(explicit) or explicit
    text = " ".join(
        str(part or "")
        for part in [
            codec,
            item.get("hdr"),
            item.get("hdr10plus"),
            item.get("hdr10_plus"),
            item.get("profile"),
            item.get("supplemental_codecs"),
            item.get("supplementalCodecs"),
        ]
    ).lower()
    has_dv = any(token in text for token in ("dolby", "dovi", "dvhe", "dvh1"))
    inferred = _infer_video_range_from_text(text)
    if has_dv:
        if explicit and explicit not in {"SDR", "DV"}:
            return f"DV+{explicit}"
        return f"DV+{inferred}" if inferred and inferred != "SDR" else "DV"
    if explicit:
        return explicit
    return inferred or "SDR"


def _normalize_video_range(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower().replace("_", "-").replace(" ", "-")
    if normalized in {"dv", "dovi", "dolby-vision", "dolbyvision"}:
        return "DV"
    if normalized in {"hdr10+", "hdr10plus", "hdr10-plus", "hdr10p"}:
        return "HDR10+"
    if normalized in {"pq", "hdr", "hdr10", "smpte-2084", "smpte:2084", "st2084"}:
        return "HDR10"
    if normalized == "hlg":
        return "HLG"
    if normalized == "sdr":
        return "SDR"
    return value.strip().upper()


def _infer_video_range_from_text(value: str) -> str | None:
    normalized = value.lower().replace("_", "-").replace(" ", "-")
    if any(token in normalized for token in ("hdr10+", "hdr10plus", "hdr10-plus", "hdr10p", "2094-40")):
        return "HDR10+"
    if "hlg" in normalized or "arib-std-b67" in normalized:
        return "HLG"
    if any(token in normalized for token in ("hdr10", "hdr", "pq", "smpte-2084", "smpte:2084", "st2084", "bt2020")):
        return "HDR10"
    if "sdr" in normalized:
        return "SDR"
    return None


def _encrypted(item: dict[str, Any], codec: str | None, kid: str | None) -> bool:
    if _bool(item.get("encrypted")) or _bool(item.get("drm")) or kid:
        return True
    text = (codec or "").lower()
    return any(token in text for token in ("cenc", "cbcs", "prk"))


def _encryption_scheme(item: dict[str, Any], codec: str | None, encrypted: bool) -> str | None:
    if not encrypted:
        return None
    explicit = _str_or_none(item.get("encryption_scheme") or item.get("encryptionScheme") or item.get("scheme"))
    if explicit:
        return explicit.upper()
    text = (codec or "").lower()
    if "cbcs" in text:
        return "CBCS"
    if "cenc" in text:
        return "CENC"
    return "ENC"


def _extension(item: dict[str, Any], url: str, media_type: str) -> str | None:
    explicit = _str_or_none(item.get("extension") or item.get("ext"))
    if explicit:
        return explicit.strip(".").lower()
    mime_type = (_str_or_none(item.get("mime_type") or item.get("mimeType")) or "").lower()
    if "webm" in mime_type:
        return "webm"
    if "mp4" in mime_type or "mpeg4" in mime_type:
        return "mp4"
    parsed_path = urlparse(url).path
    name = parsed_path.rstrip("/").rsplit("/", 1)[-1]
    if "." in name:
        return name.rsplit(".", 1)[-1].lower()
    codec = (_str_or_none(item.get("codec") or item.get("codecs") or item.get("format")) or "").lower()
    if media_type == "subtitle":
        if "srt" in codec:
            return "srt"
        if any(token in codec for token in ("ttml", "dfxp", "stpp")):
            return "ttml"
        return "vtt"
    if media_type == "audio" and any(token in codec for token in ("aac", "ddplus", "eac3", "ac3", "opus", "av3a", "audio-vivid")):
        return "mp4"
    return "mp4"


def _bitrate(item: dict[str, Any]) -> int | None:
    bitrate = _int_or_none(item.get("bitrate") or item.get("bandwidth") or item.get("bit_rate"))
    if bitrate and bitrate < 10_000:
        return bitrate * 1000
    return bitrate


def _normalize_kid(value: Any) -> str | None:
    text = _str_or_none(value)
    if not text:
        return None
    cleaned = text.lower().replace("-", "").strip()
    return cleaned if cleaned else None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None
