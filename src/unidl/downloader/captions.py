"""HLS in-band caption rows and their selected, shared video dependency.

The master declaration is sufficient for the picker. No child playlist or
media is read until selection. Caption output never downloads its own copy of
the carrier and never invents a standalone subtitle URI.
"""
from __future__ import annotations

import contextlib
import copy
import json
import math
import mmap
import os
import shutil
import tempfile
from bisect import bisect_right
from dataclasses import fields, replace
from pathlib import Path

from .models import StreamInfo

CAPTION = "hls_instream_id"
SOURCE = "hls_caption_source"
GROUPS = "closed_caption_ids"
PRESERVE = "_unidl_preserve_captions"
_SHAPE = tuple(field.name for field in fields(StreamInfo) if field.name not in {"segments", "extra"})
_SOURCE_EXTRA = {
    "key_id", "key_ids", "media_sequence", "target_duration", "audio_id", "audio_ids",
    "video_id", "subtitle_id", "group_ids", GROUPS, "muxed_audio", "source_metadata",
    "_unidl_source_headers", PRESERVE,
}


def is_caption(stream) -> bool:
    extra = getattr(stream, "extra", {}) or {}
    return bool(extra.get(CAPTION)) and isinstance(extra.get(SOURCE), dict)


def _carrier(stream, group):
    return (stream.media_type == "video" and group in stream.extra.get(GROUPS, [])
            and not stream.extra.get("trick_mode")
            and str(stream.role or "").casefold() not in {"trickplay", "thumbnails"})


def _snapshot(source):
    return {
        **{name: getattr(source, name) for name in _SHAPE},
        "extra": {key: copy.deepcopy(value) for key, value in source.extra.items() if key in _SOURCE_EXTRA},
    }


def caption_source(stream):
    shape = stream.extra[SOURCE]
    extra = copy.deepcopy(shape.get("extra") or {})
    # Hydrated caption rows retain the carrier's transport/KIDs, not subtitle
    # encryption guesses. Inline segment objects stay shared and bounded.
    for key in ("key_id", "key_ids", "media_sequence", "target_duration", "_unidl_source_headers"):
        if key in stream.extra:
            extra[key] = stream.extra[key]
    values = {name: shape[name] for name in _SHAPE if name in shape}
    values.update(encrypted=stream.encrypted, encryption_scheme=stream.encryption_scheme,
                  duration=stream.duration)
    return StreamInfo(**values, segments=list(stream.segments), extra=extra)


def update_caption_source(caption, source):
    caption.extra[SOURCE] = _snapshot(source)
    caption.segments = list(source.segments)
    caption.duration = source.duration
    caption.encrypted, caption.encryption_scheme = source.encrypted, source.encryption_scheme
    caption.is_live = source.is_live
    for key in ("key_id", "key_ids"):
        caption.extra.pop(key, None)
        if key in source.extra:
            caption.extra[key] = copy.deepcopy(source.extra[key])


def add_caption_rows(streams, declarations):
    result = list(streams)
    seen = set()
    for attrs in declarations:
        instream = str(attrs.get("INSTREAM-ID") or "").upper()
        group = attrs.get("GROUP-ID")
        identity = (group, instream, attrs.get("LANGUAGE"))
        if identity in seen:
            continue
        seen.add(identity)
        if instream not in {"CC1", "CC2", "CC3", "CC4"} and not (
            instream.startswith("SERVICE") and instream[7:].isdigit() and 1 <= int(instream[7:]) <= 63
        ):
            continue
        candidates = [s for s in streams if _carrier(s, group)]
        if not candidates:
            continue
        source = min(candidates, key=lambda s: (s.bandwidth or float("inf"), s.url))
        result.append(replace(
            source, media_type="subtitle", id=f"{group}:{instream}", group_id=group,
            name=attrs.get("NAME"), language=attrs.get("LANGUAGE"), role=f"CC · Embedded · {instream}",
            bandwidth=None, codecs="CEA-608" if instream.startswith("CC") else "CEA-708",
            resolution=None, frame_rate=None, channels=None, video_range=None,
            extension="srt", size_bytes=None, segments=list(source.segments),
            extra={
                **source.extra, CAPTION: instream, SOURCE: _snapshot(source),
                "closed_captions": True, "subtitle_kind": "cc", "muxed_audio": False,
                "default": attrs.get("DEFAULT"), "autoselect": attrs.get("AUTOSELECT"),
                "characteristics": attrs.get("CHARACTERISTICS"),
            },
        ))
    return result


