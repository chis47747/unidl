from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

EBML_ID = 0x1A45DFA3
SEGMENT_ID = 0x18538067
CLUSTER_ID = 0x1F43B675
SEEK_HEAD_ID = 0x114D9B74
INFO_ID = 0x1549A966
TRACKS_ID = 0x1654AE6B
TIMECODE_SCALE_ID = 0x2AD7B1
DURATION_ID = 0x4489
TIMECODE_ID = 0xE7
TRACK_ENTRY_ID = 0xAE
DEFAULT_DURATION_ID = 0x23E383
SIMPLE_BLOCK_ID = 0xA3
BLOCK_GROUP_ID = 0xA0
BLOCK_ID = 0xA1
CUES_ID = 0x1C53BB6B
COPY_CHUNK_SIZE = 1024 * 1024


class WebMLiveWriterError(RuntimeError):
    pass


class ContinuousWebMWriter:
    """Write a single WebM stream from repeated WebM init/media fragments."""

    def __init__(self, target: BinaryIO, *, segment_size_len: int = 8):
        if segment_size_len < 1 or segment_size_len > 8:
            raise ValueError("segment_size_len must be 1-8 bytes")
        self.target = target
        self.segment_size_len = segment_size_len
        self.started = False
        self.metadata_written = False
        self.cluster_count = 0
        self.timecode_scale = 1_000_000
        self.default_duration_ticks = 1.0
        self.next_cluster_timecode = 0.0

    def write_fragment(self, fragment: str | Path | BinaryIO, *, duration_seconds: float | None = None) -> int:
        close_source = False
        if hasattr(fragment, "read"):
            source = fragment
        else:
            source = Path(fragment).open("rb")
            close_source = True
        try:
            return self._write_fragment_from_stream(source, duration_seconds=duration_seconds)
        finally:
            if close_source:
                source.close()

    def _write_fragment_from_stream(self, source: BinaryIO, *, duration_seconds: float | None = None) -> int:
        start = source.tell()
        source.seek(0, 2)
        end = source.tell()
        source.seek(start)
        clusters = 0
        fragment_start_timecode = self.next_cluster_timecode
        timecodes = _FragmentTimecodeState()
        while source.tell() < end:
            element = _read_stream_element(source, end)
            if element is None:
                break
            if element.element_id == EBML_ID:
                if not self.started:
                    _copy_element(source, self.target, element)
                    self._start_segment()
                else:
                    source.seek(element.content_end)
                continue
            if element.element_id == SEGMENT_ID:
                clusters += self._write_segment_children(source, element.content_end, timecodes)
                continue
            if element.element_id == CLUSTER_ID:
                self._require_started()
                self.metadata_written = True
                if self._copy_cluster(source, element, timecodes):
                    clusters += 1
                continue
            if not self.started:
                raise WebMLiveWriterError("first WebM fragment must include EBML and Segment headers")
            source.seek(element.content_end)
        if clusters:
            explicit_ticks = _seconds_to_timecode_ticks(duration_seconds, self.timecode_scale)
            if explicit_ticks is not None:
                self.next_cluster_timecode = max(self.next_cluster_timecode, fragment_start_timecode + explicit_ticks)
        self.cluster_count += clusters
        return clusters

    def _write_segment_children(self, source: BinaryIO, end: int, timecodes: _FragmentTimecodeState) -> int:
        if not self.started:
            self._start_segment()
        clusters = 0
        copied_metadata = False
        while source.tell() < end:
            element = _read_stream_element(source, end)
            if element is None:
                break
            if element.element_id == CLUSTER_ID:
                self.metadata_written = True
                if self._copy_cluster(source, element, timecodes):
                    clusters += 1
                continue
            if not self.metadata_written and element.element_id in {SEEK_HEAD_ID, CUES_ID}:
                source.seek(element.content_end)
                continue
            if not self.metadata_written and clusters == 0:
                self._copy_metadata_element(source, element)
                copied_metadata = True
            else:
                source.seek(element.content_end)
        if copied_metadata:
            self.metadata_written = True
        return clusters

    def _start_segment(self) -> None:
        if self.started:
            return
        self.target.write(_element_id_bytes(SEGMENT_ID))
        self.target.write(_encode_unknown_size(self.segment_size_len))
        self.started = True

    def _require_started(self) -> None:
        if not self.started:
            raise WebMLiveWriterError("first WebM fragment must include EBML and Segment headers")

    def _copy_metadata_element(self, source: BinaryIO, element: _Element) -> None:
        content = _read_content(source, element)
        content = _sanitize_metadata_content(element.element_id, content)
        self._learn_metadata(element.element_id, content)
        self.target.write(_encode_element(element.id_bytes, content, preferred_size_len=len(element.size_bytes)))

    def _learn_metadata(self, element_id: int, content: bytes) -> None:
        if element_id == INFO_ID:
            scale = _first_uint_child(content, TIMECODE_SCALE_ID)
            if scale and scale > 0:
                self.timecode_scale = scale
        if element_id == TRACKS_ID:
            durations = _uint_children(content, DEFAULT_DURATION_ID)
            ticks = [
                max(1.0, duration / self.timecode_scale)
                for duration in durations
                if duration > 0 and self.timecode_scale > 0
            ]
            if ticks:
                self.default_duration_ticks = min(ticks)

    def _copy_cluster(self, source: BinaryIO, element: _Element, timecodes: _FragmentTimecodeState) -> bool:
        content = _read_content(source, element)
        original_timecode = _cluster_timecode(content)
        block_timecodes = _cluster_block_timecode_range(content)
        if block_timecodes is None:
            return False
        min_relative, max_relative = block_timecodes
        if timecodes.offset is None:
            timecodes.offset = self.next_cluster_timecode - ((original_timecode or 0) + min_relative)
        adjusted_timecode = max(0.0, (original_timecode or 0) + timecodes.offset)
        rewritten = _rewrite_cluster_timecode(content, _round_timecode_tick(adjusted_timecode))
        self.target.write(_encode_element(element.id_bytes, rewritten, preferred_size_len=len(element.size_bytes)))
        duration = max(1.0, self.default_duration_ticks)
        self.next_cluster_timecode = max(self.next_cluster_timecode, adjusted_timecode + max(0, max_relative) + duration)
        return True


