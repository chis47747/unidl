"""VRT MAX Android TV API client, catalogue models and playback exchange."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlparse

import requests

APP_VERSION = "3.14.2-tv"
PACKAGE_NAME = "be.vrt.vrtnu-max"
CLIENT_NAME = "TVAndroid"
OS_VERSION = "36"
DEVICE_NAME = "Google TV Streamer"
PLAYER_VERSION = "58.4.0"
USER_AGENT = (
    f"VRTPlayer/{PLAYER_VERSION} (Android 16; Google {DEVICE_NAME}) "
    f"{CLIENT_NAME}/{APP_VERSION}"
)

GRAPHQL_URL = "https://www.vrt.be/vrtnu-api/graphql/v1"
GRAPHQL_PUBLIC_URL = "https://www.vrt.be/vrtnu-api/graphql/public/v1"
LOGIN_BASE_URL = "https://www.vrt.be/vrtmax/"
MEDIA_BASE_URL = (
    "https://media-services-public.vrt.be/"
    "vualto-video-aggregator-web/rest/external/v2/"
)
MEDIA_CLIENT = "vrtmax-androidtv@PROD"
WIDEVINE_LICENSE_URL = "https://widevine-proxy.drm.technology/proxy?token={token}"
TOKEN_FILE = "vrtmax_token.json"
TOKEN_EXPIRY_SKEW = 60
PAGE_SIZE = 50
MAX_PAGES = 100

CLIENT_HEADERS = {
    "X-VRT-client-name": CLIENT_NAME,
    "X-VRT-client-version": APP_VERSION,
    "X-VRT-client-package": PACKAGE_NAME,
    "X-VRT-client-os-version": OS_VERSION,
}


class VrtMaxError(RuntimeError):
    """A VRT MAX request or response failed without exposing auth material."""


class AuthenticationRequired(VrtMaxError):
    """The cached VRT login cannot be used or refreshed."""


class AvailabilityError(VrtMaxError):
    """VRT refused a title for this account or location."""


@dataclass
class Session:
    access_token: str = ""
    token_type: str = "Bearer"
    refresh_token: str = ""
    expires_at: float = 0.0
    id_token: str = ""
    video_token: str = ""
    scope: str = ""
    profile_sub: str = ""
    profile_type: str = ""
    account_label: str = ""

    @classmethod
    def from_cache(cls, data: dict[str, Any] | None) -> Session:
        raw = data if isinstance(data, dict) else {}
        tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else raw
        return cls(
            access_token=_text(tokens.get("access_token") or tokens.get("accessToken")),
            token_type=_text(tokens.get("token_type") or tokens.get("tokenType") or "Bearer"),
            refresh_token=_text(tokens.get("refresh_token") or tokens.get("refreshToken")),
            expires_at=_number(raw.get("expires_at") or tokens.get("expires_at")),
            id_token=_text(tokens.get("id_token") or tokens.get("idToken")),
            video_token=_text(tokens.get("video_token") or tokens.get("videoToken")),
            scope=_text(tokens.get("scope")),
            profile_sub=_text(raw.get("profile_sub")),
            profile_type=_text(raw.get("profile_type")),
            account_label=_text(raw.get("account_label")),
        )

    def to_cache(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "id_token": self.id_token,
            "video_token": self.video_token,
            "scope": self.scope,
            "profile_sub": self.profile_sub,
            "profile_type": self.profile_type,
            "account_label": self.account_label,
        }

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token or self.refresh_token)

    @property
    def recoverable(self) -> bool:
        return bool(self.refresh_token)

    def is_fresh(self, skew: int = TOKEN_EXPIRY_SKEW) -> bool:
        return bool(self.access_token and self.expires_at > time.time() + skew)

    def clear_auth(self) -> None:
        self.access_token = ""
        self.token_type = "Bearer"
        self.refresh_token = ""
        self.expires_at = 0.0
        self.id_token = ""
        self.video_token = ""
        self.scope = ""
        self.profile_sub = ""
        self.profile_type = ""
        self.account_label = ""


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: float


@dataclass(frozen=True)
class CatalogItem:
    id: str
    object_id: str
    component_id: str
    typename: str
    tile_type: str
    title: str
    description: str
    link: str
    internal_target: str
    image_url: str = ""
    primary_meta: tuple[str, ...] = ()
    secondary_meta: tuple[str, ...] = ()
    available: bool = True

    @property
    def is_program(self) -> bool:
        return self.typename in {"ProgramTile", "PodcastProgramTile", "RadioProgramTile"} or self.internal_target == "programpage"

    @property
    def is_live(self) -> bool:
        return self.typename in {"LivestreamTile", "AudioLivestreamTile"} or self.internal_target in {
            "livestreampage",
            "audiolivestreampage",
        }

    @property
    def is_audio(self) -> bool:
        return self.typename.startswith(("Audio", "Podcast", "Radio"))

    @property
    def is_playable(self) -> bool:
        return self.is_live or self.typename in {
            "EpisodeTile",
            "PodcastEpisodeTile",
            "RadioEpisodeTile",
            "RadioFragmentTile",
        } or self.internal_target in {"episodepage", "playbackpage"}

    @property
    def detail(self) -> str:
        values = [*self.primary_meta, *self.secondary_meta]
        if self.description:
            values.append(self.description)
        return " · ".join(value for value in values if value)

    @property
    def type_label(self) -> str:
        return self.tile_type or self.typename.removesuffix("Tile")

    @property
    def release_year(self) -> str | None:
        return release_year(*self.primary_meta, *self.secondary_meta)


@dataclass(frozen=True)
class CatalogList:
    title: str
    list_id: str
    items: tuple[CatalogItem, ...] = ()
    end_cursor: str = ""
    has_next: bool = False
    component_id: str = ""


@dataclass(frozen=True)
class CatalogPage:
    id: str
    title: str
    typename: str
    lists: tuple[CatalogList, ...]


@dataclass(frozen=True)
class ProgramSection:
    title: str
    lists: tuple[CatalogList, ...] = ()
    component_id: str = ""


@dataclass(frozen=True)
class Program:
    id: str
    title: str
    description: str
    primary_meta: tuple[str, ...]
    secondary_meta: tuple[str, ...]
    sections: tuple[ProgramSection, ...]

    @property
    def is_movie(self) -> bool:
        words = " ".join((*self.primary_meta, *self.secondary_meta)).lower()
        return any(word in words for word in ("film", "movie"))

    @property
    def release_year(self) -> str | None:
        return release_year(*self.primary_meta, *self.secondary_meta)


@dataclass(frozen=True)
class PlayerMode:
    typename: str
    active: bool
    label: str
    stream_id: str


@dataclass(frozen=True)
class PlayerData:
    page_id: str
    page_type: str
    brand: str
    title: str
    subtitle: str
    modes: tuple[PlayerMode, ...]
    primary_meta: tuple[str, ...] = ()
    secondary_meta: tuple[str, ...] = ()

    @property
    def release_year(self) -> str | None:
        return release_year(*self.primary_meta, *self.secondary_meta)

    def active_mode(self) -> PlayerMode:
        active = [mode for mode in self.modes if mode.active and mode.stream_id]
        if len(active) == 1:
            return active[0]
        usable = [mode for mode in self.modes if mode.stream_id]
        if not active and len(usable) == 1:
            return usable[0]
        if not usable:
            raise VrtMaxError(f"VRT MAX returned no playable mode for {self.page_id}")
        raise VrtMaxError(f"VRT MAX did not identify one active player mode for {self.page_id}")


@dataclass(frozen=True)
class PlaybackSource:
    manifest_url: str
    manifest_type: str
    alternate_manifest_urls: tuple[str, ...]
    drm_token: str
    title: str
    description: str
    duration: float | None
    channel_id: str

    @property
    def license_url(self) -> str:
        return WIDEVINE_LICENSE_URL.format(token=quote(self.drm_token, safe="")) if self.drm_token else ""


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _error_text(payload: Any, default: str) -> str:
    if not isinstance(payload, dict):
        return default
    direct = payload.get("errorDescription") or payload.get("error_description") or payload.get("message")
    if direct:
        return _text(direct)
    error = payload.get("error")
    if isinstance(error, dict):
        return _text(error.get("message") or error.get("code")) or default
    if error:
        return _text(error)
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return _text(first.get("message") or first.get("error_code")) or default
        return _text(first) or default
    return default


def _error_code(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    value = payload.get("error") or payload.get("error_code") or payload.get("code")
    if isinstance(value, dict):
        value = value.get("code") or value.get("error_code")
    return _text(value).lower()


def _meta(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    found: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        text = _text(value.get("value") or value.get("longValue") or value.get("shortValue") or value.get("label"))
        if text and text not in found:
            found.append(text)
    return tuple(found)


def _image_url(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return _text(value.get("templateUrl") or value.get("url"))


def _parse_date(value: Any) -> float:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    text = _text(value)
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse_reference(value: str) -> str | None:
    """Return the exact VRT MAX page id from a supported web URL."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("/vrtmax/"):
        path = text
    else:
        candidate = text if "://" in text else f"https://{text}"
        parsed = urlparse(candidate)
        if (parsed.hostname or "").lower() not in {"vrt.be", "www.vrt.be"}:
            return None
        path = parsed.path
        if not path.startswith("/vrtmax/"):
            return None
    path = "/" + path.lstrip("/")
    path = re.sub(r"/{2,}", "/", path)
    return path if path.endswith("/") else f"{path}/"


