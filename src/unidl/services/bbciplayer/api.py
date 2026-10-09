"""BBC iPlayer catalogue and clear media-selector playback models."""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse, urlunsplit
from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter

WWW = "https://www.bbc.co.uk/iplayer"
API_KEY = "q5wcnsqvnacnhjap7gzts9y6"
TLEO_QUERY_ID = "84633222c21447a3cd14f79dd6c878cf"
GRAPH = "https://graph.ibl.api.bbc.co.uk/"
EPISODE = (
    "https://ibl.api.bbc.co.uk/ibl/v1/episodes/{pid}"
    f"?rights=mobile&availability=available&mixin=live&api_key={API_KEY}"
)
SEARCH = (
    "https://ibl.api.bbc.co.uk/ibl/v1/new-search"
    f"?q={{query}}&rights=mobile&age_bracket=o18&mixin=live&api_key={API_KEY}"
)
CHANNELS = f"https://ibl.api.bbc.co.uk/ibl/v1/channels?rights=mobile&lang=en&region={{region}}&api_key={API_KEY}"
BROADCASTS = (
    "https://ibl.api.bbc.co.uk/ibl/v1/channels/{service}/broadcasts/"
    f"?rights=mobile&availability=available&from=-3h&per_page=40&api_key={API_KEY}"
)
TV_PLAYBACK = "https://www.live.bbctvapps.co.uk/taf-private/playback/data/iplayer:::{pid}"
OPEN_SELECTOR = "https://open.live.bbc.co.uk/mediaselector/6/select/version/2.0/mediaset/{mediaset}/vpid/{vpid}/format/json/1"
SECURE_SELECTOR = "https://securegate.iplayer.bbc.co.uk/mediaselector/6/select/version/2.0/mediaset/{mediaset}/vpid/{vpid}/format/json/1"

USER_AGENT = "BBCiPlayer/5.17.2.32046"
USER_AGENT_UHD = USER_AGENT
USER_AGENT_TV = (
    "Mozilla/5.0 (Linux; U; Android 5.0.1; NVIDIA_SHIELD_MSE; smart-tv) "
    "ShieldExperience/1.0.0 (10.1.10.1; foster) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/76.0.3809.89 Safari/537.36"
)




