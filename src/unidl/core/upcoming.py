"""Multi-source upcoming releases radar (JustWatch, TVMaze, AniList, TMDB).

Users can enable any single source or combination of sources via the
``upcoming_sources`` setting (default: ``justwatch``, ``tvmaze``, ``anilist``).
TMDB is optional and requires a user-supplied API key (v3 key or v4 Bearer
token); keys are never written to logs or surfaced in error messages.
"""

from __future__ import annotations

import concurrent.futures
import html
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from . import justwatch as jw
from .redact import redact
from .scheduler import default_release_hour_utc, format_iso_utc, utc_now

UPCOMING_SOURCES: tuple[str, ...] = ("justwatch", "tvmaze", "anilist", "tmdb")
DEFAULT_UPCOMING_SOURCES: tuple[str, ...] = ("justwatch", "tvmaze", "anilist")

SOURCE_LABELS: dict[str, str] = {
    "justwatch": "JustWatch",
    "tvmaze": "TVMaze",
    "anilist": "AniList",
    "tmdb": "TMDB",
}

TVMAZE_WEB_SCHEDULE_URL = "https://api.tvmaze.com/schedule/web"
ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"
TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CLEAN_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")

_JW_UPCOMING_QUERY = """
query UpcomingTitles($country: Country!, $language: Language!, $first: Int!, $filter: TitleFilter) {
  popularTitles(country: $country, first: $first, filter: $filter) {
    edges {
      node {
        id
        objectType
        ... on MovieOrShow {
          offers(country: $country, platform: WEB) {
            standardWebURL
            package {
              clearName
              technicalName
              packageId
            }
          }
        }
        content(country: $country, language: $language) {
          title
          originalTitle
          originalReleaseYear
          fullPath
          posterUrl
          shortDescription
          upcomingReleases(releaseTypes: [DIGITAL]) {
            releaseDate
            releaseCountDown(country: $country)
            label
            package {
              clearName
              technicalName
              packageId
            }
          }
        }
      }
    }
  }
}
"""

_JW_SHOW_EPISODES_QUERY = """
query ShowUpcomingEpisodes($nodeId: ID!, $country: Country!, $language: Language!) {
  node(id: $nodeId) {
    ... on Show {
      seasons(limit: 50) {
        content(country: $country, language: $language) {
          seasonNumber
          title
        }
        episodes {
          content(country: $country, language: $language) {
            seasonNumber
            episodeNumber
            title
            originalReleaseDate
          }
        }
      }
    }
  }
}
"""

_ANILIST_AIRING_QUERY = """
query UpcomingAnime($perPage: Int!, $until: Int!) {
  Page(perPage: $perPage) {
    airingSchedules(notYetAired: true, airingAt_lesser: $until, sort: TIME) {
      airingAt
      episode
      media {
        id
        seasonYear
        siteUrl
        description(asHtml: false)
        title {
          english
          romaji
        }
        coverImage {
          large
          medium
        }
        externalLinks {
          site
          url
        }
      }
    }
  }
}
"""

_ANILIST_STREAMING_SITES: dict[str, tuple[str, str]] = {
    "crunchyroll": ("crunchyroll", "Crunchyroll"),
    "netflix": ("netflix", "Netflix"),
    "hulu": ("hulu", "Hulu"),
    "disney plus": ("disneyplus", "Disney+"),
    "disney+": ("disneyplus", "Disney+"),
    "amazon": ("amazonprimevideo", "Prime Video"),
    "amazon prime video": ("amazonprimevideo", "Prime Video"),
    "hidive": ("hidive", "HIDIVE"),
    "bilibili": ("bilibili", "Bilibili"),
    "bilibili tv": ("bilibili", "Bilibili"),
    "animation digital network": ("animedigitalnetwork", "ADN"),
    "adn": ("animedigitalnetwork", "ADN"),
    "apple tv": ("appletvplus", "Apple TV"),
}


