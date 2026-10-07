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
    audio_codec: str | None = None
    audio_profile: str | None = None
    audio_quality: str | None = None
    audio_selection: str | None = None
    video_selection: str | None = None
    audio_channels: str | None = None
    subtitle_kind: str | None = None
    subtitle_selection: str | None = None
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
                self.audio_codec,
                self.audio_profile,
                self.audio_quality,
                self.audio_selection,
                self.video_selection,
                self.audio_channels,
                self.subtitle_kind,
                self.subtitle_selection,
                self.select_video,
                self.select_audio,
                self.select_subtitle,
            ]
        )


@dataclass(slots=True)
class SelectionResult:
    """Selected streams plus any hard constraints that matched nothing."""

    selected: list[StreamInfo]
    unmatched: tuple[str, ...] = ()


def select_streams(streams: list[StreamInfo], options: SelectionOptions) -> list[StreamInfo]:
    result = select_streams_detailed(streams, options)
    return [] if result.unmatched else result.selected


def select_streams_detailed(streams: list[StreamInfo], options: SelectionOptions) -> SelectionResult:
    selected: list[StreamInfo] = []
    unmatched: list[str] = []

    video_candidates = [stream for stream in streams if stream.media_type == "video"]
    audio_candidates = [stream for stream in streams if stream.media_type == "audio"]
    subtitle_candidates = [stream for stream in streams if stream.media_type in {"subtitle", "subtitles", "text"}]

    if options.video or options.video_lang or options.video_range or options.select_video:
        video_selected = _select_video(video_candidates, options)
        selected.extend(video_selected)
        if video_candidates and not video_selected:
            unmatched.append(_video_constraint_text(options))
    if (
        options.audio
        or options.audio_lang
        or options.audio_type
        or options.audio_codec
        or options.audio_profile
        or options.audio_quality
        or options.audio_selection
        or options.audio_channels
        or options.select_audio
    ):
        audio_selected = _select_audio(audio_candidates, options)
        selected.extend(audio_selected)
        if audio_candidates and not audio_selected:
            unmatched.append(_audio_constraint_text(options))
    if options.subtitle_lang or options.subtitle_kind or options.subtitle_selection or options.select_subtitle:
        subtitle_selected = _select_subtitle(subtitle_candidates, options)
        selected.extend(subtitle_selected)
        if subtitle_candidates and not subtitle_selected:
            unmatched.append(_subtitle_constraint_text(options))

    return SelectionResult(_dedupe(selected), tuple(unmatched))


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
    mode = (options.video_selection or _mode(options.video, expr, default="best")).lower()
    if mode == "all":
        if heights:
            return _dedupe(
                stream
                for bucket in buckets
                for stream in bucket
                if _height(stream) in heights
            )
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
    codec = options.audio_codec or expr.get("codec") or expr.get("codecs")
    legacy_type = options.audio_type or expr.get("type")
    if codec:
        candidates = _filter_audio_codec(candidates, codec)
    if legacy_type:
        candidates = _filter_audio_type(candidates, legacy_type)
    candidates = _filter_audio_profile(candidates, options.audio_profile or expr.get("profile"))
    candidates = _filter_channels(candidates, options.audio_channels or expr.get("channels"))
    candidates = _filter_bandwidth(candidates, expr)

    targets = _numbers(options.audio or expr.get("bw") or expr.get("bandwidth") or expr.get("bitrate"))
    mode = _mode(options.audio, expr, default="best")
    quality = (options.audio_quality or mode or "best").lower()
    if options.audio_selection == "all" or mode == "all":
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
    if quality.startswith("best"):
        buckets = _language_buckets(candidates, options.audio_lang or expr.get("lang"))
        if not buckets:
            buckets = [candidates]
        selected: list[StreamInfo] = []
        for bucket in buckets:
            selected.extend(_top_n(bucket, _best_count(mode), key=_audio_sort_key))
        return selected
    if quality.startswith("worst"):
        buckets = _language_buckets(candidates, options.audio_lang or expr.get("lang"))
        if not buckets:
            buckets = [candidates]
        selected = []
        for bucket in buckets:
            selected.extend(_bottom_n(bucket, _best_count(mode), key=_audio_sort_key))
        return selected
    return _top_n(candidates, 1, key=_audio_sort_key)


def _select_subtitle(candidates: list[StreamInfo], options: SelectionOptions) -> list[StreamInfo]:
    expr = _parse_filter_expr(options.select_subtitle)
    candidates = _apply_common_expr(candidates, expr)
    candidates = _filter_language(candidates, options.subtitle_lang or expr.get("lang"))
    candidates = _filter_subtitle_kind(candidates, options.subtitle_kind or expr.get("kind"))
    mode = (options.subtitle_selection or _mode(None, expr, default="all")).lower()
    if mode.startswith("best"):
        language_value = options.subtitle_lang or expr.get("lang")
        buckets = _language_buckets(candidates, language_value) if language_value else _language_buckets_by_language(candidates)
        if not buckets:
            buckets = [candidates]
        selected: list[StreamInfo] = []
        for bucket in buckets:
            selected.extend(_top_n(bucket, _best_count(mode), key=_subtitle_sort_key))
        return selected
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