def bind_caption_sources(selected, inventory):
    """Prefer a selected video in the declared group; otherwise the lowest one.

    Called before hydration/licensing and again before native delivery. The
    output selection is not expanded with a hidden video row.
    """
    bindings = {}
    for caption in selected:
        if not is_caption(caption):
            continue
        group = caption.group_id
        candidates = [s for s in inventory if _carrier(s, group)
                      and s.original_url == caption.original_url]
        preferred = [s for s in selected if any(s is candidate for candidate in candidates)]
        source = min(preferred or candidates, key=lambda s: (s.bandwidth or float("inf"), s.url)) if candidates else caption_source(caption)
        source.extra[PRESERVE] = True
        # Preserve already hydrated data when re-binding the same rendition.
        same = source.url == caption.url
        caption.extra[SOURCE] = _snapshot(source)
        caption.url, caption.original_url = source.url, source.original_url
        if source.segments or not same or not caption.segments:
            caption.segments = list(source.segments)
            caption.duration = source.duration
            caption.encrypted, caption.encryption_scheme = source.encrypted, source.encryption_scheme
            for key in ("key_id", "key_ids"):
                caption.extra.pop(key, None)
                if key in source.extra:
                    caption.extra[key] = copy.deepcopy(source.extra[key])
        bindings[id(caption)] = source
    return bindings


def validate_caption(stream):
    if stream.is_live:
        raise ValueError("In-band caption extraction currently requires finite VOD.")
    if stream.extra[CAPTION] not in {"CC1", "CC3"}:
        raise ValueError(f"In-band {stream.extra[CAPTION]} extraction is not supported by the current caption decoder; no other CC channel will be substituted.")


def restamp_caption_carrier(section, output, start, duration, *, normalize_composition_offsets=False):
    """Move all tracks by one time offset, respecting each mdhd timescale."""
    from .mp4_metadata import normalize_empty_c608_samples, walk_boxes
    from .postprocess import _mp4_normalize_large_composition_offsets, _rewrite_mp4_metadata

    def edit(data):
        normalize_empty_c608_samples(data)
        scales, video_ids = {}, []
        for p, size, kind, head in walk_boxes(data):
            if kind != b"trak":
                continue
            track_id = scale = None
            video = False
            for child, length, tag, header in walk_boxes(data, p + head, p + size):
                body, end = child + header, child + length
                if tag in {b"tkhd", b"mdhd"} and body < end:
                    offset = body + (20 if data[body] == 1 else 12)
                    if offset + 4 <= end:
                        value = int.from_bytes(data[offset:offset + 4], "big")
                        if tag == b"tkhd":
                            track_id = value
                        else:
                            scale = value
                if tag == b"hdlr" and body + 12 <= end:
                    video |= data[body + 8:body + 12] == b"vide"
            if track_id and scale:
                scales[track_id] = scale
                if video:
                    video_ids.append(track_id)
        records = []
        for p, size, kind, head in walk_boxes(data):
            if kind != b"traf":
                continue
            track_id, timing = None, None
            for child, length, tag, header in walk_boxes(data, p + head, p + size):
                body, end = child + header, child + length
                if tag == b"tfhd" and body + 8 <= end:
                    track_id = int.from_bytes(data[body + 4:body + 8], "big")
                if tag == b"tfdt" and body < end:
                    width = 8 if data[body] == 1 else 4
                    if body + 4 + width <= end:
                        timing = (body + 4, width, int.from_bytes(data[body + 4:body + 4 + width], "big"))
            if timing is not None:
                if track_id not in scales:
                    raise ValueError("Cannot restamp caption carrier with an unknown track timescale.")
                records.append((track_id, *timing))
        first = next((row for row in records if row[0] in video_ids), None)
        if first is None:
            raise ValueError("Cannot restamp caption carrier without a video decode timeline.")
        video_scale = scales[first[0]]
        source_zero = first[3] / video_scale
        target_zero = float(start or 0) / video_scale
        shift = target_zero - source_zero
        for track_id, position, width, value in records:
            updated = max(0, round(value + shift * scales[track_id]))
            data[position:position + width] = updated.to_bytes(width, "big")
        # sidx may use a third timescale; its earliest presentation time must
        # receive the same wall-clock shift, not video ticks.
        for p, size, kind, head in walk_boxes(data):
            if kind != b"sidx":
                continue
            body, end = p + head, p + size
            if body + 16 > end:
                continue
            scale = int.from_bytes(data[body + 8:body + 12], "big")
            width = 8 if data[body] == 1 else 4
            at = body + 12
            if at + width <= end and scale:
                value = int.from_bytes(data[at:at + width], "big")
                data[at:at + width] = max(0, round(value + shift * scale)).to_bytes(width, "big")
        if normalize_composition_offsets:
            _mp4_normalize_large_composition_offsets(data)
        return round((target_zero + float(duration or 0)) * video_scale)

    next_time = _rewrite_mp4_metadata(Path(section), Path(output), edit)
    return Path(output), next_time


