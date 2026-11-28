"""HEVC HDR10+ SEI / Dolby Vision RPU probing, VUI normalization & HLS DV helpers."""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from collections.abc import Callable, Iterable
from copy import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .embedding import current_download_runtime, managed_run
from .utils import WINDOWS_INTERMEDIATE_PATH_LIMIT, bounded_path


@dataclass(frozen=True, slots=True)
class HdrProbeResult:
    """Dynamic-range signals detected directly in an HEVC bitstream sample."""

    has_hdr10plus: bool = False
    has_dv_rpu: bool = False
    nal_units_scanned: int = 0
    upgraded_range: str = ""


def _remove_epb(data: bytes) -> bytes:
    """Strip H.265/HEVC emulation prevention bytes (``00 00 03`` -> ``00 00``)."""
    if b"\x00\x00\x03" not in data:
        return data
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        if i + 2 < n and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
            out.extend((0, 0))
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def _iter_annexb_nals(data: bytes) -> Iterable[bytes]:
    """Yield Annex-B NAL unit payloads split by 3-byte or 4-byte start codes."""
    n = len(data)
    starts: list[tuple[int, int]] = []
    i = 0
    while i < n - 3:
        idx = data.find(b"\x00\x00", i)
        if idx < 0 or idx >= n - 2:
            break
        if data[idx + 2] == 1:
            starts.append((idx, idx + 3))
            i = idx + 3
        elif idx + 3 < n and data[idx + 2] == 0 and data[idx + 3] == 1:
            starts.append((idx, idx + 4))
            i = idx + 4
        else:
            i = idx + 1
    for pos, (_, payload_start) in enumerate(starts):
        end = starts[pos + 1][0] if pos + 1 < len(starts) else n
        if payload_start < end:
            yield data[payload_start:end]


def _extract_mdat_slices(data: bytes) -> list[bytes]:
    """Return payload slices of top-level ISOBMFF ``mdat`` boxes."""
    slices: list[bytes] = []
    offset = 0
    total = len(data)
    while offset + 8 <= total:
        size, box_type = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            if offset + 16 > total:
                break
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = total - offset
        if size < header:
            break
        end = min(total, offset + size)
        if box_type == b"mdat" and offset + header < end:
            slices.append(data[offset + header : end])
        if offset + size > total:
            break
        offset += size
    return slices


def _iter_length_prefixed_nals(payload: bytes, *, length_size: int = 4) -> Iterable[bytes]:
    """Yield length-prefixed HEVC NAL units from an ISOBMFF sample buffer."""
    if length_size not in (2, 4):
        return
    fmt = ">I" if length_size == 4 else ">H"
    pos = 0
    total = len(payload)
    while pos + length_size < total:
        nal_len = struct.unpack_from(fmt, payload, pos)[0]
        pos += length_size
        if nal_len < 2 or pos + nal_len > total:
            break
        yield payload[pos : pos + nal_len]
        pos += nal_len


def _is_hdr10plus_sei(nal: bytes) -> bool:
    """Return True if an HEVC SEI NAL carries SMPTE ST 2094-40 (HDR10+) metadata."""
    if len(nal) < 10:
        return False
    nal_type = (nal[0] >> 1) & 0x3F
    if nal_type not in (39, 40):  # PREFIX_SEI_NUT / SUFFIX_SEI_NUT
        return False
    rbsp = _remove_epb(nal[2:])
    pos = 0
    end = len(rbsp)
    while pos < end:
        if rbsp[pos] == 0x80 and pos + 1 == end:
            break
        payload_type = 0
        while pos < end and rbsp[pos] == 0xFF:
            payload_type += 255
            pos += 1
        if pos >= end:
            break
        payload_type += rbsp[pos]
        pos += 1

        payload_size = 0
        while pos < end and rbsp[pos] == 0xFF:
            payload_size += 255
            pos += 1
        if pos >= end:
            break
        payload_size += rbsp[pos]
        pos += 1

        msg = rbsp[pos : pos + payload_size]
        pos += payload_size
        # ITU-T T.35 user_data_registered_itu_t_t35 (payloadType == 4):
        # country=0xB5 (USA), provider=0x003C (Samsung), oriented=0x0001 (ST 2094-40),
        # application_identifier=0x04
        if payload_type == 4 and len(msg) >= 7:
            country = msg[0]
            provider = (msg[1] << 8) | msg[2]
            oriented = (msg[3] << 8) | msg[4]
            app_id = msg[5]
            if country == 0xB5 and provider == 0x003C and oriented == 0x0001 and app_id == 0x04:
                return True
    return False


