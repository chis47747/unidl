from __future__ import annotations

import base64
import re
import time
from struct import pack

PLAYREADY_SYSTEM_ID = bytes.fromhex("9a04f07998404286ab92e65be0885f95")
START_CODE = b"\x00\x00\x00\x01"


def build_ism_init_segment(params: dict) -> bytes:
    media_type = params["media_type"]
    timescale = int(params.get("timescale") or 10_000_000)
    duration = int(params.get("duration_ticks") or 0)
    track_id = int(params.get("track_id") or 1)
    width = int(params.get("width") or 0)
    height = int(params.get("height") or 0)
    language = _language(params.get("language"))
    encrypted = bool(params.get("encrypted"))
    kid = params.get("kid") if encrypted else None
    protection_header = params.get("protection_header")

    trak = _box(
        "trak",
        _tkhd(track_id, duration, width, height)
        + _box(
            "mdia",
            _mdhd(timescale, duration, language)
            + _hdlr(media_type)
            + _box("minf", _media_header(media_type) + _dinf() + _box("stbl", _stbl(params, kid))),
        ),
    )
    moov = _mvhd(timescale, duration) + trak + _box("mvex", _mehd(duration) + _trex(track_id))
    if encrypted and protection_header:
        pssh = _playready_pssh(protection_header)
        if pssh:
            moov += pssh
    return _ftyp() + _box("moov", moov)


def extract_playready_kids(protection_header: str | None) -> list[str]:
    data = _decode_protection_header(protection_header)
    if not data:
        return []
    kids: list[str] = []
    for encoding in ("utf-16-le", "utf-8"):
        text = data.decode(encoding, errors="ignore")
        values = re.findall(r"<KID\b[^>]*\bVALUE\s*=\s*['\"]([^'\"]+)['\"][^>]*>", text, flags=re.IGNORECASE)
        values.extend(re.findall(r"<KID\b[^>]*>(.*?)</KID>", text, flags=re.IGNORECASE | re.DOTALL))
        for value in values:
            kid = _decode_playready_kid(value)
            if kid and kid not in kids:
                kids.append(kid)
    return kids


def extract_playready_kid(protection_header: str | None) -> str | None:
    kids = extract_playready_kids(protection_header)
    return kids[0] if kids else None


def _decode_playready_kid(value: str) -> str | None:
    try:
        kid = bytearray(base64.b64decode(value.strip()))
    except Exception:
        return None
    if len(kid) != 16:
        return None
    kid[0:4] = reversed(kid[0:4])
    kid[4:6] = reversed(kid[4:6])
    kid[6:8] = reversed(kid[6:8])
    return bytes(kid).hex()


def _stbl(params: dict, kid: str | None) -> bytes:
    sample_entry = _sample_entry(params, kid)
    stsd = _full_box("stsd", 0, 0, _u32(1) + sample_entry)
    return (
        stsd
        + _full_box("stts", 0, 0, _u32(0))
        + _full_box("stsc", 0, 0, _u32(0))
        + _full_box("stsz", 0, 0, _u32(0) + _u32(0))
        + _full_box("stco", 0, 0, _u32(0))
    )


def _sample_entry(params: dict, kid: str | None) -> bytes:
    media_type = params["media_type"]
    codec = str(params.get("codec") or "").lower()
    encrypted = bool(params.get("encrypted"))
    original_codec = _original_codec(codec, media_type)
    entry_type = _encrypted_entry_type(media_type) if encrypted else original_codec
    if media_type == "video":
        payload = _video_sample_entry_payload(params)
    elif media_type == "audio":
        payload = _audio_sample_entry_payload(params)
    else:
        raise ValueError(f"Unsupported ISM media type for init segment: {media_type}")
    if encrypted:
        payload += _sinf(kid, original_codec)
    return _box(entry_type, _sample_entry_header() + payload)


