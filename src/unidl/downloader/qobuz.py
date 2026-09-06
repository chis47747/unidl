"""Qobuz DASH frame metadata and AES-CTR decryption.

Qobuz's native player receives ordinary fragmented MP4 segments with a UUID
metadata box after the 24-byte MP4 header.  The box lists each audio frame and
the eight-byte counter used when that frame is encrypted.  This is a transport
cipher, not CENC or a licence-based DRM system.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from Crypto.Cipher import AES


class QobuzSegmentError(ValueError):
    """A Qobuz media segment does not match the native frame contract."""


@dataclass(frozen=True, slots=True)
class QobuzFrame:
    index: int
    offset: int
    length: int
    samples: int
    encrypted: bool
    vector: bytes


@dataclass(frozen=True, slots=True)
class QobuzSegmentMetadata:
    size: int
    box_type: str
    uuid: bytes
    vector_size: int
    frames_offset: int
    frames: tuple[QobuzFrame, ...]


_MP4_PREFIX_LENGTH = 24
_METADATA_FIXED_LENGTH = 36
_FRAME_RECORD_LENGTH = 16


def parse_qobuz_segment_metadata(data: bytes) -> QobuzSegmentMetadata:
    """Read the metadata box exactly as the Qobuz native player does."""
    if len(data) < _MP4_PREFIX_LENGTH + _METADATA_FIXED_LENGTH:
        raise QobuzSegmentError("Qobuz segment is too short to contain frame metadata")

    cursor = _MP4_PREFIX_LENGTH
    size = int.from_bytes(data[cursor : cursor + 4], "big")
    if size < _METADATA_FIXED_LENGTH:
        raise QobuzSegmentError(f"Qobuz frame metadata has an invalid size: {size}")
    metadata_end = _MP4_PREFIX_LENGTH + size
    if metadata_end > len(data):
        raise QobuzSegmentError("Qobuz frame metadata extends beyond the segment")

    box_type_bytes = data[cursor + 4 : cursor + 8]
    try:
        box_type = box_type_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise QobuzSegmentError("Qobuz frame metadata has an invalid box type") from exc
    if box_type != "uuid":
        raise QobuzSegmentError(f"Qobuz frame metadata expected a uuid box, got {box_type!r}")

    uuid = data[cursor + 8 : cursor + 24]
    frames_offset = int.from_bytes(data[cursor + 28 : cursor + 32], "big") + _MP4_PREFIX_LENGTH
    vector_size = data[cursor + 32]
    frames_count = int.from_bytes(data[cursor + 33 : cursor + 36], "big")
    expected_size = _METADATA_FIXED_LENGTH + frames_count * _FRAME_RECORD_LENGTH
    if size < expected_size:
        raise QobuzSegmentError("Qobuz frame metadata ended before all frame records")
    if vector_size != 8:
        raise QobuzSegmentError(f"Unsupported Qobuz frame vector size: {vector_size}")
    if frames_offset < metadata_end:
        raise QobuzSegmentError("Qobuz audio frames overlap the metadata box")

    frames: list[QobuzFrame] = []
    frame_cursor = cursor + _METADATA_FIXED_LENGTH
    offset = frames_offset
    for index in range(1, frames_count + 1):
        length = int.from_bytes(data[frame_cursor : frame_cursor + 4], "big")
        samples = int.from_bytes(data[frame_cursor + 4 : frame_cursor + 6], "big")
        encrypted = int.from_bytes(data[frame_cursor + 6 : frame_cursor + 8], "big") != 0
        vector = data[frame_cursor + 8 : frame_cursor + 16]
        if length < 0 or offset + length > len(data):
            raise QobuzSegmentError(f"Qobuz frame {index} extends beyond the segment")
        frames.append(QobuzFrame(index, offset, length, samples, encrypted, vector))
        offset += length
        frame_cursor += _FRAME_RECORD_LENGTH

    return QobuzSegmentMetadata(size, box_type, uuid, vector_size, frames_offset, tuple(frames))


def decrypt_qobuz_segment(data: bytes, secret_key: bytes | str) -> bytes:
    """Decrypt encrypted audio frames while preserving the MP4 metadata boxes."""
    key = _secret_key(secret_key)
    metadata = parse_qobuz_segment_metadata(data)
    output = bytearray(data)
    for frame in metadata.frames:
        if not frame.encrypted or not frame.length:
            continue
        vector = frame.vector.ljust(AES.block_size, b"\0")
        cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=int.from_bytes(vector, "big"))
        end = frame.offset + frame.length
        output[frame.offset:end] = cipher.decrypt(data[frame.offset:end])
    return bytes(output)


def decrypt_qobuz_file(path: str | Path, secret_key: bytes | str) -> Path:
    """Atomically replace one encrypted Qobuz segment or assembled stream."""
    source = Path(path)
    pending = source.with_name(f".{source.name}.qobuz.tmp")
    try:
        pending.write_bytes(decrypt_qobuz_segment(source.read_bytes(), secret_key))
        pending.replace(source)
    finally:
        pending.unlink(missing_ok=True)
    return source


def _secret_key(value: bytes | str) -> bytes:
    if isinstance(value, bytes):
        key = value
    else:
        try:
            key = bytes.fromhex(str(value or "").strip())
        except ValueError as exc:
            raise QobuzSegmentError("Qobuz frame key is not valid hexadecimal") from exc
    if len(key) not in {16, 24, 32}:
        raise QobuzSegmentError(f"Qobuz frame key has an invalid AES length: {len(key)}")
    return key


__all__ = [
    "QobuzFrame",
    "QobuzSegmentError",
    "QobuzSegmentMetadata",
    "decrypt_qobuz_file",
    "decrypt_qobuz_segment",
    "parse_qobuz_segment_metadata",
]
