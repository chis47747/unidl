"""UniDL Core orchestration and the native delivery boundary.

Everything download-shaped goes through the typed native backend, so services
never touch implementation details and the TUI never builds command lines by hand. This replaces the ~200 line
``format_command_bundle`` that had been copy-pasted into 90 scripts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from unidl.downloader import NativeDownloaderBackend, NativeManifestError, api
from unidl.downloader.models import StreamInfo
from unidl.downloader.selection import SelectionOptions, select_streams

from . import cdmrules, exports, vaults
from . import drm as drm_registry
from .cdm import WIDEVINE, CdmError
from .chapters import normalize_chapters
from .chapters import timestamp as chapter_timestamp
from .config import Config
from .delivery import (
    ArtifactEvent,
    CancellationToken,
    DeliveryHooks,
    DeliveryPlan,
    DeliverySource,
    DeliveryStatus,
    DownloadPolicy,
    LivePolicy,
    MessageEvent,
    ParsedManifest,
    ParsePolicy,
    ParseRequest,
    PauseToken,
    StageEvent,
    TrackDescriptor,
    TrackProgressEvent,
)
from .delivery import (
    LiveKeyRequest as CoreLiveKeyRequest,
)
from .playback import Playback, normalize_live_record_limit
from .secureio import atomic_write_text, locked_path, private_directory, private_file
from .settings import Settings, normalize_drop_video_pattern
from .vault import KeyVault, normalize_hex, split_pair

LineSink = Callable[[str], None]
FrameSink = Callable[[list[str]], None]
LiveKeyInput = Callable[[str | None, object, object, str, str], str | None]

_DIRECT_AUDIO_EXTENSIONS = {".aac", ".ac3", ".eac3", ".flac", ".m4a", ".m4b", ".mp3", ".ogg", ".opus", ".wav"}

_OSC_ESCAPE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_C1_OSC_ESCAPE = re.compile(r"\x9d[^\x07\x9c]*(?:\x07|\x9c)")
_CSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_C1_CSI_ESCAPE = re.compile(r"\x9b[0-?]*[ -/]*[@-~]")
_ESCAPE = re.compile(r"\x1b[ -/]*[@-~]")


def _plain_terminal_text(value: object) -> str:
    """Remove terminal state changes from an embedded downloader message."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _OSC_ESCAPE.sub("", text)
    text = _C1_OSC_ESCAPE.sub("", text)
    text = _CSI_ESCAPE.sub("", text)
    text = _C1_CSI_ESCAPE.sub("", text)
    text = _ESCAPE.sub("", text)
    return "".join(
        character
        for character in text
        if character in {"\n", "\t"}
        or (32 <= ord(character) < 127)
        or ord(character) >= 160
    )


@dataclass
class _DeliveryOverrides:
    append_url_params: bool = False
    ad_keywords: list[str] = field(default_factory=list)
    decode_audio_vivid: bool = False
    audio_vivid_decoder: str | None = None
    audio_vivid_decoder_args: str | None = None
    live_real_time_merge: bool | None = None
    live_keep_segments: bool | None = None
    live_pipe_mux: bool | None = None
    live_perform_as_vod: bool = False
    custom_range: str | None = None
    allow_hls_multi_ext_map: bool = False
    vgc: bool = False
    vgc_keep_opaque: bool = False


def _delivery_overrides(values: Sequence[str]) -> _DeliveryOverrides:
    """Turn the remaining service argv bridge into typed Core policy values."""
    result = _DeliveryOverrides()
    args = [str(value) for value in values]
    boolean_fields = {
        "--live-real-time-merge": "live_real_time_merge",
        "--live-keep-segments": "live_keep_segments",
        "--live-pipe-mux": "live_pipe_mux",
    }
    value_fields = {
        "--ad-keyword": "ad_keywords",
        "--audio-vivid-decoder": "audio_vivid_decoder",
        "--audio-vivid-decoder-args": "audio_vivid_decoder_args",
        "--custom-range": "custom_range",
    }
    index = 0
    while index < len(args):
        raw = args[index]
        flag, equals, inline = raw.partition("=")
        if flag in boolean_fields:
            value = True
            if equals:
                value = inline.strip().lower() not in {"0", "false", "no", "off"}
            elif index + 1 < len(args) and args[index + 1].strip().lower() in {
                "0",
                "1",
                "false",
                "no",
                "off",
                "on",
                "true",
                "yes",
            }:
                index += 1
                value = args[index].strip().lower() in {"1", "on", "true", "yes"}
            setattr(result, boolean_fields[flag], value)
        elif flag.startswith("--no-live-"):
            positive = "--live-" + flag.removeprefix("--no-live-")
            field_name = boolean_fields.get(positive)
            if field_name:
                setattr(result, field_name, False)
            else:
                raise ValueError(f"unsupported service delivery option: {raw}")
        elif flag in value_fields:
            if not equals:
                if index + 1 >= len(args):
                    raise ValueError(f"{flag} needs a value")
                index += 1
                inline = args[index]
            field_name = value_fields[flag]
            if field_name == "ad_keywords":
                result.ad_keywords.append(inline)
            else:
                setattr(result, field_name, inline)
        elif flag == "--append-url-params":
            result.append_url_params = True
        elif flag == "--decode-audio-vivid":
            result.decode_audio_vivid = True
        elif flag in {"--live-perform-as-vod", "--live-dvr-as-vod"}:
            result.live_perform_as_vod = True
        elif flag == "--allow-hls-multi-ext-map":
            result.allow_hls_multi_ext_map = True
        elif flag == "--vgc":
            result.vgc = True
        elif flag == "--vgc-keep-opaque":
            result.vgc_keep_opaque = True
        elif flag == "--force-ansi-console":
            # Rendering belongs to DeliveryHooks, never to a service policy.
            pass
        else:
            raise ValueError(
                f"unsupported service delivery option: {raw}; add a typed Core field"
            )
        index += 1
    return result


def _progress_size(value: int | None) -> str:
    if value is None:
        return "?"
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            precision = 0 if unit == "B" or amount >= 100 else 1
            return f"{amount:.{precision}f}{unit}"
        amount /= 1024
    return f"{amount:.1f}TB"


def _progress_clock(seconds: float | None) -> str:
    value = int(max(0, round(seconds or 0)))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _progress_label(stream) -> str:
    media_type = str(getattr(stream, "media_type", "") or "track").lower()
    prefix = {
        "video": "Vid",
        "audio": "Aud",
        "subtitle": "Sub",
        "subtitles": "Sub",
        "text": "Sub",
    }.get(media_type, media_type[:3].title() or "Trk")
    if media_type == "video":
        details = [getattr(stream, "resolution", None), getattr(stream, "codecs", None)]
    elif media_type == "audio":
        details = [
            getattr(stream, "language", None),
            getattr(stream, "channels", None),
            getattr(stream, "codecs", None),
        ]
    else:
        details = [getattr(stream, "language", None), getattr(stream, "name", None)]
    body = " ".join(str(value) for value in details if value)
    return f"{prefix} {body}".strip()


class _StructuredProgressScreen:
    """Render typed Core progress events without parsing terminal output."""

    def __init__(
        self,
        frame: FrameSink,
        width: Callable[[], int | None],
        tracks: Sequence[TrackDescriptor] = (),
    ) -> None:
        self._frame = frame
        self._width = width
        self._lock = threading.Lock()
        self._order: list[str] = []
        self._tracks = {track.track_id: track for track in tracks}
        # Publish a real initial picture as soon as delivery starts.  The native
        # downloader normally emits a zero-byte progress sample, but a concurrent
        # job can spend an arbitrary amount of time preparing its first request
        # (key setup, cache checks, or network discovery).  Without this seed the
        # TUI has enough state to show the bordered card while its body is empty.
        # That is particularly confusing when the user can already see the
        # ``Downloading`` status line above it.
        self._updates: dict[str, TrackProgressEvent] = {
            track.track_id: TrackProgressEvent(
                track_id=track.track_id,
                total_segments=1,
                live=bool(track.is_live),
                duration_seconds=(track.duration if track.is_live else None),
                status="Waiting" if track.is_live else "Downloading",
            )
            for track in tracks
        }
        self._status_rows: list[str] = []
        for track in tracks:
            if track.track_id not in self._order:
                self._order.append(track.track_id)
        if self._updates:
            self._frame(self._rows())

    def update(self, update: TrackProgressEvent) -> None:
        with self._lock:
            if update.track_id not in self._order:
                self._order.append(update.track_id)
            self._updates[update.track_id] = update
            rows = self._rows()
        self._frame(rows)

    def finish(self) -> None:
        self._frame([])

    def status(self, rows: Sequence[str]) -> None:
        """Show native post-processing state without replacing transfer rows."""
        clean = [_plain_terminal_text(row).strip() for row in rows]
        with self._lock:
            self._status_rows = [row for row in clean if row][-2:]
            rendered = self._rows()
        self._frame(rendered)

    def _rows(self) -> list[str]:
        updates = [self._updates[key] for key in self._order if key in self._updates]
        if not updates:
            return list(self._status_rows)
        width = max(40, int(self._width() or 100))
        records: list[tuple[str, float, str, str, str, str, str]] = []
        for update in updates:
            track = self._tracks.get(update.track_id)
            total, estimated = self._estimated_total(update, track)
            percent = self._percent(update, total)
            if update.live:
                total_clock = (
                    _progress_clock(update.duration_seconds)
                    if update.duration_seconds
                    else "--:--"
                )
                size = f"{_progress_clock(update.recorded_seconds)}/{total_clock}"
            else:
                downloaded = _progress_size(update.downloaded_bytes)
                estimate_mark = "~" if estimated and total is not None else ""
                size = f"{downloaded}/{estimate_mark}{_progress_size(total)}"
            segments = f"{update.completed_segments}/{update.total_segments or '?'}"
            speed = f"{_progress_size(int(update.speed_bytes_per_second or 0))}/s"
            eta_seconds = self._eta(update, total)
            eta = (
                f"ETA {_progress_clock(eta_seconds)}"
                if eta_seconds is not None
                else ""
            )
            records.append(
                (
                    _progress_label(track),
                    percent,
                    size,
                    segments,
                    speed,
                    eta,
                    "Done ✓" if update.done else "",
                )
            )

        bar_width = 16 if width >= 104 else 10 if width >= 68 else 6
        size_width = max(len(record[2]) for record in records)
        longest_label = max(len(record[0]) for record in records)
        # Keep the VOD essentials even when the window is narrow. Segments,
        # speed and ETA are appended only when there is room for each whole field.
        fixed_tail = bar_width + 2 + 4 + 1 + size_width
        label_width = max(4, min(longest_label, 28, width - fixed_tail - 2))
        rows: list[str] = []
        for label, percent, size, segments, speed, eta, done in records:
            if len(label) > label_width:
                label = label[: max(1, label_width - 1)] + "…"
            filled = max(0, min(bar_width, round(bar_width * percent / 100)))
            bar = "━" * filled + "─" * (bar_width - filled)
            row = f"{label:<{label_width}}  {bar} {percent:3.0f}% {size:<{size_width}}"
            for extra in (segments, speed, eta, done):
                if extra and len(row) + 1 + len(extra) <= width:
                    row += f" {extra}"
            rows.append(row[:width])
        rows.extend(self._status_rows)
        return rows

    @staticmethod
    def _estimated_total(
        update: TrackProgressEvent,
        track: TrackDescriptor | None,
    ) -> tuple[int | None, bool]:
        if (
            update.total_bytes is not None
            and update.total_bytes > 0
            and update.downloaded_bytes <= update.total_bytes
        ):
            return update.total_bytes, False
        if update.done and update.downloaded_bytes > 0:
            return update.downloaded_bytes, False
        if (
            update.downloaded_bytes > 0
            and update.completed_segments > 0
            and update.total_segments
            and update.total_segments > 0
        ):
            estimated = round(
                update.downloaded_bytes
                * update.total_segments
                / update.completed_segments
            )
            return max(update.downloaded_bytes, estimated), True
        if track is not None and track.size_bytes is not None and track.size_bytes > 0:
            return max(update.downloaded_bytes, track.size_bytes), True
        return None, False

    @staticmethod
    def _percent(update: TrackProgressEvent, total_bytes: int | None) -> float:
        if update.done:
            return 100.0
        if update.live:
            if update.duration_seconds:
                return min(
                    100.0,
                    (update.recorded_seconds or 0)
                    / update.duration_seconds
                    * 100,
                )
            return 0.0
        if total_bytes:
            return min(100.0, update.downloaded_bytes / total_bytes * 100)
        if update.total_segments:
            return min(
                100.0,
                update.completed_segments / update.total_segments * 100,
            )
        return 0.0

    @staticmethod
    def _eta(update: TrackProgressEvent, total_bytes: int | None) -> float | None:
        if update.done:
            return 0.0
        if update.eta_seconds is not None:
            return max(0.0, update.eta_seconds)
        speed = float(update.speed_bytes_per_second or 0)
        if (
            total_bytes is not None
            and speed > 0
            and total_bytes >= update.downloaded_bytes
        ):
            return (total_bytes - update.downloaded_bytes) / speed
        if update.completed_segments > 0 and update.total_segments:
            seconds_per_segment = update.elapsed_seconds / update.completed_segments
            return (
                max(0, update.total_segments - update.completed_segments)
                * seconds_per_segment
            )
        return None


