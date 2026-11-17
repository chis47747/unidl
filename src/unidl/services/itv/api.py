"""ITVX TV authentication, discovery, playlist and live-channel models."""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlparse

import requests

WWW = "https://www.itv.com"
AUTH_BASE = "https://auth.prd.user.itv.com"
REFRESH = f"{AUTH_BASE}/token"
ACTIVATE_URL = f"{WWW}/watch/pair"
TV_CODE_LENGTH = 7
TV_CODE_INTERVAL = 5
TV_CODE_TIMEOUT = 810
INVENTORY = "https://content-inventory.prd.oasvc.itv.com/discovery"
NOW_NEXT = "https://nownext.oasvc.itv.com/channels?supports=simulcast,live-events,fast"
TEXT_SEARCH = "https://textsearch.prd.oasvc.itv.com/search"
SHORTFORM = "https://shortform.prd.shows.itv.com/graphql"

AUTH_ORIGIN = "https://app.10ft.itv.com"
APP_VERSION = "3.878.0"
AUTH_REFERER = f"{AUTH_ORIGIN}/{APP_VERSION}/androidtv/"
CLIENT_ID = "androidtv"
SERVICE = "itv.x"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 11; BRAVIA 4K VH2 Build/RTM4.211014.001.S104; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/107.0.5304.105 Safari/537.36"
)
AUTH_ACCEPT = "application/vnd.user.auth.v2+json"
VOD_ACCEPT = "application/vnd.itv.vod.playlist.v4+json"
LIVE_ACCEPT = "application/vnd.itv.online.playlist.sim.v3+json"

WIDEVINE_FEATURES = [
    "outband-webvtt",
    "mpeg-dash",
    "widevine",
    "hd",
    "single-track",
    "inband-audio-description",
]
PLAYREADY_FEATURES = [
    "outband-webvtt",
    "mpeg-dash",
    "playready",
    "hd",
    "single-track",
]
SEARCH_FEATURES = [
    "clearkey",
    "outband-webvtt",
    "hls",
    "aes",
    "playready",
    "widevine",
    "fairplay",
    "bbts",
    "progressive",
    "hd",
    "rtmpe",
]

PROGRAMME_QUERY = """
query GetProgramme($brandFilter: BrandFilter!, $titleFilter: TitleFilter!, $id: TitleLegacyId!, $withTitlesToRedirect: Boolean!, $withImages: Boolean!) {
  brands(filter: $brandFilter) {
    ccid title numberOfAvailableSeries legacyId synopses { epg ninety } genres { id name }
    imageUrl(imageType: ITVX) @include(if: $withImages)
    series(sortBy: SEQUENCE_ASC) {
      ccid seriesNumber legacyId seriesType numberOfAvailableEpisodes
      titles(sortBy: SEQUENCE_ASC) {
        __typename ccid titleType legacyId brandLegacyId title broadcastDateTime
        imageUrl(imageType: ITVX) @include(if: $withImages)
        brand { title legacyId genres { id name } imageUrl(imageType: ITVX) @include(if: $withImages) }
        series { seriesNumber seriesType }
        synopses { ninety epg }
        latestAvailableVersion {
          legacyId duration linearContent playlistUrl subtitled audioDescribed
          variants { features }
        }
        ... on Episode { episodeNumber }
        ... on Special { episodeNumber productionYear genres { id name } }
        ... on Film { productionYear genres { id name } }
      }
    }
  }
  titles(filter: $titleFilter) {
    __typename ccid titleType legacyId brandLegacyId title broadcastDateTime
    imageUrl(imageType: ITVX) @include(if: $withImages)
    brand { title legacyId genres { id name } imageUrl(imageType: ITVX) @include(if: $withImages) }
    series { seriesNumber seriesType }
    synopses { ninety epg }
    latestAvailableVersion {
      legacyId duration linearContent playlistUrl subtitled audioDescribed variants { features }
    }
    ... on Episode { episodeNumber }
    ... on Special { episodeNumber productionYear genres { id name } }
    ... on Film { productionYear genres { id name } }
  }
  titlesToRedirect: titles(filter: { legacyId: $id, titleTypes: [EPISODE, SPECIAL, FILM], available: "NOW", tiers: ["PAID", "FREE"] })
    @include(if: $withTitlesToRedirect) { legacyId brandLegacyId title titleType brand { title legacyId } }
}
"""

SHORTFORM_QUERY = """
query GetClipByCcid($ccid: ID!, $isNews: Boolean!, $isSport: Boolean!, $withImages: Boolean! = true) {
  clip(ccid: $ccid) @include(if: $isNews) {
    ccid title description dateTime duration playlistLink subtitles
    image @include(if: $withImages)
    posterImage @include(if: $withImages)
  }
  sportsClip(ccid: $ccid) @include(if: $isSport) {
    ccid title description dateTime duration playlistLink subtitles
    image @include(if: $withImages)
    posterImage @include(if: $withImages)
  }
}
"""


