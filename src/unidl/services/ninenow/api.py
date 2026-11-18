"""9Now app search/catalogue, device-code auth and service-local WV/PR."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import quote, urlparse

import requests

from ...core import playready
from ...core.attachments import Attachment
from ...core.chapters import Chapter, normalize_chapters

WWW = "https://www.9now.com.au"
ACTIVATE = "https://9now.com.au/activate"
SERIES_API = "https://tv-api.9now.com.au/v2/pages/tv-series"
SEARCH_API = "https://api.9now.com.au/apps/pages/search"
LIVESTREAM = "https://api.9now.com.au/apps/live-experience"
CATALOGUE_DEVICE = "rn_android"
LIVE_DEFAULT_SLUG = "channel-9"
LIVE_STREAM_PARAMS = CATALOGUE_DEVICE

DEVICE_CODE = "https://login.nine.com.au/api/device/code"
DEVICE_TOKEN = "https://login.nine.com.au/api/device/token"
DEVICE_REFRESH = "https://login.nine.com.au/api/device/refresh-token"
CLIENT_ID = "9nowdevice"

BRIGHTCOVE_ACCOUNT = "4460760524001"
BRIGHTCOVE_PLAYBACK = f"https://edge.api.brightcove.com/playback/v1/accounts/{BRIGHTCOVE_ACCOUNT}/videos"
BRIGHTCOVE_KEY_VOD = (
    "BCpkADawqM1TWX5yhWjKdzhXnHCmGvnaozGSDICiEFNRv0fs12m6WA2hLxMHM8TGAEM6pv7lhJsdNhiQi76p4"
    "IcsT_jmXdtEU-wnfXhOBTx-cGR7guCqVwjyFAtQa75PFF-TmWESuiYaNTzg"
)
BRIGHTCOVE_KEY_LIVE = BRIGHTCOVE_KEY_VOD
LIVE_WIDEVINE_LICENSE = "https://lic.drmtoday.com/license-proxy-widevine/cenc/?specConform=true"
LIVE_PLAYREADY_LICENSE = "https://lic.drmtoday.com/license-proxy-headerauth/drmtoday/RightsManager.asmx"

TOKEN_FILE = "9now_token.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 25
TOKEN_EXPIRY_SKEW_SEC = 30
DEVICE_POLL_INTERVAL = 5
DEVICE_POLL_TIMEOUT = 300
SEARCH_CACHE_SEC = 300
CATALOGUE_PAGE_SIZE = 20
MAX_CATALOGUE_PAGES = 500

REGIONS = ("nsw", "vic", "qld", "sa", "wa", "tas", "act", "nt")

_HOSTS = frozenset({"9now.com.au", "www.9now.com.au"})
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_.]*$")
_CLIP_PATH = re.compile(
    r"^/(?P<series>[^/]+)/(?P<season>[^/]+)/clip-(?P<clip>[^/?#]+)",
    re.I,
)
_EPISODE_PATH = re.compile(
    r"^/(?P<series>[^/]+)/(?P<season>[^/]+)/(?P<episode>episode-[^/?#]+)",
    re.I,
)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_VTT_TIMING = re.compile(
    r"^((?:\d{2,}:)?[0-5]\d:[0-5]\d\.\d{3})\s+-->\s+"
    r"((?:\d{2,}:)?[0-5]\d:[0-5]\d\.\d{3})(?:\s|$)"
)


class NineNowError(RuntimeError):
    """9Now rejected a request or returned an unusable shape."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class Session:
    access_token: str = ""
    refresh_token: str = ""
    token_expires: float = 0.0

    @classmethod
    def from_cache(cls, data: dict[str, Any] | None) -> Session:
        data = data or {}
        try:
            expires = float(data.get("token_expires") or 0)
        except (TypeError, ValueError):
            expires = 0.0
        return cls(
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            token_expires=expires,
        )

    def to_cache(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_expires": self.token_expires,
        }

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token or self.refresh_token)

    def is_fresh(self, skew: int = TOKEN_EXPIRY_SKEW_SEC) -> bool:
        if not self.access_token:
            return False
        if self.token_expires <= 0:
            return True
        return time.time() < self.token_expires - skew


@dataclass(frozen=True)
class DeviceCode:
    auth_code: str
    device_code: str
    expires_in: int = DEVICE_POLL_TIMEOUT
    interval: int = DEVICE_POLL_INTERVAL
    verification_uri: str = ACTIVATE


@dataclass(frozen=True)
class ParsedInput:

    series_slug: str
    season_slug: str = ""
    clip_id: str = ""
    episode_slug: str = ""
    kind: str = "series"  # series | clip | episode


@dataclass(frozen=True)
class Season:
    name: str
    slug: str
    season_number: int = 0
    episode_count: int = 0

    @property
    def label(self) -> str:
        if self.season_number:
            return f"Season {self.season_number}" if not self.name else self.name
        return self.name or self.slug


@dataclass(frozen=True)
class Episode:
    title: str
    episode_number: int
    brightcove_id: str
    duration_ms: int = 0
    description: str = ""
    is_clip: bool = False
    clip_number: int = 0
    slug: str = ""
    id: str = ""

    @property
    def label(self) -> str:
        if self.is_clip:
            num = self.clip_number or self.episode_number
            return f"Clip {num:02d}  {self.title}" if num else self.title
        if self.episode_number:
            return f"E{self.episode_number:02d}  {self.title}"
        return self.title

    @property
    def detail(self) -> str:
        parts: list[str] = []
        if self.duration_ms:
            parts.append(f"{self.duration_ms // 60_000} min")
        if self.description:
            parts.append(self.description[:90])
        return " · ".join(parts)


