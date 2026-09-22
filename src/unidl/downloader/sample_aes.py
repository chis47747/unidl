"""Segment-bound HLS SAMPLE-AES decryption with local FFmpeg validation."""

from __future__ import annotations

import math
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .embedding import current_download_runtime, managed_run
from .models import SegmentInfo, StreamInfo
from .sample_aes_samples import decrypt_aac, decrypt_ac3, decrypt_eac3, decrypt_ts

if TYPE_CHECKING:
    from .postprocess import RawKey

_CONTAINERS = {"ts": "mpegts", "aac": "adts", "ac3": "ac3", "eac3": "eac3"}
_MAX_DECODE_ERROR_RATE = 0.01


def _decode_report(report: Path, diagnostics: bytes) -> tuple[int, int]:
    frames = 0
    if report.is_file():
        with report.open(encoding="ascii", errors="replace") as handle:
            for line in handle:
                fields = line.split(",")
                if len(fields) == 6 and fields[0].strip().isdigit():
                    frames += 1
    errors = sum(
        "Error submitting packet to decoder" in line or "corrupt decoded frame" in line
        or "Decoding error:" in line
        for line in diagnostics.decode("utf-8", errors="replace").splitlines()
    )
    return frames, errors


def uses_legacy_sample_aes(stream: StreamInfo) -> bool:
    """Keep fragmented MP4/CBCS and ordinary whole-segment AES on their paths."""
    if stream.manifest_type not in {"hls", "json"} or not stream.encrypted:
        return False
    segments = stream.segments
    if not segments or any(segment.index == -1 for segment in segments):
        return False
    encrypted = [segment for segment in segments if segment.encrypted]
    return bool(encrypted) and all(
        (segment.encryption_scheme or "").upper().replace("-", "_") == "SAMPLE_AES"
        for segment in encrypted
    ) and _extension(stream) in _CONTAINERS


def _extension(stream: StreamInfo) -> str:
    extension = (stream.extension or "").lower().lstrip(".")
    if not extension and stream.segments:
        extension = Path(urlparse(stream.segments[0].url).path).suffix.lower().lstrip(".")
    return "eac3" if extension == "ec3" else extension


def _segment_key(segment: SegmentInfo, keys: Sequence[RawKey]) -> bytes:
    kid = (segment.key_id or "").lower().replace("-", "")
    matching = [key for key in keys if kid and (key.kid or "").lower().replace("-", "") == kid]
    # An explicitly untagged key is allowed; never guess from other tracks' KIDs.
    candidates = matching or [key for key in keys if not key.kid]
    values = {key.key.lower() for key in candidates}
    if len(values) != 1:
        raise ValueError("SAMPLE-AES needs one unambiguous key for each encrypted segment KID")
    try:
        value = bytes.fromhex(values.pop())
    except ValueError as exc:
        raise ValueError("SAMPLE-AES requires a 16-byte hexadecimal key") from exc
    if len(value) != 16:
        raise ValueError("SAMPLE-AES requires a 16-byte hexadecimal key")
    return value