class ItvError(RuntimeError):
    """ITVX rejected a request or returned an unusable shape."""


@dataclass(frozen=True)
class TVCode:
    code: str = field(repr=False)
    expires_at: float

    @property
    def qr_url(self) -> str:
        return f"{ACTIVATE_URL}?{urlencode({'loginCode': self.code, 'device': CLIENT_ID})}"


@dataclass(frozen=True)
class ParsedInput:
    kind: str  # vod | live
    value: str
    shortform_category: str = ""


def parse_input(text: str) -> ParsedInput | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if re.fullmatch(
        r"(?:[A-Za-z0-9]{7}|(?:\d+|[A-Za-z]+\d+)(?:[/_]\w+)*)",
        raw,
    ):
        return ParsedInput("vod", _content_id(raw))
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    if host not in {"itv.com", "www.itv.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    if "live" in parts:
        position = parts.index("live")
        value = parts[position + 1] if position + 1 < len(parts) else "itv1"
        return ParsedInput("live", value.lower())
    try:
        content_id = _content_id(parts[-1])
    except ValueError:
        return None
    lowered = {part.lower() for part in parts}
    category = "NEWS" if "news" in lowered else "SPORT" if "sport" in lowered else ""
    return ParsedInput(
        "vod",
        content_id,
        category if _is_shortform_id(content_id) else "",
    )


@dataclass(frozen=True)
class SearchHit:
    route: str
    title: str
    content_id: str = ""
    channel_id: str = ""
    synopsis: str = ""
    kind: str = ""
    genre: str = ""
    episode_count: int = 0


@dataclass(frozen=True)
class Subtitle:
    url: str
    language: str = "en"
    label: str = "en-gb"
    locale: str = "en-gb"


@dataclass(frozen=True)
class Episode:
    id: str
    show_title: str
    title: str
    playlist_url: str
    season: int | None = None
    number: int | None = None
    year: str = ""
    synopsis: str = ""
    genre: str = ""
    film: bool = False
    features: tuple[str, ...] = ()
    shortform: bool = False
    image_url: str = ""
    programme_image_url: str = ""
    poster_url: str = ""

    @property
    def label(self) -> str:
        if not self.film and self.season is not None and self.number is not None:
            return f"S{self.season:02d}E{self.number:02d}  {self.title}"
        return f"{self.title} ({self.year})" if self.year else self.title


@dataclass
class Season:
    number: int | None
    title: str
    episodes: list[Episode] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.title}  ·  {len(self.episodes)} item(s)"


@dataclass
class Show:
    id: str
    title: str
    synopsis: str = ""
    seasons: list[Season] = field(default_factory=list)
    films: list[Episode] = field(default_factory=list)


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    stream_url: str
    now: str = ""
    next: str = ""
    synopsis: str = ""
    starts_at: str = ""
    logo_url: str = ""


@dataclass(frozen=True)
class Source:
    manifest: str
    license_url: str = ""
    encrypted: bool = True
    protocol: str = "DASH"
    subtitles: tuple[Subtitle, ...] = ()
    start_again: str = ""

    def line(self) -> str:
        bits = [self.protocol, "DRM" if self.encrypted else "clear"]
        if self.subtitles:
            bits.append("subtitles")
        return " · ".join(bits)


