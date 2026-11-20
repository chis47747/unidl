"""ZDF Mediathek catalogue and clear HLS/DASH playback."""

from __future__ import annotations

import base64
import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter, normalize_chapters
from ...core.playback import SubtitleReference

WWW = "https://www.zdf.de"
GRAPHQL = "https://api.zdf.de/graphql"
PTMD_BASE = "https://api.zdf.de"
TOKEN_ENDPOINT = "https://zdf-prod-futura.zdf.de/mediathekV2/token"
TV_CONFIG = "https://zdf-prod-futura.zdf.de/zdf/mediathek/config/androidtv/6.10/live_6.10.1.json"
TV_IDENTITY = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken"
TV_REFRESH = "https://securetoken.googleapis.com/v1/token"
TV_FIREBASE_HEADERS = {
    "X-Android-Package": "com.zdf.android.mediathek",
    "X-Android-Cert": "AFE8CC7417771CB14362D151D16B8BC0455F5093",
}
APP_ID = "zdf-androidtv-6.10.1"
USER_AGENT = "ngp/com.zdf.android.mediathek/6.10.1"
PLAYER_ID = "android_native_6"

HEADERS = {
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "User-Agent": USER_AGENT,
    "zdf-app-id": APP_ID,
    "zdf-tenant-keys": "zdf",
    "Accept": "application/graphql-response+json, application/json",
    "Content-Type": "application/json",
}

PLAYABLE_VIDEO_QUERY = """
query GetPlayableVideoByCanonical($canonical: String!) {
  videoByCanonical(canonical: $canonical) {
    __typename
    id
    canonical
    title
    subtitle
    sharingUrl
    contentType
    editorialDate
    productionYear
    leadParagraph
    episodeInfo {
      seasonNumber
      episodeNumber
    }
    smartCollection {
      id
      canonical
      title
      collectionType
    }
    teaser {
      title
      description
    }
    structuralMetadata {
      publicationFormMetaCollection {
        title
      }
    }
    currentMediaType
    currentMedia {
      nodes {
        __typename
        id
        ptmdTemplate
        title
        ... on VodMedia {
          vodMediaType
          geoLocation
          duration
        }
        ... on LiveMedia {
          liveMediaType
          state
          geoLocation
          start
          stop
          editorialStart
          editorialStop
        }
      }
    }
  }
}
"""

COLLECTION_QUERY = """
query GetSmartCollectionByCanonical(
  $canonical: String!,
  $videoPageSize: Int,
  $episodesFilter: VideosConnectionFilterByInput = { availableStreamTypeIn: [VOD] }
) {
  smartCollectionByCanonical(canonical: $canonical) {
    __typename
    id
    canonical
    title
    collectionType
    availableStreamTypes
    ... on ISeriesSmartCollection {
      countSeasons
      countEpisodes
    }
    extras(first: $videoPageSize) {
      nodes {
        __typename
        ...videoFragment
      }
      pageInfo {
        endCursor
        hasNextPage
      }
    }
    ... on SeasonSeriesSmartCollection {
      ascSeasons: seasons(
        sortBy: { field: NUMBER, direction: ASC },
        filterBy: { availableStreamTypesIn: [VOD] }
      ) {
        nodes {
          ...seasonAscFragment
        }
      }
    }
    ... on MiniSeriesSmartCollection {
      ascSeasons: seasons(
        sortBy: { field: NUMBER, direction: ASC },
        filterBy: { availableStreamTypesIn: [VOD] }
      ) {
        nodes {
          ...seasonAscFragment
        }
      }
    }
    ... on HybridBingeSeriesSmartCollection {
      ascSeasons: seasons(
        sortBy: { field: NUMBER, direction: DESC },
        filterBy: { availableStreamTypesIn: [VOD] }
      ) {
        nodes {
          ...seasonAscFragment
        }
      }
    }
    ... on EndlessSeriesSmartCollection {
      descSeasons: seasons(
        sortBy: { field: NUMBER, direction: DESC },
        filterBy: { availableStreamTypesIn: [VOD] }
      ) {
        nodes {
          ...seasonDescFragment
        }
      }
    }
    ... on DefaultWithSectionsSmartCollection {
      descSeasons: seasons(
        sortBy: { field: NUMBER, direction: DESC },
        filterBy: { availableStreamTypesIn: [VOD] }
      ) {
        nodes {
          ...seasonDescFragment
        }
      }
      sections(first: $videoPageSize) {
        nodes {
          __typename
          ...videoFragment
        }
        pageInfo {
          endCursor
          hasNextPage
        }
      }
    }
    ... on DefaultNoSectionsSmartCollection {
      descSeasons: seasons(
        sortBy: { field: NUMBER, direction: DESC },
        filterBy: { availableStreamTypesIn: [VOD] }
      ) {
        nodes {
          ...seasonDescFragment
        }
      }
    }
    ... on MovieSmartCollection {
      video {
        __typename
        ...videoFragment
      }
    }
  }
}

fragment seasonAscFragment on Season {
  id
  title
  number
  episodes(
    first: $videoPageSize,
    sortBy: { field: EPISODE_NUMBER, direction: ASC },
    filterBy: $episodesFilter
  ) {
    nodes {
      __typename
      ...videoFragment
    }
    pageInfo {
      endCursor
      hasNextPage
    }
  }
}

fragment seasonDescFragment on Season {
  id
  title
  number
  episodes(
    first: $videoPageSize,
    sortBy: { field: EPISODE_NUMBER, direction: DESC },
    filterBy: $episodesFilter
  ) {
    nodes {
      __typename
      ...videoFragment
    }
    pageInfo {
      endCursor
      hasNextPage
    }
  }
}

fragment videoFragment on Video {
  id
  canonical
  title
  contentType
  editorialDate
  productionYear
  leadParagraph
  episodeInfo {
    seasonNumber
    episodeNumber
  }
  smartCollection {
    id
    canonical
    title
    collectionType
  }
  teaser {
    title
    description
  }
  currentMediaType
  currentMedia {
    nodes {
      __typename
      id
      ptmdTemplate
      title
      ... on VodMedia {
        vodMediaType
        geoLocation
        duration
      }
      ... on LiveMedia {
        liveMediaType
        state
        geoLocation
        start
        stop
        editorialStart
        editorialStop
      }
    }
  }
}
"""

PLAYBACK_METADATA_QUERY = """
query VideoByCanonical($canonical: String!, $chapters: Boolean!, $attachments: Boolean!) {
  videoByCanonical(canonical: $canonical) {
    teaser @include(if: $attachments) {
      imageWithoutLogo {
        layouts {
          original
          dim1140X120 dim1140X240 dim1200X480 dim1280X720 dim1300X650
          dim1440X576 dim1500X300 dim1500X600 dim1500X800 dim1800X720
          dim1900X200 dim1900X400 dim1900X570 dim1920X1080 dim225X400
          dim240X270 dim276X155 dim276X311 dim314X314 dim380X170
          dim384X216 dim405X720 dim640X720 dim760X340 dim768X432
          dim840X140 dim840X280 dim840X360 dim860X344 dim936X520
        }
      }
    }
    external @include(if: $chapters) { streamAnchorSourceUrl }
    currentMedia @include(if: $chapters) {
      nodes {
        id
        ... on VodMedia {
          streamAnchorTags { nodes { anchorOffset anchorLabel } }
          skipIntro { startIntroTimeOffset stopIntroTimeOffset skipButtonLabel }
        }
      }
    }
  }
}
"""

