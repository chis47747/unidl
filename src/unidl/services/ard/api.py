"""ARD Mediathek catalogue, optional TV-code auth and clear HLS playback."""

from __future__ import annotations

import base64
import json
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter
from ...core.playback import SubtitleReference

WWW = "https://www.ardmediathek.de"
API = "https://api.ardmediathek.de"
PAGE = f"{API}/page-gateway"
SEARCH = f"{API}/search-system/search"
DEVICE_CODE = "https://accounts.ard.de/device/code?clientId=mt"
PROFILES = "https://accounts.ard.de/users"
REFRESH = "https://securetoken.googleapis.com/v1/token?key=AIzaSyCF2YOOMBFsvcJ764b3n2wDAf8cVZQlmgE"
IDENTITY_CUSTOM = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken"
IDENTITY_API_KEY = "AIzaSyCEvA_fVGNMRcS9F-Ubaaa0y0qBDUMlh90"
USER_AGENT = "ARD Mediathek Android / PlayStore,12.6.0,android-12,Generic"
DEFAULT_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
    "Client-Type": "AndroidTV",
    "User-Agent": USER_AGENT,
}

ARD_WEB_HOSTS = frozenset({"ardmediathek.de", "www.ardmediathek.de", "api.ardmediathek.de"})
ARD_API_HOSTS = frozenset({"api.ardmediathek.de"})
ARD_JSON_HOSTS = frozenset({"api.ardmediathek.de", "accounts.ard.de"})
DEVICE_LINK_HOSTS = frozenset(
    {"ardmediathek.de", "www.ardmediathek.de", "accounts.ard.de"}
)

SERIES_TYPES = frozenset({"SHOW", "SEASON_SERIES", "FINITE_SERIES", "INFINITE_SERIES", "SERIES", "SINGLE"})
VIDEO_TYPES = frozenset({
    "EPISODE", "SINGLE_VIDEO", "MOVIE", "CLIP", "DOCUMENTARY",
    "EXTRA_TRAILER", "EXTRA_MAKING_OF", "EXTRA_INTERVIEW", "EXTRA_BEHIND_THE_SCENES",
    "EXTRA_OUTTAKES", "EXTRA_UNCUT_CONTENT", "EXTRA_BONUS_CONTENT",
})
LIVE_TYPES = frozenset({"PERMANENT_LIVESTREAM", "LIVESTREAM", "EVENT_LIVESTREAM"})

EPISODE_NAME_RE = re.compile(
    r"^(Folge [0-9]+:)?(?P<name>[^\(]+) \(S(?P<season>[0-9]+)/E(?P<episode>[0-9]+)\).*$"
)

TOKEN_FILE = "ard_tv_auth.json"
PREVIOUS_TOKEN_FILE = ".ard_tv_auth.json"

DEFAULT_TIMEOUT = 25
SEARCH_PAGE_SIZE = 25
MAX_SEARCH_PAGES = 100
MAX_SUBTITLE_BYTES = 16 * 1024 * 1024


class ArdError(RuntimeError):
    """ARD Mediathek rejected a request or returned an unusable response."""


class PinRequired(ArdError):

    def __init__(self, item_id: str, partner: str = "ard", is_live: bool = False):
        super().__init__("This content requires an adult PIN")
        self.item_id = item_id
        self.partner = partner
        self.is_live = is_live


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


@dataclass(frozen=True)
class ContentRef:
    kind: str  # item | grouping | editorial | compilation | live | ""
    id: str
    partner: str = "ard"
    href: str = ""
    content_type: str = ""


@dataclass(frozen=True)
class SearchHit:
    title: str
    ref: ContentRef
    bucket: str = ""
    content_type: str = ""
    synopsis: str = ""
    core_asset_type: str = ""

    @property
    def label(self) -> str:
        return self.title

    @property
    def type_label(self) -> str:
        if self.bucket and self.content_type:
            return f"{self.bucket}/{self.content_type}"
        return self.bucket or self.content_type or self.core_asset_type or self.ref.kind

    @property
    def url(self) -> str:
        if self.ref.href:
            return self.ref.href
        if self.ref.kind == "grouping" and self.ref.id:
            return f"{WWW}/serie/x/{self.ref.id}"
        if self.ref.kind == "live" and self.ref.id:
            return f"{WWW}/live/{self.ref.partner}/{self.ref.id}"
        if self.ref.id:
            return f"{WWW}/video/x/{self.ref.id}"
        return WWW


@dataclass(frozen=True)
class SearchPage:
    hits: tuple[SearchHit, ...]
    page_number: int = 0
    has_next: bool = False
    total: int = 0


@dataclass(frozen=True)
class Episode:
    id: str
    partner: str
    series: str
    title: str
    season: int
    number: int
    audio_description: bool = False
    synopsis: str = ""
    year: str = ""

    @property
    def label(self) -> str:
        if self.number > 0:
            return f"E{self.number:02d}  {self.title}" if self.season <= 0 else (
                f"S{self.season:02d}E{self.number:02d}  {self.title}"
            )
        return self.title


@dataclass
class Season:
    number: int
    episodes: list[Episode] = field(default_factory=list)
    audio_description: list[Episode] = field(default_factory=list)

    @property
    def label(self) -> str:
        count = len(self.episodes)
        label = f"Season {self.number}  ·  {count} episode(s)"
        if self.audio_description:
            label += f"  ·  {len(self.audio_description)} Audiodeskription"
        return label


@dataclass
class Series:
    id: str
    partner: str
    title: str
    seasons: list[Season] = field(default_factory=list)


@dataclass(frozen=True)
class Channel:
    id: str
    partner: str
    name: str
    href: str = ""
    now: str = ""

    @property
    def label(self) -> str:
        return self.name


@dataclass(frozen=True)
class Video:
    id: str
    partner: str
    title: str
    year: str = ""
    synopsis: str = ""
    kind: str = "movie"  # movie | episode | clip | live
    season: int | None = None
    episode: int | None = None
    series: str = ""
    audio_description: bool = False
    is_live: bool = False
    duration: float | None = None


@dataclass(frozen=True)
class Source:
    manifest: str
    protocol: str = "HLS"
    title: str = ""
    is_live: bool = False
    item: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def line(self) -> str:
        bits = [self.protocol, "clear"]
        if self.is_live:
            bits.append("live")
        return " · ".join(bits)