def parse_season_episode(item: CatalogItem) -> tuple[int | None, int | None]:
    values = " ".join((*item.primary_meta, *item.secondary_meta, item.title, item.link))
    season_match = re.search(r"(?:seizoen|season|[/_-]s)(?:\s*|[/_-])(\d+)", values, re.IGNORECASE)
    episode_match = re.search(r"(?:afl\.?|episode|[/_-]a)(?:\s*|[/_-])(\d+)", values, re.IGNORECASE)
    season = int(season_match.group(1)) if season_match else None
    episode = int(episode_match.group(1)) if episode_match else None
    return season, episode


def release_year(*values: object) -> str | None:
    """Return the first film release year exposed by VRT TV metadata."""
    for value in values:
        match = re.search(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)", str(value or ""))
        if match:
            return match.group(1)
    return None


TILE_FRAGMENT = """
fragment VrtLink on Action {
 __typename
 ... on LinkAction { link internalTarget externalTarget displayLink }
}
fragment VrtTile on ITile {
 __typename
 ... on ProgramTile { id objectId componentId title description tileType image { templateUrl } primaryMeta { type value } secondaryMeta { type value } action { ...VrtLink } }
 ... on EpisodeTile { id objectId componentId title description tileType available image { templateUrl } primaryMeta { type value } secondaryMeta { type value } indexMeta { type value } action { ...VrtLink } }
 ... on LivestreamTile { id objectId componentId title description tileType brand active image { templateUrl } primaryMeta { type value } action { ...VrtLink } }
 ... on ContentTile { id objectId componentId title tileType brand active image { templateUrl } primaryMeta { type value } action { ...VrtLink } }
 ... on BannerTile { id objectId componentId title description tileType active image { templateUrl } labelMeta { type value } secondaryMeta { type value } action { ...VrtLink } }
 ... on StoryTile { id objectId componentId title tileType image { templateUrl } action { ...VrtLink } }
 ... on PodcastEpisodeTile { id objectId componentId title description tileType available active image { templateUrl } primaryMeta { type value } action { ...VrtLink } }
 ... on PodcastProgramTile { id objectId componentId title description tileType image { templateUrl } primaryMeta { type value } secondaryMeta { type value } action { ...VrtLink } }
 ... on AudioLivestreamTile { id objectId componentId title description tileType brand active image { templateUrl } primaryMeta { type value } secondaryMeta { type value } action { ...VrtLink } }
 ... on RadioEpisodeTile { id objectId componentId title description tileType available active image { templateUrl } primaryMeta { type value } indexMeta { type value } action { ...VrtLink } }
 ... on RadioFragmentTile { id objectId componentId title description tileType available active image { templateUrl } primaryMeta { type value } indexMeta { type value } action { ...VrtLink } }
 ... on RadioProgramTile { id objectId componentId title description tileType image { templateUrl } primaryMeta { type value } secondaryMeta { type value } action { ...VrtLink } }
 ... on ButtonTile { id objectId componentId title tileType image { templateUrl } action { ...VrtLink } }
 ... on TopicTile { id objectId componentId title description tileType action { ...VrtLink } }
}
"""

