from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .cenc_fragment import CencInitMetadata, decrypt_cenc_fragment, load_cenc_init_metadata
from .embedding import managed_run
from .utils import looks_like_h266

DecryptEventCallback = Callable[[str], None]


MEDIA_SUFFIXES = {
    ".aac",
    ".ac3",
    ".eac3",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ts",
    ".webm",
}


@dataclass(frozen=True, slots=True)
class RawKey:
    key: str
    kid: str | None = None


@dataclass(frozen=True, slots=True)
class MuxInput:
    path: Path
    language: str | None = None
    name: str | None = None
    default: bool | None = None
    forced: bool | None = None
    delay_ms: int | None = None
    trim_start_ms: int | None = None
    media_type: str | None = None
    track_type_filter: str | None = None
    codecs: str | None = None


def parse_keys(values: list[str] | None) -> list[RawKey]:
    keys: list[RawKey] = []
    for index, raw in enumerate(values or [], start=1):
        parsed = _parse_key_value(raw, f"--key #{index}")
        if parsed is None:
            continue
        keys.append(parsed)
    return keys


def parse_key_text_file(path: str | Path | None) -> list[RawKey]:
    if not path:
        return []
    key_path = Path(path).expanduser()
    keys: list[RawKey] = []
    for line_number, line in enumerate(key_path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _parse_key_value(line.split("#", 1)[0].strip(), f"{key_path}:{line_number}")
        if parsed is not None:
            keys.append(parsed)
    return keys


def parse_mux_import(value: str) -> MuxInput:
    parts = _split_mux_import(value)
    options: dict[str, str] = {}
    path_value: str | None = None
    for part in parts:
        if "=" not in part:
            path_value = part.strip().strip('"')
            continue
        key, raw = part.split("=", 1)
        key = key.strip().lower()
        raw = raw.strip().strip('"')
        if key in {"path", "file", "input"}:
            path_value = raw
        else:
            options[key] = raw
    if not path_value:
        raise ValueError(f"Invalid --mux-import {value!r}; expected a file path or path=FILE.")
    imported = Path(path_value).expanduser()
    if not imported.exists():
        raise ValueError(f"Mux import file not found: {imported}")
    return MuxInput(
        path=imported,
        language=options.get("lang") or options.get("language"),
        name=options.get("name") or options.get("title"),
        default=_bool_option(options.get("default")),
        forced=_bool_option(options.get("forced")),
    )


def decrypt_file(
    input_path: str | Path,
    keys: list[RawKey],
    decrypter: str = "internal",
    stream_type: str = "video",
    output_path: str | Path | None = None,
    expected_kids: Iterable[str | None] | None = None,
    event_callback: DecryptEventCallback | None = None,
) -> Path:
    if not keys:
        raise ValueError("Encrypted stream needs at least one --key KID:KEY or --key KEY.")
    _ensure_matching_keys(keys, expected_kids)
    input_path = Path(input_path)
    output = Path(output_path) if output_path else _decrypted_output_path(input_path)
    _ensure_decryption_free_space(input_path, output)
    label = "internal MP4/CENC/CBCS"
    _report_decrypt_event(event_callback, f"engine: {label}")
    try:
        internal_output = _decrypt_internal_mp4_file(input_path, output, keys, expected_kids)
        if internal_output is not None:
            return internal_output
        message = "unsupported MP4 CENC/CBCS layout"
        _report_decrypt_event(event_callback, f"failed: {label}: {message}")
        raise RuntimeError(message)
    except RuntimeError as exc:
        try:
            if output.exists():
                output.unlink()
        except OSError:
            pass
        if str(exc) != "unsupported MP4 CENC/CBCS layout":
            _report_decrypt_event(event_callback, f"failed: {label}: {_compact_error(exc)}")
        raise RuntimeError(f"{label} decryption failed: {_compact_error(exc)}") from exc


def _decrypt_internal_mp4_file(
    input_path: Path,
    output: Path,
    keys: list[RawKey],
    expected_kids: Iterable[str | None] | None = None,
) -> Path | None:
    decrypted = decrypt_cenc_fragment(
        input_path,
        keys,
        output,
        expected_kids,
        init_path=input_path,
        data_callback=_patch_encrypted_sample_entries,
    )
    if decrypted is None:
        return None
    _validate_decryption_output(output)
    _validate_decryption_size(input_path, output)
    return output


def decrypt_sections(
    section_paths: list[str | Path],
    keys: list[RawKey],
    decrypter: str = "internal",
    stream_type: str = "video",
    output_path: str | Path | None = None,
    expected_kids: Iterable[str | None] | None = None,
    restamp_timestamps: bool = False,
    section_durations: list[float] | None = None,
    section_fragment_durations: list[list[float]] | None = None,
    normalize_large_composition_offsets: bool = False,
    event_callback: DecryptEventCallback | None = None,
) -> Path:
    if not section_paths:
        raise ValueError("No sections to decrypt.")
    sections = [Path(path) for path in section_paths]
    output = Path(output_path) if output_path else _decrypted_output_path(sections[0])
    decrypted_sections: list[Path] = []
    for section in sections:
        section_output = section.with_name(f"{section.stem}.dec{section.suffix}")
        decrypted = decrypt_file(
            section,
            keys=keys,
            decrypter=decrypter,
            stream_type=stream_type,
            output_path=section_output,
            expected_kids=expected_kids,
            event_callback=event_callback,
        )
        if restamp_timestamps and stream_type == "video":
            _mp4_normalize_large_sample_durations(decrypted)
        decrypted_sections.append(decrypted)
    if restamp_timestamps:
        decrypted_sections = restamp_fragmented_mp4_sequence(
            decrypted_sections,
            start_decode_time=0,
            section_durations=section_durations,
            section_fragment_durations=section_fragment_durations,
            normalize_large_composition_offsets=normalize_large_composition_offsets,
        )
    if len(decrypted_sections) == 1:
        if decrypted_sections[0] != output:
            shutil.copyfile(decrypted_sections[0], output)
        return output
    return concat_media_files(decrypted_sections, output)


def restamp_fragmented_mp4_sequence(
    section_paths: list[str | Path],
    start_decode_time: int | None = None,
    section_durations: list[float] | None = None,
    section_fragment_durations: list[list[float]] | None = None,
    normalize_large_composition_offsets: bool = False,
) -> list[Path]:
    restamped_sections: list[Path] = []
    next_decode_time = start_decode_time
    local_section_timestamps = section_fragment_durations is not None
    for index, section_path in enumerate(section_paths):
        section = Path(section_path)
        output = section.with_name(f"{section.stem}.time{section.suffix}")
        fallback_duration = (
            section_durations[index]
            if section_durations and index < len(section_durations)
            else None
        )
        fragment_durations = (
            section_fragment_durations[index]
            if section_fragment_durations and index < len(section_fragment_durations)
            else None
        )
        restamped, next_decode_time = restamp_fragmented_mp4_timestamps(
            section,
            next_decode_time=start_decode_time if local_section_timestamps else next_decode_time,
            output_path=output,
            fallback_duration_seconds=fallback_duration,
            fragment_durations_seconds=fragment_durations,
            preserve_source_deltas=fallback_duration is not None,
            normalize_large_composition_offsets=normalize_large_composition_offsets,
        )
        restamped_sections.append(restamped)
    return restamped_sections


def decrypt_fragmented_mp4_parts(
    part_paths: list[str | Path],
    segments,
    keys: list[RawKey],
    stream_type: str = "video",
    output_path: str | Path | None = None,
    expected_kids: Iterable[str | None] | None = None,
    temp_dir: str | Path | None = None,
    event_callback: DecryptEventCallback | None = None,
    restamp_timestamps: bool = False,
) -> Path:
    if not keys:
        raise ValueError("Encrypted stream needs at least one --key KID:KEY or --key KEY.")
    _ensure_matching_keys(keys, expected_kids)
    parts = [Path(path) for path in part_paths]
    if not parts:
        raise ValueError("No downloaded parts available for fragmented MP4 decryption.")
    if len(parts) != len(segments):
        raise ValueError("Downloaded part count does not match stream segment count.")
    output = Path(output_path) if output_path else _decrypted_output_path(parts[0])
    output.parent.mkdir(parents=True, exist_ok=True)
    _ensure_output_free_space(_paths_total_size(parts), output, "decryption output")
    work_parent = Path(temp_dir).expanduser() if temp_dir else output.parent
    work_parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix=f"{output.stem}_fragments_", dir=str(work_parent)))
    current_init: Path | None = None
    current_timescale: int | None = None
    reported_internal = False
    tmp_output = output.with_name(f"{output.name}.tmp")
    init_metadata_cache: dict[tuple[Path, tuple[str, ...]], CencInitMetadata] = {}
    completed = False
    next_decode_time = 0 if restamp_timestamps else None
    restamp_durations = _fragment_restamp_duration_hints(parts, segments) if restamp_timestamps else {}
    try:
        with tmp_output.open("wb") as target:
            for index, (part, segment) in enumerate(zip(parts, segments, strict=False)):
                if segment.index == -1:
                    current_init = part
                    try:
                        current_timescale = _mp4_fragment_timescale(part.read_bytes())
                    except OSError:
                        current_timescale = None
                    clear_init = work_dir / f"{index:08d}.init.dec.mp4"
                    _report_decrypt_event(event_callback, "engine: internal fMP4 init metadata")
                    normalize_decrypted_mp4_init(part, clear_init)
                    _copy_file_to_output(clear_init, target)
                    clear_init.unlink(missing_ok=True)
                    continue
                if current_init is None:
                    raise RuntimeError("fragmented MP4 media segment appeared before init segment.")
                clear_fragment = work_dir / f"{index:08d}.frag.dec.mp4"
                fragment_init = _init_with_segment_kid(current_init, segment, work_dir, index)
                segment_kid = getattr(segment, "key_id", None)
                fragment_expected_kids = [segment_kid] if segment_kid else expected_kids
                metadata_key = (fragment_init, tuple(sorted(_normalized_kids(fragment_expected_kids))))
                init_metadata = init_metadata_cache.get(metadata_key)
                if init_metadata is None:
                    init_metadata = load_cenc_init_metadata(fragment_init, fragment_expected_kids)
                    init_metadata_cache[metadata_key] = init_metadata
                if not reported_internal:
                    _report_decrypt_event(event_callback, "engine: internal fMP4 CENC fragments")
                    reported_internal = True
                internal_error: Exception | None = None
                try:
                    custom_clear = decrypt_cenc_fragment(
                        part,
                        keys,
                        clear_fragment,
                        fragment_expected_kids,
                        init_path=fragment_init,
                        default_constant_iv=getattr(segment, "key_iv", None),
                        init_metadata=init_metadata,
                        data_callback=_patch_encrypted_sample_entries,
                    )
                except Exception as exc:
                    internal_error = exc
                    custom_clear = None
                    _report_decrypt_event(event_callback, f"failed: internal fMP4 CENC fragment: {_compact_error(exc)}")
                if custom_clear is None:
                    if internal_error is None:
                        _report_decrypt_event(event_callback, "failed: internal fMP4 CENC fragment: unsupported fragment layout")
                        raise RuntimeError("internal fragmented MP4 decryption could not handle this fragment.")
                    raise RuntimeError(f"internal fragmented MP4 decryption failed: {_compact_error(internal_error)}") from internal_error
                if restamp_timestamps:
                    restamped_fragment = work_dir / f"{index:08d}.frag.time.mp4"
                    custom_clear, next_decode_time = restamp_fragmented_mp4_timestamps(
                        custom_clear,
                        next_decode_time=next_decode_time,
                        output_path=restamped_fragment,
                        fallback_duration_seconds=getattr(segment, "duration", None),
                        fallback_duration_ticks=restamp_durations.get(index),
                        preserve_source_deltas=True,
                        timescale_hint=current_timescale,
                    )
                _copy_file_to_output(custom_clear, target)
                if custom_clear != clear_fragment:
                    clear_fragment.unlink(missing_ok=True)
                custom_clear.unlink(missing_ok=True)
        try:
            _validate_decryption_output(tmp_output)
        except RuntimeError as exc:
            label = "internal fMP4 CENC fragments"
            _report_decrypt_event(event_callback, f"failed: {label}: {_compact_error(exc)}")
            raise
        tmp_output.replace(output)
        completed = True
        return output
    finally:
        if not completed:
            try:
                tmp_output.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(work_dir, ignore_errors=True)