def _video_sample_entry_payload(params: dict) -> bytes:
    codec = str(params.get("codec") or "").lower()
    width = int(params.get("width") or 0)
    height = int(params.get("height") or 0)
    codec_private_data = _hex_bytes(params.get("codec_private_data"))
    if codec in {"avc1", "h264"}:
        config_box = _avcc(*_avc_sps_pps(codec_private_data))
    elif codec in {"hvc1", "hev1", "hevc", "h265", "dvh1", "dvhe"}:
        config_box = _hvcc(_hevc_nalus(codec_private_data))
    else:
        raise ValueError(f"Unsupported ISM video codec for init segment: {codec or 'unknown'}")
    return (
        _u16(0)
        + _u16(0)
        + _u32(0)
        + _u32(0)
        + _u32(0)
        + _u16(width)
        + _u16(height)
        + _u32(0x00480000)
        + _u32(0x00480000)
        + _u32(0)
        + _u16(1)
        + (b"\0" * 32)
        + _u16(0x18)
        + pack(">h", -1)
        + config_box
    )


def _audio_sample_entry_payload(params: dict) -> bytes:
    codec = str(params.get("codec") or "").lower()
    channels = int(params.get("channels") or 2)
    bits = int(params.get("bits") or 16)
    sample_rate = int(params.get("sample_rate") or 48_000)
    bitrate = int(params.get("bitrate") or 0)
    codec_private_data = _hex_bytes(params.get("codec_private_data"))
    payload = (
        _u32(0)
        + _u32(0)
        + _u16(channels)
        + _u16(bits)
        + _u16(0)
        + _u16(0)
        + _u32(sample_rate << 16)
    )
    if codec in {"ec-3", "ec3", "eac3"}:
        return payload + _box("dec3", codec_private_data or b"\x00\x10\x00")
    if codec in {"mp4a", "aac", "aacl", "aach"} or codec.startswith("mp4a."):
        return payload + _esds(int(params.get("track_id") or 1), bitrate, codec_private_data or b"\x12\x10")
    raise ValueError(f"Unsupported ISM audio codec for init segment: {codec or 'unknown'}")


def _original_codec(codec: str, media_type: str) -> str:
    codec = codec.lower()
    if media_type == "video":
        if codec in {"h264"}:
            return "avc1"
        return codec or "avc1"
    if media_type == "audio":
        if codec in {"ec3", "eac3"}:
            return "ec-3"
        if codec in {"aac", "aacl", "aach"} or codec.startswith("mp4a."):
            return "mp4a"
        return codec or "mp4a"
    return codec


def _encrypted_entry_type(media_type: str) -> str:
    if media_type == "video":
        return "encv"
    if media_type == "audio":
        return "enca"
    return "enc?"


def _sample_entry_header() -> bytes:
    return b"\0" * 6 + _u16(1)


def _avc_sps_pps(data: bytes) -> tuple[bytes, bytes]:
    parts = [part for part in data.split(START_CODE) if part]
    sps = next((part for part in parts if part and (part[0] & 0x1F) == 7), None)
    pps = next((part for part in parts if part and (part[0] & 0x1F) == 8), None)
    if not sps or not pps:
        raise ValueError("Missing SPS/PPS in ISM AVC CodecPrivateData.")
    return sps, pps


def _avcc(sps: bytes, pps: bytes) -> bytes:
    payload = (
        b"\x01"
        + sps[1:4]
        + b"\xff"
        + b"\xe1"
        + _u16(len(sps))
        + sps
        + b"\x01"
        + _u16(len(pps))
        + pps
    )
    return _box("avcC", payload)


def _hevc_nalus(data: bytes) -> list[bytes]:
    parts = [part for part in data.split(START_CODE) if part]
    if not parts and data:
        parts = [data]
    required = {32, 33, 34}
    present = {_hevc_nal_type(part) for part in parts}
    missing = required - {item for item in present if item is not None}
    if missing:
        raise ValueError("Missing VPS/SPS/PPS in ISM HEVC CodecPrivateData.")
    return parts


