"""ORF ON television API, authentication, catalogue and Widevine transport."""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter

API_BASE_URL = "https://api-tvthek.orf.at/api/v4.3/"
PROFILE_API_BASE_URL = "https://konto-api.orf.at/"
OIDC_DEVICE_URL = "https://anmeldung.orf.at/connect/deviceauthorization"
OIDC_TOKEN_URL = "https://anmeldung.orf.at/connect/token"
OIDC_USERINFO_URL = "https://anmeldung.orf.at/connect/userinfo"

OIDC_CLIENT_ID = "orf-mediathek-production-device"
OIDC_SCOPE = "openid profile email profile.read profile.write offline_access"
OIDC_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

CLIENT_VERSION = "6.14.18-tv"
CLIENT_VERSION_CODE = "5000190"
USER_AGENT = "ORF TVthek Android TV"
ANONYMOUS_BASIC_TOKEN = (
    "MGtpMzIzNDVhNXM5Z3g6Z2s5MDQ5Z2tkZnYzMGRmZ2szMGZnbTM1NDB0M212czhqc2w="
)

TOKEN_FILE = "orf_token.json"
TOKEN_CACHE_VERSION = 1
TOKEN_MIN_TTL_SECONDS = 60
REQUEST_TIMEOUT = (10, 30)
MAX_PROFILE_PAGES = 100

DRM_BRAND_STANDARD = "13f2e056-53fe-4469-ba6d-999970dbe549"
DRM_BRAND_MULTIKEY_VOD = "31149fc4-9e4e-4252-b99d-34a4a90f4618"
DRM_BRAND_MULTIKEY_LIVE = "3c03f537-b018-463d-a058-ff3eb5346cee"
WIDEVINE_LICENSE_URL = "https://drm.ors.at/acquire-license/widevine"
FILM_GENRE_ID = 2703833
SERIES_GENRE_ID = 13776492

WEB_HOSTS = {"orf.at", "on.orf.at", "www.on.orf.at", "api-tvthek.orf.at"}
LIVE_CHANNEL_NAMES = {
    "orf1": "ORF 1",
    "orf2": "ORF 2",
    "orf3": "ORF III",
    "orfs": "ORF SPORT+",
    "orfkids": "ORF Kids",
}
SUPPORTED_VIDEO_KINDS = {"episode", "segment", "livestream"}
_PLAYBACK_ID_PREFIXES = {
    "episode": "3dSlfek03nsLKdj4Jsd",
    "segment": "8783hjd7293kmd",
    "livestream": "8876324jshjd7293ktd",
}
_SOURCE_QUALITIES = {
    "hd": ("QXA", "Q8C", "Q6A", "Q4A"),
    "fhd": ("QXB", "QXA", "Q8C", "Q6A", "Q4A"),
    "uhd": ("QXC", "QXB", "QXA", "Q8C", "Q6A", "Q4A"),
}

_SECRET = re.compile(
    r"(access_token|id_token|refresh_token|device_code|userToken|token)"
    r"(['\"]?\s*[:=]\s*['\"]?)[^,'\"\s&]+",
    re.IGNORECASE,
)
_SEASON = re.compile(r"^(.+?)\s+(Staffel|Season)\s+(\d+)$", re.IGNORECASE)
_EPISODE_POSITION = re.compile(r"\((\d+)\s*/\s*\d+\)")
_CLOCK = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?$")

_IMAGE_ATTACHMENTS = (
    ("image", "Artwork", "image"),
    ("image16x9_with_logo", "Backdrop", "backdrop"),
    ("image2x3_with_logo", "Poster", "poster"),
)


class OrfError(RuntimeError):
    """An ORF failure whose message is safe to display."""

    def __init__(self, message: str, *, status: int = 0, code: str = "") -> None:
        super().__init__(_safe(message))
        self.status = status
        self.code = code


class AuthenticationRequired(OrfError):
    """The selected TV session cannot be used or refreshed."""


def _safe(value: Any) -> str:
    text = _SECRET.sub(r"\1\2hidden", str(value or ""))
    return re.sub(r"(userToken=)[^&\s]+", r"\1hidden", text, flags=re.IGNORECASE)


def normalize_login_mode(value: Any) -> str:
    mode = str(value or "anonymous").strip().lower()
    if mode not in {"tv", "anonymous"}:
        raise OrfError('ORF login mode must be "tv" or "anonymous"')
    return mode


def normalize_source_tier(value: Any) -> str:
    tier = str(value or "uhd").strip().lower()
    if tier not in {"hd", "fhd", "uhd"}:
        raise OrfError('ORF source tier must be "hd", "fhd" or "uhd"')
    return tier


