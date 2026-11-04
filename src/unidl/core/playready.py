"""PlayReady support.

Kept apart from :mod:`unidl.core.cdm` because although the two flows look alike,
the inputs and outputs differ in ways that matter:

============  ==========================  ============================
              Widevine                    PlayReady
============  ==========================  ============================
device        ``.wvd``                    ``.prd``
init data     PSSH box                    WRM header (XML)
challenge     bytes                       str
license       raw bytes                   SOAP message (str)
============  ==========================  ============================

``pyplayready`` mirrors ``pywidevine``'s shape - ``Cdm.from_device``, ``open``,
``get_license_challenge``, ``parse_license``, ``get_keys`` - so a service only
has to supply the license exchange, exactly as with Widevine. This module runs
one PSSH/WRM-header exchange at a time; :mod:`unidl.core.drm` deduplicates every
PlayReady object in a manifest, invokes this flow once per distinct header and
merges the keys. Widevine deliberately keeps its single-exchange path.

Written against pyplayready 0.8.5, which is the first line that coexists with
pywidevine. Where it differs from older releases the difference is absorbed here
rather than in services: notably ``PSSH.get_wrm_headers()`` became a
``wrm_headers`` attribute, and both shapes are accepted.
"""

from __future__ import annotations

import base64
import binascii
import importlib.util
import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

INSTALL_HINT = (
    "PlayReady needs pyplayready 0.8.5 or newer: python -m pip install pyplayready\n"
    "    then put a .prd device in ~/.unidl/cdm/playready/"
)

#: PlayReady's system id in a DASH manifest
PLAYREADY_SCHEME_ID = "urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95"
_WRM_HEADER = re.compile(
    r"<WRMHEADER.*?</WRMHEADER>", re.IGNORECASE | re.DOTALL
)


class PlayReadyUnavailable(RuntimeError):
    """pyplayready is not installed, or no .prd device is configured."""


def available() -> bool:
    return importlib.util.find_spec("pyplayready") is not None


def require() -> None:
    if not available():
        raise PlayReadyUnavailable(f"PlayReady is not available.\n    {INSTALL_HINT}")


def is_device(path: Path) -> bool:
    return Path(path).suffix.lower() == ".prd"


def wrm_header_from_mpd(manifest_text: str | bytes) -> str | None:
    """Pull the WRM header out of a DASH manifest's PlayReady ``pro`` data.

    PlayReady stores a base64 PlayReady Object; the WRM header is the XML inside
    it. Manifests often also carry the header verbatim in ``mspr:pro`` siblings,
    which is what this looks for first.
    """
    text = manifest_text.decode("utf-8", "ignore") if isinstance(manifest_text, bytes) else manifest_text
    match = _WRM_HEADER.search(text)
    if match:
        return match.group(0)

    import base64
    from xml.etree import ElementTree

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None
    # A few DASH packagers emit a version-1 PlayReady PSSH with a KID list but
    # an empty data payload. That
    # is enough to identify the content key, but it is not enough for
    # ``pyplayready.PSSH`` to unpack into a WRMHEADER.  Keep the normal PRO/PSSH
    # extraction above as the source of truth, then use the declared KID only as
    # a standards-compatible v4.0 header fallback below.
    playready_present = False
    candidate_kids: list[str] = []

    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag in {"pro", "pssh"} and (node.text or "").strip():
            try:
                blob = base64.b64decode(node.text.strip())
            except (ValueError, TypeError, binascii.Error):
                blob = b""
            if blob:
                decoded = blob.decode("utf-16-le", "ignore")
                found = _WRM_HEADER.search(decoded)
                if found:
                    return found.group(0)

        scheme = str(node.attrib.get("schemeIdUri") or "").strip().lower()
        if scheme == PLAYREADY_SCHEME_ID:
            playready_present = True
        for key, value in node.attrib.items():
            if key.rsplit("}", 1)[-1].lower() != "default_kid":
                continue
            for raw in re.split(r"[\s,]+", str(value)):
                kid = _canonical_uuid(raw)
                if kid and kid not in candidate_kids:
                    candidate_kids.append(kid)

    # ``default_KID`` is normally attached to the common
    # ``urn:mpeg:dash:mp4protection:2011`` ContentProtection node, so the
    # PlayReady sibling may have been visited before it.  Do a second pass for
    # the PlayReady scheme and recover KIDs from v1 PSSH boxes too.
    for node in root.iter():
        scheme = str(node.attrib.get("schemeIdUri") or "").strip().lower()
        if scheme != PLAYREADY_SCHEME_ID:
            continue
        playready_present = True
        for child in node:
            tag = child.tag.rsplit("}", 1)[-1].lower()
            if tag not in {"pro", "pssh"} or not (child.text or "").strip():
                continue
            for kid in _kids_from_playready_object(child.text.strip()):
                if kid not in candidate_kids:
                    candidate_kids.append(kid)

    if playready_present and candidate_kids:
        return _minimal_wrm_header(candidate_kids[0])
    return None


