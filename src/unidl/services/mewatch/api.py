"""mewatch Android TV API, Auth0 device login and Axis playback.

"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests

API = "https://www.mewatch.sg/api/"
AUTH0 = "https://auth.mediacorp.sg"
AUTH0_CLIENT_ID = "e0SK6ykchfnk3D3rSgTRpyb7DgrAUCJB"
AUTH0_AUDIENCE = "https://mewatch.sg/api/axis"
AUTH0_SCOPE = "openid profile email offline_access"
AUTH0_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

TOKEN_FILE = "mewatch_token.json"
DEVICE = "tv_android"
LANGUAGE = "en"
SEGMENTS = "all"
FEATURE_FLAGS = "ldp,idp,rpt,cd"
USER_AGENT = "okhttp/4.12.0"
REQUEST_TIMEOUT = 25
TOKEN_SKEW = 60
PAGE_SIZE = 24

_HOSTS = frozenset({"mewatch.sg", "www.mewatch.sg"})
_ID_AT_END = re.compile(r"(?:^|[-/])(\d+)$")
_YEAR = re.compile(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)")


class MewatchError(RuntimeError):
    """mewatch rejected a request or returned an unusable response."""

    def __init__(self, message: str, *, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _year_in(value: Any) -> int:
    match = _YEAR.search(str(value or ""))
    return int(match.group(1)) if match else 0


def _release_year(
    payload: dict[str, Any],
    *,
    title: str,
    item_type: str,
    custom_fields: dict[str, Any],
) -> int:
    year = _as_int(payload.get("releaseYear"), 0)
    if year:
        return year
    type_description = str(custom_fields.get("TypeDescription") or "").lower()
    if item_type not in {"movie", "film"} and type_description not in {"movie", "film"}:
        return 0
    metadata = payload.get("customMetadata")
    if isinstance(metadata, list):
        for row in metadata:
            if not isinstance(row, dict) or str(row.get("name") or "").lower() != "releaseyear":
                continue
            if found := _year_in(row.get("value")):
                return found
    for value in (
        title,
        custom_fields.get("TheatricalReleaseStart"),
        custom_fields.get("TxDate"),
        custom_fields.get("LicensingWindowStart"),
    ):
        if found := _year_in(value):
            return found
    return 0


def _timestamp(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _jwt_claim(token: str, key: str) -> str:
    try:
        segment = token.split(".")[1]
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        payload = json.loads(raw)
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return ""
    return str(payload.get(key) or "") if isinstance(payload, dict) else ""


def _token_expiry(token: str) -> float:
    return float(_as_int(_jwt_claim(token, "exp"), 0))


@dataclass(frozen=True)
class AxisToken:
    value: str
    type: str
    scope: str = "Catalog"
    expires_at: float = 0.0
    refreshable: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AxisToken | None:
        value = str(payload.get("value") or "")
        kind = str(payload.get("type") or "")
        if not value or kind not in {"Anonymous", "UserAccount", "UserProfile"}:
            return None
        scope = _jwt_claim(value, "sub") or str(payload.get("scope") or "Catalog")
        expires_at = _timestamp(payload.get("expirationDate")) or _token_expiry(value)
        return cls(
            value=value,
            type=kind,
            scope=scope,
            expires_at=expires_at,
            refreshable=bool(payload.get("refreshable")),
        )

    def fresh(self, skew: int = TOKEN_SKEW) -> bool:
        return bool(self.value) and (self.expires_at <= 0 or time.time() < self.expires_at - skew)

    def to_cache(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "type": self.type,
            "scope": self.scope,
            "expires_at": self.expires_at,
            "refreshable": self.refreshable,
        }

    @classmethod
    def from_cache(cls, payload: Any) -> AxisToken | None:
        if not isinstance(payload, dict):
            return None
        value = str(payload.get("value") or "")
        kind = str(payload.get("type") or "")
        if not value or kind not in {"Anonymous", "UserAccount", "UserProfile"}:
            return None
        return cls(
            value=value,
            type=kind,
            scope=str(payload.get("scope") or _jwt_claim(value, "sub") or "Catalog"),
            expires_at=_as_float(payload.get("expires_at") or _token_expiry(value), 0),
            refreshable=bool(payload.get("refreshable")),
        )


@dataclass
class Session:
    device_id: str = ""
    auth0_access_token: str = ""
    auth0_refresh_token: str = ""
    auth0_id_token: str = ""
    auth0_expires_at: float = 0.0
    axis_tokens: list[AxisToken] = field(default_factory=list)
    account: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_cache(cls, payload: dict[str, Any] | None) -> Session:
        data = payload if isinstance(payload, dict) else {}
        tokens = [AxisToken.from_cache(item) for item in data.get("axis_tokens", [])]
        state = cls(
            device_id=str(data.get("device_id") or ""),
            auth0_access_token=str(data.get("auth0_access_token") or ""),
            auth0_refresh_token=str(data.get("auth0_refresh_token") or ""),
            auth0_id_token=str(data.get("auth0_id_token") or ""),
            auth0_expires_at=_as_float(data.get("auth0_expires_at"), 0),
            axis_tokens=[token for token in tokens if token is not None],
            account=data.get("account") if isinstance(data.get("account"), dict) else {},
            profile=data.get("profile") if isinstance(data.get("profile"), dict) else {},
        )
        state.ensure_device_id()
        return state

    def to_cache(self) -> dict[str, Any]:
        self.ensure_device_id()
        return {
            "device_id": self.device_id,
            "auth0_access_token": self.auth0_access_token,
            "auth0_refresh_token": self.auth0_refresh_token,
            "auth0_id_token": self.auth0_id_token,
            "auth0_expires_at": self.auth0_expires_at,
            "axis_tokens": [token.to_cache() for token in self.axis_tokens],
            "account": self.account,
            "profile": self.profile,
        }

    def ensure_device_id(self) -> None:
        try:
            self.device_id = str(uuid.UUID(self.device_id))
        except (ValueError, TypeError, AttributeError):
            self.device_id = str(uuid.uuid4())

    @property
    def signed_in(self) -> bool:
        return bool(
            (self.auth0_access_token or self.auth0_refresh_token)
            and self.token("UserAccount", fresh=False)
        )

    @property
    def profile_name(self) -> str:
        return str(self.profile.get("name") or "")

    def auth0_fresh(self, skew: int = TOKEN_SKEW) -> bool:
        return bool(self.auth0_access_token) and (
            self.auth0_expires_at <= 0 or time.time() < self.auth0_expires_at - skew
        )

    def token(self, kind: str, scope: str = "Catalog", *, fresh: bool = True) -> AxisToken | None:
        match = next(
            (
                token
                for token in reversed(self.axis_tokens)
                if token.type == kind and token.scope.lower() == scope.lower()
            ),
            None,
        )
        if match is None and scope == "Catalog":
            match = next((token for token in reversed(self.axis_tokens) if token.type == kind), None)
        return match if match is not None and (not fresh or match.fresh()) else None

    def ingest_axis(self, payload: Any) -> None:
        rows = payload if isinstance(payload, list) else [payload]
        incoming = [AxisToken.from_payload(row) for row in rows if isinstance(row, dict)]
        merged = {(token.type, token.scope): token for token in self.axis_tokens}
        for token in incoming:
            if token is not None:
                merged[(token.type, token.scope)] = token
        self.axis_tokens = list(merged.values())

    def ingest_auth0(self, payload: dict[str, Any]) -> None:
        access = str(payload.get("access_token") or "")
        if not access:
            raise MewatchError("Auth0 completed without an access token")
        self.auth0_access_token = access
        self.auth0_refresh_token = str(payload.get("refresh_token") or self.auth0_refresh_token)
        self.auth0_id_token = str(payload.get("id_token") or self.auth0_id_token)
        expires_in = _as_int(payload.get("expires_in"), 0)
        self.auth0_expires_at = time.time() + expires_in if expires_in else _token_expiry(access)

    def clear_account(self) -> None:
        self.auth0_access_token = ""
        self.auth0_refresh_token = ""
        self.auth0_id_token = ""
        self.auth0_expires_at = 0.0
        self.axis_tokens = [token for token in self.axis_tokens if token.type == "Anonymous"]
        self.account = {}
        self.profile = {}


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int
    created_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() >= self.created_at + self.expires_in


@dataclass(frozen=True)
class ParsedInput:
    kind: str
    value: str


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    type: str
    subtype: str = ""
    path: str = ""
    description: str = ""
    year: int = 0
    season_number: int = 0
    episode_number: int = 0
    episode_name: str = ""
    season_id: str = ""
    show_id: str = ""
    duration: int = 0
    available_seasons: int = 0
    genres: tuple[str, ...] = ()
    audio_languages: tuple[str, ...] = ()
    offers: tuple[dict[str, Any], ...] = ()
    encrypted: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_payload(cls, payload: Any) -> Item | None:
        if not isinstance(payload, dict):
            return None
        item_id = str(payload.get("id") or "")
        title = str(
            payload.get("title")
            or payload.get("contextualTitle")
            or payload.get("name")
            or ""
        ).strip()
        if not item_id or not title:
            return None
        genres = payload.get("genres") if isinstance(payload.get("genres"), list) else []
        languages = (
            payload.get("audioLanguages") if isinstance(payload.get("audioLanguages"), list) else []
        )
        offers = payload.get("offers") if isinstance(payload.get("offers"), list) else []
        custom_fields = payload.get("customFields") if isinstance(payload.get("customFields"), dict) else {}
        item_type = str(payload.get("type") or "").lower()
        return cls(
            id=item_id,
            title=title,
            type=item_type,
            subtype=str(payload.get("subtype") or ""),
            path=str(payload.get("path") or payload.get("watchPath") or ""),
            description=str(payload.get("shortDescription") or payload.get("description") or ""),
            year=_release_year(
                payload,
                title=title,
                item_type=item_type,
                custom_fields=custom_fields,
            ),
            season_number=_as_int(payload.get("seasonNumber"), 0),
            episode_number=_as_int(payload.get("episodeNumber"), 0),
            episode_name=str(payload.get("episodeName") or payload.get("title") or ""),
            season_id=str(payload.get("seasonId") or ""),
            show_id=str(payload.get("showId") or ""),
            duration=_as_int(payload.get("duration"), 0),
            available_seasons=_as_int(payload.get("availableSeasonCount"), 0),
            genres=tuple(str(value) for value in genres if value),
            audio_languages=tuple(str(value) for value in languages if value),
            offers=tuple(value for value in offers if isinstance(value, dict)),
            encrypted=_optional_bool(custom_fields.get("Encryption")),
            raw=dict(payload),
        )

    @property
    def is_show(self) -> bool:
        return self.type in {"show", "series"}

    @property
    def is_movie(self) -> bool:
        return self.type in {"movie", "film"}

    @property
    def is_episode(self) -> bool:
        return self.type in {"episode", "video", "clip", "extra"}

    @property
    def movie_title(self) -> str:
        if not self.is_movie or not self.year:
            return self.title
        return re.sub(rf"\s*\({self.year}\)\s*$", "", self.title).strip() or self.title

    @property
    def type_label(self) -> str:
        labels = {
            "show": "Series",
            "series": "Series",
            "movie": "Movie",
            "film": "Movie",
            "episode": "Episode",
            "clip": "Clip",
            "extra": "Extra",
            "channel": "Channel",
            "event": "Event",
        }
        return labels.get(self.type, self.type.title() or "Title")

    @property
    def label(self) -> str:
        if self.is_episode and self.episode_number:
            return f"E{self.episode_number:02d}  {self.episode_name or self.title}"
        if self.is_movie and self.year:
            return f"{self.movie_title} ({self.year})"
        return self.title

    @property
    def detail(self) -> str:
        parts = [self.type_label]
        if self.genres:
            parts.append(", ".join(self.genres[:3]))
        if self.available_seasons:
            parts.append(f"{self.available_seasons} season(s)")
        if self.duration:
            parts.append(f"{max(1, self.duration // 60)} min")
        if self.description:
            parts.append(self.description[:100])
        return " · ".join(parts)


@dataclass(frozen=True)
class Season:
    id: str
    number: int
    episodes: tuple[Item, ...]

    @property
    def label(self) -> str:
        return f"Season {self.number}" if self.number else "Episodes"


@dataclass(frozen=True)
class Rail:
    id: str
    title: str
    items: tuple[Item, ...]
    size: int = 0


@dataclass(frozen=True)
class LiveChannel:
    id: str
    name: str
    group: str = ""
    description: str = ""
    path: str = ""

    @property
    def detail(self) -> str:
        return " · ".join(part for part in (self.group, self.description[:90]) if part)


@dataclass(frozen=True)
class Source:
    manifest: str
    license_url: str = ""
    format: str = ""
    drm_scheme: str = "NONE"
    name: str = ""
    language: str = ""
    height: int = 0
    headers: tuple[tuple[str, str], ...] = ()
    subtitles: tuple[tuple[str, str], ...] = ()
    is_live: bool = False
    encrypted_hint: bool | None = None

    @property
    def encrypted(self) -> bool:
        if self.encrypted_hint is not None:
            return self.encrypted_hint
        return self.drm_scheme.upper() not in {"", "NONE"} or bool(self.license_url)

    @property
    def protocol(self) -> str:
        value = self.format.lower()
        if "dash" in value or "mpd" in value or self.manifest.lower().split("?", 1)[0].endswith(".mpd"):
            return "DASH"
        if "hls" in value or self.manifest.lower().split("?", 1)[0].endswith(".m3u8"):
            return "HLS"
        return self.format.upper() or "stream"

    def line(self) -> str:
        parts = [self.protocol]
        if self.height:
            parts.append(f"{self.height}p")
        parts.append("Widevine" if self.encrypted else "clear")
        if self.language:
            parts.append(self.language)
        if self.subtitles:
            parts.append(f"{len(self.subtitles)} subtitle(s)")
        return " · ".join(parts)


def parse_input(value: str) -> ParsedInput | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower() in {"home", "/"}:
        return ParsedInput("page", "/")
    if text.lower() in {"movies", "movie"}:
        return ParsedInput("page", "/movies")
    if text.lower() in {"series", "shows", "tv"}:
        return ParsedInput("page", "/series")
    if text.isdigit():
        return ParsedInput("item", text)

    candidate = text if "://" in text else f"https://{text}"
    parsed = urlparse(candidate)
    if (parsed.hostname or "").lower() not in _HOSTS:
        return None
    path = parsed.path or "/"
    if parsed.fragment:
        fragment = parsed.fragment
        path = fragment if fragment.startswith("/") else f"/{fragment}"
    page = path.rstrip("/") or "/"
    if page in {"/", "/movies", "/series", "/live-tv", "/live-tv/free-channels"}:
        return ParsedInput("page", page)
    match = _ID_AT_END.search(page)
    return ParsedInput("item", match.group(1)) if match else None


class MewatchApi:
    def __init__(
        self,
        session: requests.Session,
        state: Session | None = None,
        *,
        on_save=None,
    ):
        self.http = session
        self.state = state or Session()
        self.state.ensure_device_id()
        self.on_save = on_save
        self.http.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def _save(self) -> None:
        if self.on_save is not None:
            self.on_save(self.state)

    @staticmethod
    def _error(response: requests.Response) -> MewatchError:
        message = ""
        code = ""
        try:
            payload = response.json()
        except (ValueError, requests.RequestException):
            payload = None
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("error_description") or payload.get("error") or "")
            code = str(payload.get("code") or payload.get("error") or "")
        elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
            message = str(payload[0].get("message") or payload[0].get("error") or "")
        label = f"HTTP {response.status_code}"
        if code:
            label += f" [{code}]"
        return MewatchError(
            f"{label}: {message or response.reason or 'request rejected'}",
            status=response.status_code,
            code=code,
        )

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        try:
            response = self.http.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise MewatchError(f"mewatch request failed: {exc}") from exc
        if response.status_code >= 400:
            error = self._error(response)
            response.close()
            raise error
        return response

    def _json(self, method: str, url: str, **kwargs) -> Any:
        response = self._request(method, url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise MewatchError(f"mewatch returned invalid JSON from {urlparse(url).path}") from exc
        finally:
            response.close()

    @staticmethod
    def common_params(**extra: Any) -> dict[str, Any]:
        return {
            "device": DEVICE,
            "segments": SEGMENTS,
            "ff": FEATURE_FLAGS,
            "lang": LANGUAGE,
            **extra,
        }

    def _axis_headers(self, kind: str = "") -> dict[str, str]:
        token = None
        if kind:
            token = self.state.token(kind)
        else:
            for candidate in ("UserProfile", "UserAccount", "Anonymous"):
                token = self.state.token(candidate)
                if token is not None:
                    break
        return {"X-Authorization": f"Bearer {token.value}"} if token is not None else {}

    # ------------------------------------------------------------------ auth
    def ensure_anonymous(self) -> AxisToken:
        existing = self.state.token("Anonymous")
        if existing is not None:
            return existing
        payload = self._json(
            "POST",
            API + "authorization/anonymous",
            json={"deviceId": self.state.device_id},
            params={"ff": FEATURE_FLAGS, "lang": LANGUAGE},
        )
        self.state.ingest_axis(payload)
        token = self.state.token("Anonymous")
        if token is None:
            raise MewatchError("mewatch anonymous authorization returned no Catalog token")
        self._save()
        return token

    def start_tv_code(self) -> DeviceCode:
        self.ensure_anonymous()
        payload = self._json(
            "POST",
            AUTH0 + "/oauth/device/code",
            data={
                "client_id": AUTH0_CLIENT_ID,
                "audience": AUTH0_AUDIENCE,
                "scope": AUTH0_SCOPE,
            },
        )
        challenge = DeviceCode(
            device_code=str(payload.get("device_code") or ""),
            user_code=str(payload.get("user_code") or ""),
            verification_uri=str(payload.get("verification_uri") or ""),
            verification_uri_complete=str(payload.get("verification_uri_complete") or ""),
            expires_in=_as_int(payload.get("expires_in"), 900),
            interval=max(3, _as_int(payload.get("interval"), 5)),
        )
        if not challenge.device_code or not challenge.user_code or not challenge.verification_uri:
            raise MewatchError("Auth0 returned an incomplete TV activation code")
        return challenge

    def poll_tv_code(self, challenge: DeviceCode) -> Session | None:
        if challenge.expired:
            raise MewatchError("The mewatch TV code expired; request a new code")
        try:
            payload = self._json(
                "POST",
                AUTH0 + "/oauth/token",
                data={
                    "client_id": AUTH0_CLIENT_ID,
                    "device_code": challenge.device_code,
                    "grant_type": AUTH0_DEVICE_GRANT,
                },
            )
        except MewatchError as exc:
            text = str(exc).lower()
            if "authorization_pending" in text or "slow_down" in text:
                return None
            if "expired_token" in text:
                raise MewatchError("The mewatch TV code expired; request a new code") from exc
            if "access_denied" in text:
                raise MewatchError("mewatch TV activation was denied") from exc
            raise
        self.state.ingest_auth0(payload)
        self._finish_account_login()
        self._save()
        return self.state

    def _finish_account_login(self) -> None:
        anonymous = self.ensure_anonymous()
        payload = self._json(
            "POST",
            API + "v2/authorization/sso/anonymous",
            headers={"X-Authorization": f"Bearer {anonymous.value}"},
            params={"ff": FEATURE_FLAGS, "lang": LANGUAGE},
            json={
                "token": self.state.auth0_access_token,
                "scopes": ["Catalog"],
                "deviceId": self.state.device_id,
            },
        )
        self.state.ingest_axis(payload)
        if self.state.token("UserAccount") is None:
            raise MewatchError("mewatch SSO returned no UserAccount Catalog token")

        try:
            self._json(
                "POST",
                API + "account/devices",
                headers=self._axis_headers("UserAccount"),
                params={"ff": FEATURE_FLAGS, "lang": LANGUAGE},
                json={"id": self.state.device_id, "name": "Living Room TV", "brandId": 105},
            )
        except MewatchError as exc:
            if exc.status != 409:
                raise

        account = self._json(
            "GET",
            API + "v2/account",
            headers={
                **self._axis_headers("UserAccount"),
                "token": self.state.auth0_access_token,
            },
            params={"ff": FEATURE_FLAGS, "lang": LANGUAGE},
        )
        self.state.account = account if isinstance(account, dict) else {}
        profile_id = str(
            self.state.account.get("profileId")
            or self.state.account.get("primaryProfileId")
            or self._first_profile_id()
            or ""
        )
        if self.state.token("UserProfile") is None and profile_id:
            self.select_profile(profile_id)
        elif self.state.token("UserProfile") is not None:
            self._load_profile()

    def _first_profile_id(self) -> str:
        profiles = self.state.account.get("profiles")
        if not isinstance(profiles, list):
            return ""
        first = next((row for row in profiles if isinstance(row, dict) and row.get("id")), None)
        return str(first.get("id") or "") if first else ""

    def profiles(self) -> list[dict[str, Any]]:
        rows = self.state.account.get("profiles")
        return [dict(row) for row in rows if isinstance(row, dict) and row.get("id")] if isinstance(rows, list) else []

    def select_profile(self, profile_id: str, pin: str = "") -> dict[str, Any]:
        payload = self._json(
            "POST",
            API + "authorization/profile",
            headers=self._axis_headers("UserAccount"),
            params={"ff": FEATURE_FLAGS, "lang": LANGUAGE},
            json={
                "profileId": str(profile_id),
                "pin": str(pin or ""),
                "scopes": ["Catalog"],
                "cookieType": "Session",
            },
        )
        self.state.ingest_axis(payload)
        if self.state.token("UserProfile") is None:
            raise MewatchError("mewatch profile selection returned no UserProfile token")
        return self._load_profile()

    def _load_profile(self) -> dict[str, Any]:
        payload = self._json(
            "GET",
            API + "account/profile",
            headers=self._axis_headers("UserProfile"),
            params={"ff": FEATURE_FLAGS, "lang": LANGUAGE},
        )
        self.state.profile = payload if isinstance(payload, dict) else {}
        self._save()
        return self.state.profile

    def _refresh_auth0(self) -> None:
        if self.state.auth0_fresh():
            return
        if not self.state.auth0_refresh_token:
            raise MewatchError("The mewatch Auth0 session expired; sign in again")
        payload = self._json(
            "POST",
            AUTH0 + "/oauth/token",
            data={
                "client_id": AUTH0_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self.state.auth0_refresh_token,
            },
        )
        self.state.ingest_auth0(payload)

    def refresh(self) -> bool:
        if not self.state.signed_in:
            return False
        self._refresh_auth0()
        for kind in ("UserAccount", "UserProfile"):
            token = self.state.token(kind, fresh=False)
            if token is None or token.fresh():
                continue
            payload = self._json(
                "POST",
                API + "v2/authorization/refresh",
                params={"ff": FEATURE_FLAGS, "lang": LANGUAGE},
                json={"token": token.value, "oktaToken": self.state.auth0_access_token},
            )
            self.state.ingest_axis(payload)
        if self.state.token("UserAccount") is None:
            raise MewatchError("The mewatch account token could not be refreshed; sign in again")
        self._save()
        return True

    def ensure_auth(self, *, required: bool = False) -> bool:
        if self.state.signed_in:
            self.refresh()
            return True
        self.ensure_anonymous()
        if required:
            raise MewatchError("Sign in to mewatch from the service menu first")
        return False

    # --------------------------------------------------------------- catalog
    def item(self, item_id: str) -> Item:
        self.ensure_auth()
        payload = self._json(
            "GET",
            API + f"items/{item_id}",
            headers=self._axis_headers(),
            params=self.common_params(),
        )
        item = Item.from_payload(payload)
        if item is None:
            raise MewatchError(f"mewatch returned no usable item for {item_id}")
        return item

    def show_seasons(self, show_id: str) -> list[Season]:
        self.ensure_auth()
        items: list[Item] = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            payload = self._json(
                "GET",
                API + f"items/{show_id}/show-episodes",
                headers=self._axis_headers(),
                params=self.common_params(page=page, page_size=PAGE_SIZE, order="desc"),
            )
            if not isinstance(payload, dict):
                raise MewatchError(f"mewatch returned an invalid episode list for {show_id}")
            rows = payload.get("items") if isinstance(payload.get("items"), list) else []
            items.extend(item for row in rows if (item := Item.from_payload(row)) is not None)
            paging = payload.get("paging") if isinstance(payload.get("paging"), dict) else {}
            total_pages = max(1, _as_int(paging.get("total"), 1))
            page += 1

        groups: dict[tuple[str, int], list[Item]] = {}
        for item in items:
            key = (item.season_id, item.season_number)
            groups.setdefault(key, []).append(item)
        return [
            Season(season_id or f"season-{number}", number, tuple(episodes))
            for (season_id, number), episodes in sorted(groups.items(), key=lambda row: row[0][1])
        ]

    def search(self, term: str, max_results: int = 24) -> list[Item]:
        self.ensure_auth()
        payload = self._json(
            "GET",
            API + "search",
            headers=self._axis_headers(),
            params=self.common_params(term=term, group="true", max_results=max_results),
        )
        if not isinstance(payload, dict):
            raise MewatchError("mewatch returned an invalid search response")
        found: list[Item] = []
        seen: set[str] = set()
        for key in ("tv", "movies", "extras", "sports"):
            group = payload.get(key)
            rows = group.get("items") if isinstance(group, dict) else []
            for row in rows if isinstance(rows, list) else []:
                item = Item.from_payload(row)
                if item is not None and item.id not in seen:
                    seen.add(item.id)
                    found.append(item)
        return found

    def page(self, path: str = "/") -> list[Rail]:
        self.ensure_auth()
        payload = self._json(
            "GET",
            API + "page",
            headers=self._axis_headers(),
            params=self.common_params(
                path=path,
                list_page_size=PAGE_SIZE,
                list_page_size_large=100,
                max_list_prefetch=10,
            ),
        )
        if not isinstance(payload, dict):
            raise MewatchError(f"mewatch returned an invalid page for {path}")
        rails: list[Rail] = []
        entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            listing = entry.get("list") if isinstance(entry.get("list"), dict) else {}
            rows = listing.get("items") if isinstance(listing.get("items"), list) else []
            items = tuple(item for row in rows if (item := Item.from_payload(row)) is not None)
            list_id = str(listing.get("id") or entry.get("id") or "")
            size = _as_int(listing.get("size"), len(items))
            if not items and (not list_id.isdigit() or size <= 0):
                continue
            rails.append(
                Rail(
                    id=list_id,
                    title=str(entry.get("title") or listing.get("title") or "Titles"),
                    items=items,
                    size=size,
                )
            )
        return rails

    def rail_items(self, rail: Rail) -> tuple[Item, ...]:
        if rail.items and (rail.size <= 0 or len(rail.items) >= rail.size):
            return rail.items
        if not rail.id.isdigit():
            return rail.items
        items = list(rail.items)
        seen = {item.id for item in items}
        page = len(items) // PAGE_SIZE + 1 if items else 1
        total_pages = max(1, (rail.size + PAGE_SIZE - 1) // PAGE_SIZE) if rail.size else 1
        while page <= total_pages:
            payload = self._json(
                "GET",
                API + f"lists/{rail.id}",
                headers=self._axis_headers(),
                params=self.common_params(page=page, page_size=PAGE_SIZE),
            )
            if not isinstance(payload, dict):
                raise MewatchError(f"mewatch returned an invalid list for {rail.title}")
            rows = payload.get("items") if isinstance(payload.get("items"), list) else []
            added = 0
            for row in rows:
                item = Item.from_payload(row)
                if item is not None and item.id not in seen:
                    seen.add(item.id)
                    items.append(item)
                    added += 1
            paging = payload.get("paging") if isinstance(payload.get("paging"), dict) else {}
            total_pages = max(total_pages, _as_int(paging.get("total"), total_pages))
            if not rows or not added:
                break
            page += 1
        return tuple(items)

    def live_channels(self) -> list[LiveChannel]:
        self.ensure_auth()
        payload = self._json(
            "GET",
            API + "page",
            headers=self._axis_headers(),
            params=self.common_params(
                path="/live-tv/free-channels",
                list_page_size=PAGE_SIZE,
                list_page_size_large=100,
                max_list_prefetch=10,
            ),
        )
        if not isinstance(payload, dict):
            raise MewatchError("mewatch returned an invalid live channel page")
        channels: list[LiveChannel] = []
        seen: set[str] = set()
        entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            listing = entry.get("list") if isinstance(entry.get("list"), dict) else {}
            group = str(entry.get("title") or listing.get("title") or "Live")
            rows = listing.get("items") if isinstance(listing.get("items"), list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                path = str(row.get("path") or "")
                match = re.search(r"/watch/(\d+)(?:$|[/?#])", path)
                if match is None or match.group(1) in seen:
                    continue
                seen.add(match.group(1))
                channels.append(
                    LiveChannel(
                        id=match.group(1),
                        name=str(row.get("title") or f"Channel {match.group(1)}"),
                        group=group,
                        description=str(row.get("shortDescription") or ""),
                        path=path,
                    )
                )
        return channels

    # --------------------------------------------------------------- playback
    @staticmethod
    def _media_headers(value: Any) -> tuple[tuple[str, str], ...]:
        if isinstance(value, dict):
            return tuple((str(key), str(item)) for key, item in value.items())
        if not isinstance(value, str) or not value.strip():
            return ()
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return ()
        return (
            tuple((str(key), str(item)) for key, item in parsed.items())
            if isinstance(parsed, dict)
            else ()
        )

    @staticmethod
    def _source(
        payload: dict[str, Any],
        *,
        is_live: bool,
        encrypted: bool | None,
    ) -> Source | None:
        url = str(payload.get("url") or "")
        if not url:
            return None
        subtitle_rows = (
            payload.get("subtitlesCollection")
            if isinstance(payload.get("subtitlesCollection"), list)
            else []
        )
        subtitles = tuple(
            (
                str(row.get("languageCode") or row.get("language") or "und"),
                str(row.get("url") or ""),
            )
            for row in subtitle_rows
            if isinstance(row, dict) and row.get("url")
        )
        return Source(
            manifest=url,
            license_url=str(payload.get("drm") or ""),
            format=str(payload.get("format") or ""),
            drm_scheme=str(payload.get("drmScheme") or "NONE"),
            name=str(payload.get("name") or ""),
            language=str(payload.get("language") or ""),
            height=_as_int(payload.get("height"), 0),
            headers=MewatchApi._media_headers(payload.get("headers")),
            subtitles=subtitles,
            is_live=is_live,
            encrypted_hint=encrypted,
        )

    def playback(
        self,
        item_id: str,
        *,
        is_live: bool = False,
        encrypted: bool | None = None,
    ) -> Source:
        signed_in = self.ensure_auth()
        endpoint = (
            f"account/items/{item_id}/videos"
            if signed_in and self.state.token("UserProfile") is not None
            else f"items/{item_id}/videos"
        )
        params: list[tuple[str, str]] = [
            ("delivery", "stream"),
            ("resolution", "External"),
            ("device", DEVICE),
            ("segments", SEGMENTS),
            ("ff", FEATURE_FLAGS),
            ("lang", LANGUAGE),
        ]
        payload = self._json(
            "GET",
            API + endpoint,
            headers=self._axis_headers("UserProfile") if endpoint.startswith("account/") else self._axis_headers(),
            params=params,
        )
        if not isinstance(payload, list):
            raise MewatchError(f"mewatch returned an invalid playback response for {item_id}")
        sources = [
            source
            for row in payload
            if isinstance(row, dict)
            and (source := self._source(row, is_live=is_live, encrypted=encrypted)) is not None
        ]
        if not sources:
            raise MewatchError(f"mewatch returned no playable stream for {item_id}")

        def score(source: Source) -> tuple[int, int, int]:
            protocol = source.protocol
            scheme = source.drm_scheme.upper()
            supported_drm = not source.encrypted or scheme in {
                "",
                "NONE",
                "WIDEVINE",
                "WIDEVINE_CENC",
            }
            return (
                1 if supported_drm else 0,
                2 if protocol == "DASH" else (1 if protocol == "HLS" else 0),
                source.height,
            )

        selected = max(sources, key=score)
        if selected.encrypted and selected.drm_scheme.upper().startswith("PLAYREADY"):
            raise MewatchError("mewatch returned only PlayReady media to the Widevine TV client")
        if selected.encrypted and not selected.license_url:
            raise MewatchError("mewatch returned encrypted media without a Widevine licence URL")
        return selected

    def widevine_license(
        self,
        license_url: str,
        challenge: bytes,
        *,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        request_headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/octet-stream",
            **(headers or {}),
        }
        response = self._request("POST", license_url, data=challenge, headers=request_headers)
        try:
            if not response.content:
                raise MewatchError("mewatch Widevine licence response was empty")
            return response.content
        finally:
            response.close()

    def subtitle_bytes(self, url: str) -> bytes:
        response = self._request(
            "GET",
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/vtt,text/plain,*/*"},
        )
        try:
            if not response.content:
                raise MewatchError("mewatch subtitle response was empty")
            return response.content
        finally:
            response.close()


__all__ = [
    "DEVICE",
    "TOKEN_FILE",
    "USER_AGENT",
    "AxisToken",
    "DeviceCode",
    "Item",
    "LiveChannel",
    "MewatchApi",
    "MewatchError",
    "ParsedInput",
    "Rail",
    "Season",
    "Session",
    "Source",
    "parse_input",
]