LIST_FRAGMENT = """
fragment VrtBasicList on Component {
 __typename
 ... on LazyTileList { title listId tileVariant }
 ... on StaticTileList { title listId tileVariant items { ...VrtTile } }
 ... on PaginatedTileList {
  title listId tileVariant
  paginatedItems(first: $pageSize) {
   edges { cursor node { ...VrtTile } }
   pageInfo { startCursor endCursor hasNextPage hasPreviousPage }
  }
 }
}
fragment VrtList on Component {
 ...VrtBasicList
 ... on NestedSingleComponentSelect {
  title objectId
  options {
   objectId isActive label
   nodes {
    objectId isActive label lazyComponentId
    preloadedComponent { ...VrtBasicList }
   }
  }
 }
}
"""

DYNAMIC_PAGE_QUERY = """
query DynamicPageTV($id: ID!, $pageSize: Int!, $after: ID, $first: Int!) {
 page(id: $id) {
  __typename
  ... on IPage {
   id title
   paginatedComponents(first: $first, after: $after) {
    edges { node { ...VrtList } }
    pageInfo { endCursor hasNextPage }
   }
  }
 }
}
""" + TILE_FRAGMENT + LIST_FRAGMENT

SEARCH_QUERY = """
query Search($query: String!, $facets: [SearchFacetInput], $pageSize: Int!) {
 pageSearch(input: {q: $query facets: $facets}) {
  __typename
  ... on SearchResultPage { id title components { ...VrtList } }
 }
}
""" + TILE_FRAGMENT + LIST_FRAGMENT

PROGRAM_QUERY = """
query ProgramPageTV($pageId: ID!, $pageSize: Int!) {
 page(id: $pageId) {
  __typename
  ... on ProgramPage {
   id title
   header {
    __typename
    ... on PageHeader {
     contentType title richDescription { text }
     primaryMeta { type value }
     secondaryMeta { type value }
     tertiaryMeta { type value }
    }
   }
   menu {
    __typename
    ... on ContainerNavigation {
     title objectId navigationType
     items {
      __typename
      ... on ContainerNavigationItem {
       title objectId componentId active disabled
       components { ...VrtList }
      }
     }
    }
   }
  }
 }
}
""" + TILE_FRAGMENT + LIST_FRAGMENT

COMPONENT_QUERY = """
query ComponentTv($componentId: ID!, $pageSize: Int!) {
 component(id: $componentId) {
  __typename
  ... on ContainerNavigationItem { title componentId components { ...VrtList } }
 }
}
""" + TILE_FRAGMENT + LIST_FRAGMENT

