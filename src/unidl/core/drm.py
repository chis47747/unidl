"""Which DRM systems exist, and what each one needs.

There were two, and the code said so in about nine places: an ``is_playready``
branch in the service base, a two-option setting, a hard-coded suffix table, a
device picker that grouped by one comparison. Adding a third meant touching all
of them, which is the definition of the wrong shape.

So a system is declared once, here, and everything else asks:

============  ==========  ==============  =====================  ==========
              Widevine    PlayReady       MonaLisa
============  ==========  ==============  =====================  ==========
device        ``.wvd``    ``.prd``        ``.mld`` + wasm
init data     PSSH box    WRM header      licence ticket
challenge     bytes       str             none
licence       POST        SOAP POST       none - decrypted locally
============  ==========  ==============  =====================  ==========

That last column is why the registry carries behaviour and not just labels.
MonaLisa has no licence round trip at all: a service hands out a ticket with
the playback response and the module unwraps it offline. A registry of names
would have left every caller still asking "but which kind is it".

Device *discovery* lives here too, because the file extension is what says which
system a device belongs to, and that is a fact about systems.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .playback import DrmInfo

WIDEVINE = "widevine"
PLAYREADY = "playready"
MONALISA = "monalisa"

#: how deep to look inside a search root; enough for `cdm/widevine/vendor/x.wvd`
_MAX_DEPTH = 3

_LEVEL_RE = re.compile(r"(?:^|[_\-.])(l[123]|sl[0-9]{4})(?:$|[_\-.])", re.IGNORECASE)


@dataclass
class Exchange:
    """Everything a key exchange needs, whichever system is running it.

    ``service`` is passed whole rather than as a transport callable because each
    system reaches for a different override - ``get_license`` for Widevine,
    ``get_license_soap`` for PlayReady, neither for MonaLisa - and a system is
    the right place to know that about itself.
    """

    #: the device file, when the CDM is on this machine
    device: Path | None
    init_data: str
    drm: DrmInfo
    service: Any
    log: Callable[[str], None] = lambda message: None
    #: set instead of ``device`` when the CDM is on a server. Both cannot be in
    #: play at once, and which one it is decides whether ``get_keys`` or
    #: ``remote_keys`` runs - so a system that has no remote form says so by
    #: leaving that field empty rather than by failing halfway through one.
    remote: Any = None


KeyGetter = Callable[[Exchange], list[str]]
"""Runs one exchange and returns ``kid:key`` strings."""

ChallengeMaker = Callable[[Exchange], bytes]
"""Produces a licence challenge and stops there, without asking for a licence.