def _canonical_uuid(value: str) -> str:
    """Return a UUID string in canonical form, or an empty string."""
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError):
        return ""


def _kids_from_playready_object(value: str) -> list[str]:
    """Read KIDs from a v1 PlayReady PSSH even when its data payload is empty."""
    try:
        blob = base64.b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error):
        return []
    if len(blob) < 32 or blob[4:8] != b"pssh" or blob[8] != 1:
        return []
    system_id = blob[12:28].hex()
    if system_id != PLAYREADY_SCHEME_ID.rsplit(":", 1)[-1].replace("-", ""):
        return []
    count = int.from_bytes(blob[28:32], "big")
    end = 32 + count * 16
    if end > len(blob):
        return []
    return [
        str(uuid.UUID(bytes=blob[offset : offset + 16]))
        for offset in range(32, end, 16)
    ]


def _minimal_wrm_header(kid: str) -> str:
    """Render the v4.0 header accepted by services that publish KID-only PSSH."""
    value = base64.b64encode(uuid.UUID(kid).bytes_le).decode("ascii")
    return (
        '<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" '
        'version="4.0.0.0"><DATA><PROTECTINFO><KEYLEN>16</KEYLEN>'
        f"<ALGID>AESCTR</ALGID></PROTECTINFO><KID>{value}</KID></DATA></WRMHEADER>"
    )


def wrm_headers_from_kid_only_pssh(value: str) -> list[str]:
    """Standards fallback for a PlayReady v1 box with declared KIDs only."""
    kids = _kids_from_playready_object(value)
    if not kids:
        return []
    raw = base64.b64decode(value + "=" * (-len(value) % 4))
    offset = 32 + int.from_bytes(raw[28:32], "big") * 16
    if offset + 4 > len(raw) or int.from_bytes(raw[offset:offset + 4], "big") != 0:
        return []  # Do not replace a malformed provider-owned payload with invented XML.
    return [_minimal_wrm_header(kid) for kid in kids]


#: The SOAPAction every PlayReady licence server expects. Without it most return
#: 500 with an empty body, which reads like a network fault rather than a missing
#: header, so it is set for services rather than left to each of them.
SOAP_ACTION = '"http://schemas.microsoft.com/DRM/2007/03/protocols/AcquireLicense"'