LIST_PAGE_QUERY = """
query ListPagingData($listId: ID!, $endCursor: ID, $startCursor: ID, $pageSize: Int!) {
 list(listId: $listId) {
  __typename
  ... on StaticTileList {
   title listId
   paginated: paginatedItems(first: $pageSize, after: $endCursor) {
    edges { cursor node { ...VrtTile } }
    pageInfo { startCursor endCursor hasNextPage hasPreviousPage }
   }
  }
  ... on PaginatedTileList {
   title listId
   paginated: paginatedItems(first: $pageSize, after: $endCursor, before: $startCursor) {
    edges { cursor node { ...VrtTile } }
    pageInfo { startCursor endCursor hasNextPage hasPreviousPage }
   }
  }
 }
}
""" + TILE_FRAGMENT

LIST_SNAPSHOT_QUERY = """
query ListSnapshotTV($listId: ID!, $pageSize: Int!) {
 list(listId: $listId) {
  __typename
  ... on StaticTileList { title listId items { ...VrtTile } }
  ... on PaginatedTileList {
   title listId
   paginatedItems(first: $pageSize) {
    edges { cursor node { ...VrtTile } }
    pageInfo { startCursor endCursor hasNextPage hasPreviousPage }
   }
  }
 }
}
""" + TILE_FRAGMENT

PLAYER_QUERY = """
query OnePlayerData($id: ID!) {
 page(id: $id) {
  __typename
  ... on PlaybackPage {
   id brand
   player {
    __typename maxAge title subtitle
    modes { __typename active label streamId }
    primaryMeta { type value }
    secondaryMeta { type value }
   }
  }
  ... on LivestreamPage { linkTemplate }
  ... on AudioLivestreamPage { title linkTemplate }
 }
}
"""

GEO_QUERY = "query GeoLocation { geoLocation }"


def _parse_tile(data: Any) -> CatalogItem | None:
    if not isinstance(data, dict):
        return None
    typename = _text(data.get("__typename"))
    title = _text(data.get("title") or data.get("accessibilityTitle"))
    action = data.get("action") if isinstance(data.get("action"), dict) else {}
    link = _text(action.get("link"))
    item_id = _text(data.get("id"))
    if not (title and (link or item_id)):
        return None
    available = data.get("available")
    return CatalogItem(
        id=item_id,
        object_id=_text(data.get("objectId")),
        component_id=_text(data.get("componentId")),
        typename=typename,
        tile_type=_text(data.get("tileType")),
        title=title,
        description=_text(data.get("description")),
        link=link,
        internal_target=_text(action.get("internalTarget")),
        image_url=_image_url(data.get("image")),
        primary_meta=_meta(data.get("primaryMeta") or data.get("indexMeta") or data.get("labelMeta")),
        secondary_meta=_meta(data.get("secondaryMeta")),
        available=available is not False,
    )


def _connection(data: Any, key: str = "paginatedItems") -> tuple[tuple[CatalogItem, ...], str, bool]:
    if not isinstance(data, dict):
        return (), "", False
    connection = data.get(key)
    if not isinstance(connection, dict):
        items = data.get("items")
        parsed = tuple(item for raw in items if (item := _parse_tile(raw))) if isinstance(items, list) else ()
        return parsed, "", False
    edges = connection.get("edges")
    parsed: list[CatalogItem] = []
    if isinstance(edges, list):
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            item = _parse_tile(node)
            if item is not None:
                parsed.append(item)
    page_info = connection.get("pageInfo") if isinstance(connection.get("pageInfo"), dict) else {}
    return tuple(parsed), _text(page_info.get("endCursor")), bool(page_info.get("hasNextPage"))


def _parse_list(data: Any, *, title: str = "", component_id: str = "") -> CatalogList | None:
    if not isinstance(data, dict):
        return None
    typename = _text(data.get("__typename"))
    if typename not in {"LazyTileList", "StaticTileList", "PaginatedTileList"}:
        return None
    items, cursor, has_next = _connection(data)
    return CatalogList(
        title=_text(data.get("title")) or title,
        list_id=_text(data.get("listId")),
        items=items,
        end_cursor=cursor,
        has_next=has_next,
        component_id=component_id,
    )


def _parse_components(values: Any) -> tuple[CatalogList, ...]:
    if not isinstance(values, list):
        return ()
    found: list[CatalogList] = []
    for value in values:
        direct = _parse_list(value)
        if direct is not None:
            found.append(direct)
            continue
        if not isinstance(value, dict) or value.get("__typename") != "NestedSingleComponentSelect":
            continue
        parent_title = _text(value.get("title"))
        options = value.get("options")
        if not isinstance(options, list):
            continue
        for option in options:
            if not isinstance(option, dict):
                continue
            option_title = _text(option.get("label")) or parent_title
            nodes = option.get("nodes")
            if not isinstance(nodes, list):
                continue
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_title = _text(node.get("label")) or option_title
                component_id = _text(node.get("lazyComponentId"))
                preloaded = node.get("preloadedComponent")
                parsed = _parse_list(preloaded, title=node_title, component_id=component_id)
                if parsed is not None:
                    found.append(parsed)
                elif component_id:
                    found.append(CatalogList(node_title, "", component_id=component_id))
    return tuple(found)


