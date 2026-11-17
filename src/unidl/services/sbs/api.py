"""SBS On Demand and SBS Audio catalogues, search, live and playback."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter

WWW = "https://www.sbs.com.au"
AUTH_DEVICE_LINK_URL = "https://auth.sbs.com.au/device/link"
AUTH_DEVICE_POLL_URL = "https://auth.sbs.com.au/device/poll"
AUTH_REFRESH_URL = "https://auth.sbs.com.au/refresh"
CATALOGUE = "https://catalogue.pr.sbsod.com"
SEARCH_URL = "https://content-search.pr.sbsod.com/catalogue"
PLAYBACK_URL = "https://playback.pr.sbsod.com/stream"
EPG_URL = "https://tv-epg.pr.sbsod.com"
IMAGE_URL = "https://image.pr.sbsod.com"
AUDIO_API = "https://fos.sbs.com.au/web/audio"

TV_API_KEY = "ce3f270a-31fb-486e-855e-4f608a62640e"
TV_USER_AGENT = "SBSOnDemand AndroidTV/6.4.0"
DEVICE_NAME = "Android TV - SBS On Demand"

TOKEN_FILE = "sbs_token.json"
DEVICE_ID_FILE = "sbs_device_id.txt"

ENTITY_DETAIL_ENDPOINTS = {
    "movie": "movies",
    "tv-program": "tv-programs",
    "tv-episode": "tv-episodes",
    "news-program": "news-programs",
    "news-episode": "news-episodes",
    "sports-program": "sports-programs",
    "sports-episode": "sports-episodes",
}

SERIES_ENTITY_TYPES = frozenset({"tv-series", "sports-series", "news-series"})
PROGRAM_ENTITY_TYPES = frozenset(
    {"movie", "tv-program", "news-program", "sports-program", "watch"}
)
EPISODE_ENTITY_TYPES = frozenset({"tv-episode", "news-episode", "sports-episode"})

_BARE_ID = re.compile(r"^\d+$")
_MEDIA_TIME = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?")
_UUID_TAIL = re.compile(
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/?$",
    re.IGNORECASE,
)
_KNOWN_ENTITIES = frozenset(
    {
        "tv-series",
        "tv-program",
        "tv-episode",
        "sports-series",
        "sports-program",
        "sports-episode",
        "movie",
        "watch",
        "news-program",
        "news-series",
        "news-episode",
        "collection",
        "collections",
    }
)


class SbsError(RuntimeError):
    """SBS rejected a request or returned an unusable shape."""


@dataclass(frozen=True)
class DeviceLink:
    device_code: str = field(repr=False)
    user_code: str
    verification_url: str
    qr_image_url: str
    interval: int
    expires_at: float

    @property
    def remaining(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())


@dataclass(frozen=True)
class ParsedInput:
    kind: str  # series | program | episode | watch | collection | audio
    value: str = ""
    path: str = ""
    entity: str = ""  # original ondemand segment (movie, tv-program, …)


def parse_input(text: str) -> ParsedInput | None:
    """An sbs.com.au on-demand URL, or a bare mpx media id."""
    raw = (text or "").strip()
    if not raw:
        return None
    if _BARE_ID.fullmatch(raw):
        return ParsedInput("watch", raw, entity="watch")

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    if host != "sbs.com.au" and not host.endswith(".sbs.com.au"):
        return None

    parts = [part for part in parsed.path.split("/") if part]
    audio_entities = {"podcast", "podcast-episode", "radio-program", "radio-channel"}
    if parts and parts[0] != "ondemand" and (
        any(part in audio_entities for part in parts)
        or parts[0] == "audio" and (
            len(parts) == 1 or parts[1] in {"podcasts", "radio", "music", "collection"}
        )
    ):
        return ParsedInput("audio", path=parsed.path, value=parsed.path)
    if len(parts) < 2 or parts[0] != "ondemand":
        return None

    entity = parts[1].lower()
    if entity not in _KNOWN_ENTITIES:
        return None

    value = parts[-1]
    path = "/" + "/".join(parts)
    if entity in SERIES_ENTITY_TYPES:
        slug = parts[2] if len(parts) > 2 else value
        return ParsedInput("series", slug, path=path, entity=entity)
    if entity in EPISODE_ENTITY_TYPES:
        return ParsedInput("episode", value, path=path, entity=entity)
    if entity in {"collection", "collections"}:
        return ParsedInput("collection", value, path=path, entity=entity)
    if entity == "watch":
        return ParsedInput("watch", value, path=path, entity=entity)
    return ParsedInput("program", value, path=path, entity=entity)


@dataclass
class Session:
    """Cached auth tokens from login/refresh."""

    email: str = ""
    device_name: str = DEVICE_NAME
    device_id: str = ""
    access_token: str = ""
    id_token: str = ""
    access_token_exp: int = 0
    id_token_exp: int = 0
    refresh_token: str = ""
    refresh_token_exp: int = 0
    authenticated_cookie: str = ""
    updated_at: int = 0

    def token_valid(self, min_remaining: int = 300) -> bool:
        if not self.access_token or not self.access_token_exp:
            return False
        return self.access_token_exp - int(time.time()) > min_remaining

    def to_cache(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "deviceName": self.device_name,
            "deviceId": self.device_id,
            "updatedAt": self.updated_at or int(time.time()),
            "accessToken": self.access_token,
            "idToken": self.id_token,
            "accessTokenExp": self.access_token_exp,
            "idTokenExp": self.id_token_exp,
            "refreshToken": self.refresh_token,
            "refreshTokenExp": self.refresh_token_exp,
            "authenticatedCookie": self.authenticated_cookie,
        }

    @classmethod
    def from_cache(cls, data: dict[str, Any] | None) -> Session:
        if not data:
            return cls()
        access = str(data.get("accessToken") or data.get("access_token") or "")
        identity = str(data.get("idToken") or data.get("id_token") or "")
        access_exp = int(data.get("accessTokenExp") or data.get("access_token_exp") or 0)
        id_exp = int(data.get("idTokenExp") or data.get("id_token_exp") or 0)
        if not access_exp:
            access_exp = _token_exp(access)
        if not id_exp:
            id_exp = _token_exp(identity)
        cookie = str(data.get("authenticatedCookie") or data.get("authenticated_cookie") or "")
        if not cookie:
            cookie = _build_authenticated_cookie(identity) or ""
        return cls(
            email=str(data.get("email") or ""),
            device_name=str(data.get("deviceName") or data.get("device_name") or DEVICE_NAME),
            device_id=str(data.get("deviceId") or data.get("device_id") or ""),
            access_token=access,
            id_token=identity,
            access_token_exp=access_exp,
            id_token_exp=id_exp,
            refresh_token=str(data.get("refreshToken") or data.get("refresh_token") or ""),
            refresh_token_exp=int(
                data.get("refreshTokenExp") or data.get("refresh_token_exp") or 0
            ),
            authenticated_cookie=cookie,
            updated_at=int(data.get("updatedAt") or data.get("updated_at") or 0),
        )


@dataclass(frozen=True)
class SearchHit:
    entity_type: str
    title: str
    slug: str = ""
    id: str = ""
    mpx_media_id: str = ""
    url: str = ""
    description: str = ""

    @property
    def label(self) -> str:
        return self.title

    @property
    def is_series(self) -> bool:
        return self.entity_type in {"TV_SERIES", "SPORTS_SERIES", "NEWS_SERIES"}

    @property
    def is_playable(self) -> bool:
        return self.entity_type in {
            "MOVIE",
            "TV_PROGRAM",
            "TV_EPISODE",
            "NEWS_PROGRAM",
            "NEWS_EPISODE",
            "SPORTS_PROGRAM",
            "SPORTS_EPISODE",
        } or bool(self.mpx_media_id)

    @property
    def is_collection(self) -> bool:
        return self.entity_type == "CURATED_COLLECTION"


@dataclass(frozen=True)
class MediaItem:
    """A single playable title (film, program, episode or live channel)."""

    id: str
    name: str
    kind: str = "movie"  # movie | episode | live
    series_title: str = ""
    season: int | None = None
    episode: int | None = None
    year: str = ""
    language: str = "en"
    synopsis: str = ""

    @property
    def label(self) -> str:
        if self.kind == "episode" and self.episode is not None:
            season = self.season if self.season is not None else 0
            return f"S{season:02d}E{self.episode:02d}  {self.name}"
        if self.year:
            return f"{self.name} ({self.year})"
        return self.name


@dataclass
class Season:
    number: int
    title: str
    episodes: list[MediaItem] = field(default_factory=list)

    @property
    def label(self) -> str:
        name = self.title or f"Season {self.number}"
        if self.title and self.title != f"Season {self.number}":
            name = f"Season {self.number}: {self.title}"
        count = len(self.episodes)
        return f"{name}  ·  {count} episode(s)" if count else name


@dataclass
class Series:
    title: str
    slug: str = ""
    seasons: list[Season] = field(default_factory=list)


@dataclass(frozen=True)
class Channel:
    id: str
    title: str
    mpx_media_id: str
    ibms_code: str = ""
    v3_code: str = ""

    @property
    def code(self) -> str:
        return self.v3_code or self.ibms_code or self.mpx_media_id


@dataclass(frozen=True)
class Subtitle:
    url: str
    lang: str = ""
    format: str = ""


@dataclass(frozen=True)
class Source:
    manifest: str
    subtitles: tuple[Subtitle, ...] = ()
    protocol: str = "HLS"
    is_live: bool = False
    chapters: tuple[Chapter, ...] = ()
    attachments: tuple[Attachment, ...] = ()

    def line(self) -> str:
        n = len(self.subtitles)
        mode = "live" if self.is_live else "vod"
        base = f"{self.protocol} · {mode} · clear"
        return f"{base} · {n} sidecar subtitle(s)" if n else base


@dataclass(frozen=True)
class AudioItem:
    id: str
    name: str
    kind: str
    url: str
    source: str = ""
    programme: str = ""
    artist: str = ""
    synopsis: str = ""
    language: str = ""
    cover: str = ""
    published: str = ""
    duration: float | None = None
    episode: int | None = None
    season: int | None = None

    @property
    def label(self) -> str:
        if self.kind == "radio-program" and self.published:
            return f"{self.name} · {self.published}"
        number = f"E{self.episode:02d} " if self.episode else ""
        return f"{number}{self.name}"

    @property
    def type_label(self) -> str:
        return {
            "podcast": "Podcast series", "podcast-episode": "Podcast episode",
            "radio-program": "Radio catch-up", "radio-channel": "Live radio",
            "collection": "Audio collection",
        }[self.kind]


@dataclass(frozen=True)
class AudioPage:
    title: str
    items: tuple[AudioItem, ...]
    next_page: int | None = None


class _AudioPageParser(HTMLParser):
    """Read the site's server-rendered JSON, including byte-sized Flight text rows."""

    def __init__(self) -> None:
        super().__init__()
        self._script: list[str] | None = None
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._script = []

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or self._script is None:
            return
        script = "".join(self._script)
        self._script = None
        prefix = "self.__next_f.push("
        if script.startswith(prefix):
            try:
                payload, _ = json.JSONDecoder().raw_decode(script[len(prefix):])
            except ValueError:
                return
            if isinstance(payload, list) and len(payload) == 2 and payload[0] == 1 and isinstance(payload[1], str):
                self.chunks.append(payload[1])

    def objects(self) -> Iterator[Any]:
        data = "".join(self.chunks).encode("utf-8")
        position = 0
        while position < len(data):
            colon = data.find(b":", position)
            if colon < 0 or not re.fullmatch(rb"[0-9a-f]*", data[position:colon]):
                raise SbsError("SBS Audio returned invalid page data")
            start = colon + 1
            if data[start:start + 1] == b"T":
                comma = data.find(b",", start)
                try:
                    length = int(data[start + 1:comma], 16)
                except ValueError as exc:
                    raise SbsError("SBS Audio returned invalid text data") from exc
                position = comma + 1 + length
                if comma < 0 or position > len(data):
                    raise SbsError("SBS Audio returned truncated text data")
                continue
            end = data.find(b"\n", start)
            if end < 0:
                end = len(data)
            row = data[start:end]
            position = end + 1
            if row.startswith((b"{", b"[")):
                try:
                    yield json.loads(row)
                except ValueError as exc:
                    raise SbsError("SBS Audio returned invalid JSON page data") from exc


