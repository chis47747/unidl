from __future__ import annotations

import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

from .embedding import managed_run

CONTENT_ENCODINGS_ID = 0x6D80
CONTENT_ENCODING_ID = 0x6240
CONTENT_ENCRYPTION_ID = 0x5035
CONTENT_ENC_AES_SETTINGS_ID = 0x47E7
CONTENT_ENC_KEY_ID = 0x47E2
SEGMENT_ID = 0x18538067
CLUSTER_ID = 0x1F43B675
SIMPLE_BLOCK_ID = 0xA3
BLOCK_ID = 0xA1

MASTER_IDS = {
    0x1A45DFA3,  # EBML
    SEGMENT_ID,
    0x1549A966,  # Info
    0x1654AE6B,  # Tracks
    0xAE,  # TrackEntry
    0xE0,  # Video
    0xE1,  # Audio
    CLUSTER_ID,  # Cluster
    0xA0,  # BlockGroup
}

KEY_SCAN_MASTER_IDS = MASTER_IDS | {
    CONTENT_ENCODINGS_ID,
    CONTENT_ENCODING_ID,
    CONTENT_ENCRYPTION_ID,
    CONTENT_ENC_AES_SETTINGS_ID,
}

STREAMING_MASTER_IDS = {SEGMENT_ID, CLUSTER_ID}
COPY_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Element:
    element_id: int
    id_bytes: bytes
    size: int
    size_bytes: bytes
    header_size: int
    content_start: int
    content_end: int
    unknown_size: bool
    truncated: bool = False


def decrypt_webm_parts(
    part_paths: Sequence[str | Path],
    key_hex: str,
    output_path: str | Path,
    *,
    validate: bool = True,
) -> Path:
    parts = [Path(path) for path in part_paths]
    if not parts:
        raise ValueError("No downloaded WebM parts available for decryption.")
    key = bytes.fromhex(key_hex)
    if len(key) != 16:
        raise ValueError("WebM decryption key must be 16 bytes / 32 hex characters.")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f"{output.name}.tmp")
    with tmp.open("wb") as target:
        for index, part in enumerate(parts):
            with part.open("rb") as source:
                _rewrite_webm_stream(source, target, key, strip_track_encryption=index == 0)
    tmp.replace(output)
    if validate:
        _validate_webm_output(output)
    return output


