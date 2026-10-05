"""SoundCloud TV catalogue, pairing and audio-delivery contracts."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import requests

API_BASE = "https://api-mobile.soundcloud.com"
PAIRING_API_BASE = "https://api-reg.soundcloud.com"
OAUTH_TOKEN_URL = "https://api-auth.soundcloud.com/oauth/token"
WIDEVINE_LICENSE_URL = "https://license.media-streaming.soundcloud.cloud/playback/widevine"
CLIENT_ID = "nvCx0RFcGkeJvu3OX4Eu1ttc8j43WmQR"
APP_VERSION = "2026.08.03-tv-release"
APP_VERSION_CODE = "367037"
PAIRING_SCOPE = ""
PAIRING_DEVICE_TYPE = "unknown device"
USER_AGENT = f"SoundCloud/{APP_VERSION} (Android 12.0.0; {PAIRING_DEVICE_TYPE})"
ACTIVATION_URL = "https://secure.soundcloud.com/activate"
TOKEN_FILE = "soundcloud_token.json"

_HLS_ATTRIBUTE = re.compile(
    r'(?:^|,)\s*(?P<name>[A-Za-z0-9-]+)\s*=\s*(?:"(?P<quoted>[^"]*)"|(?P<bare>[^,]*))'
)


class SoundCloudError(RuntimeError):
    """A SoundCloud API or media contract failed."""


class SoundCloudAuthError(SoundCloudError):
    """The SoundCloud account is absent, expired or unauthorized."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _value(value: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in value and value[name] is not None:
            return value[name]
    return default


def _urn_id(value: str) -> str:
    return _text(value).rsplit(":", 1)[-1]


def _urn_kind(value: str) -> str:
    parts = _text(value).casefold().split(":")
    if len(parts) < 3 or parts[0] != "soundcloud":
        return ""
    return {
        "tracks": "track",
        "playlists": "playlist",
        "system-playlists": "playlist",
        "users": "user",
    }.get(parts[1], "")


def _date(value: Any) -> str:
    raw = _text(value)
    match = re.match(r"^(\d{4})[/-](\d{2})[/-](\d{2})", raw)
    return "-".join(match.groups()) if match else raw


def _next_href(raw: Any) -> str:
    links = _mapping(_mapping(raw).get("_links"))
    next_value = links.get("next")
    if isinstance(next_value, dict):
        return _text(next_value.get("href"))
    return _text(next_value)


def artwork_url(value: Any, size: str = "t500x500") -> str:
    """Resolve SoundCloud's image template without rewriting fixed URLs."""
    raw = _text(value)
    return raw.replace("{size}", size) if raw else ""


@dataclass
class TokenState:
    access_token: str = ""
    refresh_token: str = ""
    device_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_urn: str = ""
    username: str = ""
    permalink: str = ""

    @classmethod
    def from_cache(cls, raw: dict[str, Any] | None) -> TokenState:
        value = raw if isinstance(raw, dict) else {}
        return cls(
            access_token=_text(value.get("access_token")),
            refresh_token=_text(value.get("refresh_token")),
            device_id=_text(value.get("device_id")) or str(uuid.uuid4()),
            user_urn=_text(value.get("user_urn")),
            username=_text(value.get("username")),
            permalink=_text(value.get("permalink")),
        )

    def to_cache(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "device_id": self.device_id,
            "user_urn": self.user_urn,
            "username": self.username,
            "permalink": self.permalink,
        }

    @property
    def logged_in(self) -> bool:
        return bool(self.access_token)

    def clear_session(self) -> None:
        self.access_token = ""
        self.refresh_token = ""
        self.user_urn = ""
        self.username = ""
        self.permalink = ""


@dataclass(frozen=True)
class PairingChallenge:
    code: str
    poll_token: str
    interval: float = 5.0
    expires_in: int = 600
    verification_uri: str = ACTIVATION_URL


@dataclass(frozen=True)
class ParsedTarget:
    kind: str
    id: str


@dataclass(frozen=True)
class Transcoding:
    url: str
    preset: str
    quality: str
    protocol: str
    mime_type: str
    snipped: bool = False
    stream_type: str = "stream"
    file_size: int = 0

    @property
    def codec(self) -> str:
        mime = self.mime_type.casefold()
        match = re.search(r"codecs?\s*=\s*[\"']?([^\"';,]+)", mime)
        if match:
            return match.group(1).strip()
        if "mpeg" in mime or self.preset.startswith("mp3"):
            return "mp3"
        if "opus" in mime or "opus" in self.preset:
            return "opus"
        if "aac" in mime or self.preset.startswith("aac"):
            return "aac"
        return ""

    @property
    def quality_label(self) -> str:
        preset = self.preset.casefold()
        match = re.search(r"(?:aac|mp3|opus)[_-]?(\d{2,3})k", preset)
        codec = "AAC" if "aac" in preset else "Opus" if "opus" in preset else "MP3" if "mp3" in preset else "Audio"
        rate = f" {match.group(1)} Kbps" if match else ""
        tier = {"hq": "High", "sq": "Standard", "lq": "Low"}.get(self.quality.casefold(), self.quality)
        return f"{codec}{rate}" + (f" · {tier}" if tier else "")