Needed by services that must present a challenge **before** they are allowed to
learn anything about the content. The challenge comes first and the key ids
come back with the answer. Declared as its own capability rather than faked by
aborting an exchange, because aborting one means raising through code whose job is
to try alternatives and report that none worked.
"""

Extractor = Callable[[bytes | str, "DrmInfo", Callable[[str], None]], str | None]
"""Pulls init data out of a manifest, filling in ``drm`` as a side effect."""


@dataclass(frozen=True)
class DrmSystem:
    """One DRM system, and how to use it."""

    id: str
    label: str
    #: device file extension, the thing that identifies a device on disk
    suffix: str
    #: which :class:`~unidl.core.playback.DrmInfo` field carries its init data
    init_attr: str
    #: what to say when that field is empty
    missing_init: str
    get_keys: KeyGetter
    #: True when the library this needs is importable right now
    probe: Callable[[], bool]
    #: how to read init data off a DASH manifest, where that is possible at all
    extract: Extractor | None = None
    #: the same exchange, run against a CDM on a server. ``None`` means this
    #: system has no remote form - MonaLisa decrypts locally against a wasm
    #: module, so there is nothing on the other end of a socket to ask.
    remote_keys: KeyGetter | None = None
    #: produce a challenge and go no further. ``None`` means the system has no
    #: separable challenge step, which is true of MonaLisa: there is no licence
    #: request, so there is nothing to challenge with.
    make_challenge: ChallengeMaker | None = None
    #: does obtaining a key involve a licence server?
    networked: bool = True
    #: this system's init data is per key id, not per title, so every place one can
    #: hide is worth reading. Widevine repeats the same PSSH everywhere and its
    #: licence answers with every key, so the first one found is the whole story;
    #: PlayReady may put a different object - and therefore a different key - in
    #: each representation, and one exchange only answers for the header it was made
    #: with. Two readers act on this: the init-segment scan collects every distinct
    #: representation instead of stopping at the first, and the HLS reader also
    #: fetches media playlists from the encrypted licence inventory.
    collect_init_segments: bool = False
    help: str = ""
    install_hint: str = ""
    #: an ordering hint for pickers and settings; lower comes first
    rank: int = 50

    @property
    def available(self) -> bool:
        try:
            return bool(self.probe())
        except Exception:
            return False

    @property
    def remote_capable(self) -> bool:
        """Whether a CDM for this system can live on a server.

        Worth asking rather than assuming: a remote CDM needs no local library at
        all, so a system that is *not* installed here is still usable remotely -
        which is the main reason anyone sets one up.
        """
        return self.remote_keys is not None

    def option_label(self) -> str:
        """How the settings screen names it, including whether it can be used."""
        base = f"{self.label} ({self.suffix} device)"
        return base if self.available else f"{base} - not installed"


_SYSTEMS: dict[str, DrmSystem] = {}


def register(system: DrmSystem) -> DrmSystem:
    _SYSTEMS[system.id] = system
    return system


def get(system_id: str) -> DrmSystem | None:
    return _SYSTEMS.get(str(system_id or "").strip().lower())


def require(system_id: str) -> DrmSystem:
    system = get(system_id)
    if system is None:
        known = ", ".join(ids()) or "none"
        raise CdmError(f"Unknown DRM system {system_id!r}. Known systems: {known}")
    return system


def all_systems() -> list[DrmSystem]:
    return sorted(_SYSTEMS.values(), key=lambda s: (s.rank, s.label.lower()))


def ids() -> list[str]:
    return [system.id for system in all_systems()]


def available_ids() -> list[str]:
    return [system.id for system in all_systems() if system.available]


def suffixes() -> dict[str, str]:
    """``.wvd -> widevine``, built from the registry rather than restated."""
    return {system.suffix: system.id for system in all_systems()}


def system_of(path: Path | str) -> str:
    """Which DRM system a device file belongs to, from its extension."""
    return suffixes().get(Path(path).suffix.lower(), "")


class CdmError(RuntimeError):
    """Anything that went wrong obtaining a key, for any system."""


class FatalLicenseError(CdmError):
    """A service refusal for which another licence request must not be attempted."""


# ------------------------------------------------------------------ discovery


@dataclass
class DeviceInfo:
    name: str
    path: Path

    @property
    def exists(self) -> bool:
        return self.path.is_file()


@dataclass(frozen=True)
class LocalDeviceMetadata:
    """Non-secret facts read from one local CDM file.

    A file name is user-controlled display text, not an attestation of the
    device inside it. Widevine stores its level in the WVD structure and
    PlayReady stores it in the device certificate chain, so those are the only
    authoritative sources used for those systems. ``error`` is deliberately
    separate from ``level``: an unreadable ``strong_l1.wvd`` must be shown as
    unusable, not silently downgraded to a filename guess.
    """

    level: str = ""
    error: str = ""

    @property
    def usable(self) -> bool:
        return not self.error


def _security_level(value: Any, prefix: str, allowed: set[int]) -> LocalDeviceMetadata:
    if isinstance(value, bool):
        return LocalDeviceMetadata(error="security level is a boolean")
    try:
        level = int(value)
    except (TypeError, ValueError):
        return LocalDeviceMetadata(error="device contains no readable security level")
    if level not in allowed:
        return LocalDeviceMetadata(error=f"device contains unsupported security level {level}")
    return LocalDeviceMetadata(level=f"{prefix}{level}")


@lru_cache(maxsize=512)
def _read_local_device_metadata(
    path_text: str,
    system: str,
    _size: int,
    _mtime_ns: int,
    _ctime_ns: int,
    _inode: int,
) -> LocalDeviceMetadata:
    """Read a local device once for this exact file revision.

    The stat fields are cache-key material. Device pickers repaint frequently,
    while loading a WVD imports its RSA key and loading a PRD parses its
    certificate chain. Replacing or editing a file changes the key and causes a
    fresh inspection without keeping any device/private-key object in memory.
    """
    path = Path(path_text)
    try:
        if system == WIDEVINE:
            from pywidevine.device import Device

            device = Device.load(path)
            return _security_level(
                getattr(device, "security_level", None),
                "L",
                {1, 2, 3},
            )
        if system == PLAYREADY:
            from pyplayready.device import Device

            device = Device.load(path)
            return _security_level(
                getattr(device, "security_level", None),
                "SL",
                {150, 2000, 3000},
            )
    except Exception as exc:  # optional readers expose several parse errors
        detail = str(exc).strip()
        message = type(exc).__name__ if not detail else f"{type(exc).__name__}: {detail}"
        return LocalDeviceMetadata(error=message)
    return LocalDeviceMetadata()


def local_device_metadata(path: Path | str, system: str = "") -> LocalDeviceMetadata:
    """Authoritative display metadata for a local WVD/PRD.

    Missing and unreadable files are data, not picker-fatal exceptions. Systems
    without an embedded security level (currently MonaLisa) return an empty
    result and keep their existing system-specific presentation.
    """
    candidate = Path(path)
    if not candidate.name:
        return LocalDeviceMetadata()
    try:
        stat = candidate.stat()
    except OSError as exc:
        return LocalDeviceMetadata(error=f"{type(exc).__name__}: {exc}")
    if not candidate.is_file():
        return LocalDeviceMetadata(error="not a regular file")
    try:
        resolved = str(candidate.resolve())
    except OSError:
        resolved = str(candidate)
    resolved_system = str(system or system_of(candidate)).strip().lower()
    return _read_local_device_metadata(
        resolved,
        resolved_system,
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
        int(getattr(stat, "st_ino", 0)),
    )


@dataclass(frozen=True)
class DeviceFile:
    """One CDM that can be chosen: a file on disk, or a remote one.

    A remote CDM is not a file, but it *is* a device you pick, and the picker
    already knows how to list, number, filter and mark these. Giving it a second
    kind of row rather than a second list is what keeps "which CDM am I using"
    one question with one answer.
    """

    path: Path = Path()
    system: str = ""
    #: the configured name, set only for a remote CDM
    remote: str = ""
    #: security level as the source states it, for sources that state it rather
    #: than encoding it in a file name
    stated_level: str = ""
    #: where it is, for the picker's right-hand column
    origin: str = ""

    @property
    def is_remote(self) -> bool:
        return bool(self.remote)

    @property
    def name(self) -> str:
        return self.remote or self.path.stem

    @property
    def level(self) -> str:
        """Security level stated remotely or read from the local device.

        WVD/PRD names never decide this value. MonaLisa's JSON format has no
        security-level field, so its historical filename hint remains only for
        that non-Widevine/non-PlayReady format until the format states one.
        """
        if self.stated_level:
            return self.stated_level
        system = str(self.system or "").strip().lower()
        if system in {WIDEVINE, PLAYREADY}:
            return local_device_metadata(self.path, system).level
        match = _LEVEL_RE.search(self.path.stem)
        return match.group(1).upper() if match else ""

    @property
    def problem(self) -> str:
        """Why this local WVD/PRD cannot be inspected, if applicable."""
        system = str(self.system or "").strip().lower()
        if self.is_remote or system not in {WIDEVINE, PLAYREADY}:
            return ""
        return local_device_metadata(self.path, system).error

    @property
    def usable(self) -> bool:
        """Whether the picker may offer this device for a local exchange."""
        return self.is_remote or not self.problem

    @property
    def where(self) -> str:
        """The folder it came from, or the host it lives on."""
        return self.origin or (self.path.parent.name if self.path.name else "")

    @property
    def label(self) -> str:
        return f"{self.name}  {self.level}".strip()


def remote_devices(configs) -> list[DeviceFile]:
    """The configured remote CDMs, as picker rows.

    Kept next to :func:`discover` because both answer the same question - what
    can be chosen - and a caller that needs one almost always needs the other.
    """
    from urllib.parse import urlparse

    rows: list[DeviceFile] = []
    for config in configs or []:
        if not getattr(config, "enabled", True):
            continue
        host = urlparse(config.host).netloc or config.host
        rows.append(
            DeviceFile(
                path=Path(),
                system=config.system,
                remote=config.name,
                stated_level=config.level,
                origin=host,
            )
        )
    return rows


def discover(roots: Iterable[Path]) -> list[DeviceFile]:
    """Every CDM file under ``roots``, de-duplicated and ordered by system.

    Registration order, then alphabetical, so the picker groups without needing
    its own opinion about which systems exist. Unreadable directories are skipped
    rather than raising: a stale search path in the config should not stop the
    picker from opening.
    """
    order = {system.id: index for index, system in enumerate(all_systems())}
    found: dict[Path, DeviceFile] = {}
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for suffix, system_id in suffixes().items():
            for depth in range(_MAX_DEPTH + 1):
                pattern = "/".join(["*"] * depth + [f"*{suffix}"]) if depth else f"*{suffix}"
                try:
                    matches = list(root.glob(pattern))
                except OSError:
                    continue
                for path in matches:
                    if not path.is_file():
                        continue
                    try:
                        key = path.resolve()
                    except OSError:
                        key = path
                    found.setdefault(key, DeviceFile(path=path, system=system_id))
    return sorted(
        found.values(), key=lambda d: (order.get(d.system, 99), d.name.lower())
    )


# --------------------------------------------------------- the three systems
# Imports are deliberately inside the functions. wasmtime, pywidevine and
# pyplayready are all optional at import time - a missing one must degrade to
# "that system is unavailable", not stop the application from starting.


def _widevine_keys(exchange: Exchange) -> list[str]:
    from . import cdm

    return cdm.get_keys(
        exchange.device,
        exchange.init_data,
        lambda challenge: exchange.service.get_license(challenge, exchange.drm),
        service_certificate=exchange.drm.service_certificate,
    )


def _widevine_remote_keys(exchange: Exchange) -> list[str]:
    from . import remotecdm

    return remotecdm.widevine_keys(
        exchange.remote,
        exchange.init_data,
        lambda challenge: exchange.service.get_license(challenge, exchange.drm),
        certificate=exchange.drm.service_certificate,
        log=exchange.log,
    )


def _widevine_challenge(exchange: Exchange) -> bytes:
    """A Widevine challenge on its own, from whichever CDM is in play."""
    if exchange.remote is not None:
        from . import remotecdm

        with remotecdm.RemoteCdm(exchange.remote, log=exchange.log) as cdm:
            return cdm.challenge(exchange.init_data)

    from pywidevine.cdm import Cdm
    from pywidevine.pssh import PSSH

    from . import cdm as cdm_module

    cdm = Cdm.from_device(cdm_module.load_device(exchange.device))
    session_id = cdm.open()
    try:
        return cdm.get_license_challenge(session_id, PSSH(exchange.init_data))
    finally:
        with contextlib.suppress(Exception):
            cdm.close(session_id)


def _widevine_extract(manifest: bytes | str, drm: DrmInfo, log) -> str | None:
    from . import pssh as pssh_tools

    drm.pssh = pssh_tools.from_mpd(manifest)
    if not drm.pssh:
        # HLS keeps the same PSSH in an EXT-X-KEY attribute rather than in XML, so
        # the DASH parser finds nothing and the title looks unprotected.
        drm.pssh = pssh_tools.from_hls(manifest)
        if drm.pssh:
            log("Widevine: PSSH taken from the playlist's key line")
    return drm.pssh


def _playready_keys(exchange: Exchange) -> list[str]:
    from . import playready

    def get_one(header: str) -> list[str]:
        args = (
            exchange.device,
            header,
            lambda challenge: exchange.service.get_license_soap(challenge, exchange.drm),
        )
        if exchange.drm.playready_custom_data:
            return playready.get_keys(
                *args,
                custom_data=exchange.drm.playready_custom_data,
            )
        return playready.get_keys(*args)

    return _collect_playready_keys(
        exchange,
        get_one,
    )


def _distinct(values: Iterable[str | None]) -> list[str]:
    """Non-empty strings in first-seen order, without duplicate requests."""
    found: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        found.append(text)
    return found


#: A refusal shaped like "you are asking too often". Worth stopping the loop for:
#: the next header gets the same answer, and that answer says nothing about it.
#: Some providers answer 403 ``throttled`` after a handful of licence requests.
_THROTTLED = ("throttl", "too many request", "rate limit", "429")


def _playready_headers(exchange: Exchange) -> list[str]:
    """Every distinct WRM header represented by this PlayReady playback.

    Two rounds of deduplication, because one is not enough. Identical text catches
    the same object repeated per representation; the same *key id* set catches the
    same key written differently - a v4.1 header and its v4.0 downgrade, or one
    object per rendition that all name one key. Each survivor costs a licence
    request, so a duplicate here is a request that buys nothing.
    """
    from . import playready

    headers = _distinct(
        [
            exchange.init_data,
            *(exchange.drm.context.get("wrm_headers") or []),
        ]
    )
    unique: list[str] = []
    seen: set[frozenset[str]] = set()
    for header in headers:
        kids = frozenset(playready.key_ids_from_header(header))
        # no readable key ids: keep it, it cannot be judged a duplicate
        if kids and kids in seen:
            continue
        if kids:
            seen.add(kids)
        unique.append(header)
    return unique


def _playready_wanted_kids(exchange: Exchange, headers: Iterable[str]) -> set[str]:
    """The key ids this playback has to end up with.

    Core's encrypted licence inventory first - normally the full ladder, or the
    explicit selected-track compatibility inventory - and the key ids the headers
    themselves declare, which is the answer when the manifest does not state key
    ids at all, as HLS usually does not.
    """
    from . import playready

    wanted = {
        playready.canonical_kid(kid)
        for kid in (exchange.drm.context.get("license_track_kids") or [])
    }
    for header in headers:
        wanted.update(playready.key_ids_from_header(header))
    return {kid for kid in wanted if kid}


def _collect_playready_keys(
    exchange: Exchange,
    get_one: Callable[[str], list[str]],
) -> list[str]:
    """Run one independent licence exchange per distinct PlayReady header.

    pyplayready exposes one content key for the PSSH/WRM header used to create a
    challenge. A manifest can carry separate PlayReady objects for video, audio
    and different representations, so returning after the first accepted
    licence silently drops the remaining keys. Each header gets a fresh local or
    remote CDM session through ``get_one``; successful keys are then merged.

    The loop is driven by *coverage* rather than by the length of the header list:
    it stops as soon as every key id in the licence inventory has a key, and skips a
    header whose key ids are already answered. That is what keeps a service which
    throttles licence requests from being asked more times than the download needs
    - one licence often carries several keys, and the headers behind them are then
    already paid for.
    """
    from . import playready

    headers = _playready_headers(exchange)
    if not headers:
        raise CdmError("No WRM header available; the service must supply one")

    drm = exchange.drm
    wanted = _playready_wanted_kids(exchange, headers)
    # Core may have already obtained some KIDs from the vault. Keep them in the
    # same coverage map as freshly licensed keys so a header for an already
    # covered KID is skipped, while a header carrying another KID still goes to
    # the service's own license endpoint.
    from .vault import split_pair

    keys: dict[str, str] = {}
    for value in drm.context.get("vault_keys") or []:
        pair = split_pair(str(value))
        if pair:
            keys.setdefault(*pair)
    original = drm.wrm_header
    cached_count = len(keys)
    failures: list[Exception] = []
    asked = 0
    throttled = False
    try:
        for index, header in enumerate(headers, 1):
            kids = playready.key_ids_from_header(header)
            if kids and all(kid in keys for kid in kids):
                exchange.log(
                    f"PlayReady: header {index}/{len(headers)} is already answered, "
                    "not asking again"
                )
                continue
            # Some transports need the KID from the header as well as the SOAP
            # challenge. Keep DrmInfo aligned with the exchange
            # currently in flight rather than leaving it pinned to header one.
            drm.wrm_header = header
            try:
                current = list(get_one(header) or [])
                asked += 1
            except FatalLicenseError:
                raise
            except Exception as exc:  # noqa: BLE001 - collect every usable key
                failures.append(exc)
                exchange.log(
                    f"PlayReady: licence {index}/{len(headers)} failed "
                    f"({str(exc)[:160]})"
                )
                if any(word in str(exc).lower() for word in _THROTTLED):
                    exchange.log(
                        "PlayReady: the licence server is throttling, stopping with "
                        f"{len(keys)} key(s)"
                    )
                    throttled = True
                    break
                continue
            for key in current:
                kid, _, value = str(key).strip().partition(":")
                kid = playready.canonical_kid(kid) or kid.lower()
                if kid and value:
                    keys.setdefault(kid, value.lower())
            exchange.log(
                f"PlayReady: licence {index}/{len(headers)} returned "
                f"{len(current)} key(s)"
            )
            if wanted and wanted <= set(keys):
                break
    finally:
        drm.wrm_header = original or headers[0]

    if keys:
        cache_note = f", {cached_count} from vault" if cached_count else ""
        exchange.log(
            f"PlayReady: {len(keys)} key(s) from {asked} licence request(s)"
            f"{cache_note} across {len(headers)} distinct header(s)"
        )
        missing = sorted(wanted - set(keys))
        if missing and not throttled:
            exchange.log(
                f"PlayReady: no key for {', '.join(missing)} - a track using it "
                "will not decrypt"
            )
        return [f"{kid}:{value}" for kid, value in keys.items()]
    last = failures[-1] if failures else "no content keys"
    raise CdmError(f"No PlayReady header was accepted: {last}")


def _playready_challenge(exchange: Exchange) -> bytes:
    """A PlayReady challenge on its own. It is XML, so it comes back encoded."""
    if exchange.remote is not None:
        from . import remotecdm

        with remotecdm.RemoteCdm(exchange.remote, log=exchange.log) as cdm:
            return cdm.challenge(exchange.init_data)

    from pyplayready.cdm import Cdm
    from pyplayready.device import Device

    from . import playready

    playready.require()
    try:
        device = Device.load(str(exchange.device))
    except Exception as exc:  # noqa: BLE001 - any load failure means the same thing
        raise CdmError(f"could not load {exchange.device}: {exc}") from exc
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    try:
        challenge = cdm.get_license_challenge(
            session_id,
            exchange.init_data,
            custom_data=exchange.drm.playready_custom_data,
        )
    finally:
        with contextlib.suppress(Exception):
            cdm.close(session_id)
    return challenge.encode("utf-8") if isinstance(challenge, str) else bytes(challenge)


def _playready_remote_keys(exchange: Exchange) -> list[str]:
    from . import remotecdm

    if exchange.drm.playready_custom_data:
        raise CdmError(
            "This PlayReady exchange requires signed CustomData and therefore a local PlayReady CDM"
        )

    return _collect_playready_keys(
        exchange,
        lambda header: remotecdm.playready_keys(
            exchange.remote,
            header,
            lambda challenge: exchange.service.get_license_soap(challenge, exchange.drm),
            log=exchange.log,
        ),
    )


def _playready_extract(manifest: bytes | str, drm: DrmInfo, log) -> str | None:
    from . import playready

    # pyplayready's own parser first: DASH and Smooth Streaming both carry a
    # base64 PlayReady object, not the header verbatim. Unpacking it by hand is
    # how you end up with a header the CDM will not accept.
    objects = playready.playready_objects_from_manifest(manifest)
    from_hls = False
    if not objects:
        # the same object, but in an HLS key line instead of an XML node
        objects = playready.playready_objects_from_hls(manifest)
        from_hls = bool(objects)

    known_objects = _distinct(
        [*(drm.context.get("playready_psshs") or []), *objects]
    )
    if known_objects:
        # Preserve the deduplicated source objects as well as their parsed WRM
        # headers. This makes repeated init segments cheap and keeps the public
        # state faithful to the "one request per unique PSSH" rule.
        drm.context["playready_psshs"] = known_objects

    parsed_headers: list[str] = []
    for blob in _distinct(objects):
        try:
            parsed_headers.extend(playready.wrm_headers_from_pssh(blob))
        except Exception as exc:  # noqa: BLE001
            log(f"skipping a PlayReady PSSH: {exc}")
    if from_hls and parsed_headers:
        log("PlayReady: object taken from the playlist's key line")
    if not parsed_headers:
        single = playready.wrm_header_from_mpd(manifest)
        parsed_headers = [single] if single else []

    before = len(drm.context.get("wrm_headers") or [])
    headers = _distinct(
        [
            drm.wrm_header,
            *(drm.context.get("wrm_headers") or []),
            *parsed_headers,
        ]
    )
    if headers:
        drm.wrm_header = headers[0]
        drm.context["wrm_headers"] = headers
    # Counts are cumulative: this runs once per document - the master playlist, each
    # selected media playlist, each representation's init segment - so saying it
    # again unchanged only reads as a repeat of the same finding.
    if headers and len(headers) != before:
        log(
            f"PlayReady: {len(known_objects)} distinct PSSH object(s) so far, "
            f"{len(headers)} WRM header(s)"
        )
    return drm.wrm_header


def _monalisa_keys(exchange: Exchange) -> list[str]:
    from . import monalisa

    return monalisa.get_keys(exchange.device, exchange.init_data)


def _probe(module: str) -> Callable[[], bool]:
    def probe() -> bool:
        import importlib.util

        return importlib.util.find_spec(module) is not None

    return probe


register(
    DrmSystem(
        id=WIDEVINE,
        label="Widevine",
        suffix=".wvd",
        init_attr="pssh",
        missing_init="No Widevine PSSH found in the manifest",
        get_keys=_widevine_keys,
        probe=_probe("pywidevine"),
        extract=_widevine_extract,
        remote_keys=_widevine_remote_keys,
        make_challenge=_widevine_challenge,
        help="The common case. Needs a .wvd device; an L3 one cannot open every stream.",
        install_hint="python -m pip install pywidevine, then configure a .wvd under paths.cdm in unidl.yaml",
        rank=10,
    )
)

register(
    DrmSystem(
        id=PLAYREADY,
        label="PlayReady",
        suffix=".prd",
        init_attr="wrm_header",
        missing_init="No PlayReady WRM header found in the manifest",
        get_keys=_playready_keys,
        probe=_probe("pyplayready"),
        extract=_playready_extract,
        remote_keys=_playready_remote_keys,
        make_challenge=_playready_challenge,
        collect_init_segments=True,
        help="Some services only answer PlayReady, or answer it at a higher quality.",
        install_hint="python -m pip install pyplayready, then configure a .prd under paths.cdm in unidl.yaml",
        rank=20,
    )
)

register(
    DrmSystem(
        id=MONALISA,
        label="MonaLisa",
        suffix=".mld",
        init_attr="init_data",
        missing_init="No MonaLisa ticket; the service must supply one",
        get_keys=_monalisa_keys,
        probe=_probe("pymonalisa"),
        # nothing to read off a manifest: the ticket arrives with the playback
        # response, so only the service can provide it
        extract=None,
        networked=False,
        help="The ticket comes with the playback response and is unwrapped "
        "locally, so there is no licence request to make.",
        install_hint="python -m pip install pymonalisa, then configure a .mld and its "
        "wasm module under paths.cdm in unidl.yaml",
        rank=30,
    )
)


def init_data_for(drm: DrmInfo, system_id: str) -> str | None:
    """The init data the named system reads, whichever field that is."""
    system = get(system_id)
    if system is None:
        return None
    return getattr(drm, system.init_attr, None)


def set_init_data(drm: DrmInfo, system_id: str, value: str | None) -> None:
    system = get(system_id)
    if system is not None:
        setattr(drm, system.init_attr, value)


__all__ = [
    "MONALISA",
    "PLAYREADY",
    "WIDEVINE",
    "CdmError",
    "FatalLicenseError",
    "DeviceFile",
    "DeviceInfo",
    "DrmSystem",
    "Exchange",
    "all_systems",
    "available_ids",
    "discover",
    "get",
    "ids",
    "init_data_for",
    "register",
    "remote_devices",
    "require",
    "set_init_data",
    "suffixes",
    "system_of",
]
