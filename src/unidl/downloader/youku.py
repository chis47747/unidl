from __future__ import annotations

from collections import defaultdict

from Crypto.Cipher import AES

from .postprocess import normalize_decrypted_mp4_bytes


class YoukuTsError(ValueError):
    pass


_TS_PACKET_SIZE = 188
_MEDIA_STREAM_IDS = frozenset({0xBD, *range(0xC0, 0xF0)})
_MP4_TOP_LEVEL_BOXES = frozenset(
    {
        b"emsg",
        b"free",
        b"ftyp",
        b"mdat",
        b"moof",
        b"moov",
        b"prft",
        b"sidx",
        b"skip",
        b"styp",
    }
)


def decrypt_youku_segment(data: bytes, key: bytes) -> bytes:
    """Decrypt a Youku copyrightDRM MPEG-TS or fragmented MP4 segment."""

    if not data:
        return data
    if data[:1] == b"\x47":
        return decrypt_youku_ts(data, key)
    return decrypt_youku_fmp4(data, key)


def decrypt_youku_ts(data: bytes, key: bytes) -> bytes:
    """Decrypt Youku copyrightDRM PES payloads without changing TS framing."""

    if len(key) != 16:
        raise YoukuTsError("Youku copyrightDRM key must be 16 bytes.")
    if not data:
        return data
    if len(data) % _TS_PACKET_SIZE:
        raise YoukuTsError("Youku copyrightDRM segment is not an aligned MPEG-TS stream.")

    output = bytearray(data)
    active: dict[int, list[int]] = defaultdict(list)
    cipher = AES.new(key, AES.MODE_ECB)

    def flush(pid: int) -> None:
        positions = active.pop(pid, [])
        if len(positions) < 9:
            return
        pes = bytes(output[position] for position in positions)
        if pes[:3] != b"\x00\x00\x01" or pes[3] not in _MEDIA_STREAM_IDS:
            return
        payload_start = 9 + pes[8]
        if payload_start > len(pes):
            raise YoukuTsError("Youku copyrightDRM PES header is truncated.")
        packet_length = int.from_bytes(pes[4:6], "big")
        payload_end = min(len(pes), packet_length + 6) if packet_length else len(pes)
        encrypted_size = (payload_end - payload_start) // AES.block_size * AES.block_size
        if encrypted_size <= 0:
            return
        clear = cipher.decrypt(pes[payload_start : payload_start + encrypted_size])
        for position, value in zip(
            positions[payload_start : payload_start + encrypted_size],
            clear,
            strict=True,
        ):
            output[position] = value

    for packet_start in range(0, len(output), _TS_PACKET_SIZE):
        if output[packet_start] != 0x47:
            raise YoukuTsError(
                f"Youku copyrightDRM segment lost MPEG-TS sync at byte {packet_start}."
            )
        second = output[packet_start + 1]
        pid = ((second & 0x1F) << 8) | output[packet_start + 2]
        adaptation_control = (output[packet_start + 3] >> 4) & 0x03
        if adaptation_control == 0:
            raise YoukuTsError("Youku copyrightDRM segment has a reserved TS packet header.")
        payload_offset = 4
        if adaptation_control & 0x02:
            payload_offset += 1 + output[packet_start + 4]
            if payload_offset > _TS_PACKET_SIZE:
                raise YoukuTsError("Youku copyrightDRM TS adaptation field is truncated.")
        if not (adaptation_control & 0x01) or payload_offset == _TS_PACKET_SIZE:
            continue
        if second & 0x40:
            flush(pid)
        active[pid].extend(
            range(packet_start + payload_offset, packet_start + _TS_PACKET_SIZE)
        )

    for pid in list(active):
        flush(pid)
    return bytes(output)


def decrypt_youku_fmp4(data: bytes, key: bytes) -> bytes:
    """Normalize clear CMAF advertised as Youku copyrightDRM.

    TV CMAF samples are already clear; only the initialization metadata retains
    protected sample-entry markers. Applying the TS AES-ECB transform to
    ``mdat`` corrupts NAL lengths and AAC frames.
    """

    if len(key) != AES.block_size:
        raise YoukuTsError("Youku copyrightDRM key must be 16 bytes.")
    if not data:
        return data

    output = bytearray(data)
    position = 0
    saw_box = False
    while position < len(data):
        if position + 8 > len(data):
            raise YoukuTsError("Youku copyrightDRM MP4 box header is truncated.")
        size = int.from_bytes(data[position : position + 4], "big")
        box_type = data[position + 4 : position + 8]
        header_size = 8
        if box_type not in _MP4_TOP_LEVEL_BOXES:
            raise YoukuTsError("Youku copyrightDRM segment is not MPEG-TS or fragmented MP4.")
        if size == 1:
            if position + 16 > len(data):
                raise YoukuTsError("Youku copyrightDRM extended MP4 box is truncated.")
            size = int.from_bytes(data[position + 8 : position + 16], "big")
            header_size = 16
        elif size == 0:
            size = len(data) - position
        if size < header_size or position + size > len(data):
            raise YoukuTsError("Youku copyrightDRM MP4 box size is invalid.")
        saw_box = True
        position += size
    if not saw_box:
        raise YoukuTsError("Youku copyrightDRM segment is empty.")
    return normalize_decrypted_mp4_bytes(bytes(output))
