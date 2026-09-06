"""Typed boundary between UniDL orchestration and a download implementation.

The contract deliberately contains no account, CDM or licence transport types.
Services resolve those concerns before constructing a :class:`DeliveryPlan`;
the backend receives a source, selected tracks and raw keys only.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, TypeAlias, runtime_checkable


class SourceKind(str, Enum):
    REFERENCE = "reference"
    INLINE_HLS = "inline_hls"
    INLINE_DASH = "inline_dash"
    JSON_MANIFEST = "json_manifest"


@dataclass(frozen=True, slots=True)
class DeliverySource:
    """Exactly one source representation accepted by the downloader."""

    kind: SourceKind
    reference: str | None = None
    inline_text: str | None = None
    json_document: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        supplied = sum(
            value is not None
            for value in (self.reference, self.inline_text, self.json_document)
        )
        if supplied != 1:
            raise ValueError("a delivery source must contain exactly one input")
        expected = {
            SourceKind.REFERENCE: self.reference,
            SourceKind.INLINE_HLS: self.inline_text,
            SourceKind.INLINE_DASH: self.inline_text,
            SourceKind.JSON_MANIFEST: self.json_document,
        }[self.kind]
        if expected is None:
            raise ValueError(f"{self.kind.value} does not match its source value")

    @classmethod
    def from_reference(cls, value: str) -> DeliverySource:
        if not str(value or "").strip():
            raise ValueError("a manifest reference cannot be empty")
        return cls(SourceKind.REFERENCE, reference=str(value))

    @classmethod
    def from_hls(cls, text: str) -> DeliverySource:
        if not str(text or "").lstrip().startswith("#EXTM3U"):
            raise ValueError("inline HLS must start with #EXTM3U")
        return cls(SourceKind.INLINE_HLS, inline_text=str(text))

    @classmethod
    def from_dash(cls, text: str) -> DeliverySource:
        if "<MPD" not in str(text or "")[:2048] and ":MPD" not in str(text or "")[:2048]:
            raise ValueError("inline DASH must contain an MPD document")
        return cls(SourceKind.INLINE_DASH, inline_text=str(text))

    @classmethod
    def from_json(cls, document: Mapping[str, Any]) -> DeliverySource:
        return cls(SourceKind.JSON_MANIFEST, json_document=dict(document))


@dataclass(frozen=True, slots=True)
class ParsePolicy:
    headers: Mapping[str, str] = field(default_factory=dict)
    proxy: str | None = None
    use_system_proxy: bool = True
    details: bool = False
    no_child_playlists: bool = False
    no_probe: bool = False
    base_url: str | None = None
    append_url_params: bool = False
    ad_keywords: tuple[str, ...] = ()
    drop_video: str | None = None
    drop_audio: str | None = None
    drop_subtitle: str | None = None


@dataclass(frozen=True, slots=True)
class ParseRequest:
    source: DeliverySource
    policy: ParsePolicy = field(default_factory=ParsePolicy)
    scratch_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class TrackDescriptor:
    """Stable Core view of a backend track.

    ``track_id`` is the selection identity.  No caller needs object identity or
    a CLI row number, and a backend can keep its richer native object opaque.
    """

    track_id: str
    manifest_type: str
    media_type: str
    source_id: str = ""
    group_id: str = ""
    url: str = ""
    original_url: str = ""
    name: str = ""
    language: str = ""
    role: str = ""
    bandwidth: int | None = None
    codecs: str = ""
    resolution: str = ""
    frame_rate: float | None = None
    channels: str = ""
    extension: str = ""
    video_range: str = ""
    duration: float | None = None
    size_bytes: int | None = None
    encrypted: bool = False
    encryption_scheme: str = ""
    is_live: bool = False
    key_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedManifest:
    request: ParseRequest
    tracks: tuple[TrackDescriptor, ...]
    backend_state: object = field(repr=False, compare=False)

    def select(self, track_ids: tuple[str, ...]) -> tuple[TrackDescriptor, ...]:
        wanted = set(track_ids)
        available = {track.track_id: track for track in self.tracks}
        missing = wanted.difference(available)
        if missing:
            raise ValueError(f"unknown track id(s): {', '.join(sorted(missing))}")
        return tuple(track for track in self.tracks if track.track_id in wanted)


@dataclass(frozen=True, slots=True)
class LivePolicy:
    enabled: bool = False
    record_limit: str | None = None
    real_time_merge: bool | None = None
    keep_segments: bool | None = None
    pipe_mux: bool | None = None
    perform_as_vod: bool = False
    dvr_from_start: bool | None = None
    dvr_start_at: str | None = None
    dvr_end_at: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadPolicy:
    output: Path | None = None
    key_text_file: Path | None = None
    workers: int | None = None
    retries: int | None = None
    concurrent_tracks: bool = True
    max_speed: str | None = None
    http_timeout: int | None = None
    check_segments_count: bool | None = None
    resume: bool | None = None
    downloader: str | None = None
    mux: bool | None = None
    mux_format: str | None = None
    muxer: str | None = None
    mux_imports: tuple[str, ...] = ()
    #: JSON chapter sidecar generated from service-owned Playback metadata.
    chapters_file: Path | None = None
    subtitle_format: str | None = None
    auto_subtitle_fix: bool | None = None
    subtitle_only: bool = False
    audio_format: str | None = None
    audio_metadata_file: Path | None = None
    decode_audio_vivid: bool = False
    audio_vivid_decoder: str | None = None
    audio_vivid_decoder_args: str | None = None
    no_decrypt: bool = False
    decrypter: str | None = None
    hls_method: str | None = None
    hls_key: str | None = None
    hls_iv: str | None = None
    hls_decryptor: Any | None = None
    custom_range: str | None = None
    allow_hls_multi_ext_map: bool = False
    vgc: bool = False
    vgc_keep_opaque: bool = False
    temp_dir: Path | None = None
    log_file: Path | None = None
    write_meta_json: bool = False
    keep_temp: bool = False
    keep_after_done: bool = False
    no_color: bool = True
    live: LivePolicy = field(default_factory=LivePolicy)


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    manifest: ParsedManifest
    selected_track_ids: tuple[str, ...]
    save_name: str
    output_dir: Path
    keys: tuple[str, ...] = ()
    service_context: Mapping[str, Any] = field(default_factory=dict)
    policy: DownloadPolicy = field(default_factory=DownloadPolicy)

    def __post_init__(self) -> None:
        if not self.save_name.strip():
            raise ValueError("a delivery plan needs a save name")
        if not self.selected_track_ids:
            raise ValueError("a delivery plan needs at least one selected track")
        self.manifest.select(self.selected_track_ids)


class DeliveryStage(str, Enum):
    PREPARING = "preparing"
    DOWNLOADING = "downloading"
    DECRYPTING = "decrypting"
    MUXING = "muxing"
    CLEANING = "cleaning"
    COMPLETE = "complete"


class MessageLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class StageEvent:
    stage: DeliveryStage
    detail: str = ""


@dataclass(frozen=True, slots=True)
class MessageEvent:
    message: str
    level: MessageLevel = MessageLevel.INFO
    transient: bool = False


@dataclass(frozen=True, slots=True)
class TrackProgressEvent:
    track_id: str
    completed_segments: int = 0
    total_segments: int | None = None
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    elapsed_seconds: float = 0.0
    speed_bytes_per_second: float | None = None
    eta_seconds: float | None = None
    live: bool = False
    recorded_seconds: float | None = None
    duration_seconds: float | None = None
    status: str = "Downloading"
    done: bool = False


@dataclass(frozen=True, slots=True)
class OutputArtifact:
    path: Path
    kind: str = "media"
    track_id: str | None = None
    temporary: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactEvent:
    artifact: OutputArtifact


DeliveryEvent: TypeAlias = StageEvent | MessageEvent | TrackProgressEvent | ArtifactEvent


@dataclass(frozen=True, slots=True)
class LiveSegmentDescriptor:
    """Bounded Core view of the fragment that triggered key rotation."""

    url: str
    index: int | None = None
    duration: float | None = None
    byte_range: tuple[int, int] | None = None
    encrypted: bool = False
    encryption_scheme: str = ""
    key_id: str | None = None
    key_uri: str | None = None
    program_date_time: str | None = None


@dataclass(frozen=True, slots=True)
class LiveKeyRequest:
    kid: str | None
    track_id: str
    reason: str
    segment_index: int | None = None
    track: TrackDescriptor | None = None
    segment: LiveSegmentDescriptor | None = None
    require_kid: bool = False
    force: bool = False
    replace_existing: bool = False


class DeliveryStatus(str, Enum):
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DeliveryFailure:
    stage: DeliveryStage
    message: str
    code: str = "download_failed"
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status: DeliveryStatus
    artifacts: tuple[OutputArtifact, ...] = ()
    failure: DeliveryFailure | None = None
    exit_code: int | None = None

    @property
    def ok(self) -> bool:
        return self.status is DeliveryStatus.SUCCEEDED

    @property
    def cancelled(self) -> bool:
        return self.status is DeliveryStatus.CANCELLED


class DeliveryCancelled(RuntimeError):
    pass


class CancellationToken:
    """Thread-safe cancellation independent of terminal output."""

    def __init__(self, cancel_requested: Callable[[], bool] | None = None) -> None:
        self._event = threading.Event()
        self._cancel_requested = cancel_requested

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or bool(
            self._cancel_requested is not None and self._cancel_requested()
        )

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise DeliveryCancelled("delivery cancelled")


class PauseToken:
    """Keep a transfer job alive while workers wait instead of exiting.

    Distinct from :class:`CancellationToken`: pause does not finish the delivery,
    does not discard the native segment cache, and resume continues the same run.
    """

    def __init__(self, pause_requested: Callable[[], bool] | None = None) -> None:
        self._event = threading.Event()
        self._pause_requested = pause_requested

    def pause(self) -> None:
        self._event.set()

    def resume(self) -> None:
        self._event.clear()

    @property
    def paused(self) -> bool:
        return self._event.is_set() or bool(
            self._pause_requested is not None and self._pause_requested()
        )

    def wait(self, cancelled: Callable[[], bool] | None = None) -> None:
        """Block while paused, but still honour an explicit cancel."""
        while self.paused:
            if cancelled is not None and cancelled():
                raise DeliveryCancelled("delivery cancelled")
            time.sleep(0.1)


def _discard_event(_event: DeliveryEvent) -> None:
    return None


def _no_live_key(_request: LiveKeyRequest) -> str | None:
    return None


def _no_width() -> int | None:
    return None


@dataclass(slots=True)
class DeliveryHooks:
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    pause: PauseToken = field(default_factory=PauseToken)
    emit: Callable[[DeliveryEvent], None] = _discard_event
    request_live_key: Callable[[LiveKeyRequest], str | None] = _no_live_key
    display_width: Callable[[], int | None] = _no_width


@runtime_checkable
class DownloaderBackend(Protocol):
    def parse(self, request: ParseRequest) -> ParsedManifest: ...

    def command_line(self, plan: DeliveryPlan) -> str: ...

    def run(self, plan: DeliveryPlan, hooks: DeliveryHooks) -> DeliveryResult: ...


__all__ = [
    "ArtifactEvent",
    "CancellationToken",
    "DeliveryCancelled",
    "DeliveryEvent",
    "DeliveryFailure",
    "DeliveryHooks",
    "DeliveryPlan",
    "DeliveryResult",
    "DeliverySource",
    "DeliveryStage",
    "DeliveryStatus",
    "DownloadPolicy",
    "DownloaderBackend",
    "LiveKeyRequest",
    "LiveSegmentDescriptor",
    "LivePolicy",
    "MessageEvent",
    "MessageLevel",
    "OutputArtifact",
    "ParsePolicy",
    "ParseRequest",
    "ParsedManifest",
    "PauseToken",
    "SourceKind",
    "StageEvent",
    "TrackDescriptor",
    "TrackProgressEvent",
]