def write_continuous_webm_fragments(
    fragments: Iterable[str | Path],
    output_path: str | Path,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f"{output.name}.tmp")
    with tmp.open("wb") as target:
        writer = ContinuousWebMWriter(target)
        for fragment in fragments:
            writer.write_fragment(fragment)
        if not writer.started:
            raise WebMLiveWriterError("no usable WebM headers found")
        if writer.cluster_count <= 0:
            raise WebMLiveWriterError("no WebM clusters found")
    tmp.replace(output)
    return output


class _Element:
    __slots__ = (
        "element_id",
        "id_bytes",
        "size",
        "size_bytes",
        "content_start",
        "content_end",
        "unknown_size",
    )

    def __init__(
        self,
        *,
        element_id: int,
        id_bytes: bytes,
        size: int,
        size_bytes: bytes,
        content_start: int,
        content_end: int,
        unknown_size: bool,
    ):
        self.element_id = element_id
        self.id_bytes = id_bytes
        self.size = size
        self.size_bytes = size_bytes
        self.content_start = content_start
        self.content_end = content_end
        self.unknown_size = unknown_size


class _FragmentTimecodeState:
    __slots__ = ("offset",)

    def __init__(self):
        self.offset: float | None = None


def _read_stream_element(source: BinaryIO, limit: int) -> _Element | None:
    position = source.tell()
    if position >= limit:
        return None
    first = source.read(1)
    if not first:
        return None
    id_length = _vint_length_from_first(first[0], max_len=4)
    if id_length is None or position + id_length > limit:
        source.seek(position)
        return None
    id_bytes = first + source.read(id_length - 1)
    if len(id_bytes) != id_length:
        source.seek(position)
        return None
    element_id = int.from_bytes(id_bytes, "big")

    size_position = source.tell()
    size_first = source.read(1)
    if not size_first:
        source.seek(position)
        return None
    size_length = _vint_length_from_first(size_first[0], max_len=8)
    if size_length is None or size_position + size_length > limit:
        source.seek(position)
        return None
    size_bytes = size_first + source.read(size_length - 1)
    if len(size_bytes) != size_length:
        source.seek(position)
        return None
    size, unknown_size = _parse_size_bytes(size_bytes)
    content_start = source.tell()
    declared_end = limit if unknown_size else content_start + size
    content_end = min(limit, declared_end)
    if content_start > limit:
        source.seek(position)
        return None
    return _Element(
        element_id=element_id,
        id_bytes=id_bytes,
        size=size,
        size_bytes=size_bytes,
        content_start=content_start,
        content_end=content_end,
        unknown_size=unknown_size,
    )


