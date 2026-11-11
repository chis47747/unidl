from __future__ import annotations

import re
from pathlib import Path

try:
    from Crypto.Util.strxor import strxor as _native_xor
except (ImportError, OSError):
    _native_xor = None

TS_PACKET_SIZE = 188
_SPARSE_BATCH_BLOCKS = 4096  # 64 KiB masks, never a file-sized keystream.
_PACKET_WRITE_BATCH_BYTES = TS_PACKET_SIZE * 4096


class BbtsError(RuntimeError):
    pass


def decrypt_bbts_file(input_path: str | Path, output_path: str | Path, key_hex: str) -> Path:
    input_path = Path(input_path)
    output_path = Path(output_path)
    _decrypt_stream(input_path, output_path, _hex_key(key_hex))
    if not output_path.exists():
        raise BbtsError(f"BBTS decryption output was not created: {output_path}")
    return output_path


def bbts_part_is_complete(path: str | Path, size: int | None = None) -> bool:
    path = Path(path)
    try:
        file_size = path.stat().st_size if size is None else size
        if file_size < 3 * TS_PACKET_SIZE or file_size % TS_PACKET_SIZE:
            return False
        with path.open("rb") as source:
            marker = source.read(TS_PACKET_SIZE)
            pat = source.read(TS_PACKET_SIZE)
            pmt = source.read(TS_PACKET_SIZE)
            if _packet_info(marker)[0] != 17 or _extract_packet_block_key(marker) is None:
                return False
            programs = _parse_pat(pat)
            if not programs or _packet_info(pmt)[0] not in programs.values():
                return False
            while packet := source.read(TS_PACKET_SIZE):
                if len(packet) != TS_PACKET_SIZE or packet[0] != 0x47:
                    return False
    except OSError:
        return False
    return True


def _hex_key(value: str) -> bytes:
    cleaned = value.strip().lower().replace("0x", "").replace("-", "")
    if len(cleaned) != 32 or any(char not in "0123456789abcdef" for char in cleaned):
        raise BbtsError("BBTS key must be 32 hex characters.")
    return bytes.fromhex(cleaned)


def _make_aes_encryptor(key: bytes):
    try:
        from Crypto.Cipher import AES as crypto_aes

        cipher = crypto_aes.new(key, crypto_aes.MODE_ECB)
        return cipher.encrypt
    except Exception:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return encryptor.update
    except Exception as exc:
        raise BbtsError("BBTS decryption needs pycryptodome or cryptography installed.") from exc


def _packet_info(packet: bytes) -> tuple[int, bool, int, int, int | None]:
    if len(packet) != TS_PACKET_SIZE or packet[0] != 0x47:
        return -1, False, 0, 0, None
    pid = ((packet[1] & 0x1F) << 8) | packet[2]
    payload_unit_start = (packet[1] & 0x40) != 0
    adaptation_field_control = (packet[3] >> 4) & 0x03
    continuity_counter = packet[3] & 0x0F
    payload_offset = 4
    if adaptation_field_control in (2, 3):
        if payload_offset >= TS_PACKET_SIZE:
            return pid, payload_unit_start, adaptation_field_control, continuity_counter, None
        adaptation_length = packet[payload_offset]
        payload_offset += 1 + adaptation_length
        if payload_offset > TS_PACKET_SIZE:
            return pid, payload_unit_start, adaptation_field_control, continuity_counter, None
    if adaptation_field_control not in (1, 3):
        return pid, payload_unit_start, adaptation_field_control, continuity_counter, None
    return pid, payload_unit_start, adaptation_field_control, continuity_counter, payload_offset


def _printable_text(data: bytes) -> str:
    return "".join(chr(value) for value in data if 32 <= value <= 126)


def _extract_packet_block_key(packet: bytes) -> bytes | None:
    match = re.search(r"\|v([0-9a-fA-F]{32})\|", _printable_text(packet[4:]))
    return bytes.fromhex(match.group(1)) if match else None


