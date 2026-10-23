"""Bounded, best-effort CENC/CBCS preflight; never confuse clear lead with a pass.

This module owns samples, not credentials or licensing. A caller may try several
keys against the same ciphertext. Unsupported layouts/codecs are inconclusive.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import requests

from .cenc_fragment import _mp4_boxes, _parse_tfhd, _parse_trun, _top_level_mdat_payload_ranges
from .embedding import DownloadCancelled, current_download_runtime, managed_popen
from .models import SegmentInfo, StreamInfo
from .postprocess import RawKey, decrypt_file


@dataclass(frozen=True)
class Verdict:
    status: str  # passed | failed | unknown
    reason: str


class ProbeUnavailable(RuntimeError):
    pass


def key_token(keys: list[RawKey]) -> str:
    """An in-memory completion marker, never raw keys in track metadata."""
    return hashlib.sha256(repr(sorted((key.kid or "", key.key) for key in keys)).encode()).hexdigest()


def stream_token(stream: StreamInfo, keys: list[RawKey], kids) -> str:
    scope = (key_token(keys), stream.media_type, stream.codecs, stream.encrypted,
             str(stream.encryption_scheme or "").upper(), tuple(sorted(kids)))
    return hashlib.sha256(repr(scope).encode()).hexdigest()


def _positions(length: int) -> list[int]:
    return list(dict.fromkeys((0, length // 2, length * 3 // 4, length // 4, length - 1))) if length else []


def _first_sample_changed(sample: bytes, clear: bytes) -> bool:
    """Conservative proof that decoding starts on encrypted, not clear-lead media.

    A later encrypted packet in a long fragment is not enough: FFmpeg's first
    seconds could still be clear. Mixed/multiplexed layouts are left unjudged.
    """
    moov = next((box for box in _mp4_boxes(sample) if box[2] == b"moov"), None)
    moof = next((box for box in _mp4_boxes(sample) if box[2] == b"moof"), None)
    if moov is None or moof is None:
        return False
    tracks = [box for box in _mp4_boxes(sample, moov[0] + moov[3], moov[0] + moov[1]) if box[2] == b"trak"]
    trafs = [box for box in _mp4_boxes(sample, moof[0] + moof[3], moof[0] + moof[1]) if box[2] == b"traf"]
    if len(tracks) != 1 or len(trafs) != 1:
        return False
    traf = trafs[0]
    children = list(_mp4_boxes(sample, traf[0] + traf[3], traf[0] + traf[1]))
    if any(int.from_bytes(sample[p + head:p + head + 4], "big") & 1
           for p, _size, kind, head in children if kind == b"tfhd"):
        return False  # absolute file offsets cannot be relocated into a tiny sample
    tfhd = next((_parse_tfhd(sample, p, size, head) for p, size, kind, head in children if kind == b"tfhd"), None)
    trun = next((_parse_trun(sample, p, size, head, tfhd.default_sample_size if tfhd else None)
                 for p, size, kind, head in children if kind == b"trun"), None)
    if trun is None or not trun.sample_sizes or trun.data_offset is None:
        return False
    start = moof[0] + trun.data_offset
    end = start + trun.sample_sizes[0]
    if not any(left <= start < end <= right for left, right in _top_level_mdat_payload_ranges(sample)):
        return False
    return sample[start:end] != clear[start:end]


def _sidx_ranges(data: bytes, position: int, size: int, header: int, base: int = 0) -> list[tuple[int, int]]:
    body = position + header
    if body + 4 > position + size:
        return []
    version = data[body]
    if version not in (0, 1):
        return []
    width = 8 if version else 4
    if body + 12 + width * 2 + 4 > position + size:
        return []
    offset = body + 12 + width
    first_offset = int.from_bytes(data[offset:offset + width], "big")
    offset += width + 2
    count = int.from_bytes(data[offset:offset + 2], "big")
    offset += 2
    if offset + count * 12 > position + size:
        return []
    start = base + position + size + first_offset
    result = []
    for _ in range(count):
        reference = int.from_bytes(data[offset:offset + 4], "big")
        length = reference & 0x7fffffff
        if reference >> 31 or not length:
            return []  # nested indexes need a separate adapter
        result.append((start, start + length - 1))
        start += length
        offset += 12
    return result


class KeyProbe:
    """One title's bounded sample cache. No download/resume files are modified."""

    def __init__(self, *, cancel: Event | Callable[[], bool] | None = None, max_bytes: int = 32 * 1024 * 1024,
                 max_requests: int = 48, seconds: float = 45, tmp: Path | None = None):
        self.cancel = cancel
        self.max_bytes = max_bytes
        self.max_requests = max_requests
        self.deadline = time.monotonic() + seconds
        self.bytes_read = 0
        self.requests = 0
        self.tmp = tmp
        self._cache: dict[tuple, bytes] = {}
        self.resource_sizes: dict[tuple, int] = {}
        self._samples: dict[int, list[bytes]] = {}
        self._iterators: dict[int, object] = {}

    def check(self) -> None:
        stopped = (getattr(self.cancel, "is_set", None) or self.cancel) if self.cancel is not None else None
        if stopped is not None and stopped():
            raise DownloadCancelled("key verification cancelled")
        runtime = current_download_runtime()
        if runtime is not None:
            runtime.checkpoint()
        if time.monotonic() >= self.deadline:
            raise ProbeUnavailable("sample time budget reached")

    @staticmethod
    def _identity(url, headers, proxy):
        return url, tuple(sorted((k.lower(), v) for k, v in headers.items() if k.lower() != "range")), proxy

    def fetch(self, url: str, span: tuple[int, int], headers: dict, proxy: str | None) -> bytes:
        self.check()
        identity = self._identity(url, headers, proxy)
        for (saved, left, right), value in self._cache.items():
            if identity == saved and left <= span[0] and right >= span[1]:
                return value[span[0] - left:span[1] - left + 1]
        length = span[1] - span[0] + 1
        if length <= 0 or length > 8 * 1024 * 1024 or self.bytes_read + length > self.max_bytes:
            raise ProbeUnavailable("sample byte budget reached")
        if self.requests >= self.max_requests:
            raise ProbeUnavailable("sample request budget reached")
        self.requests += 1
        request_headers = {k: v for k, v in headers.items() if k.lower() not in {"range", "accept-encoding"}}
        request_headers.update({"Range": f"bytes={span[0]}-{span[1]}", "Accept-Encoding": "identity"})
        if not url.startswith(("https://", "http://")):
            raise ProbeUnavailable("only authorized HTTP media references are sampled")
        try:
            with requests.get(url, headers=request_headers, proxies={"http": proxy, "https": proxy} if proxy else None,
                              timeout=(5, 8), stream=True) as response:
                expected = None
                if response.status_code == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", response.headers.get("Content-Range", ""))
                    if not match or int(match[1]) != span[0] or int(match[2]) > span[1]:
                        raise ProbeUnavailable("media range response did not match the requested sample")
                    if match[3].isdigit():
                        self.resource_sizes[identity] = int(match[3])
                    expected = int(match[2]) - int(match[1]) + 1
                elif response.status_code != 200 or span[0] != 0:
                    raise ProbeUnavailable(f"sample HTTP status {response.status_code}")
                elif response.headers.get("Content-Length", "").isdigit():
                    self.resource_sizes[identity] = int(response.headers["Content-Length"])
                chunks = []
                total = 0
                for chunk in response.iter_content(16384):
                    self.check()
                    if not chunk:
                        continue
                    remaining = length - total
                    chunks.append(chunk[:remaining])
                    total += min(len(chunk), remaining)
                    self.bytes_read += min(len(chunk), remaining)
                    if total >= length:
                        break
                payload = b"".join(chunks)
                if expected is not None and len(payload) != expected:
                    raise ProbeUnavailable("media sample response was incomplete")
        except requests.RequestException as exc:
            # Requests' exception strings include signed URLs. Never emit them.
            raise ProbeUnavailable("media sample request failed") from exc
        self._cache[(identity, span[0], span[0] + len(payload) - 1)] = payload
        return payload

    def _read_segment(self, segment: SegmentInfo, headers: dict, proxy) -> bytes:
        if segment.data is not None:
            if len(segment.data) > 8 * 1024 * 1024:
                raise ProbeUnavailable("inline sample exceeds the byte budget")
            return bytes(segment.data)
        limit = min(8 * 1024 * 1024, self.max_bytes - self.bytes_read)
        return self.fetch(segment.url, segment.byte_range or (0, limit - 1), headers, proxy)

    def _file_samples(self, url: str, headers: dict, proxy):
        prefix = self.fetch(url, (0, 131071), headers, proxy)
        initialization = b""
        ranges = []
        for position, size, kind, header in _mp4_boxes(prefix):
            if kind in {b"ftyp", b"moov"}:
                initialization += prefix[position:position + size]
            elif kind == b"sidx":
                ranges = _sidx_ranges(prefix, position, size, header)
        if b"moov" not in initialization:
            raise ProbeUnavailable("complete MP4 initialization is outside the sample budget")
        if ranges:
            for index in _positions(len(ranges)):
                yield initialization + self.fetch(url, ranges[index], headers, proxy)
            return
        # A bounded structural walk skips mdat payloads instead of downloading a
        # clear lead. Never seek to an arbitrary byte offset inside compressed data.
        offset = 0
        fragment = None
        for _ in range(96):
            head = self.fetch(url, (offset, offset + 15), headers, proxy)
            if len(head) < 8:
                break
            size, kind = int.from_bytes(head[:4], "big"), head[4:8]
            if size == 1:
                size = int.from_bytes(head[8:16], "big")
            if size < 8:
                break
            if kind == b"moof":
                fragment = offset
            elif kind == b"mdat" and fragment is not None:
                yield initialization + self.fetch(url, (fragment, offset + size - 1), headers, proxy)
                fragment = None
            offset += size

    def _load_samples(self, stream: StreamInfo, headers: dict, proxy):
        media = [segment for segment in stream.segments if segment.index != -1]
        init = next((segment for segment in stream.segments if segment.index == -1), None)
        if not media:
            raise ProbeUnavailable("no media sample reference")
        # Downloader-generated ranges of one direct file are not MP4 fragments.
        if init is None and media[0].data is None and len({segment.url for segment in media}) == 1:
            yield from self._file_samples(media[0].url, headers, proxy)
            return
        initialization = self._read_segment(init, headers, proxy) if init else b""
        for index in _positions(len(media)):
            yield initialization + self._read_segment(media[index], headers, proxy)

    def samples(self, stream: StreamInfo, headers: dict, proxy):
        identity = id(stream)
        cached = self._samples.setdefault(identity, [])
        yield from cached
        iterator = self._iterators.setdefault(identity, self._load_samples(stream, headers, proxy))
        for sample in iterator:
            cached.append(sample)
            yield sample

    def verify(self, stream: StreamInfo, kid: str, key: str, *, aliases: tuple[str, ...] = (),
               headers: dict | None = None, proxy: str | None = None) -> Verdict:
        if str(stream.encryption_scheme or "").upper() not in {"", "CENC", "CENS", "CBCS", "CBC1"}:
            return Verdict("unknown", "transport encryption is outside the CENC/CBCS preflight")
        executable = shutil.which("ffmpeg")
        if not executable:
            return Verdict("unknown", "FFmpeg is unavailable")
        failures = 0
        try:
            for sample in self.samples(stream, headers or {}, proxy):
                self.check()
                result = self._verify_sample(sample, stream.media_type, kid, key, aliases, executable)
                if result.status == "passed":
                    return result
                if result.status == "failed":
                    failures += 1
                    if failures >= 2:
                        return Verdict("failed", "two encrypted samples failed to decode")
            return Verdict("unknown", "no conclusive encrypted sample for this KID")
        except (ProbeUnavailable, OSError, ValueError, IndexError, OverflowError) as exc:
            return Verdict("unknown", str(exc) if isinstance(exc, ProbeUnavailable) else "sample could not be inspected")

    def _verify_sample(self, sample, media_type, kid, key, aliases, executable) -> Verdict:
        boxes = list(_mp4_boxes(sample))
        if not boxes or boxes[-1][0] + boxes[-1][1] != len(sample):
            return Verdict("unknown", "sample is not a complete MP4 fragment")
        if sum(kind == b"moov" for _p, _size, kind, _head in boxes) != 1:
            return Verdict("unknown", "sample initialization is missing or ambiguous")
        payloads = _top_level_mdat_payload_ranges(sample)
        if not payloads:
            return Verdict("unknown", "sample has no media payload")
        with tempfile.TemporaryDirectory(prefix="unidl-key-probe-", dir=self.tmp) as root:
            source, output = Path(root) / "sample.mp4", Path(root) / "clear.mp4"
            source.write_bytes(sample)
            try:
                decrypt_file(source, [RawKey(key, value) for value in dict.fromkeys((kid, *aliases))],
                             output_path=output, expected_kids=[kid])
            except (RuntimeError, ValueError, OSError, IndexError, OverflowError):
                return Verdict("unknown", "sample layout or KID is not supported by the internal decryptor")
            clear = output.read_bytes()
            # Header rewriting is not evidence. At least one actual mdat byte
            # must have been decrypted with the supplied, explicitly scoped KID.
            if not any(sample[start:end] != clear[start:end] for start, end in payloads):
                return Verdict("unknown", "clear lead: no encrypted payload was exercised")
            if not _first_sample_changed(sample, clear):
                return Verdict("unknown", "sample begins with clear or unverified media")
            return self._decode(output, media_type, executable)

    def _decode(self, path, media_type, executable) -> Verdict:
        command = [executable, "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-err_detect", "explode",
                   "-threads", "1", "-i", str(path), "-map", "0:v:0" if media_type == "video" else "0:a:0",
                   "-t", "3", "-threads", "1", "-progress", "pipe:1", "-nostats", "-f", "null", "-"]
        process = managed_popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            end = min(self.deadline, time.monotonic() + 8)
            while True:
                self.check()
                try:
                    stdout, stderr = process.communicate(timeout=min(0.2, max(0.01, end - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= end:
                        return Verdict("unknown", "sample decode timed out")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            runtime = current_download_runtime()
            if runtime is not None:
                runtime.unregister_process(process)
        error = stderr.decode("utf-8", "replace").lower()
        if re.search(r"decoder.*not found|decoding.*not supported", error) or any(word in error for word in ("not implemented", "not yet implemented", "decoder not found", "unknown decoder",
                                           "unsupported", "error while opening decoder", "no decoder", "out of memory",
                                           "could not find ref", "missing reference", "reference picture missing")):
            return Verdict("unknown", "decoder cannot reliably judge this sample")
        progress = stdout.decode("utf-8", "replace")
        produced = any(int(value) > 0 for value in re.findall(r"(?:^|\n)(?:frame|out_time_us)=(\d+)", progress))
        if process.returncode == 0 and produced:
            return Verdict("passed", "encrypted media decoded successfully")
        if process.returncode != 0 and any(word in error for word in (
            "invalid nal", "invalid data", "error while decoding", "error submitting packet", "decode_slice_header",
            "error parsing", "invalid bitstream", "channel element", "cabac", "corrupt", "invalid frame", "invalid sync",
        )):
            return Verdict("failed", "encrypted media did not decode")
        return Verdict("unknown", "no reliable decoder verdict")