def decrypt_sample_aes_parts(
    parts: Sequence[Path],
    stream: StreamInfo,
    keys: Sequence[RawKey],
    output_path: Path,
    *,
    temp_dir: Path | None = None,
    event_callback: Callable[[str], None] | None = None,
) -> Path:
    """Decrypt each segment and rebuild a private, clear local playlist.

    AAC/AC-3/E-AC-3/H.264 are decrypted before demuxing so buffered packets cannot use
    the next segment's key/IV. FFmpeg gets no original URLs or credentials. Segment
    boundaries matter for IVs and ID3 timestamps; concatenating ciphertext first
    loses that information. Keys are never written to disk or passed to FFmpeg.
    """
    if not uses_legacy_sample_aes(stream):
        raise ValueError("Not a legacy SAMPLE-AES TS/packed-audio stream")
    if len(parts) != len(stream.segments):
        raise ValueError("SAMPLE-AES downloaded part count does not match its playlist")
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("FFmpeg is required for legacy SAMPLE-AES TS/packed-audio processing")
    material = []
    for part, segment in zip(parts, stream.segments, strict=True):
        if segment.gap:
            raise ValueError("Cannot decrypt a SAMPLE-AES playlist with missing segments")
        path = Path(part).resolve(strict=True)
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("SAMPLE-AES segment is empty or not a file")
        if segment.byte_range:
            start, end = segment.byte_range
            if start < 0 or end < start or path.stat().st_size != end - start + 1:
                raise ValueError("SAMPLE-AES part does not match its downloaded byte range")
        duration = segment.duration
        if duration is None or not math.isfinite(duration) or duration <= 0:
            raise ValueError("SAMPLE-AES segment requires a positive playlist duration")
        key = _segment_key(segment, keys) if segment.encrypted else None
        iv = segment.key_iv
        if key and iv is None:
            if segment.index is None or not 0 <= segment.index < 1 << 128:
                raise ValueError("SAMPLE-AES needs an IV or valid media sequence")
            iv = segment.index.to_bytes(16, "big")
        if key and len(iv) != 16:
            raise ValueError("SAMPLE-AES requires a 16-byte IV")
        material.append((path, segment, key, iv))

    extension = _extension(stream)
    output = Path(output_path).resolve().with_suffix(f".{extension}")
    if any(path == output for path, *_ in material):
        raise ValueError("SAMPLE-AES output must not overwrite an encrypted segment")
    output.parent.mkdir(parents=True, exist_ok=True)
    parent = Path(temp_dir) if temp_dir else output.parent
    parent.mkdir(parents=True, exist_ok=True)
    if event_callback:
        event_callback("engine: segment-bound SAMPLE-AES; FFmpeg local remux/validation")
    with tempfile.TemporaryDirectory(prefix="sample-aes-", dir=parent) as directory:
        work = Path(directory)
        # Keep decrypted intermediates private regardless of the process umask.
        work.chmod(0o700)
        lines = ["#EXTM3U", "#EXT-X-VERSION:5", "#EXT-X-PLAYLIST-TYPE:VOD"]
        media_sequence = material[0][1].index
        if isinstance(media_sequence, int) and media_sequence >= 0:
            lines.append(f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}")
        target_duration = stream.extra.get("target_duration")
        if not isinstance(target_duration, (int, float)) or not math.isfinite(target_duration):
            target_duration = 0
        target_duration = max(target_duration, max(item[1].duration for item in material))
        lines.append(f"#EXT-X-TARGETDURATION:{math.ceil(float(target_duration))}")
        for index, (path, segment, key, iv) in enumerate(material):
            runtime = current_download_runtime()
            if runtime is not None:
                runtime.checkpoint()
            local_name = f"part-{index}.{extension}"
            if key:
                decrypt = {"ts": decrypt_ts, "aac": decrypt_aac, "ac3": decrypt_ac3, "eac3": decrypt_eac3}[extension]
                (work / local_name).write_bytes(decrypt(path.read_bytes(), key, iv))
            # Fixed relative names also handle original paths with quotes/newlines.
            else:
                try:
                    os.link(path, work / local_name)
                except OSError:
                    shutil.copyfile(path, work / local_name)
            lines.append("#EXT-X-KEY:METHOD=NONE")
            # The downloader has already sliced BYTERANGE resources into parts.
            # Original resource offsets must not be applied to these files again.
            lines.append(f"#EXTINF:{segment.duration:.9f},")
            lines.append(local_name)
            if segment.discontinuity_after:
                lines.append("#EXT-X-DISCONTINUITY")
        lines.append("#EXT-X-ENDLIST")
        playlist = work / "input.m3u8"
        playlist.write_text("\n".join(lines) + "\n", encoding="ascii")
        temporary_output = work / f"decrypted.{extension}"
        result = managed_run([
            executable, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-protocol_whitelist", "file", "-allowed_extensions", "ALL",
            "-i", str(playlist), "-map", "0:v?", "-map", "0:a?", "-c", "copy",
            "-f", _CONTAINERS[extension], str(temporary_output),
        ], capture_output=True)
        if result.returncode or not temporary_output.is_file() or temporary_output.stat().st_size == 0:
            # Do not echo subprocess arguments or raw diagnostics containing paths.
            raise RuntimeError(
                "SAMPLE-AES decode validation failed during demux/decryption "
                f"(exit {result.returncode}); no decrypted output was published."
            )
        # Finish decoding despite isolated bad frames. Some FFmpeg releases
        # return zero even after widespread decoder errors, so also count
        # decoded frames and unrepeated decoder diagnostics before publishing.
        report = work / "decoded.framehash"
        validation = managed_run([
            executable, "-hide_banner", "-nostdin", "-loglevel", "repeat+error",
            "-max_error_rate", str(_MAX_DECODE_ERROR_RATE), "-abort_on", "empty_output",
            "-err_detect", "explode", "-protocol_whitelist", "file", "-i", str(temporary_output),
            "-map", "0:v?", "-map", "0:a?", "-fps_mode", "passthrough",
            "-f", "framehash", str(report),
        ], capture_output=True)
        frames, errors = _decode_report(report, validation.stderr or b"")
        if validation.returncode or not frames or errors / (frames + errors) > _MAX_DECODE_ERROR_RATE:
            missing_iv = any(
                segment.encrypted and segment.key_iv is None
                and (segment.key_uri or "").startswith("skd://")
                for segment in stream.segments
            )
            detail = (
                " The FairPlay key declaration has no explicit IV; a matching "
                "PlayReady KID alone does not establish the required CBC IV."
                if missing_iv else ""
            )
            raise RuntimeError(
                "SAMPLE-AES decode validation failed; check the content key, IV and "
                f"source format.{detail} Decoded frames: {frames}; decoder errors: {errors}. "
                "No decrypted output was published."
            )
        if event_callback and (getattr(result, "stderr", b"") or getattr(validation, "stderr", b"")):
            event_callback(
                "warning: SAMPLE-AES finished with media diagnostics; decode validation "
                "completed within the 1% error limit. Output may contain damaged frames."
            )
        # Publish atomically even when the download temp directory is on another
        # filesystem. Cancellation must not replace a previous completed output.
        staged = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".sample-aes-", dir=output.parent, delete=False) as handle:
                staged = Path(handle.name)
                with temporary_output.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        runtime = current_download_runtime()
                        if runtime is not None:
                            runtime.checkpoint()
                        handle.write(chunk)
            runtime = current_download_runtime()
            if runtime is not None:
                runtime.checkpoint()
            os.replace(staged, output)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
    return output
