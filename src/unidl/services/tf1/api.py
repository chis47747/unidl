"""TF1+ Android TV (fr.tf1.mytf1) API.

Protocol from TF1+ Android TV 11.41.2: OAuth (anonymous smart_tv or device-code),
GraphQL smarttv catalog, mediainfocombo delivery, Widevine DRM proxy.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests

APP_VERSION = "11.41.2"
APP_PACKAGE = "fr.tf1.mytf1"
GRAPHQL_STREAM = "smarttv"
GRAPHQL_BASE = "https://app-api.tf1.fr/graphql/"
TOKEN_BASE = "https://www.tf1.fr"
MEDIAINFO_BASE = "https://mediainfo.tf1.fr"
DRM_PROXY_DEFAULT = "https://drm-wide.tf1.fr/proxy"
DEVICE_CODE_PATH = "/token/device/code"
OAUTH2_PATH = "/token/oauth2"
REFRESH_PATH = "/token/refresh"

GRANT_ANONYMOUS = "smart_tv_anonymous"
GRANT_DEVICE_CODE = "urn:ietf:params:oauth:grant-type:device_code"

CONSENT_IDS = [
    "4",
    "10001",
    "10003",
    "10005",
    "10007",
    "10009",
    "10011",
    "10013",
    "10015",
    "10017",
    "10019",
]

GQL_SEARCH_PROGRAMS = "e78b188"
GQL_SEARCH_VIDEOS = "b2dc9439"
GQL_PROGRAMS = "483ce0f"
GQL_PROGRAM_VIDEOS = "a6f9cf0e"
GQL_PROGRAM_SECTIONS = "379fec96081ab1a2"
GQL_LIST_BY_ID = "76bc5c35edb166fa637a6c6686cb05c7140d2a8b"
GQL_VIDEO_BY_SLUG = "9b80783950b85247541dd1d851f9cc7fa36574af015621f853ab111a679ce26f"

LIVE_CHANNELS = [
    {"id": "L_TF1", "slug": "tf1", "title": "TF1"},
    {"id": "L_TMC", "slug": "tmc", "title": "TMC"},
    {"id": "L_TFX", "slug": "tfx", "title": "TFX"},
    {"id": "L_TF1-SERIES-FILMS", "slug": "tf1-series-films", "title": "TF1 Series Films"},
    {"id": "L_LCI", "slug": "lci", "title": "LCI"},
]

PLAYER_VERSION = "5.30.0"
PRODUCT_NAME = "mytf1"
MEDIAINFO_CONTEXT = "mytf1"
MEDIAINFO_FORMAT = "dash"
MEDIAINFO_PVER = "5015000"

TOKEN_MIN_TTL_SECONDS = 120
DEVICE_POLL_SECONDS = 10
DEVICE_TIMEOUT_SECONDS = 10 * 60
REQUEST_TIMEOUT = 30

TOKEN_FILE = "tf1_token.json"
CLIENT_USER_AGENT = f"TF1+/{APP_VERSION} (Linux; Android 12; TV) {APP_PACKAGE}"
PLAYER_USER_AGENT = f"TF1+/{APP_VERSION} AndroidTV player/{PLAYER_VERSION}"
USER_AGENT = CLIENT_USER_AGENT
ACTIVATION_URL = "https://www.tf1.fr/tv"


class Tf1Error(RuntimeError):
    def __init__(self, message: str, payload: Any = None):
        super().__init__(message)
        self.payload = payload


class Tf1AuthRequired(Tf1Error):
    pass


# --------------------------------------------------------------------------- models


@dataclass
class Session:
    access_token: str = ""
    refresh_token: str = ""
    device_id: str = ""
    login_method: str = "anonymous"
    expires_at: int = 0
    expires_in: Any = None
    token_type: str = "bearer"
    right: Any = None
    issuer: str = ""
    platform: str = ""
    updated_at: int = 0
    didomi: Any = None

    @classmethod
    def from_cache(cls, raw: Any) -> Session:
        if not isinstance(raw, dict):
            state = cls()
            state.device_id = str(uuid.uuid4())
            return state
        return cls(
            access_token=str(raw.get("access_token") or ""),
            refresh_token=str(raw.get("refresh_token") or ""),
            device_id=str(raw.get("device_id") or uuid.uuid4()),
            login_method=str(raw.get("login_method") or "anonymous"),
            expires_at=_as_int(raw.get("expires_at")) or 0,
            expires_in=raw.get("expires_in"),
            token_type=str(raw.get("token_type") or "bearer"),
            right=raw.get("right"),
            issuer=str(raw.get("issuer") or ""),
            platform=str(raw.get("platform") or ""),
            updated_at=_as_int(raw.get("updated_at")) or 0,
            didomi=raw.get("didomi"),
        )

    def to_cache(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": 1,
            "access_token": self.access_token,
            "device_id": self.device_id or str(uuid.uuid4()),
            "login_method": self.login_method,
            "expires_at": self.expires_at,
            "expires_in": self.expires_in,
            "token_type": self.token_type,
            "right": self.right,
            "issuer": self.issuer,
            "platform": self.platform,
            "updated_at": self.updated_at or int(time.time()),
        }
        if self.refresh_token:
            out["refresh_token"] = self.refresh_token
        if self.didomi is not None:
            out["didomi"] = self.didomi
        return out

    def is_fresh(self, min_ttl: int = TOKEN_MIN_TTL_SECONDS) -> bool:
        if not self.access_token:
            return False
        exp = self.expires_at or _jwt_exp(self.access_token) or 0
        return exp > int(time.time()) + min_ttl

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token)


@dataclass(frozen=True)
class CatalogItem:
    id: str
    title: str
    kind: str = "Video"
    slug: str = ""
    program_slug: str = ""
    program_name: str = ""
    season: int | None = None
    episode: int | None = None
    year: str = ""
    is_live: bool = False
    detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def label(self) -> str:
        return self.title or self.slug or self.id or "Untitled"


@dataclass(frozen=True)
class Source:
    media_id: str
    manifest: str
    title: str
    series_title: str = ""
    season: int | None = None
    episode: int | None = None
    year: str = ""
    is_live: bool = False
    encrypted: bool = False
    license_url: str = ""
    license_headers: dict[str, str] = field(default_factory=dict)
    format: str = ""
    note: str = ""


@dataclass
class DeviceCodeChallenge:
    device_code: str
    user_code: str
    verification_url: str
    verification_complete: str = ""
    interval: int = DEVICE_POLL_SECONDS
    expires_in: int = DEVICE_TIMEOUT_SECONDS
    deadline: float = 0.0


# --------------------------------------------------------------------------- helpers


def _as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any, default: str = "") -> str:
    return str(value if value is not None else default).strip()


def _jwt_payload(token: str | None) -> dict[str, Any]:
    try:
        part = str(token or "").split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (IndexError, ValueError, TypeError, binascii.Error, json.JSONDecodeError):
        return {}


def _jwt_exp(token: str | None) -> int | None:
    value = _jwt_payload(token).get("exp")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_year(value: Any) -> str:
    year = _text(value)
    return year if re.fullmatch(r"(?:18|19|20|21)\d{2}", year) else ""


def _season_number_from_label(label: str) -> int | None:
    match = re.search(r"(?:saison|season|s)\s*([0-9]{1,3})", label or "", re.I)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _episode_number_from_text(*values: Any) -> int | None:
    for value in values:
        text = str(value or "")
        if not text:
            continue
        patterns = [
            r"(?:S\d{1,2}E|E|EP|épisode|episode|quotidienne|uncut|prime|émission|emission)\s*0*(\d{1,3})\b",
            r"\b0*(\d{1,3})\s*(?:/|-)?\s*(?:du|de)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                try:
                    number = int(match.group(1))
                except ValueError:
                    continue
                if 1 <= number <= 999:
                    return number
    return None


def _item_title(item: dict[str, Any]) -> str:
    decoration = item.get("decoration") or {}
    if isinstance(decoration, dict):
        for key in ("label", "programLabel", "shortLabel"):
            if decoration.get(key):
                return str(decoration[key])
    return str(
        item.get("title")
        or item.get("name")
        or item.get("extendedTitle")
        or item.get("slug")
        or item.get("id")
        or "Untitled"
    )


def normalize_video_item(
    video: dict[str, Any],
    *,
    program_slug: str = "",
    program_name: str = "",
    season_label: str = "",
    fallback_episode: int | None = None,
) -> CatalogItem:
    decoration = video.get("decoration") if isinstance(video.get("decoration"), dict) else {}
    program = video.get("program") if isinstance(video.get("program"), dict) else {}
    season = _as_int(video.get("season"))
    episode = _as_int(video.get("episode"))
    if season is None and season_label:
        season = _season_number_from_label(season_label)
    if episode is None:
        episode = _episode_number_from_text(
            decoration.get("label"),
            decoration.get("shortLabel"),
            video.get("slug"),
            video.get("title"),
        )
        if episode is None:
            episode = fallback_episode
    year = _normalize_year(
        program.get("releaseYear") or video.get("releaseYear") or video.get("year")
    )
    return CatalogItem(
        id=str(video.get("id") or ""),
        title=_item_title(video),
        kind=str(video.get("type") or video.get("__typename") or "Video"),
        slug=str(video.get("slug") or ""),
        program_slug=program_slug or str(program.get("slug") or video.get("programSlug") or ""),
        program_name=program_name
        or str(
            program.get("name")
            or decoration.get("programLabel")
            or video.get("programName")
            or ""
        ),
        season=season,
        episode=episode,
        year=year,
        is_live=str(video.get("type") or "").lower() == "live",
        detail=_text(decoration.get("description") or video.get("description")),
        raw=dict(video),
    )


def parse_input(value: str) -> dict[str, str] | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"L_[A-Za-z0-9_-]+", raw):
        return {"id": raw, "type": "Live"}
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        raw,
        re.I,
    ):
        return {"id": raw, "type": "Video"}
    if re.fullmatch(r"\d{6,}", raw):
        return {"id": raw, "type": "Video"}

    request_url = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(request_url)
    if parsed.scheme not in {"http", "https"}:
        return None
    hostname = (parsed.hostname or "").lower()
    if hostname not in {
        "www.tf1.fr",
        "tf1.fr",
        "www.tf1info.fr",
        "tf1info.fr",
        "lci.fr",
        "www.lci.fr",
    }:
        if "tf1" not in hostname and "lci" not in hostname:
            return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 4 and path_parts[2].lower() == "videos":
        program_slug = path_parts[1]
        video_slug = path_parts[3]
        if video_slug.endswith(".html"):
            video_slug = video_slug[: -len(".html")]
        return {
            "id": video_slug,
            "type": "VideoSlug",
            "programSlug": program_slug,
            "channel": path_parts[0],
        }
    if len(path_parts) >= 3 and path_parts[1].lower() == "videos":
        program_slug = path_parts[0]
        video_slug = path_parts[2]
        if video_slug.endswith(".html"):
            video_slug = video_slug[: -len(".html")]
        return {"id": video_slug, "type": "VideoSlug", "programSlug": program_slug}
    if path_parts and path_parts[-1].lower() == "direct":
        channel = path_parts[0] if path_parts[0].lower() != "direct" else "tf1"
        if channel.lower() in {"www", "fr"}:
            channel = "tf1"
        return {"id": channel, "type": "LiveSlug"}
    if len(path_parts) >= 2 and path_parts[0].lower() not in {"programmes-tv", "replay"}:
        return {"id": path_parts[1], "type": "ProgramSlug", "channel": path_parts[0]}
    if len(path_parts) == 1:
        return {"id": path_parts[0], "type": "ProgramSlug"}

    query = parse_qs(parsed.query)
    for key in ("id", "videoId", "contentId", "mediaId"):
        candidate = str((query.get(key) or [""])[0])
        if candidate:
            return {"id": candidate, "type": "Video"}
    return None


def normalize_login_method(value: str) -> str:
    text = str(value or "anonymous").strip().lower().replace(" ", "_")
    if text in {"tv", "tv_login", "device", "account", "login"}:
        return "tv"
    return "anonymous"


# --------------------------------------------------------------------------- client


class Tf1Api:
    def __init__(
        self,
        session: requests.Session,
        *,
        state: Session | None = None,
        on_save: Callable[[Session], None] | None = None,
        login_method: str = "anonymous",
    ):
        self.session = session
        self.state = state or Session(device_id=str(uuid.uuid4()))
        if not self.state.device_id:
            self.state.device_id = str(uuid.uuid4())
        self.on_save = on_save
        self.login_method = normalize_login_method(login_method)

    def _save(self) -> None:
        if self.on_save is not None:
            self.on_save(self.state)

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        try:
            return self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise Tf1Error(f"Network request failed: {exc}") from exc

    def _safe_json(self, response: requests.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise Tf1Error(
                f"Non-JSON response (HTTP {response.status_code})",
                response.text[:500],
            ) from exc
        if not isinstance(value, dict):
            raise Tf1Error("JSON response is not an object", value)
        return value

    def _common_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": CLIENT_USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://www.tf1.fr",
            "Referer": "https://www.tf1.fr/",
        }
        if self.state.access_token:
            headers["Authorization"] = f"Bearer {self.state.access_token}"
        return headers

    def _token_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "POST",
            urljoin(TOKEN_BASE + "/", path.lstrip("/")),
            headers={
                "User-Agent": CLIENT_USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://www.tf1.fr",
                "Referer": "https://www.tf1.fr/",
            },
            json=payload,
        )
        data = self._safe_json(response)
        if response.status_code >= 400:
            message = (
                data.get("error")
                or data.get("message")
                or data.get("error_description")
                or "request failed"
            )
            raise Tf1Error(f"TF1 token HTTP {response.status_code}: {message}", data)
        return data

    def _apply_token_response(self, token_data: dict[str, Any], login_method: str) -> Session:
        access_token = str(token_data.get("access_token") or token_data.get("token") or "")
        if not access_token:
            raise Tf1Error("TF1 auth response did not include access_token", token_data)
        self.state.access_token = access_token
        refresh_token = str(token_data.get("refresh_token") or self.state.refresh_token or "")
        if refresh_token:
            self.state.refresh_token = refresh_token
        elif login_method == "anonymous":
            self.state.refresh_token = ""
        expires_at = _jwt_exp(access_token)
        if expires_at is None:
            try:
                expires_at = int(time.time()) + int(token_data.get("expires_in") or 0)
            except (TypeError, ValueError):
                expires_at = 0
        self.state.expires_at = expires_at
        self.state.expires_in = token_data.get("expires_in")
        self.state.token_type = str(token_data.get("token_type") or "bearer")
        self.state.right = token_data.get("right")
        self.state.login_method = login_method
        payload = _jwt_payload(access_token)
        self.state.issuer = str(payload.get("iss") or "")
        self.state.platform = str(payload.get("plt") or "")
        self.state.updated_at = int(time.time())
        if token_data.get("didomi") is not None:
            self.state.didomi = token_data.get("didomi")
        if not self.state.device_id:
            self.state.device_id = str(uuid.uuid4())
        self._save()
        return self.state

    def login_anonymous(self) -> Session:
        token_data = self._token_post(
            OAUTH2_PATH,
            {"grant_type": GRANT_ANONYMOUS, "consent_ids": CONSENT_IDS},
        )
        return self._apply_token_response(token_data, "anonymous")

    def start_device_code(self) -> DeviceCodeChallenge:
        activated = self._token_post(DEVICE_CODE_PATH, {})
        device_code = str(activated.get("device_code") or "")
        user_code = str(activated.get("user_code") or "")
        verification_url = str(activated.get("verification_uri") or ACTIVATION_URL)
        verification_complete = str(activated.get("verification_uri_complete") or "")
        interval = int(activated.get("interval") or DEVICE_POLL_SECONDS)
        expires_in = int(activated.get("expires_in") or DEVICE_TIMEOUT_SECONDS)
        if not device_code or not user_code:
            raise Tf1Error("TV activation returned no device_code or user_code", activated)
        return DeviceCodeChallenge(
            device_code=device_code,
            user_code=user_code,
            verification_url=verification_url,
            verification_complete=verification_complete,
            interval=max(interval, 3),
            expires_in=expires_in,
            deadline=time.monotonic() + max(expires_in, 60),
        )

    def poll_device_code(self, challenge: DeviceCodeChallenge) -> Session | None:
        if time.monotonic() >= challenge.deadline:
            raise Tf1Error("TV activation timed out; start login again")
        try:
            token_data = self._token_post(
                OAUTH2_PATH,
                {
                    "grant_type": GRANT_DEVICE_CODE,
                    "device_code": challenge.device_code,
                    "consent_ids": CONSENT_IDS,
                },
            )
        except Tf1Error as exc:
            payload = exc.payload if isinstance(exc.payload, dict) else {}
            error = str(payload.get("error") or "")
            if error in {"authorization_pending", "slow_down"}:
                return None
            if error in {"expired_token", "access_denied"}:
                raise Tf1Error(f"TV activation ended with {error}", payload) from exc
            if "pending" in error or "authorization_pending" in str(exc).lower():
                return None
            raise
        return self._apply_token_response(token_data, "tv")

    def refresh(self) -> Session | None:
        if self.state.refresh_token:
            try:
                token_data = self._token_post(
                    REFRESH_PATH,
                    {
                        "refresh_token": self.state.refresh_token,
                        "consent_ids": CONSENT_IDS,
                    },
                )
                return self._apply_token_response(
                    token_data, self.state.login_method or "tv"
                )
            except Tf1Error:
                return None
        if self.state.login_method == "anonymous" or self.login_method == "anonymous":
            try:
                return self.login_anonymous()
            except Tf1Error:
                return None
        return None

    def ensure_auth(self) -> Session:
        if self.state.is_fresh():
            return self.state
        if self.refresh():
            return self.state
        if self.login_method == "anonymous" or self.state.login_method == "anonymous":
            return self.login_anonymous()
        raise Tf1AuthRequired(
            f"TF1 is not signed in. Open Sign in and enter the code at {ACTIVATION_URL}."
        )

    def _graphql(
        self, query_id: str, variables: dict[str, Any] | None = None, *, retry: bool = True
    ) -> dict[str, Any]:
        self.ensure_auth()
        response = self._request(
            "GET",
            urljoin(GRAPHQL_BASE, GRAPHQL_STREAM),
            headers=self._common_headers(),
            params={
                "id": query_id,
                "variables": json.dumps(variables or {}, ensure_ascii=False, separators=(",", ":")),
            },
        )
        data = self._safe_json(response)
        unauthorized = response.status_code == 401 or any(
            "auth" in str(error.get("message") or "").lower()
            or str((error.get("extensions") or {}).get("code") or "").upper()
            in {"UNAUTHENTICATED", "UNAUTHORIZED"}
            for error in (data.get("errors") or [])
            if isinstance(error, dict)
        )
        if unauthorized and retry and self.refresh():
            return self._graphql(query_id, variables, retry=False)
        if response.status_code >= 400:
            raise Tf1Error(f"GraphQL HTTP {response.status_code}", data)
        if data.get("errors") and not data.get("data"):
            first = data["errors"][0] if isinstance(data["errors"][0], dict) else {}
            raise Tf1Error(first.get("message") or "GraphQL request failed", data.get("errors"))
        result = data.get("data")
        return result if isinstance(result, dict) else {}

    # --------------------------------------------------------------- catalogue

    def search(self, keyword: str) -> list[CatalogItem]:
        programs = self._graphql(
            GQL_SEARCH_PROGRAMS,
            {"query": keyword, "offset": 0, "limit": 40},
        ).get("searchPrograms") or {}
        videos = self._graphql(
            GQL_SEARCH_VIDEOS,
            {"query": keyword, "offset": 0, "limit": 40},
        ).get("searchVideos") or {}
        items: list[CatalogItem] = []
        for program in programs.get("items") or []:
            if not isinstance(program, dict):
                continue
            items.append(
                CatalogItem(
                    id=str(program.get("id") or ""),
                    title=_item_title(program),
                    kind="Program",
                    slug=str(program.get("slug") or ""),
                    program_slug=str(program.get("slug") or ""),
                    program_name=_item_title(program),
                    year=_normalize_year(program.get("releaseYear")),
                    detail=_text(program.get("description")),
                    raw=dict(program),
                )
            )
        for video in videos.get("items") or []:
            if not isinstance(video, dict):
                continue
            program = video.get("program") if isinstance(video.get("program"), dict) else {}
            decoration = (
                video.get("decoration") if isinstance(video.get("decoration"), dict) else {}
            )
            items.append(
                normalize_video_item(
                    video,
                    program_slug=str(program.get("slug") or ""),
                    program_name=str(
                        program.get("name") or decoration.get("programLabel") or ""
                    ),
                )
            )
        return items

    def program_videos(
        self, program_slug: str, video_type: str = "REPLAY"
    ) -> list[CatalogItem]:
        data = self._graphql(
            GQL_PROGRAM_VIDEOS,
            {
                "programSlug": program_slug,
                "offset": 0,
                "limit": 40,
                "sort": {"type": "DATE", "order": "DESC"},
                "types": [video_type],
            },
        )
        program = data.get("programBySlug") or {}
        program_name = _item_title(program) if isinstance(program, dict) else program_slug
        videos = []
        if isinstance(program, dict):
            videos = (program.get("videos") or {}).get("items") or program.get("items") or []
        items: list[CatalogItem] = []
        for index, video in enumerate(videos or []):
            if not isinstance(video, dict):
                continue
            items.append(
                normalize_video_item(
                    video,
                    program_slug=program_slug,
                    program_name=program_name,
                    fallback_episode=index + 1,
                )
            )
        return items

    def program_by_slug(self, slug: str) -> CatalogItem:
        data = self._graphql(GQL_PROGRAMS, {"slug": slug})
        # fallbacks: some builds use programBySlug only via videos query
        program = data.get("programBySlug") or data.get("program") or {}
        if not program:
            # probe via videos query for metadata
            pdata = self._graphql(
                GQL_PROGRAM_VIDEOS,
                {
                    "programSlug": slug,
                    "offset": 0,
                    "limit": 1,
                    "sort": {"type": "DATE", "order": "DESC"},
                    "types": ["REPLAY"],
                },
            )
            program = pdata.get("programBySlug") or {}
        if not isinstance(program, dict) or not (program.get("id") or program.get("slug") or slug):
            raise Tf1Error(f"Program not found: {slug}", data)
        return CatalogItem(
            id=str(program.get("id") or ""),
            title=_item_title(program) or slug,
            kind="Program",
            slug=str(program.get("slug") or slug),
            program_slug=str(program.get("slug") or slug),
            program_name=_item_title(program) or slug,
            year=_normalize_year(program.get("releaseYear")),
            raw=dict(program),
        )

    def video_by_slug(self, program_slug: str, video_slug: str) -> CatalogItem:
        data = self._graphql(
            GQL_VIDEO_BY_SLUG,
            {"programSlug": program_slug, "videoSlug": video_slug},
        )
        video = data.get("videoBySlug") or data.get("video") or {}
        if not isinstance(video, dict) or not video.get("id"):
            raise Tf1Error(
                f"Video not found: {program_slug}/{video_slug}",
                data,
            )
        program = video.get("program") if isinstance(video.get("program"), dict) else {}
        return normalize_video_item(
            video,
            program_slug=program_slug or str(program.get("slug") or ""),
            program_name=str(program.get("name") or ""),
        )

    def live_channels(self) -> list[CatalogItem]:
        return [
            CatalogItem(
                id=str(channel["id"]),
                title=str(channel["title"]),
                kind="Live",
                slug=str(channel["slug"]),
                is_live=True,
                raw=dict(channel),
            )
            for channel in LIVE_CHANNELS
        ]

    def live_by_slug(self, slug: str) -> CatalogItem:
        raw = slug.strip()
        if raw.upper().startswith("L_"):
            for channel in self.live_channels():
                if channel.id.upper() == raw.upper():
                    return channel
            return CatalogItem(
                id=raw.upper(),
                title=raw.upper(),
                kind="Live",
                slug=raw,
                is_live=True,
            )
        normalized = raw.lower().replace("_", "-")
        aliases = {
            "tf1-series-films": "tf1-series-films",
            "tf1seriesfilms": "tf1-series-films",
            "series-films": "tf1-series-films",
            "mytf1": "tf1",
        }
        normalized = aliases.get(normalized, normalized)
        for channel in self.live_channels():
            if channel.slug.lower() == normalized:
                return channel
        return CatalogItem(
            id=f"L_{normalized.upper()}",
            title=normalized.upper(),
            kind="Live",
            slug=normalized,
            is_live=True,
        )

    # ---------------------------------------------------------------- playback

    def _mediainfo_params(self) -> dict[str, str]:
        return {
            "context": MEDIAINFO_CONTEXT,
            "pver": MEDIAINFO_PVER,
            "platform": "android",
            "os": "android",
            "osVersion": "12",
            "device": "tv",
            "playerVersion": PLAYER_VERSION,
            "productName": PRODUCT_NAME,
            "productVersion": APP_VERSION,
            "format": MEDIAINFO_FORMAT,
        }

    def get_mediainfo(self, media_id: str, *, retry: bool = True) -> dict[str, Any]:
        self.ensure_auth()
        response = self._request(
            "GET",
            f"{MEDIAINFO_BASE}/mediainfocombo/{media_id}",
            headers={
                "User-Agent": PLAYER_USER_AGENT,
                "Accept": "application/json",
                "Authorization": f"Bearer {self.state.access_token}",
            },
            params=self._mediainfo_params(),
        )
        if response.status_code == 401 and retry and self.refresh():
            return self.get_mediainfo(media_id, retry=False)
        data = self._safe_json(response)
        if response.status_code >= 400:
            raise Tf1Error(f"MediaInfo HTTP {response.status_code}", data)
        media = data.get("media") or {}
        if media.get("error_code") and media.get("error_code") not in {"0", 0, None, ""}:
            delivery = data.get("delivery") or {}
            if not delivery.get("url"):
                raise Tf1Error(
                    f"MediaInfo error [{media.get('error_code')}]: "
                    f"{media.get('error_desc') or 'request failed'}",
                    data,
                )
        return data

    def _license_from_delivery(
        self, delivery: dict[str, Any], media_id: str
    ) -> tuple[str, dict[str, str]]:
        license_url = ""
        headers: dict[str, str] = {}
        drms = delivery.get("drms") or []
        if isinstance(drms, list):
            for drm in drms:
                if not isinstance(drm, dict):
                    continue
                name = str(drm.get("name") or "").lower()
                if name and name != "widevine":
                    continue
                license_url = str(drm.get("url") or "")
                drm_headers = drm.get("h") or drm.get("drmHeaders") or []
                if isinstance(drm_headers, list):
                    for header in drm_headers:
                        if not isinstance(header, dict):
                            continue
                        key = header.get("k") or header.get("name") or header.get("key")
                        value = header.get("v") or header.get("value")
                        if key and value is not None:
                            headers[str(key)] = str(value)
                if license_url:
                    break
        if not license_url:
            license_url = str(delivery.get("drm-server") or delivery.get("drmServer") or "")
        if not license_url:
            license_url = f"{DRM_PROXY_DEFAULT}?id={media_id}"
        return license_url, headers

    def resolve(
        self,
        media_id: str,
        *,
        title: str = "",
        series_title: str = "",
        season: int | None = None,
        episode: int | None = None,
        year: str = "",
        is_live: bool = False,
    ) -> Source:
        media_info = self.get_mediainfo(media_id)
        media = media_info.get("media") or {}
        delivery = media_info.get("delivery") or {}
        resolved_title = str(media.get("title") or title or media_id)
        manifest_url = str(delivery.get("url") or "")
        if not manifest_url:
            raise Tf1Error("Playback response has no delivery.url", media_info)
        live = is_live or str(media.get("type") or "").lower() == "live"
        needs_drm = bool(media.get("drm")) or bool(delivery.get("drm")) or bool(delivery.get("drms"))
        license_url = ""
        license_headers: dict[str, str] = {}
        encrypted = False
        if needs_drm or delivery.get("drms"):
            encrypted = True
            license_url, license_headers = self._license_from_delivery(delivery, media_id)
        return Source(
            media_id=str(media_id),
            manifest=manifest_url,
            title=resolved_title,
            series_title=series_title,
            season=season,
            episode=episode,
            year=year or _normalize_year(media.get("year") or media.get("releaseYear")),
            is_live=live,
            encrypted=encrypted,
            license_url=license_url,
            license_headers=license_headers,
            format=str(delivery.get("format") or ""),
            note="widevine" if encrypted else "clear",
        )

    def widevine_license(
        self,
        challenge: bytes,
        *,
        license_url: str,
        license_headers: dict[str, str] | None = None,
    ) -> bytes:
        if not license_url:
            raise Tf1Error("Widevine license URL is missing")
        if not challenge:
            raise Tf1Error("Empty Widevine challenge")
        headers = {
            "Content-Type": "application/octet-stream",
            "User-Agent": PLAYER_USER_AGENT,
        }
        if license_headers:
            headers.update(license_headers)
        response = self._request(
            "POST",
            license_url,
            headers=headers,
            data=challenge,
        )
        if response.status_code >= 400:
            raise Tf1Error(
                f"Widevine license HTTP {response.status_code}",
                response.text[:500],
            )
        body = response.content or b""
        if not body:
            raise Tf1Error("Widevine license server returned an empty body")
        return body


__all__ = [
    "ACTIVATION_URL",
    "CatalogItem",
    "DeviceCodeChallenge",
    "LIVE_CHANNELS",
    "Session",
    "Source",
    "TOKEN_FILE",
    "Tf1Api",
    "Tf1AuthRequired",
    "Tf1Error",
    "USER_AGENT",
    "normalize_login_method",
    "parse_input",
]