def _is_dv_rpu_nal(nal: bytes) -> bool:
    """Return True if an HEVC NAL is a Dolby Vision RPU (NAL type 62 / UNSPEC62)."""
    if len(nal) < 2:
        return False
    return ((nal[0] >> 1) & 0x3F) == 62


def _upgrade_video_range(current: str, *, has_hdr10plus: bool, has_dv_rpu: bool) -> str:
    norm = str(current or "").strip().upper().replace(" ", "")
    has_dv = has_dv_rpu or ("DV" in norm or "DOVI" in norm or "DOLBYVISION" in norm)
    has_h10p = has_hdr10plus or ("HDR10+" in norm or "HDR10PLUS" in norm or "HDR10P" in norm)
    if has_dv and has_h10p:
        return "DV+HDR10+"
    if has_h10p:
        return "HDR10+"
    if has_dv and ("HDR10" in norm or norm == "HDR"):
        return "DV+HDR10"
    if has_dv:
        return "DV"
    return str(current or "")


def probe_hevc_hdr_bytes(data: bytes, *, current_range: str = "") -> HdrProbeResult:
    """Scan HEVC Annex-B or ISOBMFF bytes for ST 2094-40 HDR10+ SEI and DV RPU NALs."""
    if not data:
        return HdrProbeResult(upgraded_range=current_range)
    has_h10p = False
    has_dv = False
    scanned = 0

    for nal in _iter_annexb_nals(data):
        scanned += 1
        if not has_h10p and _is_hdr10plus_sei(nal):
            has_h10p = True
        if not has_dv and _is_dv_rpu_nal(nal):
            has_dv = True
        if has_h10p and has_dv:
            break

    if scanned == 0:
        mdat_slices = _extract_mdat_slices(data)
        buffers = mdat_slices if mdat_slices else [data]
        for buf in buffers:
            for nal in _iter_length_prefixed_nals(buf, length_size=4):
                scanned += 1
                if not has_h10p and _is_hdr10plus_sei(nal):
                    has_h10p = True
                if not has_dv and _is_dv_rpu_nal(nal):
                    has_dv = True
                if has_h10p and has_dv:
                    break
            if has_h10p and has_dv:
                break

    upgraded = _upgrade_video_range(current_range, has_hdr10plus=has_h10p, has_dv_rpu=has_dv)
    return HdrProbeResult(
        has_hdr10plus=has_h10p,
        has_dv_rpu=has_dv,
        nal_units_scanned=scanned,
        upgraded_range=upgraded,
    )


