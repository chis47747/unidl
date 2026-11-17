"""Timestamped AVC/HEVC SEI scanning and CEA-708 service decoding."""
from __future__ import annotations

import contextlib
import heapq
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
from itertools import count, pairwise
from pathlib import Path

from .embedding import DownloadCancelled, DownloadRuntime, current_download_runtime, managed_popen, managed_run
from .subtitles import _parse_text_subtitle, _write_subtitle

_RCWT_HEADER = bytes.fromhex("ccccedcc00940001000000")
_ANNEX_B = re.compile(rb"\x00\x00(?:\x00)?\x01")
_EMULATION = re.compile(rb"\x00\x00\x03(?=[\x00-\x03])")
_SERVICE_FILE = re.compile(r"decoded(?:\.p\d+)?\.svc(\d{1,2})\.srt", re.I)
_PACKET_LINE_LIMIT = 1024 * 1024
_REORDER_PACKETS = 64


class Cea708Error(RuntimeError):
    """An unavailable or unextractable CEA-708 track, not a carrier failure."""


def service_number(instream_id: object) -> int | None:
    match = re.fullmatch(r"SERVICE([1-9][0-9]?)", str(instream_id or ""))
    number = int(match[1]) if match else 0
    return number if 1 <= number <= 63 else None


def unavailable_reason(*, live: bool = False, raw: bool = False) -> str:
    if live:
        return "CEA-708 extraction currently requires finite VOD."
    if raw:
        return "CEA-708 requires a text subtitle format; original-track output is not supported."
    if not shutil.which("ccextractor"):
        return "CEA-708 requires the optional CCExtractor executable."
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return "CEA-708 also requires FFmpeg and ffprobe."
    return ""


def _check_cancelled() -> None:
    runtime = current_download_runtime()
    if runtime is not None:
        runtime.checkpoint()


def _sei_triplets(packet: bytes, codec: str, *, valid_types: tuple[int, ...] = (6, 7)) -> bytes:
    """Read selected valid cc_data types from ATSC registered SEI."""
    matches = list(_ANNEX_B.finditer(packet))
    result = bytearray()
    hevc = codec == "hevc"
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(packet)
        nal = packet[match.end():end]
        header_width = 2 if hevc else 1
        if len(nal) <= header_width:
            continue
        kind = (nal[0] >> 1) & 63 if hevc else nal[0] & 31
        if kind not in ({39, 40} if hevc else {6}):
            continue
        rbsp = _EMULATION.sub(b"\x00\x00", nal[header_width:])
        at = 0
        while at + 1 < len(rbsp):
            payload_type = payload_size = 0
            while at < len(rbsp) and rbsp[at] == 255:
                payload_type += 255
                at += 1
            if at >= len(rbsp):
                break
            payload_type += rbsp[at]
            at += 1
            while at < len(rbsp) and rbsp[at] == 255:
                payload_size += 255
                at += 1
            if at >= len(rbsp):
                break
            payload_size += rbsp[at]
            at += 1
            end_at = at + payload_size
            if end_at > len(rbsp):
                break
            if payload_type == 4 and payload_size >= 10 and rbsp[at:at + 8] == b"\xb5\x00\x31GA94\x03":
                header = rbsp[at + 8]
                start = at + 10
                stop = start + (header & 31) * 3
                if header & 0x40 and stop < end_at and rbsp[stop] == 255:
                    for i in range(start, stop, 3):
                        if rbsp[i] & 7 in valid_types:
                            result.extend(rbsp[i:i + 3])
            at = end_at
    return bytes(result)


def _packet_data(line: bytes) -> tuple[int, bytes] | None:
    fields, found, dump = line.partition(b"|data=")
    if not found or not fields.startswith(b"pts_time="):
        return None
    try:
        pts_ms = round(float(fields[9:]) * 1000)
        # ffprobe's compact writer escapes the newline before each hex-dump row.
        # Match only the hex columns, not the ASCII representation.
        data = bytes.fromhex(b"".join(
            row[10:49].split(b"  ", 1)[0].replace(b" ", b"")
            for row in dump.split(b"\\n")
            if re.match(rb"^[0-9a-fA-F]{8}: ", row)
        ).decode("ascii"))
    except (ValueError, OverflowError) as exc:
        raise Cea708Error("Invalid timestamp or packet dump in the CEA-708 carrier.") from exc
    return pts_ms, data