def _hvcc(nalus: list[bytes]) -> bytes:
    sps = next(nal for nal in nalus if _hevc_nal_type(nal) == 33)
    max_sub_layers_minus1 = ((sps[2] >> 1) & 0x07) if len(sps) > 2 else 0
    temporal_id_nested = sps[2] & 0x01 if len(sps) > 2 else 1
    profile = _hevc_profile_tier_level_bytes(sps)
    chroma_format_idc, bit_depth_luma_minus8, bit_depth_chroma_minus8 = _hevc_sps_format(sps)
    arrays = []
    for nal_type in (32, 33, 34):
        grouped = [nal for nal in nalus if _hevc_nal_type(nal) == nal_type]
        payload = bytes([0x80 | nal_type]) + _u16(len(grouped))
        for nal in grouped:
            payload += _u16(len(nal)) + nal
        arrays.append(payload)
    payload = (
        b"\x01"
        + profile
        + _u16(0xF000)
        + bytes(
            [
                0xFC,
                0xFC | (chroma_format_idc & 0x03),
                0xF8 | (bit_depth_luma_minus8 & 0x07),
                0xF8 | (bit_depth_chroma_minus8 & 0x07),
            ]
        )
        + _u16(0)
        + bytes([((max_sub_layers_minus1 + 1) & 0x07) << 3 | ((temporal_id_nested & 0x01) << 2) | 0x03])
        + bytes([len(arrays)])
        + b"".join(arrays)
    )
    return _box("hvcC", payload)


def _hevc_profile_tier_level_bytes(sps: bytes) -> bytes:
    if len(sps) >= 15:
        return sps[3:15]
    return b"\x01\x60\x00\x00\x00\x90\x00\x00\x00\x00\x00\x00"


def _hevc_sps_format(sps: bytes) -> tuple[int, int, int]:
    try:
        reader = _BitReader(_rbsp_from_ebsp(sps[2:]))
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
        bit_depth_luma_minus8 = reader.read_ue()
        bit_depth_chroma_minus8 = reader.read_ue()
        return chroma_format_idc, bit_depth_luma_minus8, bit_depth_chroma_minus8
    except Exception:
        return 1, 2, 2


def _hevc_nal_type(nal: bytes) -> int | None:
    if len(nal) < 2:
        return None
    return (nal[0] >> 1) & 0x3F


def _esds(track_id: int, bitrate: int, codec_private_data: bytes) -> bytes:
    descriptor = (
        b"\x03"
        + _descriptor_size(20 + len(codec_private_data))
        + _u16(track_id & 0xFFFF)
        + b"\0"
        + b"\x04"
        + _descriptor_size(15 + len(codec_private_data))
        + b"\x40"
        + b"\x15"
        + b"\xff\xff\xff"
        + _u32(bitrate)
        + _u32(bitrate)
        + b"\x05"
        + _descriptor_size(len(codec_private_data))
        + codec_private_data
        + b"\x06\x01\x02"
    )
    return _full_box("esds", 0, 0, descriptor)


def _descriptor_size(size: int) -> bytes:
    if size < 0x80:
        return bytes([size])
    parts = []
    values = [size & 0x7F]
    size >>= 7
    while size:
        values.append(size & 0x7F)
        size >>= 7
    for value in reversed(values):
        parts.append(value | (0x80 if value != values[0] else 0))
    return bytes(parts)


def _sinf(kid: str | None, codec: str) -> bytes:
    key = _kid_bytes(kid)
    tenc = _full_box("tenc", 0, 0, b"\0\0\x01\x08" + key)
    schm = _full_box("schm", 0, 0, b"cenc" + _u32(0x00010000))
    return _box("sinf", _box("frma", codec.encode("ascii")) + schm + _box("schi", tenc))


def _playready_pssh(protection_header: str | None) -> bytes | None:
    data = _decode_protection_header(protection_header)
    if not data:
        return None
    return _full_box("pssh", 0, 0, PLAYREADY_SYSTEM_ID + _u32(len(data)) + data)


def _decode_protection_header(value: str | None) -> bytes | None:
    if not value:
        return None
    try:
        return base64.b64decode(value.strip())
    except Exception:
        return None


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


def _kid_bytes(kid: str | None) -> bytes:
    value = (kid or "").strip().replace("-", "").replace(" ", "")
    if value.startswith("0x"):
        value = value[2:]
    try:
        data = bytes.fromhex(value)
    except ValueError:
        data = b""
    return data if len(data) == 16 else b"\0" * 16