def _sample_candidates_for_stream(
    stream: Any,
) -> list[tuple[str, tuple[int, int] | None, bytes | None]]:
    """Return ordered ``(url, byte_range, inline_bytes)`` sample candidates.

    Always prefers real media segments (``index != -1`` and not ``gap``) over
    DASH/HLS initialization segments (``index == -1``), which contain only
    ``ftyp``/``moov`` boxes and no HEVC video NAL units.
    """
    candidates: list[tuple[str, tuple[int, int] | None, bytes | None]] = []
    seen: set[tuple[str, tuple[int, int] | None, bool]] = set()

    def _add(url: str, br: tuple[int, int] | None = None, data: bytes | None = None) -> None:
        key = (url, br, data is not None)
        if key not in seen:
            seen.add(key)
            candidates.append((url, br, data))

    extra = getattr(stream, "extra", None)
    if isinstance(extra, dict):
        for key in ("first_segment_url", "sample_url"):
            val = str(extra.get(key) or "").strip()
            if val.startswith(("http://", "https://")):
                _add(val)

    segments = getattr(stream, "segments", None) or ()
    media_added = 0
    fallback_inits: list[tuple[str, tuple[int, int] | None, bytes | None]] = []
    for seg in segments:
        if getattr(seg, "gap", False):
            continue
        seg_index = getattr(seg, "index", None)
        raw_br = getattr(seg, "byte_range", None)
        br: tuple[int, int] | None = (
            (int(raw_br[0]), int(raw_br[1]))
            if isinstance(raw_br, (tuple, list)) and len(raw_br) == 2
            else None
        )
        inline = getattr(seg, "data", None)
        url = str(getattr(seg, "url", "") or "").strip()
        if not isinstance(inline, (bytes, bytearray)) and not url.startswith(("http://", "https://")):
            continue
        item = (url, br, bytes(inline) if isinstance(inline, (bytes, bytearray)) else None)
        if seg_index == -1:
            fallback_inits.append(item)
            continue
        _add(*item)
        media_added += 1
        if media_added >= 2:
            break

    direct = str(getattr(stream, "url", "") or "").strip()
    if direct.startswith(("http://", "https://")) and not direct.lower().split("?", 1)[0].endswith((".m3u8", ".mpd")):
        _add(direct)

    if not candidates:
        for item in fallback_inits[:1]:
            _add(*item)
    return candidates


def _sample_url_for_stream(stream: Any) -> str:
    for url, _br, _data in _sample_candidates_for_stream(stream):
        if url:
            return url
    return ""


def _fetch_sample_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
    timeout: int = 10,
    max_bytes: int = 524288,
) -> bytes:
    req_headers = dict(headers or {})
    if not any(k.lower() == "range" for k in req_headers):
        req_headers["Range"] = f"bytes=0-{max(4096, max_bytes) - 1}"
    try:
        from ..core.network import get as network_get

        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = network_get(url, headers=req_headers, proxies=proxies, timeout=timeout, stream=True)
        resp.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                break
        resp.close()
        return b"".join(chunks)[:max_bytes]
    except Exception:
        if proxy:
            return b""
        try:
            req = Request(url, headers=req_headers)
            with urlopen(req, timeout=timeout) as fh:  # noqa: S310
                return fh.read(max_bytes)
        except Exception:
            return b""


def probe_tracks_hdr_metadata(
    streams: Iterable[Any],
    *,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
    timeout: int = 10,
    max_bytes: int = 524288,
    fetcher: Callable[..., bytes] | None = None,
    log: Callable[[str], None] | None = None,
) -> int:
    """Inspect candidate HEVC HDR streams and upgrade ``video_range`` when SEI/RPU found.

    Returns the number of streams whose ``video_range`` was upgraded.
    Encrypted streams are skipped safely (SEI payloads cannot be read before decryption).
    """
    fetch = fetcher or _fetch_sample_bytes
    upgraded_count = 0
    for stream in streams:
        if getattr(stream, "media_type", "") != "video" or getattr(stream, "is_live", False):
            continue
        if getattr(stream, "encrypted", False):
            continue
        codec = str(getattr(stream, "codecs", "") or "").lower()
        if not codec.startswith(("hev", "hvc", "dvhe", "dvh1", "h.265", "h265")):
            continue
        current = str(getattr(stream, "video_range", "") or "").strip()
        norm = current.upper().replace(" ", "")
        if norm in {"", "SDR", "HLG", "DV+HDR10+"}:
            continue
        candidates = _sample_candidates_for_stream(stream)
        if not candidates:
            continue
        for sample_url, byte_range, inline_data in candidates[:2]:
            if inline_data is not None:
                data = inline_data[:max_bytes]
            else:
                if not sample_url:
                    continue
                call_headers = dict(headers or {})
                if byte_range is not None:
                    start, end = byte_range
                    clamped_end = min(end, start + max(4096, max_bytes) - 1)
                    if clamped_end >= start >= 0:
                        call_headers["Range"] = f"bytes={start}-{clamped_end}"
                data = fetch(
                    sample_url,
                    headers=call_headers or headers,
                    proxy=proxy,
                    timeout=timeout,
                    max_bytes=max_bytes,
                )
            if not data:
                continue
            probe = probe_hevc_hdr_bytes(data, current_range=current)
            if probe.upgraded_range and probe.upgraded_range != current:
                stream.video_range = probe.upgraded_range
                upgraded_count += 1
                if callable(log):
                    log(
                        f"HDR probe upgraded track {getattr(stream, 'id', '') or '?'} range: "
                        f"{current} -> {probe.upgraded_range}"
                    )
                break
            if probe.nal_units_scanned > 0:
                break
    return upgraded_count