BBC_CERTIFICATE_B64 = """LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tDQpNSUlFT3pDQ0F5T2dBd0lCQWdJQkFUQU5CZ2txaGtpRzl3MEJBUVVGQURDQm96RU
  xNQWtHQTFVRUJoTUNWVk14DQpFekFSQmdOVkJBZ1RDa05oYkdsbWIzSnVhV0V4RWpBUUJnTlZCQWNUQ1VOMWNHVnlkR2x1YnpFZU1C
  d0dBMVVFDQpDeE1WVUhKdlpDQlNiMjkwSUVObGNuUnBabWxqWVhSbE1Sa3dGd1lEVlFRTEV4QkVhV2RwZEdGc0lGQnliMlIxDQpZM1
  J6TVE4d0RRWURWUVFLRXdaQmJXRjZiMjR4SHpBZEJnTlZCQU1URmtGdFlYcHZiaUJHYVhKbFZGWWdVbTl2DQpkRU5CTURFd0hoY05N
  VFF4TURFMU1EQTFPREkyV2hjTk16UXhNREV3TURBMU9ESTJXakNCbVRFTE1Ba0dBMVVFDQpCaE1DVlZNeEV6QVJCZ05WQkFnVENrTm
  hiR2xtYjNKdWFXRXhFakFRQmdOVkJBY1RDVU4xY0dWeWRHbHViekVkDQpNQnNHQTFVRUN4TVVSR1YySUZKdmIzUWdRMlZ5ZEdsbWFX
  TmhkR1V4R1RBWEJnTlZCQXNURUVScFoybDBZV3dnDQpVSEp2WkhWamRITXhEekFOQmdOVkJBb1RCa0Z0WVhwdmJqRVdNQlFHQTFVRU
  F4TU5SbWx5WlZSV1VISnZaREF3DQpNVENDQVNBd0RRWUpLb1pJaHZjTkFRRUJCUUFEZ2dFTkFEQ0NBUWdDZ2dFQkFNRFZTNUwwVUR4
  WnMwNkpGMld2DQpuZE1KajdIVGRlSlg5b0ltWWg3aytNY0VENXZ5OTA2M0p5c3FkS0tsbzVJZERvY2tuczg0VEhWNlNCVkFBaTBEDQ
  p6cEI4dHRJNUFBM1l3djFZUDJiOThpQ3F2OWhQalZndE9nNHFvMXZkK0oxdFdISUh5ZkV6cWlPRXVXNTlVd2xoDQpVTmFvY3JtZGNx
  bGcyWmIyZ1VybTZ2dlZqUThZcjQzY29MNnBBMk5ESXNyT0Z4c0ZZaXdaVk12cDZqMlk4dnFrDQpFOHJ2Tm04c3JkY0FhZjRXdHBuYW
  gyZ3RBY3IrdTVYNExZdmEwTzZrNGhENEdnNHZQQ2xQZ0JXbDZFSHRBdnFDDQpGWm9KbDhMNTN2VVY1QWhMQjdKQk0wUTFXVERINWs4
  NWNYT2tFd042NDhuZ09hZUtPMGxqYndZVG52NHhDV2NlDQo2RXNDQVFPamdZTXdnWUF3SHdZRFZSMGpCQmd3Rm9BVVo2RFJJSlNLK2
  hmWCtHVnBycWlubGMraTVmZ3dIUVlEDQpWUjBPQkJZRUZOeUNPZkhja3Vpclp2QXF6TzBXbjZLTmtlR1BNQWtHQTFVZEV3UUNNQUF3
  RXdZRFZSMGxCQXd3DQpDZ1lJS3dZQkJRVUhBd0l3RVFZSllJWklBWWI0UWdFQkJBUURBZ2VBTUFzR0ExVWREd1FFQXdJSGdEQU5CZ2
  txDQpoa2lHOXcwQkFRVUZBQU9DQVFFQXZXUHd4b1VhV3IwV0tXRXhHdHpQOElGVUUrZis5SUZjSzNoWXl2QmxLOUxODQo3Ym9WZHhx
  dWJGeEgzMFNmOC90VnNYMUpBOUM3bnMzZ09jV2Z0dTEzeUtzK0RnZGhqdG5GVkgraW4zNkVpZEZBDQpRRzM1UE1PU0ltNGNaVXkwME
  4xRXRwVGpGY2VBbmF1ZjVJTTZNZmRBWlQ0RXNsL09OUHp5VGJYdHRCVlpBQmsxDQpXV2VHMEcwNDdUVlV6M2Ira0dOVTNzZEs5Ri9o
  NmRiS3c0azdlZWJMZi9KNjZKSnlkQUhybFhJdVd6R2tDbjFqDQozNWdHRHlQajd5MDZWNXV6MlUzYjlMZTdZWENnNkJCanBRN0wrRW
  d3OVVsSmpoN1pRMXU2R2RCNUEwcGFWM0VQDQpQTk1KN2J6Rkl1cHozdklPdk5nUVV4ZWs1SUVIczZKeXdjNXByck5MS3c9PQ0KLS0t
  LS1FTkQgQ0VSVElGSUNBVEUtLS0tLQ0KLS0tLS1CRUdJTiBQUklWQVRFIEtFWS0tLS0tDQpNSUlFdlFJQkFEQU5CZ2txaGtpRzl3ME
  JBUUVGQUFTQ0JLY3dnZ1NqQWdFQUFvSUJBUURBMVV1UzlGQThXYk5PDQppUmRscjUzVENZK3gwM1hpVi9hQ0ptSWU1UGpIQkErYjh2
  ZE90eWNyS25TaXBhT1NIUTZISko3UE9FeDFla2dWDQpRQUl0QTg2UWZMYlNPUUFOMk1MOVdEOW0vZklncXIvWVQ0MVlMVG9PS3FOYj
  NmaWRiVmh5QjhueE02b2poTGx1DQpmVk1KWVZEV3FISzVuWEtwWU5tVzlvRks1dXI3MVkwUEdLK04zS0MrcVFOalF5TEt6aGNiQldJ
  c0dWVEw2ZW85DQptUEw2cEJQSzd6WnZMSzNYQUduK0ZyYVoyb2RvTFFISy9ydVYrQzJMMnREdXBPSVErQm9PTHp3cFQ0QVZwZWhCDQ
  o3UUw2Z2hXYUNaZkMrZDcxRmVRSVN3ZXlRVE5FTlZrd3grWlBPWEZ6cEJNRGV1UEo0RG1uaWp0SlkyOEdFNTcrDQpNUWxuSHVoTEFn
  RURBb0lCQVFDQWpqSmgrRFY5a1NJMFcyVHVkUlBpQmwvTDRrNlc1VThCYnV3VW1LWGFBclVTDQpvZm8wZWhvY3h2aHNibTBNRTE4RX
  d4U0tKWWhPVVlWamdBRnpWOThLL2M4MjBLcXo1ZGRUa0NwRXFVd1Z4eXFRDQpOUWpsYzN3SmNjSTlQcVcrU09XaFdvYWd6UndYcmRE
  MFU0eXc2NHM1eGFIUkU2SEdRSkVQVHdEY21mSDlOK0JXDQovdVU4YVc1QWZOcHhqRzduSGF0cmhJQjU1cDZuNHNFNUVoTjBnSk9WMD
  lmMEdOb1pQUVhiT1VVcEJWOU1jQ2FsDQpsK1VTalpBRmRIbUlqWFBwR1FEelJJWTViY1hVQzBZYlRwaytRSmhrZ1RjSW1LRFJmd0FC
  YXRIdnlMeDlpaVY1DQp0ZWZoV1hhaDE4STdkbUF3TmRTN0U4QlpoL3d5MlIwNXQ0RHppYjlyQW9HQkFPU25yZXAybk1VRVAyNXdSQW
  RBDQozWDUxenYwOFNLWkh6b0VuNExRS1krLzg5VFRGOHZWS2wwQjZLWWlaYW14aWJqU1RtaDRCWHI4ZndRaytiazFCDQpReEZ3ZHVG
  eTd1MU43d0hSNU45WEFpNEtuamgxQStHcW9SYjg4bk43b1htekM3cTZzdFZRUk9peDJlRVFJWTVvDQpiREZUellaRnloNGlMdkU0bj
  V1WnVHL1JBb0dCQU5mazdHMDhvYlpacmsxSXJIVXZSQmVENzZRNDlzQ0lSMGRBDQpIU0hCZjBadFBEMjdGSEZtamFDN0YwWkM2QXdU
  RnBNL0FNWDR4UlpqNnhGalltYnlENGN3MFpGZ08rb0pwZjFIDQpFajNHSHdMNHFZekJFUXdRTmswSk9GbE84cDdVMm1ZL2hEVXM3bG
  JQQm82YUo4VVpJMGs3SHhSOVRWYVhud0h1DQovaXhnRjlsYkFvR0JBSmh2eVViNXZkaXRmNTcxZ3ErQWs2bWozMU45aGNRdjN3REZR
  SGdHN1Vxb28zaUQ5MDR4DQp1aXI4RzdCbVJ2THNTWGhpWnI2cmxIOXFnTERVU1lqV0xMWksrZXVoOUo0ejlLdmhReitQVnNsY2FYcj
  RyVUVjDQphMlNvb2FKU2E2WjNYU2NuSWVPSzJKc2hPK3RnRmw3d1NDRGlpUVF1aHI3QmRLRFFhbWU3MEVxTEFvR0JBSS90DQo4dk45
  d1NRN3lZamJIYU4wMkErdFNtMTdUeXNGaE5vcXZoYUEvNFJJMHRQU0RhRHZDUlhTRDRRc21ySzNaR0lxDQpBSVA3TGc3dFIyRHM3RV
  NoWDY5MTRRdVZmVWF4R1ZPRXR0UFphZ0g3RzdNcllMSzFlWWl3MER1Sjl4U041dTdWDQpBczRkOURuZldiUm14UzRRd2pEU0ZMaFRp
  T1JsRkt2MHFYTHF1cERuQW9HQWVFa3J4SjhJaXdhVEhnWXltM21TDQprU2h5anNWK01tVkJsVHNRK0ZabjFTM3k0YVdxbERhNUtMZF
  QvWDEwQXg4NHNQTmVtQVFVMGV4YTN0OHM5bHdIDQorT3NEaktLb3hqQ1Q3S2wzckdQeUFISnJmVlZ5U2VFZVgrOERLZFZKcjByU1Bk
  Qkk4Y2tFQ3kzQXpsVmphK3d3DQpST0N0emMxVHVyeG5OQTVxV0QzbjNmND0NCi0tLS0tRU5EIFBSSVZBVEUgS0VZLS0tLS0NCg=="""

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

