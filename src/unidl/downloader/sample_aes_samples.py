"""SAMPLE-AES AAC/AC-3/E-AC-3/H.264 bound to each segment's key/IV."""

from __future__ import annotations

import re
from bisect import bisect_left

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .bbts import _make_payload_packet_from_original, _packet_info, _parse_pat, _parse_pmt
from .embedding import current_download_runtime


def _cbc(data: bytes, key: bytes, iv: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _id3_end(data: bytes, offset: int) -> int:
    if offset + 10 > len(data) or any(byte & 0x80 for byte in data[offset + 6:offset + 10]):
        raise ValueError("SAMPLE-AES audio contains an invalid ID3 header")
    size = 0
    for byte in data[offset + 6:offset + 10]:
        size = (size << 7) | byte
    end = offset + size + 10 + (10 if data[offset + 3] == 4 and data[offset + 5] & 0x10 else 0)
    if end > len(data):
        raise ValueError("SAMPLE-AES audio contains truncated ID3 data")
    return end


def decrypt_ac3(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AC-3 SAMPLE-AES: reset CBC per syncframe, preserving its 16-byte leader."""
    return _decrypt_dolby(data, key, iv, enhanced=False)


def decrypt_eac3(data: bytes, key: bytes, iv: bytes) -> bytes:
    """E-AC-3 resets CBC for every syncframe, including dependent substreams."""
    return _decrypt_dolby(data, key, iv, enhanced=True)


def _decrypt_dolby(data: bytes, key: bytes, iv: bytes, *, enhanced: bool) -> bytes:
    bitrates = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 448, 512, 576, 640)
    codec = "E-AC-3" if enhanced else "AC-3"
    output = bytearray(data)
    offset = 0
    frames = 0
    while offset < len(data):
        if frames % 256 == 0 and (runtime := current_download_runtime()) is not None:
            runtime.checkpoint()
        if data[offset:offset + 3] == b"ID3":
            offset = _id3_end(data, offset)
            continue
        if offset + 7 > len(data) or data[offset:offset + 2] != b"\x0b\x77":
            raise ValueError(f"SAMPLE-AES audio contains an invalid {codec} syncframe")
        bsid = data[offset + 5] >> 3
        if enhanced:
            if not 11 <= bsid <= 16 or data[offset + 2] >> 6 == 3:
                raise ValueError("SAMPLE-AES audio contains an invalid E-AC-3 header")
            if data[offset + 4] & 0xF0 == 0xF0:
                raise ValueError("SAMPLE-AES audio contains a reserved E-AC-3 sample rate")
            size = (((data[offset + 2] & 7) << 8 | data[offset + 3]) + 1) * 2
        else:
            if bsid > 10:
                raise ValueError("Expected AC-3, not E-AC-3, in packed audio")
            rate = data[offset + 4] >> 6
            code = data[offset + 4] & 63
            if rate == 3 or code > 37:
                raise ValueError("SAMPLE-AES audio contains an invalid AC-3 frame size")
            bitrate = bitrates[code >> 1]
            # Frame sizes are measured in 16-bit words; 44.1 kHz alternates by one.
            words = (bitrate * 2, bitrate * 320 // 147 + (code & 1), bitrate * 3)[rate]
            size = words * 2
        if size < 7 or offset + size > len(data):
            raise ValueError(f"SAMPLE-AES audio contains an invalid or truncated {codec} syncframe")
        start = offset + 16
        count = max(0, (size - 16) // 16 * 16)
        if count:
            output[start:start + count] = _cbc(data[start:start + count], key, iv)
        offset += size
        frames += 1
    return bytes(output)


def decrypt_aac(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Preserve ID3, ADTS headers, clear leaders and incomplete trailing blocks."""
    output = bytearray(data)
    offset = 0
    frames = 0
    while offset < len(data):
        if frames % 256 == 0 and (runtime := current_download_runtime()) is not None:
            runtime.checkpoint()
        if data[offset:offset + 3] == b"ID3":
            offset = _id3_end(data, offset)
            continue
        if offset + 7 > len(data) or data[offset] != 255 or data[offset + 1] & 0xF6 != 0xF0:
            raise ValueError("SAMPLE-AES audio contains an invalid ADTS frame")
        size = ((data[offset + 3] & 3) << 11) | (data[offset + 4] << 3) | (data[offset + 5] >> 5)
        header = 7 if data[offset + 1] & 1 else 9
        if size < header or offset + size > len(data):
            raise ValueError("SAMPLE-AES audio contains a truncated ADTS frame")
        if data[offset + 6] & 3:
            raise ValueError("SAMPLE-AES AAC with multiple raw data blocks is unsupported")
        start = offset + header + 16
        count = max(0, (size - header - 16) // 16 * 16)
        if count:
            output[start:start + count] = _cbc(data[start:start + count], key, iv)
        offset += size
        frames += 1
    return bytes(output)


def decrypt_h264(data: bytes, key: bytes, iv: bytes) -> bytes:
    return _decrypt_h264(data, key, iv)[0]


def _decrypt_h264(data: bytes, key: bytes, iv: bytes) -> tuple[bytes, list[int]]:
    starts = list(re.finditer(b"\x00\x00(?:\x00)?\x01", data))
    if not starts:
        raise ValueError("SAMPLE-AES video PES contains no Annex B NAL units")
    output = bytearray(data[:starts[0].start()])
    removed = []
    for index, start in enumerate(starts):
        if index % 64 == 0 and (runtime := current_download_runtime()) is not None:
            runtime.checkpoint()
        end = starts[index + 1].start() if index + 1 < len(starts) else len(data)
        nal = data[start.end():end]
        if nal and nal[0] & 31 in {1, 5} and len(nal) > 48:
            # Remove the encryption-added EPB layer once. Original AVC escape
            # bytes were part of the encrypted sample and must remain intact.
            removed.extend(start.end() + match.start() + 2 for match in re.finditer(b"\x00\x00\x03", nal))
            nal = bytearray(nal.replace(b"\x00\x00\x03", b"\x00\x00"))
            positions = range(32, len(nal) - 16, 160)
            protected = b"".join(nal[pos:pos + 16] for pos in positions)
            clear = _cbc(protected, key, iv)
            for block, pos in enumerate(positions):
                nal[pos:pos + 16] = clear[block * 16:(block + 1) * 16]
        output.extend(start.group())
        output.extend(nal)
    return bytes(output), removed


def decrypt_ts(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Keep PAT/PMT, PTS/DTS, PCR and packet counters while replacing PES data."""
    if not data or len(data) % 188:
        raise ValueError("SAMPLE-AES segment is not an aligned MPEG-TS stream")
    output = bytearray(data)
    programs = {}
    codecs = {}
    active: dict[int, list[tuple[int, int]]] = {}
    samples: dict[int, list[tuple[list[tuple[int, int]], bytes, bytes]]] = {}

    def flush(pid: int) -> None:
        packets = active.pop(pid, [])
        if not packets:
            return
        pes = b"".join(data[start + offset:start + 188] for start, offset in packets)
        if len(pes) < 9 or pes[:3] != b"\x00\x00\x01":
            raise ValueError("SAMPLE-AES segment contains an invalid PES header")
        begin = 9 + pes[8]
        length = int.from_bytes(pes[4:6], "big")
        if begin > len(pes) or (length and length + 6 != len(pes)):
            raise ValueError("SAMPLE-AES PES crosses a segment boundary or is truncated")
        samples.setdefault(pid, []).append((packets, pes[:begin], pes[begin:]))

    def scatter(packets: list[tuple[int, int]], prefix: bytes, payload: bytes) -> None:
        clear = bytearray(prefix) + payload
        if int.from_bytes(prefix[4:6], "big"):
            clear[4:6] = (len(clear) - 6).to_bytes(2, "big")
        capacities = [188 - offset for _, offset in packets]
        reduction = sum(capacities) - len(clear)
        if reduction < 0:
            raise ValueError("SAMPLE-AES plaintext exceeds its TS packet capacity")
        for i in range(len(capacities) - 1, -1, -1):
            take = min(reduction, capacities[i] - 1)
            capacities[i] -= take
            reduction -= take
        if reduction:
            raise ValueError("SAMPLE-AES PES cannot retain its transport framing")
        cursor = 0
        for (start, _), size in zip(packets, capacities, strict=True):
            output[start:start + 188] = _make_payload_packet_from_original(
                data[start:start + 188], bytes(clear[cursor:cursor + size]),
            )
            cursor += size

    for start in range(0, len(data), 188):
        if start % (188 * 1024) == 0 and (runtime := current_download_runtime()) is not None:
            runtime.checkpoint()
        packet = data[start:start + 188]
        pid, begins, afc, _cc, offset = _packet_info(packet)
        if pid < 0 or afc == 0 or packet[1] & 0x80 or packet[3] & 0xC0 or (afc == 3 and offset is None):
            raise ValueError("SAMPLE-AES segment contains an invalid TS packet")
        if pid == 0:
            programs.update({pmt: number for number, pmt in _parse_pat(packet).items()})
        if pid in programs:
            for track in _parse_pmt(packet, programs[pid]):
                kind = track["stream_type"]
                if kind in {0x1B, 0xDB}:
                    codecs[track["pid"]] = "h264"
                elif kind in {0x0F, 0xCF}:
                    codecs[track["pid"]] = "aac"
                elif kind not in {0x15, 0x06}:
                    raise ValueError("Unsupported SAMPLE-AES TS codec; expected H.264/AAC")
        if pid not in codecs or offset is None or offset == 188:
            continue
        if begins:
            flush(pid)
            active[pid] = []
        if pid not in active:
            raise ValueError("SAMPLE-AES segment begins with an incomplete PES")
        active[pid].append((start, offset))
    if not codecs:
        raise ValueError("SAMPLE-AES segment has no supported program map")
    for pid in list(active):
        flush(pid)
    # A NAL/ADTS frame may span several PES packets. Decrypt its complete ES,
    # then map removed EPBs back to PES boundaries without moving timestamps.
    for pid, groups in samples.items():
        es = b"".join(payload for _, _, payload in groups)
        if codecs[pid] == "h264":
            clear, removed = _decrypt_h264(es, key, iv)
        else:
            clear, removed = decrypt_aac(es, key, iv), []
        cursor = 0
        for packets, prefix, payload in groups:
            begin = cursor - bisect_left(removed, cursor)
            cursor += len(payload)
            end = cursor - bisect_left(removed, cursor)
            scatter(packets, prefix, clear[begin:end])
    return bytes(output)