@dataclass(frozen=True)
class Track:
    urn: str
    title: str
    artist: str = ""
    uploader: str = ""
    album: str = ""
    album_artist: str = ""
    duration: float = 0.0
    release_date: str = ""
    track_number: int = 0
    track_total: int = 0
    genre: str = ""
    publisher: str = ""
    copyright: str = ""
    isrc: str = ""
    composer: str = ""
    description: str = ""
    cover_url: str = ""
    permalink_url: str = ""
    explicit: bool = False
    blocked: bool = False
    snipped: bool = False
    transcodings: tuple[Transcoding, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def id(self) -> str:
        return _urn_id(self.urn)

    @property
    def label(self) -> str:
        number = f"{self.track_number:02d}  " if self.track_number else ""
        return f"{number}{self.title}"

    @property
    def has_full_stream(self) -> bool:
        return any(not item.snipped and item.stream_type in {"", "stream"} for item in self.transcodings)


@dataclass(frozen=True)
class Playlist:
    urn: str
    title: str
    creator: str = ""
    description: str = ""
    release_date: str = ""
    cover_url: str = ""
    genre: str = ""
    track_count: int = 0
    is_album: bool = False
    set_type: str = ""
    permalink_url: str = ""
    tracks: tuple[Track, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def id(self) -> str:
        return _urn_id(self.urn)

    @property
    def kind(self) -> str:
        return "album" if self.is_album else "playlist"


@dataclass(frozen=True)
class User:
    urn: str
    username: str
    permalink: str = ""
    description: str = ""
    city: str = ""
    avatar_url: str = ""
    verified: bool = False
    tracks_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def id(self) -> str:
        return _urn_id(self.urn)


@dataclass(frozen=True)
class SearchHit:
    kind: str
    urn: str
    title: str
    detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def id(self) -> str:
        return _urn_id(self.urn)


@dataclass(frozen=True)
class HomeSection:
    title: str
    items: tuple[SearchHit, ...]


@dataclass(frozen=True)
class MediaSource:
    url: str
    preset: str
    quality: str
    protocol: str
    mime_type: str
    codec: str
    quality_label: str
    authorization_required: bool = False
    transcoding_url: str = ""
    license_auth_token: str = ""


def parse_target(value: str) -> ParsedTarget:
    raw = _text(value)
    kind = _urn_kind(raw)
    if kind:
        return ParsedTarget(kind, raw)
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise SoundCloudError("Enter a complete SoundCloud URL or SoundCloud URN")
    host = (parsed.hostname or "").casefold()
    if host not in {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"}:
        raise SoundCloudError("That URL is not a SoundCloud link")
    if not parsed.path.strip("/"):
        raise SoundCloudError("The SoundCloud URL does not name a track, album, playlist or profile")
    return ParsedTarget("resolve", raw)


def _transcoding_from_json(raw: Any) -> Transcoding | None:
    value = _mapping(raw)
    format_value = _mapping(value.get("format"))
    url = _text(value.get("url"))
    if not url:
        return None
    return Transcoding(
        url=url,
        preset=_text(value.get("preset")),
        quality=_text(value.get("quality")).casefold(),
        protocol=_text(format_value.get("protocol") or value.get("protocol")).casefold(),
        mime_type=_text(_value(format_value, "mime_type", "mimeType") or _value(value, "mime_type", "mimeType")),
        snipped=bool(value.get("snipped")),
        stream_type=_text(value.get("type")) or "stream",
        file_size=_int(_value(value, "file_size", "fileSize")),
    )


def track_from_json(
    raw: Any,
    *,
    album: Playlist | None = None,
    index: int = 0,
    total: int = 0,
) -> Track:
    value = _mapping(raw)
    urn = _text(value.get("urn"))
    title = _text(value.get("title") or value.get("name"))
    if not urn or _urn_kind(urn) != "track" or not title:
        raise SoundCloudError("SoundCloud track response did not contain a track")
    embedded = _mapping(value.get("_embedded"))
    user = _mapping(embedded.get("user") or value.get("user"))
    publisher = _mapping(_value(value, "publisher_metadata", "publisherMetadata", "publisher"))
    uploader = _text(user.get("username") or user.get("display_name") or user.get("permalink"))
    artist = _text(publisher.get("artist")) or uploader
    media = _mapping(value.get("media"))
    raw_transcodings = _value(media, "transcodings", default=_value(value, "transcodings", default=[]))
    transcodings = tuple(
        parsed for item in _list(raw_transcodings) if (parsed := _transcoding_from_json(item)) is not None
    )
    template = _value(value, "artwork_url_template", "artworkUrlTemplate", "artwork_url", "artworkUrl")
    if not _text(template):
        template = _value(user, "avatar_url_template", "avatarUrlTemplate", "avatar_url", "avatarUrl")
    release_date = _date(
        _value(value, "release_date", "releaseDate", "published_at", "publishedAt", "created_at", "createdAt")
    )
    album_title = _text(_value(publisher, "album_title", "albumTitle", "release_title", "releaseTitle")) or (
        album.title if album else ""
    )
    album_artist = album.creator if album else ""
    full_duration = _int(_value(value, "full_duration", "fullDuration"))
    duration = full_duration or _int(value.get("duration"))
    explicit_value = _value(value, "is_explicit", "isExplicit", "explicit")
    return Track(
        urn=urn,
        title=title,
        artist=artist,
        uploader=uploader,
        album=album_title,
        album_artist=album_artist or artist,
        duration=duration / 1000 if duration else 0.0,
        release_date=(album.release_date if album and album.release_date else release_date),
        track_number=_int(_value(value, "track_number", "trackNumber"), index) or index,
        track_total=_int(_value(value, "track_count", "trackCount"), total) or total,
        genre=_text(value.get("genre") or (album.genre if album else "")),
        publisher=_text(_value(publisher, "publisher", "label_name", "labelName", "p_line", "pLine")),
        copyright=_text(_value(publisher, "c_line", "cLine", "copyright")),
        isrc=_text(_value(publisher, "isrc", "ISRC")),
        composer=_text(_value(publisher, "writer_composer", "writerComposer", "composer")),
        description=_text(value.get("description")),
        cover_url=artwork_url(template),
        permalink_url=_text(_value(value, "permalink_url", "permalinkUrl")),
        explicit=bool(explicit_value),
        blocked=bool(value.get("blocked")),
        snipped=bool(value.get("snipped")),
        transcodings=transcodings,
        raw=value,
    )


def playlist_from_json(raw: Any) -> Playlist:
    value = _mapping(raw)
    urn = _text(value.get("urn"))
    title = _text(value.get("title") or value.get("name"))
    if not urn or _urn_kind(urn) != "playlist" or not title:
        raise SoundCloudError("SoundCloud playlist response did not contain a playlist")
    embedded = _mapping(value.get("_embedded"))
    user = _mapping(embedded.get("user") or value.get("user"))
    creator = _text(user.get("username") or user.get("display_name") or value.get("user_urn"))
    is_album = bool(_value(value, "is_album", "isAlbum"))
    playlist_type = _text(_value(value, "playlist_type", "playlistType")).casefold()
    set_type = _text(_value(value, "set_type", "setType")).casefold()
    if playlist_type == "album" or set_type in {"album", "single", "ep", "compilation"}:
        is_album = True
    template = _value(value, "artwork_url_template", "artworkUrlTemplate", "artwork_url", "artworkUrl")
    return Playlist(
        urn=urn,
        title=title,
        creator=creator,
        description=_text(value.get("description")),
        release_date=_date(_value(value, "release_date", "releaseDate", "created_at", "createdAt")),
        cover_url=artwork_url(template),
        genre=_text(value.get("genre")),
        track_count=_int(_value(value, "track_count", "trackCount")),
        is_album=is_album,
        set_type=set_type,
        permalink_url=_text(_value(value, "permalink_url", "permalinkUrl")),
        raw=value,
    )


def user_from_json(raw: Any) -> User:
    value = _mapping(raw)
    urn = _text(value.get("urn"))
    username = _text(value.get("username") or value.get("display_name") or value.get("permalink"))
    if not urn or _urn_kind(urn) != "user" or not username:
        raise SoundCloudError("SoundCloud user response did not contain a profile")
    return User(
        urn=urn,
        username=username,
        permalink=_text(value.get("permalink")),
        description=_text(value.get("description")),
        city=_text(value.get("city")),
        avatar_url=artwork_url(_value(value, "avatar_url_template", "avatarUrlTemplate", "avatar_url", "avatarUrl")),
        verified=bool(value.get("verified")),
        tracks_count=_int(_value(value, "tracks_count", "tracksCount")),
        raw=value,
    )


def _hit(kind: str, raw: Any) -> SearchHit | None:
    value = _mapping(raw)
    if kind == "track":
        try:
            track = track_from_json(value)
        except SoundCloudError:
            return None
        detail = " · ".join(filter(None, (track.artist, track.genre)))
        return SearchHit("track", track.urn, track.title, detail, value)
    if kind in {"playlist", "album"}:
        try:
            playlist = playlist_from_json(value)
        except SoundCloudError:
            return None
        actual_kind = "album" if kind == "album" or playlist.is_album else "playlist"
        detail = " · ".join(filter(None, (playlist.creator, playlist.release_date[:4])))
        return SearchHit(actual_kind, playlist.urn, playlist.title, detail, value)
    if kind == "user":
        try:
            user = user_from_json(value)
        except SoundCloudError:
            return None
        detail = " · ".join(filter(None, ("Verified" if user.verified else "", user.city)))
        return SearchHit("user", user.urn, user.username, detail, value)
    return None


def _entity_hit(raw: Any, hinted_kind: str = "") -> SearchHit | None:
    value = _mapping(raw)
    kind = _text(value.get("type") or hinted_kind).casefold().replace("track_repost", "track")
    data = _mapping(value.get("data")) if "data" in value else value
    if kind in {"track", "playlist", "album", "user"}:
        nested = _mapping(data.get(kind))
        return _hit(kind, nested or data)
    for candidate in ("track", "playlist", "album", "user"):
        nested = _mapping(value.get(candidate))
        if nested:
            return _hit(candidate, nested)
    return _hit(_urn_kind(_text(data.get("urn"))), data)


def hits_from_json(raw: Any) -> tuple[SearchHit, ...]:
    payload = _mapping(raw)
    collection = _list(payload.get("collection"))
    if not collection and payload.get("urn"):
        collection = [payload]
    output: list[SearchHit] = []
    seen: set[tuple[str, str]] = set()

    def append(hit: SearchHit | None) -> None:
        if hit is None or (hit.kind, hit.urn) in seen:
            return
        seen.add((hit.kind, hit.urn))
        output.append(hit)

    for item in collection:
        value = _mapping(item)
        top_user = _mapping(value.get("top_result_user"))
        if top_user:
            append(_hit("user", top_user.get("user")))
            for related in _list(top_user.get("items")):
                append(_entity_hit(related))
            continue
        append(_entity_hit(value))
    return tuple(output)


def home_sections_from_json(raw: Any) -> tuple[HomeSection, ...]:
    payload = _mapping(raw)
    entities = _mapping(payload.get("entities"))
    output: list[HomeSection] = []
    for index, raw_section in enumerate(_list(payload.get("sections")), 1):
        section = _mapping(raw_section)
        data = _mapping(section.get("data"))
        title = _text(data.get("title") or section.get("title")) or f"Recommendations {index}"
        urns = _list(data.get("results"))
        if data.get("result"):
            urns.insert(0, data["result"])
        hits: list[SearchHit] = []
        seen: set[tuple[str, str]] = set()
        for urn_value in urns:
            urn = _text(urn_value)
            entity = entities.get(urn)
            hit = _entity_hit(entity, _urn_kind(urn)) if entity is not None else None
            if hit is not None and (hit.kind, hit.urn) not in seen:
                seen.add((hit.kind, hit.urn))
                hits.append(hit)
        if hits:
            output.append(HomeSection(title, tuple(hits)))
    if output:
        return tuple(output)
    direct = hits_from_json(payload)
    return (HomeSection("Recommendations", direct),) if direct else ()


def _quality_rank(value: str, requested: str) -> int:
    quality = value.casefold()
    wanted = requested.casefold()
    if wanted in {"best", "high", "hq", "auto"}:
        order = ("hq", "sq", "lq", "")
    elif wanted in {"low", "lq"}:
        order = ("lq", "sq", "hq", "")
    else:
        order = ("sq", "lq", "hq", "")
    try:
        return order.index(quality)
    except ValueError:
        return len(order)


def _preset_rank(value: str, requested: str) -> int:
    preset = value.casefold()
    wanted = requested.casefold()
    if wanted in {"best", "high", "hq", "auto"}:
        order = ("aac_256k", "aac_160k", "aac_96k")
    elif wanted in {"low", "lq"}:
        order = ("aac_96k", "aac_160k", "aac_256k")
    else:
        order = ("aac_160k", "aac_96k", "aac_256k")
    for index, marker in enumerate(order):
        if preset == marker:
            return index
    if "aac" in preset:
        return 10
    if "opus" in preset:
        return 11
    if "mp3" in preset:
        return 12
    return 20


def select_transcoding(track: Track, quality: str) -> Transcoding:
    full = [
        item
        for item in track.transcodings
        if not item.snipped
        and item.stream_type in {"", "stream"}
        and (item.protocol == "progressive" or "hls" in item.protocol)
    ]
    if not full:
        if track.transcodings or track.snipped:
            raise SoundCloudError(
                f"{track.title} has no full-length stream for this account; a subscription or different region may be required"
            )
        raise SoundCloudError(f"{track.title} returned no playable audio transcodings")
    return min(
        full,
        key=lambda item: (
            _quality_rank(item.quality, quality),
            _preset_rank(item.preset, quality),
            0 if "hls" in item.protocol else 1,
            0 if not item.preset.startswith("abr_") else 1,
            -item.file_size,
        ),
    )


class SoundCloudApi:
    def __init__(self, session: requests.Session, state: TokenState, *, save_state=None) -> None:
        self.session = session
        self.state = state
        self.save_state = save_state

    def _headers(self, *, auth: bool = True) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "App-Version": APP_VERSION_CODE,
            "App-Locale": "en",
            "App-Environment": "production",
            "UDID": self.state.device_id,
        }
        if auth and self.state.access_token:
            headers["Authorization"] = f"OAuth {self.state.access_token}"
        return headers

    def _pairing_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
            "App-Version": APP_VERSION_CODE,
            "UDID": self.state.device_id,
        }

    def media_headers(self, source: MediaSource) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT}
        if source.authorization_required and self.state.access_token:
            headers["Authorization"] = f"OAuth {self.state.access_token}"
        return headers

    def _save(self) -> None:
        if self.save_state:
            self.save_state(self.state)

    def _expire_login(self) -> None:
        self.state.clear_session()
        self._save()

    @staticmethod
    def _error(response: requests.Response, label: str) -> SoundCloudError:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = {}
        value = _mapping(payload)
        errors = value.get("errors")
        first_error = _mapping(errors[0]) if isinstance(errors, list) and errors else {}
        message = _text(
            value.get("message") or value.get("error_description") or value.get("error") or first_error.get("message")
        )
        detail = f": {message}" if message else ""
        kind = SoundCloudAuthError if response.status_code in {401, 403} else SoundCloudError
        return kind(f"{label} failed: HTTP {response.status_code}{detail}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
        pairing: bool = False,
        retry_auth: bool = True,
        error_label: str | None = None,
    ) -> Any:
        query = {**(params or {})}
        query.setdefault("client_id", CLIENT_ID)
        base_url = PAIRING_API_BASE if pairing else API_BASE
        try:
            response = self.session.request(
                method.upper(),
                f"{base_url}/{path.lstrip('/')}",
                params=query,
                json=json_body,
                headers=self._pairing_headers() if pairing else self._headers(auth=auth),
                timeout=30,
            )
        except requests.RequestException as exc:
            raise SoundCloudError(f"SoundCloud {path} could not be reached: {exc}") from exc
        if response.status_code == 401 and auth:
            if retry_auth and self.state.refresh_token:
                try:
                    self.refresh()
                except SoundCloudAuthError as exc:
                    self._expire_login()
                    raise SoundCloudAuthError(
                        "SoundCloud authorization expired; sign in again with a new pairing code"
                    ) from exc
                return self._request(
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    auth=auth,
                    pairing=pairing,
                    retry_auth=False,
                    error_label=error_label,
                )
            self._expire_login()
            raise SoundCloudAuthError("SoundCloud authorization expired; sign in again with a new pairing code")
        if response.status_code >= 400:
            raise self._error(response, error_label or f"SoundCloud {path}")
        try:
            return response.json()
        except ValueError as exc:
            raise SoundCloudError(f"SoundCloud {path} returned invalid JSON") from exc

    # ---------------------------------------------------------------- auth
    def begin_pairing(self) -> PairingChallenge:
        payload = _mapping(
            self._request(
                "POST",
                "pairing/codes",
                json_body={
                    "device": {
                        "id": self.state.device_id,
                        "type": f"television#{PAIRING_DEVICE_TYPE}",
                        "name": "",
                    }
                },
                auth=False,
                pairing=True,
                error_label="SoundCloud pairing-code request",
            )
        )
        code = _text(payload.get("code"))
        poll_token = _text(_value(payload, "poll_token", "pollToken"))
        interval = _int(_value(payload, "poll_interval_seconds", "pollIntervalSeconds"), 5)
        if not code or not poll_token:
            raise SoundCloudAuthError("SoundCloud TV-code request returned an incomplete challenge")
        return PairingChallenge(
            code,
            poll_token,
            max(float(interval), 1.0),
            verification_uri=f"{ACTIVATION_URL}/{quote(code, safe='')}",
        )

    def poll_pairing(self, challenge: PairingChallenge) -> TokenState | None:
        activation = _mapping(
            self._request(
                "GET",
                f"pairing/codes/{quote(challenge.code, safe='')}",
                params={"poll_token": challenge.poll_token},
                auth=False,
                pairing=True,
                error_label="SoundCloud pairing status",
            )
        )
        status = _text(activation.get("status")).upper()
        if status in {"CREATED", "PENDING", "WAITING"}:
            return None
        if status == "EXPIRED":
            raise SoundCloudAuthError("SoundCloud TV code expired; request a new code")
        if status != "ACTIVATED":
            raise SoundCloudAuthError(f"SoundCloud TV-code activation returned {status or 'no status'}")
        payload = _mapping(
            self._request(
                "POST",
                "pairing/sign-in",
                json_body={
                    "client_id": CLIENT_ID,
                    "pairing_code": challenge.code,
                    "poll_token": challenge.poll_token,
                    "scope": PAIRING_SCOPE,
                },
                auth=False,
                pairing=True,
                error_label="SoundCloud pairing token exchange",
            )
        )
        access_token = _text(payload.get("access_token"))
        refresh_token = _text(payload.get("refresh_token"))
        if not access_token:
            raise SoundCloudAuthError("SoundCloud TV-code sign-in returned no access token")
        if not refresh_token:
            raise SoundCloudAuthError("SoundCloud TV-code sign-in returned no refresh token")
        self.state.access_token = access_token
        self.state.refresh_token = refresh_token
        self._save()
        return self.state

    def refresh(self) -> TokenState:
        if not self.state.refresh_token:
            raise SoundCloudAuthError("SoundCloud has no refresh token; sign in again with a new pairing code")
        try:
            response = self.session.request(
                "POST",
                OAUTH_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.state.refresh_token,
                    "client_id": CLIENT_ID,
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": USER_AGENT,
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise SoundCloudError(f"SoundCloud token refresh could not be reached: {exc}") from exc
        if response.status_code >= 400:
            raise self._error(response, "SoundCloud token refresh")
        try:
            payload = _mapping(response.json())
        except (TypeError, ValueError) as exc:
            raise SoundCloudAuthError("SoundCloud token refresh returned invalid JSON") from exc
        access_token = _text(payload.get("access_token"))
        refresh_token = _text(payload.get("refresh_token"))
        if not access_token or not refresh_token:
            raise SoundCloudAuthError("SoundCloud token refresh returned incomplete tokens")
        self.state.access_token = access_token
        self.state.refresh_token = refresh_token
        self._save()
        return self.state

    def profile(self) -> User:
        user = user_from_json(self._request("GET", "me"))
        self.state.user_urn = user.urn
        self.state.username = user.username
        self.state.permalink = user.permalink
        self._save()
        return user

    # ------------------------------------------------------------- catalogue
    def resolve(self, target: ParsedTarget | str) -> SearchHit:
        parsed = parse_target(target) if isinstance(target, str) else target
        if parsed.kind == "resolve":
            payload = _mapping(self._request("GET", "resolve", params={"identifier": parsed.id}))
            hit = _entity_hit(payload)
            if hit is None:
                raise SoundCloudError("The SoundCloud URL did not resolve to a track, album, playlist or profile")
            return hit
        if parsed.kind == "track":
            track = self.track(parsed.id)
            return _hit("track", track.raw)  # type: ignore[return-value]
        if parsed.kind == "playlist":
            playlist = self.playlist(parsed.id)
            return _hit(playlist.kind, playlist.raw)  # type: ignore[return-value]
        if parsed.kind == "user":
            user = self.user(parsed.id)
            return _hit("user", user.raw)  # type: ignore[return-value]
        raise SoundCloudError(f"Unsupported SoundCloud target type: {parsed.kind}")

    def track(self, urn: str) -> Track:
        wanted = urn if _urn_kind(urn) == "track" else f"soundcloud:tracks:{urn}"
        payload = _mapping(self._request("POST", "tracks/fetch", json_body={"urns": [wanted]}))
        rows = _list(payload.get("collection"))
        if not rows:
            raise SoundCloudError(f"SoundCloud did not return track {wanted}")
        return track_from_json(rows[0])

    def playlist_summary(self, urn: str) -> Playlist:
        wanted = urn if _urn_kind(urn) == "playlist" else f"soundcloud:playlists:{urn}"
        payload = _mapping(self._request("POST", "playlists/fetch", json_body={"urns": [wanted]}))
        rows = _list(payload.get("collection"))
        if not rows:
            raise SoundCloudError(f"SoundCloud did not return playlist {wanted}")
        return playlist_from_json(rows[0])

    def playlist(self, urn: str) -> Playlist:
        wanted = urn if _urn_kind(urn) == "playlist" else f"soundcloud:playlists:{urn}"
        payload = _mapping(self._request("GET", f"playlists/{wanted}/info"))
        playlist = playlist_from_json(payload.get("playlist"))
        track_section = _mapping(payload.get("tracks"))
        rows = _list(track_section.get("collection") or payload.get("collection"))
        next_href = _next_href(track_section) or _next_href(payload)
        visited: set[str] = set()
        while next_href and next_href not in visited:
            visited.add(next_href)
            page = _mapping(self._request_href(next_href))
            page_tracks = _mapping(page.get("tracks")) or page
            rows.extend(_list(page_tracks.get("collection")))
            next_href = _next_href(page_tracks) or _next_href(page)
        total = playlist.track_count or len(rows)
        tracks: list[Track] = []
        for index, item in enumerate(rows, 1):
            try:
                tracks.append(track_from_json(item, album=playlist, index=index, total=total))
            except SoundCloudError:
                continue
        return replace(playlist, tracks=tuple(tracks), track_count=total)

    def user(self, urn: str) -> User:
        wanted = urn if _urn_kind(urn) == "user" else f"soundcloud:users:{urn}"
        payload = _mapping(self._request("POST", "users/fetch", json_body={"urns": [wanted]}))
        rows = _list(payload.get("collection"))
        if not rows:
            raise SoundCloudError(f"SoundCloud did not return profile {wanted}")
        return user_from_json(rows[0])

    def search(self, query: str, *, limit: int = 50) -> tuple[SearchHit, ...]:
        payload = self._request(
            "GET",
            "search/universal",
            params={"q": query, "top_results": "v2", "limit": min(max(limit, 1), 50)},
        )
        return hits_from_json(payload)

    def home_sections(self) -> tuple[HomeSection, ...]:
        payload = self._request(
            "GET",
            "home/query",
            params={
                "q": " ",
                "layout": "soundcloud:layouts:home-library",
                "version": "v10",
            },
        )
        return home_sections_from_json(payload)

    def library_items(self, category: str) -> tuple[SearchHit, ...]:
        paths = {
            "tracks": "you/posts_and_reposts/tracks",
            "playlists": "you/posts_and_reposts/playlists",
            "recent": "recently-played/tracks",
        }
        path = paths.get(category)
        if not path:
            raise SoundCloudError(f"Unsupported SoundCloud library category: {category}")
        return self._paged_hits(path, {"limit": 100})

    def user_items(self, user_urn: str, category: str) -> tuple[SearchHit, ...]:
        suffixes = {
            "tracks": "tracks/posted",
            "albums": "albums/posted",
            "playlists": "playlists/posted",
        }
        suffix = suffixes.get(category)
        if not suffix:
            raise SoundCloudError(f"Unsupported SoundCloud profile category: {category}")
        return self._paged_hits(f"users/{user_urn}/{suffix}", {"limit": 100})

    def _paged_hits(
        self,
        path: str,
        params: dict[str, Any],
    ) -> tuple[SearchHit, ...]:
        payload = _mapping(self._request("GET", path, params=params))
        output = list(hits_from_json(payload))
        seen = {(item.kind, item.urn) for item in output}
        next_href = _next_href(payload)
        visited: set[str] = set()
        while next_href and next_href not in visited:
            visited.add(next_href)
            page = _mapping(self._request_href(next_href))
            for item in hits_from_json(page):
                key = (item.kind, item.urn)
                if key not in seen:
                    seen.add(key)
                    output.append(item)
            next_href = _next_href(page)
        return tuple(output)

    def _request_href(self, href: str) -> Any:
        parsed = urlsplit(href)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() != urlsplit(API_BASE).netloc:
            raise SoundCloudError("SoundCloud pagination returned an unexpected API URL")
        return self._request(
            "GET",
            parsed.path,
            params=dict(parse_qsl(parsed.query, keep_blank_values=True)),
        )

    # --------------------------------------------------------------- playback
    def media_source(self, track: Track, quality: str) -> MediaSource:
        selected = select_transcoding(track, quality)
        url, auth_required, license_auth_token = self._resolve_stream_url(selected.url)
        return MediaSource(
            url=url,
            preset=selected.preset,
            quality=selected.quality,
            protocol=selected.protocol,
            mime_type=selected.mime_type,
            codec=selected.codec,
            quality_label=selected.quality_label,
            authorization_required=auth_required,
            transcoding_url=selected.url,
            license_auth_token=license_auth_token,
        )

    def resolve_license_token(self, transcoding_url: str) -> str:
        """Resolve the APK's stream redirect and return its license token."""
        _resolved, _auth_required, token = self._resolve_stream_url(
            transcoding_url,
            retry_auth=True,
        )
        if not token:
            raise SoundCloudError("SoundCloud audio redirect returned no license authorization token")
        return token

    def post_license(self, challenge: bytes, token: str) -> bytes:
        """POST a raw Widevine challenge using SoundCloud's APK contract."""
        if not isinstance(challenge, bytes):
            raise SoundCloudError("SoundCloud Widevine challenge must be bytes")
        token = _text(token)
        if not token:
            raise SoundCloudError("SoundCloud Widevine license token is missing")
        license_url = f"{WIDEVINE_LICENSE_URL}?{urlencode({'license_token': token})}"
        try:
            response = self.session.request(
                "POST",
                license_url,
                data=challenge,
                headers={
                    "Accept": "*/*",
                    "Content-Type": "application/octet-stream",
                    "User-Agent": USER_AGENT,
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise SoundCloudError(f"SoundCloud Widevine license could not be reached: {exc}") from exc
        if response.status_code >= 400:
            raise self._error(response, "SoundCloud Widevine license")
        content = getattr(response, "content", b"")
        if not isinstance(content, bytes) or not content:
            raise SoundCloudError("SoundCloud Widevine license returned an empty response")
        return content

    def fetch_hls_init_segment(
        self,
        manifest_url: str,
        *,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
    ) -> tuple[str, bytes]:
        """Fetch the init segment named by SoundCloud's HLS ``EXT-X-MAP``.

        SoundCloud puts the DRM PSSH in the MP4 init resource, not in the
        playlist text and not in a media ``.m4s`` segment. A multivariant
        response is followed only through its explicitly advertised child
        playlists until an ``EXT-X-MAP`` is found.
        """
        request_headers = {"User-Agent": USER_AGENT}
        request_headers.update(headers or {})
        proxies = {"http": proxy, "https": proxy} if proxy else None

        def get(url: str, *, byte_range: tuple[int, int] | None = None) -> bytes:
            request_headers_for_url = dict(request_headers)
            if byte_range:
                request_headers_for_url["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
            try:
                response = self.session.request(
                    "GET",
                    url,
                    headers=request_headers_for_url,
                    proxies=proxies,
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise SoundCloudError(f"SoundCloud HLS resource could not be reached: {exc}") from exc
            if response.status_code >= 400:
                raise self._error(response, "SoundCloud HLS resource")
            content = getattr(response, "content", b"")
            if not isinstance(content, bytes):
                content = bytes(content or b"")
            return content

        queue = [manifest_url]
        visited: set[str] = set()
        while queue:
            playlist_url = queue.pop(0)
            if not playlist_url or playlist_url in visited:
                continue
            visited.add(playlist_url)
            document = get(playlist_url).decode("utf-8-sig", errors="replace")
            references = _hls_init_references(playlist_url, document)
            if references:
                for init_url, byte_range in references:
                    init_data = get(init_url, byte_range=byte_range)
                    if init_data:
                        return init_url, init_data
                raise SoundCloudError("SoundCloud HLS EXT-X-MAP init segment was empty")
            queue.extend(_hls_variant_references(playlist_url, document))
        raise SoundCloudError("SoundCloud HLS response contains no EXT-X-MAP init URL")

    def _resolve_stream_url(
        self,
        url: str,
        *,
        retry_auth: bool = True,
    ) -> tuple[str, bool, str]:
        try:
            response = self.session.request(
                "GET",
                url,
                params={"client_id": CLIENT_ID},
                headers=self._headers(auth=True),
                timeout=30,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise SoundCloudError(f"SoundCloud audio URL could not be resolved: {exc}") from exc
        if response.status_code == 401:
            if retry_auth and self.state.refresh_token:
                try:
                    self.refresh()
                except SoundCloudAuthError as exc:
                    self._expire_login()
                    raise SoundCloudAuthError(
                        "SoundCloud authorization expired; sign in again with a new pairing code"
                    ) from exc
                return self._resolve_stream_url(url, retry_auth=False)
            self._expire_login()
            raise SoundCloudAuthError("SoundCloud authorization expired; sign in again with a new pairing code")
        if 300 <= response.status_code < 400:
            location = _text(response.headers.get("Location"))
            if location:
                return location, False, _header(response.headers, "x-sc-license-auth-token")
        if response.status_code >= 400:
            raise self._error(response, "SoundCloud audio URL")
        try:
            payload = _mapping(response.json())
        except (TypeError, ValueError):
            payload = {}
        resolved = _text(payload.get("url"))
        if resolved:
            return resolved, False, _header(response.headers, "x-sc-license-auth-token")
        response_url = _text(getattr(response, "url", ""))
        if response_url and not response_url.startswith(API_BASE):
            return response_url, False, _header(response.headers, "x-sc-license-auth-token")
        return _with_client_id(response_url or url), True, _header(response.headers, "x-sc-license-auth-token")


def _header(headers: Any, wanted: str) -> str:
    wanted = wanted.casefold()
    for key, value in dict(headers or {}).items():
        if str(key).casefold() == wanted:
            return _text(value)
    return ""


def _with_client_id(url: str) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("client_id", CLIENT_ID)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _hls_attribute(raw: str, name: str) -> str:
    wanted = name.casefold()
    for match in _HLS_ATTRIBUTE.finditer(raw):
        if match.group("name").casefold() == wanted:
            return _text(match.group("quoted") or match.group("bare"))
    return ""


def _hls_init_references(manifest_url: str, text: str) -> list[tuple[str, tuple[int, int] | None]]:
    """Return only the init-map references advertised by one HLS document."""
    references: list[tuple[str, tuple[int, int] | None]] = []
    seen: set[tuple[str, tuple[int, int] | None]] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line.upper().startswith("#EXT-X-MAP:"):
            continue
        attributes = line.split(":", 1)[1]
        uri = _hls_attribute(attributes, "URI")
        if not uri:
            continue
        byte_range: tuple[int, int] | None = None
        raw_range = _hls_attribute(attributes, "BYTERANGE")
        if raw_range:
            size_text, separator, offset_text = raw_range.partition("@")
            try:
                size = int(size_text)
                offset = int(offset_text) if separator else 0
            except ValueError:
                size = 0
                offset = 0
            if size > 0:
                byte_range = (offset, offset + size - 1)
        reference = (urljoin(manifest_url, uri), byte_range)
        if reference not in seen:
            seen.add(reference)
            references.append(reference)
    return references


def _hls_variant_references(manifest_url: str, text: str) -> list[str]:
    """Return child playlists from a multivariant HLS document."""
    variants: list[str] = []
    expect_uri = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("#EXT-X-STREAM-INF:"):
            expect_uri = True
            continue
        if expect_uri and not line.startswith("#"):
            uri = urljoin(manifest_url, line)
            if uri not in variants:
                variants.append(uri)
            expect_uri = False
    return variants


def widevine_pssh_from_soundcloud_init(data: bytes | bytearray) -> str | None:
    """Build the Widevine PSSH used by Core from SoundCloud's init MP4.

    SoundCloud's HLS init contains a version-1 PSSH with the content KID, but
    its system UUID is service-specific. Android's player maps that init data
    into its Widevine session; pywidevine needs the equivalent standard
    Widevine box, so preserve the KID and normalize only this service's box.
    """
    from ...core.pssh import bmff_boxes, from_key_ids

    key_ids: list[str] = []
    for box in bmff_boxes(bytes(data), b"pssh"):
        if len(box) < 32 or box[8] != 1:
            continue
        count = int.from_bytes(box[28:32], "big")
        end = 32 + count * 16
        if count <= 0 or end > len(box):
            continue
        for offset in range(32, end, 16):
            value = uuid.UUID(bytes=box[offset : offset + 16]).hex
            if value not in key_ids:
                key_ids.append(value)
    return from_key_ids(key_ids) if key_ids else None


__all__ = [
    "ACTIVATION_URL",
    "APP_VERSION",
    "APP_VERSION_CODE",
    "CLIENT_ID",
    "OAUTH_TOKEN_URL",
    "PAIRING_API_BASE",
    "PAIRING_DEVICE_TYPE",
    "PAIRING_SCOPE",
    "WIDEVINE_LICENSE_URL",
    "HomeSection",
    "MediaSource",
    "PairingChallenge",
    "ParsedTarget",
    "Playlist",
    "SearchHit",
    "SoundCloudApi",
    "SoundCloudAuthError",
    "SoundCloudError",
    "TOKEN_FILE",
    "TokenState",
    "Track",
    "Transcoding",
    "USER_AGENT",
    "User",
    "artwork_url",
    "hits_from_json",
    "home_sections_from_json",
    "parse_target",
    "playlist_from_json",
    "select_transcoding",
    "track_from_json",
    "user_from_json",
    "widevine_pssh_from_soundcloud_init",
]
