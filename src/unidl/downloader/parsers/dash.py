from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from ..live_rules import join_dash_base_uri, should_skip_dash_period
from ..models import SegmentInfo, StreamInfo
from ..utils import ceil_div_duration, join_uri, parse_frame_rate, parse_iso8601_duration, pretty_codec
from ..video_range import combine_dolby_vision_range


def parse_dash(uri: str, text: str) -> list[StreamInfo]:
    text = text.lstrip("\ufeff \t\r\n")
    root = ET.fromstring(text)
    if _local(root.tag) != "MPD":
        raise ValueError("Not a DASH MPD manifest.")

    mpd_duration = parse_iso8601_duration(root.attrib.get("mediaPresentationDuration"))
    is_live = root.attrib.get("type", "static").lower() == "dynamic"
    live_context = _LiveDashContext(
        is_live=is_live,
        availability_start=_parse_dash_datetime(root.attrib.get("availabilityStartTime")),
        time_shift_buffer_depth=parse_iso8601_duration(root.attrib.get("timeShiftBufferDepth")),
        refresh_location=_dash_location(root, uri),
    )
    mpd_base = _extend_base(uri, root)
    streams: list[StreamInfo] = []

    periods = _children(root, "Period") or [root]
    period_starts = [parse_iso8601_duration(item.attrib.get("start")) for item in periods]
    for period_index, period in enumerate(periods):
        period_id = period.attrib.get("id") or str(period_index)
        period_start = period_starts[period_index]
        period_duration = parse_iso8601_duration(period.attrib.get("duration")) or _implied_period_duration(period_index, period_starts, mpd_duration)
        period_base = _extend_base(mpd_base, period)
        if should_skip_dash_period(period_id, period_base, is_live):
            continue

        for adaptation in _children(period, "AdaptationSet"):
            adaptation_base = _extend_base(period_base, adaptation)
            adaptation_role = _role(adaptation)
            adaptation_content_type = _attr(adaptation, "contentType") or _mime_prefix(_attr(adaptation, "mimeType"))
            adaptation_frame_rate = parse_frame_rate(_attr(adaptation, "frameRate"))
            adaptation_encrypted, adaptation_scheme, adaptation_key_id = _content_protection(adaptation)
            adaptation_video_range = _video_range(adaptation)

            representations = _children(adaptation, "Representation")
            if not representations:
                representations = [adaptation]

            for representation in representations:
                rep_base = _extend_base(adaptation_base, representation)
                content_type = (
                    _attr(representation, "contentType")
                    or _mime_prefix(_attr(representation, "mimeType"))
                    or adaptation_content_type
                )
                codecs = _attr(representation, "codecs") or _attr(adaptation, "codecs")
                media_type = _dash_media_type(content_type, codecs, representation, adaptation)
                encrypted, scheme, key_id = _content_protection(representation)
                encrypted = encrypted or adaptation_encrypted
                scheme = scheme or adaptation_scheme
                key_id = key_id or adaptation_key_id
                width = _attr(representation, "width") or _attr(adaptation, "width")
                height = _attr(representation, "height") or _attr(adaptation, "height")
                resolution = f"{width}x{height}" if width and height else None
                bandwidth = _int_or_none(_attr(representation, "bandwidth") or _attr(adaptation, "bandwidth"))
                trick_mode = _is_trick_mode(representation, adaptation, content_type)
                # Without a Role element _stream_role() invents "Main" for
                # anything typed video, which is actively misleading here.
                role = "Thumbnails" if trick_mode else _stream_role(media_type, representation, adaptation, adaptation_role)
                audio_atmos = _dash_audio_is_atmos(media_type, codecs, representation, adaptation)
                stream_extra = _stream_extra(period_id, period_start, adaptation, key_id)
                if trick_mode:
                    stream_extra["trick_mode"] = 1
                stream_extra.update(_dash_template_extra(rep_base, adaptation, representation, bandwidth, _attr(representation, "id") or f"{period_id}-{len(streams)}", live_context))
                if audio_atmos:
                    stream_extra["audio_atmos"] = 1
                stream = StreamInfo(
                    manifest_type="dash",
                    media_type=media_type,
                    url=rep_base,
                    original_url=uri,
                    id=_attr(representation, "id") or f"{period_id}-{len(streams)}",
                    group_id=_stream_group_id(adaptation, media_type),
                    name=_stream_name(adaptation, media_type),
                    language=_attr(representation, "lang") or _attr(adaptation, "lang"),
                    role=role,
                    bandwidth=bandwidth,
                    codecs=codecs,
                    resolution=resolution,
                    frame_rate=parse_frame_rate(_attr(representation, "frameRate")) or adaptation_frame_rate,
                    channels=_audio_channels(representation) or _audio_channels(adaptation),
                    extension=_extension_from_mime(_attr(representation, "mimeType") or _attr(adaptation, "mimeType")),
                    video_range=_video_range_for_stream(media_type, codecs, representation, adaptation, adaptation_video_range),
                    duration=period_duration,
                    encrypted=encrypted,
                    encryption_scheme=scheme,
                    is_live=is_live,
                    extra=stream_extra,
                )
                stream.segments = _segments_for_representation(
                    base_uri=rep_base,
                    period_start=period_start,
                    period_duration=period_duration,
                    adaptation=adaptation,
                    representation=representation,
                    bandwidth=bandwidth,
                    representation_id=stream.id,
                    encrypted=encrypted,
                    encryption_scheme=scheme,
                    key_id=key_id,
                    live_context=live_context,
                )
                if stream.segments:
                    stream.url = stream.segments[0].url
                stream.size_bytes = _byte_range_segments_size(stream.segments)
                streams.append(stream)

    return _merge_period_streams(streams, live=is_live) if len(periods) > 1 else streams


