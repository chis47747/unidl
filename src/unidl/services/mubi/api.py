"""MUBI Android TV API client.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pycountry
import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter

_LOG = logging.getLogger(__name__)

API_BASE = "https://api.mubi.com/v4"
LICENSE_URL = "https://lic.drmtoday.com/license-proxy-widevine/cenc/"
TOKEN_FILE = "mubi_tokens.json"

APP_VERSION = "99.1"
CLIENT = "android_tv"
CLIENT_APP = "mubi"
DEVICE_BRAND = "Google"
DEVICE_MODEL = "Chromecast"
DEVICE_OS = "12"
USER_AGENT = "okhttp/4.12.0"
ACCEPT_LANGUAGE = "zh-CN"
ACCEPT_AUDIO_CODECS = "EAC3,AAC"
REQUEST_TIMEOUT = 30
LOGIN_TIMEOUT = 600
LOGIN_POLL_INTERVAL = 3.0

AUTH_ERROR_CODES = {8, 21, 26, 40, 41, 42}
PENDING_AUTH_CODE = 20

ERROR_NAMES = {
    1: "missing parameter",
    2: "invalid parameter",
    3: "invalid country",
    4: "unsupported country",
    7: "film not found",
    8: "invalid login token",
    20: "TV activation is pending",
    21: "invalid TV authentication token",
    26: "re-authentication is required",
    40: "invalid access token",
    41: "expired access token",
    42: "invalidated access token",
    50: "film is not authorized",
    51: "the account is not subscribed",
    52: "there is no active viewing",
    53: "the viewing is not usable in this country",
    54: "the film is no longer available",
    55: "reel not found",
    56: "the reel does not belong to this film",
    57: "the reel is not authorized",
    59: "playback must stop",
    97: "access denied",
    98: "temporary service error",
    120: "the client must be upgraded",
    121: "a client upgrade is recommended",
    122: "the subscription is paused",
    304: "a parental PIN is required",
    305: "the parental PIN is wrong",
    306: "too many parental PIN attempts",
    10002: "the client is rate limited",
    10003: "the client is blocked",
}

VIDEO_CODECS = {
    "any": ("AV1", "VP9", "H265", "H264"),
    "av1": ("AV1",),
    "vp9": ("VP9",),
    "h265": ("H265",),
    "h264": ("H264",),
}

QUALITY_HEIGHTS: dict[str, int | None] = {
    "best": None,
    "worst": None,
    "4320": 4320,
    "2160": 2160,
    "1440": 1440,
    "1080": 1080,
    "720": 720,
    "480": 480,
}

_URL = re.compile(
    r"^https?://(?:www\.)?mubi\.com(?:/[^/?#]+)*/(?P<kind>films|series)/(?P<slug>[^/?#]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_HTML = re.compile(r"<[^>]+>")


class MubiError(RuntimeError):
    """A current MUBI request or response could not be used."""


class MubiAuthError(MubiError):
    """The cached access token is explicitly invalid."""


@dataclass(frozen=True)
class ParsedInput:
    kind: str  # film | series
    slug: str


@dataclass(frozen=True)
class SourceRequest:
    quality: str
    video_codec: str
    height: int | None
    codecs: tuple[str, ...]

    @property
    def codecs_header(self) -> str:
        return ",".join(self.codecs)

    @property
    def label(self) -> str:
        quality = "automatic height" if self.height is None else f"up to {self.height}p"
        codecs = "/".join(self.codecs)
        return f"API {quality} · {codecs}"


@dataclass(frozen=True)
class Activation:
    code: str
    auth_token: str
    url: str
    qr_code: str = ""
    expires_in: int = LOGIN_TIMEOUT
    poll_interval: float = LOGIN_POLL_INTERVAL


@dataclass
class SessionState:
    token: str = ""
    user_id: str = ""
    user_name: str = ""
    country_code: str = ""
    device_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @classmethod
    def from_cache(cls, raw: dict[str, Any] | None) -> SessionState:
        data = raw if isinstance(raw, dict) else {}
        device_id = str(data.get("device_id") or "").strip()
        return cls(
            token=str(data.get("token") or "").strip(),
            user_id=str(data.get("user_id") or "").strip(),
            user_name=str(data.get("user_name") or "").strip(),
            country_code=str(data.get("country_code") or "").strip().upper(),
            device_id=device_id or str(uuid.uuid4()),
        )

    def to_cache(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "country_code": self.country_code,
            "device_id": self.device_id,
        }

    @property
    def signed_in(self) -> bool:
        return bool(self.token)


@dataclass(frozen=True)
class EpisodeInfo:
    series_title: str
    title: str
    season: int
    number: int

    @property
    def label(self) -> str:
        return f"S{self.season:02d}E{self.number:02d}  {self.title}"


@dataclass(frozen=True)
class Film:
    id: str
    slug: str
    title: str
    year: int | None = None
    synopsis: str = ""
    genres: tuple[str, ...] = ()
    episode: EpisodeInfo | None = None
    media_features: tuple[str, ...] = ()
    duration_seconds: int | None = None
    attachments: tuple[Attachment, ...] = ()
    original_audio_language: str = ""

    @property
    def kind(self) -> str:
        return "episode" if self.episode is not None else "movie"

    @property
    def label(self) -> str:
        if self.episode is not None:
            return f"{self.episode.series_title} {self.episode.label}"
        return f"{self.title} ({self.year})" if self.year else self.title


@dataclass(frozen=True)
class Season:
    id: str
    number: int
    title: str
    episode_count: int
    release_year: int | None = None

    @property
    def label(self) -> str:
        return f"Season {self.number} · {self.title}"


@dataclass(frozen=True)
class Series:
    id: str
    slug: str
    title: str
    synopsis: str
    seasons: tuple[Season, ...]


@dataclass(frozen=True)
class SearchHit:
    kind: str  # movie | episode | series
    film: Film | None = None
    series_slug: str = ""
    series_title: str = ""

    @property
    def label(self) -> str:
        if self.kind == "series":
            return self.series_title
        if self.film is None:
            return ""
        return self.film.label


@dataclass(frozen=True)
class SubtitleTrack:
    id: str
    url: str
    language: str
    role: str
    legacy_format: bool

    @property
    def label(self) -> str:
        return " ".join(part for part in (self.language, self.role) if part) or self.id


@dataclass(frozen=True)
class SubtitleFile:
    content: bytes
    suffix: str


@dataclass(frozen=True)
class Source:
    manifest: str
    encrypted: bool
    video_codec: str
    subtitles: tuple[SubtitleTrack, ...]
    request: SourceRequest
    reel_id: int | None = None

    def note(self) -> str:
        returned = self.video_codec or "codec not named"
        return f"TV source {self.request.label} · returned {returned} · {len(self.subtitles)} subtitle(s)"


def parse_input(text: str) -> ParsedInput | None:
    raw = str(text or "").strip()
    match = _URL.match(raw)
    if not match:
        return None
    kind = "film" if match.group("kind").lower() == "films" else "series"
    return ParsedInput(kind=kind, slug=match.group("slug"))


def source_request(quality: str, video_codec: str) -> SourceRequest:
    quality_key = str(quality or "").strip().lower()
    codec_key = str(video_codec or "").strip().lower()
    if quality_key not in QUALITY_HEIGHTS:
        raise MubiError(f"MUBI does not define a source request for video quality '{quality}'")
    if codec_key not in VIDEO_CODECS:
        raise MubiError(f"MUBI does not define a source request for codec '{video_codec}'")
    return SourceRequest(
        quality=quality_key,
        video_codec=codec_key,
        height=QUALITY_HEIGHTS[quality_key],
        codecs=VIDEO_CODECS[codec_key],
    )


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MubiError(f"MUBI response omitted {field_name}")
    return value.strip()


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MubiError(f"MUBI response returned a non-integer {field_name}")
    return value


def _optional_integer(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return _integer(value, field_name)


def _object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MubiError(f"MUBI response returned a non-object {field_name}")
    return value


def _array(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise MubiError(f"MUBI response returned a non-list {field_name}")
    return value


def _description(raw: dict[str, Any]) -> str:
    value = raw.get("short_synopsis") or raw.get("default_editorial") or ""
    return unescape(_HTML.sub("", str(value))).strip()


def _episode_info(raw: dict[str, Any]) -> EpisodeInfo:
    return EpisodeInfo(
        series_title=_text(raw.get("series_title"), "episode.series_title"),
        title=_text(raw.get("episode_title"), "episode.episode_title"),
        season=_integer(raw.get("season_number"), "episode.season_number"),
        number=_integer(raw.get("number"), "episode.number"),
    )


def language_code(value: str) -> str:
    """Resolve ISO language names/tags without guessing unknown display names."""
    value = value.strip().replace("_", "-")
    if not value:
        return ""
    is_tag = bool(re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value))
    primary, _, suffix = value.partition("-")
    if is_tag:
        primary = primary.lower()
        language = (
            pycountry.languages.get(alpha_2=primary) if len(primary) == 2 else
            pycountry.languages.get(alpha_3=primary) or pycountry.languages.get(bibliographic=primary)
        )
    else:
        try:
            language = pycountry.languages.lookup(value)
        except LookupError:
            return ""
    if language is None:
        return ""
    code = getattr(language, "alpha_2", language.alpha_3)
    if code in {"und", "mul", "zxx"}:
        return ""
    return code + (f"-{suffix.lower()}" if is_tag and suffix else "")


def original_audio_matches(language: str, original: str) -> bool:
    actual, wanted = language_code(language), language_code(original)
    return bool(actual and wanted and (actual == wanted or ("-" not in wanted and actual.split("-", 1)[0] == wanted)))


def film_from_raw(raw: dict[str, Any]) -> Film:
    episode_raw = raw.get("episode")
    episode = None if episode_raw is None else _episode_info(_object(episode_raw, "film.episode"))
    genres_raw = raw.get("genres")
    if genres_raw is None:
        genres: tuple[str, ...] = ()
    else:
        values = _array(genres_raw, "film.genres")
        if not all(isinstance(item, str) for item in values):
            raise MubiError("MUBI response returned a non-string film genre")
        genres = tuple(item.strip() for item in values if item.strip())
    consumable = raw.get("consumable")
    languages = consumable.get("playback_languages") if isinstance(consumable, dict) else None
    features = languages.get("media_features") if isinstance(languages, dict) else None
    options = languages.get("media_options") if isinstance(languages, dict) else None
    duration = options.get("duration") if isinstance(options, dict) else None
    original = languages.get("original_audio_language") if isinstance(languages, dict) else None
    if original is not None and not isinstance(original, str):
        raise MubiError("MUBI response returned a non-string original_audio_language")
    return Film(
        id=str(_integer(raw.get("id"), "film.id")),
        slug=_text(raw.get("slug"), "film.slug"),
        title=_text(raw.get("title"), "film.title"),
        year=_optional_integer(raw.get("year"), "film.year"),
        synopsis=_description(raw),
        genres=genres,
        episode=episode,
        media_features=tuple(value for value in features if isinstance(value, str))
        if isinstance(features, list) else (),
        duration_seconds=duration if type(duration) is int and duration > 0 else None,
        attachments=attachments_from_raw(raw),
        original_audio_language=(original or "").strip(),
    )


def attachments_from_raw(raw: dict[str, Any]) -> tuple[Attachment, ...]:
    """Keep declared artwork URLs and locale/format labels; never resize URLs."""
    attachments: list[Attachment] = []
    seen: set[str] = set()

    def add(value: Any, name: str, kind: str) -> None:
        if not isinstance(value, str):
            return
        url = value.strip()
        try:
            parsed = urlparse(url)
            valid = (
                parsed.scheme in {"http", "https"} and parsed.hostname
                and not parsed.username and not parsed.password
            )
        except ValueError:
            return
        if not valid or url in seen:
            return
        seen.add(url)
        attachments.append(Attachment(url=url, name=name, kind=kind))

    artworks = raw.get("artworks")
    for artwork in artworks if isinstance(artworks, list) else ():
        if not isinstance(artwork, dict):
            continue
        format_name = artwork.get("format")
        if not isinstance(format_name, str) or not format_name.strip():
            continue
        name = format_name.replace("_", " ").strip().title()
        locale = artwork.get("locale")
        if isinstance(locale, str) and locale.strip():
            name += f" ({locale.strip()})"
        kind = "poster" if format_name == "cover_artwork_vertical" else "artwork"
        add(artwork.get("image_url"), name, kind)
    add(raw.get("still_url"), "Film still", "thumbnail")
    add(raw.get("title_treatment_url"), "Title treatment", "artwork")
    stills = raw.get("stills")
    if isinstance(stills, dict):
        for key in ("retina", "standard", "medium", "small", "small_overlaid", "large_overlaid", "standard_push"):
            add(stills.get(key), f"Still {key.replace('_', ' ')}", "thumbnail")
    return tuple(attachments)


def chapters_from_reel(raw: dict[str, Any]) -> tuple[Chapter, ...]:
    """Reel.duration and Reel.credits_roll_at are seconds in APK 99.1."""
    duration = raw.get("duration")
    credits = raw.get("credits_roll_at")
    if credits is None or credits == 0:
        return ()
    if type(duration) is not int or type(credits) is not int or not 0 < credits < duration:
        _LOG.warning("MUBI reel chapter markers are invalid; omitting chapters")
        return ()
    return (
        Chapter.from_seconds(0, "Main feature", end=credits),
        Chapter.from_seconds(credits, "Credits", end=duration, kind="credits"),
    )


def season_from_raw(raw: dict[str, Any]) -> Season:
    return Season(
        id=str(_integer(raw.get("id"), "season.id")),
        number=_integer(raw.get("season_number"), "season.season_number"),
        title=_text(raw.get("title"), "season.title"),
        episode_count=_integer(raw.get("episode_count"), "season.episode_count"),
        release_year=_optional_integer(raw.get("release_year"), "season.release_year"),
    )


def series_from_raw(raw: dict[str, Any]) -> Series:
    seasons = tuple(
        season_from_raw(_object(item, "series.seasons[]"))
        for item in _array(raw.get("seasons"), "series.seasons")
    )
    return Series(
        id=str(_integer(raw.get("id"), "series.id")),
        slug=_text(raw.get("slug"), "series.slug"),
        title=_text(raw.get("title"), "series.title"),
        synopsis=_description(raw),
        seasons=seasons,
    )


def search_hit_from_raw(raw: dict[str, Any]) -> SearchHit:
    film = film_from_raw(raw)
    series_raw = raw.get("series")
    if film.episode is not None:
        series = _object(series_raw, "film.series")
        return SearchHit(
            kind="episode",
            film=film,
            series_slug=_text(series.get("slug"), "film.series.slug"),
            series_title=_text(series.get("title"), "film.series.title"),
        )
    if series_raw is not None:
        series = _object(series_raw, "film.series")
        return SearchHit(
            kind="series",
            film=film,
            series_slug=_text(series.get("slug"), "film.series.slug"),
            series_title=_text(series.get("title"), "film.series.title"),
        )
    return SearchHit(kind="movie", film=film)


def subtitle_from_raw(raw: dict[str, Any]) -> SubtitleTrack:
    legacy = raw.get("legacy_format", False)
    if not isinstance(legacy, bool):
        raise MubiError("MUBI response returned a non-boolean subtitle legacy_format")
    language = raw.get("language_code")
    role = raw.get("role")
    if language is not None and not isinstance(language, str):
        raise MubiError("MUBI response returned a non-string subtitle language_code")
    if role is not None and not isinstance(role, str):
        raise MubiError("MUBI response returned a non-string subtitle role")
    return SubtitleTrack(
        id=_text(raw.get("id"), "subtitle.id"),
        url=_text(raw.get("url"), "subtitle.url"),
        language=str(language or "und").strip() or "und",
        role=str(role or "").strip(),
        legacy_format=legacy,
    )


def source_from_raw(raw: dict[str, Any], request: SourceRequest) -> Source:
    subtitles_raw = raw.get("text_track_urls")
    subtitles = ()
    if subtitles_raw is not None:
        subtitles = tuple(
            subtitle_from_raw(_object(item, "secure_url.text_track_urls[]"))
            for item in _array(subtitles_raw, "secure_url.text_track_urls")
        )
    codec = raw.get("video_codec")
    if codec is not None and not isinstance(codec, str):
        raise MubiError("MUBI secure_url returned a non-string video_codec")
    if request.video_codec != "any" and codec and codec.strip().upper() not in request.codecs:
        raise MubiError(
            f"MUBI returned {codec.upper()} for requested {request.codecs_header}; "
            "the requested codec is unavailable for this viewing"
        )
    return Source(
        manifest=_text(raw.get("url"), "secure_url.url"),
        encrypted=raw.get("drm") is not None,
        video_codec=str(codec or "").strip(),
        subtitles=subtitles,
        request=request,
    )


class MubiApi:
    def __init__(
        self,
        session: requests.Session,
        state: SessionState | None = None,
        on_update: Callable[[SessionState], None] | None = None,
    ):
        self.session = session
        self.state = state or SessionState()
        self.on_update = on_update
        self._verified = False
        self._playback_verified_until: dict[str, float] = {}

    def _save(self) -> None:
        if self.on_update is not None:
            self.on_update(self.state)

    def _headers(
        self,
        *,
        auth: bool,
        country: bool = True,
        video_codecs: tuple[str, ...] | None = None,
    ) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": ACCEPT_LANGUAGE,
            "Client": CLIENT,
            "Client-Version": APP_VERSION,
            "Client-Device-Identifier": self.state.device_id,
            "Client-App": CLIENT_APP,
            "Client-Device-Brand": DEVICE_BRAND,
            "Client-Device-Model": DEVICE_MODEL,
            "Client-Device-OS": DEVICE_OS,
            "Client-Accept-Video-Codecs": ",".join(video_codecs or VIDEO_CODECS["any"]),
            "Client-Accept-Audio-Codecs": ACCEPT_AUDIO_CODECS,
        }
        if country and self.state.country_code:
            headers["Client-Country"] = self.state.country_code
        if auth:
            if not self.state.token:
                raise MubiAuthError("MUBI is not signed in; use Sign in first")
            headers["Authorization"] = f"Bearer {self.state.token}"
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        label: str,
        auth: bool,
        country: bool = True,
        video_codecs: tuple[str, ...] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        headers = self._headers(auth=auth, country=country, video_codecs=video_codecs)
        headers.update(kwargs.pop("headers", {}) or {})
        try:
            return self.session.request(
                method,
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise MubiError(f"{label} failed: {exc}") from exc

    @staticmethod
    def _json(response: requests.Response, label: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise MubiError(f"{label} returned non-JSON data") from exc
        if not isinstance(payload, dict):
            raise MubiError(f"{label} returned a non-object JSON response")
        return payload

    @staticmethod
    def _error(response: requests.Response, payload: dict[str, Any] | None = None) -> MubiError:
        data = payload
        if data is None:
            try:
                value = response.json()
                data = value if isinstance(value, dict) else {}
            except ValueError:
                data = {}
        code = data.get("code") if type(data.get("code")) is int else None
        message = str(data.get("user_message") or data.get("message") or "").strip()
        known = ERROR_NAMES.get(code, "") if code is not None else ""
        detail = message or known or f"HTTP {response.status_code}"
        if code is not None:
            detail = f"{detail} (MUBI #{code})"
        error_type = MubiAuthError if code in AUTH_ERROR_CODES or (response.status_code == 401 and code is None) else MubiError
        return error_type(detail)

    def _raise_for_error(self, response: requests.Response) -> None:
        if response.status_code >= 400:
            error = self._error(response)
            if isinstance(error, MubiAuthError):
                self._verified = False
                self._playback_verified_until.clear()
            raise error

    def _json_ok(self, response: requests.Response, label: str) -> dict[str, Any]:
        self._raise_for_error(response)
        return self._json(response, label)

    def app_config(self) -> tuple[str, str]:
        response = self._request(
            "GET",
            f"{API_BASE}/app_config",
            label="MUBI app_config",
            auth=False,
            country=False,
        )
        payload = self._json_ok(response, "MUBI app_config")
        activation_url = _text(payload.get("activation_url"), "app_config.activation_url")
        country = _text(payload.get("country"), "app_config.country").upper()
        if not activation_url.lower().startswith(("http://", "https://")):
            activation_url = "https://" + activation_url.lstrip("/")
        return activation_url, country

    def start_login(self) -> Activation:
        activation_url, country = self.app_config()
        self.state.country_code = country
        self._save()
        response = self._request(
            "GET",
            f"{API_BASE}/link_code",
            label="MUBI link code",
            auth=False,
        )
        payload = self._json_ok(response, "MUBI link code")
        qr_code = payload.get("qr_code")
        if qr_code is not None and not isinstance(qr_code, str):
            raise MubiError("MUBI link_code returned a non-string qr_code")
        return Activation(
            code=_text(payload.get("link_code"), "link_code.link_code"),
            auth_token=_text(payload.get("auth_token"), "link_code.auth_token"),
            url=activation_url,
            qr_code=str(qr_code or ""),
        )

    def poll_login(self, activation: Activation) -> SessionState | None:
        response = self._request(
            "POST",
            f"{API_BASE}/authenticate",
            label="MUBI authentication",
            auth=False,
            json={"auth_token": activation.auth_token},
        )
        payload = self._json(response, "MUBI authentication")
        if response.status_code == 422 and payload.get("code") == PENDING_AUTH_CODE:
            return None
        self._raise_for_error(response)
        token = _text(payload.get("token"), "authenticate.token")
        user = _object(payload.get("user"), "authenticate.user")
        self.state.token = token
        self._playback_verified_until.clear()
        self.state.user_id = str(_integer(user.get("id"), "authenticate.user.id"))
        name = user.get("name") or user.get("email") or ""
        if name is not None and not isinstance(name, str):
            raise MubiError("MUBI authenticate returned a non-string user name")
        self.state.user_name = str(name).strip()
        self._verified = False
        self.current_user()
        self._save()
        return self.state

    def current_user(self) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"{API_BASE}/current_user",
            label="MUBI current_user",
            auth=True,
        )
        payload = self._json_ok(response, "MUBI current_user")
        user_id = str(_integer(payload.get("id"), "current_user.id"))
        name = payload.get("name") or payload.get("email") or ""
        if name is not None and not isinstance(name, str):
            raise MubiError("MUBI current_user returned a non-string name")
        changed = user_id != self.state.user_id or str(name).strip() != self.state.user_name
        self.state.user_id = user_id
        self.state.user_name = str(name).strip()
        self._verified = True
        if changed:
            self._save()
        return payload

    def ensure(self) -> None:
        if not self.state.token:
            raise MubiAuthError("MUBI is not signed in; use Sign in first")
        if not self._verified:
            self.current_user()

    def login_token(self) -> str:
        """Issue a fresh web sign-in token, as the APK does before opening billing."""
        self.ensure()
        response = self._request(
            "POST",
            f"{API_BASE}/login_tokens",
            label="MUBI login token",
            auth=True,
        )
        payload = self._json_ok(response, "MUBI login token")
        return _text(payload.get("login_token"), "login_tokens.login_token")

    def logout(self) -> None:
        """Revoke this TV session through the APK's DELETE sessions endpoint."""
        if not self.state.token:
            return
        response = self._request(
            "DELETE", f"{API_BASE}/sessions", label="MUBI sign out", auth=True,
        )
        if response.status_code >= 400:
            error = self._error(response)
            if not isinstance(error, MubiAuthError):
                raise error
        self._verified = False
        self._playback_verified_until.clear()

    def verify_playback(self, film_id: str) -> None:
        """Verify at delivery boundaries, reusing only the server-provided TTL."""
        self.ensure()
        if self._playback_verified_until.get(film_id, 0) > time.monotonic():
            return
        response = self._request(
            "PUT", f"{API_BASE}/films/{film_id}/viewing/verify",
            label="MUBI playback verification", auth=True,
        )
        payload = self._json_ok(response, "MUBI playback verification")
        ttl = _integer(payload.get("ttl"), "playback verification ttl")
        if ttl <= 0:
            raise MubiError("MUBI playback verification returned a non-positive ttl")
        self._playback_verified_until[film_id] = time.monotonic() + ttl

    def film(self, slug: str) -> Film:
        self.ensure()
        response = self._request(
            "GET",
            f"{API_BASE}/films/{slug}",
            label="MUBI film",
            auth=True,
        )
        return film_from_raw(self._json_ok(response, "MUBI film"))

    def series(self, slug: str) -> Series:
        self.ensure()
        response = self._request(
            "GET",
            f"{API_BASE}/series/{slug}",
            label="MUBI series",
            auth=True,
        )
        return series_from_raw(self._json_ok(response, "MUBI series"))

    def season_episodes(self, series_slug: str, season_id: str) -> list[Film]:
        self.ensure()
        response = self._request(
            "GET",
            f"{API_BASE}/series/{series_slug}/seasons/{season_id}/episodes/available",
            label="MUBI season episodes",
            auth=True,
        )
        payload = self._json_ok(response, "MUBI season episodes")
        films = [
            film_from_raw(_object(item, "episodes.episodes[]"))
            for item in _array(payload.get("episodes"), "episodes.episodes")
        ]
        if any(item.episode is None for item in films):
            raise MubiError("MUBI season episodes contained a film without episode metadata")
        return sorted(films, key=lambda item: (item.episode.season, item.episode.number))  # type: ignore[union-attr]

    def search(self, query: str, *, page: int = 1, per_page: int = 24) -> list[SearchHit]:
        """Follow the APK pagination metadata without exposing zero-hit recommendations."""
        self.ensure()
        if type(page) is not int or page < 1 or type(per_page) is not int or per_page < 1:
            raise MubiError("MUBI search requires a positive page and per_page")
        hits: list[SearchHit] = []
        while True:
            response = self._request(
                "GET", f"{API_BASE}/search/films", label="MUBI search", auth=True,
                params={"page": page, "per_page": per_page, "playable": "true",
                        "all_films_on_zero_hits": "true", "query": query},
            )
            payload = self._json_ok(response, "MUBI search")
            zero_results = payload.get("zero_results")
            if not isinstance(zero_results, bool):
                raise MubiError("MUBI search returned a non-boolean zero_results")
            if zero_results:
                return hits
            rows = _array(payload.get("films"), "search.films")
            meta = _object(payload.get("meta"), "search.meta")
            current = _integer(meta.get("current_page"), "search.meta.current_page")
            total = _integer(meta.get("total_pages"), "search.meta.total_pages")
            following = _optional_integer(meta.get("next_page"), "search.meta.next_page")
            if current != page or total < current:
                raise MubiError("MUBI search pagination did not match the requested page")
            hits.extend(search_hit_from_raw(_object(item, "search.films[]")) for item in rows)
            if following is None:
                if current != total:
                    raise MubiError("MUBI search pagination ended before its final page")
                return hits
            if not rows or following != current + 1 or following > total:
                raise MubiError("MUBI search pagination did not advance to the next page")
            page = following

    def playback(self, film_id: str, request: SourceRequest) -> Source:
        self.ensure()
        viewing = self._request(
            "POST",
            f"{API_BASE}/films/{film_id}/viewing",
            label="MUBI viewing",
            auth=True,
            video_codecs=request.codecs,
            params={"download": "false", "parental_lock_enabled": "true"},
        )
        viewing_data = self._json_ok(viewing, "MUBI viewing")
        self._playback_verified_until.pop(film_id, None)
        reel_id = viewing_data.get("reel_id")

        params: dict[str, Any] = {"download": "false"}
        if request.height is not None:
            params["height"] = request.height
        secure = self._request(
            "GET",
            f"{API_BASE}/films/{film_id}/viewing/secure_url",
            label="MUBI secure_url",
            auth=True,
            video_codecs=request.codecs,
            params=params,
        )
        source = source_from_raw(self._json_ok(secure, "MUBI secure_url"), request)
        return replace(source, reel_id=reel_id if type(reel_id) is int and reel_id > 0 else None)

    def chapters(self, film_id: str, reel_id: int) -> tuple[Chapter, ...]:
        self.ensure()
        response = self._request(
            "GET",
            f"{API_BASE}/films/{film_id}/reels",
            label="MUBI chapter reels",
            auth=True,
        )
        self._raise_for_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise MubiError("MUBI chapter reels returned non-JSON data") from exc
        reels = _array(payload, "reels")
        matches = [row for row in reels if isinstance(row, dict)
                   and type(row.get("id")) is int and row["id"] == reel_id]
        if len(matches) != 1:
            raise MubiError("MUBI chapter reels did not identify the active viewing reel")
        return chapters_from_reel(matches[0])

    def subtitle(self, track: SubtitleTrack) -> SubtitleFile:
        try:
            response = self.session.get(
                track.url,
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise MubiError(f"MUBI subtitle request failed: {exc}") from exc
        if response.status_code >= 400:
            raise MubiError(f"MUBI subtitle request returned HTTP {response.status_code}")
        content = response.content or b""
        if not content:
            raise MubiError(f"MUBI subtitle {track.label} was empty")
        content_type = str(response.headers.get("content-type") or "").lower().split(";", 1)[0]
        suffix_by_type = {
            "text/vtt": "vtt",
            "application/x-subrip": "srt",
            "application/ttml+xml": "ttml",
            "application/xml": "xml",
            "text/xml": "xml",
        }
        suffix = suffix_by_type.get(content_type, "")
        if not suffix:
            candidate = Path(urlparse(track.url).path).suffix.lower().lstrip(".")
            if candidate in {"vtt", "srt", "ttml", "xml"}:
                suffix = candidate
        if not suffix:
            raise MubiError(
                f"MUBI subtitle {track.label} used unsupported content type '{content_type or 'unknown'}'"
            )
        return SubtitleFile(content=content, suffix=suffix)

    def widevine_license(self, challenge: bytes, *, user_id: str, token: str) -> bytes:
        if not user_id or not token:
            raise MubiError("MUBI Widevine context is missing the user id or access token")
        custom = base64.b64encode(
            json.dumps(
                {"userId": user_id, "sessionId": token, "merchant": "mubi"},
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        try:
            response = self.session.post(
                LICENSE_URL,
                params={"logRequestId": uuid.uuid4().hex},
                data=challenge,
                headers={
                    "User-Agent": USER_AGENT,
                    "Content-Type": "application/octet-stream",
                    "dt-custom-data": custom,
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise MubiError(f"MUBI Widevine licence request failed: {exc}") from exc
        if response.status_code >= 400:
            raise MubiError(f"MUBI Widevine licence returned HTTP {response.status_code}")
        content = response.content or b""
        content_type = str(response.headers.get("content-type") or "").lower()
        if "application/json" in content_type or content.lstrip().startswith(b"{"):
            try:
                payload = response.json()
            except ValueError as exc:
                raise MubiError("MUBI Widevine licence JSON could not be parsed") from exc
            if not isinstance(payload, dict) or not payload.get("license"):
                raise MubiError("MUBI Widevine licence response contained no license")
            try:
                return base64.b64decode(str(payload["license"]), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise MubiError("MUBI Widevine licence response contained invalid Base64") from exc
        if not content:
            raise MubiError("MUBI Widevine licence response was empty")
        return content


__all__ = [
    "Activation",
    "Film",
    "MubiApi",
    "MubiAuthError",
    "MubiError",
    "ParsedInput",
    "SearchHit",
    "Season",
    "Series",
    "SessionState",
    "Source",
    "SourceRequest",
    "SubtitleFile",
    "SubtitleTrack",
    "film_from_raw",
    "parse_input",
    "search_hit_from_raw",
    "series_from_raw",
    "source_from_raw",
    "source_request",
]