def extract_caption(source: Path, output: Path, stream, *, raw=False):
    """Decode a real MP4 c608 track; never treat an absent track as success."""
    from .embedding import managed_run
    from .postprocess import _run_external

    validate_caption(stream)
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and ffprobe are required for embedded CC extraction.")
    probe = managed_run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)], capture_output=True, text=True, timeout=30)
    if probe.returncode:
        raise RuntimeError("Could not inspect the downloaded caption carrier.")
    document = json.loads(probe.stdout)
    tracks = [row for row in document.get("streams", []) if row.get("codec_name") == "eia_608" and row.get("codec_tag_string") == "c608"]
    if len(tracks) != 1:
        raise RuntimeError("Expected one MP4 c608 caption track; this carrier needs a different caption extraction method.")
    output = output.with_suffix(".mov" if raw else ".srt")
    output.parent.mkdir(parents=True, exist_ok=True)
    source_offset = float(document.get("format", {}).get("start_time") or 0)
    if not math.isfinite(source_offset):
        raise ValueError("Invalid caption carrier start time.")
    fd, name = tempfile.mkstemp(prefix=".unidl-cc-", suffix=output.suffix, dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    compact = temporary.with_suffix(".source.mov")
    command = [ffmpeg, "-v", "error", "-nostdin", "-y", "-discard:v", "all", "-discard:a", "all"]
    if not raw:
        command.extend(["-data_field", "0" if stream.extra[CAPTION] == "CC1" else "1"])
    try:
        isolated = _compact_c608(source, compact, int(tracks[0].get("id") or "0", 0))
        command.extend(["-copyts", "-i", str(compact if isolated else source),
                        "-map", "0:s:0" if isolated else f"0:{tracks[0]['index']}", "-map_metadata", "-1",
                        "-c:s", "copy" if raw else "srt", "-avoid_negative_ts", "disabled", str(temporary)])
        _run_external(command, "embedded CC extraction", reject_demux_errors=True)
        if not temporary.is_file():
            raise RuntimeError("Embedded CC extraction did not produce an output file.")
        if not raw and source_offset:
            from .subtitles import _parse_text_subtitle, _write_subtitle

            cues = _parse_text_subtitle(temporary.read_text(encoding="utf-8-sig"), ".srt")
            shifted = [
                replace(cue, start=max(0, cue.start - source_offset), end=max(0, cue.end - source_offset))
                for cue in cues if cue.end > source_offset
            ]
            _write_subtitle(temporary, shifted, "srt")
        # Empty captions are valid (for example an opening with no speech).
        temporary.replace(output)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            compact.unlink(missing_ok=True)
    return output


def _compact_c608(source, output, track_id):
    """Copy only clear c608 payloads, never a video-sized normalization file.

    Apple uses all-zero c608 placeholders. Isolating the caption track lets us
    replace those with valid same-sized cdat padding without reading/decrypting
    video samples or causing FFmpeg to abort at the first empty caption.
    """
    from .cenc_fragment import _parse_tfhd, _parse_trun
    from .embedding import current_download_runtime
    from .mp4_metadata import walk_boxes
    from .postprocess import _mp4_boxes

    def box(kind, payload):
        return (len(payload) + 8).to_bytes(4, "big") + kind + payload

    with Path(source).open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
        roots = list(_mp4_boxes(data))
        if not any(kind == b"moof" for _, _, kind, _ in roots):
            return False  # A regular MOV can use FFmpeg's ordinary demuxer.
        default_size = None
        init = []
        for p, size, kind, head in roots:
            if kind == b"ftyp":
                init.append(bytes(data[p:p + size]))
            elif kind == b"moov":
                children = []
                for cp, length, tag, ch in _mp4_boxes(data, p + head, p + size):
                    payload = bytes(data[cp + ch:cp + length])
                    if tag == b"trak":
                        tkhd = next((row for row in walk_boxes(data, cp + ch, cp + length) if row[2] == b"tkhd"), None)
                        if tkhd is None:
                            continue
                        tp, ts, _, th = tkhd
                        at = tp + th + (20 if data[tp + th] == 1 else 12)
                        if at + 4 > tp + ts or int.from_bytes(data[at:at + 4], "big") != track_id:
                            continue
                    elif tag == b"mvex":
                        kept = []
                        for tp, ts, typ, th in _mp4_boxes(data, cp + ch, cp + length):
                            if typ == b"trex":
                                if ts < th + 24 or int.from_bytes(data[tp + th + 4:tp + th + 8], "big") != track_id:
                                    continue
                                default_size = int.from_bytes(data[tp + th + 16:tp + th + 20], "big") or None
                            kept.append(bytes(data[tp:tp + ts]))
                        payload = b"".join(kept)
                    children.append(box(tag, payload))
                init.append(box(b"moov", b"".join(children)))
        mdats = [(p + head, p + size) for p, size, kind, head in roots if kind == b"mdat"]
        starts = [p for p, _ in mdats]
        with Path(output).open("wb") as target:
            target.write(b"".join(init))
            for mp, ms, mk, mh in roots:
                if mk != b"moof":
                    continue
                runtime = current_download_runtime()
                if runtime is not None:
                    runtime.checkpoint()
                fragment = []
                caption_traf = None
                samples = []
                for tp, ts, tk, th in _mp4_boxes(data, mp + mh, mp + ms):
                    if tk == b"mfhd":
                        fragment.append(bytes(data[tp:tp + ts]))
                    if tk != b"traf":
                        continue
                    children = list(_mp4_boxes(data, tp + th, tp + ts))
                    tfhd_box = next((row for row in children if row[2] == b"tfhd"), None)
                    if tfhd_box is None:
                        continue
                    fp, fs, _, fh = tfhd_box
                    tfhd = _parse_tfhd(data, fp, fs, fh)
                    if tfhd is None or tfhd.track_id != track_id:
                        continue
                    flags = int.from_bytes(data[fp + fh:fp + fh + 4], "big") & 0xFFFFFF
                    if not flags & 0x020000 or flags & 1 or any(k in {b"senc", b"saiz", b"saio"} for _, _, k, _ in children):
                        raise ValueError("Unsupported c608 fragment offsets or protected caption payload.")
                    caption_traf = []
                    next_at = None
                    for p, size, kind, head in children:
                        if kind not in {b"tfhd", b"tfdt", b"trun"}:
                            continue
                        payload = bytearray(data[p + head:p + size])
                        if kind == b"trun":
                            count = int.from_bytes(payload[4:8], "big")
                            if count > 1_000_000:
                                raise ValueError("Caption fragment sample count exceeds safe bounds.")
                            trun = _parse_trun(data, p, size, head, tfhd.default_sample_size or default_size)
                            if trun is None:
                                raise ValueError("Unknown caption sample layout.")
                            at = mp + trun.data_offset if trun.data_offset is not None else next_at
                            if at is None or sum(trun.sample_sizes) > 16 * 1024 * 1024:
                                raise ValueError("Invalid caption fragment size/offset.")
                            part = []
                            for length in trun.sample_sizes:
                                mdat = bisect_right(starts, at) - 1
                                if mdat < 0 or at + length > mdats[mdat][1]:
                                    raise ValueError("Caption sample exceeds media-data bounds.")
                                value = bytes(data[at:at + length])
                                if length >= 10 and length <= 65536 and length % 2 == 0 and not any(value):
                                    value = length.to_bytes(4, "big") + b"cdat" + b"\x80\x80" * ((length - 8) // 2)
                                part.append(value)
                                at += length
                            next_at = at
                            # Give every trun an explicit moof-relative offset.
                            if int.from_bytes(payload[:4], "big") & 1:
                                payload[8:12] = b"\x00" * 4
                            else:
                                payload[:4] = (int.from_bytes(payload[:4], "big") | 1).to_bytes(4, "big")
                                payload[8:8] = b"\x00" * 4
                            samples.append(b"".join(part))
                        caption_traf.append((kind, payload))
                if caption_traf is None:
                    continue
                moof_size = 8 + sum(map(len, fragment)) + 8 + sum(len(value) + 8 for _, value in caption_traf)
                cursor = moof_size + 8
                index = 0
                rebuilt = []
                for kind, payload in caption_traf:
                    if kind == b"trun":
                        payload[8:12] = cursor.to_bytes(4, "big", signed=True)
                        cursor += len(samples[index])
                        index += 1
                    rebuilt.append(box(kind, payload))
                target.write(box(b"moof", b"".join(fragment) + box(b"traf", b"".join(rebuilt))))
                target.write(box(b"mdat", b"".join(samples)))
    return True