class ItvApi:
    def __init__(
        self,
        session: requests.Session | None = None,
        state: dict[str, Any] | None = None,
        *,
        drm_system: str = "widevine",
    ):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.state = state or {}
        self.access_token = ""
        self.refresh_token = ""
        self.drm_system = drm_system
        self.variant_features = PLAYREADY_FEATURES if drm_system == "playready" else WIDEVINE_FEATURES
        self.discovery_features = [feature.replace("-", "_").upper() for feature in self.variant_features]
        self.set_state(self.state)

    # ------------------------------------------------------------------- auth
    def set_state(self, state: dict[str, Any]) -> None:
        self.state = state
        self.access_token = str(state.get("access_token") or "")
        self.refresh_token = str(state.get("refresh_token") or "")
        cookie = str(state.get("session_cookie") or "")
        if cookie:
            self.session.cookies.set("Itv.Session", cookie, domain=".itv.com", path="/")
        if not self.session.cookies.get("Itv.Cid"):
            self.session.cookies.set("Itv.Cid", str(uuid.uuid4()), domain=".itv.com", path="/")

    def start_tv_code(self) -> TVCode:
        # Android TV 1.28.0 loads 3.878.0: authPairCode.length=7, 162 polls at 5s.
        data = self._auth_json("/generate/code", params={"size": TV_CODE_LENGTH})
        code = data.get("code")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9]{7}", code):
            raise ItvError("ITVX returned an invalid TV sign-in code")
        return TVCode(code, time.monotonic() + TV_CODE_TIMEOUT)

    def poll_tv_code(self, challenge: TVCode) -> dict[str, Any] | None:
        if time.monotonic() >= challenge.expires_at:
            raise ItvError("The ITVX TV code expired; start TV code sign-in again")
        data = self._auth_json(f"/verify/{challenge.code}", pending=True)
        if data is None:
            return None
        token = data.get("token")
        if not isinstance(token, str) or _jwt_expiry(token) <= time.time():
            raise ItvError("ITVX pairing returned no valid refresh token")
        return self._accept_tokens(self._auth_json("/token", params={"refresh": token}), refresh_token=token)

    def _auth_json(self, path: str, *, params=None, pending: bool = False) -> dict[str, Any] | None:
        try:
            response = self.session.get(
                f"{AUTH_BASE}{path}",
                headers=self._auth_headers(AUTH_ACCEPT, origin=AUTH_ORIGIN, referer=AUTH_REFERER),
                params=params,
                timeout=10,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise ItvError(f"ITVX TV authentication request failed ({type(exc).__name__})") from None
        if pending and response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            raise ItvError(f"ITVX TV authentication refused the request ({response.status_code}); sign in with a new TV code")
        try:
            data = response.json()
        except ValueError:
            raise ItvError("ITVX TV authentication returned invalid JSON") from None
        if not isinstance(data, dict):
            raise ItvError("ITVX TV authentication returned an invalid response")
        return data

    def authenticate(self) -> dict[str, Any]:
        now = int(time.time())
        if self.access_token and (_int(self.state.get("token_expires")) or _jwt_expiry(self.access_token)) > now + 300:
            return self.state
        refresh_expiry = _int(self.state.get("refresh_token_expires")) or _jwt_expiry(self.refresh_token)
        if self.refresh_token and refresh_expiry > now:
            data = self._auth_json("/token", params={"refresh": self.refresh_token})
            return self._accept_tokens(data)
        raise ItvError("ITVX needs TV code sign-in; choose Sign in with TV code")

    def _accept_tokens(self, data: dict[str, Any], *, refresh_token: str = "") -> dict[str, Any]:
        access = data.get("access_token")
        if not isinstance(access, str) or _jwt_expiry(access) <= time.time():
            raise ItvError("ITVX authentication returned no valid access token")
        refresh = str(data.get("refresh_token") or refresh_token or self.refresh_token)
        profile = str(data.get("profile_token") or self.state.get("profile_token") or "")
        access_claims = _jwt(access)
        refresh_claims = _jwt(refresh)
        account_id = str(access_claims.get("sub") or refresh_claims.get("sub") or self.state.get("account_id") or "")
        profile_id = str(
            access_claims.get("accountProfileIdInUse")
            or refresh_claims.get("accountProfileIdInUse")
            or self.state.get("profile_id")
            or (f"{account_id}_0" if account_id else "")
        )
        state = {
            "access_token": access,
            "refresh_token": refresh,
            "profile_token": profile,
            "token_expires": _jwt_expiry(access),
            "refresh_token_expires": _jwt_expiry(refresh),
            "profile_token_expires": _jwt_expiry(profile),
            "account_id": account_id,
            "profile_id": profile_id,
            "saved_at": int(time.time()),
        }
        self.access_token = access
        self.refresh_token = refresh
        state["session_cookie"] = self._session_cookie(data, state)
        self.set_state(state)
        return state

    def _session_cookie(self, token_data: dict[str, Any], state: dict[str, Any]) -> str:
        content = {
            "entitlement": token_data.get("entitlement", {}),
            "email_verified": token_data.get("email_verified"),
            "token_type": token_data.get("token_type", "bearer"),
            "access_token": state["access_token"],
            "refresh_token": state["refresh_token"],
            "profile_token": state.get("profile_token"),
            "missingUserData": token_data.get("missingUserData", []),
        }
        return json.dumps({"tokens": {"content": content}, "sticky": True, "redirect": "/"}, separators=(",", ":"))

    # -------------------------------------------------------------- catalogue
    def search(self, query: str) -> list[SearchHit]:
        params = {
            "platform": "dotcom",
            "featureSet": ",".join(SEARCH_FEATURES),
            "broadcaster": "itv",
            "channelType": "simulcast",
            "query": query.strip().lower(),
            "size": "20",
            "pretx": "true",
        }
        data = self._json("GET", f"{TEXT_SEARCH}?{urlencode(params).replace('%2C', ',')}", headers=self._headers())
        return [_search_hit(raw) for raw in data.get("results") or [] if isinstance(raw, dict)]

    def show(self, content_id: str, *, shortform_category: str = "", with_images: bool = True) -> Show:
        content_id = _content_id(content_id)
        if shortform_category and _is_shortform_id(content_id):
            return self._shortform_show(content_id, shortform_category, with_images=with_images)
        last_error: ItvError | None = None
        for candidate in _content_id_candidates(content_id):
            try:
                return self._show(
                    candidate,
                    shortform_category=shortform_category,
                    allow_redirect=True,
                    with_images=with_images,
                )
            except ItvError as exc:
                if not str(exc).startswith("ITVX returned no playable programme"):
                    raise
                last_error = exc
        raise last_error or ItvError(f"ITVX returned no playable programme {content_id}")

    def _show(
        self,
        content_id: str,
        *,
        shortform_category: str,
        allow_redirect: bool,
        with_images: bool,
    ) -> Show:
        data = self._programme(content_id, with_images=with_images)
        content = data.get("data") or {}
        brands = content.get("brands") or []
        titles = content.get("titles") or []
        if brands:
            brand = brands[0]
            show_title = str(brand.get("title") or content_id)
            synopsis = str(
                (brand.get("synopses") or {}).get("epg")
                or (brand.get("synopses") or {}).get("ninety")
                or ""
            )
            seasons: list[Season] = []
            films: list[Episode] = []
            for raw_series in brand.get("series") or []:
                number = _int(raw_series.get("seriesNumber"))
                items = [
                    item
                    for raw in raw_series.get("titles") or []
                    if isinstance(raw, dict)
                    and (item := _episode(
                        raw, show_title, number, self.discovery_features, brand.get("imageUrl")
                    )) is not None
                ]
                if items and all(item.film for item in items):
                    films.extend(items)
                elif items:
                    seasons.append(
                        Season(number, f"Series {number}" if number is not None else "Episodes", items)
                    )
            if not seasons and not films:
                fallback = [
                    item
                    for raw in titles
                    if isinstance(raw, dict)
                    and (item := _episode(
                        raw, show_title, None, self.discovery_features, brand.get("imageUrl")
                    )) is not None
                ]
                films = [item for item in fallback if item.film]
                episodic = [item for item in fallback if not item.film]
                if episodic:
                    seasons.append(Season(None, "Episodes", episodic))
            if seasons or films:
                return Show(content_id, show_title, synopsis, seasons, films)

        items = [
            item
            for raw in titles
            if isinstance(raw, dict) and (item := _episode(raw, "", None, self.discovery_features)) is not None
        ]
        if items:
            title = items[0].show_title or items[0].title
            films = [item for item in items if item.film]
            episodes = [item for item in items if not item.film]
            return Show(
                content_id,
                title,
                seasons=[Season(None, "Episodes", episodes)] if episodes else [],
                films=films,
            )

        redirects = content.get("titlesToRedirect") or []
        redirect = str((redirects[0] if redirects else {}).get("brandLegacyId") or "")
        if allow_redirect and redirect and redirect != content_id:
            return self._show(
                _content_id(redirect),
                shortform_category=shortform_category,
                allow_redirect=False,
                with_images=with_images,
            )
        if _is_shortform_id(content_id):
            return self._shortform_show(content_id, shortform_category, with_images=with_images)
        raise ItvError(f"ITVX returned no playable programme {content_id}")

    def _shortform_show(self, content_id: str, category: str = "", *, with_images: bool = True) -> Show:
        routes = {"NEWS": "clip", "SPORT": "sportsClip"}
        requested = category.upper()
        if requested and requested not in routes:
            raise ItvError(f"Unsupported ITVX shortform category {category}")
        categories = (requested,) if requested else tuple(routes)
        for candidate in categories:
            field_name = routes[candidate]
            data = self._json(
                "POST",
                SHORTFORM,
                headers={**self._headers(), "Content-Type": "application/json"},
                json={
                    "query": SHORTFORM_QUERY,
                    "variables": {
                        "ccid": content_id,
                        "isNews": candidate == "NEWS",
                        "isSport": candidate == "SPORT",
                        **({"withImages": False} if not with_images else {}),
                    },
                },
            )
            if data.get("errors"):
                raise ItvError(
                    str(data["errors"][0].get("message") or "ITVX shortform content error")
                )
            clip = (data.get("data") or {}).get(field_name)
            if clip is None:
                continue
            if not isinstance(clip, dict):
                raise ItvError(f"ITVX returned an invalid {candidate.lower()} clip")
            playlist_url = _normalise_shortform_playlist_url(
                str(clip.get("playlistLink") or "")
            )
            if not playlist_url:
                raise ItvError(f"ITVX returned no valid playlist for clip {content_id}")

            title = str(clip.get("title") or clip.get("ccid") or content_id)
            synopsis = str(clip.get("description") or "")
            genre = "Sport" if candidate == "SPORT" else "News"
            item = Episode(
                id=str(clip.get("ccid") or content_id),
                show_title=title,
                title=title,
                playlist_url=playlist_url,
                year=_year(clip.get("dateTime")),
                synopsis=synopsis,
                genre=genre,
                shortform=True,
                image_url=clip.get("image") or "",
                poster_url=clip.get("posterImage") or "",
            )
            return Show(
                content_id,
                title,
                synopsis,
                seasons=[Season(None, genre, [item])],
            )
        raise ItvError(f"ITVX returned no playable programme {content_id}")

    def _programme(self, content_id: str, *, with_images: bool = True) -> dict[str, Any]:
        variables = _programme_variables(content_id, self.discovery_features)
        variables["withImages"] = with_images
        params = {
            "query": _compact_graphql(PROGRAMME_QUERY),
            "variables": json.dumps(variables, separators=(",", ":")),
        }
        data = self._json("GET", INVENTORY, params=params, headers=self._headers())
        if data.get("errors"):
            raise ItvError(str(data["errors"][0].get("message") or "ITVX content error"))
        return data

    # ------------------------------------------------------------------- live
    def channels(self) -> list[Channel]:
        data = self._json("GET", NOW_NEXT, headers=self._headers())
        channels: list[Channel] = []
        for raw in data.get("channels") or []:
            if not isinstance(raw, dict) or not raw.get("streamUrl"):
                continue
            slots = raw.get("slots") or {}
            now = slots.get("now") or {}
            nxt = slots.get("next") or {}
            channels.append(
                Channel(
                    id=str(raw.get("id") or raw.get("channelId") or raw.get("slug") or ""),
                    name=str(raw.get("name") or raw.get("channelName") or raw.get("id") or "ITV Live"),
                    stream_url=str(raw["streamUrl"]),
                    now=str(now.get("title") or now.get("programmeTitle") or ""),
                    next=str(nxt.get("title") or nxt.get("programmeTitle") or ""),
                    synopsis=str(now.get("shortSynopsis") or now.get("synopsis") or now.get("description") or ""),
                    starts_at=str(now.get("broadcastAt") or ""),
                    logo_url=(raw.get("images") or {}).get("logo") or "",
                )
            )
        return channels

    def channel(self, value: str) -> Channel:
        wanted = value.lower()
        for channel in self.channels():
            identities = {channel.id.lower(), channel.name.lower().replace(" ", "")}
            if wanted in identities or wanted == "itv" and "itv1" in identities:
                return channel
        raise ItvError(f"ITVX returned no live channel {value}")

    # --------------------------------------------------------------- playback
    def source(self, item: Episode, drm_system: str) -> Source:
        playlist_url = _playlist_url(item.playlist_url)
        item_features = {
            str(feature).lower().replace("_", "-") for feature in item.features
        }
        requested_features = (
            item.features
            if item.shortform or drm_system.lower() in item_features
            else (
                PLAYREADY_FEATURES
                if drm_system.lower() == "playready"
                else WIDEVINE_FEATURES
            )
        )
        data = self._json(
            "POST",
            playlist_url,
            headers=self._playlist_headers(VOD_ACCEPT),
            json=self._playlist_payload(
                drm_system,
                requested_features,
                live=False,
                shortform=item.shortform,
            ),
            allow_redirects=False,
        )
        video = ((data.get("Playlist") or {}).get("Video") or {})
        files = video.get("MediaFiles") or []
        media = next(
            (entry for entry in files if isinstance(entry, dict) and entry.get("Href")),
            None,
        )
        if media is None:
            raise ItvError(f"ITVX returned no media file for {item.label}")
        base_url = str(video.get("Base") or "")
        manifest = urljoin(base_url, str(media.get("Href") or ""))
        license_url = str(media.get("KeyServiceUrl") or "")
        return Source(
            manifest,
            license_url,
            bool(license_url),
            _protocol(manifest),
            _subtitles(video.get("Subtitles"), base_url),
        )

    def live_source(self, channel: Channel, drm_system: str) -> Source:
        stream_url = _playlist_url(channel.stream_url)
        data = self._json(
            "POST",
            stream_url,
            headers=self._playlist_headers(LIVE_ACCEPT),
            json=self._playlist_payload(
                drm_system,
                ["mpeg-dash", drm_system],
                live=True,
            ),
            allow_redirects=False,
        )
        video = ((data.get("Playlist") or {}).get("Video") or {})
        locations = video.get("VideoLocations") or []
        location = next(
            (entry for entry in locations if isinstance(entry, dict) and entry.get("Url")),
            None,
        )
        if location is None:
            raise ItvError(f"ITVX returned no live location for {channel.name}")
        manifest = str(location.get("Url") or "")
        return Source(
            manifest,
            str(location.get("KeyServiceUrl") or ""),
            bool(location.get("KeyServiceUrl")),
            _protocol(manifest),
            _subtitles(video.get("Subtitles"), str(video.get("Base") or "")),
            _start_again_url(str(location.get("StartAgainUrl") or ""), channel.starts_at),
        )

    def subtitle_bytes(self, subtitle: Subtitle) -> bytes:
        try:
            response = self.session.get(
                subtitle.url,
                headers=self._headers(),
                timeout=35,
            )
        except requests.RequestException as exc:
            raise ItvError(f"ITVX subtitle request failed: {exc}") from exc
        if not response.ok:
            raise ItvError(f"ITVX refused the subtitle request ({response.status_code})")
        if not response.content:
            raise ItvError("ITVX returned an empty subtitle")
        return response.content

    def _playlist_payload(
        self,
        drm_system: str,
        features: Any,
        *,
        live: bool,
        shortform: bool = False,
    ) -> dict[str, Any]:
        normalised = [
            str(feature).lower().replace("_", "-")
            for feature in (features or (["mpeg-dash", drm_system] if live else self.variant_features))
        ]
        availability: dict[str, Any] = {"platformTag": "ctv"}
        if not shortform:
            availability["featureset"] = (
                {"min": normalised, "max": normalised} if live else normalised
            )
        if not live:
            availability.update(
                {
                    "player": "dash",
                    "drm": {
                        "system": drm_system,
                        "maxSupported": "SL3000" if drm_system == "playready" else "L3",
                    },
                }
            )
        return {
            "client": {
                "id": CLIENT_ID,
                "isp": {},
                "supportsAdPods": True,
                "appVersion": APP_VERSION,
                "version": APP_VERSION,
                "service": SERVICE,
                "thirdPartyPaymentModel": "free",
            },
            "device": {
                "manufacturer": CLIENT_ID,
                "model": "Android TV",
                "os": {"name": "android"},
                **({} if live else {"deviceGroup": "ctv"}),
            },
            "variantAvailability": availability,
            "user": {"token": self.access_token},
        }

    # --------------------------------------------------------------- request
    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": AUTH_ORIGIN,
            "Referer": AUTH_REFERER,
        }

    def _auth_headers(
        self,
        accept: str,
        *,
        content_type: str = "",
        origin: str = WWW,
        referer: str = f"{WWW}/",
    ) -> dict[str, str]:
        headers = {**self._headers(), "Accept": accept, "Origin": origin, "Referer": referer}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _playlist_headers(self, accept: str) -> dict[str, str]:
        return {**self._headers(), "Accept": accept, "Content-Type": "application/json"}

    def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.session.request(method, url, timeout=35, **kwargs)
        except requests.RequestException as exc:
            raise ItvError(f"ITVX request failed: {exc}") from exc

        if not 200 <= response.status_code < 300:
            message = response.text[:180]
            if response.content and response.text.strip():
                try:
                    error_data = response.json()
                except ValueError:
                    pass
                else:
                    if isinstance(error_data, dict):
                        message = str(
                            error_data.get("message")
                            or error_data.get("error_description")
                            or error_data.get("error")
                            or message
                        )
                        if error_data.get("IP"):
                            message = f"{message} (IP: {error_data['IP']})"
            raise ItvError(
                f"ITVX refused the request ({response.status_code}): "
                f"{message or 'empty response'}"
            )
        if response.status_code == 204 or not response.content or not response.text.strip():
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise ItvError("ITVX returned something that is not JSON") from exc
        if not isinstance(data, dict):
            raise ItvError("ITVX returned something that is not an object")
        return data


