from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from ..ism_live import ism_segment_extra, ism_segment_index
from ..models import SegmentInfo, StreamInfo
from ..utils import join_uri
from .ism_init import build_ism_init_segment, extract_playready_kids


def parse_ism(uri: str, text: str) -> list[StreamInfo]:
    root = ET.fromstring(text)
    if _local(root.tag) != "SmoothStreamingMedia":
        raise ValueError("Not a Smooth Streaming manifest.")

    root_timescale = _int_or_none(root.attrib.get("TimeScale")) or 10_000_000
    root_duration_ticks = _int_or_none(root.attrib.get("Duration"))
    root_duration = root_duration_ticks / root_timescale if root_duration_ticks else None
    is_live = root.attrib.get("IsLive", "false").lower() == "true"
    protection = _first_child(root, "Protection")
    protection_header = _protection_header(protection)
    key_ids = extract_playready_kids(protection_header)
    protected = protection is not None
    encryption_scheme = "CENC" if protected else None
    streams: list[StreamInfo] = []
    next_track_id = 1

    for stream_index in _children(root, "StreamIndex"):
        stream_type = (stream_index.attrib.get("Type") or "").lower()
        media_type = _media_type(stream_type)
        if media_type == "unknown":
            continue
        track_key_ids = key_ids if media_type != "subtitle" else []
        track_kid = track_key_ids[0] if track_key_ids else None
        stream_timescale = _int_or_none(stream_index.attrib.get("TimeScale")) or root_timescale
        chunk_starts, chunk_durations = _chunks(stream_index, stream_timescale, root_duration_ticks)
        language = stream_index.attrib.get("Language")
        name = stream_index.attrib.get("Name") or stream_type
        stream_url_template = stream_index.attrib.get("Url")

        for quality in _children(stream_index, "QualityLevel"):
            track_id = _quality_track_id(quality, next_track_id, is_live=is_live)
            next_track_id = max(next_track_id, track_id) + 1
            bitrate = _int_or_none(quality.attrib.get("Bitrate"))
            width = quality.attrib.get("MaxWidth") or quality.attrib.get("Width")
            height = quality.attrib.get("MaxHeight") or quality.attrib.get("Height")
            resolution = f"{width}x{height}" if width and height and width != "0" else None
            codec = _codec_from_quality(quality)
            url_template = quality.attrib.get("Url") or stream_url_template
            segments = _segments(
                base_uri=uri,
                url_template=url_template,
                bitrate=bitrate,
                starts=chunk_starts,
                durations=chunk_durations,
                index_by_timeline=is_live,
                encrypted=protected and media_type != "subtitle",
                encryption_scheme=encryption_scheme,
                key_id=track_kid,
            )
            init_error = None
            if segments and media_type in {"video", "audio"}:
                try:
                    init_data = build_ism_init_segment(
                        {
                            "track_id": track_id,
                            "media_type": media_type,
                            "timescale": stream_timescale,
                            "duration_ticks": int(round((sum(chunk_durations) if chunk_durations else (root_duration or 0)) * stream_timescale)),
                            "language": language,
                            "width": _int_or_none(width),
                            "height": _int_or_none(height),
                            "codec": codec,
                            "codec_private_data": quality.attrib.get("CodecPrivateData"),
                            "bitrate": bitrate,
                            "channels": _int_or_none(quality.attrib.get("Channels")),
                            "bits": _int_or_none(quality.attrib.get("BitsPerSample")),
                            "sample_rate": _int_or_none(quality.attrib.get("SamplingRate")),
                            "encrypted": protected and media_type != "subtitle",
                            "kid": track_kid,
                            "protection_header": protection_header,
                        }
                    )
                    segments.insert(
                        0,
                        SegmentInfo(
                            url=f"ism-init://{media_type}/{bitrate or 'unknown'}",
                            index=-1,
                            data=init_data,
                            encrypted=protected and media_type != "subtitle",
                            encryption_scheme=encryption_scheme,
                            key_id=track_kid,
                        ),
                    )
                except Exception as exc:
                    init_error = str(exc)
            stream = StreamInfo(
                manifest_type="ism",
                media_type=media_type,
                url=segments[0].url if segments else uri,
                original_url=uri,
                id=quality.attrib.get("Index") or f"{name}-{len(streams)}",
                group_id=name,
                name=name,
                language=language if language and len(language) <= 8 else None,
                role="Main" if media_type == "video" else None,
                bandwidth=bitrate,
                codecs=codec,
                resolution=resolution,
                frame_rate=_float_or_none(quality.attrib.get("FrameRate") or quality.attrib.get("Fps")),
                channels=quality.attrib.get("Channels"),
                extension="m4s" if media_type in {"video", "audio"} else "ttml",
                video_range=_video_range_for_quality(quality, stream_index, codec) if media_type == "video" else None,
                duration=sum(chunk_durations) if chunk_durations else root_duration,
                encrypted=protected and media_type != "subtitle",
                encryption_scheme=encryption_scheme,
                is_live=is_live,
                segments=segments,
                extra={
                    "fourcc": quality.attrib.get("FourCC"),
                    "codec_private_data": quality.attrib.get("CodecPrivateData"),
                    "kid": track_kid,
                    "key_id": track_kid,
                    "key_ids": list(track_key_ids),
                    "track_id": track_id,
                    **ism_segment_extra(stream_timescale, is_live),
                    "init_error": init_error,
                },
            )
            streams.append(stream)

    return streams


