"""Play Belgium (GoPlay) Android TV API client."""

from __future__ import annotations

import base64
import json
import math
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlparse

import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter

APP_VERSION = "3.1.30"
VERSION_CODE = 306
PACKAGE_NAME = "be.goplay.app"
ANDROID_SDK = 34
DEVICE_NAME = "Google TV Streamer"
API_PLATFORM = "ANDROIDTV"
USER_AGENT = f"GoPlay/{VERSION_CODE} AndroidTV/{ANDROID_SDK}"

API_BASE_URL = "https://api.play.tv/tv/"
SITE_URL = "https://www.play.tv"
LOGIN_PAGE_URL = "https://login.play.tv/device/authorize"
DAI_VOD_URL = "https://dai.google.com/ondemand/v1/dash/content/{content_source_id}/vid/{video_id}/stream"
DRM_LICENSE_URL = "https://drm.play.tv?drm-type=widevine"
DRM_ALB_KEY = "prd-fa10dd3e027a715d35f24d4e2be5d8a699e2b024e42a1bc7d0300abe8eda6c80"

TOKEN_FILE = "goplay_token.json"
TOKEN_EXPIRY_SKEW = 300
PAGE_SIZE = 10
MAX_PAGES = 100

DEEP_LINK_HOSTS = frozenset(
    {
        "play.tv",
        "www.play.tv",
        "email.play.tv",
        "goplay.be",
        "www.goplay.be",
        "mail.goplay.be",
        "clicks.playmedia.be",
    }
)

HARDCODED_PAGES = {
    "": "home",
    "/": "home",
    "/zoeken": "search",
    "/programmas": "search",
    "/kijk-verder": "continue-watching",
    "/mijn-items": "my-goplay",
    "/mijn-lijst": "my-list",
}

BROWSE_PAGES = (
    ("home", "Home"),
    ("programs", "Programs"),
    ("play-sports", "Play Sports"),
    ("playkinepolis", "Kinepolis"),
)

LIBRARY_PAGES = (
    ("continue-watching", "Continue watching"),
    ("my-goplay", "My Play"),
    ("my-list", "My list"),
)

SKIP_LANE_TYPES = frozenset(
    {
        "BANNER",
        "BRAZE_CONTENT_CARD",
        "SPECIAL_CONTENT_BLOCK",
        "TRAILER_BANNER",
    }
)

GEO_ERROR_CODES = frozenset({"BELGIUM_ONLY", "EU_PORTABILITY_REQUIRED", "OUTSIDE_EU"})


class GoPlayError(RuntimeError):
    """A Play request or response failed without exposing auth material."""


class AuthenticationRequired(GoPlayError):
    """The cached Play TV login cannot be used or refreshed."""


class AvailabilityError(GoPlayError):
    """Play refused a title for this account or location."""


class NotFoundError(GoPlayError):
    """The requested Play resource does not exist."""


@dataclass
class Session:
    access_token: str = ""
    token_type: str = "Bearer"
    refresh_token: str = ""
    expires_at: float = 0.0
    id_token: str = ""
    device_id: str = ""
    device_name: str = DEVICE_NAME
    account_label: str = ""

    @classmethod
    def from_cache(cls, data: dict[str, Any] | None) -> Session:
        raw = data if isinstance(data, dict) else {}
        tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else raw
        device_id = _text(raw.get("device_id") or tokens.get("device_id"))
        return cls(
            access_token=_text(tokens.get("access_token") or tokens.get("accessToken") or tokens.get("AccessToken")),
            token_type=_text(
                tokens.get("token_type") or tokens.get("tokenType") or tokens.get("TokenType") or "Bearer"
            ),
            refresh_token=_text(tokens.get("refresh_token") or tokens.get("refreshToken")),
            expires_at=_number(raw.get("expires_at") or tokens.get("expires_at")),
            id_token=_text(tokens.get("id_token") or tokens.get("idToken") or tokens.get("IdToken")),
            device_id=device_id or str(uuid.uuid4()),
            device_name=_text(raw.get("device_name") or tokens.get("device_name")) or DEVICE_NAME,
            account_label=_text(raw.get("account_label")),
        )

    def to_cache(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "id_token": self.id_token,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "account_label": self.account_label,
        }

    @property
    def signed_in(self) -> bool:
        return bool(self.id_token or self.refresh_token)

    @property
    def recoverable(self) -> bool:
        return bool(self.refresh_token)

    def is_fresh(self, skew: int = 0) -> bool:
        return bool(self.id_token and self.expires_at > time.time() + skew)

    def ensure_device(self) -> None:
        if not self.device_id:
            self.device_id = str(uuid.uuid4())
        if not self.device_name:
            self.device_name = DEVICE_NAME

    def clear_auth(self) -> None:
        self.access_token = ""
        self.token_type = "Bearer"
        self.refresh_token = ""
        self.expires_at = 0.0
        self.id_token = ""
        self.account_label = ""


@dataclass(frozen=True)
class DeviceCode:
    code: str
    uri: str
    created: float
    expiry: float
    interval: float

    @property
    def expires_in(self) -> int:
        remaining = self.expiry - time.time()
        return max(1, int(remaining)) if remaining > 0 else 1


@dataclass(frozen=True)
class CatalogItem:
    id: str
    title: str
    kind: str
    subtitle: str = ""
    description: str = ""
    image_url: str = ""
    slug: str = ""
    link: str = ""
    duration: float | None = None
    season: int | None = None
    episode: int | None = None
    brand: str = ""
    dummy: bool = False
    now_title: str = ""
    now_episode: str = ""
    next_title: str = ""
    starts_at: float | None = None
    ends_at: float | None = None
    attachments: tuple[Attachment, ...] = ()

    @property
    def is_program(self) -> bool:
        return self.kind in {"PROGRAM", "RANKED_PROGRAM"}

    @property
    def is_video(self) -> bool:
        return self.kind == "VIDEO"

    @property
    def is_shortform(self) -> bool:
        return self.kind in {"SHORTFORM", "TRAILER"}

    @property
    def is_live(self) -> bool:
        return self.kind in {"LIVE", "LIVE_CHANNEL"}

    @property
    def is_page(self) -> bool:
        return self.kind in {"THEME_PAGE", "PAGE"}

    @property
    def is_playable(self) -> bool:
        return self.is_video or self.is_shortform or self.is_live

    @property
    def detail(self) -> str:
        now = self.now_title
        if now and self.now_episode:
            now = f"{now} · {self.now_episode}"
        values = [
            f"Nu: {now}" if now else "",
            f"Volgende: {self.next_title}" if self.next_title else "",
            self.subtitle if not now else "",
            self.brand,
            self.description if not now else "",
        ]
        return " · ".join(value for value in values if value)

    @property
    def type_label(self) -> str:
        return self.kind.replace("_", " ").title() if self.kind else ""


