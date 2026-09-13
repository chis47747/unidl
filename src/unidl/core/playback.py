"""The handoff contract between a service and the download engine.

A service's only job is to produce a :class:`Playback`. Everything after that
(parse, select, download, decrypt, subs, mux) belongs to UniDL.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from .chapters import Chapter, normalize_chapters
from .lyrics import Lyrics
from .secureio import safe_filename
from .titles import Title


def normalize_live_record_limit(value: object) -> str:
    """Return a live recording limit, with zero meaning no limit.

    The TUI presents ``00:00:00`` as the deliberately explicit "keep recording"
    choice.  Keeping that spelling in settings is useful to a person, but passing
    it down as a limit is ambiguous at the downloader boundary (and makes status
    text look as if a recording should finish immediately).  Normalize the common
    clock and unit spellings here so every caller shares the same contract.
    Invalid, non-zero text is left untouched; the native parser remains the place
    that validates the accepted duration syntax.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    compact = re.sub(r"\s+", "", text).lower()
    if compact in {"0", "0.0", "0s", "0sec", "0secs", "0second", "0seconds"}:
        return ""
    if ":" not in compact and not re.search(r"[hms]", compact):
        try:
            if float(compact) == 0:
                return ""
        except ValueError:
            pass
    if ":" in compact:
        parts = compact.split(":")
        try:
            if len(parts) in (2, 3) and all(float(part) == 0 for part in parts):
                return ""
        except ValueError:
            pass
    # The downloader also accepts compact unit forms such as ``1h20m``.  Only
    # treat a string made entirely of zero-valued units as unlimited.
    units = re.findall(r"(\d+(?:\.\d+)?)([hms])", compact)
    if units and "".join(f"{number}{unit}" for number, unit in units) == compact:
        try:
            if all(float(number) == 0 for number, _unit in units):
                return ""
        except ValueError:
            pass
    return text


@dataclass
class DrmInfo:
    """Everything core needs to run the DRM flow for one playback.

    ``pssh`` may be left empty, in which case core extracts it from the
    manifest. ``context`` carries service-private values plus core's
    ``license_tracks`` / ``license_track_kids`` inventory and is passed back to
    the service DRM path.
    """

    #: "widevine" or "playready", or ``None`` for "no opinion, use the global
    #: setting". A service that only ever serves one system should pin it, and
    #: ``None`` is what makes that pin distinguishable from silence: while the
    #: default was ``"widevine"`` the two looked identical, so a global toggle
    #: flipped Widevine-only services to PlayReady. PlayReady uses ``wrm_header``
    #: in place of ``pssh``.
    system: str | None = None
    wrm_header: str | None = None

    pssh: str | None = None
    license_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    cdm: str | None = None
    #: Init data for a system that does not use ``pssh`` or ``wrm_header``.
    #: MonaLisa's licence ticket lands here: it arrives with the playback
    #: response rather than being read off a manifest, and the registry knows
    #: which field its system reads, so a fourth system needs no fourth field.
    init_data: str | None = None

    #: Widevine privacy-mode certificate supplied by the service before the CDM
    #: creates its licence challenge. Most services leave this empty.
    service_certificate: bytes | None = None

    #: Signed PlayReady ``CustomData`` inserted while the CDM builds the SOAP
    #: challenge. This cannot be added later by a licence transport because it is
    #: part of the signed ``LA`` body. Services leave it empty unless their client
    #: identity explicitly requires it.
    playready_custom_data: str | None = None

    # HLS AES-128 path: services that pull a raw key themselves fill these in.
    hls_key: str | None = None
    hls_iv: str | None = None
    hls_method: str | None = None
    #: Stateful in-process HLS decryptor for service-owned native DRM transports.
    hls_decryptor: Any | None = None

    #: The service attached this only to say the stream is *not* encrypted - a
    #: clear title inside an otherwise protected catalogue. Saying so is the only
    #: way core can tell that apart from a licence it has not been told about yet.
    clear: bool = False

    @property
    def is_playready(self) -> bool:
        return str(self.system or "").lower() == "playready"

    @property
    def needs_license(self) -> bool:
        """Fail closed: a ``DrmInfo`` that exists means DRM unless told otherwise.

        This used to be inferred from which fields happened to be filled in, but
        this class invites a service to leave ``pssh`` empty and let core read it
        off the manifest, and ``get_license`` is overridable, so a service that
        builds its licence URL at request time legitimately has all three empty.
        Guessing "no DRM" there printed ``key: none needed`` in the same colour as
        a real answer and handed an encrypted manifest to UniDL, which then
        failed much later and for an unrelated-looking reason.
        """
        if self.clear or self.hls_key or self.hls_decryptor is not None:
            return False  # a raw HLS key is its own path, not a licence exchange
        return True


