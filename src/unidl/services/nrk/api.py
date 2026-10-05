"""NRK TV Android TV API client.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import secrets
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests

APP_VERSION = "2026.23.1"
USER_AGENT = f"NRK TV/{APP_VERSION} (Android 16; tv; unidl; Scale/1.0)_app_"
LOGIN_USER_AGENT = USER_AGENT
TOKEN_FILE = "nrk_token.json"

LOGIN_BASE_URL = "https://innlogging.nrk.no"
PSAPI_BASE_URL = "https://psapi.nrk.no"
PAGES_BASE_URL = "https://pages.tv.api.nrk.no"
SEARCH_BASE_URL = "https://search.tv.api.nrk.no"

CLIENT_ID = "tv.nrk.no.androidtv"
# This is the public client credential embedded in the Android TV application.
CLIENT_SECRET = "IceCreamSandwich4Life!"
LOGIN_SCOPE = "openid profile psapi-userdata offline_access email nrkno-userdata"
DEVICE_NAME = "Android TV"
DEVICE_LOGIN_CONTEXT = "nrktvSettingsLogOnButton"
CONTENT_GROUP = "adults"
ANDROID_CALLER = "NRK TV"
REQUEST_TIMEOUT = 30
TOKEN_REFRESH_MARGIN = 60
CLEARKEY_UUID = "e2719d58-a985-b3c9-781a-b030af78d30e"


def new_device_identifier() -> str:
    """Match the Android client identifier: UUID followed by creation time."""
    return f"{uuid.uuid4()}{int(time.time() * 1000)}"


class NrkError(RuntimeError):
    """An NRK request or response could not be used."""

    def __init__(self, message: str, payload: Any = None) -> None:
        super().__init__(message)
        self.payload = payload


class AuthenticationRequired(NrkError):
    """The saved NRK account session is no longer accepted."""


@dataclass
class SessionState:
    mode: str = "anonymous"
    device_identifier: str = ""
    access_token: str = ""
    refresh_token: str = ""
    token_type: str = "Bearer"
    expires_at: float = 0.0
    account_name: str = ""
    subject_id: str = ""

    @classmethod
    def from_cache(cls, raw: Any) -> SessionState:
        if not isinstance(raw, dict):
            raw = {}
        expires_at = raw.get("expires_at", 0)
        try:
            expires_at = float(expires_at)
        except (TypeError, ValueError):
            expires_at = 0.0
        state = cls(
            mode=str(raw.get("mode") or ("tv" if raw.get("refresh_token") else "anonymous")),
            device_identifier=str(raw.get("device_identifier") or ""),
            access_token=str(raw.get("access_token") or ""),
            refresh_token=str(raw.get("refresh_token") or ""),
            token_type=str(raw.get("token_type") or "Bearer"),
            expires_at=expires_at,
            account_name=str(raw.get("account_name") or ""),
            subject_id=str(raw.get("subject_id") or ""),
        )
        if not state.device_identifier:
            state.device_identifier = new_device_identifier()
        return state

    @property
    def signed_in(self) -> bool:
        return self.mode == "tv" and bool(self.refresh_token or self.access_token)

    def is_fresh(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at - TOKEN_REFRESH_MARGIN

    def to_cache(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "device_identifier": self.device_identifier,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "account_name": self.account_name,
            "subject_id": self.subject_id,
        }

    def use_anonymous(self) -> None:
        self.mode = "anonymous"
        self.access_token = ""
        self.refresh_token = ""
        self.token_type = "Bearer"
        self.expires_at = 0.0
        self.account_name = ""
        self.subject_id = ""

    def clear(self) -> None:
        self.use_anonymous()
        self.device_identifier = new_device_identifier()


@dataclass(frozen=True)
class DeviceChallenge:
    user_code: str
    correlation_id: str
    verification_url: str
    verification_url_complete: str
    interval: int
    expires_in: int
    poll_url: str
    post_data: dict[str, Any] | None
    code_verifier: str
    device_identifier: str


@dataclass(frozen=True)
class ParsedTarget:
    kind: str
    id: str
    series_id: str = ""
    season_id: str = ""


@dataclass
class CatalogItem:
    kind: str
    id: str
    title: str
    subtitle: str = ""
    description: str = ""
    series_id: str = ""
    season_id: str = ""
    series_type: str = ""
    duration: float | None = None
    production_year: str = ""
    status: str = ""
    image_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def type_label(self) -> str:
        return {
            "series": "series",
            "program": "program",
            "episode": "episode",
            "extra": "extra",
            "channel": "channel",
        }.get(self.kind, self.kind)

    @property
    def detail(self) -> str:
        values = [value for value in (self.subtitle, self.status) if value]
        if self.duration:
            values.append(format_duration(self.duration))
        return " · ".join(values)


@dataclass(frozen=True)
class Season:
    id: str
    title: str
    href: str = ""


@dataclass
class Series:
    id: str
    title: str
    subtitle: str = ""
    series_type: str = ""
    seasons: list[Season] = field(default_factory=list)
    latest_episodes: list[CatalogItem] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class HomeSection:
    title: str
    refs: list[str]
    display_contract: str = ""


@dataclass
class Channel:
    id: str
    title: str
    description: str = ""
    geo_blocked: bool = False
    channel_type: str = ""
    current_title: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlaybackSource:
    id: str
    manifest_url: str
    format: str
    encrypted: bool
    encryption_scheme: str
    is_live: bool
    license_url: str = ""
    keys: list[str] = field(default_factory=list)
    subtitles: list[str] = field(default_factory=list)

    @property
    def note(self) -> str:
        mode = "live" if self.is_live else "on-demand"
        protection = self.encryption_scheme if self.encrypted else "clear"
        return f"{self.format} · {mode} · {protection}"


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def image_url(value: Any) -> str:
    if isinstance(value, list):
        candidates = [item for item in value if isinstance(item, dict) and item.get("url")]
        if candidates:
            return str(sorted(candidates, key=lambda item: int(item.get("width") or 0))[-1]["url"])
    if isinstance(value, dict):
        images = value.get("webImages")
        return image_url(images)
    return ""


def parse_iso_duration(value: Any) -> float | None:
    text = clean_text(value)
    if not text:
        return None
    match = re.fullmatch(
        r"P(?:\d+D)?(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?",
        text,
    )
    if not match:
        return None
    return sum(
        float(match.group(name) or 0) * multiplier
        for name, multiplier in (("hours", 3600), ("minutes", 60), ("seconds", 1))
    )


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, remainder = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {remainder:02d}s"


def parse_input(target: str) -> ParsedTarget | None:
    raw = clean_text(target)
    if not raw:
        return None
    if re.fullmatch(r"[A-Za-z]{4}\d{8}", raw):
        return ParsedTarget("program", raw.upper())
    if "://" not in raw and re.fullmatch(r"[a-z0-9-]+", raw.casefold()):
        return ParsedTarget("series", raw.casefold(), series_id=raw.casefold())
    candidate = raw if "://" in raw else f"https://tv.nrk.no/{raw.lstrip('/')}"
    parsed = urlparse(candidate)
    host = parsed.netloc.casefold().split(":", 1)[0]
    if host not in {"tv.nrk.no", "www.tv.nrk.no"}:
        return None

    query = parse_qs(parsed.query)
    for key in ("v", "program", "id"):
        values = query.get(key)
        if values and re.fullmatch(r"[A-Za-z]{4}\d{8}", values[0]):
            return ParsedTarget("program", values[0].upper())

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    if parts[0].casefold() in {"program", "programs"} and len(parts) >= 2:
        return ParsedTarget("program", parts[1].upper())
    if parts[0].casefold() == "serie" and len(parts) >= 2:
        series_id = parts[1].casefold()
        season_id = ""
        for index, part in enumerate(parts[2:], start=2):
            if (
                part.casefold() in {"episode", "episoder", "program"}
                and index + 1 < len(parts)
                and re.fullmatch(r"[A-Za-z]{4}\d{8}", parts[index + 1])
            ):
                return ParsedTarget(
                    "program",
                    parts[index + 1].upper(),
                    series_id=series_id,
                    season_id=season_id,
                )
            if part.casefold() in {"sesong", "season"} and index + 1 < len(parts):
                season_id = parts[index + 1]
        return ParsedTarget("series", series_id, series_id=series_id, season_id=season_id)
    if parts[0].casefold() in {"se", "episode", "episoder"} and len(parts) >= 2:
        return ParsedTarget("program", parts[1].upper())
    if parts[0].casefold() in {"direkte", "live"} and len(parts) >= 2:
        return ParsedTarget("channel", parts[1].casefold())
    if parts[0].casefold() == "programmer" and len(parts) >= 2:
        series_id = parts[1].casefold()
        return ParsedTarget("series", series_id, series_id=series_id)
    return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _nested_title(raw: dict[str, Any]) -> tuple[str, str]:
    for key in ("titles", "title"):
        value = raw.get(key)
        if isinstance(value, dict):
            title = clean_text(value.get("title"))
            subtitle = clean_text(value.get("subtitle"))
            if title:
                return title, subtitle
        elif isinstance(value, str) and clean_text(value):
            return clean_text(value), ""
    return "", ""


def item_from_raw(raw: dict[str, Any], *, kind: str | None = None) -> CatalogItem | None:
    raw_kind = clean_text(raw.get("type") or raw.get("plugType") or "").casefold()
    item_kind = kind or {
        "series": "series",
        "program": "program",
        "episode": "episode",
        "clip": "extra",
    }.get(raw_kind, raw_kind)
    item_id = clean_text(raw.get("contentId") or raw.get("prfId") or raw.get("id"))
    if not item_id:
        return None
    title, subtitle = _nested_title(raw)
    if not title:
        title = clean_text(raw.get("name") or item_id)
    info = raw.get("programInformation") if isinstance(raw.get("programInformation"), dict) else {}
    if info:
        info_title, info_subtitle = _nested_title(info)
        title = info_title or title
        subtitle = info_subtitle or subtitle
    more = raw.get("moreInformation") if isinstance(raw.get("moreInformation"), dict) else {}
    duration_data = more.get("duration") if isinstance(more.get("duration"), dict) else {}
    duration = raw.get("durationInSeconds") or duration_data.get("seconds")
    try:
        duration_value = float(duration) if duration is not None else parse_iso_duration(raw.get("duration"))
    except (TypeError, ValueError):
        duration_value = None
    availability = raw.get("availability") or info.get("availability") or {}
    return CatalogItem(
        kind=item_kind or "program",
        id=item_id,
        title=title,
        subtitle=subtitle,
        description=clean_text(raw.get("description")),
        series_id=clean_text(raw.get("seriesId")),
        season_id=clean_text(raw.get("seasonId")),
        series_type=clean_text(raw.get("seriesType")),
        duration=duration_value,
        production_year=clean_text(raw.get("productionYear") or more.get("productionYear")),
        status=clean_text(availability.get("label") or availability.get("status")),
        image_url=image_url(raw.get("image") or info.get("image")),
        raw=raw,
    )


def _href(link: Any) -> str:
    return clean_text(link.get("href")) if isinstance(link, dict) else ""


class NrkApi:
    def __init__(
        self,
        session: requests.Session,
        state: SessionState | None = None,
        on_save: Callable[[SessionState], None] | None = None,
    ) -> None:
        self.session = session
        self.state = state or SessionState.from_cache({})
        self.on_save = on_save

    # ---------------------------------------------------------------- auth
    def _save(self) -> None:
        if self.on_save:
            self.on_save(self.state)

    def use_anonymous(self) -> None:
        self.state.use_anonymous()
        self._save()

    def _basic_auth(self) -> tuple[str, str]:
        return CLIENT_ID, CLIENT_SECRET

    def _login_post(self, url: str, action: str, **kwargs: Any) -> requests.Response:
        try:
            return self.session.post(url, timeout=REQUEST_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            raise NrkError(f"{action} failed: {exc}") from exc

    def refresh(self) -> SessionState:
        if not self.state.refresh_token:
            raise AuthenticationRequired("NRK TV has no refresh token; sign in again")
        response = self._login_post(
            f"{LOGIN_BASE_URL}/connect/token",
            "NRK token refresh",
            params={"device_name": DEVICE_NAME},
            data={
                "grant_type": "refresh_token",
                "scope": LOGIN_SCOPE,
                "refresh_token": self.state.refresh_token,
            },
            auth=self._basic_auth(),
            headers={"Accept": "application/json", "User-Agent": LOGIN_USER_AGENT},
        )
        payload = self._json_response(response, "NRK token refresh")
        self._apply_tokens(payload)
        self.state.mode = "tv"
        self._save()
        return self.state

    def ensure_auth(self) -> None:
        if self.state.signed_in and not self.state.is_fresh():
            try:
                self.refresh()
            except NrkError as exc:
                raise AuthenticationRequired(f"NRK TV session refresh failed: {exc}") from exc

    def start_device_login(self) -> DeviceChallenge:
        verifier = _b64url(secrets.token_bytes(63))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        params: dict[str, str] = {
            "client_id": CLIENT_ID,
            "code_challenge": challenge,
            "device_identifier": self.state.device_identifier,
            "device_name": DEVICE_NAME,
            "acr_values": f"nrkctx={DEVICE_LOGIN_CONTEXT}",
        }
        if self.state.refresh_token:
            params["refresh_token"] = self.state.refresh_token
        response = self._login_post(
            f"{LOGIN_BASE_URL}/api/deviceactivation/initiate",
            "NRK TV code request",
            params=params,
            auth=self._basic_auth(),
            headers={"Accept": "application/json", "User-Agent": LOGIN_USER_AGENT},
        )
        payload = self._json_response(response, "NRK TV code request")
        post_data = payload.get("postData")
        if post_data is not None and not isinstance(post_data, dict):
            raise NrkError("NRK TV code response returned invalid activation data", payload)
        user_code = clean_text(payload.get("userCode"))
        correlation_id = clean_text(payload.get("correlationId"))
        poll_url = clean_text(payload.get("pollUrl"))
        if not user_code or not correlation_id or not poll_url:
            raise NrkError("NRK TV code response was incomplete", payload)
        return DeviceChallenge(
            user_code=user_code,
            correlation_id=correlation_id,
            verification_url=clean_text(payload.get("verificationUrl")),
            verification_url_complete=clean_text(payload.get("verificationUrlComplete")),
            interval=max(1, int(payload.get("interval") or 5)),
            expires_in=max(30, int(payload.get("expiresIn") or 600)),
            poll_url=urljoin(LOGIN_BASE_URL, poll_url),
            post_data=post_data,
            code_verifier=verifier,
            device_identifier=self.state.device_identifier,
        )

    def poll_device_login(self, challenge: DeviceChallenge) -> SessionState | None:
        response = self._login_post(
            challenge.poll_url,
            "NRK TV code status",
            json=challenge.post_data,
            auth=self._basic_auth(),
            headers={"Accept": "application/json", "User-Agent": LOGIN_USER_AGENT},
        )
        payload = self._json_response(response, "NRK TV code status")
        status = clean_text(payload.get("status")).casefold()
        activations = payload.get("activations")
        if not isinstance(activations, list):
            activations = []
        ready = next(
            (
                activation
                for activation in activations
                if isinstance(activation, dict) and clean_text(activation.get("status")).casefold() == "ready"
            ),
            None,
        )
        if ready is not None:
            return self._exchange_device_code(challenge, ready)
        if status in {"aborted", "expired", "cancelled", "error"}:
            raise NrkError(f"NRK TV code activation ended with status {status}", payload)
        return None

    def _exchange_device_code(self, challenge: DeviceChallenge, activation: dict[str, Any]) -> SessionState:
        response = self._login_post(
            f"{LOGIN_BASE_URL}/connect/token",
            "NRK TV code exchange",
            data={
                "grant_type": "device_activation_code",
                "code": challenge.user_code,
                "scope": LOGIN_SCOPE,
                "sub": clean_text(activation.get("subjectId")),
                "correlation_id": challenge.correlation_id,
                "code_verifier": challenge.code_verifier,
            },
            auth=self._basic_auth(),
            headers={"Accept": "application/json", "User-Agent": LOGIN_USER_AGENT},
        )
        payload = self._json_response(response, "NRK TV code exchange")
        self._apply_tokens(payload)
        self.state.mode = "tv"
        self.state.device_identifier = challenge.device_identifier
        self.state.subject_id = clean_text(activation.get("subjectId"))
        self.state.account_name = clean_text(
            activation.get("subjectName") or payload.get("user_info", {}).get("name")
            if isinstance(payload.get("user_info"), dict)
            else activation.get("subjectName")
        )
        self._save()
        return self.state

    def revoke(self) -> None:
        if self.state.refresh_token:
            try:
                self.session.post(
                    f"{LOGIN_BASE_URL}/connect/revocation",
                    data={"token": self.state.refresh_token, "token_type_hint": "refresh_token"},
                    headers={"Accept": "application/json", "User-Agent": LOGIN_USER_AGENT},
                    auth=self._basic_auth(),
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException:
                pass
        self.state.clear()
        self._save()

    def _apply_tokens(self, payload: dict[str, Any]) -> None:
        access_token = clean_text(payload.get("access_token"))
        refresh_token = clean_text(payload.get("refresh_token")) or self.state.refresh_token
        if not access_token or not refresh_token:
            raise NrkError("NRK token response did not contain an access and refresh token", payload)
        try:
            expires_in = float(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600.0
        self.state.access_token = access_token
        self.state.refresh_token = refresh_token
        self.state.token_type = clean_text(payload.get("token_type") or "Bearer")
        self.state.expires_at = time.time() + max(60.0, expires_in)

    # ------------------------------------------------------------- transport
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self.state.is_fresh():
            headers["Authorization"] = f"{self.state.token_type} {self.state.access_token}"
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self, method: str, url: str, *, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> requests.Response:
        self.ensure_auth()
        response = self._send(method, url, headers=headers, **kwargs)
        if response.status_code == 401 and self.state.signed_in:
            try:
                self.refresh()
            except NrkError as exc:
                raise AuthenticationRequired(f"NRK TV session refresh failed: {exc}") from exc
            response = self._send(method, url, headers=headers, **kwargs)
            if response.status_code == 401:
                raise AuthenticationRequired("NRK TV rejected the refreshed session; sign in again")
        if response.status_code >= 400:
            raise NrkError(f"NRK HTTP {response.status_code}", self._response_payload(response))
        return response

    def _send(
        self, method: str, url: str, *, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> requests.Response:
        try:
            return self.session.request(
                method,
                url,
                headers=self._headers(headers),
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise NrkError(f"NRK request failed: {exc}") from exc

    def _json_response(self, response: requests.Response, action: str) -> dict[str, Any]:
        payload = self._response_payload(response)
        if response.status_code >= 400:
            if isinstance(payload, dict):
                detail = payload.get("error_description") or payload.get("message") or payload.get("error")
            else:
                detail = payload
            raise NrkError(f"{action} failed: HTTP {response.status_code} {clean_text(detail)}".strip(), payload)
        if not isinstance(payload, dict):
            raise NrkError(f"{action} returned a non-object response", payload)
        return payload

    @staticmethod
    def _response_payload(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text[:1000]

    def _json_get(self, base: str, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request("GET", urljoin(base, path), params=params)
        return self._json_response(response, "NRK JSON request")

    # ------------------------------------------------------------ catalogue
    def home(self) -> list[HomeSection]:
        payload = self._json_get(
            PAGES_BASE_URL,
            "/v4.9/pages/frontpage",
            params={"contentGroup": CONTENT_GROUP, "includeLinkPlugs": "true"},
        )
        sections: list[HomeSection] = []
        for section in payload.get("sections", []):
            if not isinstance(section, dict):
                continue
            included = section.get("included") if isinstance(section.get("included"), dict) else section
            refs = [
                clean_text(ref.get("ref"))
                for ref in included.get("plugRefs", [])
                if isinstance(ref, dict) and clean_text(ref.get("ref"))
            ]
            title = clean_text(included.get("title"))
            if title and refs:
                sections.append(HomeSection(title, refs, clean_text(included.get("displayContract"))))
        return sections

    def section_items(self, section: HomeSection) -> list[CatalogItem]:
        items: list[CatalogItem] = []
        for ref in section.refs:
            plug = self._json_get(PAGES_BASE_URL, f"/v4.9/plugs/{ref}")
            item = item_from_raw(plug)
            live = plug.get("liveChannelProgram") if isinstance(plug.get("liveChannelProgram"), dict) else {}
            if plug.get("type") == "liveChannelProgram" and live.get("channelId"):
                item = CatalogItem(
                    kind="channel",
                    id=clean_text(live.get("channelId")),
                    title=clean_text(plug.get("title") or live.get("channelId")),
                    subtitle=clean_text(plug.get("subtitle")),
                    description=clean_text(plug.get("description")),
                    raw=plug,
                )
            if item is not None:
                items.append(item)
        return items

    def series(self, series_id: str) -> Series:
        payload = self._json_get(
            PSAPI_BASE_URL,
            f"/tv/catalog/series/{series_id}",
            params={"contentGroup": CONTENT_GROUP},
        )
        series_type = clean_text(payload.get("seriesType"))
        details = payload.get(series_type)
        if not isinstance(details, dict):
            details = {}
        title, subtitle = _nested_title(details)
        seasons: list[Season] = []
        for raw_season in payload.get("_embedded", {}).get("seasons", []):
            if not isinstance(raw_season, dict):
                continue
            link = raw_season.get("_links", {}).get("self", {})
            href = _href(link)
            season_id = clean_text(raw_season.get("id")) or href.rstrip("/").split("/")[-1]
            season_title, _ = _nested_title(raw_season)
            if season_id and season_title:
                seasons.append(Season(season_id, season_title, href))
        latest: list[CatalogItem] = []
        instalments = payload.get("_embedded", {}).get("instalments", {})
        if isinstance(instalments, dict):
            for raw_item in instalments.get("_embedded", {}).get("instalments", []):
                if isinstance(raw_item, dict):
                    item = item_from_raw(raw_item, kind="episode")
                    if item:
                        item.series_id = series_id
                        latest.append(item)
        return Series(
            id=series_id,
            title=title or series_id,
            subtitle=subtitle,
            series_type=series_type,
            seasons=seasons,
            latest_episodes=latest,
            raw=payload,
        )

    def season(self, series_id: str, season_id: str) -> list[CatalogItem]:
        payload = self._json_get(
            PSAPI_BASE_URL,
            f"/tv/catalog/series/{series_id}/seasons/{season_id}",
            params={"contentGroup": CONTENT_GROUP},
        )
        result: list[CatalogItem] = []
        for raw_item in payload.get("_embedded", {}).get("instalments", []):
            if not isinstance(raw_item, dict):
                continue
            item = item_from_raw(raw_item, kind="episode")
            if item:
                item.series_id = series_id
                item.season_id = season_id
                result.append(item)
        return result

    def program(self, program_id: str) -> CatalogItem:
        payload = self._json_get(
            PSAPI_BASE_URL,
            f"/tv/catalog/programs/{program_id}",
            params={"contentGroup": CONTENT_GROUP},
        )
        item = item_from_raw({"id": program_id, **payload}, kind="program")
        if item is None:
            raise NrkError(f"NRK program {program_id} returned no title")
        series_page = payload.get("_links", {}).get("seriesPage", {})
        season_link = payload.get("_links", {}).get("season", {})
        item.id = program_id
        item.series_id = clean_text(series_page.get("href", "").rstrip("/").split("/")[-1])
        item.season_id = clean_text(season_link.get("name"))
        return item

    def search(self, query: str) -> list[CatalogItem]:
        payload = self._json_get(
            SEARCH_BASE_URL,
            "/v3/search",
            params={"q": query, "contentGroup": CONTENT_GROUP},
        )
        result: list[CatalogItem] = []
        for group in payload.get("results", []):
            if not isinstance(group, dict):
                continue
            for raw_item in group.get("plugs", []):
                if not isinstance(raw_item, dict):
                    continue
                item = item_from_raw(raw_item)
                if item:
                    result.append(item)
        return result

    # --------------------------------------------------------------- live
    def live_channels(self) -> list[Channel]:
        response = self._request(
            "GET",
            f"{PSAPI_BASE_URL}/tv/live/plugs",
            params={
                "contentGroup": CONTENT_GROUP,
                "overridePromoInterval": "false",
                "showStreamingChannels": "true",
            },
        )
        payload = self._response_payload(response)
        if not isinstance(payload, list):
            raise NrkError("NRK live channels returned a non-list response", payload)
        channels: list[Channel] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            channel_id = clean_text(raw.get("id"))
            if channel_id:
                channels.append(
                    Channel(
                        id=channel_id,
                        title=clean_text(raw.get("title") or channel_id),
                        description=clean_text(raw.get("description")),
                        geo_blocked=bool(raw.get("isGeoBlocked")),
                        channel_type=clean_text(raw.get("type")),
                        raw=raw,
                    )
                )
        return channels

    # ------------------------------------------------------------- playback
    def playback(self, target: ParsedTarget | CatalogItem) -> PlaybackSource:
        kind = target.kind
        item_id = target.id
        path_kind = "channel" if kind == "channel" else "program" if kind in {"program", "episode"} else "clip"
        metadata = self._json_get(
            PSAPI_BASE_URL,
            f"/playback/metadata/{path_kind}/{item_id}",
            params={"live2vod": "true", "eea-portability": "true", "contentGroup": CONTENT_GROUP},
        )
        if clean_text(metadata.get("playability")).casefold() != "playable":
            non_playable = metadata.get("nonPlayable") if isinstance(metadata.get("nonPlayable"), dict) else {}
            message = clean_text(non_playable.get("endUserMessage")) or "NRK marked this title as not playable"
            raise NrkError(message, metadata)
        manifest_links = metadata.get("_links", {}).get("manifests")
        if not isinstance(manifest_links, list):
            manifest_links = []
        manifest_href = (
            _href(manifest_links[0]) if manifest_links else clean_text(metadata.get("playable", {}).get("resolve"))
        )
        if not manifest_href:
            raise NrkError("NRK playback metadata returned no manifest")
        manifest_url = urljoin(PSAPI_BASE_URL, manifest_href)
        response = self._request(
            "GET",
            manifest_url,
            headers={"Accept": "application/vnd.nrk.psapi+json; version=9", "Android-Caller": ANDROID_CALLER},
            params={"live2vod": "true", "eea-portability": "true", "contentGroup": CONTENT_GROUP},
        )
        manifest = self._response_payload(response)
        if not isinstance(manifest, dict):
            raise NrkError("NRK playback manifest returned a non-object response", manifest)
        assets = manifest.get("playable", {}).get("assets", [])
        if not isinstance(assets, list) or not assets:
            raise NrkError("NRK playback manifest returned no media asset", manifest)
        selected = self._select_asset(assets)
        media_url = clean_text(selected.get("url"))
        if not media_url:
            raise NrkError("NRK playback asset returned no media URL", selected)
        encrypted = bool(selected.get("encrypted"))
        scheme = clean_text(selected.get("encryptionScheme") or "none").casefold()
        source = PlaybackSource(
            id=item_id,
            manifest_url=media_url,
            format=clean_text(selected.get("format") or "stream"),
            encrypted=encrypted,
            encryption_scheme=scheme,
            is_live=clean_text(manifest.get("streamingMode")).casefold() == "live" or kind == "channel",
            subtitles=[
                clean_text(subtitle.get("webVtt"))
                for subtitle in manifest.get("playable", {}).get("subtitles", [])
                if isinstance(subtitle, dict) and clean_text(subtitle.get("webVtt"))
            ],
        )
        if encrypted:
            self._enrich_encrypted_source(source)
        return source

    @staticmethod
    def _select_asset(assets: list[Any]) -> dict[str, Any]:
        usable = [asset for asset in assets if isinstance(asset, dict) and clean_text(asset.get("url"))]
        encrypted = [asset for asset in usable if bool(asset.get("encrypted"))]
        if encrypted:
            return next(
                (asset for asset in encrypted if clean_text(asset.get("format")).casefold() == "dash"), encrypted[0]
            )
        return next((asset for asset in usable if clean_text(asset.get("format")).casefold() == "hls"), usable[0])

    def _enrich_encrypted_source(self, source: PlaybackSource) -> None:
        response = self._request(
            "GET",
            source.manifest_url,
            headers={"Accept": "application/dash+xml,application/xml", "Android-Caller": ANDROID_CALLER},
        )
        kids, license_url = extract_drm_data(response.content, source.manifest_url, source.encryption_scheme)
        source.license_url = license_url
        if source.encryption_scheme == "clearkey":
            source.keys = self.request_clearkey(kids, license_url)

    def request_clearkey(self, kids: list[str], license_url: str) -> list[str]:
        if not kids or not license_url:
            raise NrkError("NRK ClearKey manifest did not contain key information")
        try:
            response = self.session.post(
                license_url,
                json={"kids": [_b64url(bytes.fromhex(kid)) for kid in kids], "type": "temporary"},
                headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise NrkError(f"NRK ClearKey request failed: {exc}") from exc
        payload = self._response_payload(response)
        if response.status_code >= 400:
            raise NrkError(f"NRK ClearKey request failed: HTTP {response.status_code}", payload)
        entries = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise NrkError("NRK ClearKey response returned no keys", payload)
        result: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("kid") or not entry.get("k"):
                continue
            try:
                kid = _b64url_decode(str(entry["kid"])).hex()
                key = _b64url_decode(str(entry["k"])).hex()
            except (ValueError, binascii.Error):
                continue
            if len(kid) == 32 and len(key) == 32:
                result.append(f"{kid}:{key}")
        if not result:
            raise NrkError("NRK ClearKey response contained no usable keys", payload)
        return list(dict.fromkeys(result))

    def widevine_license(self, license_url: str, challenge: bytes) -> bytes:
        try:
            response = self.session.post(
                license_url,
                data=challenge,
                headers={
                    "Accept": "application/octet-stream",
                    "Content-Type": "application/octet-stream",
                    "User-Agent": USER_AGENT,
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise NrkError(f"NRK licence request failed: {exc}") from exc
        if response.status_code >= 400:
            raise NrkError(f"NRK licence request failed: HTTP {response.status_code}", response.text[:500])
        return response.content


def local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _kid_values(value: Any) -> Iterator[str]:
    for raw in re.split(r"[,\s]+", clean_text(value)):
        if not raw:
            continue
        compact = raw.replace("-", "")
        if len(compact) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", compact):
            yield compact.casefold()


def extract_drm_data(manifest: bytes, manifest_url: str, scheme: str) -> tuple[list[str], str]:
    try:
        root = ET.fromstring(manifest)
    except ET.ParseError as exc:
        raise NrkError("NRK encrypted manifest is not valid XML") from exc
    wanted = scheme.casefold()
    keys: list[str] = []
    license_url = ""
    for element in root.iter():
        if local_name(element.tag).casefold() != "contentprotection":
            continue
        attrs = {local_name(str(key)).casefold(): str(value) for key, value in element.attrib.items()}
        scheme_uri = attrs.get("schemeiduri", "").casefold()
        is_match = (wanted == "clearkey" and (CLEARKEY_UUID in scheme_uri or "clearkey" in scheme_uri)) or (
            wanted != "clearkey"
            and ("widevine" in scheme_uri or scheme_uri.endswith("edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"))
        )
        if not is_match:
            # DASH commonly puts the default_KID on the sibling mp4protection
            # element and the licence URL on the ClearKey/Widevine element.
            keys.extend(_kid_values(attrs.get("default_kid") or attrs.get("kid")))
            continue
        keys.extend(_kid_values(attrs.get("default_kid") or attrs.get("kid")))
        for name, value in attrs.items():
            if name in {"licenseurl", "laurl", "href"} and value:
                license_url = urljoin(manifest_url, value)
        for child in element.iter():
            if child is element:
                continue
            child_name = local_name(child.tag).casefold()
            if child_name in {"laurl", "licenseurl"} and clean_text(child.text):
                license_url = urljoin(manifest_url, clean_text(child.text))
            for name, value in child.attrib.items():
                if local_name(str(name)).casefold() in {"licenseurl", "laurl", "href"} and clean_text(value):
                    license_url = urljoin(manifest_url, clean_text(value))
    return list(dict.fromkeys(keys)), license_url


__all__ = [
    "ANDROID_CALLER",
    "AuthenticationRequired",
    "CatalogItem",
    "Channel",
    "DEVICE_LOGIN_CONTEXT",
    "DeviceChallenge",
    "HomeSection",
    "LOGIN_USER_AGENT",
    "NrkApi",
    "NrkError",
    "ParsedTarget",
    "PlaybackSource",
    "Season",
    "SessionState",
    "Series",
    "TOKEN_FILE",
    "USER_AGENT",
    "extract_drm_data",
    "format_duration",
    "item_from_raw",
    "new_device_identifier",
    "parse_input",
    "parse_iso_duration",
]