@dataclass
class AuthState:
    id_token: str = ""
    refresh_token: str = ""
    refresh_url: str = REFRESH
    custom_token: str = ""
    user_id: str = ""
    profile: dict[str, Any] = field(default_factory=dict)
    jwt: dict[str, Any] = field(default_factory=dict)

    def as_cache(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.id_token:
            payload["idToken"] = self.id_token
        if self.refresh_token:
            payload["refreshToken"] = self.refresh_token
        if self.refresh_url:
            payload["refreshUrl"] = self.refresh_url
        if self.custom_token:
            payload["customToken"] = self.custom_token
        if self.user_id:
            payload["userId"] = self.user_id
        if self.profile:
            payload["profile"] = dict(self.profile)
        return payload

    @property
    def signed_in(self) -> bool:
        return bool(self.id_token or self.refresh_token)

    @property
    def display_name(self) -> str:
        return str(
            self.profile.get("displayName")
            or self.jwt.get("name")
            or "TV Profile"
        )

    @property
    def pin(self) -> str:
        value = self.profile.get("pin") or self.jwt.get("pin")
        return str(value).strip() if value not in (None, "") else ""


@dataclass
class DeviceCode:
    user_code: str
    verification_uri: str
    token_uri: str
    interval: int = 5
    expires_in: int = 180
    qr_code_uri: str = ""
    next_poll_at: float = 0.0


@dataclass(frozen=True)
class DevicePollFailure:
    """A terminal poll response carried through the generic Await driver."""

    message: str



def _has_https_authority(value: str, allowed_hosts: frozenset[str]) -> bool:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return bool(
        parsed.scheme.lower() == "https"
        and host in allowed_hosts
        and parsed.username is None
        and parsed.password is None
        and port is None
    )


def _is_ard_host(host: str) -> bool:
    return (host or "").lower() in ARD_WEB_HOSTS


def _require_json_url(url: str, *, authenticated: bool = False) -> None:
    if not _has_https_authority(url, ARD_JSON_HOSTS):
        purpose = "authenticated ARD" if authenticated else "ARD JSON"
        raise ArdError(f"Refusing {purpose} request outside trusted HTTPS authorities")


def _safe_device_link(value: str, *, qr: bool = False) -> bool:
    if _has_https_authority(value, DEVICE_LINK_HOSTS):
        return True
    if not qr or len(value) > 1_000_000:
        return False
    return bool(
        re.fullmatch(
            r"data:image/png;base64,[A-Za-z0-9+/]+={0,2}",
            value,
            re.IGNORECASE,
        )
    )


def decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
        data = json.loads(decoded.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (IndexError, TypeError, UnicodeError, ValueError):
        return {}


def token_is_fresh(id_token: str, leeway: int = 60) -> bool:
    payload = decode_jwt_payload(id_token)
    try:
        return int(payload.get("exp", 0)) > int(time.time()) + leeway
    except (TypeError, ValueError):
        return False


def auth_from_cache(data: dict[str, Any] | None) -> AuthState | None:
    data = data or {}
    id_token = str(data.get("idToken") or "")
    refresh = str(data.get("refreshToken") or "")
    if not id_token and not refresh:
        return None
    profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
    cached_refresh_url = str(data.get("refreshUrl") or "")
    state = AuthState(
        id_token=id_token,
        refresh_token=refresh,
        # The cache stores credentials, not authority. Never let a modified cache
        # choose where its refresh token is posted.
        refresh_url=REFRESH if cached_refresh_url != REFRESH else cached_refresh_url,
        custom_token=str(data.get("customToken") or ""),
        user_id=str(data.get("userId") or ""),
        profile=dict(profile or {}),
    )
    return _hydrate(state)


def _hydrate(state: AuthState) -> AuthState:
    state.jwt = decode_jwt_payload(state.id_token) if state.id_token else {}
    user_id = (
        state.user_id
        or state.jwt.get("user_id")
        or state.jwt.get("sub")
        or state.profile.get("uid")
        or ""
    )
    state.user_id = str(user_id or "")
    merged = {
        "uid": state.user_id,
        "displayName": state.jwt.get("name") or state.profile.get("displayName") or "TV Profile",
        "photoURL": state.jwt.get("picture") or state.profile.get("photoURL"),
        "pin": state.jwt.get("pin") or state.profile.get("pin"),
        "ageRating": state.jwt.get("age_rating") or state.profile.get("ageRating"),
    }
    for key, value in state.profile.items():
        if key not in merged or merged[key] in (None, ""):
            merged[key] = value
    state.profile = {k: v for k, v in merged.items() if v not in (None, "")}
    return state


def absolute_api_url(href: str) -> str:
    """Resolve an API href without permitting network-path or foreign URLs."""
    raw = str(href or "").strip()
    if not raw:
        return ""
    if raw.startswith("//") or "\\" in raw:
        raise ArdError("Refusing unsafe ARD API href")
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        if not _has_https_authority(raw, ARD_API_HOSTS):
            raise ArdError("Refusing ARD API href outside api.ardmediathek.de HTTPS")
        return raw
    resolved = f"{API}{raw}" if raw.startswith("/") else f"{API}/{raw}"
    if not _has_https_authority(resolved, ARD_API_HOSTS):
        raise ArdError("Refusing unsafe ARD API href")
    return resolved


def _normalize_web_id(value: str) -> str:
    return re.sub(r"-(movie|episode|show|season)$", "", str(value or "").strip())


def _is_likely_ard_id(value: str) -> bool:
    value = _normalize_web_id(value)
    if not value or value.isdigit():
        return False
    if value.startswith("crid:"):
        return True
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8", "ignore")
    except (TypeError, UnicodeError, ValueError):
        decoded = ""
    if decoded.startswith("crid://"):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{28,}", value)) and not bool(
        re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value)
    )


def _web_content_id(parts: list[str]) -> str:
    for segment in reversed(parts):
        candidate = _normalize_web_id(segment)
        if _is_likely_ard_id(candidate):
            return candidate
    return ""


def _empty_ref(raw: str, partner: str = "ard") -> ContentRef:
    return ContentRef(kind="", id="", partner=partner or "ard", href=raw)


def parse_content_reference(
    href: str,
    fallback_id: str = "",
    fallback_partner: str = "ard",
) -> ContentRef:
    """Parse an ARD web URL, API href, or bare content id."""
    raw = (href or "").strip()
    ref = ContentRef(
        kind="",
        id=fallback_id or "",
        partner=fallback_partner or "ard",
        href=raw,
        content_type="",
    )
    if not raw:
        return ref

    if "://" not in raw and "/" not in raw and _is_likely_ard_id(raw):
        return ContentRef(kind="item", id=_normalize_web_id(raw), partner=ref.partner, href=raw)

    if raw.startswith("//") or "\\" in raw:
        return _empty_ref(raw, ref.partner)
    direct = urlparse(raw)
    if direct.scheme and direct.scheme.lower() != "https":
        return _empty_ref(raw, ref.partner)

    is_relative_api = raw.startswith("/")
    candidate = raw if direct.scheme else (raw if is_relative_api else f"https://{raw}")
    parsed = urlparse(candidate)
    if not is_relative_api and not _has_https_authority(candidate, ARD_WEB_HOSTS):
        return _empty_ref(raw, ref.partner)
    path = unquote(parsed.path)
    if "\\" in path:
        return _empty_ref(raw, ref.partner)

    patterns = [
        ("item", r"/page-gateway/pages/([^/]+)/item/([^/?#]+)"),
        ("grouping", r"/page-gateway/pages/([^/]+)/grouping/([^/?#]+)"),
        ("editorial", r"/page-gateway/pages/([^/]+)/editorial/([^/?#]+)"),
        ("compilation", r"/page-gateway/pages/([^/]+)/compilation/([^/?#]+)"),
    ]
    for kind, pattern in patterns:
        match = re.search(pattern, path)
        if match:
            return ContentRef(
                kind=kind,
                partner=match.group(1),
                id=match.group(2),
                href=raw,
            )

    raw_candidate = _normalize_web_id(path.strip("/"))
    if raw_candidate and "/" not in raw_candidate and _is_likely_ard_id(raw_candidate):
        return ContentRef(kind="item", id=raw_candidate, partner=ref.partner, href=raw)

    segments = [segment for segment in path.split("/") if segment]
    is_ard_web = _is_ard_host(parsed.hostname or "")
    if segments and (is_ard_web or is_relative_api):
        content_type = segments[0]
        item_id = _web_content_id(segments[1:])
        kind = ""
        partner = ref.partner
        if content_type == "live":
            kind = "live"
            if len(segments) > 1:
                partner = segments[1]
        elif content_type in {"serie", "sendung"}:
            kind = "grouping"
        elif content_type in {"video", "film"}:
            kind = "item"
        return ContentRef(
            kind=kind,
            id=item_id or ref.id,
            partner=partner,
            href=raw,
            content_type=content_type,
        )

    return ref


def parse_input(text: str) -> ContentRef | None:
    raw = (text or "").strip()
    if not raw:
        return None
    ref = parse_content_reference(raw)
    if not ref.id and ref.kind not in {"editorial", "compilation"}:
        return None
    return ref


def teaser_title(teaser: dict[str, Any]) -> str:
    return str(
        teaser.get("longTitle")
        or teaser.get("title")
        or teaser.get("showTitle")
        or teaser.get("headline")
        or "Unknown"
    )


def _iter_image_records(value: Any, *, key_hint: str = "", depth: int = 0) -> list[tuple[dict[str, Any], str]]:
    """Flatten ARD image maps such as ``aspect16x9`` without scraping text."""
    if depth > 4:
        return []
    if isinstance(value, dict):
        if value.get("url") or value.get("src") or value.get("href"):
            return [(value, key_hint)]
        records: list[tuple[dict[str, Any], str]] = []
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                records.extend(
                    _iter_image_records(child, key_hint=str(key), depth=depth + 1)
                )
        return records
    if isinstance(value, list):
        records = []
        for child in value:
            records.extend(_iter_image_records(child, key_hint=key_hint, depth=depth + 1))
        return records
    return []


