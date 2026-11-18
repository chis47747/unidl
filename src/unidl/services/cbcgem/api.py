"""CBC Gem API: Radio-Canada OTT protocol
"""

from __future__ import annotations

import base64
import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from mimetypes import guess_type
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse

import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter

BASE_URL = "https://services.radio-canada.ca"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) "
    "Gecko/20100101 Firefox/147.0"
)

MEDIA_INDEX_PATH = "/media/meta/v1/index.ashx"
MEDIA_VALIDATION_PATH = "/media/validation/v2"
LIVE_APP_CODE = "medianetlive"

_EXPIRY_SKEW = 3600
_REQUEST_TIMEOUT = 30
_LICENSE_TIMEOUT = 30
_DEVICE_TIMEOUT = 10
_DEVICE_CLIENT_ID = "65824cc0-e69f-4298-afc8-1dad7edec765"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_AUTH_HOST = "login.cbc.radio-canada.ca"
_TV_ACTIVATION_PAGE = "https://gem.cbc.ca/androidtv"
_TV_ACTIVATION_QUERY = {"azpcontext": "cbcgem", "ui_locales": "en", "fromApp": "ott-androidtv-app"}

_TITLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHOW_SELECTION_RE = re.compile(r"s\d+(?:e\d+)?", re.I)
_CATALOG_ROUTES = {
    "section": "section",
    "collection": "collection",
    "category": "category",
    "categorie": "category",
    "catégorie": "category",
}
_LIVE_ROUTES = {"live-event", "liveevent", "evenement-en-direct", "événement-en-direct"}
_GEM_HOST = re.compile(r"(?:^|\.)gem\.cbc\.ca$", re.I)
_TOUTV_HOST = re.compile(r"(?:^|\.)ici\.tou\.tv$", re.I)


class RadioCanadaError(RuntimeError):
    """API, schema or auth failure the service should surface."""


class RadioCanadaAuthRequired(RadioCanadaError):
    """Session is missing or unusable; credentials needed."""


def _jwt_expiry(token: str) -> int:
    try:
        return int(_jwt_payload(token).get("exp") or 0)
    except (ValueError, TypeError):
        return 0


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        middle = str(token or "").split(".")[1]
        payload = json.loads(
            base64.urlsafe_b64decode(middle + "=" * (-len(middle) % 4))
        )
        return payload if isinstance(payload, dict) else {}
    except (IndexError, ValueError, TypeError):
        return {}



@dataclass(frozen=True)
class Tenant:

    key: str
    label: str
    token_file: str
    settings_path: str
    profile_path: str
    show_path: str
    live_path: str
    app_code: str
    hosts: tuple[str, ...]

    def show_url(self, title_id: str) -> str:
        return self.show_path.format(title_id=title_id)

    @property
    def search_path(self) -> str:
        return f"/ott/catalog/v2/{self.key}/search"


GEM = Tenant(
    key="gem",
    label="CBC Gem",
    token_file="cbc_token.json",
    settings_path="/ott/catalog/v1/gem/settings",
    profile_path="/ott/subscription/v2/gem/Subscriber/profile",
    show_path="/ott/catalog/v2/gem/show/{title_id}",
    live_path="/ott/catalog/v2/gem/live",
    app_code="gem",
    hosts=("gem.cbc.ca",),
)


TENANTS: dict[str, Tenant] = {
    GEM.key: GEM,
}