@dataclass(frozen=True)
class CatalogList:
    title: str
    list_id: str
    items: tuple[CatalogItem, ...] = ()
    total: int = 0
    page_slug: str = ""
    lane_type: str = ""
    format: str = ""


@dataclass(frozen=True)
class CatalogPage:
    slug: str
    title: str
    lists: tuple[CatalogList, ...]


@dataclass(frozen=True)
class Playlist:
    id: str
    title: str
    index: int = 0


@dataclass(frozen=True)
class Program:
    id: str
    title: str
    subtitle: str = ""
    description: str = ""
    brand: str = ""
    category: str = ""
    program_type: str = ""
    year: str | None = None
    duration: float | None = None
    image_url: str = ""
    playlists: tuple[Playlist, ...] = ()
    trailer_uuid: str = ""
    teaser_uuid: str = ""
    attachments: tuple[Attachment, ...] = ()

    @property
    def is_movie(self) -> bool:
        return self.program_type.upper() == "MOVIE"


@dataclass(frozen=True)
class SsaiSource:
    content_source_id: str
    video_id: str


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    subtitle: str = ""
    program_id: str = ""
    program_title: str = ""
    season: int | None = None
    episode: int | None = None
    duration: float | None = None
    brand: str = ""
    short_form: bool = False
    drm_xml: str = ""
    manifests: tuple[tuple[str, str, str], ...] = ()
    chapters: tuple[Chapter, ...] = ()
    ssai: SsaiSource | None = None


@dataclass(frozen=True)
class PlaybackSource:
    manifest_url: str
    manifest_type: str
    alternate_manifest_urls: tuple[str, ...]
    drm_xml: str
    title: str
    duration: float | None
    heartbeat_ms: int | None = None
    chapters: tuple[Chapter, ...] = ()

    @property
    def license_url(self) -> str:
        return DRM_LICENSE_URL if self.drm_xml else ""


@dataclass(frozen=True)
class DeepLink:
    kind: str
    uuid: str = ""
    slug: str = ""
    program_uuid: str = ""


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any, *, default: int = 0) -> int | None:
    if value in (None, ""):
        return None if default is None else default
    try:
        return int(value)
    except (TypeError, ValueError):
        return None if default is None else default


def _error_text(payload: Any, default: str) -> str:
    if not isinstance(payload, dict):
        return default
    copy = payload.get("copy")
    if isinstance(copy, dict):
        message = _text(copy.get("message") or copy.get("title"))
        if message:
            return message
    direct = payload.get("message") or payload.get("error_description") or payload.get("title")
    if direct:
        return _text(direct)
    error = payload.get("error")
    if isinstance(error, dict):
        return _text(error.get("message") or error.get("code")) or default
    if error:
        return _text(error)
    code = payload.get("code")
    if isinstance(code, dict):
        return _text(code.get("name") or code.get("code")) or default
    if code:
        return _text(code)
    return default


def _error_code(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    value = payload.get("code") or payload.get("error") or payload.get("error_code")
    if isinstance(value, dict):
        value = value.get("code") or value.get("name")
    return _text(value).upper()


def _parse_date(value: Any) -> float:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    text = _text(value)
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _year_from(value: Any) -> str | None:
    """Theatrical/release year from a human date string, never a unix timestamp."""
    if isinstance(value, (int, float)):
        return None
    text = _text(value)
    if not text or re.fullmatch(r"\d{9,13}", text):
        return None
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)
    return match.group(1) if match else None


def _video_id(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("videoUuid") or value.get("uuid") or value.get("id"))
    return _text(value)


def _image_url(value: Any, *, large: bool = False) -> str:
    if isinstance(value, str):
        url = _text(value)
        try:
            parsed = urlparse(url)
            return url if parsed.scheme in {"http", "https"} and parsed.hostname else ""
        except ValueError:
            return ""
    if isinstance(value, dict):
        if "url" in value:
            return _image_url(value["url"])
        for key in ("background", "portrait", "logo", "titleLogo"):
            url = _image_url(value.get(key), large=large)
            if url:
                return url
    if isinstance(value, list):
        styles = ("large", "medium", "small") if large else ("medium", "large", "small")
        for style in styles:
            for item in value:
                if isinstance(item, dict) and _text(item.get("style")).lower() == style:
                    url = _image_url(item)
                    if url:
                        return url
    return ""


def extract_artwork(raw: Any, *, program: bool = False) -> tuple[Attachment, ...]:
    if not isinstance(raw, dict):
        return ()
    images = raw.get("images")
    if program:
        images = images if isinstance(images, dict) else {}
        entries = (
            (images.get("background"), "Program background", "artwork"),
            (images.get("portrait"), "Program poster", "poster"),
            (images.get("logo"), "Program logo", "image"),
            (images.get("titleLogo"), "Program title logo", "image"),
        )
    else:
        entries = (
            (images or raw.get("image"), "Artwork", "artwork"),
            (raw.get("logo"), "Logo", "image"),
        )
    attachments: list[Attachment] = []
    seen: set[str] = set()
    for value, name, kind in entries:
        url = _image_url(value, large=True)
        if url and url not in seen:
            seen.add(url)
            attachments.append(Attachment(url=url, name=name, kind=kind, headers={"User-Agent": USER_AGENT}))
    return tuple(attachments)


def _program_type(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("code") or value.get("type"))
    return _text(value)


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _account_label(id_token: str, fallback: str = "") -> str:
    claims = _jwt_claims(id_token)
    given = _text(claims.get("name") or claims.get("given_name"))
    family = _text(claims.get("family_name"))
    combined = " ".join(part for part in (given, family) if part)
    return combined or _text(claims.get("email") or claims.get("cognito:username")) or fallback


def parse_reference(value: str) -> str | None:
    """Return a Play URL, page slug, or UUID the TV API can resolve."""
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"[0-9a-fA-F-]{8,}", text) and text.count("-") >= 1:
        return text
    if text.startswith("/") and "://" not in text:
        return f"{SITE_URL}{text}"
    candidate = text if "://" in text else f"https://{text}"
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if host not in DEEP_LINK_HOSTS:
        return None
    return candidate