@dataclass
class TrackSet:
    """A parsed ladder plus what is currently chosen."""

    streams: list[StreamInfo] = field(default_factory=list)
    selected: list[StreamInfo] = field(default_factory=list)
    manifest: ParsedManifest | None = field(default=None, repr=False)
    #: The fully authorized service playback that produced each native stream.
    #: This is deliberately keyed by object identity: the backend keeps those
    #: native objects intact while merging/deduplicating manifests, and Core must
    #: be able to send a selected track back through the matching DRM token,
    #: endpoint and client session.  It never crosses the delivery boundary.
    origins: dict[int, Playback] = field(default_factory=dict, repr=False)

    @property
    def video(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.media_type == "video"]

    @property
    def audio(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.media_type == "audio"]

    @property
    def subtitles(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.media_type in {"subtitle", "subtitles", "text"}]

    def summary(self) -> str:
        return (
            f"{len(self.selected)} selected / {len(self.streams)} total | "
            f"{len(self.video)} video | {len(self.audio)} audio | {len(self.subtitles)} subtitle"
        )


#: the conventional exit code for "interrupted", which is what a cancel is
CANCELLED_EXIT = 130


@dataclass
class DownloadResult:
    exit_code: int
    output_dir: Path
    command: str
    artifacts: tuple[Path, ...] = ()
    failure: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def cancelled(self) -> bool:
        """Stopped on request. Not a failure, and must not be reported as one."""
        return self.exit_code == CANCELLED_EXIT


def _folder_name(service: str) -> str:
    """A validated service id, or "" when there is nothing to name one."""
    raw = str(service or "").strip()
    if not raw:
        return ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", raw):
        raise ValueError(f"invalid service folder name: {service!r}")
    return raw


