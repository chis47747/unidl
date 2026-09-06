from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse

from ..loader import LoadError, load_text
from ..models import SegmentInfo, StreamInfo
from ..utils import join_uri, looks_like_h266, pretty_codec, uri_basename
from ..video_range import combine_dolby_vision_range


@dataclass(slots=True)
class MediaPlaylistSummary:
    segments: list[SegmentInfo]
    duration: float | None
    encrypted: bool
    encryption_scheme: str | None
    is_live: bool
    extension: str | None
    media_sequence: int = 0
    target_duration: float | None = None


@dataclass(slots=True)
class HlsKeyInfo:
    encrypted: bool = False
    encryption_scheme: str | None = None
    key_id: str | None = None
    key_uri: str | None = None
    key_iv: bytes | None = None


def parse_hls(
    uri: str,
    text: str,
    headers: dict[str, str] | None = None,
    fetch_child_playlists: bool = True,
) -> list[StreamInfo]:
    lines = _meaningful_lines(text)
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("Not an M3U/M3U8 playlist.")

    if _is_master(lines):
        return _parse_master(uri, lines, headers=headers, fetch_child_playlists=fetch_child_playlists)
    if _looks_like_hls_media(lines):
        summary = _parse_media_playlist(uri, lines)
        media_type = _infer_media_type_from_segments(summary.segments)
        codecs = "H.266" if media_type == "video" and looks_like_h266(uri) else None
        return [
            StreamInfo(
                manifest_type="hls",
                media_type=media_type,
                url=uri,
                original_url=uri,
                id=uri_basename(uri),
                name=uri_basename(uri),
                codecs=codecs,
                duration=summary.duration,
                extension=summary.extension,
                encrypted=summary.encrypted,
                encryption_scheme=summary.encryption_scheme,
                is_live=summary.is_live,
                segments=summary.segments,
                extra={
                    "media_sequence": summary.media_sequence,
                    "target_duration": summary.target_duration,
                    "key_id": _summary_or_session_key_id(summary, None),
                    "key_ids": _key_ids_from_summary_or_session(summary, None),
                    "muxed_audio": media_type == "video" and summary.extension in {"ts", "m2ts", "mts", "bbts"},
                },
            )
        ]
    return _parse_plain_m3u(uri, lines)


def _parse_master(
    uri: str,
    lines: list[str],
    headers: dict[str, str] | None,
    fetch_child_playlists: bool,
) -> list[StreamInfo]:
    streams: list[StreamInfo] = []
    pending_variant: dict[str, str] | None = None
    session_key = _session_key_info(uri, lines)
    media_hints = _master_media_hints(lines)

    for line in lines[1:]:
        if line.startswith("#EXT-X-SESSION-KEY:"):
            continue
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attribute_list(line.split(":", 1)[1])
            media_type = _hls_media_type(attrs.get("TYPE"))
            child_uri = attrs.get("URI")
            if not child_uri:
                continue
            child_url = join_uri(uri, child_uri)
            summary = _load_child_summary(child_url, headers, fetch_child_playlists)
            hint = media_hints.get((media_type, attrs.get("GROUP-ID") or ""))
            media_session_key = session_key if media_type not in {"subtitle", "subtitles", "text"} else None
            _apply_session_key_to_summary(summary, media_session_key)
            codecs = _hls_media_codec(attrs, child_url, summary, hint, media_type)
            audio_atmos = _hls_audio_is_atmos(attrs, child_url, hint)
            if media_type == "audio" and audio_atmos and pretty_codec(codecs, "audio") == "AC-3":
                codecs = "ec-3"
            streams.append(
                StreamInfo(
                    manifest_type="hls",
                    media_type=media_type,
                    url=child_url,
                    original_url=uri,
                    id=attrs.get("GROUP-ID") or attrs.get("NAME") or uri_basename(child_url),
                    group_id=attrs.get("GROUP-ID"),
                    name=attrs.get("NAME"),
                    language=_hls_language(attrs.get("LANGUAGE")),
                    role=_role_from_hls_media(attrs, media_type),
                    bandwidth=_hls_media_bandwidth(attrs, child_url, hint),
                    codecs=codecs,
                    channels=_normalize_hls_channels(attrs.get("CHANNELS")),
                    duration=summary.duration if summary else None,
                    extension=summary.extension if summary else None,
                    encrypted=_summary_or_session_encrypted(summary, media_session_key),
                    encryption_scheme=_summary_or_session_scheme(summary, media_session_key),
                    is_live=summary.is_live if summary else False,
                    segments=summary.segments if summary else [],
                    extra={
                        "default": attrs.get("DEFAULT"),
                        "autoselect": attrs.get("AUTOSELECT"),
                        "media_sequence": summary.media_sequence if summary else None,
                        "target_duration": summary.target_duration if summary else None,
                        "key_id": _summary_or_session_key_id(summary, media_session_key),
                        "key_ids": _key_ids_from_summary_or_session(summary, media_session_key),
                        "audio_atmos": 1 if media_type == "audio" and audio_atmos else None,
                        "channels_raw": attrs.get("CHANNELS"),
                    },
                )
            )
        elif line.startswith("#EXT-X-I-FRAME-STREAM-INF:"):
            pending_variant = None
            continue
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending_variant = parse_attribute_list(line.split(":", 1)[1])
        elif line.startswith("#"):
            continue
        elif pending_variant is not None:
            streams.append(_stream_from_variant(uri, line, pending_variant, headers, fetch_child_playlists, session_key))
            pending_variant = None

    return _dedupe_master_streams(streams)