@dataclass
class SeriesPage:
    slug: str
    name: str = ""
    seasons: list[Season] = field(default_factory=list)
    description: str = ""
    genre: str = ""
    is_movie: bool = False
    year: str = ""


@dataclass(frozen=True)
class SearchHit:
    name: str
    slug: str
    description: str = ""
    genre: str = ""
    is_movie: bool = False
    episode_count: int = 0
    season_count: int = 0

    @property
    def label(self) -> str:
        return self.name

    @property
    def detail(self) -> str:
        kind = "Movie" if self.is_movie else (self.genre or "Series")
        count = ""
        if not self.is_movie and self.episode_count:
            count = f" · {self.episode_count} episode(s)"
        description = f" · {self.description[:80]}" if self.description else ""
        return f"{kind}{count}{description}"


@dataclass(frozen=True)
class LiveItem:
    name: str
    id: str
    brightcove_id: str = ""
    stream_url: str = ""
    has_drm: bool = False
    stream_type: str = "hls"
    description: str = ""
    kind: str = "channel"  # channel | event
    start_date: str = ""
    end_date: str = ""
    attachments: tuple[Attachment, ...] = ()

    @property
    def label(self) -> str:
        tag = self.kind.upper()
        return f"[{tag}] {self.name}"

    @property
    def detail(self) -> str:
        return (self.description or "")[:90]


