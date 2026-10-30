"""Dependency-free ISO-BMFF metadata probes and bounded box traversal."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator

_CONTAINERS = {
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"sinf", b"schi",
    b"moof", b"traf", b"mvex", b"edts", b"dinf", b"udta", b"wave",
}
_AUDIO_ENTRIES = {
    b"enca", b"mp4a", b"ac-3", b"ec-3", b"ac-4", b"Opus", b"fLaC",
    b"alac", b".mp3", b"mp3 ", b"sowt", b"twos", b"lpcm", b"ulaw",
    b"alaw", b"ipcm", b"fpcm", b"mlpa", b"dtsc", b"dtsh", b"dtsl", b"dtse",
}
_VIDEO_ENTRIES = {
    b"encv", b"avc1", b"avc2", b"avc3", b"avc4", b"hvc1", b"hev1",
    b"dvhe", b"dvh1", b"vvc1", b"vvi1", b"av01", b"vp08", b"vp09", b"mp4v",
}


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


def walk_boxes(
    data: bytes | bytearray,
    start: int = 0,
    end: int | None = None,
    *,
    _depth: int = 0,
) -> Iterator[tuple[int, int, bytes, int]]:
    """Visit structural boxes, never arbitrary bytes in media/codec payloads.

    Sample entries and full boxes have fixed fields before their children.
    Unknown boxes are leaves; malformed children cannot escape their parent.
    """
    if _depth >= 32:
        return
    for position, size, kind, header in _boxes(data, start, end):
        yield position, size, kind, header
        payload, stop = position + header, position + size
        if kind in _CONTAINERS:
            child = payload
        elif kind == b"stsd":
            child = payload + 8  # full-box flags and entry_count
        elif kind == b"meta":
            child = payload + 4
        elif kind in _VIDEO_ENTRIES:
            child = payload + 78
        elif kind in _AUDIO_ENTRIES:
            if payload + 28 > stop:
                continue
            version = int.from_bytes(data[payload + 8:payload + 10], "big")
            if version not in {0, 1, 2}:
                continue
            child = payload + {0: 28, 1: 44, 2: 64}[version]
        else:
            continue
        if child < stop:
            yield from walk_boxes(data, child, stop, _depth=_depth + 1)


def box_ranges(data: bytes | bytearray, box_type: bytes) -> list[tuple[int, int]]:
    """Find real box boundaries, including standalone initialization boxes."""
    return [(position, size) for position, size, kind, _header in walk_boxes(data) if kind == box_type]


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


def video_range_from_init(data: bytes | bytearray) -> str | None:
    """Read HDR transfer metadata from ISO-BMFF ``colr/nclx`` boxes."""
    ranges: list[str] = []
    raw = bytes(data)
    offset = 0
    while True:
        index = raw.find(b"colr", offset)
        if index < 4:
            break
        box_start = index - 4
        size = int.from_bytes(raw[box_start:index], "big")
        header_size = 8
        if size == 1 and index + 12 <= len(raw):
            size = int.from_bytes(raw[index + 4:index + 12], "big")
            header_size = 16
        end = box_start + size if size else len(raw)
        payload_start = box_start + header_size
        if size >= header_size and payload_start + 10 <= end <= len(raw):
            payload = raw[payload_start:end]
            if payload[:4] in {b"nclx", b"nclc"}:
                primaries = int.from_bytes(payload[4:6], "big")
                transfer = int.from_bytes(payload[6:8], "big")
                if transfer == 18:
                    ranges.append("HLG")
                elif transfer in {14, 15, 16} and primaries == 9:
                    ranges.append("HDR10")
        offset = index + 4
    if "HLG" in ranges:
        return "HLG"
    if "HDR10" in ranges:
        return "HDR10"
    return None


def video_signature_from_init(data: bytes | bytearray) -> tuple[str, int, int, bytes | None] | None:
    """Actual codec/dimensions/colour declaration of one visual sample entry."""
    entries = [item for item in walk_boxes(data) if item[2] in _VIDEO_ENTRIES]
    if len(entries) != 1:
        return None
    position, size, kind, header = entries[0]
    if size < header + 78:
        return None
    children = list(walk_boxes(data, position + header + 78, position + size))
    if kind == b"encv":
        formats = [bytes(data[p + h:p + h + 4]) for p, s, k, h in children if k == b"frma" and s >= h + 4]
        if len(formats) != 1:
            return None
        kind = formats[0]
    codecs = {b"avc1": "h264", b"avc3": "h264", b"hvc1": "h265", b"hev1": "h265", b"dvh1": "h265", b"dvhe": "h265"}
    codec = codecs.get(kind)
    width = int.from_bytes(data[position + header + 24:position + header + 26], "big")
    height = int.from_bytes(data[position + header + 26:position + header + 28], "big")
    if codec is None or not width or not height:
        return None
    colour = next((bytes(data[p + h + 4:p + h + 10]) for p, s, k, h in children
                   if k == b"colr" and s >= h + 10 and bytes(data[p + h:p + h + 4]) in {b"nclx", b"nclc"}), None)
    return codec, width, height, colour


def normalize_empty_c608_samples(data: bytearray) -> int:
    """Replace only all-zero clear c608 placeholders with same-size CC padding.

    Bounds come from real trak/tfhd/trun/mdat boxes, never payload searches.
    Ambiguous descriptions, encrypted entries and unsupported offsets are left
    alone. Valid captions and all audio/video sample bytes remain unchanged.
    """
    if sum(kind == b"moov" for _p, _s, kind, _h in _boxes(data)) != 1:
        return 0
    tracks: set[int] = set()
    for position, size, kind, header in walk_boxes(data):
        if kind != b"trak":
            continue
        fields = list(walk_boxes(data, position + header, position + size))
        tkhd = next((item for item in fields if item[2] == b"tkhd"), None)
        stsd = next((item for item in fields if item[2] == b"stsd"), None)
        if tkhd is None or stsd is None:
            continue
        p, s, _k, h = stsd
        if s < h + 8:
            continue
        entries = list(_boxes(data, p + h + 8, p + s))
        if int.from_bytes(data[p + h + 4:p + h + 8], "big") != 1 or len(entries) != 1 or entries[0][2] != b"c608":
            continue
        p, s, _k, h = tkhd
        if s < h + 4 or data[p + h] not in {0, 1}:
            continue
        offset = p + h + (20 if data[p + h] == 1 else 12)
        if offset + 4 <= p + s:
            tracks.add(int.from_bytes(data[offset:offset + 4], "big"))
    if not tracks:
        return 0
    mdats = [(p + h, p + s) for p, s, k, h in _boxes(data) if k == b"mdat"]
    mdat_starts = [start for start, _end in mdats]
    changed = 0
    for mp, ms, mk, mh in _boxes(data):
        if mk != b"moof":
            continue
        for tp, ts, tk, th in _boxes(data, mp + mh, mp + ms):
            if tk != b"traf":
                continue
            fields = list(_boxes(data, tp + th, tp + ts))
            tfhd = next((item for item in fields if item[2] == b"tfhd"), None)
            if tfhd is None or any(k in {b"senc", b"saiz", b"saio"} for _p, _s, k, _h in fields):
                continue
            p, s, _k, h = tfhd
            if s < h + 8 or data[p + h] != 0:
                continue
            flags = int.from_bytes(data[p + h + 1:p + h + 4], "big")
            track_id = int.from_bytes(data[p + h + 4:p + h + 8], "big")
            if track_id not in tracks or not flags & 0x020000 or flags & 1:
                continue  # only an explicit moof-relative base is unambiguous
            cursor = p + h + 8
            if flags & 2:
                if cursor + 4 > p + s or int.from_bytes(data[cursor:cursor + 4], "big") != 1:
                    continue
                cursor += 4
            cursor += 4 if flags & 8 else 0
            if cursor + (4 if flags & 16 else 0) + (4 if flags & 32 else 0) > p + s:
                continue
            default_size = int.from_bytes(data[cursor:cursor + 4], "big") if flags & 16 and cursor + 4 <= p + s else 0
            next_sample = None
            for p, s, k, h in fields:
                if k != b"trun" or s < h + 8 or data[p + h] not in {0, 1}:
                    continue
                flags = int.from_bytes(data[p + h + 1:p + h + 4], "big")
                count = int.from_bytes(data[p + h + 4:p + h + 8], "big")
                cursor = p + h + 8
                if flags & 1:
                    if cursor + 4 > p + s:
                        break
                    next_sample = mp + int.from_bytes(data[cursor:cursor + 4], "big", signed=True)
                    cursor += 4
                cursor += 4 if flags & 4 else 0
                stride = 4 * sum(bool(flags & flag) for flag in (0x100, 0x200, 0x400, 0x800))
                if next_sample is None or count > 1_000_000 or cursor + count * stride > p + s or (not flags & 0x200 and not default_size):
                    break
                for _ in range(count):
                    size_offset = cursor + (4 if flags & 0x100 else 0)
                    sample_size = int.from_bytes(data[size_offset:size_offset + 4], "big") if flags & 0x200 else default_size
                    stop = next_sample + sample_size
                    mdat_index = bisect_right(mdat_starts, next_sample) - 1
                    if 10 <= sample_size <= 65536 and sample_size % 2 == 0 and mdat_index >= 0 and stop <= mdats[mdat_index][1]:
                        if not any(data[next_sample:stop]):
                            data[next_sample:stop] = sample_size.to_bytes(4, "big") + b"cdat" + b"\x80\x80" * ((sample_size - 8) // 2)
                            changed += 1
                    next_sample = stop
                    cursor += stride
    return changed


__all__ = ["audio_channel_count_from_init", "video_range_from_init", "video_signature_from_init", "normalize_empty_c608_samples"]