def _audio_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _audio_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _audio_dicts(child)


def _audio_image(data: dict[str, Any]) -> str:
    watermark = data.get("watermarkImageView") or {}
    if watermark.get("src"):
        return str(watermark["src"])
    attributes = (data.get("leadImage") or {}).get("attributes") or {}
    sizes = attributes.get("sizes") or []
    square = next((size for size in sizes if size.get("name") == "1x1"), None)
    image = square or attributes.get("size") or next(iter(sizes), {})
    return str(image.get("src") or "")


def _normalize_radio_source(value: str) -> str:
    """Correct SBS's legacy playlist extension for the StreamGuys HLS host."""
    parsed = urlparse(str(value or ""))
    if (
        parsed.hostname == "sbs-hls.streamguys1.com"
        and parsed.path.endswith("/playlist.m3u")
    ):
        return parsed._replace(path=f"{parsed.path}8").geturl()
    return str(value or "")


def _audio_item(data: dict[str, Any], parent: dict[str, Any] | None = None) -> AudioItem | None:
    parent = parent or {}
    route = data.get("route") or {}
    kind = route.get("contentType")
    if kind is None and isinstance(data.get("audio"), dict):
        kind = "podcast-episode"
    if kind not in {"podcast", "podcast-episode", "radio-program", "radio-channel", "collection-page"}:
        return None
    url = urljoin(WWW, str(route.get("permalink") or "")) if route.get("permalink") else ""
    if not url or not (target := parse_input(url)) or target.kind != "audio":
        return None
    series = next(iter(data.get("podcastSeries") or []), parent)
    audio = data.get("audio") or {}
    language = (audio.get("localeView") or {}).get("languageCode")
    if not language:
        language = next(iter(series.get("audioLanguages") or []), "")
    if not language:
        language = (parent.get("localeView") or {}).get("languageCode") or ""
    date = data.get("displayDate") or data.get("publishDate") or {}
    published = str(date.get("utcDateTime") or "")[:10]
    if not published and date.get("unixEpochMilli"):
        published = datetime.fromtimestamp(int(date["unixEpochMilli"]) / 1000, timezone.utc).date().isoformat()
    return AudioItem(
        id=str(data.get("id") or data.get("bspId") or route.get("UUID") or url),
        name=str(data.get("headline") or data.get("name") or data.get("title") or "SBS Audio"),
        kind="collection" if kind == "collection-page" else kind,
        url=url,
        source=str(data.get("monetisationUrl") or audio.get("url") or data.get("streamUrl") or ""),
        programme=str(series.get("headline") or series.get("title") or ""),
        artist=", ".join(str(author["name"]) for author in (data.get("authors") or series.get("authors") or []) if author.get("name")),
        synopsis=str((data.get("seo") or {}).get("description") or data.get("description") or ""),
        language=str(language or ""),
        cover=_audio_image(series) or _audio_image(data),
        published=published,
        duration=float(audio["duration"]) if audio.get("duration") else None,
        episode=_to_int(data.get("episodeNumber")) or None,
        season=_to_int(data.get("seasonNumber")) or None,
    )