@dataclass(frozen=True)
class Source:
    manifest: str
    license_url: str = ""
    protocol: str = "DASH"
    encrypted: bool = True
    name: str = ""
    duration_ms: int = 0
    subtitles: tuple[tuple[str, str], ...] = ()  # (lang, url)
    chapters: tuple[Chapter, ...] = ()
    chapter_tracks: tuple[str, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    license_headers: tuple[tuple[str, str], ...] = ()
    system: str = "widevine"

    def line(self) -> str:
        drm = ("PlayReady" if self.system == "playready" else "Widevine") if self.encrypted else "clear"
        subs = f" · {len(self.subtitles)} sub(s)" if self.subtitles else ""
        return f"{self.protocol} · {drm}{subs}"


def _asset_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if url.startswith("//"):
        url = "https:" + url
    try:
        parsed = urlparse(url)
        if parsed.scheme in {"https", "http"} and parsed.hostname and parsed.username is None:
            return _https(url)
    except ValueError:
        pass
    return ""


def _brightcove_asset(row: dict[str, Any], key: str, sources_key: str) -> str:
    url = _asset_url(row.get(key))
    sources = row.get(sources_key)
    if url or not isinstance(sources, list):
        return url
    return next((
        url for source in sources
        if isinstance(source, dict) and (url := _asset_url(source.get("src")))
    ), "")


def _live_image(image: Any) -> str:
    sizes = image.get("sizes") if isinstance(image, dict) else None
    if not isinstance(sizes, dict):
        return ""
    widths = sorted((key for key in sizes if re.fullmatch(r"w\d+", key)), key=lambda key: int(key[1:]))
    return next((url for key in reversed(widths) if (url := _asset_url(sizes[key]))), "")


def _playback_chapters(data: dict[str, Any], duration_ms: int) -> tuple[Chapter, ...]:
    cues = data.get("cue_points")
    if not isinstance(cues, list):
        return ()
    chapters: list[Chapter] = []
    labels = {
        "intro": ("Intro", "intro"), "introstart": ("Intro", "intro"),
        "recap": ("Recap", "recap"), "recapstart": ("Recap", "recap"),
        "credits": ("Credits", "credits"), "creditsstart": ("Credits", "credits"),
        "endcredits": ("Credits", "credits"),
    }
    for row in cues:
        if not isinstance(row, dict) or str(row.get("type")).upper() != "CODE":
            continue
        name = str(row.get("name") or "").strip()
        marker = re.sub(r"[^a-z]", "", name.lower())
        if marker in labels:
            title, kind = labels[marker]
        elif re.match(r"^chapter\b", name, re.I):
            title, kind = name, "chapter"
        else:
            continue
        try:
            chapter = Chapter.from_seconds(row.get("time"), title, kind=kind)
        except (ValueError, OverflowError):
            continue
        if not duration_ms or chapter.start_ms < duration_ms:
            chapters.append(chapter)
    return normalize_chapters(chapters, duration_ms=duration_ms or None)


def _vtt_chapters(text: str) -> list[Chapter]:
    def milliseconds(timestamp: str) -> int:
        clock, fraction = timestamp.split(".")
        seconds = 0
        for component in clock.split(":"):
            seconds = seconds * 60 + int(component)
        return seconds * 1000 + int(fraction)

    blocks = re.split(r"\n[ \t]*\n", text.lstrip("\ufeff").replace("\r\n", "\n").strip())
    if not blocks or not re.match(r"^WEBVTT(?:\s|$)", blocks[0]):
        return []
    chapters: list[Chapter] = []
    for block in blocks[1:]:
        lines = block.splitlines()
        if not lines or re.match(r"^(?:NOTE|STYLE|REGION)(?:\s|$)", lines[0]):
            continue
        index = 0 if "-->" in lines[0] else 1
        if len(lines) <= index + 1 or not (timing := _VTT_TIMING.match(lines[index])):
            continue
        title = re.sub(r"<[^>]+>", "", " ".join(lines[index + 1:])).strip()
        if title and (end := milliseconds(timing[2])) > (start := milliseconds(timing[1])):
            chapters.append(Chapter(start, title, end, "chapter"))
    return chapters


def parse_input(text: str) -> ParsedInput | None:
    raw = (text or "").strip()
    if not raw:
        return None

    if "://" not in raw and "/" not in raw and _SLUG.fullmatch(raw):
        return ParsedInput(series_slug=raw.lower())

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    if host not in _HOSTS:
        return None
    path = parsed.path or "/"
    clip = _CLIP_PATH.match(path)
    if clip:
        return ParsedInput(
            series_slug=clip.group("series").lower(),
            season_slug=clip.group("season"),
            clip_id=clip.group("clip"),
            kind="clip",
        )
    episode = _EPISODE_PATH.match(path)
    if episode:
        return ParsedInput(
            series_slug=episode.group("series").lower(),
            season_slug=episode.group("season"),
            episode_slug=episode.group("episode"),
            kind="episode",
        )
    parts = [part for part in path.split("/") if part]
    if not parts or not _SLUG.fullmatch(parts[0]):
        return None
    return ParsedInput(series_slug=parts[0].lower())


def _normalize_region(value: str | None) -> str:
    region = (value or "nsw").strip().lower()
    return region if region in REGIONS else "nsw"



class NineNowApi:
    def __init__(
        self,
        session: requests.Session | None = None,
        state: Session | None = None,
        *,
        region: str = "nsw",
        on_save: Any | None = None,
        system: str = "widevine",
    ):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.state = state or Session()
        self.region = _normalize_region(region)
        self.on_save = on_save
        if system not in {"widevine", "playready"}:
            raise NineNowError(f"unsupported 9Now DRM system: {system}")
        self.system = system
        self._search_catalogue: tuple[dict[str, Any], ...] = ()
        self._search_catalogue_at = 0.0
        self._apply_auth()

    def adopt(self, data: dict[str, Any] | None) -> None:
        self.state = Session.from_cache(data)
        self._apply_auth()

    def _apply_auth(self) -> None:
        if self.state.is_fresh():
            self.session.headers["Authorization"] = f"Bearer {self.state.access_token}"
        else:
            self.session.headers.pop("Authorization", None)

    def _persist(self) -> None:
        if callable(self.on_save):
            self.on_save(self.state)

    def start_device_code(self) -> DeviceCode:
        data = self._json(
            "POST",
            DEVICE_CODE,
            params={"client_id": CLIENT_ID},
            data={},
            auth=False,
        )
        if not isinstance(data, dict):
            raise NineNowError("9Now device-code endpoint returned a non-object")
        auth_code = str(data.get("authCode") or "").strip()
        device_code = str(data.get("deviceCode") or "").strip()
        if not auth_code or not device_code:
            raise NineNowError("9Now device-code response was missing codes")
        expires = _int(data.get("expiresIn")) or DEVICE_POLL_TIMEOUT
        interval = _int(data.get("interval")) or DEVICE_POLL_INTERVAL
        return DeviceCode(
            auth_code=auth_code,
            device_code=device_code,
            expires_in=max(60, expires),
            interval=max(3, interval),
            verification_uri=str(data.get("verificationUri") or ACTIVATE),
        )

    def poll_device_code(self, challenge: DeviceCode) -> Session | None:
        """Return a Session once the user has activated, else None (keep polling)."""
        try:
            response = self.session.get(
                DEVICE_TOKEN,
                headers={"Authorization": None},
                params={
                    "auth_code": challenge.auth_code,
                    "device_code": challenge.device_code,
                    "client_id": CLIENT_ID,
                    "response_types": "id_token",
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            return None
        if response.status_code in (401, 403, 404, 428):
            return None
        if not response.ok:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        if not isinstance(data, dict) or "accessToken" not in data:
            return None
        self._apply_token_payload(data)
        self._persist()
        return self.state

    def refresh(self) -> bool:
        """Refresh the access token. False on auth failure; clears a dead session."""
        if not self.state.refresh_token:
            return False
        try:
            response = self.session.post(
                DEVICE_REFRESH,
                headers={"Authorization": None},
                params={
                    "refresh_token": self.state.refresh_token,
                    "client_id": CLIENT_ID,
                    "response_types": "id_token",
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            return False
        if response.status_code in (401, 403):
            self.state = Session()
            self._apply_auth()
            self._persist()
            return False
        if not response.ok:
            return False
        try:
            data = response.json()
        except ValueError:
            return False
        if not isinstance(data, dict) or data.get("error") or "accessToken" not in data:
            self.state = Session()
            self._apply_auth()
            self._persist()
            return False
        self._apply_token_payload(data)
        self._persist()
        return True

    def ensure_auth(self, *, required: bool = False) -> bool:
        if self.state.is_fresh():
            self._apply_auth()
            return True
        if self.state.refresh_token:
            if self.refresh() and self.state.is_fresh():
                return True
        self._apply_auth()
        if required:
            raise NineNowError("9Now is not signed in. Choose Sign in and enter the TV code first.")
        return self.state.is_fresh()

    def _apply_token_payload(self, data: dict[str, Any]) -> None:
        access = str(data.get("accessToken") or data.get("access_token") or "")
        refresh = str(data.get("refresh_token") or data.get("refreshToken") or self.state.refresh_token or "")
        expires_in = _int(data.get("expiresIn") or data.get("expires_in")) or 3600
        self.state = Session(
            access_token=access,
            refresh_token=refresh,
            token_expires=time.time() + max(60, expires_in) - TOKEN_EXPIRY_SKEW_SEC,
        )
        self._apply_auth()

    def search(self, query: str) -> list[SearchHit]:
        wanted = _search_text(query)
        if not wanted:
            return []
        try:
            data = self._json(
                "GET", SEARCH_API,
                params={"device": CATALOGUE_DEVICE, "region": self.region, "query": query.strip()},
                auth=False,
            )
        except NineNowError as exc:
            if exc.status_code not in {404, 410, 501}:
                raise
        else:
            hits = self._search_page_hits(data)
            if hits is not None:
                return hits
        return self._search_legacy(query)

    @staticmethod
    def _search_page_hits(data: Any) -> list[SearchHit] | None:
        envelope = data.get("data") if isinstance(data, dict) else None
        page = envelope.get("getSearchPage") if isinstance(envelope, dict) else None
        sections = page.get("sections") if isinstance(page, dict) else None
        content = sections.get("content") if isinstance(sections, dict) else None
        tabs = content.get("tabs") if isinstance(content, dict) else None
        if not isinstance(tabs, list):
            return None
        hits: dict[str, SearchHit] = {}
        for tab in tabs:
            if not isinstance(tab, dict) or str(tab.get("heading") or tab.get("title")).casefold() != "search results":
                continue
            cards = tab.get("cards")
            if not isinstance(cards, list):
                return None
            if (_int(tab.get("cardCount")) or 0) > len(cards):
                return None
            for card in cards:
                if not isinstance(card, dict):
                    continue
                identity = str(card.get("id") or "")
                if not identity.startswith("Card-tv-series:"):
                    continue
                slug = identity.removeprefix("Card-tv-series:")
                title = card.get("title") if isinstance(card.get("title"), dict) else {}
                name = str(title.get("text") or "").strip()
                if not _SLUG.fullmatch(slug) or not name:
                    continue
                info = card.get("additionalInfo") if isinstance(card.get("additionalInfo"), dict) else {}
                texts = [str(row.get("text") or "") for row in info.get("metadata") or [] if isinstance(row, dict)]
                genre = next((text for text in texts if text and not re.fullmatch(r"\d+\s+seasons?", text, re.I)), "")
                seasons = next((int(match.group(1)) for text in texts if (match := re.fullmatch(r"(\d+)\s+seasons?", text, re.I))), 0)
                description = card.get("description") if isinstance(card.get("description"), dict) else {}
                hits.setdefault(slug, SearchHit(
                    name=name, slug=slug, description=str(description.get("text") or ""),
                    genre=genre, is_movie=genre.casefold() == "movies", season_count=seasons,
                ))
        return list(hits.values())

    def _search_legacy(self, query: str) -> list[SearchHit]:
        wanted = _search_text(query)
        terms = wanted.split()
        if time.time() - self._search_catalogue_at >= SEARCH_CACHE_SEC:
            data = self._json(
                "GET",
                SERIES_API,
                params={"device": CATALOGUE_DEVICE},
                auth=False,
            )
            if not isinstance(data, dict):
                raise NineNowError("9Now returned no searchable catalogue")
            self._search_catalogue = tuple(raw for raw in data.get("tvSeries") or [] if isinstance(raw, dict))
            self._search_catalogue_at = time.time()

        ranked: list[tuple[int, str, SearchHit]] = []
        for raw in self._search_catalogue:
            name = str(raw.get("name") or raw.get("displayName") or "").strip()
            slug = str(raw.get("slug") or "").strip()
            normalized = _search_text(name)
            if not name or not slug or not all(term in normalized for term in terms):
                continue
            genre_raw = raw.get("genre") if isinstance(raw.get("genre"), dict) else {}
            genre = str(genre_raw.get("name") or "").strip()
            genre_slug = str(genre_raw.get("slug") or "").strip().casefold()
            is_movie = genre_slug == "movies" or genre.casefold() == "movies"
            score = 0 if normalized == wanted else 1 if normalized.startswith(wanted) else 2
            hit = SearchHit(
                name=name,
                slug=slug,
                description=str(raw.get("description") or "").strip(),
                genre=genre,
                is_movie=is_movie,
                episode_count=_int(raw.get("episodeCount")) or 0,
                season_count=_int(raw.get("seasonCount")) or 0,
            )
            ranked.append((score, normalized, hit))
        ranked.sort(key=lambda entry: (entry[0], entry[1], entry[2].slug))
        return [entry[2] for entry in ranked]

    def series(self, slug: str) -> SeriesPage:
        data = self._json(
            "GET",
            f"{SERIES_API}/{slug}",
            params={"device": CATALOGUE_DEVICE},
            auth=False,
        )
        if not isinstance(data, dict):
            raise NineNowError(f"9Now returned no series page for {slug}")
        tv_series = data.get("tvSeries") if isinstance(data.get("tvSeries"), dict) else {}
        genre_raw = data.get("genre") if isinstance(data.get("genre"), dict) else {}
        name = str(
            data.get("name")
            or data.get("displayName")
            or data.get("title")
            or tv_series.get("name")
            or slug.replace("-", " ").title()
        )
        seasons: list[Season] = []
        for raw in data.get("seasons") or []:
            if not isinstance(raw, dict):
                continue
            season_slug = str(raw.get("slug") or "").strip()
            if not season_slug:
                continue
            seasons.append(
                Season(
                    name=str(raw.get("name") or season_slug),
                    slug=season_slug,
                    season_number=_int(raw.get("seasonNumber")) or 0,
                    episode_count=_int(raw.get("episodeCount")) or 0,
                )
            )
        if not seasons:
            raise NineNowError(f"9Now listed no seasons for {name}")
        genre = str(genre_raw.get("name") or "").strip()
        is_movie = str(genre_raw.get("slug") or "").strip().casefold() == "movies" or genre.casefold() == "movies"
        return SeriesPage(
            slug=str(tv_series.get("slug") or slug),
            name=name,
            seasons=seasons,
            description=str(tv_series.get("description") or data.get("description") or ""),
            genre=genre,
            is_movie=is_movie,
            year=_movie_year(data, seasons) if is_movie else "",
        )

    def episodes(self, series_slug: str, season_slug: str) -> list[Episode]:
        episodes = self._videos(series_slug, season_slug, is_clip=False)
        episodes.sort(key=lambda e: (e.episode_number == 0, e.episode_number, e.title))
        return episodes

    def clips(self, series_slug: str, season_slug: str) -> list[Episode]:
        return self._videos(series_slug, season_slug, is_clip=True)

    def _videos(self, series_slug: str, season_slug: str, *, is_clip: bool) -> list[Episode]:
        kind = "clips" if is_clip else "episodes"
        url = f"{SERIES_API}/{series_slug}/seasons/{season_slug}/{kind}"
        videos: list[Episode] = []
        seen: set[str] = set()
        skip = 0
        for _ in range(MAX_CATALOGUE_PAGES):
            params: dict[str, Any] = {
                "device": CATALOGUE_DEVICE, "sort": "", "take": CATALOGUE_PAGE_SIZE, "skip": skip,
            }
            if is_clip:
                params["tagSlug"] = ""
            data = self._json("GET", url, params=params, auth=False)
            if not isinstance(data, dict):
                raise NineNowError(f"9Now returned no {kind} payload")
            bucket = data.get(kind)
            bucket = bucket if isinstance(bucket, dict) else data
            rows = bucket.get("items")
            if not isinstance(rows, list):
                raise NineNowError(f"9Now returned invalid {kind} items")
            if not rows:
                return videos
            added = False
            for index, raw in enumerate(rows, start=skip + 1):
                if not isinstance(raw, dict):
                    continue
                video = self._episode(raw, is_clip=is_clip, fallback_number=index)
                identity = str(raw.get("id") or raw.get("slug") or (video.brightcove_id if video else ""))
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                added = True
                if video is not None:
                    videos.append(video)
            if not added:
                raise NineNowError(f"9Now repeated a {kind} page without advancing")
            skip += len(rows)
            count = _int(bucket.get("count"))
            if count is not None and skip >= count:
                return videos
            if count is None and len(rows) < CATALOGUE_PAGE_SIZE:
                return videos
        raise NineNowError(f"9Now {kind} catalogue exceeded the pagination safety limit")

    def find_clip(self, series_slug: str, season_slug: str, clip_id: str) -> tuple[SeriesPage, Season, Episode]:
        return self._find_video(series_slug, season_slug, clip_id, is_clip=True)

    def find_episode(self, series_slug: str, season_slug: str, episode_slug: str) -> tuple[SeriesPage, Season, Episode]:
        return self._find_video(series_slug, season_slug, episode_slug, is_clip=False)

    def _find_video(
        self, series_slug: str, season_slug: str, identifier: str, *, is_clip: bool
    ) -> tuple[SeriesPage, Season, Episode]:
        page = self.series(series_slug)
        season = next((s for s in page.seasons if s.slug == season_slug), None)
        if season is None:
            raise NineNowError(f"Season {season_slug!r} not found under {page.name}")
        kind = "clip" if is_clip else "episode"
        needle = identifier.strip().casefold()
        slug = needle if needle.startswith(f"{kind}-") else f"{kind}-{needle}"
        try:
            data = self._json(
                "GET", f"{SERIES_API}/{series_slug}/seasons/{season_slug}/{kind}s/{quote(slug, safe='')}",
                params={"device": CATALOGUE_DEVICE}, auth=False,
            )
        except NineNowError as exc:
            if exc.status_code != 404:
                raise
        else:
            raw = data.get(kind) if isinstance(data, dict) else None
            if isinstance(raw, dict):
                video = self._episode(raw, is_clip=is_clip, fallback_number=0)
                if video is not None and video.slug.casefold() == slug:
                    return page, season, video
            raise NineNowError(f"9Now returned no playable {kind} detail for {identifier!r}")
        for video in self._videos(series_slug, season_slug, is_clip=is_clip):
            if needle in {video.id.casefold(), video.brightcove_id.casefold()} or video.slug.casefold() == slug:
                return page, season, video
        raise NineNowError(f"Could not match {kind} {identifier!r} in {page.name} / {season.label}")

    @staticmethod
    def _episode(raw: dict[str, Any], *, is_clip: bool, fallback_number: int) -> Episode | None:
        title = str(
            raw.get("name") or raw.get("displayName") or raw.get("episodeTitle") or raw.get("title") or ""
        ).strip() or ("Unknown Clip" if is_clip else "Unknown Episode")
        video = raw.get("video") if isinstance(raw.get("video"), dict) else {}
        brightcove_id = str(video.get("brightcoveId") or raw.get("brightcoveId") or "").strip()
        if not brightcove_id:
            return None
        number = _int(raw.get("episodeNumber") or raw.get("episode")) or fallback_number
        duration_ms = _int(video.get("duration") or raw.get("duration")) or 0
        return Episode(
            title=title,
            episode_number=number,
            brightcove_id=brightcove_id,
            duration_ms=duration_ms,
            description=str(raw.get("description") or ""),
            is_clip=is_clip,
            clip_number=number if is_clip else 0,
            slug=str(raw.get("slug") or ""),
            id=str(raw.get("id") or ""),
        )

    def live(self, region: str | None = None) -> list[LiveItem]:
        self.ensure_auth(required=False)
        reg = _normalize_region(region or self.region)
        payload = self._live_payload(LIVE_DEFAULT_SLUG, reg)
        selected_stream = payload.get("stream") if isinstance(payload.get("stream"), dict) else {}
        items: list[LiveItem] = []
        for raw in payload.get("switcherRail") or []:
            if not isinstance(raw, dict):
                continue
            slug = str(raw.get("slug") or "").strip()
            if not slug:
                continue
            stream = selected_stream if raw.get("selected") else {}
            item = self._live_item(raw, stream)
            if item is not None:
                items.append(item)
        if not items:
            raise NineNowError(f"9Now listed no live channels or events for {reg.upper()}")
        return items

    def _live_payload(self, slug: str, region: str | None = None, *, drm: bool = False) -> dict[str, Any]:
        self.ensure_auth(required=drm)
        params: dict[str, Any] = {
            "device": CATALOGUE_DEVICE,
            "streamParams": LIVE_STREAM_PARAMS,
            "slug": slug,
            "region": _normalize_region(region or self.region),
        }
        if drm:
            params["containsToken"] = "true"
        elif self.state.is_fresh():
            params["token"] = self.state.access_token
        headers = {} if drm else {"Authorization": None}
        data = self._json("GET", LIVESTREAM, params=params, headers=headers)
        if not isinstance(data, dict):
            raise NineNowError("9Now live endpoint returned a non-object")
        payload = (data.get("data") or {}).get("getLXP")
        if not isinstance(payload, dict):
            raise NineNowError("9Now live payload was unusable")
        return payload

    @staticmethod
    def _live_item(raw: dict[str, Any], stream: dict[str, Any]) -> LiveItem | None:
        slug = str(raw.get("slug") or stream.get("slug") or "").strip()
        if not slug:
            return None
        display = stream.get("display") if isinstance(stream.get("display"), dict) else {}
        listings = display.get("listings") if isinstance(display.get("listings"), list) else []
        airings = raw.get("airings") if isinstance(raw.get("airings"), list) else []
        current = next((entry for entry in listings + airings if isinstance(entry, dict)), {})
        video = stream.get("video") if isinstance(stream.get("video"), dict) else {}
        raw_type = str(raw.get("type") or stream.get("type") or "channel")
        kind = "event" if raw_type in {"event", "live-event", "live-event-group"} else "channel"
        name = str(raw.get("name") or display.get("tagline") or slug.replace("-", " ").title())
        description = str(current.get("title") or current.get("description") or "")
        subtitle = str(current.get("subtitle") or current.get("subTitle") or "")
        if subtitle and subtitle not in description:
            description = f"{description} · {subtitle}" if description else subtitle
        attachments: list[Attachment] = []
        if logo := _live_image(raw.get("switcherLogo")):
            attachments.append(Attachment(logo, "Channel logo", "image"))
        if (promo := _live_image(display.get("promoImage"))) and all(a.url != promo for a in attachments):
            attachments.append(Attachment(promo, "Live artwork", "artwork"))
        return LiveItem(
            name=name,
            id=slug,
            brightcove_id=str(video.get("referenceId") or ""),
            stream_url=str(video.get("url") or ""),
            has_drm=bool(video.get("hasDrm")),
            stream_type=str(video.get("streamType") or "hls"),
            description=description,
            kind=kind,
            start_date=str(current.get("startDate") or current.get("programStartDate") or ""),
            end_date=str(current.get("endDate") or current.get("programEndDate") or ""),
            attachments=tuple(attachments),
        )

    def source(self, video_id: str, *, live: bool = False) -> Source:
        if not video_id:
            raise NineNowError("no Brightcove video id")
        vid = str(video_id).strip()
        if not vid.isdigit() and not vid.startswith("ref:"):
            vid = f"ref:{vid}"

        if live:
            self.ensure_auth(required=False)

        policy = BRIGHTCOVE_KEY_LIVE if live else BRIGHTCOVE_KEY_VOD
        headers = {
            "BCOV-POLICY": policy,
            "Accept": "application/json",
            "Origin": WWW,
            "Referer": f"{WWW}/",
            "User-Agent": USER_AGENT,
        }
        data = self._json("GET", f"{BRIGHTCOVE_PLAYBACK}/{vid}", headers=headers, auth=False)
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            raise NineNowError(f"Brightcove returned no playback for {vid}")
        if data.get("error_code"):
            raise NineNowError(f"Brightcove error {data.get('error_code')}: {data.get('message')}")
        return self._pick_source(data)

    def live_source(self, item: LiveItem) -> Source:
        payload = self._live_payload(item.id, self.region)
        stream = payload.get("stream") if isinstance(payload.get("stream"), dict) else {}
        resolved = self._live_item({"slug": item.id, "name": item.name, "type": item.kind}, stream)
        if resolved is None:
            raise NineNowError(f"9Now returned no current stream for {item.name}")
        artwork = {attachment.url: attachment for attachment in (*item.attachments, *resolved.attachments)}
        if resolved.stream_url:
            license_headers: tuple[tuple[str, str], ...] = ()
            if resolved.has_drm:
                protected = self._live_payload(item.id, self.region, drm=True)
                protected_stream = protected.get("stream")
                video = protected_stream.get("video") if isinstance(protected_stream, dict) else None
                drm_data = video.get("drm") if isinstance(video, dict) else None
                if not isinstance(drm_data, dict) or not drm_data.get("customData") or not drm_data.get("authToken"):
                    raise NineNowError(f"9Now returned no live DRM authorization for {item.name}")
                license_headers = (
                    ("x-dt-custom-data" if self.system == "playready" else "dt-custom-data", str(drm_data["customData"])),
                    ("x-dt-auth-token", str(drm_data["authToken"])),
                )
            return Source(
                manifest=_https(resolved.stream_url),
                protocol="DASH" if resolved.stream_type.casefold() == "dash" or ".mpd" in urlparse(resolved.stream_url).path else "HLS",
                encrypted=resolved.has_drm,
                license_url=(LIVE_PLAYREADY_LICENSE if self.system == "playready" else LIVE_WIDEVINE_LICENSE) if resolved.has_drm else "",
                license_headers=license_headers,
                system=self.system,
                name=resolved.name,
                attachments=tuple(artwork.values()),
            )
        if not resolved.brightcove_id:
            if resolved.has_drm:
                raise NineNowError(f"{resolved.name} returned a protected live stream without a Brightcove reference")
            raise NineNowError(f"no stream or Brightcove id for {item.name}")
        source = self.source(resolved.brightcove_id, live=True)
        for attachment in source.attachments:
            artwork.setdefault(attachment.url, attachment)
        return replace(
            source,
            name=resolved.name or source.name,
            attachments=tuple(artwork.values()),
        )

    def _pick_source(self, data: dict[str, Any]) -> Source:
        sources = data.get("sources") or []
        if not isinstance(sources, list):
            raise NineNowError("Brightcove response had no sources")
        duration_ms = max(0, _int(data.get("duration")) or 0)
        artwork: dict[str, Attachment] = {}
        for kind in ("poster", "thumbnail"):
            if url := _brightcove_asset(data, kind, f"{kind}_sources"):
                artwork.setdefault(url, Attachment(url, kind.title(), kind))
        tracks = data.get("text_tracks")
        chapter_tracks = tuple(dict.fromkeys(
            url for track in tracks
            if isinstance(track, dict) and str(track.get("kind")).casefold() == "chapters"
            and (url := _brightcove_asset(track, "src", "sources"))
        )) if isinstance(tracks, list) else ()
        metadata = {
            "system": self.system,
            "name": str(data.get("name") or ""),
            "duration_ms": duration_ms,
            "subtitles": self._subtitles(data),
            "chapters": _playback_chapters(data, duration_ms),
            "chapter_tracks": chapter_tracks,
            "attachments": tuple(artwork.values()),
        }

        protected: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        clear: list[tuple[int, dict[str, Any]]] = []
        key_system = "com.microsoft.playready" if self.system == "playready" else "com.widevine.alpha"
        for raw in sources:
            if not isinstance(raw, dict) or not raw.get("src"):
                continue
            kind = str(raw.get("type") or "").lower()
            src = str(raw["src"])
            systems = raw.get("key_systems") if isinstance(raw.get("key_systems"), dict) else {}
            protocol = self._source_protocol(src, kind)
            if not protocol:
                continue
            rank = {"DASH": 30, "HLS": 20, "ISM": 10}[protocol]
            protection = systems.get(key_system)
            if isinstance(protection, dict) and protection.get("license_url"):
                if protocol == "DASH" and "com.widevine.alpha" in systems and "com.microsoft.playready" in systems:
                    rank += 1
                protected.append((rank, raw, protection))
            elif not systems:
                clear.append((rank, raw))

        if protected:
            _, pick, protection = max(protected, key=lambda entry: entry[0])
            properties = data.get("properties") if isinstance(data.get("properties"), dict) else {}
            headers = properties.get("playreadyHeaders" if self.system == "playready" else "widevineHeaders")
            license_headers = tuple((str(k), str(v)) for k, v in headers.items() if v is not None) if isinstance(headers, dict) else ()
            return Source(
                manifest=_https(str(pick["src"])),
                license_url=_https(str(protection["license_url"])),
                protocol=self._source_protocol(str(pick["src"]), str(pick.get("type") or "")),
                encrypted=True, license_headers=license_headers, **metadata,
            )
        if clear:
            _, pick = max(clear, key=lambda entry: entry[0])
            return Source(
                manifest=_https(str(pick["src"])),
                protocol=self._source_protocol(str(pick["src"]), str(pick.get("type") or "")),
                encrypted=False, **metadata,
            )
        raise NineNowError(f"Brightcove offered no playable {self.system} or clear source")

    @staticmethod
    def _source_protocol(src: str, kind: str) -> str:
        kind = kind.casefold()
        path = urlparse(src).path.casefold()
        if "dash" in kind or path.endswith(".mpd"):
            return "DASH"
        if "mpegurl" in kind or path.endswith(".m3u8"):
            return "HLS"
        if "sstr" in kind or path.endswith((".ismc", ".ism/manifest")):
            return "ISM"
        return ""

    def chapters(self, source: Source) -> tuple[Chapter, ...]:
        for url in source.chapter_tracks:
            try:
                response = self.session.get(
                    url,
                    headers={"Authorization": None, "Accept": "text/vtt"},
                    timeout=REQUEST_TIMEOUT,
                )
                response.raise_for_status()
            except requests.RequestException:
                continue
            chapters = _vtt_chapters(response.content.decode("utf-8-sig", errors="replace"))
            if chapters:
                return normalize_chapters(chapters, duration_ms=source.duration_ms or None)
        return source.chapters

    @staticmethod
    def _subtitles(data: dict[str, Any]) -> tuple[tuple[str, str], ...]:
        tracks = data.get("text_tracks")
        if not isinstance(tracks, list):
            return ()
        out: list[tuple[str, str]] = []
        for track in tracks:
            if not isinstance(track, dict):
                continue
            if str(track.get("kind") or "").lower() not in {"captions", "subtitles", ""}:
                if track.get("kind") and str(track.get("kind")).lower() != "captions":
                    continue
            lang = str(track.get("srclang") or track.get("label") or "und").strip()
            url = ""
            for source in track.get("sources") or []:
                if isinstance(source, dict):
                    src = str(source.get("src") or "")
                    if src.startswith("https://"):
                        url = src
                        break
                    if src and not url:
                        url = src
            if not url:
                url = str(track.get("src") or "")
            if url:
                out.append((lang, _https(url)))
        return tuple(out)

    def playready_license(
        self,
        license_url: str,
        challenge: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        if not license_url:
            raise NineNowError("no 9Now PlayReady licence URL came with this playback")
        request_headers = {
            "Authorization": None,
            "Origin": WWW,
            "Referer": f"{WWW}/",
            "User-Agent": USER_AGENT,
            **(headers or {}),
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": playready.SOAP_ACTION,
        }
        try:
            response = self.session.post(
                license_url,
                data=challenge.encode("utf-8") if isinstance(challenge, str) else bytes(challenge),
                headers=request_headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise NineNowError(f"could not reach 9Now PlayReady licence server ({type(exc).__name__})") from None
        if not response.ok:
            raise NineNowError(f"9Now PlayReady licence server answered {response.status_code}", status_code=response.status_code)
        return response.text

    def widevine_license(
        self,
        license_url: str,
        challenge: bytes,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        if not license_url:
            raise NineNowError("no 9Now licence URL came with this playback")
        request_headers = {
            "Authorization": None,
            "Content-Type": "application/octet-stream",
            "Origin": WWW,
            "Referer": f"{WWW}/",
            "User-Agent": USER_AGENT,
        }
        request_headers.update(headers or {})
        try:
            response = self.session.post(
                license_url,
                data=challenge,
                headers=request_headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise NineNowError(f"could not reach 9Now licence server ({type(exc).__name__})") from None
        if not response.ok:
            raise NineNowError(f"9Now licence server answered {response.status_code}", status_code=response.status_code)
        return response.content

    def _json(
        self,
        method: str,
        url: str,
        *,
        auth: bool = True,
        **kwargs: Any,
    ) -> Any:
        if auth and self.state.signed_in:
            self.ensure_auth(required=False)
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("User-Agent", USER_AGENT)
        if not auth:
            headers["Authorization"] = None
        if isinstance(kwargs.get("params"), dict):
            kwargs["params"] = dict(kwargs["params"])
            if auth and "token" in kwargs["params"]:
                kwargs["params"]["token"] = self.state.access_token if self.state.is_fresh() else None
        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )
            if response.status_code == 401 and auth and method.upper() == "GET" and self.state.refresh_token:
                if self.refresh():
                    if "token" in (kwargs.get("params") or {}):
                        kwargs["params"]["token"] = self.state.access_token
                    response = self.session.request(
                        method, url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs,
                    )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            if response is not None:
                raise NineNowError(
                    f"9Now HTTP {response.status_code} for {urlparse(url).path}",
                    status_code=response.status_code,
                ) from None
            raise NineNowError(f"9Now request failed ({type(exc).__name__})") from None
        except ValueError as exc:
            raise NineNowError("9Now returned something that is not JSON") from exc


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _https(url: str) -> str:
    return "https://" + url[7:] if url.startswith("http://") else url


def _search_text(value: Any) -> str:
    return " ".join(re.findall(r"[0-9a-z]+", str(value or "").casefold()))


def _movie_year(data: dict[str, Any], seasons: list[Season]) -> str:
    for season in seasons:
        for value in (season.name, season.slug, season.season_number):
            match = _YEAR.search(str(value or ""))
            if match:
                return match.group(0)
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    for value in (meta.get("pageHeading"), meta.get("title")):
        matches = _YEAR.findall(str(value or ""))
        if matches:
            return matches[-1]
    return ""


__all__ = [
    "ACTIVATE",
    "BRIGHTCOVE_ACCOUNT",
    "BRIGHTCOVE_KEY_LIVE",
    "BRIGHTCOVE_KEY_VOD",
    "CATALOGUE_DEVICE",
    "DeviceCode",
    "Episode",
    "LiveItem",
    "NineNowApi",
    "NineNowError",
    "ParsedInput",
    "REGIONS",
    "Season",
    "SearchHit",
    "SeriesPage",
    "Session",
    "Source",
    "TOKEN_FILE",
    "USER_AGENT",
    "WWW",
    "parse_input",
]
