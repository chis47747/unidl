"""The CW - free, ad-supported, US only.

"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter

WWW = "https://www.cwtv.com"
DATA = "https://data.cwtv.com"
BRIGHTCOVE = "https://edge.api.brightcove.com/playback/v1/accounts/{account}/videos/{video}"

ACCOUNT = "6415823816001"
POLICY_KEY = (
    "BCpkADawqM0t2qFXB_K2XdHv2JmeRgQjpP6De9_Fl7d4akhL5aeqYwErorzsAxa7dyOF2FdxuG5wWVOREHEwb0DI"
    "-M8CGBBDpqwvDBEPfDKQg7kYGnccdNDErkvEh2O28CrGR3sEG6MZBlZ03I0xH7EflYKooIhfwvNWWw"
)

USER_AGENT = "Mozilla/5.0 (Linux; Android 11; Smart TV Build/AR2101; wv)"

KEY_SYSTEMS = {
    "widevine": "com.widevine.alpha",
    "playready": "com.microsoft.playready",
}

CWUID = "8195356001251527455"

_URL = re.compile(
    r"(?:https?://)?(?:www\.)?cwtv\.com/"
    r"(?P<kind>series|shows|movies)/"
    r"(?P<slug>[\w-]+)"
    r"(?:/(?P<extra>[\w-]+))?"
    r"/?(?:\?[^#]*\bplay=(?P<play>[\w-]+))?",
    re.IGNORECASE,
)

_CHANNEL_URL = re.compile(
    r"(?:https?://)?(?:www\.)?cwtv\.com/channels/?"
    r"(?:\?[^#]*\bchannel=(?P<query>[\w-]+)|(?P<path>[\w-]+))?",
    re.IGNORECASE,
)
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

_SUPER = re.compile(r"^S(?P<season>\d+)\s*E(?P<episode>\d+)\b", re.IGNORECASE)


class CwError(RuntimeError):
    pass



@dataclass
class ParsedInput:
    kind: str  # "show" | "movie" | "video" | "channel"
    slug: str = ""
    play_id: str = ""


def parse_input(text: str) -> ParsedInput | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if _GUID.match(raw):
        return ParsedInput("video", play_id=raw.lower())
    channel = _CHANNEL_URL.search(raw)
    if channel:
        return ParsedInput(
            "channel", slug=(channel.group("query") or channel.group("path") or "").lower()
        )
    match = _URL.search(raw)
    if not match:
        return None
    play = match.group("play")
    if play:
        return ParsedInput("video", slug=match.group("slug"), play_id=play)
    kind = "movie" if match.group("kind").lower() == "movies" else "show"
    return ParsedInput(kind, slug=match.group("slug"))


@dataclass
class SearchHit:
    kind: str  # show | movie | episode | channel
    slug: str
    name: str
    synopsis: str = ""
    genres: str = ""
    rating: str = ""
    guid: str = ""
    series: str = ""
    season: int | None = None
    episode: int | None = None
    duration: str = ""

    @property
    def url(self) -> str:
        if self.kind == "channel":
            return f"{WWW}/channels/?channel={self.slug}"
        if self.kind == "episode":
            return f"{WWW}/shows/{self.slug}/?play={self.guid}"
        return f"{WWW}/{'movies' if self.kind == 'movie' else 'shows'}/{self.slug}"

    @property
    def label(self) -> str:
        if self.kind != "episode":
            return self.name
        head = (
            f"S{self.season:02d}E{self.episode:02d}"
            if self.season and self.episode
            else ""
        )
        return "  ".join(part for part in (self.series, head, self.name) if part)


@dataclass
class Item:
    """One playable title - an episode or a film."""

    video_id: str  # the Brightcove id, which is what plays
    guid: str  # the CW's own id, which is what a ?play= link carries
    name: str
    series: str = ""
    season: int | None = None
    episode: int | None = None
    year: str = ""
    synopsis: str = ""
    duration: int = 0
    kind: str = "episode"  # episode | movie
    encrypted: bool = True
    rating: str = ""
    expires: str = ""
    genre: str = ""
    series_slug: str = ""

    @property
    def label(self) -> str:
        if self.kind == "episode" and self.season and self.episode:
            return f"S{self.season:02d}E{self.episode:02d}  {self.name}"
        return f"{self.name} ({self.year})" if self.year else self.name

    @property
    def leaves(self) -> str:
        when = _time(self.expires)
        return f"leaves {when.strftime('%-d %b')}" if when else ""

    @property
    def url(self) -> str:
        if not self.guid:
            return ""
        kind = "movies" if self.kind == "movie" else "shows"
        return f"{WWW}/{kind}/{self.series_slug}/?play={self.guid}"


@dataclass
class Season:
    index: int
    episodes: list[Item] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"Season {self.index}  ·  {len(self.episodes)} episode(s)"


@dataclass
class Program:
    """One EPG entry on a live channel."""

    title: str
    subtitle: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @property
    def label(self) -> str:
        return f"{self.title}: {self.subtitle}" if self.subtitle else self.title


@dataclass
class Channel:
    slug: str
    name: str
    synopsis: str = ""
    genre: str = ""
    stream_url: str = ""
    stream_type: str = "hls"
    encrypted: bool = False
    media_items: list[dict[str, Any]] = field(default_factory=list)
    programs: list[Program] = field(default_factory=list)

    @property
    def on_now(self) -> str:
        now = datetime.now(timezone.utc)
        for program in self.programs:
            if program.starts_at and program.ends_at and program.starts_at <= now < program.ends_at:
                return program.label
        return self.programs[0].label if self.programs else ""

    def line(self) -> str:
        return f"{self.stream_type.upper()}  {'drm' if self.encrypted else 'clear'}"


@dataclass
class Source:
    manifest: str
    protocol: str = "DASH"
    license_url: str = ""
    system: str | None = None
    is_live: bool = False
    duration: float = 0.0
    subtitles: list[tuple[str, str]] = field(default_factory=list)
    chapters: tuple[Chapter, ...] = ()
    attachments: tuple[Attachment, ...] = ()

    @property
    def encrypted(self) -> bool:
        return bool(self.license_url)

    def line(self) -> str:
        bits = [self.protocol, f"drm {self.system}" if self.encrypted else "clear"]
        if self.is_live:
            bits.append("live")
        if self.subtitles:
            bits.append(f"{len(self.subtitles)} subtitle track(s)")
        return "  ".join(bits)


class CwApi:

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)

    def search(self, query: str) -> list[SearchHit]:
        data = self._json(
            f"{WWW}/search/",
            params={"q": query, "format": "json2", "service": "t", "cwuid": CWUID},
        )
        found: list[SearchHit] = []
        for entry in data.get("items") or []:
            hit = self._hit(entry)
            if hit is not None:
                found.append(hit)
        return found

    @staticmethod
    def _hit(entry: dict[str, Any]) -> SearchHit | None:
        kind = str(entry.get("type") or "").lower()
        synopsis = str(entry.get("description_long") or "")
        genres = str(entry.get("genres") or "")
        rating = str(entry.get("rating") or "")
        title = str(entry.get("title") or entry.get("show_title") or "Unknown")
        if kind == "channels":
            # A channel hit carries its EPG slug in ``slug``, not ``show_slug``.
            slug = str(entry.get("slug") or entry.get("guid") or "")
            return (
                SearchHit("channel", slug, title, synopsis, genres, rating)
                if slug
                else None
            )
        slug = str(entry.get("show_slug") or "")
        if not slug:
            return None
        if kind == "episodes":
            guid = str(entry.get("guid") or "")
            if not guid:
                return None
            numbering = _SUPER.match(str(entry.get("super_title") or ""))
            return SearchHit(
                kind="episode",
                slug=slug,
                name=title,
                synopsis=synopsis,
                genres=genres,
                rating=rating,
                guid=guid,
                series=str(entry.get("show_title") or entry.get("series_name") or ""),
                season=_int(numbering.group("season")) if numbering else None,
                episode=_int(numbering.group("episode")) if numbering else None,
                duration=str(entry.get("duration") or ""),
            )
        if kind not in ("shows", "series", "movies"):
            return None
        return SearchHit(
            kind="movie" if kind == "movies" else "show",
            slug=slug,
            name=title,
            synopsis=synopsis,
            genres=genres,
            rating=rating,
            duration=str(entry.get("duration") or ""),
        )

    def catalogue(self, slug: str, hint: str = "") -> tuple[str, list[Item]]:
        data = self._json(
            f"{DATA}/feed/app-2/videos/show_{slug}/type_episodes"
            "/apiversion_24/device_androidtv"
        )
        entries = data.get("items") or []
        if not entries:
            raise CwError(
                f"The CW has nothing free under {slug}. It keeps only a rolling "
                "window of recent episodes, so a show between seasons is empty."
            )
        found = [
            self._item(entry, slug, hint) for entry in entries if entry.get("fullep") == 1
        ]
        if not found:
            raise CwError(f"{slug} has clips but no full episodes right now")
        series = found[0].series or slug
        return series, found

    def video(self, guid: str) -> Item:
        data = self._json(
            f"{DATA}/feed/app-2/video-meta/guid_{guid}/apiversion_24/device_androidtv"
        )
        entry = data.get("video") or {}
        if not entry:
            raise CwError(f"The CW has no video with the id {guid}")
        return self._item(entry, str(entry.get("show_slug") or ""))

    @staticmethod
    def group(items: list[Item]) -> list[Season]:
        seasons: dict[int, Season] = {}
        for item in items:
            index = item.season or 0
            seasons.setdefault(index, Season(index=index)).episodes.append(item)
        for season in seasons.values():
            season.episodes.sort(key=lambda entry: entry.episode or 0)
        return sorted(seasons.values(), key=lambda season: season.index, reverse=True)

    def _item(self, entry: dict[str, Any], slug: str, hint: str = "") -> Item:
        series_type = str(entry.get("series_type") or "").lower()
        if not series_type:
            series_type = "movie" if hint == "movie" else "series"
        return Item(
            video_id=str(entry.get("bc_video_id") or ""),
            guid=str(entry.get("guid") or ""),
            name=str(entry.get("title") or "Untitled"),
            series=str(entry.get("series_name") or entry.get("show_title") or ""),
            season=_int(entry.get("season")),
            episode=_int(entry.get("episode_in_season")),
            year=str(entry.get("release_year") or ""),
            synopsis=str(entry.get("description_long") or ""),
            duration=_int(entry.get("duration_secs")) or 0,
            kind="movie" if series_type == "movie" else "episode",
            # The feed's own flag, used only as the initial guess: what Brightcove
            # actually serves is what decides, and the two do disagree.
            encrypted=bool(entry.get("has_drm") or entry.get("bc_drm")),
            rating=str(entry.get("rating") or ""),
            expires=str(entry.get("expire_time") or ""),
            genre=str(entry.get("imdb_genres") or entry.get("comscore_genre") or ""),
            series_slug=str(entry.get("show_slug") or slug),
        )

    def channels(self) -> list[Channel]:
        data = self._json(
            f"{DATA}/feed/app-2/landing/epg/page_1/pagesize_75/device_web/apiversion_24"
        )
        found: list[Channel] = []
        for entry in data.get("channels") or []:
            stream = str(entry.get("stream_url") or "")
            if not stream:
                continue
            found.append(
                Channel(
                    slug=str(entry.get("slug") or entry.get("channel_slug") or ""),
                    name=str(entry.get("title") or "Channel"),
                    synopsis=str(entry.get("description") or ""),
                    genre=str(entry.get("genre") or ""),
                    stream_url=stream,
                    stream_type=str(entry.get("stream_type") or "hls").lower(),
                    encrypted=bool(entry.get("has_drm")),
                    media_items=list(entry.get("drm_media_items") or []),
                    programs=[_program(p) for p in (entry.get("programs") or [])],
                )
            )
        return found

    def channel(self, slug: str) -> Channel:
        wanted = (slug or "").strip().lower()
        for found in self.channels():
            if wanted in (found.slug.lower(), found.name.lower()):
                return found
        raise CwError(f"The CW has no channel called {slug}")

    def live_source(self, channel: Channel, system: str = "widevine") -> Source:
        if not channel.encrypted:
            return Source(
                manifest=channel.stream_url,
                protocol=channel.stream_type.upper(),
                is_live=True,
            )
        for media in channel.media_items:
            url = str(media.get("url") or media.get("stream_url") or "")
            licence = str(
                (media.get("drm") or {}).get(KEY_SYSTEMS.get(system, ""), "")
                or media.get(f"{system}_license_url")
                or media.get("license_url")
                or ""
            )
            if url and licence:
                return Source(
                    manifest=url,
                    protocol=str(media.get("stream_type") or "dash").upper(),
                    license_url=licence,
                    system=system,
                    is_live=True,
                )
        raise CwError(
            f"{channel.name} is marked DRM-protected but offers no {system} stream"
        )

    def source(self, item: Item, system: str = "widevine") -> Source:
        if not item.video_id:
            raise CwError(f"{item.name} has no Brightcove id, so there is nothing to play")
        data = self._json(
            BRIGHTCOVE.format(account=ACCOUNT, video=item.video_id),
            headers={"accept": f"application/json;pk={POLICY_KEY}"},
        )
        wanted = KEY_SYSTEMS.get(system, KEY_SYSTEMS["widevine"])
        dash = [
            source
            for source in (data.get("sources") or [])
            if str(source.get("type") or "").lower() == "application/dash+xml"
            and source.get("src")
        ]
        if not dash:
            raise CwError(
                f"Brightcove offered no DASH for {item.name}"
                + (" - only HLS with FairPlay, which cannot be opened here"
                   if data.get("sources") else "")
            )
        dash.sort(key=lambda source: "/v2/" in str(source.get("src") or ""))
        chosen = next(
            (source for source in dash if (source.get("key_systems") or {}).get(wanted)),
            dash[0],
        )
        key_systems = chosen.get("key_systems") or {}
        licence = str((key_systems.get(wanted) or {}).get("license_url") or "")
        if key_systems and not licence:
            offered = sorted(key_systems)
            raise CwError(
                f"{item.name} is not offered under {system}; Brightcove has "
                f"{', '.join(offered)}"
            )
        return Source(
            manifest=str(chosen["src"]),
            protocol="DASH",
            license_url=licence,
            system=system if licence else None,
            duration=_seconds(data.get("duration")),
            subtitles=_subtitles(data),
            chapters=_chapters(data),
            attachments=_attachments(data),
        )

    def _json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self.session.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException as exc:
            raise CwError(f"could not reach The CW: {exc}") from exc
        if response.status_code == 403:
            raise CwError(
                "The CW refused that. It is US-only, and its feed hosts block "
                "some datacentre ranges outright."
            )
        if response.status_code == 404:
            raise CwError("The CW has nothing at that address")
        if response.status_code >= 400:
            raise CwError(f"The CW returned {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise CwError("The CW answered with something that is not JSON") from exc
        return data if isinstance(data, dict) else {}

    def widevine_license(
        self, license_url: str, challenge: bytes, headers: dict[str, str] | None = None
    ) -> bytes:
        if not license_url:
            raise CwError("no The CW licence URL came with this playback")
        request_headers = {"Content-Type": "application/octet-stream"}
        request_headers.update(headers or {})
        try:
            response = self.session.post(
                license_url, data=challenge, headers=request_headers, timeout=30
            )
        except requests.RequestException as exc:
            raise CwError(f"could not reach The CW's licence server: {exc}") from exc
        if not response.ok:
            raise CwError(
                f"The CW's licence server answered {response.status_code}: "
                f"{str(response.text)[:200]}"
            )
        return response.content

    def playready_license(
        self, license_url: str, challenge: str, headers: dict[str, str] | None = None
    ) -> str:
        if not license_url:
            raise CwError("no The CW licence URL came with this playback")
        request_headers = {"Content-Type": "text/xml; charset=utf-8"}
        request_headers.update(headers or {})
        try:
            response = self.session.post(
                license_url,
                data=challenge.encode("utf-8"),
                headers=request_headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise CwError(f"could not reach The CW's licence server: {exc}") from exc
        if not response.ok:
            raise CwError(
                f"The CW's licence server answered {response.status_code}: "
                f"{str(response.text)[:200]}"
            )
        return response.text


def _subtitles(data: dict[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for track in data.get("text_tracks") or []:
        url = str(track.get("src") or "")
        if url and str(track.get("kind") or "captions") in ("captions", "subtitles"):
            found.append((str(track.get("srclang") or "und"), url))
    return found


def _program(entry: dict[str, Any]) -> Program:
    return Program(
        title=str(entry.get("title") or ""),
        subtitle=str(entry.get("subtitle") or ""),
        starts_at=_time(entry.get("start_time")),
        ends_at=_time(entry.get("end_time")),
    )


def _time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _seconds(value: Any) -> float:
    try:
        return round(float(str(value).strip()) / 1000, 3)
    except (TypeError, ValueError):
        return 0.0


def _chapter_seconds(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number / 1000.0 if number >= 100_000 else number


def _chapters(data: dict[str, Any]) -> tuple[Chapter, ...]:
    rows: list[Any] = []
    for key in ("chapters", "cue_points", "cuePoints", "markers"):
        value = data.get(key)
        if isinstance(value, list):
            rows.extend(value)
    found: list[Chapter] = []
    seen: set[tuple[int, str, str]] = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        start = next(
            (
                _chapter_seconds(row.get(key))
                for key in ("time", "start", "start_time", "offset")
                if row.get(key) not in (None, "")
            ),
            None,
        )
        if start is None:
            continue
        end = next(
            (
                _chapter_seconds(row.get(key))
                for key in ("end", "end_time")
                if row.get(key) not in (None, "")
            ),
            None,
        )
        title = str(row.get("name") or row.get("title") or row.get("label") or f"Chapter {index}")
        kind = str(row.get("type") or row.get("kind") or "").strip().lower()
        try:
            chapter = Chapter.from_seconds(start, title, end, kind=kind)
        except ValueError:
            continue
        identity = (chapter.start_ms, chapter.title, chapter.kind)
        if identity not in seen:
            seen.add(identity)
            found.append(chapter)
    return tuple(found)


def _image_urls(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip().startswith(("https://", "http://")) else []
    if isinstance(value, dict):
        direct = value.get("url") or value.get("src")
        if direct:
            return _image_urls(direct)
        urls: list[str] = []
        for child in value.values():
            urls.extend(_image_urls(child))
        return urls
    if isinstance(value, list):
        urls: list[str] = []
        for child in value:
            urls.extend(_image_urls(child))
        return urls
    return []


def _attachments(data: dict[str, Any]) -> tuple[Attachment, ...]:
    fields = (
        ("poster", "Poster", "poster"),
        ("poster_url", "Poster", "poster"),
        ("thumbnail", "Thumbnail", "thumbnail"),
        ("thumbnail_url", "Thumbnail", "thumbnail"),
        ("images", "Artwork", "artwork"),
    )
    found: list[Attachment] = []
    seen: set[str] = set()
    for key, name, kind in fields:
        for url in _image_urls(data.get(key)):
            if url in seen:
                continue
            seen.add(url)
            found.append(Attachment(url, name=name, kind=kind, mime_type="image/jpeg"))
    return tuple(found)


__all__ = [
    "ACCOUNT",
    "BRIGHTCOVE",
    "DATA",
    "KEY_SYSTEMS",
    "POLICY_KEY",
    "USER_AGENT",
    "WWW",
    "Channel",
    "CwApi",
    "CwError",
    "Item",
    "ParsedInput",
    "Program",
    "SearchHit",
    "Season",
    "Source",
    "parse_input",
]
