"""PSSH and HLS key discovery.

Covers manifest-embedded data, ``pssh`` boxes inside an initialization segment,
KID-only DASH fallbacks, and ``EXT-X-KEY`` URIs inside HLS playlists. Services
with truly exotic sources still supply the PSSH themselves via ``DrmInfo.pssh``.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import urljoin
from uuid import UUID
from xml.etree import ElementTree as ET

WIDEVINE_SCHEME_ID = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
WIDEVINE_SYSTEM_ID = UUID(WIDEVINE_SCHEME_ID.rsplit(":", 1)[-1])
_KEY_URI_QUOTED = re.compile(r'URI="([^"]+)"')
_KEY_URI_BARE = re.compile(r"URI=([^,]+)")


def bmff_boxes(data: bytes | bytearray, box_type: bytes) -> list[bytes]:
    """Return validated ISO-BMFF boxes found anywhere in an init segment.

    This follows the reference project's marker scan rather than relying on a
    recursive MP4 walk: protection boxes may be nested more deeply than a parser
    chooses to traverse. Regular and 64-bit sizes are accepted; truncated or
    implausible matches are ignored.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("ISO-BMFF data must be bytes")
    if not isinstance(box_type, bytes) or len(box_type) != 4:
        raise ValueError("ISO-BMFF box type must be four bytes")

    raw = bytes(data)
    found: list[bytes] = []
    offset = 0
    while offset < len(raw):
        marker = raw.find(box_type, offset)
        if marker < 0:
            break
        start = marker - 4
        if start < 0:
            offset = marker + 4
            continue
        size32 = int.from_bytes(raw[start:marker], "big")
        header_size = 8
        if size32 == 1:
            if marker + 12 > len(raw):
                break
            size = int.from_bytes(raw[marker + 4 : marker + 12], "big")
            header_size = 16
        elif size32 == 0:
            size = len(raw) - start
        else:
            size = size32
        end = start + size
        if size < header_size or end > len(raw):
            offset = marker + 4
            continue
        box = raw[start:end]
        if box[4:8] == box_type and box not in found:
            found.append(box)
        offset = max(end, marker + 4)
    return found


def _pssh_system_id(box: bytes) -> bytes:
    if len(box) < 32 or box[4:8] != b"pssh":
        return b""
    header_size = 16 if int.from_bytes(box[:4], "big") == 1 else 8
    position = header_size + 4  # full-box version/flags precede the system id
    return box[position : position + 16]


def from_init_segment(
    data: bytes | bytearray,
    system_id: UUID | str | bytes = WIDEVINE_SYSTEM_ID,
) -> list[str]:
    """Return base64 PSSH boxes for one DRM system from initialization data."""
    if isinstance(system_id, UUID):
        wanted = system_id.bytes
    elif isinstance(system_id, str):
        wanted = UUID(system_id.strip().strip("{}").removeprefix("urn:uuid:")).bytes
    else:
        wanted = bytes(system_id)
    if len(wanted) != 16:
        raise ValueError("PSSH system id must be 16 bytes")
    return [
        base64.b64encode(box).decode()
        for box in bmff_boxes(data, b"pssh")
        if _pssh_system_id(box) == wanted
    ]


def key_ids_from_init_segment(data: bytes | bytearray) -> list[str]:
    """Return protected sample-entry KIDs from an ISO-BMFF init segment.

    A few DASH packagers omit both ``ContentProtection`` and a PSSH box while
    keeping the CENC default KID in ``tenc``.  That is enough to build a standard
    Widevine v1 request, but only when the init really declares protected samples.
    """
    found: list[str] = []
    for box in bmff_boxes(data, b"tenc"):
        for kid_offset in _tenc_kid_offsets(box):
            protected_offset = kid_offset - 2
            if kid_offset + 16 > len(box) or protected_offset >= len(box):
                continue
            if not box[protected_offset]:
                continue
            kid = box[kid_offset : kid_offset + 16].hex()
            if kid != "0" * 32 and kid not in found:
                found.append(kid)
            break
    return found


def init_segment_has_protection(data: bytes | bytearray) -> bool:
    """Whether an init segment declares a standard encrypted sample entry."""
    if bmff_boxes(data, b"pssh") or bmff_boxes(data, b"encv") or bmff_boxes(data, b"enca"):
        return True
    for box in bmff_boxes(data, b"tenc"):
        if any(kid_offset - 2 < len(box) and box[kid_offset - 2] for kid_offset in _tenc_kid_offsets(box)):
            return True
    return False


def _tenc_kid_offsets(box: bytes) -> tuple[int, ...]:
    """Accept standard tenc layout and the four-byte-prefixed variant in the wild."""
    version = box[8] if len(box) > 8 else 0
    if version == 1:
        return (16, 15)
    return (15, 16) if len(box) >= 32 else (15,)


def key_ids_from_mpd(manifest_text: str | bytes) -> list[str]:
    """Return distinct CENC default KIDs for the last-resort PSSH fallback."""
    try:
        root = ET.fromstring(manifest_text)
    except ET.ParseError:
        return []
    found: list[str] = []
    for node in root.findall(".//{*}ContentProtection"):
        for key, value in node.attrib.items():
            if key.rsplit("}", 1)[-1].lower() != "default_kid":
                continue
            for candidate in str(value).replace(",", " ").split():
                try:
                    rendered = UUID(candidate.strip().strip("{}")).hex
                except ValueError:
                    continue
                if rendered not in found:
                    found.append(rendered)
    return found