def soap_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The headers a PlayReady licence POST needs."""
    headers = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": SOAP_ACTION}
    headers.update(extra or {})
    return headers


def wrm_headers_from_pssh(data: str | bytes) -> list[str]:
    """Every WRM header inside a base64 PlayReady object, PSSH box or header.

    pyplayready changed shape here: it used to expose
    ``PSSH(...).get_wrm_headers(downgrade_to_v4=False)`` and now exposes a
    ``wrm_headers`` list of ``WRMHeader`` objects. Both are handled, because the
    version installed is not something this code gets to decide.
    """
    require()
    from pyplayready.system.pssh import PSSH

    try:
        box = PSSH(data)
    except Exception as exc:
        raise PlayReadyUnavailable(f"Could not parse the PlayReady PSSH: {exc}") from exc

    headers = getattr(box, "wrm_headers", None)
    if headers is None:
        legacy = getattr(box, "get_wrm_headers", None)
        headers = legacy(downgrade_to_v4=False) if callable(legacy) else []

    found: list[str] = []
    for header in headers or []:
        # a WRMHeader in the new version, already a string in the old one
        text = header.dumps() if hasattr(header, "dumps") else str(header)
        if text and text not in found:
            found.append(text)
    return found


def canonical_kid(value: str) -> str:
    """A key id as 32 hex characters, from any of the forms in use.

    The same key id is written three ways along this path: base64 of a
    little-endian GUID in a v4.0 header, a dashed GUID in a v4.1 ``KID VALUE``,
    and a UUID in the licence's keys. Comparing any two of those as text makes
    every key look like it belongs to a key id nobody asked for, which is how a
    "missing key" is really a missing conversion.
    """
    text = (value or "").strip()
    if not text:
        return ""
    try:
        return uuid.UUID(text).hex
    except ValueError:
        pass
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, binascii.Error):
        return ""
    # base64 of a GUID stored little-endian, which is how PlayReady writes it
    return uuid.UUID(bytes_le=raw).hex if len(raw) == 16 else ""


def key_ids_from_header(header: str) -> list[str]:
    """The key ids a WRM header declares, canonicalised.

    Empty when the header cannot be read. That is a usable answer rather than an
    error: a header whose key ids are unknown is still worth an exchange, it just
    cannot be skipped as already covered.
    """
    if not header:
        return []
    found: list[str] = []
    if available():
        from pyplayready.system.wrmheader import WRMHeader

        try:
            for entry in WRMHeader(header).key_ids or []:
                kid = canonical_kid(str(getattr(entry, "value", entry) or ""))
                if kid and kid not in found:
                    found.append(kid)
        except Exception:  # noqa: BLE001 - the regex below reads the same thing
            found = []
    if found:
        return found
    raw = re.findall(r'<KID[^>]*VALUE="([^"]+)"', header, re.IGNORECASE)
    raw += re.findall(r"<KID[^>]*>([^<]+)</KID>", header, re.IGNORECASE)
    for value in raw:
        kid = canonical_kid(value)
        if kid and kid not in found:
            found.append(kid)
    return found


def headers_for_kids(
    headers: Iterable[str], kids: Iterable[str], *, keep_unknown: bool = False
) -> list[str]:
    """Keep original WRM headers that cover the requested canonical KIDs.

    Never rewrite a multi-key header: its original bytes may be signed or part
    of the provider's challenge contract. A shared header is one valid request,
    but its other KIDs must not expand the selected-track coverage requirement.
    """
    wanted = {canonical_kid(str(kid)) for kid in kids} - {""}
    found: list[str] = []
    for header in headers:
        text = str(header or "").strip()
        if not text or text in found:
            continue
        declared = set(key_ids_from_header(text))
        if (wanted and declared & wanted) or (keep_unknown and (not wanted or not declared)):
            found.append(text)
    return found


def dash_manifest_for_tracks(
    manifest: str | bytes, tracks: Sequence[object]
) -> bytes | None:
    """An in-memory DASH init-data view containing only selected representations.

    This does not edit the playback manifest or fetch media. Parser identities
    and declared KIDs associate representations with the selected inventory;
    if neither can prove an association, Core must use the selected init segment
    rather than fall back to unrelated MPD protection objects. Non-DASH inputs
    return None so HLS/ISM keep their own extraction path.
    """
    from xml.etree import ElementTree as ET

    try:
        root = ET.fromstring(manifest)
    except ET.ParseError:
        return None
    if root.tag.rsplit("}", 1)[-1] != "MPD":
        return None

    def children(node, name):
        return [child for child in node if child.tag.rsplit("}", 1)[-1] == name]

    def track_kids(track):
        extra = getattr(track, "extra", {}) or {}
        values = [extra.get("key_id"), *(extra.get("key_ids") or [])]
        values.extend(getattr(segment, "key_id", None) for segment in getattr(track, "segments", ()))
        return {canonical_kid(str(value or "")) for value in values} - {""}

    def protection_kids(node):
        found = set()
        for protection in children(node, "ContentProtection"):
            for attr, value in protection.attrib.items():
                if attr.rsplit("}", 1)[-1].lower() == "default_kid":
                    found.update(canonical_kid(kid) for kid in re.split(r"[\s,]+", value))
        return found - {""}

    inventory = []
    for track in tracks:
        if str(getattr(track, "manifest_type", "")).lower() != "dash":
            continue
        extra = getattr(track, "extra", {}) or {}
        members = extra.get("dash_period_tracks")
        if isinstance(members, list) and members:
            known = track_kids(track)
            for member in members:
                if not isinstance(member, dict) or member.get("period_id") is None:
                    continue
                inventory.append((
                    {str(member["period_id"])},
                    str(member.get("adaptation_id") or ""),
                    str(member.get("representation_id") or ""),
                    known,
                    True,
                ))
            continue
        period_ids = extra.get("period_ids") or [extra.get("period_id")]
        inventory.append((
            {str(value) for value in period_ids if value is not None},
            str(extra.get("dash_adaptation_id") or ""),
            str(getattr(track, "id", "") or ""),
            track_kids(track),
            False,
        ))

    periods = children(root, "Period") or [root]
    for index, period in enumerate(periods):
        period_id = period.get("id") or str(index)
        representation_ids = {
            (candidate.get("id") or "", node.get("id"))
            for candidate in children(period, "AdaptationSet")
            for node in children(candidate, "Representation")
        }
        period_representation_ids = {value for _, value in representation_ids}
        for adaptation in children(period, "AdaptationSet"):
            representations = children(adaptation, "Representation")
            kept = []
            for representation in representations or [adaptation]:
                declared = protection_kids(representation) or protection_kids(adaptation)
                for period_ids, adaptation_id, representation_id, known, exact in inventory:
                    if period_ids and period_id not in period_ids:
                        continue
                    identified = (
                        (adaptation_id, representation_id) in representation_ids
                        if adaptation_id else representation_id in period_representation_ids
                    )
                    if identified or (exact and (adaptation_id or representation_id)):
                        matches = (
                            (not adaptation_id or adaptation_id == adaptation.get("id"))
                            and bool(representation.get("id"))
                            and representation.get("id") == representation_id
                        )
                    else:
                        matches = bool(known & declared)
                    if matches:
                        kept.append(representation)
                        break
            if not kept:
                period.remove(adaptation)
            else:
                for representation in representations:
                    if representation not in kept:
                        adaptation.remove(representation)
        if period is not root and not children(period, "AdaptationSet"):
            root.remove(period)
    return ET.tostring(root, encoding="utf-8")


#: PlayReady's HLS key format. One name, unlike Widevine's urn, but still written
#: in either case by different services.
HLS_KEYFORMAT = "com.microsoft.playready"


def playready_objects_from_hls(manifest_text: str | bytes) -> list[str]:
    """Base64 PlayReady objects from an HLS playlist's key lines."""
    from . import pssh as pssh_tools

    return pssh_tools.init_data_from_hls(manifest_text, HLS_KEYFORMAT)


