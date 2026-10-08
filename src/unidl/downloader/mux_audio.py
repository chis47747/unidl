"""Choose a default playback audio track without changing download selection."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from .models import StreamInfo
from .selection import _LANGUAGE_ALIASES, _audio_sort_key, _extra_bool
from .utils import compact_join, pretty_codec


def normalize_default_audio(value: object) -> str:
    """One preference, never a language filter or a comma-separated list."""
    value = str(value or "auto").strip().replace("_", "-").casefold()
    if not re.fullmatch(r"[a-z]{2,8}(?:-[a-z0-9]{1,8})*", value):
        raise ValueError("Use auto, original, or one language tag, for example en or fr-CA.")
    return value


def _language(value: object) -> str:
    tag = str(value or "und").strip().replace("_", "-").casefold()
    primary, separator, suffix = tag.partition("-")
    return _LANGUAGE_ALIASES.get(primary, primary) + (separator + suffix if separator else "")


def _matches(language: object, wanted: str) -> bool:
    actual, target = _language(language), _language(wanted)
    # A regional preference is exact. A primary tag may match its regional forms.
    return actual == target or ("-" not in target and actual.partition("-")[0] == target)


def _role(stream: StreamInfo) -> str:
    return (
        " ".join(
            str(value or "")
            for value in (
                stream.role,
                stream.name,
                stream.extra.get("audio_role"),
            )
        )
        .casefold()
        .replace("_", " ")
        .replace("-", " ")
    )


def original_audio(stream: StreamInfo) -> bool:
    if any(
        _extra_bool(stream, key)
        for key in (
            "original",
            "is_original",
            "isOriginal",
            "original_language",
        )
    ):
        return True
    original_language = stream.extra.get("original_language")
    if (
        isinstance(original_language, str)
        and original_language.strip()
        and original_language.casefold() not in {"true", "false", "yes", "no", "und", "unknown"}
    ):
        if _language(original_language) == _language(stream.language):
            return True
    raw = stream.extra.get("raw")
    if isinstance(raw, dict) and any(
        _extra_bool(StreamInfo("file", extra=raw), key) for key in ("original", "is_original", "isOriginal")
    ):
        return True
    return bool(re.search(r"\boriginal\b", _role(stream)))


def source_default_audio(stream: StreamInfo) -> bool:
    raw = stream.extra.get("raw")
    if isinstance(raw, dict) and any(
        _extra_bool(StreamInfo("file", extra=raw), key)
        for key in ("default", "is_default", "default_track", "isDefault")
    ):
        return True
    return any(
        _extra_bool(stream, key)
        for key in (
            "default",
            "is_default",
            "default_track",
            "isDefault",
        )
    ) or bool(re.search(r"\bdefault\b", _role(stream)))


def _ordinary(stream: StreamInfo) -> bool:
    return not (
        "audiodescription" in re.sub(r"\s+", "", _role(stream))
        or re.search(r"\b(commentary|descriptive|description|secondary)\b", _role(stream))
        or any(_extra_bool(stream, key) for key in ("descriptive", "description", "commentary", "audio_description"))
    )


def _best(candidates: list[StreamInfo]) -> StreamInfo:
    # Same ranking as Audio Best; max retains the first track on exact ties.
    return max(
        candidates,
        key=lambda stream: _audio_sort_key(
            replace(stream, channels=str(stream.channels)) if stream.channels is not None else stream
        ),
    )


@dataclass(frozen=True)
class DefaultAudioChoice:
    stream: StreamInfo | None
    preference: str = "auto"
    fallback: str = ""  # missing_language | missing_original
    supported: bool = True

    @property
    def label(self) -> str:
        if self.stream is None:
            return ""
        stream = self.stream
        codec = pretty_codec(stream.codecs, "audio") or stream.codecs
        if stream.extra.get("audio_atmos") and codec in {"AC-3", "E-AC-3"}:
            codec = "E-AC-3 Atmos"
        return compact_join(
            [
                stream.language or "und",
                codec,
                f"{stream.channels}CH" if stream.channels else None,
                stream.role if not _ordinary(stream) else None,
            ]
        )


def resolve_default_audio(
    selected: Sequence[StreamInfo],
    preference: object = "auto",
) -> DefaultAudioChoice:
    """Resolve afresh for the final selection of each title; do not mutate it."""
    wanted = normalize_default_audio(preference)
    audio = [stream for stream in selected if stream.media_type == "audio"]
    if not audio:
        return DefaultAudioChoice(None, wanted)
    candidates = audio
    fallback = ""
    if wanted != "auto":
        candidates = [
            stream
            for stream in audio
            if (original_audio(stream) if wanted == "original" else _matches(stream.language, wanted))
        ]
        if not candidates:
            fallback = "missing_original" if wanted == "original" else "missing_language"
            candidates = audio
    ordinary = [stream for stream in candidates if _ordinary(stream)]
    candidates = ordinary or candidates
    if wanted == "auto" or fallback:
        marked = [stream for stream in candidates if source_default_audio(stream)]
        original = [stream for stream in candidates if original_audio(stream)]
        if marked or original:
            candidates = marked or original
        else:
            # Preserve the first selected language, rather than letting a high
            # bitrate in an unrelated language change the playback language.
            language = _language(candidates[0].language)
            candidates = [stream for stream in candidates if _language(stream.language) == language]
    return DefaultAudioChoice(_best(candidates), wanted, fallback)