def _episode(
    raw: dict[str, Any],
    show_title: str,
    series_number: int | None,
    discovery_features: list[str],
    programme_image_url: str = "",
) -> Episode | None:
    version = raw.get("latestAvailableVersion") or {}
    playlist = str(version.get("playlistUrl") or "")
    if not playlist:
        return None
    title_type = str(raw.get("titleType") or raw.get("__typename") or "").upper()
    film = title_type == "FILM"
    series = raw.get("series") or {}
    brand = raw.get("brand") or {}
    show = show_title or str(brand.get("title") or raw.get("title") or "ITVX")
    episode_title = str(raw.get("title") or show)
    genres = raw.get("genres") or brand.get("genres") or []
    genre = ", ".join(str(entry.get("name") or entry.get("id") or "") for entry in genres if isinstance(entry, dict))
    synopsis = str((raw.get("synopses") or {}).get("epg") or (raw.get("synopses") or {}).get("ninety") or "")
    year = _year(raw.get("productionYear")) if film else _year(raw.get("productionYear"), raw.get("broadcastDateTime"))
    features = _features(version.get("variants"), discovery_features)
    return Episode(
        id=str(raw.get("legacyId") or version.get("legacyId") or raw.get("ccid") or ""),
        show_title=show,
        title=episode_title,
        playlist_url=playlist,
        season=None if film else _first_int(series.get("seriesNumber"), series_number),
        number=None if film else _int(raw.get("episodeNumber")),
        year=year,
        synopsis=synopsis,
        genre=genre,
        film=film,
        features=tuple(features),
        image_url=raw.get("imageUrl") or "",
        programme_image_url=programme_image_url or brand.get("imageUrl") or "",
    )