def playready_objects_from_manifest(manifest_text: str | bytes) -> list[str]:
    """Base64 PlayReady objects from DASH or Smooth Streaming, in document order.

    DASH carries ``pssh``/``pro`` under a PlayReady ``ContentProtection`` node;
    Smooth Streaming carries the same PlayReady object in ``ProtectionHeader``.
    A manifest can carry several - one per key id - so all are returned.
    """
    from xml.etree import ElementTree

    text = (
        manifest_text.decode("utf-8", "ignore")
        if isinstance(manifest_text, bytes)
        else manifest_text
    )
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        if not isinstance(manifest_text, bytes):
            return []
        from . import pssh as pssh_tools

        return pssh_tools.from_init_segment(
            manifest_text,
            PLAYREADY_SCHEME_ID.rsplit(":", 1)[-1],
        )

    wanted = PLAYREADY_SCHEME_ID.lower()
    wanted_guid = wanted.rsplit(":", 1)[-1].replace("-", "")
    found: list[str] = []
    for node in root.findall(".//{*}ContentProtection"):
        if (node.attrib.get("schemeIdUri") or "").strip().lower() != wanted:
            continue
        for child in node:
            tag = child.tag.rsplit("}", 1)[-1].lower()
            value = (child.text or "").strip()
            # `pssh` is the box; `pro` is the bare PlayReady object. Both parse.
            if tag in ("pssh", "pro") and value and value not in found:
                found.append(value)
    for node in root.findall(".//{*}ProtectionHeader"):
        system_id = str(
            node.attrib.get("SystemID")
            or node.attrib.get("SystemId")
            or node.attrib.get("systemId")
            or ""
        ).strip().strip("{}").lower().replace("-", "")
        value = (node.text or "").strip()
        if system_id == wanted_guid and value and value not in found:
            found.append(value)
    return found