def _implied_period_duration(index: int, starts: list[float | None], mpd_duration: float | None) -> float | None:
    start = starts[index]
    next_start = starts[index + 1] if index + 1 < len(starts) else None
    if start is not None and next_start is not None:
        return max(0.0, next_start - start)
    if mpd_duration is not None and start:
        return max(0.0, mpd_duration - start)
    return mpd_duration


def _merge_period_streams(streams: list[StreamInfo], live: bool = False) -> list[StreamInfo]:
    merged: list[StreamInfo] = []
    by_key: dict[tuple[object, ...], StreamInfo] = {}
    merge_states: dict[tuple[object, ...], _PeriodSegmentMergeState] = {}
    period_occurrences: dict[tuple[str | None, tuple[object, ...]], int] = {}
    for stream in streams:
        key = _period_stream_key(stream, live=live)
        if not live:
            # Representation IDs are not guaranteed to be stable across VOD
            # periods. Some packagers generate a new UUID for every period,
            # even though the representation continues the same track. Use
            # its position among otherwise identical representations to keep
            # genuine same-period duplicates separate without making the ID
            # part of the cross-period identity.
            period_id = stream.extra.get("period_id") if stream.extra else None
            period_id = period_id if isinstance(period_id, str) else None
            occurrence_key = (period_id, key)
            occurrence = period_occurrences.get(occurrence_key, 0)
            period_occurrences[occurrence_key] = occurrence + 1
            key = (*key, occurrence)
        target = by_key.get(key)
        if target is None:
            _remember_period(stream, stream)
            by_key[key] = stream
            merge_states[key] = _PeriodSegmentMergeState.from_segments(stream.segments)
            merged.append(stream)
            continue
        if _stream_already_has_period(target, stream):
            continue
        _merge_period_stream(target, stream, preserve_indexes=live, merge_state=merge_states[key])
    return merged


def _period_stream_key(stream: StreamInfo, live: bool = False) -> tuple[object, ...]:
    return (
        stream.manifest_type,
        stream.media_type,
        None if live else stream.group_id,
        stream.language,
        stream.role,
        stream.name,
        stream.resolution,
        _live_bandwidth_key(stream) if live else None,
        _codec_key(stream.codecs),
        stream.frame_rate if not live else None,
        stream.channels,
        stream.extension if not live else None,
        stream.video_range,
    )


def _codec_key(codecs: str | None) -> str | None:
    if not codecs:
        return None
    return ",".join(token.strip().lower() for token in codecs.split(",") if token.strip()) or None


def _live_bandwidth_key(stream: StreamInfo) -> str | None:
    if not stream.bandwidth:
        return None
    step = 10_000 if stream.media_type == "audio" else 1_000
    return str(int(round(stream.bandwidth / step) * step))


def _merge_period_stream(
    target: StreamInfo,
    source: StreamInfo,
    preserve_indexes: bool = False,
    merge_state: _PeriodSegmentMergeState | None = None,
) -> None:
    target.duration = _sum_optional(target.duration or target.total_duration, source.duration or source.total_duration)
    target.encrypted = target.encrypted or source.encrypted
    target.encryption_scheme = target.encryption_scheme or source.encryption_scheme
    _remember_period(target, source)
    _merge_key_ids(target, source)
    _append_period_segments(
        target,
        source.segments,
        preserve_indexes=preserve_indexes,
        period_offset=_period_index_offset(source),
        merge_state=merge_state,
    )
    if target.segments:
        target.url = target.segments[0].url


def _stream_already_has_period(target: StreamInfo, source: StreamInfo) -> bool:
    period_id = source.extra.get("period_id") if source.extra else None
    if not isinstance(period_id, str) or not period_id:
        return False
    ids = target.extra.get("period_ids") if target.extra else None
    if isinstance(ids, list) and period_id in ids:
        return True
    target_period_id = target.extra.get("period_id") if target.extra else None
    return target_period_id == period_id


