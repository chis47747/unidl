"""ABC iView (Australia) - free AU catalogue, search, live and KeyOS Widevine.

"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlsplit
from xml.etree.ElementTree import ParseError

import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter
from ...core.playback import SubtitleReference
from ...downloader.models import StreamInfo
from ...downloader.parsers.dash import parse_dash

APP_VERSION = "2026.10.20440-tv"
USER_AGENT = f"ABC iview/{APP_VERSION} (Android TV; au.net.abc.iview)"

BASE_URL = "https://api.iview.abc.net.au"
LISTEN_BASE_URL = "https://www.abc.net.au"
PROFILE_BASE_URL = "https://mylogin-api.abc.net.au/latest/"
WWW = "https://iview.abc.net.au"
LICENSE_URL = "https://widevine.keyos.com/api/v4/getLicense"

CLIENT_ID = "88a12649-f212-4ac8-b9c9-044d8c8b0b21"

ALGOLIA_APP_ID = "5N1YUZ1SJL"
ALGOLIA_API_KEY = "b9ba46e3bc6fc76d060c0b868aeac17a"
ALGOLIA_INDEX = "iview_prod_iview"
ALGOLIA_USER_TOKEN = "ABCIVIEW_ANDROID_USER"
ALGOLIA_SEARCH = "https://5n1yuz1sjl-dsn.algolia.net/1/indexes/*/queries"
LISTEN_ALGOLIA_APP_ID = "1KTWJENJUU"
LISTEN_ALGOLIA_API_KEY = "7c84f3c11388161508d38ef678966728"
LISTEN_ALGOLIA_INDEX = "listen_prod_all"
LISTEN_ALGOLIA_SEARCH = "https://1ktwjenjuu-dsn.algolia.net/1/indexes/listen_prod_all/query"

PROFILE_STATUS = "status"
PROFILE_API_KEY = "Jtt9iwRAiB2Z8JTYSXRt73J0gwrzE0n82y9U79QG"
PROFILE_PRODUCT_ID = "iview"
PROFILE_LINK = "ctv/link"
PROFILE_STATUS_LINK = "ctv/status"
PROFILE_UNLINK = "ctv/unlink"
TV_LINK_URL = "https://abc.net.au/linktv"
TV_LINK_INTERVAL = 5.0
TV_LINK_TIMEOUT = 300.0
TOKEN_FILE = "abciview_token.json"
SHOW_INFO = "/v3/show/{show_id}?embed=seriesList,selectedSeries,highlightVideo"
VIDEO_INFO = "/v3/video/{video_id}?include=episodeNumber,seriesNumber"
LIVE_CATEGORY = "/v3/category/watch-live?spotlight=true"
LISTEN_RADIO_PAGE = "/listen/radio"
LISTEN_RADIO_COLLECTION = "/core-next/api/collection/listen/radio"
JWT_TOKEN = "/v3/token/jwt"
DRM_TOKEN = "/v3/token/drm/{video_id}"
HLS_TIME = "/v3/time"
HLS_SIGN = "/auth/hls/sign"
HLS_DEVICE = "android-tv"
HLS_SECRET = "android.content.res.Resources"

DASH_STREAM_TYPES = ("mpegdash-h265-cbcs", "mpegdash")
HLS_STREAM_TYPES = ("hls-latest", "hls")
QUALITY_PRIORITY = ("1080", "720", "sd", "sd-low")

HTTP_TIMEOUT = 30
DASH_PROBE_TIMEOUT = 10
MAX_SUBTITLE_BYTES = 16 * 1024 * 1024
JWT_MIN_TTL = 300
_SUBTITLE_EXTENSIONS = frozenset({"vtt", "srt", "ttml", "dfxp", "xml", "ass", "ssa"})
_CAPTION_KEYS = (
    ("src-vtt", "vtt"),
    ("src-srt", "srt"),
    ("src-ttml", "ttml"),
    ("src-dfxp", "dfxp"),
    ("src-xml", "xml"),
    ("url", ""),
)

_SHOW_URL = re.compile(
    r"(?:https?://)?(?:iview\.)?abc\.net\.au/show/(?P<id>[^/?#]+)",
    re.IGNORECASE,
)
_VIDEO_URL = re.compile(
    r"(?:https?://)?(?:iview\.)?abc\.net\.au/(?:show/[^/]+/)?video/(?P<id>[^/?#]+)",
    re.IGNORECASE,
)
_BARE_ID = re.compile(r"^[a-zA-Z0-9_-]+$")
_VIDEOISH_ID = re.compile(r"^[A-Z0-9]{6,}S\d{2}$")
_LISTEN_EPISODE_URL = re.compile(
    r"https?://(?:www\.)?abc\.net\.au/listen/programs/[^/?#]+/[^/?#]+/(?P<id>\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_LISTEN_AUDIOBOOK_URL = re.compile(
    r"https?://(?:www\.)?abc\.net\.au/listen/audiobooks/(?P<slug>[^/?#]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_LISTEN_AUDIOBOOK_EPISODE_URL = re.compile(
    r"https?://(?:www\.)?abc\.net\.au/listen/audiobooks/[^/?#]+/[^/?#]+/(?P<id>\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_LISTEN_PROGRAM_URL = re.compile(
    r"https?://(?:www\.)?abc\.net\.au/listen/programs/(?P<slug>[^/?#]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_LISTEN_LIVE_URL = re.compile(
    r"https?://(?:www\.)?abc\.net\.au/listen/live/(?P<slug>[^/?#]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_LISTEN_STATION_URL = re.compile(
    r"https?://(?:www\.)?abc\.net\.au/listen/(?P<slug>[^/?#]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)


class AbcIviewError(RuntimeError):
    """ABC iView refused a request or returned an unusable shape."""


@dataclass(frozen=True)
class ParsedInput:
    kind: str  # show | video | listen_program | listen_audiobook | listen_episode | listen_live | listen_station
    value: str


def parse_input(text: str) -> ParsedInput | None:
    raw = (text or "").strip()
    if not raw:
        return None
    match = _LISTEN_EPISODE_URL.match(raw)
    if match:
        return ParsedInput("listen_episode", raw)
    match = _LISTEN_AUDIOBOOK_EPISODE_URL.match(raw)
    if match:
        return ParsedInput("listen_episode", raw)
    match = _LISTEN_AUDIOBOOK_URL.match(raw)
    if match:
        return ParsedInput("listen_audiobook", raw)
    match = _LISTEN_PROGRAM_URL.match(raw)
    if match:
        return ParsedInput("listen_program", raw)
    match = _LISTEN_LIVE_URL.match(raw)
    if match:
        return ParsedInput("listen_live", raw)
    match = _LISTEN_STATION_URL.match(raw)
    if match and match.group("slug").casefold() not in {"programs", "radio", "podcasts"}:
        return ParsedInput("listen_station", raw)
    if "/series/" in raw and "iview.abc.net.au" in raw.casefold():
        raise AbcIviewError(
            "Series URLs are handled through the show page. "
            "Paste a show or video URL instead."
        )
    match = _VIDEO_URL.search(raw)
    if match:
        return ParsedInput("video", match.group("id"))
    match = _SHOW_URL.search(raw)
    if match:
        return ParsedInput("show", match.group("id"))
    if _BARE_ID.fullmatch(raw):
        if _VIDEOISH_ID.match(raw) or raw.isupper():
            return ParsedInput("video", raw)
        return ParsedInput("show", raw)
    return None


@dataclass(frozen=True)
class SearchHit:
    id: str
    title: str
    kind: str  # show | video | podcast_series | podcast_episode | radio_replay | radio | unknown
    type_label: str = ""
    description: str = ""
    url: str = ""

    @property
    def label(self) -> str:
        return self.title or self.id or "Untitled"


@dataclass
class Episode:
    id: str
    title: str
    year: str = ""
    season: int = 0
    number: int = 0
    name: str = ""
    description: str = ""
    is_live: bool = False
    type: str = "video"
    date: str = ""
    collection: str = ""

    @property
    def label(self) -> str:
        if self.is_live:
            group = f" [{self.collection}]" if self.collection else ""
            return f"{self.title}{group}"
        if self.season and self.number:
            return f"S{self.season:02d}E{self.number:02d}  {self.name or self.title}"
        if self.number:
            return f"E{self.number:02d}  {self.name or self.title}"
        return self.name or self.title or self.id


@dataclass
class Show:
    id: str
    title: str
    kind: str  # series | movie | video
    year: str = ""
    video_id: str = ""
    synopsis: str = ""
    unavailable_message: str = ""
    episodes: list[Episode] = field(default_factory=list)


@dataclass
class Source:

    manifest: str
    protected: bool = False
    customdata: str = ""
    video_id: str = ""
    stream_type: str = ""
    playlist_type: str = ""
    is_live: bool = False
    duration: int | None = None
    chapters: list[Chapter] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    subtitles: list[SubtitleReference] = field(default_factory=list)

    def line(self) -> str:
        bits = [self.stream_type or "stream"]
        bits.append("Widevine" if self.protected else "clear")
        if self.is_live:
            bits.append("live")
        if self.subtitles:
            bits.append("subtitles")
        return " · ".join(bits)


@dataclass
class ListenEpisode:

    id: str
    title: str
    url: str
    description: str = ""
    duration: float | None = None
    cover_url: str = ""
    presenter: str = ""
    year: str = ""

    @property
    def label(self) -> str:
        return self.title or self.id


@dataclass
class ListenProgram:

    slug: str
    title: str
    description: str = ""
    episodes: list[ListenEpisode] = field(default_factory=list)


@dataclass
class ListenMedia:

    id: str
    title: str
    url: str
    mime: str = ""
    codec: str = ""
    duration: float | None = None
    description: str = ""
    cover_url: str = ""
    program: str = ""
    year: str = ""
    artist: str = ""
    album: str = ""
    bitrate: int | None = None
    is_live: bool = False
    alternates: tuple[str, ...] = ()


@dataclass
class RadioStation:

    id: str
    title: str
    url: str
    description: str = ""
    cover_url: str = ""
    region: str = ""

    @property
    def label(self) -> str:
        return self.title or self.id


@dataclass
class SessionState:

    device_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    login_method: str = "anonymous"
    uid: str = ""
    account_type: str = ""
    display_name: str = ""
    email: str = ""
    request_id: str = ""
    updated_at: int = 0

    @classmethod
    def from_cache(cls, raw: Any) -> SessionState:
        data = raw if isinstance(raw, dict) else {}
        return cls(
            device_id=str(data.get("device_id") or uuid.uuid4().hex),
            login_method=normalize_login_method(data.get("login_method")),
            uid=str(data.get("uid") or data.get("UID") or ""),
            account_type=str(data.get("account_type") or ""),
            display_name=str(data.get("display_name") or ""),
            email=str(data.get("email") or ""),
            request_id=str(data.get("request_id") or ""),
            updated_at=_int(data.get("updated_at")) or 0,
        )

    def to_cache(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "device_id": self.device_id,
            "login_method": normalize_login_method(self.login_method),
            "uid": self.uid,
            "account_type": self.account_type,
            "display_name": self.display_name,
            "email": self.email,
            "request_id": self.request_id,
            "updated_at": self.updated_at or int(time.time()),
        }

    @property
    def linked(self) -> bool:
        return bool(self.uid)

    @property
    def account_label(self) -> str:
        return self.display_name or self.email or self.uid or "ABC account"

    def clear_account(self) -> None:
        self.login_method = "anonymous"
        self.uid = ""
        self.account_type = ""
        self.display_name = ""
        self.email = ""
        self.request_id = ""


@dataclass(frozen=True)
class TvLinkChallenge:
    code: str
    verification_url: str
    device_id: str
    request_id: str = ""
    expires_at: float = 0.0
    interval: float = TV_LINK_INTERVAL

    @property
    def expires_in(self) -> float:
        if not self.expires_at:
            return TV_LINK_TIMEOUT
        return max(1.0, self.expires_at - time.time())



class AbcIviewApi:

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        state: SessionState | None = None,
        on_save: Callable[[SessionState], Any] | None = None,
    ):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.session.headers.setdefault("accept", "application/json")
        self.session.headers.setdefault("accept-language", "en-US,en;q=0.8")
        self.state = state or SessionState()
        self.on_save = on_save
        self.jwt_token: str | None = None
        self.jwt_expiry: int = 0
        self.profile_status: dict[str, Any] | None = None

    def anonymous_login(self) -> dict[str, Any]:
        self.profile_status = self._json(
            "GET", PROFILE_STATUS, base_url=PROFILE_BASE_URL
        )
        return self.profile_status

    def start_tv_link(self) -> TvLinkChallenge:
        payload = self._profile_json(
            "POST",
            PROFILE_LINK,
            json={
                "productId": PROFILE_PRODUCT_ID,
                "deviceId": self.state.device_id,
                "deviceName": "UniDL Android TV",
            },
        )
        code = str(payload.get("code") or "")
        request_id = str(payload.get("requestId") or "")
        if not code or not request_id:
            raise AbcIviewError("ABC iView TV-link response carried no code")
        verification_url = str(payload.get("url") or TV_LINK_URL).strip()
        if not verification_url.startswith(("http://", "https://")):
            verification_url = f"https://{verification_url.lstrip('/')}"
        self.state.login_method = "tv"
        self.state.request_id = request_id
        self._persist()
        return TvLinkChallenge(
            code=code,
            verification_url=verification_url,
            device_id=self.state.device_id,
            request_id=request_id,
            expires_at=_iso_epoch(payload.get("expiryAt")),
        )

    def poll_tv_link(self) -> SessionState | None:
        try:
            payload = self._profile_json(
                "GET",
                PROFILE_STATUS_LINK,
                params={
                    "productId": PROFILE_PRODUCT_ID,
                    "deviceId": self.state.device_id,
                },
            )
        except AbcIviewError as exc:
            if "(404)" in str(exc) or "HTTP 404" in str(exc):
                return None
            raise

        uid = str(payload.get("UID") or payload.get("uid") or "")
        if not uid:
            return None
        self.state.login_method = "tv"
        self.state.uid = uid
        self.state.account_type = str(payload.get("accountType") or "")
        self.state.display_name = str(payload.get("displayName") or "")
        self.state.email = str(payload.get("email") or "")
        self.state.request_id = str(payload.get("requestId") or self.state.request_id)
        self._persist()
        return self.state

    def unlink_tv(self) -> None:
        if not self.state.uid:
            return
        try:
            self._profile_json(
                "POST",
                PROFILE_UNLINK,
                json={
                    "productId": PROFILE_PRODUCT_ID,
                    "deviceId": self.state.device_id,
                    "UID": self.state.uid,
                },
            )
        finally:
            self.state.clear_account()
            self._persist()

    def _persist(self) -> None:
        if self.on_save is not None:
            self.on_save(self.state)

    def _profile_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["X-API-KEY"] = PROFILE_API_KEY
        headers.setdefault("Accept", "application/json")
        kwargs.setdefault("timeout", HTTP_TIMEOUT)
        try:
            response = self.session.request(
                method,
                urljoin(PROFILE_BASE_URL, path),
                params=params,
                headers=headers,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise AbcIviewError(f"ABC iView profile request failed: {exc}") from exc
        if response.status_code != 200:
            raise AbcIviewError(
                f"ABC iView profile request failed ({response.status_code}): "
                f"{_preview(response.text)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AbcIviewError("ABC iView profile response was not JSON") from exc
        if not isinstance(payload, dict):
            raise AbcIviewError("ABC iView profile response had an unexpected shape")
        return payload

    def get_jwt_token(self, *, force_refresh: bool = False) -> str:
        now = int(time.time())
        if (
            not force_refresh
            and self.jwt_token
            and (not self.jwt_expiry or self.jwt_expiry - now > JWT_MIN_TTL)
        ):
            return self.jwt_token

        url = urljoin(BASE_URL, JWT_TOKEN)
        try:
            response = self.session.post(
                url, data={"clientId": CLIENT_ID}, timeout=HTTP_TIMEOUT
            )
        except requests.RequestException as exc:
            raise AbcIviewError(f"could not reach ABC iView for a JWT: {exc}") from exc
        if response.status_code != 200:
            raise AbcIviewError(
                f"JWT request failed ({response.status_code}): {_preview(response.text)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AbcIviewError("JWT response was not JSON") from exc
        token = str(payload.get("token") or "")
        if not token:
            raise AbcIviewError("JWT response carried no token")
        self.jwt_token = token
        self.jwt_expiry = _jwt_expiry(token)
        return token

    def search(self, query: str) -> list[SearchHit]:
        params = urlencode(
            {
                "x-algolia-agent": "Algolia for Kotlin (2.1.0); Android TV",
                "x-algolia-api-key": ALGOLIA_API_KEY,
                "x-algolia-application-id": ALGOLIA_APP_ID,
            }
        )
        url = f"{ALGOLIA_SEARCH}?{params}"
        search_params = urlencode(
            {
                "query": query,
                "hitsPerPage": 20,
                "userToken": ALGOLIA_USER_TOKEN,
                "clickAnalytics": "true",
            }
        )
        payload = {
            "requests": [
                {
                    "indexName": ALGOLIA_INDEX,
                    "params": search_params,
                }
            ],
        }
        try:
            response = self.session.post(url, json=payload, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise AbcIviewError(f"search failed: {exc}") from exc
        except ValueError as exc:
            raise AbcIviewError("search response was not JSON") from exc

        hits = (data.get("results") or [{}])[0].get("hits") or []
        found: list[SearchHit] = []
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            parsed = _parse_search_hit(hit)
            if parsed is not None:
                found.append(parsed)
        try:
            found.extend(self.search_listen(query))
        except AbcIviewError:
            pass
        return found

    def search_listen(self, query: str) -> list[SearchHit]:
        found: list[SearchHit] = []
        seen: set[str] = set()
        for filters, limit in (("", 20), ("contentType:program", 5), ("contentType:channel", 5)):
            try:
                data = self._listen_search_payload(query, filters=filters, limit=limit)
            except AbcIviewError:
                if not found:
                    raise
                continue
            for hit in data.get("hits") or []:
                if not isinstance(hit, dict):
                    continue
                parsed = _parse_listen_search_hit(hit)
                if parsed is None or parsed.url in seen:
                    continue
                seen.add(parsed.url)
                found.append(parsed)
        return found

    def _listen_search_payload(
        self, query: str, *, filters: str = "", limit: int = 20
    ) -> dict[str, Any]:
        headers = {
            "X-Algolia-Application-Id": LISTEN_ALGOLIA_APP_ID,
            "X-Algolia-API-Key": LISTEN_ALGOLIA_API_KEY,
        }
        try:
            response = self.session.post(
                LISTEN_ALGOLIA_SEARCH,
                headers=headers,
                json={
                    "query": query,
                    "hitsPerPage": limit,
                    "clickAnalytics": True,
                    **({"filters": filters} if filters else {}),
                },
                timeout=HTTP_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise AbcIviewError(f"ABC Listen search failed: {exc}") from exc
        except ValueError as exc:
            raise AbcIviewError("ABC Listen search response was not JSON") from exc
        if not isinstance(data, dict):
            raise AbcIviewError("ABC Listen search returned an unexpected shape")
        return data

    def get_listen_program(self, url_or_slug: str) -> ListenProgram:
        url = _listen_page_url(url_or_slug, "/listen/programs/")
        page = self._listen_page(url)
        page_props = page.get("props", {}).get("pageProps", {})
        hero = page_props.get("heroImageWithCTAPrepared") or {}
        slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        title = str(hero.get("heading") or _title_from_slug(slug))
        description = _listen_text(
            (page_props.get("heroDescriptionPrepared") or {}).get("descriptionPrepared")
        )

        episodes: list[ListenEpisode] = []
        seen: set[str] = set()
        blocks = [page_props.get("latestEpisodePrepared") or {}, page_props.get("programCollectionPrepared") or {}]
        collection = blocks[-1]
        pagination = collection.get("pagination") or {}
        total = _int(pagination.get("total")) or 0
        page_size = _int(pagination.get("size")) or _int(pagination.get("collectionLoaderLimit")) or 0
        if total > len(collection.get("items") or []) and page_size:
            page_count = (total + page_size - 1) // page_size
            separator = "&" if "?" in url else "?"
            for page_number in range(2, page_count + 1):
                next_page = self._listen_page(f"{url}{separator}page={page_number}")
                next_props = next_page.get("props", {}).get("pageProps", {})
                blocks.append(next_props.get("programCollectionPrepared") or {})

        for block in blocks:
            for item in block.get("items") or []:
                episode = _listen_episode_card(item, program_url=url)
                if episode is None or episode.id in seen:
                    continue
                seen.add(episode.id)
                episodes.append(episode)
        if not episodes:
            raise AbcIviewError(f"ABC Listen programme {title} has no published episodes")
        return ListenProgram(
            slug=slug,
            title=title,
            description=description,
            episodes=episodes,
        )

    def get_listen_audiobook(self, url: str) -> ListenProgram:
        page_url = _listen_page_url(url, "/listen/audiobooks/")
        try:
            page = self._listen_page(page_url)
            page_props = page.get("props", {}).get("pageProps", {})
        except AbcIviewError:
            page_props = {}
        hero = page_props.get("heroImageWithCTAPrepared") or {}
        title = str(hero.get("heading") or _title_from_slug(_listen_audiobook_slug(page_url)))
        cover_url = _listen_image(hero.get("heroImagePrepared") or {})
        program_id = _coremedia_id(hero.get("uri"))
        hits = self._listen_search_payload(title, filters="contentType:audioepisode", limit=100).get("hits") or []
        prefix = page_url.rstrip("/") + "/"
        episodes: list[ListenEpisode] = []
        seen: set[str] = set()
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            episode = _listen_episode_from_search_hit(
                hit,
                prefix=prefix,
                program_id=program_id,
                program_title=title,
                cover_url=cover_url,
            )
            if episode is None or episode.id in seen:
                continue
            seen.add(episode.id)
            episodes.append(episode)
        if not episodes:
            raise AbcIviewError(f"ABC audiobook {title} has no downloadable chapters")
        episodes.sort(key=lambda item: _audiobook_chapter_number(item.title, item.id))
        return ListenProgram(
            slug=_listen_audiobook_slug(page_url),
            title=title,
            description=_listen_text(
                (page_props.get("heroDescriptionPrepared") or {}).get("descriptionPrepared")
            ),
            episodes=episodes,
        )

    def get_listen_episode(self, url_or_id: str) -> ListenMedia:
        if str(url_or_id).strip().isdigit():
            raise AbcIviewError(
                "An ABC Listen episode needs its programme URL; open the programme first."
            )
        url = _listen_page_url(url_or_id, "/listen/programs/")
        try:
            page = self._listen_page(url)
            page_props = page.get("props", {}).get("pageProps", {})
            document = ((page_props.get("data") or {}).get("documentProps") or {})
        except AbcIviewError:
            page_props = {}
            document = {}
        if not isinstance(document, dict):
            raise AbcIviewError("ABC Listen episode page has no media metadata")

        renditions = [
            item
            for item in document.get("renditions") or []
            if isinstance(item, dict)
            and str(item.get("url") or "").startswith(("http://", "https://"))
            and str(item.get("MIMEType") or item.get("mimeType") or "").lower().startswith("audio/")
        ]
        if not renditions:
            hit = self._find_listen_asset(url)
            if hit is not None:
                media = _listen_media_from_search_hit(hit)
                if media is not None:
                    return media
            raise AbcIviewError("ABC Listen episode has no downloadable audio rendition")
        rendition = max(renditions, key=lambda item: _int(item.get("bitrate")) or 0)
        mime = str(rendition.get("MIMEType") or rendition.get("mimeType") or "").lower()
        media_url = str(rendition.get("url") or "")
        title = str(document.get("title") or document.get("id") or "ABC Listen episode")
        program = _listen_program_title(page_props, document)
        if program == "ABC Listen":
            program = _title_from_slug(_listen_program_slug(url))
        return ListenMedia(
            id=str(document.get("id") or ""),
            title=title,
            url=media_url,
            mime=mime,
            codec=_listen_codec(mime, str(rendition.get("codec") or "")),
            duration=_number(document.get("duration")),
            description=_listen_text(document.get("synopsis")),
            cover_url=_listen_image(document.get("thumbnailLink")),
            program=program,
            year=_listen_year(document.get("availableFrom")),
            artist=program,
            album=program,
            bitrate=_int(rendition.get("bitrate")) or None,
        )

    def _find_listen_asset(self, url: str) -> dict[str, Any] | None:
        parsed = urlsplit(url)
        target = parsed.path.rstrip("/")
        slug = target.rsplit("/", 1)[-1].replace("-", " ")
        parent_slug = target.rsplit("/", 2)[-2].replace("-", " ")
        candidates: list[dict[str, Any]] = []
        for query in (slug, parent_slug):
            try:
                hits = self._listen_search_payload(
                    query, filters="contentType:audioepisode", limit=100
                ).get("hits") or []
            except AbcIviewError:
                continue
            candidates.extend(hit for hit in hits if isinstance(hit, dict))
        for hit in candidates:
            canonical = str(hit.get("canonicalURL") or hit.get("canonicalUrl") or "").rstrip("/")
            if urlsplit(canonical).path.rstrip("/") == target:
                return hit
        return None

    def get_radio_stations(self) -> list[RadioStation]:
        page = self._listen_page(urljoin(LISTEN_BASE_URL, LISTEN_RADIO_PAGE))
        page_props = page.get("props", {}).get("pageProps", {})
        components = ((page_props.get("data") or {}).get("componentsContent") or [])
        stations: list[RadioStation] = []
        seen: set[str] = set()
        for component in components:
            if not isinstance(component, dict) or component.get("component") != "MetaCollection":
                continue
            props = component.get("componentProps") or {}
            root_uri = str(props.get("contentUri") or "")
            root_id = root_uri.rsplit("/", 1)[-1] or str(component.get("key") or "")
            for group in props.get("items") or []:
                if not isinstance(group, dict):
                    continue
                collection_id = str(group.get("id") or "")
                if not collection_id:
                    continue
                collection = self._listen_collection(
                    collection_id,
                    root_id,
                    region=str(group.get("title") or props.get("heading") or ""),
                )
                for station in collection:
                    if station.url in seen:
                        continue
                    seen.add(station.url)
                    stations.append(station)
        if not stations:
            raise AbcIviewError("ABC Listen radio directory returned no stations")
        return stations

    def _listen_collection(
        self, collection_id: str, root_id: str, *, region: str
    ) -> list[RadioStation]:
        query = urlencode(
            {
                "collectionId": collection_id,
                "rootMetaCollectionId": root_id,
                "metaInMeta": "false",
            }
        )
        payload = self._listen_json(f"{LISTEN_RADIO_COLLECTION}?{query}")
        collection = payload.get("collection") or {}
        stations: list[RadioStation] = []
        for item in collection.get("items") or []:
            if not isinstance(item, dict):
                continue
            link = str(item.get("articleLink") or "")
            if not link:
                continue
            url = urljoin(LISTEN_BASE_URL, link)
            parsed = urlsplit(url)
            if parsed.hostname not in {"abc.net.au", "www.abc.net.au"}:
                continue
            if parsed.path.rstrip("/") in {"/listen/radio", "/listen/live"}:
                continue
            station_id = str(item.get("cardId") or item.get("contentUri") or url)
            stations.append(
                RadioStation(
                    id=station_id,
                    title=str(item.get("cardTitle") or "ABC Radio"),
                    url=url,
                    description=str(item.get("description") or ""),
                    cover_url=_listen_image(item.get("cardImagePrepared")),
                    region=region,
                )
            )
        return stations

    def get_radio_station(self, url_or_slug: str) -> ListenMedia:
        url = _radio_live_url(url_or_slug)
        page = self._listen_page(url)
        page_props = page.get("props", {}).get("pageProps", {})
        live_props = _find_live_player(page_props)
        if not live_props:
            raise AbcIviewError("ABC Listen live page has no radio player")

        sources = _listen_sources(live_props.get("config"))
        if not sources:
            raise AbcIviewError("ABC Listen radio page has no playable audio source")
        primary = next((source for source in sources if source[1] == "hls"), sources[0])
        slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        heading = str(live_props.get("heading") or "")
        logo = (live_props.get("radioHeadingPrepared") or {}).get("logoPrepared") or {}
        logo_alt = str(logo.get("altText") or "")
        station = logo_alt if logo_alt.lower().startswith("abc ") else f"ABC {heading or _title_from_slug(slug)}"
        station = station.strip()
        program = str(live_props.get("title") or "")
        alternates = tuple(source[0] for source in sources if source[0] != primary[0])
        return ListenMedia(
            id=slug,
            title=station,
            url=primary[0],
            mime="application/vnd.apple.mpegurl" if primary[1] == "hls" else "audio/aac",
            codec="aac",
            description="",
            cover_url=_listen_image(live_props.get("preparedImage")),
            program=program,
            artist=station,
            album=station,
            is_live=True,
            alternates=alternates,
        )

    def _listen_json(self, path: str) -> dict[str, Any]:
        url = path if path.startswith("http") else urljoin(LISTEN_BASE_URL, path)
        try:
            response = self.session.get(
                url,
                headers={"Accept": "application/json"},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise AbcIviewError(f"could not reach ABC Listen: {exc}") from exc
        if response.status_code != 200:
            raise AbcIviewError(
                f"ABC Listen collection failed ({response.status_code}): {_preview(response.text)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AbcIviewError("ABC Listen collection response was not JSON") from exc
        if not isinstance(payload, dict):
            raise AbcIviewError("ABC Listen collection returned an unexpected shape")
        return payload

    def _listen_page(self, url: str) -> dict[str, Any]:
        try:
            response = self.session.get(
                url,
                headers={"Accept": "text/html,application/xhtml+xml"},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise AbcIviewError(f"could not reach ABC Listen: {exc}") from exc
        if response.status_code == 404:
            raise AbcIviewError("ABC Listen has nothing at that address")
        if response.status_code != 200:
            raise AbcIviewError(
                f"ABC Listen page failed ({response.status_code}): {_preview(response.text)}"
            )
        match = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            response.text,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            raise AbcIviewError("ABC Listen page is missing its media data")
        try:
            payload = json.loads(match.group(1))
        except ValueError as exc:
            raise AbcIviewError("ABC Listen page contains malformed media data") from exc
        if not isinstance(payload, dict):
            raise AbcIviewError("ABC Listen page returned an unexpected data shape")
        return payload

    def get_show(self, show_id: str) -> Show:
        data = self._json(
            "GET", SHOW_INFO.format(show_id=quote(show_id, safe=""))
        )
        title = str(data.get("title") or show_id)
        label = str(data.get("type") or "").lower()
        year = str(data.get("productionYear") or "") or ""
        highlight = (data.get("_embedded") or {}).get("highlightVideo") or {}
        video_id = str(highlight.get("id") or "") if isinstance(highlight, dict) else ""
        synopsis = str(data.get("description") or data.get("synopsis") or "")
        unavailable_message = str(data.get("unavailableMessage") or "")

        if label in ("series", "program"):
            return Show(
                id=show_id,
                title=title,
                kind="series",
                year=year,
                video_id=video_id,
                synopsis=synopsis,
                unavailable_message=unavailable_message,
                episodes=self.get_series_episodes(show_id, data=data),
            )
        if label in ("feature", "movie", "single"):
            return Show(
                id=show_id,
                title=title,
                kind="movie",
                year=year,
                video_id=video_id,
                synopsis=synopsis,
                unavailable_message=unavailable_message,
            )
        return Show(
            id=show_id,
            title=title,
            kind="video" if video_id else "series",
            year=year,
            video_id=video_id,
            synopsis=synopsis,
            unavailable_message=unavailable_message,
            episodes=(
                self.get_series_episodes(show_id, data=data)
                if not video_id
                else []
            ),
        )

    def get_series_episodes(
        self, series_id: str, data: dict[str, Any] | None = None
    ) -> list[Episode]:
        if data is None:
            data = self._json(
                "GET", SHOW_INFO.format(show_id=quote(series_id, safe=""))
            )
        season_refs = (data.get("_embedded") or {}).get("seriesList") or []
        selected = (data.get("_embedded") or {}).get("selectedSeries")
        seasons: list[dict[str, Any]] = []
        selected_id = selected.get("id") if isinstance(selected, dict) else None

        for ref in season_refs:
            if not isinstance(ref, dict):
                continue
            if selected_id and ref.get("id") == selected_id and isinstance(selected, dict):
                seasons.append(selected)
                continue
            href = ((ref.get("_links") or {}).get("self") or {}).get("href") or ""
            if not href:
                seasons.append(ref)
                continue
            seasons.append(self._json("GET", f"/v3{href}"))

        if not seasons and isinstance(selected, dict):
            seasons.append(selected)

        episodes: list[Episode] = []
        seen: set[str] = set()
        for season in seasons:
            if not isinstance(season, dict):
                continue
            block = (season.get("_embedded") or {}).get("videoEpisodes") or {}
            if isinstance(block, list):
                items = block
            else:
                items = block.get("items") or []
            for episode in items:
                if not isinstance(episode, dict):
                    continue
                parsed = parse_episode(episode)
                if not parsed.id or parsed.id in seen:
                    continue
                seen.add(parsed.id)
                episodes.append(parsed)
        return episodes

    def get_video(self, video_id: str) -> Episode:
        data = self._json(
            "GET", VIDEO_INFO.format(video_id=quote(video_id, safe=""))
        )
        episode = parse_episode(data)
        if episode.type in {"feature", "movie", "single"} and not episode.year:
            show_id = _linked_show_id(data)
            if show_id:
                episode.year = self.get_show(show_id).year
        return episode

    def get_live_channels(self) -> list[Episode]:
        data = self._json("GET", LIVE_CATEGORY)
        collections: list[dict[str, Any]] = []
        featured = (data.get("_embedded") or {}).get("featuredCollection")
        if isinstance(featured, dict):
            collections.append(featured)
        collections.extend(
            item
            for item in ((data.get("_embedded") or {}).get("collections") or [])
            if isinstance(item, dict)
        )

        channels: list[Episode] = []
        seen: set[str] = set()
        for collection in collections:
            collection_title = str(collection.get("title") or "")
            for item in collection.get("items") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("_entity") != "video" and item.get("type") != "livestream":
                    continue
                video_id = str(item.get("id") or item.get("houseNumber") or "")
                if not video_id or video_id in seen:
                    continue
                seen.add(video_id)
                parsed = parse_episode(item)
                parsed.collection = collection_title
                channels.append(parsed)
        return channels

    def get_source(self, video_id: str, *, codec: str = "auto") -> Source:
        video = self._json(
            "GET", VIDEO_INFO.format(video_id=quote(video_id, safe=""))
        )
        if not video.get("playable"):
            raise AbcIviewError(
                str(video.get("unavailableMessage") or "Video is not playable")
            )

        playlist = (video.get("_embedded") or {}).get("playlist") or []
        if isinstance(playlist, dict):
            playlist = [playlist]
        if not playlist:
            raise AbcIviewError("Could not find playlist for this video")

        playlist_item = next(
            (
                entry
                for entry in playlist
                if isinstance(entry, dict)
                and entry.get("type") in {"program", "livestream", "trailer"}
            ),
            None,
        )
        if playlist_item is None:
            raise AbcIviewError(
                "Could not find program, livestream, or trailer streams"
            )

        stream_groups = playlist_item.get("streams") or {}
        stream_type = _select_stream_type(stream_groups, codec=codec)
        if not stream_type:
            requested = _normalize_codec(codec)
            if requested == "dash":
                raise AbcIviewError("DASH is not available for this video")
            if requested == "hls":
                raise AbcIviewError("HLS is not available for this video")
            raise AbcIviewError("Could not find a supported stream type")

        streams = stream_groups.get(stream_type) or {}
        if not isinstance(streams, dict):
            raise AbcIviewError("Stream group had an unexpected shape")

        manifest = _stream_manifest(streams)
        if not manifest:
            raise AbcIviewError("Could not find a working manifest")

        is_live = (
            video.get("type") == "livestream"
            or playlist_item.get("type") == "livestream"
        )
        if stream_type in DASH_STREAM_TYPES and not is_live:
            manifest = self._prefer_dash_1080(
                manifest, duration=_int(video.get("duration"))
            )
        elif stream_type in HLS_STREAM_TYPES:
            manifest = self._append_hls_token(manifest, video_id)

        protected = bool(streams.get("protected", False))
        customdata = ""
        if protected:
            customdata = self.get_license_customdata(video_id)

        return Source(
            manifest=str(manifest),
            protected=protected,
            customdata=customdata,
            video_id=video_id,
            stream_type=stream_type,
            playlist_type=str(playlist_item.get("type") or ""),
            is_live=bool(is_live),
            duration=_int(video.get("duration")) or None,
            chapters=[] if is_live else _video_chapters(video),
            attachments=_video_attachments(video),
            subtitles=[] if is_live else _video_subtitles(playlist_item),
        )

    def _prefer_dash_1080(self, manifest: str, *, duration: int | None) -> str:
        parsed = urlsplit(manifest)
        if not parsed.path.endswith("/720.mpd"):
            return manifest
        candidate = parsed._replace(path=parsed.path[:-len("720.mpd")] + "1080.mpd").geturl()
        try:
            with self.session.get(candidate, timeout=DASH_PROBE_TIMEOUT) as response:
                if response.status_code != 200:
                    return manifest
                tracks = parse_dash(response.url or candidate, response.text)
            video = next(
                (
                    track for track in tracks
                    if track.media_type == "video"
                    and not track.extra.get("trick_mode")
                    and (track.resolution or "").endswith("x1080")
                ),
                None,
            )
            audio = next((track for track in tracks if track.media_type == "audio"), None)
            if video is None or audio is None:
                return manifest
            minimum_duration = max(0, (duration or 0) - max(5, (duration or 0) * 0.01))
            if any((track.total_duration or 0) < minimum_duration for track in (video, audio)):
                return manifest
            if not all(self._probe_dash_track(track) for track in (video, audio)):
                return manifest
        except (requests.RequestException, ParseError, ValueError, ArithmeticError):
            return manifest
        return candidate

    def _probe_dash_track(self, track: StreamInfo) -> bool:
        segment = next((item for item in track.segments if item.index != -1), None)
        url = segment.url if segment is not None else track.url
        if not url:
            return False
        start = segment.byte_range[0] if segment is not None and segment.byte_range else 0
        with self.session.get(
            url,
            headers={"Range": f"bytes={start}-{start + 31}"},
            stream=True,
            timeout=DASH_PROBE_TIMEOUT,
        ) as response:
            if response.status_code not in {200, 206}:
                return False
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type.startswith("text/") or content_type in {"application/json", "application/xml"}:
                return False
            return bool(next(response.iter_content(chunk_size=32), b""))

    def _append_hls_token(self, manifest: str, video_id: str) -> str:
        token = self.get_hls_token(video_id)
        separator = "&" if "?" in manifest else "?"
        return f"{manifest}{separator}hdnea={token}"

    def get_hls_token(self, video_id: str) -> str:
        try:
            time_response = self.session.get(
                urljoin(BASE_URL, HLS_TIME), timeout=HTTP_TIMEOUT
            )
            time_response.raise_for_status()
            timestamp = time_response.text.strip().strip('"')
            if not timestamp.isdigit():
                raise AbcIviewError("HLS time response was not an epoch timestamp")

            query = urlencode(
                {
                    "ts": timestamp,
                    "hn": video_id,
                    "d": HLS_DEVICE,
                }
            )
            signed_path = f"{HLS_SIGN}?{query}"
            signature = hmac.new(
                HLS_SECRET.encode("utf-8"),
                signed_path.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            response = self.session.get(
                urljoin(BASE_URL, f"{signed_path}&sig={signature}"),
                timeout=HTTP_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise AbcIviewError(f"HLS token request failed: {exc}") from exc

        token = response.text.strip().strip('"')
        if not token:
            raise AbcIviewError("HLS token response was empty")
        return token

    def get_license_customdata(self, video_id: str) -> str:
        url = urljoin(
            BASE_URL, DRM_TOKEN.format(video_id=quote(video_id, safe=""))
        )
        token = self.get_jwt_token()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            response = self.session.post(
                url, headers=headers, data=b"", timeout=HTTP_TIMEOUT
            )
        except requests.RequestException as exc:
            raise AbcIviewError(f"DRM token request failed: {exc}") from exc

        if response.status_code in (401, 403):
            token = self.get_jwt_token(force_refresh=True)
            headers = {"Authorization": f"Bearer {token}"}
            try:
                response = self.session.post(
                    url, headers=headers, data=b"", timeout=HTTP_TIMEOUT
                )
            except requests.RequestException as exc:
                raise AbcIviewError(f"DRM token request failed: {exc}") from exc

        if response.status_code != 200:
            raise AbcIviewError(
                f"DRM token failed ({response.status_code}): {_preview(response.text)}"
            )
        try:
            drm_data = response.json()
        except ValueError as exc:
            raise AbcIviewError("DRM token response was not JSON") from exc
        license_data = str(drm_data.get("license") or "")
        if not license_data:
            raise AbcIviewError("DRM token response carried no license customdata")
        return license_data

    def widevine_license(
        self,
        challenge: bytes,
        *,
        customdata: str,
        license_url: str = LICENSE_URL,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        if not license_url:
            raise AbcIviewError("no KeyOS licence URL came with this playback")
        if not customdata:
            raise AbcIviewError("no KeyOS customdata came with this playback")
        request_headers = {
            "Content-Type": "application/octet-stream",
            "x-keyos-authorization": customdata,
        }
        request_headers.update(headers or {})
        try:
            response = self.session.post(
                license_url,
                data=challenge,
                headers=request_headers,
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise AbcIviewError(f"could not reach KeyOS: {exc}") from exc
        if not response.ok:
            raise AbcIviewError(
                f"KeyOS answered {response.status_code}: {_preview(response.text)}"
            )
        return response.content

    def subtitle_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> bytes:
        target = _valid_http_url(url)
        if not target:
            raise AbcIviewError("ABC iView returned an invalid subtitle URL")
        request_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/vtt, text/plain, */*",
        }
        for key, value in (headers or {}).items():
            if key and value is not None:
                request_headers[str(key)] = str(value)
        try:
            with self.session.get(
                target,
                headers=request_headers,
                timeout=HTTP_TIMEOUT,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    raise AbcIviewError(
                        f"ABC iView subtitle request failed (HTTP {response.status_code})"
                    )
                size = str(response.headers.get("Content-Length") or "").strip()
                total = int(size) if size.isdigit() else None
                if total is not None and total > MAX_SUBTITLE_BYTES:
                    raise AbcIviewError("ABC iView subtitle exceeds the 16 MiB size limit")
                data = bytearray()
                for chunk in response.iter_content(chunk_size=65_536):
                    if not chunk:
                        continue
                    data.extend(chunk)
                    if len(data) > MAX_SUBTITLE_BYTES:
                        raise AbcIviewError("ABC iView subtitle exceeds the 16 MiB size limit")
                    if progress is not None:
                        progress(len(data), total)
        except requests.RequestException as exc:
            raise AbcIviewError(f"ABC iView subtitle request failed: {exc}") from exc
        if not data:
            raise AbcIviewError("ABC iView subtitle response was empty")
        head = bytes(data[:512]).lstrip().lower()
        if head.startswith((b"<!doctype html", b"<html")):
            raise AbcIviewError("ABC iView subtitle response was HTML instead of captions")
        return bytes(data)

    def _json(
        self,
        method: str,
        api: str,
        *,
        base_url: str = BASE_URL,
        **kwargs: Any,
    ) -> dict[str, Any] | list[Any]:
        url = api if api.startswith("http") else urljoin(base_url, api)
        kwargs.setdefault("timeout", HTTP_TIMEOUT)
        try:
            response = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise AbcIviewError(f"could not reach ABC iView: {exc}") from exc
        if response.status_code == 404:
            raise AbcIviewError("ABC iView has nothing at that address")
        if response.status_code in (401, 403):
            raise AbcIviewError(
                "ABC iView refused that. The free catalogue is AU-only."
            )
        if response.status_code != 200:
            raise AbcIviewError(
                f"Request failed ({response.status_code}): {_preview(response.text)}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise AbcIviewError(
                f"ABC iView returned non-JSON: {_preview(response.text)}"
            ) from exc


def _select_stream_type(stream_groups: Any, *, codec: str = "auto") -> str | None:
    if not isinstance(stream_groups, dict):
        return None

    requested = _normalize_codec(codec)
    families = HLS_STREAM_TYPES if requested == "hls" else DASH_STREAM_TYPES
    available = [name for name in families if _stream_manifest(stream_groups.get(name))]
    if requested != "hls":
        for name in available:
            if _stream_manifest({"1080": stream_groups[name].get("1080")}):
                return name
    if available:
        return available[0]
    if requested == "auto":
        return next(
            (name for name in HLS_STREAM_TYPES if _stream_manifest(stream_groups.get(name))),
            None,
        )
    return None


def _stream_manifest(streams: Any) -> str:
    if isinstance(streams, dict):
        for quality in QUALITY_PRIORITY:
            value = streams.get(quality)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _normalize_codec(value: Any) -> str:
    value = str(value or "auto").strip().lower()
    return value if value in {"auto", "dash", "hls"} else "auto"


def _listen_page_url(value: str, prefix: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith(("http://", "https://")):
        parsed = urlsplit(raw)
        if parsed.hostname not in {"abc.net.au", "www.abc.net.au"}:
            raise AbcIviewError("ABC Listen URL must use www.abc.net.au")
        return raw
    slug = raw.strip("/").split("/")[-1]
    if not slug:
        raise AbcIviewError("ABC Listen URL is empty")
    return urljoin(LISTEN_BASE_URL, f"{prefix.rstrip('/')}/{quote(slug, safe='')}")


def _radio_live_url(value: str) -> str:
    url = _listen_page_url(value, "/listen/live/")
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 2 and parts[0] == "listen" and parts[1] not in {"live", "radio"}:
        parsed = parsed._replace(path=f"/listen/live/{parts[1]}")
        return parsed.geturl()
    return url


def _listen_episode_card(item: Any, *, program_url: str) -> ListenEpisode | None:
    if not isinstance(item, dict):
        return None
    content_uri = str(item.get("contentUri") or "")
    match = re.search(r"audioepisode/(\d+)", content_uri, re.IGNORECASE)
    episode_id = str(item.get("cardId") or (match.group(1) if match else ""))
    if not episode_id:
        return None
    article_link = str(item.get("articleLink") or "")
    if not article_link:
        return None
    presenters = item.get("presentersPrepared") or []
    presenter = ""
    if isinstance(presenters, list) and presenters and isinstance(presenters[0], dict):
        presenter = str((presenters[0].get("names") or {}).get("full") or "")
    attribution = item.get("cardAttributionPrepared") or {}
    indicator = item.get("cardMediaIndicatorPrepared") or {}
    return ListenEpisode(
        id=episode_id,
        title=str(item.get("cardTitle") or episode_id),
        url=urljoin(program_url, article_link),
        description=_listen_text(item.get("description")),
        duration=_number(indicator.get("duration")),
        cover_url=_listen_image(item.get("cardImagePrepared")),
        presenter=presenter,
        year=_listen_year(attribution.get("publishedDate")),
    )


def _listen_text(value: Any) -> str:
    parts: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, str):
            text = re.sub(r"\s+", " ", node).strip()
            if text:
                parts.append(text)
            return
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if not isinstance(node, dict):
            return
        if "content" in node:
            visit(node["content"])
            return
        if "children" in node:
            visit(node["children"])
            return
        if "descriptor" in node:
            visit(node["descriptor"])
            return
        for key in ("descriptionPrepared", "text", "value"):
            if key in node:
                visit(node[key])
                return

    visit(value)
    return " ".join(parts)


def _listen_image(value: Any) -> str:
    if isinstance(value, str):
        return value if value.startswith(("http://", "https://")) else ""
    if not isinstance(value, dict):
        return ""
    direct = str(value.get("imgSrc") or "")
    if direct.startswith(("http://", "https://")):
        return direct
    candidates: list[tuple[int, str, str]] = []
    for group in value.get("cropInfo") or []:
        if not isinstance(group, dict):
            continue
        for crop in group.get("value") or []:
            if not isinstance(crop, dict):
                continue
            url = str(crop.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            candidates.append((_int(crop.get("width")) or 0, str(crop.get("ratio") or ""), url))
    if candidates:
        square = [candidate for candidate in candidates if candidate[1] == "1x1"]
        return max(square or candidates, key=lambda candidate: candidate[0])[2]
    return ""


def _listen_codec(mime: str, declared: str = "") -> str:
    value = f"{declared} {mime}".casefold()
    if "mpeg" in value or "mp3" in value:
        return "mp3"
    if "aac" in value or "mp4" in value or "m3u" in value:
        return "aac"
    return declared.strip().casefold() or "audio"


def _listen_program_title(page_props: dict[str, Any], document: dict[str, Any]) -> str:
    page_data = page_props.get("data") or {}
    for value in (
        (page_props.get("heroImageWithCTAPrepared") or {}).get("heading"),
        page_data.get("title"),
    ):
        if value and str(value).strip():
            title = str(value).strip()
            if title.casefold() != str(document.get("title") or "").casefold():
                return title
    return "ABC Listen"


def _listen_program_slug(url: str) -> str:
    parts = [part for part in urlsplit(url).path.split("/") if part]
    try:
        return parts[parts.index("programs") + 1]
    except (ValueError, IndexError):
        return "abc-listen"


def _listen_audiobook_slug(url: str) -> str:
    parts = [part for part in urlsplit(url).path.split("/") if part]
    try:
        return parts[parts.index("audiobooks") + 1]
    except (ValueError, IndexError):
        return "audiobook"


def _coremedia_id(value: Any) -> str:
    match = re.search(r"/(\d+)$", str(value or ""))
    return match.group(1) if match else ""


def _listen_episode_from_search_hit(
    hit: dict[str, Any],
    *,
    prefix: str,
    program_id: str,
    program_title: str,
    cover_url: str,
) -> ListenEpisode | None:
    canonical = str(hit.get("canonicalURL") or hit.get("canonicalUrl") or "").strip()
    if not canonical.startswith(prefix):
        return None
    if str(hit.get("contentType") or "").casefold() != "audioepisode":
        return None
    if program_id and str(hit.get("programID") or "") not in {"", program_id}:
        return None
    media = _listen_media_rendition(hit)
    if media is None:
        return None
    return ListenEpisode(
        id=_coremedia_id(hit.get("objectID")) or _coremedia_id(canonical),
        title=str(hit.get("title") or hit.get("shortTeaserTitle") or "Chapter"),
        url=canonical,
        description=str(hit.get("synopsis") or hit.get("shortTeaserText") or ""),
        duration=_number(hit.get("duration")),
        cover_url=str(hit.get("programDefaultThumbnail", {}).get("1x1") or cover_url),
        presenter=program_title,
        year=_listen_year((hit.get("dates") or {}).get("publicationDate")),
    )


def _listen_media_rendition(hit: dict[str, Any]) -> dict[str, Any] | None:
    for rendition in hit.get("mediaRenditions") or []:
        if not isinstance(rendition, dict):
            continue
        url = str(rendition.get("url") or "")
        mime = str(rendition.get("contentType") or rendition.get("MIMEType") or "").lower()
        if url.startswith(("http://", "https://")) and mime.startswith("audio/"):
            return rendition
    return None


def _listen_media_from_search_hit(hit: dict[str, Any]) -> ListenMedia | None:
    rendition = _listen_media_rendition(hit)
    if rendition is None:
        return None
    mime = str(rendition.get("contentType") or rendition.get("MIMEType") or "").lower()
    program = str(hit.get("programTitle") or "ABC Listen")
    return ListenMedia(
        id=_coremedia_id(hit.get("objectID")) or _coremedia_id(hit.get("canonicalURL")),
        title=str(hit.get("title") or hit.get("shortTeaserTitle") or "ABC Listen episode"),
        url=str(rendition.get("url") or ""),
        mime=mime,
        codec=_listen_codec(mime, str(rendition.get("codec") or "")),
        duration=_number(hit.get("duration")),
        description=str(hit.get("synopsis") or hit.get("shortTeaserText") or ""),
        cover_url=str((hit.get("programDefaultThumbnail") or {}).get("1x1") or ""),
        program=program,
        year=_listen_year((hit.get("dates") or {}).get("publicationDate")),
        artist=program,
        album=program,
        bitrate=_int(rendition.get("bitrate")) or None,
    )


def _audiobook_chapter_number(title: str, fallback: str) -> tuple[int, str]:
    match = re.search(r"(?:chapter|part)\s+(\d+)", title, re.IGNORECASE)
    return (int(match.group(1)) if match else 10_000, fallback)


def _find_live_player(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        for child in value:
            found = _find_live_player(child)
            if found:
                return found
        return None
    if not isinstance(value, dict):
        return None
    component = str(value.get("component") or "")
    props = value.get("componentProps")
    if component.startswith("LiveAudioPlayer") and isinstance(props, dict):
        return props
    for child in value.values():
        found = _find_live_player(child)
        if found:
            return found
    return None


def _listen_sources(config: Any) -> list[tuple[str, str]]:
    if not isinstance(config, dict):
        return []
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source in config.get("sources") or []:
        if not isinstance(source, dict):
            continue
        url = str(source.get("file") or source.get("url") or "")
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        kind = str(source.get("type") or "").casefold()
        if not kind:
            kind = "hls" if ".m3u8" in url.casefold() else "aac"
        seen.add(url)
        result.append((url, kind))
    return result


def _title_from_slug(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title() or "ABC Listen"


def _listen_year(value: Any) -> str:
    match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return match.group(0) if match else ""


def _video_chapters(video: dict[str, Any]) -> list[Chapter]:
    cues = video.get("cuePoints")
    if not isinstance(cues, list):
        return []
    chapters: list[Chapter] = []
    for cue in cues:
        if not isinstance(cue, dict):
            continue
        kind = str(cue.get("type") or "").strip()
        title = str(cue.get("title") or kind.replace("-", " ").title())
        try:
            chapters.append(Chapter.from_seconds(cue.get("start"), title, cue.get("end"), kind=kind))
        except ValueError:
            continue
    return chapters


def _valid_http_url(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    url = raw.strip()
    if not url or any(character.isspace() for character in url):
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
        return ""
    return url


def normalize_subtitle_format(raw: Any, *, url: str = "", default: str = "") -> str:
    explicit = str(raw or "").strip().lower().lstrip(".")
    if explicit:
        normalized = "vtt" if explicit == "webvtt" else explicit
        return normalized if normalized in _SUBTITLE_EXTENSIONS else ""
    path = urlsplit(url).path if url else ""
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    normalized = "vtt" if suffix == "webvtt" else suffix
    if normalized in _SUBTITLE_EXTENSIONS:
        return normalized
    return default


def _normalize_subtitle_kind(raw: Any) -> str:
    value = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if value == "forced":
        return "forced"
    if value in {"sdh", "cc", "closed_captions", "closedcaptions", "captions"}:
        return "sdh"
    if value == "commentary":
        return "commentary"
    if value in {"audio_description", "audiodescription", "descriptive"}:
        return "audio_description"
    return "normal"


def _video_subtitles(playlist_item: dict[str, Any]) -> list[SubtitleReference]:
    raw = playlist_item.get("captions")
    if isinstance(raw, dict):
        entries = [raw]
    elif isinstance(raw, list):
        entries = [item for item in raw if isinstance(item, dict)]
    else:
        return []

    subtitles: list[SubtitleReference] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        url = ""
        key_format = ""
        for key, default_format in _CAPTION_KEYS:
            candidate = _valid_http_url(entry.get(key))
            if candidate:
                url = candidate
                key_format = default_format
                break
        if not url:
            continue
        fmt = normalize_subtitle_format(entry.get("format"), url=url, default=key_format or "vtt")
        if not fmt:
            continue
        language = str(entry.get("language") or entry.get("lang") or "en").strip().replace("_", "-") or "en"
        kind = _normalize_subtitle_kind(entry.get("kind") or entry.get("type"))
        identity = (url, language.casefold(), kind)
        if identity in seen:
            continue
        seen.add(identity)
        default_name = "English" if language.casefold() == "en" or language.casefold().startswith("en-") else language
        name = str(entry.get("name") or entry.get("label") or default_name).strip() or default_name
        subtitles.append(
            SubtitleReference(
                url=url,
                language=language,
                kind=kind,
                name=name,
                format=fmt,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/vtt, text/plain, */*",
                },
            )
        )
    return subtitles


def _video_attachments(video: dict[str, Any]) -> list[Attachment]:
    images = video.get("images")
    if not isinstance(images, list):
        return []
    attachments: list[Attachment] = []
    seen: set[str] = set()
    for image in images:
        if not isinstance(image, dict):
            continue
        url = _valid_http_url(image.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        image_type = str(image.get("type") or "")
        kind = "artwork"
        if image_type.startswith("portrait"):
            kind = "poster"
        elif image_type.startswith("thumb"):
            kind = "thumbnail"
        name = str(image.get("name") or image_type or "Artwork")
        name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("-", " ").title()
        attachments.append(Attachment(url=url, name=name, kind=kind))
    return attachments


def parse_episode(episode: dict[str, Any]) -> Episode:
    title = str(episode.get("showTitle") or episode.get("title") or "Unknown Show")
    episode_id = str(episode.get("id") or "")

    analytics = (episode.get("analytics") or {}).get("dataLayer") or {}
    content_type = str(
        episode.get("type") or analytics.get("d_videoType") or "video"
    ).lower()
    series_id = str(analytics.get("d_series_id") or "")
    episode_name = analytics.get("d_episode_name") or episode.get("displaySubtitle")
    if episode_name is None:
        episode_name = episode.get("title") or ""
    episode_name = str(episode_name)

    display_subtitle = str(episode.get("displaySubtitle") or "")
    episode_number_match = (
        re.search(r"Episode (\d+)", display_subtitle) if display_subtitle else None
    )
    name_match = (
        re.search(r"S\d+\sEpisode\s\d+\s(.*)", episode_name) if episode_name else None
    )

    season = _int(episode.get("seriesNumber")) or 0
    if series_id and "-" in series_id:
        suffix = series_id.rsplit("-", 1)[-1]
        if not season and suffix.isdigit():
            season = int(suffix)

    number = _int(episode.get("episodeNumber")) or 0
    if not number and episode_number_match:
        number = int(episode_number_match.group(1))
    if not number and content_type == "episode":
        if match := re.search(r"[A-Z](\d{3})(?=S\d{2})", episode_id):
            number = int(match.group(1))

    clean_name = episode_name or str(episode.get("title") or "")
    if name_match:
        clean_name = name_match.group(1)
    if clean_name == title:
        clean_name = display_subtitle or str(episode.get("title") or "")

    pub_date = str(episode.get("pubDate") or "")
    date_str = ""
    if pub_date:
        try:
            date_str = datetime.strptime(pub_date, "%Y-%m-%d %H:%M:%S").strftime(
                "%Y%m%d"
            )
        except ValueError:
            date_str = ""

    return Episode(
        id=episode_id,
        title=title,
        year=str(episode.get("productionYear") or ""),
        season=season,
        number=number,
        name=clean_name,
        description=str(episode.get("description") or ""),
        is_live=episode.get("type") == "livestream",
        type=content_type,
        date=date_str,
    )


def _linked_show_id(video: dict[str, Any]) -> str:
    links = video.get("_links") or {}
    show_href = (links.get("show") or {}).get("href") or ""
    match = re.search(r"/show/([^/?#]+)", str(show_href))
    if match:
        return match.group(1)

    deeplink = (links.get("deeplink") or {}).get("href") or ""
    match = re.search(r"/show/([^/?#]+)", str(deeplink))
    return match.group(1) if match else ""


def _parse_search_hit(hit: dict[str, Any]) -> SearchHit | None:
    if hit.get("playable") is False:
        return None

    doc_type = str(hit.get("docType") or "Unknown")
    sub_type = str(hit.get("subType") or hit.get("type") or "")
    title = str(
        hit.get("title")
        or hit.get("programTitle")
        or hit.get("episodeTitle")
        or "Untitled"
    )
    object_id = str(hit.get("objectID") or "")
    content_id = hit.get("id") or hit.get("houseNumber") or hit.get("episodeHouseNumber")
    slug = hit.get("slug")

    kind = "unknown"
    url = str(hit.get("canonicalURL") or "")
    if doc_type == "Program" and slug:
        kind = "show"
        content_id = slug
        url = f"{WWW}/show/{slug}"
    elif doc_type in {"VideoEpisode", "Video", "Episode"} or "video" in object_id:
        kind = "video"
        if not content_id and "/" in object_id:
            content_id = object_id.rsplit("/", 1)[-1]
        if content_id:
            if slug:
                url = f"{WWW}/show/{slug}/video/{content_id}"
            else:
                url = f"{WWW}/video/{content_id}"
    elif sub_type == "livestream":
        kind = "video"

    if not content_id:
        return None

    type_label = " / ".join(part for part in (doc_type, sub_type) if part)
    return SearchHit(
        id=str(content_id),
        title=title,
        kind=kind,
        type_label=type_label,
        description=str(hit.get("synopsis") or ""),
        url=url,
    )


def _parse_listen_search_hit(hit: dict[str, Any]) -> SearchHit | None:
    content_type = str(hit.get("contentType") or hit.get("docType") or "").casefold()
    url = str(hit.get("canonicalURL") or hit.get("canonicalUrl") or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    parsed_url = urlsplit(url)
    if parsed_url.hostname not in {"abc.net.au", "www.abc.net.au"} or not parsed_url.path.startswith("/listen/"):
        return None
    title = str(hit.get("title") or hit.get("programTitle") or "Untitled")
    description = str(
        hit.get("synopsis")
        or hit.get("channel_description")
        or hit.get("shortTeaserText")
        or ""
    )
    if content_type == "program":
        kind = "podcast_series"
        type_label = "Podcast series"
    elif content_type == "channel":
        kind = "radio"
        type_label = "Radio"
    else:
        keywords = " ".join(str(value) for value in (hit.get("keywords") or []))
        podcast = "podcast" in f"{keywords} {description}".casefold()
        audiobook = parsed_url.path.startswith("/listen/audiobooks/")
        if audiobook:
            kind = "audiobook_chapter"
            type_label = "Audiobook chapter"
        else:
            kind = "podcast_episode" if podcast or content_type in {"audioepisode", "audio"} else "radio_replay"
            type_label = "Podcast episode" if kind == "podcast_episode" else "Radio replay"
    object_id = str(hit.get("objectID") or "")
    match = re.search(r"/(\d+)$", object_id)
    content_id = match.group(1) if match else object_id
    if not content_id:
        content_id = url
    return SearchHit(
        id=content_id,
        title=title,
        kind=kind,
        type_label=type_label,
        description=description,
        url=url,
    )


def _jwt_expiry(token: str) -> int:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
        return int(data.get("exp") or 0)
    except Exception:
        return 0


def normalize_login_method(value: Any) -> str:
    return "tv" if str(value or "").strip().lower() == "tv" else "anonymous"


def _iso_epoch(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _preview(text: str, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


__all__ = [
    "ALGOLIA_API_KEY",
    "ALGOLIA_APP_ID",
    "ALGOLIA_INDEX",
    "BASE_URL",
    "CLIENT_ID",
    "LISTEN_BASE_URL",
    "LISTEN_ALGOLIA_API_KEY",
    "LISTEN_ALGOLIA_APP_ID",
    "LISTEN_ALGOLIA_INDEX",
    "LICENSE_URL",
    "QUALITY_PRIORITY",
    "TOKEN_FILE",
    "USER_AGENT",
    "WWW",
    "AbcIviewApi",
    "AbcIviewError",
    "Episode",
    "ListenEpisode",
    "ListenMedia",
    "ListenProgram",
    "RadioStation",
    "ParsedInput",
    "SearchHit",
    "SessionState",
    "Show",
    "Source",
    "TvLinkChallenge",
    "normalize_login_method",
    "normalize_subtitle_format",
    "parse_episode",
    "parse_input",
]