def _master_media_hints(lines: list[str]) -> dict[tuple[str, str], dict[str, str]]:
    hints: dict[tuple[str, str], dict[str, str]] = {}
    pending_variant: dict[str, str] | None = None
    for line in lines[1:]:
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending_variant = parse_attribute_list(line.split(":", 1)[1])
            continue
        if line.startswith("#") or pending_variant is None:
            continue
        for attr_name, media_type in (("AUDIO", "audio"), ("VIDEO", "video")):
            group_id = pending_variant.get(attr_name)
            if not group_id:
                continue
            hint = hints.setdefault((media_type, group_id), {})
            codec = _codec_token_for_media_type(pending_variant.get("CODECS"), media_type)
            if codec and not hint.get("codecs"):
                hint["codecs"] = codec
        pending_variant = None
    return hints


def _dedupe_master_streams(streams: list[StreamInfo]) -> list[StreamInfo]:
    deduped: list[StreamInfo] = []
    seen: dict[tuple, StreamInfo] = {}
    for stream in streams:
        key = _master_stream_key(stream)
        existing = seen.get(key)
        if existing is None:
            seen[key] = stream
            deduped.append(stream)
            continue
        _merge_duplicate_master_stream(existing, stream)
    return deduped


def _master_stream_key(stream: StreamInfo) -> tuple:
    if stream.media_type == "video":
        return (
            stream.media_type,
            stream.url,
            stream.resolution,
            stream.frame_rate,
            stream.video_range,
            _video_codec_token(stream.codecs),
        )
    if stream.url:
        return (
            stream.media_type,
            stream.url,
            stream.language,
            stream.name,
            stream.channels,
        )
    return (
        stream.media_type,
        stream.group_id,
        stream.language,
        stream.name,
    )


def _merge_duplicate_master_stream(target: StreamInfo, duplicate: StreamInfo) -> None:
    if not target.bandwidth and duplicate.bandwidth:
        target.bandwidth = duplicate.bandwidth
    if not target.codecs and duplicate.codecs:
        target.codecs = duplicate.codecs
    if not target.role and duplicate.role:
        target.role = duplicate.role
    if not target.group_id and duplicate.group_id:
        target.group_id = duplicate.group_id
    group_ids = [
        item
        for item in [
            target.group_id,
            duplicate.group_id,
            *(target.extra.get("group_ids") or []),
            *(duplicate.extra.get("group_ids") or []),
        ]
        if item
    ]
    if len(group_ids) > 1:
        target.extra["group_ids"] = list(dict.fromkeys(group_ids))
    audio_ids = [
        item
        for item in [
            target.extra.get("audio_id"),
            duplicate.extra.get("audio_id"),
            *(target.extra.get("audio_ids") or []),
            *(duplicate.extra.get("audio_ids") or []),
        ]
        if item
    ]
    if audio_ids:
        target.extra["audio_ids"] = list(dict.fromkeys(audio_ids))


def _video_codec_token(codecs: str | None) -> str | None:
    for token in (codecs or "").split(","):
        token = token.strip().lower()
        if token.startswith(("avc1", "avc3", "dva1", "dvav", "hvc1", "hev1", "dvh1", "dvhe", "vp09", "vp9", "vp08", "vp8", "av01", "av1", "vvc1", "vvi1", "h266", "h.266", "vvc")):
            return token
    return None


def _session_key_info(uri: str, lines: list[str]) -> HlsKeyInfo | None:
    found: HlsKeyInfo | None = None
    found_with_key_id: HlsKeyInfo | None = None
    found_cmaf_drm: HlsKeyInfo | None = None
    for line in lines[1:]:
        if not line.startswith("#EXT-X-SESSION-KEY:"):
            continue
        attrs = parse_attribute_list(line.split(":", 1)[1])
        method = (attrs.get("METHOD") or "NONE").upper()
        if method == "NONE":
            continue
        info = HlsKeyInfo(
            encrypted=True,
            encryption_scheme=_normalize_encryption(method, attrs),
            key_id=_key_id_from_hls_key(attrs),
            key_uri=_hls_key_uri(uri, attrs.get("URI")),
            key_iv=_hls_key_iv(attrs.get("IV")),
        )
        if info.encryption_scheme in {"CBCS", "CENC"}:
            found_cmaf_drm = found_cmaf_drm or info
        if info.key_id:
            found_with_key_id = found_with_key_id or info
        found = found or info
    return found_cmaf_drm or found_with_key_id or found


