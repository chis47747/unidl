"""MP4Box discovery and lossless MV-HEVC container handling.

Only small ISO-BMFF headers are buffered. Media payloads are copied in bounded
chunks; source tracks/checkpoints are never edited.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .embedding import current_download_runtime
from .mp4_metadata import _boxes, walk_boxes

INSTALL_HINT = "Install GPAC/MP4Box and add MP4Box to PATH (macOS: brew install gpac)."
_MAX_HEADER = 16 * 1024 * 1024
_HEVC = {b"hvc1", b"hev1", b"dvh1", b"dvhe"}
_CONFIG = {b"hvcC", b"lhvC", b"vexu", b"dvcC", b"dvvC"}


def find_mp4box() -> str | None:
    return shutil.which("MP4Box") or shutil.which("mp4box")


def require_mp4box() -> str:
    executable = find_mp4box()
    if not executable:
        raise RuntimeError(
            "MP4Box not found; MP4Box muxing and lossless MV-HEVC 3D output require it. "
            + INSTALL_HINT
        )
    return executable


def _file_boxes(source):
    limit = os.fstat(source.fileno()).st_size
    position = 0
    while position + 8 <= limit:
        source.seek(position)
        header = source.read(16)
        size, kind = int.from_bytes(header[:4], "big"), header[4:8]
        if size == 1:
            size = int.from_bytes(header[8:16], "big")
            minimum = 16
        else:
            minimum = 8
            size = size or limit - position
        if size < minimum or position + size > limit:
            return
        yield position, size, kind
        position += size


def _header(path: Path, kind=b"moov") -> bytes:
    if not path.is_file():
        return b""
    with path.open("rb") as source:
        for position, size, found in _file_boxes(source):
            if found == kind:
                if size > _MAX_HEADER:
                    raise RuntimeError("MP4 header exceeds the bounded metadata limit.")
                source.seek(position)
                return source.read(size)
    return b""


def stereo_configurations(path: Path) -> set[tuple[bytes, ...]]:
    """Keep configs associated with their sample entry, not loose file-wide tags."""
    data = _header(Path(path))
    result = set()
    for position, size, kind, header in walk_boxes(data):
        if kind not in _HEVC:
            continue
        children = list(_boxes(data, position + header + 78, position + size))
        if not any(child[2] in {b"lhvC", b"vexu"} for child in children):
            continue
        result.add(tuple(sorted(
            bytes(data[pos:pos + length])
            for pos, length, box, _ in children if box in _CONFIG
        )))
    return result


def validate_stereo(inputs: list[Path], output: Path) -> None:
    expected = set().union(*(stereo_configurations(path) for path in inputs))
    if expected and not expected.issubset(stereo_configurations(output)):
        raise RuntimeError(
            "MP4Box did not preserve the MV-HEVC stereo/Dolby Vision configuration. "
            "Update GPAC/MP4Box; the source tracks have been kept. No 2D fallback was used."
        )


def _box(kind: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def _replace_boxes(data: bytes, replacements: dict[int, bytes], start=0, end=None) -> bytes:
    result = []
    for position, size, kind, header in _boxes(data, start, end):
        if position in replacements:
            result.append(replacements[position])
        elif kind in {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"moof", b"traf"}:
            result.append(_box(kind, _replace_boxes(data, replacements, position + header, position + size)))
        else:
            result.append(data[position:position + size])
    return b"".join(result)


def _tracks(data: bytes) -> dict:
    tracks = {}
    for position, size, kind, header in walk_boxes(data):
        if kind != b"trak":
            continue
        children = list(walk_boxes(data, position + header, position + size))
        tkhd = next((box for box in children if box[2] == b"tkhd"), None)
        mdhd = next((box for box in children if box[2] == b"mdhd"), None)
        stsd = next((box for box in children if box[2] == b"stsd"), None)
        if not tkhd or not mdhd or not stsd:
            raise RuntimeError("Incomplete MP4 section track metadata.")
        p = tkhd[0] + tkhd[3]
        track_id = int.from_bytes(data[p + (20 if data[p] else 12):p + (24 if data[p] else 16)], "big")
        p = mdhd[0] + mdhd[3]
        scale = int.from_bytes(data[p + (20 if data[p] else 12):p + (24 if data[p] else 16)], "big")
        p, length, _, h = stsd
        entries = [bytes(data[q:q + s]) for q, s, _, _ in _boxes(data, p + h + 8, p + length)]
        if not entries:
            raise RuntimeError("Empty MP4 section sample descriptions.")
        tracks[track_id] = {"scale": scale, "stsd": stsd, "entries": entries, "default": 1}
    for p, _size, kind, h in walk_boxes(data):
        if kind == b"trex":
            track = tracks.get(int.from_bytes(data[p + h + 4:p + h + 8], "big"))
            if track is not None:
                track["default"] = int.from_bytes(data[p + h + 8:p + h + 12], "big")
    return tracks


def _remap_fragment(data: bytes, tracks: dict, descriptions: dict[int, list[int]]) -> bytes:
    replacements = {}
    growth = 0
    for p, size, kind, h in walk_boxes(data):
        if kind != b"tfhd":
            continue
        payload = bytearray(data[p + h:p + size])
        flags = int.from_bytes(payload[:4], "big") & 0xFFFFFF
        track_id = int.from_bytes(payload[4:8], "big")
        if track_id not in descriptions or flags & 1 or not flags & 0x020000:
            raise RuntimeError("Unsupported absolute-offset MV-HEVC section; refusing a lossy concat.")
        old = int.from_bytes(payload[8:12], "big") if flags & 2 else tracks[track_id]["default"]
        if not 1 <= old <= len(descriptions[track_id]):
            raise RuntimeError("Invalid MP4 fragment sample-description index.")
        new = descriptions[track_id][old - 1].to_bytes(4, "big")
        if flags & 2:
            payload[8:12] = new
        else:
            payload[1:4] = (flags | 2).to_bytes(3, "big")
            payload[8:8] = new
            growth += 4
        replacements[p] = _box(kind, bytes(payload))
    if not replacements:
        raise RuntimeError("No MP4 fragment track headers found.")
    for p, size, kind, h in walk_boxes(data):
        if kind != b"trun":
            continue
        payload = bytearray(data[p + h:p + size])
        if not int.from_bytes(payload[:4], "big") & 1:
            raise RuntimeError("Unsupported implicit MP4 fragment offset; refusing a lossy concat.")
        offset = int.from_bytes(payload[8:12], "big", signed=True)
        payload[8:12] = (offset + growth).to_bytes(4, "big", signed=True)
        replacements[p] = _box(kind, bytes(payload))
    return _replace_boxes(data, replacements)


def join_stereo_sections(inputs: list[Path], output: Path) -> Path:
    """Join already-restamped fMP4 with all sample descriptions intact.

    MP4Box 2.x -cat may create extra tracks after the second description; the
    streaming importer (-newfs) drops unknown stereo boxes. Keep one init with
    a union of sample descriptions and remap each fragment explicitly instead.
    """
    headers = []
    header_bytes = 0
    for path in inputs:
        data = _header(path)
        header_bytes += len(data)
        if header_bytes > 64 * 1024 * 1024:
            raise RuntimeError("MP4 section metadata exceeds the bounded inventory limit.")
        headers.append(data)
    inventories = [_tracks(data) for data in headers]
    if not inventories or not inventories[0]:
        raise RuntimeError("Missing fragmented MP4 initialization data for 3D concat.")
    combined = {key: list(track["entries"]) for key, track in inventories[0].items()}
    mappings = []
    for tracks in inventories:
        mapping = {}
        for track_id, track in tracks.items():
            original = inventories[0].get(track_id)
            if original is None or original["scale"] != track["scale"]:
                raise RuntimeError("Incompatible MP4 section track IDs or timescales.")
            entries = combined[track_id]
            for entry in track["entries"]:
                if entry not in entries:
                    # Only this well-defined HEVC configuration switch belongs
                    # in the stereo path, never silently combine H.264/HEVC.
                    if entry[4:8] not in _HEVC or entries[0][4:8] not in _HEVC:
                        raise RuntimeError("Incompatible codecs in MV-HEVC sections.")
                    entries.append(entry)
            mapping[track_id] = [entries.index(entry) + 1 for entry in track["entries"]]
        mappings.append(mapping)
    replacements = {}
    for track_id, track in inventories[0].items():
        p, _size, _kind, h = track["stsd"]
        replacements[p] = _box(b"stsd", headers[0][p + h:p + h + 4]
                                + len(combined[track_id]).to_bytes(4, "big")
                                + b"".join(combined[track_id]))
    init = _replace_boxes(headers[0], replacements)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".unidl-stereo-", suffix=".mp4", dir=output.parent)
    temporary = Path(name)
    runtime = current_download_runtime()
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(_header(inputs[0], b"ftyp"))
            target.write(init)
            for path, tracks, mapping in zip(inputs, inventories, mappings, strict=True):
                fragments = 0
                with path.open("rb") as source:
                    for p, size, kind in _file_boxes(source):
                        if runtime is not None:
                            runtime.checkpoint()
                        if kind == b"moof":
                            if size > _MAX_HEADER:
                                raise RuntimeError("Oversized MP4 fragment header.")
                            source.seek(p)
                            target.write(_remap_fragment(source.read(size), tracks, mapping))
                            fragments += 1
                        elif kind not in {b"ftyp", b"moov", b"sidx", b"mfra", b"styp"}:
                            source.seek(p)
                            remaining = size
                            while remaining:
                                if runtime is not None:
                                    runtime.checkpoint()
                                chunk = source.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    raise RuntimeError("Truncated MP4 section payload.")
                                target.write(chunk)
                                remaining -= len(chunk)
                if not fragments:
                    raise RuntimeError("3D section is not fragmented MP4; cannot safely join it.")
        validate_stereo(inputs, temporary)
        temporary.replace(output)
        return output
    finally:
        temporary.unlink(missing_ok=True)