def _media_time_ms(value: object) -> int | None:
    match = _MEDIA_TIME.fullmatch(value) if isinstance(value, str) else None
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = match.groups()
    try:
        return (int(hours or 0) * 3600 + int(minutes or 0) * 60) * 1000 + round(float(seconds or 0) * 1000)
    except (ValueError, OverflowError):
        return None


def _playback_chapters(data: dict[str, Any]) -> tuple[Chapter, ...]:
    events = data.get("playbackEvents")
    if not isinstance(events, list):
        return ()
    duration = _media_time_ms(data.get("duration"))
    markers: dict[str, int] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind not in ("recapBegin", "recapEnd", "openingCreditsBegin", "openingCreditsEnd", "closingCreditsBegin"):
            continue
        position = _media_time_ms(event.get("time"))
        if position is not None and (duration is None or position <= duration):
            markers.setdefault(kind, position)

    chapters: list[Chapter] = []
    opening = markers.get("openingCreditsBegin")
    opening_end = markers.get("openingCreditsEnd")
    closing = markers.get("closingCreditsBegin")
    recap = markers.get("recapBegin")
    recap_end = markers.get("recapEnd")
    if recap is not None and (duration is None or recap < duration):
        following = min(
            (position for position in (opening, closing, duration) if position is not None and position > recap),
            default=None,
        )
        if recap_end is not None and (recap_end <= recap or (following is not None and recap_end > following)):
            recap_end = None
        chapters.append(Chapter(recap, "Recap", end_ms=recap_end, kind="recap"))
    if opening is not None and (duration is None or opening < duration) and (closing is None or opening < closing):
        if opening_end is not None and (opening_end <= opening or (closing is not None and opening_end > closing)):
            opening_end = None
        chapters.append(Chapter(opening, "Opening credits", end_ms=opening_end, kind="intro"))
    if closing is not None and (duration is None or closing < duration):
        chapters.append(Chapter(closing, "Closing credits", end_ms=duration, kind="credits"))
    return tuple(chapters)


