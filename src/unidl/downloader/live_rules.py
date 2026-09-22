from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlparse, urlunparse

from .utils import join_uri, pretty_codec

_LIVE_DASH_ROOTS = {"dayone", "pdx-nitro"}
_IQIYI_AUDIO_GROUP = "iq-audio"


@dataclass(frozen=True, slots=True)
class AudioVividPolicyResult:
    path: Path
    codecs: tuple[str, ...] = ()
    decoded_path: Path | None = None
    replace_track: bool = False
    warning: str = ""




def apply_audio_vivid_policy(streams, enabled: bool) -> None:
    if not enabled:
        return
    for stream in streams or []:
        extra = getattr(stream, "extra", None)
        if not isinstance(extra, dict):
            extra = {}
            stream.extra = extra
        extra["audio_vivid_policy"] = True


def iqiyi_separate_audio_mux_media_type(stream, selected_streams) -> str | None:
    """Drop iQ's embedded AAC when an independent iQ audio track is selected.

    iQ's generated HLS master gives its replacement audio group the stable
    ``iq-audio`` id. Its video TS still physically contains the fallback AAC,
    so muxing the whole video input together with DD+ (or separate AAC) adds a
    duplicate audio stream and can invalidate per-track metadata. Keep the
    embedded AAC when video is selected alone, and filter only an explicitly
    paired iQ selection.
    """

    media_type = str(getattr(stream, "media_type", None) or "").strip().lower()
    if media_type not in {"video", "audio"} or not _is_iqiyi_audio_group_track(stream):
        return None
    has_separate_audio = any(
        str(getattr(candidate, "media_type", None) or "").strip().lower() == "audio"
        and _is_iqiyi_audio_group_track(candidate)
        for candidate in selected_streams or []
    )
    return media_type if has_separate_audio else None


def _is_iqiyi_audio_group_track(stream) -> bool:
    extra = getattr(stream, "extra", None)
    audio_id = extra.get("audio_id") if isinstance(extra, dict) else None
    groups = {
        str(getattr(stream, "group_id", None) or "").strip(),
        str(audio_id or "").strip(),
    }
    return _IQIYI_AUDIO_GROUP in groups


def postprocess_audio_vivid(
    path: str | Path,
    stream,
    *,
    decoder: str | Path | None = None,
    decoder_args: str | None = None,
) -> AudioVividPolicyResult:
    from .avs3 import (
        Avs3Error,
        decode_mp4_audio_vivid,
        decode_mpeg_ts_audio_vivid,
        inspect_mp4,
        inspect_mpeg_ts,
    )

    source = Path(path)
    if not _has_audio_vivid_policy(stream):
        return AudioVividPolicyResult(source)
    suffix = source.suffix.lower()
    try:
        if suffix in {".ts", ".m2ts", ".mts"}:
            inspection = inspect_mpeg_ts(source)
            codecs = tuple(dict.fromkeys(item.codec for item in inspection.streams))
            has_vivid = "audio-vivid" in codecs
            decode = decode_mpeg_ts_audio_vivid
        elif suffix in {".mp4", ".m4a", ".mov"}:
            inspection = inspect_mp4(source)
            codecs = tuple(dict.fromkeys(item.codec for item in inspection.audio_tracks))
            has_vivid = "audio-vivid" in codecs
            decode = decode_mp4_audio_vivid
        else:
            return AudioVividPolicyResult(source)
    except Avs3Error as exc:
        return AudioVividPolicyResult(source, warning=f"could not inspect audio codecs: {exc}")
    if not has_vivid:
        audio_codecs = [codec for codec in codecs if codec in {"aac", "e-ac-3", "ac-3"}]
        if getattr(stream, "media_type", None) == "audio" and audio_codecs:
            stream.codecs = audio_codecs[0]
        return AudioVividPolicyResult(source, codecs=codecs)

    extra = getattr(stream, "extra", None)
    if isinstance(extra, dict):
        extra["audio_vivid_detected"] = True
        # Keep the source(s) separate until decoding has succeeded. This also
        # prevents a missing optional decoder from handing av3a back to FFmpeg.
        extra["audio_vivid_preserve_source_container"] = True

    decoded = source.with_name(f"{source.stem}.audio-vivid.wav")
    try:
        decode(
            source,
            decoded,
            decoder=decoder,
            decoder_args=decoder_args,
        )
    except Avs3Error as exc:
        return AudioVividPolicyResult(
            source,
            codecs=codecs,
            warning=f"Audio Vivid was preserved but could not be decoded: {exc}",
        )
    replace = getattr(stream, "media_type", None) == "audio"
    if isinstance(extra, dict):
        # The downloader adds this WAV as a companion mux input for a muxed
        # video track, so the unknown av3a entry never reaches the muxer.
        extra["audio_vivid_preserve_source_container"] = False
        extra["audio_vivid_wav"] = str(decoded)
    return AudioVividPolicyResult(
        decoded if replace else source,
        codecs=codecs,
        decoded_path=decoded,
        replace_track=replace,
    )