def _features(variants: Any, discovery_features: list[str]) -> list[str]:
    allowed = {
        str(feature).upper().replace("-", "_")
        for feature in discovery_features
    }
    matches: list[list[str]] = []
    for variant in variants or []:
        if not isinstance(variant, dict):
            continue
        features = [str(feature) for feature in variant.get("features") or []]
        normalised = {feature.upper().replace("-", "_") for feature in features}
        if features and normalised <= allowed:
            if normalised == allowed:
                return features
            matches.append(features)
    return max(matches, key=len) if matches else list(discovery_features)


def _is_shortform_id(content_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]{7}", content_id or ""))


def _is_itv_https_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and bool(host)
        and (host == "itv.com" or host.endswith(".itv.com"))
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


def _playlist_url(value: str) -> str:
    if not _is_itv_https_url(value):
        raise ItvError("ITVX returned an unsafe playlist URL")
    return value


def _normalise_shortform_playlist_url(playlist_url: str) -> str:
    if not _is_itv_https_url(playlist_url):
        return ""
    parsed = urlparse(playlist_url)
    match = re.search(r"/playlist(?:/v\d+)?(?=/|$)", parsed.path)
    if match is None:
        return ""
    path = f"{parsed.path[:match.start()]}/playlist/v4{parsed.path[match.end():]}"
    return parsed._replace(path=path).geturl()


