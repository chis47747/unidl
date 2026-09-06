from __future__ import annotations

from pathlib import Path

from .models import StreamInfo

VGC_EXTRA_FLAG = "vgc"


class VgcError(RuntimeError):
    pass


def is_vgc_stream(stream: StreamInfo) -> bool:
    extra = getattr(stream, "extra", None)
    return isinstance(extra, dict) and bool(extra.get(VGC_EXTRA_FLAG))


def prepare_vgc_streams(streams: list[StreamInfo]) -> int:
    prepared = 0
    for stream in streams:
        if not _is_hls_like(stream):
            continue
        _mark_vgc_stream(stream)
        prepared += 1
    return prepared


def finalize_vgc_track(path: Path, stream: StreamInfo, *, allow_opaque: bool = False) -> Path:
    if allow_opaque or _is_text_stream(stream):
        return path
    if _looks_like_clear_media(path):
        return path
    raise VgcError(
        "VGC output is still protected/opaque and is not a clear MPEG-TS/MP4 track. "
        "Use --vgc-keep-opaque only when you intentionally want to keep the raw diagnostic payload."
    )


def _mark_vgc_stream(stream: StreamInfo) -> None:
    stream.extra.setdefault("vgc_original_encrypted", stream.encrypted)
    stream.extra.setdefault("vgc_original_encryption_scheme", stream.encryption_scheme)
    stream.extra[VGC_EXTRA_FLAG] = True
    stream.encrypted = True
    stream.encryption_scheme = "VGC"


def _is_hls_like(stream: StreamInfo) -> bool:
    manifest_type = (getattr(stream, "manifest_type", None) or "").lower()
    return manifest_type in {"hls", "m3u", "m3u8"}


def _is_text_stream(stream: StreamInfo) -> bool:
    media_type = (getattr(stream, "media_type", None) or "").lower()
    return media_type in {"subtitle", "subtitles", "text"}


def _looks_like_clear_media(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            data = handle.read(188 * 80)
    except OSError as exc:
        raise VgcError(f"VGC output could not be read: {exc}") from exc
    if not data:
        return False
    return _looks_like_mpeg_ts(data) or _looks_like_mp4(data) or _looks_like_adts(data) or _looks_like_id3_prefixed_audio(data)


def _looks_like_mpeg_ts(data: bytes) -> bool:
    if len(data) < 188 * 3:
        return False
    max_offset = min(188, len(data))
    for offset in range(max_offset):
        positions = range(offset, len(data), 188)
        checked = 0
        hits = 0
        for position in positions:
            checked += 1
            if data[position] == 0x47:
                hits += 1
            elif checked >= 3:
                break
            if checked >= 8:
                break
        if checked >= 3 and hits == checked:
            return True
    return False


def _looks_like_mp4(data: bytes) -> bool:
    if len(data) < 12:
        return False
    return data[4:8] == b"ftyp" or data.startswith(b"\x00\x00\x00") and b"ftyp" in data[:32]


def _looks_like_adts(data: bytes) -> bool:
    return len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xF0) == 0xF0


def _looks_like_id3_prefixed_audio(data: bytes) -> bool:
    payload = _skip_id3v2_tag(data)
    return payload is not data and (_looks_like_adts(payload) or _looks_like_mpeg_ts(payload))


def _skip_id3v2_tag(data: bytes) -> bytes:
    if len(data) < 10 or data[:3] != b"ID3":
        return data
    if any(byte & 0x80 for byte in data[6:10]):
        return data
    size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
    offset = 10 + size
    if data[5] & 0x10:
        offset += 10
    return data[offset:] if offset < len(data) else b""