_PID = re.compile(r"^[a-z][a-z0-9_]+$", re.I)
_SEASON = re.compile(r"(?:series|season)\s+(\d+)", re.I)
_FISCAL_SEASON = re.compile(r"(\d{4})/(\d{2})\s*:\s*episode\s+\d+", re.I)
_NUMBER = re.compile(r"(?:^|:\s*)(\d+)\.\s*|episode\s+(\d+)", re.I)


class IPlayerError(RuntimeError):
    """The BBC API or media selector returned no usable result."""


@dataclass(frozen=True)
class ParsedInput:
    kind: str
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
    image_url: str = ""
    poster_url: str = ""

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
    image_url: str = ""
    poster_url: str = ""

    @property
    def url(self) -> str:
        return f"{WWW}/live/{self.id}"


@dataclass(frozen=True)
class Source:
    manifest: str
    protocol: str
    quality: str
    subtitle: str = ""
    chapters: tuple[dict[str, Any], ...] = ()
    encrypted: bool = False
    image_url: str = ""
    poster_url: str = ""

    def line(self) -> str:
        return " · ".join(
            value for value in (self.protocol, "clear", self.quality, "subtitles" if self.subtitle else "") if value
        )


class _BBCSSLAdapter(HTTPAdapter):
    def __init__(self, context: ssl.SSLContext):
        self.context = context
        super().__init__()

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["ssl_context"] = self.context
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args: Any, **kwargs: Any):
        kwargs["ssl_context"] = self.context
        return super().proxy_manager_for(*args, **kwargs)


