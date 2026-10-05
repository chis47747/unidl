"""France.tv Android TV (fr.francetv.pluzz) API.

Protocol mirrors the TV client 5.14.2: Yatta catalog, K7 playback metadata,
optional proxy-gin device-code login for catalogue items flagged ``login``.
K7 itself does not consume the account access token — login is a client-side
gate only. Playback is DASH/HLS; DRM (when present) uses Widevine with
``nv-authorizations`` from the DRM tokenizer.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import requests

APP_VERSION = "5.14.2"
PLAYER_VERSION = "7.19.1"
MEDIA3_VERSION = "1.9.0"
PACKAGE_NAME = "fr.francetv.pluzz"
PLATFORM = "apps_tv"
PROGRAM_SOURCE = "apps"
SEARCH_FILTERS = "only-replay,without-extracts,with-collections"
AUTH_SOURCE = "francetv-androidtv"
AUTH_SECRET = "yUjPe3mDsovExFJLDWI3ySIgZfkKitOr"

CATALOG_BASE_URL = "https://api-mobile.yatta.francetv.fr/"
AUTH_BASE_URL = "https://proxy-gin.francetv.fr"
K7_BASE_URL = "https://k7.ftven.fr/"
GEO_URL = "https://geo-info.ftven.fr/ws/edgescape.json"
ACTIVATION_URL = "https://www.france.tv/appstv/connexion"

ANDROID_RELEASE = "12"
ANDROID_API_LEVEL = "31"
DEVICE_NAME = "Google_Chromecast_Google_sabrina"
DEVICE_MODEL = "Chromecast"
DEVICE_BUILD = "STTE.220621.019.A2"
SCREEN_WIDTH = "1500"
K7_CAPABILITIES = "drm2+mlt+spr+dai+p2p"
SUPPORT_4K = True
TOKEN_MIN_TTL_SECONDS = 42
REQUEST_TIMEOUT = 30
LOGIN_POLL_MAX_ERRORS = 3
REFRESH_REJECTED_CODES = {"00016", "00022"}

TOKEN_FILE = "francetv_token.json"

USER_AGENT = (
    f"{PACKAGE_NAME}/{APP_VERSION} (Linux; Android {ANDROID_RELEASE}; "
    f"{DEVICE_MODEL} Build/{DEVICE_BUILD}) AndroidXMedia3/{MEDIA3_VERSION} "
    f"FtvPlayerLib/{PLAYER_VERSION}"
)

_INPUT_HOST_RE = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*france\.tv",
    re.IGNORECASE,
)
_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.html)?")
_ROUTE_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_MEDIA_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_CONTENT_SEGMENT_RE = re.compile(
    r"(?P<id>\d{5,})(?:-[A-Za-z0-9][A-Za-z0-9_-]*)?(?:\.html)?"
)


class FranceTvError(RuntimeError):
    """A service-local failure with a deliberately non-reflective message."""

    def __init__(self, message: str, payload: Any = None, *, status_code: int | None = None):
        super().__init__(message)
        # Provider bodies are never retained on an exception that can cross into UI code.
        self.payload = None
        self.status_code = status_code if isinstance(status_code, int) else None


class FranceTvNetworkError(FranceTvError):
    pass


class FranceTvAuthRequired(FranceTvError):
    pass


class RefreshRejected(FranceTvError):
    pass


# --------------------------------------------------------------------------- models


@dataclass
class Session:
    access_token: str = ""
    refresh_token: str = ""
    user_id: str = ""
    expires_at: int = 0
    expire_in: str = ""
    type_token: str = ""
    updated_at: int = 0

    @classmethod
    def from_cache(cls, raw: Any) -> Session:
        if not isinstance(raw, dict):
            return cls()
        return cls(
            access_token=_scalar_text(raw.get("access_token")),
            refresh_token=_scalar_text(raw.get("refresh_token")),
            user_id=_scalar_text(raw.get("user_id")),
            expires_at=_as_int(raw.get("expires_at")) or 0,
            expire_in=_scalar_text(raw.get("expire_in")),
            type_token=_scalar_text(raw.get("type_token")),
            updated_at=_as_int(raw.get("updated_at")) or 0,
        )

    def to_cache(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "user_id": self.user_id,
            "expires_at": self.expires_at,
            "expire_in": self.expire_in,
            "type_token": self.type_token,
            "updated_at": self.updated_at or int(time.time()),
        }

    def is_fresh(self, min_ttl: int = TOKEN_MIN_TTL_SECONDS) -> bool:
        if not self.access_token:
            return False
        exp = self.expires_at or _token_exp(self.access_token) or 0
        return exp >= int(time.time()) + min_ttl

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token and self.refresh_token)


@dataclass(frozen=True)
class CatalogItem:
    id: str
    si_id: str
    title: str
    kind: str = "content"
    program_title: str = ""
    season: int | None = None
    episode: int | None = None
    year: str = ""
    is_live: bool = False
    login_required: bool = False
    program_path: str = ""
    collection_path: str = ""
    event_path: str = ""
    channel_label: str = ""
    detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def label(self) -> str:
        return self.title or self.id or "Untitled"


@dataclass(frozen=True)
class Source:
    content_id: str
    media_id: str
    manifest: str
    title: str
    program_title: str = ""
    season: int | None = None
    episode: int | None = None
    year: str = ""
    is_live: bool = False
    encrypted: bool = False
    license_url: str = ""
    drm_token: str = ""
    format: str = ""
    note: str = ""


@dataclass
class DeviceCodeChallenge:
    api_code: str
    user_code: str
    expires_in: int
    interval: int
    url: str = ACTIVATION_URL
    deadline: float = 0.0
    transient_errors: int = 0


# --------------------------------------------------------------------------- helpers


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not re.fullmatch(r"[+-]?\d+", raw):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return None


def _scalar_text(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(value)
    return default


def _text(value: Any, default: str = "") -> str:
    return _scalar_text(value, default)


def _required_text(mapping: dict[str, Any], key: str, error: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FranceTvError(error)
    return value.strip()


def _optional_positive_int(mapping: dict[str, Any], *keys: str) -> tuple[bool, int | None]:
    for key in keys:
        if key in mapping:
            value = _as_int(mapping.get(key))
            return True, value if value is not None and value > 0 else None
    return False, None


def _list_field(mapping: dict[str, Any], key: str, *, required: bool = False) -> list[Any]:
    if key not in mapping:
        if required:
            raise FranceTvError("France.tv response is missing a required list")
        return []
    value = mapping.get(key)
    if not isinstance(value, list):
        raise FranceTvError("France.tv response contains an invalid list")
    return value


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_object_from_text(text: str) -> dict[str, Any]:
    starts: list[int] = []
    user_start = text.find('{"userId"')
    if user_start >= 0:
        starts.append(user_start)
    first = text.find("{")
    if first >= 0 and first not in starts:
        starts.append(first)
    for start in starts:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _decode_token_piece(piece: str) -> dict[str, Any]:
    raw = piece.strip()
    direct = _json_object_from_text(raw)
    if direct:
        return direct
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, TypeError, binascii.Error):
        return {}
    return _json_object_from_text(decoded)


def _token_payload(token: Any) -> dict[str, Any]:
    if not isinstance(token, str):
        return {}
    raw = token.removeprefix("Bearer ").strip()
    if not raw:
        return {}
    pieces = raw.split(".")
    candidates = []
    if len(pieces) > 1:
        candidates.append(pieces[1])
    candidates.extend(pieces)
    candidates.append(raw)
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        payload = _decode_token_piece(candidate)
        if payload and any(key in payload for key in ("userId", "exp", "sub")):
            return payload
    return {}


def _token_exp(token: Any) -> int | None:
    value = _as_int(_token_payload(token).get("exp"))
    return value if value is not None and value > 0 else None


def _token_user_id(token: Any) -> str:
    value = _token_payload(token).get("userId")
    return _scalar_text(value)


def _item_diffusion(item: dict[str, Any]) -> dict[str, Any]:
    channel = item.get("channel")
    if isinstance(channel, dict):
        return channel
    partner = item.get("partner")
    return partner if isinstance(partner, dict) else {}


def _is_movie(item: dict[str, Any]) -> bool:
    category = item.get("category")
    if isinstance(category, dict):
        label = _text(category.get("label")).lower()
        path = _text(category.get("url_complete")).lower()
        if category.get("id") == 33 or path == "films" or label in {"cinéma", "cinema"}:
            return True
    sub_categories = item.get("sub_categories")
    if not isinstance(sub_categories, list):
        return False
    for category in sub_categories:
        if isinstance(category, dict) and _text(category.get("url_complete")).lower().startswith(
            "films_"
        ):
            return True
    return False


def _program_title(item: dict[str, Any]) -> str:
    program = item.get("program")
    if isinstance(program, dict):
        return _text(program.get("label") or program.get("title"))
    return _text(item.get("program_title"))


def _item_title(item: dict[str, Any]) -> str:
    if item.get("_title"):
        return _text(item["_title"])
    if item.get("_entry_kind") == "program" or item.get("type") == "program":
        return _text(item.get("label") or item.get("title") or item.get("id"), "Untitled")
    return _text(
        item.get("title") or item.get("episode_title") or item.get("label") or item.get("id"),
        "Untitled",
    )


def _normalize_year(value: Any) -> str:
    year = _text(value)
    return year if re.fullmatch(r"(?:18|19|20|21)\d{2}", year) else ""


def _validated_https_url(
    value: Any,
    error: str,
    *,
    allowed_hosts: set[str] | None = None,
    default_port_only: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FranceTvError(error)
    raw = value.strip()
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise FranceTvError(error) from exc
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or (default_port_only and port not in {None, 443})
        or (allowed_hosts is not None and host not in allowed_hosts)
    ):
        raise FranceTvError(error)
    return raw


def _input_host_allowed(host: str) -> bool:
    return bool(_INPUT_HOST_RE.fullmatch(host))


def _decoded_segments(path: str) -> list[str] | None:
    segments: list[str] = []
    for raw_segment in path.split("/"):
        if not raw_segment:
            continue
        segment = unquote(raw_segment)
        if "/" in segment or "\\" in segment or not _PATH_SEGMENT_RE.fullmatch(segment):
            return None
        segments.append(segment)
    return segments


def _parse_supported_path(path: str, query: dict[str, list[str]]) -> tuple[str, str] | None:
    segments = _decoded_segments(path)
    if segments is None:
        return None

    for key in ("contentId", "content_id", "videoId", "id"):
        if key not in query:
            continue
        values = query[key]
        if len(values) != 1 or not re.fullmatch(r"\d{5,}", values[0]):
            return None
        return ("content", values[0])

    if "program_path" in query:
        program_values = query["program_path"]
        if len(program_values) != 1 or not _ROUTE_VALUE_RE.fullmatch(program_values[0]):
            return None
        return ("program", program_values[0])

    if not segments:
        return None
    if segments[-1].lower() in {"direct", "direct.html"} and len(segments) >= 2:
        slug = segments[-2].lower().removesuffix(".html")
        return ("live_slug", slug) if _ROUTE_VALUE_RE.fullmatch(slug) else None

    for segment in reversed(segments):
        match = _CONTENT_SEGMENT_RE.fullmatch(segment)
        if match:
            return ("content", match.group("id"))
    return ("taxonomy", "_".join(segments))


def _catalogue_route(value: Any, error: str) -> str:
    if not isinstance(value, str):
        raise FranceTvError(error)
    normalized = value.strip().strip("/")
    segments = normalized.split("/") if normalized else []
    if not segments or any(not _PATH_SEGMENT_RE.fullmatch(segment) for segment in segments):
        raise FranceTvError(error)
    return normalized


def _media_id(value: Any) -> str:
    raw = _scalar_text(value)
    if not _MEDIA_ID_RE.fullmatch(raw):
        raise FranceTvError("Selected content has invalid playback metadata")
    return raw


def _retryable_status(status_code: int) -> bool:
    return status_code in {408, 429} or status_code >= 500


def catalog_item_from_raw(item: dict[str, Any], *, entry_kind: str | None = None) -> CatalogItem | None:
    if not isinstance(item, dict):
        return None
    kind = entry_kind or _text(item.get("_entry_kind")).lower()
    entry_type = _text(item.get("type")).lower()
    if not kind:
        if entry_type in {"program", "feuilleton"}:
            kind = "program"
        elif entry_type == "collection":
            kind = "collection"
        elif entry_type == "event":
            kind = "event"
        elif entry_type == "article":
            kind = "article"
        elif item.get("id") and item.get("si_id"):
            kind = "content"
        else:
            return None

    if kind == "program" and not item.get("program_path"):
        return None
    if kind == "collection" and not item.get("collection_path"):
        return None
    if kind == "event" and not item.get("url_complete"):
        return None
    if kind == "article" and not _scalar_text(item.get("id")).isdigit():
        return None
    if kind == "content" and not (item.get("id") and item.get("si_id")):
        return None

    collection_path = _text(item.get("collection_path"))
    if kind == "collection" and collection_path:
        collection_path = collection_path.strip("/").replace("/", "_")

    diffusion = _item_diffusion(item)
    is_live = item.get("is_live") is True
    if kind == "content":
        if is_live:
            display_kind = "live"
        elif _is_movie(item):
            display_kind = "movie"
        else:
            display_kind = _text(item.get("type"), "content")
    else:
        display_kind = kind

    return CatalogItem(
        id=_scalar_text(item.get("id")),
        si_id=_scalar_text(item.get("si_id")),
        title=_item_title(item),
        kind=display_kind,
        program_title=_program_title(item),
        season=_as_int(item.get("season")),
        episode=_as_int(item.get("episode")),
        year=_normalize_year(item.get("production_year")),
        is_live=is_live,
        login_required=item.get("login") is True,
        program_path=_text(item.get("program_path")),
        collection_path=collection_path,
        event_path=_text(item.get("url_complete")),
        channel_label=_text(diffusion.get("label") or item.get("_collection_label")),
        detail=_text(item.get("description") or item.get("synopsis")),
        raw=dict(item),
    )


def parse_input(value: str) -> tuple[str, str] | None:
    """Return a strict supported route without reflecting rejected input."""
    raw = value.strip() if isinstance(value, str) else ""
    if not raw:
        return None
    if re.fullmatch(r"\d{5,}", raw):
        return ("content", raw)

    if "://" not in raw:
        first = raw.partition("/")[0].lower()
        if _input_host_allowed(first):
            raw = f"https://{raw}"
        else:
            if any(marker in raw for marker in ("?", "#", "@", ":")):
                return None
            return _parse_supported_path(f"/{raw.strip('/')}", {})

    try:
        parsed = urlparse(raw)
        port = parsed.port
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError):
        return None
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or not _input_host_allowed(host)
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        return None
    return _parse_supported_path(parsed.path, query)


# --------------------------------------------------------------------------- client


class FranceTvApi:
    def __init__(
        self,
        session: requests.Session,
        *,
        state: Session | None = None,
        on_save: Callable[[Session], None] | None = None,
        require_login: bool = False,
    ):
        self.session = session
        self.state = state or Session()
        self.on_save = on_save
        self.require_login = require_login
        self._geo: tuple[str, str] | None = None

    def _commit_state(self, state: Session) -> None:
        if self.on_save is not None:
            try:
                self.on_save(state)
            except Exception as exc:
                raise FranceTvError("France.tv session could not be saved") from exc
        self.state = state

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        safe_url = _validated_https_url(url, "France.tv refused an invalid provider endpoint")
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        try:
            return self.session.request(method, safe_url, **kwargs)
        except requests.RequestException as exc:
            raise FranceTvNetworkError("France.tv network request failed") from exc

    def _safe_json(self, response: requests.Response) -> Any:
        try:
            return response.json()
        except (TypeError, ValueError) as exc:
            raise FranceTvError(
                "France.tv returned an invalid JSON response",
                status_code=response.status_code,
            ) from exc

    def _error_json_object(self, response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _catalog_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request(
            "GET",
            urljoin(CATALOG_BASE_URL, path.lstrip("/")),
            params=dict(params or {}),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        if response.status_code >= 400:
            raise FranceTvError(
                "France.tv catalogue request failed",
                status_code=response.status_code,
            )
        payload = self._safe_json(response)
        if not isinstance(payload, dict):
            raise FranceTvError("France.tv catalogue response has an invalid shape")
        return payload

    def _auth_headers(
        self,
        path: str,
        body_text: str = "",
        *,
        access_token: str = "",
        refresh_token: str = "",
    ) -> dict[str, str]:
        proxy_user_id = str(time.time_ns() // 1_000_000)
        signing: dict[str, Any] = {}
        clean_access = access_token.removeprefix("Bearer ").strip()
        if clean_access:
            signing["accessToken"] = clean_access
        signing["path"] = path
        signing["proxyUserId"] = proxy_user_id
        if refresh_token:
            signing["refreshToken"] = refresh_token
        if body_text:
            signing["requestBody"] = json.loads(body_text)
        digest = hmac.new(
            AUTH_SECRET.encode("utf-8"),
            _compact_json(signing).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        headers = {
            "X-Source": AUTH_SOURCE,
            "X-User": proxy_user_id,
            "X-HMAC-Signature": signature,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if refresh_token:
            headers["X-Refresh-Token"] = refresh_token
        if clean_access:
            headers["Authorization"] = f"Bearer {clean_access}"
        return headers

    def _auth_call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        access_token: str = "",
        refresh_token: str = "",
    ) -> tuple[requests.Response, dict[str, Any]]:
        body_text = "" if body is None else _compact_json(body)
        headers = self._auth_headers(
            path,
            body_text,
            access_token=access_token,
            refresh_token=refresh_token,
        )
        kwargs: dict[str, Any] = {"headers": headers}
        if method.upper() in {"POST", "PUT", "PATCH"}:
            kwargs["data"] = body_text.encode("utf-8")
        response = self._request(method, f"{AUTH_BASE_URL}{path}", **kwargs)
        # HTTP authority is classified before attempting to interpret an error body.
        if response.status_code >= 400:
            return response, self._error_json_object(response)
        payload = self._safe_json(response)
        if not isinstance(payload, dict):
            raise FranceTvError("France.tv login response has an invalid shape")
        return response, payload

    def _apply_token_response(
        self, token_data: dict[str, Any], *, preserve_refresh: bool = False
    ) -> Session:
        if not isinstance(token_data, dict):
            raise FranceTvError("France.tv token response has an invalid shape")
        previous = self.state
        access_token = _required_text(
            token_data,
            "access_token",
            "France.tv token response is incomplete",
        )

        if "refresh_token" in token_data:
            refresh_token = _required_text(
                token_data,
                "refresh_token",
                "France.tv token response has an invalid refresh token",
            )
        elif preserve_refresh:
            refresh_token = previous.refresh_token
        else:
            refresh_token = ""
        if not refresh_token:
            raise FranceTvError("France.tv token response is incomplete")

        type_key = "type_token" if "type_token" in token_data else "token_type"
        if type_key in token_data:
            type_token = _required_text(
                token_data,
                type_key,
                "France.tv token response has an invalid token type",
            )
        else:
            type_token = previous.type_token
        if not type_token:
            raise FranceTvError("France.tv token response is missing a token type")

        # The official client models ``expire_in`` as a String and stores it
        # verbatim.  It does not use that field to decide whether the access token
        # is fresh: it decodes ``exp`` from the JWT into a separate
        # ``accessExpiration`` column.  Requiring this provider-owned string to be
        # an integer rejected a successfully activated account before it could be
        # cached, then the UI polled the already-consumed code again and again.
        expire_in = ""
        for key in ("expire_in", "expires_in"):
            if key in token_data:
                expire_in = _scalar_text(token_data.get(key))
                break
        if not expire_in and preserve_refresh:
            expire_in = previous.expire_in

        ttl = _as_int(expire_in)
        if ttl is not None and ttl <= 0:
            ttl = None
        expires_at = _token_exp(access_token)
        has_absolute, absolute_expiry = _optional_positive_int(token_data, "expires_at")
        if expires_at is None:
            if has_absolute and absolute_expiry is None:
                raise FranceTvError("France.tv token response has an invalid expiry")
            if absolute_expiry is not None:
                expires_at = absolute_expiry
            elif ttl is not None:
                expires_at = int(time.time()) + ttl
            elif access_token == previous.access_token and previous.expires_at > 0:
                expires_at = previous.expires_at
        if expires_at is None or expires_at <= int(time.time()):
            raise FranceTvError("France.tv token response has no usable expiry")

        user_id = _token_user_id(access_token)
        if not user_id and preserve_refresh:
            user_id = previous.user_id
        if not user_id:
            raise FranceTvError("France.tv token response has no usable account id")

        candidate = Session(
            access_token=access_token,
            refresh_token=refresh_token,
            user_id=user_id,
            expires_at=expires_at,
            expire_in=expire_in,
            type_token=type_token,
            updated_at=int(time.time()),
        )
        self._commit_state(candidate)
        return candidate

    def refresh(self) -> Session:
        if not self.state.refresh_token or not self.state.user_id:
            raise FranceTvAuthRequired("France.tv has no refreshable session")
        path = f"/v3/{AUTH_SOURCE}/user/reconnect/{quote(self.state.user_id, safe='')}"
        response, payload = self._auth_call(
            "GET",
            path,
            refresh_token=self.state.refresh_token,
        )
        error_code = ""
        for key in ("errorCode", "error_code", "details"):
            value = payload.get(key)
            if isinstance(value, str) and re.fullmatch(r"\d{5}", value.strip()):
                error_code = value.strip()
                break
        if response.status_code >= 400:
            deterministic_code = (
                error_code in REFRESH_REJECTED_CODES
                and 400 <= response.status_code < 500
                and response.status_code not in {408, 429}
            )
            error_type = (
                RefreshRejected
                if response.status_code in {401, 403} or deterministic_code
                else FranceTvError
            )
            raise error_type(
                "France.tv session refresh was rejected"
                if error_type is RefreshRejected
                else "France.tv session refresh failed",
                status_code=response.status_code,
            )
        return self._apply_token_response(payload, preserve_refresh=True)

    def start_device_code(self) -> DeviceCodeChallenge:
        code_path = f"/v3/{AUTH_SOURCE}/user/login/code"
        response, initialized = self._auth_call("POST", code_path)
        if response.status_code >= 400:
            raise FranceTvError(
                "France.tv TV-code initialization failed",
                status_code=response.status_code,
            )
        api_code = _required_text(
            initialized,
            "code",
            "France.tv TV-code response is incomplete",
        )
        user_code = _required_text(
            initialized,
            "user_code",
            "France.tv TV-code response is incomplete",
        )
        expires_in = _as_int(initialized.get("expires_in"))
        interval = _as_int(initialized.get("interval"))
        if (
            expires_in is None
            or interval is None
            or expires_in <= 0
            or expires_in > 86400
            or interval <= 0
            or interval > expires_in
        ):
            raise FranceTvError("France.tv TV-code response has invalid polling values")
        return DeviceCodeChallenge(
            api_code=api_code,
            user_code=user_code,
            expires_in=expires_in,
            interval=interval,
            url=ACTIVATION_URL,
            deadline=time.monotonic() + expires_in,
        )

    def poll_device_code(self, challenge: DeviceCodeChallenge) -> Session | None:
        if not math.isfinite(challenge.deadline) or time.monotonic() >= challenge.deadline:
            raise FranceTvError("France.tv TV code expired; start sign-in again")
        status_path = f"/v3/{AUTH_SOURCE}/user/login/status"
        response, payload = self._auth_call(
            "POST",
            status_path,
            {"code": challenge.api_code},
        )
        if _retryable_status(response.status_code):
            challenge.transient_errors += 1
            if challenge.transient_errors <= LOGIN_POLL_MAX_ERRORS:
                return None
            raise FranceTvError(
                "France.tv TV-code polling failed repeatedly",
                status_code=response.status_code,
            )
        challenge.transient_errors = 0
        if response.status_code < 400 and payload.get("access_token"):
            return self._apply_token_response(payload)
        error_code = _scalar_text(payload.get("errorCode") or payload.get("error_code"))
        if response.status_code == 403 and error_code == "35012":
            return None
        raise FranceTvError(
            "France.tv TV-code sign-in failed",
            status_code=response.status_code,
        )

    def ensure_session(self, *, for_item: CatalogItem | None = None) -> Session | None:
        needs = self.require_login or (for_item is not None and for_item.login_required)
        if not needs:
            return self.state if self.state.signed_in else None
        if self.state.is_fresh():
            return self.state
        if self.state.refresh_token and self.state.user_id:
            try:
                return self.refresh()
            except RefreshRejected:
                pass
        raise FranceTvAuthRequired("France.tv TV sign-in is required for this title")

    # --------------------------------------------------------------- catalogue

    def content_details(self, content_id: str) -> CatalogItem:
        value = content_id.strip() if isinstance(content_id, str) else ""
        if not re.fullmatch(r"\d{5,}", value):
            raise FranceTvError("France.tv content id is invalid")
        payload = self._catalog_get(
            f"generic/contents/{value}",
            {"platform": PLATFORM, "podcast": "true"},
        )
        item = catalog_item_from_raw(payload, entry_kind="content")
        if item is None:
            raise FranceTvError("France.tv content response has an invalid shape")
        return item

    def search(self, keyword: str) -> list[CatalogItem]:
        if not isinstance(keyword, str) or not keyword.strip():
            raise FranceTvError("France.tv search term is empty")
        payload = self._catalog_get(
            "generic/search",
            {
                "term": keyword,
                "platform": PLATFORM,
                "filters": SEARCH_FILTERS,
                "podcast": "true",
            },
        )
        keys = ("contents", "programs", "collections")
        if not any(key in payload for key in keys):
            raise FranceTvError("France.tv search response has an invalid shape")
        results: list[CatalogItem] = []
        for key in keys:
            for raw in _list_field(payload, key):
                if not isinstance(raw, dict):
                    raise FranceTvError("France.tv search response has an invalid item")
                item = catalog_item_from_raw(raw)
                if item is None:
                    raise FranceTvError("France.tv search response has an unsupported item")
                results.append(item)
        return results

    def live_channels(self) -> list[CatalogItem]:
        payload = self._catalog_get(
            "generic/hub/directs",
            {"platform": PLATFORM, "podcast": "true"},
        )
        items: list[CatalogItem] = []
        for collection in _list_field(payload, "collections", required=True):
            if not isinstance(collection, dict):
                raise FranceTvError("France.tv live response has an invalid collection")
            collection_label = _text(collection.get("label"))
            for raw in _list_field(collection, "items", required=True):
                if not isinstance(raw, dict) or not raw.get("si_id"):
                    raise FranceTvError("France.tv live response has an invalid item")
                entry = dict(raw)
                entry["_collection_label"] = collection_label
                entry["is_live"] = True
                item = catalog_item_from_raw(entry, entry_kind="content")
                if item is None:
                    raise FranceTvError("France.tv live response has an invalid item")
                items.append(item)
        return items

    def live_by_slug(self, slug: str) -> CatalogItem:
        normalized = _catalogue_route(slug, "France.tv live channel id is invalid").lower()
        if "/" in normalized:
            raise FranceTvError("France.tv live channel id is invalid")
        for item in self.live_channels():
            diffusion = _item_diffusion(item.raw)
            candidates = {
                _text(diffusion.get("channel_path")).lower(),
                _text(diffusion.get("channel_url")).lower(),
                _text(diffusion.get("partner_path")).lower(),
            }
            if normalized in candidates:
                return item
        raise FranceTvError("France.tv live channel is currently unavailable")

    def program_page(self, program_path: str) -> list[CatalogItem]:
        normalized = _catalogue_route(program_path, "France.tv program path is invalid")
        page = self._catalog_get(
            f"{PROGRAM_SOURCE}/program/{quote(normalized, safe='-_')}",
            {
                "platform": PLATFORM,
                "filters": "only-replay",
                "sort": "episode:desc",
                "size": 150,
            },
        )
        return self._program_items(page, normalized)

    def collection_page(self, collection_path: str) -> list[CatalogItem]:
        normalized = _catalogue_route(collection_path, "France.tv collection path is invalid")
        page = self._catalog_get(
            f"apps/collections/{quote(normalized, safe='-_')}",
            {"page": 0, "size": 150, "platform": PLATFORM},
        )
        return self._page_items(page)

    def event_page(self, event_path: str) -> list[CatalogItem]:
        normalized = _catalogue_route(event_path, "France.tv event path is invalid")
        page = self._catalog_get(
            f"apps/events/{quote(normalized, safe='-_')}",
            {"platform": PLATFORM, "podcast": "true"},
        )
        return self._page_items(page)

    def resolve_taxonomy(self, taxonomy_id: str) -> CatalogItem:
        normalized = _catalogue_route(taxonomy_id, "France.tv taxonomy id is invalid")
        resolved = self._catalog_get(
            f"generic/taxonomy/{quote(normalized, safe='-_')}",
            {"platform": PLATFORM, "podcast": "true"},
        )
        item = catalog_item_from_raw(resolved)
        if item is None or item.kind == "article":
            raise FranceTvError("France.tv page type is not supported")
        return item

    def _page_items(self, page: dict[str, Any]) -> list[CatalogItem]:
        if not isinstance(page, dict):
            raise FranceTvError("France.tv page response has an invalid shape")
        known = ("collections", "contents", "items", "videos")
        if not any(key in page for key in known):
            raise FranceTvError("France.tv page response has an invalid shape")
        raw_items: list[Any] = []
        for collection in _list_field(page, "collections"):
            if not isinstance(collection, dict):
                raise FranceTvError("France.tv page response has an invalid collection")
            raw_items.extend(_list_field(collection, "items", required=True))
        for key in ("contents", "items", "videos"):
            raw_items.extend(_list_field(page, key))

        items: list[CatalogItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise FranceTvError("France.tv page response has an invalid item")
            item = catalog_item_from_raw(raw)
            if item is None:
                raise FranceTvError("France.tv page response has an unsupported item")
            items.append(item)

        seen: set[str] = set()
        unique: list[CatalogItem] = []
        for item in items:
            key = item.id or item.si_id or item.program_path or item.title
            if not key:
                raise FranceTvError("France.tv page response has an unidentified item")
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    def _program_items(
        self, page: dict[str, Any], program_path: str
    ) -> list[CatalogItem]:
        """Return the selected programme's videos, not its recommendation shelves.

        A Yatta programme response is a whole screen.  Alongside seasons and extras
        belonging to the programme it carries shelves such as ``À découvrir aussi``
        and ``Des mini-séries``.  Flattening every collection turns an exact URL into
        dozens of unrelated search-like results and can even put the programme itself
        back in the list, which opens the same screen recursively.

        Playable rows identify their owner in ``program.program_path``.  Keep that
        exact owner, tolerate an occasional owner-less bonus in a programme-owned
        shelf, and never treat navigation cards or the discovery shelf as episodes.
        """
        if not isinstance(page, dict) or "collections" not in page:
            raise FranceTvError("France.tv program page response has an invalid shape")

        root = page.get("item")
        root_id = _scalar_text(root.get("id")) if isinstance(root, dict) else ""
        raw_items: list[dict[str, Any]] = []
        for collection in _list_field(page, "collections", required=True):
            if not isinstance(collection, dict):
                raise FranceTvError("France.tv program page has an invalid collection")
            if _text(collection.get("type")).lower() == "playlist_to_discover":
                continue
            for raw in _list_field(collection, "items", required=True):
                if not isinstance(raw, dict):
                    raise FranceTvError("France.tv program page has an invalid item")
                # Programme and collection cards navigate elsewhere; only rows with
                # both identifiers can resolve to a video.
                if not (raw.get("id") and raw.get("si_id")):
                    continue
                owner = raw.get("program")
                if owner is not None and not isinstance(owner, dict):
                    raise FranceTvError("France.tv program page has invalid ownership metadata")
                if isinstance(owner, dict):
                    owner_path = _text(owner.get("program_path"))
                    owner_id = _scalar_text(owner.get("id"))
                    if owner_path and owner_path != program_path:
                        continue
                    if not owner_path and root_id and owner_id and owner_id != root_id:
                        continue
                raw_items.append(raw)

        items: list[CatalogItem] = []
        seen: set[str] = set()
        for raw in raw_items:
            item = catalog_item_from_raw(raw, entry_kind="content")
            if item is None:
                raise FranceTvError("France.tv program page has an unsupported video")
            key = item.id or item.si_id
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
        return items

    # ---------------------------------------------------------------- playback

    def geo_info(self) -> tuple[str, str]:
        if self._geo is not None:
            return self._geo
        response = self._request("GET", GEO_URL, headers={"User-Agent": USER_AGENT})
        if response.status_code >= 400:
            raise FranceTvError(
                "France.tv geolocation request failed",
                status_code=response.status_code,
            )
        payload = self._safe_json(response)
        if not isinstance(payload, dict):
            raise FranceTvError("France.tv geolocation response has an invalid shape")
        response_data = payload.get("reponse")
        if not isinstance(response_data, dict):
            raise FranceTvError("France.tv geolocation response is incomplete")
        geo = response_data.get("geo_info")
        if not isinstance(geo, dict):
            raise FranceTvError("France.tv geolocation response is incomplete")
        country = _text(geo.get("country_code"))
        timezone = _text(geo.get("timezone"))
        if not country or not timezone:
            raise FranceTvError("France.tv geolocation response is incomplete")
        self._geo = (country, timezone)
        return self._geo

    def _playable_item(self, item: CatalogItem) -> dict[str, Any]:
        raw = dict(item.raw)
        if not item.is_live:
            return raw
        diffusion = _item_diffusion(raw)
        diffusion_si_id = _text(diffusion.get("si_id"))
        if not diffusion_si_id:
            return raw
        current = _text(raw.get("si_id"))
        if diffusion_si_id != current:
            raw["si_id"] = diffusion_si_id
        return raw

    def k7_playback(self, item: CatalogItem) -> dict[str, Any]:
        playable = self._playable_item(item)
        content_id = _scalar_text(playable.get("id") or item.id)
        si_id = _media_id(playable.get("si_id") or item.si_id)
        if not re.fullmatch(r"\d{5,}", content_id):
            raise FranceTvError("Selected content has invalid playback metadata")
        country, timezone = self.geo_info()
        params = {
            "content_id": content_id,
            "connection_type": "wifi",
            "capability": K7_CAPABILITIES,
            "player_version": PLAYER_VERSION,
            "app_version": APP_VERSION,
            "country_code": country,
            "os": "androidtv",
            "os_version": ANDROID_API_LEVEL,
            "device": DEVICE_NAME,
            "device_type": "tv",
            "screen_w": SCREEN_WIDTH,
            "package_name": PACKAGE_NAME,
            "diffusion_mode": "single",
            "support4k": str(SUPPORT_4K).lower(),
            "offline": "false",
            "gmt": timezone,
        }
        response = self._request(
            "GET",
            urljoin(K7_BASE_URL, f"videos/{quote(si_id, safe='-')}") ,
            params=params,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        if response.status_code >= 400:
            raise FranceTvError(
                "France.tv playback metadata request failed",
                status_code=response.status_code,
            )
        payload = self._safe_json(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("video"), dict):
            raise FranceTvError("France.tv playback metadata has an invalid shape")
        if item.is_live and payload["video"].get("is_live") is not True:
            raise FranceTvError("France.tv live playback metadata is inconsistent")
        return payload

    def _token_url(self, video: dict[str, Any], kind: str) -> str:
        token = video.get("token")
        if token is None:
            return ""
        if isinstance(token, str):
            return token.strip() if kind == "akamai" else ""
        if not isinstance(token, dict):
            raise FranceTvError("France.tv playback token metadata has an invalid shape")
        value = token.get(kind)
        if value is None:
            return ""
        if not isinstance(value, str):
            raise FranceTvError("France.tv playback token metadata has an invalid shape")
        return value.strip()

    def _tokenize_manifest(self, video: dict[str, Any]) -> str:
        video_url = _validated_https_url(
            video.get("url"),
            "France.tv playback metadata has an invalid media endpoint",
        )
        token_url = self._token_url(video, "akamai")
        workflow_value = video.get("workflow")
        if workflow_value is not None and not isinstance(workflow_value, str):
            raise FranceTvError("France.tv playback workflow has an invalid shape")
        workflow = workflow_value or ""
        if not token_url:
            if workflow.startswith("token"):
                raise FranceTvError("France.tv playback metadata is missing a media-token endpoint")
            return video_url
        token_url = _validated_https_url(
            token_url,
            "France.tv playback metadata has an invalid media-token endpoint",
        )
        response = self._request(
            "GET",
            token_url,
            params={"url": video_url},
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        if response.status_code >= 400:
            raise FranceTvError(
                "France.tv media-token request failed",
                status_code=response.status_code,
            )
        payload = self._safe_json(response)
        if not isinstance(payload, dict):
            raise FranceTvError("France.tv media-token response has an invalid shape")
        return _validated_https_url(
            payload.get("url"),
            "France.tv media-token response has an invalid media endpoint",
        )

    def _drm_settings(self, video: dict[str, Any]) -> dict[str, Any] | None:
        drm = video.get("drm")
        if drm is None or drm is False:
            return None
        if isinstance(drm, dict):
            active = drm.get("active")
            if active is False:
                return None
            if active is not True:
                raise FranceTvError("France.tv DRM metadata has an invalid shape")
            return drm
        if drm is True:
            return {
                "active": True,
                "drm_type": video.get("drm_type"),
                "license_type": video.get("license_type"),
                "laUrl": video.get("laUrl") or video.get("la_url"),
                "account_id": video.get("account_id"),
            }
        raise FranceTvError("France.tv DRM metadata has an invalid shape")

    def _drm_token(
        self, playback: dict[str, Any], video: dict[str, Any], drm: dict[str, Any]
    ) -> str:
        token_url = _validated_https_url(
            self._token_url(video, "drm"),
            "France.tv DRM metadata is missing a valid token endpoint",
        )
        meta = playback.get("meta")
        if meta is not None and not isinstance(meta, dict):
            raise FranceTvError("France.tv DRM metadata has an invalid shape")
        video_id = _scalar_text(playback.get("id")) or _scalar_text((meta or {}).get("id"))
        if not video_id:
            raise FranceTvError("France.tv DRM metadata is missing a media id")
        drm_type = _required_text(
            drm,
            "drm_type",
            "France.tv DRM metadata has an invalid shape",
        )
        license_type = _required_text(
            drm,
            "license_type",
            "France.tv DRM metadata has an invalid shape",
        )
        account_id = _required_text(
            drm,
            "account_id",
            "France.tv DRM metadata has an invalid shape",
        )
        body = {
            "id": video_id,
            "drm_type": drm_type,
            "license_type": license_type,
            "account_id": account_id,
        }
        for attempt in range(2):
            try:
                response = self._request(
                    "POST",
                    token_url,
                    json=body,
                    headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                )
            except FranceTvNetworkError:
                if attempt == 0:
                    continue
                raise
            if _retryable_status(response.status_code):
                if attempt == 0:
                    continue
                raise FranceTvError(
                    "France.tv DRM-token request failed",
                    status_code=response.status_code,
                )
            if response.status_code >= 400:
                raise FranceTvError(
                    "France.tv DRM-token request was rejected",
                    status_code=response.status_code,
                )
            payload = self._safe_json(response)
            if not isinstance(payload, dict):
                raise FranceTvError("France.tv DRM-token response has an invalid shape")
            token = payload.get("token")
            if not isinstance(token, str) or not token.strip():
                raise FranceTvError("France.tv DRM-token response is incomplete")
            return token.strip()
        raise FranceTvError("France.tv DRM-token request failed")

    def resolve(self, item: CatalogItem) -> Source:
        self.ensure_session(for_item=item)
        playback = self.k7_playback(item)
        video = playback.get("video")
        if not isinstance(video, dict):
            raise FranceTvError("France.tv playback metadata has an invalid video object")
        manifest = self._tokenize_manifest(video)
        is_live = item.is_live or video.get("is_live") is True
        format_value = video.get("format")
        if format_value is not None and not isinstance(format_value, str):
            raise FranceTvError("France.tv playback format has an invalid shape")
        stream_type = format_value or ""
        if not stream_type:
            path = urlparse(manifest).path.lower()
            stream_type = (
                "dash" if path.endswith(".mpd") else "hls" if ".m3u8" in path else "unknown"
            )
        drm = self._drm_settings(video)
        license_url = ""
        drm_token = ""
        encrypted = False
        if drm is not None:
            encrypted = True
            license_url = _validated_https_url(
                drm.get("laUrl") or drm.get("la_url"),
                "France.tv DRM metadata is missing a valid license endpoint",
            )
            drm_token = self._drm_token(playback, video, drm)
        playable = self._playable_item(item)
        return Source(
            content_id=_scalar_text(playable.get("id") or item.id),
            media_id=_scalar_text(playable.get("si_id") or item.si_id),
            manifest=manifest,
            title=item.title,
            program_title=item.program_title,
            season=item.season,
            episode=item.episode,
            year=item.year,
            is_live=is_live,
            encrypted=encrypted,
            license_url=license_url,
            drm_token=drm_token,
            format=stream_type,
            note="widevine" if encrypted else "clear",
        )

    def widevine_license(self, challenge: bytes, *, license_url: str, drm_token: str) -> bytes:
        safe_license_url = _validated_https_url(
            license_url,
            "France.tv Widevine license endpoint is invalid",
        )
        if not isinstance(drm_token, str) or not drm_token.strip():
            raise FranceTvError("France.tv Widevine authorization is missing")
        if not isinstance(challenge, bytes) or not challenge:
            raise FranceTvError("France.tv Widevine challenge is empty")
        response: requests.Response | None = None
        for attempt in range(2):
            try:
                response = self._request(
                    "POST",
                    safe_license_url,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "User-Agent": USER_AGENT,
                        "nv-authorizations": drm_token,
                    },
                    data=challenge,
                )
            except FranceTvNetworkError:
                if attempt == 0:
                    continue
                raise
            if _retryable_status(response.status_code):
                if attempt == 0:
                    continue
                raise FranceTvError(
                    "France.tv Widevine license request failed",
                    status_code=response.status_code,
                )
            break
        if response is None:
            raise FranceTvError("France.tv Widevine license request failed")
        if response.status_code >= 400:
            raise FranceTvError(
                "France.tv Widevine license request was rejected",
                status_code=response.status_code,
            )
        content_type = _scalar_text(response.headers.get("content-type")).lower()
        if "json" not in content_type:
            body = response.content or b""
            if not body:
                raise FranceTvError("France.tv Widevine license response is empty")
            return body
        payload = self._safe_json(response)
        if not isinstance(payload, dict):
            raise FranceTvError("France.tv Widevine response has an invalid shape")
        license_value = payload.get("license")
        if not isinstance(license_value, str) or not license_value:
            raise FranceTvError("France.tv Widevine response is incomplete")
        try:
            decoded = base64.b64decode(license_value, validate=True)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise FranceTvError("France.tv Widevine license encoding is invalid") from exc
        if not decoded:
            raise FranceTvError("France.tv Widevine license response is empty")
        return decoded


__all__ = [
    "ACTIVATION_URL",
    "CatalogItem",
    "DeviceCodeChallenge",
    "FranceTvApi",
    "FranceTvAuthRequired",
    "FranceTvError",
    "RefreshRejected",
    "Session",
    "Source",
    "TOKEN_FILE",
    "USER_AGENT",
    "catalog_item_from_raw",
    "parse_input",
]
