"""Apple Music FairPlay sample decryption using the supplied APK contract."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

from ..core.helpers import load_module
from .cenc_fragment import (
    _first_mdat_payload_start,
    _mp4_boxes,
    _parse_tfhd,
    _parse_trun,
    _top_level_mdat_payload_ranges,
)
from .embedding import managed_run
from .postprocess import (
    _copy_file_to_output,
    _report_decrypt_event,
    _validate_decryption_output,
    normalize_decrypted_mp4_init,
)


def decrypt_apple_music_fmp4_parts(
    part_paths: Iterable[str | Path],
    segments,
    *,
    helper_path: str | Path,
    context_keys: Mapping[str, str],
    default_context_key: str | None = None,
    stream_type: str = "audio",
    output_path: str | Path,
    event_callback=None,
) -> Path:
    """Decrypt each HLS fMP4 sample with APK ``SVDecryptor``.

    Apple Music's HLS response is a FootHill context string, not a normal raw
    AES key. The native decryptor receives one complete media sample at a time;
    this function supplies those sample boundaries from ``moof/traf/trun`` and
    keeps the resulting MP4 timeline and container layout unchanged.
    """
    parts = [Path(path) for path in part_paths]
    segment_list = list(segments)
    if not parts:
        raise ValueError("Apple Music has no downloaded fMP4 parts")
    if len(parts) != len(segment_list):
        raise ValueError("Apple Music downloaded parts do not match the HLS playlist")
    helper = load_module(Path(helper_path), "unidl_applemusic_foothill")
    decrypt_sample = getattr(helper, "decrypt_sample", None)
    if not callable(decrypt_sample):
        raise ValueError("Apple Music FootHill helper does not implement decrypt_sample")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix=f"{output.stem}_foothill_", dir=str(output.parent)))
    temporary = output.with_name(f"{output.name}.tmp")
    normalized_contexts = {
        str(key).strip().lower(): str(value).strip()
        for key, value in context_keys.items()
        if str(key).strip() and str(value).strip()
    }
    fallback_context = str(default_context_key or "").strip()
    if not fallback_context and len(set(normalized_contexts.values())) == 1:
        fallback_context = next(iter(normalized_contexts.values()), "")
    track_type = 0 if stream_type == "audio" else 1
    sample_count = 0
    changed_sample_count = 0
    completed = False
    try:
        with temporary.open("wb") as target:
            for index, (part, segment) in enumerate(zip(parts, segment_list, strict=True)):
                raw = part.read_bytes()
                if getattr(segment, "index", None) == -1:
                    clear_init = work_dir / f"{index:08d}.init.mp4"
                    normalize_decrypted_mp4_init(part, clear_init)
                    _copy_file_to_output(clear_init, target)
                    continue
                context_key = _context_for_segment(
                    segment,
                    normalized_contexts,
                    fallback_context,
                )
                data = bytearray(raw)
                ranges = list(_apple_sample_ranges(data))
                if not ranges:
                    raise ValueError(f"Apple Music media part {index} contains no fMP4 samples")
                for start, end in ranges:
                    encrypted = bytes(data[start:end])
                    clear = decrypt_sample(
                        context_key=context_key,
                        sample=encrypted,
                        track_type=track_type,
                    )
                    if len(clear) != len(encrypted):
                        raise ValueError(
                            f"Apple Music FootHill changed sample size {len(encrypted)} -> {len(clear)}"
                        )
                    if clear == encrypted:
                        raise ValueError(
                            f"Apple Music FootHill returned unchanged encrypted sample in part {index}"
                        )
                    changed_sample_count += 1
                    data[start:end] = clear
                    sample_count += 1
                target.write(data)
        if sample_count <= 0:
            raise ValueError("Apple Music FootHill did not process any media samples")
        if changed_sample_count != sample_count:
            raise ValueError(
                "Apple Music FootHill did not change every media sample "
                f"({changed_sample_count}/{sample_count})"
            )
        _validate_decryption_output(temporary)
        _validate_audio_decode(temporary)
        temporary.replace(output)
        completed = True
        _report_decrypt_event(event_callback, f"engine: Apple Music APK FootHill samples ({sample_count})")
        return output
    finally:
        if not completed:
            temporary.unlink(missing_ok=True)
        shutil.rmtree(work_dir, ignore_errors=True)


def _context_for_segment(segment, context_keys: Mapping[str, str], default_context: str) -> str:
    key_uri = str(getattr(segment, "key_uri", "") or "").strip()
    key_id = str(getattr(segment, "key_id", "") or "").strip().lower().replace("-", "")
    for identity in (key_uri.lower(), key_id):
        if identity and context_keys.get(identity):
            return str(context_keys[identity])
    if key_uri or key_id:
        identity = key_id or key_uri
        raise ValueError(f"Apple Music has no FootHill context for segment key {identity}")
    if default_context:
        return default_context
    raise ValueError("Apple Music media segment has no FootHill context")


def _validate_audio_decode(path: Path) -> None:
    """Require ffmpeg to decode the decrypted audio stream before publishing it."""
    completed = managed_run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-1200:]
        raise ValueError(
            "Apple Music decrypted audio failed ffmpeg validation"
            + (f": {detail}" if detail else f" (status {completed.returncode})")
        )


def _apple_sample_ranges(data: bytes | bytearray):
    """Yield payload ranges for every sample in a fragmented MP4 part."""
    mdat_ranges = _top_level_mdat_payload_ranges(data)
    if not mdat_ranges:
        return
    for moof_position, moof_size, box_type, moof_header in _mp4_boxes(data):
        if box_type != b"moof":
            continue
        moof_end = moof_position + moof_size
        for traf_position, traf_size, traf_type, traf_header in _mp4_boxes(
            data, moof_position + moof_header, moof_end
        ):
            if traf_type != b"traf":
                continue
            traf_end = traf_position + traf_size
            default_sample_size = None
            truns = []
            for position, size, child_type, header_size in _mp4_boxes(
                data, traf_position + traf_header, traf_end
            ):
                if child_type == b"tfhd":
                    tfhd = _parse_tfhd(data, position, size, header_size)
                    if tfhd is not None:
                        default_sample_size = tfhd.default_sample_size
                elif child_type == b"trun":
                    trun = _parse_trun(data, position, size, header_size, default_sample_size)
                    if trun is not None and trun.sample_sizes:
                        truns.append(trun)
            next_sample_start = None
            for trun in truns:
                sample_start = (
                    moof_position + trun.data_offset
                    if trun.data_offset is not None
                    else next_sample_start
                )
                if sample_start is None:
                    sample_start = _first_mdat_payload_start(mdat_ranges)
                for sample_size in trun.sample_sizes:
                    sample_end = sample_start + sample_size
                    if sample_size > 0 and any(
                        sample_start >= start and sample_end <= end
                        for start, end in mdat_ranges
                    ):
                        yield sample_start, sample_end
                    sample_start = sample_end
                    next_sample_start = sample_start


__all__ = ["decrypt_apple_music_fmp4_parts"]