def _image_url(record: dict[str, Any]) -> str:
    raw = str(record.get("url") or record.get("src") or record.get("href") or "").strip()
    if "{width}" in raw:
        raw = raw.replace("{width}", "1920")
    if raw.startswith("/"):
        raw = f"{API}{raw}"
    if not _has_https_authority(raw, ARD_JSON_HOSTS):
        return ""
    return raw


def _image_kind(key_hint: str, record: dict[str, Any]) -> str:
    value = str(record.get("kind") or key_hint).casefold()
    if any(token in value for token in ("logo", "brand")):
        return "logo"
    if any(token in value for token in ("poster", "portrait")):
        return "poster"
    if any(token in value for token in ("preview", "thumb", "teaser")):
        return "thumbnail"
    if any(token in value for token in ("hero", "backdrop", "banner")):
        return "backdrop"
    return "artwork"


def _image_identity(url: str) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
    parsed = urlparse(url)
    query = tuple(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in {"w", "width"}
        )
    )
    return parsed.scheme, parsed.netloc, parsed.path, query


def _metadata_nodes(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Visit this player's metadata, never related-content teasers."""
    nodes = [item]
    pending = _as_dict_list(item.get("widgets"))
    while pending:
        widget = pending.pop(0)
        pending.extend(_as_dict_list(widget.get("widgets")))
        if str(widget.get("type") or "").casefold() in {"player_ondemand", "player_live"}:
            nodes.extend([widget, _as_dict(widget.get("show"))])
            collection = _as_dict(widget.get("mediaCollection"))
            collection = _as_dict(collection.get("embedded")) or collection
            nodes.extend([collection, _as_dict(collection.get("meta"))])
    if "streams" in item:
        nodes.append(_as_dict(item.get("meta")))
    return nodes


def extract_subtitles(item: dict[str, Any]) -> list[SubtitleReference]:
    """Expose each ARD subtitle once, preferring WebVTT to its EBU-TT copy."""
    if not isinstance(item, dict):
        return []
    references: list[SubtitleReference] = []
    seen: set[tuple[str, str, str]] = set()
    formats = {"webvtt": "vtt", "ebutt": "ttml"}
    for node in _metadata_nodes(item):
        for subtitle in _as_dict_list(node.get("subtitles")):
            language = str(subtitle.get("languageCode") or "und").strip() or "und"
            raw_kind = str(subtitle.get("kind") or "normal").strip().lower()
            kind = "cc" if raw_kind == "closed-captions" else raw_kind
            sources = _as_dict_list(subtitle.get("sources"))
            sources.sort(key=lambda source: source.get("kind") != "webvtt")
            for source in sources:
                fmt = formats.get(str(source.get("kind") or "").lower())
                url = str(source.get("url") or "").strip()
                if not fmt or not _has_https_authority(url, ARD_API_HOSTS):
                    continue
                identity = (url, language, kind)
                if identity not in seen:
                    seen.add(identity)
                    references.append(SubtitleReference(
                        url=url, language=language, kind=kind, name="ARD API",
                        format=fmt, headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                    ))
                break
    return references


def extract_attachments(item: dict[str, Any]) -> list[Attachment]:
    if not isinstance(item, dict):
        return []
    records: list[tuple[dict[str, Any], str]] = []
    for node in _metadata_nodes(item):
        for key in ("image", "images", "heroImage", "posters", "artwork"):
            records.extend(_iter_image_records(node.get(key), key_hint=key))

    attachments: list[Attachment] = []
    seen: set[tuple[str, str, str, tuple[tuple[str, str], ...]]] = set()
    for record, key_hint in records:
        url = _image_url(record)
        identity = _image_identity(url) if url else ()
        if not url or identity in seen:
            continue
        seen.add(identity)
        title = str(record.get("title") or record.get("alt") or "").strip()
        mime_type = str(record.get("mimeType") or "").split(";", 1)[0].strip().lower()
        if mime_type not in {"image/jpeg", "image/png", "image/webp", "image/avif"}:
            extension = urlparse(url).path.rsplit(".", 1)[-1].lower()
            mime_type = {"png": "image/png", "webp": "image/webp", "avif": "image/avif"}.get(extension, "image/jpeg")
        attachments.append(
            Attachment(
                url=url,
                name=title or f"ARD {_image_kind(key_hint, record).title()}",
                kind=_image_kind(key_hint, record),
                mime_type=mime_type,
            )
        )
    return attachments


def _jumpmarks(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        config
        for node in _metadata_nodes(item)
        if (config := _as_dict(_as_dict(node.get("pluginData")).get("jumpmarks@all")))
        and config.get("chaptersEnabled") is not False
    ]


def _jumpmark_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _as_dict_list(config.get("chapterArray"))
    if rows:
        return rows
    # EventStream scenes use relative seconds for VOD; EPG dates are not chapters.
    return [
        {"startSeconds": scene.get("offset"), "title": scene.get("title")}
        for event in _as_dict_list(_as_dict(config.get("eventStream")).get("events"))
        for scene in _as_dict_list(event.get("scene"))
    ]


def _chapter_value(row: dict[str, Any], names: tuple[str, ...]) -> tuple[Any, str]:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name], name.casefold()
    lowered = {str(key).casefold(): value for key, value in row.items()}
    for name in names:
        key = name.casefold()
        if key in lowered and lowered[key] not in (None, ""):
            return lowered[key], key
    return None, ""


def _chapter_time(value: Any, field_name: str) -> tuple[float | None, bool]:
    if value in (None, "") or isinstance(value, bool):
        return None, False
    lowered = field_name.casefold()
    seconds = not (lowered.endswith("ms") or "millisecond" in lowered)
    try:
        text = str(value).strip()
        if ":" in text and seconds:
            parts = text.split(":")
            if len(parts) not in {2, 3}:
                return None, seconds
            number = 0.0
            for part in parts:
                number = number * 60 + float(part)
        else:
            number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None, seconds
    if not math.isfinite(number) or number < 0:
        return None, seconds
    return number, seconds


def extract_chapters(item: dict[str, Any]) -> list[Chapter]:
    if not isinstance(item, dict):
        return []
    rows = [row for node in _metadata_nodes(item) for row in _as_dict_list(node.get("chapters"))]
    for config in _jumpmarks(item):
        rows.extend(_jumpmark_rows(config))
    chapters: list[Chapter] = []
    for row in rows:
        title = str(row.get("chapterTitle") or row.get("title") or row.get("name") or row.get("label") or "").strip()
        kind = str(row.get("kind") or row.get("type") or "").strip()
        if not title and not kind:
            continue
        start_raw, start_name = _chapter_value(
            row,
            ("chapterTime", "startMs", "startMilliseconds", "positionMs", "offsetMs", "startSeconds", "positionSeconds", "startTime", "start"),
        )
        end_raw, end_name = _chapter_value(
            row,
            ("endMs", "endMilliseconds", "endSeconds", "endTime", "end"),
        )
        start, start_seconds = _chapter_time(start_raw, start_name)
        if start is None:
            continue
        if start_name == "chaptertime" and start >= 1_000_000_000:
            continue
        end, end_seconds = _chapter_time(end_raw, end_name)
        try:
            chapter = Chapter(
                start_ms=round(start * 1000) if start_seconds else round(start),
                title=title,
                end_ms=(round(end * 1000) if end_seconds else round(end)) if end is not None else None,
                kind=kind,
            )
        except (TypeError, ValueError, OverflowError):
            continue
        chapters.append(chapter)
    return chapters


def duration_seconds(item: dict[str, Any]) -> float | None:
    if not isinstance(item, dict):
        return None
    values = [node.get(key) for node in _metadata_nodes(item) for key in ("duration", "durationSeconds")]
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, bool) and math.isfinite(number) and number > 0:
            return number
    return None