SEARCH_QUERY = """
query SearchDocuments(
  $query: String!,
  $mode: SearchMode,
  $first: Int,
  $after: Cursor,
  $filters: SearchFilters,
  $group: String
) {
  searchDocuments(
    query: $query,
    mode: $mode,
    first: $first,
    after: $after,
    filters: $filters,
    group: $group
  ) {
    results {
      item {
        __typename
        ... on Video {
          id
          canonical
          title
          contentType
          editorialDate
          productionYear
          currentMediaType
          currentMedia {
            nodes {
              __typename
              ... on VodMedia {
                vodMediaType
                duration
              }
              ... on LiveMedia {
                liveMediaType
                state
                start
                stop
                editorialStart
                editorialStop
              }
            }
          }
          smartCollection {
            id
            canonical
            title
            collectionType
          }
          teaser {
            title
            description
          }
        }
        ... on ISmartCollection {
          id
          canonical
          title
          collectionType
          teaser {
            title
            description
          }
          ... on ISeriesSmartCollection {
            countEpisodes
            countSeasons
          }
        }
        ... on MetaCollection {
          id
          canonical
          metaType
          teaser {
            title
            description
          }
        }
        ... on CuratedCollection {
          id
          canonical
          teaser {
            title
            description
          }
        }
      }
    }
    pageInfo {
      endCursor
      hasNextPage
    }
  }
}
"""

PAGE_VIDEO_FRAGMENT = """
fragment pageVideoFragment on Video {
  id canonical title contentType editorialDate productionYear leadParagraph
  episodeInfo { seasonNumber episodeNumber }
  smartCollection { id canonical title collectionType }
  teaser { title description }
}
"""

SEASON_PAGE_QUERY = """
query GetSmartCollectionEpisodes(
  $smartCollectionId: String!,
  $videoPageSize: Int,
  $seasonOffset: Int,
  $episodeAfter: Cursor,
  $seasonsFilter: SeasonsConnectionFilterByInput = { availableStreamTypesIn: [VOD] },
  $episodesFilter: VideosConnectionFilterByInput = { availableStreamTypeIn: [VOD] }
) {
  smartCollectionById(id: $smartCollectionId) {
    ... on SeasonSeriesSmartCollection {
      seasons(offset: $seasonOffset, first: 1, sortBy: { field: NUMBER, direction: ASC }, filterBy: $seasonsFilter) {
        nodes { id ...pageSeasonAsc }
      }
    }
    ... on MiniSeriesSmartCollection {
      seasons(offset: $seasonOffset, first: 1, sortBy: { field: NUMBER, direction: ASC }, filterBy: $seasonsFilter) {
        nodes { id ...pageSeasonAsc }
      }
    }
    ... on HybridBingeSeriesSmartCollection {
      seasons(offset: $seasonOffset, first: 1, sortBy: { field: NUMBER, direction: DESC }, filterBy: $seasonsFilter) {
        nodes { id ...pageSeasonAsc }
      }
    }
    ... on EndlessSeriesSmartCollection {
      seasons(offset: $seasonOffset, first: 1, sortBy: { field: NUMBER, direction: DESC }, filterBy: $seasonsFilter) {
        nodes { id ...pageSeasonDesc }
      }
    }
    ... on DefaultWithSectionsSmartCollection {
      seasons(offset: $seasonOffset, first: 1, sortBy: { field: NUMBER, direction: DESC }, filterBy: $seasonsFilter) {
        nodes { id ...pageSeasonDesc }
      }
    }
    ... on DefaultNoSectionsSmartCollection {
      seasons(offset: $seasonOffset, first: 1, sortBy: { field: NUMBER, direction: DESC }, filterBy: $seasonsFilter) {
        nodes { id ...pageSeasonDesc }
      }
    }
  }
}
fragment pageSeasonAsc on Season {
  episodes(first: $videoPageSize, after: $episodeAfter, sortBy: { field: EPISODE_NUMBER, direction: ASC }, filterBy: $episodesFilter) {
    nodes { ...pageVideoFragment }
    pageInfo { endCursor hasNextPage }
  }
}
fragment pageSeasonDesc on Season {
  episodes(first: $videoPageSize, after: $episodeAfter, sortBy: { field: EPISODE_NUMBER, direction: DESC }, filterBy: $episodesFilter) {
    nodes { ...pageVideoFragment }
    pageInfo { endCursor hasNextPage }
  }
}
""" + PAGE_VIDEO_FRAGMENT

EXTRAS_PAGE_QUERY = """
query GetSmartCollectionExtras($smartCollectionId: String!, $videoPageSize: Int, $extraAfter: Cursor) {
  smartCollectionById(id: $smartCollectionId) {
    extras(first: $videoPageSize, after: $extraAfter) {
      nodes { ...pageVideoFragment }
      pageInfo { endCursor hasNextPage }
    }
  }
}
""" + PAGE_VIDEO_FRAGMENT

SECTIONS_PAGE_QUERY = """
query GetSmartCollectionSections($smartCollectionId: String!, $videoPageSize: Int, $sectionAfter: Cursor) {
  smartCollectionById(id: $smartCollectionId) {
    ... on DefaultWithSectionsSmartCollection {
      sections(first: $videoPageSize, after: $sectionAfter) {
        nodes { ...pageVideoFragment }
        pageInfo { endCursor hasNextPage }
      }
    }
  }
}
""" + PAGE_VIDEO_FRAGMENT

LIVE_QUERY = """
query GetAllLiveVideos {
  videos(
    filterBy: { availableStreamTypeIn: [LIVE] },
    sortBy: [
      { field: EDITORIAL_DATE, direction: DESC },
      { field: TITLE, direction: ASC }
    ]
  ) {
    nodes {
      __typename
      id
      canonical
      title
      contentType
      smartCollection {
        id
        canonical
        title
      }
      currentMedia {
        nodes {
          __typename
          ... on LiveMedia {
            liveMediaType
            state
            start
            editorialStart
            stop
            editorialStop
          }
        }
      }
    }
  }
}
"""

_CANONICAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_CANONICAL_WITH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*-\d+$")
_QUALITY_RANK = {
    "auto": 0,
    "uhd": 1,
    "fhd": 2,
    "hd": 3,
    "veryhigh": 4,
    "high": 5,
    "med": 6,
    "low": 7,
}


class ZdfError(RuntimeError):
    """ZDF rejected a request or returned an unusable response."""


@dataclass(frozen=True)
class TvAuth:
    id_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires: int
    api_key: str

    def to_cache(self) -> dict[str, Any]:
        return {
            "id_token": self.id_token,
            "refresh_token": self.refresh_token,
            "expires": self.expires,
            "api_key": self.api_key,
        }


@dataclass
class DeviceCode:
    user_code: str = field(repr=False)
    verification_uri: str = field(repr=False)
    token_uri: str = field(repr=False)
    api_key: str
    expires_in: int = 180
    interval: int = 5
    expires_at: float = 0.0
    next_poll_at: float = 0.0


@dataclass(frozen=True)
class DevicePollFailure:
    message: str


def tv_auth_from_cache(state: Any) -> TvAuth | None:
    if not isinstance(state, dict):
        return None
    token = state.get("id_token")
    refresh = state.get("refresh_token")
    key = state.get("api_key")
    expires = _int(state.get("expires"))
    if (
        not isinstance(token, str) or not token or any(c.isspace() for c in token)
        or not isinstance(refresh, str) or not refresh or any(c.isspace() for c in refresh)
        or not isinstance(key, str) or not re.fullmatch(r"AIza[A-Za-z0-9_-]{35}", key)
        or expires is None
    ):
        return None
    return TvAuth(token, refresh, expires, key)


def _tv_url(value: Any, host: str) -> bool:
    if not _is_http_url(value):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.netloc.lower() == host and not parsed.fragment


@dataclass(frozen=True)
class ParsedInput:
    canonical: str


def cache_is_valid(state: dict[str, Any] | None, *, now: float | None = None) -> bool:
    if not isinstance(state, dict) or not str(state.get("token") or ""):
        return False
    expires = _int(state.get("expires")) or 0
    current = int(time.time() if now is None else now)
    return expires > current + 120