def _quality_track_id(quality: ET.Element, fallback: int, is_live: bool = False) -> int:
    for key in ("TrackID", "TrackId", "trackID", "trackId"):
        value = _int_or_none(quality.attrib.get(key))
        if value and value > 0:
            return value
    if is_live:
        return 1
    return fallback


def _chunks(stream_index: ET.Element, timescale: int, root_duration_ticks: int | None) -> tuple[list[int], list[float]]:
    starts: list[int] = []
    durations: list[float] = []
    current = 0
    for chunk in _children(stream_index, "c"):
        if chunk.attrib.get("t") is not None:
            current = int(chunk.attrib["t"])
        duration_ticks = _int_or_none(chunk.attrib.get("d"))
        if not duration_ticks:
            continue
        repeat_raw = _int_or_none(chunk.attrib.get("r")) or 0
        if repeat_raw < 0 and root_duration_ticks:
            count = max(1, int((root_duration_ticks - current + duration_ticks - 1) // duration_ticks))
        elif repeat_raw > 0:
            # Smooth Streaming manifests commonly treat r as a one-based chunk count.
            count = repeat_raw
        else:
            count = 1
        for _ in range(count):
            starts.append(current)
            durations.append(duration_ticks / timescale)
            current += duration_ticks
    return starts, durations


def _segments(
    base_uri: str,
    url_template: str | None,
    bitrate: int | None,
    starts: list[int],
    durations: list[float],
    index_by_timeline: bool,
    encrypted: bool,
    encryption_scheme: str | None,
    key_id: str | None,
) -> list[SegmentInfo]:
    if not url_template:
        return []
    segments: list[SegmentInfo] = []
    for index, start in enumerate(starts):
        reference = (
            url_template.replace("{bitrate}", str(bitrate or ""))
            .replace("{Bitrate}", str(bitrate or ""))
            .replace("{start time}", str(start))
            .replace("{start_time}", str(start))
            .replace("{start_time:}", str(start))
        )
        segments.append(
            SegmentInfo(
                url=join_uri(base_uri, reference),
                duration=durations[index] if index < len(durations) else None,
                index=ism_segment_index(start, index, index_by_timeline),
                encrypted=encrypted,
                encryption_scheme=encryption_scheme,
                key_id=key_id,
                timeline_time=start,
            )
        )
    return segments


def _codec_from_quality(quality: ET.Element) -> str | None:
    fourcc = (quality.attrib.get("FourCC") or quality.attrib.get("Codec") or "").upper()
    if fourcc in {"AVC1", "H264"}:
        return "avc1"
    if fourcc in {"HVC1", "HEV1", "HEVC", "H265", "DVH1", "DVHE"}:
        return "hvc1"
    if fourcc in {"VVC1", "VVI1", "VVC", "H266"}:
        return "vvc1"
    if fourcc in {"AACL", "AACH", "AAC", "MP4A"}:
        return "mp4a.40.2"
    if fourcc in {"EC-3", "EC3", "EAC3"}:
        return "ec-3"
    if fourcc in {"TTML", "DFXP"}:
        return "stpp"
    return fourcc.lower() if fourcc else None


def _media_type(value: str) -> str:
    if value == "video":
        return "video"
    if value == "audio":
        return "audio"
    if value in {"text", "subtitle", "subtitles"}:
        return "subtitle"
    return "unknown"


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local(child.tag) == name]


def _first_child(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in list(element) if _local(child.tag) == name), None)