def _read_content(source: BinaryIO, element: _Element) -> bytes:
    length = max(0, element.content_end - element.content_start)
    data = source.read(length)
    if len(data) != length:
        raise WebMLiveWriterError("truncated WebM element while reading")
    return data


def _copy_element(source: BinaryIO, target: BinaryIO, element: _Element) -> None:
    target.write(element.id_bytes)
    target.write(element.size_bytes)
    _copy_limited(source, target, element.content_end - element.content_start)


def _copy_element_with_known_size(source: BinaryIO, target: BinaryIO, element: _Element) -> None:
    length = max(0, element.content_end - element.content_start)
    target.write(element.id_bytes)
    target.write(_encode_size(length, preferred_len=len(element.size_bytes)))
    _copy_limited(source, target, length)


def _copy_limited(source: BinaryIO, target: BinaryIO, length: int) -> None:
    remaining = max(0, length)
    while remaining:
        chunk = source.read(min(COPY_CHUNK_SIZE, remaining))
        if not chunk:
            raise WebMLiveWriterError("truncated WebM element while copying")
        target.write(chunk)
        remaining -= len(chunk)


def _read_element(data: bytes, position: int, limit: int) -> _Element | None:
    if position >= limit:
        return None
    id_length = _vint_length(data, position, max_len=4)
    if id_length is None or position + id_length > limit:
        return None
    element_id = int.from_bytes(data[position:position + id_length], "big")
    size_position = position + id_length
    size_length = _vint_length(data, size_position, max_len=8)
    if size_length is None or size_position + size_length > limit:
        return None
    size, unknown_size = _parse_size_bytes(data[size_position:size_position + size_length])
    content_start = size_position + size_length
    content_end = limit if unknown_size else min(limit, content_start + size)
    if content_start > limit:
        return None
    return _Element(
        element_id=element_id,
        id_bytes=data[position:position + id_length],
        size=size,
        size_bytes=data[size_position:size_position + size_length],
        content_start=content_start,
        content_end=content_end,
        unknown_size=unknown_size,
    )


def _first_uint_child(data: bytes, element_id: int) -> int | None:
    values = _uint_children(data, element_id, first_only=True)
    return values[0] if values else None


def _uint_children(data: bytes, element_id: int, *, first_only: bool = False) -> list[int]:
    result: list[int] = []

    def scan(start: int, end: int) -> None:
        position = start
        while position < end:
            child = _read_element(data, position, end)
            if child is None:
                return
            if child.element_id == element_id:
                result.append(_parse_uint(data[child.content_start:child.content_end]))
                if first_only:
                    return
            elif child.element_id in {INFO_ID, TRACKS_ID, TRACK_ENTRY_ID}:
                scan(child.content_start, child.content_end)
                if first_only and result:
                    return
            position = child.content_end

    scan(0, len(data))
    return result


def _sanitize_metadata_content(element_id: int, content: bytes) -> bytes:
    if element_id != INFO_ID:
        return content
    return _drop_children(content, {DURATION_ID})


def _drop_children(data: bytes, drop_ids: set[int]) -> bytes:
    output = bytearray()
    position = 0
    while position < len(data):
        child = _read_element(data, position, len(data))
        if child is None:
            output.extend(data[position:])
            break
        if child.element_id not in drop_ids:
            output.extend(data[position:child.content_end])
        position = child.content_end
    return bytes(output)


def _cluster_timecode(content: bytes) -> int | None:
    position = 0
    while position < len(content):
        child = _read_element(content, position, len(content))
        if child is None:
            return None
        if child.element_id == TIMECODE_ID:
            return _parse_uint(content[child.content_start:child.content_end])
        position = child.content_end
    return None