def _subtitles(value: Any, base_url: str = "") -> tuple[Subtitle, ...]:
    entries: list[dict[str, Any]] = []

    def collect(candidate: Any) -> None:
        if isinstance(candidate, list):
            for item in candidate:
                collect(item)
        elif isinstance(candidate, dict):
            if any(key in candidate for key in ("Url", "URL", "Href", "href", "Uri", "uri")):
                entries.append(candidate)
            else:
                for item in candidate.values():
                    collect(item)
        elif isinstance(candidate, str) and candidate.strip():
            entries.append({"Href": candidate.strip()})

    collect(value)
    subtitles: list[Subtitle] = []
    seen_urls: set[str] = set()
    for entry in entries:
        href = next(
            (
                str(entry.get(key) or "").strip()
                for key in ("Url", "URL", "Href", "href", "Uri", "uri")
                if entry.get(key)
            ),
            "",
        )
        url = urljoin(str(entry.get("Base") or base_url), href)
        if urlparse(url).scheme not in {"http", "https"} or url in seen_urls:
            continue
        seen_urls.add(url)
        raw_language = next(
            (
                str(entry.get(key) or "").strip()
                for key in ("LanguageCode", "Language", "Locale", "lang", "language", "Name")
                if entry.get(key)
            ),
            "en-gb",
        )
        normalised = raw_language.lower().replace("_", "-")
        if normalised in {"english", "en"}:
            normalised = "en-gb"
        normalised = re.sub(r"[^a-z0-9-]+", "-", normalised).strip("-") or "en-gb"
        language = normalised.split("-", 1)[0] or "en"
        label = "en-gb" if language == "en" else normalised
        subtitles.append(
            Subtitle(
                url=url,
                language=language,
                label=label,
                locale=normalised,
            )
        )
    return tuple(subtitles)


