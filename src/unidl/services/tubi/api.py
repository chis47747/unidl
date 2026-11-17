"""Tubi Android TV"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter

ACCOUNT = "https://account.production-public.tubi.io"
CONTENT = "https://content-cdn.production-public.tubi.io/api/v2/content"
MOBILE_CONTENT = "https://content-cdn.production-public.tubi.io/api/v2/mobile/contents"
MOBILE_CONTENT_V1 = "https://content-cdn.production-public.tubi.io/api/v1/mobile/content"
SERIES_EPISODES = "https://content-cdn.production-public.tubi.io/api/v3/series/{series_id}/episodes"
SERIES_EPISODES_LEGACY = "https://content-cdn.production-public.tubi.io/cms/series/{series_id}/episodes"
SEARCH = "https://search.production-public.tubi.io/api/v3/search"
EPG = "https://tensor-cdn.production-public.tubi.io/api/v2/epg"
EPG_PROGRAMMING = "https://epg-cdn.production-public.tubi.io/content/epg/programming"

APP_ID = "tubitv"
APP_VERSION = "10.40.5000"
PLATFORM = "androidtv"
PLAYBACK_PLATFORM = "web"
EPG_PLATFORM = "android"
TV_UI_URL = "https://ott-androidtv.tubitv.com"
PAIRING_URL = "https://tubitv.com/activate"
PAIRING_INTERVAL = 2.0
PAIRING_TIMEOUT = 600.0

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; AFTKA Build/NS6265; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/124.0.6367.82 "
    "Safari/537.36 okhttp/4.12.0"
)

BASE_HEADERS = {
    "Content-Type": "application/json",
    "Origin": TV_UI_URL,
    "Referer": f"{TV_UI_URL}/",
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Version": "5.0.0",
    "x-tubi-platform": PLATFORM,
}

ATTACHMENT_HEADERS = {
    "Accept-Encoding": "identity",
    "User-Agent": USER_AGENT,
    "Referer": f"{TV_UI_URL}/",
}

PROGRAMMING_HEADERS = {
    "x-capability": '{"program_title_differ_with_episode_title": true}',
}

_DASH_RESOURCES = (
    "dash_widevine_nonclearlead",
    "dash_playready_psshv0",
    "dash",
)

RESOURCES = {
    "dash_h264": _DASH_RESOURCES,
    "dash_h265": _DASH_RESOURCES,
    "dash": _DASH_RESOURCES,
    "hls": (
        "hlsv6_widevine_psshv0",
        "hlsv6_widevine_nonclearlead",
        "hlsv6_playready_psshv0",
        "hlsv6",
    ),
}

DEFAULT_STREAM = "dash_h265"
STREAM_CODEC = {
    "dash_h264": "h264",
    "dash_h265": "h265",
    "dash": "h265",
}


def normalize_stream(value: str | None) -> str:
    stream = str(value or DEFAULT_STREAM).strip().casefold()
    if stream == "dash":
        return DEFAULT_STREAM
    return stream if stream in RESOURCES else DEFAULT_STREAM

RESOURCE_SYSTEM = {
    "dash_widevine_nonclearlead": "widevine",
    "hlsv6_widevine_psshv0": "widevine",
    "hlsv6_widevine_nonclearlead": "widevine",
    "dash_playready_psshv0": "playready",
    "hlsv6_playready_psshv0": "playready",
}

_SEARCH_KIND = {
    "v": "movie",
    "s": "series",
    "l": "channel",
    "se": "movie",
}

_URL_ID = re.compile(r"tubitv\.com/(?P<kind>movies|series|tv-shows)/(?P<id>\d+)", re.IGNORECASE)
_BARE_ID = re.compile(r"^\d{3,12}$")
_EPISODE_PREFIX = re.compile(r"^S\d+:E\d+\s*-\s*")
_HEIGHT = re.compile(r"(\d+)P")


class TubiError(RuntimeError):
    pass


@dataclass
class ParsedInput:
    kind: str  # "movie" | "series"
    value: str


def parse_input(text: str) -> ParsedInput | None:
    raw = (text or "").strip()
    if not raw:
        return None
    match = _URL_ID.search(raw)
    if match:
        kind = "series" if match.group("kind").lower() in ("series", "tv-shows") else "movie"
        return ParsedInput(kind, match.group("id"))
    if _BARE_ID.match(raw):
        return ParsedInput("movie", raw)
    return None


@dataclass
class Activation:

    code: str
    token: str


@dataclass
class Token:
    access_token: str = ""
    refresh_token: str = ""
    device_id: str = ""
    expires_at: float = 0.0
    user_id: str = ""
    email: str = ""
    name: str = ""
    account: bool = False

    @classmethod
    def from_cache(cls, cached: dict[str, Any] | None) -> Token:
        if not cached:
            return cls()
        expires_at = _seconds(cached.get("expires_at"))
        if not expires_at:
            fetched = _seconds(cached.get("fetched_at"))
            lifetime = _seconds(cached.get("expires_in"))
            expires_at = fetched + lifetime if fetched and lifetime else 0
        return cls(
            access_token=str(cached.get("access_token") or cached.get("token") or ""),
            refresh_token=str(cached.get("refresh_token") or ""),
            device_id=str(cached.get("device_id") or ""),
            expires_at=expires_at,
            user_id=str(cached.get("user_id") or ""),
            email=str(cached.get("email") or ""),
            name=str(cached.get("name") or ""),
            account=bool(
                cached.get("account")
                or cached.get("account_token")
                or cached.get("user_id")
                or cached.get("email")
                or cached.get("name")
            ),
        )

    @property
    def valid(self) -> bool:
        import time

        if not self.access_token or self.expires_at <= 0:
            return False
        return time.time() < self.expires_at - 60

    @property
    def refreshable(self) -> bool:
        return bool(self.refresh_token and self.device_id)

    @property
    def hours_left(self) -> int:
        import time

        return max(0, int((self.expires_at - time.time()) // 3600))


@dataclass
class Item:

    id: str
    name: str
    year: str = ""
    series: str = ""
    season: str = ""
    number: str = ""
    synopsis: str = ""
    kind: str = "movie"  # movie | episode | channel
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.season or self.number:
            return f"S{int(self.season or 0):02d}E{int(self.number or 0):02d}  {self.name}"
        return f"{self.name} ({self.year})" if self.year else self.name


@dataclass
class ChannelGroup:

    name: str
    ids: list[str] = field(default_factory=list)
    channels: list[Item] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.channels) if self.channels else len(self.ids)


@dataclass
class Subtitle:
    url: str
    language: str = "und"


@dataclass
class Source:

    manifest: str
    resource: str = ""
    codec: str = ""
    resolution: str = ""
    license_url: str = ""
    subtitles: list[Subtitle] = field(default_factory=list)

    @property
    def system(self) -> str | None:
        """Which DRM system this rendition was issued for, if any."""
        return RESOURCE_SYSTEM.get(self.resource)

    @property
    def encrypted(self) -> bool:
        return bool(self.license_url)

    @property
    def height(self) -> int:
        match = _HEIGHT.search((self.resolution or "").upper())
        return int(match.group(1)) if match else 0

    def line(self) -> str:
        bits = [self.codec or "?", self.resolution or "?"]
        bits.append("drm" if self.encrypted else "clear")
        return "  ".join(bits)


def _json(body: dict[str, Any]) -> str:
    return json.dumps(body, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.b64encode(digest).decode().replace("+", "-").replace("/", "_")


def sign(body: dict[str, Any], signing_key_b64: str, path: str) -> dict[str, str]:
    canonical = "POST\n" + path + "\n\ncontent-type:application/json\n\ncontent-type\n" + _sha256(_json(body))
    now = dt.datetime.now(dt.timezone.utc)
    date_string = now.strftime("%Y%m%dT%H%M%SZ")
    to_sign = f"TUBI-HMAC-SHA256\n{date_string}\n{_sha256(canonical)}"

    key = b"TUBI" + base64.b64decode(signing_key_b64)
    step = hmac.new(key, date_string[:8].encode(), hashlib.sha256).digest()
    step = hmac.new(step, b"tubi_request", hashlib.sha256).digest()
    signature = hmac.new(step, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Tubi-Algorithm": "TUBI-HMAC-SHA256",
        "X-Tubi-Date": date_string,
        "X-Tubi-Expires": "30",
        "X-Tubi-SignedHeaders": "content-type",
        "X-Tubi-Signature": signature,
    }



class TubiApi:

    def __init__(self, session: requests.Session | None = None, stream: str = DEFAULT_STREAM):
        self.session = session or requests.Session()
        self.session.headers.update(BASE_HEADERS)
        self.stream = normalize_stream(stream)
        self.token = Token()
        self.playback = Token()

    def anonymous_token(self, device_id: str = "") -> Token:
        device_id = device_id or self.device_id
        data = self._anonymous_grant(PLATFORM, device_id)
        return self._take_token(data, device_id=device_id, account=False)

    def ensure_playback_token(self) -> Token | None:
        if self.playback.valid:
            return self.playback
        if not hasattr(self.session, "post"):
            return None
        device_id = self.playback.device_id or str(uuid.uuid4())
        try:
            data = self._anonymous_grant(PLAYBACK_PLATFORM, device_id)
            self.playback = self._token_from_response(data, device_id=device_id, account=False)
        except TubiError:
            return None
        return self.playback if self.playback.valid else None

    def start_pairing(self) -> Activation:
        if not self.token.valid:
            self.anonymous_token(self.device_id)
        data = self._post(
            "/user_device/code/generate",
            {"device_id": self.device_id, "platform": PLATFORM},
            authorization=f"Bearer {self.token.access_token}",
        )
        code = str(data.get("activation_code") or "").strip()
        token = str(data.get("activation_token") or "").strip()
        if not code or not token:
            raise TubiError("Tubi did not issue a TV pairing code")
        return Activation(code=code, token=token)

    def poll_pairing(self, activation: Activation) -> Token | None:
        if not activation.token:
            raise TubiError("a Tubi TV pairing token is required")
        if not self.token.valid:
            self.anonymous_token(self.device_id)
        data = self._post(
            "/user_device/code/status",
            {
                "device_id": self.device_id,
                "platform": PLATFORM,
                "activation_token": activation.token,
            },
            authorization=f"Bearer {self.token.access_token}",
        )
        status = str(data.get("status") or "").strip().lower()
        if status == "expired":
            raise TubiError("that Tubi TV code expired")
        email = str(data.get("email") or "")
        name = str(data.get("name") or data.get("display_name") or "")
        if data.get("access_token") or data.get("token"):
            return self._take_token(data, device_id=self.device_id, account=True, email=email, name=name)
        if status == "registered" or data.get("user_id"):
            refresh_token = self.token.refresh_token
            if not refresh_token:
                raise TubiError("Tubi linked the device but did not issue an account token")
            refreshed = self._post(
                "/user_device/login/refresh",
                {"platform": PLATFORM, "device_id": self.device_id},
                authorization=f"Bearer {refresh_token}",
            )
            return self._take_token(
                refreshed,
                device_id=self.device_id,
                account=True,
                email=email,
                name=name,
                refresh_token=refresh_token,
            )
        return None

    def refresh(self) -> Token:
        if not self.token.refreshable:
            raise TubiError("the cached Tubi token has no usable refresh token")
        refresh_token = self.token.refresh_token
        if self.token.account:
            data = self._post(
                "/user_device/login/refresh",
                {"platform": PLATFORM, "device_id": self.device_id},
                authorization=f"Bearer {refresh_token}",
            )
            return self._take_token(
                data,
                device_id=self.device_id,
                account=True,
                email=self.token.email,
                name=self.token.name,
                refresh_token=refresh_token,
            )

        self.session.headers.pop("Authorization", None)
        verifier = uuid.uuid4().hex
        key = self._post(
            "/device/anonymous/signing_key",
            {
                "challenge": _challenge(verifier),
                "version": "1.0.0",
                "platform": PLATFORM,
                "device_id": self.device_id,
            },
        )
        if not key.get("key"):
            raise TubiError("Tubi did not issue a signing key for token refresh")
        body = {"platform": PLATFORM}
        data = self._post(
            "/device/anonymous/refresh",
            body,
            params=sign(body, str(key["key"]), "/device/anonymous/refresh"),
            authorization=f"Bearer {refresh_token}",
        )
        return self._take_token(
            data,
            device_id=self.device_id,
            account=False,
            refresh_token=refresh_token,
        )

    def _anonymous_grant(self, platform: str, device_id: str) -> dict[str, Any]:
        previous = self.session.headers.get("Authorization")
        try:
            self.session.headers.pop("Authorization", None)
            verifier = uuid.uuid4().hex
            key = self._post(
                "/device/anonymous/signing_key",
                {
                    "challenge": _challenge(verifier),
                    "version": "1.0.0",
                    "platform": platform,
                    "device_id": device_id,
                },
            )
            if not (key.get("id") and key.get("key")):
                raise TubiError("Tubi did not issue a signing key")
            body = {
                "verifier": verifier,
                "id": key["id"],
                "platform": platform,
                "device_id": device_id,
            }
            return self._post(
                "/device/anonymous/token",
                body,
                params=sign(body, key["key"], "/device/anonymous/token"),
            )
        finally:
            if previous:
                self.session.headers["Authorization"] = previous
            else:
                self.session.headers.pop("Authorization", None)

    def _token_from_response(
        self,
        data: dict[str, Any],
        *,
        device_id: str,
        account: bool,
        email: str = "",
        name: str = "",
        refresh_token: str = "",
        keep: Token | None = None,
    ) -> Token:
        access_token = str(data.get("access_token") or data.get("token") or "")
        if not access_token:
            raise TubiError("Tubi did not issue a token")
        import time

        expires_at = _seconds(data.get("expires_at"))
        if not expires_at:
            expires_at = time.time() + (_seconds(data.get("expires_in")) or 3600)
        previous = keep if keep is not None else Token()
        return Token(
            access_token=access_token,
            refresh_token=str(data.get("refresh_token") or refresh_token or ""),
            device_id=str(data.get("device_id") or device_id),
            expires_at=expires_at,
            user_id=str(data.get("user_id") or previous.user_id or ""),
            email=str(data.get("email") or email or previous.email or ""),
            name=str(data.get("name") or name or previous.name or ""),
            account=account,
        )

    def _take_token(
        self,
        data: dict[str, Any],
        *,
        device_id: str,
        account: bool,
        email: str = "",
        name: str = "",
        refresh_token: str = "",
    ) -> Token:
        self.token = self._token_from_response(
            data,
            device_id=device_id,
            account=account,
            email=email,
            name=name,
            refresh_token=refresh_token,
            keep=self.token,
        )
        self._apply_token()
        return self.token

    def adopt(self, cached: dict[str, Any] | None) -> bool:
        token = Token.from_cache(cached)
        self.token = token
        self.playback = Token.from_cache(_playback_cache(cached))
        if not token.valid:
            return False
        self._apply_token()
        return True

    def to_cache(self) -> dict[str, Any]:
        return {
            "access_token": self.token.access_token,
            "refresh_token": self.token.refresh_token,
            "device_id": self.token.device_id,
            "expires_at": self.token.expires_at,
            "user_id": self.token.user_id,
            "email": self.token.email,
            "name": self.token.name,
            "account": self.token.account,
            "account_token": self.token.account,
            "playback_access_token": self.playback.access_token,
            "playback_refresh_token": self.playback.refresh_token,
            "playback_device_id": self.playback.device_id,
            "playback_expires_at": self.playback.expires_at,
        }

    def _apply_token(self) -> None:
        self.session.headers["Authorization"] = f"Bearer {self.token.access_token}"

    @property
    def device_id(self) -> str:
        if not self.token.device_id:
            self.token.device_id = str(uuid.uuid4())
        return self.token.device_id

    def content(self, content_id: str, *, exclude_episodes: bool = True) -> dict[str, Any]:
        params = self._content_params(content_id, exclude_episodes=exclude_episodes, mobile=True)
        try:
            item = _unwrap(self._catalogue_get(MOBILE_CONTENT, params), content_id)
            if item.get("video_resources"):
                return item
        except TubiError:
            item = {}

        try:
            fallback = _unwrap(
                self._catalogue_get(CONTENT, self._content_params(content_id, exclude_episodes=exclude_episodes)),
                content_id,
            )
            if fallback.get("video_resources"):
                return fallback
            item = fallback or item
        except TubiError:
            pass

        try:
            v1 = _unwrap(
                self._catalogue_get(
                    MOBILE_CONTENT_V1,
                    [
                        ("platform", PLATFORM),
                        ("deviceId", self.device_id),
                        ("app_id", APP_ID),
                        ("content_id", content_id),
                        ("include_channels", "true"),
                        *[("video_resources[]", name) for name in RESOURCES[self.stream]],
                    ],
                ),
                content_id,
            )
            if v1.get("video_resources"):
                return v1
            return v1 or item
        except TubiError:
            return item

    def _content_params(
        self, content_id: str, *, exclude_episodes: bool, mobile: bool = False
    ) -> list[tuple[str, str]]:
        params: list[tuple[str, str]] = [
            ("app_id", APP_ID),
            ("platform", PLATFORM),
            ("device_id", self.device_id),
            ("deviceId", self.device_id),
            ("include_channels", "true"),
            ("exclude_episodes", "true" if exclude_episodes else "false"),
            ("limit_resolutions[]", "h264_1080p"),
            ("limit_resolutions[]", "h265_1080p"),
            *[("video_resources[]", name) for name in RESOURCES[self.stream]],
        ]
        params.append(("content_ids" if mobile else "content_id", content_id))
        return params

    def movie(self, content_id: str) -> tuple[Item, dict[str, Any]]:
        data = self.content(content_id)
        if not data:
            raise TubiError(f"Tubi has nothing at id {content_id}")
        return (
            Item(
                id=str(data.get("id") or content_id),
                name=str(data.get("title") or "Unknown"),
                year=str(data.get("year") or data.get("contentYear") or ""),
                synopsis=str(data.get("description") or ""),
                kind="movie",
                data=data,
            ),
            data,
        )

    def episodes(self, series_id: str) -> tuple[str, list[Item]]:
        index = self._series_index(series_id)
        seasons = _seasons(index.get("episodes_by_season"))
        if not seasons:
            raise TubiError(f"Tubi lists no episodes for series {series_id}")

        root = self.content(series_id, exclude_episodes=False)
        series_title = str(root.get("title") or "Unknown Series")
        detail = {
            str(entry.get("id")): entry
            for season in root.get("children") or []
            for entry in (season.get("children") or [])
            if isinstance(entry, dict)
        }

        found: list[Item] = []
        for season, entries in seasons:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                episode_id = str(entry.get("id") or "")
                if not episode_id:
                    continue
                info = detail.get(episode_id, {})
                raw_title = str(info.get("title") or entry.get("title") or "")
                found.append(
                    Item(
                        id=episode_id,
                        name=_EPISODE_PREFIX.sub("", raw_title).strip() or f"Episode {entry.get('num')}",
                        year=str(info.get("year") or root.get("year") or ""),
                        series=series_title,
                        season=str(season),
                        number=str(entry.get("num") or ""),
                        synopsis=str(info.get("description") or ""),
                        kind="episode",
                        data=info if isinstance(info, dict) else {},
                    )
                )
        return series_title, found

    def _series_index(self, series_id: str) -> dict[str, Any]:
        params = {"platform": PLATFORM, "device_id": self.device_id, "deviceId": self.device_id}
        try:
            return self._get(SERIES_EPISODES.format(series_id=series_id), params)
        except TubiError:
            return self._get(SERIES_EPISODES_LEGACY.format(series_id=series_id), params)

    def search(self, query: str, limit: int | None = None) -> list[Item]:
        data = self._get(
            SEARCH,
            {
                "search": query,
                "include_linear": "true",
                "include_channels": "true",
                "is_kids_mode": "false",
                "include_apps": "true",
                "feature_flags[include_coming_soon]": "true",
            },
        )
        raw = data.get("contents")
        if raw is None:
            raw = data.get("_list", data)
        entries = list(raw.values()) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        found: list[Item] = []
        seen: set[str] = set()
        for entry in entries:
            item = self._search_item(entry)
            if item is None or item.id in seen:
                continue
            seen.add(item.id)
            found.append(item)
        return found[:limit] if limit is not None and limit > 0 else found

    @staticmethod
    def _search_item(entry: Any) -> Item | None:
        if not isinstance(entry, dict) or not entry.get("id"):
            return None
        kind = _SEARCH_KIND.get(str(entry.get("type") or "").lower())
        if kind is None:
            return None
        return Item(
            id=str(entry["id"]),
            name=str(entry.get("title") or "Unknown"),
            year=str(entry.get("year") or entry.get("contentYear") or ""),
            synopsis=str(entry.get("description") or ""),
            kind=kind,
            data=entry,
        )

    @staticmethod
    def _channel_id(entry: dict[str, Any]) -> str:
        return str(entry.get("id") or entry.get("content_id") or entry.get("contentId") or "").strip()

    @staticmethod
    def _channel(entry: dict[str, Any]) -> Item | None:
        channel_id = TubiApi._channel_id(entry)
        if not channel_id:
            return None
        programmes = [value for value in (entry.get("programs") or []) if isinstance(value, dict)]
        current = programmes[0] if programmes else {}
        now = str(current.get("title") or "")
        episode = str(current.get("episode_title") or "")
        on_now = " · ".join(value for value in (now, episode) if value)
        return Item(
            id=channel_id,
            name=str(entry.get("title") or channel_id),
            synopsis=on_now or str(entry.get("description") or ""),
            kind="channel",
            data=entry,
        )

    def channel_groups(self) -> list[ChannelGroup]:
        data = self._get(
            EPG,
            {
                "mode": "tubitv_us_linear",
                "platform": EPG_PLATFORM,
                "device_id": self.device_id,
                "deviceId": self.device_id,
            },
        )
        contents = data.get("contents") or {}
        entries = list(contents.values()) if isinstance(contents, dict) else list(contents)
        local = [item for item in (self._channel(entry) for entry in entries if isinstance(entry, dict)) if item]
        groups: list[ChannelGroup] = []
        if local:
            groups.append(ChannelGroup("Local Channels", [item.id for item in local], local))
        for raw in data.get("containers") or []:
            if not isinstance(raw, dict):
                continue
            ids = [str(value) for value in (raw.get("contents") or []) if str(value)]
            if ids:
                groups.append(ChannelGroup(str(raw.get("name") or "Channels"), ids))
        return groups

    def channels(self, group: ChannelGroup | None = None) -> list[Item]:
        if group is None:
            groups = self.channel_groups()
            group = groups[0] if groups else None
        if group is None:
            return []
        if group.channels:
            return list(group.channels)
        params: list[tuple[str, str]] = [
            ("platform", PLATFORM),
            ("device_id", self.device_id),
            ("deviceId", self.device_id),
            ("limit_resolutions[]", "h264_1080p"),
            ("limit_resolutions[]", "h265_1080p"),
            ("lookahead", "1"),
            ("content_id", ",".join(group.ids)),
            *[("video_resources[]", name) for name in RESOURCES[self.stream]],
        ]
        data = self._catalogue_get(EPG_PROGRAMMING, params, headers=PROGRAMMING_HEADERS)

        containers = [data]
        containers.extend(
            value
            for key in ("data", "result", "response")
            if isinstance((value := data.get(key)), dict)
        )
        rows: list[dict[str, Any]] = []
        for container in containers:
            for key in ("rows", "contents", "channels", "items", "_list"):
                raw = container.get(key)
                values = list(raw.values()) if isinstance(raw, dict) else raw
                if not isinstance(values, list):
                    continue
                rows = [entry for entry in values if isinstance(entry, dict)]
                if rows:
                    break
            if rows:
                break

        entries: list[dict[str, Any]] = []
        for row in rows:
            entry = next(
                (
                    value
                    for key in ("content", "channel", "item")
                    if isinstance((value := row.get(key)), dict) and self._channel_id(value)
                ),
                row,
            )
            entries.append(entry)
        found = [item for item in (self._channel(entry) for entry in entries) if item]
        return sorted(found, key=lambda item: item.name.lower())

    def sources(self, data: dict[str, Any]) -> list[Source]:
        subtitles = [
            Subtitle(
                url=str(sub.get("url") or ""),
                language=str(sub.get("lang") or sub.get("lang_translation") or "und"),
            )
            for sub in data.get("subtitles") or []
            if sub.get("url")
        ]
        found: list[Source] = []
        for raw in data.get("video_resources") or []:
            manifest = ((raw.get("manifest") or {}).get("url")) or ""
            if not manifest:
                continue
            licence = (raw.get("license_server") or {}).get("url") or ""
            found.append(
                Source(
                    manifest=str(manifest),
                    resource=str(raw.get("type") or raw.get("resource") or ""),
                    codec=str(raw.get("codec") or ""),
                    resolution=str(raw.get("resolution") or ""),
                    license_url=str(licence),
                    subtitles=subtitles,
                )
            )
        found.sort(key=lambda source: (source.height, "H265" in source.codec.upper()), reverse=True)
        return found

    def chapters(self, data: dict[str, Any]) -> list[Chapter]:
        return chapters_from(data)

    def attachments(self, data: dict[str, Any]) -> list[Attachment]:
        return attachments_from(data)

    def subtitle(self, track: Subtitle) -> bytes:
        if not track.url:
            raise TubiError("Tubi subtitle has no URL")
        try:
            response = self.session.get(track.url, timeout=20)
        except requests.RequestException as exc:
            raise TubiError(f"could not reach Tubi's subtitle: {exc}") from exc
        if not response.ok:
            raise TubiError(f"Tubi's subtitle server returned {response.status_code}")
        return response.content

    def _catalogue_get(
        self, url: str, params: Any = None, headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        merged = dict(headers or {})
        token = self.ensure_playback_token()
        if token is not None:
            merged["Authorization"] = f"Bearer {token.access_token}"
        return self._get(url, params, headers=merged or None)

    def _get(self, url: str, params: Any = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            response = self.session.get(url, params=params, headers=headers, timeout=20)
        except requests.RequestException as exc:
            raise TubiError(f"could not reach Tubi: {exc}") from exc
        if response.status_code == 404:
            raise TubiError("Tubi has no such title")
        if response.status_code in (401, 403):
            raise TubiError(
                "Tubi refused that. It is US-only, so a non-US address is the "
                "usual reason; the device token may also have expired."
            )
        if response.status_code >= 400:
            raise TubiError(f"Tubi returned {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TubiError("Tubi returned something that is not JSON") from exc
        return payload if isinstance(payload, dict) else {"_list": payload}

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        params: dict[str, str] | None = None,
        authorization: str = "",
    ) -> dict[str, Any]:
        try:
            response = self.session.post(
                ACCOUNT + path,
                data=_json(body),
                params=params,
                headers={**BASE_HEADERS, **({"Authorization": authorization} if authorization else {})},
                timeout=20,
            )
        except requests.RequestException as exc:
            raise TubiError(f"could not reach Tubi: {exc}") from exc
        if response.status_code >= 400:
            raise TubiError(f"Tubi refused that request ({response.status_code})")
        try:
            return response.json()
        except ValueError as exc:
            raise TubiError("Tubi's token response was not JSON") from exc

    def widevine_license(self, license_url: str, challenge: bytes, headers: dict[str, str] | None = None) -> bytes:
        if not license_url:
            raise TubiError("no Tubi licence URL came with this playback")
        request_headers = {"Content-Type": "application/octet-stream"}
        request_headers.update(headers or {})
        try:
            response = self.session.post(license_url, data=challenge, headers=request_headers, timeout=30)
        except requests.RequestException as exc:
            raise TubiError(f"could not reach Tubi's licence server: {exc}") from exc
        if not response.ok:
            raise TubiError(f"Tubi's licence server answered {response.status_code}: {str(response.text)[:200]}")
        return response.content

    def playready_license(self, license_url: str, challenge: str, headers: dict[str, str] | None = None) -> str:
        if not license_url:
            raise TubiError("no Tubi licence URL came with this playback")
        request_headers = {"Content-Type": "text/xml; charset=utf-8"}
        request_headers.update(headers or {})
        try:
            response = self.session.post(
                license_url,
                data=challenge.encode("utf-8"),
                headers=request_headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise TubiError(f"could not reach Tubi's licence server: {exc}") from exc
        if not response.ok:
            raise TubiError(f"Tubi's licence server answered {response.status_code}: {str(response.text)[:200]}")
        return response.text


def _seconds(value: Any) -> float:
    if value in (None, "", False):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    try:
        return float(text)
    except ValueError:
        pass
    import datetime as dt

    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _playback_cache(cached: dict[str, Any] | None) -> dict[str, Any]:
    if not cached:
        return {}
    return {
        "access_token": cached.get("playback_access_token"),
        "refresh_token": cached.get("playback_refresh_token"),
        "device_id": cached.get("playback_device_id"),
        "expires_at": cached.get("playback_expires_at"),
    }


def _unwrap(payload: dict[str, Any], fallback_id: str) -> dict[str, Any]:
    if fallback_id:
        direct = payload.get(fallback_id)
        if isinstance(direct, dict) and ("video_resources" in direct or "title" in direct or "id" in direct):
            return direct
    contents = payload.get("contents")
    if isinstance(contents, dict):
        if fallback_id and isinstance(contents.get(fallback_id), dict):
            return contents[fallback_id]
        for value in contents.values():
            if isinstance(value, dict) and ("video_resources" in value or "title" in value):
                return value
    listed = payload.get("_list")
    if isinstance(listed, list):
        return listed[0] if listed else {}
    if "rows" in payload:
        rows = payload.get("rows") or []
        return rows[0] if rows else {}
    if "video_resources" in payload or "title" in payload:
        return payload
    for value in payload.values():
        if isinstance(value, dict) and ("video_resources" in value or "title" in value):
            return value
    return {}


def _seasons(by_season: Any) -> list[tuple[str, list[Any]]]:
    if isinstance(by_season, dict):
        def _key(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        return [(str(season), list(by_season[season] or [])) for season in sorted(by_season, key=_key)]
    if not isinstance(by_season, list):
        return []
    found: list[tuple[str, list[Any]]] = []
    for entry in by_season:
        if not isinstance(entry, dict):
            continue
        season = str(entry.get("season") or "")
        episodes = entry.get("episodes") or []
        if isinstance(episodes, list):
            found.append((season, episodes))
    return found


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def chapters_from(data: dict[str, Any] | None) -> list[Chapter]:
    if not isinstance(data, dict):
        return []
    raw = data.get("credit_cuepoints")
    if not isinstance(raw, dict):
        return []
    chapters: list[Chapter] = []

    def add_range(start_key: str, end_key: str, title: str, kind: str) -> None:
        start = _float(raw.get(start_key))
        end = _float(raw.get(end_key))
        if start is None or end is None or end <= start or start < 0:
            return
        try:
            chapters.append(Chapter.from_seconds(start, title, end, kind=kind))
        except ValueError:
            return

    add_range("intro_start", "intro_end", "Intro", "intro")
    add_range("recap_start", "recap_end", "Recap", "recap")
    add_range("earlycredits_start", "earlycredits_end", "Credits", "credits")
    has_credits = any(chapter.kind == "credits" for chapter in chapters)
    for key, title, kind in (("prologue", "Prologue", "prologue"), ("postlude", "Credits", "credits")):
        start = _float(raw.get(key))
        if start is None or start <= 0:
            continue
        if kind == "credits" and has_credits:
            continue
        try:
            chapters.append(Chapter.from_seconds(start, title, kind=kind))
        except ValueError:
            continue
        if kind == "credits":
            has_credits = True
    chapters.sort(key=lambda chapter: chapter.start_ms)
    return chapters


def _http_urls(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("http://", "https://")):
            found.append(text)
        return found
    if isinstance(value, list):
        for item in value:
            found.extend(_http_urls(item))
        return found
    if isinstance(value, dict):
        for item in value.values():
            found.extend(_http_urls(item))
    return found


def attachments_from(data: dict[str, Any] | None) -> list[Attachment]:
    if not isinstance(data, dict):
        return []
    fields: tuple[tuple[str, str, str], ...] = (
        ("posterarts", "poster", "Poster"),
        ("thumbnails", "thumbnail", "Thumbnail"),
        ("landscape_images", "landscape", "Landscape"),
        ("hero_images", "hero", "Hero"),
        ("backgrounds", "background", "Background"),
        ("first_frame_image", "still", "First frame"),
        ("images", "artwork", "Artwork"),
        ("series_images", "artwork", "Series art"),
    )
    found: list[Attachment] = []
    seen: set[str] = set()
    for key, kind, name in fields:
        for url in _http_urls(data.get(key)):
            if url in seen:
                continue
            seen.add(url)
            found.append(
                Attachment(
                    url=url,
                    name=name,
                    kind=kind,
                    headers=ATTACHMENT_HEADERS,
                )
            )
    return found


__all__ = [
    "ATTACHMENT_HEADERS",
    "PAIRING_INTERVAL",
    "PAIRING_TIMEOUT",
    "PAIRING_URL",
    "DEFAULT_STREAM",
    "PLAYBACK_PLATFORM",
    "PLATFORM",
    "RESOURCES",
    "STREAM_CODEC",
    "TV_UI_URL",
    "USER_AGENT",
    "Activation",
    "ChannelGroup",
    "Item",
    "ParsedInput",
    "Source",
    "Subtitle",
    "Token",
    "TubiApi",
    "TubiError",
    "attachments_from",
    "chapters_from",
    "normalize_stream",
    "parse_input",
    "sign",
]
