from __future__ import annotations

import html
import re
import shutil
import subprocess  # noqa: F401 - retained as the module's patch seam for hosts/tests
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .embedding import managed_run
from .utils import unique_path


class SubtitleConversionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SubtitleCue:
    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class _Mp4Box:
    type: bytes
    start: int
    header_size: int
    end: int

    @property
    def data_start(self) -> int:
        return self.start + self.header_size


def convert_subtitle_file(
    input_path: str | Path,
    output_format: str = "srt",
    output_path: str | Path | None = None,
    auto_fix: bool = True,
    duration: float | None = None,
) -> Path:
    input_path = Path(input_path)
    target = _normalize_output_format(output_format)
    if target == "raw":
        return input_path
    if input_path.suffix.lower().lstrip(".") == target:
        return input_path

    output = Path(output_path) if output_path else unique_path(input_path.with_suffix(f".{target}"))
    output.parent.mkdir(parents=True, exist_ok=True)

    mp4_cues, is_mp4_subtitle = _parse_mp4_subtitle_file_info(input_path)
    if mp4_cues or is_mp4_subtitle:
        if auto_fix:
            mp4_cues = _fix_cues(mp4_cues)
            mp4_cues = _fit_cues_to_duration(mp4_cues, duration)
        _write_subtitle(output, mp4_cues, target)
        return output

    try:
        text = input_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise SubtitleConversionError(f"failed to read subtitle: {exc}") from exc

    cues = _parse_text_subtitle(text, input_path.suffix.lower())
    if cues:
        if auto_fix:
            cues = _fix_cues(cues)
            cues = _fit_cues_to_duration(cues, duration)
        _write_subtitle(output, cues, target)
        return output

    return _convert_with_ffmpeg(input_path, output, target)


def _normalize_output_format(value: str) -> str:
    normalized = (value or "srt").strip().lower()
    if normalized in {"srt", "vtt", "raw"}:
        return normalized
    raise SubtitleConversionError(f"unsupported subtitle format: {value}")


def _parse_text_subtitle(text: str, suffix: str = "") -> list[SubtitleCue]:
    if not text.strip():
        return []
    if _looks_like_ttml(text, suffix):
        return _parse_ttml(text)
    if _looks_like_webvtt(text, suffix):
        return _parse_vtt(text)
    if _looks_like_srt(text, suffix):
        return _parse_srt(text)
    return _parse_vtt(text)


def _parse_mp4_subtitle_file(path: Path) -> list[SubtitleCue]:
    cues, _is_subtitle = _parse_mp4_subtitle_file_info(path)
    return cues


def _parse_mp4_subtitle_file_info(path: Path) -> tuple[list[SubtitleCue], bool]:
    try:
        data = path.read_bytes()
    except OSError:
        return [], False
    if not _looks_like_mp4(data):
        return [], False
    if b"wvtt" in data:
        return _parse_mp4_webvtt(data), True
    if b"stpp" in data or b"ttml" in data:
        return _parse_mp4_ttml(data), True
    return [], False