def _search_hit(raw: dict[str, Any]) -> SearchHit:
    content_type = str(raw.get("contentType") or "").lower()
    entity_type = str(raw.get("entityType") or "").lower()
    data = raw.get("data") or {}
    if content_type == "live":
        return SearchHit(
            "live",
            str(data.get("brandTitle") or data.get("title") or data.get("channelName") or "Live"),
            channel_id=str(data.get("channelId") or data.get("channelName") or ""),
            synopsis=str(data.get("synopsis") or data.get("description") or ""),
            kind="Live",
        )
    legacy = _legacy_id(data.get("legacyId"))
    special = _legacy_id((data.get("specialProgramme") or {}).get("legacyId"))
    content_id = legacy or special or str(
        data.get("programmeCCId")
        or data.get("filmCCId")
        or data.get("specialCCId")
        or data.get("clipCCId")
        or raw.get("id")
        or ""
    )
    title = str(
        data.get("programmeTitle")
        or data.get("filmTitle")
        or data.get("specialTitle")
        or data.get("title")
        or raw.get("id")
        or "Untitled"
    )
    genres = data.get("genre") or data.get("genres") or []
    genre = ", ".join(str(value) for value in genres) if isinstance(genres, list) else str(genres)
    return SearchHit(
        "vod" if content_type == "vod" and content_id else "unsupported",
        title,
        content_id=content_id,
        synopsis=str(data.get("synopsis") or data.get("description") or ""),
        kind=f"VOD / {entity_type.title() or content_type.upper()}",
        genre=genre,
        episode_count=_int(data.get("totalAvailableEpisodes")) or 0,
    )