def _filter_audio_codec(candidates: list[StreamInfo], value: str | None) -> list[StreamInfo]:
    values = _tokens(value)
    if not values or "any" in values:
        return candidates
    return [stream for stream in candidates if any(_audio_codec_matches(stream, item) for item in values)]


def _filter_audio_profile(candidates: list[StreamInfo], value: str | None) -> list[StreamInfo]:
    values = _tokens(value)
    if not values or "any" in values:
        return candidates
    return [stream for stream in candidates if any(_audio_profile_matches(stream, item) for item in values)]


def _filter_channels(candidates: list[StreamInfo], value: str | None) -> list[StreamInfo]:
    values = _tokens(value)
    if not values or "any" in values:
        return candidates
    wanted = {_channel_count(number) for token in values for number in re.findall(r"\d+(?:\.\d+)?", token)}
    if not wanted:
        return candidates
    return [stream for stream in candidates if _channel_count(stream.channels) in wanted]


def _filter_subtitle_kind(candidates: list[StreamInfo], value: str | None) -> list[StreamInfo]:
    values = _tokens(value)
    if not values or "all" in values or "any" in values:
        return candidates
    return [stream for stream in candidates if _subtitle_kinds(stream) & set(values)]


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


def _bottom_n(candidates: list[StreamInfo], count: int, key) -> list[StreamInfo]:
    return sorted(candidates, key=key)[:count]


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


def _channel_count(value: str | float | int | None) -> float:
    """Normalize `5.1`/`7.1` layouts and numeric channel counts for matching."""
    if value is None:
        return 0
    try:
        number = float(re.search(r"\d+(?:\.\d+)?", str(value)).group(0))
    except (AttributeError, ValueError):
        return 0
    if number == 5.1:
        return 6
    if number == 7.1:
        return 8
    return number


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
    language_values = _language_tokens(language)
    wanted = {token for value in values for token in _language_tokens(value)}
    if "*" in wanted:
        return True
    return bool(language_values & wanted)


_LANGUAGE_ALIASES = {
    "eng": "en",
    "spa": "es",
    "esl": "es",
    "fra": "fr",
    "fre": "fr",
    "deu": "de",
    "ger": "de",
    "ita": "it",
    "por": "pt",
    "zho": "zh",
    "chi": "zh",
    "jpn": "ja",
    "kor": "ko",
    "rus": "ru",
    "ara": "ar",
    "hin": "hi",
}


def _language_tokens(value: str | None) -> set[str]:
    """Return exact, primary and common ISO-639 aliases for a language tag."""
    raw = str(value or "").strip().replace("_", "-").casefold()
    if not raw:
        return set()
    parts = raw.split("-")
    primary = _LANGUAGE_ALIASES.get(parts[0], parts[0])
    tokens = {raw, parts[0], primary}
    if len(parts) > 1:
        tokens.add(f"{primary}-{'-'.join(parts[1:])}")
    return tokens


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
    if value == "atmos":
        return _audio_profile_matches(stream, value)
    if value in {"aac", "ac3", "ac4", "dd", "ddplus", "eac3", "opus", "vorbis", "flac", "alac", "mp3"}:
        codec = "eac3" if value == "ddplus" else "ac3" if value in {"dd", "ac3"} else value
        return _audio_codec_matches(stream, codec)
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


def _audio_codec_matches(stream: StreamInfo, value: str) -> bool:
    value = value.lower().replace("-", "").replace("_", "")
    codec = (pretty_codec(stream.codecs, "audio") or stream.codecs or "").lower().replace("-", "")
    text = " ".join(
        str(item or "").lower()
        for item in (stream.codecs, stream.name, stream.id, stream.group_id, stream.extra.get("audio_codec"))
    )
    aliases = {
        "aac": ("aac", "mp4a"),
        "ac3": ("ac3", "ac-3"),
        "ac4": ("ac4", "ac-4", "dac4"),
        "eac3": ("eac3", "ec3", "e-ac-3"),
        "opus": ("opus",),
        "vorbis": ("vorbis",),
        "flac": ("flac",),
        "alac": ("alac",),
        "mp3": ("mp3", "mpa"),
    }
    if value == "atmos":
        return False
    return value in codec or any(alias in text for alias in aliases.get(value, (value,)))