def is_audio_description_teaser(teaser: dict[str, Any]) -> bool:
    text = " ".join(
        str(teaser.get(field) or "")
        for field in (
            "longTitle",
            "title",
            "subtitle",
            "teaserText",
            "description",
            "widgetTitle",
        )
    ).lower()
    tokens = (
        "hörfassung",
        "audiodeskription",
        "audio description",
        "audiodescription",
        "audio-description",
    )
    if any(token in text for token in tokens):
        return True
    for audio in _as_dict_list(teaser.get("audios")):
        audio_text = " ".join(
            str(audio.get(field) or "") for field in ("kind", "label", "title")
        ).lower()
        if any(
            token in audio_text
            for token in ("audio-description", "audiodescription", "audiodeskription")
        ):
            return True
    return False


def media_audio_role(media: dict[str, Any]) -> str:
    if media.get("isAudioDescription") is True or media.get("audioDescription") is True:
        return "audio-description"
    audios = _as_dict_list(media.get("audios"))
    audio_text = " ".join(
        " ".join(str(audio.get(field) or "") for field in ("kind", "role", "label", "title"))
        for audio in audios
    ).casefold()
    url_text = str(media.get("url") or "").casefold()
    description_tokens = (
        "audio-description",
        "audiodescription",
        "audiodeskription",
        "hörfassung",
    )
    if any(token in audio_text or token in url_text for token in description_tokens):
        return "audio-description"
    if "standard" in audio_text:
        return "standard"
    if "speech-optimized" in audio_text or "klare sprache" in audio_text:
        return "speech-optimized"
    return ""


def media_score(
    media: dict[str, Any],
    *,
    is_live: bool = False,
    audio_description: bool = False,
) -> tuple[int, int, int]:
    """Lexicographic stream preference: protocol, audio role, resolution."""
    mime = str(media.get("mimeType") or media.get("mime") or "").lower().split(";", 1)[0].strip()
    url = str(media.get("url") or "").lower().split("?", 1)[0]
    hls_mimes = {
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "audio/mpegurl",
        "audio/x-mpegurl",
    }
    if mime in hls_mimes or url.endswith(".m3u8"):
        protocol_rank = 3
    elif mime == "application/dash+xml" or url.endswith(".mpd"):
        protocol_rank = 2
    elif "mp4" in mime or url.endswith(".mp4"):
        protocol_rank = 1
    else:
        protocol_rank = 0

    role = media_audio_role(media)
    if role == "audio-description":
        audio_rank = 2 if audio_description else 0
    elif role == "standard":
        audio_rank = 0 if audio_description else 2
    else:
        audio_rank = 1
    try:
        resolution = int(media.get("maxHResolutionPx") or media.get("maxResolution") or 0)
    except (TypeError, ValueError):
        resolution = 0
    return protocol_rank, audio_rank, resolution


def select_stream_url(
    media_collection: dict[str, Any],
    *,
    is_live: bool = False,
    audio_description: bool = False,
) -> str | None:
    """Pick one stream while keeping standard and AD editions distinct."""
    candidates: list[tuple[tuple[int, int, int], int, str]] = []
    order = 0
    for stream_collection in _as_dict_list(media_collection.get("streams")):
        kind = stream_collection.get("kind")
        if kind and kind != "main":
            continue
        for media in _as_dict_list(stream_collection.get("media")):
            if not isinstance(media, dict):
                continue
            url = media.get("url")
            if not url:
                continue
            is_ad = media_audio_role(media) == "audio-description"
            if is_ad != audio_description:
                continue
            candidates.append(
                (
                    media_score(
                        media,
                        is_live=is_live,
                        audio_description=audio_description,
                    ),
                    -order,
                    str(url),
                )
            )
            order += 1
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]


def protocol_for(url: str) -> str:
    lower = (url or "").lower()
    if ".mpd" in lower or "dash" in lower:
        return "DASH"
    if ".m3u8" in lower or "mpegurl" in lower or "hls" in lower:
        return "HLS"
    if ".mp4" in lower:
        return "MP4"
    return "HLS"


def _first_int(*values: Any) -> int | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