# ------------------------------------------------------------ SPS VUI & HLS DV


def build_vui_bsf_args(codec_name: str, video_range: str) -> list[str]:
    """Return FFmpeg bitstream-filter arguments to normalize HDR SPS VUI metadata."""
    codec = str(codec_name or "").strip().lower()
    vrange = str(video_range or "").strip().upper().replace(" ", "")
    if codec in {"hevc", "h265", "h.265"} or codec.startswith(("hvc1", "hev1", "dvh1", "dvhe")):
        bsf = "hevc_metadata"
    elif codec in {"h264", "avc"} or codec.startswith(("avc1", "avc3")):
        bsf = "h264_metadata"
    else:
        return []
    if vrange == "HLG":
        # BT.2020 primaries (9), ARIB STD-B67 HLG transfer (18), BT.2020 NCL matrix (9)
        return ["-bsf:v", f"{bsf}=colour_primaries=9:transfer_characteristics=18:matrix_coefficients=9"]
    if any(tok in vrange for tok in ("HDR10", "DV", "DOVI", "PQ")) or vrange == "HDR":
        # BT.2020 primaries (9), SMPTE ST 2084 PQ transfer (16), BT.2020 NCL matrix (9)
        return ["-bsf:v", f"{bsf}=colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9"]
    return []


def needs_vui_normalization(probe_stream: dict[str, Any], expected_range: str) -> bool:
    """Return True if ffprobe video stream VUI color tags are missing or contradict ``expected_range``."""
    vrange = str(expected_range or "").strip().upper().replace(" ", "")
    if not vrange or vrange in {"SDR", "UNKNOWN"}:
        return False
    primaries = str(probe_stream.get("color_primaries") or "").strip().lower()
    transfer = str(probe_stream.get("color_transfer") or "").strip().lower()
    matrix = str(probe_stream.get("color_space") or "").strip().lower()
    missing_values = {"", "unknown", "unspecified", "reserved"}
    if primaries in missing_values or transfer in missing_values or matrix in missing_values:
        return True
    if vrange == "HLG":
        return primaries != "bt2020" or transfer != "arib-std-b67" or not matrix.startswith("bt2020")
    if any(tok in vrange for tok in ("HDR10", "DV", "DOVI", "PQ")) or vrange == "HDR":
        return primaries != "bt2020" or transfer != "smpte2084" or not matrix.startswith("bt2020")
    return False