@dataclass
class LiveWindow:
    """Which part of a live stream to take.

    A live manifest usually offers more than the live edge: a replay - or DVR -
    window holding the last stretch of the broadcast. Four things can be done with
    it, and they are four different UniDL invocations rather than shades of one,
    which is why this is a mode and not a pair of timestamps:

    ``edge``
        Start now and keep going. What a recorder does by default.
    ``start``
        Start at the beginning of the window, then keep recording forward.
    ``offset``
        A stretch measured from the window's beginning - ``start_at``, optionally
        up to ``end_at``.
    ``vod``
        Take the window as it stands, once, as a finished file. No recording: it
        ends when the window has been fetched.

    Offsets are strings, in the shape UniDL takes: ``HH:MM:SS``.
    """

    mode: str = "edge"
    start_at: str = ""
    end_at: str = ""

    @property
    def records_forward(self) -> bool:
        """Whether this keeps going, and so needs a length limit to ever stop."""
        return self.mode in ("edge", "start")

    def describe(self) -> str:
        if self.mode == "vod":
            return "the replay window as it stands, once"
        if self.mode == "start":
            return "from the start of the replay window"
        if self.mode == "offset":
            span = f"from {self.start_at} into the window"
            return f"{span} to {self.end_at}" if self.end_at else span
        return "from the live edge"


@dataclass
class ExternalTrack:
    """A sidecar file to mux in (subtitle or audio)."""

    path: str
    language: str = "und"
    name: str = ""


@dataclass(frozen=True)
class SubtitleReference:
    """A remote subtitle advertised by playback metadata, not yet downloaded."""

    url: str
    language: str = "und"
    kind: str = "normal"
    name: str = ""
    original: bool = False
    selected: bool = False