def _audio_profile_matches(stream: StreamInfo, value: str) -> bool:
    value = value.lower().replace("-", "").replace("_", "").replace(" ", "")
    if value in {"any", ""}:
        return True
    if value == "atmos":
        return _extra_bool(stream, "audio_atmos") or any(
            marker in _stream_blob(stream) for marker in ("atmos", "joc", "dolbydigitalplusatmos")
        )
    kinds = _audio_role_tokens(stream)
    aliases = {
        "main": {"main", "primary", "default"},
        "primary": {"main", "primary", "default"},
        "description": {"audiodescription", "descriptive", "description"},
        "audiodescription": {"audiodescription", "descriptive", "description"},
        "commentary": {"commentary"},
        "dialog": {"dialog", "dialogue"},
    }
    return bool(kinds & aliases.get(value, {value}))


def _audio_role_tokens(stream: StreamInfo) -> set[str]:
    text = _stream_blob(stream).replace("-", " ").replace("_", " ")
    tokens = set(re.findall(r"[a-z0-9]+", text))
    compact = "".join(tokens)
    if "description" in compact or "descriptive" in compact or "audiodescription" in compact:
        tokens.add("audiodescription")
    if "commentary" in compact:
        tokens.add("commentary")
    if "dialog" in compact or "dialogue" in compact:
        tokens.add("dialog")
    if "primary" in tokens or "default" in tokens:
        tokens.add("main")
    return tokens


def _subtitle_kinds(stream: StreamInfo) -> set[str]:
    extra = stream.extra if isinstance(stream.extra, dict) else {}
    text = _stream_blob(stream).replace("-", " ").replace("_", " ")
    compact = re.sub(r"[^a-z0-9]+", "", text)
    kinds: set[str] = set()
    if _extra_bool(stream, "forced") or _extra_bool(stream, "forced_track") or "forced" in compact:
        kinds.add("forced")
    if (
        _extra_bool(stream, "sdh")
        or _extra_bool(stream, "cc")
        or _extra_bool(stream, "closed_captions")
        or any(marker in compact for marker in ("sdh", "closedcaption", "hearingimpaired", "describesmusicandsound", "transcribesspokendialog"))
        or "accessibility" in str(extra.get("characteristics") or "").lower()
    ):
        kinds.add("sdh")
    if "commentary" in compact:
        kinds.add("commentary")
    if "audiodescription" in compact or "descriptive" in compact:
        kinds.add("audio_description")
    if not kinds:
        kinds.add("normal")
    return kinds


def _subtitle_sort_key(stream: StreamInfo):
    kinds = _subtitle_kinds(stream)
    # Prefer a normal full subtitle when the user asks for one best track, then
    # forced, SDH/CC and service-specific alternatives.
    priority = 4 if "normal" in kinds else 3 if "forced" in kinds else 2 if "sdh" in kinds else 1
    return (priority, _extra_bool(stream, "default"), stream.name or "")


def _video_constraint_text(options: SelectionOptions) -> str:
    expr = _parse_filter_expr(options.select_video)
    bits = []
    quality = options.video or expr.get("res") or expr.get("height") or expr.get("for")
    language = options.video_lang or expr.get("lang")
    range_value = options.video_range or expr.get("range")
    if quality:
        bits.append(f"quality={quality}")
    if language:
        bits.append(f"language={language}")
    if range_value:
        bits.append(f"range={range_value}")
    return "video (" + ", ".join(bits or ["requested selection"]) + ")"


def _audio_constraint_text(options: SelectionOptions) -> str:
    expr = _parse_filter_expr(options.select_audio)
    bits = []
    language = options.audio_lang or expr.get("lang")
    codec = options.audio_codec or expr.get("codec") or expr.get("codecs") or options.audio_type or expr.get("type")
    profile = options.audio_profile or expr.get("profile")
    channels = options.audio_channels or expr.get("channels")
    if language:
        bits.append(f"language={language}")
    if codec:
        bits.append(f"codec={codec}")
    if profile:
        bits.append(f"profile={profile}")
    if channels:
        bits.append(f"channels={channels}")
    return "audio (" + ", ".join(bits or ["requested selection"]) + ")"


def _subtitle_constraint_text(options: SelectionOptions) -> str:
    expr = _parse_filter_expr(options.select_subtitle)
    bits = []
    language = options.subtitle_lang or expr.get("lang")
    kind = options.subtitle_kind or expr.get("kind")
    if language:
        bits.append(f"language={language}")
    if kind:
        bits.append(f"kind={kind}")
    return "subtitle (" + ", ".join(bits or ["requested selection"]) + ")"


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


def _language_buckets_by_language(candidates: list[StreamInfo]) -> list[list[StreamInfo]]:
    buckets: dict[str, list[StreamInfo]] = {}
    for stream in candidates:
        key = next(iter(sorted(_language_tokens(stream.language))), "und")
        buckets.setdefault(key, []).append(stream)
    return list(buckets.values())


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
