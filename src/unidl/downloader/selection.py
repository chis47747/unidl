from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .models import StreamInfo
from .utils import pretty_codec


@dataclass(slots=True)
class SelectionOptions:
    video: str | None = None
    audio: str | None = None
    video_lang: str | None = None
    audio_lang: str | None = None
    subtitle_lang: str | None = None
    video_range: str | None = None
    audio_type: str | None = None
    select_video: str | None = None
    select_audio: str | None = None
    select_subtitle: str | None = None

    @property
    def has_filters(self) -> bool:
        return any(
            [
                self.video,
                self.audio,
                self.video_lang,
                self.audio_lang,
                self.subtitle_lang,
                self.video_range,
                self.audio_type,
                self.select_video,
                self.select_audio,
                self.select_subtitle,
            ]
        )


def select_streams(streams: list[StreamInfo], options: SelectionOptions) -> list[StreamInfo]:
    selected: list[StreamInfo] = []

    video_candidates = [stream for stream in streams if stream.media_type == "video"]
    audio_candidates = [stream for stream in streams if stream.media_type == "audio"]
    subtitle_candidates = [stream for stream in streams if stream.media_type in {"subtitle", "subtitles", "text"}]

    if options.video or options.video_lang or options.video_range or options.select_video:
        selected.extend(_select_video(video_candidates, options))
    if options.audio or options.audio_lang or options.audio_type or options.select_audio:
        selected.extend(_select_audio(audio_candidates, options))
    if options.subtitle_lang or options.select_subtitle:
        selected.extend(_select_subtitle(subtitle_candidates, options))

    return _dedupe(selected)


def _select_video(candidates: list[StreamInfo], options: SelectionOptions) -> list[StreamInfo]:
    expr = _parse_filter_expr(options.select_video)
    candidates = _apply_common_expr(candidates, expr)
    candidates = _filter_language(candidates, options.video_lang or expr.get("lang"))
    candidates = _filter_codecs(candidates, expr.get("codecs") or expr.get("codec"))
    candidates = _filter_bandwidth(candidates, expr)
    range_value = options.video_range or expr.get("range")
    range_tokens = _tokens(range_value)
    buckets = [_filter_video_range(candidates, item) for item in range_tokens] if range_tokens else [candidates]

    heights = _numbers(options.video or expr.get("res") or expr.get("height"))
    mode = _mode(options.video, expr, default="best")
    if mode == "all":
        return _dedupe(stream for bucket in buckets for stream in bucket)
    if mode.startswith("best") and not heights:
        return _dedupe(stream for bucket in buckets for stream in _top_n(bucket, _best_count(mode), key=_video_sort_key))
    if heights:
        selected: list[StreamInfo] = []
        for bucket in buckets:
            for height in heights:
                matches = [stream for stream in bucket if _height(stream) == height]
                if matches:
                    selected.extend(_top_n(matches, _best_count(mode), key=_video_sort_key))
        return selected
    return _dedupe(stream for bucket in buckets for stream in _top_n(bucket, 1, key=_video_sort_key))


def _select_audio(candidates: list[StreamInfo], options: SelectionOptions) -> list[StreamInfo]:
    expr = _parse_filter_expr(options.select_audio)
    candidates = _apply_common_expr(candidates, expr)
    candidates = _filter_language(candidates, options.audio_lang or expr.get("lang"))
    candidates = _filter_audio_type(candidates, options.audio_type or expr.get("type") or expr.get("codec") or expr.get("codecs"))
    candidates = _filter_bandwidth(candidates, expr)

    targets = _numbers(options.audio or expr.get("bw") or expr.get("bandwidth") or expr.get("bitrate"))
    mode = _mode(options.audio, expr, default="best")
    if mode == "all":
        return candidates
    if targets:
        selected: list[StreamInfo] = []
        language_buckets = _language_buckets(candidates, options.audio_lang or expr.get("lang"))
        type_buckets = _audio_type_buckets(candidates, options.audio_type or expr.get("type"))
        buckets = _combine_buckets(candidates, language_buckets, type_buckets)
        for bucket in buckets:
            for target in targets:
                closest = min(bucket, key=lambda stream: abs((_audio_kbps(stream) or 0) - target), default=None)
                if closest is not None:
                    selected.append(closest)
        return selected
    if mode.startswith("best"):
        buckets = _language_buckets(candidates, options.audio_lang or expr.get("lang"))
        if not buckets:
            buckets = [candidates]
        selected: list[StreamInfo] = []
        for bucket in buckets:
            selected.extend(_top_n(bucket, _best_count(mode), key=_audio_sort_key))
        return selected
    return _top_n(candidates, 1, key=_audio_sort_key)


