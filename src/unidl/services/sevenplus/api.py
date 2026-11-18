"""7plus (Seven Network Australia) HTTP client."""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse
from uuid import uuid4

import requests

from ...core import playready
from ...core.attachments import Attachment
from ...core.chapters import Chapter, normalize_chapters

WWW = "https://7plus.com.au"
MARKET_IP = "https://market-cdn.swm.digital/v1/market/ip/"
AUTH_TOKEN = "https://auth2.swm.digital/connect/token"
DEVICE_AUTHORIZE = "https://auth2.swm.digital/account/device/authorize"
ACCOUNT_LOGIN = "https://login.7plus.com.au/accounts.login"
GET_JWT = "https://login.7plus.com.au/accounts.getJWT"
WEBSDK_BOOTSTRAP = "https://login.7plus.com.au/accounts.webSdkBootstrap"
OTP_VERIFY = "https://7plus.com.au/auth/otp"
SEARCH = "https://searchapi.swm.digital/3.0/api/Search"
CONTENT_CDN = "https://component-cdn.swm.digital/content"
COMPONENT_V2 = "https://component.swm.digital/v2/component"
COMPONENT = "https://component.swm.digital/component"
COMPONENT_PLAYER = "https://component.swm.digital/player/live"
PLAYBACK = "https://videoservice.swm.digital/playback"

GIGYA_API_KEY = "3_kQ8RSBuBxTSP0b9TM3s3hH015iMCbIbj0NwqdUYzTTAWG1t2kjQgW4AE894-FRBk"
SWM_API_KEY = "kGcrNnuPClrkynfnKwG8IA/NhVG6ut5nPEdWF2jscvE="
TOKEN_FILE = "7plus_tokens.json"

PLATFORM_ID = "androidtv"
PLATFORM_VERSION = "8.0"
API_VERSION = "4.11"
AUTH_API_VERSION = "1.1"
USER_AGENT = "com.swm.live/8.0.1 (Android TV)"
WEB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)

TOKEN_REFRESH_WINDOW_SECONDS = 5 * 60
TOKEN_MIN_REFRESH_INTERVAL_SECONDS = 60
DEFAULT_MARKET_ID = 4  # Sydney fallback

LIVE_REGIONS: dict[str, tuple[str, int, str]] = {
    "nsw": ("NSW · Sydney", 4, "2000"),
    "vic": ("VIC · Melbourne", 26, "3000"),
    "qld": ("QLD · Brisbane", 27, "4000"),
    "sa": ("SA · Adelaide", 31, "5000"),
    "wa": ("WA · Perth", 29, "6000"),
    "tas": ("TAS · Hobart", 23, "7000"),
    "act": ("ACT · Canberra", 15, "2600"),
    "nt": ("NT · Darwin", 30, "0800"),
}

VOD_ACCOUNT_ID = "5303576322001"
LIVE_ACCOUNT_ID = "5650355166001"
VOD_OZID = "dc6095c7-e895-41d3-6609-79f673fc7f63"
LIVE_OZID = "a633739e-c82d-4d6d-b695-293067fc43e3"

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$", re.I)
_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?7plus\.com\.au/(?P<slug>[^?/]+)/?(?:\?(?P<query>.*))?$",
    re.I,
)
_SE_FROM_NAME = re.compile(r"S(\d+)\s*E(\d+)", re.I)
_CATALOGUE_SE = re.compile(r"([A-Z]+)(\d+)-(\d+)")
_CATALOGUE_E = re.compile(r"([A-Z]+)-(\d+)")
_CARD_NUM = re.compile(r"^(\d+)\.")
_REF_ID = re.compile(r"referenceId=(\d+)")
_WIDEVINE_UUID = "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"


class SevenPlusError(RuntimeError):
    """7plus rejected a request or returned an unusable shape."""


@dataclass
class Session:
    access_token: str = ""
    refresh_token: str = ""
    token_expires: int = 0
    market_id: int | None = None
    device_id: str = ""

    @property
    def access_fresh(self) -> bool:
        return bool(self.access_token) and self.token_expires > int(time.time()) + 30

    @property
    def can_refresh(self) -> bool:
        return bool(self.refresh_token)

    def to_cache(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_expires": self.token_expires,
        }
        if self.market_id is not None:
            data["market_id"] = self.market_id
        if self.device_id:
            data["device_id"] = self.device_id
        return data

    @classmethod
    def from_cache(cls, raw: dict[str, Any] | None) -> Session:
        if not raw:
            return cls(device_id=str(uuid4()))
        return cls(
            access_token=str(raw.get("access_token") or ""),
            refresh_token=str(raw.get("refresh_token") or ""),
            token_expires=_int(raw.get("token_expires")),
            market_id=_optional_int(raw.get("market_id")),
            device_id=str(raw.get("device_id") or uuid4()),
        )


@dataclass(frozen=True)
class ParsedInput:
    kind: str  # show | live | episode_hint
    value: str = ""
    episode_id: str = ""


@dataclass(frozen=True)
class SearchHit:
    title: str
    slug: str
    url: str


@dataclass(frozen=True)
class Episode:
    id: str
    title: str
    name: str
    season: int = 0
    number: int = 0
    year: str = ""
    catalogue_number: str = ""
    image_url: str = ""

    @property
    def label(self) -> str:
        if self.season > 0 and self.number > 0:
            return f"S{self.season:02d}E{self.number:02d}  {self.name or self.title}"
        if self.number > 0:
            return f"E{self.number:02d}  {self.name or self.title}"
        return self.name or self.title or self.id


@dataclass
class Season:
    number: int
    episodes: list[Episode] = field(default_factory=list)

    @property
    def label(self) -> str:
        count = len(self.episodes)
        name = f"Season {self.number}" if self.number > 0 else "Episodes"
        return f"{name}  ·  {count} episode(s)" if count else name


@dataclass
class Series:
    title: str
    slug: str
    seasons: list[Season] = field(default_factory=list)