def _descriptor_text(descriptors: bytes) -> list[str]:
    values: list[str] = []
    index = 0
    while index + 2 <= len(descriptors):
        tag = descriptors[index]
        size = descriptors[index + 1]
        value = descriptors[index + 2 : index + 2 + size]
        if index + 2 + size > len(descriptors):
            break
        if tag == 0x05 and value:
            values.append("registration=" + value.decode("latin-1", "ignore"))
        elif tag == 0x0A and len(value) >= 3:
            values.append("language=" + value[:3].decode("latin-1", "ignore"))
        index += 2 + size
    return values


def _stream_type_name(stream_type: int) -> tuple[str, str]:
    names = {
        0x01: ("vide", "MPEG-1_video"),
        0x02: ("vide", "MPEG-2_video"),
        0x0F: ("soun", "AAC_ADTS_audio"),
        0x1B: ("vide", "H.264_AVC_video"),
        0x24: ("vide", "H.265_HEVC_video"),
        0x81: ("soun", "AC3_audio"),
        0x87: ("soun", "EAC3_audio"),
    }
    return names.get(stream_type, ("data", "unknown"))


def _parse_pat(packet: bytes) -> dict[int, int]:
    pid, payload_unit_start, _afc, _cc, payload_offset = _packet_info(packet)
    if pid != 0 or payload_offset is None:
        return {}
    payload = packet[payload_offset:]
    if payload_unit_start:
        if not payload:
            return {}
        payload = payload[1 + payload[0] :]
    if len(payload) < 8 or payload[0] != 0x00:
        return {}
    section_length = ((payload[1] & 0x0F) << 8) | payload[2]
    section = payload[: 3 + section_length]
    programs: dict[int, int] = {}
    index = 8
    end = len(section) - 4
    while index + 4 <= end:
        program_number = (section[index] << 8) | section[index + 1]
        program_map_pid = ((section[index + 2] & 0x1F) << 8) | section[index + 3]
        if program_number:
            programs[program_number] = program_map_pid
        index += 4
    return programs


def _parse_pmt(packet: bytes, program_number: int) -> list[dict]:
    _pid, payload_unit_start, _afc, _cc, payload_offset = _packet_info(packet)
    if payload_offset is None:
        return []
    payload = packet[payload_offset:]
    if payload_unit_start:
        if not payload:
            return []
        payload = payload[1 + payload[0] :]
    if len(payload) < 12 or payload[0] != 0x02:
        return []
    section_length = ((payload[1] & 0x0F) << 8) | payload[2]
    section = payload[: 3 + section_length]
    if len(section) < 16:
        return []
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    index = 12 + program_info_length
    end = len(section) - 4
    tracks: list[dict] = []
    track_index = 1
    while index + 5 <= end:
        stream_type = section[index]
        elementary_pid = ((section[index + 1] & 0x1F) << 8) | section[index + 2]
        es_info_length = ((section[index + 3] & 0x0F) << 8) | section[index + 4]
        descriptors = _descriptor_text(section[index + 5 : index + 5 + es_info_length])
        handler, codec = _stream_type_name(stream_type)
        if stream_type == 0x06 and any(value == "registration=DOVI" for value in descriptors):
            handler = "vide"
            codec = "Dolby_Vision_video"
        tracks.append(
            {
                "track": track_index,
                "pid": elementary_pid,
                "program": program_number,
                "stream_type": stream_type,
                "handler": handler,
                "codec": codec,
                "pcr": elementary_pid == pcr_pid,
                "descriptors": descriptors,
            }
        )
        track_index += 1
        index += 5 + es_info_length
    return tracks


def _track_is_video(track: dict) -> bool:
    return track.get("handler") == "vide" or any(value == "registration=DOVI" for value in track.get("descriptors", []))