class _Cc1Filter:
    """Only addressed field-one channel 1, never CC2 or 708 compatibility guesses."""

    def __init__(self) -> None:
        self.cc1 = False

    def __call__(self, triplets: bytes) -> bytes:
        result = bytearray()
        for at in range(0, len(triplets), 3):
            tag, first, second = triplets[at:at + 3]
            if tag & 7 != 4 or first.bit_count() % 2 != 1 or second.bit_count() % 2 != 1:
                continue
            byte = first & 0x7f
            if 0x10 <= byte <= 0x1f:
                self.cc1 = not bool(byte & 8)
            if self.cc1 and byte:
                result.extend((tag, first, second))
        return bytes(result)


def _write_rcwt(
    ffprobe: str, projected: Path, destination: Path, codec: str, origin_ms: int, *, cc1: bool = False,
) -> int:
    """Stream small SEI packets into presentation order without buffering video."""
    command = [
        ffprobe, "-v", "fatal", "-probesize", "32768", "-analyzeduration", "0",
        "-select_streams", "v:0", "-show_packets", "-show_data", "-show_entries",
        "packet=pts_time,data", "-of", "compact=p=0:nk=0", str(projected),
    ]
    pending: list[tuple[int, int, bytes]] = []
    last_written = -1
    last_packet = -1
    runtime = current_download_runtime()
    channel = _Cc1Filter() if cc1 else None
    caption = "CC1" if cc1 else "CEA-708"
    with destination.open("wb") as output:
        output.write(_RCWT_HEADER)

        def flush() -> None:
            nonlocal last_written
            pts_ms, _, payload = heapq.heappop(pending)
            if channel is not None:
                payload = channel(payload)
                if not payload:
                    return
            if pts_ms < last_written or len(payload) // 3 > 65535:
                raise Cea708Error("CEA-708 packet timestamps or record sizes exceed supported bounds.")
            output.write(struct.pack("<qH", pts_ms, len(payload) // 3))
            output.write(payload)
            last_written = pts_ms

        process = managed_popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            assert process.stdout is not None
            for index in count():
                line = process.stdout.readline(_PACKET_LINE_LIMIT + 1)
                if not line:
                    break
                if len(line) > _PACKET_LINE_LIMIT:
                    raise Cea708Error("CEA-708 packet metadata exceeds the scan limit.")
                if index % 256 == 0:
                    _check_cancelled()
                record = _packet_data(line)
                if record is None:
                    continue
                pts_ms, data = record
                elapsed = max(0, pts_ms - origin_ms)
                last_packet = max(last_packet, elapsed)
                if payload := _sei_triplets(data, codec, valid_types=(4,) if cc1 else (6, 7)):
                    heapq.heappush(pending, (elapsed, index, payload))
                # Flush according to video packet order as well as heap size:
                # the bound applies to video reordering, not subtitle density.
                while pending and (len(pending) > _REORDER_PACKETS or index - pending[0][1] >= _REORDER_PACKETS):
                    flush()
            returncode = process.wait()
            _check_cancelled()
            if returncode:
                raise Cea708Error(f"CEA-708 packet scan failed (ffprobe status {returncode}).")
        finally:
            if process.poll() is None:
                DownloadRuntime._terminate_process(process, runtime is not None and os.name == "posix")
                with contextlib.suppress(OSError):
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if runtime is not None:
                runtime.unregister_process(process)
        while pending:
            flush()
    if last_written < 0:
        raise Cea708Error(f"No {caption} subtitle packets were found in the video carrier.")
    return last_packet


def write_sei_rcwt(ffmpeg: str, ffprobe: str, source: Path, work: Path, *, cc1: bool = False) -> tuple[Path, int]:
    from .postprocess import _run_external

    metadata = managed_run([
        ffprobe, "-v", "error", "-select_streams", "V:0", "-show_entries",
        "stream=index,codec_name,start_time:format=start_time", "-of", "json", str(source),
    ], capture_output=True, timeout=30)
    _check_cancelled()
    if metadata.returncode:
        raise Cea708Error("Could not inspect the SEI caption carrier.")
    info = json.loads(metadata.stdout)
    video = next(iter(info.get("streams", [])), {})
    codec = video.get("codec_name")
    if codec not in {"h264", "hevc"}:
        raise Cea708Error("SEI caption extraction currently supports AVC/HEVC video carriers.")
    start_time = info.get("format", {}).get("start_time") or video.get("start_time")
    origin_ms = round(float(start_time or 0) * 1000)
    # Stream-copy only SEI units. NUT retains packet boundaries and the source time base.
    filter_types = "39|40" if codec == "hevc" else "6"
    projected = work / "sei.nut"
    try:
        _run_external([
            ffmpeg, "-v", "error", "-nostdin", "-y", "-copyts", "-i", str(source),
            "-map", f"0:{int(video['index'])}", "-c:v", "copy",
            "-bsf:v", f"{codec}_mp4toannexb,filter_units=pass_types={filter_types}",
            "-map_metadata", "-1", "-avoid_negative_ts", "disabled", "-f", "nut", str(projected),
        ], "caption SEI projection", reject_demux_errors=True)
        _check_cancelled()
        rcwt = work / "cc.bin"
        return rcwt, _write_rcwt(ffprobe, projected, rcwt, codec, origin_ms, cc1=cc1)
    finally:
        projected.unlink(missing_ok=True)


def _read_service_file(path: Path, last_packet_ms: int):
    if path.stat().st_size > 32 * 1024 * 1024:
        return []
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Some CCExtractor builds still emit ISO-8859-1 for 708 despite UTF-8 defaults.
        text = data.decode("latin-1")
    cues = _parse_text_subtitle(text, ".srt")
    if not cues or any(
        not (math.isfinite(cue.start) and math.isfinite(cue.end))
        or cue.start < 0 or cue.end <= cue.start or not cue.text.strip()
        or cue.end > last_packet_ms / 1000 + 2
        for cue in cues
    ) or any(a.start > b.start for a, b in pairwise(cues)):
        return []
    return cues


def extract_services(source: Path, destination: Path, services: tuple[int, ...]) -> dict[int, Path]:
    """Scan a shared carrier once, and accept only validated, service-tagged SRTs."""
    if not services or any(not 1 <= number <= 63 for number in services):
        raise ValueError("Invalid CEA-708 service numbers.")
    if reason := unavailable_reason():
        raise Cea708Error(reason)
    ffmpeg, ffprobe, decoder = (shutil.which(name) for name in ("ffmpeg", "ffprobe", "ccextractor"))
    assert ffmpeg and ffprobe and decoder

    try:
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cc-", dir=destination) as tmp:
            work = Path(tmp)
            rcwt, last_packet_ms = write_sei_rcwt(ffmpeg, ffprobe, source, work)
            decoded = managed_run([
                decoder, str(rcwt), "--service", ",".join(map(str, sorted(set(services)))),
                "-o", str(work / "decoded.srt"),
            ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
            _check_cancelled()
            # Legacy C builds return NO_CAPTIONS (10) after producing digital
            # captions if no 608 captions were seen. Validate the files, not
            # that status alone. Other errors are never treated as success.
            if decoded.returncode not in {0, 10}:
                raise Cea708Error(f"CCExtractor failed (status {decoded.returncode}).")
            by_service: dict[int, list[Path]] = {}
            for path in work.iterdir():
                match = _SERVICE_FILE.fullmatch(path.name)
                if match and (number := int(match[1])) in services and path.is_file():
                    by_service.setdefault(number, []).append(path)
            result = {}
            for number, paths in by_service.items():
                if len(paths) != 1 or not (cues := _read_service_file(paths[0], last_packet_ms)):
                    continue
                normalized = work / f"service-{number}.srt"
                _write_subtitle(normalized, cues, "srt")
                output = destination / normalized.name
                normalized.replace(output)
                result[number] = output
            return result
    except (Cea708Error, DownloadCancelled):
        raise
    except (OSError, RuntimeError, ValueError, OverflowError, subprocess.SubprocessError) as exc:
        raise Cea708Error(f"Could not extract CEA-708 from the video carrier: {exc}") from exc