def _select_subtitle(candidates: list[StreamInfo], options: SelectionOptions) -> list[StreamInfo]:
    expr = _parse_filter_expr(options.select_subtitle)
    candidates = _apply_common_expr(candidates, expr)
    candidates = _filter_language(candidates, options.subtitle_lang or expr.get("lang"))
    mode = _mode(None, expr, default="all")
    if mode.startswith("best"):
        return _top_n(candidates, _best_count(mode), key=lambda stream: stream.language or "")
    return candidates


def _parse_filter_expr(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    value = value.strip()
    if value.lower() in {"best", "all"} or re.fullmatch(r"best\d+", value.lower()):
        return {"for": value.lower()}
    result: dict[str, str] = {}
    parts = re.split(r":(?=[A-Za-z_][A-Za-z0-9_-]*=)", value)
    for part in parts:
        if "=" not in part:
            result.setdefault("for", part.strip().lower())
            continue
        key, item_value = part.split("=", 1)
        result[key.strip().lower()] = item_value.strip().strip('"').strip("'")
    return result


def _apply_common_expr(candidates: list[StreamInfo], expr: dict[str, str]) -> list[StreamInfo]:
    role = expr.get("role")
    name = expr.get("name")
    group = expr.get("group") or expr.get("groupid")
    if role:
        candidates = [stream for stream in candidates if _regex_match(role, stream.role)]
    if name:
        candidates = [stream for stream in candidates if _regex_match(name, stream.name)]
    if group:
        candidates = [stream for stream in candidates if _regex_match(group, stream.group_id)]
    return candidates


def _filter_language(candidates: list[StreamInfo], value: str | None) -> list[StreamInfo]:
    values = _tokens(value)
    if not values:
        return candidates
    return [stream for stream in candidates if _language_matches(stream.language, values)]


def _filter_video_range(candidates: list[StreamInfo], value: str | None) -> list[StreamInfo]:
    values = _tokens(value)
    if not values:
        return candidates
    return [stream for stream in candidates if any(_range_matches(stream, item) for item in values)]


def _filter_audio_type(candidates: list[StreamInfo], value: str | None) -> list[StreamInfo]:
    values = _tokens(value)
    if not values:
        return candidates
    return [stream for stream in candidates if any(_audio_type_matches(stream, item) for item in values)]


def _filter_codecs(candidates: list[StreamInfo], value: str | None) -> list[StreamInfo]:
    values = _tokens(value)
    if not values:
        return candidates
    return [stream for stream in candidates if any(_codec_matches(stream, item) for item in values)]


def _filter_bandwidth(candidates: list[StreamInfo], expr: dict[str, str]) -> list[StreamInfo]:
    minimum = _first_number(expr.get("bwmin") or expr.get("bandwidthmin"))
    maximum = _first_number(expr.get("bwmax") or expr.get("bandwidthmax"))
    if minimum is None and maximum is None:
        return candidates
    result = []
    for stream in candidates:
        kbps = _stream_kbps(stream)
        if kbps is None:
            continue
        if minimum is not None and kbps < minimum:
            continue
        if maximum is not None and kbps > maximum:
            continue
        result.append(stream)
    return result


def _tokens(value: str | None) -> list[str]:
    if not value:
        return []
    return [token.strip().lower() for token in re.split(r"[,|]", value) if token.strip()]


def _numbers(value: str | None) -> list[int]:
    if not value:
        return []
    lowered = value.lower()
    if lowered in {"best", "all"} or re.fullmatch(r"best\d+", lowered):
        return []
    return [int(match.group(0)) for match in re.finditer(r"\d+", value)]


def _first_number(value: str | None) -> int | None:
    numbers = _numbers(value)
    return numbers[0] if numbers else None


def _mode(raw_value: str | None, expr: dict[str, str], default: str) -> str:
    explicit = (expr.get("for") or "").lower()
    if explicit:
        return explicit
    if raw_value and raw_value.lower() in {"best", "all"}:
        return raw_value.lower()
    if raw_value and re.fullmatch(r"best\d+", raw_value.lower()):
        return raw_value.lower()
    return default


def _best_count(mode: str) -> int:
    match = re.fullmatch(r"best(\d+)", mode)
    return int(match.group(1)) if match else 1


def _top_n(candidates: list[StreamInfo], count: int, key) -> list[StreamInfo]:
    return sorted(candidates, key=key, reverse=True)[:count]


def _height(stream: StreamInfo) -> int | None:
    if not stream.resolution or "x" not in stream.resolution:
        return None
    try:
        return int(stream.resolution.lower().split("x", 1)[1])
    except ValueError:
        return None


def _stream_kbps(stream: StreamInfo) -> int | None:
    if stream.bandwidth:
        return round(stream.bandwidth / 1000)
    return _audio_kbps(stream)


def _audio_kbps(stream: StreamInfo) -> int | None:
    if stream.bandwidth:
        return round(stream.bandwidth / 1000)
    blob = _stream_blob(stream)
    matches = [int(match.group(1)) for match in re.finditer(r"(?:audio|stereo|mono|aac|ac3|ec3|eac3|ddp?)[-_]?(\d{2,4})", blob)]
    if matches:
        return max(matches)
    return None


def _video_sort_key(stream: StreamInfo):
    return (_height(stream) or 0, stream.bandwidth or 0, stream.frame_rate or 0)


def _audio_sort_key(stream: StreamInfo):
    channels = _channels(stream)
    return (_audio_primary_score(stream), channels, _audio_kbps(stream) or 0, stream.bandwidth or 0)


def _audio_primary_score(stream: StreamInfo) -> int:
    text = _stream_blob(stream).replace("_", " ").replace("-", " ")
    if re.search(r"\bsecondary\b", text) or "acont=secondary" in text:
        return 0
    if (
        re.search(r"\bprimary\b", text)
        or re.search(r"\bdefault\b", text)
        or "acont=primary" in text
        or _extra_bool(stream, "is_default")
        or _extra_bool(stream, "isDefault")
        or _extra_bool(stream, "default_track")
    ):
        return 2
    return 1


def _channels(stream: StreamInfo) -> float:
    if not stream.channels:
        return 0
    match = re.search(r"\d+(?:\.\d+)?", stream.channels)
    return float(match.group(0)) if match else 0


def _extra_bool(stream: StreamInfo, key: str) -> bool:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    value = extra.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _language_matches(language: str | None, values: list[str]) -> bool:
    if not language:
        return "und" in values or "*" in values
    language = language.lower()
    primary = language.split("-", 1)[0]
    return any(value in {"*", language, primary} for value in values)


def _range_matches(stream: StreamInfo, value: str) -> bool:
    value = value.lower().replace("_", "").replace("-", "")
    text = " ".join(filter(None, [stream.video_range, stream.codecs, stream.name, stream.group_id])).lower()
    normalized_text = text.replace("hdr10+", "hdr10plus").replace("_", "").replace("-", "")
    if value == "hdr":
        return any(item in normalized_text for item in ["hdr", "hdr10", "hdr10plus", "pq", "hlg", "dv", "dovi", "dolbyvision", "dvhe", "dvh1"])
    if value == "hdr10":
        return "hdr10" in normalized_text and "hdr10plus" not in normalized_text
    if value in {"hdr10plus", "hdr10p"}:
        return "hdr10plus" in normalized_text or "hdr10p" in normalized_text
    if value == "hlg":
        return "hlg" in normalized_text
    if value in {"dv", "dovi", "dolbyvision"}:
        return any(item in normalized_text for item in ["dv", "dovi", "dolbyvision", "dvhe", "dvh1"])
    if value == "sdr":
        return "sdr" in normalized_text or not any(item in normalized_text for item in ["hdr", "pq", "hlg", "dv", "dovi", "dolbyvision", "dvhe", "dvh1"])
    return value in normalized_text


def _audio_type_matches(stream: StreamInfo, value: str) -> bool:
    value = _normalize_audio_type(value)
    text = _stream_blob(stream)
    codec = (pretty_codec(stream.codecs, "audio") or stream.codecs or "").lower()
    if value == "atmos":
        return any(item in text for item in ["atmos", "joc"])
    if value in {"ddplus", "eac3"}:
        return any(item in text for item in ["ec-3", "eac3", "e-ac-3", "ddplus", "ddp"]) or codec == "e-ac-3"
    if value in {"dd", "ac3"}:
        return any(item in text for item in ["ac-3", "ac3", "dolby"]) or codec == "ac-3"
    if value == "aac":
        return "aac" in codec or "mp4a" in text or "aac" in text
    if value == "opus":
        return "opus" in text or "opus" in codec
    return value in text or value in codec


def _codec_matches(stream: StreamInfo, value: str) -> bool:
    value = value.lower()
    text = _stream_blob(stream)
    pretty = (pretty_codec(stream.codecs, stream.media_type) or "").lower()
    aliases = {
        "h264": ["h.264", "avc", "avc1", "avc3", "dva1", "dvav"],
        "avc": ["h.264", "avc", "avc1", "avc3", "dva1", "dvav"],
        "h265": ["h.265", "hevc", "hvc1", "hev1", "dvh1", "dvhe"],
        "hevc": ["h.265", "hevc", "hvc1", "hev1", "dvh1", "dvhe"],
        "av1": ["av1", "av01"],
        "vp9": ["vp9", "vp09"],
        "vp8": ["vp8", "vp08"],
        "h266": ["h.266", "h266", "vvc", "vvc1", "vvi1"],
        "vvc": ["h.266", "h266", "vvc", "vvc1", "vvi1"],
        "dv": ["dolby vision", "dvh1", "dvhe"],
    }
    return value in text or value in pretty or any(alias in text or alias in pretty for alias in aliases.get(value, []))


def _normalize_audio_type(value: str) -> str:
    value = value.lower().replace("-", "").replace("_", "")
    aliases = {
        "atoms": "atmos",
        "atmos": "atmos",
        "dd+": "ddplus",
        "ddp": "ddplus",
        "eac3": "eac3",
        "ec3": "eac3",
        "ddplus": "ddplus",
        "ac3": "ac3",
        "dd": "dd",
    }
    return aliases.get(value, value)


def _regex_match(pattern: str, value: str | None) -> bool:
    if value is None:
        return False
    try:
        return re.search(pattern, value, flags=re.IGNORECASE) is not None
    except re.error:
        return pattern.lower() in value.lower()


def _stream_blob(stream: StreamInfo) -> str:
    return " ".join(
        str(item)
        for item in [
            stream.id,
            stream.group_id,
            stream.name,
            stream.language,
            stream.role,
            stream.codecs,
            stream.channels,
            stream.extension,
            stream.video_range,
            stream.url,
        ]
        if item
    ).lower()


def _language_buckets(candidates: list[StreamInfo], value: str | None) -> list[list[StreamInfo]]:
    values = _tokens(value)
    if not values:
        return [candidates] if candidates else []
    buckets: list[list[StreamInfo]] = []
    for language in values:
        bucket = [stream for stream in candidates if _language_matches(stream.language, [language])]
        if bucket:
            buckets.append(bucket)
    return buckets


def _audio_type_buckets(candidates: list[StreamInfo], value: str | None) -> list[list[StreamInfo]]:
    values = _tokens(value)
    if not values:
        return [candidates] if candidates else []
    buckets: list[list[StreamInfo]] = []
    for audio_type in values:
        bucket = [stream for stream in candidates if _audio_type_matches(stream, audio_type)]
        if bucket:
            buckets.append(bucket)
    return buckets


def _combine_buckets(candidates: list[StreamInfo], *bucket_groups: list[list[StreamInfo]]) -> list[list[StreamInfo]]:
    active = [group for group in bucket_groups if group]
    if not active:
        return [candidates] if candidates else []
    buckets = active[0]
    for group in active[1:]:
        next_buckets: list[list[StreamInfo]] = []
        for left in buckets:
            left_ids = {id(stream) for stream in left}
            for right in group:
                intersection = [stream for stream in right if id(stream) in left_ids]
                if intersection:
                    next_buckets.append(intersection)
        buckets = next_buckets
    return buckets


def _dedupe(streams: Iterable[StreamInfo]) -> list[StreamInfo]:
    result: list[StreamInfo] = []
    seen: set[str] = set()
    for stream in streams:
        key = stream.url or f"{stream.media_type}:{stream.id}:{stream.group_id}:{stream.language}:{stream.bandwidth}"
        if key in seen:
            continue
        seen.add(key)
        result.append(stream)
    return result