def decrypt_webm_file(input_path: str | Path, key_hex: str, output_path: str | Path, *, validate: bool = True) -> Path:
    input_file = Path(input_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    key = bytes.fromhex(key_hex)
    if len(key) != 16:
        raise ValueError("WebM decryption key must be 16 bytes / 32 hex characters.")
    tmp = output.with_name(f"{output.name}.tmp")
    with input_file.open("rb") as source, tmp.open("wb") as target:
        _rewrite_webm_stream(source, target, key, strip_track_encryption=True)
    tmp.replace(output)
    if validate:
        _validate_webm_output(output)
    return output


def select_webm_key(keys: Iterable[object], expected_kids: Iterable[str | None] | None = None) -> str:
    normalized_expected = {
        str(kid).lower().replace("-", "")
        for kid in (expected_kids or [])
        if kid
    }
    fallback: str | None = None
    for raw_key in keys:
        key = str(getattr(raw_key, "key", "")).lower()
        kid = getattr(raw_key, "kid", None)
        kid_text = str(kid).lower().replace("-", "") if kid else None
        if not fallback:
            fallback = key
        if normalized_expected and kid_text in normalized_expected:
            return key
    if normalized_expected:
        raise ValueError(f"no matching decryption key for KID {', '.join(sorted(normalized_expected))}")
    if fallback:
        return fallback
    raise ValueError("Encrypted WebM stream needs at least one --key KID:KEY or --key KEY.")


def webm_key_ids(input_path: str | Path, max_bytes: int = 2 * 1024 * 1024) -> list[str]:
    with Path(input_path).open("rb") as source:
        data = source.read(max(1, max_bytes))
    return webm_key_ids_from_bytes(data)


def webm_key_ids_from_bytes(data: bytes) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    _collect_webm_key_ids(data, 0, len(data), result, seen)
    return result


def _collect_webm_key_ids(data: bytes, start: int, end: int, result: list[str], seen: set[str]) -> None:
    pos = start
    while pos < end:
        element = _read_element(data, pos, end)
        if element is None:
            return
        if element.element_id == CONTENT_ENC_KEY_ID:
            key_id = data[element.content_start:element.content_end].hex()
            if key_id and key_id not in seen:
                seen.add(key_id)
                result.append(key_id)
        elif element.element_id in KEY_SCAN_MASTER_IDS:
            _collect_webm_key_ids(data, element.content_start, element.content_end, result, seen)
        pos = element.content_end


def _rewrite_webm(data: bytes, key: bytes, strip_track_encryption: bool) -> bytes:
    return _rewrite_children(data, 0, len(data), key, strip_track_encryption)


def _rewrite_webm_stream(source: BinaryIO, target: BinaryIO, key: bytes, strip_track_encryption: bool) -> None:
    start = source.tell()
    source.seek(0, 2)
    end = source.tell()
    source.seek(start)
    _rewrite_children_stream(source, target, end, key, strip_track_encryption)


def _rewrite_children_stream(source: BinaryIO, target: BinaryIO, end: int, key: bytes, strip_track_encryption: bool) -> None:
    while source.tell() < end:
        element = _read_stream_element(source, end)
        if element is None:
            _copy_remaining(source, target, end)
            return
        if strip_track_encryption and element.element_id == CONTENT_ENCODINGS_ID:
            source.seek(element.content_end)
            continue
        if element.truncated and element.element_id not in MASTER_IDS:
            source.seek(element.content_start)
            return
        if element.element_id in {SIMPLE_BLOCK_ID, BLOCK_ID}:
            raw_content = _read_stream_content(source, element)
            content = _decrypt_block_value(raw_content, key)
            target.write(_encode_element(element.id_bytes, content, preferred_size_len=len(element.size_bytes)))
            continue
        if element.element_id in STREAMING_MASTER_IDS:
            target.write(element.id_bytes)
            target.write(_encode_unknown_size(len(element.size_bytes)))
            _rewrite_children_stream(source, target, element.content_end, key, strip_track_encryption)
            continue
        if element.element_id in MASTER_IDS:
            raw_content = _read_stream_content(source, element)
            content = _rewrite_children(raw_content, 0, len(raw_content), key, strip_track_encryption)
            target.write(_encode_element(element.id_bytes, content, preferred_size_len=len(element.size_bytes)))
            continue
        target.write(element.id_bytes)
        target.write(element.size_bytes)
        _copy_limited(source, target, element.content_end - element.content_start)


def _read_stream_content(source: BinaryIO, element: _Element) -> bytes:
    length = max(0, element.content_end - element.content_start)
    data = source.read(length)
    if len(data) != length:
        raise RuntimeError("truncated WebM element while decrypting")
    return data


def _copy_limited(source: BinaryIO, target: BinaryIO, length: int) -> None:
    remaining = max(0, length)
    while remaining:
        chunk = source.read(min(COPY_CHUNK_SIZE, remaining))
        if not chunk:
            raise RuntimeError("truncated WebM element while copying")
        target.write(chunk)
        remaining -= len(chunk)


def _copy_remaining(source: BinaryIO, target: BinaryIO, end: int) -> None:
    remaining = max(0, end - source.tell())
    if remaining:
        _copy_limited(source, target, remaining)


def _read_stream_element(source: BinaryIO, limit: int) -> _Element | None:
    pos = source.tell()
    if pos >= limit:
        return None
    first = source.read(1)
    if not first:
        return None
    id_length = _vint_length_from_first(first[0], max_len=4)
    if id_length is None or pos + id_length > limit:
        source.seek(pos)
        return None
    id_bytes = first + source.read(id_length - 1)
    if len(id_bytes) != id_length:
        source.seek(pos)
        return None
    element_id = int.from_bytes(id_bytes, "big")

    size_pos = source.tell()
    size_first = source.read(1)
    if not size_first:
        source.seek(pos)
        return None
    size_length = _vint_length_from_first(size_first[0], max_len=8)
    if size_length is None or size_pos + size_length > limit:
        source.seek(pos)
        return None
    size_bytes = size_first + source.read(size_length - 1)
    if len(size_bytes) != size_length:
        source.seek(pos)
        return None
    size, unknown_size = _parse_size_bytes(size_bytes)
    content_start = source.tell()
    declared_end = limit if unknown_size else content_start + size
    content_end = min(limit, declared_end)
    if content_start > limit:
        source.seek(pos)
        return None
    return _Element(
        element_id=element_id,
        id_bytes=id_bytes,
        size=size,
        size_bytes=size_bytes,
        header_size=id_length + size_length,
        content_start=content_start,
        content_end=content_end,
        unknown_size=unknown_size,
        truncated=not unknown_size and declared_end > limit,
    )


def _vint_length_from_first(first: int, max_len: int) -> int | None:
    mask = 0x80
    for length in range(1, max_len + 1):
        if first & mask:
            return length
        mask >>= 1
    return None


def _parse_size_bytes(size_bytes: bytes) -> tuple[int, bool]:
    length = len(size_bytes)
    if not length:
        raise ValueError("invalid EBML element size")
    marker = 1 << (8 - length)
    first_value = size_bytes[0] & (marker - 1)
    value = first_value
    for byte in size_bytes[1:]:
        value = (value << 8) | byte
    unknown = value == (1 << (7 * length)) - 1
    return value, unknown


def _rewrite_children(data: bytes, start: int, end: int, key: bytes, strip_track_encryption: bool) -> bytes:
    output = bytearray()
    pos = start
    while pos < end:
        element = _read_element(data, pos, end)
        if element is None:
            output.extend(data[pos:end])
            break
        if strip_track_encryption and element.element_id == CONTENT_ENCODINGS_ID:
            pos = element.content_end
            continue
        if element.truncated and element.element_id not in MASTER_IDS:
            break
        raw_content = data[element.content_start:element.content_end]
        if element.element_id in {SIMPLE_BLOCK_ID, BLOCK_ID}:
            content = _decrypt_block_value(raw_content, key)
            output.extend(_encode_element(element.id_bytes, content, preferred_size_len=len(element.size_bytes)))
        elif element.element_id in MASTER_IDS:
            content = _rewrite_children(data, element.content_start, element.content_end, key, strip_track_encryption)
            if element.element_id == SEGMENT_ID and element.unknown_size:
                output.extend(element.id_bytes)
                output.extend(element.size_bytes)
                output.extend(content)
            else:
                output.extend(_encode_element(element.id_bytes, content, preferred_size_len=len(element.size_bytes)))
        else:
            output.extend(data[pos:element.content_end])
        pos = element.content_end
    return bytes(output)


def _decrypt_block_value(value: bytes, key: bytes) -> bytes:
    header_length = _block_header_length(value)
    if header_length is None or header_length >= len(value):
        return value
    header = value[:header_length]
    payload = value[header_length:]
    signal = payload[0]
    # WebM encrypted tracks prepend a signal byte to every frame. Once the
    # Track ContentEncodings element is removed, that byte must be removed too.
    if signal == 0:
        return header + payload[1:]
    if not (signal & 0x01):
        return value
    if signal & 0xF8:
        return value
    if len(payload) < 9:
        return value

    iv = payload[1:9]
    cursor = 9
    if signal & 0x02:
        if len(payload) < cursor + 1:
            return value
        partition_count = payload[cursor]
        cursor += 1
        offset_bytes = partition_count * 4
        if len(payload) < cursor + offset_bytes:
            return value
        offsets = [
            int.from_bytes(payload[cursor + index * 4: cursor + (index + 1) * 4], "big")
            for index in range(partition_count)
        ]
        cursor += offset_bytes
        sample = payload[cursor:]
        decrypted_sample = _decrypt_partitioned_sample(sample, offsets, key, iv)
        return header + decrypted_sample

    sample = payload[cursor:]
    return header + _aes_ctr_crypt(key, iv, sample)


def _decrypt_partitioned_sample(sample: bytes, offsets: list[int], key: bytes, iv: bytes) -> bytes:
    if any(offset < 0 or offset > len(sample) for offset in offsets):
        return sample
    boundaries = [0, *offsets, len(sample)]
    output = bytearray()
    encrypted_chunks: list[tuple[int, int]] = []
    for index in range(len(boundaries) - 1):
        start = boundaries[index]
        end = boundaries[index + 1]
        if end < start:
            return sample
        chunk = sample[start:end]
        if index % 2 == 0:
            output.extend(chunk)
        else:
            encrypted_chunks.append((len(output), len(chunk)))
            output.extend(chunk)
    if not encrypted_chunks:
        return bytes(output)

    encrypted_data = b"".join(bytes(output[start:start + length]) for start, length in encrypted_chunks)
    clear_data = _aes_ctr_crypt(key, iv, encrypted_data)
    cursor = 0
    for start, length in encrypted_chunks:
        output[start:start + length] = clear_data[cursor:cursor + length]
        cursor += length
    return bytes(output)


def _block_header_length(value: bytes) -> int | None:
    track_length = _vint_length(value, 0, max_len=8)
    if track_length is None:
        return None
    header_length = track_length + 3
    return header_length if len(value) >= header_length else None


def _read_element(data: bytes, pos: int, limit: int) -> _Element | None:
    try:
        element_id, id_length = _read_element_id(data, pos, limit)
        size, size_length, unknown_size = _read_element_size(data, pos + id_length, limit)
    except ValueError:
        return None
    content_start = pos + id_length + size_length
    declared_end = limit if unknown_size else content_start + size
    content_end = min(limit, declared_end)
    if content_start > limit:
        return None
    return _Element(
        element_id=element_id,
        id_bytes=data[pos:pos + id_length],
        size=size,
        size_bytes=data[pos + id_length:pos + id_length + size_length],
        header_size=id_length + size_length,
        content_start=content_start,
        content_end=content_end,
        unknown_size=unknown_size,
        truncated=not unknown_size and declared_end > limit,
    )


def _read_element_id(data: bytes, pos: int, limit: int) -> tuple[int, int]:
    length = _vint_length(data, pos, max_len=4)
    if length is None or pos + length > limit:
        raise ValueError("invalid EBML element id")
    return int.from_bytes(data[pos:pos + length], "big"), length


def _read_element_size(data: bytes, pos: int, limit: int) -> tuple[int, int, bool]:
    length = _vint_length(data, pos, max_len=8)
    if length is None or pos + length > limit:
        raise ValueError("invalid EBML element size")
    marker = 1 << (8 - length)
    first_value = data[pos] & (marker - 1)
    value = first_value
    for byte in data[pos + 1:pos + length]:
        value = (value << 8) | byte
    unknown = value == (1 << (7 * length)) - 1
    return value, length, unknown


def _vint_length(data: bytes, pos: int, max_len: int) -> int | None:
    if pos >= len(data):
        return None
    first = data[pos]
    mask = 0x80
    for length in range(1, max_len + 1):
        if first & mask:
            return length
        mask >>= 1
    return None


def _encode_element(id_bytes: bytes, content: bytes, preferred_size_len: int | None = None) -> bytes:
    return id_bytes + _encode_size(len(content), preferred_size_len) + content


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
    if length < 1 or length > 8:
        raise ValueError("EBML unknown size length must be 1-8 bytes")
    marker = 1 << (8 - length)
    first = marker | (marker - 1)
    return bytes([first]) + (b"\xff" * (length - 1))


def _aes_ctr_crypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if not data:
        return b""
    if len(key) != 16:
        raise ValueError("WebM AES-128 key must be 16 bytes.")
    if len(iv) != 8:
        raise ValueError(f"Unsupported WebM AES-CTR IV size: {len(iv)}")
    cryptography_backend = _cryptography_cipher_backend()
    if cryptography_backend is not None:
        Cipher, algorithms, modes = cryptography_backend
        decryptor = Cipher(algorithms.AES(key), modes.CTR(iv + b"\x00" * 8)).decryptor()
        return decryptor.update(data) + decryptor.finalize()
    crypto_aes = _crypto_aes_module()
    if crypto_aes is not None:
        AES = crypto_aes
        cipher = AES.new(key, AES.MODE_CTR, nonce=iv, initial_value=0)
        return cipher.decrypt(data)
    raise RuntimeError("WebM VP8/VP9 decryption needs cryptography or pycryptodome installed.")


@lru_cache(maxsize=1)
def _cryptography_cipher_backend():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception:
        return None
    return Cipher, algorithms, modes


@lru_cache(maxsize=1)
def _crypto_aes_module():
    try:
        from Crypto.Cipher import AES
    except Exception:
        return None
    return AES


def _validate_webm_output(path: Path) -> None:
    if not path.exists() or path.stat().st_size <= 0:
        raise RuntimeError(f"WebM decryption output is empty: {path}")
    if not shutil.which("ffprobe"):
        return
    # ffprobe is advisory. Some encrypted WebM VOD files omit duration metadata,
    # but the decrypted stream should still be parseable enough to expose streams.
    result = managed_run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "default=nw=1", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().replace("\n", " | ")
        raise RuntimeError(f"WebM decryption output is not parseable: {message}")
