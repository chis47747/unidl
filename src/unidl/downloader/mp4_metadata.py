"""Small, dependency-free probes for fragmented MP4 initialization metadata."""

from __future__ import annotations

from collections.abc import Iterator


def _boxes(data: bytes | bytearray, start: int = 0, end: int | None = None) -> Iterator[tuple[int, int, bytes, int]]:
    """Yield ``(position, size, type, header_size)`` for valid MP4 boxes."""
    limit = len(data) if end is None else min(len(data), end)
    position = max(0, start)
    while position + 8 <= limit:
        size = int.from_bytes(data[position : position + 4], "big")
        box_type = bytes(data[position + 4 : position + 8])
        header_size = 8
        if size == 1:
            if position + 16 > limit:
                return
            size = int.from_bytes(data[position + 8 : position + 16], "big")
            header_size = 16
        elif size == 0:
            size = limit - position
        if size < header_size or position + size > limit:
            return
        yield position, size, box_type, header_size
        position += size


def _children(
    data: bytes | bytearray,
    parent: tuple[int, int, bytes, int],
    *,
    full_box: bool = False,
) -> list[tuple[int, int, bytes, int]]:
    position, size, _box_type, header_size = parent
    start = position + header_size + (8 if full_box else 0)
    return list(_boxes(data, start, position + size))


def audio_channel_count_from_init(data: bytes | bytearray) -> int | None:
    """Read the channel count from an ISO-BMFF audio initialization segment.

    DASH ``AudioChannelConfiguration`` metadata is sometimes wrong while the
    ``mp4a`` sample entry is correct. The sample entry's channel count is the
    value that describes the bytes which will actually be decoded. ``None`` is
    returned for a malformed or unsupported init segment so callers can retain
    the manifest declaration.
    """
    for moov in _boxes(data):
        if moov[2] != b"moov":
            continue
        for trak in _children(data, moov):
            if trak[2] != b"trak":
                continue
            mdia = next((child for child in _children(data, trak) if child[2] == b"mdia"), None)
            if mdia is None:
                continue
            handler = next((child for child in _children(data, mdia) if child[2] == b"hdlr"), None)
            if handler is None:
                continue
            handler_payload = handler[0] + handler[3]
            if handler_payload + 12 > len(data) or bytes(data[handler_payload + 8 : handler_payload + 12]) != b"soun":
                continue
            minf = next((child for child in _children(data, mdia) if child[2] == b"minf"), None)
            if minf is None:
                continue
            stbl = next((child for child in _children(data, minf) if child[2] == b"stbl"), None)
            if stbl is None:
                continue
            stsd_box = next((child for child in _children(data, stbl) if child[2] == b"stsd"), None)
            if stsd_box is None:
                continue
            sample_entry = next(
                (child for child in _children(data, stsd_box, full_box=True) if child[2] in {b"mp4a", b"enca"}),
                None,
            )
            if sample_entry is None:
                continue
            # Sample-entry fields: 6 reserved bytes, data-reference index,
            # 8 reserved bytes, then the 16-bit channel count.
            channel_offset = sample_entry[0] + sample_entry[3] + 16
            if channel_offset + 2 > sample_entry[0] + sample_entry[1]:
                continue
            count = int.from_bytes(data[channel_offset : channel_offset + 2], "big")
            if count > 0:
                return count
    return None


__all__ = ["audio_channel_count_from_init"]