def normalize_upcoming_sources(value: Any) -> tuple[str, ...]:
    """Coerce setting value into a deduplicated tuple of valid source IDs."""
    if value is None:
        return DEFAULT_UPCOMING_SOURCES
    if isinstance(value, str):
        raw_items = [part.strip().lower() for part in value.replace("\n", ",").split(",")]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw_items = [str(part).strip().lower() for part in value]
    else:
        return DEFAULT_UPCOMING_SOURCES
    cleaned: list[str] = []
    for item in raw_items:
        if item in UPCOMING_SOURCES and item not in cleaned:
            cleaned.append(item)
    return tuple(cleaned)


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    unescaped = html.unescape(_HTML_TAG_RE.sub(" ", str(text)))
    return _SPACE_RE.sub(" ", unescaped).strip()


_AGGREGATOR_HOSTS: frozenset[str] = frozenset(
    {
        "justwatch.com",
        "tvmaze.com",
        "anilist.co",
        "themoviedb.org",
        "imdb.com",
        "myanimelist.net",
        "thetvdb.com",
    }
)


def _clean_direct_url(raw: Any) -> str:
    """Return a clean HTTP(S) streaming watch URL, skipping catalog/aggregator pages."""
    url = str(raw or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return ""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return ""
    if any(host == agg or host.endswith(f".{agg}") for agg in _AGGREGATOR_HOSTS):
        return ""
    return url


def _title_key(text: str) -> str:
    cleaned = _CLEAN_RE.sub(" ", str(text or "").casefold())
    return _SPACE_RE.sub(" ", cleaned).strip()


def _make_session(proxy: str | None = None) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": jw.USER_AGENT,
            "Accept": "application/json",
        }
    )
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


@dataclass
class UpcomingEpisode:
    season: int | None = None
    episode: int | None = None
    title: str = ""
    release_date: str = ""
    release_at: str = ""

    @property
    def code(self) -> str:
        if self.season is not None and self.episode is not None:
            return f"S{int(self.season):02d}E{int(self.episode):02d}"
        if self.episode is not None:
            return f"E{int(self.episode):02d}"
        return ""


