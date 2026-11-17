"""Channel 4 catalogue, TV authentication and Red Bee playback models."""

from __future__ import annotations

import base64
import binascii
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlsplit, urlunparse
from xml.etree import ElementTree

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from curl_cffi import requests as browser_requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter, normalize_chapters
from ...core.playback import SubtitleReference

WWW = "https://www.channel4.com"
API = "https://api.channel4.com/online/v1"
AUTH = "https://api.channel4.com/online/v2/auth/token"
PIN = "https://api.channel4.com/online/v2/auth/pin"
TV_CONFIG = "https://config.bsd.client.streaming.channel4.com/26.2.0/google/config.json"
PLAYREADY_CONFIG = "https://config.bsd.client.streaming.channel4.com/26.2.0/tizen/config.json"
TV_CONFIG_KEY = "ALr9MMDdNUFaH8CLwlJt"
LICENSE = "https://c4.eme.lp.aws.redbeemedia.com/wvlicenceproxy-service/widevine/acquire"
PLAYREADY_CLIENT = "samsung-dash"
PLAYREADY_SOAP_ACTION = '"http://schemas.microsoft.com/DRM/2007/03/protocols/AcquireLicense"'

API_CLIENT = "googletv"
AUTH_CLIENT = "googletv"
DEVICE_GROUP = "console"
LIVE_SCHEDULE_CLIENT = "googletv"
LIVE_CLIENT = "googletv"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; Chromecast Build/STTL.240508.005; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/128.0.6613.146 Mobile Safari/537.36 "
    "WebAppWrapper/1.0.3 (Google TV; Channel 4; google; Chromecast; Chromecast)"
)

VOD_TOKEN_KEY = "UVNZVDY2QkJJMlgzUVRVTg=="
VOD_TOKEN_IV = "VjlZUk4zUElWWFkyTUhHNQ=="
LIVE_TOKEN_KEY = VOD_TOKEN_KEY
LIVE_TOKEN_IV = VOD_TOKEN_IV
SERVICE_CERTIFICATE_CHALLENGE = "CAQ="

CHANNEL_IDS = {
    "channel4": "C4",
    "e4": "E4",
    "more4": "M4",
    "film4": "F4",
    "4seven": "4S",
    "e4-extra": "E4X",
}
CHANNEL_NAMES = {
    "C4": "Channel 4",
    "E4": "E4",
    "M4": "More4",
    "F4": "Film4",
    "4S": "4seven",
    "E4X": "E4 Extra",
}
CHANNEL_ALIASES = {
    "c4": "C4",
    "channel-4": "C4",
    "more-4": "M4",
    "m4": "M4",
    "film-4": "F4",
    "f4": "F4",
    "4-seven": "4S",
    "4s": "4S",
    "e4extra": "E4X",
    "e4x": "E4X",
    **CHANNEL_IDS,
}

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$", re.I)
_EPISODE_ID = re.compile(r"^\d+(?:-\d+)*$")


class Channel4Error(RuntimeError):
    """Channel 4 rejected a request or returned an unusable response."""


class Channel4AuthError(Channel4Error):
    """The cached or supplied Channel 4 authority was rejected."""


class Channel4TransientError(Channel4Error):
    """A transport, throttling, or server failure may succeed later."""


class Channel4GeoError(Channel4Error):
    """The playback API rejected this network location."""


@dataclass(frozen=True)
class TvCode:
    user_code: str
    device_code: str = field(repr=False)
    create_date: str = field(repr=False)
    verification_url: str
    qr_url: str = field(repr=False)
    expires_in: int
    interval: int
    deadline: float = field(repr=False)


def tv_session(session=None, *, proxy: str | None = None, cookies=None):
    """Use the browser TLS handshake accepted by Channel 4's TV token endpoint."""
    return browser_requests.Session(
        impersonate="chrome",
        headers={**(dict(session.headers) if session is not None else {}), "User-Agent": USER_AGENT},
        cookies=getattr(session.cookies, "jar", session.cookies) if session is not None else cookies,
        proxies=dict(session.proxies) if session is not None else ({"http": proxy, "https": proxy} if proxy else None),
        trust_env=getattr(session, "trust_env", True),
    )


@dataclass(frozen=True)
class ParsedInput:
    kind: str  # show | episode | live
    value: str
    episode_id: str = ""