def _dedupe(items: list[CatalogItem]) -> tuple[CatalogItem, ...]:
    found: list[CatalogItem] = []
    seen: set[str] = set()
    for item in items:
        key = item.link or item.id or item.object_id
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(item)
    return tuple(found)


class VrtMaxApi:
    def __init__(
        self,
        session: requests.Session,
        state: Session | None = None,
        *,
        on_save=None,
    ) -> None:
        self.http = session
        self.state = state or Session()
        self.on_save = on_save
        self.http.headers.update(CLIENT_HEADERS)
        self.http.headers.setdefault("User-Agent", USER_AGENT)
        self._player_token = ""
        self._player_token_expires_at = 0.0

    def _persist(self) -> None:
        if self.on_save is not None:
            self.on_save(self.state)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        try:
            return self.http.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise VrtMaxError(f"VRT MAX request failed: {exc}") from exc

    @staticmethod
    def _json(response: requests.Response, label: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except (ValueError, requests.JSONDecodeError) as exc:
            raise VrtMaxError(f"{label} returned invalid JSON (HTTP {response.status_code})") from exc
        if not isinstance(payload, dict):
            raise VrtMaxError(f"{label} returned an unexpected response (HTTP {response.status_code})")
        return payload

    def geo_location(self) -> str:
        payload = self._graphql_once(GRAPHQL_PUBLIC_URL, GEO_QUERY, {}, "GeoLocation", headers={})
        country = _text(payload.get("geoLocation")).upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise VrtMaxError("VRT MAX did not return a two-letter geo location")
        return country

    def start_device_code(self) -> DeviceCode:
        country = self.geo_location()
        response = self._request(
            "POST",
            f"{LOGIN_BASE_URL}sso/deviceauth",
            data={"human_readable_device_name": DEVICE_NAME, "geo_location": country},
        )
        payload = self._json(response, "VRT MAX TV-code request")
        if response.status_code >= 400:
            raise VrtMaxError(_error_text(payload, f"VRT MAX TV-code request failed (HTTP {response.status_code})"))
        device_code = _text(payload.get("device_code"))
        user_code = _text(payload.get("user_code"))
        verification_uri = _text(payload.get("verification_uri"))
        if not (device_code and user_code and verification_uri):
            raise VrtMaxError("VRT MAX TV-code response is missing a device code, user code or verification URL")
        return DeviceCode(
            device_code=device_code,
            user_code=user_code,
            verification_uri=verification_uri,
            verification_uri_complete=_text(payload.get("verification_uri_complete")),
            expires_in=max(1, _integer(payload.get("expires_in"), default=600)),
            interval=max(1.0, _number(payload.get("interval")) or 5.0),
        )

    def poll_device_code(self, challenge: DeviceCode) -> Session | None:
        response = self._request(
            "POST",
            f"{LOGIN_BASE_URL}sso/devicetoken",
            data={"device_code": challenge.device_code},
        )
        payload = self._json(response, "VRT MAX TV-code confirmation")
        code = _error_code(payload)
        if code == "authorization_pending":
            return None
        if code in {"expired_token", "invalid_token"}:
            raise AuthenticationRequired("The VRT MAX TV code expired. Request a new code.")
        if response.status_code >= 400 or code:
            raise VrtMaxError(_error_text(payload, f"VRT MAX TV-code confirmation failed (HTTP {response.status_code})"))
        self._apply_login(payload)
        return self.state

    def _apply_login(self, payload: dict[str, Any]) -> None:
        if payload.get("success") is False or _error_code(payload):
            raise AuthenticationRequired(_error_text(payload, "VRT MAX rejected the login"))
        tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else payload
        access_token = _text(tokens.get("access_token") or tokens.get("accessToken"))
        id_token = _text(tokens.get("id_token") or tokens.get("idToken"))
        refresh_token = _text(tokens.get("refresh_token") or tokens.get("refreshToken")) or self.state.refresh_token
        if not (access_token and id_token and refresh_token):
            raise AuthenticationRequired("VRT MAX login did not return a complete renewable session")

        profile = payload.get("currentProfile") if isinstance(payload.get("currentProfile"), dict) else {}
        user = payload.get("userInfo") if isinstance(payload.get("userInfo"), dict) else {}
        given = _text(user.get("givenName") or user.get("given_name"))
        family = _text(user.get("familyName") or user.get("family_name"))
        account_label = " ".join(value for value in (given, family) if value)
        account_label = account_label or _text(user.get("email")) or _text(profile.get("sub")) or self.state.account_label

        expires_in = max(1, _integer(tokens.get("expires_in") or tokens.get("expiresIn"), default=3600))
        self.state = Session(
            access_token=access_token,
            token_type=_text(tokens.get("token_type") or tokens.get("tokenType") or "Bearer"),
            refresh_token=refresh_token,
            expires_at=time.time() + expires_in,
            id_token=id_token,
            video_token=_text(tokens.get("video_token") or tokens.get("videoToken")),
            scope=_text(tokens.get("scope")),
            profile_sub=_text(profile.get("sub")) or self.state.profile_sub,
            profile_type=_text(profile.get("profile_type") or profile.get("profileType")) or self.state.profile_type,
            account_label=account_label or "VRT MAX account",
        )
        self._player_token = ""
        self._player_token_expires_at = 0.0
        self._persist()

    def refresh(self) -> Session:
        if not self.state.refresh_token:
            raise AuthenticationRequired("VRT MAX is not signed in. Use a new TV code.")
        response = self._request(
            "POST",
            f"{LOGIN_BASE_URL}sso/refresh",
            json={"refresh_token": self.state.refresh_token, "subprofile": None},
        )
        payload = self._json(response, "VRT MAX token refresh")
        code = _error_code(payload)
        if response.status_code in {400, 401, 403} or code in {"expired_token", "invalid_token", "invalid_grant"}:
            self.state.clear_auth()
            self._persist()
            raise AuthenticationRequired("The VRT MAX session expired. Sign in with a new TV code.")
        if response.status_code >= 400 or code:
            raise VrtMaxError(_error_text(payload, f"VRT MAX token refresh failed (HTTP {response.status_code})"))
        self._apply_login(payload)
        return self.state

    def ensure_auth(self, *, force_refresh: bool = False) -> None:
        if not force_refresh and self.state.is_fresh():
            return
        if self.state.recoverable:
            self.refresh()
            return
        raise AuthenticationRequired("VRT MAX is not signed in. Open Sign in and enter a TV code first.")

    @staticmethod
    def _graphql_auth_error(status: int, payload: dict[str, Any]) -> bool:
        if status in {401, 403}:
            return True
        errors = payload.get("errors")
        if not isinstance(errors, list):
            return False
        for error in errors:
            if not isinstance(error, dict):
                continue
            extensions = error.get("extensions") if isinstance(error.get("extensions"), dict) else {}
            code = _text(extensions.get("code") or extensions.get("classification")).lower()
            message = _text(error.get("message")).lower()
            if code in {"unauthorized", "unauthenticated", "forbidden"} or "unauthor" in message:
                return True
        return False

    def _graphql_once(
        self,
        url: str,
        query: str,
        variables: dict[str, Any],
        operation: str,
        *,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            url,
            headers=headers,
            json={"operationName": operation, "variables": variables, "query": query},
        )
        payload = self._json(response, f"VRT MAX {operation}")
        if response.status_code >= 400:
            raise VrtMaxError(_error_text(payload, f"VRT MAX {operation} failed (HTTP {response.status_code})"))
        errors = payload.get("errors")
        if errors:
            raise VrtMaxError(_error_text(payload, f"VRT MAX {operation} returned a GraphQL error"))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise VrtMaxError(f"VRT MAX {operation} returned no GraphQL data")
        return data

    def graphql(self, query: str, variables: dict[str, Any], operation: str) -> dict[str, Any]:
        private = self.state.signed_in
        if private:
            self.ensure_auth()
        url = GRAPHQL_URL if private else GRAPHQL_PUBLIC_URL
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self.state.access_token}"} if private else {}
            response = self._request(
                "POST",
                url,
                headers=headers,
                json={"operationName": operation, "variables": variables, "query": query},
            )
            payload = self._json(response, f"VRT MAX {operation}")
            if private and attempt == 0 and self._graphql_auth_error(response.status_code, payload):
                self.ensure_auth(force_refresh=True)
                continue
            if response.status_code >= 400:
                raise VrtMaxError(_error_text(payload, f"VRT MAX {operation} failed (HTTP {response.status_code})"))
            if payload.get("errors"):
                raise VrtMaxError(_error_text(payload, f"VRT MAX {operation} returned a GraphQL error"))
            data = payload.get("data")
            if not isinstance(data, dict):
                raise VrtMaxError(f"VRT MAX {operation} returned no GraphQL data")
            return data
        raise AuthenticationRequired("VRT MAX rejected the refreshed session. Sign in again.")

    def dynamic_page(self, page_id: str) -> CatalogPage:
        after: str | None = None
        page_data: dict[str, Any] | None = None
        lists: list[CatalogList] = []
        for _page in range(MAX_PAGES):
            data = self.graphql(
                DYNAMIC_PAGE_QUERY,
                {"id": page_id, "pageSize": PAGE_SIZE, "after": after, "first": PAGE_SIZE},
                "DynamicPageTV",
            )
            raw = data.get("page")
            if not isinstance(raw, dict):
                raise VrtMaxError(f"VRT MAX returned no page for {page_id}")
            page_data = raw
            components = raw.get("paginatedComponents") if isinstance(raw.get("paginatedComponents"), dict) else {}
            edges = components.get("edges") if isinstance(components.get("edges"), list) else []
            lists.extend(_parse_components([edge.get("node") for edge in edges if isinstance(edge, dict)]))
            page_info = components.get("pageInfo") if isinstance(components.get("pageInfo"), dict) else {}
            if not page_info.get("hasNextPage"):
                break
            next_cursor = _text(page_info.get("endCursor"))
            if not next_cursor or next_cursor == after:
                raise VrtMaxError(f"VRT MAX component pagination stalled for {page_id}")
            after = next_cursor
        else:
            raise VrtMaxError(f"VRT MAX component pagination exceeded {MAX_PAGES} pages")
        if page_data is None:
            raise VrtMaxError(f"VRT MAX returned no page for {page_id}")
        return CatalogPage(
            id=_text(page_data.get("id")) or page_id,
            title=_text(page_data.get("title")),
            typename=_text(page_data.get("__typename")),
            lists=tuple(lists),
        )

    def list_page(self, catalog: CatalogList) -> CatalogList:
        if not catalog.list_id:
            raise VrtMaxError(f"VRT MAX list {catalog.title!r} has no list id")
        data = self.graphql(
            LIST_PAGE_QUERY,
            {
                "listId": catalog.list_id,
                "endCursor": catalog.end_cursor or None,
                "startCursor": None,
                "pageSize": PAGE_SIZE,
            },
            "ListPagingData",
        )
        raw = data.get("list")
        if not isinstance(raw, dict):
            raise VrtMaxError(f"VRT MAX returned no data for list {catalog.title!r}")
        items, cursor, has_next = _connection(raw, "paginated")
        return CatalogList(
            title=_text(raw.get("title")) or catalog.title,
            list_id=_text(raw.get("listId")) or catalog.list_id,
            items=items,
            end_cursor=cursor,
            has_next=has_next,
            component_id=catalog.component_id,
        )

    def list_snapshot(self, catalog: CatalogList) -> CatalogList:
        if not catalog.list_id:
            raise VrtMaxError(f"VRT MAX list {catalog.title!r} has no list id")
        data = self.graphql(
            LIST_SNAPSHOT_QUERY,
            {"listId": catalog.list_id, "pageSize": PAGE_SIZE},
            "ListSnapshotTV",
        )
        raw = data.get("list")
        if not isinstance(raw, dict):
            raise VrtMaxError(f"VRT MAX returned no snapshot for list {catalog.title!r}")
        items, cursor, has_next = _connection(raw)
        return CatalogList(
            title=_text(raw.get("title")) or catalog.title,
            list_id=_text(raw.get("listId")) or catalog.list_id,
            items=items,
            end_cursor=cursor,
            has_next=has_next,
            component_id=catalog.component_id,
        )

    def complete_list(self, catalog: CatalogList) -> CatalogList:
        current = catalog
        items = list(current.items)
        if not current.items and current.list_id:
            current = self.list_snapshot(current)
            items.extend(current.items)
        for _page in range(MAX_PAGES):
            if not current.has_next:
                return replace(current, items=_dedupe(items))
            previous_cursor = current.end_cursor
            current = self.list_page(current)
            items.extend(current.items)
            if current.has_next and (not current.end_cursor or current.end_cursor == previous_cursor):
                raise VrtMaxError(f"VRT MAX list pagination stalled for {catalog.title!r}")
        raise VrtMaxError(f"VRT MAX list pagination exceeded {MAX_PAGES} pages")

    def search(self, query: str) -> tuple[CatalogItem, ...]:
        data = self.graphql(
            SEARCH_QUERY,
            {"query": query, "facets": None, "pageSize": PAGE_SIZE},
            "Search",
        )
        page = data.get("pageSearch")
        if not isinstance(page, dict):
            return ()
        items: list[CatalogItem] = []
        for catalog in _parse_components(page.get("components")):
            complete = self.complete_list(catalog) if catalog.list_id else catalog
            items.extend(complete.items)
        return _dedupe(items)

    def program(self, page_id: str) -> Program:
        data = self.graphql(
            PROGRAM_QUERY,
            {"pageId": page_id, "pageSize": PAGE_SIZE},
            "ProgramPageTV",
        )
        page = data.get("page")
        if not isinstance(page, dict) or page.get("__typename") != "ProgramPage":
            raise VrtMaxError(f"VRT MAX returned no program page for {page_id}")
        header = page.get("header") if isinstance(page.get("header"), dict) else {}
        rich = header.get("richDescription") if isinstance(header.get("richDescription"), dict) else {}
        menu = page.get("menu") if isinstance(page.get("menu"), dict) else {}
        raw_items = menu.get("items") if isinstance(menu.get("items"), list) else []
        sections: list[ProgramSection] = []
        for raw in raw_items:
            if not isinstance(raw, dict) or raw.get("disabled") is True:
                continue
            lists = _parse_components(raw.get("components"))
            component_id = _text(raw.get("componentId"))
            if lists or component_id:
                sections.append(
                    ProgramSection(
                        title=_text(raw.get("title")) or "Program",
                        lists=lists,
                        component_id=component_id,
                    )
                )
        return Program(
            id=_text(page.get("id")) or page_id,
            title=_text(header.get("title") or page.get("title")) or page_id,
            description=_text(rich.get("text")),
            primary_meta=_meta(header.get("primaryMeta")),
            secondary_meta=_meta(header.get("secondaryMeta")) + _meta(header.get("tertiaryMeta")),
            sections=tuple(sections),
        )

    def component_lists(self, component_id: str) -> tuple[CatalogList, ...]:
        data = self.graphql(
            COMPONENT_QUERY,
            {"componentId": component_id, "pageSize": PAGE_SIZE},
            "ComponentTv",
        )
        component = data.get("component")
        if not isinstance(component, dict):
            raise VrtMaxError(f"VRT MAX returned no component for {component_id}")
        return _parse_components(component.get("components"))

    def player_data(self, page_id: str) -> PlayerData:
        data = self.graphql(PLAYER_QUERY, {"id": page_id}, "OnePlayerData")
        page = data.get("page")
        if not isinstance(page, dict):
            raise VrtMaxError(f"VRT MAX returned no player page for {page_id}")
        player = page.get("player") if isinstance(page.get("player"), dict) else {}
        raw_modes = player.get("modes") if isinstance(player.get("modes"), list) else []
        modes = tuple(
            PlayerMode(
                typename=_text(mode.get("__typename")),
                active=bool(mode.get("active")),
                label=_text(mode.get("label")),
                stream_id=_text(mode.get("streamId")),
            )
            for mode in raw_modes
            if isinstance(mode, dict) and _text(mode.get("streamId"))
        )
        result = PlayerData(
            page_id=_text(page.get("id")) or page_id,
            page_type=_text(page.get("__typename")),
            brand=_text(page.get("brand")),
            title=_text(player.get("title") or page.get("title")),
            subtitle=_text(player.get("subtitle")),
            modes=modes,
            primary_meta=_meta(player.get("primaryMeta")),
            secondary_meta=_meta(player.get("secondaryMeta")),
        )
        result.active_mode()
        return result

    def _vrt_player_token(self) -> str:
        self.ensure_auth()
        if not self.state.video_token:
            raise AuthenticationRequired("The VRT MAX session has no video token. Sign in again with a TV code.")
        if self._player_token and self._player_token_expires_at > time.time() + TOKEN_EXPIRY_SKEW:
            return self._player_token
        response = self._request(
            "POST",
            f"{MEDIA_BASE_URL}tokens",
            json={"identityToken": self.state.video_token, "playerInfo": None},
        )
        payload = self._json(response, "VRT player-token exchange")
        if response.status_code in {401, 403}:
            self.ensure_auth(force_refresh=True)
            response = self._request(
                "POST",
                f"{MEDIA_BASE_URL}tokens",
                json={"identityToken": self.state.video_token, "playerInfo": None},
            )
            payload = self._json(response, "VRT player-token exchange")
        if response.status_code >= 400:
            raise AvailabilityError(_error_text(payload, f"VRT player-token exchange failed (HTTP {response.status_code})"))
        token = _text(payload.get("vrtPlayerToken"))
        if not token:
            raise VrtMaxError("VRT player-token exchange returned no player token")
        expires_at = _parse_date(payload.get("expirationDate"))
        self._player_token = token
        self._player_token_expires_at = expires_at or time.time() + 300
        return token

    def playback_source(self, stream_id: str, *, audio_live: bool = False) -> PlaybackSource:
        player_token = self._vrt_player_token()
        safe_stream_id = quote(stream_id, safe="$:@")
        response = self._request(
            "GET",
            f"{MEDIA_BASE_URL}videos/{safe_stream_id}",
            params={"vrtPlayerToken": player_token, "client": MEDIA_CLIENT},
        )
        payload = self._json(response, "VRT playback source")
        if response.status_code >= 400:
            raise AvailabilityError(_error_text(payload, f"VRT playback source failed (HTTP {response.status_code})"))

        raw_targets = payload.get("targetUrls")
        targets: list[tuple[str, str]] = []
        if isinstance(raw_targets, list):
            for value in raw_targets:
                if not isinstance(value, dict):
                    continue
                target_type = _text(value.get("type")).lower()
                target_url = _text(value.get("url"))
                if target_type and target_url:
                    targets.append((target_type, target_url))
        if not targets:
            raise AvailabilityError("VRT MAX returned no playback target URLs")

        drm_token = _text(payload.get("drm"))
        if drm_token:
            selected = next((target for target in targets if target[0] == "mpeg_dash"), None)
            if selected is None:
                raise AvailabilityError("VRT MAX returned protected playback without an MPEG-DASH target")
            alternates: tuple[str, ...] = ()
        else:
            order = ("hls", "hls_fallback", "mpeg_dash") if audio_live else ("mpeg_dash", "hls", "hls_fallback")
            ranked = sorted(targets, key=lambda target: order.index(target[0]) if target[0] in order else len(order))
            selected = ranked[0]
            alternates = tuple(url for _kind, url in ranked[1:])

        duration = _number(payload.get("duration"))
        return PlaybackSource(
            manifest_url=selected[1],
            manifest_type=selected[0],
            alternate_manifest_urls=alternates,
            drm_token=drm_token,
            title=_text(payload.get("title")),
            description=_text(payload.get("shortDescription")),
            duration=duration if duration > 0 else None,
            channel_id=_text(payload.get("channelId")),
        )


__all__ = [
    "AuthenticationRequired",
    "AvailabilityError",
    "CatalogItem",
    "CatalogList",
    "CatalogPage",
    "DeviceCode",
    "PlaybackSource",
    "PlayerData",
    "Program",
    "ProgramSection",
    "Session",
    "VrtMaxApi",
    "VrtMaxError",
    "parse_reference",
    "parse_season_episode",
]