def _jwt_exp(token: str) -> int:
    try:
        part = str(token or "").split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part).decode("utf-8"))
        return int(payload.get("exp") or 0) if isinstance(payload, dict) else 0
    except (IndexError, TypeError, ValueError, UnicodeError, binascii.Error, json.JSONDecodeError):
        return 0


def _integer(value: Any, label: str, *, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise OrfError(f"ORF returned an invalid {label}") from exc


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise OrfError("ORF returned an invalid duration") from exc


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OrfError(f"ORF {label} is not an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise OrfError(f"ORF {label} is not a list")
    return value


def _link(raw: dict[str, Any], name: str = "self") -> str:
    links = raw.get("_links")
    if not isinstance(links, dict):
        return ""
    value = links.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("href") or "")
    return ""


def _append_query(url: str, **values: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(values)
    return urlunparse(parsed._replace(query=urlencode(query)))


@dataclass
class Session:
    access_token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    id_token: str = field(default="", repr=False)
    token_type: str = "Bearer"
    scope: str = ""
    expires_at: int = 0
    user: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_cache(cls, raw: dict[str, Any] | None) -> Session:
        if not raw:
            return cls()
        if raw.get("version") != TOKEN_CACHE_VERSION:
            raise OrfError("ORF token cache has an unsupported version; sign in again")
        return cls(
            access_token=str(raw.get("access_token") or ""),
            refresh_token=str(raw.get("refresh_token") or ""),
            id_token=str(raw.get("id_token") or ""),
            token_type=str(raw.get("token_type") or "Bearer"),
            scope=str(raw.get("scope") or ""),
            expires_at=_integer(raw.get("expires_at"), "cached token expiry", default=0),
            user=dict(raw.get("user") or {}) if isinstance(raw.get("user"), dict) else {},
        )

    def token_current(self, minimum_ttl: int = TOKEN_MIN_TTL_SECONDS) -> bool:
        return bool(self.access_token and self.expires_at > int(time.time()) + minimum_ttl)

    def label(self) -> str:
        for key in ("name", "preferred_username", "email"):
            if self.user.get(key):
                return str(self.user[key])
        return "ORF TV account"

    def to_cache(self) -> dict[str, Any]:
        return {
            "version": TOKEN_CACHE_VERSION,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "token_type": self.token_type,
            "scope": self.scope,
            "expires_at": self.expires_at,
            "user": self.user,
            "client_id": OIDC_CLIENT_ID,
            "client_version": CLIENT_VERSION,
        }


@dataclass
class DeviceChallenge:
    device_code: str = field(repr=False)
    user_code: str = ""
    verification_url: str = ""
    direct_url: str = ""
    expires_in: int = 600
    interval: float = 5.0
    next_poll_at: float = 0.0


@dataclass(frozen=True)
class PinState:
    exists: bool
    enabled: bool


@dataclass(frozen=True)
class Stream:
    url: str = field(repr=False)
    protocol: str
    quality_key: str
    quality_description: str
    adaptive: bool
    multikey: bool
    uhd: bool
    encrypted: bool


@dataclass(frozen=True)
class Profile:
    id: int
    title: str
    episodes_url: str = field(repr=False)
    related_url: str = field(default="", repr=False)

    @property
    def season_info(self) -> tuple[str, int, str] | None:
        match = _SEASON.fullmatch(self.title.strip())
        if not match:
            return None
        base = re.sub(r"\s+", " ", match.group(1)).strip()
        number = int(match.group(3))
        return base, number, f"{match.group(2)} {number}"


@dataclass(frozen=True)
class Video:
    id: int
    kind: str
    title: str
    duration: float | None = None
    exact_duration: int = 0
    genre_id: int = 0
    genre_title: str = ""
    production_year: str = ""
    production_country: str = ""
    date: str = ""
    archive: bool = False
    active_youth_protection: bool = False
    encrypted: bool = False
    drm_token: str = field(default="", repr=False)
    drm_token_multikey: str = field(default="", repr=False)
    streams: tuple[Stream, ...] = ()
    segments: tuple[Video, ...] = ()
    chapters: tuple[Chapter, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    profile: Profile | None = None
    profile_url: str = field(default="", repr=False)
    episode_url: str = field(default="", repr=False)
    episode_id: int = 0
    encrypted_id: str = field(default="", repr=False)
    subtitle_url: str = field(default="", repr=False)

    @property
    def content_kind(self) -> str:
        if self.kind != "episode":
            return self.kind
        genre = self.genre_title.casefold()
        if self.genre_id == FILM_GENRE_ID or genre == "film":
            return "film"
        if self.genre_id == SERIES_GENRE_ID or genre == "serie":
            return "series"
        return "episode"

    @property
    def episode_number(self) -> int | None:
        match = _EPISODE_POSITION.search(self.title)
        return int(match.group(1)) if match else None

    @property
    def episode_name(self) -> str:
        if ":" not in self.title:
            return ""
        return self.title.split(":", 1)[1].strip()

    @property
    def available(self) -> bool:
        return bool(self.streams)


@dataclass(frozen=True)
class SearchHit:
    id: int
    kind: str
    title: str
    type_label: str
    year: str = ""
    archive: bool = False

    @property
    def detail(self) -> str:
        parts = ["Archive" if self.archive else self.type_label]
        if self.year:
            parts.append(self.year)
        parts.append(f"ID {self.id}")
        return " · ".join(parts)


@dataclass(frozen=True)
class Channel:
    id: int
    name: str
    current: str
    playback_url: str = field(repr=False)


@dataclass(frozen=True)
class Source:
    manifest_url: str = field(repr=False)
    protocol: str
    quality_key: str
    quality_description: str
    encrypted: bool
    live: bool
    license_endpoint: str = field(default="", repr=False)
    brand_guid: str = field(default="", repr=False)
    user_token: str = field(default="", repr=False)

    @property
    def line(self) -> str:
        protection = "Widevine" if self.encrypted else "clear"
        quality = self.quality_description or self.quality_key or "adaptive"
        return f"{self.protocol.upper()} · {quality} · {protection}"


@dataclass(frozen=True)
class ParsedReference:
    kind: str
    item_id: int
    segment_id: int | None = None


def parse_reference(value: str) -> ParsedReference | None:
    parsed = urlparse(str(value or "").strip())
    host = str(parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in WEB_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]

    for route in ("sendereihe", "profile"):
        if route in parts:
            index = parts.index(route)
            if index + 1 < len(parts) and parts[index + 1].isdigit():
                return ParsedReference("profile", int(parts[index + 1]))
            return None

    if "video" in parts:
        index = parts.index("video")
        if index + 1 >= len(parts) or not parts[index + 1].isdigit():
            return None
        segment_id = None
        if index + 2 < len(parts) and parts[index + 2].isdigit():
            segment_id = int(parts[index + 2])
        return ParsedReference("episode", int(parts[index + 1]), segment_id)

    for kind in ("episode", "segment", "livestream"):
        if kind in parts:
            index = parts.index(kind)
            if index + 1 < len(parts) and parts[index + 1].isdigit():
                return ParsedReference(kind, int(parts[index + 1]))
            return None
    return None


def _profile(raw: dict[str, Any], label: str = "profile") -> Profile:
    profile_id = _integer(raw.get("id"), f"{label} ID")
    title = str(raw.get("title") or "").strip()
    episodes_url = _link(raw, "episodes")
    if not title or not episodes_url:
        raise OrfError(f"ORF {label} omitted its title or episodes link")
    return Profile(profile_id, title, episodes_url, _link(raw, "related_profiles"))


def _streams(raw: dict[str, Any]) -> tuple[Stream, ...]:
    sources = raw.get("sources")
    if sources is None:
        return ()
    source_map = _object(sources, "stream sources")
    found: list[Stream] = []
    for protocol in ("dash", "hls"):
        values = source_map.get(protocol)
        if values is None:
            continue
        for item in _list(values, f"{protocol.upper()} sources"):
            stream = _object(item, f"{protocol.upper()} source")
            url = str(stream.get("src") or "").strip()
            if not url:
                raise OrfError(f"ORF {protocol.upper()} source omitted its URL")
            found.append(
                Stream(
                    url=url,
                    protocol=protocol,
                    quality_key=str(stream.get("quality_key") or ""),
                    quality_description=str(stream.get("quality_description") or ""),
                    adaptive=bool(stream.get("is_adaptive_stream")),
                    multikey=bool(stream.get("is_multikey_source")),
                    uhd=bool(stream.get("is_uhd")),
                    encrypted=bool(stream.get("is_drm_protected")),
                )
            )
    return tuple(found)


def _clock_seconds(value: Any, label: str) -> float:
    text = str(value or "").strip()
    match = _CLOCK.fullmatch(text)
    if not match:
        raise OrfError(f"ORF {label} has an invalid timestamp")
    fraction = (match.group(4) or "").ljust(3, "0")
    return (
        int(match.group(1)) * 3600
        + int(match.group(2)) * 60
        + int(match.group(3))
        + int(fraction or "0") / 1000
    )


def _chapters(raw: dict[str, Any]) -> tuple[Chapter, ...]:
    values = raw.get("jump_marks")
    if values is None:
        values = []
    marks = _list(values, "jump marks")
    chapters: list[Chapter] = []
    for index, value in enumerate(marks, start=1):
        mark = _object(value, f"jump mark {index}")
        start = _clock_seconds(mark.get("start_time"), f"jump mark {index} start")
        end = _clock_seconds(mark.get("end_time"), f"jump mark {index} end")
        if end <= start:
            raise OrfError(f"ORF jump mark {index} ends before it starts")
        chapters.append(Chapter.from_seconds(start, "Jump mark", end, kind="jump"))

    closing = _number(raw.get("closing_credits_duration_seconds"))
    duration = _number(raw.get("duration_seconds"))
    if closing and closing > 0 and duration and duration > closing:
        chapters.append(
            Chapter.from_seconds(
                duration - closing,
                "Credits",
                duration,
                kind="credits",
            )
        )
    return tuple(chapters)


def _attachment_mime(url: str) -> str:
    suffix = urlparse(url).path.rsplit(".", 1)[-1].casefold() if "." in urlparse(url).path else ""
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(suffix, "")


def _attachments(raw: dict[str, Any], embedded: dict[str, Any]) -> tuple[Attachment, ...]:
    found: list[Attachment] = []
    seen_urls: set[str] = set()
    for key, name, kind in _IMAGE_ATTACHMENTS:
        image = embedded.get(key)
        if not isinstance(image, dict):
            continue
        public_urls = image.get("public_urls")
        if not isinstance(public_urls, dict):
            continue
        reference = public_urls.get("reference")
        if not isinstance(reference, dict):
            continue
        url = str(reference.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        found.append(
            Attachment(
                url=url,
                name=name,
                kind=kind,
                mime_type=_attachment_mime(url),
                filename=f"orf-{_integer(raw.get('id'), 'video ID')}-{kind}"
                f"{'.' + urlparse(url).path.rsplit('.', 1)[-1] if '.' in urlparse(url).path else ''}",
            )
        )
    return tuple(found)


def _video(raw: dict[str, Any], expected_kind: str = "") -> Video:
    item_id = _integer(raw.get("id"), "video ID")
    actual = str(raw.get("video_type") or expected_kind).lower()
    if expected_kind == "livestream" and actual == "timeshift":
        actual = "livestream"
    if actual not in SUPPORTED_VIDEO_KINDS:
        raise OrfError(f"ORF returned an unsupported video type: {actual or 'missing'}")
    if expected_kind and actual != expected_kind:
        raise OrfError(f"ORF returned {actual} where {expected_kind} was requested")
    title = str(raw.get("title") or raw.get("headline") or raw.get("profile_title") or "").strip()
    if not title:
        raise OrfError(f"ORF {actual} {item_id} omitted its title")

    embedded = raw.get("_embedded")
    if embedded is not None and not isinstance(embedded, dict):
        raise OrfError("ORF embedded video data is not an object")
    embedded = embedded or {}

    segment_values = embedded.get("segments")
    if segment_values is None:
        segments: tuple[Video, ...] = ()
    else:
        segments = tuple(
            _video(_object(item, "segment"), "segment")
            for item in _list(segment_values, "episode segments")
        )

    embedded_profile = embedded.get("profile")
    profile = _profile(embedded_profile, "embedded profile") if isinstance(embedded_profile, dict) else None
    subtitle = embedded.get("subtitle")
    chapters = _chapters(raw)
    attachments = _attachments(raw, embedded)

    return Video(
        id=item_id,
        kind=actual,
        title=title,
        duration=_number(raw.get("duration_seconds")),
        exact_duration=_integer(raw.get("exact_duration"), "exact duration", default=0),
        genre_id=_integer(raw.get("genre_id"), "genre ID", default=0),
        genre_title=str(raw.get("genre_title") or ""),
        production_year=str(raw.get("production_year") or ""),
        production_country=str(raw.get("production_country") or ""),
        date=str(raw.get("date") or raw.get("release_date") or ""),
        archive=bool(raw.get("is_archive")),
        active_youth_protection=bool(raw.get("has_active_youth_protection")),
        encrypted=bool(raw.get("is_drm_protected")),
        drm_token=str(raw.get("drm_token") or ""),
        drm_token_multikey=str(raw.get("drm_token_multikey") or ""),
        streams=_streams(raw),
        segments=segments,
        chapters=chapters,
        attachments=attachments,
        profile=profile,
        profile_url=_link(raw, "profile"),
        episode_url=_link(raw, "episode"),
        episode_id=_integer(raw.get("episode_id"), "episode ID", default=0),
        encrypted_id=str(raw.get("encrypted_id") or ""),
        subtitle_url=str(subtitle.get("vtt_url") or "").strip() if isinstance(subtitle, dict) else "",
    )


def equivalent_full_segment(episode: Video) -> bool:
    if len(episode.segments) != 1:
        return False
    segment = episode.segments[0]
    episode_urls = {stream.url for stream in episode.streams}
    segment_urls = {stream.url for stream in segment.streams}
    return bool(
        segment.episode_id == episode.id
        and segment.exact_duration == episode.exact_duration
        and segment.title == episode.title
        and episode_urls
        and segment_urls == episode_urls
    )


def _select_stream(video: Video, tier: str, *, live: bool) -> tuple[Stream, bool]:
    streams = {item.quality_key: item for item in video.streams if item.protocol == "dash"}
    if not streams:
        raise OrfError(f"ORF returned no DASH sources for {video.title}")
    if live:
        multi_keys = ("QXADRM", "QXCDRM") if tier == "hd" else ("QXBDRM", "QXCDRM")
        for key in multi_keys:
            if key in streams and streams[key].multikey:
                return streams[key], True
    elif video.drm_token_multikey:
        key = "QXADRM" if tier == "hd" else "QXBDRM"
        if key in streams:
            return streams[key], True

    qualities = _SOURCE_QUALITIES[tier]
    protected = video.encrypted
    if protected:
        adaptive = [key for key in qualities if key.startswith("QX")]
        qualities = (*[f"{key}DRM" for key in adaptive], *qualities)
    for key in qualities:
        if key in streams:
            return streams[key], False
    raise OrfError(f"ORF returned no supported {tier.upper()} DASH quality for {video.title}")


class OrfApi:

    def __init__(
        self,
        session: requests.Session,
        *,
        login_mode: str = "anonymous",
        state: Session | None = None,
        on_save: Callable[[Session], None] | None = None,
    ) -> None:
        self.http = session
        self.login_mode = normalize_login_mode(login_mode)
        self.state = state or Session()
        self.on_save = on_save
        self._settings: dict[str, Any] | None = None

    def _save(self) -> None:
        if self.on_save is not None:
            self.on_save(self.state)

    @staticmethod
    def _payload(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise OrfError("ORF returned invalid JSON", status=response.status_code) from exc

    @staticmethod
    def _error(response: requests.Response, action: str) -> OrfError:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        detail = ""
        code = ""
        if isinstance(payload, dict):
            code = str(payload.get("error") or payload.get("code") or "")
            for key in ("error_description", "message", "error"):
                if payload.get(key):
                    detail = _safe(payload[key])
                    break
        message = f"{action} failed with HTTP {response.status_code}"
        if detail:
            message += f" ({detail})"
        return OrfError(message, status=response.status_code, code=code)

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        profile_api: bool = False,
        retry: bool = True,
    ) -> Any:
        parsed = urlparse(path_or_url)
        base = PROFILE_API_BASE_URL if profile_api else API_BASE_URL
        url = path_or_url if parsed.scheme else urljoin(base, path_or_url)
        headers = {
            "Accept": "application/json",
            "Authorization": self.authorization_header(),
        }
        try:
            response = self.http.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise OrfError(f"Could not reach ORF: {exc}") from exc
        if response.status_code == 401 and retry and self.login_mode == "tv":
            self.state.access_token = ""
            self.refresh()
            return self._request(
                method,
                path_or_url,
                params=params,
                json_body=json_body,
                profile_api=profile_api,
                retry=False,
            )
        if response.status_code >= 400:
            raise self._error(response, "ORF request")
        return self._payload(response)

    def authorization_header(self) -> str:
        if self.login_mode == "anonymous":
            return f"Basic {ANONYMOUS_BASIC_TOKEN}"
        self.ensure_session()
        return f"Bearer {self.state.access_token}"

    def ensure_session(self) -> Session:
        if self.login_mode == "anonymous":
            return self.state
        if self.state.token_current():
            return self.state
        self.refresh()
        if not self.state.token_current():
            raise AuthenticationRequired("ORF TV sign-in did not produce a current token")
        return self.state

    def _store_token(self, raw: Any) -> Session:
        data = _object(raw, "token response")
        access_token = str(data.get("access_token") or "")
        if not access_token:
            raise OrfError("ORF token response omitted its access token")
        expires_at = _jwt_exp(access_token)
        if not expires_at:
            expires_in = _integer(data.get("expires_in"), "token lifetime")
            expires_at = int(time.time()) + expires_in
        self.state = Session(
            access_token=access_token,
            refresh_token=str(data.get("refresh_token") or self.state.refresh_token),
            id_token=str(data.get("id_token") or self.state.id_token),
            token_type=str(data.get("token_type") or "Bearer"),
            scope=str(data.get("scope") or self.state.scope),
            expires_at=expires_at,
            user=self.state.user,
        )
        self._save()
        return self.state

    def refresh(self) -> Session:
        refresh_token = self.state.refresh_token
        if not refresh_token:
            raise AuthenticationRequired("ORF TV sign-in is required")
        try:
            response = self.http.post(
                OIDC_TOKEN_URL,
                data={
                    "client_id": OIDC_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers={"Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise OrfError(f"Could not refresh the ORF TV token: {exc}") from exc
        if response.status_code >= 400:
            error = self._error(response, "ORF token refresh")
            if error.code in {"invalid_grant", "invalid_token"}:
                self.state = Session()
                self._save()
                raise AuthenticationRequired("ORF TV token expired; sign in again") from None
            raise error
        return self._store_token(self._payload(response))

    def begin_tv_login(self) -> DeviceChallenge:
        try:
            response = self.http.post(
                OIDC_DEVICE_URL,
                data={"client_id": OIDC_CLIENT_ID, "scope": OIDC_SCOPE},
                headers={"Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise OrfError(f"Could not request an ORF TV code: {exc}") from exc
        if response.status_code >= 400:
            raise self._error(response, "ORF TV-code request")
        data = _object(self._payload(response), "TV-code response")
        device_code = str(data.get("device_code") or "")
        user_code = str(data.get("user_code") or "")
        verification_url = str(data.get("verification_uri") or "")
        if not device_code or not user_code or not verification_url:
            raise OrfError("ORF TV-code response is incomplete")
        return DeviceChallenge(
            device_code=device_code,
            user_code=user_code,
            verification_url=verification_url,
            direct_url=str(data.get("verification_uri_complete") or ""),
            expires_in=max(1, _integer(data.get("expires_in"), "TV-code lifetime", default=600)),
            interval=float(max(1, _integer(data.get("interval"), "TV-code interval", default=5))),
        )

    def poll_tv_login(self, challenge: DeviceChallenge) -> Session | None:
        now = time.monotonic()
        if challenge.next_poll_at and now < challenge.next_poll_at:
            return None
        challenge.next_poll_at = now + challenge.interval
        try:
            response = self.http.post(
                OIDC_TOKEN_URL,
                data={
                    "client_id": OIDC_CLIENT_ID,
                    "grant_type": OIDC_DEVICE_GRANT,
                    "device_code": challenge.device_code,
                },
                headers={"Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise OrfError(f"Could not poll the ORF TV code: {exc}") from exc
        data = self._payload(response)
        if response.status_code < 400:
            return self._store_token(data)
        payload = _object(data, "TV-code polling response")
        error = str(payload.get("error") or "")
        if error == "authorization_pending":
            return None
        if error == "slow_down":
            challenge.interval += 5.0
            challenge.next_poll_at = now + challenge.interval
            return None
        if error in {"access_denied", "expired_token"}:
            raise AuthenticationRequired(f"ORF TV activation failed: {error}")
        raise self._error(response, "ORF TV-code polling")

    def account_info(self) -> dict[str, Any]:
        data = _object(self._request("GET", OIDC_USERINFO_URL), "account response")
        self.state.user = {
            key: data[key]
            for key in ("sub", "name", "preferred_username", "email", "age", "age_category")
            if data.get(key) is not None
        }
        self._save()
        return self.state.user

    def pin_state(self) -> PinState:
        data = _object(
            self._request("GET", "settings/tvthek/pin", profile_api=True),
            "adult PIN status",
        )
        return PinState(exists=bool(data.get("exists")), enabled=bool(data.get("enabled")))

    def verify_pin(self, value: str) -> bool:
        data = self._request(
            "POST",
            "settings/tvthek/pin:verify",
            json_body={"value": value},
            profile_api=True,
        )
        if not isinstance(data, bool):
            raise OrfError("ORF adult PIN verification did not return true or false")
        return data

    def settings(self) -> dict[str, Any]:
        if self._settings is None:
            self._settings = _object(self._request("GET", "settings"), "settings response")
        return self._settings

    def video(self, kind: str, item_id: int) -> Video:
        if kind not in SUPPORTED_VIDEO_KINDS:
            raise OrfError(f"Unsupported ORF video type: {kind}")
        data = _object(self._request("GET", f"{kind}/{item_id}"), f"{kind} response")
        return _video(data, kind)

    def playback_video(self, video: Video) -> Video:
        prefix = _PLAYBACK_ID_PREFIXES.get(video.kind)
        if prefix is None:
            raise OrfError(f"Unsupported ORF playback type: {video.kind}")
        encrypted_id = video.encrypted_id or base64.b64encode(
            f"{prefix}{video.id}".encode()
        ).decode("ascii")
        data = _object(
            self._request("GET", f"public/{video.kind}/encrypted/{quote(encrypted_id, safe='')}"),
            "public playback response",
        )
        resolved = _video(data, video.kind)
        if resolved.id != video.id:
            raise OrfError("ORF public playback returned a different video ID")
        return resolved

    def profile(self, profile_id: int) -> Profile:
        data = _object(self._request("GET", f"profile/{profile_id}"), "profile response")
        return _profile(data)

    def episode_profile(self, episode: Video) -> Profile:
        if episode.profile is not None:
            return episode.profile
        if not episode.profile_url:
            raise OrfError("ORF series episode omitted its profile link")
        return _profile(
            _object(self._request("GET", episode.profile_url), "profile response")
        )

    def segment_episode(self, segment: Video) -> Video:
        if not segment.episode_url:
            raise OrfError("ORF segment omitted its episode link")
        return _video(
            _object(self._request("GET", segment.episode_url), "episode response"),
            "episode",
        )

    def profile_episodes(self, profile: Profile) -> list[Video]:
        first = _object(
            self._request("GET", profile.episodes_url, params={"page": 1, "limit": 100}),
            "profile episodes response",
        )
        pages = _integer(first.get("pages"), "profile page count")
        if pages < 1 or pages > MAX_PROFILE_PAGES:
            raise OrfError(f"ORF profile page count is outside 1-{MAX_PROFILE_PAGES}")
        payloads = [first]
        for page in range(2, pages + 1):
            payloads.append(
                _object(
                    self._request(
                        "GET",
                        profile.episodes_url,
                        params={"page": page, "limit": 100},
                    ),
                    f"profile episodes page {page}",
                )
            )

        found: list[Video] = []
        seen: set[int] = set()
        for payload in payloads:
            embedded = _object(payload.get("_embedded"), "profile episodes embedded data")
            for raw in _list(embedded.get("items"), "profile episode items"):
                video = _video(_object(raw, "profile episode"), "episode")
                if video.id in seen:
                    continue
                seen.add(video.id)
                found.append(video)
        return found

    def related_profiles(self, profile: Profile) -> list[Profile]:
        if not profile.related_url:
            return []
        values = _list(self._request("GET", profile.related_url), "related profiles response")
        return [_profile(_object(item, "related profile"), "related profile") for item in values]

    @staticmethod
    def _search_hit(raw: dict[str, Any], group: str) -> SearchHit:
        video = _video(raw)
        if group == "history":
            type_label = "Archive"
        elif video.content_kind == "film":
            type_label = "Film"
        elif video.content_kind == "series":
            type_label = "Series"
        else:
            type_label = "Episode" if video.kind == "episode" else "Segment"
        return SearchHit(
            id=video.id,
            kind=video.kind,
            title=video.title.replace("^^^^^", "").replace("$$$$$", ""),
            type_label=type_label,
            year=video.production_year,
            archive=video.archive or group == "history",
        )

    def search(self, query: str) -> list[SearchHit]:
        data = _object(
            self._request(
                "GET",
                f"search/{quote(query, safe='')}",
                params={"page": 1, "limit": 20},
            ),
            "search response",
        )
        search_groups = _object(data.get("search"), "search groups")
        found: list[SearchHit] = []
        seen: set[tuple[str, int]] = set()
        for group in ("episodes", "segments", "history"):
            group_data = _object(search_groups.get(group), f"search {group} group")
            for raw in _list(group_data.get("items"), f"search {group} items"):
                hit = self._search_hit(_object(raw, f"search {group} item"), group)
                key = (hit.kind, hit.id)
                if key in seen:
                    continue
                seen.add(key)
                found.append(hit)
        if found:
            return found

        suggestion_groups = _object(data.get("suggestions"), "search suggestion groups")
        for group in ("episodes", "segments", "history"):
            for raw in _list(
                suggestion_groups.get(group),
                f"search {group} suggestions",
            ):
                hit = self._search_hit(_object(raw, f"search {group} suggestion"), group)
                key = (hit.kind, hit.id)
                if key in seen:
                    continue
                seen.add(key)
                found.append(hit)
        return found

    def live_channels(self) -> list[Channel]:
        data = _object(
            self._request(
                "GET", "page/start/orflive", params={"channels": ",".join(LIVE_CHANNEL_NAMES)}
            ),
            "live response",
        )
        values = _object(data.get("timeShift"), "live timeShift channels")
        channels: list[Channel] = []
        for key, raw in values.items():
            if raw is None:
                continue
            item = _object(raw, f"live channel {key}")
            channel_id = _integer(item.get("id"), f"live channel {key} ID")
            current = str(item.get("title") or "").strip()
            playback_url = _link(item)
            if key not in LIVE_CHANNEL_NAMES or not current or not playback_url:
                raise OrfError(f"ORF live channel {key} is not supported or is incomplete")
            channels.append(Channel(channel_id, LIVE_CHANNEL_NAMES[key], current, playback_url))
        return channels

    def live_video(self, channel: Channel) -> Video:
        data = _object(self._request("GET", channel.playback_url), "live playback response")
        return _video(data, "livestream")

    def source(self, video: Video, *, source_tier: str = "uhd", live: bool = False) -> Source:
        tier = normalize_source_tier(source_tier)
        stream, multikey = _select_stream(video, tier, live=live)
        manifest_url = stream.url
        endpoint = ""
        brand = ""
        token = ""
        if live and stream.multikey:
            token = video.drm_token_multikey
            if not token:
                raise OrfError("ORF protected live playback omitted its multikey token")
            brand = DRM_BRAND_MULTIKEY_LIVE
        elif not live and video.drm_token_multikey:
            token = video.drm_token_multikey
            brand = DRM_BRAND_MULTIKEY_VOD
            if multikey:
                manifest_url = _append_query(manifest_url, drm="MULTIKEY")
        elif video.drm_token:
            token = video.drm_token
            brand = DRM_BRAND_STANDARD
        encrypted = bool(token or stream.encrypted)
        if encrypted and not token:
            raise OrfError("ORF protected playback omitted its user token")
        if encrypted:
            endpoint = WIDEVINE_LICENSE_URL
        return Source(
            manifest_url=manifest_url,
            protocol=stream.protocol,
            quality_key=stream.quality_key,
            quality_description=stream.quality_description,
            encrypted=encrypted,
            live=live,
            license_endpoint=endpoint,
            brand_guid=brand,
            user_token=token,
        )

    def subtitle(
        self,
        url: str,
        *,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise OrfError("ORF returned an invalid subtitle URL")
        try:
            with self.http.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/vtt"},
                timeout=REQUEST_TIMEOUT,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    raise OrfError(f"ORF subtitle request failed with HTTP {response.status_code}")
                size = response.headers.get("Content-Length", "")
                total = int(size) if size.isdigit() else None
                content = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    content.extend(chunk)
                    if progress is not None:
                        progress(len(content), total)
        except requests.RequestException as exc:
            raise OrfError("Could not download the ORF subtitle") from exc
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise OrfError("ORF subtitle is not UTF-8 WebVTT") from exc
        if not text.startswith("WEBVTT"):
            raise OrfError("ORF subtitle response is not WebVTT")
        return bytes(content)

    def license(
        self,
        challenge: bytes,
        endpoint: str,
        brand_guid: str,
        user_token: str,
    ) -> bytes:
        if not endpoint or not brand_guid or not user_token:
            raise OrfError("ORF Widevine request is missing license context")
        try:
            response = self.http.post(
                endpoint,
                params={"BrandGuid": brand_guid, "userToken": unquote(user_token)},
                data=challenge,
                headers={
                    "Accept": "*/*",
                    "Content-Type": "application/octet-stream",
                    "User-Agent": USER_AGENT,
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise OrfError(f"Could not reach the ORF Widevine service: {exc}") from exc
        if response.status_code >= 400:
            raise self._error(response, "ORF Widevine license request")
        if not response.content:
            raise OrfError("ORF Widevine service returned an empty license")
        return bytes(response.content)


__all__ = [
    "AuthenticationRequired",
    "Channel",
    "DeviceChallenge",
    "OrfApi",
    "OrfError",
    "ParsedReference",
    "PinState",
    "Profile",
    "SearchHit",
    "Session",
    "Source",
    "Stream",
    "Video",
    "equivalent_full_segment",
    "normalize_login_mode",
    "normalize_source_tier",
    "parse_reference",
]