def _summary_or_session_encrypted(summary: MediaPlaylistSummary | None, session_key: HlsKeyInfo | None) -> bool:
    if summary is not None:
        return bool(summary.encrypted)
    return bool(session_key and session_key.encrypted)


def _summary_or_session_scheme(summary: MediaPlaylistSummary | None, session_key: HlsKeyInfo | None) -> str | None:
    if summary is not None:
        if summary.encryption_scheme:
            return summary.encryption_scheme
        return session_key.encryption_scheme if summary.encrypted and session_key else None
    return session_key.encryption_scheme if session_key else None


def _summary_or_session_key_id(summary: MediaPlaylistSummary | None, session_key: HlsKeyInfo | None) -> str | None:
    segment_key_id = next((segment.key_id for segment in (summary.segments if summary else []) if segment.key_id), None)
    if segment_key_id:
        return segment_key_id
    if summary is not None and not summary.encrypted:
        return None
    return session_key.key_id if session_key else None


def _key_ids_from_summary_or_session(summary: MediaPlaylistSummary | None, session_key: HlsKeyInfo | None) -> list[str]:
    ids = [segment.key_id for segment in (summary.segments if summary else []) if segment.key_id]
    if not ids and session_key and session_key.key_id and (summary is None or summary.encrypted):
        ids.append(session_key.key_id)
    return list(dict.fromkeys(ids))


def _apply_session_key_to_summary(summary: MediaPlaylistSummary | None, session_key: HlsKeyInfo | None) -> None:
    if not summary or not session_key or not session_key.encrypted or not summary.encrypted:
        return
    summary_has_key_id = any(segment.key_id for segment in summary.segments)
    for segment in summary.segments:
        if not segment.encrypted and not segment.encryption_scheme:
            continue
        segment.encryption_scheme = segment.encryption_scheme or session_key.encryption_scheme
        if not summary_has_key_id:
            segment.key_id = segment.key_id or session_key.key_id
        segment.key_uri = segment.key_uri or session_key.key_uri
        segment.key_iv = segment.key_iv or session_key.key_iv


def _stream_from_variant(
    master_uri: str,
    child_reference: str,
    attrs: dict[str, str],
    headers: dict[str, str] | None,
    fetch_child_playlists: bool,
    session_key: HlsKeyInfo | None = None,
) -> StreamInfo:
    child_url = join_uri(master_uri, child_reference)
    codecs = attrs.get("CODECS")
    media_type = _infer_media_type_from_variant(attrs)
    if media_type == "video" and not pretty_codec(codecs, "video") and looks_like_h266(child_url, codecs):
        codecs = "H.266"
    summary = _load_child_summary(child_url, headers, fetch_child_playlists)
    _apply_session_key_to_summary(summary, session_key)
    bandwidth = _int_or_none(attrs.get("AVERAGE-BANDWIDTH")) or _int_or_none(attrs.get("BANDWIDTH"))
    stream = StreamInfo(
        manifest_type="hls",
        media_type=media_type,
        url=child_url,
        original_url=master_uri,
        id=attrs.get("NAME") or uri_basename(child_url),
        group_id=attrs.get("VIDEO") or attrs.get("AUDIO"),
        name=attrs.get("NAME"),
        bandwidth=bandwidth,
        codecs=codecs,
        resolution=attrs.get("RESOLUTION"),
        frame_rate=_float_or_none(attrs.get("FRAME-RATE")),
        video_range=_variant_video_range(attrs, child_reference) if media_type == "video" else None,
        role=_variant_role(child_url, media_type),
        duration=summary.duration if summary else None,
        extension=summary.extension if summary else None,
        encrypted=_summary_or_session_encrypted(summary, session_key),
        encryption_scheme=_summary_or_session_scheme(summary, session_key),
        is_live=summary.is_live if summary else False,
        segments=summary.segments if summary else [],
        extra={
            "audio_id": attrs.get("AUDIO"),
            "video_id": attrs.get("VIDEO"),
            "subtitle_id": attrs.get("SUBTITLES"),
            "muxed_audio": _variant_has_muxed_audio(attrs, media_type),
            "media_sequence": summary.media_sequence if summary else None,
            "target_duration": summary.target_duration if summary else None,
            "key_id": _summary_or_session_key_id(summary, session_key),
            "key_ids": _key_ids_from_summary_or_session(summary, session_key),
            "audio_atmos": 1 if media_type == "audio" and _hls_audio_is_atmos(attrs, child_url, None) else None,
            "channels_raw": attrs.get("CHANNELS"),
        },
    )
    if media_type == "video" and not stream.video_range:
        stream.video_range = "SDR"
    if media_type == "audio" and not stream.codecs:
        stream.codecs = codecs
    return stream


