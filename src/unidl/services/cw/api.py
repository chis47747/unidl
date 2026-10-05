"""The CW - free, ad-supported, US only.

"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

WWW = "https://www.cwtv.com"
DATA = "https://data.cwtv.com"
BRIGHTCOVE = "https://edge.api.brightcove.com/playback/v1/accounts/{account}/videos/{video}"

#: The CW's Brightcove account and the public policy key its player uses. Both are
#: in the page source of every episode; neither is a credential.
ACCOUNT = "6415823816001"
POLICY_KEY = (
    "BCpkADawqM0t2qFXB_K2XdHv2JmeRgQjpP6De9_Fl7d4akhL5aeqYwErorzsAxa7dyOF2FdxuG5wWVOREHEwb0DI"
    "-M8CGBBDpqwvDBEPfDKQg7kYGnccdNDErkvEh2O28CrGR3sEG6MZBlZ03I0xH7EflYKooIhfwvNWWw"
)

USER_AGENT = "Mozilla/5.0 (Linux; Android 11; Smart TV Build/AR2101; wv)"

#: Brightcove's name for each system, next to ours. FairPlay is deliberately
#: absent: it is offered, and nothing here can open it.
KEY_SYSTEMS = {
    "widevine": "com.widevine.alpha",
    "playready": "com.microsoft.playready",
}

#: An anonymous id the search endpoint wants. Any value works; this is the one the
#: web player generates, kept so the request looks like a player's.
CWUID = "8195356001251527455"

_URL = re.compile(
    r"(?:https?://)?(?:www\.)?cwtv\.com/"
    r"(?P<kind>series|shows|movies)/"
    r"(?P<slug>[\w-]+)"
    r"(?:/(?P<extra>[\w-]+))?"
    r"/?(?:\?[^#]*\bplay=(?P<play>[\w-]+))?",
    re.IGNORECASE,
)
#: How The CW links a linear channel, and what its own search results hand out:
#: ``/channels/?channel=<slug>``. The slug is the same one the EPG feed uses.
_CHANNEL_URL = re.compile(
    r"(?:https?://)?(?:www\.)?cwtv\.com/channels/?"
    r"(?:\?[^#]*\bchannel=(?P<query>[\w-]+)|(?P<path>[\w-]+))?",
    re.IGNORECASE,
)
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
#: ``S1 E1 The American Bible Challenge`` - how search labels an episode hit. The
#: numbering is only in this string; the hit carries no ``season`` field.
_SUPER = re.compile(r"^S(?P<season>\d+)\s*E(?P<episode>\d+)\b", re.IGNORECASE)


class CwError(RuntimeError):
    pass


# ------------------------------------------------------------------------ input


@dataclass
class ParsedInput:
    kind: str  # "show" | "movie" | "video" | "channel"
    slug: str = ""
    play_id: str = ""


def parse_input(text: str) -> ParsedInput | None:
    """A cwtv.com URL, or a bare episode guid.

    ``?play=<guid>`` is how The CW links a single episode, and it wins over the
    slug in the same URL: a link that names an episode should open that episode
    rather than the series it belongs to.

    A bare guid is accepted because it is unambiguous - a 36-character UUID is not
    something anybody types by accident - while a bare slug is not, since shows and
    movies share one slug space.

    ``/channels/?channel=<slug>`` is here because it is a third kind of CW link
    rather than a variant of the first two, and it is the one the site's own search
    results carry for a linear channel. Without it a pasted channel link fell
    through to "that does not look like a CW link", which is the wrong answer for
    an address The CW itself produced. Bare ``/channels/`` parses too, with an empty
    slug, which the caller reads as "the channel list" rather than as a bad link.
    """
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


# ----------------------------------------------------------------------- models


@dataclass
class SearchHit:
    """One search result, which is not always a show.

    The endpoint answers with four kinds - ``series``, ``movies``, ``episodes`` and
    ``channels`` - and its own ``groups`` block counts them separately. Only the
    first two are a slug to browse; an episode hit is a guid that plays directly,
    and a channel hit names one of the 75 linear channels. Keeping only the two
    slug kinds threw away most of the answer: ``penn`` returns 90 results, of which
    one is a series and 88 are episodes, movies and a channel.
    """

    kind: str  # show | movie | episode | channel
    slug: str
    name: str
    synopsis: str = ""
    genres: str = ""
    rating: str = ""
    #: An episode hit's guid - the id a ``?play=`` link carries, and what
    #: :meth:`CwApi.video` takes. Empty for the other kinds.
    guid: str = ""
    #: The show an episode hit belongs to. Its own ``name`` is the episode title.
    series: str = ""
    season: int | None = None
    episode: int | None = None
    #: As The CW writes it - ``44mins``. A label, not a number.
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
        # The series first: an episode title on its own ("Episode 1", "October 1st
        # 2024") says nothing about which show answered the search.
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
        """When this drops out of the free window, as ``leaves 1 Sep``.

        The one piece of CW metadata that changes what somebody does next: the
        window is short and an episode listed today is often gone in a fortnight.
        Empty when the feed gave no expiry, which is not the same as "stays".
        """
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
    #: Populated only for an encrypted channel. Empty is the normal state: every
    #: CW channel is currently clear.
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
    #: Brightcove's own runtime in seconds, which is where the legacy script read
    #: it from. The feed's ``duration_secs`` is used first because it arrives with
    #: the listing; this is the fallback for an entry that has none.
    duration: float = 0.0
    #: The captions Brightcove lists beside the manifest, as ``[(language, url)]``.
    #:
    #: Not attached to the ``Playback``: the DASH manifest carries the same track
    #: inline - the same asset id, in a ``text/vtt`` AdaptationSet - so muxing this
    #: as a sidecar would put two identical English subtitles in the output. It is
    #: parsed because it is the cheap way to know a title *has* captions before the
    #: manifest is fetched, and because a Brightcove account that stops listing
    #: them inline would show up here first.
    subtitles: list[tuple[str, str]] = field(default_factory=list)

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


# ----------------------------------------------------------------------- client


class CwApi:
    """The CW's feed API plus the Brightcove playback API behind it."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)

    # -------------------------------------------------------------- catalogue
    def search(self, query: str) -> list[SearchHit]:
        """Everything the endpoint found, in the order it returned it.

        The order is already the useful one - series, channels, movies, episodes -
        so it is left alone rather than re-sorted into a ranking of our own.
        """
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
        """Everything free under one slug, and the series name.

        One call for a series and for a film - the feed does not distinguish, and
        ``series_type`` on each entry is what says which it is. ``fullep`` filters
        out the clips and trailers, which outnumber the episodes.

        ``hint`` is what the URL said - ``movie`` for a ``/movies/`` link - and is
        used only where ``series_type`` is missing. The feed is right today for
        every slug tested, but the URL is the caller's own statement about what it
        asked for, and it was the legacy script's only source for this. Treating a
        film as episode one of season one is a silent wrong answer rather than a
        failure, which is the kind worth guarding.
        """
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
        """One title by the guid a ``?play=`` link carries."""
        data = self._json(
            f"{DATA}/feed/app-2/video-meta/guid_{guid}/apiversion_24/device_androidtv"
        )
        entry = data.get("video") or {}
        if not entry:
            raise CwError(f"The CW has no video with the id {guid}")
        return self._item(entry, str(entry.get("show_slug") or ""))

    @staticmethod
    def group(items: list[Item]) -> list[Season]:
        """Episodes grouped into seasons, newest season first.

        Newest first because The CW's window is always the current season plus
        whatever is left of the previous one, so the interesting end is the top.
        """
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

    # ------------------------------------------------------------------- live
    def channels(self) -> list[Channel]:
        """The free linear channels, with their guide.

        No ``cacheversion`` in the path. The legacy script pinned one, the feed
        answers byte-identically without it, and a frozen cache version is exactly
        the kind of thing that quietly serves last year's line-up.
        """
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
        """One channel by its slug, for a pasted ``/channels/?channel=`` link.

        There is no per-channel endpoint - the EPG feed is the only place the
        stream URLs are - so this is a lookup in the one list. Cheap enough: it is
        the same single request the channel picker makes.
        """
        wanted = (slug or "").strip().lower()
        for found in self.channels():
            if wanted in (found.slug.lower(), found.name.lower()):
                return found
        raise CwError(f"The CW has no channel called {slug}")

    def live_source(self, channel: Channel, system: str = "widevine") -> Source:
        """A channel's stream.

        The DRM branch exists because the feed has the fields for it, and a
        channel that starts using them should not need a code change. It has never
        fired here: every CW channel is currently clear HLS.
        """
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

    # --------------------------------------------------------------- playback
    def source(self, item: Item, system: str = "widevine") -> Source:
        """The playable manifest for one title, from Brightcove.

        DASH only. Brightcove also offers HLS, but only under FairPlay, which is
        not a system this project has - offering it would mean failing at the
        licence request instead of here.

        The ``v1`` manifest is preferred over ``v2``: the ladders are identical and
        v1 carries full codec strings, while v2 is the ``avc1_mp4a``-constrained
        variant whose generic ``codecs="avc1"`` makes a codec filter useless.
        """
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
            # Pinned to the system whose licence server this URL belongs to. The
            # manifest carries init data for both, so leaving it unset would let a
            # Widevine challenge be sent to the PlayReady endpoint.
            system=system if licence else None,
            duration=_seconds(data.get("duration")),
            subtitles=_subtitles(data),
        )

    # --------------------------------------------------------------- plumbing
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

    # -------------------------------------------------------------- licensing
    def widevine_license(
        self, license_url: str, challenge: bytes, headers: dict[str, str] | None = None
    ) -> bytes:
        """The CW's own Widevine exchange: a challenge in, a licence back.

        Here rather than in core on purpose. A licence request is one of a service's
        own calls - its URL, its headers, its own way of refusing - and a shared one
        that posts to whatever URL it was handed cannot say which service was
        refused or why. Core builds the challenge and reads the keys out of the
        answer; sending it is this service's job.
        """
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
        """The same exchange for PlayReady, which sends SOAP and answers XML."""
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



# ---------------------------------------------------------------------- helpers


def _subtitles(data: dict[str, Any]) -> list[tuple[str, str]]:
    """``[(language, url), ...]`` from Brightcove's separate caption list."""
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
    """Brightcove's millisecond runtime, in seconds. ``0.0`` when it gave none."""
    try:
        return round(float(str(value).strip()) / 1000, 3)
    except (TypeError, ValueError):
        return 0.0


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