def from_key_ids(key_ids: list[str]) -> str | None:
    """Build a standard Widevine v1 PSSH when only trustworthy KIDs exist."""
    valid: list[UUID] = []
    for value in key_ids:
        try:
            key_id = UUID(str(value).strip().strip("{}"))
        except ValueError:
            continue
        if key_id.int and key_id not in valid:
            valid.append(key_id)
    if not valid:
        return None
    from pywidevine.pssh import PSSH

    return PSSH.new(PSSH.SystemId.Widevine, key_ids=valid, version=1).dumps()


def from_mpd(manifest_text: str | bytes) -> str | None:
    """Pull the first Widevine PSSH from DASH XML or raw init bytes."""
    try:
        root = ET.fromstring(manifest_text)
    except ET.ParseError:
        found = from_init_segment(manifest_text) if isinstance(manifest_text, bytes) else []
        return found[0] if found else None

    protections = [
        node
        for node in root.findall(".//{*}ContentProtection")
        if (node.get("schemeIdUri") or "").lower() == WIDEVINE_SCHEME_ID
    ]
    for node in protections:
        for child in node:
            if child.tag.rsplit("}", 1)[-1].lower() == "pssh" and (child.text or "").strip():
                return child.text.strip()
    return None


def all_from_mpd(manifest_text: str | bytes) -> list[str]:
    """Every distinct Widevine PSSH in a manifest, in document order."""
    try:
        root = ET.fromstring(manifest_text)
    except ET.ParseError:
        return []
    found: list[str] = []
    for node in root.findall(".//{*}ContentProtection"):
        if (node.get("schemeIdUri") or "").lower() != WIDEVINE_SCHEME_ID:
            continue
        for child in node:
            text = (child.text or "").strip()
            if child.tag.rsplit("}", 1)[-1].lower() == "pssh" and text and text not in found:
                found.append(text)
    return found


#: An HLS key line carrying DRM init data rather than an AES-128 key file. Both
#: tags matter: ``EXT-X-SESSION-KEY`` in a master playlist and ``EXT-X-KEY`` in a
#: variant, and a service may only have one of the two.
_HLS_KEY_TAG = re.compile(r"^#EXT-X-(?:SESSION-)?KEY[:,]", re.IGNORECASE)
_KEYFORMAT = re.compile(r'KEYFORMAT="?([^",]+)"?', re.IGNORECASE)


def _hls_key_lines(manifest_text: str | bytes) -> list[str]:
    text = (
        manifest_text.decode("utf-8", "ignore")
        if isinstance(manifest_text, (bytes, bytearray))
        else str(manifest_text or "")
    )
    if "#EXT" not in text:
        return []
    return [line.strip() for line in text.splitlines() if _HLS_KEY_TAG.match(line.strip())]


def init_data_from_hls(manifest_text: str | bytes, keyformat: str) -> list[str]:
    """Base64 init data from an HLS playlist's key lines, in playlist order.

    HLS keeps what DASH puts in a ``ContentProtection`` node inside an
    ``EXT-X-KEY`` attribute list: ``KEYFORMAT`` says which DRM system, and ``URI``
    is a ``data:`` URL whose base64 payload is the PSSH box or PlayReady object.
    Nothing about it is XML, which is why the manifest parsers here miss it
    entirely - the first HLS service with DRM finds no init data at all and
    reports it as a title with no protection.

    ``keyformat`` is matched loosely: services write the Widevine urn with and
    without its ``urn:uuid:`` prefix and in either case.
    """
    wanted = str(keyformat or "").strip().lower().removeprefix("urn:uuid:")
    found: list[str] = []
    for line in _hls_key_lines(manifest_text):
        if "METHOD=NONE" in line.upper():
            continue
        match = _KEYFORMAT.search(line)
        if not match:
            continue
        if match.group(1).strip().lower().removeprefix("urn:uuid:") != wanted:
            continue
        uri = _parse_key_uri(line) or ""
        if "base64," not in uri:
            continue
        blob = uri.split("base64,", 1)[1].strip().strip('"')
        if blob and blob not in found:
            found.append(blob)
    return found


def from_hls(manifest_text: str | bytes) -> str | None:
    """The first Widevine PSSH in an HLS playlist, or None."""
    found = init_data_from_hls(manifest_text, WIDEVINE_SCHEME_ID)
    return found[0] if found else None


def all_from_hls(manifest_text: str | bytes) -> list[str]:
    return init_data_from_hls(manifest_text, WIDEVINE_SCHEME_ID)


def _parse_key_uri(line: str) -> str | None:
    match = _KEY_URI_QUOTED.search(line)
    if match:
        return match.group(1).strip()
    match = _KEY_URI_BARE.search(line)
    if match:
        return match.group(1).strip().strip('"')
    return None


def hls_key_hex(
    playlist_url: str,
    fetch,
    *,
    max_playlists: int = 10,
) -> str | None:
    """Walk an HLS playlist tree and return the raw AES-128 key as hex.

    ``fetch(url) -> (status, text_or_bytes)`` is supplied by the caller so the
    service's authenticated session and headers are used.
    """
    visited: set[str] = set()
    queue = [playlist_url]

    while queue and len(visited) < max_playlists:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        status, body = fetch(current)
        if status != 200 or not body:
            continue
        text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else str(body)
        lines = [line.strip() for line in text.splitlines() if line.strip()]

        for line in lines:
            if not line.startswith("#EXT-X-KEY") or "URI=" not in line:
                continue
            key_uri = _parse_key_uri(line)
            if not key_uri:
                continue
            key_status, key_body = fetch(urljoin(current, key_uri), raw=True)
            if key_status != 200 or not key_body:
                continue
            if isinstance(key_body, str):
                key_body = key_body.encode()
            # a JSON body is a license response, not a raw key
            if key_body.lstrip()[:1] in (b"{", b"["):
                continue
            return key_body.hex()

        for line in lines:
            if line.startswith("#") or ".m3u8" not in line.lower():
                continue
            queue.append(urljoin(current, line))

    return None