def normalize_track_vui(track: Any, *, tools: dict[str, str] | None = None) -> Any:
    """Apply lossless FFmpeg bitstream-filter VUI normalization to one HDR video track if needed."""
    stream = getattr(track, "stream", None)
    if stream is None or getattr(stream, "media_type", "") != "video" or getattr(stream, "is_live", False):
        return track
    vrange = str(getattr(stream, "video_range", "") or "")
    bsf_args = build_vui_bsf_args(str(getattr(stream, "codecs", "") or ""), vrange)
    if not bsf_args:
        return track
    ffmpeg = (tools or {}).get("ffmpeg") or shutil.which("ffmpeg")
    ffprobe = (tools or {}).get("ffprobe") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        return track

    src = Path(track.path)
    if not src.is_file():
        return track
    runtime = current_download_runtime()
    if runtime:
        runtime.checkpoint()
    probe = managed_run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", str(src)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        return track
    try:
        stream_meta = json.loads(probe.stdout)["streams"][0]
    except (ValueError, KeyError, IndexError):
        return track
    if not needs_vui_normalization(stream_meta, vrange):
        return track
    codec_name = str(stream_meta.get("codec_name") or getattr(stream, "codecs", "") or "")
    bsf_args = build_vui_bsf_args(codec_name, vrange)
    if not bsf_args:
        return track

    suffix = src.suffix or ".mp4"
    out = bounded_path(
        src.with_name(f"{src.stem}.vui{suffix}"),
        max_length=WINDOWS_INTERMEDIATE_PATH_LIMIT,
    )
    res = managed_run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(src),
            "-map",
            "0",
            "-c",
            "copy",
            *bsf_args,
            str(out),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
    )
    if res.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        return track
    updated = copy(track)
    updated.path = out
    updated.cleanup_paths = list(dict.fromkeys([*getattr(track, "cleanup_paths", []), src, out]))
    return updated


def normalize_tracks_vui(tracks: list[Any], *, enabled: bool) -> list[Any]:
    """Normalize SPS VUI metadata on HDR video tracks when enabled."""
    if not enabled:
        return tracks
    return [normalize_track_vui(item) for item in tracks]


_HLS_CODECS_RE = re.compile(r'CODECS="([^"]+)"')
_HLS_SUPP_CODECS_RE = re.compile(r'SUPPLEMENTAL-CODECS="([^"]+)"')
_HLS_VIDEO_RANGE_RE = re.compile(r"VIDEO-RANGE=([A-Z0-9_-]+)")


def restore_hls_dv_signaling(manifest_text: str) -> str:
    """Restore composite Dolby Vision codec signaling on HLS master ``#EXT-X-STREAM-INF`` lines.

    Some HLS CDNs advertise a backward-compatible HEVC base in ``CODECS`` and place
    the Dolby Vision layer in ``SUPPLEMENTAL-CODECS="dvh1.08.06/db4h"`` (or omit
    ``VIDEO-RANGE=PQ`` when ``dvh1``/``dvhe`` is present). Normalizing the attribute
    string ensures parsers and track selectors recognize the DV+HDR10 composite stream.
    """
    if not manifest_text or "#EXT-X-STREAM-INF" not in manifest_text:
        return manifest_text
    out_lines: list[str] = []
    for line in manifest_text.splitlines():
        if line.startswith("#EXT-X-STREAM-INF:"):
            supp_match = _HLS_SUPP_CODECS_RE.search(line)
            codecs_match = _HLS_CODECS_RE.search(line)
            if supp_match and codecs_match:
                supp_raw = supp_match.group(1)
                dv_tokens = [
                    tok.split("/", 1)[0].strip()
                    for tok in supp_raw.split(",")
                    if tok.strip().lower().startswith(("dvh1.", "dvhe.", "dav1."))
                ]
                codecs_val = codecs_match.group(1)
                missing_dv = [tok for tok in dv_tokens if tok not in codecs_val]
                if missing_dv:
                    merged = f"{codecs_val},{','.join(missing_dv)}"
                    line = (
                        line[: codecs_match.start(1)]
                        + merged
                        + line[codecs_match.end(1) :]
                    )
            if (
                ("dvh1." in line or "dvhe." in line)
                and not _HLS_VIDEO_RANGE_RE.search(line)
            ):
                line = f"{line},VIDEO-RANGE=PQ"
        out_lines.append(line)
    trailing = "\n" if manifest_text.endswith("\n") else ""
    return "\n".join(out_lines) + trailing


__all__ = [
    "HdrProbeResult",
    "build_vui_bsf_args",
    "needs_vui_normalization",
    "normalize_track_vui",
    "normalize_tracks_vui",
    "probe_hevc_hdr_bytes",
    "probe_tracks_hdr_metadata",
    "restore_hls_dv_signaling",
]
