"""TVNZ+ Android TV API client."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

API_USER = "qpapiuser"
API_PASSWORD = "Tv9z@pi2026$"
CHANNEL_ID = "TVNZ_NZ"
CLIENT_ID = "tvnz-tvnz-androidtv"
DEVICE_TYPE = "androidtv"
APP_STORE_TYPE = "androidtv"
DEFAULT_CHRT = "cluster_3"
EDGE_CLIENT_ID = "android-ui-app"
EDGE_CLIENT_SECRET = "dcb2b779-1cf8-4672-81ed-8ee1a8345389"

EVERGENT_BASE_URL = "https://rest-prod-tvnz.evergentpd.com/tvnz"
DATA_STORE_BASE_URL = "https://data-store-cdn.cms-api.tvnz.co.nz"
SEARCH_BASE_URL = "https://search-cdn.cms-api.tvnz.co.nz"
EDGE_BASE_URL = "https://watch-cdn.edge-api.tvnz.co.nz"
EDGE_OAUTH_URL = f"{EDGE_BASE_URL}/oauth2/token"
USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 12; Android TV Build/STT2.230922.001)"
ANDROID_DEVICE_NAME = "sdk_google_atv_x86"
ANDROID_DEVICE_MODEL = "Android TV"
HTTP_TIMEOUT = 20
TOKEN_EXPIRY_SKEW = 300
CACHE_VERSION = 1
TOKEN_FILE = "tvnz_token.json"
DEVICE_FILE = "tvnz_device.json"

ROUTE_TYPES = {
    "tvseries",
    "tvepisode",
    "movie",
    "newsclip",
    "liveevent",
    "event",
    "sporthighlight",
    "sportclip",
}
_URL_RE = re.compile(
    r"^/(?:player/)?"
    r"(?P<kind>tvseries|tvepisode|movie|newsclip|liveevent|event|sporthighlight|sportclip)/"
    r"(?P<slug>[^/?#]+)(?:/|$)",
    re.IGNORECASE,
)


class TvnzError(RuntimeError):
    """A user-actionable TVNZ API or schema failure."""


@dataclass(frozen=True)
class ParsedInput:
    kind: str
    slug: str


@dataclass
class SessionState:
    email: str = ""
    login_method: str = ""
    access_token: str = ""
    refresh_token: str = ""
    contact_id: str = ""
    cp_customer_id: str = ""
    access_expires_at: float = 0.0
    ovat_token: str = ""
    ovat_expires_at: float = 0.0
    device_id: str = ""

    @classmethod
    def from_cache(
        cls,
        data: dict[str, Any] | None,
        *,
        device_id: str,
    ) -> SessionState:
        raw = data if isinstance(data, dict) else {}
        if raw and raw.get("schema") != CACHE_VERSION:
            return cls(device_id=device_id)
        cached_device = str(raw.get("device_id") or "")
        if cached_device and cached_device != device_id:
            return cls(device_id=device_id)
        return cls(
            email=str(raw.get("email") or ""),
            login_method=str(raw.get("login_method") or ""),
            access_token=str(raw.get("access_token") or ""),
            refresh_token=str(raw.get("refresh_token") or ""),
            contact_id=str(raw.get("contact_id") or ""),
            cp_customer_id=str(raw.get("cp_customer_id") or ""),
            access_expires_at=_float(raw.get("access_expires_at")),
            ovat_token=str(raw.get("ovat_token") or ""),
            ovat_expires_at=_float(raw.get("ovat_expires_at")),
            device_id=cached_device or device_id,
        )

    def to_cache(self) -> dict[str, Any]:
        return {
            "schema": CACHE_VERSION,
            "email": self.email,
            "login_method": self.login_method,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "contact_id": self.contact_id,
            "cp_customer_id": self.cp_customer_id,
            "access_expires_at": self.access_expires_at,
            "ovat_token": self.ovat_token,
            "ovat_expires_at": self.ovat_expires_at,
            "device_id": self.device_id,
        }

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token and self.contact_id and self.device_id)

    @property
    def can_refresh(self) -> bool:
        return bool(self.refresh_token and self.contact_id and self.device_id)

    def access_fresh(self, skew: int = TOKEN_EXPIRY_SKEW) -> bool:
        return bool(self.access_token and self.access_expires_at > time.time() + skew)

    def ovat_fresh(self, skew: int = TOKEN_EXPIRY_SKEW) -> bool:
        return bool(self.ovat_token and self.ovat_expires_at > time.time() + skew)

    @property
    def ready(self) -> bool:
        return self.signed_in and self.access_fresh() and self.ovat_fresh()


@dataclass(frozen=True)
class Account:
    email: str
    account_type: str


@dataclass(frozen=True)
class OtpChallenge:
    current_attempt: int
    maximum_attempts: int


@dataclass(frozen=True)
class SearchHit:
    kind: str
    slug: str
    title: str
    description: str = ""
    starts_at: datetime | None = None
    playable: bool = True

    @property
    def label(self) -> str:
        return self.title

    @property
    def detail(self) -> str:
        parts = [_kind_label(self.kind)]
        if self.starts_at:
            parts.append(self.starts_at.astimezone().strftime("%Y-%m-%d %H:%M"))
        if self.description:
            parts.append(self.description[:90])
        return " - ".join(part for part in parts if part)


@dataclass(frozen=True)
class Series:
    id: str
    slug: str
    title: str
    description: str = ""


@dataclass(frozen=True)
class Season:
    id: str
    number: int
    name: str

    @property
    def label(self) -> str:
        return self.name or f"Season {self.number}"


@dataclass(frozen=True)
class Episode:
    content_id: str
    series_title: str
    name: str
    season: int
    number: int
    description: str = ""

    @property
    def label(self) -> str:
        head = f"S{self.season:02d}E{self.number:02d}"
        return f"{head}  {self.name}" if self.name else head


@dataclass(frozen=True)
class Content:
    kind: str
    content_id: str
    title: str
    description: str = ""
    year: str = ""
    season: int = 0
    episode: int = 0
    episode_name: str = ""
    competition: str = ""
    duration: int = 0
    live_content_id: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    window_starts_at: datetime | None = None
    window_ends_at: datetime | None = None

    def live_playback_mode(self, now: datetime | None = None) -> str:
        if self.kind != "liveevent":
            raise TvnzError("Playback mode is only defined for TVNZ live events")
        if not all(
            (
                self.starts_at,
                self.ends_at,
                self.window_starts_at,
                self.window_ends_at,
            )
        ):
            raise TvnzError(f"{self.title} is missing its live event window")
        current = now or datetime.now(timezone.utc)
        if current < self.window_starts_at:  # type: ignore[operator]
            when = self.window_starts_at.astimezone().strftime("%Y-%m-%d %H:%M")
            raise TvnzError(f"{self.title} is not available until {when}")
        if current <= self.ends_at:  # type: ignore[operator]
            return "live"
        if current <= self.window_ends_at:  # type: ignore[operator]
            return "catchup"
        raise TvnzError(f"{self.title} is no longer available")


@dataclass(frozen=True)
class Channel:
    content_id: str
    name: str
    now: str = ""
    description: str = ""


@dataclass(frozen=True)
class Source:
    manifest: str
    license_url: str = ""
    edge_access_token: str = ""
    protected: bool = False
    is_live: bool = False
    playback_mode: str = ""

    def line(self) -> str:
        drm = "Widevine" if self.protected else "clear"
        mode = f" - {self.playback_mode}" if self.playback_mode else ""
        return f"DASH - {drm}{mode}"


def parse_input(text: str) -> ParsedInput | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host != "tvnz.co.nz" and not host.endswith(".tvnz.co.nz"):
        return None
    match = _URL_RE.match(parsed.path)
    if not match:
        return None
    return ParsedInput(match.group("kind").lower(), match.group("slug"))


class TvnzApi:
    def __init__(
        self,
        session: requests.Session,
        *,
        device_id: str,
        state: SessionState | None = None,
        on_save: Callable[[SessionState], None] | None = None,
    ) -> None:
        self.session = session
        self.device_id = device_id
        self.state = state or SessionState(device_id=device_id)
        self.state.device_id = device_id
        self.on_save = on_save

    def account(self, email: str) -> Account:
        response = self._evergent(
            "searchAccountV2",
            "SearchAccountV2RequestMessage",
            "SearchAccountV2ResponseMessage",
            {"email": email},
            action="account lookup",
        )
        return Account(email=email, account_type=str(response.get("accountType") or ""))

    def login_password(self, email: str, password: str) -> SessionState:
        response = self._evergent(
            "getOAuthAccessTokenv2",
            "GetOAuthAccessTokenv2RequestMessage",
            "GetOAuthAccessTokenv2ResponseMessage",
            {
                "contactPassword": password,
                "contactUserName": email,
                "deviceMessage": self._device_message(),
            },
            action="password login",
        )
        self._apply_token_response(response, email=email, login_method="account", initial=True)
        return self.state

    def create_otp(self, email: str) -> OtpChallenge:
        response = self._evergent(
            "createOTP",
            "CreateOTPRequestMessage",
            "CreateOTPResponseMessage",
            {"email": email},
            action="OTP request",
        )
        return OtpChallenge(
            current_attempt=_int(response.get("currentOTPCount")),
            maximum_attempts=_int(response.get("maxOTPCount")),
        )

    def confirm_otp(self, email: str, code: str) -> SessionState:
        response = self._evergent(
            "confirmOTP",
            "ConfirmOTPRequestMessage",
            "ConfirmOTPResponseMessage",
            {
                "otp": code,
                "country": "nz",
                "deviceDetails": self._device_message(),
                "email": email,
                "isGenerateJWT": True,
            },
            action="OTP verification",
        )
        self._apply_token_response(response, email=email, login_method="otp", initial=True)
        return self.state

    def refresh(self) -> SessionState:
        if not self.state.can_refresh:
            raise TvnzError("TVNZ session has no refresh token or contact ID")
        response = self._evergent(
            "refreshToken",
            "RefreshTokenRequestMessage",
            "RefreshTokenResponseMessage",
            {"refreshToken": self.state.refresh_token},
            action="token refresh",
        )
        self._apply_token_response(
            response,
            email=self.state.email,
            login_method=self.state.login_method,
            initial=False,
        )
        return self.state

    def refresh_entitlements(self) -> SessionState:
        if not self.state.access_fresh(skew=60) or not self.state.contact_id:
            raise TvnzError("TVNZ needs a current access token and contact ID for entitlements")
        response = self._evergent(
            "getEntitlements",
            "GetEntitlementsRequestMessage",
            "GetEntitlementsResponseMessage",
            {
                "contactID": self.state.contact_id,
                "returnUpgradableFlag": True,
            },
            action="entitlement refresh",
            access_token=self.state.access_token,
        )
        token = str(response.get("ovatToken") or "")
        if not token:
            raise TvnzError("TVNZ entitlement response did not contain ovatToken")
        lifetime = _int(response.get("ovatTokenExpiry"))
        if lifetime <= 0 or lifetime > 7 * 24 * 60 * 60:
            raise TvnzError("TVNZ returned an invalid OVAT lifetime")
        self.state.ovat_token = token
        self.state.ovat_expires_at = time.time() + lifetime
        self._persist()
        return self.state

    def ensure_auth(self) -> SessionState:
        if self.state.ready:
            return self.state
        if not self.state.access_fresh():
            if not self.state.can_refresh:
                raise TvnzError("Sign in to TVNZ+ before opening catalogue or playback")
            self.refresh()
        if not self.state.ovat_fresh():
            self.refresh_entitlements()
        if not self.state.ready:
            raise TvnzError("TVNZ session could not be prepared for playback")
        return self.state

    def clear_session(self) -> None:
        self.state = SessionState(device_id=self.device_id)

    def search(self, query: str, *, page_size: int = 50) -> list[SearchHit]:
        wanted = str(query or "").strip()
        if not wanted:
            return []
        payload = self._json(
            "GET",
            f"{SEARCH_BASE_URL}/content/search",
            params={
                "mode": "detail",
                "st": "published",
                "term": wanted,
                "pageNumber": 1,
                "pageSize": page_size,
                **self._catalog_params(),
            },
            headers=self._app_headers(),
        )
        header = _dict(payload.get("header"), "search header")
        if header.get("code") not in (None, 0):
            raise TvnzError(str(header.get("message") or "TVNZ search failed"))
        rows = _list(payload.get("data"), "search data")
        return [self._search_hit(row) for row in rows if isinstance(row, dict)]

    def series(self, slug: str) -> Series:
        data = self._catalog("tvseries", slug)
        return Series(
            id=_required(data, "id", "series"),
            slug=_required(data, "nu", "series"),
            title=_localized_required(data, "lon", "series title"),
            description=_localized(data, "lod"),
        )

    def seasons(self, series_id: str) -> list[Season]:
        payload = self._json(
            "GET",
            f"{DATA_STORE_BASE_URL}/content/series/{series_id}/seasons",
            params=self._catalog_params(
                {
                    "pageNumber": 1,
                    "pageSize": 30,
                    "sortBy": "snum",
                    "sortOrder": "asc",
                }
            ),
        )
        rows = _list(payload.get("data"), "season data")
        seasons = []
        for row in rows:
            item = _dict(row, "season")
            number = _int_required(item.get("snum"), "season number")
            seasons.append(
                Season(
                    id=_required(item, "id", "season"),
                    number=number,
                    name=_localized(item, "lon") or f"Season {number}",
                )
            )
        return sorted(seasons, key=lambda item: item.number)

    def episodes(self, series: Series, season: Season) -> list[Episode]:
        page = 1
        rows: list[Any] = []
        total: int | None = None
        while total is None or len(rows) < total:
            payload = self._json(
                "GET",
                f"{DATA_STORE_BASE_URL}/content/series/{series.id}/episodes",
                params=self._catalog_params(
                    {
                        "seasonId": season.id,
                        "pageNumber": page,
                        "pageSize": 50,
                        "sortBy": "epnum",
                        "sortOrder": "asc",
                    }
                ),
            )
            batch = _list(payload.get("data"), "episode data")
            header = _dict(payload.get("header"), "episode header")
            if total is None:
                total = _int_required(header.get("count"), "episode count")
            if not batch and len(rows) < total:
                raise TvnzError("TVNZ episode pagination ended before the reported count")
            rows.extend(batch)
            page += 1

        episodes = [self._episode(_dict(row, "episode"), series) for row in rows]
        return sorted(episodes, key=lambda item: (item.number, item.name))

    def content(self, kind: str, slug: str) -> Content:
        if kind not in ROUTE_TYPES - {"tvseries"}:
            raise TvnzError(f"Unsupported TVNZ content type: {kind}")
        return self._content(self._catalog(kind, slug), expected_kind=kind)

    def channels(self) -> list[Channel]:
        payload = self._json(
            "GET",
            f"{DATA_STORE_BASE_URL}/content/airing/live",
            params=self._catalog_params({"info": "detail"}),
        )
        rows = _list(payload.get("data"), "live channel data")
        channels = []
        for row in rows:
            item = _dict(row, "live channel")
            channel = _dict(item.get("ch"), "live channel metadata")
            programme = item.get("pgm") if isinstance(item.get("pgm"), dict) else {}
            channels.append(
                Channel(
                    content_id=_required(item, "cid", "live channel"),
                    name=_localized_required(channel, "lon", "live channel name"),
                    now=(
                        _localized(programme, "loepn")
                        or _localized(programme, "losn")
                        or _localized(programme, "lon")
                    ),
                    description=_localized(programme, "lod"),
                )
            )
        return channels

    def source(
        self,
        content_id: str,
        *,
        catalog_type: str,
        content_type_id: str = "vod",
        playback_mode: str = "",
        is_live: bool = False,
    ) -> Source:
        state = self.ensure_auth()
        edge_token = self._edge_client_token()
        registered_id, device_token = self._register_device(edge_token, state.ovat_token)
        headers = self._app_headers(
            {
                "X-Device-Id": device_token,
                "Authorization": f"Bearer {edge_token}",
                "X-Client-Id": CLIENT_ID,
                "X-Authorization": state.ovat_token,
            }
        )
        body: dict[str, Any] = {
            "deviceName": DEVICE_TYPE,
            "deviceId": registered_id,
            "contentId": content_id,
            "contentTypeId": content_type_id,
            "catalogType": catalog_type,
            "mediaFormat": "dash",
            "drm": "widevine",
            "delivery": "streaming",
            "disableSsai": "false",
            "deviceManufacturer": "Google",
            "deviceModelName": ANDROID_DEVICE_MODEL,
            "deviceModelNumber": ANDROID_DEVICE_NAME,
            "deviceOs": "Android",
            "deviceOsVersion": "12",
            "supportedResolution": "FHD",
            "supportedAudio": "mp4a",
            "supportedAudioCodecs": "mp4a",
            "supportedVideoCodecs": "avc,hevc,vp9,av01",
            "supportedMaxWVSecurityLevel": "L3",
            "deviceToken": device_token,
            "urlParameters": self._ad_parameters(registered_id),
        }
        if playback_mode:
            if playback_mode not in {"live", "catchup"}:
                raise TvnzError(f"Unsupported TVNZ playback mode: {playback_mode}")
            body["playbackMode"] = playback_mode

        payload = self._json(
            "POST",
            f"{EDGE_BASE_URL}/media/content/authorize",
            json=body,
            headers=headers,
        )
        self._check_edge(payload, "playback authorization")
        data = _dict(payload.get("data"), "playback authorization data")
        manifest = _required(data, "contentUrl", "playback authorization")
        custom = data.get("custom") if isinstance(data.get("custom"), dict) else {}
        session_url = str(custom.get("mtSessionUrl") or "")
        if is_live and session_url:
            manifest = self._start_live_manifest_session(
                session_url,
                custom.get("playerParams") if isinstance(custom.get("playerParams"), dict) else {},
            )
        manifest = _delivery_manifest_url(manifest)
        license_url = str(data.get("licenseUrl") or "")
        return Source(
            manifest=manifest,
            license_url=license_url,
            edge_access_token=edge_token,
            protected=bool(license_url),
            is_live=is_live,
            playback_mode=playback_mode,
        )

    def live_event_source(self, item: Content) -> Source:
        if not item.live_content_id:
            raise TvnzError(f"{item.title} has no live content ID")
        mode = item.live_playback_mode()
        return self.source(
            item.live_content_id,
            catalog_type="liveevent",
            content_type_id="live",
            playback_mode=mode,
            is_live=True,
        )

    def channel_source(self, channel: Channel) -> Source:
        return self.source(
            channel.content_id,
            catalog_type="channel",
            content_type_id="live",
            playback_mode="live",
            is_live=True,
        )

    def widevine_license(
        self,
        license_url: str,
        challenge: bytes,
        *,
        edge_access_token: str,
    ) -> bytes:
        if not license_url or not edge_access_token:
            raise TvnzError("TVNZ playback is missing its Widevine licence context")
        try:
            response = self.session.post(
                license_url,
                data=challenge,
                headers=self._app_headers(
                    {
                        "Authorization": f"Bearer {edge_access_token}",
                        "Content-Type": "application/octet-stream",
                    }
                ),
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise TvnzError(f"Could not reach the TVNZ Widevine server: {exc}") from exc
        if not response.ok:
            raise TvnzError(f"TVNZ Widevine licence request failed ({response.status_code})")
        if not response.content:
            raise TvnzError("TVNZ Widevine server returned an empty licence")
        return response.content

    def _search_hit(self, item: dict[str, Any]) -> SearchHit:
        kind = str(item.get("cty") or "").lower()
        title = _localized(item, "lon") or "Untitled"
        slug = str(item.get("nu") or "")
        playable = kind in ROUTE_TYPES and bool(slug)
        if kind == "channel":
            playable = False
        return SearchHit(
            kind=kind or "unknown",
            slug=slug,
            title=title,
            description=_localized(item, "lod"),
            starts_at=_optional_datetime(item.get("ev_st_dt") or item.get("oa_st_dt")),
            playable=playable,
        )

    def _episode(self, item: dict[str, Any], series: Series) -> Episode:
        return Episode(
            content_id=_required(item, "nu", "episode"),
            series_title=_localized_required(item, "lostl", "episode series title"),
            name=_localized(item, "losn"),
            season=_int_required(item.get("snum"), "episode season"),
            number=_int_required(item.get("epnum"), "episode number"),
            description=_localized(item, "lod"),
        )

    def _content(self, item: dict[str, Any], *, expected_kind: str) -> Content:
        kind = str(item.get("cty") or "").lower()
        if kind != expected_kind:
            raise TvnzError(
                f"TVNZ returned {kind or 'no type'} for requested {expected_kind} content"
            )
        content_id = _required(item, "nu", f"{kind} content")
        if kind == "tvepisode":
            title = _localized_required(item, "lostl", "episode series title")
            episode_name = _localized(item, "losn")
        else:
            title = _localized_required(item, "lon", f"{kind} title")
            episode_name = ""
        return Content(
            kind=kind,
            content_id=content_id,
            title=title,
            description=_localized(item, "lod"),
            year=str(item.get("r") or ""),
            season=_int(item.get("snum")),
            episode=_int(item.get("epnum")),
            episode_name=episode_name,
            competition=str(item.get("comp_stg") or ""),
            duration=_int(item.get("rt")),
            live_content_id=(
                _required(item, "cid", "live event") if kind == "liveevent" else ""
            ),
            starts_at=(
                _required_datetime(item.get("ev_st_dt"), "live event start")
                if kind == "liveevent"
                else _optional_datetime(item.get("oa_st_dt"))
            ),
            ends_at=(
                _required_datetime(item.get("ev_ed_dt"), "live event end")
                if kind == "liveevent"
                else None
            ),
            window_starts_at=(
                _required_datetime(item.get("lws"), "live event window start")
                if kind == "liveevent"
                else None
            ),
            window_ends_at=(
                _required_datetime(item.get("lwe"), "live event window end")
                if kind == "liveevent"
                else None
            ),
        )

    def _catalog(self, kind: str, slug: str) -> dict[str, Any]:
        payload = self._json(
            "GET",
            f"{DATA_STORE_BASE_URL}/content/urn/resource/catalog/{kind}/{slug}",
            params=self._catalog_params(),
        )
        return _dict(payload.get("data"), f"{kind} catalogue data")

    def _catalog_params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "reg": "nz",
            "dt": DEVICE_TYPE,
            "client": CLIENT_ID,
            "pf": "Regular",
            "allowpg": "true",
            "chrt": DEFAULT_CHRT,
        }
        if extra:
            params.update(extra)
        return params

    def _app_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "*/*",
            "User-Agent": USER_AGENT,
            "X-Device-Type": DEVICE_TYPE,
            "X-App-Store-Type": APP_STORE_TYPE,
        }
        if extra:
            headers.update(extra)
        return headers

    def _device_message(self) -> dict[str, Any]:
        return {
            "serialNo": self.device_id,
            "deviceName": ANDROID_DEVICE_NAME,
            "deviceType": "Android Tv",
            "modelNo": ANDROID_DEVICE_MODEL,
            "appType": "Android TV",
            "location": None,
            "userAgent": None,
        }

    def _evergent(
        self,
        endpoint: str,
        request_name: str,
        response_name: str,
        values: dict[str, Any],
        *,
        action: str,
        access_token: str = "",
    ) -> dict[str, Any]:
        body = {
            request_name: {
                **values,
                "apiUser": API_USER,
                "channelPartnerID": CHANNEL_ID,
                "apiPassword": API_PASSWORD,
            }
        }
        headers = self._app_headers()
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        payload = self._json(
            "POST",
            f"{EVERGENT_BASE_URL}/{endpoint}",
            json=body,
            headers=headers,
        )
        response = _dict(payload.get(response_name), f"{action} response")
        failures = response.get("failureMessage")
        if isinstance(failures, list) and failures:
            raise TvnzError(_evergent_failure(failures))
        code = response.get("responseCode")
        if code not in (None, "", 1, "1"):
            raise TvnzError(str(response.get("message") or f"TVNZ {action} failed ({code})"))
        if code in (None, ""):
            status = str(response.get("status") or "").upper()
            if status and status != "SUCCESS":
                raise TvnzError(str(response.get("message") or f"TVNZ {action} failed"))
        return response

    def _apply_token_response(
        self,
        response: dict[str, Any],
        *,
        email: str,
        login_method: str,
        initial: bool,
    ) -> None:
        params = {
            str(item.get("paramName")): item.get("paramValue")
            for item in response.get("params") or []
            if isinstance(item, dict) and item.get("paramName")
        }
        access_token = str(response.get("accessToken") or params.get("accessToken") or "")
        refresh_token = str(response.get("refreshToken") or params.get("refreshToken") or "")
        expires_in = response.get("expiresIn") or params.get("expiresIn")
        if not access_token or not refresh_token or expires_in in (None, ""):
            raise TvnzError("TVNZ authentication response is missing token fields")

        contact_id = str(response.get("contactID") or params.get("contactID") or "")
        if not contact_id:
            contact_id = self.state.contact_id
        if initial and not contact_id:
            raise TvnzError("TVNZ login response did not contain contactID")

        cp_customer_id = str(
            response.get("cpCustomerID")
            or params.get("cpCustomerID")
            or _jwt_claim(access_token, "uid")
            or self.state.cp_customer_id
        )
        self.state = SessionState(
            email=email,
            login_method=login_method,
            access_token=access_token,
            refresh_token=refresh_token,
            contact_id=contact_id,
            cp_customer_id=cp_customer_id,
            access_expires_at=_access_expiry(expires_in),
            ovat_token="" if initial else self.state.ovat_token,
            ovat_expires_at=0.0 if initial else self.state.ovat_expires_at,
            device_id=self.device_id,
        )
        self._persist()

    def _persist(self) -> None:
        if self.on_save:
            self.on_save(self.state)

    def _edge_client_token(self) -> str:
        payload = self._json(
            "POST",
            EDGE_OAUTH_URL,
            data={
                "client_id": EDGE_CLIENT_ID,
                "client_secret": EDGE_CLIENT_SECRET,
                "grant_type": "client_credentials",
                "audience": "edge-service",
                "scope": "offline openid",
            },
            headers=self._app_headers(
                {"Content-Type": "application/x-www-form-urlencoded"}
            ),
        )
        return _required(payload, "access_token", "edge OAuth response")

    def _register_device(self, edge_token: str, ovat_token: str) -> tuple[str, str]:
        payload = self._json(
            "POST",
            f"{EDGE_BASE_URL}/device/app/register",
            json={"UniqueId": self.device_id},
            headers=self._app_headers(
                {
                    "Authorization": f"Bearer {edge_token}",
                    "X-Client-Id": CLIENT_ID,
                    "X-Authorization": ovat_token,
                    "Content-Type": "application/json",
                }
            ),
        )
        self._check_edge(payload, "device registration")
        header = _dict(payload.get("header"), "device registration header")
        data = _dict(payload.get("data"), "device registration data")
        registered_id = _required(data, "deviceId", "device registration")
        secret = _required(data, "secret", "device registration")
        expiry = _int_required(data.get("expiry"), "device token lifetime")
        if expiry <= 0 or expiry > 3600:
            raise TvnzError("TVNZ returned an invalid device token lifetime")
        issued_at_ms = _int_required(header.get("system_time"), "device token issue time")
        return registered_id, _device_token(registered_id, secret, expiry, issued_at_ms)

    def _start_live_manifest_session(
        self,
        session_url: str,
        player_params: dict[str, Any],
    ) -> str:
        payload = self._json(
            "POST",
            session_url,
            data=json.dumps(
                {"playerParams": player_params, "reportingMode": "server"},
                separators=(",", ":"),
            ),
            headers=self._app_headers({"Content-Type": "text/plain;charset=UTF-8"}),
        )
        manifest = _required(payload, "manifestUrl", "live manifest session")
        return urljoin(session_url, manifest)

    def _ad_parameters(self, device_id: str) -> dict[str, Any]:
        return {
            "vpa": "click",
            "rdid": device_id,
            "is_lat": "0",
            "npa": "0",
            "idtype": "dpid",
            "endpoint": "androidtv",
            "endpoint-group": "TV",
            "endpoint_detail": "TV",
            "scor": _uuid(),
            "correlator": _uuid(),
            "adxs": 0,
            "adys": 0,
            "biw": 736,
            "bih": 815,
            "scr_x": 1920,
            "scr_y": 1080,
            "btvi": 1,
            "oid": 1,
        }

    def _check_edge(self, payload: dict[str, Any], action: str) -> None:
        header = _dict(payload.get("header"), f"{action} header")
        if header.get("code") == 0:
            return
        errors = header.get("errors") if isinstance(header.get("errors"), list) else []
        messages = []
        for item in errors:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            description = str(item.get("description") or item.get("message") or "").strip()
            messages.append(": ".join(part for part in (code, description) if part))
        detail = "; ".join(message for message in messages if message)
        raise TvnzError(detail or str(header.get("message") or f"TVNZ {action} failed"))

    def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("timeout", HTTP_TIMEOUT)
        try:
            response = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise TvnzError(f"Could not reach TVNZ: {exc}") from exc
        if response.status_code in (401, 403):
            raise TvnzError("TVNZ refused the request; sign in again or check the NZ route")
        if not response.ok:
            raise TvnzError(f"TVNZ request failed ({response.status_code})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TvnzError("TVNZ returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise TvnzError("TVNZ returned an unexpected JSON response")
        return payload


def _device_token(
    device_id: str,
    secret: str,
    lifetime: int,
    issued_at_ms: int,
) -> str:
    issued_at = int(issued_at_ms / 1000)
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {
                "deviceId": device_id,
                "aud": "playback-auth-service",
                "iat": issued_at,
                "exp": issued_at + lifetime,
            },
            separators=(",", ":"),
        ).encode()
    )
    unsigned = f"{header}.{payload}"
    try:
        key = base64.b64decode(secret)
    except ValueError as exc:
        raise TvnzError("TVNZ device registration returned an invalid secret") from exc
    signature = hmac.new(key, unsigned.encode("ascii"), hashlib.sha256).digest()
    return f"{unsigned}.{_b64url(signature)}"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _uuid() -> str:
    import uuid

    return str(uuid.uuid4())


def _access_expiry(value: Any) -> float:
    raw = _float(value)
    if raw >= 1_000_000_000_000:
        return raw / 1000.0
    if raw >= 1_000_000_000:
        return raw
    raise TvnzError("TVNZ returned an invalid access token expiry")


def _jwt_claim(token: str, name: str) -> str:
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    except (IndexError, ValueError, json.JSONDecodeError):
        return ""
    return str(payload.get(name) or "") if isinstance(payload, dict) else ""


def _evergent_failure(items: list[Any]) -> str:
    messages = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("errorCode") or "").strip()
        message = str(item.get("errorMessage") or item.get("message") or "").strip()
        messages.append(": ".join(part for part in (code, message) if part))
    return "; ".join(message for message in messages if message) or "TVNZ authentication failed"


def _localized(item: dict[str, Any], key: str) -> str:
    values = item.get(key)
    if not isinstance(values, list):
        return ""
    for value in values:
        if isinstance(value, dict):
            text = str(value.get("n") or "").strip()
            if text:
                return text
    return ""


def _localized_required(item: dict[str, Any], key: str, label: str) -> str:
    value = _localized(item, key)
    if not value:
        raise TvnzError(f"TVNZ {label} is missing")
    return value


def _required(item: dict[str, Any], key: str, label: str) -> str:
    value = str(item.get(key) or "").strip()
    if not value:
        raise TvnzError(f"TVNZ {label} is missing {key}")
    return value


def _dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TvnzError(f"TVNZ {label} is not an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise TvnzError(f"TVNZ {label} is not a list")
    return value


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _int_required(value: Any, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise TvnzError(f"TVNZ {label} is invalid") from exc


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _required_datetime(value: Any, label: str) -> datetime:
    parsed = _optional_datetime(value)
    if parsed is None:
        raise TvnzError(f"TVNZ {label} is invalid")
    return parsed


def _optional_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _kind_label(kind: str) -> str:
    return {
        "tvseries": "TV series",
        "tvepisode": "TV episode",
        "movie": "Movie",
        "newsclip": "News clip",
        "liveevent": "Live event",
        "event": "Sports event",
        "sporthighlight": "Sports highlight",
        "sportclip": "Sports clip",
        "sport": "Sports hub",
        "competition": "Competition hub",
        "category": "Category",
        "channel": "Live channel - use Live TV",
    }.get(kind, kind or "Unknown")


def _delivery_manifest_url(value: str) -> str:
    parsed = urlparse(value)
    if ".m3u8" in parsed.path.lower():
        return value
    return parsed._replace(query="", fragment="").geturl()


__all__ = [
    "APP_STORE_TYPE",
    "CHANNEL_ID",
    "CLIENT_ID",
    "DEVICE_FILE",
    "DEVICE_TYPE",
    "HTTP_TIMEOUT",
    "ROUTE_TYPES",
    "TOKEN_FILE",
    "USER_AGENT",
    "Account",
    "Channel",
    "Content",
    "Episode",
    "OtpChallenge",
    "ParsedInput",
    "SearchHit",
    "Season",
    "Series",
    "SessionState",
    "Source",
    "TvnzApi",
    "TvnzError",
    "parse_input",
]
