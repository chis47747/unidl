from __future__ import annotations

import re

_DV_CODEC_RE = re.compile(r"(?:^|,)\s*(?:dvh1|dvhe|dva1|dvav)\.(\d{1,2})(?:\.|$)", re.IGNORECASE)
_DV_CODEC_TOKEN_RE = re.compile(r"(?:^|,)\s*(?:dvh1|dvhe|dva1|dvav)(?:[.,]|$)", re.IGNORECASE)
_BASE_VIDEO_CODEC_PREFIXES = (
    "av01",
    "av1",
    "avc1",
    "avc3",
    "hvc1",
    "hev1",
    "vvc1",
    "vvi1",
    "h266",
    "h.266",
    "vvc",
)
_DV_BASE_COMPATIBLE_PROFILES = {7, 8, 10}


def combine_dolby_vision_range(
    primary_codecs: str | None,
    base_range: str | None,
    supplemental_codecs: str | None = None,
) -> str | None:
    if not has_dolby_vision_codecs(primary_codecs) and not has_dolby_vision_codecs(supplemental_codecs):
        return base_range
    if _is_dv_with_compatible_base(primary_codecs, supplemental_codecs) and base_range and base_range not in {"SDR", "DV"}:
        return f"DV+{base_range}"
    return "DV"


def has_dolby_vision_codecs(codecs: str | None) -> bool:
    text = (codecs or "").lower()
    return bool(_DV_CODEC_TOKEN_RE.search(text)) or any(token in text for token in ("dovi", "dolby.vision", "dolby-vision"))


def dolby_vision_profiles(codecs: str | None) -> list[int]:
    profiles: list[int] = []
    for match in _DV_CODEC_RE.finditer(codecs or ""):
        try:
            profiles.append(int(match.group(1)))
        except ValueError:
            continue
    return profiles


def _is_dv_with_compatible_base(primary_codecs: str | None, supplemental_codecs: str | None) -> bool:
    if has_dolby_vision_codecs(supplemental_codecs) and _has_base_video_codec(primary_codecs):
        return True
    profiles = dolby_vision_profiles(primary_codecs) + dolby_vision_profiles(supplemental_codecs)
    return any(profile in _DV_BASE_COMPATIBLE_PROFILES for profile in profiles)


def _has_base_video_codec(codecs: str | None) -> bool:
    for token in (codecs or "").split(","):
        normalized = token.strip().lower()
        if normalized.startswith(_BASE_VIDEO_CODEC_PREFIXES):
            return True
    return False