def should_preserve_audio_vivid_source_container(streams) -> bool:
    return any(
        isinstance(getattr(stream, "extra", None), dict)
        and bool(stream.extra.get("audio_vivid_preserve_source_container"))
        for stream in streams or []
    )


def join_dash_base_uri(base_uri: str, reference: str) -> str:
    joined = join_uri(base_uri, reference)
    return _preserve_live_dash_prefix(base_uri, joined)


def should_skip_dash_period(period_id: str | None, period_base: str, is_live: bool) -> bool:
    return _is_dayone_ad_period(period_id, period_base, is_live)


def child_url_params(input_url: str) -> list[tuple[str, str]]:
    parsed = urlparse(input_url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    if not params:
        return []
    if _is_dazn_host(parsed.netloc):
        return [(key, value) for key, value in params if key.lower() not in {"start", "end"}]
    return params


def should_append_child_url_params(input_url: str) -> bool:
    parsed = urlparse(input_url)
    if not _is_dazn_host(parsed.netloc):
        return False
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return bool(params.get("dazn-token"))


def child_request_header_warnings(input_url: str, headers: dict[str, str] | None = None) -> list[str]:
    required = child_url_required_headers(input_url)
    missing = [name for name in required if not _has_header(headers, name)]
    parsed = urlparse(input_url)
    if _is_dazn_host(parsed.netloc) and "user-agent" in {name.lower() for name in missing}:
        return [
            "DAZN media token is bound to User-Agent; pass the exact User-Agent used to create the token with -H \"User-Agent: ...\"."
        ]
    mismatch = _dazn_user_agent_mismatch(input_url, headers)
    if mismatch:
        return [
            "DAZN media token does not match the supplied User-Agent; pass the exact User-Agent used to create the token."
        ]
    if not missing:
        return []
    return [f"media token requires header(s): {', '.join(missing)}"]


def child_url_error_tip(url: str, error_text: str) -> str | None:
    parsed = urlparse(url)
    if _is_hulu_japan_live_media_host(parsed.netloc) and "403" in error_text:
        return (
            "Hulu Japan live CDN rejected the init/media authorization. Regenerate the live playback source and "
            "verify that the current Japanese network exit is accepted by the live CDN; lowering workers or "
            "increasing retries will not fix a persistent 403."
        )
    if not _is_dazn_host(parsed.netloc):
        return None
    normalized_error = error_text.lower().replace("-", " ")
    if "unauthorized 667" in normalized_error:
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if not params.get("dazn-token"):
            return "DAZN init/media request is missing its authentication query; retry with --append-url-params or update to a build that appends DAZN parameters automatically."
        return "DAZN rejected the init/media authorization; verify that the token is current and authorizes this media path."
    if "forbidden 688" not in normalized_error:
        return None
    required = {name.lower() for name in child_url_required_headers(url)}
    if "user-agent" in required:
        return "DAZN rejected the media token signature; pass the exact User-Agent used to create the token with -H \"User-Agent: ...\"."
    return None


def child_url_required_headers(input_url: str) -> list[str]:
    parsed = urlparse(input_url)
    if not _is_dazn_host(parsed.netloc):
        return []
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    token = params.get("dazn-token")
    if not token:
        return []
    payload = _jwt_payload(token)
    headers = payload.get("headers") if isinstance(payload, dict) else None
    required = [str(header).strip() for header in headers if str(header).strip()] if isinstance(headers, list) else []
    if isinstance(payload, dict) and payload.get("ua") and "user-agent" not in {name.lower() for name in required}:
        required.append("user-agent")
    return required


def live_pipe_input_offsets_seconds(streams, args) -> dict[int, float]:
    if not _is_proxad_oqee_live(streams):
        return {}
    starts = {
        id(stream): start
        for stream in streams
        if getattr(stream, "media_type", None) in {"video", "audio"}
        for start in [_dash_initial_presentation_start(stream, args)]
        if start is not None
    }
    if len(starts) < 2:
        return {}
    base_start = min(starts.values())
    return {
        stream_id: offset
        for stream_id, start in starts.items()
        for offset in [start - base_start]
        if offset > 0.25
    }


def should_treat_live_stream_as_fragmented_mp4(stream) -> bool:
    if _is_sabr_ump_live_stream(stream):
        return not _stream_uses_webm_container(stream)
    return _is_youtube_json_direct_live_stream(stream) and not _stream_uses_webm_container(stream)












def live_pipe_media_part_contains_init(stream, segment=None) -> bool:
    if _is_sabr_ump_live_stream(stream):
        if _stream_uses_webm_container(stream):
            return False
        return segment is not None and getattr(segment, "index", None) != -1
    if not _is_youtube_json_direct_live_stream(stream):
        return False
    if segment is not None and getattr(segment, "index", None) == -1:
        return False
    return not getattr(segment, "byte_range", None)


def live_pipe_mux_disabled_reason(stream) -> str | None:
    schemes = [getattr(stream, "encryption_scheme", None)]
    schemes.extend(
        getattr(segment, "encryption_scheme", None)
        for segment in getattr(stream, "segments", []) or []
    )
    if _is_audio_vivid_policy_muxed_hls(stream):
        return (
            "Audio Vivid-enabled muxed live TS is recorded before muxing so its PMT can distinguish "
            "AAC, E-AC-3 and Audio Vivid without sending av3a to FFmpeg."
        )
    if _is_youtube_json_direct_live_stream(stream) and _stream_uses_webm_container(stream):
        return (
            "YouTube JSON VP9/WebM live pipe mux is disabled for now; recording the tracks separately "
            "avoids short video output while the continuous WebM live muxer is hardened."
        )
    return None


def live_pipe_matroska_options(streams, output_container: str | None) -> list[str]:
    """Return Matroska live-pipe options for continuous recording."""
    if output_container != "matroska":
        return []
    options: list[str] = []
    options.extend(["-live", "1"])
    options.extend([
        "-cluster_time_limit",
        "1000",
        "-cluster_size_limit",
        str(64 * 1024),
    ])
    return options




def should_finalize_live_pipe_matroska_output(streams, output_container: str | None) -> bool:
    if output_container != "matroska":
        return False
    return any(_is_kan_medone_dash_hevc_live_stream(stream) for stream in streams or [])


def should_finalize_live_pipe_matroska_output_to_mp4(streams, output_container: str | None) -> bool:
    if output_container != "matroska":
        return False
    return any(_is_kan_medone_dash_hevc_live_stream(stream) for stream in streams or [])


def should_repeat_live_media_request(stream) -> bool:
    if not _is_youtube_json_direct_live_stream(stream):
        return False
    media_segments = [
        segment
        for segment in getattr(stream, "segments", []) or []
        if getattr(segment, "index", None) != -1
    ]
    return len(media_segments) == 1 and bool(getattr(media_segments[0], "url", None))


def live_media_retry_attempts(stream, default_retries: int) -> int:
    retries = max(1, int(default_retries or 1))
    if _is_pluto_takedown_slate_hls_stream(stream) or _is_hulu_japan_live_dash_stream(stream):
        return 1
    return retries


def live_media_retry_grace_enabled(stream) -> bool:
    if _is_vgc_stream(stream):
        return False
    return not (_is_pluto_takedown_slate_hls_stream(stream) or _is_hulu_japan_live_dash_stream(stream))


def repeated_live_media_duration_seconds(stream, fallback_seconds: float | int | None = None) -> float:
    for segment in getattr(stream, "segments", []) or []:
        duration = getattr(segment, "duration", None)
        if duration and duration > 0:
            return float(duration)
    if fallback_seconds:
        try:
            parsed = float(fallback_seconds)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass
    return 3.0


def _is_youtube_json_direct_live_stream(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "json":
        return False
    if not getattr(stream, "is_live", False):
        return False
    if not _json_stream_has_init_range(stream):
        return False
    return any(_is_youtube_live_media_url(url) for url in _stream_urls(stream))


def _is_sabr_ump_live_stream(stream) -> bool:
    if not getattr(stream, "is_live", False):
        return False
    extra = getattr(stream, "extra", {}) if isinstance(getattr(stream, "extra", None), dict) else {}
    return getattr(stream, "manifest_type", None) == "sabr_ump" or bool(extra.get("sabr_ump"))


def _is_vgc_stream(stream) -> bool:
    extra = getattr(stream, "extra", None)
    return isinstance(extra, dict) and bool(extra.get("vgc"))


















def _is_kan_medone_dash_hevc_live_stream(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "dash" or not getattr(stream, "is_live", False):
        return False
    if getattr(stream, "media_type", None) != "video":
        return False
    codec = pretty_codec(getattr(stream, "codecs", None), "video")
    raw_codec = (getattr(stream, "codecs", None) or "").lower()
    if codec != "H.265" and not raw_codec.startswith(("hvc1", "hev1", "hevc", "h265")):
        return False
    return any(_is_kan_medone_live_url(url) for url in _stream_urls(stream))


def _is_pluto_takedown_slate_hls_stream(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "hls" or not getattr(stream, "is_live", False):
        return False
    if getattr(stream, "media_type", None) != "video":
        return False
    return any(_is_pluto_takedown_slate_url(url) for url in _stream_urls(stream))


def _is_hulu_japan_live_dash_stream(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "dash" or not getattr(stream, "is_live", False):
        return False
    return any(_is_hulu_japan_live_media_host(urlparse(url).netloc) for url in _stream_urls(stream))


def _is_hulu_japan_live_media_host(host: str) -> bool:
    normalized = (host or "").split(":", 1)[0].lower()
    return normalized.startswith("live") and normalized.endswith(".happyon-cdn.jp")


def _has_audio_vivid_policy(stream) -> bool:
    extra = getattr(stream, "extra", None)
    return isinstance(extra, dict) and bool(extra.get("audio_vivid_policy"))


def _is_audio_vivid_policy_muxed_hls(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "hls" or not getattr(stream, "is_live", False):
        return False
    if getattr(stream, "media_type", None) != "video":
        return False
    extra = getattr(stream, "extra", None)
    if (
        not isinstance(extra, dict)
        or not extra.get("muxed_audio")
        or not extra.get("audio_vivid_policy")
    ):
        return False
    if (getattr(stream, "extension", None) or "").lower().lstrip(".") != "ts":
        return False
    return True








def _json_stream_has_init_range(stream) -> bool:
    raw = getattr(stream, "extra", {}).get("raw") if isinstance(getattr(stream, "extra", None), dict) else None
    if isinstance(raw, dict):
        if raw.get("init_range") or raw.get("initRange") or raw.get("initialization_range") or raw.get("initializationRange"):
            return True
    return any(getattr(segment, "index", None) == -1 for segment in getattr(stream, "segments", []) or [])


def _stream_urls(stream) -> list[str]:
    urls: list[str] = []

    def add(value) -> None:
        if isinstance(value, str) and value and value not in urls:
            urls.append(value)

    add(getattr(stream, "url", None))
    add(getattr(stream, "original_url", None))
    extra = getattr(stream, "extra", {}) if isinstance(getattr(stream, "extra", None), dict) else {}
    all_urls = extra.get("all_urls")
    if isinstance(all_urls, list):
        for value in all_urls:
            add(value)
    elif isinstance(all_urls, dict):
        for value in all_urls.values():
            add(value)
    for segment in (getattr(stream, "segments", []) or [])[:3]:
        add(getattr(segment, "url", None))
    return urls


def _is_youtube_live_media_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "googlevideo.com" not in host:
        return False
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    source = params.get("source", "").lower()
    client = params.get("c", "").lower()
    return source in {"yt_tv_broadcast", "yt_live_broadcast"} or "tvhtml5" in client


def _is_kan_medone_live_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return (
        host == "kancdn.medonecdn.net"
        or host == "cdn.avcom-bezeq.tv"
        or path.endswith("/kancdn-live/live/kan_4k/live_drm.livx")
        or "/kancdn-live/live/kan_4k/live_drm.livx" in path
    )


def _is_pluto_takedown_slate_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host != "plutotv.net" and not host.endswith(".plutotv.net"):
        return False
    return "takedownslates" in path and "/hls/" in path


def _stream_uses_webm_container(stream) -> bool:
    extension = (getattr(stream, "extension", None) or "").lower()
    if extension == "webm":
        return True
    raw = getattr(stream, "extra", {}).get("raw") if isinstance(getattr(stream, "extra", None), dict) else None
    mime_type = ""
    if isinstance(raw, dict):
        mime_type = str(raw.get("mime_type") or raw.get("mimeType") or "").lower()
    if "webm" in mime_type:
        return True
    codec = (getattr(stream, "codecs", None) or "").lower()
    return "vp9" in codec or "vp8" in codec or "opus" in codec


def _preserve_live_dash_prefix(base_uri: str, joined_uri: str) -> str:
    base = urlparse(base_uri)
    joined = urlparse(joined_uri)
    if base.scheme not in {"http", "https"} or joined.scheme not in {"http", "https"}:
        return joined_uri
    if base.netloc != joined.netloc:
        return joined_uri

    base_parts = [part for part in base.path.split("/") if part]
    joined_parts = [part for part in joined.path.split("/") if part]
    protected_prefix = _live_dash_protected_prefix(base_parts)
    if not protected_prefix or not joined_parts:
        return joined_uri
    if joined_parts[: len(protected_prefix)] == protected_prefix:
        return joined_uri
    common_len = _common_prefix_length(protected_prefix, joined_parts)
    if common_len < len(protected_prefix) and common_len < len(joined_parts) and joined_parts[common_len] in _LIVE_DASH_ROOTS:
        path = "/" + "/".join([*protected_prefix, *joined_parts[common_len:]])
        return urlunparse(joined._replace(path=_preserve_trailing_slash(path, joined.path)))
    if joined_parts[0] in _LIVE_DASH_ROOTS:
        path = "/" + "/".join([*protected_prefix, *joined_parts])
        return urlunparse(joined._replace(path=_preserve_trailing_slash(path, joined.path)))
    if len(joined_parts) > 1 and joined_parts[0] == protected_prefix[0] and joined_parts[1] in _LIVE_DASH_ROOTS:
        path = "/" + "/".join([*protected_prefix, *joined_parts[1:]])
        return urlunparse(joined._replace(path=_preserve_trailing_slash(path, joined.path)))
    return joined_uri


def _live_dash_protected_prefix(parts: list[str]) -> list[str]:
    for index, part in enumerate(parts):
        if part in _LIVE_DASH_ROOTS:
            return parts[:index]
    for marker in (("v1", "dash"), ("live", "clients", "dash")):
        marker_len = len(marker)
        for index in range(0, len(parts) - marker_len + 1):
            if tuple(parts[index : index + marker_len]) == marker:
                return parts[:index]
    return []


def _common_prefix_length(left: list[str], right: list[str]) -> int:
    count = 0
    for left_part, right_part in zip(left, right, strict=False):
        if left_part != right_part:
            break
        count += 1
    return count


def _preserve_trailing_slash(path: str, source_path: str) -> str:
    if source_path.endswith("/") and not path.endswith("/"):
        return path + "/"
    return path


def _is_dayone_ad_period(period_id: str | None, period_base: str, is_live: bool) -> bool:
    if not is_live:
        return False
    parsed = urlparse(period_base)
    path_parts = [part.lower() for part in parsed.path.split("/") if part]
    return "dayone" in path_parts and "_" in (period_id or "")


def _is_dazn_host(host: str | None) -> bool:
    normalized = (host or "").lower()
    return normalized == "indazn.com" or normalized.endswith(".indazn.com") or normalized == "dazn.com" or normalized.endswith(".dazn.com")


def _has_header(headers: dict[str, str] | None, name: str) -> bool:
    normalized = name.lower()
    return any(str(key).lower() == normalized for key in (headers or {}))


def _header_value(headers: dict[str, str] | None, name: str) -> str | None:
    normalized = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == normalized:
            return str(value)
    return None


def _dazn_user_agent_mismatch(input_url: str, headers: dict[str, str] | None) -> bool:
    payload = _dazn_token_payload(input_url)
    expected = payload.get("ua") if isinstance(payload, dict) else None
    actual = _header_value(headers, "user-agent")
    if not isinstance(expected, str) or not expected or not actual:
        return False
    return hashlib.sha1(actual.encode("utf-8")).hexdigest().lower() != expected.lower()


def _dazn_token_payload(input_url: str) -> dict:
    parsed = urlparse(input_url)
    if not _is_dazn_host(parsed.netloc):
        return {}
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    token = params.get("dazn-token")
    return _jwt_payload(token) if token else {}


def _jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode((payload + padding).encode("ascii"))
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_proxad_oqee_live(streams) -> bool:
    return any(_is_proxad_oqee_stream(stream) for stream in streams)


def _is_proxad_oqee_stream(stream) -> bool:
    urls = [
        getattr(stream, "url", None),
        getattr(stream, "original_url", None),
        (getattr(stream, "extra", {}) or {}).get("dash_template_base_uri"),
    ]
    urls.extend(segment.url for segment in (getattr(stream, "segments", []) or [])[:3])
    for value in urls:
        if not isinstance(value, str) or not value:
            continue
        parsed = urlparse(value)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if host == "api-proxad.oqee.net" and "/playlist/v1/live/" in path:
            return True
        if host.endswith(".stream.proxad.net") or host == "media4.stream.proxad.net":
            return True
    return False


def _dash_initial_presentation_start(stream, args) -> float | None:
    extra = getattr(stream, "extra", {}) or {}
    if not extra.get("dash_index_is_timeline"):
        return None
    timescale = _positive_int(extra.get("dash_timescale"))
    if not timescale:
        return None
    media_segments = [
        segment
        for segment in getattr(stream, "segments", []) or []
        if getattr(segment, "index", None) is not None and segment.index >= 0
    ]
    media_segments = _initial_media_segments(media_segments, args)
    if not media_segments:
        return None
    first_presentation_time = getattr(media_segments[0], "timeline_presentation_time", None)
    if first_presentation_time is not None:
        try:
            return float(first_presentation_time)
        except (TypeError, ValueError):
            pass
    first_time = getattr(media_segments[0], "timeline_time", None)
    if first_time is None:
        first_time = getattr(media_segments[0], "index", None)
    if first_time is None or first_time < 0:
        return None
    try:
        period_start = float(extra.get("period_start") or 0.0)
        presentation_offset = float(extra.get("dash_presentation_time_offset") or 0.0)
    except (TypeError, ValueError):
        period_start = 0.0
        presentation_offset = 0.0
    return period_start + float(first_time) / timescale - presentation_offset


def _initial_media_segments(segments: list, args) -> list:
    if not segments:
        return []
    dvr_start = _parse_live_offset(getattr(args, "live_dvr_start_at", None))
    if getattr(args, "live_dvr_from_start", False) or dvr_start is not None:
        return _segments_from_offset(segments, dvr_start)
    take_count = int(getattr(args, "live_take_count", 0) or 0)
    if take_count > 0 and len(segments) > take_count:
        return segments[-take_count:]
    return segments


def _segments_from_offset(segments: list, offset: float | None) -> list:
    if not offset or offset <= 0:
        return segments
    elapsed = 0.0
    for index, segment in enumerate(segments):
        duration = float(getattr(segment, "duration", None) or 0.0)
        if elapsed + duration > offset:
            return segments[index:]
        elapsed += duration
    return []


def _parse_live_offset(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
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
    return None


def _positive_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