def playready_pssh_from_mpd(manifest_text: str | bytes) -> list[str]:
    """Compatibility name for callers that originally handled DASH only."""
    return playready_objects_from_manifest(manifest_text)


LicenseTransport = Callable[[str], str]
"""Takes a challenge (str), returns the SOAP license response (str)."""


def get_keys(
    device_path: Path,
    wrm_header: str,
    transport: LicenseTransport,
    *,
    custom_data: str | None = None,
) -> list[str]:
    """Run one PlayReady exchange and return ``kid:key`` strings.

    Mirrors :func:`unidl.core.cdm.get_keys` so the engine does not care which
    DRM system a service uses.
    """
    require()
    from pyplayready.cdm import Cdm
    from pyplayready.device import Device

    path = Path(device_path)
    if not path.is_file():
        raise PlayReadyUnavailable(f"PlayReady device file not found: {path}")

    try:
        device = Device.load(str(path))
    except Exception as exc:
        raise PlayReadyUnavailable(f"Could not load {path.name}: {exc}") from exc

    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    try:
        challenge = cdm.get_license_challenge(
            session_id,
            wrm_header,
            custom_data=custom_data,
        )
        response = None
        try:
            response = transport(challenge)
            if not response:
                raise PlayReadyUnavailable("License server returned an empty response")
            cdm.parse_license(session_id, response)
        except Exception as exc:
            from .diagnostics import current_recorder

            recorder = current_recorder()
            if recorder is not None:
                recorder.record_license_failure(challenge, response, exc)
            raise
        keys = [f"{key.key_id.hex}:{key.key.hex()}" for key in cdm.get_keys(session_id)]
        if not keys:
            raise PlayReadyUnavailable("License parsed but contained no content keys")
        return keys
    finally:
        try:
            cdm.close(session_id)
        except Exception:
            pass


__all__ = [
    "INSTALL_HINT",
    "PLAYREADY_SCHEME_ID",
    "SOAP_ACTION",
    "PlayReadyUnavailable",
    "available",
    "canonical_kid",
    "get_keys",
    "dash_manifest_for_tracks",
    "headers_for_kids",
    "is_device",
    "key_ids_from_header",
    "playready_objects_from_manifest",
    "playready_pssh_from_mpd",
    "require",
    "soap_headers",
    "wrm_header_from_mpd",
    "wrm_headers_from_pssh",
    "wrm_headers_from_kid_only_pssh",
]