def parse_season_episode(*values: object) -> tuple[int | None, int | None]:
    blob = " ".join(str(value or "") for value in values)
    tagged = re.search(r"\bS(\d+)[-._ ]?(?:E|A(?:fl\.?)?)(\d+)\b", blob, re.IGNORECASE)
    if tagged:
        return int(tagged.group(1)), int(tagged.group(2))
    season_match = re.search(r"(?:seizoen|season|[_\-.\s]s)(?:\s*|[_\-])(\d+)", blob, re.IGNORECASE)
    episode_match = re.search(r"(?:afl\.?|aflevering|episode|[_\-.\s]e)(?:\s*|[_\-])(\d+)", blob, re.IGNORECASE)
    season = int(season_match.group(1)) if season_match else None
    episode = int(episode_match.group(1)) if episode_match else None
    return season, episode


def parse_card(raw: Any) -> CatalogItem | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("isDummy") is True:
        return None
    kind = _text(raw.get("type") or raw.get("kind")).upper() or "VIDEO"
    if kind == "ANNOUNCEMENT":
        return None
    item_id = _text(raw.get("uuid") or raw.get("id") or raw.get("videoUuid") or raw.get("programUuid"))
    title = _text(raw.get("title") or raw.get("label"))
    if not item_id or not title:
        return None
    link = raw.get("url") or raw.get("link")
    href = ""
    slug = _text(raw.get("slug"))
    if isinstance(link, dict):
        href = _text(link.get("url") or link.get("slug"))
        slug = slug or _text(link.get("slug"))
        if kind == "LINK":
            reference = link.get("reference")
            if isinstance(reference, dict):
                target_kind = {
                    "LONG_FORM": "VIDEO",
                    "SHORT_FORM": "SHORTFORM",
                    "LIVE_CHANNEL": "LIVE",
                    "PROGRAM": "PROGRAM",
                    "THEME_PAGE": "THEME_PAGE",
                }.get(_text(reference.get("type")).upper())
                if target_kind:
                    item_id = _text(reference.get("uuid"))
                    kind = target_kind
                    if not item_id or (kind == "THEME_PAGE" and not slug):
                        return None
    elif link:
        href = _text(link)
    duration = _integer(raw.get("duration"), default=None)
    return CatalogItem(
        id=item_id,
        title=title,
        kind=kind,
        subtitle=_text(raw.get("subtitle") or raw.get("label") or raw.get("category")),
        description=_text(raw.get("description")),
        image_url=_image_url(raw.get("images") or raw.get("image") or raw.get("logo")),
        slug=slug,
        link=href,
        duration=float(duration) if duration else None,
        brand=_text(raw.get("brand")),
        dummy=bool(raw.get("isDummy")),
        attachments=extract_artwork(raw),
    )


def parse_lane(raw: Any, *, page_slug: str = "") -> CatalogList | None:
    if not isinstance(raw, dict):
        return None
    list_id = _text(raw.get("uuid") or raw.get("id"))
    title = _text(raw.get("title") or raw.get("trackingTitle"))
    lane_type = _text(raw.get("laneType") or raw.get("type")).upper()
    if not list_id:
        return None
    return CatalogList(
        title=title or "List",
        list_id=list_id,
        page_slug=page_slug,
        lane_type=lane_type,
        format=_text(raw.get("format")).upper(),
    )


def parse_program(raw: Any) -> Program:
    if not isinstance(raw, dict):
        raise GoPlayError("Play returned no program")
    program_id = _text(raw.get("programUuid") or raw.get("uuid") or raw.get("id"))
    title = _text(raw.get("title"))
    if not program_id or not title:
        raise GoPlayError("Play program is missing an id or title")
    playlists: list[Playlist] = []
    for index, item in enumerate(raw.get("playlists") or []):
        if not isinstance(item, dict):
            continue
        playlist_id = _text(item.get("playlistUuid") or item.get("uuid") or item.get("id"))
        if not playlist_id:
            continue
        playlists.append(
            Playlist(
                id=playlist_id,
                title=_text(item.get("title")) or f"Season {index + 1}",
                index=_integer(item.get("index"), default=index) or index,
            )
        )
    return Program(
        id=program_id,
        title=title,
        subtitle=_text(raw.get("subtitle")),
        description=_text(raw.get("description")),
        brand=_text(raw.get("brand")),
        category=_text(raw.get("category") or raw.get("genre")),
        program_type=_program_type(raw.get("type")),
        duration=float(_integer(raw.get("duration"), default=0) or 0) or None,
        image_url=_image_url(raw.get("images")),
        playlists=tuple(playlists),
        trailer_uuid=_text(raw.get("trailerVideoUuid")),
        teaser_uuid=_text(raw.get("teaserVideoUuid")),
        year=None,
        attachments=extract_artwork(raw, program=True),
    )


def parse_video(raw: Any, *, short_form: bool = False) -> Video:
    if not isinstance(raw, dict):
        raise GoPlayError("Play returned no video")
    video_id = _text(raw.get("videoUuid") or raw.get("uuid") or raw.get("id"))
    title = _text(raw.get("title"))
    if not video_id or not title:
        raise GoPlayError("Play video is missing an id or title")
    program = raw.get("program") if isinstance(raw.get("program"), dict) else {}
    manifests: list[tuple[str, str, str]] = []
    for item in raw.get("manifestUrlsByRatio") or raw.get("manifestUrls") or []:
        parsed = _manifest_entry(item)
        if parsed:
            manifests.append(parsed)
    season = _integer(raw.get("seasonNumber"), default=None)
    episode = _integer(raw.get("episodeNumber"), default=None)
    if season is None or episode is None:
        parsed_season, parsed_episode = parse_season_episode(title, raw.get("subtitle"), raw.get("seasonTitle"))
        season = season if season is not None else parsed_season
        episode = episode if episode is not None else parsed_episode
    duration = float(_integer(raw.get("duration"), default=0) or 0) or None
    return Video(
        id=video_id,
        title=title,
        subtitle=_text(raw.get("subtitle")),
        program_id=_text(program.get("uuid") or raw.get("programUuid")),
        program_title=_text(program.get("title")),
        season=season,
        episode=episode,
        duration=duration,
        brand=_text(raw.get("brand")),
        short_form=short_form,
        drm_xml=_text(raw.get("drmXml")),
        manifests=tuple(manifests),
        chapters=extract_chapters(raw.get("markers"), duration=duration),
        ssai=_parse_ssai(raw) if short_form else None,
    )