def _is_zdf_host(host: str) -> bool:
    normalized = str(host or "").lower().rstrip(".")
    return normalized == "zdf.de" or normalized.endswith(".zdf.de")


def parse_input(text: str) -> ParsedInput | None:
    """Extract a ZDF canonical from an HTTP(S) zdf.de URL or bare id."""
    raw = (text or "").strip()
    if not raw:
        return None

    is_url = "://" in raw or raw.startswith("//") or "/" in raw
    if is_url:
        parsed = urlparse(raw if "://" in raw or raw.startswith("//") else f"https://{raw}")
        if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if not _is_zdf_host(parsed.hostname or ""):
            return None
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if not parts:
            return None
        canonical = re.sub(r"\.html?$", "", parts[-1], flags=re.I)
        return ParsedInput(canonical) if _CANONICAL_RE.fullmatch(canonical) else None

    # Bare canonical (with or without trailing id digits).
    if _CANONICAL_WITH_ID_RE.fullmatch(raw) or _CANONICAL_RE.fullmatch(raw):
        return ParsedInput(raw)
    return None


@dataclass(frozen=True)
class SearchHit:
    id: str
    canonical: str
    title: str
    kind: str  # video | collection | meta | curated | other
    content_type: str = ""
    synopsis: str = ""
    collection_type: str = ""
    year: str = ""
    seasons: int | None = None
    episodes: int | None = None

    @property
    def url(self) -> str:
        return f"{WWW}/{self.canonical}.html" if self.canonical else WWW

    @property
    def label(self) -> str:
        return self.title or self.canonical or self.id

    @property
    def detail(self) -> str:
        bits: list[str] = []
        if self.kind == "video" and self.content_type:
            bits.append(self.content_type)
        elif self.collection_type:
            bits.append(self.collection_type)
        elif self.kind != "video":
            bits.append(self.kind)
        if self.year:
            bits.append(self.year)
        if self.seasons is not None:
            bits.append(f"{self.seasons} seasons")
        if self.episodes is not None:
            bits.append(f"{self.episodes} episodes")
        if self.synopsis:
            bits.append(self.synopsis[:80])
        return " · ".join(bits)


@dataclass(frozen=True)
class SearchPage:
    hits: tuple[SearchHit, ...]
    end_cursor: str = ""
    has_next_page: bool = False


@dataclass(frozen=True)
class Video:
    id: str
    canonical: str
    title: str
    content_type: str = ""
    year: str = ""
    synopsis: str = ""
    season: int | None = None
    episode: int | None = None
    series_title: str = ""
    series_canonical: str = ""
    is_movie: bool = False
    is_live: bool = False
    media_id: str = ""
    ptmd_template: str = ""
    media_type: str = ""
    duration: float | None = None
    geo: str = ""
    state: str = ""

    @property
    def url(self) -> str:
        return f"{WWW}/{self.canonical}.html" if self.canonical else WWW

    @property
    def label(self) -> str:
        if not self.is_movie and not self.is_live and self.season is not None and self.episode is not None:
            return f"S{self.season:02d}E{self.episode:02d}  {self.title}"
        if self.year and self.is_movie:
            return f"{self.title} ({self.year})"
        if self.state:
            return f"{self.title}  [{self.state}]"
        return self.title or self.canonical or self.id


@dataclass
class Season:
    id: str
    title: str
    number: int | None = None
    episodes: list[Video] = field(default_factory=list)
    episodes_end_cursor: str = ""
    episodes_has_next_page: bool = False

    @property
    def label(self) -> str:
        name = self.title or (f"Season {self.number}" if self.number is not None else "Episodes")
        return f"{name}  ·  {len(self.episodes)} episode(s)" if self.episodes else name


@dataclass
class Collection:
    id: str
    canonical: str
    title: str
    collection_type: str = ""
    seasons: list[Season] = field(default_factory=list)
    sections: list[Video] = field(default_factory=list)
    extras: list[Video] = field(default_factory=list)
    movie: Video | None = None
    count_seasons: int | None = None
    count_episodes: int | None = None
    sections_end_cursor: str = ""
    sections_has_next_page: bool = False
    extras_end_cursor: str = ""
    extras_has_next_page: bool = False

    @property
    def url(self) -> str:
        return f"{WWW}/{self.canonical}.html" if self.canonical else WWW


@dataclass(frozen=True)
class Channel:
    id: str
    canonical: str
    title: str
    content_type: str = "LIVE"
    state: str = ""
    series_title: str = ""

    @property
    def url(self) -> str:
        return f"{WWW}/{self.canonical}.html" if self.canonical else WWW

    @property
    def label(self) -> str:
        return self.title or self.canonical or self.id


@dataclass(frozen=True)
class Source:
    url: str
    mime_type: str = ""
    quality: str = ""
    format_type: str = ""
    ptmd_url: str = ""
    player_id: str = PLAYER_ID
    resolution: int = 0
    track_class: str = ""
    subtitles: tuple[SubtitleReference, ...] = ()

    def line(self) -> str:
        bits: list[str] = []
        protocol = _protocol(self.mime_type, self.url)
        if protocol:
            bits.append(protocol)
        if self.quality:
            bits.append(self.quality)
        if self.resolution:
            bits.append(f"{self.resolution}p")
        bits.append("clear")
        return " · ".join(bits)