def _playback_attachments(images: object) -> tuple[Attachment, ...]:
    if not isinstance(images, list):
        return ()
    attachments: list[Attachment] = []
    seen: set[str] = set()
    for image in images:
        if not isinstance(image, dict):
            continue
        try:
            image_id = str(uuid.UUID(str(image.get("id") or "")))
        except ValueError:
            continue
        if image_id in seen:
            continue
        seen.add(image_id)
        category = str(image.get("category") or "")
        aspect, _, label = category.partition("|")
        label = label.rsplit("|", 1)[-1] if label else aspect
        name = label.replace("_", " ").title() or "Artwork"
        if "|" in category:
            name = f"{name} ({aspect})"
        kind = "poster" if aspect == "2:3" else "artwork"
        attachments.append(Attachment(url=f"{IMAGE_URL}/{image_id}", name=name, kind=kind))
    return tuple(attachments)


def _decode_jwt(token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
    except Exception:
        return {}


def _token_exp(token: str | None) -> int:
    return int(_decode_jwt(token).get("exp") or 0)


def _build_authenticated_cookie(id_token: str | None) -> str | None:
    claims = _decode_jwt(id_token)
    email = claims.get("email")
    issued_at = claims.get("iat")
    if not email or issued_at is None:
        return None
    email_hash = hashlib.sha256(str(email).lower().encode("utf-8")).hexdigest()
    return f"{email_hash}.{issued_at}"


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _year_str(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(value)


def _catalogue_series_path(path: str) -> str:
    """Translate an SBS website series path to the FDA catalogue path."""
    raw = (path or "").strip()
    if not raw:
        raise SbsError("Missing SBS series path")

    parsed_path = urlparse(raw).path if "://" in raw else "/" + raw.lstrip("/")
    parts = [part for part in parsed_path.split("/") if part]
    if parts and parts[0] == "ondemand":
        parts = parts[1:]
    if len(parts) < 2 or parts[0] not in SERIES_ENTITY_TYPES:
        raise SbsError(f"Unsupported SBS series path: {path}")
    if len(parts) > 2 and _UUID_TAIL.search("/" + "/".join(parts)):
        parts = parts[:-1]
    return "/" + "/".join(parts)


def _error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return (
                str(payload.get("message") or payload.get("detail") or "")
                or json.dumps(payload, ensure_ascii=False)[:300]
            )
        return str(payload)[:300]
    except Exception:
        return ((response.text or "").strip() or response.reason or "empty response")[:500]


def _ondemand_url_from_search(result: dict[str, Any]) -> str | None:
    entity_type = (result.get("entityType") or "").upper()
    slug = result.get("slug")
    media_id = result.get("mpxMediaID") or result.get("mpxMediaId") or result.get("id")
    if not slug and not media_id:
        return None

    entity_path_by_type = {
        "MOVIE": "movie",
        "TV_PROGRAM": "tv-program",
        "TV_EPISODE": "tv-episode",
        "TV_SERIES": "tv-series",
        "NEWS_PROGRAM": "news-program",
        "NEWS_EPISODE": "news-episode",
        "NEWS_SERIES": "news-series",
        "SPORTS_PROGRAM": "sports-program",
        "SPORTS_EPISODE": "sports-episode",
        "SPORTS_SERIES": "sports-series",
        "CURATED_COLLECTION": "collection",
    }
    entity_path = entity_path_by_type.get(entity_type, entity_type.lower().replace("_", "-"))

    if entity_type in {"TV_SERIES", "SPORTS_SERIES", "NEWS_SERIES", "CURATED_COLLECTION"} and slug:
        return f"{WWW}/ondemand/{entity_path}/{slug}"
    if slug and media_id:
        return f"{WWW}/ondemand/{entity_path}/{slug}/{media_id}"
    if media_id:
        return f"{WWW}/ondemand/watch/{media_id}"
    return None


class SbsAudioApi:
    """Public SBS Audio catalogue and the exact sources supplied by its website."""

    def __init__(self, session: requests.Session):
        self.session = session

    def _document(self, path: str, **params: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        response = self.session.get(
            urljoin(WWW, path), params=params or None,
            headers={"Accept": "text/html"}, timeout=25,
        )
        if response.status_code != 200:
            raise SbsError(f"SBS Audio page failed ({response.status_code})")
        parser = _AudioPageParser()
        parser.feed(response.text)
        content: dict[str, Any] = {}
        config: dict[str, Any] = {}
        for record in parser.objects():
            for value in _audio_dicts(record):
                if "baseUrl" in value and isinstance(value.get("content"), dict):
                    content = value["content"]
                if "clientEnvVars" in value and isinstance(value.get("config"), dict):
                    config = value["config"]
        if not content:
            raise SbsError("SBS Audio page contains no catalogue data")
        return content, config

    def _json(self, endpoint: str, **params: Any) -> dict[str, Any]:
        response = self.session.get(
            f"{AUDIO_API}/{endpoint}", params=params,
            headers={"Accept": "application/json"}, timeout=20,
        )
        if response.status_code != 200:
            raise SbsError(f"SBS Audio request failed ({response.status_code}): {_error_message(response)}")
        try:
            data = response.json()
        except ValueError as exc:
            raise SbsError("SBS Audio returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise SbsError("SBS Audio returned invalid catalogue data")
        return data

    @staticmethod
    def _catalogue(blocks: Any, config: dict[str, Any]) -> tuple[AudioItem, ...]:
        result: dict[str, AudioItem] = {}
        streams = (config.get("radio") or {}).get("livestreamUrls") or {}

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                item = _audio_item(value)
                if item is not None:
                    source = item.source
                    if item.kind == "radio-channel" and streams.get(value.get("epgId")):
                        source = streams[value["epgId"]]
                    source = _normalize_radio_source(source)
                    if source != item.source:
                        item = replace(item, source=source)
                    result.setdefault(item.id, item)
                    return
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(blocks)
        return tuple(result.values())

    def page(self, path: str, page: int = 1) -> AudioPage:
        parsed = parse_input(urljoin(WWW, path))
        if parsed is None or parsed.kind != "audio":
            raise SbsError("Unsupported SBS Audio link")
        content, config = self._document(parsed.path)
        title = str(content.get("headline") or content.get("name") or content.get("title") or "SBS Audio")
        kind = (content.get("route") or {}).get("contentType")
        if kind == "podcast":
            podcast_id = content.get("id")
            if not isinstance(podcast_id, str) or not podcast_id:
                raise SbsError("SBS Audio returned no podcast identifier")
            data = self._json(
                "podcast-recommendations", pathOrId=podcast_id, limit=30,
                offset=(page - 1) * 30, site="audio",
            ).get("content")
            if not isinstance(data, dict) or not isinstance(data.get("podcastEpisodes"), list):
                raise SbsError("SBS Audio returned no podcast episode list")
            metadata = {row["id"]: row for row in content.get("podcastEpisodes") or [] if row.get("id")}
            items = tuple(
                item for row in data["podcastEpisodes"] if isinstance(row, dict)
                and (item := _audio_item({**metadata.get(row.get("id"), {}), **row}, content)) is not None
            )
            return AudioPage(title, items, page + 1 if data.get("podcastEpisodesHasNext") else None)
        if kind == "radio-program" and isinstance(content.get("catchupContent"), list):
            items = tuple(
                item for row in content["catchupContent"] if isinstance(row, dict)
                and (item := _audio_item({
                    **row, "id": row.get("GUID"), "route": content["route"],
                }, content)) is not None
            )
            return AudioPage(title, items)
        if kind in {"podcast-episode", "radio-channel"}:
            items = self._catalogue([content], config)
            if not items or not items[0].source:
                raise SbsError("SBS Audio returned no playable audio source")
            return AudioPage(title, items)
        return AudioPage(title, self._catalogue(content.get("blocks") or [], config))

    def stations(self) -> AudioPage:
        content, config = self._document("/audio/radio")
        return AudioPage("SBS Radio", tuple(
            item for item in self._catalogue(content.get("blocks") or [], config)
            if item.kind == "radio-channel"
        ))

    def search(self, query: str, page: int = 1) -> AudioPage:
        content, _ = self._document("/search", query=query, filter="audio", page=page, sort="relevancy")
        search = next((block["search"] for block in content.get("blocks") or [] if isinstance(block.get("search"), dict)), {})
        rows = search.get("items") or []
        items = []
        for row in rows:
            target = parse_input(str(row.get("link") or ""))
            if target is None or target.kind != "audio":
                continue
            kind = str(row.get("type") or "")
            if kind == "collection-page" or kind == "sbs-page":
                kind = "collection"
            if kind not in {"podcast", "podcast-episode", "radio-program", "radio-channel", "collection"}:
                continue
            items.append(AudioItem(
                id=str(row["link"]), name=str(row.get("title") or "SBS Audio"), kind=kind,
                url=str(row["link"]), synopsis=str(row.get("description") or ""),
                cover=str((row.get("image") or {}).get("src") or ""),
            ))
        total = min(_to_int(search.get("totalResults")), 100)
        return AudioPage(f"SBS Audio · {query}", tuple(items), page + 1 if page * 10 < total else None)


class SbsApi:
    def __init__(
        self,
        session: requests.Session | None = None,
        state: Session | None = None,
        *,
        on_save: Any = None,
    ):
        self.session = session or requests.Session()
        self.state = state or Session()
        self.on_save = on_save  # optional callable(Session) after token updates
        if not self.state.device_id:
            self.state.device_id = str(uuid.uuid4())

    def tv_headers(
        self,
        authorization: str | None = None,
        *,
        accept: str = "application/json",
    ) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "Accept-Language": "en",
            "Content-Type": "application/json",
            "User-Agent": TV_USER_AGENT,
            "x-api-key": TV_API_KEY,
        }
        if authorization:
            headers["Authorization"] = f"Bearer {authorization}"
        return headers

    def _apply_cookies(self) -> None:
        if self.state.refresh_token:
            self.session.cookies.set(
                "auth.refresh-token", self.state.refresh_token, domain=".sbs.com.au", path="/"
            )
        if self.state.authenticated_cookie:
            self.session.cookies.set(
                "auth.authenticated",
                self.state.authenticated_cookie,
                domain=".sbs.com.au",
                path="/",
            )

    def _save(self) -> None:
        if callable(self.on_save):
            self.on_save(self.state)

    def _update_from_auth(self, auth_data: dict[str, Any], *, persist: bool = True) -> None:
        self.state.access_token = str(auth_data.get("accessToken") or "")
        self.state.id_token = str(auth_data.get("idToken") or "")
        self.state.access_token_exp = _token_exp(self.state.access_token)
        self.state.id_token_exp = _token_exp(self.state.id_token)
        refresh = auth_data.get("refreshToken") or self.state.refresh_token
        self.state.refresh_token = str(refresh or "")
        self.state.refresh_token_exp = _token_exp(self.state.refresh_token) or self.state.refresh_token_exp
        cookie = _build_authenticated_cookie(self.state.id_token)
        self.state.authenticated_cookie = cookie or self.state.authenticated_cookie
        self.state.updated_at = int(time.time())
        if not self.state.email:
            claims = _decode_jwt(self.state.id_token)
            self.state.email = str(claims.get("email") or "")
        self._apply_cookies()
        if persist:
            self._save()

    def start_tv_link(self) -> DeviceLink:
        started = time.monotonic()
        response = self.session.post(
            AUTH_DEVICE_LINK_URL,
            json={"deviceName": self.state.device_name or DEVICE_NAME},
            headers=self.tv_headers(),
            timeout=15,
        )
        if response.status_code != 200:
            raise SbsError(f"SBS TV-code login could not start: {_error_message(response)}")
        try:
            data = response.json()
        except ValueError as exc:
            raise SbsError("SBS TV-code login returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise SbsError("SBS TV-code login returned an invalid challenge")
        device_code = data.get("deviceCode")
        user_code = data.get("userCode")
        verification_url = data.get("verifyURI")
        interval = data.get("interval")
        expires_in = data.get("expiresIn")
        if (
            not isinstance(device_code, str) or not device_code.strip()
            or not isinstance(user_code, str) or not user_code.strip()
            or not isinstance(verification_url, str) or not verification_url.strip()
            or type(interval) is not int or interval <= 0
            or type(expires_in) is not int or expires_in <= 0
        ):
            raise SbsError("SBS TV-code login returned an incomplete challenge")
        verification_url = verification_url.strip()
        if "://" not in verification_url:
            verification_url = f"https://{verification_url}"
        if urlparse(verification_url).scheme != "https" or not urlparse(verification_url).hostname:
            raise SbsError("SBS TV-code login returned an invalid verification URL")
        qr_image_url = data.get("qrCodeURI") or ""
        if not isinstance(qr_image_url, str) or (
            qr_image_url and (
                urlparse(qr_image_url).scheme != "https" or not urlparse(qr_image_url).hostname
            )
        ):
            raise SbsError("SBS TV-code login returned an invalid QR image URL")
        return DeviceLink(
            device_code=device_code.strip(),
            user_code=user_code.strip(),
            verification_url=verification_url,
            qr_image_url=qr_image_url,
            interval=interval,
            expires_at=started + expires_in,
        )

    def poll_tv_link(self, challenge: DeviceLink) -> Session | None:
        remaining = challenge.remaining
        if remaining <= 0:
            raise SbsError("SBS TV code expired. Choose Sign in to request a new code.")
        response = self.session.post(
            AUTH_DEVICE_POLL_URL,
            json={"deviceCode": challenge.device_code, "refreshTokenInBody": True},
            headers=self.tv_headers(),
            timeout=min(10.0, remaining),
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise SbsError("SBS TV-code login returned invalid JSON") from exc
        if (
            response.status_code == 400 and isinstance(data, dict)
            and data.get("detail") == "authorization_pending"
        ):
            return None
        if response.status_code != 200:
            raise SbsError(f"SBS TV-code login failed: {_error_message(response)}")
        if not isinstance(data, dict) or not all(
            isinstance(data.get(key), str) and data[key].strip()
            for key in ("accessToken", "idToken")
        ):
            raise SbsError("SBS TV-code login returned incomplete tokens")
        if _token_exp(data["accessToken"]) <= int(time.time()):
            raise SbsError("SBS TV-code login returned an expired access token")
        # The service commits tokens after the activation wait completes.
        self._update_from_auth(data, persist=False)
        return self.state

    def refresh(self) -> bool:
        refresh_token = self.state.refresh_token
        if not refresh_token:
            return False
        self._apply_cookies()
        payload = {
            "deviceName": self.state.device_name or DEVICE_NAME,
            "refreshToken": refresh_token,
            "refreshTokenInBody": True,
        }
        response = self.session.post(
            AUTH_REFRESH_URL, json=payload, headers=self.tv_headers(), timeout=30
        )
        if response.status_code != 200:
            return False
        data = response.json()
        if not isinstance(data, dict) or not data.get("accessToken"):
            return False
        self._update_from_auth(data)
        return True

    def ensure_auth(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self.state.token_valid():
            return self.state.access_token
        if self.refresh():
            return self.state.access_token
        raise SbsError(
            "SBS session expired. Choose Sign in to pair using a TV code."
        )

    def request(self, method: str, url: str, *, auth: bool = False, **kwargs: Any) -> Any:
        supplied = kwargs.pop("headers", None) or {}
        headers = self.tv_headers()
        headers.update(supplied)
        if auth or url.startswith(CATALOGUE):
            if "Authorization" not in headers:
                token = self.ensure_auth()
                headers["Authorization"] = f"Bearer {token}"
        timeout = kwargs.pop("timeout", 30)
        response = self.session.request(method, url, headers=headers, timeout=timeout, **kwargs)
        if response.status_code == 401 and (auth or url.startswith(CATALOGUE)):
            token = self.ensure_auth(force_refresh=True)
            headers["Authorization"] = f"Bearer {token}"
            response = self.session.request(
                method, url, headers=headers, timeout=timeout, **kwargs
            )
        if response.status_code != 200:
            raise SbsError(
                f"Request failed ({response.status_code}) for {url}: {_error_message(response)}"
            )
        try:
            return response.json()
        except ValueError:
            return response.text

    def search(self, query: str, *, limit: int = 24) -> list[SearchHit]:
        data = self.request("GET", SEARCH_URL, params={"q": query.strip(), "limit": limit})
        if not isinstance(data, dict):
            return []
        hits: list[SearchHit] = []
        for result in data.get("items") or []:
            if not isinstance(result, dict):
                continue
            entity_type = (result.get("entityType") or "UNKNOWN").upper()
            if entity_type == "PAGE":
                continue
            mpx = result.get("mpxMediaID") or result.get("mpxMediaId")
            result_url = result.get("url") or _ondemand_url_from_search(result) or ""
            if not mpx and result_url:
                match = re.search(r"/(\d+)/?$", str(result_url))
                if match:
                    mpx = match.group(1)
            hits.append(
                SearchHit(
                    entity_type=entity_type,
                    title=str(result.get("title") or mpx or result.get("id") or "Untitled"),
                    slug=str(result.get("slug") or ""),
                    id=str(result.get("id") or ""),
                    mpx_media_id=str(mpx or ""),
                    url=str(result_url or ""),
                    description=str(result.get("description") or ""),
                )
            )
        return hits

    def channels(self) -> list[Channel]:
        data = self.request("GET", f"{EPG_URL}/channels")
        if not isinstance(data, dict):
            return []
        channels: list[Channel] = []
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            mpx = item.get("mpxMediaID") or item.get("mpxMediaId")
            if not mpx:
                continue
            channels.append(
                Channel(
                    id=str(item.get("id") or mpx),
                    title=str(item.get("title") or item.get("name") or mpx),
                    mpx_media_id=str(mpx),
                    ibms_code=str(item.get("ibmsChannelCode") or ""),
                    v3_code=str(item.get("v3ChannelCode") or ""),
                )
            )
        return channels

    def _build_media_item(self, metadata: dict[str, Any], *, fallback: str = "movie") -> MediaItem:
        mpx = metadata.get("mpxMediaID") or metadata.get("mpxMediaId")
        if not mpx:
            raise SbsError("No mpxMediaId found in catalogue response")
        title = str(metadata.get("title") or metadata.get("cdpTitle") or mpx)
        year = _year_str(metadata.get("releaseYear"))
        language = str(metadata.get("localeID") or "en")
        if fallback == "episode":
            return MediaItem(
                id=str(mpx),
                name=title,
                kind="episode",
                series_title=str(
                    metadata.get("seriesTitle") or metadata.get("programTitle") or title
                ),
                season=_to_int(metadata.get("seasonNumber")) or None,
                episode=_to_int(metadata.get("episodeNumber")) or None,
                year=year,
                language=language,
                synopsis=str(metadata.get("description") or metadata.get("synopsis") or ""),
            )
        return MediaItem(
            id=str(mpx),
            name=title,
            kind="movie",
            year=year,
            language=language,
            synopsis=str(metadata.get("description") or metadata.get("synopsis") or ""),
        )

    def mpx_media(self, media_id: str, *, as_episode: bool = False) -> MediaItem:
        data = self.request("GET", f"{CATALOGUE}/mpx-media/{media_id}")
        if not isinstance(data, dict):
            raise SbsError(f"Unexpected mpx-media response for {media_id}")
        return self._build_media_item(data, fallback="episode" if as_episode else "movie")

    def entity_item(self, entity_type: str, entity_id: str) -> MediaItem:
        endpoint = ENTITY_DETAIL_ENDPOINTS.get(entity_type)
        if not endpoint:
            raise SbsError(f"Unsupported entity type for catalogue detail: {entity_type}")
        data = self.request("GET", f"{CATALOGUE}/{endpoint}/{entity_id}")
        if not isinstance(data, dict):
            raise SbsError(f"Unexpected entity response for {entity_type}/{entity_id}")
        fallback = "episode" if entity_type.endswith("episode") else "movie"
        return self._build_media_item(data, fallback=fallback)

    def program(self, entity: str, entity_id: str) -> MediaItem:
        if _BARE_ID.fullmatch(str(entity_id)) or entity == "watch":
            return self.mpx_media(str(entity_id), as_episode=False)
        return self.entity_item(entity, entity_id)

    def episode(self, entity: str, entity_id: str) -> MediaItem:
        if _BARE_ID.fullmatch(str(entity_id)):
            return self.mpx_media(str(entity_id), as_episode=True)
        return self.entity_item(entity, entity_id)

    def series(self, path: str) -> Series:
        """Load a website series URL/path through the FDA catalogue endpoint."""
        catalogue_path = _catalogue_series_path(path)
        data = self.request("GET", f"{CATALOGUE}{catalogue_path}")
        if not isinstance(data, dict):
            raise SbsError("Unexpected series response")
        seasons_raw = data.get("seasons") or []
        if not seasons_raw:
            raise SbsError("No seasons found")

        series_title = str(data.get("title") or "Series")
        season_map: dict[tuple[int, str], Season] = {}
        for season in seasons_raw:
            if not isinstance(season, dict):
                continue
            number = _to_int(season.get("seasonNumber"))
            season_title = str(season.get("title") or f"Season {number}")
            key = (number, season_title)
            bucket = season_map.get(key)
            if bucket is None:
                bucket = Season(number=number, title=season_title)
                season_map[key] = bucket
            for ep in season.get("episodes") or []:
                if not isinstance(ep, dict):
                    continue
                mpx = ep.get("mpxMediaID") or ep.get("mpxMediaId")
                if not mpx:
                    continue
                bucket.episodes.append(
                    MediaItem(
                        id=str(mpx),
                        name=str(ep.get("title") or mpx),
                        kind="episode",
                        series_title=str(ep.get("seriesTitle") or series_title),
                        season=_to_int(ep.get("seasonNumber")) or number or None,
                        episode=_to_int(ep.get("episodeNumber")) or None,
                        year=_year_str(ep.get("releaseYear")),
                        language=str(data.get("localeID") or "en"),
                    )
                )
        seasons = list(season_map.values())
        for season in seasons:
            season.episodes.sort(key=lambda item: item.episode or 0)
        seasons.sort(key=lambda item: item.number)
        return Series(
            title=series_title,
            slug=catalogue_path.rstrip("/").rsplit("/", 1)[-1],
            seasons=seasons,
        )

    def _playback_payload(self) -> dict[str, Any]:
        return {
            "advertising": {
                "deviceID": self.state.device_id,
                "deviceTargetingOptOut": False,
                "sbsTargetingOptOut": False,
                "subtitle": "en",
                "resume": False,
            },
            "deviceClass": "androidtv",
            "streamOptions": {"audio": "demuxed"},
        }

    def playback_data(self, video_id: str) -> dict[str, Any]:
        stream_url = f"{PLAYBACK_URL}/{video_id}"
        payload = self._playback_payload()
        last_error = "Playback request failed"
        for attempt in range(2):
            try:
                token = self.ensure_auth(force_refresh=attempt == 1)
            except SbsError as exc:
                last_error = str(exc)
                break
            response = self.session.post(
                stream_url,
                json=payload,
                headers=self.tv_headers(token),
                timeout=30,
            )
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    return data
                raise SbsError("Playback response was not an object")
            if response.status_code == 401 and attempt == 0:
                continue
            last_error = (
                f"Playback request failed ({response.status_code}) for {stream_url}: "
                f"{_error_message(response)}"
            )
            break
        raise SbsError(last_error)

    def source(
        self, video_id: str, *, fetch_chapters: bool = True, fetch_attachments: bool = True
    ) -> Source:
        data = self.playback_data(video_id)
        providers = data.get("streamProviders")
        if not isinstance(providers, list):
            providers = []
            for key in ("item", "streamConfig"):
                container = data.get(key)
                if isinstance(container, dict) and isinstance(
                    container.get("streamProviders"), list
                ):
                    providers = container["streamProviders"]
                    break
        if not isinstance(providers, list):
            providers = []

        hls: dict[str, Any] | None = None
        for provider in providers:
            if not isinstance(provider, dict) or not provider.get("url"):
                continue
            kind = str(provider.get("type") or provider.get("name") or "").lower()
            url = str(provider.get("url") or "")
            if "hls" in kind or ".m3u8" in url.lower():
                hls = provider
                break
        if hls is None:
            raise SbsError(f"No playable stream provider for {video_id}")

        subs: list[Subtitle] = []
        text_tracks = hls.get("textTrack") or hls.get("textTracks") or []
        for track in text_tracks:
            if not isinstance(track, dict) or not track.get("url"):
                continue
            subs.append(
                Subtitle(
                    url=str(track.get("url")),
                    lang=str(track.get("lang") or ""),
                    format=str(track.get("format") or track.get("type") or ""),
                )
            )
        is_live = str(data.get("streamType") or "").lower() == "live"
        return Source(
            manifest=str(hls["url"]),
            subtitles=tuple(subs),
            is_live=is_live,
            chapters=_playback_chapters(data) if fetch_chapters and not is_live else (),
            attachments=_playback_attachments(data.get("images")) if fetch_attachments else (),
        )

    def subtitle(self, url: str) -> bytes:
        response = self.session.get(
            url,
            headers={"User-Agent": TV_USER_AGENT, "Accept": "text/vtt, text/plain, */*"},
            timeout=30,
        )
        if response.status_code != 200:
            raise SbsError(
                f"Subtitle request failed ({response.status_code}): {_error_message(response)}"
            )
        return response.content


__all__ = [
    "CATALOGUE",
    "DEVICE_ID_FILE",
    "DEVICE_NAME",
    "EPG_URL",
    "PLAYBACK_URL",
    "SEARCH_URL",
    "TOKEN_FILE",
    "TV_API_KEY",
    "TV_USER_AGENT",
    "WWW",
    "Channel",
    "MediaItem",
    "ParsedInput",
    "SbsApi",
    "SbsError",
    "SearchHit",
    "Season",
    "Series",
    "Session",
    "Source",
    "Subtitle",
    "parse_input",
]