# Keep the sparse cipher schedule and NAL boundary rules compatible with
# BBTSDecrypt 1.5 (ReiDoBrega, MIT); BBTS depends on its unusual tail handling.
def _decrypt_sparse_payload(es: bytes, block_key: bytes, encrypt_block) -> bytes:
    # Non-overlapping replacement matches the reference's left-to-right scan,
    # including its deliberately nonstandard restoration of the original tail.
    stripped = es.replace(b"\x00\x00\x03", b"\x00\x00")
    size = len(stripped)
    blocks = (size + 15) // 16
    counter = int.from_bytes(block_key[:12].ljust(16, b"\x00"), "big")
    output = bytearray(len(es))
    for first in range(0, blocks, _SPARSE_BATCH_BLOCKS):
        count = min(_SPARSE_BATCH_BLOCKS, blocks - first)
        mask = bytearray(b"".join(
            ((counter + first + index + 1) & ((1 << 128) - 1)).to_bytes(16, "big")
            for index in range(count)
        ))
        encrypted_indexes = list(range((-first) % 10, count, 10))
        # Every tenth block uses AES; the final block always does, even when
        # it is a complete 16-byte block. Other blocks XOR the raw counter.
        if first + count == blocks and (blocks - 1) % 10:
            encrypted_indexes.append(count - 1)
        encrypted = encrypt_block(b"".join(
            mask[index * 16:index * 16 + 16] for index in encrypted_indexes
        )) if encrypted_indexes else b""
        for offset, index in enumerate(encrypted_indexes):
            mask[index * 16:index * 16 + 16] = encrypted[offset * 16:offset * 16 + 16]
        start, end = first * 16, min(size, (first + count) * 16)
        left, right = stripped[start:end], mask[:end - start]
        if _native_xor is not None:
            output[start:end] = _native_xor(left, right)
        else:
            # Keep the cryptography-only backend fast too. Bounded big-int
            # operations perform XOR in native code without another dependency.
            output[start:end] = (
                int.from_bytes(left, "big") ^ int.from_bytes(right, "big")
            ).to_bytes(end - start, "big")
    output[size:] = es[size:]
    return bytes(output)


def _decrypt_es(input_stream: bytes, block_key: bytes, encrypt_block, stream_type: int) -> bytes:
    output = bytearray()
    nal_header_length = 1 if stream_type == 0x1B else 2
    nal_start = 0
    size = len(input_stream)
    while True:
        index = input_stream.find(b"\x00\x00\x01", nal_start + 1)
        if index < 0:
            break
        payload_start = nal_start + 3 + nal_header_length
        if index - 2 > payload_start:
            output.extend(input_stream[nal_start:payload_start])
            trailer_length = 3 if input_stream[index - 1] == 0 else 2
            payload_end = index - trailer_length
            output.extend(
                _decrypt_sparse_payload(
                    input_stream[payload_start:payload_end],
                    block_key,
                    encrypt_block,
                )
            )
            output.extend(input_stream[payload_end:index])
        else:
            output.extend(input_stream[nal_start:index])
        nal_start = index
    if size:
        payload_start = nal_start + 3 + nal_header_length
        if size - 2 > payload_start:
            output.extend(input_stream[nal_start:payload_start])
            output.extend(_decrypt_sparse_payload(
                input_stream[payload_start:size - 2], block_key, encrypt_block,
            ))
            output.extend(input_stream[size - 2:])
        else:
            output.extend(input_stream[nal_start:])

    # The reference algorithm mutates a fixed-size PES buffer; preserve that
    # behavior even when malformed input causes the reconstructed length to vary.
    result = bytearray(input_stream)
    result[: min(len(result), len(output))] = output[: len(result)]
    return bytes(result)