@dataclass(frozen=True)
class PlaybackMetadata:
    chapters: tuple[Chapter, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    warnings: tuple[str, ...] = ()


class ZdfApi:
    def __init__(
        self,
        session: requests.Session | None = None,
        state: dict[str, Any] | None = None,
        *,
        session_factory: Any = None,
        on_save: Any = None,
        on_clear: Any = None,
        on_tv_save: Any = None,
        on_tv_clear: Any = None,
    ):
        self._session_factory = session_factory or requests.Session
        self._on_save = on_save
        self._on_clear = on_clear
        self._on_tv_save = on_tv_save
        self._on_tv_clear = on_tv_clear
        self.tv_auth: TvAuth | None = None
        self.tv_login_required = False
        self.session = session if session is not None else self._session_factory()
        for key, value in HEADERS.items():
            self.session.headers.setdefault(key, value)
        self.api_token = ""
        self.token_expires = 0
        self.token_type = "Bearer"
        if state:
            self.adopt(state)

    def adopt(self, state: dict[str, Any] | None) -> bool:
        if not cache_is_valid(state):
            return False
        assert state is not None
        self.api_token = str(state.get("token") or "")
        self.token_expires = _int(state.get("expires")) or 0
        self.token_type = str(state.get("type") or "Bearer")
        self.session.headers["Api-Auth"] = f"{self.token_type} {self.api_token}"
        return True

    def to_cache(self) -> dict[str, Any]:
        return {
            "token": self.api_token,
            "type": self.token_type,
            "expires": self.token_expires,
        }

    def clear_token(self) -> None:
        had_token = bool(self.api_token or self.session.headers.get("Api-Auth"))
        self.api_token = ""
        self.token_expires = 0
        self.token_type = "Bearer"
        self.session.headers.pop("Api-Auth", None)
        if had_token and self._on_clear is not None:
            self._on_clear()

    def authenticate(self) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.session.get(
                    TOKEN_ENDPOINT,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    timeout=20,
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ZdfError("ZDF token endpoint returned an unexpected shape")
                token = data.get("token")
                if not token:
                    raise ZdfError("ZDF token endpoint returned no token")
                self.api_token = str(token)
                self.token_type = str(data.get("type") or "Bearer")
                self.token_expires = _int(data.get("expires")) or int(time.time() + 3600)
                self.session.headers["Api-Auth"] = f"{self.token_type} {self.api_token}"
                state = self.to_cache()
                if self._on_save is not None:
                    self._on_save(state)
                return state
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.SSLError,
                requests.exceptions.Timeout,
            ) as exc:
                last_error = exc
                self._reset_session()
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
            except (requests.RequestException, ValueError, TypeError) as exc:
                raise ZdfError(f"ZDF anonymous token failed: {exc}") from exc
        raise ZdfError(f"ZDF anonymous token failed: {last_error}")

    def ensure_token(self) -> None:
        if not self.api_token or time.time() > self.token_expires - 120:
            self.clear_token()
            self.authenticate()

    def set_tv_auth(self, auth: TvAuth, *, save: bool = False) -> None:
        if save and self._on_tv_save is not None:
            self._on_tv_save(auth.to_cache())
        self.tv_auth = auth
        self.tv_login_required = False

    def clear_tv_auth(self) -> None:
        if self._on_tv_clear is not None:
            self._on_tv_clear()
        self.tv_auth = None
        self.tv_login_required = True

    def _tv_request(self, method: str, url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        session = self._session_factory()
        for header in ("Api-Auth", "Authorization", "X-Authorization"):
            session.headers.pop(header, None)
        session.proxies.update(dict(getattr(self.session, "proxies", {}) or {}))
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if url in {TV_IDENTITY, TV_REFRESH}:
            headers.update(TV_FIREBASE_HEADERS)
        try:
            with session.request(
                method, url, headers=headers,
                timeout=20, allow_redirects=False, **kwargs,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise ZdfError("ZDF TV sign-in endpoint attempted a redirect")
                try:
                    data = response.json()
                except ValueError as exc:
                    raise ZdfError("ZDF TV sign-in response was not JSON") from exc
                if not isinstance(data, dict):
                    raise ZdfError("ZDF TV sign-in response was not an object")
                return response.status_code, data
        except requests.RequestException as exc:
            # Token polling URLs and refresh bodies contain credentials.
            raise ZdfError(f"ZDF TV sign-in request failed ({type(exc).__name__})") from exc
        finally:
            session.close()

    def start_device_code(self) -> DeviceCode:
        status, config = self._tv_request("GET", TV_CONFIG)
        urls = config.get("urlConfig")
        streaming = urls.get("streamingOs") if isinstance(urls, dict) else None
        key = streaming.get("apiKey") if isinstance(streaming, dict) else None
        if status != 200 or not isinstance(key, str) or not re.fullmatch(r"AIza[A-Za-z0-9_-]{35}", key):
            raise ZdfError("ZDF TV sign-in configuration has no usable Firebase API key")
        endpoint = streaming.get("deviceSession")
        if not _tv_url(endpoint, "accounts.ard-zdf.de"):
            raise ZdfError("ZDF TV sign-in configuration returned an untrusted device endpoint")
        status, data = self._tv_request("GET", endpoint)
        code = data.get("user_code")
        token_uri = data.get("token_uri")
        verification_uri = data.get("verification_uri")
        expires = _int(data.get("expires_in", 180))
        interval = _int(data.get("interval", 5))
        if status != 200 or not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9-]{3,32}", code):
            raise ZdfError("ZDF TV sign-in returned no usable device code")
        if not _tv_url(token_uri, "accounts.ard-zdf.de") or not _tv_url(verification_uri, "accounts.ard-zdf.de"):
            raise ZdfError("ZDF TV sign-in returned an untrusted confirmation URL")
        if expires is None or expires <= 0 or interval is None or interval <= 0:
            raise ZdfError("ZDF TV sign-in returned invalid code timing")
        expires = min(expires, 600)
        return DeviceCode(
            user_code=code, verification_uri=verification_uri, token_uri=token_uri,
            api_key=key, expires_in=expires, interval=min(max(interval, 2), 60),
            expires_at=time.monotonic() + expires,
        )

    @staticmethod
    def _tv_tokens(data: dict[str, Any], key: str, *, refresh: str = "") -> TvAuth:
        token = data.get("idToken") or data.get("id_token")
        refresh = data.get("refreshToken") or data.get("refresh_token") or refresh
        lifetime = _int(data.get("expiresIn") or data.get("expires_in"))
        expires = int(time.time()) + lifetime if lifetime is not None else None
        if expires is None and isinstance(token, str):
            try:
                payload = token.split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                expires = _int(claims.get("exp")) if isinstance(claims, dict) else None
            except (ValueError, IndexError, UnicodeError):
                pass
        state = tv_auth_from_cache({
            "id_token": token, "refresh_token": refresh, "api_key": key,
            "expires": expires,
        })
        if state is None or state.expires <= time.time():
            raise ZdfError("ZDF TV sign-in returned an incomplete account session")
        return state

    def poll_device_code(self, device: DeviceCode) -> TvAuth | None:
        if not _tv_url(device.token_uri, "accounts.ard-zdf.de"):
            raise ZdfError("Refusing an untrusted ZDF TV token URL")
        now = time.monotonic()
        if now >= device.expires_at:
            raise ZdfError("ZDF TV code expired; request a new code")
        if now < device.next_poll_at:
            return None
        device.next_poll_at = now + device.interval
        status, data = self._tv_request("GET", device.token_uri)
        error = data.get("error")
        if status in {200, 400, 429} and error == "authorization_pending":
            return None
        if status in {400, 429} and error == "slow_down":
            device.interval = min(device.interval + 5, 60)
            device.next_poll_at = now + device.interval
            return None
        if error == "access_denied":
            raise ZdfError("ZDF TV sign-in was declined")
        if error == "expired_token":
            raise ZdfError("ZDF TV code expired; request a new code")
        if status != 200 or error:
            raise ZdfError(f"ZDF TV confirmation failed (HTTP {status})")
        custom = data.get("customToken")
        if isinstance(custom, str) and custom:
            status, data = self._tv_request(
                "POST", TV_IDENTITY, params={"key": device.api_key},
                json={"token": custom, "returnSecureToken": True},
            )
            if status != 200:
                raise ZdfError(f"ZDF TV account token exchange failed (HTTP {status})")
        return self._tv_tokens(data, device.api_key)

    def refresh_tv_token(self) -> None:
        auth = self.tv_auth
        if auth is None:
            return
        status, data = self._tv_request(
            "POST", TV_REFRESH, params={"key": auth.api_key},
            data={"grant_type": "refresh_token", "refresh_token": auth.refresh_token},
        )
        if status != 200:
            if status in {400, 401, 403}:
                self.clear_tv_auth()
                raise ZdfError("ZDF TV session expired; sign in again or choose Anonymous")
            raise ZdfError(f"ZDF TV session refresh failed (HTTP {status})")
        self.set_tv_auth(self._tv_tokens(data, auth.api_key, refresh=auth.refresh_token), save=True)

    def ensure_tv_token(self) -> str:
        if self.tv_login_required:
            raise ZdfError("ZDF TV session expired; sign in again or choose Anonymous")
        if self.tv_auth is None:
            return ""
        if self.tv_auth.expires <= time.time() + 120:
            self.refresh_tv_token()
        return self.tv_auth.id_token if self.tv_auth is not None else ""

    def _reset_session(self) -> None:
        headers = dict(self.session.headers)
        proxies = dict(getattr(self.session, "proxies", {}) or {})
        try:
            self.session.close()
        except Exception:
            pass
        self.session = self._session_factory()
        self.session.headers.update(headers)
        if proxies and hasattr(self.session, "proxies"):
            self.session.proxies.update(proxies)
        for key, value in HEADERS.items():
            self.session.headers.setdefault(key, value)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        token_refreshed = False
        tv_refreshed = False
        supplied_headers = dict(kwargs.pop("headers", {}) or {})
        for attempt in range(3):
            self.ensure_token()
            headers = dict(supplied_headers)
            account_token = self.ensure_tv_token() if _tv_url(url, "api.zdf.de") else ""
            if account_token:
                headers["Authorization"] = f"Bearer {account_token}"
            try:
                response = self.session.request(method, url, timeout=30, headers=headers, **kwargs)
                if response.status_code == 401 and account_token and not tv_refreshed:
                    tv_refreshed = True
                    self.refresh_tv_token()
                    continue
                if response.status_code in (401, 403) and not account_token and not token_refreshed:
                    token_refreshed = True
                    self.clear_token()
                    self.authenticate()
                    continue
                if response.status_code >= 500 and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                response.raise_for_status()
                return response
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.SSLError,
                requests.exceptions.Timeout,
            ) as exc:
                last_error = exc
                self._reset_session()
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise ZdfError(f"ZDF request failed: {exc}") from exc
            except requests.RequestException as exc:
                raise ZdfError(f"ZDF request failed: {exc}") from exc
        raise ZdfError(f"ZDF request failed: {last_error or f'{method} {url}'}")

    def graphql(
        self,
        operation: str,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "operationName": operation,
            "query": query,
            "variables": variables or {},
        }
        response = self.request("POST", GRAPHQL, json=payload)
        try:
            data = response.json()
        except ValueError as exc:
            raise ZdfError(f"ZDF GraphQL returned non-JSON for {operation}") from exc
        if not isinstance(data, dict):
            raise ZdfError(f"ZDF GraphQL returned an unexpected shape for {operation}")
        errors = data.get("errors") or []
        if errors and isinstance(errors, list):
            message = (errors[0] or {}).get("message") if isinstance(errors[0], dict) else str(errors[0])
            # Some queries legitimately return null data with an error (not found).
            if not data.get("data"):
                raise ZdfError(f"ZDF GraphQL {operation}: {message or 'error'}")
        return data

    def search(self, query: str, *, first: int = 20, after: str | None = None) -> list[SearchHit]:
        return list(self.search_page(query, first=first, after=after).hits)

    def search_page(self, query: str, *, first: int = 20, after: str | None = None) -> SearchPage:
        wanted = str(query or "").strip()
        if not wanted:
            raise ZdfError("ZDF search term cannot be empty")
        data = self.graphql(
            "SearchDocuments",
            SEARCH_QUERY,
            {
                "query": wanted,
                "mode": None,
                "first": first,
                "after": after,
                "filters": None,
                "group": APP_ID,
            },
        )
        block = ((data.get("data") or {}).get("searchDocuments")) or {}
        hits: list[SearchHit] = []
        for entry in block.get("results") or []:
            if not isinstance(entry, dict):
                continue
            item = entry.get("item")
            if isinstance(item, dict):
                hit = _search_hit(item)
                if hit is not None:
                    hits.append(hit)
        page_info = block.get("pageInfo") if isinstance(block.get("pageInfo"), dict) else {}
        end_cursor = str(page_info.get("endCursor") or "")
        has_next_page = bool(page_info.get("hasNextPage"))
        if has_next_page and not end_cursor:
            raise ZdfError("ZDF search response has a next page without a cursor")
        return SearchPage(
            tuple(hits),
            end_cursor=end_cursor,
            has_next_page=has_next_page,
        )

    def video(self, canonical: str) -> Video | None:
        data = self.graphql(
            "GetPlayableVideoByCanonical",
            PLAYABLE_VIDEO_QUERY,
            {"canonical": canonical},
        )
        raw = ((data.get("data") or {}).get("videoByCanonical")) or None
        if not isinstance(raw, dict) or not raw.get("canonical"):
            return None
        return _video(raw)

    def collection(self, canonical: str, *, video_page_size: int = 100) -> Collection | None:
        data = self.graphql(
            "GetSmartCollectionByCanonical",
            COLLECTION_QUERY,
            {"canonical": canonical, "videoPageSize": video_page_size},
        )
        raw = ((data.get("data") or {}).get("smartCollectionByCanonical")) or None
        if not isinstance(raw, dict) or not raw.get("canonical"):
            return None
        return self._complete_collection(_collection(raw), video_page_size)

    def _complete_collection(
        self,
        collection: Collection,
        video_page_size: int,
    ) -> Collection:
        page_size = min(max(int(video_page_size or 100), 1), 100)
        for offset, season in enumerate(collection.seasons):
            if season.episodes_has_next_page:
                self._complete_season(collection.id, season, offset, page_size)
        for connection_name in ("sections", "extras"):
            if getattr(collection, f"{connection_name}_has_next_page"):
                self._complete_video_connection(collection, connection_name, page_size)
        return collection

    def _complete_season(
        self,
        collection_id: str,
        season: Season,
        season_offset: int,
        page_size: int,
    ) -> None:
        cursor = season.episodes_end_cursor
        cursors = {cursor}
        seen = {video.canonical or video.id for video in season.episodes}
        while season.episodes_has_next_page:
            if not cursor:
                raise ZdfError(f"ZDF season {season.id} has no next-page cursor")
            payload = self.graphql(
                "GetSmartCollectionEpisodes",
                SEASON_PAGE_QUERY,
                {
                    "smartCollectionId": collection_id,
                    "videoPageSize": page_size,
                    "seasonOffset": season_offset,
                    "episodeAfter": cursor,
                },
            )
            root = (payload.get("data") or {}).get("smartCollectionById") or {}
            seasons = _nodes(root.get("seasons"))
            if len(seasons) != 1 or str(seasons[0].get("id") or "") != season.id:
                raise ZdfError(f"ZDF returned no matching page for season {season.id}")
            connection = seasons[0].get("episodes")
            cursor, has_next = _append_page(
                season.episodes, connection, seen, cursors, f"season {season.id}"
            )
            season.episodes_end_cursor = cursor
            season.episodes_has_next_page = has_next

    def _complete_video_connection(
        self,
        collection: Collection,
        field: str,
        page_size: int,
    ) -> None:
        operation, query, cursor_name = (
            ("GetSmartCollectionSections", SECTIONS_PAGE_QUERY, "sectionAfter")
            if field == "sections"
            else ("GetSmartCollectionExtras", EXTRAS_PAGE_QUERY, "extraAfter")
        )
        target = getattr(collection, field)
        cursor = getattr(collection, f"{field}_end_cursor")
        cursors = {cursor}
        seen = {video.canonical or video.id for video in target}
        while getattr(collection, f"{field}_has_next_page"):
            if not cursor:
                raise ZdfError(f"ZDF collection {field} has no next-page cursor")
            payload = self.graphql(
                operation,
                query,
                {
                    "smartCollectionId": collection.id,
                    "videoPageSize": page_size,
                    cursor_name: cursor,
                },
            )
            root = (payload.get("data") or {}).get("smartCollectionById") or {}
            cursor, has_next = _append_page(target, root.get(field), seen, cursors, field)
            setattr(collection, f"{field}_end_cursor", cursor)
            setattr(collection, f"{field}_has_next_page", has_next)

    def resolve(self, canonical: str) -> Video | Collection:
        video = self.video(canonical)
        if video is not None:
            return video
        collection = self.collection(canonical)
        if collection is not None:
            return collection
        raise ZdfError(f"ZDF found nothing for {canonical}")

    def channels(self) -> list[Channel]:
        data = self.graphql("GetAllLiveVideos", LIVE_QUERY, {})
        nodes = (((data.get("data") or {}).get("videos") or {}).get("nodes")) or []
        channels: list[Channel] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            channel = _channel(node)
            if channel is not None:
                channels.append(channel)
        return channels

    def playback_metadata(
        self,
        video: Video,
        *,
        chapters: bool,
        attachments: bool,
    ) -> PlaybackMetadata:
        chapters = chapters and not video.is_live
        if not video.canonical or not (chapters or attachments):
            return PlaybackMetadata()
        payload = self.graphql(
            "VideoByCanonical",
            PLAYBACK_METADATA_QUERY,
            {"canonical": video.canonical, "chapters": chapters, "attachments": attachments},
        )
        data = payload.get("data")
        root = data.get("videoByCanonical") if isinstance(data, dict) else None
        if not isinstance(root, dict):
            raise ZdfError("ZDF returned no optional playback metadata")
        errors = payload.get("errors") or []
        warnings = [
            str(error.get("message") or "metadata query error")
            for error in errors if isinstance(error, dict)
        ]
        chapter_list: tuple[Chapter, ...] = ()
        if chapters:
            # Match the media actually selected for PTMD, including its version.
            media = next(
                (node for node in _nodes(root.get("currentMedia")) if node.get("id") == video.media_id),
                {},
            )
            chapter_list, chapter_warnings = _chapters(media, duration=video.duration)
            warnings.extend(chapter_warnings)
            external = root.get("external")
            source_url = external.get("streamAnchorSourceUrl") if isinstance(external, dict) else None
            if source_url:
                try:
                    document = self._chapter_document(source_url)
                    external_chapters, external_warnings = _external_chapters(document, duration=video.duration)
                    chapter_list = normalize_chapters((*chapter_list, *external_chapters))
                    warnings.extend(external_warnings)
                except (ZdfError, TypeError, ValueError) as exc:
                    warnings.append(f"ZDF external chapters unavailable: {exc}")
        image_list = _attachments(root) if attachments else ()
        return PlaybackMetadata(chapter_list, image_list, tuple(warnings))

    def _chapter_document(self, source_url: str) -> dict[str, Any]:
        if not _is_http_url(source_url):
            raise ZdfError("ZDF returned an invalid chapter source URL")
        session = self._session_factory()
        session.proxies.update(dict(getattr(self.session, "proxies", {}) or {}))
        try:
            response = session.get(
                source_url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=20,
            )
            response.raise_for_status()
            document = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ZdfError(f"ZDF chapter source failed: {exc}") from exc
        finally:
            session.close()
        if not isinstance(document, dict):
            raise ZdfError("ZDF chapter source returned an unexpected shape")
        return document

    def ensure_playable(self, video: Video) -> Video:
        if video.ptmd_template:
            return video
        if not video.canonical:
            raise ZdfError(f"ZDF video {video.id or video.title} has no PTMD template")
        refreshed = self.video(video.canonical)
        if refreshed is None or not refreshed.ptmd_template:
            raise ZdfError(f"ZDF returned no playable media for {video.canonical}")
        return refreshed

    def source(self, video: Video) -> Source:
        playable = self.ensure_playable(video)
        return self.source_from_ptmd(playable.ptmd_template)

    def source_from_ptmd(self, ptmd_template: str) -> Source:
        try:
            ptmd_url = build_ptmd_url(ptmd_template)
        except (IndexError, KeyError, ValueError) as exc:
            raise ZdfError("ZDF returned an invalid PTMD template") from exc
        try:
            response = self.request(
                "GET",
                ptmd_url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
            ptmd = response.json()
        except ValueError as exc:
            raise ZdfError("ZDF PTMD response is not JSON") from exc
        if not isinstance(ptmd, dict):
            raise ZdfError("ZDF PTMD response has an unexpected shape")
        stream = select_stream_candidate(ptmd)
        if stream is None:
            raise ZdfError("ZDF PTMD offered no supported stream")
        return Source(
            url=str(stream["url"]),
            mime_type=str(stream.get("mimeType") or ""),
            quality=str(stream.get("quality") or ""),
            format_type=str(stream.get("formatType") or ""),
            ptmd_url=ptmd_url,
            player_id=PLAYER_ID,
            resolution=_int(stream.get("resolution")) or 0,
            track_class=str(stream.get("trackClass") or ""),
            subtitles=_subtitles(ptmd),
        )

    def subtitle_bytes(
        self,
        url: str,
        *,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> bytes:
        if not _is_http_url(url):
            raise ZdfError("ZDF returned an invalid subtitle URL")
        session = self._session_factory()
        session.proxies.update(dict(getattr(self.session, "proxies", {}) or {}))
        limit = 16 * 1024 * 1024
        try:
            with session.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                timeout=30,
                stream=True,
            ) as response:
                response.raise_for_status()
                total = _int(response.headers.get("Content-Length"))
                total = total if total is not None and total > 0 else None
                if total is not None and total > limit:
                    raise ZdfError("ZDF subtitle exceeds the 16 MiB size limit")
                if progress is not None:
                    progress(0, total)
                data = bytearray()
                for chunk in response.iter_content(chunk_size=65_536):
                    if not chunk:
                        continue
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ZdfError("ZDF subtitle exceeds the 16 MiB size limit")
                    if progress is not None:
                        progress(len(data), total)
        except requests.RequestException as exc:
            raise ZdfError(f"ZDF subtitle request failed: {exc}") from exc
        finally:
            session.close()
        if not data:
            raise ZdfError("ZDF returned an empty subtitle")
        return bytes(data)


def build_ptmd_url(ptmd_template: str) -> str:
    path = ptmd_template.format(playerId=PLAYER_ID).replace("android_native_5", PLAYER_ID)
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return PTMD_BASE + path


def _subtitles(ptmd: dict[str, Any]) -> tuple[SubtitleReference, ...]:
    captions = ptmd.get("captions")
    if not isinstance(captions, list):
        return ()
    preferred: dict[tuple[str, str, str], SubtitleReference] = {}
    for caption in captions:
        if not isinstance(caption, dict):
            continue
        url = caption.get("uri")
        if not _is_http_url(url):
            continue
        fmt = str(caption.get("format") or "").lower()
        if fmt in {"webvtt", "vtt"}:
            extension = "vtt"
        elif fmt in {"srt", "subrip"}:
            extension = "srt"
        elif fmt.startswith("ebu-tt") or fmt in {"ttml", "dfxp"}:
            extension = "ttml"
        else:
            extension = {"vtt": "vtt", "srt": "srt", "xml": "ttml", "ttml": "ttml"}.get(
                urlparse(url).path.rsplit(".", 1)[-1].lower(), ""
            )
        if not extension:
            continue
        language = str(caption.get("language") or "deu").strip().replace("_", "-").lower() or "deu"
        raw_kind = str(caption.get("class") or "normal").strip().lower()
        kind = {"hoh": "sdh", "main": "normal"}.get(raw_kind, raw_kind) or "normal"
        label = _compact(caption.get("label"))
        identity = (language, kind, label)
        existing = preferred.get(identity)
        rank = {"vtt": 0, "srt": 1, "ttml": 2}
        if existing is not None and rank[existing.format] <= rank[extension]:
            continue
        name = label or ("Deutsch" if language in {"de", "deu", "ger"} else language)
        if kind != "normal" and not label:
            name += f" ({kind.upper()})"
        preferred[identity] = SubtitleReference(
            url=url,
            language=language,
            kind=kind,
            name=name,
            format=extension,
            headers={"User-Agent": USER_AGENT},
        )
    return tuple(preferred.values())


def _chapters(media: dict[str, Any], *, duration: float | None) -> tuple[tuple[Chapter, ...], list[str]]:
    result: list[Chapter] = []
    warnings: list[str] = []
    duration_ms = None
    if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0:
        duration_ms = round(duration * 1000)
    for index, raw in enumerate(_nodes(media.get("streamAnchorTags")), 1):
        try:
            chapter = Chapter.from_seconds(raw.get("anchorOffset"), _compact(raw.get("anchorLabel")))
            if duration_ms is not None and chapter.start_ms >= duration_ms:
                raise ValueError("start lies outside the selected media")
        except (TypeError, ValueError, OverflowError) as exc:
            warnings.append(f"ZDF chapter {index} skipped: {exc}")
            continue
        result.append(chapter)
    intro = media.get("skipIntro")
    if isinstance(intro, dict):
        start, stop = intro.get("startIntroTimeOffset"), intro.get("stopIntroTimeOffset")
        if start is not None or stop is not None:
            try:
                if start is None or stop is None:
                    raise ValueError("intro has no complete start/end range")
                chapter = Chapter.from_seconds(start, "Intro", end=stop, kind="intro")
                if duration_ms is not None and chapter.end_ms > duration_ms:
                    raise ValueError("intro lies outside the selected media")
                result.append(chapter)
            except (TypeError, ValueError, OverflowError) as exc:
                warnings.append(f"ZDF intro skipped: {exc}")
    return normalize_chapters(result, duration_ms=duration_ms), warnings


def _external_chapters(document: dict[str, Any], *, duration: float | None) -> tuple[tuple[Chapter, ...], list[str]]:
    scenes = document.get("scene")
    if not isinstance(scenes, list):
        raise ZdfError("ZDF chapter source returned no scene list")
    result: list[Chapter] = []
    warnings: list[str] = []
    for index, scene in enumerate(scenes, 1):
        try:
            if not isinstance(scene, dict):
                raise ValueError("scene is not an object")
            if "show_list_no" in str(scene.get("extra") or ""):
                continue
            # Heimspiel SceneDto uses offsets and lengths in seconds for VOD.
            start, length = scene.get("offset"), scene.get("length")
            if type(start) is not int or type(length) is not int or length <= 0:
                raise ValueError("scene needs integer offset and positive length")
            chapter = Chapter.from_seconds(start, _compact(scene.get("title")), end=start + length)
            if duration is not None and chapter.end_ms > duration * 1000:
                raise ValueError("scene lies outside the selected media")
            result.append(chapter)
        except (TypeError, ValueError, OverflowError) as exc:
            warnings.append(f"ZDF external chapter {index} skipped: {exc}")
    return normalize_chapters(result), warnings


def _is_http_url(value: Any) -> bool:
    if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlparse(value)
        return (
            parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and not any(char in value for char in "{}")
        )
    except ValueError:
        return False


def _attachments(raw: dict[str, Any]) -> tuple[Attachment, ...]:
    teaser = raw.get("teaser")
    image = teaser.get("imageWithoutLogo") if isinstance(teaser, dict) else None
    images = image.get("layouts") if isinstance(image, dict) else None
    if not isinstance(images, dict):
        return ()
    result: list[Attachment] = []
    seen: set[str] = set()
    for variant, value in images.items():
        if not isinstance(value, str):
            continue
        url = value.strip()
        if not _is_http_url(url) or url in seen:
            continue
        parsed = urlparse(url)
        size = re.fullmatch(r"dim(\d+)[xX](\d+)", str(variant))
        kind = "poster" if size and int(size[2]) > int(size[1]) else "thumbnail"
        mime = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "avif": "image/avif",
        }.get(parsed.path.rsplit(".", 1)[-1].lower(), "")
        result.append(Attachment(url=url, name=f"ZDF artwork · {variant}", kind=kind, mime_type=mime))
        seen.add(url)
    return tuple(result)


def select_stream_candidate(ptmd: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for priority_index, priority in enumerate(ptmd.get("priorityList") or []):
        if not isinstance(priority, dict):
            continue
        for format_index, media_format in enumerate(priority.get("formitaeten") or []):
            if not isinstance(media_format, dict):
                continue
            facets = media_format.get("facets") or []
            if "restriction_useragent" in facets:
                continue
            mime_type = str(media_format.get("mimeType") or "")
            fmt_type = str(media_format.get("type") or "")
            for quality_index, quality in enumerate(media_format.get("qualities") or []):
                if not isinstance(quality, dict):
                    continue
                quality_name = str(quality.get("quality") or "")
                resolution = _int(quality.get("highestVerticalResolution")) or 0
                audio = quality.get("audio") or {}
                if not isinstance(audio, dict):
                    continue
                for track in audio.get("tracks") or []:
                    if not isinstance(track, dict):
                        continue
                    uri = track.get("uri")
                    if not uri:
                        continue
                    track_class = str(track.get("class") or "")
                    candidates.append(
                        {
                            "url": uri,
                            "mimeType": mime_type,
                            "formatType": fmt_type,
                            "quality": quality_name,
                            "resolution": resolution,
                            "trackClass": track_class,
                            "score": (
                                _track_class_rank(track_class),
                                _mime_rank(mime_type, str(uri)),
                                _QUALITY_RANK.get(quality_name.lower(), 20),
                                -int(resolution or 0),
                                priority_index,
                                format_index,
                                quality_index,
                            ),
                        }
                    )
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item["score"])[0]


def _track_class_rank(track_class: str) -> int:
    if track_class.lower() == "main":
        return 0
    if track_class.lower() == "ot":
        return 1
    return 2


def _mime_rank(mime_type: str, uri: str) -> int:
    lowered_mime = (mime_type or "").lower()
    lowered_path = urlparse(uri or "").path.lower()
    if "mpegurl" in lowered_mime or lowered_path.endswith(".m3u8"):
        return 0
    if "dash" in lowered_mime or lowered_path.endswith(".mpd"):
        return 1
    if "mp4" in lowered_mime or lowered_path.endswith(".mp4"):
        return 2
    if "webm" in lowered_mime or lowered_path.endswith(".webm"):
        return 3
    return 4


def _protocol(mime_type: str, uri: str) -> str:
    rank = _mime_rank(mime_type, uri)
    if rank == 0:
        return "HLS"
    if rank == 1:
        return "DASH"
    if rank == 2:
        return "MP4"
    if rank == 3:
        return "WebM"
    return ""


def _search_hit(item: dict[str, Any]) -> SearchHit | None:
    canonical = str(item.get("canonical") or "")
    title = _title(item)
    if not canonical and not title:
        return None
    typename = str(item.get("__typename") or "")
    if typename == "Video":
        kind = "video"
    elif "SmartCollection" in typename or item.get("collectionType"):
        kind = "collection"
    elif typename == "MetaCollection":
        kind = "meta"
    elif typename == "CuratedCollection":
        kind = "curated"
    else:
        kind = "other"
    return SearchHit(
        id=str(item.get("id") or canonical),
        canonical=canonical,
        title=title,
        kind=kind,
        content_type=str(item.get("contentType") or item.get("currentMediaType") or ""),
        synopsis=_description(item),
        collection_type=str(item.get("collectionType") or item.get("metaType") or ""),
        year=_year(item),
        seasons=_int(item.get("countSeasons")),
        episodes=_int(item.get("countEpisodes")),
    )


def _video(raw: dict[str, Any]) -> Video:
    collection = raw.get("smartCollection") if isinstance(raw.get("smartCollection"), dict) else {}
    episode = raw.get("episodeInfo") if isinstance(raw.get("episodeInfo"), dict) else {}
    media = _select_media_node(raw)
    is_live = _is_live(raw)
    is_movie = _is_movie(raw)
    duration = None
    geo = ""
    state = ""
    media_type = ""
    media_id = ""
    ptmd = ""
    if media:
        media_id = str(media.get("id") or "")
        ptmd = str(media.get("ptmdTemplate") or "")
        media_type = str(
            media.get("vodMediaType") or media.get("liveMediaType") or media.get("__typename") or ""
        )
        geo = str(media.get("geoLocation") or "")
        state = str(media.get("state") or "")
        duration_raw = media.get("duration")
        if duration_raw is not None:
            try:
                duration = float(duration_raw)
            except (TypeError, ValueError):
                duration = None
    return Video(
        id=str(raw.get("id") or raw.get("canonical") or ""),
        canonical=str(raw.get("canonical") or ""),
        title=_title(raw),
        content_type=str(raw.get("contentType") or raw.get("currentMediaType") or ""),
        year=_year(raw),
        synopsis=_description(raw),
        season=_int(episode.get("seasonNumber")),
        episode=_int(episode.get("episodeNumber")),
        series_title=str(collection.get("title") or ""),
        series_canonical=str(collection.get("canonical") or ""),
        is_movie=is_movie,
        is_live=is_live,
        media_id=media_id,
        ptmd_template=ptmd,
        media_type=media_type,
        duration=duration,
        geo=geo,
        state=state,
    )


def _collection(raw: dict[str, Any]) -> Collection:
    movie_raw = raw.get("video")
    movie = _video(movie_raw) if isinstance(movie_raw, dict) else None
    sections_connection = raw.get("sections") if isinstance(raw.get("sections"), dict) else {}
    extras_connection = raw.get("extras") if isinstance(raw.get("extras"), dict) else {}
    sections_page_info = (
        sections_connection.get("pageInfo")
        if isinstance(sections_connection.get("pageInfo"), dict)
        else {}
    )
    extras_page_info = (
        extras_connection.get("pageInfo")
        if isinstance(extras_connection.get("pageInfo"), dict)
        else {}
    )
    seasons: list[Season] = []
    for key in ("seasons", "ascSeasons", "descSeasons"):
        for season_raw in _nodes(raw.get(key)):
            season = _season(season_raw)
            if season is not None and all(existing.id != season.id for existing in seasons):
                seasons.append(season)
    return Collection(
        id=str(raw.get("id") or raw.get("canonical") or ""),
        canonical=str(raw.get("canonical") or ""),
        title=_title(raw),
        collection_type=str(raw.get("collectionType") or raw.get("__typename") or ""),
        seasons=seasons,
        sections=[_video(item) for item in _nodes(raw.get("sections"))],
        extras=[_video(item) for item in _nodes(raw.get("extras"))],
        movie=movie,
        count_seasons=_int(raw.get("countSeasons")),
        count_episodes=_int(raw.get("countEpisodes")),
        sections_end_cursor=str(sections_page_info.get("endCursor") or ""),
        sections_has_next_page=bool(sections_page_info.get("hasNextPage")),
        extras_end_cursor=str(extras_page_info.get("endCursor") or ""),
        extras_has_next_page=bool(extras_page_info.get("hasNextPage")),
    )


def _season(raw: dict[str, Any]) -> Season | None:
    if not isinstance(raw, dict):
        return None
    episodes_connection = raw.get("episodes") if isinstance(raw.get("episodes"), dict) else {}
    episodes = [_video(item) for item in _nodes(episodes_connection)]
    page_info = episodes_connection.get("pageInfo") if isinstance(episodes_connection, dict) else {}
    page_info = page_info if isinstance(page_info, dict) else {}
    season_id = str(raw.get("id") or raw.get("number") or raw.get("title") or "")
    if not season_id and not episodes:
        return None
    return Season(
        id=season_id or f"season-{raw.get('number')}",
        title=str(raw.get("title") or ""),
        number=_int(raw.get("number")),
        episodes=episodes,
        episodes_end_cursor=str(page_info.get("endCursor") or ""),
        episodes_has_next_page=bool(page_info.get("hasNextPage")),
    )


def _channel(raw: dict[str, Any]) -> Channel | None:
    canonical = str(raw.get("canonical") or "")
    title = _title(raw)
    if not canonical and not title:
        return None
    media_nodes = _media_nodes(raw)
    media = media_nodes[0] if media_nodes else {}
    collection = raw.get("smartCollection") if isinstance(raw.get("smartCollection"), dict) else {}
    return Channel(
        id=str(raw.get("id") or canonical),
        canonical=canonical,
        title=title,
        content_type=str(raw.get("contentType") or "LIVE"),
        state=str(media.get("state") or ""),
        series_title=str(collection.get("title") or ""),
    )


def _select_media_node(video: dict[str, Any]) -> dict[str, Any] | None:
    nodes = _media_nodes(video)
    live = _is_live(video)
    if live:
        for node in nodes:
            if (
                node.get("__typename") == "LiveMedia"
                and node.get("liveMediaType") == "DEFAULT"
                and node.get("ptmdTemplate")
            ):
                return node
        for node in nodes:
            if node.get("__typename") == "LiveMedia" and node.get("ptmdTemplate"):
                return node
    for node in nodes:
        if (
            node.get("__typename") == "VodMedia"
            and node.get("vodMediaType") == "DEFAULT"
            and node.get("ptmdTemplate")
        ):
            return node
    for node in nodes:
        if node.get("ptmdTemplate"):
            return node
    return None


def _media_nodes(video: dict[str, Any]) -> list[dict[str, Any]]:
    connection = video.get("currentMedia") or {}
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    return [node for node in (nodes or []) if isinstance(node, dict)]


def _nodes(connection: Any) -> list[dict[str, Any]]:
    if not isinstance(connection, dict):
        return []
    return [node for node in (connection.get("nodes") or []) if isinstance(node, dict)]


def _append_page(
    target: list[Video],
    connection: Any,
    seen: set[str],
    cursors: set[str],
    label: str,
) -> tuple[str, bool]:
    if not isinstance(connection, dict) or not isinstance(connection.get("pageInfo"), dict):
        raise ZdfError(f"ZDF {label} returned no page information")
    page_info = connection["pageInfo"]
    cursor = str(page_info.get("endCursor") or "")
    has_next = bool(page_info.get("hasNextPage"))
    if has_next and (not cursor or cursor in cursors):
        raise ZdfError(f"ZDF {label} returned a repeated page cursor")
    cursors.add(cursor)
    for raw in _nodes(connection):
        video = _video(raw)
        identity = video.canonical or video.id
        if identity and identity not in seen:
            seen.add(identity)
            target.append(video)
    return cursor, has_next


def _is_movie(video: dict[str, Any]) -> bool:
    if video.get("contentType") == "MOVIE":
        return True
    collection = video.get("smartCollection") or {}
    if isinstance(collection, dict) and collection.get("collectionType") == "MOVIE":
        return True
    publication = (
        ((video.get("structuralMetadata") or {}).get("publicationFormMetaCollection") or {})
        if isinstance(video.get("structuralMetadata"), dict)
        else {}
    )
    return isinstance(publication, dict) and publication.get("title") == "Film"


def _is_live(video: dict[str, Any]) -> bool:
    if video.get("contentType") == "LIVE" or video.get("currentMediaType") == "LIVE":
        return True
    return any(node.get("__typename") == "LiveMedia" for node in _media_nodes(video))


def _title(item: dict[str, Any]) -> str:
    teaser = item.get("teaser") if isinstance(item.get("teaser"), dict) else {}
    return _compact(item.get("title") or teaser.get("title") or "Unknown")


def _year(item: dict[str, Any]) -> str:
    if item.get("productionYear"):
        return str(item.get("productionYear"))
    editorial = str(item.get("editorialDate") or "")
    return editorial[:4] if editorial else ""


def _description(item: dict[str, Any]) -> str:
    teaser = item.get("teaser") if isinstance(item.get("teaser"), dict) else {}
    return _compact(teaser.get("description") or item.get("leadParagraph") or "")


def _compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "APP_ID",
    "EXTRAS_PAGE_QUERY",
    "SECTIONS_PAGE_QUERY",
    "GRAPHQL",
    "HEADERS",
    "PLAYER_ID",
    "PTMD_BASE",
    "TOKEN_ENDPOINT",
    "TV_CONFIG",
    "USER_AGENT",
    "WWW",
    "Channel",
    "Collection",
    "DeviceCode",
    "DevicePollFailure",
    "ParsedInput",
    "PlaybackMetadata",
    "SearchHit",
    "SearchPage",
    "Season",
    "Source",
    "TvAuth",
    "Video",
    "ZdfApi",
    "ZdfError",
    "build_ptmd_url",
    "cache_is_valid",
    "parse_input",
    "select_stream_candidate",
    "tv_auth_from_cache",
]