@dataclass
class UpcomingItem:
    id: str
    title: str
    original_title: str = ""
    media_kind: str = "show"           # "movie" | "show"
    year: str = ""
    release_date: str = ""             # YYYY-MM-DD
    release_at: str = ""               # ISO-8601 UTC when exact time is known
    precision: str = "date"            # "exact" | "date"
    platform_name: str = ""
    platform_technical: str = ""
    service_id: str | None = None
    episode_code: str = ""
    episode_title: str = ""
    episodes: list[UpcomingEpisode] = field(default_factory=list)
    poster_url: str = ""
    synopsis: str = ""
    source: str = "justwatch"
    sources: list[str] = field(default_factory=list)
    region: str = "US"
    web_url: str = ""
    direct_url: str = ""
    platform_urls: dict[str, str] = field(default_factory=dict)
    source_ids: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.sources:
            self.sources = [self.source]

    def suggested_run_at(self) -> str:
        """Best ISO-8601 UTC start time for creating a ScheduledJob."""
        if self.release_at:
            return self.release_at
        if self.release_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.release_date):
            day = datetime.strptime(self.release_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            hour = default_release_hour_utc(self.service_id or "")
            return format_iso_utc(day + timedelta(hours=hour))
        return format_iso_utc(utc_now() + timedelta(hours=1))

    @property
    def source_badge(self) -> str:
        return " + ".join(SOURCE_LABELS.get(s, s) for s in self.sources)


@dataclass
class UpcomingReport:
    items: list[UpcomingItem] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    active_sources: list[str] = field(default_factory=list)


# --------------------------------------------------------------- individual APIs


def fetch_justwatch_upcoming(
    *,
    region: str = "US",
    language: str = "en",
    count: int = 40,
    services: Iterable[Any] = (),
    proxy: str | None = None,
    timeout: int = 15,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[UpcomingItem]:
    ref = (now or utc_now()).astimezone(timezone.utc)
    today_str = ref.strftime("%Y-%m-%d")
    svc_index = jw.service_index(services)
    sess = session or _make_session(proxy)
    region_code = (region or "US").upper()

    resp = sess.post(
        jw.GRAPHQL_URL,
        json={
            "query": _JW_UPCOMING_QUERY,
            "variables": {
                "country": region_code,
                "language": language,
                "first": max(10, int(count)),
                "filter": {
                    "releaseYear": {"min": ref.year},
                    "includeTitlesWithoutUrl": True,
                },
            },
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        msg = "; ".join(str(e.get("message") or "") for e in payload["errors"])[:180]
        raise RuntimeError(msg or "JustWatch query failed")

    edges = ((payload.get("data") or {}).get("popularTitles") or {}).get("edges") or []
    items: list[UpcomingItem] = []
    for edge in edges:
        node = edge.get("node") or {}
        content = node.get("content") or {}
        releases = content.get("upcomingReleases") or []
        if not releases:
            continue
        node_id = str(node.get("id") or "").strip()
        if not node_id:
            continue
        title = str(content.get("title") or "").strip()
        if not title:
            continue
        obj_type = str(node.get("objectType") or "").upper()
        media_kind = "show" if obj_type == "SHOW" else "movie"
        year = str(content.get("originalReleaseYear") or "")
        poster_url = jw.resolve_poster_url(content.get("posterUrl"))
        synopsis = _strip_html(content.get("shortDescription"))
        full_path = str(content.get("fullPath") or "")
        web_url = f"https://www.justwatch.com{full_path}" if full_path else ""

        offer_urls_by_tech: dict[str, str] = {}
        offer_urls_by_service: dict[str, str] = {}
        for offer in node.get("offers") or []:
            offer_url = _clean_direct_url(offer.get("standardWebURL"))
            if not offer_url:
                continue
            opkg = offer.get("package") or {}
            o_tech = str(opkg.get("technicalName") or "").strip()
            o_clear = str(opkg.get("clearName") or "").strip()
            if o_tech and o_tech.lower() not in offer_urls_by_tech:
                offer_urls_by_tech[o_tech.lower()] = offer_url
            if svc_index:
                o_sid = jw.resolve_service(o_tech, o_clear, svc_index)
                if o_sid and o_sid not in offer_urls_by_service:
                    offer_urls_by_service[o_sid] = offer_url

        seen_pkg: set[tuple[str, str]] = set()
        for rel in releases:
            release_date = str(rel.get("releaseDate") or "").strip()[:10]
            if not release_date or release_date < today_str:
                continue
            pkg = rel.get("package") or {}
            clear_name = str(pkg.get("clearName") or "").strip()
            tech_name = str(pkg.get("technicalName") or "").strip()
            dedupe_key = (release_date, tech_name or clear_name)
            if dedupe_key in seen_pkg:
                continue
            seen_pkg.add(dedupe_key)
            service_id = jw.resolve_service(tech_name, clear_name, svc_index) if svc_index else None
            direct_url = (
                offer_urls_by_tech.get(tech_name.lower(), "")
                or (offer_urls_by_service.get(service_id, "") if service_id else "")
                or (next(iter(offer_urls_by_tech.values()), "") if not tech_name and len(offer_urls_by_tech) == 1 else "")
            )
            item_platform_urls = dict(offer_urls_by_service)
            if service_id and direct_url and service_id not in item_platform_urls:
                item_platform_urls[service_id] = direct_url
            items.append(
                UpcomingItem(
                    id=f"jw:{node_id}:{tech_name or 'any'}:{release_date}",
                    title=title,
                    original_title=str(content.get("originalTitle") or ""),
                    media_kind=media_kind,
                    year=year,
                    release_date=release_date,
                    release_at="",
                    precision="date",
                    platform_name=clear_name,
                    platform_technical=tech_name,
                    service_id=service_id,
                    poster_url=poster_url,
                    synopsis=synopsis,
                    source="justwatch",
                    sources=["justwatch"],
                    region=region_code,
                    web_url=web_url,
                    direct_url=direct_url,
                    platform_urls=item_platform_urls,
                    source_ids={"justwatch": node_id},
                )
            )
    return items


def fetch_justwatch_show_episodes(
    node_id: str,
    *,
    region: str = "US",
    language: str = "en",
    proxy: str | None = None,
    timeout: int = 12,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[UpcomingEpisode]:
    """Fetch future episode release dates for a JustWatch Show node."""
    if not node_id:
        return []
    today_str = (now or utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%d")
    sess = session or _make_session(proxy)
    resp = sess.post(
        jw.GRAPHQL_URL,
        json={
            "query": _JW_SHOW_EPISODES_QUERY,
            "variables": {
                "nodeId": node_id,
                "country": (region or "US").upper(),
                "language": language,
            },
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    node = (payload.get("data") or {}).get("node") or {}
    episodes: list[UpcomingEpisode] = []
    for season in node.get("seasons") or []:
        for ep in season.get("episodes") or []:
            content = ep.get("content") or {}
            rel_date = str(content.get("originalReleaseDate") or "").strip()[:10]
            if not rel_date or rel_date < today_str:
                continue
            s_num = content.get("seasonNumber")
            e_num = content.get("episodeNumber")
            episodes.append(
                UpcomingEpisode(
                    season=int(s_num) if s_num is not None else None,
                    episode=int(e_num) if e_num is not None else None,
                    title=str(content.get("title") or "").strip(),
                    release_date=rel_date,
                )
            )
    episodes.sort(key=lambda e: (e.release_date, e.season or 0, e.episode or 0))
    return episodes


def fetch_tvmaze_upcoming(
    *,
    days: int = 4,
    region: str = "US",
    services: Iterable[Any] = (),
    proxy: str | None = None,
    timeout: int = 12,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[UpcomingItem]:
    ref = (now or utc_now()).astimezone(timezone.utc)
    svc_index = jw.service_index(services)
    sess = session or _make_session(proxy)
    region_code = (region or "US").upper()
    items: list[UpcomingItem] = []

    for offset in range(max(1, min(7, int(days)))):
        day_str = (ref + timedelta(days=offset)).strftime("%Y-%m-%d")
        resp = sess.get(TVMAZE_WEB_SCHEDULE_URL, params={"date": day_str}, timeout=timeout)
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list):
            continue
        for row in rows:
            show = (row.get("_embedded") or {}).get("show") or row.get("show") or {}
            show_name = str(show.get("name") or "").strip()
            if not show_name:
                continue
            web_ch = show.get("webChannel") or show.get("network") or {}
            platform_name = str(web_ch.get("name") or "").strip()
            ch_country = ((web_ch.get("country") or {}).get("code") or "").upper()
            # Keep global platforms (country None) or region-matching platforms.
            if ch_country and region_code and ch_country != region_code:
                continue
            airstamp = str(row.get("airstamp") or "").strip()
            release_at = ""
            precision = "date"
            if airstamp:
                try:
                    dt = datetime.fromisoformat(airstamp.replace("Z", "+00:00"))
                    release_at = format_iso_utc(dt)
                    precision = "exact"
                except ValueError:
                    release_at = ""
            season_num = row.get("season")
            ep_num = row.get("number")
            ep_obj = UpcomingEpisode(
                season=int(season_num) if isinstance(season_num, int) else None,
                episode=int(ep_num) if isinstance(ep_num, int) else None,
                title=str(row.get("name") or "").strip(),
                release_date=str(row.get("airdate") or day_str)[:10],
                release_at=release_at,
            )
            image = show.get("image") or {}
            poster_url = str(image.get("medium") or image.get("original") or "").strip()
            premiered = str(show.get("premiered") or "")
            year = premiered[:4] if len(premiered) >= 4 else ""
            service_id = jw.resolve_service(platform_name, platform_name, svc_index) if (svc_index and platform_name) else None
            show_id = str(show.get("id") or "")
            ep_id = str(row.get("id") or f"{show_id}:{ep_obj.code}:{day_str}")
            direct_url = _clean_direct_url(show.get("officialSite"))
            platform_urls = {service_id: direct_url} if (service_id and direct_url) else {}
            items.append(
                UpcomingItem(
                    id=f"tvmaze:{ep_id}",
                    title=show_name,
                    media_kind="show",
                    year=year,
                    release_date=ep_obj.release_date,
                    release_at=release_at,
                    precision=precision,
                    platform_name=platform_name,
                    platform_technical=platform_name.lower().replace(" ", ""),
                    service_id=service_id,
                    episode_code=ep_obj.code,
                    episode_title=ep_obj.title,
                    episodes=[ep_obj] if ep_obj.code else [],
                    poster_url=poster_url,
                    synopsis=_strip_html(show.get("summary")),
                    source="tvmaze",
                    sources=["tvmaze"],
                    region=ch_country or region_code,
                    web_url=str(show.get("url") or ""),
                    direct_url=direct_url,
                    platform_urls=platform_urls,
                    source_ids={"tvmaze": show_id} if show_id else {},
                )
            )
    return items


def fetch_anilist_upcoming(
    *,
    days: int = 10,
    per_page: int = 35,
    services: Iterable[Any] = (),
    proxy: str | None = None,
    timeout: int = 12,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[UpcomingItem]:
    ref = (now or utc_now()).astimezone(timezone.utc)
    cutoff = int((ref + timedelta(days=max(1, min(30, int(days))))).timestamp())
    svc_index = jw.service_index(services)
    sess = session or _make_session(proxy)

    resp = sess.post(
        ANILIST_GRAPHQL_URL,
        json={
            "query": _ANILIST_AIRING_QUERY,
            "variables": {"perPage": max(1, min(50, int(per_page))), "until": cutoff},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    schedules = ((payload.get("data") or {}).get("Page") or {}).get("airingSchedules") or []

    items: list[UpcomingItem] = []
    for entry in schedules:
        media = entry.get("media") or {}
        titles = media.get("title") or {}
        title = str(titles.get("english") or titles.get("romaji") or "").strip()
        romaji = str(titles.get("romaji") or "").strip()
        if not title:
            continue
        airing_at = entry.get("airingAt")
        if not isinstance(airing_at, (int, float)):
            continue
        dt = datetime.fromtimestamp(int(airing_at), tz=timezone.utc)
        release_at = format_iso_utc(dt)
        release_date = dt.strftime("%Y-%m-%d")
        ep_num = entry.get("episode")
        ep_obj = UpcomingEpisode(
            season=None,
            episode=int(ep_num) if isinstance(ep_num, int) else None,
            title="",
            release_date=release_date,
            release_at=release_at,
        )

        # Pick first recognized streaming external link (prefer installed services)
        # while collecting all streaming links so switching services can still prefill.
        best_service_id: str | None = None
        best_platform = ""
        best_tech = ""
        best_direct_url = ""
        platform_urls: dict[str, str] = {}
        for link in media.get("externalLinks") or []:
            site = str(link.get("site") or "").strip()
            mapped = _ANILIST_STREAMING_SITES.get(site.lower())
            if mapped is None:
                continue
            tech, label = mapped
            link_url = _clean_direct_url(link.get("url"))
            sid = jw.resolve_service(tech, label, svc_index) if svc_index else None
            if link_url and sid and sid not in platform_urls:
                platform_urls[sid] = link_url
            if sid and best_service_id is None:
                best_service_id = sid
                best_platform = label
                best_tech = tech
                best_direct_url = link_url
            elif not best_platform:
                best_platform = label
                best_tech = tech
                best_direct_url = link_url
            elif not best_direct_url and link_url and best_service_id is None:
                best_direct_url = link_url

        cover = media.get("coverImage") or {}
        poster_url = str(cover.get("large") or cover.get("medium") or "").strip()
        media_id = str(media.get("id") or "")
        year = str(media.get("seasonYear") or "")
        items.append(
            UpcomingItem(
                id=f"anilist:{media_id}:{ep_obj.code or airing_at}",
                title=title,
                original_title=romaji if romaji != title else "",
                media_kind="show",
                year=year,
                release_date=release_date,
                release_at=release_at,
                precision="exact",
                platform_name=best_platform or "Anime",
                platform_technical=best_tech,
                service_id=best_service_id,
                episode_code=ep_obj.code,
                episodes=[ep_obj] if ep_obj.code else [],
                poster_url=poster_url,
                synopsis=_strip_html(media.get("description")),
                source="anilist",
                sources=["anilist"],
                region="JP",
                web_url=str(media.get("siteUrl") or ""),
                direct_url=best_direct_url,
                platform_urls=platform_urls,
                source_ids={"anilist": media_id} if media_id else {},
            )
        )
    return items


def _tmdb_request(
    session: requests.Session,
    path: str,
    api_key: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = 12,
) -> dict[str, Any]:
    token = str(api_key or "").strip()
    if not token:
        raise ValueError("TMDB API key is not configured.")
    req_params = dict(params or {})
    headers: dict[str, str] = {}
    # TMDB v4 Read Access Tokens are long JWTs; v3 keys are short 32-char hex strings.
    if len(token) > 50 or token.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {token}"
    else:
        req_params["api_key"] = token
    try:
        resp = session.get(
            f"{TMDB_API_BASE}{path}",
            params=req_params,
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code == 401:
            raise RuntimeError("TMDB rejected the API key (401 Unauthorized).")
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        # Always scrub query-string api_key from requests error text.
        raise RuntimeError(redact(f"TMDB request failed: {exc}")) from None


def fetch_tmdb_upcoming(
    *,
    api_key: str,
    region: str = "US",
    language: str = "en-US",
    services: Iterable[Any] = (),
    proxy: str | None = None,
    timeout: int = 12,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[UpcomingItem]:
    token = str(api_key or "").strip()
    if not token:
        raise ValueError("TMDB API key is not configured.")
    ref = (now or utc_now()).astimezone(timezone.utc)
    today_str = ref.strftime("%Y-%m-%d")
    region_code = (region or "US").upper()
    sess = session or _make_session(proxy)
    items: list[UpcomingItem] = []

    movies_data = _tmdb_request(
        sess,
        "/movie/upcoming",
        token,
        params={"region": region_code, "language": language, "page": 1},
        timeout=timeout,
    )
    for row in (movies_data.get("results") or [])[:20]:
        title = str(row.get("title") or "").strip()
        rel_date = str(row.get("release_date") or "").strip()[:10]
        if not title or not rel_date or rel_date < today_str:
            continue
        poster_path = str(row.get("poster_path") or "").strip()
        poster_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else ""
        tmdb_id = str(row.get("id") or "")
        items.append(
            UpcomingItem(
                id=f"tmdb:movie:{tmdb_id}",
                title=title,
                original_title=str(row.get("original_title") or ""),
                media_kind="movie",
                year=rel_date[:4],
                release_date=rel_date,
                release_at="",
                precision="date",
                platform_name="TMDB",
                platform_technical="",
                service_id=None,
                poster_url=poster_url,
                synopsis=_strip_html(row.get("overview")),
                source="tmdb",
                sources=["tmdb"],
                region=region_code,
                web_url=f"https://www.themoviedb.org/movie/{tmdb_id}" if tmdb_id else "",
                source_ids={"tmdb": f"movie:{tmdb_id}"} if tmdb_id else {},
            )
        )

    tv_data = _tmdb_request(
        sess,
        "/tv/on_the_air",
        token,
        params={"language": language, "page": 1},
        timeout=timeout,
    )
    for row in (tv_data.get("results") or [])[:20]:
        title = str(row.get("name") or "").strip()
        first_air = str(row.get("first_air_date") or "").strip()[:10]
        if not title:
            continue
        rel_date = first_air if first_air and first_air >= today_str else today_str
        poster_path = str(row.get("poster_path") or "").strip()
        poster_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else ""
        tmdb_id = str(row.get("id") or "")
        items.append(
            UpcomingItem(
                id=f"tmdb:tv:{tmdb_id}",
                title=title,
                original_title=str(row.get("original_name") or ""),
                media_kind="show",
                year=first_air[:4] if len(first_air) >= 4 else str(ref.year),
                release_date=rel_date,
                release_at="",
                precision="date",
                platform_name="TMDB",
                platform_technical="",
                service_id=None,
                poster_url=poster_url,
                synopsis=_strip_html(row.get("overview")),
                source="tmdb",
                sources=["tmdb"],
                region=region_code,
                web_url=f"https://www.themoviedb.org/tv/{tmdb_id}" if tmdb_id else "",
                source_ids={"tmdb": f"tv:{tmdb_id}"} if tmdb_id else {},
            )
        )
    return items


# --------------------------------------------------------- merge & orchestration


def _dates_close(date_a: str, date_b: str, *, max_days: int = 2) -> bool:
    if not date_a or not date_b:
        return False
    try:
        da = datetime.strptime(date_a[:10], "%Y-%m-%d")
        db = datetime.strptime(date_b[:10], "%Y-%m-%d")
    except ValueError:
        return date_a == date_b
    return abs((da - db).days) <= max_days


def merge_upcoming_items(groups: Sequence[list[UpcomingItem]]) -> list[UpcomingItem]:
    """Merge multi-source upcoming items, enriching date-only entries with exact timestamps."""
    merged: list[UpcomingItem] = []
    by_title: dict[tuple[str, str], list[UpcomingItem]] = {}

    for batch in groups:
        for item in batch:
            key = (_title_key(item.title), item.media_kind)
            matched: UpcomingItem | None = None
            for existing in by_title.get(key, ()):
                same_ep = (
                    not item.episode_code
                    or not existing.episode_code
                    or item.episode_code == existing.episode_code
                )
                same_platform = (
                    not item.service_id
                    or not existing.service_id
                    or item.service_id == existing.service_id
                )
                item_year = str(item.year or "").strip()
                existing_year = str(existing.year or "").strip()
                same_year = (
                    not item_year
                    or not existing_year
                    or item_year == existing_year
                )
                if same_ep and same_platform and same_year and _dates_close(existing.release_date, item.release_date):
                    matched = existing
                    break

            if matched is None:
                merged.append(item)
                by_title.setdefault(key, []).append(item)
                continue

            # Enrich existing entry with higher-precision timestamp, platform, poster, synopsis.
            for src in item.sources:
                if src not in matched.sources:
                    matched.sources.append(src)
            matched.source_ids.update(item.source_ids)
            if item.year and not matched.year:
                matched.year = item.year
            if item.precision == "exact" and matched.precision != "exact":
                matched.release_at = item.release_at
                matched.release_date = item.release_date
                matched.precision = "exact"
            if item.episode_code and not matched.episode_code:
                matched.episode_code = item.episode_code
            if item.episode_title and not matched.episode_title:
                matched.episode_title = item.episode_title
            if item.episodes:
                existing_codes = {ep.code for ep in matched.episodes if ep.code}
                for ep in item.episodes:
                    if not ep.code or ep.code not in existing_codes:
                        matched.episodes.append(ep)
            if item.service_id and not matched.service_id:
                matched.service_id = item.service_id
                matched.platform_name = item.platform_name or matched.platform_name
                matched.platform_technical = item.platform_technical or matched.platform_technical
            if item.poster_url and not matched.poster_url:
                matched.poster_url = item.poster_url
            if item.synopsis and not matched.synopsis:
                matched.synopsis = item.synopsis
            for key_url, url_val in item.platform_urls.items():
                if url_val and key_url not in matched.platform_urls:
                    matched.platform_urls[key_url] = url_val
            if item.direct_url and not matched.direct_url:
                matched.direct_url = item.direct_url
            elif not matched.direct_url and matched.service_id and matched.service_id in matched.platform_urls:
                matched.direct_url = matched.platform_urls[matched.service_id]

    merged.sort(
        key=lambda x: (
            x.release_date or "9999-12-31",
            x.release_at or "9999-12-31T23:59:59Z",
            0 if x.service_id else 1,
            x.title.casefold(),
        )
    )
    return merged


def fetch_upcoming(
    *,
    sources: Sequence[str] = DEFAULT_UPCOMING_SOURCES,
    region: str = "US",
    language: str = "en",
    tmdb_api_key: str = "",
    services: Iterable[Any] = (),
    proxy: str | None = None,
    timeout: int = 12,
    now: datetime | None = None,
) -> UpcomingReport:
    """Query all enabled sources concurrently and merge into one unified feed."""
    active = list(normalize_upcoming_sources(sources))
    if not active:
        return UpcomingReport(items=[], errors={}, active_sources=[])
    service_list = list(services)

    def _run_source(source_name: str) -> list[UpcomingItem]:
        if source_name == "justwatch":
            return fetch_justwatch_upcoming(
                region=region,
                language=language,
                services=service_list,
                proxy=proxy,
                timeout=timeout,
                now=now,
            )
        if source_name == "tvmaze":
            return fetch_tvmaze_upcoming(
                region=region,
                services=service_list,
                proxy=proxy,
                timeout=timeout,
                now=now,
            )
        if source_name == "anilist":
            return fetch_anilist_upcoming(
                services=service_list,
                proxy=proxy,
                timeout=timeout,
                now=now,
            )
        if source_name == "tmdb":
            return fetch_tmdb_upcoming(
                api_key=tmdb_api_key,
                region=region,
                language=f"{language}-{region.upper()}" if "-" not in language else language,
                services=service_list,
                proxy=proxy,
                timeout=timeout,
                now=now,
            )
        return []

    results_by_source: dict[str, list[UpcomingItem]] = {}
    errors: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(active)) as pool:
        future_map = {pool.submit(_run_source, src): src for src in active}
        for future in concurrent.futures.as_completed(future_map):
            src = future_map[future]
            try:
                results_by_source[src] = future.result()
            except Exception as exc:  # noqa: BLE001
                errors[src] = redact(str(exc)) or type(exc).__name__

    # Merge in canonical priority order so JustWatch leads platform mapping,
    # TVMaze/AniList enrich exact release timestamps, and TMDB supplements metadata.
    ordered_groups = [results_by_source[s] for s in UPCOMING_SOURCES if s in results_by_source]
    merged = merge_upcoming_items(ordered_groups)
    return UpcomingReport(items=merged, errors=errors, active_sources=active)


__all__ = [
    "DEFAULT_UPCOMING_SOURCES",
    "SOURCE_LABELS",
    "UPCOMING_SOURCES",
    "UpcomingEpisode",
    "UpcomingItem",
    "UpcomingReport",
    "fetch_anilist_upcoming",
    "fetch_justwatch_show_episodes",
    "fetch_justwatch_upcoming",
    "fetch_tmdb_upcoming",
    "fetch_tvmaze_upcoming",
    "fetch_upcoming",
    "merge_upcoming_items",
    "normalize_upcoming_sources",
]