def _variant_role(child_url: str, media_type: str) -> str | None:
    if media_type != "video":
        return None
    if "trickplay" in child_url.lower():
        return "TrickPlay"
    return "Main"


def _variant_has_muxed_audio(attrs: dict[str, str], media_type: str) -> bool:
    if media_type != "video" or attrs.get("AUDIO"):
        return False
    return _codecs_have_audio(attrs.get("CODECS"))


def _codecs_have_audio(codecs: str | None) -> bool:
    return pretty_codec(codecs, "audio") in {"AAC", "HE-AAC", "AC-3", "E-AC-3", "E-AC-3 Atmos", "DTS", "DTS-HD", "DTS:X", "Opus"}


def _variant_video_range(attrs: dict[str, str], child_reference: str | None = None) -> str | None:
    explicit = _normalize_video_range(attrs.get("VIDEO-RANGE"))
    if _variant_has_hdr10plus(attrs, child_reference):
        explicit = "HDR10+"
    return combine_dolby_vision_range(attrs.get("CODECS"), explicit, attrs.get("SUPPLEMENTAL-CODECS"))


def _variant_has_hdr10plus(attrs: dict[str, str], child_reference: str | None = None) -> bool:
    text = " ".join(
        part
        for part in [
            attrs.get("VIDEO-RANGE"),
            attrs.get("CHARACTERISTICS"),
            attrs.get("SUPPLEMENTAL-CODECS"),
            child_reference,
        ]
        if part
    ).lower()
    compact = re.sub(r"[^a-z0-9]+", "", text)
    if any(token in compact for token in ("hdr10plus", "hdr10p")):
        return True
    supplemental = (attrs.get("SUPPLEMENTAL-CODECS") or "").lower()
    return bool(re.search(r"(?:^|[/,\s])cdm4(?:$|[/,\s])", supplemental))


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


def _load_child_summary(
    child_url: str,
    headers: dict[str, str] | None,
    fetch_child_playlists: bool,
) -> MediaPlaylistSummary | None:
    if not fetch_child_playlists:
        return None
    try:
        child = load_text(child_url, headers=headers)
    except LoadError:
        return None
    child_lines = _meaningful_lines(child.text or "")
    if not _looks_like_hls_media(child_lines):
        return None
    return _parse_media_playlist(child.uri, child_lines)