def _looks_like_mp4(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp"


def _parse_mp4_webvtt(data: bytes) -> list[SubtitleCue]:
    timescale = _mp4_mdhd_timescale(data) or 1000
    trex_duration, trex_size = _mp4_trex_defaults(data)
    pending_samples: list[tuple[int, int, int]] = []
    cues: list[SubtitleCue] = []
    for box in _iter_mp4_boxes(data):
        if box.type == b"moof":
            pending_samples = _mp4_fragment_samples(data, box, trex_duration, trex_size)
            continue
        if box.type != b"mdat" or not pending_samples:
            continue
        cursor = box.data_start
        for decode_time, duration, size in pending_samples:
            if size <= 0 or cursor + size > box.end:
                cursor += max(0, size)
                continue
            sample = data[cursor : cursor + size]
            cursor += size
            cue = _parse_webvtt_sample(sample, decode_time / timescale, (decode_time + duration) / timescale)
            if cue is not None:
                cues.append(cue)
        pending_samples = []
    return cues


def _parse_mp4_ttml(data: bytes) -> list[SubtitleCue]:
    timescale = _mp4_mdhd_timescale(data) or 1000
    trex_duration, trex_size = _mp4_trex_defaults(data)
    pending_samples: list[tuple[int, int, int]] = []
    cues: list[SubtitleCue] = []
    for box in _iter_mp4_boxes(data):
        if box.type == b"moof":
            pending_samples = _mp4_fragment_samples(data, box, trex_duration, trex_size)
            continue
        if box.type != b"mdat" or not pending_samples:
            continue
        cursor = box.data_start
        for decode_time, duration, size in pending_samples:
            if size <= 0 or cursor + size > box.end:
                cursor += max(0, size)
                continue
            sample = data[cursor : cursor + size]
            cursor += size
            sample_cues = _parse_ttml_sample(sample, decode_time / timescale, (decode_time + duration) / timescale)
            cues.extend(sample_cues)
        pending_samples = []
    return cues


def _iter_mp4_boxes(data: bytes, start: int = 0, end: int | None = None) -> list[_Mp4Box]:
    limit = len(data) if end is None else min(end, len(data))
    boxes: list[_Mp4Box] = []
    offset = start
    while offset + 8 <= limit:
        size = int.from_bytes(data[offset : offset + 4], "big")
        box_type = data[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > limit:
                break
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = limit - offset
        if size < header_size or offset + size > limit:
            break
        boxes.append(_Mp4Box(box_type, offset, header_size, offset + size))
        offset += size
    return boxes


def _find_mp4_boxes(data: bytes, box_type: bytes, start: int = 0, end: int | None = None) -> list[_Mp4Box]:
    found: list[_Mp4Box] = []
    for box in _iter_mp4_boxes(data, start, end):
        if box.type == box_type:
            found.append(box)
        if box.type in {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"mvex", b"moof", b"traf", b"vttc"}:
            found.extend(_find_mp4_boxes(data, box_type, box.data_start, box.end))
    return found


def _mp4_mdhd_timescale(data: bytes) -> int | None:
    for box in _find_mp4_boxes(data, b"mdhd"):
        payload = data[box.data_start : box.end]
        if len(payload) < 16:
            continue
        version = payload[0]
        offset = 20 if version == 1 else 12
        if len(payload) >= offset + 4:
            timescale = int.from_bytes(payload[offset : offset + 4], "big")
            if timescale > 0:
                return timescale
    return None


def _mp4_trex_defaults(data: bytes) -> tuple[int | None, int | None]:
    for box in _find_mp4_boxes(data, b"trex"):
        payload = data[box.data_start : box.end]
        if len(payload) >= 20:
            return (
                int.from_bytes(payload[12:16], "big") or None,
                int.from_bytes(payload[16:20], "big") or None,
            )
    return None, None


def _mp4_fragment_samples(
    data: bytes,
    moof: _Mp4Box,
    trex_duration: int | None,
    trex_size: int | None,
) -> list[tuple[int, int, int]]:
    samples: list[tuple[int, int, int]] = []
    for traf in [box for box in _iter_mp4_boxes(data, moof.data_start, moof.end) if box.type == b"traf"]:
        default_duration = trex_duration
        default_size = trex_size
        base_decode_time = 0
        for child in _iter_mp4_boxes(data, traf.data_start, traf.end):
            payload = data[child.data_start : child.end]
            if child.type == b"tfhd":
                parsed_duration, parsed_size = _parse_tfhd_defaults(payload)
                default_duration = parsed_duration or default_duration
                default_size = parsed_size or default_size
            elif child.type == b"tfdt":
                base_decode_time = _parse_tfdt_time(payload)
            elif child.type == b"trun":
                samples.extend(_parse_trun_samples(payload, base_decode_time, default_duration, default_size))
    return samples


def _parse_tfhd_defaults(payload: bytes) -> tuple[int | None, int | None]:
    if len(payload) < 8:
        return None, None
    flags = int.from_bytes(payload[1:4], "big")
    offset = 8
    if flags & 0x000001:
        offset += 8
    if flags & 0x000002:
        offset += 4
    default_duration = None
    default_size = None
    if flags & 0x000008 and len(payload) >= offset + 4:
        default_duration = int.from_bytes(payload[offset : offset + 4], "big")
        offset += 4
    if flags & 0x000010 and len(payload) >= offset + 4:
        default_size = int.from_bytes(payload[offset : offset + 4], "big")
    return default_duration, default_size


def _parse_tfdt_time(payload: bytes) -> int:
    if len(payload) < 8:
        return 0
    version = payload[0]
    if version == 1 and len(payload) >= 12:
        return int.from_bytes(payload[4:12], "big")
    return int.from_bytes(payload[4:8], "big")


def _parse_trun_samples(
    payload: bytes,
    base_decode_time: int,
    default_duration: int | None,
    default_size: int | None,
) -> list[tuple[int, int, int]]:
    if len(payload) < 8:
        return []
    flags = int.from_bytes(payload[1:4], "big")
    sample_count = int.from_bytes(payload[4:8], "big")
    offset = 8
    if flags & 0x000001:
        offset += 4
    if flags & 0x000004:
        offset += 4
    samples: list[tuple[int, int, int]] = []
    cursor_time = base_decode_time
    for _ in range(sample_count):
        duration = default_duration or 0
        size = default_size or 0
        if flags & 0x000100:
            if len(payload) < offset + 4:
                break
            duration = int.from_bytes(payload[offset : offset + 4], "big")
            offset += 4
        if flags & 0x000200:
            if len(payload) < offset + 4:
                break
            size = int.from_bytes(payload[offset : offset + 4], "big")
            offset += 4
        if flags & 0x000400:
            offset += 4
        if flags & 0x000800:
            offset += 4
        samples.append((cursor_time, duration, size))
        cursor_time += duration
    return samples


def _parse_webvtt_sample(sample: bytes, start: float, end: float) -> SubtitleCue | None:
    for box in _iter_mp4_boxes(sample):
        if box.type == b"vtte":
            return None
        if box.type != b"vttc":
            continue
        payloads: list[str] = []
        for child in _iter_mp4_boxes(sample, box.data_start, box.end):
            if child.type == b"payl":
                payloads.append(sample[child.data_start : child.end].decode("utf-8", errors="replace"))
        text = _clean_subtitle_text("\n".join(payloads))
        if text and end > start:
            return SubtitleCue(start, end, text)
    return None


def _parse_ttml_sample(sample: bytes, start: float, end: float) -> list[SubtitleCue]:
    text = _decode_subtitle_payload(sample)
    if not text or "<" not in text:
        return []
    cues = _parse_ttml(text)
    if cues:
        return cues
    text_value = _clean_subtitle_text(text)
    if text_value and end > start:
        return [SubtitleCue(start, end, text_value)]
    return []


def _decode_subtitle_payload(data: bytes) -> str:
    stripped = data.strip(b"\x00\r\n\t ")
    if not stripped:
        return ""
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            text = stripped.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "<" in text or text.strip():
            return text.strip("\ufeff\x00")
    return stripped.decode("utf-8", errors="replace").strip("\ufeff\x00")


def _looks_like_ttml(text: str, suffix: str) -> bool:
    head = text[:500].lower()
    return suffix in {".ttml", ".dfxp", ".xml"} or "<tt" in head or "timedtext" in head


def _looks_like_webvtt(text: str, suffix: str) -> bool:
    head = text[:500].lstrip("\ufeff").lstrip().lower()
    return suffix in {".vtt", ".webvtt"} or head.startswith("webvtt") or "-->" in text


def _looks_like_srt(text: str, suffix: str) -> bool:
    return suffix == ".srt" or bool(re.search(r"\d{1,2}:\d{2}:\d{2},\d{1,3}\s*-->", text))


def _parse_vtt(text: str) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    base_mpegts: int | None = None
    for document in _vtt_documents(text):
        document_cues, mpegts, local_time = _parse_vtt_document(document)
        if mpegts is None:
            cues.extend(document_cues)
            continue
        if base_mpegts is None:
            base_mpegts = mpegts
        offset = (mpegts - base_mpegts) / 90000 - (local_time or 0.0)
        cues.extend(SubtitleCue(cue.start + offset, cue.end + offset, cue.text) for cue in document_cues)
    return cues


def _vtt_documents(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    parts = re.split(r"(?=^WEBVTT\b)", normalized, flags=re.IGNORECASE | re.MULTILINE)
    documents = [part for part in parts if part.strip()]
    return documents or [normalized]


def _parse_vtt_document(text: str) -> tuple[list[SubtitleCue], int | None, float | None]:
    lines = text.split("\n")
    cues: list[SubtitleCue] = []
    mpegts: int | None = None
    local_time: float | None = None
    index = 0
    while index < len(lines):
        line = lines[index].strip("\ufeff")
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("X-TIMESTAMP-MAP"):
            parsed_mpegts, parsed_local_time = _parse_vtt_timestamp_map(stripped)
            mpegts = parsed_mpegts if parsed_mpegts is not None else mpegts
            local_time = parsed_local_time if parsed_local_time is not None else local_time
            index += 1
            continue
        if not stripped or upper.startswith("WEBVTT"):
            index += 1
            continue
        if upper.startswith(("NOTE", "STYLE", "REGION")):
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue

        timing = stripped if "-->" in stripped else ""
        if not timing and index + 1 < len(lines) and "-->" in lines[index + 1]:
            index += 1
            timing = lines[index].strip()
        if "-->" not in timing:
            index += 1
            continue

        start, end = _parse_timing_line(timing)
        index += 1
        payload: list[str] = []
        while index < len(lines) and lines[index].strip():
            payload.append(lines[index])
            index += 1
        text_value = _clean_subtitle_text("\n".join(payload))
        if text_value and end > start:
            cues.append(SubtitleCue(start, end, text_value))
    return cues, mpegts, local_time


def _parse_vtt_timestamp_map(line: str) -> tuple[int | None, float | None]:
    mpegts: int | None = None
    local_time: float | None = None
    match = re.search(r"\bMPEGTS\s*:\s*(\d+)", line, flags=re.IGNORECASE)
    if match:
        try:
            mpegts = int(match.group(1))
        except ValueError:
            mpegts = None
    match = re.search(r"\bLOCAL\s*:\s*([^,]+)", line, flags=re.IGNORECASE)
    if match:
        local_time = _parse_subtitle_time(match.group(1).strip())
    return mpegts, local_time


def _parse_srt(text: str) -> list[SubtitleCue]:
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    cues: list[SubtitleCue] = []
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        timing_index = next((idx for idx, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        start, end = _parse_timing_line(lines[timing_index].strip())
        payload = lines[timing_index + 1 :]
        text_value = _clean_subtitle_text("\n".join(payload))
        if text_value and end > start:
            cues.append(SubtitleCue(start, end, text_value))
    return cues


def _parse_ttml(text: str) -> list[SubtitleCue]:
    documents = _ttml_documents(text)
    cues: list[SubtitleCue] = []
    for document in documents:
        try:
            root = ET.fromstring(document)
        except ET.ParseError:
            continue
        for node in root.iter():
            if _local_name(node.tag) != "p":
                continue
            begin = _parse_ttml_time(_attr(node, "begin"))
            end = _parse_ttml_time(_attr(node, "end"))
            dur = _parse_ttml_time(_attr(node, "dur"))
            if begin is None:
                continue
            if end is None and dur is not None:
                end = begin + dur
            if end is None or end <= begin:
                continue
            text_value = _clean_subtitle_text(_ttml_node_text(node))
            if text_value:
                cues.append(SubtitleCue(begin, end, text_value))
    return cues


def _ttml_documents(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.count("<tt") <= 1:
        return [stripped]
    docs = re.findall(r"<tt\b.*?</tt>", stripped, flags=re.IGNORECASE | re.DOTALL)
    return docs or [stripped]


def _attr(node: ET.Element, name: str) -> str | None:
    if name in node.attrib:
        return node.attrib[name]
    for key, value in node.attrib.items():
        if _local_name(key) == name:
            return value
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _ttml_node_text(node: ET.Element) -> str:
    parts: list[str] = []

    def walk(element: ET.Element) -> None:
        if element.text:
            parts.append(element.text)
        for child in element:
            if _local_name(child.tag).lower() == "br":
                parts.append("\n")
            walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(node)
    return "".join(parts)


def _parse_timing_line(line: str) -> tuple[float, float]:
    left, right = line.split("-->", 1)
    start = _parse_subtitle_time(left.strip())
    end = _parse_subtitle_time(right.strip().split()[0])
    if start is None or end is None:
        raise SubtitleConversionError(f"invalid subtitle timing: {line}")
    return start, end


def _parse_subtitle_time(value: str) -> float | None:
    value = value.strip().replace(",", ".")
    match = re.match(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?$", value)
    if not match:
        return _parse_ttml_time(value)
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    millis = int((match.group(4) or "0").ljust(3, "0")[:3])
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _parse_ttml_time(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip().replace(",", ".")
    if value.endswith("ms"):
        try:
            return float(value[:-2]) / 1000
        except ValueError:
            return None
    if value.endswith("s"):
        try:
            return float(value[:-1])
        except ValueError:
            return None
    match = re.match(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?$", value)
    if match:
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        millis = int((match.group(4) or "0").ljust(3, "0")[:3])
        return hours * 3600 + minutes * 60 + seconds + millis / 1000
    match = re.match(r"^(\d+):(\d{2}):(\d{2}):(\d{1,2})$", value)
    if match:
        hours, minutes, seconds, frames = (int(part) for part in match.groups())
        return hours * 3600 + minutes * 60 + seconds + frames / 30
    try:
        return float(value)
    except ValueError:
        return None


def _clean_subtitle_text(value: str) -> str:
    value = re.sub(r"</?(?:c|v|lang|ruby|rt|b|i|u|font|span)\b[^>]*>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _fix_cues(cues: list[SubtitleCue]) -> list[SubtitleCue]:
    fixed: list[SubtitleCue] = []
    seen: set[tuple[int, int, str]] = set()
    for cue in sorted(cues, key=lambda item: (item.start, item.end, item.text)):
        start = max(0.0, cue.start)
        end = max(start + 0.001, cue.end)
        key = (round(start * 1000), round(end * 1000), cue.text)
        if key in seen:
            continue
        seen.add(key)
        normalized = SubtitleCue(start, end, cue.text)
        if fixed and fixed[-1].text == normalized.text and normalized.start <= fixed[-1].end + 0.35:
            previous = fixed[-1]
            fixed[-1] = SubtitleCue(previous.start, max(previous.end, normalized.end), previous.text)
            continue
        fixed.append(normalized)
    return _collapse_incremental_cues(fixed)


def _fit_cues_to_duration(cues: list[SubtitleCue], duration: float | None) -> list[SubtitleCue]:
    if not cues or duration is None or duration <= 0:
        return cues
    first_start = min(cue.start for cue in cues)
    last_end = max(cue.end for cue in cues)
    tolerance = max(30.0, min(300.0, duration * 0.05))
    if first_start > 30.0 and last_end > duration + tolerance and last_end - first_start <= duration + tolerance:
        cues = [SubtitleCue(max(0.0, cue.start - first_start), max(0.001, cue.end - first_start), cue.text) for cue in cues]
    return [
        SubtitleCue(cue.start, min(cue.end, duration), cue.text)
        for cue in cues
        if cue.start < duration and min(cue.end, duration) > cue.start
    ]


def _collapse_incremental_cues(cues: list[SubtitleCue]) -> list[SubtitleCue]:
    collapsed: list[SubtitleCue] = []
    group_start: float | None = None
    for index, cue in enumerate(cues):
        next_cue = cues[index + 1] if index + 1 < len(cues) else None
        if next_cue and _is_incremental_prefix(cue, next_cue):
            if group_start is None:
                group_start = cue.start
            continue
        start = group_start if group_start is not None else cue.start
        end = cue.end
        if group_start is not None and next_cue and next_cue.start > end:
            end = next_cue.start
        collapsed.append(SubtitleCue(start, max(start + 0.001, end), cue.text))
        group_start = None
    return collapsed


def _is_incremental_prefix(cue: SubtitleCue, next_cue: SubtitleCue) -> bool:
    current = _subtitle_compare_text(cue.text)
    following = _subtitle_compare_text(next_cue.text)
    if not current or len(current) >= len(following):
        return False
    if not following.startswith(current):
        return False
    if next_cue.start > cue.end + 0.5 and next_cue.start - cue.start > 1.0:
        return False
    return cue.end - cue.start <= 1.5


def _subtitle_compare_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _write_subtitle(path: Path, cues: list[SubtitleCue], output_format: str) -> None:
    if output_format == "srt":
        path.write_text(_compose_srt(cues), encoding="utf-8")
        return
    if output_format == "vtt":
        path.write_text(_compose_vtt(cues), encoding="utf-8")
        return
    raise SubtitleConversionError(f"unsupported subtitle format: {output_format}")


def _compose_srt(cues: list[SubtitleCue]) -> str:
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(f"{index}\n{_format_srt_time(cue.start)} --> {_format_srt_time(cue.end)}\n{cue.text}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _compose_vtt(cues: list[SubtitleCue]) -> str:
    blocks = ["WEBVTT"]
    for cue in cues:
        blocks.append(f"{_format_vtt_time(cue.start)} --> {_format_vtt_time(cue.end)}\n{cue.text}")
    return "\n\n".join(blocks) + "\n"


def _format_srt_time(seconds: float) -> str:
    hours, minutes, secs, millis = _time_parts(seconds)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _format_vtt_time(seconds: float) -> str:
    hours, minutes, secs, millis = _time_parts(seconds)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _time_parts(seconds: float) -> tuple[int, int, int, int]:
    total_millis = max(0, int(round(seconds * 1000)))
    total_seconds, millis = divmod(total_millis, 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return hours, minutes, secs, millis


def _convert_with_ffmpeg(input_path: Path, output_path: Path, output_format: str) -> Path:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise SubtitleConversionError("subtitle conversion failed and ffmpeg was not found")
    args = [executable, "-hide_banner", "-y", "-i", str(input_path), str(output_path)]
    completed = managed_run(args, capture_output=True)
    if completed.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        error = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SubtitleConversionError(f"ffmpeg subtitle conversion failed: {error}")
    return output_path