def _fragment_restamp_duration_hints(parts: list[Path], segments) -> dict[int, int]:
    hints: dict[int, int] = {}
    media: list[tuple[int, int]] = []
    for index, (part, segment) in enumerate(zip(parts, segments, strict=False)):
        if getattr(segment, "index", None) == -1:
            continue
        try:
            tfdt = _mp4_first_tfdt_time(part.read_bytes())
        except OSError:
            tfdt = None
        if tfdt is not None:
            media.append((index, int(tfdt)))
    previous_delta: int | None = None
    for position, (index, tfdt) in enumerate(media):
        delta = None
        if position + 1 < len(media):
            next_tfdt = media[position + 1][1]
            if next_tfdt > tfdt:
                delta = next_tfdt - tfdt
        if delta is None:
            delta = previous_delta
        if delta is not None and delta > 0:
            hints[index] = int(delta)
            previous_delta = int(delta)
    return hints


def _copy_file_to_output(path: Path, output) -> None:
    with path.open("rb") as source:
        shutil.copyfileobj(source, output, length=1024 * 1024)


def _report_decrypt_event(callback: DecryptEventCallback | None, message: str) -> None:
    if callback:
        callback(message)


def _compact_error(error: Exception) -> str:
    text = str(error).strip().replace("\n", " | ")
    return text if len(text) <= 240 else text[:237] + "..."


def _init_with_segment_kid(current_init: Path, segment, work_dir: Path, index: int) -> Path:
    kid = _normalize_single_kid(getattr(segment, "key_id", None))
    if not kid:
        return current_init
    current_kids = mp4_tenc_default_kids(current_init)
    if not current_kids or kid in current_kids:
        return current_init
    return patch_mp4_tenc_default_kid(current_init, kid, work_dir / f"{index:08d}.{kid}.init.mp4")


def decrypt_fragmented_mp4_part(
    part_path: str | Path,
    init_path: str | Path,
    keys: list[RawKey],
    stream_type: str = "video",
    output_path: str | Path | None = None,
    expected_kids: Iterable[str | None] | None = None,
    decrypter: str = "internal",
    event_callback: DecryptEventCallback | None = None,
    default_constant_iv: bytes | None = None,
) -> Path:
    if not keys:
        raise ValueError("Encrypted stream needs at least one --key KID:KEY or --key KEY.")
    _ensure_matching_keys(keys, expected_kids)
    part = Path(part_path)
    init = Path(init_path)
    output = Path(output_path) if output_path else part.with_name(f"{part.stem}.dec{part.suffix}")
    output.parent.mkdir(parents=True, exist_ok=True)
    _report_decrypt_event(event_callback, "engine: internal fMP4 CENC fragment")
    internal_error: Exception | None = None
    try:
        custom_clear = decrypt_cenc_fragment(
            part,
            keys,
            output,
            expected_kids,
            init_path=init,
            default_constant_iv=default_constant_iv,
            data_callback=_patch_encrypted_sample_entries,
        )
    except Exception as exc:
        internal_error = exc
        custom_clear = None
        _report_decrypt_event(event_callback, f"failed: internal fMP4 CENC fragment: {_compact_error(exc)}")
    if custom_clear is not None:
        try:
            _validate_decryption_output(output)
            return output
        except RuntimeError as exc:
            internal_error = exc
            custom_clear = None
            _report_decrypt_event(event_callback, f"failed: internal fMP4 CENC fragment: {_compact_error(exc)}")
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass
    if internal_error is None:
        _report_decrypt_event(event_callback, "failed: internal fMP4 CENC fragment: unsupported fragment layout")
        raise RuntimeError("internal fragmented MP4 decryption could not handle this fragment.")
    raise RuntimeError(f"internal fragmented MP4 decryption failed: {_compact_error(internal_error)}") from internal_error


