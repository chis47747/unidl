"""U (formerly UKTV Play) catalogue and Brightcove playback.

The catalogue is a small public UKTV API. It returns brands, series and
episodes; each episode carries the Brightcove video id that actually plays.
Brightcove then supplies a Widevine DASH manifest and licence URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

WWW = "https://u.co.uk"
CATALOGUE = "https://vschedules.uktv.co.uk/vod"
BRIGHTCOVE = "https://edge.api.brightcove.com/playback/v1/accounts/1242911124001/videos/{video}"

# Public policy key used by U's Android player. It authorises reads from this
# Brightcove account; it is not an account credential.
POLICY_KEY = (
    "BCpkADawqM2ZEz-kf0i2xEP9VuhJF_DB5boH7YAeSx5EHDSNFFl4QUoHZ3bKLQ9yWboSOBNyvZKm4HiZrqMNRxXm-"
    "laTAnmls1QOL7_kUM3Eij4KjQMz0epMs3WIedg64fnRxQTX6XubGE9p"
)
CATALOGUE_USER_AGENT = "okhttp/4.7.2"
USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 12; SM-A226B Build/SP1A.210812.016)"
BRIGHTCOVE_HEADERS = {
    "BCOV-POLICY": POLICY_KEY,
    "User-Agent": USER_AGENT,
    "Host": "edge.api.brightcove.com",
    "Connection": "keep-alive",
}

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$", re.I)


class UError(RuntimeError):
    """The U API rejected a request or returned an unusable shape."""


@dataclass(frozen=True)
class ParsedInput:
    slug: str
    video_id: str = ""


def parse_input(text: str) -> ParsedInput | None:
    """An HTTP(S) u.co.uk show URL or an unambiguous bare show slug."""
    raw = (text or "").strip()
    if not raw:
        return None
    if _SLUG.fullmatch(raw):
        return ParsedInput(raw.lower())

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in {"u.co.uk", "www.u.co.uk"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] != "shows":
        return None
    slug = parts[1]
    if not _SLUG.fullmatch(slug):
        return None

    # The source's direct episode shape is
    # /shows/<slug>/<page>/<series>/<brightcove-id>.
    tail = parts[2:]
    video_id = ""
    if len(tail) >= 3 and re.fullmatch(r"[0-9-]+", tail[2]):
        video_id = tail[2]
    return ParsedInput(slug.lower(), video_id)


@dataclass(frozen=True)
class SearchHit:
    slug: str
    name: str
    kind: str = "series"
    synopsis: str = ""
    channel: str = ""
    episode_count: int = 0

    @property
    def url(self) -> str:
        return f"{WWW}/shows/{self.slug}/watch-online"


@dataclass(frozen=True)
class Episode:
    video_id: str
    name: str
    brand: str
    brand_slug: str
    season: int | None = None
    number: int | None = None
    synopsis: str = ""
    duration: int = 0
    channel: str = ""
    feature: bool = False

    @property
    def label(self) -> str:
        if not self.feature and self.season and self.number:
            return f"S{self.season:02d}E{self.number:02d}  {self.name}"
        return self.name or self.brand


@dataclass
class Season:
    id: str
    number: int | None = None
    episodes: list[Episode] = field(default_factory=list)

    @property
    def label(self) -> str:
        name = f"Season {self.number}" if self.number is not None else "Episodes"
        return f"{name}  ·  {len(self.episodes)} episode(s)" if self.episodes else name


@dataclass
class Show:
    slug: str
    name: str
    synopsis: str = ""
    channel: str = ""
    seasons: list[Season] = field(default_factory=list)


@dataclass(frozen=True)
class Source:
    manifest: str
    license_url: str = ""
    protocol: str = "DASH"
    encrypted: bool = True
    subtitles: tuple[str, ...] = ()

    def line(self) -> str:
        drm = "Widevine" if self.encrypted else "clear"
        subs = f" · {len(self.subtitles)} subtitle(s)" if self.subtitles else ""
        return f"{self.protocol} · {drm}{subs}"


class UApi:
    def __init__(self, session: requests.Session | None = None):
        self.session = session if session is not None else requests.Session()
        self.session.headers.setdefault("User-Agent", CATALOGUE_USER_AGENT)

    # ---------------------------------------------------------------- input
    def search(self, query: str) -> list[SearchHit]:
        data = self._json(f"{CATALOGUE}/search/", params={"q": query})
        if not isinstance(data, list):
            raise UError("U search returned something that is not a list")
        hits: list[SearchHit] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or "").strip()
            name = str(item.get("name") or "").strip()
            if not slug or not name:
                continue
            hits.append(
                SearchHit(
                    slug=slug,
                    name=name,
                    kind=str(item.get("type") or "series").lower(),
                    synopsis=str(item.get("synopsis") or ""),
                    channel=str(item.get("channel") or ""),
                    episode_count=_int(item.get("ep_count")) or 0,
                )
            )
        return hits

    def show(self, slug: str) -> Show:
        data = self._json(f"{CATALOGUE}/brand/", params={"slug": slug})
        if not isinstance(data, dict):
            raise UError(f"U returned no show for {slug}")
        name = str(data.get("name") or "").strip()
        if not name:
            raise UError(f"U returned no show for {slug}")

        seasons: list[Season] = []
        for entry in data.get("series") or []:
            if not isinstance(entry, dict) or entry.get("id") in (None, ""):
                continue
            seasons.append(
                Season(
                    id=str(entry["id"]),
                    number=_int(entry.get("series_number")) or _int(entry.get("number")),
                )
            )
        seasons.sort(key=lambda season: season.number if season.number is not None else 10_000)
        return Show(
            slug=slug,
            name=name,
            synopsis=str(data.get("synopsis_long") or data.get("description") or ""),
            channel=str(data.get("channel") or ""),
            seasons=seasons,
        )

    def season(self, entry: Season) -> Season:
        data = self._json(f"{CATALOGUE}/series/", params={"id": entry.id})
        if not isinstance(data, dict):
            raise UError(f"U returned no season for {entry.id}")
        number = _int(data.get("series_number")) or _int(data.get("number"))
        brand_data = data.get("brand") if isinstance(data.get("brand"), dict) else {}
        episodes = [
            episode
            for raw in data.get("episodes") or []
            if isinstance(raw, dict) and (episode := self._episode(raw, brand_data)) is not None
        ]
        episodes.sort(key=lambda item: (item.number is None, item.number or 0, item.name))
        return Season(id=entry.id, number=number if number is not None else entry.number, episodes=episodes)

    def catalogue(self, slug: str) -> Show:
        show = self.show(slug)
        show.seasons = [self.season(entry) for entry in show.seasons]
        show.seasons = [entry for entry in show.seasons if entry.episodes]
        if not show.seasons:
            raise UError(f"U returned no playable episodes for {show.name}")
        return show

    def find_episode(self, slug: str, video_id: str) -> tuple[Show, Episode]:
        show = self.catalogue(slug)
        for season in show.seasons:
            for episode in season.episodes:
                if episode.video_id == str(video_id):
                    return show, episode
        raise UError(f"U did not list video {video_id} under {show.name}")

    # -------------------------------------------------------------- playback
    def source(self, video_id: str) -> Source:
        data = self._json(
            BRIGHTCOVE.format(video=video_id),
            headers=BRIGHTCOVE_HEADERS,
        )
        if not isinstance(data, dict):
            raise UError(f"Brightcove returned no playback for {video_id}")

        for raw in data.get("sources") or []:
            if not isinstance(raw, dict) or not raw.get("src"):
                continue
            systems = raw.get("key_systems") if isinstance(raw.get("key_systems"), dict) else {}
            widevine = systems.get("com.widevine.alpha") if isinstance(systems, dict) else None
            if not isinstance(widevine, dict):
                continue
            license_url = str(widevine.get("license_url") or "")
            if not license_url:
                raise UError(f"Brightcove Widevine source has no licence URL for {video_id}")
            return Source(
                manifest=str(raw["src"]),
                license_url=license_url,
                encrypted=True,
                subtitles=self._subtitle_languages(data),
            )

        raise UError(f"Brightcove offered no Widevine source for {video_id}")

    # --------------------------------------------------------------- helpers
    def _json(self, url: str, **kwargs: Any) -> Any:
        try:
            response = self.session.get(url, timeout=25, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            detail = ""
            response = getattr(exc, "response", None)
            if response is not None:
                detail = (response.text or "").strip().replace("\n", " ")[:180]
            raise UError(f"U request failed: {exc}{f' - {detail}' if detail else ''}") from exc
        except ValueError as exc:
            raise UError("U returned something that is not JSON") from exc

    @staticmethod
    def _episode(raw: dict[str, Any], brand_data: dict[str, Any]) -> Episode | None:
        video_id = str(raw.get("video_id") or "").strip()
        if not video_id:
            return None
        feature = bool(raw.get("is_feature") or raw.get("brand_is_feature"))
        brand = str(raw.get("brand_name") or brand_data.get("name") or "U")
        return Episode(
            video_id=video_id,
            name=str(raw.get("name") or brand),
            brand=brand,
            brand_slug=str(raw.get("brand_slug") or brand_data.get("slug") or ""),
            season=_int(raw.get("series_number")),
            number=_int(raw.get("episode_number")),
            synopsis=str(raw.get("synopsis") or raw.get("synopsis_short") or ""),
            duration=_int(raw.get("content_duration")) or 0,
            channel=str(raw.get("channel") or brand_data.get("channel") or ""),
            feature=feature,
        )

    @staticmethod
    def _subtitle_languages(data: dict[str, Any]) -> tuple[str, ...]:
        languages: list[str] = []
        for track in data.get("text_tracks") or []:
            if not isinstance(track, dict):
                continue
            language = str(track.get("srclang") or track.get("label") or "").strip()
            if language and language not in languages:
                languages.append(language)
        return tuple(languages)

    # -------------------------------------------------------------- licensing
    def widevine_license(
        self, license_url: str, challenge: bytes, headers: dict[str, str] | None = None
    ) -> bytes:
        """U's own Widevine exchange: a challenge in, a licence back.

        Here rather than in core on purpose. A licence request is one of a service's
        own calls - its URL, its headers, its own way of refusing - and a shared one
        that posts to whatever URL it was handed cannot say which service was
        refused or why. Core builds the challenge and reads the keys out of the
        answer; sending it is this service's job.
        """
        if not license_url:
            raise UError("no U licence URL came with this playback")
        request_headers = {"Content-Type": "application/octet-stream"}
        request_headers.update(headers or {})
        try:
            response = self.session.post(
                license_url, data=challenge, headers=request_headers, timeout=30
            )
        except requests.RequestException as exc:
            raise UError(f"could not reach U's licence server: {exc}") from exc
        if not response.ok:
            raise UError(
                f"U's licence server answered {response.status_code}: "
                f"{str(response.text)[:200]}"
            )
        body = response.content or b""
        if not body:
            raise UError("U's licence server returned an empty body")
        return body



def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BRIGHTCOVE",
    "BRIGHTCOVE_HEADERS",
    "CATALOGUE",
    "CATALOGUE_USER_AGENT",
    "POLICY_KEY",
    "USER_AGENT",
    "WWW",
    "Episode",
    "ParsedInput",
    "SearchHit",
    "Season",
    "Show",
    "Source",
    "UApi",
    "UError",
    "parse_input",
]