def _programme_variables(content_id: str, discovery_features: list[str]) -> dict[str, Any]:
    legacy = not _is_shortform_id(content_id)
    title_legacy = _is_title_legacy_id(content_id)
    brand_filter = _compact(
        {
            "ccid": None if legacy else content_id,
            "legacyId": content_id if legacy else None,
            "features": discovery_features,
            "available": "NOW",
            "broadcaster": "ITV",
            "platform": "CTV",
            "tiers": ["FREE", "PAID"],
        }
    )
    title_filter = _compact(
        {
            "titleTypes": ["EPISODE", "SPECIAL", "FILM"],
            "legacyId": content_id if title_legacy else None,
            "ccid": None if legacy else content_id,
            "brandLegacyId": content_id if legacy and not title_legacy else None,
            "features": discovery_features,
            "available": "NOW",
            "broadcaster": "ITV",
            "platform": "CTV",
            "tiers": ["FREE", "PAID"],
        }
    )
    return {"id": content_id, "brandFilter": brand_filter, "titleFilter": title_filter, "withTitlesToRedirect": True}


def _content_id(value: str) -> str:
    raw = unquote(str(value or "").strip().strip("/"))
    if not raw:
        raise ValueError("empty ITV id")
    if re.fullmatch(r"(?:\d+|[A-Z]+\d*)(?:a[A-Za-z0-9]+)+", raw):
        normalised = raw.replace("a", "/")
    else:
        normalised = raw.replace("_", "/")
    return normalised


def _content_id_candidates(content_id: str) -> tuple[str, ...]:
    without_page_suffix = re.sub(r"(/\d+)[A-Z]$", r"\1", content_id)
    if _is_title_legacy_id(content_id) and without_page_suffix != content_id:
        return content_id, without_page_suffix
    return (content_id,)


def _is_title_legacy_id(content_id: str) -> bool:
    return bool(
        re.fullmatch(r"\d+/\d+/[A-Za-z0-9#]+", content_id or "")
        or re.fullmatch(r"[A-Z]+\d+/\d+[A-Za-z0-9#]*", content_id or "")
    )


def _start_again_url(value: str, starts_at: str) -> str:
    if not value or "{START_TIME}" not in value:
        return value
    return value.replace("{START_TIME}", starts_at) if starts_at else ""


def _protocol(value: str) -> str:
    path = urlparse(value).path.lower()
    if path.endswith(".mpd"):
        return "DASH"
    if path.endswith(".m3u8"):
        return "HLS"
    return "HTTP"


def _compact_graphql(query: str) -> str:
    return re.sub(r"[,\s]+", " ", query.replace("\n", " ")).strip()


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def _legacy_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("officialFormat") or value.get("apiEncoded") or "")
    return str(value or "")


def _jwt(value: str) -> dict[str, Any]:
    try:
        payload = value.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _jwt_expiry(value: str) -> int:
    return _int(_jwt(value).get("exp")) or 0


def _year(*values: Any) -> str:
    for value in values:
        match = re.search(r"\b(19\d{2}|20\d{2}|2100)\b", str(value or ""))
        if match:
            return match.group()
    return ""


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_int(*values: Any) -> int | None:
    for value in values:
        parsed = _int(value)
        if parsed is not None:
            return parsed
    return None


__all__ = [
    "USER_AGENT",
    "WWW",
    "Channel",
    "Episode",
    "ItvApi",
    "ItvError",
    "ParsedInput",
    "SearchHit",
    "Season",
    "Show",
    "Source",
    "Subtitle",
    "TVCode",
    "parse_input",
]
