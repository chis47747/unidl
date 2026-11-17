"""Channel 5 Android TV 4.22.6 catalogue, pairing and Cassie playback."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlsplit, urlunparse

import requests

from ...core.chapters import Chapter

WWW = "https://www.channel5.com"
CORONA = "https://corona.channel5.com"
EDNA = "https://feeds-api.channel5.com"
CASSIE = "https://cassie-auth.channel5.com/api/v2"
APP_VERSION = "4.22.6"
CONFIG_URL = "https://cassie.channel5.com/platform/my5androidtv/config.json"
USER_SERVICE = "https://userservice-api.channel5.com"
ACTIVATE_URL = "https://activate.channel5.com/"
PLATFORM = "androidtv"
CASSIE_PLATFORM = "My5AndroidTVHydra"
USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 14; Android TV)"

CassieFetch = Callable[[str, bool], dict[str, Any]]

_WATCHABLE = re.compile(r"^C\d{6,}$", re.I)
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*", re.I | re.ASCII)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


class Channel5Error(RuntimeError):
    pass


class PairingUnavailable(Channel5Error):
    """The pairing request can be retried without issuing a new PIN."""


@dataclass(frozen=True)
class TVCode:
    pin: str
    secret: str = field(repr=False)
    qr_url: str = ""


def bearer_token(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._~+/\-]+=*", value):
        return value
    return ""


@dataclass(frozen=True)
class ParsedInput:
    kind: str  # show | season | episode | watchable | live
    value: str
    season: str = ""
    episode: str = ""


def parse_input(text: str) -> ParsedInput | None:
    """A Channel 5 URL, a show id/slug, or a C-prefixed watchable id."""
    raw = (text or "").strip()
    if not raw:
        return None
    if _WATCHABLE.fullmatch(raw):
        return ParsedInput("watchable", raw.upper())
    if re.fullmatch(r"\d{6,}", raw):
        return ParsedInput("show", raw)
    if _SLUG.fullmatch(raw):
        return ParsedInput("show", raw.lower())

    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None or parsed.password is not None
            or not any(host == domain or host.endswith(f".{domain}") for domain in ("channel5.com", "my5.tv"))
        ):
            return None
    except ValueError:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    head = parts[0].lower()
    if head in {"watch", "watchable", "watchables", "episode", "episodes"}:
        value = next((part for part in reversed(parts[1:]) if _WATCHABLE.fullmatch(part)), "")
        return ParsedInput("watchable", value.upper()) if value else None
    if head in {"channel", "channels", "live"} and len(parts) > 1:
        return ParsedInput("live", parts[1].lower())
    if head in {"show", "shows"} and len(parts) >= 2:
        show = parts[1].lower()
        if len(parts) == 2:
            return ParsedInput("show", show)
        season = parts[2].lower()
        if not re.fullmatch(r"(?:season-)?\d+", season):
            return None
        if len(parts) == 3:
            return ParsedInput("season", show, season=season)
        if len(parts) == 4:
            if _WATCHABLE.fullmatch(parts[3]):
                return ParsedInput("watchable", parts[3].upper())
            episode = parts[3].lower()
            if _SLUG.fullmatch(episode):
                return ParsedInput("episode", show, season=season, episode=episode)
    if head == "season" and len(parts) == 3 and re.fullmatch(r"(?:season-)?\d+", parts[2], re.I):
        return ParsedInput("season", parts[1].lower(), season=parts[2].lower())
    return None


@dataclass(frozen=True)
class SearchHit:
    kind: str  # show | watchable | live
    id: str
    title: str
    synopsis: str = ""
    genre: str = ""

    @property
    def url(self) -> str:
        if self.kind == "watchable":
            return f"{WWW}/watch/{self.id}"
        if self.kind == "live":
            return f"{WWW}/channels/{_slug(self.title)}"
        return f"{WWW}/show/{_slug(self.title)}"


@dataclass(frozen=True)
class Episode:
    id: str
    title: str
    show_id: str = ""
    show_title: str = ""
    season: int | None = None
    number: int | None = None
    year: str = ""
    synopsis: str = ""
    genre: str = ""
    duration: int = 0
    artwork: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def film(self) -> bool:
        return self.genre.lower() == "film" or not (self.season and self.number)

    @property
    def label(self) -> str:
        if not self.film and self.season and self.number:
            return f"S{self.season:02d}E{self.number:02d}  {self.title}"
        return f"{self.title} ({self.year})" if self.year else self.title


@dataclass
class Season:
    number: int
    episodes: list[Episode] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"Season {self.number}  ·  {len(self.episodes)} episode(s)"


@dataclass
class Show:
    id: str
    title: str
    synopsis: str = ""
    genre: str = ""
    seasons: list[Season] = field(default_factory=list)
    films: list[Episode] = field(default_factory=list)


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    slug: str
    now: str = ""
    next: str = ""
    artwork: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class Source:
    manifest: str
    license_url: str
    encrypted: bool = True
    protocol: str = "DASH"
    profile: str = ""
    subtitles: bool = False
    audio_description: bool = False
    duration: int = 0
    chapters: tuple[Chapter, ...] = ()

    def line(self) -> str:
        bits = [self.protocol, "Widevine" if self.encrypted else "clear"]
        if self.profile:
            bits.append(self.profile)
        if self.subtitles:
            bits.append("subtitles")
        if self.audio_description:
            bits.append("audio description")
        return " · ".join(bits)


def cassie_url(asset_id: str, is_live: bool = False) -> str:
    kind = "live_media" if is_live else "media"
    return f"{CASSIE}/{kind}/{CASSIE_PLATFORM}/{quote(str(asset_id), safe='')}.json"


def curl_config(certificate: Path, password: str, *, session_token: str = "") -> str:
    """Build the curl stdin config; credentials never go in argv."""
    cert = str(certificate)
    if any(char in cert + password for char in ('"', "\r", "\n")):
        raise Channel5Error("Cassie certificate path/password contains unsupported characters")
    config = (
        'cert-type = "P12"\n'
        f'cert = "{cert}"\n'
        f'pass = "{password}"\n'
        f'header = "User-Agent: {USER_AGENT}"\n'
        'header = "Accept: application/json"\n'
    )
    if session_token:
        if not bearer_token(session_token):
            raise Channel5Error("Invalid Channel 5 session token")
        config += f'header = "Authorization: Bearer {session_token}"\n'
    return config


def parse_cassie(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise Channel5Error("Cassie returned something that is not JSON") from exc
    if not isinstance(data, dict):
        raise Channel5Error("Cassie returned something that is not an object")
    if not data.get("assets"):
        message = str(data.get("message") or data.get("code") or "no assets")
        raise Channel5Error(f"Cassie refused playback: {message}")
    return data


class Channel5Api:
    def __init__(self, session: requests.Session | None = None, cassie: CassieFetch | None = None):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.cassie = cassie
        self._live_collection: str | None = None

    def start_tv_code(self) -> TVCode:
        response = self._pairing_request("POST", "/activationPins")
        if response.status_code != 201:
            raise Channel5Error(f"Channel 5 could not issue a TV code (HTTP {response.status_code})")
        data = self._pairing_json(response)
        pin = data.get("pin")
        secret = bearer_token(data.get("pinSecret"))
        if not isinstance(pin, str) or not re.fullmatch(r"[A-Za-z0-9]{6}", pin) or not secret:
            raise Channel5Error("Channel 5 returned an invalid TV code")
        qr_url = data.get("qrCode")
        if isinstance(qr_url, str):
            try:
                link = urlsplit(qr_url)
                if link.scheme == "https" and link.netloc == urlsplit(USER_SERVICE).netloc:
                    return TVCode(pin, secret, qr_url=qr_url)
            except ValueError:
                pass
        return TVCode(pin, secret)

    def poll_tv_code(self, code: TVCode) -> str | None:
        response = self._pairing_request(
            "GET", f"/activationPins/{code.pin}/poll",
            params={"longPoll": "false"},
            headers={"Authorization": f"Bearer {code.secret}"},
        )
        if response.status_code == 204:
            return None
        if response.status_code in {404, 410}:
            raise Channel5Error("Channel 5 TV code is no longer valid; request a new code")
        if response.status_code != 201:
            raise Channel5Error(f"Channel 5 could not confirm the TV code (HTTP {response.status_code})")
        token = bearer_token(self._pairing_json(response).get("sessionToken"))
        if not token:
            raise Channel5Error("Channel 5 confirmed the TV code without a valid session token")
        return token

    def _pairing_request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            response = self.session.request(
                method, f"{USER_SERVICE}{path}", timeout=10, allow_redirects=False, **kwargs,
            )
        except (requests.ConnectionError, requests.Timeout):
            raise PairingUnavailable("Could not reach Channel 5 TV pairing") from None
        except requests.RequestException:
            raise Channel5Error("Channel 5 TV pairing request failed") from None
        if response.status_code == 429 or response.status_code >= 500:
            raise PairingUnavailable(f"Channel 5 TV pairing is temporarily unavailable (HTTP {response.status_code})")
        return response

    @staticmethod
    def _pairing_json(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            raise Channel5Error("Channel 5 returned invalid TV pairing data") from None
        if not isinstance(data, dict):
            raise Channel5Error("Channel 5 returned invalid TV pairing data")
        return data

    def search(self, query: str) -> list[SearchHit]:
        data = self._json(
            f"{CORONA}/shows/search.json",
            params={"platform": PLATFORM, "query": query},
        )
        hits: list[SearchHit] = []
        for raw in data.get("shows") or []:
            if not isinstance(raw, dict) or not raw.get("id") or not raw.get("title"):
                continue
            hits.append(
                SearchHit(
                    "show",
                    str(raw["id"]),
                    str(raw["title"]),
                    str(raw.get("s_desc") or raw.get("m_desc") or ""),
                    str(raw.get("genre") or ""),
                )
            )
        return hits

    def resolve_show(self, value: str) -> SearchHit:
        data = self._show_json(value)
        return SearchHit(
            "show",
            str(data.get("id") or value),
            str(data.get("title") or value),
            str(data.get("s_desc") or data.get("m_desc") or ""),
            str(data.get("genre") or ""),
        )

    def show(self, show_id: str, *, season: str = "") -> Show:
        info = self._show_json(show_id)
        base = f"{CORONA}/shows/{quote(show_id, safe='')}"
        if season:
            base += f"/seasons/{quote(season, safe='')}"
        data = self._json(
            f"{base}/episodes.json",
            params={"platform": PLATFORM, "friendly": "1"} if season or not show_id.isdigit() else {"platform": PLATFORM},
        )
        episodes = [
            item
            for raw in data.get("episodes") or []
            if isinstance(raw, dict) and (item := self._episode(raw)) is not None
        ]
        grouped: dict[int, list[Episode]] = {}
        films: list[Episode] = []
        for item in episodes:
            if item.film:
                films.append(item)
            else:
                grouped.setdefault(item.season or 0, []).append(item)
        seasons = [
            Season(number, sorted(items, key=lambda item: (item.number or 0, item.title)))
            for number, items in sorted(grouped.items())
        ]
        return Show(
            id=str(info.get("id") or show_id),
            title=str(info.get("title") or (episodes[0].show_title if episodes else show_id)),
            synopsis=str(info.get("m_desc") or info.get("s_desc") or ""),
            genre=str(info.get("genre") or ""),
            seasons=seasons,
            films=films,
        )

    def watchable(self, watchable_id: str) -> Episode:
        data = self._json(
            f"{CORONA}/watchables/{quote(watchable_id, safe='')}.json",
            params={"platform": PLATFORM},
        )
        item = self._episode(data)
        if item is None:
            raise Channel5Error(f"Channel 5 returned no watchable {watchable_id}")
        return item

    def episode(self, show: str, season: str, episode: str) -> Episode:
        data = self._json(
            f"{CORONA}/shows/{quote(show, safe='')}/seasons/{quote(season, safe='')}/episodes/{quote(episode, safe='')}.json",
            params={"platform": PLATFORM, "friendly": "1"},
        )
        item = self._episode(data)
        if item is None:
            raise Channel5Error(f"Channel 5 returned no episode for {show}/{season}/{episode}")
        return item

    def channels(self) -> list[Channel]:
        if self._live_collection is None:
            config = self._json(CONFIG_URL, params={"label": APP_VERSION})
            settings = config.get("dataServiceSettings")
            collection = settings.get("ednaChannelsSubNavId") if isinstance(settings, dict) else None
            if not isinstance(collection, str) or not collection.strip():
                raise Channel5Error("Channel 5 TV configuration returned no live channel collection")
            self._live_collection = collection.strip()
        data = self._json(
            f"{EDNA}/collections/{quote(self._live_collection, safe='')}/concise.json",
            params={"vod_available": PLATFORM},
        )
        filters = data.get("filters")
        contents = filters.get("contents") if isinstance(filters, dict) else None
        at = datetime.now(UTC)
        channels: list[Channel] = []
        for raw in contents or []:
            if not isinstance(raw, dict) or raw.get("live") is not True:
                continue
            channel_id = raw.get("channel")
            if not isinstance(channel_id, str) or not channel_id:
                continue
            now, nxt = self._now_next(channel_id, at)
            name = str(raw.get("title") or raw.get("channel_friendly_name") or channel_id)
            channels.append(
                Channel(
                    id=channel_id,
                    name=name,
                    slug=str(raw.get("channel_friendly_name") or _slug(name)),
                    now=now,
                    next=nxt,
                    artwork=raw,
                )
            )
        return channels

    def _now_next(self, channel_id: str, at: datetime) -> tuple[str, str]:
        start = at.replace(minute=0, second=0, microsecond=0)
        try:
            epg = self._json(
                f"{CORONA}/channels/{quote(channel_id, safe='')}/epg.json",
                params={
                    "platform": PLATFORM,
                    "start": start.strftime("%Y-%m-%dT%H:%M:%S0Z"),
                    "end": (start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S0Z"),
                },
                timeout=8,
            )
        except Channel5Error:
            return "", ""
        return _programme_schedule(epg.get("transmissions"), at)

    def channel(self, slug: str) -> Channel:
        wanted = _slug(slug)
        for channel in self.channels():
            if wanted in {_slug(channel.id), _slug(channel.name), _slug(channel.slug)}:
                return channel
        raise Channel5Error(f"Channel 5 returned no live channel for {slug}")

    def source(
        self,
        asset_id: str,
        *,
        is_live: bool = False,
        fetch_chapters: bool = True,
    ) -> Source:
        if self.cassie is None:
            raise Channel5Error("Cassie playback transport is not configured")
        data = self.cassie(asset_id, is_live)
        assets = [item for item in data.get("assets") or [] if isinstance(item, dict)]
        asset = next((item for item in assets if str(item.get("drm") or "").lower() == "widevine"), None)
        asset = asset or (assets[0] if assets else None)
        if asset is None:
            raise Channel5Error(f"Cassie returned no asset for {asset_id}")
        renditions = [item for item in asset.get("renditions") or [] if isinstance(item, dict)]
        if not renditions:
            raise Channel5Error(f"Cassie returned no rendition for {asset_id}")
        manifest = self._manifest(renditions, is_live=is_live)
        license_url = str(asset.get("keyserver") or "")
        encrypted = str(asset.get("drm") or "").lower() == "widevine" or bool(license_url)
        if encrypted and not license_url:
            raise Channel5Error(f"Cassie returned Widevine without a licence URL for {asset_id}")
        duration = _int(asset.get("duration")) or 0
        return Source(
            manifest=manifest,
            license_url=license_url,
            encrypted=encrypted,
            profile=str(asset.get("profile") or renditions[0].get("format") or ""),
            subtitles=bool(asset.get("subtitled")),
            audio_description=bool(asset.get("audiodescribed")),
            duration=duration,
            chapters=(
                _credits_chapters(asset, duration=duration)
                if fetch_chapters and not is_live
                else ()
            ),
        )

    def _manifest(self, renditions: list[dict[str, Any]], *, is_live: bool) -> str:
        candidates: list[str] = []
        for rendition in renditions:
            for key in ("fallbackurl", "url") if is_live else ("url", "fallbackurl"):
                value = str(rendition.get(key) or "")
                if value and value not in candidates:
                    candidates.append(value)
        if not candidates:
            raise Channel5Error("Cassie returned no manifest URL")
        if is_live:
            for candidate in candidates:
                try:
                    response = self.session.get(candidate, timeout=15)
                    if response.ok and "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" in response.text:
                        return candidate
                except requests.RequestException:
                    continue
            return candidates[0]
        return normalise_vod_manifest_url(candidates[0])

    def _show_json(self, show_id: str) -> dict[str, Any]:
        return self._json(
            f"{CORONA}/shows/{quote(show_id, safe='')}.json",
            params={"platform": PLATFORM, "friendly": "1"} if not show_id.isdigit() else {"platform": PLATFORM},
        )

    def _json(self, url: str, *, timeout: int = 25, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.session.get(url, timeout=timeout, **kwargs)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            detail = ""
            response = getattr(exc, "response", None)
            if response is not None:
                detail = (response.text or "").strip().replace("\n", " ")[:180]
            raise Channel5Error(f"Channel 5 request failed: {exc}{f' - {detail}' if detail else ''}") from exc
        except ValueError as exc:
            raise Channel5Error("Channel 5 returned something that is not JSON") from exc
        if not isinstance(data, dict):
            raise Channel5Error("Channel 5 returned something that is not an object")
        return data

    @staticmethod
    def _episode(raw: dict[str, Any]) -> Episode | None:
        item_id = str(raw.get("id") or "")
        title = str(raw.get("title") or "")
        if not item_id or not title:
            return None
        year = str(raw.get("year") or "")
        if not year:
            start = _int(raw.get("vod_s"))
            if start:
                year = str(datetime.fromtimestamp(start).year)
        duration_ms = _int(raw.get("len")) or 0
        return Episode(
            id=item_id,
            title=title,
            show_id=str(raw.get("sh_id") or raw.get("showId") or ""),
            show_title=str(raw.get("sh_title") or raw.get("showTitle") or title),
            season=_int(raw.get("sea_num") or raw.get("seasonNumber")),
            number=_int(raw.get("ep_num") or raw.get("episodeNumber")),
            year=year,
            synopsis=str(raw.get("m_desc") or raw.get("s_desc") or ""),
            genre=str(raw.get("genre") or ""),
            duration=round(duration_ms / 1000) if duration_ms else 0,
            artwork=raw,
        )

    # -------------------------------------------------------------- licensing
    def widevine_license(self, license_url: str, challenge: bytes, headers: dict[str, str] | None = None) -> bytes:
        if not license_url:
            raise Channel5Error("no Channel 5 licence URL came with this playback")
        request_headers = {"Content-Type": "application/octet-stream"}
        request_headers.update(headers or {})
        try:
            response = self.session.post(license_url, data=challenge, headers=request_headers, timeout=30)
        except requests.RequestException as exc:
            raise Channel5Error(f"could not reach Channel 5's licence server: {exc}") from exc
        if not response.ok:
            raise Channel5Error(
                f"Channel 5's licence server answered {response.status_code}: {str(response.text)[:200]}"
            )
        return response.content


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _credits_chapters(asset: dict[str, Any], *, duration: int) -> tuple[Chapter, ...]:
    raw_duration = asset.get("duration")
    raw_credits = asset.get("creditsduration")
    if isinstance(raw_duration, bool) or isinstance(raw_credits, bool):
        return ()
    try:
        credits_duration = float(raw_credits)
    except (TypeError, ValueError, OverflowError):
        return ()
    if not math.isfinite(credits_duration) or not 0 < credits_duration < duration:
        return ()
    try:
        return (
            Chapter.from_seconds(
                duration - credits_duration,
                "Credits",
                duration,
                kind="credits",
            ),
        )
    except ValueError:
        return ()


def normalise_vod_manifest_url(manifest_url: str) -> str:
    if not manifest_url:
        return manifest_url
    parsed = urlparse(manifest_url)
    parts = parsed.path.split("/")
    if not parts:
        return manifest_url
    filename = parts[-1]
    match = re.fullmatch(r"(?P<stem>.+)(?P<ext>\.mpd)", filename, re.IGNORECASE)
    if match is None:
        return manifest_url
    stem = match.group("stem")
    if stem.casefold() in {"index", "manifest"}:
        return manifest_url
    base_stem = stem.split("-", 1)[0].split("_", 1)[0]
    if base_stem == stem:
        return manifest_url
    parts[-1] = f"{base_stem}{match.group('ext')}"
    return urlunparse(parsed._replace(path="/".join(parts)))


def _slug(value: str) -> str:
    return _NON_ALNUM.sub("-", str(value or "").lower()).strip("-")


def _programme(raw: dict[str, Any]) -> str:
    show = str(raw.get("showTitle") or "")
    episode = str(raw.get("episodeTitle") or "")
    return f"{show}: {episode}" if show and episode else show or episode


def _programme_schedule(transmissions: Any, at: datetime) -> tuple[str, str]:
    if not isinstance(transmissions, list):
        return "", ""
    current = upcoming = ""
    current_start, upcoming_start = None, None
    for slot in transmissions:
        if not isinstance(slot, dict):
            continue
        try:
            start = datetime.fromisoformat(slot["start"])
            end = datetime.fromisoformat(slot["end"])
            if start <= at < end and (current_start is None or start > current_start):
                current, current_start = _programme(slot), start
            elif at < start < end and (upcoming_start is None or start < upcoming_start):
                upcoming, upcoming_start = _programme(slot), start
        except (KeyError, TypeError, ValueError):
            continue
    return current, upcoming


__all__ = [
    "ACTIVATE_URL",
    "APP_VERSION",
    "CASSIE",
    "CASSIE_PLATFORM",
    "CORONA",
    "CONFIG_URL",
    "EDNA",
    "PLATFORM",
    "USER_AGENT",
    "USER_SERVICE",
    "WWW",
    "Channel",
    "Channel5Api",
    "Channel5Error",
    "Episode",
    "ParsedInput",
    "PairingUnavailable",
    "SearchHit",
    "Season",
    "Show",
    "Source",
    "TVCode",
    "bearer_token",
    "cassie_url",
    "curl_config",
    "normalise_vod_manifest_url",
    "parse_cassie",
    "parse_input",
]
