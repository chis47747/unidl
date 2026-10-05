"""CBC Gem API: Radio-Canada OTT protocol
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

BASE_URL = "https://services.radio-canada.ca"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) "
    "Gecko/20100101 Firefox/147.0"
)

MEDIA_INDEX_PATH = "/media/meta/v1/index.ashx"
MEDIA_VALIDATION_PATH = "/media/validation/v2"
LIVE_APP_CODE = "medianetlive"

# Seconds shaved off expires_in so we refresh before the server rejects us.
_EXPIRY_SKEW = 3600
_REQUEST_TIMEOUT = 30
_LICENSE_TIMEOUT = 30

_TITLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_GEM_HOST = re.compile(r"(?:^|\.)gem\.cbc\.ca$", re.I)
_TOUTV_HOST = re.compile(r"(?:^|\.)ici\.tou\.tv$", re.I)


class RadioCanadaError(RuntimeError):
    """API, schema or auth failure the service should surface."""


class RadioCanadaAuthRequired(RadioCanadaError):
    """Session is missing or unusable; credentials needed."""


def _jwt_expiry(token: str) -> int:
    try:
        middle = str(token or "").split(".")[1]
        payload = json.loads(
            base64.urlsafe_b64decode(middle + "=" * (-len(middle) % 4))
        )
        return int(payload.get("exp") or 0) if isinstance(payload, dict) else 0
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return 0


# --------------------------------------------------------------------------- tenants


@dataclass(frozen=True)
class Tenant:
    """CBC Gem Radio-Canada OTT identity."""

    key: str
    label: str
    client_id: str
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
    client_id="fc05b0ee-3865-4400-a3cc-3da82c330c23",
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


# --------------------------------------------------------------------------- session


@dataclass
class SessionState:
    """ROPC tokens plus optional claims token for media calls."""

    access_token: str = ""
    refresh_token: str = ""
    expiration_time: float = 0.0
    claims_token: str = ""
    claims_expiration_time: float = 0.0
    username: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_cache(cls, data: dict[str, Any] | None) -> SessionState:
        """Accept native fields or the legacy ``data`` + ``expiration_time`` envelope."""
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
                # ISO and malformed values are not accepted as live tokens.
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
        )

    def to_cache(self) -> dict[str, Any]:
        """Write the legacy envelope so older tools and migrations stay compatible."""
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
        if self.claims_token:
            out["claims_token"] = self.claims_token
            if self.claims_expiration_time:
                out["claims_expiration_time"] = self.claims_expiration_time
        return out

    def usable(self, leeway: int = 60) -> bool:
        return bool(self.access_token) and self.expiration_time > (time.time() + leeway)

    def refreshable(self) -> bool:
        return bool(self.refresh_token)

    def claims_usable(self, leeway: int = 60) -> bool:
        if not self.claims_token:
            return False
        # Older caches may contain an opaque claims token. Keep accepting it and
        # let the claims-specific 401 recovery below establish a dated token.
        return not self.claims_expiration_time or self.claims_expiration_time > (
            time.time() + leeway
        )

    def apply_grant(self, grant: dict[str, Any]) -> None:
        self.raw = dict(grant)
        self.access_token = str(grant.get("access_token") or "")
        self.refresh_token = str(grant.get("refresh_token") or self.refresh_token)
        try:
            expires_in = int(grant.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        # Match legacy: refresh an hour early when the token lives longer than that.
        skew = _EXPIRY_SKEW if expires_in > _EXPIRY_SKEW else max(60, expires_in // 10)
        self.expiration_time = time.time() + max(0, expires_in - skew)
        self.claims_token = ""
        self.claims_expiration_time = 0.0


# --------------------------------------------------------------------------- catalogue models


@dataclass(frozen=True)
class ParsedInput:
    title_id: str
    tenant_hint: str | None = None  # "gem" | "toutv" | None
    kind: str = "show"  # show | section | collection
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


@dataclass(frozen=True)
class SearchHit:
    kind: str  # show | season | media | section | collection
    title: str
    url: str
    info_title: str = ""
    tier: str = ""
    media_id: str = ""
    synopsis: str = ""
    cover_url: str = ""

    @property
    def show_id(self) -> str:
        parts = self.url.strip("/").split("/")
        if self.kind in {"section", "collection"} and len(parts) > 1:
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


# --------------------------------------------------------------------------- parsing


def parse_input(text: str, *, default_tenant: str | None = None) -> ParsedInput | None:
    """Resolve a Gem / TOU.TV show, episode, section or collection URL."""
    raw = (text or "").strip()
    if not raw:
        return None

    tenant_hint = default_tenant
    if "://" in raw or raw.lower().startswith("www.") or "/" in raw:
        candidate = raw if "://" in raw else f"https://{raw}"
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if host:
            if _GEM_HOST.search(host):
                tenant_hint = "gem"
            elif _TOUTV_HOST.search(host):
                tenant_hint = "toutv"
            else:
                return None

        path = (parsed.path or "").strip("/")
        parts = [p for p in path.split("/") if p]
        while parts and parts[0].lower() in {"en", "fr"}:
            parts.pop(0)
        if not parts:
            return None

        kind = "show"
        route = parts[0].lower()
        if route in {"section", "collection"}:
            kind = route
            parts.pop(0)
        elif route in {"media", "show"}:
            parts.pop(0)
        if not parts:
            return None

        # Catalogue URLs append a selected season or episode after the show slug.
        title_id = parts[0].split("?")[0].split("#")[0]
        if not _TITLE_ID_RE.fullmatch(title_id):
            return None
        selection = parts[1].split("?")[0].split("#")[0] if len(parts) > 1 else ""
        if selection and not re.fullmatch(r"s\d+(?:e\d+)?", selection, re.I):
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
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


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
        # "S01E02. Name" style — keep the part after the first period when short.
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
    )


def _parse_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _live_channel(item: dict[str, Any], *, kind: str, feed_type: str = "") -> LiveChannel | None:
    media_id = str(item.get("idMedia") or item.get("id") or "").strip()
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
    ends_at = None
    if isinstance(next_item, dict):
        ends_at = _parse_datetime(next_item.get("airDate"))
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

    # ``liveFeeds`` is the web page's regional selector (label/value pairs), not
    # a second channel list. ``freeTv.items`` already contains every selected
    # region as a playable item, so consuming both would duplicate those feeds.
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
    """Compatibility flat view: unique entries from the synthetic All category."""
    categories = live_catalog_from_payload(data)
    return list(categories[0].channels) if categories else []


def _catalog_hit(raw: dict[str, Any]) -> SearchHit | None:
    kind = str(raw.get("type") or "").strip().lower()
    if kind not in {"show", "season", "media", "section", "collection"}:
        return None
    original_url = str(raw.get("url") or "").strip().strip("/")
    parsed_url = urlparse(f"https://catalog.invalid/{original_url}")
    url = parsed_url.path.strip("/")
    segments = url.split("/") if url else []
    if not segments or not all(_TITLE_ID_RE.fullmatch(segment) for segment in segments):
        return None
    if kind in {"section", "collection"}:
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
    permission = raw.get("permission") if isinstance(raw.get("permission"), dict) else {}
    return SearchHit(
        kind=kind,
        title=str(raw.get("title") or url).strip() or url,
        url=url,
        info_title=str(raw.get("infoTitle") or "").strip(),
        tier=str(raw.get("tier") or permission.get("tier") or "").strip(),
        media_id=str(raw.get("idMedia") or raw.get("formattedIdMedia") or "").strip(),
        synopsis=str(raw.get("description") or "").strip(),
        cover_url=_image_url(raw),
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
    elif kind == "collection":
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


# --------------------------------------------------------------------------- API client


class RadioCanadaApi:
    """HTTP client for CBC Gem."""

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

        # Legacy accepted 426 (upgrade required) alongside 200 for some endpoints.
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
        """Replace a refused claims token, once, without recursive 401 retries."""

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

    # ------------------------------------------------------------------- auth

    def get_settings(self) -> tuple[str, str]:
        data = self._request("GET", self.tenant.settings_path, params={"device": "web"})
        if not isinstance(data, dict):
            raise RadioCanadaError(f"{self.tenant.label} settings response is invalid")
        identity = data.get("identityManagement") if isinstance(data.get("identityManagement"), dict) else {}
        ropc = identity.get("ropc") if isinstance(identity.get("ropc"), dict) else {}
        auth_url = str(ropc.get("url") or "").strip()
        scopes = str(ropc.get("scopes") or "").strip()
        if not auth_url:
            raise RadioCanadaError(f"{self.tenant.label} settings missing ROPC url")
        return auth_url, scopes

    def login(self, username: str, password: str) -> SessionState:
        username = (username or "").strip()
        password = password or ""
        if not username or not password:
            raise RadioCanadaAuthRequired(f"{self.tenant.label} email and password are required")
        self._drop_claims_header()
        auth_url, scopes = self.get_settings()
        grant = self._request(
            "POST",
            auth_url,
            data={
                "client_id": self.tenant.client_id,
                "grant_type": "password",
                "username": username,
                "password": password,
                "scope": scopes,
            },
        )
        if not isinstance(grant, dict) or not grant.get("access_token"):
            raise RadioCanadaError(f"{self.tenant.label} login returned no access token")
        self.state.apply_grant(grant)
        self.state.username = username
        self._refresh_claims()
        self._save()
        return self.state

    def refresh(self) -> SessionState:
        if not self.state.refresh_token:
            raise RadioCanadaAuthRequired(f"{self.tenant.label} has no refresh token")
        # A stale session-wide header must not poison settings, OAuth or profile.
        self._drop_claims_header()
        auth_url, scopes = self.get_settings()
        try:
            grant = self._request(
                "POST",
                auth_url,
                data={
                    "client_id": self.tenant.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": self.state.refresh_token,
                    "scope": scopes,
                },
            )
        except RadioCanadaError as exc:
            raise RadioCanadaAuthRequired(
                f"{self.tenant.label} token refresh failed: {exc}"
            ) from exc
        if not isinstance(grant, dict) or not grant.get("access_token"):
            raise RadioCanadaAuthRequired(f"{self.tenant.label} refresh returned no access token")
        self.state.apply_grant(grant)
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

    def ensure_session(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
    ) -> SessionState:
        if self.state.usable():
            if not self.state.claims_usable():
                self._refresh_claims()
                self._save()
            else:
                self.session.headers["x-claims-token"] = self.state.claims_token
            return self.state
        if self.state.refreshable():
            try:
                return self.refresh()
            except RadioCanadaError:
                if not username or not password:
                    raise
        if username and password:
            return self.login(username, password)
        raise RadioCanadaAuthRequired(
            f"not signed in to {self.tenant.label} - open Sign in or set credentials"
        )

    # --------------------------------------------------------------- catalogue

    def show(self, title_id: str) -> Show:
        data = self._request(
            "GET",
            self.tenant.show_url(title_id),
            params={"device": "web"},
        )
        if not isinstance(data, dict):
            raise RadioCanadaError(f"{self.tenant.label} show response is invalid")
        return show_from_payload(data, title_id=title_id)

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
        if wanted_kind not in {"section", "collection"} or not _TITLE_ID_RE.fullmatch(wanted_id):
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

    # ---------------------------------------------------------------- playback

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
        username: str | None = None,
        password: str | None = None,
    ) -> Source:
        code = app_code or (LIVE_APP_CODE if is_live else self.tenant.app_code)
        # The legacy client intentionally allowed public linear playback without
        # an account. LiveToVod still arrives with ``is_live=False`` and remains
        # authenticated even though it uses the same appCode.
        if not (is_live and code == LIVE_APP_CODE):
            self.ensure_session(username=username, password=password)
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
            duration=duration,
            synopsis=synopsis,
            cover_url=cover_url,
            starts_at=starts_at,
            ends_at=ends_at,
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
