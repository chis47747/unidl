"""ABC iView (Australia) - free AU catalogue, search, live and KeyOS Widevine.

"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode, urljoin

import requests

APP_VERSION = "2026.04.18056-tv"
USER_AGENT = f"ABC iview/{APP_VERSION} (Android TV; au.net.abc.iview)"

BASE_URL = "https://api.iview.abc.net.au"
PROFILE_BASE_URL = "https://mylogin-api.abc.net.au"
WWW = "https://iview.abc.net.au"
LICENSE_URL = "https://widevine.keyos.com/api/v4/getLicense"

#: Public app client id (not a user secret). Same value the Android TV app posts.
CLIENT_ID = "88a12649-f212-4ac8-b9c9-044d8c8b0b21"

ALGOLIA_APP_ID = "Y63Q32NVDL"
ALGOLIA_API_KEY = "2626fc5cbe4fa06b409eada8be9b16a3"
ALGOLIA_INDEX = "ABC_production_iview_TV"
ALGOLIA_USER_TOKEN = "ABCIVIEW_ANDROID_USER"
ALGOLIA_SEARCH = "https://y63q32nvdl-dsn.algolia.net/1/indexes/*/queries"

PROFILE_STATUS = "/latest/status"
SHOW_INFO = "/v3/show/{show_id}?embed=seriesList,selectedSeries,highlightVideo"
VIDEO_INFO = "/v3/video/{video_id}?include=episodeNumber,seriesNumber"
LIVE_CATEGORY = "/v3/category/watch-live?spotlight=true"
JWT_TOKEN = "/v3/token/jwt"
DRM_TOKEN = "/v3/token/drm/{video_id}"

STREAM_TYPE_PRIORITY = ("mpegdash-h265-cbcs", "mpegdash", "hls-latest")
QUALITY_PRIORITY = ("1080", "720", "sd", "sd-low")

HTTP_TIMEOUT = 30
#: Refresh JWT this many seconds before ``exp``. Matches the legacy client.
JWT_MIN_TTL = 300

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


class AbcIviewError(RuntimeError):
    """ABC iView refused a request or returned an unusable shape."""


# ------------------------------------------------------------------------ input


@dataclass(frozen=True)
class ParsedInput:
    kind: str  # show | video
    value: str


def parse_input(text: str) -> ParsedInput | None:
    """A show or video URL on iview.abc.net.au, or a bare content id."""
    raw = (text or "").strip()
    if not raw:
        return None
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


# ----------------------------------------------------------------------- models


@dataclass(frozen=True)
class SearchHit:
    id: str
    title: str
    kind: str  # show | video | unknown
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
    """One playable stream, reduced to what a Playback needs."""

    manifest: str
    protected: bool = False
    customdata: str = ""
    video_id: str = ""
    stream_type: str = ""
    playlist_type: str = ""
    is_live: bool = False

    def line(self) -> str:
        bits = [self.stream_type or "stream"]
        bits.append("Widevine" if self.protected else "clear")
        if self.is_live:
            bits.append("live")
        return " · ".join(bits)


# ----------------------------------------------------------------------- client


class AbcIviewApi:
    """One anonymous session against ABC iView's public TV API."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.session.headers.setdefault("accept", "application/json")
        self.session.headers.setdefault("accept-language", "en-US,en;q=0.8")
        self.jwt_token: str | None = None
        self.jwt_expiry: int = 0
        self.profile_status: dict[str, Any] | None = None

    # ---------------------------------------------------------------- session
    def anonymous_login(self) -> dict[str, Any]:
        """Optional profile-status probe; the free catalogue does not need it."""
        self.profile_status = self._json(
            "GET", PROFILE_STATUS, base_url=PROFILE_BASE_URL
        )
        return self.profile_status

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

    # ----------------------------------------------------------------- search
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
        return found

    # -------------------------------------------------------------- catalogue
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

    # --------------------------------------------------------------- playback
    def get_source(self, video_id: str) -> Source:
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
        stream_type = next(
            (name for name in STREAM_TYPE_PRIORITY if name in stream_groups),
            None,
        )
        if not stream_type:
            raise AbcIviewError("Could not find a supported stream type")

        streams = stream_groups.get(stream_type) or {}
        if not isinstance(streams, dict):
            raise AbcIviewError("Stream group had an unexpected shape")

        manifest = next(
            (streams[key] for key in QUALITY_PRIORITY if streams.get(key)),
            None,
        )
        if not manifest:
            raise AbcIviewError("Could not find a working manifest")

        protected = bool(streams.get("protected", False))
        customdata = ""
        if protected:
            customdata = self.get_license_customdata(video_id)

        is_live = (
            video.get("type") == "livestream"
            or playlist_item.get("type") == "livestream"
        )
        return Source(
            manifest=str(manifest),
            protected=protected,
            customdata=customdata,
            video_id=video_id,
            stream_type=stream_type,
            playlist_type=str(playlist_item.get("type") or ""),
            is_live=bool(is_live),
        )

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

    # -------------------------------------------------------------- licensing
    def widevine_license(
        self,
        challenge: bytes,
        *,
        customdata: str,
        license_url: str = LICENSE_URL,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """KeyOS Widevine: raw challenge in, licence bytes back.

        The authentication is the ``x-keyos-authorization`` header whose value is
        the customdata string from :meth:`get_license_customdata`.
        """
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

    # --------------------------------------------------------------- plumbing
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


# ---------------------------------------------------------------------- helpers


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


def _jwt_expiry(token: str) -> int:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
        return int(data.get("exp") or 0)
    except Exception:
        return 0


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _preview(text: str, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


__all__ = [
    "ALGOLIA_API_KEY",
    "ALGOLIA_APP_ID",
    "ALGOLIA_INDEX",
    "BASE_URL",
    "CLIENT_ID",
    "LICENSE_URL",
    "QUALITY_PRIORITY",
    "STREAM_TYPE_PRIORITY",
    "USER_AGENT",
    "WWW",
    "AbcIviewApi",
    "AbcIviewError",
    "Episode",
    "ParsedInput",
    "SearchHit",
    "Show",
    "Source",
    "parse_episode",
    "parse_input",
]