class IPlayerApi:
    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        resolution: Any = "auto",
        region: str = "london",
    ):
        self.profiles = normalize_source_profiles(resolution)
        if self.profiles == ("auto",):
            self.resolution = "auto"
        else:
            self.resolution = self.profiles[0].split("-", 1)[0]
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.region = region
        self._tv_playback_cache: dict[str, dict[str, Any]] = {}
        self._manifest_height_cache: dict[str, int] = {}
        self._subtitle_url_cache: dict[str, str] = {}
        self._region_variant_cache: dict[str, list[Channel]] = {}
        self._selector_cache: dict[tuple[str, str, bool], dict[str, Any]] = {}


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
        seasons = _seasons_from(data, pid)
        if not seasons:
            data = self._programme(pid, None, 200)
            title = _text(data.get("title")) or title
            seasons = _seasons_from(data, pid)
        if not seasons:
            seasons = [Season("", "Episodes", pid)]
        return Show(pid, title, seasons)

    def season(self, season: Season) -> Season:
        episodes: list[Episode] = []
        seen: set[str] = set()
        page = 1
        while page <= 20:
            data = self._programme(season.programme_id, season.id or None, 200, page=page)
            entities = _entity_results(data)
            added = 0
            for entity in entities:
                raw = entity.get("episode") if isinstance(entity, dict) and entity.get("episode") else entity
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                episode_id = str(raw["id"])
                if episode_id in seen:
                    continue
                seen.add(episode_id)
                episodes.append(_episode(raw, season.number))
                added += 1
            total = _int(_mapping(data.get("entities")).get("total"))
            if added == 0 or len(entities) < 200 or (total is not None and len(episodes) >= total):
                break
            page += 1
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
            image_url=item.image_url or enriched.image_url,
            poster_url=item.poster_url or enriched.poster_url,
        )

    def _episode_in_programme(self, programme_id: str, episode_id: str, parent_id: str = "") -> dict[str, Any] | None:
        if not programme_id or programme_id == episode_id:
            return None
        root = self._programme(programme_id, None, 0)
        slices = [entry.id for entry in _seasons_from(root, programme_id) if entry.id]
        if not slices:
            slices = [entry.id for entry in _seasons_from(self._programme(programme_id, None, 200), programme_id) if entry.id]
        if parent_id:
            matching = [value for value in slices if value == parent_id or value.endswith(parent_id)]
            if matching:
                slices = matching
        for slice_id in slices or [None]:
            data = self._programme(programme_id, slice_id, 200)
            entities = _entity_results(data)
            for entity in entities:
                raw = entity.get("episode") if isinstance(entity, dict) and entity.get("episode") else entity
                if isinstance(raw, dict) and raw.get("id") == episode_id:
                    return raw
        return None

    def _programme(self, pid: str, slice_id: str | None, per_page: int, page: int = 1) -> dict[str, Any]:
        data = self._json(
            "POST",
            GRAPH,
            json={
                "id": TLEO_QUERY_ID,
                "variables": {"id": pid, "perPage": per_page, "page": max(1, int(page or 1)), "sliceId": slice_id},
            },
        )
        programme = (data.get("data") or {}).get("programme")
        if not isinstance(programme, dict):
            errors = data.get("errors") or []
            message = errors[0].get("message") if errors and isinstance(errors[0], dict) else "not found"
            raise IPlayerError(f"BBC programme metadata failed: {message}")
        return programme

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
                continue
            image_url, poster_url = _images(raw)
            channel = Channel(
                id=channel_id,
                name=_text(raw.get("title")) or channel_id,
                master_brand=str(raw.get("master_brand_id") or raw.get("masterBrand") or ""),
                region=selected_region,
                image_url=image_url,
                poster_url=poster_url,
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
        programme_image, programme_poster = _images(current_ep)
        return replace(
            channel,
            now=_text(current_ep.get("title")) or _text((current or {}).get("title")),
            next=_text(next_ep.get("title")) or _text((following or {}).get("title")),
            synopsis=_text(current_ep.get("synopsis"), "small", "default"),
            episode_id=str(current_ep.get("id") or ""),
            poster_url=programme_poster or programme_image or channel.poster_url,
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


    def _secure_selector(self, vpid: str, mediaset: str) -> dict[str, Any]:
        cert_path = ""
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        if self.session.proxies:
            session.proxies.update(self.session.proxies)
        context = ssl.create_default_context()
        try:
            context.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            context.set_ciphers("DEFAULT:@SECLEVEL=1")
        adapter = _BBCSSLAdapter(context)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        try:
            data = base64.b64decode("".join(BBC_CERTIFICATE_B64.split()))
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pem") as handle:
                handle.write(data)
                cert_path = handle.name
            response = session.get(
                SECURE_SELECTOR.format(mediaset=mediaset, vpid=vpid),
                cert=cert_path,
                timeout=20,
            )
            if response.status_code != 200:
                return {}
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, requests.RequestException):
            return {}
        finally:
            session.close()
            if cert_path:
                try:
                    os.unlink(cert_path)
                except OSError:
                    pass

    def sources(self, item: Episode | Channel) -> list[Source]:
        live = isinstance(item, Channel) or item.live
        vpids = [item.id] if isinstance(item, Channel) else [item.service_id or item.vpid]
        plan = source_profile_plan(self.profiles)
        if live and any(quality == "uhd" for quality, _protocol in plan):
            episode_pid = item.episode_id if isinstance(item, Channel) else item.id
            vpids = [*self._live_uhd_vpids(episode_pid), *vpids]
        vpids = list(dict.fromkeys(vpid for vpid in vpids if vpid))
        if not vpids:
            raise IPlayerError("BBC metadata contained no playable version id")
        image_url = item.image_url
        poster_url = item.poster_url
        listing = None if live or isinstance(item, Channel) else self._episode_listing(item.id)
        if listing is not None:
            listing_image, listing_poster = _images(listing)
            image_url = listing_image or image_url
            poster_url = listing_poster or poster_url
        chapters = () if listing is None else tuple(self._chapters_from_listing(listing, vpids))
        stop_at_first = live or self.profiles == ("auto",)
        found: list[Source] = []
        seen: set[str] = set()
        for quality, protocol in plan:
            for vpid in vpids:
                result = self._source_for(vpid, quality, live, protocol=protocol)
                if result is None or result.manifest in seen:
                    continue
                seen.add(result.manifest)
                found.append(replace(result, chapters=chapters, image_url=image_url, poster_url=poster_url))
                break
            if found and stop_at_first:
                return found
        if not found:
            requested = ", ".join(_profile_label(quality, protocol) for quality, protocol in plan)
            raise IPlayerError(
                f"BBC playback is unavailable at the requested quality ({requested}; often a UK region gate)"
            )
        return found

    def source(self, item: Episode | Channel) -> Source:
        return self.sources(item)[0]

    def _episode_listing(self, pid: str) -> dict[str, Any] | None:
        try:
            data = self._json("GET", EPISODE.format(pid=pid))
        except IPlayerError:
            return None
        episodes = data.get("episodes") or []
        if not episodes or not isinstance(episodes[0], dict):
            return None
        return episodes[0]

    def _chapters_for_episode(self, pid: str, vpids: list[str]) -> list[dict[str, Any]]:
        return self._chapters_from_listing(self._episode_listing(pid), vpids)

    def _chapters_from_listing(self, raw: dict[str, Any] | None, vpids: list[str]) -> list[dict[str, Any]]:
        if not raw:
            return []
        versions = [value for value in raw.get("versions") or [] if isinstance(value, dict)]
        selected = next(
            (value for value in versions if str(value.get("id") or value.get("pid") or "") in vpids),
            None,
        )
        selected = selected or next(
            (value for value in versions if str(value.get("kind") or "").lower() not in {"audio-described", "signed"}),
            None,
        )
        if not selected:
            return []
        values: list[dict[str, Any]] = []
        seen: set[tuple[int, str]] = set()

        def add(seconds: Any, title: str, kind: str) -> None:
            try:
                start_ms = max(0, int(round(float(seconds) * 1000)))
            except (TypeError, ValueError):
                return
            key = (start_ms, title)
            if key in seen:
                return
            seen.add(key)
            values.append({"start_ms": start_ms, "title": title, "kind": kind})

        for interaction in selected.get("interactions") or []:
            if not isinstance(interaction, dict):
                continue
            subtype = str(interaction.get("subtype") or "").lower()
            title_data = interaction.get("title") or {}
            title = _text(title_data, "long", "short") or subtype.title() or "Chapter"
            points = interaction.get("interaction_points") or {}
            show_from = points.get("show_from")
            skip_to = points.get("skip_to")
            if subtype == "intro":
                add(show_from, "Intro", "intro")
                add(skip_to, "Content", "scene")
            elif subtype == "recap":
                add(show_from, "Recap", "recap")
                add(skip_to, "Content", "scene")
            else:
                add(show_from, title, subtype or "scene")
                add(skip_to, "Content", "scene")
        if selected.get("credits_start") is not None:
            add(selected.get("credits_start"), "Credits", "credits")
        values.sort(key=lambda value: int(value.get("start_ms") or 0))
        return values

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

    def _selector_payload(self, vpid: str, quality: str, live: bool) -> dict[str, Any]:
        mediaset = (
            "iptv-uhd"
            if quality == "uhd"
            else "iptv-all"
            if not live
            else ("iptv-mse" if quality == "fhd" else "mobile-phone-main")
        )
        key = (vpid, mediaset, bool(live))
        cached = self._selector_cache.get(key)
        if cached is not None:
            return cached
        payload: dict[str, Any] = {}
        if quality == "uhd":
            payload = self._secure_selector(vpid, "iptv-uhd")
        else:
            try:
                payload = self._json(
                    "GET",
                    OPEN_SELECTOR.format(mediaset=mediaset, vpid=vpid),
                    headers={"User-Agent": USER_AGENT_TV if live and mediaset == "iptv-mse" else USER_AGENT},
                )
            except IPlayerError:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        self._selector_cache[key] = payload
        return payload

    def _source_for(self, vpid: str, quality: str, live: bool, *, protocol: str | None = None) -> Source | None:
        payload = self._selector_payload(vpid, quality, live)
        media = [entry for entry in (payload or {}).get("media") or [] if isinstance(entry, dict)]
        if not media:
            return None
        subtitle = self._subtitle_url(media)
        preferred = {
            "uhd": ("h265", "hevc"),
            "fhd": ("h264", "avc"),
            "hd": ("h264", "avc"),
        }[quality]
        wanted = str(protocol or "").strip().lower()
        protocol_order: tuple[str, ...] = (wanted,) if wanted in {"dash", "hls"} else ()
        if not protocol_order and not live:
            if quality == "fhd":
                protocol_order = ("hls",)
            elif quality == "uhd":
                protocol_order = ("dash",)
        candidates = _connections(media, preferred, protocol_order)
        minimum = {"uhd": 2160, "fhd": 1080, "hd": 720}[quality]
        maximum = 1439 if quality == "fhd" else 1079 if quality == "hd" else None
        for entry, connection, raw_url in candidates:
            transfer = str(connection.get("transferFormat") or "").lower()
            encoding = str(entry.get("encoding") or "").lower()
            if wanted in {"dash", "hls"} and transfer != wanted:
                continue
            if not wanted and not live and quality == "fhd" and (transfer != "hls" or encoding not in {"h264", "avc", "avc1"}):
                continue
            if not live and quality == "uhd" and encoding not in {"h265", "hevc", "hev1", "hvc1"}:
                continue
            url = _normalize_vod_manifest(raw_url, transfer) if not live else raw_url
            height = self._manifest_height(url)
            if not height:
                continue
            if height < minimum or maximum is not None and height > maximum:
                continue
            kind = "DASH" if transfer == "dash" or url.split("?", 1)[0].endswith(".mpd") else "HLS"
            codec = _codec_label(entry)
            tier = "UHD " if quality == "uhd" else ""
            return Source(url, kind, f"{height}p {tier}{codec}".strip(), subtitle)
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

    def _subtitle_url(self, media: list[dict[str, Any]]) -> str:
        candidates: list[tuple[int, int, int, str]] = []
        for entry in media:
            if str(entry.get("kind") or "").lower() != "captions":
                continue
            for connection in entry.get("connection") or []:
                if not isinstance(connection, dict):
                    continue
                url = str(connection.get("href") or "").strip()
                if not url:
                    continue
                supplier = str(connection.get("supplier") or "").lower()
                protocol = str(connection.get("protocol") or "").lower()
                supplier_rank = 0 if "cloudfront" in supplier else 1 if "bidi" in supplier else 2 if "akamai" in supplier else 3
                protocol_rank = 0 if protocol == "https" or url.lower().startswith("https://") else 1
                candidates.append((supplier_rank, protocol_rank, _connection_priority(connection), url))
        if not candidates:
            return ""
        candidates.sort(key=lambda value: value[:3])
        return candidates[0][3]

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
    if has_numbering:
        # BBC exposes the user-facing episode label separately in ``slice``:
        # e.g. ``default = Series 1: Episode 2`` and ``slice = Episode 2``.
        # The editorial value is often a synopsis (or ``2/6 ...``), so it must
        # not replace the episode name in the picker.
        name = subtitle_slice
        if not name:
            episode_match = re.search(r"\bepisode\s+\d+\b", subtitle, re.I)
            name = episode_match.group(0) if episode_match else ""
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
    image_url, poster_url = _images(raw)
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
        image_url=image_url,
        poster_url=poster_url,
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _entity_results(data: dict[str, Any] | None) -> list[Any]:
    results = _mapping(data).get("entities")
    if isinstance(results, dict):
        rows = results.get("results") or results.get("elements") or []
        return list(rows) if isinstance(rows, list) else []
    if isinstance(results, list):
        return results
    return []


def _slice_rows(data: dict[str, Any] | None) -> list[Any]:
    rows = _mapping(data).get("slices")
    if isinstance(rows, dict):
        rows = rows.get("elements") or rows.get("results") or []
    return list(rows) if isinstance(rows, list) else []


def _seasons_from(data: dict[str, Any] | None, programme_id: str) -> list[Season]:
    seasons: list[Season] = []
    seen: set[str] = set()
    for raw in _slice_rows(data):
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        slice_id = str(raw["id"])
        label = _text(raw.get("title")) or slice_id
        if slice_id == "more-like-this" or "more like" in label.lower():
            continue
        if slice_id in seen:
            continue
        seen.add(slice_id)
        match = _SEASON.search(label)
        seasons.append(Season(slice_id, label, programme_id, int(match.group(1)) if match else None))
    return seasons


def _text(value: Any, *keys: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in keys or ("default", "editorial", "small"):
            if value.get(key):
                return str(value[key])
    return ""


def expand_image(url: str, recipe: str = "raw") -> str:
    """Fill a BBC ichef ``{recipe}`` (or ``{width}x{height}``) template."""
    text = str(url or "").strip()
    if not text:
        return ""
    chosen = str(recipe or "raw").strip() or "raw"
    return text.replace("{recipe}", chosen).replace("{width}x{height}", chosen)


def _image_entry_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(
            value.get("url")
            or value.get("templatedUrl")
            or value.get("templateUrl")
            or value.get("src")
            or value.get("href")
            or value.get("standard")
            or ""
        ).strip()
    return ""


def _images(raw: dict[str, Any] | None) -> tuple[str, str]:
    """Return ``(landscape, portrait)`` ichef URL templates from an IBL or GraphQL object."""
    if not isinstance(raw, dict):
        return "", ""
    standard = ""
    portrait = ""
    images = raw.get("images") if raw.get("images") is not None else raw.get("image")
    if isinstance(images, str):
        standard = images.strip()
    elif isinstance(images, dict):
        standard = str(
            images.get("standard")
            or images.get("landscape")
            or images.get("promotional")
            or images.get("default")
            or images.get("url")
            or images.get("templatedUrl")
            or images.get("templateUrl")
            or ""
        ).strip()
        portrait = str(images.get("vertical") or images.get("portrait") or "").strip()
        kind = str(images.get("type") or images.get("kind") or "").strip().lower()
        if kind in {"portrait", "vertical", "poster"} and standard and not portrait:
            portrait = standard
            standard = ""
    elif isinstance(images, list):
        for entry in images:
            url = _image_entry_url(entry)
            if not url:
                continue
            kind = ""
            if isinstance(entry, dict):
                kind = str(entry.get("type") or entry.get("kind") or "").strip().lower()
            if kind in {"portrait", "vertical", "poster"} and not portrait:
                portrait = url
            elif not standard:
                standard = url
    if not standard and not portrait:
        fallback = raw.get("image_url") or raw.get("imageUrl") or raw.get("image")
        standard = _image_entry_url(fallback)
    return standard, portrait


_SOURCE_PROFILE_ALIASES = {
    "auto": "auto",
    "best": "auto",
    "4k": "uhd-dash",
    "2160p": "uhd-dash",
    "uhd": "uhd-dash",
    "1080p": "fhd-hls",
    "fhd": "fhd-hls",
    "720p": "hd-dash",
    "hd": "hd-dash",
    "uhd-dash": "uhd-dash",
    "uhd-hls": "uhd-hls",
    "fhd-dash": "fhd-dash",
    "fhd-hls": "fhd-hls",
    "hd-dash": "hd-dash",
    "hd-hls": "hd-hls",
}
_SOURCE_PROFILE_IDS = (
    "auto",
    "uhd-dash",
    "uhd-hls",
    "fhd-dash",
    "fhd-hls",
    "hd-dash",
    "hd-hls",
)


def normalize_source_profiles(value: Any) -> tuple[str, ...]:
    """Coerce a stored Playback source value into exclusive auto or explicit rows."""

    if isinstance(value, str):
        parts = [part.strip() for part in value.replace("\n", ",").split(",")]
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = list(value)
    else:
        parts = [] if value in (None, "") else [value]
    mapped: list[str] = []
    for part in parts:
        name = _SOURCE_PROFILE_ALIASES.get(str(part or "").strip().lower())
        if name in _SOURCE_PROFILE_IDS and name not in mapped:
            mapped.append(name)
    if not mapped or mapped[-1] == "auto":
        return ("auto",)
    return tuple(item for item in mapped if item != "auto") or ("auto",)


def source_profile_plan(profiles: tuple[str, ...] | str) -> list[tuple[str, str | None]]:
    """Return ``(quality, protocol)`` rows. ``protocol`` is None for auto preference."""

    selected = normalize_source_profiles(profiles)
    if selected == ("auto",):
        return [("uhd", None), ("fhd", None), ("hd", None)]
    rows: list[tuple[str, str | None]] = []
    for name in selected:
        quality, separator, protocol = name.partition("-")
        if quality in {"uhd", "fhd", "hd"} and protocol in {"dash", "hls"}:
            rows.append((quality, protocol))
        elif separator == "" and quality in {"uhd", "fhd", "hd"}:
            rows.append((quality, None))
    return rows or [("uhd", None), ("fhd", None), ("hd", None)]


def _profile_label(quality: str, protocol: str | None) -> str:
    suffix = f" {protocol.upper()}" if protocol else ""
    return f"{quality.upper()}{suffix}"


def _quality_plan(resolution: str) -> tuple[str, ...]:
    """Return source qualities in preference order for the configured setting."""

    return tuple(dict.fromkeys(quality for quality, _protocol in source_profile_plan(resolution)))


def _connections(
    media: list[dict[str, Any]],
    preferred_encodings: tuple[str, ...] = (),
    preferred_protocols: tuple[str, ...] = (),
) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    preferred = tuple(value.lower() for value in preferred_encodings)
    protocol_preference = tuple(value.lower() for value in preferred_protocols)

    def sort_key(entry: dict[str, Any]) -> tuple[int, int, int]:
        encoding = str(entry.get("encoding") or "").lower()
        score = len(preferred) - preferred.index(encoding) if encoding in preferred else 0
        return score, _int(entry.get("height")) or 0, _int(entry.get("bitrate")) or 0

    def connection_key(value: dict[str, Any]) -> tuple[int, int, int, int]:
        transfer = str(value.get("transferFormat") or "").lower()
        protocol_score = protocol_preference.index(transfer) if transfer in protocol_preference else len(protocol_preference)
        supplier = str(value.get("supplier") or "").lower()
        supplier_score = 0 if "cloudfront" in supplier else 1 if "bidi" in supplier else 2 if "akamai" in supplier else 3
        return (
            protocol_score,
            supplier_score,
            _connection_priority(value),
            str(value.get("protocol") or "").lower() != "http",
        )

    found = []
    seen: set[str] = set()
    videos = (value for value in media if value.get("kind") == "video")
    for entry in sorted(videos, key=sort_key, reverse=True):
        connections = sorted(
            (value for value in entry.get("connection") or [] if isinstance(value, dict)),
            key=connection_key,
        )
        for connection in connections:
            href = str(connection.get("href") or "")
            transfer = str(connection.get("transferFormat") or "").lower()
            if href and href not in seen and transfer in {"dash", "hls"}:
                seen.add(href)
                found.append((entry, connection, href))
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


def _normalize_vod_manifest(url: str, transfer: str) -> str:
    if transfer != "hls":
        return url
    parsed = urlparse(url)
    if ".ism" not in parsed.path:
        return url
    if "/hls/master.m3u8" in parsed.path:
        return url
    asset = parsed.path.split(".ism", 1)[0] + ".ism"
    return urlunsplit((parsed.scheme, parsed.netloc, f"{asset}/hls/master.m3u8", "", ""))


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
    "expand_image",
    "parse_input",
    "parse_selector",
]