def _rewrite_cluster_timecode(content: bytes, timecode: int) -> bytes:
    output = bytearray()
    position = 0
    replaced = False
    while position < len(content):
        child = _read_element(content, position, len(content))
        if child is None:
            output.extend(content[position:])
            break
        if child.element_id == TIMECODE_ID and not replaced:
            payload = _encode_uint(timecode, preferred_len=max(1, child.content_end - child.content_start))
            output.extend(_encode_element(child.id_bytes, payload, preferred_size_len=len(child.size_bytes)))
            replaced = True
        else:
            output.extend(content[position:child.content_end])
        position = child.content_end
    if not replaced:
        output[0:0] = _encode_element(_element_id_bytes(TIMECODE_ID), _encode_uint(timecode))
    return bytes(output)


def _cluster_block_timecode_range(content: bytes) -> tuple[int, int] | None:
    values: list[int] = []

    def scan(start: int, end: int) -> None:
        position = start
        while position < end:
            child = _read_element(content, position, end)
            if child is None:
                return
            if child.element_id in {SIMPLE_BLOCK_ID, BLOCK_ID}:
                relative = _block_relative_timecode(content[child.content_start:child.content_end])
                if relative is not None:
                    values.append(relative)
            elif child.element_id == BLOCK_GROUP_ID:
                scan(child.content_start, child.content_end)
            position = child.content_end

    scan(0, len(content))
    return (min(values), max(values)) if values else None


def _seconds_to_timecode_ticks(seconds: float | int | None, timecode_scale: int) -> int | None:
    if seconds is None or timecode_scale <= 0:
        return None
    try:
        parsed = float(seconds)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return max(1, round(parsed * 1_000_000_000 / timecode_scale))


def _round_timecode_tick(value: float | int) -> int:
    return int(max(0, float(value)) + 0.5)


def _block_relative_timecode(value: bytes) -> int | None:
    track_length = _vint_length(value, 0, max_len=8)
    if track_length is None or len(value) < track_length + 3:
        return None
    return int.from_bytes(value[track_length:track_length + 2], "big", signed=True)


def _parse_uint(data: bytes) -> int:
    value = 0
    for byte in data:
        value = (value << 8) | byte
    return value


def _encode_uint(value: int, preferred_len: int | None = None) -> bytes:
    if value < 0:
        raise ValueError("EBML unsigned integer cannot be negative")
    lengths = [preferred_len] if preferred_len else []
    lengths.extend(length for length in range(1, 9) if length != preferred_len)
    for length in lengths:
        if length and value < (1 << (length * 8)):
            return value.to_bytes(length, "big")
    raise ValueError("EBML unsigned integer is too large")


def _encode_element(id_bytes: bytes, content: bytes, preferred_size_len: int | None = None) -> bytes:
    return id_bytes + _encode_size(len(content), preferred_size_len) + content


def _vint_length_from_first(first: int, max_len: int) -> int | None:
    mask = 0x80
    for length in range(1, max_len + 1):
        if first & mask:
            return length
        mask >>= 1
    return None


def _vint_length(data: bytes, position: int, max_len: int) -> int | None:
    if position >= len(data):
        return None
    return _vint_length_from_first(data[position], max_len=max_len)


def _parse_size_bytes(size_bytes: bytes) -> tuple[int, bool]:
    length = len(size_bytes)
    marker = 1 << (8 - length)
    value = size_bytes[0] & (marker - 1)
    for byte in size_bytes[1:]:
        value = (value << 8) | byte
    unknown = value == (1 << (7 * length)) - 1
    return value, unknown


def _encode_size(value: int, preferred_len: int | None = None) -> bytes:
    lengths = [preferred_len] if preferred_len else []
    lengths.extend(length for length in range(1, 9) if length != preferred_len)
    for length in lengths:
        if not length:
            continue
        max_value = (1 << (7 * length)) - 2
        if value <= max_value:
            raw = value.to_bytes(length, "big")
            marker = 1 << (8 - length)
            return bytes([raw[0] | marker]) + raw[1:]
    raise ValueError("EBML size is too large")


def _encode_unknown_size(length: int) -> bytes:
    marker = 1 << (8 - length)
    first = marker | (marker - 1)
    return bytes([first]) + (b"\xff" * (length - 1))


def _element_id_bytes(element_id: int) -> bytes:
    length = max(1, (element_id.bit_length() + 7) // 8)
    return element_id.to_bytes(length, "big")