def _parse_media_playlist(uri: str, lines: list[str]) -> MediaPlaylistSummary:
    segments: list[SegmentInfo] = []
    current_duration: float | None = None
    current_key_scheme: str | None = None
    current_key_id: str | None = None
    current_key_uri: str | None = None
    current_key_iv: bytes | None = None
    encrypted = False
    is_live = True
    pending_range: tuple[int, int] | None = None
    previous_range_end = -1
    extension: str | None = None
    media_sequence = 0
    target_duration: float | None = None
    current_program_time: str | None = None
    pending_gap = False
    media_index = 0

    for line in lines[1:]:
        if line.startswith("#EXTINF:"):
            value = line.split(":", 1)[1].split(",", 1)[0]
            current_duration = _float_or_none(value)
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = _int_or_none(line.split(":", 1)[1].strip()) or 0
            media_index = media_sequence
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = _float_or_none(line.split(":", 1)[1].strip())
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            current_program_time = line.split(":", 1)[1].strip()
        elif line.startswith("#EXT-X-MAP:"):
            attrs = parse_attribute_list(line.split(":", 1)[1])
            map_uri = attrs.get("URI")
            if map_uri:
                byte_range, previous_range_end = _parse_hls_range(attrs.get("BYTERANGE", ""), previous_range_end)
                map_url = join_uri(uri, map_uri)
                map_key_id = current_key_id or _key_id_from_hls_map_uri(map_uri)
                segments.append(
                    SegmentInfo(
                        url=map_url,
                        duration=0,
                        index=-1,
                        byte_range=byte_range,
                        encrypted=current_key_scheme is not None or map_key_id is not None,
                        encryption_scheme=current_key_scheme,
                        key_id=map_key_id,
                        key_uri=current_key_uri,
                        key_iv=current_key_iv,
                    )
                )
        elif line.startswith("#EXT-X-KEY:") or line.startswith("#EXT-X-SESSION-KEY:"):
            attrs = parse_attribute_list(line.split(":", 1)[1])
            method = (attrs.get("METHOD") or "NONE").upper()
            if method == "NONE":
                current_key_scheme = None
                current_key_id = None
                current_key_uri = None
                current_key_iv = None
            else:
                current_key_scheme = _normalize_encryption(method, attrs)
                current_key_id = _key_id_from_hls_key(attrs) or current_key_id
                current_key_uri = _hls_key_uri(uri, attrs.get("URI")) or current_key_uri
                current_key_iv = _hls_key_iv(attrs.get("IV"))
                _apply_key_to_trailing_maps(segments, current_key_scheme, current_key_id, current_key_uri, current_key_iv)
                encrypted = True
        elif line.startswith("#EXT-X-GAP"):
            pending_gap = True
        elif line.startswith("#EXT-X-DISCONTINUITY"):
            _mark_previous_media_segment_discontinuity_after(segments)
        elif line.startswith("#EXT-X-BYTERANGE:"):
            raw_range = line.split(":", 1)[1].strip()
            pending_range, previous_range_end = _parse_hls_range(raw_range, previous_range_end)
        elif line.startswith("#EXT-X-ENDLIST"):
            is_live = False
        elif line.startswith("#"):
            continue
        else:
            absolute_url = join_uri(uri, line)
            query_range = _byte_range_from_segment_query(absolute_url) if pending_range is None else None
            extension = extension or _extension_from_segment(absolute_url)
            segment_is_bbts = _is_bbts_segment(absolute_url)
            segment_scheme = "BBTS" if segment_is_bbts else current_key_scheme
            segments.append(
                SegmentInfo(
                    url=absolute_url,
                    duration=current_duration,
                    index=media_index,
                    byte_range=pending_range or query_range,
                    allow_range_status_200=query_range is not None,
                    encrypted=segment_is_bbts or current_key_scheme is not None,
                    encryption_scheme=segment_scheme,
                    key_id=current_key_id,
                    key_uri=current_key_uri,
                    key_iv=current_key_iv,
                    program_date_time=current_program_time,
                    gap=pending_gap,
                )
            )
            media_index += 1
            current_duration = None
            current_program_time = None
            pending_range = None
            pending_gap = False

    duration = sum(segment.duration or 0 for segment in segments) or None
    summary = MediaPlaylistSummary(
        segments=segments,
        duration=duration,
        encrypted=encrypted or any(segment.encrypted for segment in segments),
        encryption_scheme=next((segment.encryption_scheme for segment in segments if segment.encryption_scheme), current_key_scheme),
        is_live=is_live,
        extension=extension,
        media_sequence=media_sequence,
        target_duration=target_duration,
    )
    return summary


def _mark_previous_media_segment_discontinuity_after(segments: list[SegmentInfo]) -> None:
    for segment in reversed(segments):
        if segment.index != -1:
            segment.discontinuity_after = True
            return


def _parse_plain_m3u(uri: str, lines: list[str]) -> list[StreamInfo]:
    streams: list[StreamInfo] = []
    pending_name: str | None = None
    for line in lines[1:]:
        if line.startswith("#EXTINF:"):
            pending_name = line.rsplit(",", 1)[-1].strip() or None
        elif line.startswith("#"):
            continue
        else:
            media_url = join_uri(uri, line)
            media_type = _infer_type_from_extension(media_url)
            streams.append(
                StreamInfo(
                    manifest_type="m3u",
                    media_type=media_type,
                    url=media_url,
                    original_url=uri,
                    id=pending_name or uri_basename(media_url),
                    name=pending_name or uri_basename(media_url),
                    extension=_extension_from_segment(media_url),
                    segments=[SegmentInfo(url=media_url, index=0)],
                )
            )
            pending_name = None
    return streams


def parse_attribute_list(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    i = 0
    while i < len(raw):
        while i < len(raw) and raw[i] in " ,\t":
            i += 1
        key_start = i
        while i < len(raw) and raw[i] not in "=":
            i += 1
        if i >= len(raw):
            break
        key = raw[key_start:i].strip()
        i += 1
        if i < len(raw) and raw[i] == '"':
            i += 1
            value_chars: list[str] = []
            while i < len(raw):
                if raw[i] == '"':
                    i += 1
                    break
                value_chars.append(raw[i])
                i += 1
            value = "".join(value_chars)
        else:
            value_start = i
            while i < len(raw) and raw[i] != ",":
                i += 1
            value = raw[value_start:i].strip()
        attrs[key.upper()] = value
        if i < len(raw) and raw[i] == ",":
            i += 1
    return attrs


def _meaningful_lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]


def _is_master(lines: list[str]) -> bool:
    return any(line.startswith(("#EXT-X-STREAM-INF:", "#EXT-X-I-FRAME-STREAM-INF:", "#EXT-X-MEDIA:")) for line in lines)


def _looks_like_hls_media(lines: list[str]) -> bool:
    return any(line.startswith(("#EXTINF:", "#EXT-X-TARGETDURATION:", "#EXT-X-MAP:", "#EXT-X-KEY:")) for line in lines)


