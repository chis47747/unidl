"""Prove a live cursor against a renewed manifest, without renumbering media."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from .ism_live import stream_ism_timescale
from .models import SegmentInfo, StreamInfo

_TOLERANCE = 0.125
Clock = tuple[str, float]


@dataclass(frozen=True, slots=True)
class LiveCursor:
    segment: SegmentInfo
    clock: Clock | None
    committed: bool


def _absolute_program_time(segment: SegmentInfo) -> float | None:
    if segment.program_date_time:
        try:
            timestamp = datetime.fromisoformat(segment.program_date_time.replace("Z", "+00:00"))
            if timestamp.tzinfo is not None:
                return timestamp.timestamp()
        except (ValueError, OverflowError):
            pass
    return None


def live_positions(stream: StreamInfo, segments: list[SegmentInfo] | None = None) -> dict[int, Clock]:
    """Shared wall-clock, DASH presentation time or scaled ISM media time."""
    media = [segment for segment in (stream.segments if segments is None else segments) if segment.index != -1]
    result: dict[int, Clock] = {}
    if stream.manifest_type == "hls":
        # HLS only needs to declare PDT once per continuous run. Never infer a
        # time across a discontinuity, gap, or unknown duration.
        position = None
        for segment in media:
            explicit = _absolute_program_time(segment)
            if explicit is not None:
                position = explicit
            if position is not None and not segment.gap:
                result[id(segment)] = ("wallclock", position)
            position = (
                position + segment.duration
                if position is not None and segment.duration and not segment.discontinuity_after and not segment.gap
                else None
            )
        position = None
        for segment in reversed(media):
            if segment.discontinuity_after or segment.gap:
                position = None
            known = result.get(id(segment))
            if known is not None:
                position = known[1]
            elif position is not None and segment.duration:
                position -= segment.duration
                result[id(segment)] = ("wallclock", position)
            else:
                position = None
        return result

    if stream.manifest_type == "ism":
        timescale = stream_ism_timescale(stream)
        if timescale:
            for segment in media:
                if segment.timeline_time is not None:
                    result[id(segment)] = ("ism", segment.timeline_time / timescale)
    elif stream.manifest_type == "dash":
        availability = stream.extra.get("dash_live_availability_start")
        # A period-relative clock without an MPD origin is only comparable
        # inside the same Period. Cross-Period renewal needs absolute timing.
        period = stream.extra.get("period_id")
        for segment in media:
            if segment.timeline_presentation_time is None:
                continue
            position = segment.timeline_presentation_time
            if isinstance(availability, (int, float)):
                result[id(segment)] = ("wallclock", availability + position)
            elif period is not None:
                result[id(segment)] = (f"dash:{period}", position)
    return result


def _known_equal(left, right) -> bool:
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def _rendition_score(original: StreamInfo, candidate: StreamInfo) -> int:
    if (
        not candidate.is_live
        or original.manifest_type != candidate.manifest_type
        or original.media_type != candidate.media_type
        or original.is_3d != candidate.is_3d
    ):
        return -1
    stable_id = bool(original.id and original.id == candidate.id and original.group_id == candidate.group_id)
    for attr in ("resolution", "codecs", "frame_rate", "video_range", "language", "channels", "role", "extension"):
        left, right = getattr(original, attr), getattr(candidate, attr)
        if left and (
            (right and not _known_equal(left, right))
            or (not right and not stable_id and attr in {"resolution", "codecs", "language"})
        ):
            return -1
    for prop in ("embedded_caption", "audio_atmos", "muxed_audio", "forced"):
        left, right = original.extra.get(prop), candidate.extra.get(prop)
        if (left or right) and not _known_equal(left, right):
            return -1
    if (
        original.bandwidth and candidate.bandwidth and original.bandwidth != candidate.bandwidth
        and not stable_id
    ):
        return -1
    # Display names may contain CDN hostnames or representation labels.
    # They resolve ambiguity but should not reject a known same rendition.
    return (
        8 * stable_id
        + 4 * bool(original.bandwidth and original.bandwidth == candidate.bandwidth)
        + 2 * bool(original.group_id and original.group_id == candidate.group_id)
        + bool(original.name and original.name == candidate.name)
    )


def _locator(segment: SegmentInfo) -> tuple[str, tuple[int, int] | None]:
    return urlsplit(segment.url).path, segment.byte_range


def _next_segment(candidate: StreamInfo, cursor: LiveCursor) -> SegmentInfo | None:
    media = [segment for segment in candidate.segments if segment.index is not None and segment.index >= 0]
    if not media:
        return None
    old = cursor.segment
    if cursor.clock is not None and (not cursor.committed or old.duration):
        domain, position = cursor.clock
        target = position + ((old.duration or 0) if cursor.committed else 0)
        positions = live_positions(candidate)
        matched = [
            segment for segment in media
            if not segment.gap and (clock := positions.get(id(segment))) is not None
            and clock[0] == domain and abs(clock[1] - target) <= _TOLERANCE
        ]
        if len(matched) == 1:
            return matched[0]
        # With comparable clock evidence, do not override a known gap or an
        # ambiguous boundary using a reused URL or sequence number.
        if any(clock[0] == domain for clock in positions.values()):
            return None
    if old.index is None or not old.url:
        return None
    location = _locator(old)
    overlap = [segment for segment in media if _locator(segment) == location]
    if not location[0] or len(overlap) != 1 or overlap[0].index != old.index:
        return None
    if old.duration and overlap[0].duration and abs(old.duration - overlap[0].duration) > _TOLERANCE:
        return None
    expected = old.index + int(cursor.committed)
    candidates = [segment for segment in media if segment.index == expected and not segment.gap]
    return candidates[0] if len(candidates) == 1 else None


def match_live_cursor(
    original: StreamInfo, candidates: list[StreamInfo], cursor: LiveCursor,
) -> tuple[StreamInfo, SegmentInfo] | None:
    matches = []
    for candidate in candidates:
        score = _rendition_score(original, candidate)
        if score >= 0 and (first := _next_segment(candidate, cursor)) is not None:
            matches.append((score, candidate, first))
    if not matches:
        return None
    matches.sort(key=lambda entry: entry[0], reverse=True)
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        return None
    return matches[0][1], matches[0][2]
