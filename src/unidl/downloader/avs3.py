from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess  # noqa: F401 - retained as the module's patch seam for hosts/tests
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .embedding import managed_run

TS_PACKET_SIZE = 188
AVS3_VIDEO_STREAM_TYPE = 0xD4
AUDIO_VIVID_STREAM_TYPE = 0xD5
EAC3_STREAM_TYPE = 0x87


class Avs3Error(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TsDescriptor:
    tag: int
    data: bytes


@dataclass(frozen=True, slots=True)
class TsElementaryStream:
    pid: int
    stream_type: int
    descriptors: tuple[TsDescriptor, ...] = ()

    @property
    def registration(self) -> str | None:
        for descriptor in self.descriptors:
            if descriptor.tag == 0x05 and len(descriptor.data) >= 4:
                return descriptor.data[:4].decode("ascii", errors="replace")
        return None

    @property
    def codec(self) -> str:
        registration = (self.registration or "").lower()
        if self.stream_type == AUDIO_VIVID_STREAM_TYPE or registration in {"av3a", "avsa"}:
            return "audio-vivid"
        if self.stream_type == AVS3_VIDEO_STREAM_TYPE or registration == "avsv":
            return "avs3-video"
        if self.stream_type == 0x24 or registration == "hevc":
            return "hevc"
        if self.stream_type in {0x0F, 0x11}:
            return "aac"
        if self.stream_type == EAC3_STREAM_TYPE or registration in {"ec-3", "eac3"}:
            return "e-ac-3"
        if self.stream_type == 0x81 or registration == "ac-3":
            return "ac-3"
        descriptor_tags = {descriptor.tag for descriptor in self.descriptors}
        if 0x7A in descriptor_tags:
            return "e-ac-3"
        if 0x6A in descriptor_tags:
            return "ac-3"
        return f"stream-type-0x{self.stream_type:02x}"


@dataclass(frozen=True, slots=True)
class TsProgram:
    program_number: int
    pmt_pid: int
    pcr_pid: int
    streams: tuple[TsElementaryStream, ...]


@dataclass(frozen=True, slots=True)
class TsInspection:
    packet_size: int
    sync_offset: int
    programs: tuple[TsProgram, ...]

    @property
    def streams(self) -> tuple[TsElementaryStream, ...]:
        return tuple(stream for program in self.programs for stream in program.streams)


@dataclass(frozen=True, slots=True)
class Mp4AudioTrack:
    track_id: int
    codec: str
    sample_entry: str
    sample_ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class Mp4Inspection:
    audio_tracks: tuple[Mp4AudioTrack, ...]


def inspect_mpeg_ts(input_path: str | Path) -> TsInspection:
    path = Path(input_path)
    packet_size, sync_offset = _detect_ts_layout(path)
    pat = _first_psi_section(path, 0, packet_size, sync_offset)
    pmt_pids = _parse_pat(pat)
    programs = tuple(
        _parse_pmt(
            _first_psi_section(path, pmt_pid, packet_size, sync_offset),
            pmt_pid,
            expected_program_number=program_number,
        )
        for program_number, pmt_pid in pmt_pids
    )
    if not programs:
        raise Avs3Error(f"No MPEG-TS programs found in {path}.")
    return TsInspection(packet_size=packet_size, sync_offset=sync_offset, programs=programs)


def inspect_mp4(input_path: str | Path) -> Mp4Inspection:
    path = Path(input_path)
    if not path.is_file():
        raise Avs3Error(f"MP4 input file not found: {path}")
    file_size = path.stat().st_size
    moov_offset = moov_size = moov_header = None
    with path.open("rb") as source:
        for offset, size, box_type, header_size in _iter_file_boxes(source, file_size):
            if box_type == b"moov":
                moov_offset, moov_size, moov_header = offset, size, header_size
                break
    if moov_offset is None or moov_size is None or moov_header is None:
        raise Avs3Error(f"MP4 movie metadata was not found in {path}.")
    with path.open("rb") as source:
        source.seek(moov_offset)
        moov = source.read(moov_size)
    if len(moov) != moov_size:
        raise Avs3Error(f"MP4 movie metadata is truncated in {path}.")

    tracks: list[Mp4AudioTrack] = []
    for trak in _child_boxes(moov, moov_header, len(moov), b"trak"):
        parsed = _mp4_audio_track(moov, trak)
        if parsed is not None:
            tracks.append(parsed)
    return Mp4Inspection(audio_tracks=tuple(tracks))


def extract_mp4_audio_vivid(
    input_path: str | Path,
    output_path: str | Path,
    *,
    track_id: int | None = None,
) -> Path:
    source = Path(input_path)
    inspection = inspect_mp4(source)
    candidates = [
        track
        for track in inspection.audio_tracks
        if track.codec == "audio-vivid" and (track_id is None or track.track_id == track_id)
    ]
    if not candidates:
        selector = f" track {track_id}" if track_id is not None else ""
        raise Avs3Error(f"MP4 has no Audio Vivid{selector} track.")
    if len(candidates) > 1:
        ids = ", ".join(str(track.track_id) for track in candidates)
        raise Avs3Error(f"MP4 has multiple Audio Vivid tracks ({ids}); select a track ID explicitly.")
    selected = candidates[0]
    if not selected.sample_ranges:
        raise Avs3Error(
            f"MP4 Audio Vivid track {selected.track_id} uses an unsupported fragmented sample layout."
        )
    output = _prepare_output_path(source, output_path)
    written = 0
    try:
        with source.open("rb") as media, output.open("wb") as target:
            for offset, size in selected.sample_ranges:
                media.seek(offset)
                payload = media.read(size)
                if len(payload) != size:
                    raise Avs3Error(
                        f"MP4 Audio Vivid sample at byte {offset} is truncated in {source}."
                    )
                target.write(payload)
                written += len(payload)
        if written == 0:
            raise Avs3Error(f"MP4 Audio Vivid track {selected.track_id} contained no samples.")
    except BaseException:
        _remove_failed_output(output)
        raise
    return output


def extract_mpeg_ts_stream(
    input_path: str | Path,
    output_path: str | Path,
    *,
    pid: int | None = None,
    codec: str | None = None,
) -> Path:
    source = Path(input_path)
    inspection = inspect_mpeg_ts(source)
    selected = _select_stream(inspection.streams, pid=pid, codec=codec)
    output = _prepare_output_path(source, output_path)
    written = 0
    try:
        with output.open("wb") as target:
            for packet in _iter_pes_packets(
                source,
                selected.pid,
                inspection.packet_size,
                inspection.sync_offset,
            ):
                payload = _pes_payload(packet)
                target.write(payload)
                written += len(payload)
        if written == 0:
            raise Avs3Error(f"MPEG-TS PID 0x{selected.pid:x} did not contain PES media data.")
    except BaseException:
        _remove_failed_output(output)
        raise
    return output


def decode_audio_vivid(
    input_path: str | Path,
    output_path: str | Path,
    *,
    decoder: str | Path | None = None,
    decoder_args: str | None = None,
) -> Path:
    executable = _find_decoder(
        decoder,
        env_name="UNIDOWN_AUDIO_VIVID_DECODER",
        candidates=("avs3Decoder", "avs3RM0Decoder", "audio-vivid-decoder"),
        label="Audio Vivid",
    )
    source = Path(input_path).resolve()
    output = _prepare_output_path(source, output_path)
    arguments = decoder_args
    if arguments is None:
        arguments = os.environ.get("UNIDOWN_AUDIO_VIVID_DECODER_ARGS", "{input} {output}")
    command = _decoder_command(executable, arguments, source, output)
    try:
        _run_decoder(
            command,
            cwd=executable.parent,
            label="Audio Vivid",
        )
        _validate_wav_output(output, "Audio Vivid")
    except BaseException:
        _remove_failed_output(output)
        raise
    return output


def decode_avs3_video(
    input_path: str | Path,
    output_path: str | Path,
    *,
    decoder: str | Path | None = None,
    threads: int | None = None,
    frames: int | None = None,
) -> Path:
    executable = _find_decoder(
        decoder,
        env_name="UNIDOWN_UAVS3D",
        candidates=("uavs3dec", "uavs3d"),
        label="AVS3-P2 video",
    )
    source = Path(input_path).resolve()
    output = _prepare_output_path(source, output_path)
    command = [str(executable), "-i", str(source), "-o", str(output), "-l", "1"]
    if threads is not None:
        command.extend(["-t", str(max(1, int(threads)))])
    if frames is not None:
        command.extend(["-f", str(max(1, int(frames)))])
    try:
        _run_decoder(command, cwd=executable.parent, label="AVS3-P2 video")
        _validate_binary_output(output, "AVS3-P2 video")
    except BaseException:
        _remove_failed_output(output)
        raise
    return output


def decode_mpeg_ts_audio_vivid(
    input_path: str | Path,
    output_path: str | Path,
    *,
    pid: int | None = None,
    decoder: str | Path | None = None,
    decoder_args: str | None = None,
    elementary_output: str | Path | None = None,
) -> Path:
    if elementary_output is not None:
        elementary = extract_mpeg_ts_stream(
            input_path,
            elementary_output,
            pid=pid,
            codec="audio-vivid" if pid is None else None,
        )
        return decode_audio_vivid(elementary, output_path, decoder=decoder, decoder_args=decoder_args)
    with tempfile.TemporaryDirectory(prefix="unidown_audio_vivid_") as temp_dir:
        elementary = extract_mpeg_ts_stream(
            input_path,
            Path(temp_dir) / "audio.av3a",
            pid=pid,
            codec="audio-vivid" if pid is None else None,
        )
        return decode_audio_vivid(elementary, output_path, decoder=decoder, decoder_args=decoder_args)


def decode_mp4_audio_vivid(
    input_path: str | Path,
    output_path: str | Path,
    *,
    track_id: int | None = None,
    decoder: str | Path | None = None,
    decoder_args: str | None = None,
    elementary_output: str | Path | None = None,
) -> Path:
    if elementary_output is not None:
        elementary = extract_mp4_audio_vivid(
            input_path,
            elementary_output,
            track_id=track_id,
        )
        return decode_audio_vivid(elementary, output_path, decoder=decoder, decoder_args=decoder_args)
    with tempfile.TemporaryDirectory(prefix="unidown_audio_vivid_mp4_") as temp_dir:
        elementary = extract_mp4_audio_vivid(
            input_path,
            Path(temp_dir) / "audio.av3a",
            track_id=track_id,
        )
        return decode_audio_vivid(elementary, output_path, decoder=decoder, decoder_args=decoder_args)


def decode_mpeg_ts_avs3_video(
    input_path: str | Path,
    output_path: str | Path,
    *,
    pid: int | None = None,
    decoder: str | Path | None = None,
    elementary_output: str | Path | None = None,
    threads: int | None = None,
    frames: int | None = None,
) -> Path:
    if elementary_output is not None:
        elementary = extract_mpeg_ts_stream(
            input_path,
            elementary_output,
            pid=pid,
            codec="avs3-video" if pid is None else None,
        )
        return decode_avs3_video(elementary, output_path, decoder=decoder, threads=threads, frames=frames)
    with tempfile.TemporaryDirectory(prefix="unidown_avs3_video_") as temp_dir:
        elementary = extract_mpeg_ts_stream(
            input_path,
            Path(temp_dir) / "video.avs3",
            pid=pid,
            codec="avs3-video" if pid is None else None,
        )
        return decode_avs3_video(elementary, output_path, decoder=decoder, threads=threads, frames=frames)


def _detect_ts_layout(path: Path) -> tuple[int, int]:
    if not path.exists():
        raise Avs3Error(f"MPEG-TS input file not found: {path}")
    with path.open("rb") as source:
        probe = source.read(64 * 1024)
    best: tuple[int, int, int] | None = None
    for packet_size in (188, 192, 204):
        for sync_offset in range(min(packet_size, len(probe))):
            matches = 0
            position = sync_offset
            while position < len(probe) and probe[position] == 0x47:
                matches += 1
                position += packet_size
            if best is None or matches > best[0]:
                best = (matches, packet_size, sync_offset)
    if best is None or best[0] < 4:
        raise Avs3Error(f"Input is not a supported 188/192/204-byte MPEG transport stream: {path}")
    return best[1], best[2]


def _iter_file_boxes(source, end: int, start: int = 0):
    offset = start
    while offset + 8 <= end:
        source.seek(offset)
        header = source.read(16)
        if len(header) < 8:
            return
        size = int.from_bytes(header[:4], "big")
        box_type = header[4:8]
        header_size = 8
        if size == 1:
            if len(header) < 16:
                return
            size = int.from_bytes(header[8:16], "big")
            header_size = 16
        elif size == 0:
            size = end - offset
        if size < header_size or offset + size > end:
            raise Avs3Error(f"Invalid MP4 {box_type.decode('latin-1')} box at byte {offset}.")
        yield offset, size, box_type, header_size
        offset += size


def _child_boxes(data: bytes, start: int, end: int, wanted: bytes | None = None):
    offset = start
    while offset + 8 <= end:
        size = int.from_bytes(data[offset : offset + 4], "big")
        box_type = data[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > end:
                return
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = end - offset
        if size < header_size or offset + size > end:
            return
        if wanted is None or box_type == wanted:
            yield offset, size, box_type, header_size
        offset += size


def _box_path(data: bytes, parent, *names: bytes):
    current = parent
    for name in names:
        children_start = current[0] + current[3]
        if current[2] == b"meta":
            children_start += 4
        current = next(_child_boxes(data, children_start, current[0] + current[1], name), None)
        if current is None:
            return None
    return current


def _mp4_audio_track(data: bytes, trak) -> Mp4AudioTrack | None:
    mdia = _box_path(data, trak, b"mdia")
    hdlr = _box_path(data, mdia, b"hdlr") if mdia is not None else None
    if hdlr is None:
        return None
    payload = hdlr[0] + hdlr[3]
    if payload + 12 > hdlr[0] + hdlr[1] or data[payload + 8 : payload + 12] != b"soun":
        return None
    tkhd = _box_path(data, trak, b"tkhd")
    if tkhd is None:
        return None
    tkhd_payload = tkhd[0] + tkhd[3]
    version = data[tkhd_payload] if tkhd_payload < len(data) else 0
    track_id_offset = tkhd_payload + (20 if version == 1 else 12)
    if track_id_offset + 4 > tkhd[0] + tkhd[1]:
        return None
    track_id = int.from_bytes(data[track_id_offset : track_id_offset + 4], "big")
    stbl = _box_path(data, mdia, b"minf", b"stbl")
    if stbl is None:
        return None
    stsd = _box_path(data, stbl, b"stsd")
    if stsd is None:
        return None
    stsd_payload = stsd[0] + stsd[3]
    if stsd_payload + 16 > stsd[0] + stsd[1]:
        return None
    sample_entry = data[stsd_payload + 12 : stsd_payload + 16].decode("latin-1")
    codec = _mp4_audio_codec(sample_entry)
    ranges: list[tuple[int, int]] = []
    if codec == "audio-vivid":
        try:
            ranges = _mp4_sample_ranges(data, stbl)
        except Avs3Error:
            ranges = []
    return Mp4AudioTrack(
        track_id=track_id,
        codec=codec,
        sample_entry=sample_entry,
        sample_ranges=tuple(ranges),
    )


def _mp4_audio_codec(sample_entry: str) -> str:
    normalized = sample_entry.lower()
    if normalized in {"av3a", "avsa"}:
        return "audio-vivid"
    if normalized in {"ec-3", "dec3", "eac3"}:
        return "e-ac-3"
    if normalized in {"ac-3", "dac3"}:
        return "ac-3"
    if normalized in {"mp4a", "aac "}:
        return "aac"
    return normalized or "unknown"


def _mp4_sample_ranges(data: bytes, stbl) -> list[tuple[int, int]]:
    stsz = _box_path(data, stbl, b"stsz")
    stsc = _box_path(data, stbl, b"stsc")
    stco = _box_path(data, stbl, b"stco")
    co64 = _box_path(data, stbl, b"co64")
    if stsz is None or stsc is None or (stco is None and co64 is None):
        raise Avs3Error("Fragmented MP4 Audio Vivid extraction is not supported yet; expected stsz/stsc/stco tables.")
    sample_sizes = _mp4_sample_sizes(data, stsz)
    chunk_map = _mp4_sample_to_chunk(data, stsc)
    chunk_offsets = _mp4_chunk_offsets(data, co64 or stco, wide=co64 is not None)
    ranges: list[tuple[int, int]] = []
    sample_index = 0
    for chunk_number, chunk_offset in enumerate(chunk_offsets, start=1):
        samples_per_chunk = _samples_per_chunk(chunk_map, chunk_number)
        offset = chunk_offset
        for _ in range(samples_per_chunk):
            if sample_index >= len(sample_sizes):
                break
            size = sample_sizes[sample_index]
            ranges.append((offset, size))
            offset += size
            sample_index += 1
    if sample_index != len(sample_sizes):
        raise Avs3Error("MP4 Audio Vivid sample tables did not map every sample to a chunk.")
    return ranges


def _mp4_sample_sizes(data: bytes, box) -> list[int]:
    payload = box[0] + box[3]
    if payload + 12 > box[0] + box[1]:
        raise Avs3Error("MP4 stsz box is truncated.")
    sample_size = int.from_bytes(data[payload + 4 : payload + 8], "big")
    count = int.from_bytes(data[payload + 8 : payload + 12], "big")
    if sample_size:
        return [sample_size] * count
    end = payload + 12 + count * 4
    if end > box[0] + box[1]:
        raise Avs3Error("MP4 stsz sample table is truncated.")
    return [int.from_bytes(data[offset : offset + 4], "big") for offset in range(payload + 12, end, 4)]


def _mp4_sample_to_chunk(data: bytes, box) -> list[tuple[int, int]]:
    payload = box[0] + box[3]
    if payload + 8 > box[0] + box[1]:
        raise Avs3Error("MP4 stsc box is truncated.")
    count = int.from_bytes(data[payload + 4 : payload + 8], "big")
    end = payload + 8 + count * 12
    if end > box[0] + box[1]:
        raise Avs3Error("MP4 stsc sample table is truncated.")
    return [
        (
            int.from_bytes(data[offset : offset + 4], "big"),
            int.from_bytes(data[offset + 4 : offset + 8], "big"),
        )
        for offset in range(payload + 8, end, 12)
    ]


def _mp4_chunk_offsets(data: bytes, box, *, wide: bool) -> list[int]:
    payload = box[0] + box[3]
    if payload + 8 > box[0] + box[1]:
        raise Avs3Error("MP4 chunk offset box is truncated.")
    count = int.from_bytes(data[payload + 4 : payload + 8], "big")
    width = 8 if wide else 4
    end = payload + 8 + count * width
    if end > box[0] + box[1]:
        raise Avs3Error("MP4 chunk offset table is truncated.")
    return [int.from_bytes(data[offset : offset + width], "big") for offset in range(payload + 8, end, width)]


def _samples_per_chunk(entries: list[tuple[int, int]], chunk_number: int) -> int:
    selected = 0
    for first_chunk, samples_per_chunk in entries:
        if first_chunk > chunk_number:
            break
        selected = samples_per_chunk
    if selected <= 0:
        raise Avs3Error(f"MP4 stsc has no mapping for chunk {chunk_number}.")
    return selected


def _iter_ts_packets(path: Path, packet_size: int, sync_offset: int) -> Iterable[bytes]:
    with path.open("rb") as source:
        source.seek(sync_offset)
        while True:
            raw = source.read(packet_size)
            if not raw:
                return
            if len(raw) < TS_PACKET_SIZE:
                return
            packet = raw[:TS_PACKET_SIZE]
            if packet[0] != 0x47:
                raise Avs3Error(f"MPEG-TS sync was lost at byte {source.tell() - len(raw)} in {path}.")
            yield packet


def _packet_payload(packet: bytes) -> tuple[int, bool, bytes] | None:
    if packet[0] != 0x47 or packet[1] & 0x80:
        return None
    pid = ((packet[1] & 0x1F) << 8) | packet[2]
    payload_unit_start = bool(packet[1] & 0x40)
    adaptation_control = (packet[3] >> 4) & 0x03
    if adaptation_control not in {1, 3}:
        return None
    offset = 4
    if adaptation_control == 3:
        offset += 1 + packet[4]
    if offset >= TS_PACKET_SIZE:
        return None
    return pid, payload_unit_start, packet[offset:]


def _first_psi_section(path: Path, pid: int, packet_size: int, sync_offset: int) -> bytes:
    buffer = bytearray()
    for packet in _iter_ts_packets(path, packet_size, sync_offset):
        parsed = _packet_payload(packet)
        if parsed is None or parsed[0] != pid:
            continue
        _, payload_unit_start, payload = parsed
        if payload_unit_start:
            if not payload:
                continue
            pointer = payload[0]
            payload = payload[1:]
            if pointer > len(payload):
                buffer.clear()
                continue
            if buffer and pointer:
                buffer.extend(payload[:pointer])
                section = _complete_psi_section(buffer)
                if section is not None:
                    return section
            buffer = bytearray(payload[pointer:])
        else:
            buffer.extend(payload)
        section = _complete_psi_section(buffer)
        if section is not None:
            return section
    raise Avs3Error(f"MPEG-TS PSI section for PID 0x{pid:x} was not found in {path}.")


def _complete_psi_section(buffer: bytearray) -> bytes | None:
    while buffer and buffer[0] == 0xFF:
        del buffer[0]
    if len(buffer) < 3:
        return None
    total = 3 + (((buffer[1] & 0x0F) << 8) | buffer[2])
    if len(buffer) < total:
        return None
    return bytes(buffer[:total])


def _parse_pat(section: bytes) -> list[tuple[int, int]]:
    if len(section) < 12 or section[0] != 0x00:
        raise Avs3Error("Invalid MPEG-TS program association table.")
    end = 3 + (((section[1] & 0x0F) << 8) | section[2]) - 4
    programs: list[tuple[int, int]] = []
    for offset in range(8, end, 4):
        if offset + 4 > len(section):
            break
        program_number = int.from_bytes(section[offset : offset + 2], "big")
        pmt_pid = ((section[offset + 2] & 0x1F) << 8) | section[offset + 3]
        if program_number:
            programs.append((program_number, pmt_pid))
    return programs


def _parse_pmt(section: bytes, pmt_pid: int, *, expected_program_number: int) -> TsProgram:
    if len(section) < 16 or section[0] != 0x02:
        raise Avs3Error(f"Invalid MPEG-TS program map table on PID 0x{pmt_pid:x}.")
    program_number = int.from_bytes(section[3:5], "big")
    if program_number != expected_program_number:
        raise Avs3Error(
            f"MPEG-TS PMT PID 0x{pmt_pid:x} described program {program_number}, expected {expected_program_number}."
        )
    section_end = 3 + (((section[1] & 0x0F) << 8) | section[2]) - 4
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    offset = 12 + program_info_length
    streams: list[TsElementaryStream] = []
    while offset + 5 <= section_end:
        stream_type = section[offset]
        elementary_pid = ((section[offset + 1] & 0x1F) << 8) | section[offset + 2]
        info_length = ((section[offset + 3] & 0x0F) << 8) | section[offset + 4]
        descriptor_data = section[offset + 5 : offset + 5 + info_length]
        streams.append(
            TsElementaryStream(
                pid=elementary_pid,
                stream_type=stream_type,
                descriptors=tuple(_parse_descriptors(descriptor_data)),
            )
        )
        offset += 5 + info_length
    return TsProgram(
        program_number=program_number,
        pmt_pid=pmt_pid,
        pcr_pid=pcr_pid,
        streams=tuple(streams),
    )


def _parse_descriptors(data: bytes) -> Iterable[TsDescriptor]:
    offset = 0
    while offset + 2 <= len(data):
        length = data[offset + 1]
        end = offset + 2 + length
        if end > len(data):
            return
        yield TsDescriptor(tag=data[offset], data=data[offset + 2 : end])
        offset = end


def _select_stream(
    streams: tuple[TsElementaryStream, ...],
    *,
    pid: int | None,
    codec: str | None,
) -> TsElementaryStream:
    if pid is not None:
        matches = [stream for stream in streams if stream.pid == pid]
    elif codec:
        normalized = codec.strip().lower()
        matches = [stream for stream in streams if stream.codec == normalized]
    else:
        raise Avs3Error("Select an MPEG-TS stream with pid= or codec=.")
    if not matches:
        selector = f"PID 0x{pid:x}" if pid is not None else f"codec {codec}"
        raise Avs3Error(f"MPEG-TS has no stream matching {selector}.")
    if len(matches) > 1:
        selector = f"codec {codec}"
        pids = ", ".join(f"0x{stream.pid:x}" for stream in matches)
        raise Avs3Error(f"MPEG-TS has multiple streams matching {selector}: {pids}; select a PID explicitly.")
    return matches[0]


def _iter_pes_packets(
    path: Path,
    pid: int,
    packet_size: int,
    sync_offset: int,
) -> Iterable[bytes]:
    current = bytearray()
    for packet in _iter_ts_packets(path, packet_size, sync_offset):
        parsed = _packet_payload(packet)
        if parsed is None or parsed[0] != pid:
            continue
        _, payload_unit_start, payload = parsed
        if payload_unit_start and current:
            yield bytes(current)
            current.clear()
        current.extend(payload)
    if current:
        yield bytes(current)


def _pes_payload(packet: bytes) -> bytes:
    if len(packet) < 6 or packet[:3] != b"\x00\x00\x01":
        raise Avs3Error("MPEG-TS stream contains an invalid PES packet.")
    stream_id = packet[3]
    packet_length = int.from_bytes(packet[4:6], "big")
    packet_end = min(len(packet), 6 + packet_length) if packet_length else len(packet)
    if stream_id in {0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xFF, 0xF2, 0xF8}:
        payload_offset = 6
    else:
        if len(packet) < 9:
            raise Avs3Error("MPEG-TS stream contains a truncated PES optional header.")
        payload_offset = 9 + packet[8]
    if payload_offset > packet_end:
        raise Avs3Error("MPEG-TS stream contains an invalid PES header length.")
    return packet[payload_offset:packet_end]


def _find_decoder(
    explicit: str | Path | None,
    *,
    env_name: str,
    candidates: tuple[str, ...],
    label: str,
) -> Path:
    requested = str(explicit or os.environ.get(env_name) or "").strip()
    if requested:
        resolved = shutil.which(requested)
        path = Path(resolved or requested).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
        raise Avs3Error(f"{label} decoder is not executable: {path}")
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return Path(resolved).resolve()
    names = ", ".join(candidates)
    raise Avs3Error(
        f"{label} decoder not found. Set {env_name} or install one of these executables in PATH: {names}."
    )


def _run_decoder(command: list[str], *, cwd: Path, label: str) -> None:
    try:
        result = managed_run(command, cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        raise Avs3Error(f"{label} decoder failed to start: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise Avs3Error(f"{label} decoder failed with status {result.returncode}: {detail}")


def _prepare_output_path(input_path: str | Path, output_path: str | Path) -> Path:
    source = Path(input_path).resolve()
    output = Path(output_path).expanduser().resolve()
    if source == output:
        raise Avs3Error(f"Input and output paths must be different: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.unlink(missing_ok=True)
    except OSError as exc:
        raise Avs3Error(f"Could not replace output file {output}: {exc}") from exc
    return output


def _remove_failed_output(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _decoder_command(
    executable: Path,
    arguments: str,
    input_path: Path,
    output_path: Path,
) -> list[str]:
    try:
        tokens = shlex.split(arguments)
    except ValueError as exc:
        raise Avs3Error(f"Invalid Audio Vivid decoder argument template: {exc}") from exc
    if not tokens:
        raise Avs3Error("Audio Vivid decoder argument template is empty.")
    fields = {"input": str(input_path), "output": str(output_path)}
    try:
        command = [str(executable), *(token.format_map(fields) for token in tokens)]
    except (KeyError, ValueError) as exc:
        raise Avs3Error(f"Invalid Audio Vivid decoder argument template: {exc}") from exc
    if not any("{input}" in token for token in tokens) or not any("{output}" in token for token in tokens):
        raise Avs3Error("Audio Vivid decoder argument template must contain {input} and {output}.")
    return command


def _validate_binary_output(path: Path, label: str) -> None:
    if not path.exists() or path.stat().st_size == 0:
        raise Avs3Error(f"{label} decoder produced an empty output: {path}")


def _validate_wav_output(path: Path, label: str) -> None:
    _validate_binary_output(path, label)
    with path.open("rb") as source:
        header = source.read(12)
    if len(header) < 12 or header[:4] not in {b"RIFF", b"RF64"} or header[8:12] != b"WAVE":
        raise Avs3Error(f"{label} decoder did not produce a WAV file: {path}")


def _parse_pid(value: str) -> int:
    try:
        pid = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid PID: {value}") from exc
    if not 0 <= pid <= 0x1FFF:
        raise argparse.ArgumentTypeError("PID must be between 0 and 0x1fff")
    return pid


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect, extract, and decode AVS3/Audio Vivid media without FFmpeg.")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_command = commands.add_parser("inspect", help="Inspect MPEG-TS programs and stream descriptors.")
    inspect_command.add_argument("input")

    extract_command = commands.add_parser("extract", help="Extract one MPEG-TS elementary stream.")
    extract_command.add_argument("input")
    extract_command.add_argument("output")
    extract_command.add_argument("--pid", type=_parse_pid)
    extract_command.add_argument("--codec", choices=("audio-vivid", "avs3-video"))

    audio_command = commands.add_parser("decode-audio", help="Decode Audio Vivid elementary audio to WAV.")
    audio_command.add_argument("input")
    audio_command.add_argument("output")
    audio_command.add_argument("--decoder")
    audio_command.add_argument(
        "--decoder-args",
        help="Decoder arguments containing {input} and {output}; defaults to '{input} {output}'.",
    )
    audio_command.add_argument("--pid", type=_parse_pid)
    audio_command.add_argument("--elementary-output")
    audio_command.add_argument("--ts", action="store_true", help="Treat input as MPEG-TS and extract Audio Vivid first.")

    video_command = commands.add_parser("decode-video", help="Decode AVS3-P2 elementary video to planar YUV.")
    video_command.add_argument("input")
    video_command.add_argument("output")
    video_command.add_argument("--decoder")
    video_command.add_argument("--pid", type=_parse_pid)
    video_command.add_argument("--elementary-output")
    video_command.add_argument("--threads", type=int)
    video_command.add_argument("--frames", type=int)
    video_command.add_argument("--ts", action="store_true", help="Treat input as MPEG-TS and extract AVS3 video first.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            inspection = inspect_mpeg_ts(args.input)
            print(f"packet size: {inspection.packet_size}")
            for program in inspection.programs:
                print(
                    f"program {program.program_number}: PMT 0x{program.pmt_pid:x}, PCR 0x{program.pcr_pid:x}"
                )
                for stream in program.streams:
                    registration = f", registration {stream.registration}" if stream.registration else ""
                    print(
                        f"  PID 0x{stream.pid:x}: {stream.codec}, stream type 0x{stream.stream_type:02x}{registration}"
                    )
        elif args.command == "extract":
            if args.pid is None and args.codec is None:
                raise Avs3Error("extract needs --pid or --codec.")
            print(extract_mpeg_ts_stream(args.input, args.output, pid=args.pid, codec=args.codec))
        elif args.command == "decode-audio":
            if args.ts:
                result = decode_mpeg_ts_audio_vivid(
                    args.input,
                    args.output,
                    pid=args.pid,
                    decoder=args.decoder,
                    decoder_args=args.decoder_args,
                    elementary_output=args.elementary_output,
                )
            else:
                result = decode_audio_vivid(
                    args.input,
                    args.output,
                    decoder=args.decoder,
                    decoder_args=args.decoder_args,
                )
            print(result)
        elif args.command == "decode-video":
            if args.ts:
                result = decode_mpeg_ts_avs3_video(
                    args.input,
                    args.output,
                    pid=args.pid,
                    decoder=args.decoder,
                    elementary_output=args.elementary_output,
                    threads=args.threads,
                    frames=args.frames,
                )
            else:
                result = decode_avs3_video(
                    args.input,
                    args.output,
                    decoder=args.decoder,
                    threads=args.threads,
                    frames=args.frames,
                )
            print(result)
        return 0
    except Avs3Error as exc:
        parser = _build_parser()
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
