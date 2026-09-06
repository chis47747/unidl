"""Deezer's native Blowfish CBC stripe transport."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from Crypto.Cipher import Blowfish

BLOWFISH_SECRET = b"g4el58wc0zvf9na1"
STRIPE_SIZE = 2048
STRIPE_INTERVAL = 3
BF_CBC_STRIPE = "BF_CBC_STRIPE"
_DECRYPTED_FILES: set[tuple[str, str, int, int, int, int]] = set()


class DeezerTransportError(ValueError):
    """A Deezer media file does not match the native transport contract."""


def normalize_deezer_cipher(value: Any) -> str:
    """Return a stable cipher name from Deezer's string or typed value."""
    if isinstance(value, Mapping):
        value = value.get("TYPE") or value.get("type") or value.get("name") or value.get("cipher")
    return str(value or "").strip().upper().replace("-", "_")


def deezer_blowfish_key(track_id: str) -> bytes:
    """Derive the 16-byte key used for one Deezer track's stripes."""
    digest = hashlib.md5(str(track_id).encode("utf-8")).hexdigest()
    return bytes(
        ord(digest[index]) ^ ord(digest[index + 16]) ^ BLOWFISH_SECRET[index]
        for index in range(16)
    )


def decrypt_deezer_bytes(data: bytes, track_id: str) -> bytes:
    """Decrypt every third complete 2048-byte stripe and preserve other bytes."""
    key = deezer_blowfish_key(track_id)
    iv = bytes(range(Blowfish.block_size))
    output = bytearray()
    for index in range(0, len(data), STRIPE_SIZE):
        stripe = data[index : index + STRIPE_SIZE]
        stripe_number = index // STRIPE_SIZE
        if stripe_number % STRIPE_INTERVAL == 0 and len(stripe) == STRIPE_SIZE:
            stripe = Blowfish.new(key, Blowfish.MODE_CBC, iv).decrypt(stripe)
        output.extend(stripe)
    return bytes(output)


def decrypt_deezer_file(
    path: str | Path,
    track_id: str,
    cipher: Any,
    *,
    extension: str = "",
) -> Path:
    """Atomically decrypt one downloaded Deezer file in place.

    The transport is deliberately handled before generic audio conversion. A
    clear codec signature and the current-process file identity make this
    operation idempotent for resumed files.
    """
    source = Path(path)
    cipher_name = normalize_deezer_cipher(cipher)
    if cipher_name == "NONE":
        return source
    if cipher_name != BF_CBC_STRIPE:
        raise DeezerTransportError(f"Unsupported Deezer media cipher: {cipher_name or 'unknown'}")
    if not source.is_file():
        raise DeezerTransportError(f"Deezer media file does not exist: {source}")
    stat = source.stat()
    if stat.st_size == 0:
        raise DeezerTransportError("Deezer returned an empty media file")
    identity = (
        str(source.resolve()),
        str(track_id),
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )
    if identity in _DECRYPTED_FILES:
        return source

    extension = str(extension or source.suffix).strip().lower().lstrip(".")
    if _looks_like_json_error(source):
        raise DeezerTransportError(
            "Deezer returned a JSON error instead of audio; the media token or quality may be unavailable"
        )
    if _looks_like_clear_audio(source, extension):
        return source

    temporary = source.with_name(f".{source.name}.deezer.tmp")
    temporary.unlink(missing_ok=True)
    key = deezer_blowfish_key(track_id)
    iv = bytes(range(Blowfish.block_size))
    try:
        with source.open("rb") as input_file, temporary.open("wb") as output_file:
            index = 0
            while True:
                stripe = input_file.read(STRIPE_SIZE)
                if not stripe:
                    break
                if index % STRIPE_INTERVAL == 0 and len(stripe) == STRIPE_SIZE:
                    stripe = Blowfish.new(
                        key,
                        Blowfish.MODE_CBC,
                        iv,
                    ).decrypt(stripe)
                output_file.write(stripe)
                index += 1
            output_file.flush()
        temporary.replace(source)
        replaced = source.stat()
        _DECRYPTED_FILES.add(
            (
                str(source.resolve()),
                str(track_id),
                int(replaced.st_dev),
                int(replaced.st_ino),
                int(replaced.st_size),
                int(replaced.st_mtime_ns),
            )
        )
    except (OSError, ValueError) as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, DeezerTransportError):
            raise
        raise DeezerTransportError(f"Deezer stripe decryption failed: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return source


def _looks_like_json_error(path: Path) -> bool:
    try:
        with path.open("rb") as input_file:
            prefix = input_file.read(1024 * 1024)
    except OSError as exc:
        raise DeezerTransportError(f"Could not read Deezer media file: {exc}") from exc
    stripped = prefix.lstrip()
    if not stripped or stripped[:1] not in {b"{", b"["}:
        return False
    try:
        json.loads(prefix.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def _looks_like_clear_audio(path: Path, extension: str) -> bool:
    try:
        with path.open("rb") as input_file:
            prefix = input_file.read(16)
    except OSError as exc:
        raise DeezerTransportError(f"Could not inspect Deezer media file: {exc}") from exc
    if extension == "flac":
        return prefix.startswith(b"fLaC")
    if extension == "mp3":
        return prefix.startswith(b"ID3") or _looks_like_mpeg_frame(prefix)
    if extension in {"aac", "adts"}:
        return _looks_like_adts_frame(prefix)
    if extension in {"ogg", "opus"}:
        return prefix.startswith((b"OggS", b"\x1aE\xdf\xa3"))
    if extension in {"m4a", "mp4"}:
        return len(prefix) >= 8 and prefix[4:8] in {b"ftyp", b"moov", b"mdat"}
    return False


def _looks_like_mpeg_frame(prefix: bytes) -> bool:
    if len(prefix) < 4 or prefix[0] != 0xFF or prefix[1] & 0xE0 != 0xE0:
        return False
    version = (prefix[1] >> 3) & 0x03
    layer = (prefix[1] >> 1) & 0x03
    bitrate_index = (prefix[2] >> 4) & 0x0F
    sample_rate_index = (prefix[2] >> 2) & 0x03
    return version != 0x01 and layer != 0 and bitrate_index not in {0, 0x0F} and sample_rate_index != 0x03


def _looks_like_adts_frame(prefix: bytes) -> bool:
    if len(prefix) < 7 or prefix[0] != 0xFF or prefix[1] & 0xF6 != 0xF0:
        return False
    sample_rate_index = (prefix[2] >> 2) & 0x0F
    frame_length = ((prefix[3] & 0x03) << 11) | (prefix[4] << 3) | (prefix[5] >> 5)
    return sample_rate_index != 0x0F and frame_length >= 7


__all__ = [
    "BF_CBC_STRIPE",
    "BLOWFISH_SECRET",
    "DeezerTransportError",
    "STRIPE_INTERVAL",
    "STRIPE_SIZE",
    "deezer_blowfish_key",
    "decrypt_deezer_bytes",
    "decrypt_deezer_file",
    "normalize_deezer_cipher",
]