@dataclass
class Playback:
    title: Title
    save_name: str

    # Exactly one of these is the input UniDL consumes.
    manifest_url: str | None = None
    #: Authorized remote URL used to resolve relative media URLs when
    #: ``manifest_url`` points at a local manifest snapshot.
    manifest_base_url: str | None = None
    #: A service may receive an authorized manifest document instead of a URL.
    #: The downloader materializes it in its private manifest cache and parses it
    #: through the same native path as a remote DASH/HLS reference.
    inline_manifest: str | None = None
    #: Additional manifest URLs explicitly returned by the same playback
    #: authorization, in service-defined order. The engine only tries entries
    #: listed here; it never manufactures alternate hosts or paths.
    alternate_manifest_urls: tuple[str, ...] = ()
    #: Number of attempts for each candidate. Services should leave this at one
    #: unless the platform's verified playback contract retries a transient
    #: manifest failure.
    manifest_attempts: int = 1
    #: Parse every service-authorized playback variant and expose one deduplicated
    #: ladder.  Variants are obtained through ``Service.manifest_variants`` so
    #: Core never guesses API parameters, hosts or resolutions.  Off preserves the
    #: ordinary first-usable-manifest fallback path.
    merge_manifests: bool = False
    #: Parse successive service-authorized media windows and concatenate their
    #: timeline segments. This is for finite catch-up programmes whose playback
    #: API exposes a short sliding window per request, not quality ladders.
    merge_manifest_segments: bool = False
    #: Intended duration of a finite sequence merge, in seconds. The backend
    #: uses this to stop at the programme boundary and reject truncated results.
    manifest_duration: float = 0.0
    json_manifest: dict[str, Any] | None = None

    headers: dict[str, str] = field(default_factory=dict)
    proxy: str | None = None
    is_live: bool = False
    #: which part of a live stream to take. None means the live edge, which is also
    #: what an unanswered or unoffered replay-window question leaves behind.
    live_window: LiveWindow | None = None
    #: Per-delivery override confirmed after track selection. Empty means use the
    #: service setting, which remains the default for headless/non-interactive runs;
    #: an explicit ``00:00:00`` is retained as a non-empty zero override so it can
    #: replace an older non-zero service default with unlimited recording.
    live_record_limit: str = ""

    drm: DrmInfo | None = None
    keys: list[str] = field(default_factory=list)  # "kid:key", filled by core or service
    #: Content keys that have already been consumed by a service-owned transport.
    #: They are shown for inspection but never sent to the downloader or exports.
    display_keys: list[str] = field(default_factory=list)

    mux_imports: list[ExternalTrack] = field(default_factory=list)
    #: Complete remote subtitle inventory. Kept separate from ``mux_imports``:
    #: command-only output lists these URLs without fetching them, while a real
    #: download may materialize only references marked ``selected``.
    subtitle_references: list[SubtitleReference] = field(default_factory=list)
    #: Optional navigation chapters returned by this service's title API.  The
    #: service translates its own payload into Core milliseconds; no capability
    #: declaration or empty hook is required when the service has no chapter API.
    chapters: list[Chapter] = field(default_factory=list)
    #: Optional static, line-synchronized, or word-synchronized lyrics. Services
    #: retain the original TTML inside this neutral model for display and export.
    lyrics: Lyrics | None = None
    extra_args: list[str] = field(default_factory=list)  # escape hatch for odd services

    #: Fail closed when automatic quality/codec/range rules match no video.
    #: Most services keep the app-wide permissive behaviour; source APIs whose
    #: request is itself exact can opt out of an accidental audio-only result.
    strict_track_selection: bool = False

    #: Dynamic range reported by the playback API when the media manifest omits
    #: that metadata. Core uses this only for video tracks parsed as SDR/unknown;
    #: an explicit manifest value such as DV, HLG or HDR10+ always wins.
    video_range_hint: str = ""

    #: A service-owned source-resolution choice. When set, it takes precedence
    #: over the app-wide video-quality setting for this playback only.
    video_quality_hint: str = ""

    #: What the service asked its playback API for, and what that API explicitly
    #: reported back. These are deliberately separate from the parsed manifest:
    #: a requested UHD profile can legitimately return an HD rendition.
    requested: str = ""
    returned: str = ""

    note: str = ""  # shown in the UI, e.g. "clear stream, no DRM"

    #: Nothing but audio is worth taking: a radio programme, a podcast, a music
    #: stream. Set automatically for audio title kinds; a service can also set it
    #: for a video source it wants delivered as audio.
    audio_only: bool = False
    #: Codec declared by the service's exact playback/variant response. Used for
    #: audio-only container decisions when a child manifest omits codec metadata.
    audio_codec_hint: str = ""
    #: ID3 tags for the output. Defaults to whatever the title knows.
    audio_tags: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.save_name = safe_filename(self.save_name, label="Playback.save_name")
        if not self.manifest_url and not self.inline_manifest and self.json_manifest is None:
            raise ValueError("Playback needs a manifest URL, inline manifest or json_manifest")
        if self.merge_manifests and self.merge_manifest_segments:
            raise ValueError("Playback cannot merge ladders and timeline windows together")
        # One source of truth: the title kind already says what this is, so a
        # service that sets the kind does not also have to remember these two.
        # Only ever turned on here - a service can force either flag for a kind
        # that does not imply it, such as recording a live channel as audio.
        if self.title is not None:
            if self.title.is_audio:
                self.audio_only = True
            if self.title.is_live:
                self.is_live = True
        if self.audio_only and not self.audio_tags and self.title is not None:
            self.audio_tags = self.title.audio_tags()
        duration_ms = None
        if self.title is not None and self.title.duration is not None:
            try:
                duration = float(self.title.duration)
            except (TypeError, ValueError):
                duration = None
            if duration is not None and math.isfinite(duration) and duration > 0:
                duration_ms = round(duration * 1000)
        self.chapters = list(
            normalize_chapters(self.chapters, duration_ms=duration_ms)
        )