def _make_payload_packet_from_original(packet: bytes, payload: bytes) -> bytes:
    _pid, _pus, adaptation_field_control, _cc, payload_offset = _packet_info(packet)
    if payload_offset is None or payload_offset > TS_PACKET_SIZE:
        payload_offset = 4
        adaptation_field_control = 1
    original_header = bytearray(packet[:payload_offset])
    payload_capacity = TS_PACKET_SIZE - len(original_header)
    payload = payload[: max(0, payload_capacity)]
    stuffing_needed = payload_capacity - len(payload)
    if stuffing_needed <= 0:
        output_header = bytearray(original_header)
        if len(output_header) >= 4:
            output_header[3] = (output_header[3] & 0xCF) | (adaptation_field_control << 4)
        return bytes(output_header + payload).ljust(TS_PACKET_SIZE, b"\xff")[:TS_PACKET_SIZE]
    if adaptation_field_control == 3 and len(original_header) >= 5:
        old_length = original_header[4]
        new_length = old_length + stuffing_needed
        if new_length <= 183:
            output_header = bytearray(original_header)
            output_header[3] = (output_header[3] & 0xCF) | 0x30
            output_header[4] = new_length
            output_header.extend(b"\xff" * stuffing_needed)
            return bytes(output_header + payload).ljust(TS_PACKET_SIZE, b"\xff")[:TS_PACKET_SIZE]
    base_header = bytearray(packet[:4])
    base_header[3] = (base_header[3] & 0xCF) | 0x30
    adaptation_length = stuffing_needed - 1
    output = bytearray(base_header)
    output.append(adaptation_length)
    if adaptation_length > 0:
        output.append(0)
        if adaptation_length > 1:
            output.extend(b"\xff" * (adaptation_length - 1))
    output.extend(payload)
    return bytes(output).ljust(TS_PACKET_SIZE, b"\xff")[:TS_PACKET_SIZE]


def _make_adaptation_only_packet_from_original(packet: bytes) -> bytes:
    header = bytearray(packet[:4])
    header[3] = (header[3] & 0xCF) | 0x20
    _pid, _pus, adaptation_field_control, _cc, _payload_offset = _packet_info(packet)
    content = b""
    if adaptation_field_control == 3 and len(packet) >= 5:
        old_length = packet[4]
        old_end = min(5 + old_length, TS_PACKET_SIZE)
        content = packet[5:old_end]
    content = content[:183]
    return bytes(header) + bytes([183]) + content + b"\xff" * (183 - len(content))


def _update_pes_packet_length_if_needed(prefix: bytes, payload_length: int) -> bytes:
    if len(prefix) < 6 or prefix[:3] != b"\x00\x00\x01":
        return prefix
    current_length = (prefix[4] << 8) | prefix[5]
    if current_length == 0:
        return prefix
    new_length = len(prefix) + payload_length - 6
    if new_length < 0 or new_length > 0xFFFF:
        return prefix
    output = bytearray(prefix)
    output[4] = (new_length >> 8) & 0xFF
    output[5] = new_length & 0xFF
    return bytes(output)


def _patch_group(
    output_handle,
    group_entries: list[dict],
    block_key: bytes | None,
    encrypt_block,
    stream_type: int,
) -> bool:
    if not group_entries or block_key is None or len(block_key) != 16:
        return False
    payload = bytearray()
    packet_parts: list[dict] = []
    for entry in group_entries:
        packet = entry["packet"]
        _pid, payload_unit_start, _afc, _cc, payload_offset = _packet_info(packet)
        if payload_offset is None:
            continue
        pes_header = b""
        payload_start = payload_offset
        if payload_unit_start:
            packet_payload = packet[payload_offset:]
            if len(packet_payload) >= 9 and packet_payload[:3] == b"\x00\x00\x01":
                pes_header_size = 9 + packet_payload[8]
                if pes_header_size <= len(packet_payload):
                    pes_header = packet_payload[:pes_header_size]
                    payload_start = payload_offset + pes_header_size
        packet_parts.append({"offset": entry["offset"], "packet": packet, "payload_unit_start": payload_unit_start, "pes_header": pes_header})
        payload.extend(packet[payload_start:])
    if not packet_parts:
        return False
    decrypted = _decrypt_es(bytes(payload), block_key, encrypt_block, stream_type)
    position = 0
    ended = False
    return_position = output_handle.tell()
    pending = bytearray()
    pending_offset = 0

    def flush_packets() -> None:
        if pending:
            output_handle.seek(pending_offset)
            output_handle.write(pending)
            pending.clear()

    def write_packet(offset: int, packet: bytes) -> None:
        nonlocal pending_offset
        if pending and (offset != pending_offset + len(pending) or len(pending) >= _PACKET_WRITE_BATCH_BYTES):
            flush_packets()
        if not pending:
            pending_offset = offset
        pending.extend(packet)

    for part in packet_parts:
        packet = part["packet"]
        if ended:
            rebuilt = _make_adaptation_only_packet_from_original(packet)
            write_packet(part["offset"], rebuilt)
            continue
        prefix = part["pes_header"] if part["payload_unit_start"] else b""
        if prefix:
            prefix = _update_pes_packet_length_if_needed(prefix, len(decrypted))
        _pid, _pus, _afc, _cc, payload_offset = _packet_info(packet)
        if payload_offset is None or payload_offset > TS_PACKET_SIZE:
            payload_offset = 4
        capacity = TS_PACKET_SIZE - payload_offset - len(prefix)
        remaining = len(decrypted) - position
        if remaining <= 0:
            rebuilt = _make_payload_packet_from_original(packet, prefix) if prefix else _make_adaptation_only_packet_from_original(packet)
            write_packet(part["offset"], rebuilt)
            ended = True
            continue
        take = min(max(0, capacity), remaining)
        chunk = decrypted[position : position + take]
        position += take
        rebuilt = _make_payload_packet_from_original(packet, prefix + chunk)
        write_packet(part["offset"], rebuilt)
        if position >= len(decrypted):
            ended = True
    flush_packets()
    output_handle.seek(return_position)
    return True


