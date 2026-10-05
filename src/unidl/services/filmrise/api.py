"""FilmRise API client: anonymous session, native search, Widevine DRM, live channels, attachments, and chapters.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter
from ...core.playback import SubtitleReference

logger = logging.getLogger(__name__)

# Android TV APK Constants (com.recipe.filmrise v12.3-146)
APP_PACKAGE = "com.recipe.filmrise"
APP_VERSION = "12.3"
VERSION_CODE = 146
APP_ID = "7"
SITE_ID = "1045"
AUTH_TOKEN = "1216525"
API_VERSION = "sv6.0"
DEVICE_TYPE = 2
DEVICE_TYPE_STR = "googletv-filmrise"
USER_AGENT = "okhttp/4.9.0"

# Base Endpoints
BASE_URL = "https://rapi.ifood.tv"
INDEX_URL = f"{BASE_URL}/index.php"
SUBCAT_URL = f"{BASE_URL}/sub-categories.php"
RECIPES_URL = f"{BASE_URL}/recipes.php"
SHOWS_URL = f"{BASE_URL}/shows.php"
RECIPE_INFO_URL = f"{BASE_URL}/recipeInfo.php"
DRM_LICENSE_URL_TEMPLATE = (
    f"{BASE_URL}/drmService.php?appId={APP_ID}&siteId={SITE_ID}"
    f"&auth-token={AUTH_TOKEN}&version={API_VERSION}&deviceId={{device_id}}&drm_type=widevine"
)
EPG_URL = f"{BASE_URL}/epg.php"

DEFAULT_TTL = 86400  # 24 hours
DEFAULT_TIMEOUT = 30
TOKEN_FILE = "filmrise_token.json"

_LANG_MAP: dict[str, str] = {
    "english": "en",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "portuguese": "pt",
    "russian": "ru",
    "chinese": "zh",
    "japanese": "ja",
    "korean": "ko",
    "hindi": "hi",
    "arabic": "ar",
}


class FilmRiseError(Exception):
    """Base exception for FilmRise service failures."""


def trim_id(val: str | int | None) -> str:
    """Trim 2000-prefixed IDs to canonical node/show IDs.

    Matches com.recipe.util.Utilities.trimID:
    If str contains '2000', removes the leading '2' and strips leading zeros.
    E.g. '200000010758618' -> '10758618', '200000000082992' -> '82992'.
    """
    s = str(val or "").strip()
    if s and "2000" in s:
        s = re.sub(r"^2", "", s)
        s = re.sub(r"^0+", "", s) or "0"
    return s


def clean_live_macro_url(url: str) -> str:
    """Strip unexpanded ad-tracking macros ([FT_...]) from FAST channel playlist URLs."""
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    pairs = [
        p
        for p in parsed.query.split("&")
        if p and not re.search(r"\[.*?\]", p) and not p.endswith("=")
    ]
    new_query = "&".join(pairs)
    return urlunparse(parsed._replace(query=new_query))


def normalize_language(lang: str) -> str:
    """Normalize subtitle language names to standard codes."""
    if not lang:
        return "und"
    clean = lang.strip().lower()
    if clean in _LANG_MAP:
        return _LANG_MAP[clean]
    if len(clean) in (2, 3) and clean.isalpha():
        return clean
    return "und"


@dataclass
class TokenState:
    """Anonymous session state aligned with Android TV APK."""

    device_id: str = ""
    session_id: str = ""
    auth_token: str = AUTH_TOKEN
    site_id: str = SITE_ID
    app_id: str = APP_ID
    api_version: str = API_VERSION
    user_name: str = "Guest"
    user_email: str = "Guest@gmail.com"
    uid: int = 0
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    def is_valid(self) -> bool:
        """Whether the session has a device_id and is within its expiration window."""
        return bool(self.device_id and self.session_id and time.time() < (self.expires_at - 60))

    def is_refreshable(self) -> bool:
        """Whether we can refresh this session without re-generating device_id."""
        return bool(self.device_id)

    def to_cache(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "session_id": self.session_id,
            "auth_token": self.auth_token,
            "site_id": self.site_id,
            "app_id": self.app_id,
            "api_version": self.api_version,
            "user_name": self.user_name,
            "user_email": self.user_email,
            "uid": self.uid,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_cache(cls, data: dict[str, Any] | None) -> TokenState:
        if not isinstance(data, dict):
            return cls()
        return cls(
            device_id=str(data.get("device_id") or ""),
            session_id=str(data.get("session_id") or ""),
            auth_token=str(data.get("auth_token") or AUTH_TOKEN),
            site_id=str(data.get("site_id") or SITE_ID),
            app_id=str(data.get("app_id") or APP_ID),
            api_version=str(data.get("api_version") or API_VERSION),
            user_name=str(data.get("user_name") or "Guest"),
            user_email=str(data.get("user_email") or "Guest@gmail.com"),
            uid=int(data.get("uid") or 0),
            created_at=float(data.get("created_at") or time.time()),
            expires_at=float(data.get("expires_at") or 0.0),
        )

    @classmethod
    def mint_anonymous(cls, device_id: str | None = None) -> TokenState:
        now = time.time()
        dev_id = device_id or uuid.uuid4().hex[:16]
        sess_id = f"{AUTH_TOKEN}_{SITE_ID}_{dev_id}_{int(now)}"
        return cls(
            device_id=dev_id,
            session_id=sess_id,
            auth_token=AUTH_TOKEN,
            site_id=SITE_ID,
            app_id=APP_ID,
            api_version=API_VERSION,
            user_name="Guest",
            user_email="Guest@gmail.com",
            uid=0,
            created_at=now,
            expires_at=now + DEFAULT_TTL,
        )

    def refresh(self) -> None:
        now = time.time()
        if not self.device_id:
            self.device_id = uuid.uuid4().hex[:16]
        self.session_id = f"{self.auth_token}_{self.site_id}_{self.device_id}_{int(now)}"
        self.created_at = now
        self.expires_at = now + DEFAULT_TTL


@dataclass
class TitleItem:
    """A VOD item (movie or episode), or TV show series."""

    id: str
    node_id: str
    title: str
    kind: str  # "movie", "series", "season", "episode", "live"
    description: str = ""
    video_url: str = ""
    is_drm: bool = False
    drm_type: str = "widevine"
    year: int | None = None
    duration: int | None = None  # in seconds
    season_number: int | None = None
    episode_number: int | None = None
    series_name: str = ""
    episode_title: str = ""
    genres: list[str] = field(default_factory=list)
    rating: str = ""
    subtitles: list[dict[str, str]] = field(default_factory=list)
    intro_st: int = 0
    intro_et: int = 0
    endcredit_st: int = 0
    endcredit_et: int = 0
    images: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_show(self) -> bool:
        return self.kind == "series"

    @property
    def is_playable(self) -> bool:
        return self.kind in {"movie", "episode", "live"}

    @property
    def label(self) -> str:
        if self.kind == "episode":
            s_num = f"S{self.season_number:02d}" if self.season_number else ""
            e_num = f"E{self.episode_number:02d}" if self.episode_number else ""
            prefix = f"{s_num}{e_num}".strip()
            name = self.episode_title or self.title
            return f"{prefix} · {name}" if prefix else name
        if self.kind == "series":
            return f"{self.title} [Series]"
        if self.year:
            return f"{self.title} ({self.year})"
        return self.title

    @property
    def detail(self) -> str:
        parts: list[str] = []
        if self.kind == "episode":
            if self.series_name:
                parts.append(self.series_name)
        elif self.kind == "series":
            parts.append("TV Series")
        else:
            parts.append("Movie")

        if self.year:
            parts.append(str(self.year))
        if self.duration:
            mins = self.duration // 60
            parts.append(f"{mins}m")
        if self.genres:
            parts.append(", ".join(self.genres[:2]))
        if self.rating:
            parts.append(self.rating)
        if self.is_drm:
            parts.append("DRM")
        return " · ".join(parts)


@dataclass
class SeasonItem:
    """A TV show season holding its episodes."""

    number: int
    title: str
    feed_url: str
    description: str = ""
    image_url: str = ""
    episodes: list[TitleItem] = field(default_factory=list)


@dataclass
class LiveChannel:
    """A FAST live TV channel."""

    id: str
    title: str
    stream_url: str
    epg_url: str = ""
    category: str = ""
    images: dict[str, str] = field(default_factory=dict)
    current_program: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.title

    @property
    def detail(self) -> str:
        parts = ["Live FAST Channel"]
        if self.category:
            parts.append(self.category)
        if self.current_program:
            parts.append(f"Now: {self.current_program}")
        return " · ".join(parts)


@dataclass
class SectionItem:
    """A top-level section from index.php (e.g. Home, TV, Movies, Live)."""

    id: str
    title: str
    url: str
    feed_type: str = "category"
    is_live: bool = False


@dataclass
class ShelfItem:
    """A shelf/row from sub-categories.php (e.g. Featured Movies & TV, Trending, etc.)."""

    id: str
    title: str
    url: str
    row_type: str = ""
    feed_type: str = ""
    is_live: bool = False


class FilmRiseApi:
    """Client for FilmRise backend APIs."""

    def __init__(
        self,
        session: requests.Session | None = None,
        state: TokenState | None = None,
        on_update: Callable[[TokenState], None] | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
            }
        )
        self.state = state or TokenState()
        self.on_update = on_update

    # ------------------------------------------------------------------ session

    def ensure_session(self) -> TokenState:
        """Ensure an active anonymous session exists, refreshing if expired."""
        if self.state.is_valid():
            return self.state

        if self.state.is_refreshable():
            return self.refresh_session()

        # Mint completely fresh anonymous session
        self.state = TokenState.mint_anonymous()
        # Validate connectivity by pinging index.php
        try:
            self._get_json(INDEX_URL)
        except Exception as exc:
            logger.debug("FilmRise index check on mint failed: %s", exc)

        if self.on_update:
            self.on_update(self.state)
        return self.state

    def refresh_session(self) -> TokenState:
        """Refresh current anonymous session token and save."""
        self.state.refresh()
        try:
            self._get_json(INDEX_URL)
        except Exception as exc:
            logger.debug("FilmRise index check on refresh failed: %s", exc)

        if self.on_update:
            self.on_update(self.state)
        return self.state

    def get_device_id(self) -> str:
        if not self.state.device_id:
            self.ensure_session()
        return self.state.device_id

    def get_session_id(self) -> str:
        if not self.state.session_id:
            self.ensure_session()
        return self.state.session_id

    def get_drm_license_url(self) -> str:
        return DRM_LICENSE_URL_TEMPLATE.format(device_id=self.get_device_id())

    # ---------------------------------------------------------------- HTTP base

    def _default_params(self) -> dict[str, str]:
        return {
            "appId": APP_ID,
            "siteId": SITE_ID,
            "auth-token": AUTH_TOKEN,
            "version": API_VERSION,
            "deviceId": self.get_device_id(),
            "session-id": self.get_session_id(),
        }

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        full_params = self._default_params()
        if params:
            full_params.update(params)

        try:
            resp = self.session.get(url, params=full_params, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise FilmRiseError(f"HTTP request to {url} failed: {exc}") from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise FilmRiseError(f"Invalid JSON returned from {url}: {exc}") from exc

    # ----------------------------------------------------------- Native Search

    def search(self, query: str, max_results: int = 50) -> list[TitleItem]:
        """Perform native search using recipes.php?searchType=search.

        Notice: Uses FilmRise's own native search API, strictly decoupled from
        Core's JustWatch search.
        """
        query_str = str(query or "").strip()
        if not query_str:
            return []

        params = {
            "searchType": "search",
            "keys": query_str,
            "max-results": str(max_results),
        }
        data = self._get_json(RECIPES_URL, params=params)
        results = data.get("results", []) if isinstance(data, dict) else []
        items: list[TitleItem] = []
        for raw in results:
            item = self._parse_recipe_item(raw)
            if item:
                items.append(item)
        return items

    # --------------------------------------------------- Browse Home Navigation

    def get_sections(self) -> list[SectionItem]:
        """Fetch top-level sections from index.php (Home, TV, Movies, Live)."""
        data = self._get_json(INDEX_URL)
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise FilmRiseError("Failed to fetch FilmRise index categories")

        channels = data.get("channels", [])
        sections: list[SectionItem] = []
        for ch in channels:
            if not isinstance(ch, dict):
                continue
            name = ch.get("name") or ch.get("title") or ""
            # Exclude login and search actions from browse section list
            if name.lower() in {"login", "search", "about"}:
                continue
            url = ch.get("url") or ""
            feed_type = ch.get("feed_type") or "category"
            is_live = name.lower() == "live" or "searchtype=live" in url.lower()
            sections.append(
                SectionItem(
                    id=str(ch.get("category_id") or name.lower()),
                    title=name,
                    url=url,
                    feed_type=feed_type,
                    is_live=is_live,
                )
            )
        return sections

    def get_shelves(self, section_name: str = "Home", section_url: str = "") -> list[ShelfItem]:
        """Fetch rows/shelves for a section from sub-categories.php."""
        target_url = section_url or SUBCAT_URL
        params: dict[str, str] = {}
        if not section_url:
            params = {
                "parent": section_name,
                "default-cat": "Home",
                "country": "US",
            }

        data = self._get_json(target_url, params=params)
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise FilmRiseError(f"Failed to fetch FilmRise shelves for section {section_name}")

        channels = data.get("channels", [])
        shelves: list[ShelfItem] = []
        for ch in channels:
            if not isinstance(ch, dict):
                continue
            title = ch.get("name") or ch.get("title") or ""
            url = ch.get("url") or ""
            row_type = ch.get("row_type") or ""
            feed_type = ch.get("feed_type") or ""
            # Filter out external child-app links that don't host FilmRise videos
            if "promoapps.php" in url:
                continue
            is_live = row_type == "live" or "searchtype=live" in url.lower() or title.lower() == "live"
            shelves.append(
                ShelfItem(
                    id=str(ch.get("category_id") or title),
                    title=title,
                    url=url,
                    row_type=row_type,
                    feed_type=feed_type,
                    is_live=is_live,
                )
            )
        return shelves

    def get_shelf_items(self, shelf_url: str) -> list[TitleItem | LiveChannel]:
        """Fetch content items (TitleItems or LiveChannels) from a shelf's feed URL."""
        if not shelf_url:
            return []

        data = self._get_json(shelf_url)
        if not isinstance(data, dict) or data.get("status") != "ok":
            return []

        # If the shelf contains TV shows as a listoflist channel mapping
        if "channels" in data and isinstance(data["channels"], dict):
            # Show container format
            items: list[TitleItem | LiveChannel] = []
            for k, s_val in data["channels"].items():
                if isinstance(s_val, dict):
                    title = s_val.get("title") or f"Show {k}"
                    feed = s_val.get("feed") or ""
                    items.append(
                        TitleItem(
                            id=str(k),
                            node_id=str(k),
                            title=title,
                            kind="series",
                            video_url=feed,
                        )
                    )
            return items

        results = data.get("results", [])
        if not isinstance(results, list):
            return []

        is_live_shelf = "searchtype=live" in shelf_url.lower()

        shelf_items: list[TitleItem | LiveChannel] = []
        for raw in results:
            if not isinstance(raw, dict):
                continue
            if is_live_shelf or raw.get("feed_type") == "live" or raw.get("epg_url"):
                ch = self._parse_live_channel(raw)
                if ch:
                    shelf_items.append(ch)
            else:
                it = self._parse_recipe_item(raw)
                if it:
                    shelf_items.append(it)
        return shelf_items

    # ----------------------------------------------------------- Live Channels

    def get_live_channels(self) -> list[LiveChannel]:
        """Fetch active FAST live channels from FilmRise lineup."""
        # 1. First attempt: fetch from Live section sub-categories
        try:
            live_shelves = self.get_shelves("Live")
            for shelf in live_shelves:
                if shelf.is_live and shelf.url:
                    items = self.get_shelf_items(shelf.url)
                    channels = [it for it in items if isinstance(it, LiveChannel)]
                    if channels:
                        return channels
        except Exception as exc:
            logger.debug("Failed to fetch live channels via Live shelf: %s", exc)

        # 2. Second attempt: fetch from Home section's Live TV row
        try:
            home_shelves = self.get_shelves("Home")
            for shelf in home_shelves:
                if shelf.is_live and shelf.url:
                    items = self.get_shelf_items(shelf.url)
                    channels = [it for it in items if isinstance(it, LiveChannel)]
                    if channels:
                        return channels
        except Exception as exc:
            logger.debug("Failed to fetch live channels via Home Live shelf: %s", exc)

        # 3. Fallback direct live searchType call
        params = {
            "searchType": "live",
            "keys": "110374",
            "sort_type": "",
            "country": "US",
        }
        try:
            data = self._get_json(RECIPES_URL, params=params)
            results = data.get("results", []) if isinstance(data, dict) else []
            channels: list[LiveChannel] = []
            for raw in results:
                ch = self._parse_live_channel(raw)
                if ch:
                    channels.append(ch)
            return channels
        except Exception as exc:
            logger.debug("Failed direct live query: %s", exc)
            return []

    def _parse_live_channel(self, raw: dict[str, Any]) -> LiveChannel | None:
        title = raw.get("title") or raw.get("name") or ""
        stream_url = raw.get("video_url") or raw.get("url") or ""
        cid = str(raw.get("id") or raw.get("node_id") or "")
        if not title or not stream_url:
            return None

        images: dict[str, str] = {}
        for k, v in (
            ("main_picture", "poster"),
            ("picture", "thumbnail"),
            ("bg_image_landscape", "banner"),
            ("squareIcon", "icon"),
        ):
            if raw.get(k):
                images[v] = str(raw[k])

        current_prog = ""
        epg_url = raw.get("epg_url") or ""
        if epg_url:
            try:
                epg_events = self.get_epg_now_playing(epg_url)
                if epg_events and isinstance(epg_events, list):
                    first_event = epg_events[0]
                    if isinstance(first_event, dict):
                        current_prog = first_event.get("title") or first_event.get("name") or ""
            except Exception:
                pass

        return LiveChannel(
            id=cid,
            title=title,
            stream_url=stream_url,
            epg_url=epg_url,
            category=str(raw.get("primary_genre") or raw.get("content_genre") or ""),
            images=images,
            current_program=current_prog,
            raw=raw,
        )

    def get_epg_now_playing(self, epg_url: str) -> list[dict[str, Any]]:
        """Fetch EPG schedule items for a live channel."""
        if not epg_url:
            return []
        try:
            data = self._get_json(epg_url)
            return data.get("epg", []) if isinstance(data, dict) else []
        except Exception as exc:
            logger.debug("Failed to fetch EPG from %s: %s", epg_url, exc)
            return []

    # ------------------------------------------------------------- Shows & VODs

    def get_show(self, show_id: str) -> tuple[TitleItem, list[SeasonItem]]:
        """Fetch TV show details and its list of seasons."""
        trimmed = trim_id(show_id)
        params = {
            "searchType": "listoflist",
            "keys": trimmed,
            "session-id": "",
            "sort_type": "",
        }
        data = self._get_json(SHOWS_URL, params=params)
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise FilmRiseError(f"Failed to find TV show with ID {show_id}")

        channels = data.get("channels", {})
        if not isinstance(channels, dict) or not channels:
            raise FilmRiseError(f"Show {show_id} contains no season channels")

        first_season_data = next(iter(channels.values()), {})
        series_title = first_season_data.get("title") or f"Show {show_id}"
        clean_series_title = re.sub(r"\s+S\d+$", "", series_title, flags=re.IGNORECASE)

        series_year = None
        for y_key in ("release_year", "year"):
            if first_season_data.get(y_key):
                try:
                    series_year = int(first_season_data[y_key])
                    break
                except (ValueError, TypeError):
                    pass

        series_item = TitleItem(
            id=show_id,
            node_id=trimmed,
            title=clean_series_title,
            kind="series",
            year=series_year,
            description=first_season_data.get("description") or "",
            images={
                "thumbnail": first_season_data.get("sd_image") or "",
                "poster": first_season_data.get("hd_image") or "",
            },
            raw=data,
        )

        seasons: list[SeasonItem] = []
        for key, s_val in channels.items():
            if not isinstance(s_val, dict):
                continue
            try:
                s_num = int(key)
            except ValueError:
                match = re.search(r"\d+", key)
                s_num = int(match.group()) if match else len(seasons) + 1

            s_title = s_val.get("title") or f"Season {s_num}"
            feed_url = s_val.get("feed") or ""
            seasons.append(
                SeasonItem(
                    number=s_num,
                    title=s_title,
                    feed_url=feed_url,
                    description=s_val.get("description") or "",
                    image_url=s_val.get("hd_image") or s_val.get("sd_image") or "",
                )
            )

        seasons.sort(key=lambda s: s.number)
        return series_item, seasons

    def get_season_episodes(self, feed_url: str) -> list[TitleItem]:
        """Fetch episodes for a specific season feed URL."""
        if not feed_url:
            return []
        data = self._get_json(feed_url)
        results = data.get("results", []) if isinstance(data, dict) else []
        episodes: list[TitleItem] = []
        for raw in results:
            ep = self._parse_recipe_item(raw, default_kind="episode")
            if ep:
                episodes.append(ep)
        return episodes

    def get_video(self, video_id: str, title_hint: str = "") -> TitleItem:
        """Fetch video information by node ID using recipeInfo.php or search."""
        trimmed = trim_id(video_id)

        # 1. If title_hint provided, try searching it first to get rich metadata
        if title_hint:
            try:
                for it in self.search(title_hint, max_results=10):
                    if trim_id(it.id) == trimmed or trim_id(it.node_id) == trimmed:
                        return it
            except Exception:
                pass

        # 2. Try recipeInfo.php?searchType=nid&nid=...
        data = self._get_json(RECIPE_INFO_URL, params={"searchType": "nid", "nid": trimmed})
        if isinstance(data, dict) and data.get("status") == "ok":
            node = data.get("node_data") or {}
            if isinstance(node, dict) and node.get("video_url"):
                node_title = node.get("title") or ""
                if node_title:
                    clean_title = re.sub(r"\(.*?\)", "", node_title).strip()
                    try:
                        hits = self.search(clean_title, max_results=10)
                        for hit in hits:
                            if trim_id(hit.node_id) == trimmed or trim_id(hit.id) == trimmed:
                                return hit
                    except Exception:
                        pass
                return self._parse_node_data(node, original_id=video_id)

        # 3. Try searching by ID in search results
        search_results = self.search(trimmed, max_results=20)
        for it in search_results:
            if trim_id(it.id) == trimmed or trim_id(it.node_id) == trimmed:
                return it

        raise FilmRiseError(f"No video found for ID {video_id}")

    # ------------------------------------------------------------- DRM License

    def widevine_license(self, license_url: str, challenge: bytes) -> bytes:
        """Execute Widevine DRM license acquisition."""
        url = license_url or self.get_drm_license_url()
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/octet-stream",
        }
        try:
            resp = self.session.post(
                url,
                data=challenge,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
            if resp.status_code != 200:
                raise FilmRiseError(
                    f"DRM license request failed HTTP {resp.status_code}: {resp.text[:200]}"
                )
            return resp.content
        except requests.RequestException as exc:
            raise FilmRiseError(f"Widevine license acquisition failed: {exc}") from exc

    # ---------------------------------------------------------- Content Parsers

    def _parse_recipe_item(
        self, raw: dict[str, Any], default_kind: str = "movie"
    ) -> TitleItem | None:
        if not isinstance(raw, dict):
            return None

        raw_id = str(raw.get("id") or "")
        node_id = str(raw.get("node_id") or trim_id(raw_id))
        title = raw.get("title") or ""
        if not raw_id and not node_id:
            return None

        raw_type = str(raw.get("type") or "").lower()
        item_type = str(raw.get("item_type") or "").lower()
        if raw_type == "listoflist" or item_type == "tvshow":
            kind = "series"
        elif default_kind == "episode" or raw.get("season") is not None or raw.get("episode") is not None:
            kind = "episode"
        else:
            kind = "movie"

        video_url = raw.get("video_url") or raw.get("url") or ""
        is_drm = raw.get("drm") == 1 or ".mpd" in video_url

        # Duration & Year
        duration = None
        if raw.get("runtime"):
            try:
                duration = int(raw["runtime"])
            except (ValueError, TypeError):
                pass

        year = None
        for y_key in ("release_year", "year", "field_release_year_value"):
            val = raw.get(y_key)
            if val is not None and str(val).strip():
                try:
                    candidate = int(str(val).strip())
                    if 1888 <= candidate <= 2100:
                        year = candidate
                        break
                except (ValueError, TypeError):
                    pass

        if year is None and title:
            match = re.search(r"\b(19\d\d|20\d\d)\b", title)
            if match:
                try:
                    candidate = int(match.group(1))
                    if 1888 <= candidate <= 2100:
                        year = candidate
                except (ValueError, TypeError):
                    pass

        # Season and episode
        season_num = None
        if raw.get("season") is not None:
            try:
                season_num = int(raw["season"])
            except (ValueError, TypeError):
                pass

        episode_num = None
        if raw.get("episode") is not None:
            try:
                episode_num = int(raw["episode"])
            except (ValueError, TypeError):
                pass

        # Subtitles
        subtitles: list[dict[str, str]] = []
        if raw.get("cc_path"):
            subtitles.append({"language": "English", "file_path": str(raw["cc_path"]), "name": "English"})
        if isinstance(raw.get("cc_path_multi_lang"), list):
            for sub in raw["cc_path_multi_lang"]:
                if isinstance(sub, dict) and sub.get("file_path"):
                    lang_name = sub.get("language") or "Subtitle"
                    subtitles.append({
                        "language": lang_name,
                        "file_path": str(sub["file_path"]),
                        "name": lang_name,
                    })

        # Chapters (seconds)
        intro_st = int(raw.get("intro_st") or 0)
        intro_et = int(raw.get("intro_et") or 0)
        endcredit_st = int(raw.get("endcredit_st") or 0)
        endcredit_et = int(raw.get("endcredit_et") or 0)

        # Images
        images: dict[str, str] = {}
        for k, v in (
            ("hero_image", "hero"),
            ("bg_image_landscape", "banner"),
            ("bg_image_portrait", "poster"),
            ("main_picture", "thumbnail"),
            ("picture", "sd_thumbnail"),
            ("squareIcon", "icon"),
        ):
            if raw.get(k):
                images[v] = str(raw[k])

        genres: list[str] = []
        if raw.get("primary_genre"):
            genres.append(str(raw["primary_genre"]))
        elif raw.get("content_genre"):
            genres.extend([g.strip() for g in str(raw["content_genre"]).split(",") if g.strip()])

        return TitleItem(
            id=raw_id,
            node_id=node_id,
            title=title,
            kind=kind,
            description=str(raw.get("description") or ""),
            video_url=video_url,
            is_drm=is_drm,
            year=year,
            duration=duration,
            season_number=season_num,
            episode_number=episode_num,
            series_name=str(raw.get("series_name") or ""),
            episode_title=str(raw.get("episode_title") or ""),
            genres=genres,
            rating=str(raw.get("age_appropriate_rating") or ""),
            subtitles=subtitles,
            intro_st=intro_st,
            intro_et=intro_et,
            endcredit_st=endcredit_st,
            endcredit_et=endcredit_et,
            images=images,
            raw=raw,
        )

    def _parse_node_data(self, node: dict[str, Any], original_id: str = "") -> TitleItem:
        title = node.get("title") or ""
        video_id = str(node.get("video_id") or trim_id(original_id))
        video_url = node.get("video_url") or ""
        desc = node.get("field_recipe_description_value") or ""
        if isinstance(desc, str):
            desc = re.sub(r'\{.*?"class":"media-element file-wysiwyg"\}', "", desc).strip()

        year = None
        for y_key in ("release_year", "year", "field_release_year_value"):
            val = node.get(y_key)
            if val is not None and str(val).strip():
                try:
                    candidate = int(str(val).strip())
                    if 1888 <= candidate <= 2100:
                        year = candidate
                        break
                except (ValueError, TypeError):
                    pass

        if year is None and title:
            match = re.search(r"\b(19\d\d|20\d\d)\b", title)
            if match:
                try:
                    candidate = int(match.group(1))
                    if 1888 <= candidate <= 2100:
                        year = candidate
                except (ValueError, TypeError):
                    pass

        images: dict[str, str] = {}
        if node.get("picture_url"):
            images["thumbnail"] = node["picture_url"]
        if node.get("main_picture_url"):
            images["poster"] = node["main_picture_url"]

        return TitleItem(
            id=str(node.get("encoded_video_id") or original_id or video_id),
            node_id=video_id,
            title=title,
            kind="movie",
            year=year,
            description=desc,
            video_url=video_url,
            is_drm=".mpd" in video_url,
            images=images,
            raw=node,
        )

    # ------------------------------------------------ Chapters & Attachments

    def parse_chapters(self, item: TitleItem) -> list[Chapter]:
        """Convert intro_st/et and endcredit_st/et (in seconds) to milliseconds Chapters."""
        chapters: list[Chapter] = []
        if item.intro_et > item.intro_st and item.intro_et > 0:
            chapters.append(
                Chapter(
                    start_ms=item.intro_st * 1000,
                    end_ms=item.intro_et * 1000,
                    title="Intro",
                    kind="intro",
                )
            )

        if item.endcredit_st > 0:
            end_ms = item.endcredit_et * 1000 if item.endcredit_et > item.endcredit_st else None
            chapters.append(
                Chapter(
                    start_ms=item.endcredit_st * 1000,
                    end_ms=end_ms,
                    title="Credits",
                    kind="credits",
                )
            )

        return chapters

    def extract_attachments(self, item: TitleItem) -> list[Attachment]:
        """Extract posters, backdrops, and thumbnails as Attachments."""
        attachments: list[Attachment] = []
        seen_urls: set[str] = set()

        mapping = [
            ("poster", "Poster", "poster"),
            ("hero", "Backdrop", "artwork"),
            ("banner", "Banner", "artwork"),
            ("thumbnail", "Thumbnail", "thumbnail"),
            ("icon", "Square Icon", "artwork"),
        ]

        for key, name, kind in mapping:
            url = item.images.get(key)
            if url and url not in seen_urls:
                seen_urls.add(url)
                attachments.append(
                    Attachment(
                        url=url,
                        name=name,
                        kind=kind,
                        mime_type="image/jpeg" if url.endswith((".jpg", ".jpeg")) else "image/png",
                    )
                )

        return attachments

    def extract_subtitles(self, item: TitleItem) -> list[SubtitleReference]:
        """Extract SubtitleReferences from item subtitles list."""
        refs: list[SubtitleReference] = []
        seen: set[str] = set()
        for idx, sub in enumerate(item.subtitles):
            file_url = sub.get("file_path") or ""
            if not file_url or file_url in seen:
                continue
            seen.add(file_url)
            lang_name = sub.get("name") or sub.get("language") or "Subtitle"
            lang_code = normalize_language(lang_name)
            refs.append(
                SubtitleReference(
                    url=file_url,
                    language=lang_code,
                    name=lang_name,
                    selected=(idx == 0),
                )
            )
        return refs


def parse_filmrise_reference(target: str) -> tuple[str, str]:
    """Parse a URL or reference string into (kind, content_id).

    Supported reference formats:
    - filmrise:movie:10758618
    - filmrise:show:82992
    - filmrise:live:1898
    - filmrise:10758618
    - happykids://play?contentID=10758618
    - happykids://play?Tvshowkey=82992
    - https://filmrise.com/movies/10758618
    - https://filmrise.com/shows/82992
    - http://fawesome.ifood.tv/fawesome-topics/10758618-savage-hunt
    - Direct numeric IDs: "10758618", "200000010758618", "82992"
    """
    raw = str(target or "").strip()
    if not raw:
        raise FilmRiseError("Empty FilmRise target reference")

    # 1. Scheme URI: filmrise:kind:id or filmrise:id
    scheme_match = re.match(
        r"^(?:filmrise|film_rise|filmrisetv):(?:(movie|show|series|live|video):)?([0-9]+)$",
        raw,
        re.IGNORECASE,
    )
    if scheme_match:
        k = scheme_match.group(1) or "auto"
        cid = scheme_match.group(2)
        if k in {"show", "series"}:
            return "show", cid
        if k == "video":
            return "movie", cid
        return k.lower(), cid

    # 2. Deep link scheme: happykids://play?...
    tvshow_match = re.search(r"[?&]Tvshowkey=([0-9]+)", raw, re.IGNORECASE)
    if tvshow_match:
        return "show", tvshow_match.group(1)

    deep_match = re.search(r"[?&](?:contentID|keys|nid|id)=([0-9]+)", raw, re.IGNORECASE)
    if deep_match:
        return "auto", deep_match.group(1)

    # 3. Web URLs (filmrise.com or fawesome/ifood.tv links)
    if "filmrise.com" in raw or "fawesome.tv" in raw or "ifood.tv" in raw:
        show_match = re.search(r"/(?:shows|tv-shows|series)/([0-9]+)", raw, re.IGNORECASE)
        if show_match:
            return "show", show_match.group(1)

        live_match = re.search(r"/(?:live|live-tv)/([0-9]+)", raw, re.IGNORECASE)
        if live_match:
            return "live", live_match.group(1)

        movie_match = re.search(r"/(?:movies|movie|fawesome-topics|watch)/([0-9]+)", raw, re.IGNORECASE)
        if movie_match:
            return "movie", movie_match.group(1)

    # 4. Pure numeric ID
    if re.fullmatch(r"[0-9]+", raw):
        return "auto", raw

    raise FilmRiseError(f"Unrecognized FilmRise reference: {target}")