def _protection_header(protection: ET.Element | None) -> str | None:
    if protection is None:
        return None
    header = _first_child(protection, "ProtectionHeader")
    if header is None or header.text is None:
        return None
    return header.text.strip() or None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _float_or_none(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _video_range_for_quality(quality: ET.Element, stream_index: ET.Element, codec: str | None) -> str:
    fourcc = (quality.attrib.get("FourCC") or quality.attrib.get("Codec") or "").lower()
    has_dv = fourcc in {"dvh1", "dvhe"} or bool(codec and codec.lower().startswith(("dvh1", "dvhe")))
    base_range = _explicit_video_range(quality) or _explicit_video_range(stream_index) or _hevc_video_range_from_codec_private_data(quality.attrib.get("CodecPrivateData"))
    if has_dv:
        if base_range and base_range not in {"SDR", "DV"}:
            return f"DV+{base_range}"
        return "DV"
    return base_range or "SDR"


def _explicit_video_range(element: ET.Element) -> str | None:
    for key in ("VideoRange", "videoRange", "DynamicRange", "dynamicRange", "HDR", "Hdr", "ColorSpace", "TransferCharacteristics"):
        value = element.attrib.get(key)
        normalized = _normalize_video_range(value)
        if normalized:
            return normalized
    text = " ".join(f"{key}={value}" for key, value in element.attrib.items()).lower()
    if _is_hdr10_plus_signal(text):
        return "HDR10+"
    if "dolby" in text and "vision" in text:
        return "DV"
    if "hlg" in text or "arib-std-b67" in text:
        return "HLG"
    if "hdr" in text or "st2084" in text or "smpte-2084" in text or "pq" in text:
        return "HDR10"
    if "sdr" in text:
        return "SDR"
    return None


def _normalize_video_range(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower().replace("_", "-").replace(" ", "-")
    if normalized == "18":
        return "HLG"
    if normalized == "16":
        return "HDR10"
    if normalized in {"1", "6", "13"}:
        return "SDR"
    if normalized in {"dv", "dovi", "dolby-vision", "dolbyvision"}:
        return "DV"
    if normalized in {"hdr10+", "hdr10plus", "hdr10-plus", "hdr10p"}:
        return "HDR10+"
    if normalized in {"pq", "hdr", "hdr10", "smpte-2084", "smpte:2084", "st2084"}:
        return "HDR10"
    if normalized == "hlg":
        return "HLG"
    if normalized == "sdr":
        return "SDR"
    return None


def _is_hdr10_plus_signal(value: str) -> bool:
    normalized = value.lower().replace("_", "-").replace(" ", "-")
    return any(token in normalized for token in ("hdr10+", "hdr10plus", "hdr10-plus", "hdr10p", "2094-40"))


def _hevc_video_range_from_codec_private_data(value: str | None) -> str | None:
    if not value:
        return None
    try:
        for nal in _nal_units_from_codec_private_data(value):
            if _hevc_nal_type(nal) != 33:
                continue
            colour = _hevc_sps_colour_description(nal)
            if not colour:
                continue
            primaries, transfer, matrix = colour
            if transfer == 18:
                return "HLG"
            if transfer == 16:
                return "HDR10"
            if primaries == 9 and matrix == 9 and transfer not in {1, 6, 13}:
                return "HDR10"
            if transfer in {1, 6, 13}:
                return "SDR"
    except Exception:
        return None
    return None


def _nal_units_from_codec_private_data(value: str) -> list[bytes]:
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", value)
    if not cleaned:
        return []
    data = bytes.fromhex(cleaned)
    starts: list[tuple[int, int]] = []
    index = 0
    while index < len(data) - 3:
        if data[index : index + 4] == b"\x00\x00\x00\x01":
            starts.append((index, 4))
            index += 4
            continue
        if data[index : index + 3] == b"\x00\x00\x01":
            starts.append((index, 3))
            index += 3
            continue
        index += 1
    if not starts:
        return [data]
    units: list[bytes] = []
    for position, (start, length) in enumerate(starts):
        nal_start = start + length
        nal_end = starts[position + 1][0] if position + 1 < len(starts) else len(data)
        if nal_end > nal_start:
            units.append(data[nal_start:nal_end])
    return units


def _hevc_nal_type(nal: bytes) -> int | None:
    if len(nal) < 2:
        return None
    return (nal[0] >> 1) & 0x3F


def _hevc_sps_colour_description(nal: bytes) -> tuple[int, int, int] | None:
    if len(nal) < 3:
        return None
    reader = _BitReader(_rbsp_from_ebsp(nal[2:]))
    reader.read_bits(4)
    max_sub_layers_minus1 = reader.read_bits(3)
    reader.read_bits(1)
    _skip_hevc_profile_tier_level(reader, max_sub_layers_minus1)
    reader.read_ue()
    chroma_format_idc = reader.read_ue()
    if chroma_format_idc == 3:
        reader.read_bits(1)
    reader.read_ue()
    reader.read_ue()
    if reader.read_bits(1):
        reader.read_ue()
        reader.read_ue()
        reader.read_ue()
        reader.read_ue()
    reader.read_ue()
    reader.read_ue()
    log2_max_pic_order_cnt_lsb = reader.read_ue() + 4
    sub_layer_ordering_info_present_flag = reader.read_bits(1)
    first_sub_layer = 0 if sub_layer_ordering_info_present_flag else max_sub_layers_minus1
    for _ in range(first_sub_layer, max_sub_layers_minus1 + 1):
        reader.read_ue()
        reader.read_ue()
        reader.read_ue()
    for _ in range(6):
        reader.read_ue()
    if reader.read_bits(1) and reader.read_bits(1):
        _skip_hevc_scaling_list_data(reader)
    reader.read_bits(1)
    reader.read_bits(1)
    if reader.read_bits(1):
        reader.read_bits(4)
        reader.read_bits(4)
        reader.read_ue()
        reader.read_ue()
        reader.read_bits(1)
    num_short_term_ref_pic_sets = reader.read_ue()
    ref_pic_set_delta_counts: list[int] = []
    for index in range(num_short_term_ref_pic_sets):
        _skip_short_term_ref_pic_set(reader, index, num_short_term_ref_pic_sets, ref_pic_set_delta_counts)
    if reader.read_bits(1):
        for _ in range(reader.read_ue()):
            reader.read_bits(log2_max_pic_order_cnt_lsb)
            reader.read_bits(1)
    reader.read_bits(1)
    reader.read_bits(1)
    if not reader.read_bits(1):
        return None
    return _hevc_vui_colour_description(reader)


def _hevc_vui_colour_description(reader: _BitReader) -> tuple[int, int, int] | None:
    if reader.read_bits(1):
        aspect_ratio_idc = reader.read_bits(8)
        if aspect_ratio_idc == 255:
            reader.read_bits(16)
            reader.read_bits(16)
    if reader.read_bits(1):
        reader.read_bits(1)
    if reader.read_bits(1):
        reader.read_bits(3)
        reader.read_bits(1)
        if reader.read_bits(1):
            return reader.read_bits(8), reader.read_bits(8), reader.read_bits(8)
    return None


def _skip_hevc_profile_tier_level(reader: _BitReader, max_sub_layers_minus1: int) -> None:
    reader.read_bits(2 + 1 + 5)
    reader.read_bits(32)
    reader.read_bits(4)
    reader.read_bits(44)
    reader.read_bits(8)
    profile_present_flags: list[int] = []
    level_present_flags: list[int] = []
    for _ in range(max_sub_layers_minus1):
        profile_present_flags.append(reader.read_bits(1))
        level_present_flags.append(reader.read_bits(1))
    if max_sub_layers_minus1 > 0:
        for _ in range(max_sub_layers_minus1, 8):
            reader.read_bits(2)
    for index in range(max_sub_layers_minus1):
        if profile_present_flags[index]:
            reader.read_bits(2 + 1 + 5)
            reader.read_bits(32)
            reader.read_bits(4)
            reader.read_bits(44)
        if level_present_flags[index]:
            reader.read_bits(8)


def _skip_hevc_scaling_list_data(reader: _BitReader) -> None:
    for size_id in range(4):
        matrix_id = 0
        while matrix_id < 6:
            if not reader.read_bits(1):
                reader.read_ue()
            else:
                coef_num = min(64, 1 << (4 + (size_id << 1)))
                if size_id > 1:
                    reader.read_se()
                for _ in range(coef_num):
                    reader.read_se()
            matrix_id += 3 if size_id == 3 else 1


def _skip_short_term_ref_pic_set(reader: _BitReader, index: int, total_count: int, delta_counts: list[int]) -> None:
    inter_ref_pic_set_prediction_flag = index != 0 and bool(reader.read_bits(1))
    if inter_ref_pic_set_prediction_flag:
        delta_index = reader.read_ue() + 1 if index == total_count else 1
        reader.read_bits(1)
        reader.read_ue()
        reference_index = index - delta_index
        reference_count = delta_counts[reference_index] if 0 <= reference_index < len(delta_counts) else 0
        for _ in range(reference_count + 1):
            used_by_curr_pic_flag = reader.read_bits(1)
            if not used_by_curr_pic_flag:
                reader.read_bits(1)
        delta_counts.append(0)
        return

    negative_count = reader.read_ue()
    positive_count = reader.read_ue()
    for _ in range(negative_count):
        reader.read_ue()
        reader.read_bits(1)
    for _ in range(positive_count):
        reader.read_ue()
        reader.read_bits(1)
    delta_counts.append(negative_count + positive_count)


def _rbsp_from_ebsp(data: bytes) -> bytes:
    output = bytearray()
    zero_count = 0
    for byte in data:
        if zero_count >= 2 and byte == 0x03:
            zero_count = 0
            continue
        output.append(byte)
        zero_count = zero_count + 1 if byte == 0 else 0
    return bytes(output)


class _BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.bit_position = 0

    def read_bits(self, count: int) -> int:
        value = 0
        for _ in range(count):
            if self.bit_position >= len(self.data) * 8:
                raise EOFError("Unexpected end of HEVC bitstream.")
            byte = self.data[self.bit_position // 8]
            shift = 7 - (self.bit_position % 8)
            self.bit_position += 1
            value = (value << 1) | ((byte >> shift) & 1)
        return value

    def read_ue(self) -> int:
        leading_zero_bits = 0
        while self.read_bits(1) == 0:
            leading_zero_bits += 1
        suffix = self.read_bits(leading_zero_bits) if leading_zero_bits else 0
        return (1 << leading_zero_bits) - 1 + suffix

    def read_se(self) -> int:
        code_num = self.read_ue()
        value = (code_num + 1) // 2
        return value if code_num % 2 else -value