def _decrypt_stream(input_path: Path, output_path: Path, decryption_key: bytes) -> None:
    if input_path.stat().st_size < TS_PACKET_SIZE:
        raise BbtsError("Input file is too small to be a TS/BBTS file.")
    encrypt_block = _make_aes_encryptor(decryption_key)
    programs: dict[int, int] = {}
    pmt_pids: set[int] = set()
    tracks: list[dict] = []
    target_pids: set[int] = set()
    fallback_video_pid = -1
    active_block_key: bytes | None = None
    current_groups: dict[int, list[dict]] = {}
    current_group_keys: dict[int, bytes | None] = {}

    def refresh_target_pids() -> None:
        nonlocal target_pids
        found = {track["pid"] for track in tracks if _track_is_video(track)}
        if found:
            target_pids = found
        elif fallback_video_pid >= 0:
            target_pids = {fallback_video_pid}

    def flush_group(output_handle, pid: int) -> None:
        group = current_groups.get(pid)
        group_key = current_group_keys.get(pid)
        if group:
            stream_type = next((track["stream_type"] for track in tracks if track["pid"] == pid), 0x24)
            _patch_group(output_handle, group, group_key, encrypt_block, stream_type)
        current_groups[pid] = []
        current_group_keys[pid] = active_block_key

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with input_path.open("rb") as source, output_path.open("wb+") as output:
        while True:
            packet = source.read(TS_PACKET_SIZE)
            if not packet:
                break
            if len(packet) != TS_PACKET_SIZE:
                break
            output_offset = output.tell()
            output.write(packet)
            pid, payload_unit_start, _afc, _cc, payload_offset = _packet_info(packet)
            if pid == 17:
                found_key = _extract_packet_block_key(packet)
                if found_key is not None:
                    active_block_key = found_key
            if pid == 0:
                found_programs = _parse_pat(packet)
                if found_programs:
                    programs.update(found_programs)
                    pmt_pids.update(found_programs.values())
            elif pid in pmt_pids:
                program_number = next((number for number, pmt_pid in programs.items() if pmt_pid == pid), 0)
                found_tracks = _parse_pmt(packet, program_number)
                if found_tracks:
                    tracks = found_tracks
                    refresh_target_pids()
            if not target_pids and payload_unit_start and active_block_key is not None and payload_offset is not None and 32 <= pid <= 256:
                fallback_video_pid = pid
                refresh_target_pids()
            if pid in target_pids and payload_offset is not None:
                if payload_unit_start:
                    if pid in current_groups and current_groups[pid]:
                        flush_group(output, pid)
                    current_groups[pid] = []
                    current_group_keys[pid] = active_block_key
                if pid in current_groups:
                    current_groups[pid].append({"offset": output_offset, "packet": packet})
        for pid in list(current_groups):
            if current_groups[pid]:
                flush_group(output, pid)
        output.flush()
