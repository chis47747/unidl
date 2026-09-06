"""BBC iPlayer catalogue and clear media-selector playback models."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse, urlunsplit
from xml.etree import ElementTree as ET

import requests

WWW = "https://www.bbc.co.uk/iplayer"
API_KEY = "q5wcnsqvnacnhjap7gzts9y6"
TLEO_QUERY_ID = "84633222c21447a3cd14f79dd6c878cf"
GRAPH = "https://graph.ibl.api.bbc.co.uk/"
EPISODE = (
    "https://ibl.api.bbci.co.uk/ibl/v1/episodes/{pid}"
    f"?rights=mobile&availability=available&mixin=live&api_key={API_KEY}"
)
SEARCH = (
    "https://ibl.api.bbci.co.uk/ibl/v1/new-search"
    f"?q={{query}}&rights=mobile&age_bracket=o18&mixin=live&api_key={API_KEY}"
)
CHANNELS = f"https://ibl.api.bbci.co.uk/ibl/v1/channels?rights=mobile&lang=en&region={{region}}&api_key={API_KEY}"
BROADCASTS = (
    "https://ibl.api.bbci.co.uk/ibl/v1/channels/{service}/broadcasts/"
    f"?rights=mobile&availability=available&from=-3h&per_page=40&api_key={API_KEY}"
)
TV_PLAYBACK = "https://www.live.bbctvapps.co.uk/taf-private/playback/data/iplayer:::{pid}"
OPEN_SELECTOR = "https://open.live.bbc.co.uk/mediaselector/6/select/version/2.0/mediaset/{mediaset}/vpid/{vpid}/"
SECURE_SELECTOR = (
    "https://securegate.iplayer.bbc.co.uk/mediaselector/6/select/version/2.0/"
    "vpid/{vpid}/format/json/mediaset/{mediaset}/proto/https"
)

USER_AGENT = "BBCiPlayer/5.60.0.37606"
USER_AGENT_UHD = "smarttv_AFTMM_Build_0003255372676_Chromium_41.0.2250.2"
USER_AGENT_TV = (
    "Mozilla/5.0 (Linux; U; Android 5.0.1; NVIDIA_SHIELD_MSE; smart-tv) "
    "ShieldExperience/1.0.0 (10.1.10.1; foster) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/76.0.3809.89 Safari/537.36"
)

# BBC calls these the regions of the ``bbc_one`` master brand.  The regular
# channel catalogue is deliberately scoped to one region, so fetch these only
# after a BBC One viewer explicitly asks to choose a variant.
BBC_ONE_REGIONS = (
    ("london", "London"),
    ("south", "South"),
    ("south_east", "South East"),
    ("east", "East"),
    ("east_midlands", "East Midlands"),
    ("west_midlands", "West Midlands"),
    ("west", "West"),
    ("south_west", "South West"),
    ("channel_islands", "Channel Islands"),
    ("yorkshire", "Yorkshire"),
    ("east_yorkshire", "East Yorkshire & Lincolnshire"),
    ("north_east", "North East & Cumbria"),
    ("north_west", "North West"),
    ("scotland", "Scotland"),
    ("wales", "Wales"),
    ("northern_ireland", "Northern Ireland"),
)

SecureFetch = Callable[[str, str], dict[str, Any] | None]

_PID = re.compile(r"^[a-z][a-z0-9_]+$", re.I)
_SEASON = re.compile(r"(?:series|season)\s+(\d+)", re.I)
_FISCAL_SEASON = re.compile(r"(\d{4})/(\d{2})\s*:\s*episode\s+\d+", re.I)
_NUMBER = re.compile(r"(?:^|:\s*)(\d+)\.\s*|episode\s+(\d+)", re.I)


class IPlayerError(RuntimeError):
    """The BBC API or media selector returned no usable result."""


@dataclass(frozen=True)
class ParsedInput:
    kind: str  # programme | episode | live | unknown
    pid: str
    series_id: str = ""


def parse_input(text: str) -> ParsedInput | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if _PID.fullmatch(raw):
        kind = "live" if raw.startswith("bbc_") or raw in {"cbbc", "cbeebies", "s4cpbs"} else "unknown"
        return ParsedInput(kind, raw.lower())
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if (parsed.hostname or "").lower() not in {"bbc.co.uk", "www.bbc.co.uk", "bbc.com", "www.bbc.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    series_id = (parse_qs(parsed.query).get("seriesId") or [""])[0]
    if len(parts) >= 2 and parts[0] == "programmes" and _PID.fullmatch(parts[1]):
        return ParsedInput("unknown", parts[1].lower(), series_id)
    if len(parts) < 3 or parts[0] != "iplayer":
        return None
    kind = {"episode": "episode", "episodes": "programme", "live": "live"}.get(parts[1])
    if kind is None or not _PID.fullmatch(parts[2]):
        return None
    return ParsedInput(kind, parts[2].lower(), series_id)


@dataclass(frozen=True)
class SearchHit:
    kind: str
    id: str
    title: str
    synopsis: str = ""
    category: str = ""

    @property
    def url(self) -> str:
        part = "episodes" if self.kind == "programme" else "live" if self.kind == "live" else "episode"
        return f"{WWW}/{part}/{self.id}"


@dataclass(frozen=True)
class Episode:
    id: str
    title: str
    name: str
    vpid: str
    season: int | None = None
    number: int | None = None
    year: str = ""
    synopsis: str = ""
    category: str = ""
    film: bool = False
    live: bool = False
    service_id: str = ""
    version_kind: str = ""
    tleo_id: str = ""
    parent_id: str = ""

    @property
    def label(self) -> str:
        if not self.film and self.season is not None and self.number is not None:
            return f"S{self.season:02d}E{self.number:02d}  {self.name or self.title}"
        value = self.name or self.title
        return f"{value} ({self.year})" if self.year else value


@dataclass
class Season:
    id: str
    title: str
    programme_id: str
    number: int | None = None
    episodes: list[Episode] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.title}  ·  {len(self.episodes)} episode(s)" if self.episodes else self.title


@dataclass
class Show:
    id: str
    title: str
    seasons: list[Season] = field(default_factory=list)


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    now: str = ""
    next: str = ""
    synopsis: str = ""
    episode_id: str = ""
    master_brand: str = ""
    region: str = ""

    @property
    def url(self) -> str:
        return f"{WWW}/live/{self.id}"


@dataclass(frozen=True)
class Source:
    manifest: str
    protocol: str
    quality: str
    subtitle: str = ""
    encrypted: bool = False

    def line(self) -> str:
        return " · ".join(
            value for value in (self.protocol, "clear", self.quality, "subtitles" if self.subtitle else "") if value
        )


class IPlayerApi:
    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        resolution: str = "auto",
        region: str = "london",
        secure: SecureFetch | None = None,
    ):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT_UHD if resolution in {"auto", "4k"} else USER_AGENT)
        self.resolution = resolution
        self.region = region
        self.secure = secure
        self._tv_playback_cache: dict[str, dict[str, Any]] = {}
        self._manifest_height_cache: dict[str, int] = {}
        self._region_variant_cache: dict[str, list[Channel]] = {}

    # -------------------------------------------------------------- catalogue
    def search(self, query: str) -> list[SearchHit]:
        data = self._json("GET", SEARCH.format(query=quote_plus(query)))
        hits: list[SearchHit] = []
        for raw in (data.get("new_search") or {}).get("results") or []:
            if not isinstance(raw, dict) or not raw.get("id") or not raw.get("title"):
                continue
            raw_type = str(raw.get("type") or "").lower()
            kind = (
                "programme"
                if raw_type == "programme"
                else "live"
                if raw_type == "channel" or raw.get("live")
                else "episode"
            )
            labels = raw.get("labels") or {}
            hits.append(
                SearchHit(
                    kind,
                    str(raw["id"]),
                    _text(raw.get("title")) or str(raw["id"]),
                    _text(raw.get("synopsis"), "small", "default"),
                    _text(labels.get("category")),
                )
            )
        return hits

    def show(self, pid: str) -> Show:
        data = self._programme(pid, None, 0)
        if not data:
            raise IPlayerError(f"BBC returned no programme {pid}")
        title = _text(data.get("title")) or pid
        seasons = []
        for raw in data.get("slices") or []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            label = _text(raw.get("title")) or str(raw["id"])
            if raw["id"] == "more-like-this" or "more like" in label.lower():
                continue
            match = _SEASON.search(label)
            seasons.append(Season(str(raw["id"]), label, pid, int(match.group(1)) if match else None))
        if not seasons:
            seasons = [Season("", "Episodes", pid)]
        return Show(pid, title, seasons)

    def season(self, season: Season) -> Season:
        data = self._programme(season.programme_id, season.id or None, 200)
        entities = ((data.get("entities") or {}).get("results") or []) if data else []
        episodes = []
        for entity in entities:
            raw = entity.get("episode") if isinstance(entity, dict) and entity.get("episode") else entity
            if isinstance(raw, dict) and raw.get("id"):
                episodes.append(_episode(raw, season.number))
        return Season(season.id, season.title, season.programme_id, season.number, episodes)

    def episode(self, pid: str) -> Episode:
        data = self._json("GET", EPISODE.format(pid=pid))
        entries = data.get("episodes") or []
        if not entries or not isinstance(entries[0], dict):
            raise IPlayerError(f"BBC returned no episode {pid}")
        raw = entries[0]
        default_season = 0 if raw.get("tleo_type") == "episode" else 1
        return self._enrich_episode(_episode(raw, default_season))

    def _enrich_episode(self, item: Episode) -> Episode:
        if not item.tleo_id or (item.name and item.season and item.number):
            return item
        try:
            raw = self._episode_in_programme(item.tleo_id, item.id, item.parent_id)
        except IPlayerError:
            return item
        if raw is None:
            return item
        enriched = _episode(raw)
        return replace(
            enriched,
            id=item.id or enriched.id,
            title=item.title or enriched.title,
            vpid=item.vpid or enriched.vpid,
            year=item.year or enriched.year,
            category=item.category or enriched.category,
            live=item.live or enriched.live,
            service_id=item.service_id or enriched.service_id,
            version_kind=item.version_kind or enriched.version_kind,
            tleo_id=item.tleo_id or enriched.tleo_id,
            parent_id=item.parent_id or enriched.parent_id,
        )

    def _episode_in_programme(self, programme_id: str, episode_id: str, parent_id: str = "") -> dict[str, Any] | None:
        if not programme_id or programme_id == episode_id:
            return None
        root = self._programme(programme_id, None, 0)
        slices = []
        for entry in root.get("slices") or []:
            if not isinstance(entry, dict):
                continue
            slice_id = str(entry.get("id") or "")
            label = _text(entry.get("title")).lower()
            if not slice_id or slice_id == "more-like-this" or "more like" in label:
                continue
            slices.append(slice_id)
        if parent_id:
            matching = [value for value in slices if value == parent_id or value.endswith(parent_id)]
            if matching:
                slices = matching
        for slice_id in slices or [None]:
            data = self._programme(programme_id, slice_id, 200)
            entities = (data.get("entities") or {}).get("results") or []
            for entity in entities:
                raw = entity.get("episode") if isinstance(entity, dict) and entity.get("episode") else entity
                if isinstance(raw, dict) and raw.get("id") == episode_id:
                    return raw
        return None

    def _programme(self, pid: str, slice_id: str | None, per_page: int) -> dict[str, Any]:
        data = self._json(
            "POST",
            GRAPH,
            json={
                "id": TLEO_QUERY_ID,
                "variables": {"id": pid, "perPage": per_page, "page": 1, "sliceId": slice_id},
            },
        )
        programme = (data.get("data") or {}).get("programme")
        if not isinstance(programme, dict):
            errors = data.get("errors") or []
            message = errors[0].get("message") if errors and isinstance(errors[0], dict) else "not found"
            raise IPlayerError(f"BBC programme metadata failed: {message}")
        return programme

    # ------------------------------------------------------------------- live
    def channels(self, region: str | None = None, *, include_schedule: bool = True) -> list[Channel]:
        selected_region = region or self.region
        data = self._json("GET", CHANNELS.format(region=selected_region))
        raw_channels = data.get("channels") or []
        if isinstance(raw_channels, dict):
            raw_channels = raw_channels.get("elements") or raw_channels.get("results") or []
        channels: list[Channel] = []
        for raw in raw_channels:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            channel_id = str(raw["id"])
            if "radio" in channel_id:
                continue  # radio belongs to the separate BBC Sounds service
            channel = Channel(
                id=channel_id,
                name=_text(raw.get("title")) or channel_id,
                master_brand=str(raw.get("master_brand_id") or raw.get("masterBrand") or ""),
                region=selected_region,
            )
            channels.append(self._scheduled_channel(channel) if include_schedule else channel)
        return channels

    def channel(self, channel_id: str) -> Channel:
        requested_region = next(
            (region for region, _label in BBC_ONE_REGIONS if channel_id == f"bbc_one_{region}"), self.region
        )
        channel = next(
            (channel for channel in self.channels(requested_region, include_schedule=False) if channel.id == channel_id),
            None,
        )
        return self._scheduled_channel(channel) if channel is not None else Channel(channel_id, channel_id)

    def _scheduled_channel(self, channel: Channel) -> Channel:
        try:
            current, following = self._broadcasts(channel.id)
        except IPlayerError:
            return channel
        current_ep = (current or {}).get("episode") or {}
        next_ep = (following or {}).get("episode") or {}
        return replace(
            channel,
            now=_text(current_ep.get("title")) or _text((current or {}).get("title")),
            next=_text(next_ep.get("title")) or _text((following or {}).get("title")),
            synopsis=_text(current_ep.get("synopsis"), "small", "default"),
            episode_id=str(current_ep.get("id") or ""),
        )

    def regional_variants(self, channel: Channel) -> list[Channel]:
        """Return the BBC One regional feeds without reloading their schedules."""

        if channel.master_brand != "bbc_one" and not channel.id.startswith("bbc_one_"):
            return [channel]
        cache_key = channel.master_brand or "bbc_one"
        if cache_key in self._region_variant_cache:
            return self._region_variant_cache[cache_key]

        variants: list[Channel] = []
        seen: set[str] = set()
        for region, label in BBC_ONE_REGIONS:
            try:
                regional_channels = self.channels(region, include_schedule=False)
            except IPlayerError:
                continue
            match = next(
                (
                    value
                    for value in regional_channels
                    if value.master_brand == cache_key or value.id.startswith(f"{cache_key}_")
                ),
                None,
            )
            if match is not None and match.id not in seen:
                seen.add(match.id)
                variants.append(replace(match, region=label))
        self._region_variant_cache[cache_key] = variants or [channel]
        return self._region_variant_cache[cache_key]

    def _broadcasts(self, service_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        data = self._json("GET", BROADCASTS.format(service=service_id))
        elements = (data.get("broadcasts") or {}).get("elements") or []
        now = datetime.now(timezone.utc)
        for index, raw in enumerate(elements):
            if not isinstance(raw, dict) or raw.get("blanked"):
                continue
            start = _date(raw.get("scheduled_start"))
            end = _date(raw.get("scheduled_end"))
            if start and end and start <= now <= end:
                following = elements[index + 1] if index + 1 < len(elements) else None
                return raw, following if isinstance(following, dict) else None
        return None, None

    # --------------------------------------------------------------- playback
    def source(self, item: Episode | Channel) -> Source:
        live = isinstance(item, Channel) or item.live
        vpids = [item.id] if isinstance(item, Channel) else [item.service_id or item.vpid]
        if live and self.resolution in {"auto", "4k"}:
            episode_pid = item.episode_id if isinstance(item, Channel) else item.id
            vpids = [*self._live_uhd_vpids(episode_pid), *vpids]
        vpids = list(dict.fromkeys(vpid for vpid in vpids if vpid))
        if not vpids:
            raise IPlayerError("BBC metadata contained no playable version id")
        for quality in _quality_plan(self.resolution):
            for vpid in vpids:
                result = self._source_for(vpid, quality, live)
                if result is not None:
                    return result
        raise IPlayerError("BBC playback is unavailable at the requested quality (often a UK region gate)")

    def _live_uhd_vpids(self, episode_pid: str) -> list[str]:
        if not episode_pid:
            return []
        data = self._tv_playback(episode_pid)
        if not data or data.get("uhdCapacityReached"):
            return []
        vpids = [str(data.get("uhdVersionId") or "")]
        for version in data.get("versions") or data.get("availableVersions") or []:
            if not isinstance(version, dict):
                continue
            vpids.append(str(version.get("uhdVersionId") or ""))
            version_id = str(version.get("id") or version.get("pid") or "")
            kind = str(version.get("kind") or "").lower()
            quality = str(version.get("quality") or version.get("type") or "").lower()
            stream_type = str(version.get("streamType") or version.get("stream_type") or "").lower()
            is_live = stream_type in {"live", "simulcast", "webcast"} or kind in {"simulcast", "webcast"}
            is_uhd = bool(
                version.get("uhd")
                or version.get("isUHD")
                or version.get("uhdAvailable")
                or kind == "uhd"
                or quality == "uhd"
            )
            if version_id and is_live and is_uhd:
                vpids.append(version_id)
        return list(dict.fromkeys(value for value in vpids if value))

    def _tv_playback(self, episode_pid: str) -> dict[str, Any]:
        if episode_pid in self._tv_playback_cache:
            return self._tv_playback_cache[episode_pid]
        try:
            data = self._json(
                "GET",
                TV_PLAYBACK.format(pid=episode_pid),
                params={
                    "includeLive": "true",
                    "autoplay": "true",
                    "uhdDevice": "true",
                    "version": "default",
                    "region": self.region,
                    "apiVersion": "2",
                    "prerollEligible": "false",
                },
                headers={"User-Agent": USER_AGENT_TV},
            )
        except IPlayerError:
            data = {}
        if data.get("code"):
            data = {}
        self._tv_playback_cache[episode_pid] = data
        return data

    def _source_for(self, vpid: str, quality: str, live: bool) -> Source | None:
        if quality == "uhd":
            if self.secure is None:
                return None
            payload = self.secure(vpid, "iptv-uhd")
        else:
            mediaset = "iptv-mse" if quality == "fhd" else "mobile-phone-main"
            try:
                payload = self._json(
                    "GET",
                    OPEN_SELECTOR.format(mediaset=mediaset, vpid=vpid),
                    headers={"User-Agent": USER_AGENT_TV if mediaset == "iptv-mse" else USER_AGENT},
                )
            except IPlayerError:
                return None
        media = [entry for entry in (payload or {}).get("media") or [] if isinstance(entry, dict)]
        if not media:
            return None
        subtitle = _subtitle(media)
        preferred = {
            "uhd": ("h265", "hevc"),
            "fhd": ("h265", "hevc", "h264", "avc"),
            "hd": ("h264", "avc"),
        }[quality]
        candidates = _connections(media, preferred)
        if quality == "fhd" and not live:
            h264_candidates = _connections(media, ("h264", "avc"))
            converted = next((_vod_hls(url) for _entry, url in h264_candidates if _vod_hls(url)), "")
            if converted and self._manifest_height(converted) >= 1080:
                return Source(converted, "HLS", "1080p H.264", subtitle)
        minimum = {"uhd": 2160, "fhd": 1080, "hd": 720}[quality]
        maximum = 1439 if quality == "fhd" else 1079 if quality == "hd" else None
        for entry, url in candidates:
            height = self._manifest_height(url) or _int(entry.get("height")) or 0
            if height < minimum or maximum is not None and height > maximum:
                continue
            protocol = "DASH" if url.split("?", 1)[0].endswith(".mpd") else "HLS"
            codec = _codec_label(entry)
            tier = "UHD " if quality == "uhd" else ""
            return Source(url, protocol, f"{height}p {tier}{codec}".strip(), subtitle)
        return None

    def _manifest_height(self, url: str) -> int:
        if not url:
            return 0
        if url in self._manifest_height_cache:
            return self._manifest_height_cache[url]
        height = 0
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
        except requests.RequestException:
            self._manifest_height_cache[url] = height
            return height
        if url.split("?", 1)[0].endswith(".m3u8"):
            height = max(
                (int(match.group(1)) for match in re.finditer(r"RESOLUTION=\d+x(\d+)", response.text, re.I)), default=0
            )
        else:
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError:
                root = None
            if root is not None:
                heights = []
                for element in root.iter():
                    for attr in ("height", "maxHeight"):
                        value = _int(element.get(attr))
                        if value:
                            heights.append(value)
                height = max(heights, default=0)
        self._manifest_height_cache[url] = height
        return height

    def subtitle(self, url: str) -> bytes:
        try:
            response = self.session.get(url, timeout=20)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise IPlayerError(f"BBC subtitle request failed: {exc}") from exc
        if not response.content:
            raise IPlayerError("BBC returned an empty subtitle")
        return response.content

    def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.session.request(method, url, timeout=20, **kwargs)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise IPlayerError(f"BBC request failed: {exc}") from exc
        except ValueError as exc:
            raise IPlayerError("BBC returned something that is not JSON") from exc
        if not isinstance(data, dict):
            raise IPlayerError("BBC returned something that is not an object")
        if data.get("result") and not data.get("media"):
            raise IPlayerError(f"BBC media selector refused playback: {data['result']}")
        return data


def parse_selector(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise IPlayerError("BBC secure media selector returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise IPlayerError("BBC secure media selector returned an unexpected value")
    if data.get("result") and not data.get("media"):
        raise IPlayerError(f"BBC secure media selector refused playback: {data['result']}")
    return data


def _episode(raw: dict[str, Any], default_season: int | None = None) -> Episode:
    title = _text(raw.get("title"), "default", "editorial") or str(raw.get("id") or "BBC")
    raw_subtitle = raw.get("subtitle")
    subtitle_default = _text(raw_subtitle, "default").strip()
    subtitle_slice = _text(raw_subtitle, "slice").strip()
    subtitle_editorial = _text(raw_subtitle, "editorial").strip()
    subtitle = subtitle_default or subtitle_slice or subtitle_editorial
    season_match = _SEASON.search(subtitle)
    fiscal_match = _FISCAL_SEASON.search(subtitle)
    number_match = _NUMBER.search(subtitle)
    has_numbering = bool(season_match or fiscal_match or number_match)
    if season_match:
        season = int(season_match.group(1))
    elif fiscal_match:
        season = int(f"{fiscal_match.group(1)}{fiscal_match.group(2)}")
    else:
        season = default_season if has_numbering else None
    if number_match:
        number = int(number_match.group(1) or number_match.group(2))
    elif has_numbering:
        number = _int(raw.get("numeric_tleo_position") or raw.get("numericTleoPosition"))
    else:
        number = None
    name_match = re.search(r"(?:^|:\s*)\d+\.\s*(.+)", subtitle)
    name = name_match.group(1) if name_match else subtitle
    if re.search(r"(?:series|season)\s+\d+\s*:\s*episode\s+\d+", subtitle, re.I):
        name = ""
    if not name and subtitle_editorial:
        name = re.sub(r"^\d+/\d+\s*", "", subtitle_editorial).strip()

    labels = raw.get("labels") or {}
    time_label = _text(labels.get("time")).lower()
    live_hint = bool(raw.get("live") or time_label == "live")
    versions = [
        value
        for value in raw.get("versions") or []
        if isinstance(value, dict)
        and (value.get("id") or value.get("pid"))
        and str(value.get("kind") or "").lower() not in {"audio-described", "signed"}
    ]
    live_hint = live_hint or any(str(value.get("kind") or "").lower() in {"simulcast", "webcast"} for value in versions)
    preferred_kinds = {"simulcast", "webcast"} if live_hint else {"original"}
    version = next(
        (value for value in versions if str(value.get("kind") or "").lower() in preferred_kinds),
        None,
    )
    version = version or (versions[0] if versions else {})
    version_kind = str(version.get("kind") or "").lower()
    live = bool(live_hint or version_kind in {"simulcast", "webcast"})

    category = _text(labels.get("category"))
    categories = [str(value).lower() for value in raw.get("categories") or []]
    original_title = _text(raw.get("original_title"), "default", "editorial").lower()
    film = bool(
        category.lower().startswith(("film", "movie"))
        or any(value in {"film", "films", "movie", "movies"} for value in categories)
        or re.match(r"^films?:\s*\d+\.", subtitle, re.I)
        or title.lower().endswith("(film)")
        or original_title.endswith("(film)")
    )
    year = ""
    date_values = (
        raw.get("release_date_time"),
        raw.get("releaseDateTime"),
        raw.get("release_date"),
        raw.get("releaseDate"),
        raw.get("first_broadcast_date_time"),
        raw.get("firstBroadcastDateTime"),
        raw.get("first_broadcast"),
        raw.get("firstBroadcast"),
        version.get("release_date_time"),
        version.get("releaseDateTime"),
        version.get("first_broadcast_date_time"),
        version.get("firstBroadcastDateTime"),
        version.get("first_broadcast"),
        version.get("firstBroadcast"),
    )
    for value in date_values:
        match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
        if match:
            year = match.group()
            break
    return Episode(
        id=str(raw.get("id") or ""),
        title=title,
        name=name,
        vpid=str(version.get("id") or version.get("pid") or ""),
        season=None if film else season,
        number=None if film else number,
        year=year,
        synopsis=_text(raw.get("synopsis"), "small", "default"),
        category=category,
        film=film,
        live=live,
        service_id=str(
            raw.get("service_id") or raw.get("serviceId") or version.get("service_id") or version.get("serviceId") or ""
        ),
        version_kind=version_kind,
        tleo_id=str(raw.get("tleo_id") or raw.get("tleoId") or ""),
        parent_id=str(raw.get("parent_id") or raw.get("parentId") or ""),
    )


def _text(value: Any, *keys: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in keys or ("default", "editorial", "small"):
            if value.get(key):
                return str(value[key])
    return ""


def _connections(
    media: list[dict[str, Any]], preferred_encodings: tuple[str, ...] = ()
) -> list[tuple[dict[str, Any], str]]:
    preferred = tuple(value.lower() for value in preferred_encodings)

    def sort_key(entry: dict[str, Any]) -> tuple[int, int, int]:
        encoding = str(entry.get("encoding") or "").lower()
        score = len(preferred) - preferred.index(encoding) if encoding in preferred else 0
        return score, _int(entry.get("height")) or 0, _int(entry.get("bitrate")) or 0

    found = []
    seen: set[str] = set()
    videos = (value for value in media if value.get("kind") == "video")
    for entry in sorted(videos, key=sort_key, reverse=True):
        connections = sorted(
            (value for value in entry.get("connection") or [] if isinstance(value, dict)),
            key=lambda value: (
                _connection_priority(value),
                str(value.get("protocol") or "").lower() != "https",
            ),
        )
        for connection in connections:
            href = str(connection.get("href") or "")
            transfer = str(connection.get("transferFormat") or "").lower()
            if href and href not in seen and transfer in {"dash", "hls"}:
                seen.add(href)
                found.append((entry, href))
    return found


def _codec_label(entry: dict[str, Any]) -> str:
    encoding = str(entry.get("encoding") or "").lower()
    if encoding in {"h265", "hevc", "hev1", "hvc1"}:
        return "HEVC"
    if encoding in {"h264", "avc", "avc1"}:
        return "H.264"
    return encoding.upper() or "video"


def _subtitle(media: list[dict[str, Any]]) -> str:
    for entry in media:
        if entry.get("kind") != "captions":
            continue
        connections = sorted(
            (value for value in entry.get("connection") or [] if isinstance(value, dict) and value.get("href")),
            key=lambda value: (
                _connection_priority(value),
                str(value.get("protocol") or "").lower() != "https",
            ),
        )
        if connections:
            return str(connections[0]["href"])
    return ""


def _connection_priority(connection: dict[str, Any]) -> int:
    priority = _int(connection.get("priority"))
    return priority if priority is not None else 99


def _vod_hls(url: str) -> str:
    parsed = urlparse(url)
    if ".ism/" not in parsed.path or "vod-dash-uk" not in parsed.netloc:
        return ""
    asset = parsed.path.split(".ism/", 1)[0] + ".ism"
    return urlunsplit(
        (parsed.scheme, parsed.netloc.replace("vod-dash-", "vod-hls-", 1), f"{asset}/hls/master.m3u8", "", "")
    )


def _quality_plan(resolution: str) -> tuple[str, ...]:
    return {
        "auto": ("uhd", "fhd", "hd"),
        "4k": ("uhd",),
        "1080p": ("fhd",),
        "720p": ("hd",),
    }.get(resolution, ("fhd", "hd"))


def _date(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "SECURE_SELECTOR",
    "USER_AGENT",
    "USER_AGENT_UHD",
    "WWW",
    "Channel",
    "Episode",
    "IPlayerApi",
    "IPlayerError",
    "ParsedInput",
    "SearchHit",
    "Season",
    "Show",
    "Source",
    "parse_input",
    "parse_selector",
]