@dataclass
class SessionState:
    """Session tokens plus an optional claims token for media calls."""

    access_token: str = ""
    refresh_token: str = ""
    expiration_time: float = 0.0
    claims_token: str = ""
    claims_expiration_time: float = 0.0
    username: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    tv_code: bool = False

    @classmethod
    def from_cache(cls, data: dict[str, Any] | None) -> SessionState:
        raw = data if isinstance(data, dict) else {}
        nested = raw.get("data")
        if isinstance(nested, dict) and (
            nested.get("access_token") or nested.get("refresh_token")
        ):
            body = nested
            try:
                expiration = float(raw.get("expiration_time") or 0)
            except (TypeError, ValueError):
                expiration = 0.0
        else:
            body = raw
            raw_expiration = (
                raw.get("expiration_time")
                or raw.get("expiry")
                or raw.get("expires_at")
                or 0
            )
            try:
                expiration = float(raw_expiration)
            except (TypeError, ValueError):
                expiration = 0.0
            if expiration == 0 and raw.get("expires_in"):
                try:
                    expiration = time.time() + float(raw["expires_in"]) - _EXPIRY_SKEW
                except (TypeError, ValueError):
                    expiration = 0.0

        claims = str(raw.get("claims_token") or body.get("claims_token") or "")
        try:
            claims_expiration = float(
                raw.get("claims_expiration_time")
                or body.get("claims_expiration_time")
                or 0
            )
        except (TypeError, ValueError):
            claims_expiration = 0.0
        if claims and not claims_expiration:
            claims_expiration = float(_jwt_expiry(claims))

        return cls(
            access_token=str(body.get("access_token") or ""),
            refresh_token=str(body.get("refresh_token") or ""),
            expiration_time=expiration,
            claims_token=claims,
            claims_expiration_time=claims_expiration,
            username=str(raw.get("username") or body.get("username") or ""),
            raw=dict(body) if body else {},
            tv_code=raw.get("auth_method") == "tv_code",
        )

    def to_cache(self) -> dict[str, Any]:
        body = dict(self.raw) if self.raw else {}
        if self.access_token:
            body["access_token"] = self.access_token
        if self.refresh_token:
            body["refresh_token"] = self.refresh_token
        out: dict[str, Any] = {
            "data": body,
            "expiration_time": self.expiration_time,
        }
        if self.username:
            out["username"] = self.username
        if self.tv_code:
            out["auth_method"] = "tv_code"
        if self.claims_token:
            out["claims_token"] = self.claims_token
            if self.claims_expiration_time:
                out["claims_expiration_time"] = self.claims_expiration_time
        return out

    def usable(self, leeway: int = 60) -> bool:
        return bool(self.access_token) and self.expiration_time > (time.time() + leeway)

    def refreshable(self) -> bool:
        return self.tv_code and bool(self.refresh_token)

    def claims_usable(self, leeway: int = 60) -> bool:
        if not self.claims_token:
            return False
        return not self.claims_expiration_time or self.claims_expiration_time > (
            time.time() + leeway
        )

    def apply_grant(self, grant: dict[str, Any]) -> None:
        id_token = self.raw.get("id_token")
        self.raw = dict(grant)
        if id_token and not self.raw.get("id_token"):
            self.raw["id_token"] = id_token
        self.access_token = str(grant.get("access_token") or "")
        self.refresh_token = str(grant.get("refresh_token") or self.refresh_token)
        try:
            expires_in = int(grant.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        skew = min(_EXPIRY_SKEW, expires_in // 10)
        self.expiration_time = time.time() + max(0, expires_in - skew)
        self.claims_token = ""
        self.claims_expiration_time = 0.0


@dataclass(frozen=True)
class DeviceAuthConfig:
    scope: str
    tenant_id: str
    login_policy: str
    policies: tuple[str, ...]

    @property
    def device_url(self) -> str:
        return f"https://{_AUTH_HOST}/oauth2/v2.0/device"

    def refresh_url(self, id_token: str) -> str:
        # The SDK refreshes against the policy named in the device grant's ID token.
        claims = _jwt_payload(id_token)
        policy = str(claims.get("acr") or claims.get("tfp") or "")
        active = next((p for p in self.policies if p.casefold() == policy.casefold()), self.login_policy)
        return f"https://{_AUTH_HOST}/{self.tenant_id}/{active}/oauth2/v2.0/token"


@dataclass
class DeviceCode:
    config: DeviceAuthConfig = field(repr=False)
    device_code: str = field(repr=False)
    user_code: str = field(repr=False)
    verification_url: str
    direct_url: str = field(repr=False)
    expires_in: int
    interval: int
    expires_at: float
    next_poll_at: float


@dataclass(frozen=True)
class ParsedInput:
    title_id: str
    tenant_hint: str | None = None  # "gem" | "toutv" | None
    kind: str = "show"  # show | section | collection | category | live
    selection: str = ""  # sXX / sXXeYY when a catalogue URL selects one item


@dataclass(frozen=True)
class Episode:
    media_id: str
    number: int
    title: str
    season: int = 0
    media_type: str = "episode"
    year: str = ""
    duration: float | None = None
    synopsis: str = ""
    cover_url: str = ""
    attachments: tuple[Attachment, ...] = ()
    selection: str = ""

    @property
    def label(self) -> str:
        if self.number:
            return f"E{self.number:02d}  {self.title}"
        return self.title


@dataclass(frozen=True)
class Season:
    number: int
    episodes: list[Episode]

    @property
    def label(self) -> str:
        count = len(self.episodes)
        name = "Specials" if self.number == 0 else f"Season {self.number}"
        return f"{name}  ·  {count} episode(s)" if count else name


@dataclass(frozen=True)
class MovieItem:
    media_id: str
    title: str
    year: str = ""
    media_type: str = "episode"
    duration: float | None = None
    synopsis: str = ""
    cover_url: str = ""
    attachments: tuple[Attachment, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.title}{f' ({self.year})' if self.year else ''}"

    @property
    def is_live_to_vod(self) -> bool:
        return self.media_type.lower() == "livetovod"


@dataclass(frozen=True)
class Show:
    id: str
    title: str
    kind: str  # movie | series
    seasons: list[Season] = field(default_factory=list)
    movies: list[MovieItem] = field(default_factory=list)
    extras: list[MovieItem] = field(default_factory=list)
    year: str = ""
    synopsis: str = ""
    cover_url: str = ""
    external_url: str = ""
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class SearchHit:
    kind: str  # show, season, media, section, collection, category, live
    title: str
    url: str
    info_title: str = ""
    tier: str = ""
    media_id: str = ""
    synopsis: str = ""
    cover_url: str = ""
    attachments: tuple[Attachment, ...] = ()

    @property
    def show_id(self) -> str:
        parts = self.url.strip("/").split("/")
        if self.kind in {"section", "collection", "category", "live"} and len(parts) > 1:
            return parts[1]
        return parts[0] if parts else ""

    @property
    def selection(self) -> str:
        parts = self.url.strip("/").split("/", 1)
        return parts[1] if len(parts) > 1 else ""

    @property
    def season_episode(self) -> tuple[int | None, int | None]:
        match = re.search(r"(?:^|/)s(\d+)e(\d+)(?:$|[/?])", self.url, re.I)
        if not match:
            return None, None
        return int(match.group(1)), int(match.group(2))

    @property
    def detail(self) -> str:
        parts = [self.info_title, self.kind.title(), self.tier]
        return " | ".join(part for part in parts if part)


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    total: int


@dataclass(frozen=True)
class CatalogLineup:
    title: str
    items: list[SearchHit]

    @property
    def label(self) -> str:
        return f"{self.title}  ·  {len(self.items)} item(s)"


@dataclass(frozen=True)
class CatalogPage:
    kind: str
    id: str
    title: str
    lineups: list[CatalogLineup]


@dataclass(frozen=True)
class LiveChannel:
    media_id: str
    title: str
    detail: str = ""
    kind: str = "stream"
    tier: str = ""
    feed_type: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    synopsis: str = ""
    cover_url: str = ""
    attachments: tuple[Attachment, ...] = ()
    event_slug: str = ""

    @property
    def label(self) -> str:
        return self.title

    @property
    def menu_detail(self) -> str:
        parts = [self.detail, self.tier, self.feed_type]
        return " | ".join(part for part in parts if part)


@dataclass(frozen=True)
class LiveCategory:
    key: str
    title: str
    channels: list[LiveChannel]
    feed_type: str = ""

    @property
    def label(self) -> str:
        return f"{self.title}  ·  {len(self.channels)}"


@dataclass
class Source:
    media_id: str
    manifest: str
    title: str
    year: str = ""
    season: int | None = None
    episode: int | None = None
    episode_name: str = ""
    series_title: str = ""
    protected: bool = True
    tech: str = ""
    license_url: str = ""
    auth_token: str = ""
    is_live: bool = False
    app_code: str = ""
    media_type: str = ""
    duration: float | None = None
    synopsis: str = ""
    cover_url: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    chapters: tuple[Chapter, ...] = ()
    attachments: tuple[Attachment, ...] = ()

    def note(self) -> str:
        drm = "Widevine" if self.protected else "clear"
        path = urlparse(self.manifest).path.lower()
        protocol = "HLS" if "hls" in self.tech.lower() or path.endswith(".m3u8") else "DASH"
        bits = [protocol, drm]
        if self.tech:
            bits.append(self.tech)
        if self.is_live:
            bits.append("live")
        return " · ".join(bits)


def parse_input(text: str, *, default_tenant: str | None = None) -> ParsedInput | None:
    raw = (text or "").strip()
    if not raw:
        return None

    tenant_hint = default_tenant
    if "://" in raw or raw.lower().startswith("www.") or "/" in raw:
        candidate = raw if "://" in raw else f"https://{raw}"
        try:
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower()
        except ValueError:
            return None
        if not host or parsed.scheme not in {"http", "https"}:
            return None
        if _GEM_HOST.search(host):
            tenant_hint = "gem"
        elif _TOUTV_HOST.search(host):
            tenant_hint = "toutv"
        else:
            return None

        path = (parsed.path or "").strip("/")
        parts = [unquote(p) for p in path.split("/") if p]
        while parts and parts[0].lower() in {"en", "fr"}:
            parts.pop(0)
        if not parts:
            return None

        kind = "show"
        route = parts[0].lower()
        if route in _CATALOG_ROUTES:
            kind = _CATALOG_ROUTES[route]
            parts.pop(0)
        elif route in _LIVE_ROUTES:
            kind = "live"
            parts.pop(0)
        elif route in {"media", "show"}:
            parts.pop(0)
        if not parts:
            return None

        title_id = parts[0]
        if not _TITLE_ID_RE.fullmatch(title_id):
            return None
        selection = parts[1] if len(parts) > 1 else ""
        if kind != "show" or not _SHOW_SELECTION_RE.fullmatch(selection):
            selection = ""
        return ParsedInput(title_id, tenant_hint, kind, selection)

    if _TITLE_ID_RE.fullmatch(raw):
        return ParsedInput(raw, tenant_hint)
    return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 <= number and math.isfinite(number * 1000) else None


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    media = metadata.get("media")
    if isinstance(media, dict):
        return {**media, **metadata}
    return metadata


def _image_url(item: dict[str, Any]) -> str:
    images = item.get("images")
    if not isinstance(images, dict):
        return ""
    for name in ("card", "background", "thumbnail", "generic"):
        image = images.get(name)
        if isinstance(image, dict) and image.get("url"):
            return str(image["url"])
        if isinstance(image, str) and image:
            return image
    return ""


def _duration(item: dict[str, Any]) -> float | None:
    metadata = _metadata(item)
    return _safe_float(metadata.get("duration") or item.get("duration"))


def _artwork(value: Any, name: str, kind: str) -> Attachment | None:
    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str):
        return None
    url = value.strip()
    if url.startswith("//"):
        url = "https:" + url
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return None
    except ValueError:
        return None
    mime = guess_type(parsed.path)[0]
    return Attachment(url=url, name=name, kind=kind, mime_type=mime or "")


def artwork_for(item: dict[str, Any]) -> tuple[Attachment, ...]:
    images = item.get("images")
    images = images if isinstance(images, dict) else {}
    candidates = (
        (images.get("card"), "Cover", "poster"),
        (images.get("background"), "Background", "artwork"),
        (images.get("logo"), "Title logo", "image"),
        (images.get("thumbnail"), "Thumbnail", "thumbnail"),
        (images.get("generic") or item.get("genericImage"), "Channel artwork", "artwork"),
    )
    return tuple(
        attachment
        for value, name, kind in candidates
        if (attachment := _artwork(value, name, kind)) is not None
    )


def _selected_artwork(
    attachments: tuple[Attachment, ...], cover_url: str, metas: dict[str, Any]
) -> tuple[Attachment, ...]:
    artwork: dict[str, Attachment] = {}
    for item in attachments:
        artwork.setdefault(item.url, item)
    if cover := _artwork(cover_url, "Cover", "poster"):
        artwork.setdefault(cover.url, cover)
    for field_name in ("imagePlayerLargeA", "imagePlayerNormalC"):
        if image := _artwork(metas.get(field_name), "Player artwork", "thumbnail"):
            artwork.setdefault(image.url, image)
            break
    return tuple(artwork.values())


def _index_duration(metas: dict[str, Any], fallback: float | None) -> float | None:
    for value in (metas.get("length"), metas.get("Av-Duree"), fallback):
        seconds = _safe_float(value)
        if seconds:
            return seconds
    return None


def chapters_from_metas(
    metas: dict[str, Any], duration: float | None = None,
) -> tuple[Chapter, ...]:
    """Read the media index's comma-separated HH:MM:SS and seconds markers."""
    duration = _index_duration(metas, duration)
    limit = round(duration * 1000) if duration else None
    starts: set[int] = set()
    markers = metas.get("Chapitres")
    if isinstance(markers, str):
        for marker in markers.split(","):
            match = re.fullmatch(
                r"(\d{1,4}):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?",
                marker.strip(),
            )
            if match is None:
                continue
            hours, minutes, seconds = map(int, match.groups()[:3])
            milliseconds = int((match.group(4) or "").ljust(3, "0"))
            start = (hours * 3600 + minutes * 60 + seconds) * 1000 + milliseconds
            if start > 0 and (limit is None or start < limit):
                starts.add(start)
    credits = None
    seconds = _safe_float(metas.get("CreditStartTime"))
    if seconds:
        start = round(seconds * 1000)
        if start > 0 and (limit is None or start < limit):
            credits = Chapter(start_ms=start, title="Credits", kind="credits")
            starts.discard(start)
    if starts:
        chapters = [
            Chapter(start_ms=start, title=f"Chapter {number:02d}")
            for number, start in enumerate((0, *sorted(starts)), 1)
        ]
    elif credits:
        chapters = [Chapter(start_ms=0, title="Main programme")]
    else:
        return ()
    if credits:
        chapters.append(credits)
    return tuple(sorted(chapters, key=lambda chapter: chapter.start_ms))


def _year(item: dict[str, Any]) -> str:
    metadata = _metadata(item)
    if metadata.get("productionYear"):
        return str(metadata["productionYear"])
    structured = item.get("structuredMetadata")
    if isinstance(structured, dict):
        match = re.search(r"\b(19|20)\d{2}\b", str(structured.get("dateCreated") or ""))
        if match:
            return match.group(0)
    return ""


def _external_url(data: dict[str, Any]) -> str:
    external = data.get("externalSite")
    if isinstance(external, str):
        return external.strip()
    if isinstance(external, dict):
        return str(external.get("url") or external.get("href") or "").strip()
    return ""


def _clean_episode_title(raw: str) -> str:
    text = (raw or "").strip()
    if "." in text:
        left, right = text.split(".", 1)
        if left.strip().isdigit() or re.fullmatch(r"\d+", left.strip()):
            return right.strip() or text
        if re.fullmatch(r"[Ss]?\d+[Ee]\d+", left.strip()):
            return right.strip() or text
    return text


def _is_movie_payload(data: dict[str, Any]) -> bool:
    content_type = str(data.get("contentType") or "").lower()
    return content_type in ("film", "movie", "standalone")


def _lineups_for(data: dict[str, Any], *, episodes: bool) -> list[dict[str, Any]]:
    content = data.get("content") or []
    if not isinstance(content, list):
        return []

    if episodes:
        for wanted in ("episodes", "épisodes"):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if str(block.get("title") or "").lower() == wanted:
                    lineups = block.get("lineups") or []
                    if isinstance(lineups, list) and lineups:
                        return [x for x in lineups if isinstance(x, dict)]
        for block in content:
            if not isinstance(block, dict):
                continue
            lineups = block.get("lineups") or []
            if isinstance(lineups, list) and lineups:
                return [x for x in lineups if isinstance(x, dict)]
        return []

    unwanted = {
        "episodes",
        "épisodes",
        "trailers",
        "extras",
        "compléments",
        "complements",
    }
    for block in content:
        if not isinstance(block, dict):
            continue
        title = str(block.get("title") or "").lower()
        if title in unwanted:
            continue
        lineups = block.get("lineups") or []
        if isinstance(lineups, list) and lineups:
            return [x for x in lineups if isinstance(x, dict)]
    return []


def show_from_payload(data: dict[str, Any], *, title_id: str) -> Show:
    title = str(data.get("title") or title_id).strip() or title_id
    year = _year(data)
    synopsis = str(data.get("description") or "").strip()
    cover_url = _image_url(data)
    external_url = _external_url(data)

    def movie_item(item: dict[str, Any], *, fallback_type: str = "episode") -> MovieItem | None:
        media_id = str(item.get("idMedia") or item.get("id") or "").strip()
        if not media_id:
            return None
        media_type = str(item.get("mediaType") or fallback_type).strip().lower()
        name = str(item.get("title") or title).strip() or title
        if media_type == "episode":
            name = _clean_episode_title(name)
        return MovieItem(
            media_id=media_id,
            title=name,
            year=_year(item) or year,
            media_type=media_type,
            duration=_duration(item),
            synopsis=str(item.get("description") or synopsis).strip(),
            cover_url=_image_url(item) or cover_url,
            attachments=artwork_for(item),
        )

    extras: list[MovieItem] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        block_title = str(block.get("title") or "").strip().lower()
        if block_title not in {"trailers", "extras", "compléments", "complements"}:
            continue
        for lineup in block.get("lineups") or []:
            if not isinstance(lineup, dict):
                continue
            for item in lineup.get("items") or []:
                if not isinstance(item, dict):
                    continue
                parsed = movie_item(
                    item,
                    fallback_type=str(item.get("mediaType") or "extra").lower(),
                )
                if parsed is not None:
                    extras.append(parsed)

    if _is_movie_payload(data):
        movies: list[MovieItem] = []
        for season in _lineups_for(data, episodes=False):
            for item in season.get("items") or []:
                if not isinstance(item, dict):
                    continue
                media_type = str(item.get("mediaType") or "").lower()
                if media_type not in ("episode", "livetovod", "film", "movie", "standalone", ""):
                    continue
                parsed = movie_item(item, fallback_type=media_type or "episode")
                if parsed is not None:
                    movies.append(parsed)
        return Show(
            id=title_id,
            title=title,
            kind="movie",
            movies=movies,
            extras=extras,
            year=year,
            synopsis=synopsis,
            cover_url=cover_url,
            external_url=external_url,
            attachments=artwork_for(data),
        )

    seasons: list[Season] = []
    for lineup in _lineups_for(data, episodes=True):
        season_num = _safe_int(lineup.get("seasonNumber"), 0)
        episodes: list[Episode] = []
        for item in lineup.get("items") or []:
            if not isinstance(item, dict):
                continue
            media_type = str(item.get("mediaType") or "").lower()
            if media_type and media_type not in ("episode", "livetovod"):
                continue
            media_id = str(item.get("idMedia") or item.get("id") or "").strip()
            if not media_id:
                continue
            number = _safe_int(item.get("episodeNumber"), 0)
            episodes.append(
                Episode(
                    media_id=media_id,
                    number=number,
                    title=_clean_episode_title(str(item.get("title") or f"Episode {number}")),
                    season=season_num,
                    media_type=media_type or "episode",
                    year=_year(item) or year,
                    duration=_duration(item),
                    synopsis=str(item.get("description") or synopsis).strip(),
                    cover_url=_image_url(item) or cover_url,
                    attachments=artwork_for(item),
                    selection=_episode_selection(item.get("url")),
                )
            )
        episodes.sort(key=lambda ep: (ep.number, ep.title))
        if episodes:
            seasons.append(Season(number=season_num, episodes=episodes))
    seasons.sort(key=lambda s: s.number)
    return Show(
        id=title_id,
        title=title,
        kind="series",
        seasons=seasons,
        extras=extras,
        year=year,
        synopsis=synopsis,
        cover_url=cover_url,
        external_url=external_url,
        attachments=artwork_for(data),
    )


def _parse_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _episode_selection(value: Any) -> str:
    segment = str(value or "").partition("?")[0].rstrip("/").rsplit("/", 1)[-1].lower()
    return segment if _SHOW_SELECTION_RE.fullmatch(segment) else ""


def _live_channel(item: dict[str, Any], *, kind: str, feed_type: str = "") -> LiveChannel | None:
    media_id = str(item.get("idMedia") or item.get("formattedIdMedia") or item.get("id") or "").strip()
    if not media_id:
        return None
    programme = str(item.get("title") or "Live").strip() or "Live"
    station = str(item.get("streamTitle") or "").strip()
    info = str(item.get("infoTitle") or "").strip()
    if station:
        title = station
        detail = " · ".join(part for part in (programme, info) if part)
    else:
        title = programme
        detail = info
    permission = item.get("permission") if isinstance(item.get("permission"), dict) else {}
    tier = str(item.get("tier") or permission.get("tier") or "").strip()
    next_item = item.get("nextScheduleItem")
    ends_at = _parse_datetime(item.get("airDateEnd"))
    if isinstance(next_item, dict):
        ends_at = ends_at or _parse_datetime(next_item.get("airDate"))
        next_title = str(next_item.get("title") or "").strip()
        if next_title:
            detail = f"{detail} · Next: {next_title}" if detail else f"Next: {next_title}"
    return LiveChannel(
        media_id=media_id,
        title=title,
        detail=detail,
        kind=kind,
        tier=tier,
        feed_type=str(item.get("feedType") or feed_type or "").strip(),
        starts_at=_parse_datetime(item.get("airDate")),
        ends_at=ends_at,
        synopsis=str(item.get("description") or "").strip(),
        cover_url=_image_url(item) or str(item.get("genericImage") or "").strip(),
        attachments=artwork_for(item),
        event_slug=str(item.get("url") or "").partition("?")[0].strip("/").rsplit("/", 1)[-1],
    )


def _live_identity(item: dict[str, Any]) -> str:
    url = str(item.get("url") or "").strip().casefold()
    key = str(item.get("key") or "").strip().casefold()
    if url:
        return f"url:{url}"
    if key:
        return f"key:{key}"
    return "|".join(
        str(item.get(name) or "").strip().casefold()
        for name in ("idMedia", "title", "streamTitle")
    )


def live_catalog_from_payload(data: dict[str, Any]) -> list[LiveCategory]:
    categories: list[LiveCategory] = []
    identities: dict[str, LiveChannel] = {}

    def add_category(raw: dict[str, Any], *, key: str, kind: str) -> None:
        channels: list[LiveChannel] = []
        seen: set[str] = set()
        feed_type = str(raw.get("feedType") or "").strip()
        for item in raw.get("items") or []:
            if not isinstance(item, dict):
                continue
            identity = _live_identity(item)
            if identity in seen:
                continue
            channel = _live_channel(item, kind=kind, feed_type=feed_type)
            if channel is None:
                continue
            seen.add(identity)
            identities.setdefault(identity, channel)
            channels.append(channel)
        if channels:
            categories.append(
                LiveCategory(
                    key=key,
                    title=str(raw.get("title") or "Live").strip() or "Live",
                    channels=channels,
                    feed_type=feed_type,
                )
            )

    free_tv = data.get("freeTv") if isinstance(data.get("freeTv"), dict) else {}
    add_category(free_tv, key="free-tv", kind="freeTv")
    for index, category in enumerate(data.get("streams") or []):
        if isinstance(category, dict):
            add_category(
                category,
                key=str(category.get("key") or f"streams-{index}"),
                kind="stream",
            )

    all_channels = sorted(identities.values(), key=lambda channel: channel.label.casefold())
    if not all_channels:
        return []
    return [LiveCategory("all", "All live channels", all_channels), *categories]


def live_from_payload(data: dict[str, Any]) -> list[LiveChannel]:
    categories = live_catalog_from_payload(data)
    return list(categories[0].channels) if categories else []


def _catalog_hit(raw: dict[str, Any]) -> SearchHit | None:
    kind = str(raw.get("type") or "").strip().lower()
    if kind not in {"show", "season", "media", "section", "collection", "category", "live"}:
        return None
    original_url = str(raw.get("url") or "").strip().strip("/")
    parsed_url = urlparse(f"https://catalog.invalid/{original_url}")
    url = parsed_url.path.strip("/")
    segments = [unquote(part) for part in url.split("/")] if url else []
    if kind == "category" and len(segments) == 2 and _CATALOG_ROUTES.get(segments[0].lower()) == kind:
        segments[0] = kind
    if kind == "live" and len(segments) == 2 and segments[0].lower() in _LIVE_ROUTES:
        segments[0] = "live-event"
    if not segments or not all(_TITLE_ID_RE.fullmatch(segment) for segment in segments):
        return None
    if kind in {"section", "collection", "category"}:
        if len(segments) != 2 or segments[0].lower() != kind:
            return None
    elif kind == "show" and len(segments) != 1:
        return None
    elif kind == "season" and (len(segments) != 2 or not re.fullmatch(r"s\d+", segments[1], re.I)):
        return None
    elif kind == "media":
        if len(segments) not in {1, 2}:
            return None
        if len(segments) == 2 and not re.fullmatch(r"s\d+e\d+", segments[1], re.I):
            return None
    elif kind == "live" and not (
        len(segments) == 1 or (len(segments) == 2 and segments[0] == "live-event")
    ):
        return None
    permission = raw.get("permission") if isinstance(raw.get("permission"), dict) else {}
    return SearchHit(
        kind=kind,
        title=str(raw.get("title") or url).strip() or url,
        url="/".join(segments),
        info_title=str(raw.get("infoTitle") or "").strip(),
        tier=str(raw.get("tier") or permission.get("tier") or "").strip(),
        media_id=str(raw.get("idMedia") or raw.get("formattedIdMedia") or "").strip(),
        synopsis=str(raw.get("description") or "").strip(),
        cover_url=_image_url(raw),
        attachments=artwork_for(raw),
    )


def search_from_payload(data: dict[str, Any]) -> SearchResult:
    raw_results = data.get("results") if isinstance(data, dict) else None
    hits: list[SearchHit] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_results if isinstance(raw_results, list) else []:
        if not isinstance(raw, dict):
            continue
        hit = _catalog_hit(raw)
        if hit is None:
            continue
        identity = hit.kind, hit.url.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        hits.append(hit)
    total = _safe_int(data.get("totalRecords"), len(hits))
    return SearchResult(hits=hits, total=total)


def catalog_from_payload(
    data: dict[str, Any], *, kind: str, catalog_id: str
) -> CatalogPage:
    raw_lineups: list[dict[str, Any]] = []
    if kind == "section":
        lineups = data.get("lineups")
        if isinstance(lineups, dict) and isinstance(lineups.get("results"), list):
            raw_lineups = [item for item in lineups["results"] if isinstance(item, dict)]
    elif kind in {"collection", "category"}:
        for content in data.get("content") or []:
            if not isinstance(content, dict):
                continue
            items = content.get("items")
            results = items.get("results") if isinstance(items, dict) else None
            if isinstance(results, list):
                raw_lineups.append({**content, "items": results})

    parsed: list[CatalogLineup] = []
    for index, lineup in enumerate(raw_lineups):
        items: list[SearchHit] = []
        seen: set[tuple[str, str]] = set()
        for raw in lineup.get("items") or []:
            if not isinstance(raw, dict):
                continue
            hit = _catalog_hit(raw)
            if hit is None:
                continue
            identity = hit.kind, hit.url.casefold()
            if identity in seen:
                continue
            seen.add(identity)
            items.append(hit)
        if items:
            parsed.append(
                CatalogLineup(
                    str(lineup.get("title") or f"Items {index + 1}").strip(),
                    items,
                )
            )
    return CatalogPage(
        kind=kind,
        id=catalog_id,
        title=str(data.get("title") or catalog_id).strip() or catalog_id,
        lineups=parsed,
    )


class RadioCanadaApi:

    def __init__(
        self,
        session: requests.Session,
        tenant: Tenant,
        *,
        state: SessionState | None = None,
        on_save: Callable[[SessionState], None] | None = None,
    ):
        self.session = session
        self.tenant = tenant
        self.state = state or SessionState()
        self.on_save = on_save
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.session.headers.setdefault("Accept", "application/json")
        if self.state.claims_usable():
            self.session.headers["x-claims-token"] = self.state.claims_token
        else:
            self.session.headers.pop("x-claims-token", None)

    def _save(self) -> None:
        if self.on_save is not None:
            self.on_save(self.state)

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return urljoin(BASE_URL + "/", path.lstrip("/"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        auth_bearer: str | None = None,
        raw: bool = False,
        _retry_claims: bool = True,
    ) -> Any:
        req_headers = dict(headers or {})
        if auth_bearer:
            req_headers["Authorization"] = f"Bearer {auth_bearer}"

        def send() -> requests.Response:
            try:
                return self.session.request(
                    method,
                    self._url(path),
                    params=params,
                    data=data,
                    headers=req_headers or None,
                    timeout=_REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                raise RadioCanadaError(
                    f"{self.tenant.label} request failed: {exc}"
                ) from exc

        response = send()
        if _retry_claims and self._expired_claims_response(response):
            self._recover_claims()
            for key in list(req_headers):
                if key.lower() == "x-claims-token":
                    req_headers.pop(key, None)
            if self.state.claims_usable():
                req_headers["x-claims-token"] = self.state.claims_token
            response = send()

        if response.status_code not in (200, 426):
            raise RadioCanadaError(
                f"{self.tenant.label} HTTP {response.status_code}: {response.text[:300]}"
            )
        if raw:
            return response.content
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise RadioCanadaError(
                f"{self.tenant.label} returned non-JSON from {path}"
            ) from exc
        if isinstance(payload, dict):
            error = next(
                (
                    payload.get(key)
                    for key in ("errorMessage", "ErrorMessage", "error", "ErrorCode", "errorCode")
                    if payload.get(key) not in (None, "", 0, "0")
                ),
                None,
            )
            if error is not None:
                message = str(payload.get("message") or "").strip()
                detail = f"{message} (API error {error})" if message else f"API error {error}"
                raise RadioCanadaError(f"{self.tenant.label}: {detail}")
        return payload

    @staticmethod
    def _expired_claims_response(response: requests.Response) -> bool:
        if response.status_code != 401:
            return False
        message = str(response.text or "").lower()
        return "claims token" in message and (
            "expired" in message or "invalid" in message
        )

    def _drop_claims_header(self) -> None:
        self.session.headers.pop("x-claims-token", None)

    def _recover_claims(self) -> None:

        self._drop_claims_header()
        if self.state.usable(leeway=0):
            try:
                self._refresh_claims()
                self._save()
                return
            except RadioCanadaError:
                if not self.state.refreshable():
                    raise
        if self.state.refreshable():
            self.refresh()
            return
        raise RadioCanadaAuthRequired(
            f"{self.tenant.label} claims token expired; sign in again"
        )


    def _device_settings(self) -> DeviceAuthConfig:
        data = self._request("GET", self.tenant.settings_path, params={"device": "androidtv"})
        if not isinstance(data, dict):
            raise RadioCanadaError(f"{self.tenant.label} settings response is invalid")
        identity = data.get("identityManagement") if isinstance(data.get("identityManagement"), dict) else {}
        device = identity.get("deviceAuthFlow")
        pkce = identity.get("pkce")
        audience = identity.get("ropc")
        tenant = str(identity.get("tenant") or "").strip()
        tenant_id = str(identity.get("tenantId") or "").strip()
        app_id = str(audience.get("appId") or "").strip() if isinstance(audience, dict) else ""
        policy = str(device.get("loginPolicyName") or "").strip() if isinstance(device, dict) else ""
        base_scopes = str(identity.get("scopes") or "").split()
        api_scopes = str(identity.get("apiScopes") or "").split()
        if (
            identity.get("tenantDomain") != _AUTH_HOST
            or not all(re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in (tenant, tenant_id, app_id, policy))
            or not api_scopes
        ):
            raise RadioCanadaError(f"{self.tenant.label}: Android TV login configuration is incomplete")
        policies = [policy]
        fallback_policy = policy
        for group in (device, pkce):
            if isinstance(group, dict):
                for key in ("loginPolicyName", "signupPolicyName"):
                    name = str(group.get(key) or "").strip()
                    if re.fullmatch(r"[A-Za-z0-9_-]+", name) and name not in policies:
                        policies.append(name)
                    if group is pkce and key == "loginPolicyName" and name in policies:
                        fallback_policy = name
        prefix = f"https://{tenant}.onmicrosoft.com/{app_id}/"
        return DeviceAuthConfig(
            " ".join([*base_scopes, *(prefix + scope for scope in api_scopes)]),
            tenant_id,
            fallback_policy,
            tuple(policies),
        )

    def _device_post(self, url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:
        try:
            response = self.session.post(
                url, data=data, headers={"x-claims-token": None},
                allow_redirects=False, timeout=_DEVICE_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RadioCanadaError(f"{self.tenant.label}: TV code server connection failed") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return response.status_code, payload if isinstance(payload, dict) else {}

    def start_login(self) -> DeviceCode:
        config = self._device_settings()
        status, payload = self._device_post(
            f"{config.device_url}/code",
            {"client_id": _DEVICE_CLIENT_ID, "scope": config.scope},
        )
        if status != 200:
            raise RadioCanadaError(f"{self.tenant.label}: could not request a TV code (HTTP {status})")
        device_code = str(payload.get("device_code") or "").strip()
        user_code = str(payload.get("user_code") or "").strip()
        verification_url = str(payload.get("verification_uri") or "").strip()
        expires_in = _safe_int(payload.get("expires_in"))
        if not device_code or not user_code or expires_in <= 0 or not self._verification_url(verification_url):
            raise RadioCanadaError(f"{self.tenant.label}: TV code response was incomplete")
        direct_url = str(payload.get("verification_uri_complete") or "").strip()
        now = time.monotonic()
        interval = _safe_int(payload.get("interval"), 5)
        if interval <= 0:
            interval = 5
        return DeviceCode(
            config=config,
            device_code=device_code,
            user_code=user_code,
            verification_url=_TV_ACTIVATION_PAGE,
            direct_url=self._branded_verification_url(direct_url),
            expires_in=expires_in,
            interval=interval,
            expires_at=now + expires_in,
            next_poll_at=now + interval,
        )

    @staticmethod
    def _verification_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
            return parsed.scheme == "https" and parsed.netloc.casefold() == _AUTH_HOST
        except ValueError:
            return False

    @classmethod
    def _branded_verification_url(cls, url: str) -> str:
        if not cls._verification_url(url):
            return ""
        parsed = urlparse(url)
        pairs = [
            (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key not in _TV_ACTIVATION_QUERY
        ]
        return parsed._replace(query=urlencode([*pairs, *_TV_ACTIVATION_QUERY.items()])).geturl()

    def poll_login(self, code: DeviceCode) -> dict[str, Any] | None:
        now = time.monotonic()
        if now >= code.expires_at:
            raise RadioCanadaAuthRequired(f"{self.tenant.label}: TV code expired; request a new one")
        if now < code.next_poll_at:
            return None
        code.next_poll_at = now + code.interval
        status, payload = self._device_post(
            f"{code.config.device_url}/token",
            {
                "client_id": _DEVICE_CLIENT_ID,
                "scope": code.config.scope,
                "device_code": code.device_code,
                "grant_type": _DEVICE_GRANT,
            },
        )
        error = payload.get("error")
        if status == 200 and payload.get("access_token") and not error:
            return payload
        if error == "slow_down" or status == 429:
            code.interval += 5
            code.next_poll_at = time.monotonic() + code.interval
            return None
        if error == "authorization_pending":
            return None
        if error == "expired_token":
            raise RadioCanadaAuthRequired(f"{self.tenant.label}: TV code expired; request a new one")
        if error == "access_denied":
            raise RadioCanadaAuthRequired(f"{self.tenant.label}: TV code approval was denied")
        raise RadioCanadaError(f"{self.tenant.label}: TV code approval failed (HTTP {status})")

    def complete_login(self, grant: dict[str, Any]) -> SessionState:
        self.state = SessionState(tv_code=True)
        self.state.apply_grant(grant)
        self._refresh_claims()
        self._save()
        return self.state

    def refresh(self) -> SessionState:
        if not self.state.refreshable():
            raise RadioCanadaAuthRequired(f"{self.tenant.label}: open Sign in to pair with a TV code")
        self._drop_claims_header()
        config = self._device_settings()
        status, grant = self._device_post(
            config.refresh_url(str(self.state.raw.get("id_token") or "")),
            {
                "client_id": _DEVICE_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self.state.refresh_token,
                "scope": config.scope,
            },
        )
        if status != 200 or not grant.get("access_token") or grant.get("error"):
            raise RadioCanadaAuthRequired(
                f"{self.tenant.label}: TV session refresh failed (HTTP {status}); sign in again with a TV code"
            )
        self.state.apply_grant(grant)
        self._save()
        self._refresh_claims()
        self._save()
        return self.state

    def _refresh_claims(self) -> str:
        if not self.state.access_token:
            raise RadioCanadaAuthRequired(f"{self.tenant.label} has no access token")
        self._drop_claims_header()
        data = self._request(
            "GET",
            self.tenant.profile_path,
            params={"device": "web"},
            auth_bearer=self.state.access_token,
            _retry_claims=False,
        )
        if not isinstance(data, dict):
            raise RadioCanadaError(f"{self.tenant.label} profile response is invalid")
        claims = str(data.get("claimsToken") or data.get("claims_token") or "").strip()
        if not claims:
            raise RadioCanadaError(f"{self.tenant.label} profile is missing claimsToken")
        self.state.claims_token = claims
        self.state.claims_expiration_time = float(_jwt_expiry(claims))
        self.session.headers["x-claims-token"] = claims
        return claims

    def ensure_session(self) -> SessionState:
        if self.state.usable():
            if not self.state.claims_usable():
                self._refresh_claims()
                self._save()
            else:
                self.session.headers["x-claims-token"] = self.state.claims_token
            return self.state
        if self.state.refreshable():
            return self.refresh()
        raise RadioCanadaAuthRequired(
            f"not signed in to {self.tenant.label} - open Sign in to pair with a TV code"
        )


    def show(self, title_id: str) -> Show:
        slug, _, selection = title_id.partition("/")
        if not _TITLE_ID_RE.fullmatch(slug) or (selection and not _SHOW_SELECTION_RE.fullmatch(selection)):
            raise RadioCanadaError(f"{self.tenant.label}: invalid programme path")
        path = self.tenant.show_url(slug)
        if selection:
            path = f"{path}/{selection.lower()}"
        data = self._request(
            "GET",
            path,
            params={"device": "web"},
        )
        if not isinstance(data, dict):
            raise RadioCanadaError(f"{self.tenant.label} show response is invalid")
        return show_from_payload(data, title_id=slug)

    def live_event(self, event_id: str) -> LiveChannel:
        if not _TITLE_ID_RE.fullmatch(event_id):
            raise RadioCanadaError(f"{self.tenant.label}: invalid live event")
        path = f"/ott/catalog/v2/{self.tenant.key}/liveevent"
        if not event_id.isdecimal():
            for channel in self.live_channels():
                if channel.event_slug.casefold() == event_id.casefold():
                    return channel
            redirect = self._request("GET", f"{path}/{event_id}/resolve", params={"device": "web"})
            event_id = str(redirect.get("internalId") or "") if isinstance(redirect, dict) else ""
            if not re.fullmatch(r"[0-9]+", event_id):
                raise RadioCanadaError(f"{self.tenant.label}: live event could not be resolved")
        data = self._request("GET", f"{path}/{event_id}", params={"device": "web"})
        event = data.get("event") if isinstance(data, dict) else None
        channel = _live_channel(event, kind="stream") if isinstance(event, dict) else None
        if channel is None:
            raise RadioCanadaError(f"{self.tenant.label}: live event has no playable feed")
        return channel

    def live_channels(self) -> list[LiveChannel]:
        categories = self.live_catalog()
        return list(categories[0].channels) if categories else []

    def live_catalog(self) -> list[LiveCategory]:
        data = self._request(
            "GET",
            self.tenant.live_path,
            params={"device": "web"},
        )
        if not isinstance(data, dict):
            raise RadioCanadaError(f"{self.tenant.label} live response is invalid")
        return live_catalog_from_payload(data)

    def catalog(self, kind: str, catalog_id: str) -> CatalogPage:
        wanted_kind = str(kind or "").strip().lower()
        wanted_id = str(catalog_id or "").strip()
        if wanted_kind not in {"section", "collection", "category"} or not _TITLE_ID_RE.fullmatch(wanted_id):
            raise RadioCanadaError(f"{self.tenant.label}: invalid catalogue route")
        data = self._request(
            "GET",
            f"/ott/catalog/v2/{self.tenant.key}/{wanted_kind}/{wanted_id}",
            params={"device": "web"},
        )
        if not isinstance(data, dict):
            raise RadioCanadaError(f"{self.tenant.label} {wanted_kind} response is invalid")
        return catalog_from_payload(data, kind=wanted_kind, catalog_id=wanted_id)

    def search(self, query: str, *, page_size: int = 50) -> SearchResult:
        wanted = str(query or "").strip()
        if not wanted:
            return SearchResult([], 0)
        size = max(1, min(int(page_size), 100))
        hits: list[SearchHit] = []
        seen: set[tuple[str, str]] = set()
        total = 0
        total_pages = 1
        page = 1
        while page <= total_pages:
            data = self._request(
                "GET",
                self.tenant.search_path,
                params={
                    "device": "web",
                    "term": wanted,
                    "pageNumber": page,
                    "pageSize": size,
                },
            )
            if not isinstance(data, dict):
                raise RadioCanadaError(f"{self.tenant.label} search response is invalid")
            parsed = search_from_payload(data)
            if page == 1:
                total = parsed.total
                total_pages = max(1, min(_safe_int(data.get("totalPages"), 1), 100))
            for hit in parsed.hits:
                identity = hit.kind, hit.url.casefold()
                if identity not in seen:
                    seen.add(identity)
                    hits.append(hit)
            page += 1
        return SearchResult(hits, total or len(hits))

    def stream(
        self,
        media_id: str,
        *,
        title: str = "",
        year: str = "",
        season: int | None = None,
        episode: int | None = None,
        episode_name: str = "",
        series_title: str = "",
        media_type: str = "",
        duration: float | None = None,
        synopsis: str = "",
        cover_url: str = "",
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        app_code: str | None = None,
        is_live: bool = False,
        fetch_chapters: bool = False,
        fetch_attachments: bool = False,
        attachments: tuple[Attachment, ...] = (),
    ) -> Source:
        code = app_code or (LIVE_APP_CODE if is_live else self.tenant.app_code)
        if not (is_live and code == LIVE_APP_CODE):
            self.ensure_session()
        index = self._request(
            "GET",
            MEDIA_INDEX_PATH,
            params={
                "appCode": code,
                "idMedia": str(media_id),
                "output": "jsonObject",
            },
        )
        if not isinstance(index, dict):
            raise RadioCanadaError(f"{self.tenant.label} media index is invalid")

        metas = index.get("Metas") if isinstance(index.get("Metas"), dict) else {}
        protected = str(metas.get("isDrmActive") or "").lower() == "true"
        techs = index.get("availableTechs") or []
        if not isinstance(techs, list):
            techs = []

        tech_name = ""
        if protected:
            for tech in techs:
                if not isinstance(tech, dict):
                    continue
                drm = str(tech.get("drm") or "").lower()
                if "widevine" in drm:
                    tech_name = str(tech.get("name") or "")
                    break
        else:
            for tech in techs:
                if not isinstance(tech, dict):
                    continue
                if not tech.get("drm"):
                    tech_name = str(tech.get("name") or "")
                    break
        if not tech_name:
            raise RadioCanadaError(
                f"{self.tenant.label}: no suitable streaming technology for media {media_id}"
            )

        validation = self._request(
            "GET",
            MEDIA_VALIDATION_PATH,
            params={
                "appCode": code,
                "connectionType": "uhd",
                "deviceType": "android",
                "idMedia": str(media_id),
                "manifestType": "smart-tv",
                "output": "json",
                "tech": tech_name,
            },
        )
        if not isinstance(validation, dict):
            raise RadioCanadaError(f"{self.tenant.label} media validation is invalid")

        manifest = str(validation.get("url") or "").strip()
        if not manifest:
            raise RadioCanadaError(f"{self.tenant.label}: no manifest URL for media {media_id}")

        license_url = ""
        auth_token = ""
        for param in validation.get("params") or []:
            if not isinstance(param, dict):
                continue
            name = str(param.get("name") or "")
            value = str(param.get("value") or "")
            if "widevineLicenseUrl" in name:
                license_url = value
            elif "widevineAuthToken" in name:
                auth_token = value

        return Source(
            media_id=str(media_id),
            manifest=manifest,
            title=title or str(media_id),
            year=year,
            season=season,
            episode=episode,
            episode_name=episode_name,
            series_title=series_title,
            protected=protected,
            tech=tech_name,
            license_url=license_url,
            auth_token=auth_token,
            is_live=is_live,
            app_code=code,
            media_type=media_type,
            duration=duration if is_live else _index_duration(metas, duration),
            synopsis=synopsis,
            cover_url=cover_url,
            starts_at=starts_at,
            ends_at=ends_at,
            chapters=chapters_from_metas(metas, duration) if fetch_chapters and not is_live else (),
            attachments=_selected_artwork(attachments, cover_url, metas) if fetch_attachments else (),
        )

    def widevine_license(
        self,
        challenge: bytes,
        *,
        license_url: str,
        auth_token: str,
    ) -> bytes:
        if not license_url:
            raise RadioCanadaError("no CBC Gem Widevine licence URL for this stream")
        if not auth_token:
            raise RadioCanadaError("no CBC Gem Widevine auth token for this stream")
        try:
            response = self.session.post(
                license_url,
                data=challenge,
                headers={
                    "Content-Type": "application/octet-stream",
                    "x-dt-auth-token": auth_token,
                },
                timeout=_LICENSE_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RadioCanadaError(f"CBC Gem licence request failed: {exc}") from exc
        if response.status_code >= 400:
            raise RadioCanadaError(
                f"CBC Gem licence server returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        return response.content or b""


__all__ = [
    "BASE_URL",
    "GEM",
    "LIVE_APP_CODE",
    "MEDIA_INDEX_PATH",
    "MEDIA_VALIDATION_PATH",
    "TENANTS",
    "CatalogLineup",
    "CatalogPage",
    "Episode",
    "LiveCategory",
    "LiveChannel",
    "MovieItem",
    "ParsedInput",
    "RadioCanadaApi",
    "RadioCanadaAuthRequired",
    "RadioCanadaError",
    "SearchHit",
    "SearchResult",
    "Season",
    "SessionState",
    "Show",
    "Source",
    "Tenant",
    "USER_AGENT",
    "parse_input",
    "catalog_from_payload",
    "show_from_payload",
    "live_catalog_from_payload",
    "live_from_payload",
    "search_from_payload",
]