def parse_input(text: str) -> ParsedInput | None:
    raw = (text or "").strip()
    if not raw:
        return None
    alias = raw.lower()
    if alias in CHANNEL_ALIASES or alias.upper() in CHANNEL_NAMES:
        return ParsedInput("live", CHANNEL_ALIASES.get(alias, alias.upper()))
    if _SLUG.fullmatch(raw):
        return ParsedInput("show", raw.lower())

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in {"channel4.com", "www.channel4.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 2 and parts[0].lower() == "now":
        channel = channel_id(parts[1])
        return ParsedInput("live", channel) if channel else None
    if len(parts) == 2 and parts[0].lower() == "programmes" and _SLUG.fullmatch(parts[1]):
        return ParsedInput("show", parts[1].lower())
    if (
        len(parts) == 4
        and parts[0].lower() == "programmes"
        and _SLUG.fullmatch(parts[1])
        and parts[2].lower() == "on-demand"
        and _EPISODE_ID.fullmatch(parts[3])
    ):
        return ParsedInput("episode", parts[1].lower(), parts[3])
    return None


def channel_id(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw.upper() in CHANNEL_NAMES:
        return raw.upper()
    return CHANNEL_ALIASES.get(raw, "")


@dataclass(frozen=True)
class SearchHit:
    slug: str
    title: str
    synopsis: str = ""
    label: str = ""

    @property
    def url(self) -> str:
        return f"{WWW}/programmes/{self.slug}"


@dataclass(frozen=True)
class Episode:
    id: str
    show_slug: str
    show_title: str
    title: str
    season: int | None = None
    number: int | None = None
    year: str = ""
    synopsis: str = ""
    vod_href: str = ""
    film: bool = False
    chapters: tuple[Chapter, ...] = ()
    duration: float | None = None
    artwork: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def url(self) -> str:
        return f"{WWW}/programmes/{self.show_slug}/on-demand/{self.id}"

    @property
    def label(self) -> str:
        if not self.film and self.season is not None and self.number is not None:
            return f"S{self.season:02d}E{self.number:02d}  {self.title}"
        return f"{self.title} ({self.year})" if self.year else self.title


@dataclass
class Season:
    number: int
    episodes: list[Episode] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"Series {self.number}  ·  {len(self.episodes)} episode(s)"


@dataclass
class Show:
    slug: str
    title: str
    kind: str = "series"
    seasons: list[Season] = field(default_factory=list)
    films: list[Episode] = field(default_factory=list)


@dataclass(frozen=True)
class Channel:
    id: str
    slug: str
    name: str
    now: str = ""
    next: str = ""
    synopsis: str = ""
    restart: bool = False
    artwork: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    stream_url: str = ""

    @property
    def url(self) -> str:
        return f"{WWW}/now/{self.slug}"


@dataclass(frozen=True)
class Source:
    manifest: str
    license_token: str
    video_type: str
    video_url: str
    protocol: str = "DASH"
    subtitles: tuple[SubtitleReference, ...] = ()
    service_certificate: bytes | None = None
    restart: bool = False
    chapters: tuple[Chapter, ...] = ()
    duration: float | None = None
    artwork: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    drm_system: str = "widevine"
    license_url: str = field(default=LICENSE, repr=False)

    @property
    def subtitle(self) -> str:
        return self.subtitles[0].url if self.subtitles else ""

    def line(self) -> str:
        bits = [self.protocol, "PlayReady" if self.drm_system == "playready" else "Widevine"]
        if self.restart:
            bits.append("restart")
        if self.subtitles:
            bits.append("subtitles")
        return " · ".join(bits)


class Channel4Api:
    def __init__(self, session: browser_requests.Session | None = None, state: dict[str, Any] | None = None):
        # Normalize an injected legacy Session too; it must not reintroduce Python TLS.
        self.session = tv_session(session) if session is None or hasattr(session, "adapters") else session
        if isinstance(self.session, browser_requests.Session):
            self.session.impersonate = "chrome"
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.state = state or {}
        self.authorization = ""
        self._config: dict[str, Any] | None = None
        self._tizen: dict[str, Any] | None = None
        self.set_state(self.state)

    # ------------------------------------------------------------------- auth
    def set_state(self, state: dict[str, Any]) -> None:
        self.state = state if isinstance(state, dict) else {}
        tokens = _dict(self.state.get("tokens"))
        access = _token_value(tokens, "accessToken", "access_token")
        self.authorization = f"Bearer {access}" if access else ""

    def authenticate(self) -> dict[str, Any]:
        if self.state.get("auth_client") != AUTH_CLIENT:
            raise Channel4AuthError("Sign in with a Channel 4 TV code from the service menu")
        tokens = _dict(self.state.get("tokens"))
        expiry = _int(self.state.get("tokens_expiry")) or 0
        if self.authorization and expiry > int(time.time()) + 60:
            return self.state

        refresh = _token_value(tokens, "refreshToken", "refresh_token")
        if refresh:
            try:
                fresh = self._token_request(
                    {"grant_type": "refresh_token", "refresh_token": refresh}
                )
                return self._accept_tokens(fresh, previous=tokens)
            except Channel4AuthError as exc:
                raise Channel4AuthError(f"{exc}. Sign in with a new Channel 4 TV code") from exc
        raise Channel4AuthError("Sign in with a Channel 4 TV code from the service menu")

    def start_tv_code(self) -> TvCode:
        data = self._json("GET", PIN, headers=self._auth_headers())
        user_code = data.get("userCode")
        device_code = data.get("deviceCode")
        created = data.get("createDate")
        expires = _int(data.get("expiresIn"))
        interval = _int(data.get("nextPollIn"))
        verification = _dict(data.get("verification"))
        url = _verification_url(verification.get("url"))
        qr_url = _verification_url(verification.get("urlForDynamicQr")) or url
        if (
            not isinstance(user_code, str) or not user_code.strip()
            or not isinstance(device_code, str) or not device_code.strip() or isinstance(created, bool)
            or not isinstance(created, (str, int)) or not str(created).strip()
            or isinstance(data.get("expiresIn"), bool) or expires is None or not 0 < expires <= 3600
            or isinstance(data.get("nextPollIn"), bool) or interval is None or not 0 < interval < expires or not url
        ):
            raise Channel4AuthError("Channel 4 returned an incomplete TV code")
        return TvCode(user_code, device_code, str(created), url, qr_url, expires, interval, time.monotonic() + expires)

    def poll_tv_code(self, challenge: TvCode) -> dict[str, Any] | None:
        if time.monotonic() >= challenge.deadline:
            raise Channel4AuthError("Channel 4 TV code expired; request a new code")
        data = self._token_request(
            {"grant_type": "pin_pairing", "code": challenge.device_code, "create_date": challenge.create_date},
            pairing=True,
        )
        paired = data.get("paired")
        if paired is False or (isinstance(paired, str) and paired.casefold() == "false"):
            return None
        if paired is not True and not (isinstance(paired, str) and paired.casefold() == "true"):
            raise Channel4AuthError("Channel 4 returned an invalid TV pairing status")
        return self._accept_tokens(data)

    def _auth_headers(self) -> dict[str, str]:
        if self._config is None:
            config = self._json("GET", TV_CONFIG, headers={"x-c4-api-key": TV_CONFIG_KEY, "User-Agent": USER_AGENT})
            oauth = _dict(config.get("oauth"))
            if not all(isinstance(oauth.get(key), str) and oauth[key] for key in ("c_key", "c_secret")):
                raise Channel4AuthError("Channel 4 Google TV configuration has no OAuth client")
            if _dict(config.get("deviceStrings")).get("apiClient") != AUTH_CLIENT:
                raise Channel4AuthError("Channel 4 Google TV client configuration changed")
            self._config = config
        oauth = self._config["oauth"]
        basic = base64.b64encode(f"{oauth['c_key']}:{oauth['c_secret']}".encode()).decode("ascii")
        return {**self._headers(), "Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"}

    def _token_request(self, form: dict[str, str], *, pairing: bool = False) -> dict[str, Any]:
        url = AUTH if pairing else f"{AUTH}?client={AUTH_CLIENT}"
        return self._json("POST", url, headers=self._auth_headers(), data=form)

    def _accept_tokens(
        self,
        tokens: dict[str, Any],
        *,
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(tokens, dict):
            raise Channel4AuthError("Channel 4 authentication response is invalid")
        previous = previous or {}
        access = _token_value(tokens, "accessToken", "access_token")
        refresh = _token_value(tokens, "refreshToken", "refresh_token") or _token_value(previous, "refreshToken", "refresh_token")
        expires = _int(_token_value(tokens, "expiresIn", "expires_in"))
        if not access or not refresh:
            raise Channel4AuthError("Channel 4 authentication returned incomplete tokens")
        if expires is None or expires <= 0:
            raise Channel4AuthError("Channel 4 authentication returned no trustworthy expiry")
        merged = {"accessToken": access, "refreshToken": refresh}
        user = _dict(tokens.get("user")) or _dict(previous.get("user"))
        if user:
            merged["user"] = user
        state = {"tokens": merged, "tokens_expiry": int(time.time()) + expires, "auth_client": AUTH_CLIENT}
        self.set_state(state)
        return state

    @property
    def user_uuid(self) -> str:
        user = _dict(_dict(self.state.get("tokens")).get("user"))
        return str(user.get("uuid") or "")

    # -------------------------------------------------------------- catalogue
    def search(self, query: str) -> list[SearchHit]:
        data = self._json(
            "GET",
            f"{API}/search/{DEVICE_GROUP}",
            params={"q": query, "limit": "100", "client": API_CLIENT},
            headers=self._headers(auth=True),
        )
        page = _dict(data.get("page"))
        slices = _list(data.get("slices")) or _list(page.get("slices"))
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for section in slices:
            if not isinstance(section, dict) or str(section.get("type") or "").lower() != "results":
                continue
            for item in _list(section.get("sliceItems")) or _list(section.get("items")):
                if not isinstance(item, dict):
                    continue
                brand = _dict(item.get("brand"))
                info = _dict(item.get("ip")) or _dict(item.get("informationPage"))
                source = (
                    info
                    if str(item.get("type") or "").lower() == "ip" and info
                    else brand or item
                )
                slug = str(source.get("websafeTitle") or source.get("webSafeTitle") or "")
                title = str(source.get("title") or item.get("title") or "")
                if not slug or not title or slug in seen:
                    continue
                seen.add(slug)
                hits.append(
                    SearchHit(
                        slug,
                        title,
                        str(item.get("summary") or source.get("description") or source.get("summary") or ""),
                        str(item.get("label") or ""),
                    )
                )
        return hits

    def show(self, slug: str, episode_id: str = "") -> Show:
        if not _SLUG.fullmatch(str(slug or "")):
            raise Channel4Error("Channel 4 programme identifier is invalid")
        if episode_id and not _EPISODE_ID.fullmatch(str(episode_id)):
            raise Channel4Error("Channel 4 episode identifier is invalid")
        data = self._json(
            "GET",
            f"{API}/views/content-hubs/{slug}.json",
            params=[("client", API_CLIENT), ("deviceGroup", DEVICE_GROUP)],
            headers=self._headers(auth=True),
        )
        brand = _dict(data.get("brand"))
        if not brand.get("title"):
            raise Channel4Error("Channel 4 returned no programme")
        film = brand.get("programmeType") == "FM"
        episodes = [
            item
            for raw in _list(brand.get("episodes"))
            if isinstance(raw, dict)
            and (not episode_id or str(raw.get("programmeId")) == episode_id)
            and (item := self._episode(raw, slug, str(brand["title"]), film, brand=brand)) is not None
        ]
        if episode_id and not episodes:
            raise Channel4Error("Channel 4 could not find the requested episode")
        if film:
            return Show(slug, str(brand["title"]), "movie", films=episodes)
        grouped: dict[int, list[Episode]] = {}
        for item in episodes:
            grouped.setdefault(item.season or 0, []).append(item)
        seasons = [
            Season(number, sorted(items, key=lambda item: (item.number or 0, item.title)))
            for number, items in sorted(grouped.items())
        ]
        return Show(slug, str(brand["title"]), seasons=seasons)

    @staticmethod
    def _episode(
        raw: dict[str, Any], slug: str, title: str, film: bool, *, brand: dict[str, Any] | None = None,
    ) -> Episode | None:
        asset = _dict(raw.get("assetInfo"))
        stream = _dict(asset.get("streaming")) or _dict(asset.get("download"))
        item_id = str(raw.get("programmeId") or "")
        if not item_id or not stream:
            return None
        return Episode(
            id=item_id,
            show_slug=slug,
            show_title=title,
            title=str(raw.get("originalTitle") or title),
            season=_int(raw.get("seriesNumber")),
            number=_int(raw.get("episodeNumber")),
            year=str(raw.get("year") or raw.get("productionYear") or ""),
            synopsis=str(raw.get("synopsis") or raw.get("summary") or ""),
            vod_href=str(stream.get("vodBSHref") or ""),
            film=film,
            chapters=extract_chapters(raw, duration=stream.get("duration")),
            duration=_number(stream.get("duration")),
            artwork={"episode": image_fields(raw), "brand": image_fields(brand)},
        )

    # --------------------------------------------------------------- VOD source
    def source(self, item: Episode, drm_system: str = "widevine") -> Source:
        _check_drm(drm_system)
        playready = drm_system == "playready"
        url = urljoin(f"{API}/", item.vod_href) if item.vod_href else f"{API}/vod/stream/{item.id}"
        url = _query(url, {"client": PLAYREADY_CLIENT if playready else API_CLIENT, "uuid": self.user_uuid or None})
        data = self._json("GET", url, headers=self._stream_headers())
        channel_info = _dict(data.get("channelInfo"))
        profiles = _list(data.get("videoProfiles")) or _list(channel_info.get("videoProfiles"))
        preferred = str(_dict(self._tizen_config().get("stream")).get("videoProfileDashVOD") or "") if playready else ""
        profile = _vod_profile(profiles, drm_system, preferred=preferred)
        if not playready and _dict(data.get("uriData")).get("token"):
            stream = _profile_stream(data)
        elif profile is None:
            raise Channel4Error(f"Channel 4 returned no {drm_system} VOD profile")
        else:
            stream = _profile_stream(profile)
        manifest = str(stream.get("uri") or "")
        encrypted_token = str(stream.get("token") or "")
        if not manifest or not encrypted_token:
            raise Channel4Error("Channel 4 returned incomplete playback")
        manifest = self._working_manifest(manifest)
        token = "" if playready else decrypt_token(encrypted_token, live=False)
        license_url = self._playready_license_url(encrypted_token) if playready else LICENSE
        subtitles = _subtitles(data)
        duration = _number(data.get("duration"))
        if duration is None:
            duration = item.duration
        chapters = extract_chapters(data, duration=duration) or item.chapters
        return Source(
            manifest,
            token,
            "ondemand",
            manifest,
            subtitles=subtitles,
            chapters=chapters,
            duration=duration,
            artwork={"episode": image_fields(data)},
            drm_system=drm_system,
            license_url=license_url,
        )

    # ------------------------------------------------------------------- live
    def channels(self) -> list[Channel]:
        data = self._data(
            "GET",
            f"{API}/programmes/tx-schedule/now-next.json",
            params={"client": LIVE_SCHEDULE_CLIENT},
            headers=self._headers(auth=True),
        )
        slots: dict[str, dict[str, Any]] = {}
        if isinstance(data, list):
            sections = data
        elif isinstance(_dict(data).get("items"), list):
            sections = [data]
        else:
            mapping = _dict(data)
            sections = _list(mapping.get("results")) or _list(mapping.get("slices"))
        if not sections and isinstance(data, dict):
            sections = [value for value in data.values() if isinstance(value, dict)]
        for section in sections:
            if not isinstance(section, dict):
                continue
            items = _list(section.get("sliceItems")) or _list(section.get("items"))
            for item in items:
                if not isinstance(item, dict):
                    continue
                slot = _dict(item.get("slot"))
                channel = str(slot.get("slotTXChannel") or "")
                if channel:
                    slots[channel] = item
        channels: list[Channel] = []
        for slug, identifier in CHANNEL_IDS.items():
            item = _dict(slots.get(identifier))
            slot = _dict(item.get("slot"))
            now = str(slot.get("episodeTitle") or item.get("title") or "")
            next_item = _dict(item.get("next"))
            channels.append(
                Channel(
                    id=identifier,
                    slug=slug,
                    name=CHANNEL_NAMES[identifier],
                    now=now,
                    next=str(next_item.get("title") or next_item.get("episodeTitle") or ""),
                    synopsis=str(slot.get("summary") or item.get("summary") or ""),
                    restart=str(slot.get("startAgainRights") or "").lower() == "true",
                    artwork={
                        "episode": image_fields(item),
                        "brand": image_fields(item.get("brand")),
                    },
                    stream_url=str(slot.get("simulcastBSStreamUri") or ""),
                )
            )
        return channels

    def live_source(self, channel: Channel, *, restart: bool = False, drm_system: str = "widevine") -> Source:
        _check_drm(drm_system)
        playready = drm_system == "playready"
        data = self._json(
            "GET",
            _query(
                channel.stream_url or f"{WWW}/simulcast/channels/{channel.id}",
                {"client": PLAYREADY_CLIENT if playready else LIVE_CLIENT, "quality": "HD"},
            ),
            headers=self._stream_headers(),
        )
        slot = _dict(data.get("slotInfo"))
        info = _dict(data.get("channelInfo"))
        profiles = _list(info.get("videoProfiles"))
        if restart and str(slot.get("startAgainRights") or "").lower() != "true":
            raise Channel4Error("Restart is not available right now")
        watch_live = _dict(_dict(self._tizen_config().get("features")).get("watchLive")) if playready else {}
        preferred = str(watch_live.get("videoProfileForStartAgain" if restart else "videoProfileForLive") or "")
        profile = _live_profile(profiles, restart, drm_system, preferred=preferred)
        if profile is None:
            raise Channel4Error(f"Channel 4 returned no {drm_system} live profile")
        if restart:
            stream = _dict(profile.get("startAgainStream"))
        else:
            streams = _list(profile.get("streams"))
            stream = _dict(profile.get("stream")) or (_dict(streams[0]) if streams else {})
        manifest = str(stream.get("uri") or "").replace("http:", "https:")
        encrypted_token = str(stream.get("token") or "")
        if not manifest or manifest == "https://tbc.mpd" or not encrypted_token:
            raise Channel4Error("Channel 4 returned incomplete live playback")
        token = "" if playready else decrypt_token(encrypted_token, live=True)
        license_url = self._playready_license_url(encrypted_token) if playready else LICENSE
        certificate = None if playready else self.service_certificate(token, "simulcast", manifest)
        return Source(
            manifest,
            token,
            "simulcast",
            manifest,
            service_certificate=certificate,
            restart=restart,
            subtitles=_subtitles(data),
            artwork={"episode": image_fields(slot), "channel": image_fields(info)},
            drm_system=drm_system,
            license_url=license_url,
        )

    def _tizen_config(self) -> dict[str, Any]:
        if self._tizen is None:
            config = self._json(
                "GET", PLAYREADY_CONFIG,
                headers={"x-c4-api-key": TV_CONFIG_KEY, "User-Agent": USER_AGENT},
            )
            clients = [_dict(config.get("deviceStrings"))]
            clients.extend(
                _dict(_dict(item.get("override")).get("deviceStrings"))
                for item in _list(config.get("deviceOverrides")) if isinstance(item, dict)
            )
            if config.get("platform") != "tizen" or not any(
                client.get("vodStreamClient") == PLAYREADY_CLIENT for client in clients
            ):
                raise Channel4Error("Channel 4 Tizen DASH client configuration changed")
            self._tizen = config
        return self._tizen

    def _playready_license_url(self, encrypted_token: str) -> str:
        dash = _dict(_dict(_dict(self._tizen_config().get("stream")).get("keys")).get("dash"))
        try:
            # The TV app unwraps the configured DASH key before decoding the licence URL.
            key = unpad(AES.new(b"Up69fq3tve9uTHG5", AES.MODE_CBC, iv=bytes(16)).decrypt(
                base64.b64decode(dash["key"], validate=True)
            ), AES.block_size)
            decoded = unpad(AES.new(key, AES.MODE_CBC, iv=dash["iv"].encode()).decrypt(
                base64.b64decode(encrypted_token, validate=True)
            ), AES.block_size).decode().strip()
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise Channel4Error("Channel 4 returned invalid PlayReady token parameters") from exc
        return _playready_url(decoded)

    def license_soap(self, challenge: str, license_url: str) -> str:
        url = _playready_url(license_url)
        try:
            response = self.session.post(
                url,
                data=challenge.encode("utf-8"),
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": PLAYREADY_SOAP_ACTION,
                    "User-Agent": USER_AGENT,
                },
                timeout=35,
            )
        except browser_requests.RequestsError as exc:
            raise Channel4TransientError(f"Channel 4 PlayReady licence request failed ({type(exc).__name__})") from exc
        if not response.ok:
            raise Channel4Error(f"Channel 4 PlayReady licence HTTP {response.status_code}")
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise Channel4Error("Channel 4 returned an invalid PlayReady licence response") from exc
        tags = {node.tag.rsplit("}", 1)[-1] for node in root.iter()}
        if "Fault" in tags or "AcquireLicenseResponse" not in tags:
            raise Channel4Error("Channel 4 rejected the PlayReady licence challenge")
        return response.text

    def subtitle_bytes(self, url: str) -> bytes:
        try:
            parsed = urlparse(url)
        except ValueError as exc:
            raise Channel4Error("Channel 4 returned an invalid subtitle URL") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise Channel4Error("Channel 4 returned an invalid subtitle URL")
        try:
            response = self.session.get(
                url,
                headers={"User-Agent": USER_AGENT, "Origin": WWW, "Referer": f"{WWW}/"},
                timeout=35,
            )
        except browser_requests.RequestsError as exc:
            raise Channel4TransientError(
                f"Channel 4 subtitle request failed ({type(exc).__name__})"
            ) from exc
        if not response.ok:
            raise Channel4Error(f"Channel 4 subtitle request HTTP {response.status_code}")
        if not response.content:
            raise Channel4Error("Channel 4 returned an empty subtitle")
        if "text/html" in response.headers.get("Content-Type", "").lower():
            raise Channel4Error("Channel 4 returned HTML instead of a subtitle")
        return response.content

    # ---------------------------------------------------------------- licence
    def service_certificate(self, token: str, video_type: str, video_url: str) -> bytes:
        data = self._license_json(
            {
                "message": SERVICE_CERTIFICATE_CHALLENGE,
                "token": token,
                "video": {"type": video_type, "url": video_url},
            }
        )
        encoded = str(data.get("license") or "")
        if not encoded:
            raise Channel4Error("Channel 4 returned no Widevine service certificate")
        return _b64decode(encoded, "service certificate")

    def license(
        self,
        challenge: bytes,
        *,
        token: str,
        video_type: str,
        video_url: str,
        request_id: str = "",
    ) -> bytes:
        payload: dict[str, Any] = {
            "message": base64.b64encode(challenge).decode("ascii"),
            "token": token,
            "video": {"type": video_type, "url": video_url},
        }
        if request_id:
            payload["request_id"] = request_id
        data = self._license_json(payload)
        encoded = str(data.get("license") or "")
        if not encoded:
            raise Channel4Error("Channel 4 licence response is incomplete")
        return _b64decode(encoded, "licence")

    def _license_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one Red Bee exchange, recovering once from a dead proxy tunnel.

        Live privacy mode makes two POSTs to the same host: the service-certificate
        exchange while resolving playback, then the content-license exchange after
        the manifest has been read and a CDM challenge has been built. Some HTTP
        proxies leave the first tunnel pooled but unusable for the second request.
        VOD normally makes only the latter exchange, which is why the same route can
        appear healthy there. Repeating the identical DRM exchange once is safe;
        ordinary catalogue/authentication writes deliberately do not use this path.
        """
        try:
            return self._json("POST", LICENSE, json=payload)
        except Channel4TransientError:
            self._discard_connections()
            return self._json("POST", LICENSE, json=payload)

    def _discard_connections(self) -> None:
        """Drop pooled transports without losing this session's proxy or cookies."""
        if isinstance(self.session, browser_requests.Session):
            previous = self.session
            self.session = tv_session(previous)
            previous.close()
            return

    # --------------------------------------------------------------- internals
    def _headers(self, *, auth: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json", "Accept-Language": "en-US,en;q=0.8", "User-Agent": USER_AGENT}
        if auth:
            if not self.authorization:
                raise Channel4Error("Channel 4 is not signed in")
            headers["Authorization"] = self.authorization
        return headers

    def _stream_headers(self) -> dict[str, str]:
        return {
            **self._headers(auth=True),
            "X-C4-Date": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }

    def _working_manifest(self, manifest: str) -> str:
        parsed = urlparse(manifest)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise Channel4Error("Channel 4 returned an invalid manifest URL")
        https_url = urlunparse(parsed._replace(scheme="https"))
        http_url = urlunparse(parsed._replace(scheme="http"))
        candidates = list(dict.fromkeys((https_url, http_url)))
        failures: list[str] = []
        statuses: list[int] = []
        for candidate in candidates:
            try:
                response = self.session.get(candidate, timeout=20)
            except browser_requests.RequestsError as exc:
                failures.append(type(exc).__name__)
                continue
            if response.ok:
                return candidate
            statuses.append(response.status_code)
            failures.append(f"HTTP {response.status_code}")
        detail = ", ".join(failures) or "no response"
        if statuses and all(status == 429 or status >= 500 for status in statuses):
            raise Channel4TransientError(f"Channel 4 manifest unavailable ({detail})")
        if not statuses:
            raise Channel4TransientError(f"Channel 4 manifest request failed ({detail})")
        raise Channel4Error(f"Channel 4 manifest unavailable ({detail})")

    def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        data = self._data(method, url, **kwargs)
        if not isinstance(data, dict):
            raise Channel4Error("Channel 4 returned something that is not an object")
        return data

    def _data(self, method: str, url: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
        try:
            response = self.session.request(method, url, timeout=35, **kwargs)
        except browser_requests.RequestsError as exc:
            raise Channel4TransientError(
                f"Channel 4 request failed ({type(exc).__name__})"
            ) from exc
        status = response.status_code
        if not response.ok:
            try:
                error = _dict(response.json())
            except ValueError:
                error = {}
            code = _int(error.get("code") or error.get("errorCode"))
            if status == 403 and code in {10200, 10302} and "location" in str(error.get("description") or "").lower():
                raise Channel4GeoError(
                    f"Channel 4 playback is blocked in the current location (HTTP 403, code {code}). "
                    "Select a UK proxy in Channel 4 settings."
                )
            message = f"Channel 4 request HTTP {status}"
            if code is not None:
                message += f" (API code {code})"
            elif "text/html" in response.headers.get("Content-Type", "").lower():
                message += " (HTML response)"
            if url.startswith(AUTH) and status == 403:
                raise Channel4AuthError("Channel 4 TV token request was blocked (HTTP 403); no session was saved")
            if status == 401 or (url.startswith(AUTH) and status in {400, 403}):
                raise Channel4AuthError(message)
            if status == 429 or status >= 500:
                raise Channel4TransientError(message)
            raise Channel4Error(message)
        try:
            data = response.json()
        except ValueError as exc:
            raise Channel4Error("Channel 4 returned something that is not JSON") from exc
        if isinstance(data, dict):
            if data.get("error") or data.get("fault"):
                raise Channel4Error("Channel 4 response reported an error")
            return data
        if isinstance(data, list):
            return data
        raise Channel4Error("Channel 4 returned an unexpected JSON value")


_IMAGE_FIELDS = ("href", "title", "type", "templated", "width", "height")
_MIME_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".avif": "image/avif", ".svg": "image/svg+xml",
}


def image_fields(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    images = [raw.get("image")]
    if isinstance(raw.get("images"), list):
        images.extend(raw["images"])
    return {"images": [
        {key: image[key] for key in _IMAGE_FIELDS if key in image}
        for image in images if isinstance(image, dict)
    ]}


def attachments(*documents: dict[str, Any]) -> list[Attachment]:
    result: list[Attachment] = []
    seen: set[str] = set()
    for document in documents:
        for group, label, kind in (
            ("episode", "Episode image", "thumbnail"),
            ("brand", "Programme artwork", "artwork"),
            ("channel", "Channel artwork", "artwork"),
        ):
            section = document.get(group)
            if not isinstance(section, dict) or not isinstance(section.get("images"), list):
                continue
            for image in section["images"]:
                if not isinstance(image, dict):
                    continue
                value = image.get("href")
                if not isinstance(value, str):
                    continue
                # TV image URLs include this optional resize template even when templated=False.
                url = value.strip().replace("{&resize}", "")
                try:
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or parsed.username is not None or parsed.password is not None
                        or any(char.isspace() or ord(char) < 32 for char in url)
                        or any(char in url for char in "{}")
                    ):
                        continue
                except ValueError:
                    continue
                if url in seen:
                    continue
                mime = str(image.get("type") or "").lower()
                if mime and not mime.startswith("image/"):
                    continue
                if not mime:
                    mime = _MIME_TYPES.get("." + parsed.path.rsplit(".", 1)[-1].lower(), "")
                image_kind = kind
                width, height = image.get("width"), image.get("height")
                if type(width) in (int, float) and type(height) in (int, float) and height > width > 0:
                    image_kind = "poster"
                title = image.get("title")
                name = title if isinstance(title, str) and title.strip() else label
                result.append(Attachment(url=url, name=name, kind=image_kind, mime_type=mime))
                seen.add(url)
    return result


def decrypt_token(token: str, *, live: bool) -> str:
    key = LIVE_TOKEN_KEY if live else VOD_TOKEN_KEY
    iv = LIVE_TOKEN_IV if live else VOD_TOKEN_IV
    try:
        cipher = AES.new(base64.b64decode(key), AES.MODE_CBC, base64.b64decode(iv))
        decoded = unpad(
            cipher.decrypt(base64.b64decode(token, validate=True)),
            AES.block_size,
        ).decode().strip()
    except Exception as exc:
        raise Channel4Error("Channel 4 returned an invalid encrypted playback token") from exc
    if "|" in decoded:
        return decoded.split("|", 1)[1].strip()
    if decoded.startswith("http"):
        values = parse_qs(urlparse(decoded).query).get("t")
        return values[0].strip() if values else decoded
    return decoded


def _profile_stream(profile: dict[str, Any]) -> dict[str, Any]:
    uri = _dict(profile.get("uriData"))
    if uri:
        return {"uri": uri.get("streamUri") or uri.get("uri"), "token": uri.get("token")}
    stream = _dict(profile.get("stream"))
    if stream:
        return stream
    streams = _list(profile.get("streams"))
    return _dict(streams[0]) if streams else {}


def _check_drm(drm_system: str) -> None:
    if drm_system not in {"widevine", "playready"}:
        raise Channel4Error("Channel 4 supports Widevine or PlayReady")


def _playready_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme == "https" and parsed.hostname == "api.channel4.com"
            and parsed.path.startswith("/online/v1/licence/")
            and parsed.username is None and parsed.password is None
            and parsed.port in {None, 443} and not parsed.fragment
            and not any(char.isspace() or ord(char) < 32 for char in url)
            and parse_qs(parsed.query).get("token")
        ):
            return url
    except (TypeError, ValueError):
        pass
    raise Channel4Error("Channel 4 returned an invalid PlayReady licence URL")


def _vod_profile(profiles: Any, drm_system: str = "widevine", *, preferred: str = "") -> dict[str, Any] | None:
    entries = [item for item in _list(profiles) if isinstance(item, dict)]
    names = (preferred, "samsungdashpr-dyn-stream-1") if drm_system == "playready" else (
        "googledashwv-dyn-stream-1",
        "widevine-stream-4",
        "widevine-stream-3",
        "widevine-stream-2",
        "widevine-stream-1",
        "dashwv-dyn-stream-1",
        "dashwv-stream-1",
    )
    prefixes = ("samsungdashpr", "dashpr") if drm_system == "playready" else ("googledashwv", "dashwv", "widevine-")
    entries = [profile for profile in entries if str(profile.get("profileName") or profile.get("name") or "").startswith(prefixes)]
    for wanted in names:
        for profile in entries:
            stream = _profile_stream(profile)
            if (profile.get("profileName") or profile.get("name")) == wanted and stream.get("uri") and stream.get("token"):
                return profile
    return next(
        (
            profile
            for profile in entries
            if _profile_stream(profile).get("uri")
            and _profile_stream(profile).get("token")
        ),
        None,
    )


def _live_profile(
    profiles: Any, restart: bool, drm_system: str = "widevine", *, preferred: str = "",
) -> dict[str, Any] | None:
    entries = [item for item in _list(profiles) if isinstance(item, dict)]
    names = (preferred,) if drm_system == "playready" else (
        ("dashwv-dai-live-stream-iso-dash-mp-tl", "dashwv-live-stream-iso-dash-sp-tl")
        if restart
        else ("dashwv-live-stream-iso-dash-sp-tl", "dashwv-dai-live-stream-iso-dash-mp-tl")
    )

    def available(profile: dict[str, Any]) -> bool:
        name = str(profile.get("profileName") or profile.get("name") or "")
        if not name.startswith("dashpr" if drm_system == "playready" else "dashwv"):
            return False
        if restart:
            stream = _dict(profile.get("startAgainStream"))
        else:
            stream = _profile_stream(profile)
        uri = stream.get("uri")
        return bool(uri and uri not in {"https://tbc.mpd", "http://tbc.mpd"} and stream.get("token"))

    for wanted in names:
        match = next(
            (profile for profile in entries if (profile.get("profileName") or profile.get("name")) == wanted and available(profile)),
            None,
        )
        if match is not None:
            return match
    return next((profile for profile in entries if available(profile)), None)


def extract_chapters(
    payload: Any,
    *,
    duration: int | float | None = None,
) -> tuple[Chapter, ...]:
    """Translate the TV playback markers into UniDL chapters.

    The TV WebApp exposes credit metadata as ``endCredits.squeezeIn`` in
    seconds.  Some playback responses also carry an explicit ``chapters`` or
    ``cuePoints`` array; those markers are preserved and the credit marker is
    added only when a credits marker is not already present at that position.
    """
    if not isinstance(payload, dict):
        return ()

    duration_value = _number(duration)
    chapters: list[Chapter] = []
    for container in _chapter_containers(payload):
        for key in ("chapters", "cuePoints"):
            for raw in _list(container.get(key)):
                chapter = _chapter_from_marker(raw, duration=duration_value)
                if chapter is not None:
                    chapters.append(chapter)
        if "endCredits" in container:
            chapter = _chapter_from_end_credits(
                container.get("endCredits"),
                duration=duration_value,
            )
            if chapter is not None and not any(
                existing.start_ms == chapter.start_ms and existing.kind == "credits"
                for existing in chapters
            ):
                chapters.append(chapter)

    if not chapters:
        return ()
    return normalize_chapters(
        chapters,
        duration_ms=_seconds_to_milliseconds(duration_value),
    )


def _chapter_containers(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return the TV response objects where playback metadata is published."""
    values: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(value: Any) -> None:
        if isinstance(value, dict) and id(value) not in seen:
            seen.add(id(value))
            values.append(value)

    add(payload)
    for key in ("episode", "assetInfo", "channelInfo"):
        child = _dict(payload.get(key))
        if child:
            add(child)
            add(child.get("assetInfo"))
            add(child.get("streaming"))
            nested_asset = _dict(child.get("assetInfo"))
            add(nested_asset.get("streaming"))
    return tuple(values)


def _chapter_from_marker(
    raw: Any,
    *,
    duration: float | None,
) -> Chapter | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or raw.get("name") or raw.get("label") or "")
    kind = str(raw.get("kind") or raw.get("type") or "")
    start_ms = _first_number(raw, "start_ms", "startMs", "startMillis")
    end_ms = _first_number(raw, "end_ms", "endMs", "endMillis")
    try:
        if start_ms is not None:
            return Chapter(
                start_ms=round(start_ms),
                end_ms=round(end_ms) if end_ms is not None else None,
                title=title,
                kind=kind,
            )
        start = _first_number(raw, "start", "startTime", "timeIn", "position")
        end = _first_number(raw, "end", "endTime", "timeOut")
        if start is None:
            return None
        if end is None and duration is not None and duration > start:
            end = duration
        return Chapter.from_seconds(start, title, end=end, kind=kind)
    except (TypeError, ValueError, OverflowError):
        return None


def _chapter_from_end_credits(
    raw: Any,
    *,
    duration: float | None,
) -> Chapter | None:
    if isinstance(raw, dict):
        start = _first_number(raw, "squeezeIn", "start", "startTime", "timeIn")
        end = _first_number(raw, "end", "endTime", "timeOut")
    else:
        start = _number(raw)
        end = None
    if start is None:
        return None
    if end is None and duration is not None and duration > start:
        end = duration
    try:
        return Chapter.from_seconds(start, "Credits", end=end, kind="credits")
    except (TypeError, ValueError, OverflowError):
        return None


def _first_number(mapping: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = _number(mapping.get(name))
        if value is not None:
            return value
    return None


def _seconds_to_milliseconds(value: float | None) -> int | None:
    if value is None:
        return None
    return round(value * 1000)


def _subtitles(data: dict[str, Any]) -> tuple[SubtitleReference, ...]:
    # The TV app uses webvtt_007; the SRT/HLS entries are alternate encodings.
    preferred: dict[tuple[str, str], SubtitleReference] = {}
    for container in (data, _dict(data.get("channelInfo"))):
        assets = _list(container.get("subtitlesAssets")) or _list(container.get("subtitleAssets"))
        for item in assets:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("href") or "").strip()
            try:
                parsed = urlparse(url)
            except ValueError:
                continue
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            asset_format = str(item.get("format") or "").lower()
            extension = {"webvtt_007": "vtt", "srt_009": "srt"}.get(asset_format)
            if not asset_format:
                extension = parsed.path.rsplit(".", 1)[-1].lower()
            if extension not in {"vtt", "srt"}:
                continue
            language = str(item.get("language") or item.get("languageCode") or "en").replace("_", "-")
            kind = str(item.get("kind") or "normal")
            identity = (language.casefold(), kind.casefold())
            existing = preferred.get(identity)
            if existing is not None and (existing.format == "vtt" or extension != "vtt"):
                continue
            preferred[identity] = SubtitleReference(
                url=url,
                language=language,
                name=str(item.get("label") or item.get("name") or ("English" if language == "en" else language)),
                kind=kind,
                format=extension,
                headers={"User-Agent": USER_AGENT, "Origin": WWW, "Referer": f"{WWW}/"},
            )
    return tuple(preferred.values())


def _subtitle(data: dict[str, Any]) -> str:
    subtitles = _subtitles(data)
    return subtitles[0].url if subtitles else ""


def _query(url: str, updates: dict[str, str | None]) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in updates.items():
        if value is not None:
            query[key] = [value]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _verification_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    value = value.strip()
    url = value if "://" in value else f"https://{value}"
    try:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or parsed.hostname not in {"channel4.com", "www.channel4.com", "c4.tv", "www.c4.tv"}
            or parsed.port not in {None, 443} or any(char.isspace() for char in url)
        ):
            return ""
    except ValueError:
        return ""
    return url


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _token_value(tokens: dict[str, Any], *names: str) -> str:
    return next((str(tokens[name]) for name in names if tokens.get(name)), "")


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _b64decode(value: str, label: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise Channel4Error(f"Channel 4 returned an invalid base64 {label}") from exc
    if not decoded:
        raise Channel4Error(f"Channel 4 returned an empty base64 {label}")
    return decoded


__all__ = [
    "API",
    "CHANNEL_IDS",
    "LICENSE",
    "USER_AGENT",
    "WWW",
    "Channel",
    "Channel4Api",
    "Channel4AuthError",
    "Channel4Error",
    "Channel4GeoError",
    "Channel4TransientError",
    "Episode",
    "ParsedInput",
    "SearchHit",
    "Season",
    "Show",
    "Source",
    "TvCode",
    "channel_id",
    "attachments",
    "image_fields",
    "decrypt_token",
    "extract_chapters",
    "parse_input",
    "tv_session",
]