class Engine:
    def __init__(
        self,
        config: Config,
        log: LineSink | None = None,
        vault: KeyVault | None = None,
        vault_collection: vaults.Vaults | None = None,
    ):
        self.config = config
        self.log: LineSink = log or (lambda _line: None)
        #: UniDL's native downloader implementation. Core execution below speaks
        #: only in DeliveryPlan/DeliveryEvent/DeliveryResult values.
        self.downloader = NativeDownloaderBackend()
        #: Where structured progress frames go. Without one, permanent messages
        #: still reach :attr:`log` and no terminal renderer is involved.
        self.frame: FrameSink | None = None
        #: How wide the progress panel is. The native formatter reads this through
        #: a context-local hook on every render, including after a window resize.
        self.frame_columns: int | None = None
        #: Manual fallback for a real live key rotation. The TUI supplies this;
        #: headless callers leave it empty and get a clear error after automatic
        #: vault/service resolution is exhausted.
        self.live_key_input: LiveKeyInput | None = None
        #: the local store. Still a single object because it is also the
        #: provenance record behind the key screen and the ``keys`` command,
        #: which a remote vault cannot answer.
        self.vault = vault if vault is not None else KeyVault(config.paths.keys_db)
        #: every configured vault, in preference order, for lookups and writes.
        #: With no ``key_vaults`` section this is exactly the local one.
        self.vaults = vault_collection or vaults.build(
            config,
            local=self.vault,
            log=lambda line: self.log(line),
        )
        # A shared TUI collection was built before this Engine got its live log
        # sink. Route later backend diagnostics to the current delivery screen.
        self.vaults.log = lambda line: self.log(line)

    def shutdown_active_download(self) -> None:
        """Immediately release the native delivery owned by this engine."""
        shutdown = getattr(self.downloader, "shutdown_active_download", None)
        if callable(shutdown):
            shutdown()

    # ------------------------------------------------------------------- keys
    def fetch_manifest(self, playback: Playback) -> bytes | None:
        if playback.inline_manifest:
            return str(playback.inline_manifest).encode("utf-8")
        if not playback.manifest_url:
            return None
        proxies = {"http": playback.proxy, "https": playback.proxy} if playback.proxy else None
        response = requests.get(playback.manifest_url, headers=playback.headers, proxies=proxies, timeout=30)
        response.raise_for_status()
        return response.content

    def playback_input(self, playback: Playback) -> str:
        """Return the concrete input handed to UniDL.

        Services such as Netflix and YouTube receive an adaptive track inventory
        rather than an MPD/HLS URL.  ``Playback`` keeps that inventory as a dict,
        while UniDL deliberately accepts JSON through the same file-oriented
        input as every other manifest.  Materialising it here keeps services out
        of temp-file management and, importantly, gives parsing, command export
        and downloading the exact same input path.
        """
        if playback.inline_manifest:
            payload = str(playback.inline_manifest)
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
            stem = re.sub(r"[^A-Za-z0-9._-]+", ".", playback.save_name).strip(".") or "playback"
            directory = self.config.paths.temp / "manifests"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{stem}.{digest}.mpd"
            if not path.exists() or path.read_text("utf-8") != payload:
                path.write_text(payload, "utf-8")
            return str(path)
        if playback.manifest_url:
            manifest = str(playback.manifest_url)
            if manifest.lstrip("\ufeff\r\n\t ").upper().startswith("#EXTM3U"):
                payload = manifest.rstrip() + "\n"
                digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
                stem = re.sub(r"[^A-Za-z0-9._-]+", ".", playback.save_name).strip(".") or "playback"
                directory = self.config.paths.temp / "manifests"
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"{stem}.{digest}.m3u8"
                if not path.exists() or path.read_text("utf-8") != payload:
                    path.write_text(payload, "utf-8")
                return str(path)
            return manifest
        if playback.json_manifest is None:
            return ""
        try:
            payload = json.dumps(
                playback.json_manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n"
        except (TypeError, ValueError) as exc:
            raise ValueError(f"JSON playback manifest is not serializable: {exc}") from exc
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        stem = re.sub(r"[^A-Za-z0-9._-]+", ".", playback.save_name).strip(".") or "playback"
        directory = self.config.paths.temp / "json-manifests"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{stem}.{digest}.json"
        if not path.exists() or path.read_text("utf-8") != payload:
            path.write_text(payload, "utf-8")
        return str(path)

    def fetch_init_segments(
        self,
        playback: Playback,
        tracks: TrackSet,
        *,
        maximum_size: int = 65_536,
        collect_all: bool = False,
    ) -> list[bytes]:
        """Fetch bounded initialization data from encrypted parsed tracks.

        Init URLs and byte ranges already came from unidl.downloader's manifest parser.
        A bounded streaming read avoids accidentally downloading a media file
        when a server ignores ``Range``; 64 KiB comfortably covers normal init
        segments while keeping this a metadata operation.
        """
        proxies = {"http": playback.proxy, "https": playback.proxy} if playback.proxy else None
        payloads: list[bytes] = []
        seen: set[tuple[str, tuple[int, int] | None]] = set()
        for stream in tracks.streams:
            if not stream.encrypted:
                continue
            for segment in stream.segments:
                if segment.index != -1:
                    continue
                byte_range = tuple(segment.byte_range) if segment.byte_range else None
                identity = (segment.url, byte_range)
                if identity in seen:
                    continue
                seen.add(identity)
                if segment.data:
                    payloads.append(bytes(segment.data[:maximum_size]))
                    if not collect_all:
                        return payloads
                    continue
                if not segment.url.startswith(("http://", "https://")):
                    continue
                headers = dict(playback.headers)
                if byte_range:
                    headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
                else:
                    headers["Range"] = f"bytes=0-{maximum_size - 1}"
                try:
                    with requests.get(
                        segment.url,
                        headers=headers,
                        proxies=proxies,
                        timeout=30,
                        stream=True,
                    ) as response:
                        response.raise_for_status()
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in response.iter_content(8192):
                            if not chunk:
                                continue
                            remaining = maximum_size - total
                            chunks.append(chunk[:remaining])
                            total += min(len(chunk), remaining)
                            if total >= maximum_size:
                                break
                        if chunks:
                            payloads.append(b"".join(chunks))
                except requests.RequestException as exc:
                    self.log(f"skipping an unreadable initialization segment: {exc}")
                if payloads and not collect_all:
                    # Different representations normally repeat the same PSSH;
                    # one successfully fetched init is enough to inspect first.
                    return payloads
        return payloads

    @staticmethod
    def _apply_playback_drm_hint(
        playback: Playback,
        streams: Sequence[StreamInfo],
    ) -> None:
        """Propagate service-level DRM to manifests that omit ContentProtection.

        Some TV360 DASH documents contain only SegmentTemplate URLs. The playback
        response is still authoritative in that case: ``isDrm`` selects a license
        flow while the init segment carries the actual CENC metadata. Leaving the
        parsed streams marked clear prevents Core from fetching that init data.
        """
        drm = playback.drm
        if drm is None or not drm.needs_license:
            return
        for stream in streams:
            if stream.media_type not in {"video", "audio"}:
                continue
            stream.encrypted = True
            stream.encryption_scheme = stream.encryption_scheme or "CENC"
            for segment in stream.segments:
                segment.encrypted = True
                segment.encryption_scheme = segment.encryption_scheme or stream.encryption_scheme

    @staticmethod
    def _mark_tracks_clear(tracks: TrackSet | None) -> None:
        """Undo a service DRM hint after the authorized init proves clear."""
        if tracks is None:
            return
        for stream in tracks.streams:
            stream.encrypted = False
            stream.encryption_scheme = None
            for segment in stream.segments:
                segment.encrypted = False
                segment.encryption_scheme = None
                segment.key_id = None

    def _init_data_from_playlist(
        self, playback: Playback, system, tracks: TrackSet | None = None
    ) -> str | None:
        """Read DRM init data out of an HLS playlist.

        Three places, and which ones are read depends on the system. A master
        playlist may announce the key up front with ``EXT-X-SESSION-KEY``, which is
        what Disney+ does and is the cheap answer. For a system whose init data is
        per key id, the playlists in the encrypted licence inventory are read first:
        an audio rendition and a video rendition can carry different keys, and each
        PlayReady exchange only answers for the header it was made with, so reading
        the master alone gets one of the two keys and no sign that the other exists.

        Shared output selection is intentionally absent here. A service that needs
        a narrower plan must supply its own licence-track/profile setting and init
        data hook rather than borrowing UniDL's eventual download selection.

        When nothing announces a key, the first couple of variants are followed, for
        the services that put ``EXT-X-KEY`` only in their media playlists.
        """
        drm = playback.drm
        if drm is None:
            return None
        self.log("Reading the playlist for DRM init data")
        text = self._fetch_text(playback, playback.manifest_url)
        if not text:
            return None
        resolved: str | None = None
        for url in self._license_playlists(playback, system, tracks):
            media = self._fetch_text(playback, url)
            if media:
                resolved = system.extract(media, drm, self.log) or resolved
        # the master last, so a per-track key line is what the first exchange uses
        # and the master's is the fallback - the order the legacy scripts used
        resolved = system.extract(text, drm, self.log) or resolved
        if resolved or "#EXT-X-STREAM-INF" not in text:
            return resolved
        append_query = "--append-url-params" in playback.extra_args
        for variant in _hls_variants(text, playback.manifest_url, append_query=append_query)[:2]:
            media = self._fetch_text(playback, variant)
            if not media:
                continue
            resolved = system.extract(media, drm, self.log)
            if resolved:
                self.log("DRM init data came from a variant playlist")
                return resolved
        return None

    def _license_playlists(
        self, playback: Playback, system, tracks: TrackSet | None
    ) -> list[str]:
        """The media playlists in the parsed encrypted licence inventory.

        Empty for a system whose licence answers with every key at once, because
        then one init data is the whole title and these fetches buy nothing.
        """
        if not system.collect_init_segments or tracks is None:
            return []
        inventory = [stream for stream in tracks.streams if stream.encrypted]
        urls: list[str] = []
        for stream in inventory:
            target = str(stream.url or stream.original_url or "").strip()
            if not target:
                continue
            absolute = _hls_url(
                playback.manifest_url,
                target,
                append_query="--append-url-params" in playback.extra_args,
            )
            if absolute == playback.manifest_url or absolute in urls:
                continue
            if _looks_like_hls(absolute):
                urls.append(absolute)
        if urls:
            self.log(f"Reading {len(urls)} licence playlist(s) for per-key init data")
        return urls

    def _fetch_text(self, playback: Playback, url: str) -> str:
        proxies = {"http": playback.proxy, "https": playback.proxy} if playback.proxy else None
        try:
            response = requests.get(url, headers=playback.headers, proxies=proxies, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CdmError(f"Could not fetch playlist: {exc}") from exc
        return response.text

    def resolve_init_data(self, playback: Playback, tracks: TrackSet | None = None) -> str | None:
        """Fill DRM init data from MPD, initialization segment, then KID fallback.

        What "init data" means is the system's business: a PSSH box for Widevine,
        a WRM header for PlayReady, a licence ticket for MonaLisa. Which field it
        lands in, and whether a manifest can supply it at all, are both asked of
        the registry - so a system with no manifest step states that once, where
        it is declared, instead of being identified here by elimination.
        """
        drm = playback.drm
        if drm is None:
            return None
        wanted = drm.system or WIDEVINE
        system = drm_registry.get(wanted)
        existing = drm_registry.init_data_for(drm, wanted)
        existing_headers = drm.context.get("wrm_headers") or []
        init_scan_marker = f"init_data_scanned_{wanted}"
        needs_playready_collection = bool(
            existing
            and system is not None
            and system.collect_init_segments
            and tracks is not None
            and len(existing_headers) <= 1
            and not drm.context.get(init_scan_marker)
        )
        if existing and not needs_playready_collection:
            return existing
        if system is None or system.extract is None:
            return existing
        if not playback.inline_manifest and not playback.manifest_url:
            return None
        if playback.manifest_url and _looks_like_hls(playback.manifest_url):
            # HLS does carry init data, in an EXT-X-KEY/EXT-X-SESSION-KEY attribute
            # rather than in XML. Skipping the fetch here is what made Disney+ report
            # "no PSSH found in the manifest" about a playlist that announces one.
            return self._init_data_from_playlist(playback, system, tracks)

        # Everything else is worth fetching: this used to require ".mpd" in the URL,
        # and a service that serves DASH from a path without it - Plex serves
        # /library/parts/<id>-dash - was told "no PSSH found in the manifest" about a
        # manifest nobody had looked at.
        self.log(
            "Reading inline manifest for DRM init data"
            if playback.inline_manifest
            else "Fetching manifest for DRM init data"
        )
        try:
            manifest = self.fetch_manifest(playback) or b""
        except requests.RequestException as exc:
            raise CdmError(f"Could not fetch manifest: {exc}") from exc

        resolved = system.extract(manifest, drm, self.log)
        if resolved or tracks is None:
            return resolved

        self.log("Fetching initialization segment for DRM init data")
        init_result: str | None = None
        init_payloads: list[bytes] = []
        try:
            init_payloads = self.fetch_init_segments(
                playback,
                tracks,
                collect_all=system.collect_init_segments,
            )
            for init_data in init_payloads:
                resolved = system.extract(init_data, drm, self.log)
                if resolved:
                    init_result = resolved
                    if not system.collect_init_segments:
                        return resolved
        finally:
            if system.collect_init_segments:
                # A second vault check in the same delivery must not fetch the
                # same MPD and init resources again. New playback contexts get a
                # fresh marker, so this does not suppress a later authorization.
                drm.context[init_scan_marker] = True

        if init_result:
            return init_result

        if wanted == WIDEVINE:
            from . import pssh as pssh_tools

            key_ids = pssh_tools.key_ids_from_mpd(manifest)
            if not key_ids:
                key_ids = self.downloader.key_ids(
                    [stream for stream in tracks.streams if stream.encrypted]
                )
            if not key_ids:
                for init_data in init_payloads:
                    key_ids.extend(pssh_tools.key_ids_from_init_segment(init_data))
                key_ids = list(dict.fromkeys(key_ids))
            fallback = pssh_tools.from_key_ids(key_ids)
            if fallback:
                drm.pssh = fallback
                self.log("Widevine: built a KID-only fallback PSSH")
                return fallback
        if init_payloads:
            from . import pssh as pssh_tools

            if not any(pssh_tools.init_segment_has_protection(data) for data in init_payloads):
                drm.clear = True
                drm.pssh = None
                drm.wrm_header = None
                self._mark_tracks_clear(tracks)
                self.log("DRM metadata absent and init segment is clear; skipping license")
                return None
        return None


    @staticmethod
    def vault_use(settings: Settings | None) -> tuple[bool, bool]:
        """Which vaults may be used: ``(local, remote)``.

        Two switches, because they are two different decisions. Reading a local
        file is free and private; reaching a remote vault is a network call that
        also sends keys somewhere. ``None`` settings - a check script, a caller
        with no scope - means local only, which is the safe reading of "no opinion":
        nothing leaves the machine because nobody asked for it to.
        """
        if settings is None:
            return True, False
        return (
            bool(settings.get("local_vault", True)),
            bool(settings.get("remote_vault", False)),
        )

    @staticmethod
    def vault_targets(
        settings: Settings | None,
    ) -> tuple[
        bool,
        bool,
        tuple[str, ...] | None,
        tuple[str, ...] | None,
        tuple[str, ...] | None,
        tuple[str, ...] | None,
    ]:
        """Return master switches plus read and write vault name filters.

        The old boolean switches remain the safety gates.  The two target
        settings narrow those gates to named vaults; ``None`` means every vault
        in that category and ``()`` means none.
        """
        use_local, use_remote = Engine.vault_use(settings)
        if settings is None:
            return use_local, use_remote, None, None, None, None
        read = vaults.parse_targets(settings.get("vault_read_targets", ""))
        write = vaults.parse_targets(settings.get("vault_write_targets", ""))
        return use_local, use_remote, read, read, write, write

    @staticmethod
    def vault_reads(settings: Settings | None) -> bool:
        """Whether any vault may answer instead of the licence server."""
        return any(Engine.vault_use(settings))

    @staticmethod
    def _drm_key_ids(playback: Playback) -> list[str]:
        """KIDs discovered in DRM init data in addition to track metadata."""
        drm = playback.drm
        if drm is None:
            return []
        found: list[str] = []

        def add(value: object) -> None:
            kid = normalize_hex(value)
            if kid and kid not in found:
                found.append(kid)

        if drm.system == drm_registry.PLAYREADY:
            from . import playready

            for header in drm.context.get("wrm_headers") or []:
                for kid in playready.key_ids_from_header(str(header)):
                    add(kid)
        elif drm.system == WIDEVINE and drm.pssh:
            # Widevine v1 PSSH boxes can carry several KIDs even when the
            # UniDL stream metadata exposes only the first one.
            try:
                from pywidevine.pssh import PSSH

                for kid in PSSH(drm.pssh).key_ids:
                    add(kid)
            except Exception:  # noqa: BLE001 - malformed optional init data is ignored
                pass
        return found

    def vault_lookup(
        self,
        playback: Playback,
        tracks: TrackSet,
        service,
        settings: Settings | None = None,
    ) -> list[str]:
        """Serve keys from the vaults when every encrypted KID is known.

        Cheap and worth doing: a service usually issues the same KID for video
        and audio and across renditions, so one stored key often covers a whole
        title, and a repeat download needs no license exchange at all.

        The return value remains all-or-nothing for callers that only want to
        decide whether a licence request can be skipped. A partial hit is kept
        on the playback's DRM context, however, so the normal service-owned
        exchange can fill the missing KIDs and the two sets can be merged. A
        manifest with several KIDs must never lose a key merely because one of
        them was already cached.
        """
        if playback.drm is not None:
            # Do not let a second lookup reuse a result from an earlier playback
            # state (for example after more init data was discovered).
            playback.drm.context.pop("vault_keys", None)
        use_local, use_remote, read_local, read_remote, write_local, _write_remote = (
            self.vault_targets(settings)
        )
        if not (use_local or use_remote):
            self._log_vault_status(
                playback,
                "vault: both vault switches are off, going to the licence server",
            )
            return []
        # Vault coverage follows the same encrypted inventory as licensing. It is
        # the full ladder normally; the explicit post-selection compatibility mode
        # supplies a selected-only TrackSet instead.
        encrypted = [stream for stream in tracks.streams if stream.encrypted]
        if not encrypted:
            return []
        # ``license_track_kids`` normally contains the same ids as the parsed
        # streams, but a service may replace it in ``prepare_drm`` when its media
        # and licence protocols spell the same KID differently. Netflix
        # PlayReady Web is the important case: JSON/MP4 carries a little-endian
        # fragment GUID while its WRM header, licence response and stored vault
        # row use the canonical UUID. Looking up the stream spelling made every
        # repeat download miss keys that were already in the vault.
        declared = list(playback.drm.context.get("license_track_kids") or [])
        inventory_kids = declared or self.downloader.key_ids(encrypted)
        kids: list[str] = []
        for value in [*inventory_kids, *self._drm_key_ids(playback)]:
            kid = normalize_hex(value)
            if kid and kid not in kids:
                kids.append(kid)
        if not kids:
            return []
        found = self.vaults.get_keys(
            kids,
            service.ID,
            use_local=use_local,
            use_remote=use_remote,
            local_names=read_local,
            remote_names=read_remote,
            backfill_local_names=write_local,
        )
        cached = self._merge_key_pairs(
            [f"{kid}:{found[kid]}" for kid in kids if kid in found]
        )
        if playback.drm is not None and cached:
            # Service-specific DRM code (notably PlayReady) can use this to avoid
            # asking a header whose KID is already covered by the vault.
            playback.drm.context["vault_keys"] = cached
        missing = [kid for kid in kids if kid not in found]
        if missing:
            self._log_vault_status(
                playback,
                f"vault: {len(found)}/{len(kids)} keys cached, requesting a license",
            )
            return []
        active = self.vaults.enabled(
            use_local=use_local,
            use_remote=use_remote,
            local_names=read_local,
            remote_names=read_remote,
        )
        where = ", ".join(backend.name for backend in active) or "selected"
        self._log_vault_status(
            playback,
            f"vault: all {len(found)} keys served from {where}, no license request",
        )
        return cached

    def _log_vault_status(self, playback: Playback, message: str) -> None:
        """Log a changed vault result once for one playback.

        Init-data discovery deliberately performs a second lookup because it can
        reveal more KIDs. If it reveals none, printing the identical ``0/2`` (or
        identical full-hit message) twice looks like two failed operations.
        """
        drm = playback.drm
        if drm is not None:
            if drm.context.get("_vault_status") == message:
                return
            drm.context["_vault_status"] = message
        self.log(message)

    @staticmethod
    def _merge_key_pairs(*groups: Sequence[str]) -> list[str]:
        """Merge ``kid:key`` lists, keeping the first key for each KID.

        Vault keys are deliberately first: a service exchange only fills the
        KIDs that were not cached, while a duplicate response must not replace a
        key that has already been selected for this playback.
        """
        merged: dict[str, str] = {}
        for group in groups:
            for value in group:
                pair = split_pair(str(value))
                if pair:
                    merged.setdefault(*pair)
        return [f"{kid}:{key}" for kid, key in merged.items()]

    @staticmethod
    def _format_keys(service, playback: Playback, keys: Sequence[str]) -> list[str]:
        """Apply a service's external key representation, if it has one."""
        formatter = getattr(service, "format_keys", None)
        if not callable(formatter):
            return list(keys)
        return list(formatter(playback, list(keys)) or [])

    def device_used(self, playback: Playback, service, system_id: str = "") -> str:
        """Which CDM this playback's licence actually goes through.

        Narrowest first, in the order :meth:`Service.resolve_keys` itself resolves
        it: the playback's own pin - a service that chose a device for this title,
        or a quality rule that chose one for this resolution - then the service's
        setting for the DRM system in play, then whatever the session was built
        with.

        One definition, two readers, because "which device" has one answer and they
        used to disagree: the log named the device the licence went out on while the
        vault recorded the app-wide one, so a key fetched with a service's own L1
        was filed against whatever the main screen happened to be showing.
        """
        drm = playback.drm
        pinned = str(getattr(drm, "cdm", "") or "")
        if pinned:
            return pinned
        system = system_id or str(getattr(drm, "system", "") or "")
        named = getattr(service, "cdm_name", None)
        if callable(named):
            found = str(named(system) or "")
            if found:
                return found
        return str(getattr(getattr(service, "ctx", None), "device_name", "") or "")

    def store_keys(self, playback: Playback, service, settings: Settings | None = None) -> int:
        """Record freshly acquired keys with where they came from.

        A **selected local** write happens whatever the local-read switch says:
        turning reuse off is about not trusting what is stored, not about refusing
        to record what a real licence request just returned. The write-target list
        can still deliberately exclude one database or every database.

        The **remote** write follows its switch, because that one is an action with
        an effect outside this machine. A remote vault that is switched off is not
        written to, and that asymmetry is the point of having two switches rather
        than one.
        """
        if not playback.keys:
            return 0
        _use_local, use_remote, _read_local, _read_remote, write_local, write_remote = (
            self.vault_targets(settings)
        )
        reports = self.vaults.add_pairs_report(
            service.ID,
            playback.keys,
            use_local=True,
            use_remote=use_remote,
            local_names=write_local,
            remote_names=write_remote,
            title=playback.save_name,
            pssh=playback.drm.pssh if playback.drm else None,
            source="license",
            cdm=self.device_used(playback, service) or None,
        )
        accepted = [
            report
            for report in reports
            if not report.error and not report.skipped
        ]
        if accepted:
            destinations = ", ".join(
                f"{report.name} ({report.added} new)" for report in accepted
            )
            self.log(f"vault: write targets {destinations}")
        elif not reports:
            self.log(
                "vault: no writable target is active; check the write-target "
                "selection and remote-vault switch"
            )
        local = [report.added for report in accepted if not report.remote]
        remote = [report.added for report in accepted if report.remote]
        return max(local or remote or [0])

    def live_key_provider(
        self,
        playback: Playback,
        service,
        settings: Settings | None = None,
    ) -> Callable[[str | None, object, object, str], str]:
        """Resolve a KID discovered after a live recording has started.

        UniDL used to ask for this on the controlling terminal. That is valid
        for its standalone CLI, but an embedded Textual application already owns
        the terminal and must never let a downloader change its input mode.

        The automatic provider is intentionally a service-declared Widevine path:
        the event's exact stream/segment/KID goes to ``service.live_key_pssh`` and
        the resulting challenge still goes through that service's
        ``resolve_keys`` / ``get_license``. There is no common licence transport
        and no guessed KID-only PSSH. If vault and service cannot answer, the TUI
        callback asks for KEY or KID:KEY without exposing Textual's stdin.
        """

        def remember(values: Sequence[str], *, source: str, pssh: str | None = None) -> list[str]:
            pairs = self._merge_key_pairs(values)
            if not pairs:
                return []
            playback.keys = self._merge_key_pairs(playback.keys, pairs)
            if service is None:
                return pairs
            (
                _use_local,
                use_remote,
                _read_local,
                _read_remote,
                write_local,
                write_remote,
            ) = self.vault_targets(settings)
            stored = self.vaults.add_pairs(
                service.ID,
                pairs,
                use_local=True,
                use_remote=use_remote,
                local_names=write_local,
                remote_names=write_remote,
                title=playback.save_name,
                pssh=pssh,
                source=source,
                cdm=self.device_used(playback, service, WIDEVINE) or None,
            )
            if stored:
                self.log(f"live key rotation: stored {stored} new key(s) in the vault")
            return pairs

        def provide(kid: str | None, stream: object, segment: object, reason: str) -> str:
            wanted = normalize_hex(kid)
            automatic_error = ""
            (
                use_local,
                use_remote,
                read_local,
                read_remote,
                write_local,
                _write_remote,
            ) = self.vault_targets(settings)
            validation_failed = "validation failed" in str(reason).lower()
            # A validation failure means the key already in hand did not decrypt
            # the fragment. Do not feed the same cached value back into the retry.
            if (
                wanted
                and service is not None
                and not validation_failed
                and (use_local or use_remote)
            ):
                cached = self.vaults.get_keys(
                    [wanted],
                    service.ID,
                    use_local=use_local,
                    use_remote=use_remote,
                    local_names=read_local,
                    remote_names=read_remote,
                    backfill_local_names=write_local,
                )
                if key := cached.get(wanted):
                    pair = f"{wanted}:{key}"
                    playback.keys = self._merge_key_pairs(playback.keys, [pair])
                    self.log(f"live key rotation: {wanted} served from the vault")
                    return pair

            drm = playback.drm
            held_kids = {
                pair[0]
                for value in playback.keys
                if (pair := split_pair(str(value))) is not None
            }
            if wanted and wanted in held_kids:
                automatic_error = (
                    f"KID {wanted} is already in the recording key set; automatic "
                    "licence rotation only handles a newly observed KID"
                )
            elif wanted and drm is not None and service is not None:
                system = str(drm.system or WIDEVINE).lower()
                if system != WIDEVINE:
                    automatic_error = (
                        f"automatic live rotation needs a {system} service implementation"
                    )
                elif service.USES.is_self("drm"):
                    automatic_error = (
                        f"{service.NAME} owns its DRM flow and exposes no core live exchange"
                    )
                else:
                    try:
                        pssh = service.live_key_pssh(
                            playback,
                            stream,
                            segment,
                            wanted,
                            self.log,
                        )
                        if pssh:
                            context = dict(drm.context)
                            context.pop("vault_keys", None)
                            context["license_track_kids"] = [wanted]
                            rotated_drm = replace(
                                drm,
                                system=WIDEVINE,
                                pssh=pssh,
                                wrm_header=None,
                                context=context,
                            )
                            rotated = replace(playback, drm=rotated_drm, keys=[])
                            self.log(
                                f"live key rotation: requesting {wanted} through "
                                f"{service.NAME}"
                            )
                            fresh = list(service.resolve_keys(rotated, log=self.log) or [])
                            fresh = remember(fresh, source="license", pssh=pssh)
                            matched = [
                                value
                                for value in fresh
                                if (pair := split_pair(value)) and pair[0] == wanted
                            ]
                            if matched:
                                return matched[0]
                            automatic_error = (
                                f"{service.NAME} licence contained no key for {wanted}"
                            )
                        else:
                            automatic_error = (
                                f"{service.NAME} could not match this KID to an exact PSSH"
                            )
                    except Exception as exc:  # noqa: BLE001 - manual entry is the fallback
                        automatic_error = str(exc)
            elif not wanted:
                automatic_error = "the live fragment did not expose its KID"
            elif service is None:
                automatic_error = "the embedded download has no service licence context"
            else:
                automatic_error = "the playback has no DRM context"

            if automatic_error:
                self.log(f"live key rotation: automatic lookup failed ({automatic_error})")
            if self.live_key_input is not None:
                supplied = self.live_key_input(
                    wanted,
                    stream,
                    segment,
                    reason,
                    automatic_error,
                )
                pair = split_pair(str(supplied or ""))
                if pair and (not wanted or pair[0] == wanted):
                    return remember(
                        [f"{pair[0]}:{pair[1]}"],
                        source="manual",
                    )[0]
                raise CdmError(
                    f"{reason}: manual live key entry did not match KID "
                    f"{wanted or 'unknown'}"
                )
            raise CdmError(
                f"{reason}: no key is available for live KID {wanted or 'unknown'}"
                f"{f' ({automatic_error})' if automatic_error else ''}"
            )

        return provide

    def warn_uncovered(self, playback: Playback, tracks: TrackSet | None) -> list[str]:
        """Name licence-inventory KIDs the keys in hand do not open. Returns them.

        Says nothing when everything is covered, which is the normal case, and
        nothing when the manifest declares no key ids - HLS usually does not, and
        "no ids" is not "wrong ids".
        """
        if tracks is None:
            return []
        held = {
            str(pair).partition(":")[0].strip().lower().replace("-", "")
            for pair in playback.keys
        }
        inventory = [stream for stream in tracks.streams if stream.encrypted]
        wanted = [
            str(kid).strip().lower().replace("-", "")
            for kid in (self.downloader.key_ids(inventory) or [])
        ]
        missing = [kid for kid in dict.fromkeys(wanted) if kid and kid not in held]
        if missing:
            self.log(
                f"warning: {len(missing)} licence track key(s) are not in the keys "
                f"in hand: {', '.join(missing)}"
            )
        return missing

    def apply_cdm_rule(
        self,
        playback: Playback,
        service,
        tracks: TrackSet | None,
        settings: Settings | None,
        system_id: str,
    ) -> str:
        """Let a quality rule name the device for this licence. Returns its name.

        Written onto the playback's own ``drm.cdm``, which is the field a service
        already uses to pin a device for one title - so the rule enters the
        existing precedence at the narrowest end and nothing downstream needs to
        know a rule was involved. A playback that already names a device keeps it:
        a service that pinned one knows something a rule about resolution does not.

        Silent when there are no rules, which is every install that has not written
        any. Says so when there are rules and the manifest reports no resolution,
        because "the rule did not fire" is otherwise indistinguishable from "the
        rule is not there".
        """
        drm = playback.drm
        if drm is None or drm.cdm or settings is None:
            return ""
        rules = cdmrules.parse(settings.get("cdm_rules", ""))
        if not rules:
            return ""
        inventory = (
            [stream for stream in tracks.streams if stream.encrypted]
            if tracks is not None
            else []
        )
        height = cdmrules.height_of(inventory)
        config = getattr(getattr(service, "ctx", None), "config", None)
        rule = cdmrules.choose(
            rules, height, system=system_id, systems=cdmrules.device_systems(config)
        )
        if rule is None:
            # An "any resolution" rule would have matched even here, so this really
            # is "your rules are all about numbers and there is no number".
            self.log(
                f"cdm rules: none of them covers {height}p"
                if height
                else "cdm rules: nothing here says what resolution it is, so none applied"
            )
            return ""
        drm.cdm = rule.device
        self.log(f"cdm rule: {rule.label()} -> {rule.device}")
        return rule.device

    def resolve_keys(
        self,
        playback: Playback,
        service,
        tracks: TrackSet | None = None,
        settings: Settings | None = None,
        *,
        license_tracks: TrackSet | None = None,
    ) -> list[str]:
        """Run whichever DRM path the service declared, vault first."""
        # ``tracks.selected`` normally belongs only to output selection.  The
        # optional explicit inventory is the one deliberate compatibility mode
        # where a service asks us to license only the media playlists the user
        # confirmed (HLS services can hide init data in child playlists).  Keeping
        # this keyword opt-in preserves the long-standing full-manifest behavior.
        inventory_tracks = (
            license_tracks
            if license_tracks is not None
            else self.license_inventory(tracks)
        )
        if playback.keys:
            # Nothing to ask for. A playback that arrives with keys is a service
            # that fetched them itself or an import replaying a file. Check those
            # keys against Core's encrypted inventory so an incomplete export is
            # named clearly. That inventory is full by default and selected-only
            # only under the explicit compatibility switch.
            playback.keys = self._format_keys(service, playback, playback.keys)
            self.warn_uncovered(playback, inventory_tracks)
            return playback.keys

        # A merged ladder can contain tracks authorized by different playback
        # responses.  They may carry different DRM tokens, licence URLs or even
        # manifest technologies, so treating the primary Playback as the licence
        # context for every track is unsafe.  Resolve each origin independently,
        # then expose one key set to the native downloader.
        variant_groups = self._variant_license_groups(playback, inventory_tracks)
        if variant_groups:
            return self._resolve_variant_keys(
                playback,
                service,
                variant_groups,
                settings,
                selected_only=license_tracks is not None,
            )
        drm = playback.drm
        if drm is None:
            return []
        if not drm.needs_license:
            # Clear playbacks and raw HLS keys do not enter either the
            # service-owned or Core-owned licence paths.
            return []

        # This is a DRM-only inventory. It is deliberately derived from the full
        # parsed ladder and never from ``TrackSet.selected``, which belongs solely
        # to UniDL output. A self-owned service may narrow this inventory only
        # with its own service-level ``license_*`` policy.
        if inventory_tracks is not None:
            inventory = list(inventory_tracks.selected)
            known_kids = list(drm.context.get("license_track_kids") or [])
            drm.context["license_tracks"] = inventory
            drm.context["license_inventory_mode"] = (
                "selected" if license_tracks is not None else "full"
            )
            drm.context["license_track_kids"] = list(
                dict.fromkeys([*known_kids, *self.downloader.key_ids(inventory)])
            )

        if service.USES.is_self("drm"):
            self.log("Requesting keys from the service")
            playback.keys = list(service.get_keys(playback) or [])
            self.store_keys(playback, service, settings)
            playback.keys = self._format_keys(service, playback, playback.keys)
            return playback.keys

        # Three levels, narrowest first. A playback that named a system meant it
        # - `system is None` is what "no opinion" looks like. Then the service's
        # own setting, which exists only for services that can do more than one
        # system and applies to that service alone. Then the app-wide setting.
        # Then Widevine, because something has to be tried.
        if not drm.system:
            declared = getattr(service, "drm_default", lambda: "")()
            chosen = settings.inherited("drm_system", "") if settings is not None else ""
            drm.system = str(declared or chosen or WIDEVINE)

        system = drm_registry.require(drm.system)
        if inventory_tracks is not None:
            prepare = getattr(service, "prepare_drm", None)
            if callable(prepare):
                prepare(playback, inventory_tracks, self.log)
        if inventory_tracks is not None:
            cached = self.vault_lookup(playback, inventory_tracks, service, settings)
            # A cache hit is only provisional until init data has been scanned.
            # HLS PlayReady media playlists and Widevine v1 PSSH boxes can name
            # more KIDs than UniDL's stream metadata does. Recheck the vault
            # against that complete set before deciding that no licence is needed.
            if cached or playback.drm.context.get("vault_keys") or system.collect_init_segments:
                self.resolve_init_data(playback, inventory_tracks)
                if drm.clear:
                    playback.keys = []
                    return playback.keys
                cached = self.vault_lookup(playback, inventory_tracks, service, settings)
            if cached:
                playback.keys = cached
                playback.keys = self._format_keys(service, playback, playback.keys)
                return playback.keys
        self.resolve_init_data(playback, inventory_tracks)
        if drm.clear:
            playback.keys = []
            return playback.keys
        if not drm_registry.init_data_for(drm, system.id):
            raise CdmError(system.missing_init)

        # CDM routing is part of key acquisition, so it sees the licence inventory,
        # never the later output choice.
        self.apply_cdm_rule(playback, service, inventory_tracks, settings, system.id)

        # The device for *this system*, not the one resolved when the service was
        # built: a service that can do both systems has one device per system, and
        # naming the app-wide one here reported a Widevine .wvd while a PlayReady
        # licence was being fetched with a .prd.
        where = self.device_used(playback, service, system.id) or "default device"
        # "licence request" would be a lie for a system that does not make one
        what = "license request" if system.networked else "local ticket"
        self.log(f"{system.label} {what} ({where})")
        fresh_keys = list(service.resolve_keys(playback, log=self.log) or [])
        cached_keys = playback.drm.context.get("vault_keys") or []
        playback.keys = (
            self._merge_key_pairs(cached_keys, fresh_keys)
            if cached_keys
            else list(fresh_keys)
        )
        raw_keys = list(playback.keys)
        output_keys = self._format_keys(service, playback, raw_keys)
        for key in output_keys:
            self.log(f"key {key}")
        self.store_keys(playback, service, settings)
        playback.keys = output_keys
        return playback.keys

    @staticmethod
    def _variant_license_groups(
        primary: Playback,
        tracks: TrackSet | None,
    ) -> list[tuple[Playback, list[StreamInfo]]]:
        """Group an encrypted inventory by its service authorization.

        An ordinary manifest has either no origin map or only ``primary`` and
        keeps the established single-playback path.  A single surviving variant
        still needs this path when the primary manifest failed to parse.
        """
        if tracks is None or not tracks.selected or not tracks.origins:
            return []
        grouped: list[tuple[Playback, list[StreamInfo]]] = []
        positions: dict[int, int] = {}
        for stream in tracks.selected:
            origin = tracks.origins.get(id(stream), primary)
            identity = id(origin)
            index = positions.get(identity)
            if index is None:
                positions[identity] = len(grouped)
                grouped.append((origin, [stream]))
            else:
                grouped[index][1].append(stream)
        if len(grouped) == 1 and grouped[0][0] is primary:
            return []
        return grouped

    def _resolve_variant_keys(
        self,
        primary: Playback,
        service,
        groups: list[tuple[Playback, list[StreamInfo]]],
        settings: Settings | None,
        *,
        selected_only: bool,
    ) -> list[str]:
        """Resolve every authorized manifest group without crossing contexts."""
        self.log(f"resolving keys across {len(groups)} authorized manifest profile(s)")
        combined: list[str] = []
        failures: list[str] = []
        for index, (origin, streams) in enumerate(groups, start=1):
            label = str(origin.requested or origin.returned or origin.note or f"profile {index}")
            self.log(f"manifest profile {index}/{len(groups)}: {label}")
            group = TrackSet(streams=list(streams), selected=list(streams))
            try:
                keys = self.resolve_keys(
                    origin,
                    service,
                    group,
                    settings,
                    license_tracks=(group if selected_only else None),
                )
            except Exception as exc:  # continue so another selected profile can still succeed
                failures.append(f"{label}: {exc}")
                self.log(f"manifest profile {index}/{len(groups)} key request failed: {exc}")
                continue
            combined = self._merge_key_pairs(combined, keys)

        primary.keys = combined
        if failures:
            detail = "; ".join(failures)
            raise CdmError(
                f"Could not resolve every authorized manifest profile ({detail})"
            )
        return primary.keys

    @staticmethod
    def license_inventory(
        tracks: TrackSet | None,
        *,
        selected_only: bool = False,
    ) -> TrackSet | None:
        """Return a DRM-only view of parsed encrypted tracks.

        The default is intentionally the complete manifest inventory.  Callers
        that explicitly opt into post-selection licensing can request the final
        output set with ``selected_only=True``; this does not alter the shared
        track-selection rules themselves.
        """
        if tracks is None:
            return None
        streams = list(tracks.selected if selected_only else tracks.streams)
        encrypted = [stream for stream in streams if stream.encrypted]
        origins = {
            id(stream): tracks.origins[id(stream)]
            for stream in streams
            if id(stream) in tracks.origins
        }
        return TrackSet(streams=streams, selected=encrypted, origins=origins)

    # ------------------------------------------------------------------ parse
    def parse_request(
        self,
        playback: Playback,
        settings: Settings,
        *,
        details: bool | None = None,
    ) -> ParseRequest:
        """Build the typed manifest request used by the production flow."""
        overrides = _delivery_overrides(playback.extra_args)
        return ParseRequest(
            self.delivery_source(playback),
            ParsePolicy(
                headers=dict(playback.headers),
                proxy=playback.proxy,
                details=(
                    bool(settings.get("hls_details", False))
                    if details is None
                    else details
                ),
                append_url_params=overrides.append_url_params,
                ad_keywords=tuple(overrides.ad_keywords),
                drop_video=normalize_drop_video_pattern(
                    settings.get("drop_video", "")
                ),
            ),
            scratch_dir=self.config.paths.temp / "manifests",
        )

    def parse_options(self, playback: Playback, settings: Settings):
        """Legacy diagnostic view; production parsing uses :meth:`parse_request`."""
        overrides = _delivery_overrides(playback.extra_args)
        return api.ParseOptions(
            input=self.playback_input(playback),
            headers=dict(playback.headers),
            proxy=playback.proxy,
            details=bool(settings.get("hls_details", False)),
            append_url_params=overrides.append_url_params,
            ad_keywords=list(overrides.ad_keywords),
            # trick-play and thumbnail ladders otherwise win "best video" on
            # height alone, which is what the old scripts used -dv to avoid
            drop_video=normalize_drop_video_pattern(settings.get("drop_video", "")),
        )

    @staticmethod
    def _load_streams(options) -> list[StreamInfo]:
        """Call the native parser without letting argparse kill the host UI."""
        try:
            return api.load_streams(options)
        except (SystemExit, api.DownloaderArgumentError) as exc:
            code = getattr(exc, "code", None)
            detail = (
                f"exit code {code}"
                if isinstance(code, int)
                else str(code or exc or "unknown error")
            )
            raise ValueError(
                f"native downloader rejected manifest parser options ({detail})"
            ) from exc

    def _parse_manifest(self, request: ParseRequest) -> ParsedManifest:
        """Parse through the typed backend while keeping host-safe errors."""
        try:
            return self.downloader.parse(request)
        except NativeManifestError as exc:
            code = getattr(exc, "code", None)
            detail = (
                f"exit code {code}"
                if isinstance(code, int)
                else str(code or exc or "unknown error")
            )
            raise ValueError(
                f"native downloader rejected manifest parser options ({detail})"
            ) from exc

    def parse_tracks(
        self,
        playback: Playback,
        settings: Settings,
        *,
        service=None,
    ) -> TrackSet:
        """Parse a ladder without making any output-track selection.

        Ordinary playbacks retain their first-usable authorized-candidate
        semantics. A service can explicitly opt into a merged ladder and provide
        additional fully authorized :class:`Playback` objects; only then are all
        successful variants parsed and deduplicated by the native backend.
        """
        if playback.merge_manifest_segments and not playback.is_live:
            return self._parse_manifest_segments(playback, settings, service=service)

        if not playback.merge_manifests or playback.is_live:
            if playback.merge_manifests and playback.is_live:
                self.log(
                    "manifest merge skipped for live playback; preserving refreshable "
                    "media-playlist state"
                )
            manifest = self._parse_playback_manifest(playback, settings)
            streams = self.downloader.streams(manifest)
            return TrackSet(
                streams=streams,
                manifest=manifest,
                origins={id(stream): playback for stream in streams},
            )

        variants = [playback]
        if service is not None:
            provided = service.manifest_variants(playback, self.log) or []
            variants.extend(item for item in provided if isinstance(item, Playback))
        else:
            self.log(
                "manifest merge requested without a service; using the primary "
                "authorized playback only"
            )

        manifests: list[ParsedManifest] = []
        successful_variants: list[Playback] = []
        last_error: Exception | None = None
        for index, variant in enumerate(variants, start=1):
            try:
                manifests.append(self._parse_playback_manifest(variant, settings))
                successful_variants.append(variant)
            except Exception as exc:  # one requested profile can be unavailable
                last_error = exc
                self.log(
                    f"manifest profile {index}/{len(variants)} unavailable: {exc}"
                )
        if not manifests:
            if last_error is not None:
                raise last_error
            raise ValueError("Playback did not provide a usable manifest")
        if len(manifests) == 1:
            streams = self.downloader.streams(manifests[0])
            origin = successful_variants[0]
            return TrackSet(
                streams=streams,
                manifest=manifests[0],
                origins={id(stream): origin for stream in streams},
            )

        merged = self.downloader.merge(manifests)
        streams = self.downloader.streams(merged)
        all_origins: dict[int, Playback] = {}
        for variant, manifest in zip(successful_variants, manifests, strict=True):
            for stream in self.downloader.streams(manifest):
                all_origins.setdefault(id(stream), variant)
        origins = {
            id(stream): all_origins[id(stream)]
            for stream in streams
            if id(stream) in all_origins
        }
        self.log(
            f"merged {len(manifests)} authorized manifests into "
            f"{len(streams)} distinct tracks"
        )
        return TrackSet(streams=streams, manifest=merged, origins=origins)

    def _parse_manifest_segments(
        self,
        playback: Playback,
        settings: Settings,
        *,
        service=None,
    ) -> TrackSet:
        """Parse and concatenate finite service-authorized timeline windows."""
        manifests = [self._parse_playback_manifest(playback, settings)]
        if service is None:
            self.log(
                "timeline merge requested without a service; using the primary "
                "authorized window only"
            )
        else:
            for index, variant in enumerate(
                service.manifest_segments(playback, self.log) or (),
                start=2,
            ):
                if not isinstance(variant, Playback):
                    continue
                try:
                    manifests.append(self._parse_playback_manifest(variant, settings))
                except Exception as exc:
                    self.log(f"timeline window {index} unavailable: {exc}")

        merged = self.downloader.merge_segments(
            manifests,
            duration=float(playback.manifest_duration or 0),
        )
        streams = self.downloader.streams(merged)
        origins = {id(stream): playback for stream in streams}
        self.log(
            f"merged {len(manifests)} authorized timeline windows into "
            f"{max((stream.segments_count for stream in streams), default=0)} segments"
        )
        return TrackSet(streams=streams, manifest=merged, origins=origins)

    def _parse_playback_manifest(
        self,
        playback: Playback,
        settings: Settings,
    ) -> ParsedManifest:
        """Parse the first usable authorized candidate for one playback."""
        candidates = [str(playback.manifest_url or "")]
        candidates.extend(str(url) for url in playback.alternate_manifest_urls if url)
        candidates = list(dict.fromkeys(url for url in candidates if url))

        # Dictionary manifests have no URL candidates and keep the ordinary
        # single-load path. URL retries are opt-in facts supplied by a service.
        if not candidates:
            manifest = self._parse_manifest(self.parse_request(playback, settings))
            streams = self.downloader.streams(manifest)
            self._apply_playback_drm_hint(playback, streams)
            self._apply_video_range_hint(playback, streams)
            return self.downloader.adopt(manifest.request, streams)

        attempts = max(1, int(playback.manifest_attempts or 1))
        last_error: Exception | None = None
        for candidate_index, candidate in enumerate(candidates, 1):
            playback.manifest_url = candidate
            for attempt in range(1, attempts + 1):
                try:
                    manifest = self._parse_manifest(
                        self.parse_request(playback, settings)
                    )
                    streams = self.downloader.streams(manifest)
                except Exception as exc:  # one authorized candidate may be unavailable
                    last_error = exc
                    if attempt < attempts:
                        self.log(
                            f"Manifest unavailable; retrying {attempt + 1}/{attempts}"
                        )
                    elif candidate_index < len(candidates):
                        self.log(
                            "Manifest unavailable; trying authorized alternate "
                            f"{candidate_index + 1}/{len(candidates)}"
                        )
                    continue
                self._apply_playback_drm_hint(playback, streams)
                self._apply_video_range_hint(playback, streams)
                return self.downloader.adopt(manifest.request, streams)

        if last_error is not None:
            raise last_error
        raise ValueError("Playback did not provide a usable manifest URL")

    @staticmethod
    def _apply_video_range_hint(playback: Playback, streams: Sequence[StreamInfo]) -> None:
        hint = str(playback.video_range_hint or "").strip().upper()
        if hint not in {"DV", "HDR10", "HDR10+", "HDR VIVID", "HLG"}:
            return
        for stream in streams:
            current = str(stream.video_range or "").strip().upper()
            if stream.media_type == "video" and hint == "HDR VIVID" and current in {
                "HDR",
                "HDR10",
                "HDRVIVID",
                "HDR-VIVID",
                "PQ",
                "VIVID",
            }:
                stream.video_range = "HDR Vivid"
                continue
            if stream.media_type == "video" and current in {"", "SDR", "UNKNOWN"}:
                stream.video_range = hint

    def select_tracks(
        self,
        playback: Playback,
        settings: Settings,
        tracks: TrackSet,
    ) -> list[StreamInfo]:
        """Apply shared Track output selection to an already parsed ladder."""
        kwargs = {"strict": True} if playback.strict_track_selection else {}
        tracks.selected = self.auto_select(
            tracks.streams,
            settings,
            audio_only=playback.audio_only,
            quality_override=playback.video_quality_hint,
            **kwargs,
        )
        return tracks.selected

    def load_tracks(
        self,
        playback: Playback,
        settings: Settings,
        *,
        service=None,
    ) -> TrackSet:
        """Parse and select tracks for non-interactive engine callers."""
        tracks = self.parse_tracks(playback, settings, service=service)
        self.select_tracks(playback, settings, tracks)
        return tracks

    def auto_select(
        self,
        streams: Sequence[StreamInfo],
        settings: Settings,
        *,
        audio_only: bool = False,
        strict: bool = False,
        quality_override: str = "",
    ) -> list[StreamInfo]:
        """Apply the shared track settings against a real ladder."""
        streams = self._normalize_audio_only_streams(streams, audio_only=audio_only)
        options = SelectionOptions()

        if audio_only:
            # No video selector at all rather than a filter that drops it: a
            # radio stream has no video ladder to rank, and UniDL refuses
            # --audio-format outright if a video track is selected.
            return self._select_audio_only(streams, settings)

        quality = quality_override or settings.get("video_quality", "best")
        codec = settings.get("video_codec", "any")
        video_range = settings.get("video_range", "any")
        parts = []
        if quality == "best":
            parts.append("for=best")
        elif quality == "worst":
            parts.append("for=worst")
        elif quality:
            parts.append(f"res={quality}")
            parts.append("for=best")
        if codec and codec != "any":
            parts.append(f"codecs={codec}")
        if video_range and video_range != "any":
            parts.append(f"range={video_range}")
        options.select_video = ":".join(parts) if parts else "best"

        audio_parts = []
        langs = str(settings.get("audio_langs", "") or "").strip()
        if langs:
            audio_parts.append(f"lang={langs}")
        audio_codec = settings.get("audio_codec", "any")
        if audio_codec and audio_codec != "any":
            audio_parts.append(f"codecs={audio_codec}")
        channels = settings.get("audio_channels", "any")
        if channels and channels != "any":
            audio_parts.append(f"channels={channels}")
        audio_parts.append("for=all" if langs else "for=best")
        options.select_audio = ":".join(audio_parts)

        sub_langs = str(settings.get("sub_langs", "") or "").strip()
        if sub_langs and sub_langs != "none":
            options.select_subtitle = "for=all" if sub_langs == "all" else f"lang={sub_langs}:for=all"

        chosen = select_streams(list(streams), options)
        if (
            strict
            and any(stream.media_type == "video" for stream in streams)
            and not any(stream.media_type == "video" for stream in chosen)
        ):
            return []
        if not chosen and streams:
            best_video = next((s for s in streams if s.media_type == "video"), None)
            best_audio = next((s for s in streams if s.media_type == "audio"), None)
            chosen = [s for s in (best_video, best_audio) if s is not None] or [streams[0]]
        return chosen

    @staticmethod
    def _normalize_audio_only_streams(
        streams: Sequence[StreamInfo], *, audio_only: bool
    ) -> list[StreamInfo]:
        """Repair direct audio misclassified as video by a media probe.

        Some MP3 files carry an attached cover image. ffprobe reports that image
        as a video stream, and a direct parser that prefers video then leaves an
        audio-only playback with no selectable audio track. The playback's title
        kind is authoritative here; direct streams and a single HLS media
        playlist are eligible, even when the signed URL has no useful extension.
        """
        selected = list(streams)
        if not audio_only or any(stream.media_type == "audio" for stream in selected):
            return selected
        audio_extensions = {"aac", "ac3", "eac3", "flac", "m4a", "m4b", "mp3", "ogg", "opus", "wav"}
        audio_codecs = ("aac", "ac-3", "e-ac-3", "eac3", "flac", "mp3", "mp4a", "opus", "vorbis")
        for stream in selected:
            if stream.media_type != "video" or stream.manifest_type not in {"direct", "hls"}:
                continue
            extension = str(stream.extension or "").lower().lstrip(".")
            if not extension:
                path = urlsplit(str(stream.url or stream.original_url or "")).path
                extension = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            codec = str(stream.codecs or "").lower()
            if extension in audio_extensions or codec.startswith(audio_codecs) or len(selected) == 1:
                stream.media_type = "audio"
        return selected

    def _select_audio_only(
        self, streams: Sequence[StreamInfo], settings: Settings
    ) -> list[StreamInfo]:
        """Best audio, and nothing else.

        Subtitles are left out too: a radio programme rarely has any, and where
        one exists it cannot be muxed into an MP3, so selecting it would only
        produce a stray file and a confusing track list.
        """
        options = SelectionOptions()
        parts = []
        langs = str(settings.get("audio_langs", "") or "").strip()
        if langs:
            parts.append(f"lang={langs}")
        codec = settings.get("audio_codec", "any")
        if codec and codec != "any":
            parts.append(f"codecs={codec}")
        parts.append("for=all" if langs else "for=best")
        options.select_audio = ":".join(parts)

        chosen = [s for s in select_streams(list(streams), options) if s.media_type == "audio"]
        if chosen:
            return chosen
        # the filters matched nothing; take whatever audio exists rather than
        # failing a download over a codec preference
        fallback = next((s for s in streams if s.media_type == "audio"), None)
        return [fallback] if fallback is not None else []

    # ------------------------------------------------------------------ audio
    def audio_format_for(
        self,
        playback: Playback,
        settings: Settings,
        tracks: TrackSet | None = None,
    ) -> str | None:
        """The container to re-encode audio into, or None to keep the original.

        Only meaningful for an audio-only playback. A value the native engine cannot
        produce is treated as "keep the original" rather than passed through to
        fail at the last moment.
        """
        if not playback.audio_only:
            return None
        wanted = str(settings.get("audio_format", "mp3") or "").lower()
        if wanted == "auto":
            wanted = self._automatic_audio_format(playback, tracks)
        if wanted in ("", "source", "none"):
            return None
        supported = self.downloader.audio_formats()
        if supported and wanted not in supported:
            self.log(f"UniDL cannot produce {wanted}; keeping the original audio")
            return None
        return wanted

    @staticmethod
    def _automatic_audio_format(
        playback: Playback,
        tracks: TrackSet | None,
    ) -> str:
        """Keep lossless and special audio codecs intact in auto mode.

        AAC and MP3 retain the historical MP3 export. ALAC and FLAC get common
        lossless containers with their audio copied unchanged; Atmos/E-AC-3 and
        unknown codecs stay in their source container so spatial information is
        not discarded. The service's explicit codec hint is authoritative when a
        direct source has not populated parsed stream metadata yet.
        """
        declared_codec = str(playback.audio_codec_hint or "").strip().casefold()
        if declared_codec.startswith("alac"):
            return "alac"
        if declared_codec.startswith("flac"):
            return "flac"
        if declared_codec.startswith(("ec-3", "eac3", "ac-3", "ac3")):
            return "m4a"
        ordinary_prefixes = ("mp4a", "aac", "mp3", "mpa")
        if declared_codec:
            return "mp3" if declared_codec.startswith(ordinary_prefixes) else "source"
        selected = list(getattr(tracks, "selected", ()) or ())
        if not selected:
            selected = list(getattr(tracks, "streams", ()) or ())
        if not selected:
            return "mp3"
        for stream in selected:
            codec = Engine._audio_codec_hint(stream)
            if codec.startswith("alac"):
                return "alac"
            if codec.startswith("flac"):
                return "flac"
            if codec.startswith(("ec-3", "eac3", "ac-3", "ac3")):
                return "m4a"
            if not codec or not codec.startswith(ordinary_prefixes):
                return "source"
        return "mp3"

    @staticmethod
    def _audio_codec_hint(stream) -> str:
        codec = str(getattr(stream, "codecs", "") or "").strip().casefold()
        if codec:
            return codec
        hints = " ".join(
            str(getattr(stream, field, "") or "").casefold()
            for field in ("name", "id", "group_id", "url", "original_url")
        )
        for marker in ("alac", "flac", "ec-3", "eac3", "ac-3", "mp4a", "aac", "mp3", "mpa"):
            if marker in hints:
                return marker
        return ""

    def write_audio_sidecar(
        self,
        playback: Playback,
        *,
        include_lyrics: bool = True,
    ) -> Path | None:
        """Write the ID3 tags UniDL reads with ``--audio-metadata-file``.

        A real file rather than flags, because that is the interface UniDL
        offers, and because an exported command has to still work when it is
        re-run later - which it would not if the tags only existed in memory.
        """
        tags = dict(playback.audio_tags)
        if include_lyrics and playback.lyrics is not None and playback.lyrics.plain_text:
            tags["lyrics"] = playback.lyrics.plain_text
        if not tags:
            return None
        directory = self.config.paths.temp / "audio-tags"
        private_directory(directory)
        path = directory / f"{playback.save_name}.json"
        try:
            atomic_write_text(
                path,
                json.dumps(tags, ensure_ascii=False, indent=2) + "\n",
            )
        except OSError as exc:
            self.log(f"could not write audio tags: {exc}")
            return None
        return path

    def write_chapters_sidecar(
        self,
        playback: Playback,
        tracks: TrackSet | None = None,
    ) -> Path | None:
        """Persist service chapter metadata for command export and native muxing.

        The content digest is part of the name so a later title with the same
        release name cannot silently rewrite the chapters referenced by an older
        saved command.  The downloader reads this neutral JSON and generates the
        muxer-specific metadata file only while muxing.
        """
        if not playback.chapters:
            return None
        # A forward live pipe is assembled continuously and cannot be rewritten
        # with a static chapter table. Replay windows explicitly taken as VOD are
        # finite, so they may still use the normal container-metadata path.
        if playback.is_live and (
            playback.live_window is None or playback.live_window.mode != "vod"
        ):
            return None
        def positive_duration(value: object) -> float | None:
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                return None
            return seconds if math.isfinite(seconds) and seconds > 0 else None

        duration_seconds: list[float] = []
        title_duration = positive_duration(getattr(playback.title, "duration", None))
        if title_duration is not None:
            duration_seconds.append(title_duration)
        if tracks is not None:
            for stream in tracks.selected:
                duration = positive_duration(
                    getattr(stream, "total_duration", None)
                    or getattr(stream, "duration", 0)
                    or 0
                )
                if duration is not None:
                    duration_seconds.append(duration)
        duration_ms = round(max(duration_seconds) * 1000) if duration_seconds else None
        chapters = normalize_chapters(playback.chapters, duration_ms=duration_ms)
        playback.chapters = list(chapters)
        document = {
            "kind": "unidl-chapters",
            "version": 1,
            "chapters": [chapter.as_document() for chapter in chapters],
        }
        payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        directory = self.config.paths.temp / "chapters"
        private_directory(directory)
        path = directory / f"{playback.save_name}.{digest}.json"
        try:
            atomic_write_text(path, payload)
        except OSError as exc:
            self.log(f"could not write chapters: {exc}")
            return None
        return path

    @staticmethod
    def embed_chapters(settings: Settings | dict[str, object]) -> bool:
        """Whether this delivery should pass chapter metadata to the muxer.

        The switch is a shared *service Track/output setting*, so every service
        gets the same wording and default while retaining an independent value.
        Older/headless settings objects do not declare it; those keep the safe
        historical default of embedding chapters.
        """
        value = settings.get("embed_chapters", True)
        if isinstance(value, str):
            return value.strip().casefold() not in {"", "0", "false", "no", "off"}
        return bool(value)

    @staticmethod
    def embed_lyrics(settings: Settings | dict[str, object]) -> bool:
        value = settings.get("embed_lyrics", True)
        if isinstance(value, str):
            return value.strip().casefold() not in {"", "0", "false", "no", "off"}
        return bool(value)

    # --------------------------------------------------------------- download
    def save_dir(self, settings: Settings | None = None, service: str = "") -> Path:
        """Where files land: the root, then a folder for the service.

        One answer for downloads and live recordings both, because they are the same
        question and answering it in two places is how they end up in two folders.
        The setting wins over the configured path so it can be changed without
        editing a file, and an empty setting means "whatever unidl.yaml says" rather
        than "nowhere".

        The per-service folder is not optional. A flat folder of a few thousand
        files from a hundred services is not somewhere you find anything, and the
        service is the one thing every file in it has in common.
        """
        chosen = str((settings.get("download_dir") if settings else "") or "").strip()
        root = Path(chosen).expanduser() if chosen else self.config.paths.downloads
        name = _folder_name(service)
        return root / name if name else root

    @staticmethod
    def delivery_source(playback: Playback) -> DeliverySource:
        """Represent a service playback without a downloader-specific temp file."""
        if playback.inline_manifest:
            text = str(playback.inline_manifest)
            if text.lstrip().startswith("#EXTM3U"):
                return DeliverySource.from_hls(text.rstrip() + "\n")
            return DeliverySource.from_dash(text)
        if playback.manifest_url:
            manifest = str(playback.manifest_url)
            if manifest.lstrip("\ufeff\r\n\t ").upper().startswith("#EXTM3U"):
                return DeliverySource.from_hls(manifest.rstrip() + "\n")
            return DeliverySource.from_reference(manifest)
        if playback.json_manifest is not None:
            return DeliverySource.from_json(playback.json_manifest)
        raise ValueError("Playback did not provide a delivery source")

    def delivery_plan(
        self,
        playback: Playback,
        settings: Settings,
        tracks: TrackSet,
    ) -> DeliveryPlan:
        """Build the Core contract consumed by UniDL's native downloader."""
        if not tracks.selected:
            raise ValueError("a delivery plan needs at least one selected track")
        overrides = _delivery_overrides(playback.extra_args)
        debug = bool(settings.get("debug", False))
        download_proxy = (
            playback.proxy if settings.get("proxy_downloads", True) else None
        )
        direct_audio_no_probe = (
            playback.audio_only
            and playback.drm is None
            and playback.json_manifest is None
            and bool(playback.manifest_url)
            and Path(urlsplit(str(playback.manifest_url)).path).suffix.lower()
            in _DIRECT_AUDIO_EXTENSIONS
        )
        source = (
            tracks.manifest.request.source
            if tracks.manifest is not None
            else self.delivery_source(playback)
        )
        request = ParseRequest(
            source,
            ParsePolicy(
                headers=dict(playback.headers),
                proxy=download_proxy,
                details=bool(settings.get("hls_details", False)),
                no_probe=direct_audio_no_probe,
                append_url_params=overrides.append_url_params,
                ad_keywords=tuple(overrides.ad_keywords),
                drop_video=normalize_drop_video_pattern(
                    settings.get("drop_video", "")
                ),
            ),
            scratch_dir=self.config.paths.temp / "manifests",
        )
        if tracks.manifest is None:
            manifest = self.downloader.adopt(request, tracks.streams)
        else:
            bound = self.downloader.streams(tracks.manifest)
            if len(bound) != len(tracks.streams) or any(
                left is not right
                for left, right in zip(bound, tracks.streams, strict=True)
            ):
                raise ValueError("parsed manifest does not match the track picker")
            manifest = ParsedManifest(
                request,
                tracks.manifest.tracks,
                tracks.manifest.backend_state,
            )
        track_ids = {
            id(stream): track.track_id
            for track, stream in zip(
                manifest.tracks,
                self.downloader.streams(manifest),
                strict=True,
            )
        }
        selected_track_ids = tuple(
            track_ids[id(stream)]
            for stream in tracks.selected
            if id(stream) in track_ids
        )
        if len(selected_track_ids) != len(tracks.selected):
            raise ValueError("selected track is not part of the parsed manifest")

        audio_format = self.audio_format_for(playback, settings, tracks)
        audio_sidecar = (
            self.write_audio_sidecar(
                playback,
                include_lyrics=self.embed_lyrics(settings),
            )
            if audio_format
            else None
        )
        chapters_sidecar = (
            self.write_chapters_sidecar(playback, tracks)
            if self.embed_chapters(settings)
            else None
        )
        mux_format = (
            None
            if playback.audio_only
            else str(settings.get("mux_format", "mkv"))
        )
        drm = playback.drm
        live_real_time_merge = bool(settings.get("live_real_time_merge", True))
        live_keep_segments = bool(settings.get("live_keep_segments", False))
        live_pipe_mux = bool(settings.get("live_pipe_mux", True)) and not bool(audio_format)
        if overrides.live_real_time_merge is not None:
            live_real_time_merge = overrides.live_real_time_merge
        if overrides.live_keep_segments is not None:
            live_keep_segments = overrides.live_keep_segments
        if overrides.live_pipe_mux is not None:
            live_pipe_mux = overrides.live_pipe_mux

        live_perform_as_vod = overrides.live_perform_as_vod
        live_dvr_from_start: bool | None = None
        live_dvr_start_at: str | None = None
        live_dvr_end_at: str | None = None
        window = playback.live_window
        if window is not None and window.mode == "vod":
            live_perform_as_vod = True
        elif window is not None and window.mode == "start":
            live_dvr_from_start = True
        elif window is not None and window.mode == "offset" and window.start_at:
            live_dvr_start_at = window.start_at
            live_dvr_end_at = window.end_at or None
        live_record_limit: str | None = None
        if playback.is_live and (window is None or window.records_forward):
            value = normalize_live_record_limit(
                playback.live_record_limit
                or settings.get("live_record_limit", "")
                or ""
            )
            live_record_limit = value or None

        muxer = str(settings.get("muxer", "auto") or "auto")
        if muxer not in {"auto", "ffmpeg", "mkvmerge"}:
            muxer = "auto"
        downloader = str(settings.get("segment_downloader", "python") or "python")
        if downloader not in {"python", "aria2c", "auto"}:
            downloader = "python"
        max_speed = str(settings.get("max_speed", "") or "").strip() or None
        policy = DownloadPolicy(
            workers=int(settings.get("workers", 16) or 16),
            retries=int(settings.get("retries", 5) or 5),
            concurrent_tracks=bool(settings.get("concurrent_tracks", True)),
            max_speed=max_speed,
            http_timeout=max(1, int(settings.get("http_timeout", 30) or 30)),
            check_segments_count=bool(settings.get("check_segments_count", True)),
            resume=bool(settings.get("resume_parts", True)),
            downloader=downloader,
            mux_format=mux_format,
            muxer=muxer,
            mux_imports=tuple(
                f"path={track.path}:lang={track.language}:name={track.name or track.language}"
                for track in playback.mux_imports
            ),
            chapters_file=chapters_sidecar,
            subtitle_format=str(settings.get("sub_format", "srt")),
            auto_subtitle_fix=bool(settings.get("auto_subtitle_fix", True)),
            audio_format=audio_format,
            audio_metadata_file=audio_sidecar,
            decode_audio_vivid=overrides.decode_audio_vivid,
            audio_vivid_decoder=overrides.audio_vivid_decoder,
            audio_vivid_decoder_args=overrides.audio_vivid_decoder_args,
            hls_method=(
                drm.hls_method or ("AES_128" if drm.hls_key else None)
                if drm is not None
                else None
            ),
            hls_key=drm.hls_key if drm is not None else None,
            hls_iv=drm.hls_iv if drm is not None else None,
            hls_decryptor=drm.hls_decryptor if drm is not None else None,
            custom_range=overrides.custom_range,
            allow_hls_multi_ext_map=overrides.allow_hls_multi_ext_map,
            vgc=overrides.vgc,
            vgc_keep_opaque=overrides.vgc_keep_opaque,
            temp_dir=self.config.paths.temp,
            log_file=(
                self.config.paths.logs / f"{playback.save_name}.log"
                if debug
                else None
            ),
            write_meta_json=debug,
            keep_temp=debug or bool(settings.get("keep_temp", False)),
            keep_after_done=debug or not bool(settings.get("delete_temp_after_done", True)),
            no_color=self.frame is None,
            live=LivePolicy(
                enabled=playback.is_live,
                record_limit=live_record_limit,
                real_time_merge=live_real_time_merge if playback.is_live else None,
                keep_segments=live_keep_segments if playback.is_live else None,
                pipe_mux=live_pipe_mux if playback.is_live else None,
                perform_as_vod=live_perform_as_vod,
                dvr_from_start=live_dvr_from_start,
                dvr_start_at=live_dvr_start_at,
                dvr_end_at=live_dvr_end_at,
            ),
        )
        return DeliveryPlan(
            manifest=manifest,
            selected_track_ids=selected_track_ids,
            save_name=playback.save_name,
            output_dir=self.save_dir(settings, playback.title.service),
            keys=tuple(playback.keys),
            service_context=dict(playback.drm.context) if playback.drm is not None else {},
            policy=policy,
        )

    def download_options(self, playback: Playback, settings: Settings):
        """Legacy diagnostic options; production execution uses ``DeliveryPlan``."""
        drm = playback.drm
        debug = bool(settings.get("debug", False))
        # The one place a proxy can be opted out of without giving it up entirely.
        # Off, UniDL goes direct - and it fetches the manifest again itself, so this
        # is for a slow proxy in front of an open CDN, not for a geofenced manifest.
        # Everything above stays on the proxy: the manifest this engine reads for DRM
        # init data, the playlists, and the service's licence request.
        download_proxy = playback.proxy if settings.get("proxy_downloads", True) else None
        direct_audio_no_probe = (
            playback.audio_only
            and playback.drm is None
            and playback.json_manifest is None
            and bool(playback.manifest_url)
            and Path(urlsplit(str(playback.manifest_url)).path).suffix.lower() in _DIRECT_AUDIO_EXTENSIONS
        )
        # Audio-only output is already a single final file after UniDL's
        # transcode/tagging pass. Passing the app-wide MKV default would make
        # UniDL mux that MP3 again and discard the audio-only output contract.
        mux_format = None if playback.audio_only else str(settings.get("mux_format", "mkv"))
        muxer = str(settings.get("muxer", "auto") or "auto")
        if muxer not in {"auto", "ffmpeg", "mkvmerge"}:
            muxer = "auto"
        downloader = str(settings.get("segment_downloader", "python") or "python")
        if downloader not in {"python", "aria2c", "auto"}:
            downloader = "python"
        options = api.DownloadOptions(
            input=self.playback_input(playback),
            headers=dict(playback.headers),
            proxy=download_proxy,
            save_name=playback.save_name,
            save_dir=str(self.save_dir(settings, playback.title.service)),
            keys=list(playback.keys),
            service_context=dict(drm.context) if drm is not None else {},
            workers=int(settings.get("workers", 16) or 16),
            retries=int(settings.get("retries", 5) or 5),
            concurrent_tracks=bool(settings.get("concurrent_tracks", True)),
            max_speed=str(settings.get("max_speed", "") or "").strip() or None,
            # A direct MP3 with an attached picture is reported as video by
            # ffprobe. The service already declared this source audio-only, so
            # exported commands must keep the extension/content-type path too.
            no_probe=direct_audio_no_probe,
            mux_format=mux_format,
            muxer=muxer,
            sub_format=str(settings.get("sub_format", "srt")),
            auto_subtitle_fix=bool(settings.get("auto_subtitle_fix", True)),
            http_request_timeout=max(1, int(settings.get("http_timeout", 30) or 30)),
            check_segments_count=bool(settings.get("check_segments_count", True)),
            no_resume=not bool(settings.get("resume_parts", True)),
            downloader=downloader,
            tmp_dir=str(self.config.paths.temp),
            is_live=playback.is_live,
            extra_args=list(playback.extra_args),
            # UniDL turns its own progress rendering off when stdout is not a
            # tty, and in-process it never is. Asked to render anyway, it draws the
            # display it draws on a terminal - which is the point: a download
            # should look the same here as it does on its own. Only when there is
            # somewhere to paint it; a headless run wants the plain lines.
            no_color=self.frame is None,
            drop_video=normalize_drop_video_pattern(settings.get("drop_video", "")),
            # debug mode keeps the evidence around instead of cleaning up
            write_meta_json=debug,
            keep_temp=debug or bool(settings.get("keep_temp", False)),
            no_del_after_done=debug or not bool(settings.get("delete_temp_after_done", True)),
            log_file_path=str(self.config.paths.logs / f"{playback.save_name}.log") if debug else None,
        )
        if drm is not None:
            options.custom_hls_key = drm.hls_key
            options.custom_hls_iv = drm.hls_iv
            options.custom_hls_method = drm.hls_method or ("AES_128" if drm.hls_key else None)
            options.hls_decryptor = drm.hls_decryptor
        options.mux_imports = [
            f"path={track.path}:lang={track.language}:name={track.name or track.language}"
            for track in playback.mux_imports
        ]
        chapters_sidecar = (
            self.write_chapters_sidecar(playback)
            if self.embed_chapters(settings)
            else None
        )
        if chapters_sidecar is not None:
            options.chapters_file = str(chapters_sidecar)
        audio_format = self.audio_format_for(playback, settings)
        if audio_format:
            options.audio_format = audio_format
            sidecar = self.write_audio_sidecar(
                playback,
                include_lyrics=self.embed_lyrics(settings),
            )
            if sidecar is not None:
                options.audio_metadata_file = str(sidecar)

        if playback.is_live:
            options.live_real_time_merge = bool(settings.get("live_real_time_merge", True))
            options.live_keep_segments = bool(settings.get("live_keep_segments", False))
            # UniDL rejects --audio-format together with --live-pipe-mux, and
            # piping is pointless for a single audio track anyway
            options.live_pipe_mux = bool(settings.get("live_pipe_mux", True)) and not audio_format
            window = playback.live_window
            if window is not None and window.mode == "vod":
                # the window as it stands: this finishes on its own, so a recording
                # limit would only cut it short
                options.live_perform_as_vod = True
            elif window is not None and window.mode == "start":
                options.live_dvr_from_start = True
            elif window is not None and window.mode == "offset" and window.start_at:
                options.live_dvr_start_at = window.start_at
                if window.end_at:
                    options.live_dvr_end_at = window.end_at
            if window is None or window.records_forward:
                # A live stream has no end. Without this the recording runs until
                # something stops it, which on an unattended run is the disk.
                limit = normalize_live_record_limit(
                    playback.live_record_limit
                    or settings.get("live_record_limit", "")
                    or ""
                )
                if limit:
                    options.live_record_limit = limit
        return options

    #: Below this, a live manifest is only holding the segments a player needs to
    #: start - a few seconds of buffer, not something to choose a position in. BBC
    #: radio publishes 32 seconds; a broadcaster's replay window is measured in
    #: hours. Two minutes is well clear of the first and well under the second.
    LIVE_WINDOW_FLOOR = 120.0

    def live_window_seconds(self, tracks: TrackSet) -> float:
        """How far back a parsed ladder says it can be rewound, in seconds.

        The length of a live playlist *is* the replay window - what the server is
        still publishing. 0 means either that there is nothing behind the live edge
        or that this parse did not look; :meth:`measure_live_window` is the one that
        makes sure.
        """
        spans = [
            float(stream.duration or 0.0)
            for stream in tracks.streams
            if getattr(stream, "is_live", False)
        ]
        return max(spans) if spans else 0.0

    def measure_live_window(
        self,
        playback: Playback,
        settings: Settings,
        tracks: TrackSet | None = None,
    ) -> float:
        """Find out how much replay this live stream is offering.

        This needs its own parse, with details on. The normal one stops at the
        master playlist - everything the track picker needs is described there - and
        without the child playlists nothing knows how long the live window is, or
        even that the stream is live: ``is_live`` comes back False and ``duration``
        empty. Measuring costs one request per rendition, so it is done only when
        the answer is about to be used, and the ladder already in hand is trusted
        when it happens to know.
        """
        if tracks is not None:
            known = self.live_window_seconds(tracks)
            if known:
                return known
        try:
            manifest = self._parse_manifest(
                self.parse_request(playback, settings, details=True)
            )
            streams = self.downloader.streams(manifest)
        except Exception as exc:  # noqa: BLE001 - no window is a fine answer here
            self.log(f"could not measure the replay window: {exc}")
            return 0.0
        return self.live_window_seconds(TrackSet(streams=streams))

    def has_replay_window(self, tracks: TrackSet) -> bool:
        return self.live_window_seconds(tracks) >= self.LIVE_WINDOW_FLOOR

    def command_for(
        self,
        playback: Playback,
        settings: Settings,
        tracks: TrackSet | None = None,
    ) -> str:
        if tracks and tracks.selected:
            plan = self.delivery_plan(playback, settings, tracks)
            return self.downloader.command_line(plan)
        return api.command_line(self.download_options(playback, settings))

    def export_command(
        self,
        playback: Playback,
        settings: Settings,
        tracks: TrackSet | None = None,
        *,
        service_id: str = "",
    ) -> Path:
        """Write the command to disk, mirroring the old ``download_commands/`` habit."""
        from datetime import datetime

        command = self.command_for(playback, settings, tracks)
        service = _folder_name(service_id or playback.title.service or "unknown")
        directory = self.config.paths.commands / service
        private_directory(directory)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = directory / f"{playback.save_name}_{stamp}.txt"

        # A saved command is intended to be executable as a shell script. Keep
        # the useful resolution record below it, but make every annotation a
        # comment instead of leaving shell-invalid display text after command 1.
        lines = [command, ""]

        def note(value: str = "") -> None:
            lines.append(f"# {value}" if value else "#")

        if playback.keys:
            note("keys:")
            lines += [f"#   {key}" for key in playback.keys]
            note()
        if tracks:
            note(f"tracks: {tracks.summary()}")
            lines += [f"#   [{'x' if s in tracks.selected else ' '}] {s.format_line()}" for s in tracks.streams]
        if playback.subtitle_references:
            note()
            note("all available subtitle URLs:")
            current_kind = ""
            for subtitle in playback.subtitle_references:
                kind = subtitle.kind or "normal"
                if kind != current_kind:
                    current_kind = kind
                    note(f"  [{kind.upper()}]")
                flags = [value for value, enabled in (("Original", subtitle.original), ("Selected", subtitle.selected)) if enabled]
                suffix = f" ({', '.join(flags)})" if flags else ""
                label = subtitle.language or "und"
                if subtitle.name and subtitle.name.casefold() != label.casefold():
                    label += f" · {subtitle.name}"
                note(f"  {label}{suffix}: {subtitle.url}")
        if playback.chapters:
            note()
            note("chapters:")
            for index, chapter in enumerate(playback.chapters, start=1):
                span = chapter_timestamp(chapter.start_ms)
                if chapter.end_ms is not None:
                    span += f"-{chapter_timestamp(chapter.end_ms)}"
                kind = f" [{chapter.kind}]" if chapter.kind else ""
                note(f"  {index:02d} {span}{kind} {chapter.title}")
        atomic_write_text(path, "\n".join(lines) + "\n")
        return path

    def export_document(
        self,
        playback: Playback,
        tracks: TrackSet | None = None,
        *,
        service_id: str = "",
        service_name: str = "",
        path: Path | None = None,
    ) -> Path:
        """Write, or extend, the portable export for this run. Returns its path.

        One file per run, like the command card is one card per run: a season
        picked at once is one thing that was resolved, and ten files holding one
        episode each is ten things to keep together by hand. ``path`` is what the
        caller was given last time, and passing it back is how the second title
        lands in the same file.

        The file is rewritten whole rather than appended to, because it is a JSON
        document and a half-written one is not readable at all.
        """
        service = _folder_name(service_id or playback.title.service or "unknown")
        def new_document() -> exports.Document:
            from .. import __version__

            return exports.Document(
                service=service,
                service_name=service_name or service,
                app=f"unidl {__version__}",
            )

        if path is not None:
            target = Path(path)
            with locked_path(target):
                document = None
                if target.is_file():
                    try:
                        document = exports.read(target)
                    except exports.ExportError as exc:
                        # A file this build cannot read must not be silently replaced - it
                        # may be somebody's only copy of a licence already spent.
                        raise CdmError(
                            f"the export file for this run cannot be read: {exc}"
                        ) from exc
                document = document or new_document()
                document.add(exports.entry_for(playback, tracks))
                return exports.write(target, document)

        document = new_document()
        document.add(exports.entry_for(playback, tracks))
        target = self.config.paths.exports / service / exports.file_name(document)
        with locked_path(target):
            return exports.write(target, document)

    def run(
        self,
        playback: Playback,
        settings: Settings,
        tracks: TrackSet | None = None,
        cancel: threading.Event | None = None,
        pause: threading.Event | None = None,
        *,
        service=None,
    ) -> DownloadResult:
        """Execute the native download through structured host callbacks.

        ``cancel`` is passed as an execution hook. Native download and
        live-refresh loops check it independently of terminal rendering, and
        ordinary messages no longer require process-wide stdout/stderr capture.
        ``pause`` holds workers between segments without ending the job; the
        same run continues when it is cleared.
        """
        command = self.command_for(playback, settings, tracks)
        where = self.save_dir(settings, playback.title.service)
        where.mkdir(parents=True, exist_ok=True)

        if cancel is not None and cancel.is_set():
            # asked to stop before it even started, which happens to the rest of
            # a batch when one is cancelled
            return DownloadResult(CANCELLED_EXIT, where, command)

        progress_screen: _StructuredProgressScreen | None = None
        try:
            if tracks is None:
                tracks = self.load_tracks(playback, settings, service=service)
            plan = self.delivery_plan(playback, settings, tracks)
            command = self.downloader.command_line(plan)
            progress_screen = (
                _StructuredProgressScreen(
                    self.frame,
                    lambda: self.frame_columns,
                    plan.manifest.select(plan.selected_track_ids),
                )
                if self.frame is not None
                else None
            )
            if playback.is_live:
                provider = self.live_key_provider(playback, service, settings)

                def supply_live_key(request: CoreLiveKeyRequest) -> str:
                    return provider(
                        request.kid,
                        request.track or object(),
                        request.segment,
                        request.reason,
                    )
            else:
                def supply_live_key(_request: CoreLiveKeyRequest) -> None:
                    return None

            def publish_event(event) -> None:
                if isinstance(event, TrackProgressEvent):
                    if progress_screen is not None:
                        progress_screen.update(event)
                    return
                if isinstance(event, MessageEvent):
                    clean = _plain_terminal_text(event.message)
                    lines = clean.splitlines() or [""]
                    if event.transient and progress_screen is not None:
                        # Keep post-processing state under the transfer rows.
                        # Alternating two differently-sized frames was visible
                        # as a flash whenever another progress event arrived.
                        progress_screen.status(lines)
                        return
                    for line in lines:
                        self.log(line)
                    return
                if isinstance(event, (StageEvent, ArtifactEvent)):
                    return

            delivery_hooks = DeliveryHooks(
                cancellation=CancellationToken(
                    cancel.is_set if cancel is not None else None
                ),
                pause=PauseToken(pause.is_set if pause is not None else None),
                emit=publish_event,
                request_live_key=supply_live_key,
                display_width=lambda: self.frame_columns,
            )
            delivery = self.downloader.run(plan, delivery_hooks)
            if delivery.status is DeliveryStatus.SUCCEEDED:
                exit_code = 0
            elif delivery.status is DeliveryStatus.CANCELLED:
                self.log("cancelled")
                exit_code = CANCELLED_EXIT
            else:
                exit_code = delivery.exit_code or 1
                if delivery.failure is not None:
                    self.log(f"error: {delivery.failure.message}")
            artifacts = tuple(artifact.path for artifact in delivery.artifacts)
            failure = delivery.failure.message if delivery.failure is not None else ""
        except KeyboardInterrupt:
            self.log("cancelled")
            exit_code = CANCELLED_EXIT
            artifacts = ()
            failure = "cancelled"
        except Exception as exc:
            self.log(f"error: {exc}")
            exit_code = 1
            artifacts = ()
            failure = str(exc)
        finally:
            if progress_screen is not None:
                progress_screen.finish()
            if debug_log := (
                self.config.paths.logs / f"{playback.save_name}.log"
                if settings.get("debug", False)
                else None
            ):
                private_file(debug_log)
        return DownloadResult(
            exit_code=exit_code,
            output_dir=where,
            command=command,
            artifacts=artifacts,
            failure=failure,
        )

def _hls_variants(text: str, base_url: str, *, append_query: bool = False) -> list[str]:
    """The variant playlist URLs a master lists, in the order it lists them."""
    urls: list[str] = []
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        for candidate in lines[index + 1 :]:
            if not candidate or candidate.startswith("#"):
                continue
            url = _hls_url(base_url, candidate, append_query=append_query)
            if url not in urls:
                urls.append(url)
            break
    return urls


def _hls_url(base_url: str, candidate: str, *, append_query: bool = False) -> str:
    """Resolve one HLS child, optionally carrying an unsigned child query forward."""
    joined = urljoin(base_url, candidate)
    child = urlsplit(joined)
    parent_query = urlsplit(base_url).query
    if not append_query or child.query or not parent_query:
        return joined
    return urlunsplit((child.scheme, child.netloc, child.path, parent_query, child.fragment))


def _looks_like_hls(url: str) -> str | bool:
    """Whether a manifest URL is an HLS playlist rather than DASH.

    Judged by the extension and the path, because that is all there is before
    fetching it. Wrong in the safe direction: a DASH manifest misread as HLS costs
    one skipped lookup, while an HLS playlist misread as DASH costs one wasted
    fetch that finds nothing.
    """
    lowered = (url or "").split("?", 1)[0].lower()
    return (
        lowered.endswith((".m3u8", ".m3u"))
        or "/hls/" in lowered
        or "/m3u8" in lowered
    )