def _hls_key_uri(base_uri: str, value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.lower().startswith("data:"):
        return value
    return join_uri(base_uri, value)


def _hls_key_iv(value: str | None) -> bytes | None:
    if not value:
        return None
    cleaned = value.strip().lower().replace("0x", "").replace(" ", "")
    if len(cleaned) != 32 or any(char not in "0123456789abcdef" for char in cleaned):
        return None
    return bytes.fromhex(cleaned)


def _key_id_from_hls_key(attrs: dict[str, str]) -> str | None:
    explicit = _clean_key_id(attrs.get("KEYID"))
    if explicit:
        return explicit
    uri = attrs.get("URI") or ""
    uri_kid = _key_id_from_hls_key_uri(uri)
    if uri_kid:
        return uri_kid
    key_format = (attrs.get("KEYFORMAT") or "").lower()
    if not uri.startswith("data:") or "," not in uri:
        return None
    try:
        data = base64.b64decode(uri.split(",", 1)[1])
    except Exception:
        return None
    if "playready" in key_format or "9a04f079" in key_format or b"WRMHEADER" in data:
        return _playready_kid_from_bytes(data)
    if "widevine" in key_format or "edef8ba9" in key_format:
        return _widevine_kid_from_bytes(data)
    return _playready_kid_from_bytes(data) or _widevine_kid_from_bytes(data)


def _key_id_from_hls_key_uri(uri: str) -> str | None:
    if not uri:
        return None
    parsed = urlparse(uri)
    if parsed.scheme.lower() == "skd":
        return _clean_key_id(parsed.netloc) or _clean_key_id(parsed.path.strip("/"))
    return None


def _key_id_from_hls_map_uri(uri: str) -> str | None:
    if not uri:
        return None
    parsed = urlparse(uri)
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        if name.lower() == "k" and (key_id := _clean_key_id(value)):
            return key_id
    match = re.search(
        r"(?:^|[/;,._-])k[=,_-]?([0-9a-f]{32})(?=$|[^0-9a-f])",
        parsed.path.lower(),
    )
    if not match:
        return None
    return _clean_key_id(match.group(1))


def _apply_key_to_trailing_maps(
    segments: list[SegmentInfo],
    key_scheme: str | None,
    key_id: str | None,
    key_uri: str | None,
    key_iv: bytes | None,
) -> None:
    for segment in reversed(segments):
        if segment.index != -1:
            break
        segment.encrypted = segment.encrypted or key_scheme is not None or key_id is not None
        if key_scheme and (
            not segment.encryption_scheme
            or (segment.encryption_scheme == "SAMPLE-AES" and key_scheme in {"CBCS", "CENC"})
        ):
            segment.encryption_scheme = key_scheme
        segment.key_id = segment.key_id or key_id
        segment.key_uri = segment.key_uri or key_uri
        segment.key_iv = segment.key_iv or key_iv


def _clean_key_id(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower().replace("0x", "").replace("-", "")
    if len(cleaned) == 32 and all(char in "0123456789abcdef" for char in cleaned):
        return cleaned
    return None


def _playready_kid_from_bytes(data: bytes) -> str | None:
    for encoding in ("utf-16-le", "utf-8"):
        text = data.decode(encoding, errors="ignore")
        for pattern in (r"<KID[^>]*VALUE=\"([^\"]+)\"", r"<KID>(.*?)</KID>"):
            match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
            if not match:
                continue
            try:
                kid = bytearray(base64.b64decode(match.group(1).strip()))
            except Exception:
                continue
            if len(kid) != 16:
                continue
            kid[0:4] = reversed(kid[0:4])
            kid[4:6] = reversed(kid[4:6])
            kid[6:8] = reversed(kid[6:8])
            return bytes(kid).hex()
    return None


def _widevine_kid_from_bytes(data: bytes) -> str | None:
    payload = _pssh_payload(data) or data
    marker = b"\x12\x10"
    index = payload.find(marker)
    if index < 0 or index + 18 > len(payload):
        return None
    return payload[index + 2 : index + 18].hex()


def _pssh_payload(data: bytes) -> bytes | None:
    pssh_at = data.find(b"pssh")
    if pssh_at < 4:
        return None
    start = pssh_at - 4
    size = int.from_bytes(data[start : start + 4], "big")
    if size < 32 or start + size > len(data):
        return None
    version = data[start + 8]
    position = start + 28
    if version > 0:
        if position + 4 > start + size:
            return None
        count = int.from_bytes(data[position : position + 4], "big")
        position += 4 + 16 * count
    if position + 4 > start + size:
        return None
    payload_size = int.from_bytes(data[position : position + 4], "big")
    position += 4
    return data[position : min(position + payload_size, start + size)]


def _hls_media_type(value: str | None) -> str:
    value = (value or "").upper().replace("-", "_")
    if value == "AUDIO":
        return "audio"
    if value in {"SUBTITLES", "CLOSED_CAPTIONS"}:
        return "subtitle"
    if value == "VIDEO":
        return "video"
    return "unknown"


def _hls_language(value: str | None) -> str | None:
    language = str(value or "").strip()
    if not language:
        return None
    if language.lower() in {"default", "none", "null", "unknown", "undefined"}:
        return "und"
    return language


def _infer_media_type_from_variant(attrs: dict[str, str]) -> str:
    codecs = attrs.get("CODECS") or ""
    if attrs.get("RESOLUTION"):
        return "video"
    pretty = pretty_codec(codecs, "audio")
    if pretty and pretty in {"AAC", "AC-3", "E-AC-3", "E-AC-3 Atmos", "DTS", "DTS-HD", "DTS:X", "Opus"} and "," not in codecs:
        return "audio"
    return "video"


def _infer_media_type_from_segments(segments: list[SegmentInfo]) -> str:
    if not segments:
        return "video"
    return _infer_type_from_extension(segments[0].url)


def _infer_type_from_extension(url: str) -> str:
    ext = (_extension_from_segment(url) or "").lower()
    if ext in {"mp3", "m4a", "aac", "flac", "wav"}:
        return "audio"
    if ext in {"vtt", "srt", "ttml", "dfxp"}:
        return "subtitle"
    return "video"


def _extension_from_segment(url: str) -> str | None:
    name = uri_basename(url).split("?", 1)[0]
    if "." not in name:
        return None
    return name.rsplit(".", 1)[-1].lower()


def _byte_range_from_segment_query(url: str) -> tuple[int, int] | None:
    values: dict[str, str] = {}
    required = {"start", "end", "contentlength"}
    for name, value in parse_qsl(urlparse(url).query, keep_blank_values=True):
        normalized = name.lower()
        if normalized not in required:
            continue
        if normalized in values:
            return None
        values[normalized] = value
    if values.keys() != required:
        return None
    try:
        start = int(values["start"])
        end = int(values["end"])
        content_length = int(values["contentlength"])
    except ValueError:
        return None
    if start < 0 or end <= start or content_length <= 0 or end - start != content_length:
        return None
    return start, end - 1


def _is_bbts_segment(url: str) -> bool:
    return _extension_from_segment(url) == "bbts"


def _role_from_hls_media(attrs: dict[str, str], media_type: str) -> str | None:
    text = " ".join(
        part
        for part in [
            attrs.get("NAME"),
            attrs.get("GROUP-ID"),
            attrs.get("CHARACTERISTICS"),
            attrs.get("FORCED"),
        ]
        if part
    ).lower()
    if attrs.get("FORCED", "").upper() == "YES" or "forced" in text:
        return "Forced"
    if media_type == "audio":
        if any(token in text for token in ("audio description", "audio-description", "descriptive", "describes-video", "_description", "-description")):
            return "Audio Description"
        return "Main"
    if media_type in {"subtitle", "subtitles", "text"}:
        characteristic = _subtitle_characteristic_label(attrs.get("CHARACTERISTICS"))
        return characteristic or "Subtitle"
    if media_type == "video":
        return "Main"
    return None


def _subtitle_characteristic_label(value: str | None) -> str | None:
    if not value:
        return None
    labels: list[str] = []
    for item in value.split(","):
        item = item.strip().lower()
        if not item:
            continue
        if item.startswith("public.accessibility."):
            item = item.removeprefix("public.accessibility.")
        elif item.startswith("public."):
            item = item.removeprefix("public.")
        labels.append(item)
    return "+".join(dict.fromkeys(labels)) or None


def _hls_media_bandwidth(attrs: dict[str, str], child_url: str, hint: dict[str, str] | None) -> int | None:
    explicit = _int_or_none(attrs.get("AVERAGE-BANDWIDTH")) or _int_or_none(attrs.get("BANDWIDTH"))
    if explicit:
        return explicit
    text = " ".join(part for part in [attrs.get("GROUP-ID"), attrs.get("NAME"), child_url] if part).lower()
    match = re.search(r"(?:^|[^a-z0-9])(\d{2,5})\s*(?:k|kbps)(?:[^a-z0-9]|$)", text)
    if not match:
        match = re.search(r"(?:^|[/_-])(\d{2,5})(?:[_-]complete|complete)(?:[^a-z0-9]|$)", text)
    if match:
        return int(match.group(1)) * 1000
    if hint:
        return _int_or_none(hint.get("bandwidth"))
    return None


def _hls_media_codec(
    attrs: dict[str, str],
    child_url: str,
    summary: MediaPlaylistSummary | None,
    hint: dict[str, str] | None,
    media_type: str | None = None,
) -> str | None:
    explicit = attrs.get("CODECS")
    if explicit:
        return explicit
    url_text = " ".join(part for part in [child_url, summary.extension if summary else None] if part).lower()
    text = " ".join(part for part in [attrs.get("GROUP-ID"), attrs.get("NAME"), child_url, summary.extension if summary else None] if part).lower()
    if media_type in {"subtitle", "subtitles", "text"}:
        return _hls_subtitle_codec(text)
    if _looks_like_dtsx_codec(url_text):
        return "dtsx"
    if re.search(r"(?:^|[^a-z0-9])(?:dtsh|dtsl)(?:[^a-z0-9]|$)", url_text):
        return "dtsh"
    if re.search(r"(?:^|[^a-z0-9])dtse(?:[^a-z0-9]|$)", url_text):
        return "dtse"
    if re.search(r"(?:^|[^a-z0-9])dtsc(?:[^a-z0-9]|$)", url_text):
        return "dtsc"
    hinted = (hint or {}).get("codecs")
    if hinted:
        return hinted
    if "ec-3" in text or "eac-3" in text or "eac3" in text or "ddplus" in text:
        return "ec-3"
    if "ac-3" in text or "ac3" in text:
        return "ac-3"
    if "he-aac" in text or "heaac" in text or "aach" in text:
        return "mp4a.40.5"
    if "aac" in text or "mp4a" in text or ".m4a" in text:
        return "mp4a.40.2"
    if "webvtt" in text or ".vtt" in text:
        return "wvtt"
    if "ttml" in text or "stpp" in text or ".ttml" in text or ".dfxp" in text:
        return "stpp"
    return None


def _hls_subtitle_codec(text: str) -> str | None:
    if "webvtt" in text or ".vtt" in text:
        return "wvtt"
    if "ttml" in text or "stpp" in text or ".ttml" in text or ".dfxp" in text:
        return "stpp"
    return None


def _looks_like_dtsx_codec(text: str) -> bool:
    return bool(re.search(r"(?:^|[^a-z0-9])(?:dtsx|dts-x|dts_x)(?:[^a-z0-9]|$)", text))


def _hls_audio_is_atmos(attrs: dict[str, str], child_url: str, hint: dict[str, str] | None) -> bool:
    text = " ".join(
        part
        for part in [
            attrs.get("GROUP-ID"),
            attrs.get("NAME"),
            attrs.get("CHARACTERISTICS"),
            attrs.get("CHANNELS"),
            attrs.get("CODECS"),
            (hint or {}).get("codecs"),
            child_url,
        ]
        if part
    ).lower()
    return "atmos" in text or "joc" in text


def _codec_token_for_media_type(codecs: str | None, media_type: str) -> str | None:
    if not codecs:
        return None
    audio_prefixes = ("mp4a", "aac", "ac-3", "ec-3", "dts", "opus", "flac")
    video_prefixes = ("avc1", "avc3", "dva1", "dvav", "hvc1", "hev1", "dvh1", "dvhe", "vp09", "vp9", "vp08", "vp8", "av01", "av1", "vvc1", "vvi1", "h266", "h.266", "vvc")
    prefixes = audio_prefixes if media_type == "audio" else video_prefixes
    for token in codecs.split(","):
        cleaned = token.strip()
        if cleaned.lower().startswith(prefixes):
            return cleaned
    return None


def _normalize_hls_channels(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().strip('"')
    if not cleaned:
        return None
    return cleaned.split("/", 1)[0]


def _normalize_encryption(method: str, attrs: dict[str, str] | None = None) -> str:
    method = method.upper()
    if method.replace("_", "-") == "SM4-CBC":
        key_format = ((attrs or {}).get("KEYFORMAT") or "").lower()
        if "3d5e6d35-9b9a-41e8-b843-dd3c6e72c42c" in key_format:
            return "CHINA-DRM-SM4-CBC"
        return "SM4-CBC"
    if method in {"SAMPLE-AES-CENC", "SAMPLE-AES-CTR"}:
        return "CENC"
    if method == "SAMPLE-AES":
        key_format = ((attrs or {}).get("KEYFORMAT") or "").lower()
        uri = ((attrs or {}).get("URI") or "").lower()
        if (
            "uuid" in key_format
            or "widevine" in key_format
            or "playready" in key_format
            or "pssh" in uri
            or uri.startswith("data:")
        ):
            return "CBCS"
        return "SAMPLE-AES"
    return method


def _parse_hls_range(raw_range: str, previous_end: int) -> tuple[tuple[int, int] | None, int]:
    try:
        if "@" in raw_range:
            length_raw, start_raw = raw_range.split("@", 1)
            start = int(start_raw)
        else:
            length_raw = raw_range
            start = previous_end + 1
        length = int(length_raw)
        end = start + length - 1
        return (start, end), end
    except ValueError:
        return None, previous_end


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except ValueError:
        return None


def _float_or_none(value: str | None) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except ValueError:
        return None