def _parse_ssai(raw: dict[str, Any]) -> SsaiSource | None:
    if _text(raw.get("adType")).upper() != "SSAI" or raw.get("ssai") is None:
        return None
    ssai = raw["ssai"]
    if not isinstance(ssai, dict):
        raise GoPlayError("Play returned invalid SSAI source metadata")
    content_source_id = _text(ssai.get("contentSourceID"))
    video_id = _text(ssai.get("videoID"))
    if not content_source_id or not video_id:
        raise GoPlayError("Play SSAI source is missing its content source or video id")
    return SsaiSource(content_source_id, video_id)


def _marker_seconds(value: Any, *, milliseconds: bool = False) -> float | None:
    """Skip markers are seconds; legacy editorial markers are milliseconds."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number / 1000.0 if milliseconds else number


def _first_marker(
    raw: dict[str, Any], keys: tuple[str, ...], *, duration: float | None, allow_zero: bool
) -> float | None:
    for key in keys:
        moment = _marker_seconds(raw.get(key), milliseconds=not key.startswith("skip"))
        if moment is None:
            continue
        if duration and moment >= duration:
            continue
        if moment > 0 or (allow_zero and moment == 0):
            return moment
    return None


def _credits_start(raw: dict[str, Any], *, duration: float | None) -> float | None:
    skip = _marker_seconds(raw.get("skipOutro"))
    if skip is not None and duration:
        start = duration + skip if skip < 0 else skip
        if 0 < start < duration - 1:
            return start
    credits = _marker_seconds(raw.get("endCredits"), milliseconds=True)
    if credits is not None and duration and 0 < credits < duration - 1:
        return credits
    return None


def _chapter(start: float, title: str, end: float | None = None, *, kind: str = "") -> Chapter | None:
    try:
        return Chapter.from_seconds(start, title, end, kind=kind)
    except ValueError:
        return None


def extract_chapters(raw: Any, *, duration: float | None = None) -> tuple[Chapter, ...]:
    """Turn Play ``markers`` into Core chapters.

    The Android TV player skips intro via ``skipIntroStart``/``skipIntroEnd`` and
    outro via ``skipOutro`` (seconds, negative = from the end). The TV API also
    sends millisecond ``endCredits`` and recap/opening fields. Ad breaks are not
    chapters. Invalid or out-of-content markers do not create chapters.
    """
    if not isinstance(raw, dict):
        return ()
    duration = _marker_seconds(duration)
    duration = duration if duration is not None and duration > 0 else None
    recap = _first_marker(raw, ("previouslyOn",), duration=duration, allow_zero=True)
    intro_start = _first_marker(raw, ("skipIntroStart", "openingScene"), duration=duration, allow_zero=True)
    intro_end = _first_marker(raw, ("skipIntroEnd", "mainContent"), duration=duration, allow_zero=False)
    credits = _credits_start(raw, duration=duration)
    valid_intro = intro_start is not None and intro_end is not None and intro_end > intro_start
    main_start = intro_end if valid_intro else _first_marker(raw, ("mainContent",), duration=duration, allow_zero=False)
    if credits is not None and main_start is not None and credits <= main_start:
        credits = None
    chapters: list[Chapter] = []
    recap_end = intro_start if valid_intro else main_start
    if recap is not None and recap_end is not None and recap_end > recap:
        chapter = _chapter(recap, "Recap", recap_end, kind="recap")
        if chapter:
            chapters.append(chapter)
    if valid_intro:
        chapter = _chapter(intro_start, "Intro", intro_end, kind="intro")
        if chapter:
            chapters.append(chapter)
    if credits is not None:
        if main_start is not None and credits > main_start:
            chapter = _chapter(main_start, "Main", credits)
            if chapter:
                chapters.append(chapter)
        credit_end = duration if duration is not None and duration > credits else None
        chapter = _chapter(credits, "Credits", credit_end, kind="credits")
        if chapter:
            chapters.append(chapter)
    elif main_start is not None and duration is not None and duration > main_start:
        chapter = _chapter(main_start, "Main", duration)
        if chapter:
            chapters.append(chapter)
    return tuple(chapters)


def ssai_chapters(chapters: tuple[Chapter, ...], payload: dict[str, Any]) -> tuple[Chapter, ...]:
    """Map content markers onto the unmodified stitched DAI stream timeline."""
    if not chapters:
        return ()
    content_duration = _marker_seconds(payload.get("content_duration"))
    total_duration = _marker_seconds(payload.get("total_duration"))
    rows = payload.get("ad_breaks")
    if content_duration is None or total_duration is None or not isinstance(rows, list):
        return ()
    if content_duration <= 0 or total_duration < content_duration:
        return ()
    content_ms, total_ms = round(content_duration * 1000), round(total_duration * 1000)
    breaks: list[tuple[int, int]] = []
    for row in rows:
        if not isinstance(row, dict):
            return ()
        start = _marker_seconds(row.get("start"))
        duration = _marker_seconds(row.get("duration"))
        if start is None or duration is None or start < 0 or duration < 0:
            return ()
        breaks.append((round(start * 1000), round(duration * 1000)))
    inserted = previous_end = 0
    offsets: list[tuple[int, int]] = []
    for start, duration in sorted(breaks):
        content_start = start - inserted
        if start < previous_end or start + duration > total_ms or not 0 <= content_start <= content_ms:
            return ()
        offsets.append((content_start, duration))
        inserted += duration
        previous_end = start + duration
    if abs(total_ms - content_ms - inserted) > 100:
        return ()

    def stream_time(position: int, *, end: bool = False) -> int:
        # At a break boundary the next chapter starts after ads; the previous
        # chapter ends before them. No ad media is removed or made a chapter.
        return position + sum(
            duration for start, duration in offsets if start < position or (start == position and not end)
        )

    mapped: list[Chapter] = []
    for chapter in chapters:
        if chapter.start_ms >= content_ms:
            continue
        end_ms = min(chapter.end_ms, content_ms) if chapter.end_ms is not None else None
        mapped.append(
            replace(
                chapter,
                start_ms=stream_time(chapter.start_ms),
                end_ms=stream_time(end_ms, end=True) if end_ms is not None else None,
            )
        )
    return tuple(mapped)


def parse_video_teaser(raw: Any, *, playlist_title: str = "") -> CatalogItem | None:
    if not isinstance(raw, dict):
        return None
    video_id = _text(raw.get("videoUuid") or raw.get("uuid") or raw.get("id"))
    title = _text(raw.get("title"))
    if not video_id or not title:
        return None
    season, episode = parse_season_episode(title, playlist_title, raw.get("description"))
    duration = _integer(raw.get("duration"), default=None)
    return CatalogItem(
        id=video_id,
        title=title,
        kind="VIDEO",
        subtitle=_text(raw.get("description")),
        image_url=_image_url(raw.get("image")),
        duration=float(duration) if duration else None,
        season=season,
        episode=episode,
        attachments=extract_artwork(raw),
    )


@dataclass(frozen=True)
class EpgProgram:
    title: str
    episode_title: str = ""
    season: int | None = None
    episode: int | None = None
    starts_at: float | None = None
    ends_at: float | None = None
    description: str = ""
    program_id: str = ""
    video_id: str = ""

    @property
    def label(self) -> str:
        if self.title and self.episode_title:
            return f"{self.title} · {self.episode_title}"
        return self.title or self.episode_title


def parse_epg_program(raw: Any) -> EpgProgram | None:
    if not isinstance(raw, dict):
        return None
    title = _text(raw.get("programTitle") or raw.get("title"))
    if not title:
        return None
    start = _parse_date(raw.get("timestamp"))
    duration = _integer(raw.get("duration"), default=None)
    end = start + duration if start and duration else 0.0
    video = raw.get("video") if isinstance(raw.get("video"), dict) else {}
    return EpgProgram(
        title=title,
        episode_title=_text(raw.get("episodeTitle")),
        season=_integer(raw.get("season"), default=None),
        episode=_integer(raw.get("episodeNr") or raw.get("episodeNumber"), default=None),
        starts_at=start or None,
        ends_at=end or None,
        description=_text(raw.get("contentEpisode") or raw.get("programConcept")),
        program_id=_text(raw.get("programUuid")),
        video_id=_video_id(video),
    )


def parse_epg_list(raw: Any) -> tuple[EpgProgram, ...]:
    rows = raw if isinstance(raw, list) else []
    return tuple(item for item in (parse_epg_program(row) for row in rows) if item)


def parse_live_card(raw: Any) -> CatalogItem | None:
    if not isinstance(raw, dict):
        return None
    item_id = _text(raw.get("uuid") or raw.get("id"))
    title = _text(raw.get("title") or raw.get("label"))
    if not item_id or not title:
        return None
    epg = parse_epg_list(raw.get("epgPrograms"))
    now = epg[0] if epg else None
    nxt = epg[1] if len(epg) > 1 else None
    override = _text(raw.get("epgOverride"))
    now_title = now.title if now else override
    return CatalogItem(
        id=item_id,
        title=title,
        kind="LIVE",
        subtitle=_text(raw.get("label") or (now.label if now else "") or raw.get("description")),
        description=_text(raw.get("description")),
        image_url=_image_url(raw.get("images")),
        brand=_text(raw.get("brand")),
        now_title=now_title if now_title != title else "",
        now_episode=now.episode_title if now else "",
        next_title=nxt.label if nxt else "",
        starts_at=now.starts_at if now else None,
        ends_at=now.ends_at if now else None,
        season=now.season if now else None,
        episode=now.episode if now else None,
        attachments=extract_artwork(raw),
    )


def _manifest_entry(raw: Any) -> tuple[str, str, str] | None:
    if not isinstance(raw, dict):
        return None
    url = _text(raw.get("url"))
    if not url:
        return None
    protocol = _text(raw.get("protocol")).lower() or ("dash" if ".mpd" in url else "hls")
    ratio = _text(raw.get("ratio")) or "16:9"
    return protocol, ratio, url


def _pick_manifest(
    entries: list[tuple[str, str, str]] | tuple[tuple[str, str, str], ...],
    *,
    extra_hls: str = "",
    extra_dash: str = "",
) -> tuple[str, str, tuple[str, ...]]:
    ranked: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(protocol: str, url: str) -> None:
        if url and url not in seen:
            ranked.append((protocol, url))
            seen.add(url)

    combined = list(entries)
    if extra_dash:
        combined.append(("dash", "16:9", extra_dash))
    if extra_hls:
        combined.append(("hls", "16:9", extra_hls))

    def sort_key(item: tuple[str, str, str]) -> tuple[int, int]:
        protocol, ratio, _url = item
        landscape = 0 if ratio in {"16:9", "LANDSCAPE", ""} else 1
        dash = 0 if protocol == "dash" else 1
        return (landscape, dash)

    for protocol, ratio, url in sorted(combined, key=sort_key):
        if protocol in {"dash", "hls"} and ratio in {"16:9", "LANDSCAPE", ""}:
            add(protocol, url)
    if not ranked:
        raise GoPlayError("Play returned no playable manifest")
    primary_protocol, primary_url = ranked[0]
    alternates = tuple(url for _protocol, url in ranked[1:])
    return primary_url, primary_protocol, alternates


class GoPlayApi:
    def __init__(
        self,
        session: requests.Session,
        state: Session | None = None,
        *,
        on_save=None,
    ) -> None:
        self.http = session
        self.state = state or Session()
        self.state.ensure_device()
        self.on_save = on_save
        self.http.headers["User-Agent"] = USER_AGENT
        self.http.headers["X-Platform"] = API_PLATFORM
        self.http.headers["Accept"] = "application/json"

    def _persist(self) -> None:
        if self.on_save is not None:
            self.on_save(self.state)

    def _request(self, method: str, url: str, *, authorize: bool = True, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        headers = dict(kwargs.pop("headers", None) or {})
        if authorize and "authorisation" not in url.lower() and "Authorization" not in headers:
            if self.state.id_token:
                headers["Authorization"] = f"Bearer {self.state.id_token}"
        elif not authorize:
            # None also suppresses a bearer inherited from requests.Session.
            headers["Authorization"] = None
        kwargs["headers"] = headers
        try:
            return self.http.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise GoPlayError(f"Play request failed: {exc}") from exc

    def _json(self, response: requests.Response, label: str) -> dict[str, Any] | list[Any]:
        if not response.content:
            return {}
        try:
            payload = response.json()
        except (ValueError, requests.JSONDecodeError) as exc:
            raise GoPlayError(f"{label} returned invalid JSON (HTTP {response.status_code})") from exc
        if not isinstance(payload, (dict, list)):
            raise GoPlayError(f"{label} returned an unexpected response (HTTP {response.status_code})")
        return payload

    def _raise_http(self, payload: Any, status: int, label: str) -> None:
        code = _error_code(payload)
        if code in GEO_ERROR_CODES:
            raise AvailabilityError(_error_text(payload, "Play is not available from this location"))
        if status in {401, 403} or code in {"EXPIRED_TOKEN", "INVALID_TOKEN", "INVALID_GRANT", "UNAUTHORIZED"}:
            raise AuthenticationRequired(_error_text(payload, "Play rejected the TV session"))
        if status == 404:
            raise NotFoundError(_error_text(payload, f"{label} was not found"))
        raise GoPlayError(_error_text(payload, f"{label} failed (HTTP {status})"))

    def _api(
        self,
        method: str,
        path: str,
        *,
        authorize: bool = True,
        retry: bool = True,
        **kwargs,
    ) -> Any:
        url = path if path.startswith("http") else f"{API_BASE_URL}{path.lstrip('/')}"
        if authorize:
            self.ensure_auth()
        response = self._request(method, url, authorize=authorize, **kwargs)
        if retry and authorize and response.status_code == 401 and self.state.recoverable:
            self.refresh()
            return self._api(method, path, authorize=authorize, retry=False, **kwargs)
        payload = self._json(response, path)
        if response.status_code >= 400:
            self._raise_http(payload, response.status_code, path)
        return payload

    def start_device_code(self) -> DeviceCode:
        self.state.ensure_device()
        payload = self._api(
            "POST",
            "v1/authorisation/codes",
            authorize=False,
            json={"deviceId": self.state.device_id, "deviceName": self.state.device_name},
        )
        if not isinstance(payload, dict):
            raise GoPlayError("Play TV-code response was not an object")
        code = _text(payload.get("code"))
        uri = _text(payload.get("uri")) or LOGIN_PAGE_URL
        if not code:
            raise GoPlayError("Play TV-code response is missing a user code")
        created = _parse_date(payload.get("created")) or time.time()
        expiry = _parse_date(payload.get("expiry")) or (created + 600)
        interval = max(1.0, _number(payload.get("pollingInterval")) or 5.0)
        self._persist()
        return DeviceCode(code=code, uri=uri, created=created, expiry=expiry, interval=interval)

    def poll_device_code(self, challenge: DeviceCode) -> Session | None:
        response = self._request(
            "GET",
            f"{API_BASE_URL}v1/authorisation/codes/{challenge.code}",
            authorize=False,
        )
        payload = self._json(response, "Play TV-code confirmation")
        if response.status_code in {404, 410}:
            raise AuthenticationRequired("The Play TV code expired. Request a new code.")
        if response.status_code >= 400:
            self._raise_http(payload, response.status_code, "Play TV-code confirmation")
        if not isinstance(payload, dict):
            raise GoPlayError("Play TV-code confirmation was not an object")
        status = _text(payload.get("status")).upper()
        if status in {"", "PENDING", "CREATED"}:
            return None
        if status != "AUTHORISED":
            raise GoPlayError(f"Play TV-code confirmation returned {status or 'an unknown status'}")
        tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
        expires_in = (
            _integer(payload.get("ExpiresIn") or payload.get("expiresIn") or payload.get("expires_in"), default=3600)
            or 3600
        )
        self._apply_tokens(tokens, expires_in=expires_in, keep_refresh=False)
        return self.state

    def _apply_tokens(self, tokens: dict[str, Any], *, expires_in: int, keep_refresh: bool = True) -> None:
        access_token = _text(tokens.get("accessToken") or tokens.get("AccessToken") or tokens.get("access_token"))
        id_token = _text(tokens.get("idToken") or tokens.get("IdToken") or tokens.get("id_token"))
        refresh_token = _text(tokens.get("refreshToken") or tokens.get("refresh_token"))
        if keep_refresh:
            refresh_token = refresh_token or self.state.refresh_token
        if not (id_token and refresh_token):
            raise AuthenticationRequired("Play login did not return a complete renewable session")
        token_type = _text(tokens.get("tokenType") or tokens.get("TokenType") or tokens.get("token_type") or "Bearer")
        self.state.access_token = access_token or id_token
        self.state.token_type = token_type or "Bearer"
        self.state.refresh_token = refresh_token
        self.state.id_token = id_token
        # The TV client renews five minutes early, except for shorter lifetimes.
        lifetime = expires_in - TOKEN_EXPIRY_SKEW if expires_in >= TOKEN_EXPIRY_SKEW else expires_in
        self.state.expires_at = time.time() + max(0, lifetime)
        self.state.account_label = _account_label(id_token, self.state.account_label or "Play account")
        self.state.ensure_device()
        self._persist()

    def refresh(self) -> Session:
        if not self.state.refresh_token:
            raise AuthenticationRequired("Play is not signed in. Use a new TV code.")
        response = self._request(
            "POST",
            f"{API_BASE_URL}v1/authorisation/refresh",
            authorize=False,
            json={"refreshToken": self.state.refresh_token},
        )
        payload = self._json(response, "Play token refresh")
        code = _error_code(payload)
        if response.status_code in {400, 401, 403} or code in {"EXPIRED_TOKEN", "INVALID_TOKEN", "INVALID_GRANT"}:
            self.state.clear_auth()
            self._persist()
            raise AuthenticationRequired("The Play session expired. Sign in with a new TV code.")
        if response.status_code >= 400 or (isinstance(payload, dict) and code):
            raise GoPlayError(_error_text(payload, f"Play token refresh failed (HTTP {response.status_code})"))
        if not isinstance(payload, dict):
            raise GoPlayError("Play token refresh returned no tokens")
        expires_in = (
            _integer(payload.get("ExpiresIn") or payload.get("expiresIn") or payload.get("expires_in"), default=3600)
            or 3600
        )
        self._apply_tokens(payload, expires_in=expires_in)
        return self.state

    def ensure_auth(self, *, force_refresh: bool = False) -> None:
        if not force_refresh and self.state.is_fresh():
            return
        if self.state.recoverable:
            self.refresh()
            return
        raise AuthenticationRequired("Play is not signed in. Open Sign in and enter a TV code first.")

    def page(self, slug: str) -> CatalogPage:
        payload = self._api("GET", f"v2/pages/{slug}")
        if not isinstance(payload, dict):
            raise GoPlayError(f"Play page {slug!r} was not an object")
        lists: list[CatalogList] = []
        for item in payload.get("lanes") or []:
            catalog = parse_lane(item, page_slug=slug)
            if catalog and catalog.format != "OTHER" and catalog.lane_type not in SKIP_LANE_TYPES:
                lists.append(catalog)
        return CatalogPage(slug=slug, title=_text(payload.get("title")) or slug, lists=tuple(lists))

    def lane(
        self, page_slug: str, lane_uuid: str, *, offset: int = 0, limit: int = PAGE_SIZE
    ) -> tuple[int, tuple[CatalogItem, ...]]:
        total, rows = self._list_page(f"v2/pages/{page_slug}/lanes/{lane_uuid}", "cards", offset=offset, limit=limit)
        items = tuple(item for item in (parse_card(raw) for raw in rows) if item)
        return total, items

    def _list_page(
        self, path: str, field: str, *, offset: int, limit: int, query: str | None = None
    ) -> tuple[int, list[Any]]:
        params = {"offset": offset, "limit": limit}
        if query is None:
            payload = self._api("GET", path, params=params)
        else:
            payload = self._api("POST", path, json={**params, "query": query})
        if not isinstance(payload, dict) or not isinstance(payload.get(field), list):
            raise GoPlayError(f"Play {path} returned no {field} list")
        rows = payload[field]
        total = _integer(payload.get("total"), default=len(rows))
        return max(0, total), rows

    def _list_pages(
        self, path: str, field: str, *, query: str | None = None, limit: int | None = None
    ) -> tuple[int, list[Any]]:
        collected: list[Any] = []
        offset = total = 0
        for _ in range(MAX_PAGES):
            page_limit = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - offset)
            if page_limit <= 0:
                break
            total, rows = self._list_page(path, field, offset=offset, limit=page_limit, query=query)
            collected.extend(rows)
            # Offsets count wire entries, including dummy and unsupported cards.
            offset += len(rows)
            if not rows or offset >= total:
                break
        return total, collected

    def complete_list(self, catalog: CatalogList) -> CatalogList:
        if not catalog.list_id or not catalog.page_slug:
            return catalog
        total, rows = self._list_pages(f"v2/pages/{catalog.page_slug}/lanes/{catalog.list_id}", "cards")
        items = tuple(item for item in (parse_card(raw) for raw in rows) if item)
        return replace(catalog, items=items, total=total)

    def program(self, program_uuid: str) -> Program:
        payload = self._api("GET", f"v2/programs/{program_uuid}")
        return parse_program(payload)

    def program_actions(self, program: Program) -> tuple[CatalogItem, ...]:
        payload = self._api("GET", f"v1/programs/{program.id}/buttons", params={"context": "program"})
        if not isinstance(payload, dict) or not isinstance(payload.get("buttons"), list):
            raise GoPlayError("Play program buttons response was not a list")
        items: list[CatalogItem] = []
        seen: set[tuple[str, str]] = set()
        for index, button in enumerate(payload["buttons"]):
            if not isinstance(button, dict):
                continue
            link = button.get("link")
            if not isinstance(link, dict):
                continue
            item = parse_card(
                {
                    "uuid": f"button-{index}",
                    "title": _text(button.get("text") or link.get("text")) or program.title,
                    "type": "LINK",
                    "url": link,
                }
            )
            if item and item.is_playable:
                if item.is_shortform and _text(button.get("type")).upper() == "TRAILER":
                    item = replace(item, kind="TRAILER")
                if (item.kind, item.id) in seen:
                    continue
                seen.add((item.kind, item.id))
                items.append(item)
        return tuple(items)

    def playlist(
        self, playlist_uuid: str, *, offset: int = 0, limit: int = PAGE_SIZE
    ) -> tuple[int, tuple[CatalogItem, ...]]:
        total, rows = self._list_page(f"v1/playlists/{playlist_uuid}", "videos", offset=offset, limit=limit)
        items = tuple(item for item in (parse_video_teaser(raw) for raw in rows) if item)
        return total, items

    def complete_playlist(self, playlist: Playlist) -> tuple[CatalogItem, ...]:
        _total, rows = self._list_pages(f"v1/playlists/{playlist.id}", "videos")
        return tuple(item for item in (parse_video_teaser(raw, playlist_title=playlist.title) for raw in rows) if item)

    def search(self, query: str, *, limit: int = 50) -> tuple[CatalogItem, ...]:
        _total, rows = self._list_pages("v1/search", "cards", query=query, limit=limit)
        return tuple(item for item in (parse_card(raw) for raw in rows) if item)

    def live_streams(self) -> tuple[CatalogItem, ...]:
        payload = self._api("GET", "v1/liveStreams")
        rows = payload if isinstance(payload, list) else []
        return tuple(item for item in (parse_live_card(raw) for raw in rows) if item)

    def my_list(self) -> tuple[str, ...]:
        payload = self._api("GET", "v1/programs/myList")
        if not isinstance(payload, list):
            return ()
        return tuple(_text(item) for item in payload if _text(item))

    def resolve_url(self, target: str) -> DeepLink:
        reference = parse_reference(target)
        if reference is None:
            raise GoPlayError("Expected a play.tv or goplay.be URL, or a content UUID")
        if "://" not in reference:
            return self._resolve_uuid(reference)
        parsed = urlparse(reference)
        path = parsed.path.rstrip("/") or "/"
        hardcoded = HARDCODED_PAGES.get(path) or HARDCODED_PAGES.get(path.lower())
        if hardcoded:
            return DeepLink(kind="page", slug=hardcoded)
        if path.lower() == "/profiel":
            return DeepLink(kind="library")
        payload = self._api("GET", reference, authorize=False, headers={"Accept": "application/json"})
        if not isinstance(payload, dict):
            raise GoPlayError("Play could not resolve that URL")
        content_type = _text(payload.get("contentType")).lower().replace("_", "")
        item_id = _text(payload.get("uuid"))
        slug = _text(payload.get("slug"))
        program_uuid = _text(payload.get("programUuid"))
        if content_type in {"livechannel", "live"}:
            return DeepLink(kind="live", uuid=item_id)
        if content_type == "longform":
            return DeepLink(kind="video", uuid=item_id, program_uuid=program_uuid)
        if content_type == "shortform":
            return DeepLink(kind="shortform", uuid=item_id, program_uuid=program_uuid)
        if content_type == "program":
            return DeepLink(kind="program", uuid=item_id or program_uuid)
        if content_type == "themepage":
            return DeepLink(kind="page", slug=slug or item_id)
        raise GoPlayError("Play did not recognise that URL")

    def _resolve_uuid(self, value: str) -> DeepLink:
        try:
            self.program(value)
            return DeepLink(kind="program", uuid=value)
        except NotFoundError:
            pass
        try:
            self.long_form(value)
            return DeepLink(kind="video", uuid=value)
        except NotFoundError:
            pass
        try:
            self.short_form(value)
            return DeepLink(kind="shortform", uuid=value)
        except NotFoundError:
            pass
        try:
            self.live_stream(value)
            return DeepLink(kind="live", uuid=value)
        except NotFoundError as exc:
            raise GoPlayError(f"Play could not resolve {value}") from exc

    def long_form(self, video_uuid: str) -> Video:
        payload = self._api("GET", f"v1/videos/long-form/{video_uuid}", params={"iabConsentString": ""})
        return parse_video(payload, short_form=False)

    def short_form(self, video_uuid: str) -> Video:
        payload = self._api("GET", f"v1/videos/short-form/{video_uuid}", params={"iabConsentString": ""})
        return parse_video(payload, short_form=True)

    def _ssai_playback(
        self, source: SsaiSource, video: Video, *, drm_xml: str, heartbeat_ms: int | None = None
    ) -> PlaybackSource:
        url = DAI_VOD_URL.format(
            content_source_id=quote(source.content_source_id, safe=""),
            video_id=quote(source.video_id, safe=""),
        )
        response = self._request(
            "POST",
            url,
            authorize=False,
            headers={"Content-Type": "application/x-www-form-urlencoded", "X-Platform": None},
            data={},
        )
        if response.status_code >= 400:
            raise AvailabilityError(f"Play SSAI stream request was refused (HTTP {response.status_code})")
        payload = self._json(response, "Play SSAI stream")
        manifest_url = _text(payload.get("stream_manifest")) if isinstance(payload, dict) else ""
        parsed = urlparse(manifest_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise GoPlayError("Play SSAI stream response is missing a valid stream_manifest")
        duration = _marker_seconds(payload.get("total_duration"))
        return PlaybackSource(
            manifest_url=manifest_url,
            manifest_type="dash",
            alternate_manifest_urls=(),
            drm_xml=drm_xml,
            title=video.title,
            duration=duration if duration is not None and duration > 0 else video.duration,
            heartbeat_ms=heartbeat_ms,
            chapters=ssai_chapters(video.chapters, payload),
        )

    def play(self, video: Video) -> PlaybackSource:
        if video.short_form:
            if video.ssai:
                return self._ssai_playback(video.ssai, video, drm_xml=video.drm_xml)
            url, protocol, alternates = _pick_manifest(video.manifests)
            return PlaybackSource(
                manifest_url=url,
                manifest_type=protocol,
                alternate_manifest_urls=() if video.drm_xml else alternates,
                drm_xml=video.drm_xml,
                title=video.title,
                duration=video.duration,
                chapters=video.chapters,
            )
        payload = self._api(
            "POST",
            f"v1/videos/{video.id}/play",
            json={"deviceFriendlyName": self.state.device_name, "deviceId": self.state.device_id},
        )
        if not isinstance(payload, dict):
            raise GoPlayError("Play playback response was not an object")
        eligibility = _text(payload.get("eligibilityStatus")).upper()
        if eligibility != "ALLOWED":
            raise AvailabilityError(_eligibility_message(eligibility))
        drm_xml = _text(payload.get("drmXml"))
        heartbeat = _integer(payload.get("heartbeatTimeMs"), default=None)
        entries = [_manifest_entry(item) for item in payload.get("manifestUrls") or []]
        manifests = [item for item in entries if item]
        try:
            ssai = _parse_ssai(payload)
            if ssai:
                return self._ssai_playback(ssai, video, drm_xml=drm_xml, heartbeat_ms=heartbeat)
            url, protocol, alternates = _pick_manifest(manifests)
        except GoPlayError as exc:
            raise AvailabilityError(_error_text(payload, str(exc))) from exc
        if drm_xml:
            alternates = ()
        return PlaybackSource(
            manifest_url=url,
            manifest_type=protocol,
            alternate_manifest_urls=alternates,
            drm_xml=drm_xml,
            title=video.title,
            duration=video.duration,
            heartbeat_ms=heartbeat,
            chapters=video.chapters,
        )

    def live_stream(self, live_uuid: str) -> dict[str, Any]:
        payload = self._api("GET", f"v1/liveStreams/{live_uuid}")
        if not isinstance(payload, dict):
            raise GoPlayError("Play live stream response was not an object")
        return payload

    def live_detail(self, live_uuid: str) -> tuple[CatalogItem, PlaybackSource]:
        payload = self.live_stream(live_uuid)
        item = parse_live_card(payload)
        if item is None:
            raise GoPlayError("Play live stream is missing an id or title")
        drm_xml = _text(payload.get("drmXml"))
        manifests_obj = payload.get("manifestUrls") if isinstance(payload.get("manifestUrls"), dict) else {}
        extra_dash = _text(manifests_obj.get("dash"))
        extra_hls = _text(manifests_obj.get("hls"))
        try:
            url, protocol, alternates = _pick_manifest((), extra_dash=extra_dash, extra_hls=extra_hls)
        except GoPlayError as exc:
            raise AvailabilityError(str(exc)) from exc
        if drm_xml:
            alternates = ()
        source = PlaybackSource(
            manifest_url=url,
            manifest_type=protocol,
            alternate_manifest_urls=alternates,
            drm_xml=drm_xml,
            title=item.now_title or item.title,
            duration=None,
        )
        return item, source

    def live_playback(self, live_uuid: str) -> PlaybackSource:
        _item, source = self.live_detail(live_uuid)
        return source


def _eligibility_message(code: str) -> str:
    if code == "CONCURRENT_STREAM_LIMIT_EXCEEDED":
        return "Play refused playback because too many streams are already active"
    if code == "DEVICE_LIMIT_EXCEEDED":
        return "Play refused playback because this account has too many devices"
    if code == "EXPIRED":
        return "Play refused playback because the entitlement has expired"
    return "Play refused playback for this title"


def drm_headers(drm_xml: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/octet-stream",
        "customdata": drm_xml,
        "x-alb-key": DRM_ALB_KEY,
    }