def _sum_optional(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _remember_period(target: StreamInfo, source: StreamInfo) -> None:
    ids = target.extra.get("period_ids")
    if not isinstance(ids, list):
        ids = []
        first_id = target.extra.get("period_id")
        if isinstance(first_id, str) and first_id:
            ids.append(first_id)
        target.extra["period_ids"] = ids
    period_id = source.extra.get("period_id")
    if isinstance(period_id, str) and period_id and period_id not in ids:
        ids.append(period_id)


def _merge_key_ids(target: StreamInfo, source: StreamInfo) -> None:
    keys = target.extra.get("key_ids")
    if not isinstance(keys, list):
        keys = []
        target_key = target.extra.get("key_id")
        if isinstance(target_key, str) and target_key:
            keys.append(target_key)
        target.extra["key_ids"] = keys
    source_keys = source.extra.get("key_ids")
    if not isinstance(source_keys, list):
        source_keys = []
    for key in [source.extra.get("key_id"), *source_keys]:
        if isinstance(key, str) and key and key not in keys:
            keys.append(key)
    if "key_id" not in target.extra and keys:
        target.extra["key_id"] = keys[0]


def _append_period_segments(
    target: StreamInfo,
    segments: list[SegmentInfo],
    preserve_indexes: bool = False,
    period_offset: int | None = None,
    merge_state: _PeriodSegmentMergeState | None = None,
) -> None:
    state = merge_state or _PeriodSegmentMergeState.from_segments(target.segments)
    pending_init: list[SegmentInfo] = []
    for segment in segments:
        if segment.index == -1:
            pending_init.append(segment)
            continue
        duplicate_key = _media_segment_duplicate_key(segment)
        duplicate = state.duplicates.get(duplicate_key)
        if duplicate is not None:
            duplicate.duration = _sum_optional(duplicate.duration, segment.duration)
            pending_init = []
            continue
        for init_segment in pending_init:
            target.segments.append(replace(init_segment, index=-1))
        pending_init = []
        media_index = _merged_media_index(segment, state.next_index, preserve_indexes, period_offset)
        if media_index in state.used_indexes:
            media_index = state.next_index
        appended = replace(segment, index=media_index)
        target.segments.append(appended)
        state.duplicates.setdefault(duplicate_key, appended)
        state.used_indexes.add(media_index)
        state.next_index = max(state.next_index, media_index + 1)


@dataclass(slots=True)
class _PeriodSegmentMergeState:
    next_index: int
    used_indexes: set[int]
    duplicates: dict[tuple[object, ...], SegmentInfo]

    @classmethod
    def from_segments(cls, segments: list[SegmentInfo]) -> _PeriodSegmentMergeState:
        used_indexes: set[int] = set()
        duplicates: dict[tuple[object, ...], SegmentInfo] = {}
        next_index = 0
        for segment in segments:
            if segment.index != -1:
                duplicates.setdefault(_media_segment_duplicate_key(segment), segment)
            if segment.index is None or segment.index < 0:
                continue
            used_indexes.add(segment.index)
            next_index = max(next_index, segment.index + 1)
        return cls(next_index=next_index, used_indexes=used_indexes, duplicates=duplicates)


def _media_segment_duplicate_key(segment: SegmentInfo) -> tuple[object, ...]:
    return (
        segment.url,
        segment.byte_range,
        segment.data,
        segment.encrypted,
        segment.encryption_scheme,
        segment.key_id,
    )


def _next_media_index(segments: list[SegmentInfo]) -> int:
    indexes = [segment.index for segment in segments if segment.index is not None and segment.index >= 0]
    return max(indexes, default=-1) + 1


def _merged_media_index(segment: SegmentInfo, fallback: int, preserve_indexes: bool, period_offset: int | None) -> int:
    if not preserve_indexes or segment.index is None or segment.index < 0:
        return fallback
    if period_offset is None:
        return segment.index
    return period_offset + segment.index


def _index_is_used(segments: list[SegmentInfo], index: int) -> bool:
    return any(segment.index == index for segment in segments if segment.index is not None and segment.index >= 0)


def _period_index_offset(stream: StreamInfo) -> int | None:
    if stream.extra and stream.extra.get("dash_index_is_timeline"):
        # SegmentTimeline indexes are already media timeline timestamps; adding
        # period start makes audio/video tracks with different timescales drift.
        return None
    value = stream.extra.get("period_start") if stream.extra else None
    if isinstance(value, (int, float)):
        return int(value * 1000) * 1_000_000
    period_id = stream.extra.get("period_id") if stream.extra else None
    if isinstance(period_id, str) and period_id.isdigit():
        return int(period_id) * 1_000_000
    return None


def _find_duplicate_media_segment(segments: list[SegmentInfo], candidate: SegmentInfo) -> SegmentInfo | None:
    for segment in segments:
        if segment.index == -1:
            continue
        if (
            segment.url == candidate.url
            and segment.byte_range == candidate.byte_range
            and segment.data == candidate.data
            and segment.encrypted == candidate.encrypted
            and segment.encryption_scheme == candidate.encryption_scheme
            and segment.key_id == candidate.key_id
        ):
            return segment
    return None


def _segments_for_representation(
    base_uri: str,
    period_start: float | None,
    period_duration: float | None,
    adaptation: ET.Element,
    representation: ET.Element,
    bandwidth: int | None,
    representation_id: str | None,
    encrypted: bool,
    encryption_scheme: str | None,
    key_id: str | None,
    live_context: _LiveDashContext | None = None,
) -> list[SegmentInfo]:
    segment_template = _segment_template_for(representation, adaptation)
    if segment_template is not None:
        return _segments_from_template(
            base_uri,
            period_start,
            period_duration,
            segment_template,
            bandwidth,
            representation_id,
            encrypted,
            encryption_scheme,
            key_id,
            live_context,
        )

    segment_list = _first_child(representation, "SegmentList")
    if segment_list is None:
        segment_list = _first_child(adaptation, "SegmentList")
    if segment_list is not None:
        return _segments_from_list(base_uri, period_duration, segment_list, adaptation, representation, encrypted, encryption_scheme, key_id, live_context)

    segment_base = _first_child(representation, "SegmentBase")
    if segment_base is None:
        segment_base = _first_child(adaptation, "SegmentBase")
    if segment_base is not None:
        return [SegmentInfo(url=base_uri, duration=period_duration, index=0, encrypted=encrypted, encryption_scheme=encryption_scheme, key_id=key_id)]

    return [SegmentInfo(url=base_uri, duration=period_duration, index=0, encrypted=encrypted, encryption_scheme=encryption_scheme, key_id=key_id)]


def _segment_template_for(representation: ET.Element, adaptation: ET.Element) -> ET.Element | None:
    child = _first_child(representation, "SegmentTemplate")
    parent = _first_child(adaptation, "SegmentTemplate")
    if child is None:
        return parent
    if parent is None:
        return child
    merged = ET.Element(child.tag, {**parent.attrib, **child.attrib})
    overridden = {_local(item.tag) for item in child}
    for item in list(child) + [node for node in parent if _local(node.tag) not in overridden]:
        merged.append(deepcopy(item))
    return merged


class _LiveDashContext:
    def __init__(
        self,
        is_live: bool,
        availability_start: datetime | None = None,
        time_shift_buffer_depth: float | None = None,
        refresh_location: str | None = None,
    ) -> None:
        self.is_live = is_live
        self.availability_start = availability_start
        self.time_shift_buffer_depth = time_shift_buffer_depth
        self.refresh_location = refresh_location


def _segments_from_template(
    base_uri: str,
    period_start: float | None,
    period_duration: float | None,
    template: ET.Element,
    bandwidth: int | None,
    representation_id: str | None,
    encrypted: bool,
    encryption_scheme: str | None,
    key_id: str | None,
    live_context: _LiveDashContext | None = None,
) -> list[SegmentInfo]:
    segments: list[SegmentInfo] = []
    timescale = _int_or_none(template.attrib.get("timescale")) or 1
    presentation_time_offset_ticks = _int_or_none(template.attrib.get("presentationTimeOffset")) or 0
    start_number = _dash_start_number(template)
    duration_ticks = _int_or_none(template.attrib.get("duration"))
    end_number = _int_or_none(template.attrib.get("endNumber"))
    media_template = template.attrib.get("media")
    init_template = template.attrib.get("initialization")
    variables = {
        "RepresentationID": representation_id or "",
        "Bandwidth": str(bandwidth or ""),
    }

    if init_template:
        init_url = join_uri(base_uri, _replace_template_vars(init_template, variables))
        segments.append(
            SegmentInfo(
                url=init_url,
                duration=0,
                index=-1,
                encrypted=encrypted,
                encryption_scheme=encryption_scheme,
                key_id=key_id,
            )
        )

    if not media_template:
        return segments

    timeline = _first_child(template, "SegmentTimeline")
    if timeline is not None:
        sequence_number = start_number
        current_time = 0
        media_index = 0
        s_nodes = _children(timeline, "S")
        for pos, node in enumerate(s_nodes):
            if node.attrib.get("t") is not None:
                current_time = int(node.attrib["t"])
            duration = int(node.attrib.get("d", "0"))
            if duration <= 0:
                continue
            repeat = int(node.attrib.get("r", "0"))
            if repeat < 0:
                next_t = _next_t(s_nodes, pos)
                if next_t is not None:
                    total_entries = max(1, math.ceil((next_t - current_time) / duration))
                else:
                    total_entries = ceil_div_duration(period_duration, duration, timescale)
            else:
                total_entries = repeat + 1
            for _ in range(total_entries):
                variables.update({"Number": str(sequence_number), "Time": str(current_time)})
                presentation_time = _segment_timeline_presentation_time(current_time, presentation_time_offset_ticks, timescale, period_start)
                segments.append(
                    SegmentInfo(
                        url=join_uri(base_uri, _replace_template_vars(media_template, variables)),
                        duration=duration / timescale,
                        index=current_time if live_context and live_context.is_live else media_index,
                        encrypted=encrypted,
                        encryption_scheme=encryption_scheme,
                        key_id=key_id,
                        timeline_time=current_time,
                        timeline_presentation_time=presentation_time,
                    )
                )
                sequence_number += 1
                media_index += 1
                current_time += duration
        return segments

    if duration_ticks is None:
        return segments
    live_start_number, total_segments = _live_template_window(start_number, duration_ticks, timescale, template, live_context)
    if total_segments <= 0:
        total_segments = _static_template_segment_count(start_number, end_number, period_duration, duration_ticks, timescale)
        live_start_number = start_number
    segment_duration = duration_ticks / timescale
    static_period_duration = period_duration if end_number is None and not (live_context and live_context.is_live) else None
    for media_index, number in enumerate(range(live_start_number, live_start_number + total_segments)):
        variables.update({"Number": str(number), "Time": str((number - start_number) * duration_ticks)})
        media_duration = segment_duration
        if static_period_duration is not None:
            remaining_duration = static_period_duration - media_index * segment_duration
            if remaining_duration > 0:
                media_duration = min(segment_duration, remaining_duration)
        segments.append(
            SegmentInfo(
                url=join_uri(base_uri, _replace_template_vars(media_template, variables)),
                duration=media_duration,
                index=number if live_context and live_context.is_live else media_index,
                encrypted=encrypted,
                encryption_scheme=encryption_scheme,
                key_id=key_id,
            )
        )
    return segments


def _segment_timeline_presentation_time(time_value: int, presentation_time_offset_ticks: int, timescale: int, period_start: float | None) -> float:
    return float(period_start or 0.0) + (float(time_value - presentation_time_offset_ticks) / max(1, timescale))


def _live_template_window(
    start_number: int,
    duration_ticks: int,
    timescale: int,
    template: ET.Element,
    live_context: _LiveDashContext | None,
) -> tuple[int, int]:
    if not live_context or not live_context.is_live:
        return start_number, 0
    if not live_context.availability_start or not live_context.time_shift_buffer_depth:
        return start_number, 0
    segment_duration = duration_ticks / max(1, timescale)
    if segment_duration <= 0:
        return start_number, 0
    now = datetime.now(timezone.utc)
    presentation_offset = (_int_or_none(template.attrib.get("presentationTimeOffset")) or 0) / max(1, timescale)
    available_start = live_context.availability_start
    if available_start.tzinfo is None:
        available_start = available_start.replace(tzinfo=timezone.utc)
    elapsed = max(0.0, (now - available_start.astimezone(timezone.utc)).total_seconds() - presentation_offset)
    depth = max(segment_duration, live_context.time_shift_buffer_depth)
    total_segments = max(1, int(depth / segment_duration))
    first_available = start_number + max(0, int((elapsed - depth) / segment_duration))
    return first_available, total_segments


def _dash_start_number(template: ET.Element) -> int:
    value = _int_or_none(template.attrib.get("startNumber"))
    return 1 if value is None else value


def _static_template_segment_count(
    start_number: int,
    end_number: int | None,
    period_duration: float | None,
    duration_ticks: int,
    timescale: int,
) -> int:
    if end_number is not None and end_number >= start_number:
        return end_number - start_number + 1
    return ceil_div_duration(period_duration, duration_ticks, timescale)


def _segments_from_list(
    base_uri: str,
    period_duration: float | None,
    segment_list: ET.Element,
    adaptation: ET.Element,
    representation: ET.Element,
    encrypted: bool,
    encryption_scheme: str | None,
    key_id: str | None,
    live_context: _LiveDashContext | None = None,
) -> list[SegmentInfo]:
    segments: list[SegmentInfo] = []
    timescale = _int_or_none(segment_list.attrib.get("timescale")) or 1
    start_number = _dash_start_number(segment_list)
    duration_ticks = _int_or_none(segment_list.attrib.get("duration"))
    segment_urls = _children(segment_list, "SegmentURL")
    if segment_urls and not segment_urls[0].attrib.get("media") and not segment_urls[0].attrib.get("mediaRange"):
        return [SegmentInfo(url=base_uri, duration=period_duration, index=0, encrypted=encrypted, encryption_scheme=encryption_scheme, key_id=key_id)]
    timeline_entries = _segment_list_timeline_entries(segment_list, period_duration, timescale, len(segment_urls))
    duration_seconds = _segment_list_duration_seconds(segment_list, adaptation, representation, timescale, len(segment_urls))
    initialization = _first_child(segment_list, "Initialization")
    if initialization is not None:
        init_source = initialization.attrib.get("sourceURL")
        init_range = _parse_range(initialization.attrib.get("range"))
        if init_source or init_range:
            init_url = join_uri(base_uri, init_source) if init_source else base_uri
            segments.append(
                SegmentInfo(
                    url=init_url,
                    duration=0,
                    index=-1,
                    byte_range=init_range,
                    encrypted=encrypted,
                    encryption_scheme=encryption_scheme,
                    key_id=key_id,
                )
            )

    for index, node in enumerate(segment_urls):
        media = node.attrib.get("media")
        byte_range = _parse_range(node.attrib.get("mediaRange"))
        if not media and not byte_range:
            continue
        segment_url = join_uri(base_uri, media) if media else base_uri
        timeline_entry = timeline_entries[index] if index < len(timeline_entries) else None
        duration = _segment_list_segment_duration(index, timeline_entry, duration_seconds, duration_ticks, timescale)
        media_index = index
        if live_context and live_context.is_live:
            media_index = int(timeline_entry[0]) if timeline_entry is not None else start_number + index
        segments.append(
            SegmentInfo(
                url=segment_url,
                duration=duration,
                index=media_index,
                byte_range=byte_range,
                encrypted=encrypted,
                encryption_scheme=encryption_scheme,
                key_id=key_id,
            )
        )
    return segments


def _segment_list_segment_duration(
    index: int,
    timeline_entry: tuple[int, int] | None,
    duration_seconds: list[float],
    duration_ticks: int | None,
    timescale: int,
) -> float | None:
    if timeline_entry is not None:
        return timeline_entry[1] / timescale
    if index < len(duration_seconds):
        return duration_seconds[index]
    if duration_ticks:
        return duration_ticks / timescale
    return None


def _segment_list_duration_seconds(
    segment_list: ET.Element,
    adaptation: ET.Element,
    representation: ET.Element,
    default_timescale: int,
    max_entries: int,
) -> list[float]:
    durations = _first_child(representation, "SegmentDurations")
    if durations is None:
        durations = _first_child(segment_list, "SegmentDurations")
    if durations is None:
        durations = _first_child(adaptation, "SegmentDurations")
    if durations is None:
        return []
    timescale = _int_or_none(durations.attrib.get("timescale")) or default_timescale or 1
    entries: list[float] = []
    for node in _children(durations, "S"):
        duration = _int_or_none(node.attrib.get("d")) or 0
        if duration <= 0:
            continue
        repeat = max(0, _int_or_none(node.attrib.get("r")) or 0)
        for _ in range(repeat + 1):
            if max_entries and len(entries) >= max_entries:
                return entries
            entries.append(duration / timescale)
    return entries


def _byte_range_segments_size(segments: list[SegmentInfo]) -> int | None:
    total = 0
    found = False
    for segment in segments:
        if not segment.byte_range:
            continue
        start, end = segment.byte_range
        total += max(0, end - start + 1)
        found = True
    return total if found else None


def _segment_list_timeline_entries(
    segment_list: ET.Element,
    period_duration: float | None,
    timescale: int,
    max_entries: int,
) -> list[tuple[int, int]]:
    timeline = _first_child(segment_list, "SegmentTimeline")
    if timeline is None:
        return []
    entries: list[tuple[int, int]] = []
    current_time = 0
    s_nodes = _children(timeline, "S")
    for pos, node in enumerate(s_nodes):
        if node.attrib.get("t") is not None:
            current_time = int(node.attrib["t"])
        duration = int(node.attrib.get("d", "0"))
        if duration <= 0:
            continue
        repeat = int(node.attrib.get("r", "0"))
        if repeat < 0:
            next_t = _next_t(s_nodes, pos)
            if next_t is not None:
                total_entries = max(1, math.ceil((next_t - current_time) / duration))
            elif max_entries:
                total_entries = max(0, max_entries - len(entries))
            else:
                total_entries = ceil_div_duration(period_duration, duration, timescale)
        else:
            total_entries = repeat + 1
        for _ in range(total_entries):
            if max_entries and len(entries) >= max_entries:
                return entries
            entries.append((current_time, duration))
            current_time += duration
    return entries


def _replace_template_vars(template: str, values: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group("name")
        fmt = match.group("fmt")
        value = values.get(name, "")
        if fmt:
            try:
                return fmt % int(value)
            except (ValueError, TypeError):
                return value
        return value

    return re.sub(r"\$(?P<name>[A-Za-z]+)(?P<fmt>%0?\d+d)?\$", repl, template)


def _parse_dash_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _content_protection(element: ET.Element) -> tuple[bool, str | None, str | None]:
    protections = _children(element, "ContentProtection")
    if not protections:
        return False, None, None
    encrypted = False
    scheme_name: str | None = None
    key_id: str | None = None
    for protection in protections:
        scheme = (protection.attrib.get("schemeIdUri") or "").lower()
        value = (protection.attrib.get("value") or "").lower()
        key_id = key_id or _content_protection_key_id(protection)
        if "cenc" in scheme or "widevine" in scheme or "playready" in scheme or key_id:
            encrypted = True
            scheme_name = scheme_name or _protection_scheme(value)
    return encrypted or bool(protections), scheme_name or "CENC", key_id


def _content_protection_key_id(element: ET.Element) -> str | None:
    for name, value in element.attrib.items():
        attr_name = _local(name).lower().split(":")[-1]
        if attr_name in {"default_kid", "kid", "keyid"}:
            kid = _normalize_key_id(value)
            if kid:
                return kid
    return None


def _normalize_key_id(value: str | None) -> str | None:
    if not value:
        return None
    for token in re.findall(r"(?:0x)?[0-9A-Fa-f][0-9A-Fa-f\-\s{}]{30,70}", value):
        cleaned = token.lower().replace("0x", "").replace("-", "").replace("{", "").replace("}", "").replace(" ", "")
        if len(cleaned) >= 32:
            cleaned = cleaned[:32]
        if len(cleaned) == 32 and all(char in "0123456789abcdef" for char in cleaned):
            return cleaned
    cleaned = value.lower().replace("0x", "").replace("-", "").replace("{", "").replace("}", "").replace(" ", "")
    if len(cleaned) == 32 and all(char in "0123456789abcdef" for char in cleaned):
        return cleaned
    return None


def _protection_scheme(value: str | None) -> str:
    normalized = (value or "").lower()
    if "cbcs" in normalized:
        return "CBCS"
    if "cenc" in normalized:
        return "CENC"
    return "CENC"


_TRICK_MODE_SCHEME_HINTS = ("trickmode", "thumbnail_tile")


def _is_trick_mode(representation: ET.Element, adaptation: ET.Element, content_type: str | None) -> bool:
    """Whether this is a thumbnail-tile / trick-play set rather than a rendition.

    These carry a sprite grid, so their coded size is the size of the whole
    grid: a 4x8 tile of 320x180 thumbnails reports 1280x1440 and outranks the
    real 1080p rendition on height alone. `_dash_media_type` still calls them
    video (they do have a width), so they must be flagged for "best" ranking
    and for selection filters to skip.
    """
    if (content_type or "").lower() == "image":
        return True
    for element in (representation, adaptation):
        for child in list(element):
            if _local(child.tag) not in {"SupplementalProperty", "EssentialProperty"}:
                continue
            scheme = (child.attrib.get("schemeIdUri") or "").lower()
            if any(hint in scheme for hint in _TRICK_MODE_SCHEME_HINTS):
                return True
    return False


def _video_range(element: ET.Element) -> str | None:
    explicit = element.attrib.get("videoRange")
    if explicit:
        return _normalize_video_range(explicit)
    for child in list(element):
        if _local(child.tag) not in {"SupplementalProperty", "EssentialProperty"}:
            continue
        scheme = (child.attrib.get("schemeIdUri") or "").lower()
        value = (child.attrib.get("value") or "").lower()
        raw = " ".join(str(value).lower() for value in child.attrib.values())
        if "transfercharacteristics" in scheme:
            if value == "18":
                return "HLG"
            if value == "16":
                return "HDR10"
            if value in {"1", "6", "13"}:
                return "SDR"
        if _is_hdr10_plus_signal(raw):
            return "HDR10+"
        if "hlg" in raw or "arib-std-b67" in raw:
            return "HLG"
        if "hdr" in raw or "smpte:2084" in raw or "st2084" in raw or "pq" in raw:
            return "HDR10"
        if "sdr" in raw:
            return "SDR"
    return None


def _video_range_for_stream(
    media_type: str,
    codecs: str | None,
    representation: ET.Element,
    adaptation: ET.Element,
    adaptation_range: str | None,
) -> str | None:
    if media_type != "video":
        return None
    base_range = adaptation_range or _video_range(representation)
    supplemental_codecs = ",".join(
        value
        for value in [_supplemental_codecs(representation), _supplemental_codecs(adaptation)]
        if value
    )
    return combine_dolby_vision_range(codecs, base_range, supplemental_codecs) or "SDR"


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


def _is_hdr10_plus_signal(value: str) -> bool:
    normalized = value.lower().replace("_", "-").replace(" ", "-")
    return any(token in normalized for token in ("hdr10+", "hdr10plus", "hdr10-plus", "hdr10p", "2094-40"))


def _supplemental_codecs(element: ET.Element) -> str | None:
    values: list[str] = []
    for name, value in element.attrib.items():
        if _local(name).lower().split(":")[-1] in {"supplementalcodecs", "supplemental-codecs"} and value:
            values.append(value)
    return ",".join(values) or None


def _dash_media_type(content_type: str | None, codecs: str | None, representation: ET.Element, adaptation: ET.Element) -> str:
    content_type = (content_type or "").lower()
    if content_type in {"video", "audio", "text"}:
        return "subtitle" if content_type == "text" else content_type
    codec_name = pretty_codec(codecs)
    if codec_name in {"WebVTT", "TTML"}:
        return "subtitle"
    if representation.attrib.get("width") or adaptation.attrib.get("width"):
        return "video"
    if _audio_channels(representation) or _audio_channels(adaptation):
        return "audio"
    return "unknown"


def _role(element: ET.Element) -> str | None:
    role = _first_child(element, "Role")
    if role is None:
        return None
    value = role.attrib.get("value")
    if not value:
        return None
    return value.replace("-", " ").title().replace(" ", "")


def _stream_role(media_type: str, representation: ET.Element, adaptation: ET.Element, adaptation_role: str | None) -> str | None:
    explicit = _role(representation)
    if media_type == "audio":
        return _normalize_audio_role(explicit) or _audio_track_role(representation, adaptation) or _normalize_audio_role(adaptation_role)
    return explicit or adaptation_role or ("Main" if media_type == "video" else None)


def _audio_track_role(*elements: ET.Element) -> str | None:
    parts: list[str] = []
    for element in elements:
        parts.extend(
            str(value)
            for value in [
                element.attrib.get("audioTrackSubtype"),
                element.attrib.get("audioTrackType"),
                element.attrib.get("role"),
                element.attrib.get("audioTrackId"),
                element.attrib.get("id"),
                *_descriptor_values(element),
            ]
            if value
        )
    subtype = (
        " ".join(parts)
    )
    raw = subtype.lower()
    compact = re.sub(r"[^a-z0-9]+", "", raw)
    if "description" in compact or "descriptive" in compact or "describesvideo" in compact:
        return "Audio Description"
    if "boosteddialoghigh" in compact:
        return "Boosted Dialog High"
    if "boosteddialogmedium" in compact:
        return "Boosted Dialog Medium"
    if "boosteddialog" in compact or "enhanceddialog" in compact:
        return "Boosted Dialog"
    if "commentary" in compact:
        return "Commentary"
    if "dialog" in compact or "dialogue" in compact:
        return "Dialog"
    if "alternate" in compact:
        return "Alternate"
    return None


def _normalize_audio_role(value: str | None) -> str | None:
    if not value:
        return None
    compact = re.sub(r"[^a-z0-9]+", "", value.lower())
    if compact in {"description", "descriptive", "audiodescription"}:
        return "Audio Description"
    if compact in {"main", "dialog", "commentary", "alternate"}:
        return value
    return value


def _dash_audio_is_atmos(media_type: str, codecs: str | None, representation: ET.Element, adaptation: ET.Element) -> bool:
    if media_type != "audio":
        return False
    codec_label = pretty_codec(codecs, "audio")
    if codec_label not in {"E-AC-3", "E-AC-3 Atmos"}:
        return False
    raw = " ".join(
        str(value)
        for element in (representation, adaptation)
        for value in [
            element.attrib.get("id"),
            element.attrib.get("audioTrackId"),
            element.attrib.get("audioTrackSubtype"),
            element.attrib.get("codecs"),
            *_descriptor_values(element),
        ]
        if value
    ).lower()
    compact = re.sub(r"[^a-z0-9]+", "", raw)
    return "atmos" in compact or "joc" in compact or "atm3" in compact


def _descriptor_values(element: ET.Element) -> list[str]:
    values: list[str] = []
    for child in list(element):
        if _local(child.tag) not in {"Role", "Accessibility", "SupplementalProperty", "EssentialProperty", "Label"}:
            continue
        values.extend(str(value) for value in child.attrib.values() if value)
        if child.text and child.text.strip():
            values.append(child.text.strip())
    return values


def _stream_group_id(adaptation: ET.Element, media_type: str) -> str | None:
    if media_type == "audio":
        return adaptation.attrib.get("audioTrackId") or _attr(adaptation, "id")
    return _attr(adaptation, "id")


def _stream_name(adaptation: ET.Element, media_type: str) -> str | None:
    if media_type == "audio":
        return adaptation.attrib.get("audioTrackId")
    return None


def _stream_extra(period_id: str, period_start: float | None, adaptation: ET.Element, key_id: str | None) -> dict[str, str | float | list[str]]:
    extra: dict[str, str | float | list[str]] = {"period_id": period_id}
    if period_start is not None:
        extra["period_start"] = period_start
    for name in ("audioTrackId", "audioTrackIndex", "audioTrackSubtype"):
        value = adaptation.attrib.get(name)
        if value:
            extra[name] = value
    if key_id:
        extra["key_id"] = key_id
        extra["key_ids"] = [key_id]
    return extra


def _dash_template_extra(
    base_uri: str,
    adaptation: ET.Element,
    representation: ET.Element,
    bandwidth: int | None,
    representation_id: str | None,
    live_context: _LiveDashContext | None,
) -> dict[str, str | int | float]:
    extra: dict[str, str | int | float] = {}
    adaptation_id = _attr(adaptation, "id")
    if adaptation_id:
        extra["dash_adaptation_id"] = adaptation_id
    if live_context and live_context.refresh_location:
        extra["dash_refresh_url"] = live_context.refresh_location
    template = _segment_template_for(representation, adaptation)
    if template is None:
        segment_list = _first_child(representation, "SegmentList")
        if segment_list is None:
            segment_list = _first_child(adaptation, "SegmentList")
        if segment_list is not None:
            extra.update(_dash_presentation_timing_extra(segment_list))
            if _first_child(segment_list, "SegmentTimeline") is not None:
                extra["dash_index_is_timeline"] = 1
        return extra
    timing_extra = _dash_presentation_timing_extra(template)
    extra.update(timing_extra)
    if _first_child(template, "SegmentTimeline") is not None:
        extra["dash_index_is_timeline"] = 1
    media = template.attrib.get("media")
    if media:
        extra["dash_template_base_uri"] = base_uri
        extra["dash_media_template"] = media
        extra["dash_timescale"] = _int_or_none(template.attrib.get("timescale")) or 1
        extra["dash_start_number"] = _dash_start_number(template)
        if representation_id:
            extra["dash_representation_id"] = representation_id
        if bandwidth:
            extra["dash_bandwidth"] = bandwidth
    return extra


def _dash_presentation_timing_extra(template: ET.Element) -> dict[str, float | int]:
    timescale = _int_or_none(template.attrib.get("timescale")) or 1
    presentation_time_offset = _int_or_none(template.attrib.get("presentationTimeOffset")) or 0
    first_time = _dash_first_segment_time(template)
    if first_time is None and presentation_time_offset == 0:
        return {}
    first_time = first_time or 0
    presentation_start = (first_time - presentation_time_offset) / max(1, timescale)
    return {
        "dash_presentation_time_offset": presentation_time_offset / max(1, timescale),
        "dash_presentation_start": presentation_start,
    }


def _dash_first_segment_time(template: ET.Element) -> int | None:
    timeline = _first_child(template, "SegmentTimeline")
    if timeline is not None:
        nodes = _children(timeline, "S")
        if not nodes:
            return None
        return _int_or_none(nodes[0].attrib.get("t")) or 0
    duration = _int_or_none(template.attrib.get("duration"))
    if duration is None:
        return None
    return 0


def _dash_location(root: ET.Element, uri: str) -> str | None:
    location = _first_child(root, "Location")
    if location is None or not (location.text or "").strip():
        return None
    return join_uri(uri, location.text.strip())


def _audio_channels(element: ET.Element) -> str | None:
    node = _first_child(element, "AudioChannelConfiguration")
    if node is None:
        return None
    value = node.attrib.get("value")
    if not value:
        return None
    scheme = (node.attrib.get("schemeIdUri") or "").lower()
    if "dolby" in scheme:
        cleaned = value.strip().lower().removeprefix("0x")
        if cleaned and all(char in "0123456789abcdef" for char in cleaned):
            channels = int(cleaned, 16).bit_count()
            return str(channels) if channels else None
    return value


def _extension_from_mime(mime_type: str | None) -> str | None:
    if not mime_type or "/" not in mime_type:
        return None
    extension = mime_type.split("/", 1)[1].split(";", 1)[0]
    if extension == "mp4":
        return "m4s"
    if extension == "ttml+xml":
        return "ttml"
    return extension


def _extend_base(base_uri: str, element: ET.Element) -> str:
    base_node = _first_child(element, "BaseURL")
    if base_node is None or not (base_node.text or "").strip():
        return base_uri
    return join_dash_base_uri(base_uri, base_node.text.strip())


def _mime_prefix(value: str | None) -> str | None:
    if not value or "/" not in value:
        return None
    return value.split("/", 1)[0]


def _parse_range(value: str | None) -> tuple[int, int] | None:
    if not value or "-" not in value:
        return None
    try:
        start, end = value.split("-", 1)
        return int(start), int(end)
    except ValueError:
        return None


def _next_t(nodes: list[ET.Element], current_index: int) -> int | None:
    for node in nodes[current_index + 1 :]:
        if node.attrib.get("t") is not None:
            return int(node.attrib["t"])
    return None


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local(child.tag) == name]


def _first_child(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in list(element) if _local(child.tag) == name), None)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _attr(element: ET.Element, name: str) -> str | None:
    return element.attrib.get(name)


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None