class ArdApi:
    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        auth: AuthState | None = None,
        on_save: Any = None,
    ):
        self.http = session or requests.Session()
        self.http.headers.update(DEFAULT_HEADERS)
        self.auth = auth or AuthState()
        self.on_save = on_save  # callable[[AuthState], None] | None
        self.search_warnings: list[str] = []

    def set_auth(self, auth: AuthState | None) -> None:
        self.auth = _hydrate(auth) if auth else AuthState()

    def _save(self) -> None:
        if self.on_save is not None:
            self.on_save(self.auth)

    def _x_auth_headers(self) -> dict[str, str]:
        token = self.auth.id_token
        if not token:
            return {}
        return {
            "X-Authorization": f"Bearer {token}",
            "Cache-control": "max-age=0, no-cache, no-store",
        }

    def ensure_token(self) -> bool:
        if self.auth.id_token and token_is_fresh(self.auth.id_token):
            return True
        return self.refresh() or self.sign_in_with_custom_token()

    def refresh(self) -> bool:
        refresh_token = self.auth.refresh_token
        if not refresh_token:
            return False
        refresh_url = REFRESH
        try:
            response = self.http.post(
                refresh_url,
                data={"refresh_token": refresh_token, "grant_type": "refresh_token"},
                timeout=20,
                allow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                return False
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        id_token = data.get("id_token") or data.get("idToken")
        if not id_token:
            return False
        self.auth.id_token = str(id_token)
        self.auth.refresh_token = str(
            data.get("refresh_token") or data.get("refreshToken") or refresh_token
        )
        self.auth.refresh_url = refresh_url
        self.auth.user_id = str(
            data.get("user_id") or data.get("userId") or self.auth.user_id
        )
        _hydrate(self.auth)
        self._save()
        return True

    def sign_in_with_custom_token(self) -> bool:
        custom = self.auth.custom_token
        if not custom:
            return False
        try:
            response = self.http.post(
                IDENTITY_CUSTOM,
                params={"key": IDENTITY_API_KEY},
                json={"token": custom, "returnSecureToken": True},
                headers={
                    "Referer": "https://accounts.ard.de/",
                    "Content-Type": "application/json",
                },
                timeout=20,
                allow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                return False
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        id_token = data.get("idToken")
        refresh_token = data.get("refreshToken")
        if not id_token or not refresh_token:
            return False
        token_profile = decode_jwt_payload(str(id_token))
        self.auth.id_token = str(id_token)
        self.auth.refresh_token = str(refresh_token)
        self.auth.refresh_url = REFRESH
        self.auth.user_id = str(
            token_profile.get("user_id") or token_profile.get("sub") or self.auth.user_id
        )
        _hydrate(self.auth)
        self._save()
        return True

    def start_device_code(self) -> DeviceCode:
        try:
            response = self.http.get(DEVICE_CODE, timeout=20, allow_redirects=False)
            if 300 <= response.status_code < 400:
                raise ArdError("ARD TV login endpoint attempted a redirect")
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise ArdError(f"Could not create ARD TV login code: {exc}") from exc
        except ValueError as exc:
            raise ArdError("ARD TV login code response was not JSON") from exc
        if not isinstance(data, dict):
            raise ArdError("ARD TV login code response was not an object")
        user_code = str(data.get("user_code") or "")
        token_uri = str(data.get("token_uri") or "")
        verification_uri = str(data.get("verification_uri") or "")
        qr_code_uri = str(data.get("qr_code_uri") or "")
        if not user_code or not token_uri:
            raise ArdError("ARD TV login response was incomplete")
        if not _has_https_authority(token_uri, frozenset({"accounts.ard.de"})):
            raise ArdError("ARD TV login returned an untrusted token authority")
        if verification_uri and not _safe_device_link(verification_uri):
            raise ArdError("ARD TV login returned an untrusted verification authority")
        if qr_code_uri and not _safe_device_link(qr_code_uri, qr=True):
            raise ArdError("ARD TV login returned an untrusted QR authority")
        if not verification_uri and not qr_code_uri:
            raise ArdError("ARD TV login response had no trusted verification link")
        try:
            interval = max(2, int(data.get("interval") or 5))
            expires_in = max(1, int(data.get("expires_in") or 180))
        except (TypeError, ValueError) as exc:
            raise ArdError("ARD TV login code response had invalid timing") from exc
        return DeviceCode(
            user_code=user_code,
            verification_uri=verification_uri,
            token_uri=token_uri,
            interval=interval,
            expires_in=expires_in,
            qr_code_uri=qr_code_uri,
        )

    def poll_device_code(self, device: DeviceCode) -> AuthState | None:
        if not _has_https_authority(device.token_uri, frozenset({"accounts.ard.de"})):
            raise ArdError("Refusing untrusted ARD TV token authority")
        now = time.monotonic()
        if device.next_poll_at and now < device.next_poll_at:
            return None
        device.next_poll_at = now + device.interval
        try:
            response = self.http.get(device.token_uri, timeout=20, allow_redirects=False)
        except requests.RequestException:
            return None
        if 300 <= response.status_code < 400:
            raise ArdError("ARD TV token endpoint attempted a redirect")
        try:
            data = response.json() if response.text else {}
        except ValueError as exc:
            raise ArdError("ARD TV token response was not JSON") from exc
        if not isinstance(data, dict):
            raise ArdError("ARD TV token response was not an object")
        id_token = data.get("idToken")
        refresh_token = data.get("refreshToken")
        if id_token and refresh_token:
            self.auth = _hydrate(
                AuthState(
                    id_token=str(id_token),
                    refresh_token=str(refresh_token),
                    refresh_url=REFRESH,
                    custom_token=str(data.get("customToken") or ""),
                )
            )
            self.fetch_profiles()
            self._save()
            return self.auth
        error = str(data.get("error") or "").strip()
        if error == "slow_down":
            device.interval = min(60, max(2, device.interval + 5))
            device.next_poll_at = now + device.interval
        elif error and error not in {"authorization_pending", "pending"}:
            raise ArdError(f"ARD TV login error: {error}")
        return None

    def fetch_profiles(self) -> list[dict[str, Any]]:
        if not self.auth.id_token:
            return []
        data = self.get_json(PROFILES, authenticated=True)
        if isinstance(data, list):
            profiles = data
        elif isinstance(data, dict):
            profiles = data.get("profiles") or data.get("users") or data.get("items") or []
        else:
            raise ArdError("ARD profile response was not a list or object")
        profiles = _as_dict_list(profiles)
        if not profiles:
            raise ArdError("ARD profile response contained no profiles")
        current_uid = self.auth.user_id or self.auth.profile.get("uid")
        selected = next((profile for profile in profiles if profile.get("uid") == current_uid), None)
        if not selected:
            selected = next((profile for profile in profiles if not profile.get("isSubUser")), None)
        if not selected:
            selected = profiles[0]
        self.auth.profile = {**self.auth.profile, **selected}
        if self.auth.profile.get("uid"):
            self.auth.user_id = str(self.auth.profile["uid"])
        _hydrate(self.auth)
        self._save()
        return profiles

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = False,
    ) -> Any:
        _require_json_url(url, authenticated=authenticated)
        request_headers = dict(headers or {})
        if authenticated:
            if not self.ensure_token():
                raise ArdError("ARD TV session is not signed in")
            request_headers.update(self._x_auth_headers())
        try:
            response = self.http.get(
                url,
                params=params,
                headers=request_headers or None,
                timeout=DEFAULT_TIMEOUT,
                allow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                raise ArdError("ARD JSON request attempted a redirect")
            if authenticated and response.status_code == 401:
                if self.refresh() or self.sign_in_with_custom_token():
                    request_headers.update(self._x_auth_headers())
                    response = self.http.get(
                        url,
                        params=params,
                        headers=request_headers or None,
                        timeout=DEFAULT_TIMEOUT,
                        allow_redirects=False,
                    )
                    if 300 <= response.status_code < 400:
                        raise ArdError("Authenticated ARD retry attempted a redirect")
            if not response.ok:
                detail = (response.text or "")[:180]
                raise ArdError(f"ARD returned HTTP {response.status_code}: {detail}")
            return response.json()
        except requests.RequestException as exc:
            raise ArdError(f"ARD request failed: {exc}") from exc
        except ValueError as exc:
            raise ArdError("ARD returned something that is not JSON") from exc

    def item_params(self, *, authenticated: bool = False) -> dict[str, str]:
        params = {"mcV6": "true", "devicetype": "mobile"}
        user_id = self.auth.user_id or self.auth.profile.get("uid") or ""
        if authenticated and user_id:
            params["userId"] = str(user_id)
            params["random"] = secrets.token_hex(8)
        return params

    def fetch_item(
        self,
        item_id: str,
        partner: str = "ard",
        *,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        channel = "ard" if authenticated else partner or "ard"
        url = f"{PAGE}/pages/{channel}/item/{item_id}"
        data = self.get_json(
            url,
            params=self.item_params(authenticated=authenticated),
            authenticated=authenticated,
        )
        if not isinstance(data, dict):
            raise ArdError(f"ARD returned no item {item_id}")
        return data

    def fetch_series(self, item_id: str, partner: str = "ard", *, single: bool = False) -> dict[str, Any]:
        url = f"{PAGE}/pages/ard/grouping/{item_id}"
        data = self.get_json(
            url,
            params={"seasoned": "true", "single": str(single).lower()},
        )
        if not isinstance(data, dict):
            raise ArdError(f"ARD returned no series {item_id}")
        return data

    def fetch_page(self, href: str) -> dict[str, Any]:
        url = absolute_api_url(href)
        if not url:
            raise ArdError("Empty ARD page href")
        params: dict[str, str] = {}
        if "embedded=" not in url:
            params["embedded"] = "true"
        if "devicetype=" not in url:
            params["devicetype"] = "mobile"
        data = self.get_json(url, params=params or None)
        if not isinstance(data, dict):
            raise ArdError("ARD page response was empty")
        return data

    def teaser_reference(self, teaser: dict[str, Any]) -> ContentRef:
        links = _as_dict(teaser.get("links"))
        target = _as_dict(links.get("target"))
        self_link = _as_dict(links.get("self"))
        publication_service = _as_dict(teaser.get("publicationService"))
        href = str(target.get("href") or self_link.get("href") or "")
        fallback_partner = str(
            target.get("partner") or publication_service.get("partner") or "ard"
        )
        fallback_id = str(target.get("id") or teaser.get("id") or "")
        ref = parse_content_reference(href, fallback_id, fallback_partner)
        teaser_type = str(teaser.get("type") or "").lower()
        core_type = str(teaser.get("coreAssetType") or "").upper()
        if teaser_type == "live" or core_type in LIVE_TYPES:
            ref = ContentRef(
                kind="live",
                id=ref.id,
                partner=ref.partner,
                href=ref.href,
                content_type=ref.content_type,
            )
        return ref

    def extract_teasers(self, page: dict[str, Any]) -> list[dict[str, Any]]:
        teasers: list[dict[str, Any]] = []
        if not isinstance(page, dict) or not page:
            return teasers
        teasers.extend(_as_dict_list(page.get("teasers")))
        pending = _as_dict_list(page.get("widgets"))
        while pending:
            widget = pending.pop(0)
            teasers.extend(_as_dict_list(widget.get("teasers")))
            pending.extend(_as_dict_list(widget.get("widgets")))
        return teasers

    def search(self, query: str) -> list[SearchHit]:
        results: list[SearchHit] = []
        seen: set[tuple[Any, ...]] = set()
        errors: list[tuple[str, ArdError]] = []
        self.search_warnings = []
        for kind, bucket in (("vods", "VOD"), ("shows", "SHOW")):
            try:
                teasers = self._search_bucket(query, kind, bucket)
            except ArdError as exc:
                errors.append((bucket, exc))
                continue
            for teaser in teasers:
                ref = self.teaser_reference(teaser)
                if not ref.id and not ref.href:
                    continue
                if ref.id:
                    key = ("ref", ref.kind, ref.partner, ref.id)
                elif ref.href:
                    key = ("href", ref.href)
                else:
                    key = ("title", teaser_title(teaser), str(teaser.get("coreAssetType") or ""))
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    SearchHit(
                        title=teaser_title(teaser),
                        ref=ref,
                        bucket=bucket,
                        content_type=str(
                            teaser.get("coreAssetType") or teaser.get("type") or ""
                        ),
                        synopsis=str(
                            teaser.get("teaserText")
                            or teaser.get("synopsis")
                            or teaser.get("description")
                            or ""
                        ),
                        core_asset_type=str(teaser.get("coreAssetType") or ""),
                    )
                )
        if len(errors) == 2:
            summary = "; ".join(f"{bucket}: {error}" for bucket, error in errors)
            raise ArdError(f"ARD search failed in all result buckets ({summary})")
        if errors:
            failed = ", ".join(bucket for bucket, _error in errors)
            self.search_warnings.append(f"ARD search returned partial results; {failed} failed")
        return results

    def search_page(self, query: str, page_number: int = 0) -> SearchPage:
        if page_number < 0 or page_number >= MAX_SEARCH_PAGES:
            raise ArdError("ARD search page number is outside the supported range")
        if page_number == 0:
            self.search_warnings = []

        results: list[SearchHit] = []
        seen: set[tuple[Any, ...]] = set()
        errors: list[tuple[str, ArdError]] = []
        total = 0
        has_next = False
        for kind, bucket in (("vods", "VOD"), ("shows", "SHOW")):
            try:
                teasers, bucket_has_next, bucket_total = self._search_bucket_page(
                    query, kind, bucket, page_number
                )
            except ArdError as exc:
                errors.append((bucket, exc))
                continue
            self._append_search_hits(results, seen, teasers)
            has_next = has_next or bucket_has_next
            total += bucket_total
        if len(errors) == 2:
            summary = "; ".join(f"{bucket}: {error}" for bucket, error in errors)
            raise ArdError(f"ARD search failed in all result buckets ({summary})")
        if errors:
            failed = ", ".join(bucket for bucket, _error in errors)
            self.search_warnings.append(
                f"ARD search returned partial results; {failed} failed on page {page_number + 1}"
            )
        return SearchPage(tuple(results), page_number, has_next, total)

    def _append_search_hits(
        self,
        results: list[SearchHit],
        seen: set[tuple[Any, ...]],
        teasers: list[dict[str, Any]],
    ) -> None:
        for teaser in teasers:
            ref = self.teaser_reference(teaser)
            if not ref.id and not ref.href:
                continue
            if ref.id:
                key = ("ref", ref.kind, ref.partner, ref.id)
            elif ref.href:
                key = ("href", ref.href)
            else:
                key = ("title", teaser_title(teaser), str(teaser.get("coreAssetType") or ""))
            if key in seen:
                continue
            seen.add(key)
            results.append(
                SearchHit(
                    title=teaser_title(teaser),
                    ref=ref,
                    bucket=str(teaser.get("_searchBucket") or ""),
                    content_type=str(
                        teaser.get("coreAssetType") or teaser.get("type") or ""
                    ),
                    synopsis=str(
                        teaser.get("teaserText")
                        or teaser.get("synopsis")
                        or teaser.get("description")
                        or ""
                    ),
                    core_asset_type=str(teaser.get("coreAssetType") or ""),
                )
            )

    def _search_bucket(self, query: str, kind: str, bucket: str) -> list[dict[str, Any]]:
        return self._search_bucket_page(query, kind, bucket, 0)[0]

    def _search_bucket_page(
        self,
        query: str,
        kind: str,
        bucket: str,
        page_number: int,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        params = {
            "query": query,
            "pageNumber": page_number,
            "pageSize": SEARCH_PAGE_SIZE,
            "subtitle": "false",
            "audioDes": "false",
            "signLang": "false",
            "childCont": "false",
            "familyOrChildCont": "false",
            "platform": "MEDIA_THEK",
            "sortingCriteria": "SCORE_DESC",
        }
        headers = {
            "Referer": "ard",
            "Client-Type": "AndroidTV",
        }
        data = self.get_json(
            f"{SEARCH}/{kind}/ard",
            params=params,
            headers=headers,
        )
        if not isinstance(data, dict):
            raise ArdError(f"ARD {bucket} search returned a non-object response")
        teasers: list[dict[str, Any]] = []
        for section in ("topResults", "teasers"):
            value = (data or {}).get(section) or []
            if isinstance(value, dict):
                value = value.get("teasers") or value.get("items") or []
            for teaser in _as_dict_list(value):
                copied = dict(teaser)
                copied["_searchBucket"] = bucket
                teasers.append(copied)
        pagination = data.get("pagination")
        if not isinstance(pagination, dict):
            return teasers, False, len(teasers)
        try:
            returned_page = int(pagination.get("pageNumber", page_number))
            page_size = int(pagination.get("pageSize", SEARCH_PAGE_SIZE))
            total = int(pagination.get("totalElements", len(teasers)))
        except (TypeError, ValueError) as exc:
            raise ArdError(f"ARD {bucket} search returned invalid pagination") from exc
        if returned_page != page_number or page_size <= 0 or total < 0:
            raise ArdError(f"ARD {bucket} search returned inconsistent pagination")
        explicit = pagination.get("hasNext")
        if explicit is None:
            explicit = pagination.get("hasNextPage")
        if isinstance(explicit, bool):
            has_next = explicit
        elif explicit is None:
            has_next = total > (page_number + 1) * page_size
        else:
            normalized = str(explicit).strip().lower()
            if normalized not in {"true", "false"}:
                raise ArdError(f"ARD {bucket} search returned invalid next-page flag")
            has_next = normalized == "true"
        return teasers, has_next, total

    def channels(self) -> list[Channel]:
        data = self.get_json(
            f"{PAGE}/static/livestreams",
        )
        if not isinstance(data, dict):
            raise ArdError("ARD live catalogue returned a non-object response")
        teasers = self.extract_teasers(data)
        channels: list[Channel] = []
        seen: set[tuple[str, str]] = set()
        for teaser in teasers:
            ref = self.teaser_reference(teaser)
            publication = _as_dict(teaser.get("publicationService"))
            programme = teaser_title(teaser)
            name = str(
                publication.get("name")
                or publication.get("title")
                or teaser.get("channelName")
                or programme
                or teaser.get("id")
                or "Livestream"
            )
            now = str(teaser.get("subtitle") or "")
            if not now and programme not in {"", "Unknown", name}:
                now = programme
            if not ref.id:
                continue
            key = (ref.partner or "ard", ref.id)
            if key in seen:
                continue
            seen.add(key)
            channels.append(
                Channel(
                    id=ref.id,
                    partner=ref.partner or "ard",
                    name=name,
                    href=ref.href,
                    now=now,
                )
            )
        return channels

    def _widget_teasers(self, widget: dict[str, Any]) -> list[dict[str, Any]]:
        rows = _as_dict_list(widget.get("teasers"))
        page = widget
        self_link = _as_dict(_as_dict(widget.get("links")).get("self"))
        href = str(self_link.get("href") or "")
        for _ in range(MAX_SEARCH_PAGES):
            pagination = _as_dict(page.get("pagination"))
            if not pagination:
                return rows
            try:
                number = int(pagination.get("pageNumber", 0))
                size = int(pagination.get("pageSize", len(rows)))
                total = int(pagination.get("totalElements", len(rows)))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ArdError("ARD series returned invalid pagination") from exc
            if number < 0 or size <= 0 or total < 0:
                raise ArdError("ARD series returned invalid pagination")
            if (number + 1) * size >= total:
                return rows
            if not href:
                raise ArdError("ARD series is incomplete: missing next-page link")
            parsed = urlparse(absolute_api_url(href))
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            query.update(pageNumber=str(number + 1), pageSize=str(size))
            url = urlunparse(parsed._replace(query=urlencode(query)))
            page = self.get_json(url)
            if not isinstance(page, dict):
                raise ArdError("ARD series page was not an object")
            if _as_dict(page.get("pagination")).get("pageNumber") != number + 1:
                raise ArdError("ARD series pagination did not advance")
            more = _as_dict_list(page.get("teasers"))
            if not more:
                raise ArdError("ARD series returned an empty continuation page")
            rows.extend(more)
        raise ArdError("ARD series exceeded the pagination limit")

    def series(self, item_id: str, partner: str = "ard", *, single: bool = False) -> Series:
        data = self.fetch_series(item_id, partner, single=True) if single else self.fetch_series(item_id, partner)
        pub = _as_dict(data.get("publicationService"))
        series_title = str(pub.get("title") or data.get("title") or "Unknown Series")
        episodes_by_season: dict[int, list[Episode]] = {}
        ad_by_season: dict[int, list[Episode]] = {}
        seen: set[Any] = set()
        seen_ad: set[Any] = set()

        pending_widgets = _as_dict_list(data.get("widgets"))
        while pending_widgets:
            widget = pending_widgets.pop(0)
            pending_widgets.extend(_as_dict_list(widget.get("widgets")))
            if str(widget.get("type") or "").lower() != "gridlist":
                continue
            compilation_type = str(widget.get("compilationType") or "").casefold()
            if compilation_type not in {"", "itemsofshow", "itemsofseason", "playlist"}:
                continue
            for teaser in self._widget_teasers(widget):
                if str(teaser.get("coreAssetType") or "").upper() not in VIDEO_TYPES:
                    continue
                is_ad = widget.get("withAudiodescription") is True or is_audio_description_teaser(
                    {**teaser, "widgetTitle": widget.get("title") or ""}
                )
                long_title = str(teaser.get("longTitle") or teaser.get("title") or "")
                match = EPISODE_NAME_RE.match(long_title)
                if match:
                    season = int(match.group("season"))
                    episode_no = int(match.group("episode"))
                    name = match.group("name").strip()
                else:
                    parsed_season = _first_int(teaser.get("seasonNumber"), widget.get("seasonNumber"))
                    parsed_episode = _first_int(teaser.get("episodeNumber"))
                    season = parsed_season if parsed_season is not None else 1
                    episode_no = parsed_episode if parsed_episode is not None else 0
                    name = long_title or teaser_title(teaser)

                show = teaser.get("showTitle") or _as_dict(teaser.get("show")).get("title")
                if show and (not series_title or series_title == "Unknown Series"):
                    series_title = str(show)

                ref = self.teaser_reference(teaser)
                ep_id = ref.id or str(teaser.get("id") or "")
                ep_partner = ref.partner or partner
                if not ep_id:
                    continue
                key = (season, episode_no) if episode_no > 0 else ep_id
                used = seen_ad if is_ad else seen
                if key in used:
                    continue
                used.add(key)
                year = str(teaser.get("broadcastedOn") or "")[:4]
                ep = Episode(
                    id=ep_id,
                    partner=ep_partner,
                    series=series_title,
                    title=name,
                    season=season,
                    number=episode_no,
                    audio_description=is_ad,
                    synopsis=str(teaser.get("teaserText") or teaser.get("synopsis") or ""),
                    year=year if year.isdigit() else "",
                )
                target = ad_by_season if is_ad else episodes_by_season
                target.setdefault(season, []).append(ep)

        season_numbers = sorted(set(episodes_by_season) | set(ad_by_season))
        seasons: list[Season] = []
        for number in season_numbers:
            eps = episodes_by_season.get(number) or []
            ads = ad_by_season.get(number) or []
            # Python's sort is stable. Equal unnumbered rows retain API editorial order.
            eps.sort(key=lambda episode: (episode.number <= 0, episode.number))
            ads.sort(key=lambda episode: (episode.number <= 0, episode.number))
            seasons.append(Season(number=number, episodes=eps, audio_description=ads))

        if not seasons:
            raise ArdError(f"No episodes found for {series_title}")
        return Series(id=item_id, partner=partner, title=series_title, seasons=seasons)

    def video_from_item(
        self,
        item: dict[str, Any],
        item_id: str,
        partner: str,
        *,
        is_live: bool = False,
        content_type: str = "",
        title_hint: str = "",
    ) -> Video:
        widgets = self.player_widgets(item, is_live=is_live)
        widget = widgets[0] if widgets else {}
        raw_title = str(
            widget.get("longTitle")
            or widget.get("title")
            or item.get("longTitle")
            or item.get("title")
            or title_hint
            or "Unknown"
        )
        synopsis = str(
            widget.get("synopsis")
            or widget.get("teaserText")
            or item.get("synopsis")
            or ""
        )
        if is_live:
            return Video(
                id=item_id,
                partner=partner,
                title=raw_title,
                synopsis=synopsis,
                kind="live",
                is_live=True,
                duration=duration_seconds(item),
            )

        core_type = str(
            content_type
            or widget.get("coreAssetType")
            or item.get("coreAssetType")
            or item.get("type")
            or ""
        ).upper()
        match = EPISODE_NAME_RE.match(raw_title)
        season = _first_int(
            widget.get("seasonNumber"),
            item.get("seasonNumber"),
            match.group("season") if match else None,
        )
        episode_no = _first_int(
            widget.get("episodeNumber"),
            item.get("episodeNumber"),
            match.group("episode") if match else None,
        )
        show = widget.get("show") or item.get("show") or {}
        series = str(
            widget.get("showTitle")
            or item.get("showTitle")
            or (show.get("title") if isinstance(show, dict) else "")
            or ""
        )
        show_core_type = str(
            show.get("coreAssetType") if isinstance(show, dict) else ""
        ).upper()
        explicit_non_episode = core_type in {"MOVIE", "SINGLE_VIDEO", "DOCUMENTARY", "CLIP"}
        inferred_episode = bool(
            match
            or season is not None
            or episode_no is not None
            or series
        )
        standalone_single = (
            season is None
            and episode_no is None
            and (
                show_core_type in {"SINGLE", "MOVIE", "SINGLE_VIDEO"}
                or (series and series.casefold() == raw_title.casefold())
            )
        )
        is_episode = core_type == "EPISODE" or (
            not core_type and inferred_episode
        )
        if explicit_non_episode or standalone_single:
            is_episode = False
        if is_episode:
            episode_title = match.group("name").strip() if match else raw_title
            if not series:
                item_title = str(item.get("title") or "")
                series = item_title if item_title and item_title != episode_title else title_hint
            return Video(
                id=item_id,
                partner=partner,
                title=episode_title or "Unknown",
                synopsis=synopsis,
                kind="episode",
                season=season,
                episode=episode_no,
                series=series or episode_title or "Unknown",
                audio_description=is_audio_description_teaser({**item, **widget}),
                duration=duration_seconds(item),
            )

        year_match = re.search(r"\s*\((\d{4})\)\s*$", raw_title)
        year = year_match.group(1) if year_match else ""
        title = raw_title[: year_match.start()].rstrip() if year_match else raw_title
        if not year:
            year = str(widget.get("broadcastedOn") or item.get("broadcastedOn") or "")[:4]
            if not (year.isdigit() and 1900 <= int(year) <= 2100):
                year = ""
        return Video(
            id=item_id,
            partner=partner,
            title=title or "Unknown",
            year=year,
            synopsis=synopsis,
            kind="movie",
            duration=duration_seconds(item),
        )

    @staticmethod
    def player_widgets(item: dict[str, Any], *, is_live: bool = False) -> list[dict[str, Any]]:
        player_types = {"player_live"} if is_live else {"player_ondemand"}
        players: list[dict[str, Any]] = []
        pending = _as_dict_list(item.get("widgets")) if isinstance(item, dict) else []
        while pending:
            widget = pending.pop(0)
            if str(widget.get("type") or "").casefold() in player_types:
                players.append(widget)
            pending.extend(_as_dict_list(widget.get("widgets")))
        return players

    def widget_is_blocked(self, widget: dict[str, Any]) -> bool:
        if widget.get("blockedByFsk"):
            return True
        rating = widget.get("maturityContentRating") or {}
        if isinstance(rating, dict):
            return bool(
                rating.get("isBlocked")
                or rating.get("blocked")
                or rating.get("blockedByFsk")
            )
        return False

    @staticmethod
    def media_collection_is_blocked(media_collection: dict[str, Any]) -> bool:
        """Read the block flag ARD puts on the embedded collection metadata."""
        candidates = [
            media_collection,
            _as_dict(media_collection.get("meta")),
        ]
        for candidate in candidates:
            if candidate.get("blockedByFsk"):
                return True
            rating = candidate.get("maturityContentRating")
            if isinstance(rating, dict) and (
                rating.get("isBlocked")
                or rating.get("blocked")
                or rating.get("blockedByFsk")
            ):
                return True
        return False

    def resolve_media_collection(
        self,
        widget: dict[str, Any],
        *,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        media_collection = _as_dict(widget.get("mediaCollection"))
        if media_collection.get("streams"):
            return media_collection

        embedded = media_collection.get("embedded")
        if isinstance(embedded, dict):
            return embedded
        if isinstance(embedded, str):
            href = embedded
        else:
            links = _as_dict(media_collection.get("links"))
            self_link = _as_dict(links.get("self"))
            href = (
                media_collection.get("href")
                or media_collection.get("url")
                or self_link.get("href")
                or ""
            )
        if href:
            # Resolve and validate outside the request-error fallback. An unsafe
            # authority is a hard boundary failure, not a missing stream.
            url = absolute_api_url(str(href))
            data = self.get_json(url, authenticated=authenticated)
            if not isinstance(data, dict):
                raise ArdError("ARD media collection response was not an object")
            return data
        return {}

    def extract_stream(
        self,
        item: dict[str, Any],
        *,
        is_live: bool = False,
        audio_description: bool = False,
        ignore_block: bool = False,
        authenticated: bool = False,
    ) -> tuple[str | None, dict[str, Any], bool]:
        fallback_widget: dict[str, Any] = {}
        blocked = False
        last_error: ArdError | None = None
        for widget in self.player_widgets(item, is_live=is_live):
            if not fallback_widget:
                fallback_widget = widget
            if self.widget_is_blocked(widget) and not ignore_block:
                blocked = True
                continue
            try:
                media_collection = self.resolve_media_collection(
                    widget, authenticated=authenticated
                )
            except ArdError as exc:
                last_error = exc
                continue
            if self.media_collection_is_blocked(media_collection) and not ignore_block:
                blocked = True
                continue
            stream_url = select_stream_url(
                media_collection,
                is_live=is_live,
                audio_description=audio_description,
            )
            if stream_url:
                # Keep the resolved collection for chapters/artwork, including
                # collections loaded through a link or after PIN authorization.
                resolved_widget = {**widget, "mediaCollection": {"embedded": media_collection}}
                return stream_url, resolved_widget, False
        if last_error is not None:
            raise last_error
        return None, fallback_widget, blocked

    def source(
        self,
        item_id: str,
        partner: str = "ard",
        *,
        is_live: bool = False,
        audio_description: bool = False,
        item: dict[str, Any] | None = None,
        allow_pin: bool = True,
    ) -> Source:
        item = item or self.fetch_item(item_id, partner)
        authenticated = False
        if any(widget.get("blockedByLoginOnly") for widget in self.player_widgets(item, is_live=is_live)):
            if not self.ensure_token():
                raise ArdError("This content requires Sign in with an ARD TV device code")
            item = self.fetch_item(item_id, partner, authenticated=True)
            authenticated = True
            if any(widget.get("blockedByLoginOnly") for widget in self.player_widgets(item, is_live=is_live)):
                raise ArdError("This content is not available to the active ARD TV profile")
        stream_url, widget, blocked = self.extract_stream(
            item,
            is_live=is_live,
            audio_description=audio_description,
            authenticated=authenticated,
        )
        if stream_url:
            return Source(
                manifest=stream_url,
                protocol=protocol_for(stream_url),
                title=str(widget.get("title") or item.get("title") or ""),
                is_live=is_live,
                item={**item, "widgets": [widget]},
            )
        if blocked and allow_pin:
            raise PinRequired(item_id, partner, is_live)
        if blocked:
            raise ArdError("This content is FSK-blocked and needs a TV login with PIN")
        if audio_description:
            raise ArdError(f"ARD returned no Audiodeskription stream for {item_id}")
        raise ArdError(f"ARD returned no playable stream for {item_id}")

    def unlock_with_pin(
        self,
        item_id: str,
        partner: str,
        pin: str,
        *,
        is_live: bool = False,
        audio_description: bool = False,
    ) -> Source:
        if not self.ensure_token():
            raise ArdError("PIN unlock needs an ARD TV login")
        expected = self.auth.pin
        if not expected:
            raise ArdError("No adult PIN found on the active TV profile")
        if str(pin).strip() != expected:
            raise ArdError("Incorrect adult PIN")
        unlocked = self.fetch_item(item_id, partner, authenticated=True)
        stream_url, widget, _ = self.extract_stream(
            unlocked,
            is_live=is_live,
            audio_description=audio_description,
            ignore_block=True,
            authenticated=True,
        )
        if not stream_url:
            edition = " Audiodeskription" if audio_description else ""
            raise ArdError(f"PIN accepted, but ARD returned no{edition} stream")
        return Source(
            manifest=stream_url,
            protocol=protocol_for(stream_url),
            title=str(widget.get("title") or unlocked.get("title") or ""),
            is_live=is_live,
            item={**unlocked, "widgets": [widget]},
        )

    def chapters(self, item: dict[str, Any]) -> list[Chapter]:
        chapters = extract_chapters(item)
        for config in _jumpmarks(item):
            href = str(config.get("url") or "").strip()
            if not href or _jumpmark_rows(config):
                continue
            data = self.get_json(absolute_api_url(href))
            if isinstance(data, list):
                remote = {"chapterArray": data}
            elif isinstance(data, dict) and isinstance(data.get("events"), list):
                remote = {"eventStream": data}
            else:
                raise ArdError("ARD jumpmarks feed returned an unsupported response")
            chapters.extend(extract_chapters({"pluginData": {"jumpmarks@all": remote}}))
        return chapters

    def subtitle(self, url: str, *, progress: Any = None) -> bytes:
        if not _has_https_authority(url, ARD_API_HOSTS):
            raise ArdError("Refusing subtitle request outside the ARD HTTPS API")
        headers = {
            "User-Agent": USER_AGENT, "Accept": "*/*",
            "Authorization": None, "X-Authorization": None, "Cookie": "",
        }
        try:
            with self.http.get(
                url, headers=headers, timeout=DEFAULT_TIMEOUT,
                allow_redirects=False, stream=True,
            ) as response:
                if response.status_code != 200:
                    raise ArdError(f"ARD subtitle request returned HTTP {response.status_code}")
                size = str(response.headers.get("Content-Length") or "")
                total = int(size) if size.isdigit() else None
                if total is not None and total > MAX_SUBTITLE_BYTES:
                    raise ArdError("ARD subtitle exceeds the size limit")
                chunks: list[bytes] = []
                received = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > MAX_SUBTITLE_BYTES:
                        raise ArdError("ARD subtitle exceeds the size limit")
                    chunks.append(chunk)
                    if progress is not None:
                        progress(received, total)
                if not received:
                    raise ArdError("ARD subtitle response was empty")
                return b"".join(chunks)
        except requests.RequestException as exc:
            raise ArdError(f"ARD subtitle request failed: {exc}") from exc


__all__ = [
    "API",
    "DEFAULT_HEADERS",
    "PREVIOUS_TOKEN_FILE",
    "LIVE_TYPES",
    "SERIES_TYPES",
    "TOKEN_FILE",
    "USER_AGENT",
    "VIDEO_TYPES",
    "WWW",
    "ArdApi",
    "ArdError",
    "AuthState",
    "Channel",
    "ContentRef",
    "DeviceCode",
    "DevicePollFailure",
    "Episode",
    "PinRequired",
    "SearchHit",
    "SearchPage",
    "Season",
    "Series",
    "Source",
    "Video",
    "absolute_api_url",
    "auth_from_cache",
    "duration_seconds",
    "extract_attachments",
    "extract_chapters",
    "extract_subtitles",
    "parse_content_reference",
    "parse_input",
    "select_stream_url",
    "teaser_title",
    "token_is_fresh",
]