def _ftyp() -> bytes:
    return _box("ftyp", b"mp41" + _u32(1) + b"iso8isommp41dashcmfc")


def _mvhd(timescale: int, duration: int) -> bytes:
    now = int(time.time())
    payload = (
        _u64(now)
        + _u64(now)
        + _u32(timescale)
        + _u64(duration)
        + _u32(0x00010000)
        + _u16(0x0100)
        + _u16(0)
        + _u32(0)
        + _u32(0)
        + _unity_matrix()
        + (_u32(0) * 6)
        + _u32(0xFFFFFFFF)
    )
    return _full_box("mvhd", 1, 0, payload)


def _tkhd(track_id: int, duration: int, width: int, height: int) -> bytes:
    now = int(time.time())
    payload = (
        _u64(now)
        + _u64(now)
        + _u32(track_id)
        + _u32(0)
        + _u64(duration)
        + _u32(0)
        + _u32(0)
        + pack(">h", 0)
        + pack(">h", 0)
        + pack(">h", 0x0100 if width == 0 and height == 0 else 0)
        + _u16(0)
        + _unity_matrix()
        + _u32(width << 16)
        + _u32(height << 16)
    )
    return _full_box("tkhd", 1, 0x000007, payload)


def _mdhd(timescale: int, duration: int, language: str) -> bytes:
    now = int(time.time())
    payload = _u64(now) + _u64(now) + _u32(timescale) + _u64(duration) + _u16(_language_code(language)) + _u16(0)
    return _full_box("mdhd", 1, 0, payload)


def _hdlr(media_type: str) -> bytes:
    if media_type == "video":
        handler, name = b"vide", b"video\0"
    elif media_type == "audio":
        handler, name = b"soun", b"audio\0"
    else:
        handler, name = b"subt", b"subtitle\0"
    return _full_box("hdlr", 0, 0, _u32(0) + handler + _u32(0) + _u32(0) + _u32(0) + name)


def _media_header(media_type: str) -> bytes:
    if media_type == "video":
        return _full_box("vmhd", 0, 1, _u16(0) + _u16(0) + _u16(0) + _u16(0))
    if media_type == "audio":
        return _full_box("smhd", 0, 0, pack(">h", 0) + _u16(0))
    return _full_box("sthd", 0, 0, b"")


def _dinf() -> bytes:
    return _box("dinf", _full_box("dref", 0, 0, _u32(1) + _full_box("url ", 0, 1, b"")))


def _mehd(duration: int) -> bytes:
    return _full_box("mehd", 1, 0, _u64(duration))


def _trex(track_id: int) -> bytes:
    return _full_box("trex", 0, 0, _u32(track_id) + _u32(1) + _u32(0) + _u32(0) + _u32(0))


def _box(name: str, payload: bytes) -> bytes:
    return _u32(8 + len(payload)) + name.encode("ascii") + payload


def _full_box(name: str, version: int, flags: int, payload: bytes) -> bytes:
    return _box(name, bytes([version]) + flags.to_bytes(3, "big") + payload)


def _unity_matrix() -> bytes:
    return pack(">i", 0x10000) + pack(">i", 0) * 3 + pack(">i", 0x10000) + pack(">i", 0) * 3 + pack(">i", 0x40000000)


def _hex_bytes(value: str | None) -> bytes:
    value = (value or "").strip().replace(" ", "").replace("-", "")
    return bytes.fromhex(value) if value else b""


def _language(value: str | None) -> str:
    value = (value or "und").lower()
    return value if len(value) == 3 and value.isalpha() else "und"


def _language_code(value: str) -> int:
    value = _language(value)
    return ((ord(value[0]) - 0x60) << 10) | ((ord(value[1]) - 0x60) << 5) | (ord(value[2]) - 0x60)


def _u16(value: int) -> bytes:
    return pack(">H", value & 0xFFFF)


def _u32(value: int) -> bytes:
    return pack(">I", value & 0xFFFFFFFF)


def _u64(value: int) -> bytes:
    return pack(">Q", value & 0xFFFFFFFFFFFFFFFF)