def normalize_decrypted_mp4_init(input_path: str | Path, output_path: str | Path | None = None) -> Path:
    input_path = Path(input_path)
    output = Path(output_path) if output_path else input_path.with_name(f"{input_path.stem}.dec{input_path.suffix}")
    data = bytearray(input_path.read_bytes())
    _patch_encrypted_sample_entries(data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    _validate_decryption_output(output)
    return output


def normalize_decrypted_mp4_bytes(data: bytes) -> bytes:
    """Restore clear sample entries after an external payload decrypter."""

    output = bytearray(data)
    _patch_encrypted_sample_entries(output)
    return bytes(output)


def patch_mp4_tenc_default_kid(input_path: str | Path, kid: str, output_path: str | Path | None = None) -> Path:
    input_path = Path(input_path)
    normalized = _normalize_single_kid(kid)
    if normalized is None:
        raise ValueError(f"Invalid KID for init patch: {kid}")
    data = bytearray(input_path.read_bytes())
    positions = _tenc_default_kid_positions(data)
    if not positions:
        raise RuntimeError(f"init segment has no tenc default_KID to patch: {input_path}")
    kid_bytes = bytes.fromhex(normalized)
    changed = False
    for position in positions:
        if data[position : position + 16] != kid_bytes:
            data[position : position + 16] = kid_bytes
            changed = True
    output = Path(output_path) if output_path else input_path.with_name(f"{input_path.stem}.{normalized}.init{input_path.suffix}")
    if not changed and output == input_path:
        return input_path
    output.write_bytes(data)
    return output


def mp4_tenc_default_kids(input_path: str | Path) -> list[str]:
    try:
        data = Path(input_path).read_bytes()
    except OSError:
        return []
    return mp4_tenc_default_kids_from_bytes(data)


def mp4_tenc_default_kids_from_bytes(data: bytes | bytearray) -> list[str]:
    kids = [data[position : position + 16].hex() for position in _tenc_default_kid_positions(data)]
    return list(dict.fromkeys(kids))


def restamp_fragmented_mp4_timestamps(
    input_path: str | Path,
    next_decode_time: int | None = None,
    output_path: str | Path | None = None,
    min_duration_seconds: float | None = None,
    fallback_duration_seconds: float | None = None,
    fallback_duration_ticks: int | None = None,
    fragment_durations_seconds: list[float] | None = None,
    preserve_source_deltas: bool = False,
    normalize_large_composition_offsets: bool = False,
    timescale_hint: int | None = None,
) -> tuple[Path, int | None]:
    input_path = Path(input_path)
    output = Path(output_path) if output_path else input_path
    data = bytearray(input_path.read_bytes())
    tfdt_positions = _mp4_find_tfdt_positions(data)
    if not tfdt_positions:
        if output != input_path:
            shutil.copyfile(input_path, output)
        return output, next_decode_time

    current = next_decode_time if next_decode_time is not None else _mp4_first_tfdt_time(data)
    if fragment_durations_seconds is not None:
        timescale = timescale_hint or _mp4_fragment_timescale(data)
        if current is not None and timescale:
            durations = [int(round(max(0.0, duration) * timescale)) for duration in fragment_durations_seconds]
            if durations:
                patched_times: list[int] = []
                cursor = current
                last_duration = durations[-1]
                for index, position in enumerate(tfdt_positions):
                    patched_times.append(cursor)
                    _mp4_patch_tfdt(data, position, cursor)
                    cursor += durations[index] if index < len(durations) else last_duration
                _mp4_patch_top_level_sidx_values(data, patched_times)
                if normalize_large_composition_offsets:
                    _mp4_normalize_large_composition_offsets(data)
                next_time = current + sum(durations)
                if len(tfdt_positions) > len(durations):
                    next_time = cursor
                output.write_bytes(data)
                return output, next_time

    if preserve_source_deltas and fallback_duration_seconds is not None:
        timescale = timescale_hint or _mp4_fragment_timescale(data)
        source_times = _mp4_tfdt_times(data)
        if current is not None and timescale and source_times:
            first_source = source_times[0]
            patched_times = [current + max(0, source_time - first_source) for source_time in source_times]
            _mp4_patch_top_level_sidx_values(data, patched_times)
            for position, target_time in zip(tfdt_positions, patched_times, strict=False):
                _mp4_patch_tfdt(data, position, target_time)
            if normalize_large_composition_offsets:
                _mp4_normalize_large_composition_offsets(data)
            output.write_bytes(data)
            return output, current + int(round(max(0.0, fallback_duration_seconds) * timescale))

    if preserve_source_deltas and fallback_duration_ticks is not None:
        source_times = _mp4_tfdt_times(data)
        if current is not None and source_times:
            first_source = source_times[0]
            patched_times = [current + max(0, source_time - first_source) for source_time in source_times]
            _mp4_patch_top_level_sidx_values(data, patched_times)
            for position, target_time in zip(tfdt_positions, patched_times, strict=False):
                _mp4_patch_tfdt(data, position, target_time)
            if normalize_large_composition_offsets:
                _mp4_normalize_large_composition_offsets(data)
            output.write_bytes(data)
            return output, current + int(max(0, fallback_duration_ticks))

    durations = _mp4_sidx_durations(data) or _mp4_trun_durations(data)
    if current is None or not durations:
        if normalize_large_composition_offsets and _mp4_normalize_large_composition_offsets(data):
            output.write_bytes(data)
        elif output != input_path:
            shutil.copyfile(input_path, output)
        return output, next_decode_time
    if len(durations) < len(tfdt_positions):
        durations.extend([durations[-1]] * (len(tfdt_positions) - len(durations)))
    if min_duration_seconds is not None:
        timescale = _mp4_sidx_timescale(data) or timescale_hint
        if timescale:
            minimum_duration = int(round(max(0.0, min_duration_seconds) * timescale))
            actual_duration = sum(durations)
            if minimum_duration > actual_duration:
                extra_duration = minimum_duration - actual_duration
                _mp4_extend_last_sidx_reference(data, extra_duration)
                if _mp4_stretch_trun_timing(data, minimum_duration):
                    durations[-1] = minimum_duration

    _mp4_patch_top_level_sidx(data, current)
    for index, position in enumerate(tfdt_positions):
        _mp4_patch_tfdt(data, position, current)
        current += durations[index]
    if normalize_large_composition_offsets:
        _mp4_normalize_large_composition_offsets(data)

    output.write_bytes(data)
    return output, current


def fragmented_mp4_decode_time(input_path: str | Path) -> int | None:
    try:
        return _mp4_first_tfdt_time(Path(input_path).read_bytes())
    except OSError:
        return None


def fragmented_mp4_timing(input_path: str | Path) -> tuple[int | None, int | None, int | None]:
    try:
        data = Path(input_path).read_bytes()
    except OSError:
        return None, None, None
    durations = _mp4_sidx_durations(data) or _mp4_trun_durations(data)
    return _mp4_first_tfdt_time(data), _mp4_fragment_timescale(data), sum(durations) if durations else None


def split_fragmented_mp4_init_media(
    input_path: str | Path,
    init_output_path: str | Path,
    media_output_path: str | Path,
) -> tuple[Path | None, Path]:
    input_path = Path(input_path)
    init_output = Path(init_output_path)
    media_output = Path(media_output_path)
    data = input_path.read_bytes()
    media_start = _mp4_media_start(data)
    if media_start is None or media_start <= 0:
        shutil.copyfile(input_path, media_output)
        return None, media_output

    init_data = data[:media_start]
    media_data = data[media_start:]
    init_path: Path | None = None
    if _has_box_type(init_data, b"moov"):
        init_output.write_bytes(init_data)
        init_path = init_output
    media_output.write_bytes(media_data)
    return init_path, media_output


def mux_files(
    inputs: list[str | Path | MuxInput],
    output_path: str | Path,
    muxer: str = "auto",
    imports: list[MuxInput] | None = None,
    chapters_file: str | Path | None = None,
    *,
    force_vvc_mp4: bool = True,
) -> Path:
    primary = [_coerce_mux_input(item) for item in inputs]
    imported = imports or []
    all_inputs = [*primary, *imported]
    if not all_inputs:
        raise ValueError("No input files to mux.")
    output = Path(output_path)
    if chapters_file and output.suffix.casefold() in {".ts", ".m2ts"}:
        raise ValueError(
            "Chapters cannot be embedded in an MPEG-TS output; choose MKV/MP4 or use --no-mux."
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    prepared_inputs, temp_dir = _prepare_mux_inputs(all_inputs, output)
    chapter_temp_dir: Path | None = None
    try:
        _ensure_mux_free_space([item.path for item in prepared_inputs], output)
        selected = _select_muxer(muxer, output)
        vvc_video = _mux_inputs_include_vvc(prepared_inputs)
        vvc_mp4 = vvc_video and force_vvc_mp4
        if vvc_mp4 and output.suffix.lower() in {".mkv", ".mka", ".mks"}:
            # A VVC track written by mkvmerge is identified as V_QUICKTIME.
            # Keep the path's suffix truthful as well as selecting FFmpeg.
            output = output.with_suffix(".mp4")
        if vvc_mp4 and selected == "mkvmerge":
            if not shutil.which("ffmpeg"):
                raise RuntimeError(
                    "VVC muxing requires ffmpeg; mkvmerge cannot write a usable VVC track."
                )
            selected = "ffmpeg"
        elif selected == "mkvmerge" and muxer == "auto":
            executable = shutil.which("mkvmerge")
            if executable and shutil.which("ffmpeg") and (
                vvc_mp4 or not _mkvmerge_recognizes_inputs(executable, prepared_inputs)
            ):
                selected = "ffmpeg"
        chapter_path: Path | None = None
        if chapters_file:
            from .chapters import (
                load_chapters_file,
                write_ffmetadata,
                write_ogm_chapters,
            )

            chapters = load_chapters_file(chapters_file)
            if chapters:
                chapter_temp_dir = Path(
                    tempfile.mkdtemp(prefix=f"{output.stem}_chapters_", dir=str(output.parent))
                )
                if selected == "mkvmerge":
                    chapter_path = write_ogm_chapters(
                        chapters, chapter_temp_dir / "chapters.txt"
                    )
                else:
                    chapter_path = write_ffmetadata(
                        chapters, chapter_temp_dir / "chapters.ffmeta"
                    )
        if selected == "mkvmerge":
            executable = shutil.which("mkvmerge")
            if not executable:
                raise RuntimeError("mkvmerge not found.")
            args = [executable, "--output", str(output)]
            if chapter_path is not None:
                args.extend(["--chapters", str(chapter_path)])
            for item in prepared_inputs:
                args.extend(_mkvmerge_import_args(item))
        elif selected == "ffmpeg":
            executable = shutil.which("ffmpeg")
            if not executable:
                raise RuntimeError("ffmpeg not found.")
            args = [executable, "-hide_banner", "-y"]
            for item in prepared_inputs:
                if vvc_mp4:
                    # Tencent VVC transport can carry presentation-ordered
                    # timestamps. Regenerate them per input before FFmpeg
                    # writes the strictly ordered MP4 sample timeline.
                    args.extend(["-fflags", "+genpts"])
                if item.delay_ms:
                    args.extend(["-itsoffset", _ffmpeg_delay_value(item.delay_ms)])
                args.extend(["-i", str(item.path)])
            chapter_input = None
            if chapter_path is not None:
                chapter_input = len(prepared_inputs)
                args.extend(["-f", "ffmetadata", "-i", str(chapter_path)])
            for index, item in enumerate(prepared_inputs):
                args.extend(["-map", _ffmpeg_mux_map_spec(index, item)])
            for index, item in enumerate(prepared_inputs):
                args.extend(_ffmpeg_import_metadata_args(index, item))
            args.extend(["-c", "copy"])
            if chapter_input is not None:
                args.extend(["-map_chapters", str(chapter_input)])
            if vvc_video:
                args.extend(["-tag:v", "vvc1"])
            if vvc_mp4:
                args.extend(["-avoid_negative_ts", "make_zero"])
            args.append(str(output))
        else:
            raise ValueError(f"Unsupported muxer: {muxer}")
        _run_external(args, "muxing")
        return output
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        if chapter_temp_dir is not None:
            shutil.rmtree(chapter_temp_dir, ignore_errors=True)


def _coerce_mux_input(item: str | Path | MuxInput) -> MuxInput:
    if isinstance(item, MuxInput):
        return item
    return MuxInput(path=Path(item))


def _prepare_mux_inputs(inputs: list[MuxInput], output: Path) -> tuple[list[MuxInput], Path | None]:
    if not any(item.trim_start_ms and item.trim_start_ms > 0 for item in inputs):
        return inputs, None
    temp_dir = Path(tempfile.mkdtemp(prefix=f"{output.stem}_mux_", dir=str(output.parent)))
    prepared: list[MuxInput] = []
    for index, item in enumerate(inputs, start=1):
        if not item.trim_start_ms or item.trim_start_ms <= 0:
            prepared.append(item)
            continue
        suffix = item.path.suffix or ".mp4"
        trimmed = temp_dir / f"{index:02d}_{item.path.stem}.trim{suffix}"
        _trim_mux_input(item.path, trimmed, item.trim_start_ms)
        prepared.append(
            MuxInput(
                path=trimmed,
                language=item.language,
                name=item.name,
                default=item.default,
                forced=item.forced,
                delay_ms=item.delay_ms,
                media_type=item.media_type,
                track_type_filter=item.track_type_filter,
                codecs=item.codecs,
            )
        )
    return prepared, temp_dir


def _trim_mux_input(input_path: Path, output_path: Path, trim_start_ms: int) -> None:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg not found; mux input trimming needs ffmpeg.")
    _run_external(
        [
            executable,
            "-hide_banner",
            "-y",
            "-i",
            str(input_path),
            "-ss",
            _ffmpeg_delay_value(trim_start_ms),
            "-map",
            "0",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            str(output_path),
        ],
        "mux input trimming",
    )


def repackage_ffmpeg(input_path: str | Path, output_path: str | Path | None = None) -> Path:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg not found.")
    input_path = Path(input_path)
    output = Path(output_path) if output_path else input_path.with_suffix(f".repack{input_path.suffix}")
    _run_external([executable, "-hide_banner", "-y", "-i", str(input_path), "-c", "copy", str(output)], "repackaging")
    return output


def concat_media_files(inputs: list[str | Path], output_path: str | Path) -> Path:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg not found.")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as list_file:
        list_path = Path(list_file.name)
        for item in inputs:
            list_file.write(f"file '{_ffmpeg_concat_path(Path(item))}'\n")
    try:
        _run_external(
            [
                executable,
                "-hide_banner",
                "-y",
                "-fflags",
                "+genpts",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                str(output),
            ],
            "section concat",
        )
    finally:
        try:
            list_path.unlink()
        except OSError:
            pass
    return output


def _decrypted_output_path(input_path: Path) -> Path:
    suffix = input_path.suffix.lower()
    if suffix in MEDIA_SUFFIXES:
        return input_path.with_suffix(f".dec{input_path.suffix}")
    return input_path.with_name(input_path.name + ".dec.mp4")


def _split_mux_import(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for index, char in enumerate(value):
        if char in {"'", '"'}:
            quote = None if quote == char else (char if quote is None else quote)
            current.append(char)
            continue
        if quote is None and char in {":", ","} and _looks_like_mux_separator(value, index, char):
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(char)
    part = "".join(current).strip()
    if part:
        parts.append(part)
    return parts or [value]


def _looks_like_mux_separator(value: str, current_length: int, separator: str) -> bool:
    position = current_length
    if position >= len(value) or value[position] != separator:
        return False
    rest = value[position + 1 :].lstrip()
    return bool(re.match(r"(path|file|input|lang|language|name|title|default|forced)=", rest, flags=re.IGNORECASE))


def _bool_option(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    if value.lower() in {"1", "true", "yes", "y", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _mkvmerge_import_args(item: MuxInput) -> list[str]:
    args: list[str] = []
    if item.track_type_filter == "video":
        args.extend(["--no-audio", "--no-subtitles", "--no-buttons"])
    elif item.track_type_filter == "audio":
        args.extend(["--no-video", "--no-subtitles", "--no-buttons"])
    if item.language:
        args.extend(["--language", f"0:{item.language}"])
    if item.name:
        args.extend(["--track-name", f"0:{item.name}"])
    if item.default is not None:
        args.extend(["--default-track", f"0:{'yes' if item.default else 'no'}"])
    if item.forced is not None:
        args.extend(["--forced-track", f"0:{'yes' if item.forced else 'no'}"])
    if item.delay_ms:
        args.extend(["--sync", f"0:{item.delay_ms}"])
    args.append(str(item.path))
    return args


def _ffmpeg_mux_map_spec(input_index: int, item: MuxInput) -> str:
    if item.track_type_filter == "video":
        return f"{input_index}:v"
    if item.track_type_filter == "audio":
        return f"{input_index}:a"
    return str(input_index)


def _ffmpeg_delay_value(delay_ms: int) -> str:
    seconds = delay_ms / 1000
    return f"{seconds:.3f}".rstrip("0").rstrip(".")


def _ffmpeg_import_metadata_args(output_index: int, item: MuxInput) -> list[str]:
    args: list[str] = []
    if item.language:
        args.extend([f"-metadata:s:{output_index}", f"language={item.language}"])
    if item.name:
        args.extend([f"-metadata:s:{output_index}", f"title={item.name}"])
    if item.default is not None or item.forced is not None:
        disposition = []
        if item.default:
            disposition.append("default")
        if item.forced:
            disposition.append("forced")
        args.extend([f"-disposition:{output_index}", "+".join(disposition) if disposition else "0"])
    return args


def _patch_encrypted_sample_entries(data: bytearray) -> bool:
    changed = False
    for position, size in _scan_box_ranges(data, b"encv") + _scan_box_ranges(data, b"enca"):
        box_end = position + size
        original_format = _sample_entry_original_format(data, position, box_end)
        if original_format:
            data[position + 4 : position + 8] = original_format
            changed = True
        if _patch_tenc_clear_state(data, position, box_end):
            changed = True
        if _neutralize_sample_entry_protection(data, position, box_end):
            changed = True
    return changed


def _sample_entry_original_format(data: bytes | bytearray, start: int, end: int) -> bytes | None:
    for position, _size in _scan_box_ranges(data[start:end], b"frma"):
        absolute = start + position
        if absolute + 12 <= end:
            return bytes(data[absolute + 8 : absolute + 12])
    return None


def _patch_tenc_clear_state(data: bytearray, start: int, end: int) -> bool:
    changed = False
    for position, size in _scan_box_ranges(data[start:end], b"tenc"):
        absolute = start + position
        if absolute + size > end:
            continue
        selected: tuple[int, int] | None = None
        for protected_offset, iv_size_offset in ((14, 15), (13, 14)):
            if absolute + iv_size_offset >= len(data):
                continue
            protected = data[absolute + protected_offset]
            iv_size = data[absolute + iv_size_offset]
            if protected != 0 and iv_size in {0, 8, 16}:
                selected = (protected_offset, iv_size_offset)
                break
        if selected is None:
            continue
        protected_offset, iv_size_offset = selected
        is_protected_position = absolute + protected_offset
        iv_size_position = absolute + iv_size_offset
        if data[is_protected_position] != 0:
            data[is_protected_position] = 0
            changed = True
        if data[iv_size_position] != 0:
            data[iv_size_position] = 0
            changed = True
    return changed


def _neutralize_sample_entry_protection(data: bytearray, start: int, end: int) -> bool:
    changed = False
    for position, size in _scan_box_ranges(data[start:end], b"sinf"):
        absolute = start + position
        if absolute + size > end:
            continue
        data[absolute + 4 : absolute + 8] = b"free"
        changed = True
    return changed


_MP4_CONTAINER_BOXES = {
    b"moov",
    b"trak",
    b"mdia",
    b"minf",
    b"stbl",
    b"edts",
    b"moof",
    b"traf",
    b"mvex",
    b"meta",
    b"sinf",
    b"schi",
}


_MP4_MEDIA_BOXES = {
    b"styp",
    b"sidx",
    b"moof",
    b"mdat",
    b"emsg",
    b"prft",
}


def _mp4_boxes(data: bytes | bytearray, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    position = start
    while position + 8 <= end:
        size = int.from_bytes(data[position : position + 4], "big")
        box_type = bytes(data[position + 4 : position + 8])
        header_size = 8
        if size == 1:
            if position + 16 > end:
                break
            size = int.from_bytes(data[position + 8 : position + 16], "big")
            header_size = 16
        elif size == 0:
            size = end - position
        if size < header_size or position + size > end:
            break
        yield position, size, box_type, header_size
        position += size


def _mp4_find_tfdt_positions(data: bytes | bytearray) -> list[int]:
    positions: list[int] = []

    def walk(start: int, end: int) -> None:
        for position, size, box_type, header_size in _mp4_boxes(data, start, end):
            if box_type == b"tfdt":
                positions.append(position)
            if box_type in _MP4_CONTAINER_BOXES:
                child_start = position + header_size + (4 if box_type == b"meta" else 0)
                walk(child_start, position + size)

    walk(0, len(data))
    return positions


def _mp4_media_start(data: bytes | bytearray) -> int | None:
    for position, _size, box_type, _header_size in _mp4_boxes(data):
        if box_type in _MP4_MEDIA_BOXES:
            return position
    return None


def _mp4_first_tfdt_time(data: bytes | bytearray) -> int | None:
    positions = _mp4_find_tfdt_positions(data)
    if not positions:
        return None
    return _mp4_tfdt_time_at(data, positions[0])


def _mp4_tfdt_times(data: bytes | bytearray) -> list[int]:
    times: list[int] = []
    for position in _mp4_find_tfdt_positions(data):
        value = _mp4_tfdt_time_at(data, position)
        if value is not None:
            times.append(value)
    return times


def _mp4_tfdt_time_at(data: bytes | bytearray, position: int) -> int | None:
    payload = position + 8
    if payload + 8 > len(data):
        return None
    version = data[payload]
    if version == 1:
        if payload + 12 > len(data):
            return None
        return int.from_bytes(data[payload + 4 : payload + 12], "big")
    return int.from_bytes(data[payload + 4 : payload + 8], "big")


def _mp4_patch_tfdt(data: bytearray, position: int, value: int) -> None:
    payload = position + 8
    version = data[payload]
    if version == 1:
        data[payload + 4 : payload + 12] = int(value).to_bytes(8, "big")
    else:
        data[payload + 4 : payload + 8] = int(value).to_bytes(4, "big")


def _mp4_patch_top_level_sidx(data: bytearray, value: int) -> None:
    _mp4_patch_top_level_sidx_values(data, [value])


def _mp4_patch_top_level_sidx_values(data: bytearray, values: list[int]) -> None:
    value_index = 0
    for position, _size, box_type, header_size in _mp4_boxes(data):
        if box_type != b"sidx":
            continue
        if value_index >= len(values):
            return
        value = values[value_index]
        value_index += 1
        payload = position + header_size
        version = data[payload]
        earliest_offset = payload + 4 + 4 + 4
        if version == 1:
            data[earliest_offset : earliest_offset + 8] = int(value).to_bytes(8, "big")
        else:
            data[earliest_offset : earliest_offset + 4] = int(value).to_bytes(4, "big")


def _mp4_fragment_timescale(data: bytes | bytearray) -> int | None:
    return _mp4_sidx_timescale(data) or _mp4_mdhd_timescale(data)


def _mp4_sidx_timescale(data: bytes | bytearray) -> int | None:
    for position, _size, box_type, header_size in _mp4_boxes(data):
        if box_type != b"sidx":
            continue
        payload = position + header_size
        if payload + 12 <= len(data):
            return int.from_bytes(data[payload + 8 : payload + 12], "big")
    return None


def _mp4_mdhd_timescale(data: bytes | bytearray) -> int | None:
    def walk(start: int, end: int) -> int | None:
        for position, size, box_type, header_size in _mp4_boxes(data, start, end):
            if box_type == b"mdhd":
                payload = position + header_size
                if payload + 4 > len(data):
                    return None
                version = data[payload]
                timescale_offset = payload + 20 if version == 1 else payload + 12
                if timescale_offset + 4 <= position + size:
                    timescale = int.from_bytes(data[timescale_offset : timescale_offset + 4], "big")
                    return timescale or None
                return None
            if box_type in _MP4_CONTAINER_BOXES:
                child_start = position + header_size + (4 if box_type == b"meta" else 0)
                found = walk(child_start, position + size)
                if found:
                    return found
        return None

    return walk(0, len(data))


def _mp4_extend_last_sidx_reference(data: bytearray, extra_duration: int) -> bool:
    target: tuple[int, int] | None = None
    for position, size, box_type, header_size in _mp4_boxes(data):
        if box_type != b"sidx":
            continue
        payload = position + header_size
        version = data[payload]
        cursor = payload + 4 + 4 + 4
        cursor += 8 if version == 1 else 4
        cursor += 8 if version == 1 else 4
        cursor += 2
        if cursor + 2 > position + size:
            continue
        reference_count = int.from_bytes(data[cursor : cursor + 2], "big")
        cursor += 2
        for _ in range(reference_count):
            if cursor + 12 > position + size:
                break
            target = (cursor + 4, int.from_bytes(data[cursor + 4 : cursor + 8], "big"))
            cursor += 12
    if target is None:
        return False
    offset, value = target
    data[offset : offset + 4] = int(value + extra_duration).to_bytes(4, "big")
    return True


def _mp4_sidx_durations(data: bytes | bytearray) -> list[int]:
    durations: list[int] = []
    for position, size, box_type, header_size in _mp4_boxes(data):
        if box_type != b"sidx":
            continue
        payload = position + header_size
        version = data[payload]
        cursor = payload + 4 + 4 + 4
        cursor += 8 if version == 1 else 4
        cursor += 8 if version == 1 else 4
        cursor += 2
        if cursor + 2 > position + size:
            continue
        reference_count = int.from_bytes(data[cursor : cursor + 2], "big")
        cursor += 2
        for _ in range(reference_count):
            if cursor + 12 > position + size:
                break
            durations.append(int.from_bytes(data[cursor + 4 : cursor + 8], "big"))
            cursor += 12
    return durations


def _mp4_trun_durations(data: bytes | bytearray) -> list[int]:
    durations: list[int] = []

    def walk(start: int, end: int) -> None:
        for position, size, box_type, header_size in _mp4_boxes(data, start, end):
            if box_type == b"trun":
                total = _mp4_trun_duration(data, position, size, header_size)
                if total:
                    durations.append(total)
            if box_type in _MP4_CONTAINER_BOXES:
                child_start = position + header_size + (4 if box_type == b"meta" else 0)
                walk(child_start, position + size)

    walk(0, len(data))
    return durations


def _mp4_stretch_trun_timing(data: bytearray, target_duration: int) -> bool:
    samples: list[tuple[int, int, int | None, int | None, bool]] = []

    def walk(start: int, end: int) -> None:
        for position, size, box_type, header_size in _mp4_boxes(data, start, end):
            if box_type == b"trun":
                samples.extend(_mp4_trun_sample_timing_positions(data, position, size, header_size))
            if box_type in _MP4_CONTAINER_BOXES:
                child_start = position + header_size + (4 if box_type == b"meta" else 0)
                walk(child_start, position + size)

    walk(0, len(data))
    if not samples:
        return False
    actual_duration = sum(value for _offset, value, _cto_offset, _cto, _signed in samples)
    if actual_duration <= 0:
        return False
    scale = target_duration / actual_duration
    scaled_durations = [max(1, int(round(value * scale))) for _offset, value, _cto_offset, _cto, _signed in samples]
    difference = int(target_duration) - sum(scaled_durations)
    step = 1 if difference >= 0 else -1
    for index in range(abs(difference)):
        position = len(scaled_durations) - 1 - (index % len(scaled_durations))
        if step < 0 and scaled_durations[position] <= 1:
            continue
        scaled_durations[position] += step
    for index, (offset, _value, cto_offset, cto_value, cto_signed) in enumerate(samples):
        data[offset : offset + 4] = int(scaled_durations[index]).to_bytes(4, "big")
        if cto_offset is not None and cto_value is not None:
            scaled_cto = int(round(cto_value * scale))
            data[cto_offset : cto_offset + 4] = int(scaled_cto).to_bytes(4, "big", signed=cto_signed)
    return True


def _mp4_normalize_large_composition_offsets(data: bytearray) -> bool:
    timescale = _mp4_fragment_timescale(data)
    if not timescale:
        return False
    threshold = max(1, int(round(timescale)))
    changed = False

    def walk(start: int, end: int) -> None:
        nonlocal changed
        for position, size, box_type, header_size in _mp4_boxes(data, start, end):
            if box_type == b"trun":
                samples = _mp4_trun_composition_offset_positions(data, position, size, header_size)
                offsets = [cto for _cto_offset, cto, _signed in samples]
                if offsets:
                    baseline = min(offsets)
                    if baseline > threshold:
                        for cto_offset, cto, cto_signed in samples:
                            normalized = cto - baseline
                            data[cto_offset : cto_offset + 4] = int(normalized).to_bytes(4, "big", signed=cto_signed)
                            changed = True
            if box_type in _MP4_CONTAINER_BOXES:
                child_start = position + header_size + (4 if box_type == b"meta" else 0)
                walk(child_start, position + size)

    walk(0, len(data))
    return changed


def _mp4_normalize_large_sample_durations(input_path: str | Path) -> bool:
    path = Path(input_path)
    if _mp4_file_is_fragmented(path):
        return False
    try:
        data = bytearray(path.read_bytes())
    except OSError:
        return False
    changed = _mp4_normalize_large_stts_durations(data)
    if changed:
        path.write_bytes(data)
    return changed


def _mp4_file_is_fragmented(path: Path) -> bool:
    try:
        size = path.stat().st_size
        with path.open("rb") as file:
            position = 0
            while position + 8 <= size:
                file.seek(position)
                header = file.read(8)
                if len(header) < 8:
                    return False
                box_size = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                header_size = 8
                if box_size == 1:
                    extended = file.read(8)
                    if len(extended) < 8:
                        return False
                    box_size = int.from_bytes(extended, "big")
                    header_size = 16
                elif box_size == 0:
                    box_size = size - position
                if box_size < header_size or position + box_size > size:
                    return False
                if box_type == b"moof":
                    return True
                if box_type == b"mdat":
                    return False
                position += box_size
    except OSError:
        return False
    return False


def _mp4_normalize_large_stts_durations(data: bytearray) -> bool:
    changed = False
    duration_rewrites: list[tuple[int, int]] = []
    media_timescale = _mp4_first_mdhd_timescale(data)

    def walk(start: int, end: int) -> None:
        nonlocal changed
        for position, size, box_type, header_size in _mp4_boxes(data, start, end):
            if box_type == b"stts":
                result = _mp4_normalize_stts_box(data, position, size, header_size, media_timescale)
                if result is not None:
                    old_duration, new_duration = result
                    duration_rewrites.append((old_duration, new_duration))
                    changed = True
            if box_type in _MP4_CONTAINER_BOXES:
                child_start = position + header_size + (4 if box_type == b"meta" else 0)
                walk(child_start, position + size)

    walk(0, len(data))
    if changed:
        _mp4_rewrite_matching_mdhd_durations(data, duration_rewrites)
    return changed


def _mp4_normalize_stts_box(data: bytearray, position: int, size: int, header_size: int, timescale: int | None) -> tuple[int, int] | None:
    payload = position + header_size
    cursor = payload + 4
    if cursor + 4 > position + size:
        return None
    entry_count = int.from_bytes(data[cursor : cursor + 4], "big")
    cursor += 4
    entries: list[tuple[int, int, int]] = []
    weights: dict[int, int] = {}
    old_duration = 0
    for _ in range(entry_count):
        if cursor + 8 > position + size:
            return None
        count = int.from_bytes(data[cursor : cursor + 4], "big")
        delta_offset = cursor + 4
        delta = int.from_bytes(data[delta_offset : delta_offset + 4], "big")
        cursor += 8
        if count > 0 and delta > 0:
            weights[delta] = weights.get(delta, 0) + count
            old_duration += count * delta
        entries.append((count, delta, delta_offset))
    if not weights:
        return None
    nominal = max(weights.items(), key=lambda item: (item[1], -item[0]))[0]
    threshold = max(nominal * 4, nominal + (timescale or 0))
    new_duration = 0
    changed = False
    for count, delta, delta_offset in entries:
        replacement = nominal if delta > threshold else delta
        if replacement != delta:
            data[delta_offset : delta_offset + 4] = int(replacement).to_bytes(4, "big")
            changed = True
        new_duration += count * replacement
    if not changed or new_duration <= 0 or new_duration >= old_duration:
        return None
    return old_duration, new_duration


def _mp4_first_mdhd_timescale(data: bytes | bytearray) -> int | None:
    for position, size, box_type, header_size in _mp4_boxes(data):
        if box_type in _MP4_CONTAINER_BOXES:
            value = _mp4_first_mdhd_timescale_in(data, position + header_size + (4 if box_type == b"meta" else 0), position + size)
            if value:
                return value
    return None


def _mp4_first_mdhd_timescale_in(data: bytes | bytearray, start: int, end: int) -> int | None:
    for position, size, box_type, header_size in _mp4_boxes(data, start, end):
        if box_type == b"mdhd":
            payload = position + header_size
            version = data[payload]
            cursor = payload + 4
            if version == 1:
                timescale_offset = cursor + 16
            else:
                timescale_offset = cursor + 8
            if timescale_offset + 4 <= position + size:
                return int.from_bytes(data[timescale_offset : timescale_offset + 4], "big")
        if box_type in _MP4_CONTAINER_BOXES:
            value = _mp4_first_mdhd_timescale_in(data, position + header_size + (4 if box_type == b"meta" else 0), position + size)
            if value:
                return value
    return None


def _mp4_rewrite_matching_mdhd_durations(data: bytearray, rewrites: list[tuple[int, int]]) -> None:
    rewrite_map = {old: new for old, new in rewrites if old > 0 and new > 0}
    if not rewrite_map:
        return

    def walk(start: int, end: int) -> None:
        for position, size, box_type, header_size in _mp4_boxes(data, start, end):
            if box_type == b"mdhd":
                payload = position + header_size
                version = data[payload]
                cursor = payload + 4
                if version == 1:
                    duration_offset = cursor + 20
                    duration_size = 8
                else:
                    duration_offset = cursor + 12
                    duration_size = 4
                if duration_offset + duration_size <= position + size:
                    old_duration = int.from_bytes(data[duration_offset : duration_offset + duration_size], "big")
                    new_duration = rewrite_map.get(old_duration)
                    if new_duration is not None:
                        data[duration_offset : duration_offset + duration_size] = int(new_duration).to_bytes(duration_size, "big")
            if box_type in _MP4_CONTAINER_BOXES:
                child_start = position + header_size + (4 if box_type == b"meta" else 0)
                walk(child_start, position + size)

    walk(0, len(data))


def _mp4_trun_composition_offset_positions(data: bytes | bytearray, position: int, size: int, header_size: int) -> list[tuple[int, int, bool]]:
    payload = position + header_size
    version = data[payload]
    flags = int.from_bytes(data[payload + 1 : payload + 4], "big")
    if not flags & 0x800:
        return []
    cursor = payload + 4
    if cursor + 4 > position + size:
        return []
    sample_count = int.from_bytes(data[cursor : cursor + 4], "big")
    cursor += 4
    if flags & 0x001:
        cursor += 4
    if flags & 0x004:
        cursor += 4
    samples: list[tuple[int, int, bool]] = []
    cto_signed = version == 1
    for _ in range(sample_count):
        if flags & 0x100:
            cursor += 4
        if flags & 0x200:
            cursor += 4
        if flags & 0x400:
            cursor += 4
        if flags & 0x800:
            if cursor + 4 > position + size:
                return samples
            cto_offset = cursor
            cto_value = int.from_bytes(data[cursor : cursor + 4], "big", signed=cto_signed)
            cursor += 4
            samples.append((cto_offset, cto_value, cto_signed))
    return samples


def _mp4_trun_sample_timing_positions(data: bytes | bytearray, position: int, size: int, header_size: int) -> list[tuple[int, int, int | None, int | None, bool]]:
    payload = position + header_size
    version = data[payload]
    flags = int.from_bytes(data[payload + 1 : payload + 4], "big")
    if not flags & 0x100:
        return []
    cursor = payload + 4
    if cursor + 4 > position + size:
        return []
    sample_count = int.from_bytes(data[cursor : cursor + 4], "big")
    cursor += 4
    if flags & 0x001:
        cursor += 4
    if flags & 0x004:
        cursor += 4
    samples: list[tuple[int, int, int | None, int | None, bool]] = []
    for _ in range(sample_count):
        duration_offset: int | None = None
        duration_value: int | None = None
        cto_offset: int | None = None
        cto_value: int | None = None
        cto_signed = version == 1
        if flags & 0x100:
            if cursor + 4 > position + size:
                return samples
            duration_offset = cursor
            duration_value = int.from_bytes(data[cursor : cursor + 4], "big")
            cursor += 4
        if flags & 0x200:
            cursor += 4
        if flags & 0x400:
            cursor += 4
        if flags & 0x800:
            if cursor + 4 > position + size:
                return samples
            cto_offset = cursor
            cto_value = int.from_bytes(data[cursor : cursor + 4], "big", signed=cto_signed)
            cursor += 4
        if duration_offset is not None and duration_value is not None:
            samples.append((duration_offset, duration_value, cto_offset, cto_value, cto_signed))
    return samples


def _mp4_trun_duration(data: bytes | bytearray, position: int, size: int, header_size: int) -> int:
    payload = position + header_size
    flags = int.from_bytes(data[payload + 1 : payload + 4], "big")
    cursor = payload + 4
    if cursor + 4 > position + size:
        return 0
    sample_count = int.from_bytes(data[cursor : cursor + 4], "big")
    cursor += 4
    if flags & 0x001:
        cursor += 4
    if flags & 0x004:
        cursor += 4
    total = 0
    for _ in range(sample_count):
        if flags & 0x100:
            if cursor + 4 > position + size:
                return total
            total += int.from_bytes(data[cursor : cursor + 4], "big")
            cursor += 4
        if flags & 0x200:
            cursor += 4
        if flags & 0x400:
            cursor += 4
        if flags & 0x800:
            cursor += 4
    return total


def _ensure_matching_keys(keys: list[RawKey], expected_kids: Iterable[str | None] | None) -> None:
    expected = _normalized_kids(expected_kids)
    if not expected:
        return
    if any(key.kid is None for key in keys):
        return
    provided = _normalized_kids(key.kid for key in keys)
    if expected.intersection(provided):
        return
    expected_text = ", ".join(sorted(expected))
    provided_text = ", ".join(sorted(provided)) if provided else "none"
    raise ValueError(f"no matching decryption key for selected stream KID(s): {expected_text}; provided KID(s): {provided_text}")


def _normalized_kids(values: Iterable[str | None] | None) -> set[str]:
    result: set[str] = set()
    for value in values or []:
        cleaned = _normalize_single_kid(value)
        if cleaned:
            result.add(cleaned)
    return result


def _normalize_single_kid(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = str(value).strip().lower().replace("-", "")
    if len(cleaned) != 32 or any(char not in "0123456789abcdef" for char in cleaned):
        return None
    return cleaned


def _validate_decryption_output(output: Path) -> None:
    try:
        size = output.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"decryption output was not created: {output}") from exc
    if size <= 0:
        raise RuntimeError(f"decryption output is empty: {output}")
    if _has_encrypted_sample_entry(output):
        raise RuntimeError(
            "decryption finished but output still contains encrypted sample entries; "
            "the stream may need section-by-section decryption or a track-id key"
        )


def _validate_decryption_size(input_path: Path, output: Path) -> None:
    try:
        input_size = input_path.stat().st_size
        output_size = output.stat().st_size
    except OSError:
        return
    if input_size < 1024 * 1024:
        return
    if output_size >= input_size * 0.2:
        return
    raise RuntimeError(
        "decryption output is unexpectedly small; "
        "the stream likely needs fragment-by-fragment decryption"
    )


def _ensure_decryption_free_space(input_path: Path, output: Path) -> None:
    try:
        input_size = input_path.stat().st_size
    except OSError:
        return
    if input_size <= 0:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    _ensure_output_free_space(input_size, output, "decryption output")


def _ensure_output_free_space(input_size: int, output: Path, label: str) -> None:
    if input_size <= 0:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    required = input_size + _disk_space_reserve(input_size)
    free = _free_bytes(output.parent)
    if free is None or free >= required:
        return
    raise RuntimeError(
        f"not enough disk space for {label}: "
        f"need about {_format_disk_bytes(required)}, free {_format_disk_bytes(free)} at {output.parent}"
    )


def _ensure_mux_free_space(input_paths: Iterable[Path], output: Path) -> None:
    total = 0
    for path in input_paths:
        try:
            total += Path(path).stat().st_size
        except OSError:
            continue
    if total <= 0:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    required = total + _disk_space_reserve(total)
    free = _free_bytes(output.parent)
    if free is None or free >= required:
        return
    raise RuntimeError(
        "not enough disk space for mux output: "
        f"need about {_format_disk_bytes(required)}, free {_format_disk_bytes(free)} at {output.parent}"
    )


def _paths_total_size(paths: Iterable[Path]) -> int:
    total = 0
    for path in paths:
        try:
            total += Path(path).stat().st_size
        except OSError:
            continue
    return total


def _disk_space_reserve(size: int) -> int:
    return max(64 * 1024 * 1024, min(1024 * 1024 * 1024, int(size * 0.02)))


def _free_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _format_disk_bytes(size: int) -> str:
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.2f}{unit}"
        value /= 1024


def _has_encrypted_sample_entry(path: Path) -> bool:
    try:
        size = path.stat().st_size
    except OSError:
        return False
    try:
        with path.open("rb") as file:
            position = 0
            while position + 8 <= size:
                file.seek(position)
                header = file.read(8)
                if len(header) < 8:
                    return False
                box_size = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                header_size = 8
                if box_size == 1:
                    extended = file.read(8)
                    if len(extended) < 8:
                        return False
                    box_size = int.from_bytes(extended, "big")
                    header += extended
                    header_size = 16
                elif box_size == 0:
                    box_size = size - position
                if box_size < header_size or position + box_size > size:
                    return False
                if box_type in {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"}:
                    payload = file.read(box_size - header_size)
                    if _box_tree_has_type(header + payload, {b"encv", b"enca"}):
                        return True
                position += box_size
    except OSError:
        return False
    return False


def _box_tree_has_type(data: bytes | bytearray, targets: set[bytes]) -> bool:
    def walk(start: int, end: int) -> bool:
        for position, size, box_type, header_size in _mp4_boxes(data, start, end):
            if box_type in targets:
                return True
            if box_type in _MP4_CONTAINER_BOXES or box_type == b"stsd":
                child_start = position + header_size
                if box_type == b"meta":
                    child_start += 4
                elif box_type == b"stsd":
                    child_start += 8
                if child_start < position + size and walk(child_start, position + size):
                    return True
        return False

    return walk(0, len(data))


def _parse_tenc_info(box: bytes | bytearray) -> tuple[bool, str, int] | None:
    for kid_offset, protected_offset, iv_size_offset in ((16, 14, 15), (15, 13, 14)):
        if len(box) < kid_offset + 16:
            continue
        is_protected = box[protected_offset] != 0
        iv_size = box[iv_size_offset]
        if is_protected and iv_size not in {0, 8, 16}:
            continue
        kid = bytes(box[kid_offset : kid_offset + 16]).hex()
        return is_protected, kid, kid_offset
    return None


def _tenc_default_kid_positions(data: bytes | bytearray) -> list[int]:
    positions: list[int] = []
    for position, size in _scan_box_ranges(data, b"tenc"):
        parsed = _parse_tenc_info(data[position : position + size])
        if parsed:
            _is_protected, _kid, relative = parsed
            positions.append(position + relative)
    return positions


def _scan_box_ranges(data: bytes | bytearray, box_type: bytes) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    while True:
        type_at = data.find(box_type, start)
        if type_at < 4:
            return ranges
        box_start = type_at - 4
        size = int.from_bytes(data[box_start:type_at], "big")
        header = 8
        if size == 1 and box_start + 16 <= len(data):
            size = int.from_bytes(data[box_start + 8 : box_start + 16], "big")
            header = 16
        if size >= header and box_start + size <= len(data):
            ranges.append((box_start, size))
            start = box_start + size
        else:
            start = type_at + 4


def _scan_boxes(data: bytes, box_type: bytes) -> list[bytes]:
    return [bytes(data[position : position + size]) for position, size in _scan_box_ranges(data, box_type)]


def _has_box_type(data: bytes, box_type: bytes) -> bool:
    return bool(_scan_boxes(data, box_type))


def _ffmpeg_concat_path(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def _select_muxer(muxer: str, output: Path) -> str:
    if muxer != "auto":
        return muxer
    if output.suffix.lower() in {".mkv", ".mka", ".mks"} and shutil.which("mkvmerge"):
        return "mkvmerge"
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    if shutil.which("mkvmerge"):
        return "mkvmerge"
    raise RuntimeError("No muxer found. Install ffmpeg or mkvmerge.")


def _mux_inputs_include_vvc(inputs: list[MuxInput]) -> bool:
    for item in inputs:
        if looks_like_h266(item.codecs, str(item.path), item.name):
            return True
        if item.media_type in {None, "video"} and _probe_input_is_vvc(item.path):
            return True
    return False


def _probe_input_is_vvc(path: Path) -> bool:
    executable = shutil.which("ffprobe")
    if not executable:
        return False
    try:
        completed = managed_run(
            [
                executable,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and "vvc" in completed.stdout.decode("utf-8", errors="replace").lower()


def _mkvmerge_recognizes_inputs(executable: str, inputs: list[MuxInput]) -> bool:
    for item in inputs:
        try:
            completed = managed_run(
                [executable, "--identification-format", "json", "--identify", str(item.path)],
                capture_output=True,
                timeout=30,
            )
            if completed.returncode > 1:
                return False
            identified = json.loads(completed.stdout.decode("utf-8", errors="replace"))
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            return False
        tracks = identified.get("tracks")
        if not tracks:
            return False
        expected_type = _mkvmerge_track_type(item.track_type_filter or item.media_type)
        if expected_type and not any(
            _mkvmerge_track_type(track.get("type")) == expected_type
            for track in tracks
            if isinstance(track, dict)
        ):
            return False
    return True


def _mkvmerge_track_type(media_type: object) -> str | None:
    normalized = str(media_type or "").strip().lower()
    if normalized == "subtitle":
        return "subtitles"
    if normalized in {"video", "audio", "subtitles"}:
        return normalized
    return None


def _run_external(args: list[str], action: str) -> None:
    result = managed_run(args, capture_output=True)
    if result.returncode == 0:
        return
    raw = result.stderr or result.stdout
    details = _command_details(raw.decode("utf-8", errors="replace") if raw else "")
    suffix = f": {details}" if details else ""
    raise RuntimeError(f"{action} command failed with status {result.returncode}{suffix}")


def _command_details(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    important = [
        line
        for line in lines
        if any(
            token in line.lower()
            for token in ("error", "failed", "failure", "cannot", "could not", "invalid", "warning", "skipping")
        )
    ]
    selected = important[-2:] or lines[-2:]
    return " | ".join(line[:220] for line in selected)


def _parse_key_value(raw: str, label: str) -> RawKey | None:
    original = raw
    raw = raw.strip().strip('"').strip("'").replace("-", "")
    if not raw:
        return None
    try:
        if ":" in raw:
            kid, key = raw.split(":", 1)
            return RawKey(kid=_clean_hex(kid, f"{label} KID"), key=_clean_hex(key, f"{label} KEY"))
        return RawKey(key=_clean_hex(raw, f"{label} KEY"))
    except ValueError as exc:
        raise ValueError(f"{exc}. Offending value: {original!r}") from exc


def _clean_hex(value: str, label: str = "hex value") -> str:
    value = value.strip().lower().replace("0x", "")
    if not value:
        raise ValueError(f"Empty {label}; expected 32 hex characters")
    if any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"Invalid {label}: {value}; expected only 0-9/a-f hex characters")
    if len(value) != 32:
        raise ValueError(f"Invalid {label} length: {len(value)}; expected 32 hex characters")
    return value