@dataclass(frozen=True)
class Movie:
    id: str
    title: str
    year: str = ""
    image_url: str = ""


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    channel_name: str
    current_show: str = ""
    stream_id: str = ""
    player_url: str = ""
    logo_url: str = ""

    @property
    def playback_key(self) -> str:
        return self.channel_name or self.name or self.id


@dataclass(frozen=True)
class Source:
    manifest: str
    license_url: str = ""
    encrypted: bool = False
    auth_token: str = ""
    drm_provider: str = ""
    drm_headers: tuple[tuple[str, str], ...] = ()
    pssh: str = ""
    is_live: bool = False
    duration_ms: int = 0
    chapters: tuple[Chapter, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    system: str = "widevine"
    wrm_headers: tuple[str, ...] = ()

    def line(self) -> str:
        if self.encrypted:
            return ("PlayReady" if self.system == "playready" else "Widevine") + (" · live" if self.is_live else "")
        return "clear" + (" · live" if self.is_live else "")


def _image_url(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urlparse(value)
        if parsed.scheme in {"https", "http"} and parsed.hostname and parsed.username is None:
            return value
    except ValueError:
        pass
    return ""


def _playback_artwork(media: dict[str, Any]) -> tuple[Attachment, ...]:
    artwork: list[Attachment] = []
    for kind in ("poster", "thumbnail"):
        url = _image_url(media.get(kind))
        variants = media.get(f"{kind}_sources")
        if not url and isinstance(variants, list):
            url = next((
                src for entry in variants
                if isinstance(entry, dict) and (src := _image_url(entry.get("src")))
            ), "")
        if url and all(item.url != url for item in artwork):
            artwork.append(Attachment(url, kind.title(), kind))
    return tuple(artwork)


def _playback_chapters(media: dict[str, Any], duration_ms: int) -> tuple[Chapter, ...]:
    """SWM stores recap/intro boundaries and credits as Brightcove CODE cues."""
    cues = media.get("cue_points")
    if not isinstance(cues, list):
        return ()
    markers: dict[str, list[Chapter]] = {}
    for cue in cues:
        if not isinstance(cue, dict) or str(cue.get("type")).upper() != "CODE":
            continue
        name = re.sub(r"[^a-z]", "", str(cue.get("name") or "").lower())
        if name not in {"recap", "recapstart", "recapend", "intro", "introstart", "introend",
                        "credits", "creditsstart", "creditsend"}:
            continue
        try:
            chapter = Chapter.from_seconds(cue.get("time"), name)
        except (ValueError, OverflowError):
            continue
        if not duration_ms or chapter.start_ms <= duration_ms:
            markers.setdefault(name, []).append(chapter)

    chapters: list[Chapter] = []
    for kind in ("recap", "intro", "credits"):
        starts = markers.get(f"{kind}start", []) + markers.get(kind, [])
        ends = markers.get(f"{kind}end", [])
        for start in starts:
            if duration_ms and start.start_ms >= duration_ms:
                continue
            end = min((row.start_ms for row in ends if row.start_ms > start.start_ms), default=None)
            chapters.append(Chapter(start.start_ms, kind.title(), end, kind))
    return normalize_chapters(chapters, duration_ms=duration_ms or None)


def parse_input(text: str) -> ParsedInput | None:
    raw = (text or "").strip()
    if not raw:
        return None
    raw = raw.split("#", 1)[0]
    if raw.lower() == "live-tv":
        return ParsedInput("live")
    if _SLUG.fullmatch(raw) and "://" not in raw:
        return ParsedInput("show", raw.lower())

    match = _URL.match(raw)
    if match:
        slug = match.group("slug")
        query = match.group("query") or ""
        params = parse_qs(query)
        episode_id = (params.get("episode-id") or [""])[0]
        if slug.lower() == "live-tv" or "channel-id" in params:
            channel = (params.get("channel-id") or [""])[0]
            return ParsedInput("live", channel)
        return ParsedInput(
            "episode_hint" if episode_id else "show",
            slug,
            episode_id=episode_id,
        )

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    if host and host not in {"7plus.com.au", "www.7plus.com.au"}:
        return None
    return None


class SevenPlusApi:
    def __init__(
        self,
        session: requests.Session | None = None,
        state: Session | None = None,
        *,
        on_save: Callable[[Session], None] | None = None,
        security_level: str = "L3",
        system: str = "widevine",
        live_region: str = "auto",
    ):
        self.session = session or requests.Session()
        self.state = state or Session(device_id=str(uuid4()))
        self.on_save = on_save
        self.system = system if system in {"widevine", "playready"} else "widevine"
        self.security_level = security_level.upper()
        self.live_region = live_region
        self._last_refresh_at = 0
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.session.headers.setdefault("x-swm-apikey", SWM_API_KEY)
        if self.state.access_fresh:
            self.session.headers["authorization"] = f"Bearer {self.state.access_token}"


    def _persist(self) -> None:
        if self.on_save is not None:
            self.on_save(self.state)

    def ensure_market(self) -> int:
        if self.state.market_id is not None:
            return self.state.market_id
        try:
            response = self.session.get(MARKET_IP, params={"apikey": "web"}, timeout=20)
            response.raise_for_status()
            self.state.market_id = _int(response.json().get("_id"), DEFAULT_MARKET_ID) or DEFAULT_MARKET_ID
        except (requests.RequestException, ValueError, TypeError):
            self.state.market_id = DEFAULT_MARKET_ID
        return self.state.market_id

    def ensure_session(self, username: str = "", password: str = "") -> Session:
        """Use a cached access token, refresh, or sign in with credentials."""
        self.ensure_market()
        refresh_error: SevenPlusError | None = None
        if self.state.access_fresh:
            remaining = self.state.token_expires - int(time.time())
            if remaining <= TOKEN_REFRESH_WINDOW_SECONDS and self.state.can_refresh:
                try:
                    self.refresh(force=True, invalidate_on_failure=False)
                except SevenPlusError:
                    pass
            if self.state.access_fresh:
                self.session.headers["authorization"] = f"Bearer {self.state.access_token}"
                return self.state
        if self.state.can_refresh:
            try:
                return self.refresh(force=True)
            except SevenPlusError as exc:
                refresh_error = exc
        if username and password:
            return self.authenticate(username, password)
        if refresh_error is not None:
            raise SevenPlusError(f"7plus session could not be refreshed: {refresh_error}. Sign in again.") from refresh_error
        raise SevenPlusError(
            "no 7plus session. Sign in with account credentials, or place a "
            f"legacy {TOKEN_FILE} in the cache."
        )

    def refresh(self, *, force: bool = False, invalidate_on_failure: bool = True) -> Session:
        now = int(time.time())
        if not force:
            if self.state.access_fresh and (self.state.token_expires - now) > TOKEN_REFRESH_WINDOW_SECONDS:
                return self.state
            if self._last_refresh_at and (now - self._last_refresh_at) < TOKEN_MIN_REFRESH_INTERVAL_SECONDS:
                return self.state
        if not self.state.refresh_token:
            raise SevenPlusError("no 7plus refresh token")

        try:
            response = self.session.post(
                AUTH_TOKEN,
                params={"api-version": AUTH_API_VERSION},
                data={
                    "platformId": PLATFORM_ID,
                    "regSource": "7plus",
                    "refreshToken": self.state.refresh_token,
                    "deviceId": self.state.device_id,
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise SevenPlusError("could not reach 7plus token refresh endpoint") from exc
        if response.status_code != 200:
            if invalidate_on_failure:
                self.state.access_token = ""
                self.session.headers.pop("authorization", None)
            raise SevenPlusError(f"token refresh failed (HTTP {response.status_code})")
        try:
            tokens = response.json()
        except ValueError as exc:
            raise SevenPlusError("token refresh returned non-JSON") from exc
        if not isinstance(tokens, dict):
            raise SevenPlusError("token refresh returned an invalid response")
        access = tokens.get("access_token")
        if not access:
            if invalidate_on_failure:
                self.state.access_token = ""
                self.session.headers.pop("authorization", None)
            raise SevenPlusError("token refresh returned no access_token")
        self._apply_tokens(
            access_token=str(access),
            expires_in=_int(tokens.get("expires_in"), 3600),
            refresh_token=str(tokens.get("refresh_token") or self.state.refresh_token),
        )
        self._last_refresh_at = int(time.time())
        return self.state

    def authenticate(self, username: str, password: str) -> Session:
        """Gigya password login, then device pairing against auth2.swm.digital."""
        username = (username or "").strip()
        password = password or ""
        if not username or not password:
            raise SevenPlusError("7plus needs an email and password")
        self.ensure_market()

        gigya_headers = {
            "origin": WWW,
            "referer": f"{WWW}/?overlay=sign-in",
            "user-agent": WEB_USER_AGENT,
        }
        try:
            self.session.get(
                WEBSDK_BOOTSTRAP,
                params={
                    "apiKey": GIGYA_API_KEY,
                    "pageURL": f"{WWW}/?overlay=sign-in",
                    "sdk": "js_latest",
                    "sdkBuild": "1535",
                    "format": "json",
                },
                headers=gigya_headers,
                timeout=20,
            )
        except requests.RequestException:
            pass

        login_response = self.session.post(
            ACCOUNT_LOGIN,
            data={
                "loginID": username,
                "password": password,
                "sessionExpiration": -2,
                "targetEnv": "jssdk",
                "include": "profile,data",
                "includeUserInfo": True,
                "lang": "en",
                "APIKey": GIGYA_API_KEY,
                "sdk": "js_latest",
                "authMode": "cookie",
                "pageURL": f"{WWW}/?overlay=sign-in",
                "sdkBuild": "1535",
                "format": "json",
            },
            headers=gigya_headers,
            timeout=30,
        )
        if login_response.status_code != 200:
            raise SevenPlusError(f"Gigya login failed (HTTP {login_response.status_code})")
        login_data = login_response.json()
        login_token = (login_data.get("sessionInfo") or {}).get("login_token")
        if not login_token:
            for name, value in self.session.cookies.get_dict().items():
                if name.startswith("glt_"):
                    login_token = value
                    break
        if not login_token:
            reason = login_data.get("statusReason") or login_data.get("errorDetails") or "no login_token"
            code = login_data.get("errorCode")
            raise SevenPlusError(f"Gigya login failed: {reason}" + (f" ({code})" if code else ""))

        device_id = str(uuid4())
        auth_response = self.session.post(
            DEVICE_AUTHORIZE,
            data={
                "platformId": PLATFORM_ID,
                "regSource": "7plus",
                "deviceId": device_id,
                "locationVerificationRequired": "false",
            },
            timeout=30,
        )
        if auth_response.status_code != 200:
            raise SevenPlusError(f"device authorize failed (HTTP {auth_response.status_code})")
        auth = auth_response.json()
        user_code = auth.get("user_code")
        device_code = auth.get("device_code")
        if not user_code or not device_code:
            raise SevenPlusError("device authorize returned no codes")

        jwt_response = self.session.post(
            GET_JWT,
            data={
                "APIKey": GIGYA_API_KEY,
                "sdk": "js_next",
                "login_token": login_token,
                "authMode": "cookie",
                "pageURL": f"{WWW}/connect",
                "sdkBuild": "18051",
                "format": "json",
            },
            headers=gigya_headers,
            timeout=30,
        )
        if jwt_response.status_code != 200:
            raise SevenPlusError(f"Gigya JWT failed (HTTP {jwt_response.status_code})")
        id_token = jwt_response.json().get("id_token")
        if not id_token:
            raise SevenPlusError("Gigya JWT returned no id_token")

        otp_response = self.session.post(
            OTP_VERIFY,
            headers={
                "accept": "application/json, text/plain, */*",
                "accept-language": "en-US,en;q=0.9",
                "authorization": f"Bearer {id_token}",
                "content-type": "application/json;charset=UTF-8",
                "origin": WWW,
                "referer": f"{WWW}/connect",
                "user-agent": WEB_USER_AGENT,
            },
            json={
                "platformId": "web",
                "regSource": "7plus",
                "code": user_code,
                "attemptLocationPairing": False,
            },
            timeout=30,
        )
        if otp_response.status_code != 200:
            raise SevenPlusError(f"OTP verify failed (HTTP {otp_response.status_code})")

        token_response = self.session.post(
            AUTH_TOKEN,
            params={"api-version": AUTH_API_VERSION},
            data={
                "platformId": PLATFORM_ID,
                "regSource": "7plus",
                "deviceCode": device_code,
                "deviceId": device_id,
            },
            timeout=30,
        )
        if token_response.status_code != 200:
            raise SevenPlusError(f"device token failed (HTTP {token_response.status_code}): {token_response.text[:200]}")
        tokens = token_response.json()
        access = tokens.get("access_token")
        if not access:
            raise SevenPlusError("device token returned no access_token")
        self.state.device_id = device_id
        self._apply_tokens(
            access_token=str(access),
            expires_in=_int(tokens.get("expires_in"), 3600),
            refresh_token=str(tokens.get("refresh_token") or ""),
        )
        return self.state

    def _apply_tokens(self, *, access_token: str, expires_in: int, refresh_token: str) -> None:
        self.state.access_token = access_token
        self.state.refresh_token = refresh_token
        self.state.token_expires = int(time.time()) + max(expires_in, 60) - 30
        self.session.headers["authorization"] = f"Bearer {access_token}"
        self._persist()

    def _ensure_fresh(self) -> None:
        self.ensure_market()
        if self.state.access_fresh and self.state.can_refresh:
            remaining = self.state.token_expires - int(time.time())
            if remaining <= TOKEN_REFRESH_WINDOW_SECONDS:
                try:
                    self.refresh(force=False, invalidate_on_failure=False)
                except SevenPlusError:
                    pass
        elif self.state.can_refresh and not self.state.access_fresh:
            self.refresh(force=True)
        elif self.state.access_token and not self.state.access_fresh:
            raise SevenPlusError("7plus session has expired. Sign in again.")

    def _platform_params(self, **extra: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "platform-id": PLATFORM_ID,
            "market-id": extra["market-id"] if "market-id" in extra else self.ensure_market(),
            "platform-version": PLATFORM_VERSION,
            "api-version": API_VERSION,
        }
        params.update(extra)
        return params

    def search(self, query: str) -> list[SearchHit]:
        self._ensure_fresh()
        data = self._json(
            "GET",
            SEARCH,
            params={
                "searchTerm": query.strip(),
                **self._platform_params(),
            },
        )
        hits: list[SearchHit] = []
        rows = data if isinstance(data, list) else []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            title = str((raw.get("image") or {}).get("altTag") or raw.get("title") or "Unknown")
            slug_path = str((raw.get("contentLink") or {}).get("url") or "")
            if not slug_path:
                continue
            slug = slug_path.strip("/").split("?")[0].split("/")[0]
            url = f"{WWW}{slug_path}" if slug_path.startswith("/") else f"{WWW}/{slug_path}"
            hits.append(SearchHit(title=title, slug=slug, url=url))
        return hits


    def content(self, slug: str) -> dict[str, Any]:
        self._ensure_fresh()
        return self._json("GET", f"{CONTENT_CDN}/{slug}", params=self._platform_params())

    def series(self, slug: str) -> Series:
        content = self.content(slug)
        title = str(content.get("title") or slug)
        items = next((x for x in content.get("items") or [] if isinstance(x, dict) and x.get("type") == "shelfContainer"), {})
        episodes_shelf = next(
            (x for x in (items.get("items") or []) if isinstance(x, dict) and x.get("title") == "Episodes"),
            {},
        )
        seasons_container = next(
            (
                x
                for x in (episodes_shelf.get("items") or [])
                if isinstance(x, dict) and x.get("title") in ("Season", "Year", "Bulletin")
            ),
            {},
        )
        season_meta: list[tuple[str, str]] = []
        for item in seasons_container.get("items") or []:
            if not isinstance(item, dict):
                continue
            children = item.get("items") or []
            if children and isinstance(children[0], dict) and children[0].get("id"):
                season_meta.append((str(children[0]["id"]), str(item.get("title") or "Season")))
        if not season_meta:
            raise SevenPlusError(f"no seasons found for {title}")

        all_episodes: list[Episode] = []
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(season_meta)))) as pool:
            futures = [pool.submit(self._season_episodes, sid, slug) for sid, _ in season_meta]
            for future in futures:
                try:
                    all_episodes.extend(future.result())
                except Exception as exc:  # noqa: BLE001 — one season must not kill the show
                    # keep other seasons; surface partial results
                    if not all_episodes:
                        raise SevenPlusError(f"failed to load seasons for {title}: {exc}") from exc

        by_season: dict[int, list[Episode]] = {}
        for ep in all_episodes:
            by_season.setdefault(ep.season, []).append(ep)
        seasons: list[Season] = []
        for number in sorted(by_season):
            episodes = sorted(
                by_season[number],
                key=lambda e: e.number if e.number > 0 else 999,
            )
            seasons.append(Season(number=number, episodes=episodes))
        if not seasons:
            raise SevenPlusError(f"no episodes found for {title}")
        return Series(title=title, slug=slug, seasons=seasons)

    def _season_episodes(self, season_id: str, slug: str) -> list[Episode]:
        data = self._json(
            "GET",
            f"{COMPONENT}/{slug}",
            params={
                "component-id": season_id,
                **self._platform_params(),
                "signedUp": "True",
            },
        )
        episodes: list[Episode] = []
        season_num: int | None = None
        for raw in data.get("items") or []:
            if not isinstance(raw, dict):
                continue
            info_panel = raw.get("infoPanelData") or {}
            player_data = raw.get("playerData") or {}
            card_data = raw.get("cardData") or {}
            catalogue_number = str(raw.get("catalogueNumber") or "")
            video_id = str(player_data.get("episodePlayerId") or "")
            if not video_id:
                continue
            title = str(info_panel.get("title") or "")
            episode_name = str((card_data.get("image") or {}).get("altTag") or "")
            card_title = str(card_data.get("title") or "")
            name = card_title
            name = re.sub(r"^[\d\s.]+", "", name).strip()
            name = re.sub(r"^Episode\s*\d+\s*[-—]\s*", "", name, flags=re.I).strip()

            episode_num = 0
            ep_season: int | None = None
            match = _SE_FROM_NAME.search(episode_name)
            if match:
                ep_season = int(match.group(1))
                episode_num = int(match.group(2))
            if not episode_num and catalogue_number:
                match = _CATALOGUE_SE.search(catalogue_number)
                if match:
                    ep_season = int(match.group(2))
                    episode_num = int(match.group(3))
                else:
                    match = _CATALOGUE_E.search(catalogue_number)
                    if match:
                        ep_season = 1
                        episode_num = int(match.group(2))
            if not episode_num:
                match = _CARD_NUM.match(card_title)
                if match:
                    episode_num = int(match.group(1))
            if season_num is None and ep_season is not None:
                season_num = ep_season
            episodes.append(
                Episode(
                    id=video_id,
                    title=title,
                    name=name or title,
                    season=season_num if season_num is not None else (ep_season or 0),
                    number=episode_num if episode_num > 0 else 0,
                    year=str(card_data.get("productionYear") or ""),
                    catalogue_number=catalogue_number,
                    image_url=_image_url(card_data.get("image")) or _image_url(player_data.get("image")),
                )
            )
        return episodes

    def movie(self, slug: str) -> Movie:
        return self._parse_movie(self.content(slug), slug)

    @staticmethod
    def _parse_movie(content: dict[str, Any], slug: str) -> Movie:
        title = str(content.get("title") or slug)
        items = [item for item in content.get("items") or [] if isinstance(item, dict)]
        header = next((item for item in items if item.get("type") == "featuredShowHeader"), {})
        metadata = header.get("videoMetadata") if isinstance(header.get("videoMetadata"), dict) else {}
        if not metadata:
            raise SevenPlusError(f"no movie metadata for {title}")
        video_id = str(metadata.get("videoBref") or "")
        if not video_id:
            raise SevenPlusError(f"no video id for {title}")
        return Movie(
            id=video_id,
            title=title,
            year=str(metadata.get("productionYear") or ""),
            image_url=_image_url(header.get("backgroundImagePortrait")) or _image_url(header.get("videoImage")),
        )

    def resolve(self, slug: str) -> Series | Movie:
        content = self.content(slug)
        if _looks_like_movie(content):
            return self._parse_movie(content, slug)
        try:
            return self._parse_series(content, slug)
        except SevenPlusError:
            return self._parse_movie(content, slug)

    def _parse_series(self, content: dict[str, Any], slug: str) -> Series:
        title = str(content.get("title") or slug)
        items = next((x for x in content.get("items") or [] if isinstance(x, dict) and x.get("type") == "shelfContainer"), {})
        episodes_shelf = next(
            (x for x in (items.get("items") or []) if isinstance(x, dict) and x.get("title") == "Episodes"),
            {},
        )
        seasons_container = next(
            (
                x
                for x in (episodes_shelf.get("items") or [])
                if isinstance(x, dict) and x.get("title") in ("Season", "Year", "Bulletin")
            ),
            {},
        )
        season_meta: list[str] = []
        for item in seasons_container.get("items") or []:
            if not isinstance(item, dict):
                continue
            children = item.get("items") or []
            if children and isinstance(children[0], dict) and children[0].get("id"):
                season_meta.append(str(children[0]["id"]))
        if not season_meta:
            raise SevenPlusError(f"no seasons found for {title}")

        all_episodes: list[Episode] = []
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(season_meta)))) as pool:
            futures = [pool.submit(self._season_episodes, sid, slug) for sid in season_meta]
            for future in futures:
                all_episodes.extend(future.result())

        by_season: dict[int, list[Episode]] = {}
        for ep in all_episodes:
            by_season.setdefault(ep.season, []).append(ep)
        seasons = [
            Season(
                number=number,
                episodes=sorted(eps, key=lambda e: e.number if e.number > 0 else 999),
            )
            for number, eps in sorted(by_season.items())
        ]
        if not seasons:
            raise SevenPlusError(f"no episodes found for {title}")
        return Series(title=title, slug=slug, seasons=seasons)


    def live_market(self) -> int:
        region = LIVE_REGIONS.get(self.live_region)
        return region[1] if region else self.ensure_market()

    def live_postcode(self) -> str:
        region = LIVE_REGIONS.get(self.live_region)
        if region:
            return region[2]
        market = self.ensure_market()
        return next((row[2] for row in LIVE_REGIONS.values() if row[1] == market), "")

    def channels(self) -> list[Channel]:
        self._ensure_fresh()
        data = self._json(
            "GET",
            f"{COMPONENT_V2}/live-tv",
            params={
                "component-id": "489700",
                **self._platform_params(**{"market-id": self.live_market()}),
                "signedUp": "True",
            },
        )
        channels: list[Channel] = []
        for item in data.get("mediaItems") or []:
            if not isinstance(item, dict) or item.get("type") != "ChannelItem":
                continue
            current = "N/A"
            schedules = (item.get("schedules") or {}).get("items") or []
            for schedule in schedules:
                if isinstance(schedule, dict) and _int(schedule.get("progress")) > 0:
                    current = f"{schedule.get('title', 'N/A')} - {schedule.get('subTitle', '')}".strip(" -")
                    break
            if current == "N/A" and schedules and isinstance(schedules[0], dict):
                current = f"{schedules[0].get('title', 'N/A')} - {schedules[0].get('subTitle', '')}".strip(" -")
            player_url = ""
            for source in (item.get("schedules") or {}).get("sourceList") or []:
                if isinstance(source, dict) and source.get("type") == "player":
                    player_url = str(source.get("url") or "")
                    break
            channel_id = str(item.get("channelId") or "")
            channels.append(
                Channel(
                    id=channel_id,
                    name=str(item.get("name") or channel_id),
                    channel_name=str(item.get("channelName") or item.get("name") or channel_id),
                    current_show=current,
                    stream_id=channel_id,
                    player_url=player_url,
                    logo_url=_image_url(item.get("channelLogo")) or _image_url(item.get("altChannelLogo")),
                )
            )
        return channels

    def live_source(self, channel: Channel | str) -> Source:
        self._ensure_fresh()
        key = channel.playback_key if isinstance(channel, Channel) else str(channel)
        player = self._json(
            "GET",
            f"{COMPONENT_PLAYER}/{key}",
            params=self._platform_params(signedUp="True", **{"market-id": self.live_market()}),
            headers={"X-Client-Capabilities": "drm-auth", "X-USE-AUTHENTICATION": "UseTokenAuthentication"},
        )
        video_player = player.get("videoPlayer") or {}
        video_url = str(video_player.get("videoUrl") or "")
        video_id = None
        match = _REF_ID.search(video_url)
        if match:
            video_id = match.group(1)
        if not video_id:
            video_id = video_player.get("videoReferenceId")
        if not video_id:
            raise SevenPlusError(f"no video id for live channel {key}")

        if video_url:
            playback_url, playback_params = self._playback_request(video_url, live=True, video_id=str(video_id))
        else:
            playback_url = PLAYBACK
            playback_params = {
                "appId": "7plus",
                "deviceType": PLATFORM_ID,
                "platformType": "tv",
                "deviceId": self.state.device_id or str(uuid4()),
                "pc": self.live_postcode(),
                "advertid": "null",
                "accountId": LIVE_ACCOUNT_ID,
                "referenceId": str(video_id),
                "deliveryId": "csai",
                "videoType": "live",
                "marketId": self.live_market(),
                "tvid": "null",
                "ozid": LIVE_OZID,
                "sdkverification": "true",
            }
        data = self._json(
            "GET",
            playback_url,
            params=playback_params,
            headers=self._video_capability_headers(),
        )
        return self._parse_playback(data, is_live=True)

    def source(self, video_id: str) -> Source:
        self._ensure_fresh()
        data = self._json(
            "GET",
            PLAYBACK,
            params={
                "appId": "7plus",
                "deviceType": PLATFORM_ID,
                "platformType": "tv",
                "deviceId": self.state.device_id or str(uuid4()),
                "pc": 3181,
                "advertid": "null",
                "accountId": VOD_ACCOUNT_ID,
                "referenceId": f"ref:{video_id}",
                "deliveryId": "csai",
                "marketId": self.ensure_market(),
                "ozid": VOD_OZID,
                "sdkverification": "true",
            },
            headers=self._video_capability_headers(),
        )
        return self._parse_playback(data, is_live=False)

    def _playback_request(
        self, video_url: str, *, live: bool, video_id: str = ""
    ) -> tuple[str, dict[str, str]]:
        """Apply the current app's playback URL contract to a player URL."""
        parsed = urlparse(video_url)
        if not parsed.scheme or not parsed.netloc:
            raise SevenPlusError("7plus returned an invalid playback URL")
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        params.update(
            {
                "deliveryId": "csai",
                "advertid": "null",
                "deviceId": self.state.device_id or str(uuid4()),
                "pc": self.live_postcode() if live else "3181",
                "deviceType": PLATFORM_ID,
                "ozid": LIVE_OZID if live else VOD_OZID,
                "sdkverification": "true",
            }
        )
        params.pop("ppId", None)
        if video_id:
            params["referenceId"] = video_id
        if live:
            params["videoType"] = "live"
            params["marketId"] = str(self.live_market())
        return parsed._replace(query="", fragment="").geturl(), params

    def _video_capability_headers(self) -> dict[str, str]:
        """Use the app's capability schema, declaring the selected CDM's level."""
        levels = {"SL150", "SL2000", "SL3000"} if self.system == "playready" else {"L1", "L2", "L3"}
        level = self.security_level if self.security_level in levels else (
            "SL2000" if self.system == "playready" else "L3"
        )
        capabilities = {
            "encryptionType": ["cbcs"],
            "drmSystems": [{"maxSL": level, "system": self.system}],
            "containerFormat": ["cmaf", "ts"],
            "codecs": ["avc"],
            "drmAuth": True,
            "supportDiscontinuity": "true",
        }
        return {
            "X-Client-Capabilities": json.dumps(capabilities, separators=(",", ":")),
            "X-USE-AUTHENTICATION": "UseTokenAuthentication",
        }

    def _parse_playback(self, data: dict[str, Any], *, is_live: bool) -> Source:
        media = data.get("media") or {}
        sources = media.get("sources") or []
        encrypted = bool(media.get("stream_type_drm"))
        manifest = ""
        license_url = ""
        auth_token = ""
        drm_provider = ""
        drm_headers: list[tuple[str, str]] = []
        properties = data.get("properties") or media.get("properties") or {}
        if isinstance(properties, dict):
            license_headers = properties.get("playreadyHeaders" if self.system == "playready" else "widevineHeaders")
            if isinstance(license_headers, dict):
                drm_headers = [
                    (str(name), str(value))
                    for name, value in license_headers.items()
                    if str(name).strip() and value is not None
                ]

        if encrypted:
            best_rank = -1
            for entry in sources:
                if not isinstance(entry, dict):
                    continue
                systems = entry.get("key_systems") or {}
                if not isinstance(systems, dict):
                    continue
                protection = systems.get("com.microsoft.playready" if self.system == "playready" else "com.widevine.alpha")
                if not isinstance(protection, dict) or not entry.get("src"):
                    continue
                rank = int(entry.get("type") == "application/dash+xml")
                if self.system == "playready" and "com.widevine.alpha" in systems:
                    rank += 2
                if rank < best_rank:
                    continue
                best_rank = rank
                manifest = str(entry["src"])
                license_url = str(protection.get("license_url") or "")
                auth_token = ""
                drm_provider = ""
                auth = systems.get("authorization")
                if isinstance(auth, dict) and auth.get("token"):
                    auth_token = str(auth["token"])
                provider = systems.get("drm_provider")
                if isinstance(provider, str):
                    drm_provider = provider.strip().lower()
        else:
            for entry in sources:
                if not isinstance(entry, dict):
                    continue
                type_ = str(entry.get("type") or "")
                src = str(entry.get("src") or "")
                if not src:
                    continue
                if type_ in {"application/dash+xml", "application/vnd.apple.mpegurl"}:
                    manifest = src
                    break
                if not manifest:
                    manifest = src
            if not manifest and sources and isinstance(sources[0], dict):
                manifest = str(sources[0].get("src") or "")

        if is_live:
            manifest = self._short_live_manifest(manifest)

        if not manifest:
            raise SevenPlusError(f"7plus returned no playable {self.system} source")

        pssh = ""
        wrm_headers: tuple[str, ...] = ()
        if encrypted and self.system == "playready" and ".m3u8" in urlparse(manifest).path.lower():
            wrm_headers = self.playready_headers(manifest)
        elif encrypted and self.system == "widevine":
            pssh = self.extract_pssh(manifest) or ""

        duration_ms = max(0, _int(media.get("duration"))) if not is_live else 0
        return Source(
            manifest=manifest,
            license_url=license_url,
            encrypted=encrypted,
            auth_token=auth_token,
            drm_provider=drm_provider,
            drm_headers=tuple(drm_headers),
            pssh=pssh,
            is_live=is_live,
            duration_ms=duration_ms,
            chapters=_playback_chapters(media, duration_ms) if not is_live else (),
            attachments=_playback_artwork(media),
            system=self.system,
            wrm_headers=wrm_headers,
        )

    @staticmethod
    def _short_live_manifest(manifest: str) -> str:
        parsed = urlparse(manifest)
        host = (parsed.hostname or "").lower()
        prefix = "/csm/extlive/"
        if (
            parsed.scheme in {"http", "https"}
            and (host == "yospace.com" or host.endswith(".yospace.com"))
            and parsed.path.startswith(prefix)
            and "/" not in parsed.path[len(prefix):]
            and parsed.path.lower().endswith(".m3u8")
        ):
            authorization = [(name, value) for name, value in parse_qsl(parsed.query, keep_blank_values=True) if name == "yo.pp"]
            return parsed._replace(query=urlencode(authorization)).geturl()
        return manifest

    def playready_headers(self, manifest_url: str) -> tuple[str, ...]:
        try:
            response = self.session.get(manifest_url, timeout=30)
            response.raise_for_status()
            text = response.text
            objects = playready.playready_objects_from_hls(text)
            if not objects:
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                candidates: list[tuple[int, str]] = []
                for index, line in enumerate(lines[:-1]):
                    if not line.startswith("#EXT-X-STREAM-INF:"):
                        continue
                    match = re.search(r"(?:^|,)BANDWIDTH=(\d+)", line.partition(":")[2])
                    uri = lines[index + 1]
                    if match and not uri.startswith("#"):
                        candidates.append((int(match.group(1)), urljoin(manifest_url, uri)))
                if candidates:
                    media = self.session.get(max(candidates, key=lambda pair: pair[0])[1], timeout=30)
                    media.raise_for_status()
                    objects = playready.playready_objects_from_hls(media.text)
            headers: list[str] = []
            for obj in dict.fromkeys(objects):
                for header in playready.wrm_headers_from_pssh(obj):
                    if header not in headers:
                        headers.append(header)
            if not headers:
                raise SevenPlusError("7plus live playlist contains no PlayReady init data")
            return tuple(headers)
        except (requests.RequestException, playready.PlayReadyUnavailable) as exc:
            raise SevenPlusError("could not read 7plus PlayReady playlist init data") from exc

    @staticmethod
    def _license_headers(
        auth_token: str, drm_provider: str, headers: dict[str, str] | None = None
    ) -> dict[str, str]:
        result = dict(headers or {})
        names = {key.lower() for key in result}
        if auth_token and drm_provider.strip().lower() == "brightcove":
            if "bcov-auth" not in names:
                result["BCOV-Auth"] = auth_token
        elif auth_token and "authorization" not in names and "bcov-auth" not in names:
            result["Authorization"] = auth_token if auth_token.lower().startswith("bearer ") else f"Bearer {auth_token}"
        return result

    def playready_license(
        self,
        license_url: str,
        challenge: str,
        *,
        auth_token: str = "",
        drm_provider: str = "",
        headers: dict[str, str] | None = None,
    ) -> str:
        """7plus-owned SOAP transport; never use the Widevine licence endpoint."""
        if not license_url:
            raise SevenPlusError("no PlayReady licence URL")
        request_headers = {
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": playready.SOAP_ACTION,
            **self._license_headers(auth_token, drm_provider, headers),
        }
        try:
            response = self.session.post(
                license_url,
                data=challenge.encode("utf-8") if isinstance(challenge, str) else bytes(challenge),
                headers=request_headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise SevenPlusError("could not reach 7plus PlayReady licence server") from exc
        if response.status_code >= 400:
            raise SevenPlusError(f"PlayReady licence HTTP {response.status_code}: {response.text[:200]}")
        return response.text

    def widevine_license(
        self,
        license_url: str,
        challenge: bytes,
        *,
        auth_token: str = "",
        drm_provider: str = "",
        headers: dict[str, str] | None = None,
    ) -> bytes:
        if not license_url:
            raise SevenPlusError("no Widevine licence URL")
        req_headers = {"Content-Type": "application/octet-stream"}
        if headers:
            req_headers.update(headers)
        token = auth_token
        if not token:
            for name in ("Authorization", "authorization", "BCOV-Auth", "bcov-auth"):
                value = (headers or {}).get(name, "")
                if value:
                    token = value.removeprefix("Bearer ").strip()
                    break
        header_names = {key.lower() for key in req_headers}
        if token and drm_provider.strip().lower() == "brightcove":
            if "bcov-auth" not in header_names:
                req_headers["BCOV-Auth"] = token
        elif token and "authorization" not in header_names and "bcov-auth" not in header_names:
            req_headers["Authorization"] = f"Bearer {token}" if not token.lower().startswith("bearer ") else token
        try:
            response = self.session.post(license_url, data=challenge, headers=req_headers, timeout=30)
        except requests.RequestException as exc:
            raise SevenPlusError(f"licence request failed: {exc}") from exc
        if response.status_code >= 400:
            raise SevenPlusError(f"licence HTTP {response.status_code}: {response.text[:200]}")
        return response.content

    def extract_pssh(self, manifest_url: str) -> str | None:
        try:
            if ".m3u8" in manifest_url:
                return self._pssh_from_hls(manifest_url)
            response = self.session.get(manifest_url, timeout=30)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            namespaces = {
                "mpd": "urn:mpeg:dash:schema:mpd:2011",
                "cenc": "urn:mpeg:cenc:2013",
            }
            for protection in root.findall(".//mpd:ContentProtection", namespaces):
                scheme = protection.get("schemeIdUri", "")
                if _WIDEVINE_UUID not in scheme.lower():
                    continue
                pssh_el = protection.find(".//cenc:pssh", namespaces)
                if pssh_el is not None and pssh_el.text:
                    return pssh_el.text.strip()
            for protection in root.iter():
                if not protection.tag.endswith("ContentProtection"):
                    continue
                scheme = (protection.get("schemeIdUri") or "").lower()
                if _WIDEVINE_UUID not in scheme:
                    continue
                for child in protection.iter():
                    if child.tag.endswith("pssh") and child.text:
                        return child.text.strip()
        except Exception:  # noqa: BLE001 — PSSH is best-effort; core can still try
            return None
        return None

    def _pssh_from_hls(self, manifest_url: str) -> str | None:
        response = self.session.get(manifest_url, timeout=30)
        response.raise_for_status()
        content = response.text
        pssh = self._widevine_pssh_from_hls_text(content)
        if pssh:
            return pssh
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        best_bandwidth = -1
        best_url: str | None = None
        for i, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF"):
                continue
            bw_match = re.search(r"BANDWIDTH=(\d+)", line)
            if not bw_match:
                continue
            bandwidth = int(bw_match.group(1))
            variant = None
            for j in range(i + 1, len(lines)):
                if not lines[j].startswith("#"):
                    variant = lines[j]
                    break
            if variant and bandwidth > best_bandwidth:
                best_bandwidth = bandwidth
                best_url = urljoin(manifest_url, variant)
        if best_url:
            media = self.session.get(best_url, timeout=30)
            media.raise_for_status()
            return self._widevine_pssh_from_hls_text(media.text)
        return None

    @staticmethod
    def _widevine_pssh_from_hls_text(content: str) -> str | None:
        for raw in content.splitlines():
            line = raw.strip()
            if not (line.startswith("#EXT-X-KEY:") or line.startswith("#EXT-X-SESSION-KEY:")):
                continue
            fmt = re.search(r'KEYFORMAT="([^"]+)"', line)
            if not fmt or fmt.group(1).strip().lower() != f"urn:uuid:{_WIDEVINE_UUID}":
                continue
            uri = re.search(r'URI="([^"]+)"', line)
            if not uri:
                continue
            value = uri.group(1).strip()
            if value.startswith("data:text/plain;base64,"):
                pssh = value.split("base64,", 1)[1].strip()
                if pssh:
                    return pssh
        return None

    def _json(self, method: str, url: str, **kwargs: Any) -> Any:
        timeout = kwargs.pop("timeout", 30)
        try:
            response = self.session.request(method, url, timeout=timeout, **kwargs)
            if response.status_code == 401 and method.upper() == "GET" and self.state.can_refresh:
                self.refresh(force=True)
                response = self.session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            raise SevenPlusError(f"could not reach 7plus: {exc}") from exc
        if response.status_code >= 400:
            raise SevenPlusError(f"7plus HTTP {response.status_code}: {response.text[:240]}")
        try:
            return response.json()
        except ValueError as exc:
            raise SevenPlusError("7plus returned non-JSON") from exc


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _looks_like_movie(content: dict[str, Any]) -> bool:
    title = str(content.get("title") or "")
    items = [item for item in content.get("items") or [] if isinstance(item, dict)]
    header = next((item for item in items if item.get("type") == "featuredShowHeader"), {})
    metadata = header.get("videoMetadata") if isinstance(header.get("videoMetadata"), dict) else {}
    if not metadata.get("videoBref") or not re.fullmatch(r"(?:19|20)\d{2}", str(metadata.get("productionYear") or "")):
        return False

    shelf = next((item for item in items if item.get("type") == "shelfContainer"), {})
    episodes = next(
        (
            item
            for item in shelf.get("items") or []
            if isinstance(item, dict) and item.get("title") == "Episodes"
        ),
        {},
    )
    year_group = next(
        (
            item
            for item in episodes.get("items") or []
            if isinstance(item, dict) and item.get("title") == "Year"
        ),
        {},
    )
    year_tabs = [item for item in year_group.get("items") or [] if isinstance(item, dict)]
    return len(year_tabs) == 1 and _same_title(title, str(header.get("subtitle") or ""))


def _same_title(left: str, right: str) -> bool:
    def normalize(value: str) -> str:
        return "".join(char for char in value.casefold() if char.isalnum())

    return bool(normalize(left)) and normalize(left) == normalize(right)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "Channel",
    "Episode",
    "Movie",
    "ParsedInput",
    "SearchHit",
    "Season",
    "Series",
    "Session",
    "SevenPlusApi",
    "SevenPlusError",
    "Source",
    "TOKEN_FILE",
    "USER_AGENT",
    "WWW",
    "parse_input",
]
