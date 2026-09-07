"""JustWatch: who has this title, where, and from which season on.

Not a service. Nothing here downloads anything, and it is deliberately not in
``services/`` - it would otherwise show up in the platform grid and in "search
in", offering to open a session it cannot run.

What it is for is the question that comes *before* picking a platform. "Which of
them has this" is a real question, and the answer changes by region. JustWatch
already knows; the only part worth adding is the
last step - taking a provider it names and opening *our* service for it, which
is why the provider mapping lives here rather than in a browser tab.

Three queries, all public and unauthenticated:

* ``search``   - titles matching a term, in one region
* ``offers``   - who carries one title, in one region
* ``seasons``  - for a show, which seasons each provider actually has

The GraphQL documents are the ones the site itself sends. They are typed
strictly at the other end: an unused variable is a 422, not a warning, so the
declarations here are exactly what each document uses.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import requests

GRAPHQL_URL = "https://apis.justwatch.com/graphql"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

#: How each way of paying is described. Words, not emoji: this is rendered next
#: to our own colour-coded rows, and a coloured word survives a theme change and
#: a terminal without emoji support.
MONETIZATION = {
    "FLATRATE": "Subscription",
    "FLATRATE_AND_BUY": "Subscription",
    "FREE": "Free",
    "ADS": "Free with ads",
    "RENT": "Rent",
    "BUY": "Buy",
    "CINEMA": "In cinemas",
}

#: Which palette role each one is drawn in.
MONETIZATION_ROLE = {
    "FLATRATE": "ok",
    "FLATRATE_AND_BUY": "ok",
    "FREE": "ok",
    "ADS": "warn",
    "RENT": "warn",
    "BUY": "muted",
    "CINEMA": "muted",
}

#: The order they are worth reading in: what you can watch on a subscription
#: first, what you have to pay for last.
MONETIZATION_ORDER = ["FLATRATE", "FLATRATE_AND_BUY", "FREE", "ADS", "RENT", "BUY", "CINEMA"]

#: How a presentation type is written. JustWatch prefixes 4K with an underscore
#: because a GraphQL enum cannot start with a digit.
PRESENTATION = {
    "_4K": "4K",
    "HD": "HD",
    "SD": "SD",
    "DVD": "DVD",
    "BLURAY": "Blu-ray",
    "BLURAY_4K": "4K Blu-ray",
    "CANVAS": "Canvas",
}

PRESENTATION_RANK = ["_4K", "BLURAY_4K", "HD", "BLURAY", "SD", "DVD", "CANVAS"]

#: Built-in regions used when no browsing preference has been saved. They are a
#: reasonable spread of catalogues and remain stable for existing users.
DEFAULT_REGIONS = [
    "US", "GB", "CA", "AU", "JP", "DE", "FR", "KR", "TW", "HK",
    "ZA", "EG", "ES", "PT", "NL", "IT", "GE",
]

#: Region codes offered in the picker, with names to search by. ISO 3166-1
#: alpha-2, which is what the ``Country`` scalar wants.
REGIONS: dict[str, str] = {
    "AE": "United Arab Emirates", "AR": "Argentina", "AT": "Austria",
    "AU": "Australia", "BE": "Belgium", "BG": "Bulgaria", "BR": "Brazil",
    "CA": "Canada", "CH": "Switzerland", "CL": "Chile", "CO": "Colombia",
    "CZ": "Czechia", "DE": "Germany", "DK": "Denmark", "EE": "Estonia",
    "EG": "Egypt", "ES": "Spain", "FI": "Finland", "FR": "France",
    "GB": "United Kingdom", "GE": "Georgia", "GR": "Greece", "HK": "Hong Kong",
    "HR": "Croatia", "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland",
    "IL": "Israel", "IN": "India", "IS": "Iceland", "IT": "Italy",
    "JP": "Japan", "KR": "South Korea", "LT": "Lithuania", "LV": "Latvia",
    "MA": "Morocco", "MX": "Mexico", "MY": "Malaysia", "NL": "Netherlands",
    "NO": "Norway", "NZ": "New Zealand", "PE": "Peru", "PH": "Philippines",
    "PL": "Poland", "PT": "Portugal", "RO": "Romania", "RS": "Serbia",
    "SA": "Saudi Arabia", "SE": "Sweden", "SG": "Singapore", "SI": "Slovenia",
    "SK": "Slovakia", "TH": "Thailand", "TR": "Turkey", "TW": "Taiwan",
    "UA": "Ukraine", "US": "United States", "VN": "Vietnam",
    "ZA": "South Africa",
}


def region_label(code: str) -> str:
    """``US`` -> ``US · United States``, falling back to the bare code."""
    code = (code or "").upper()
    name = REGIONS.get(code)
    return f"{code} · {name}" if name else code


def parse_regions(text: str) -> list[str]:
    """Read a comma separated list of region codes, keeping the order given."""
    seen: list[str] = []
    for part in re.split(r"[,\s]+", str(text or "")):
        code = part.strip().upper()
        if code and code not in seen:
            seen.append(code)
    return seen


# --------------------------------------------------------------------- models


@dataclass
class Offer:
    """One way to watch one title in one region."""

    provider: str  # "HBO Max"
    technical_name: str  # "max"
    package_id: int | None
    monetization: str  # "FLATRATE"
    presentation: str  # "_4K"
    price: str = ""
    url: str = ""
    #: seasons this provider actually carries, for a show
    seasons: list[int] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return MONETIZATION.get(self.monetization, self.monetization.title())

    @property
    def role(self) -> str:
        return MONETIZATION_ROLE.get(self.monetization, "muted")

    @property
    def quality(self) -> str:
        return PRESENTATION.get(self.presentation, self.presentation)

    @property
    def physical(self) -> bool:
        """A disc, not a stream.

        A provider may sell both physical and streaming offers under one display
        name, so without this a DVD listing could be offered as a media stream.
        """
        return self.presentation in {"DVD", "BLURAY", "BLURAY_4K"}

    def season_summary(self) -> str:
        """``S01-S03`` rather than ``S01, S02, S03``: it is a row, not a list."""
        if not self.seasons:
            return ""
        numbers = sorted(set(self.seasons))
        runs: list[str] = []
        start = previous = numbers[0]
        for number in numbers[1:]:
            if number == previous + 1:
                previous = number
                continue
            runs.append(f"S{start:02d}" if start == previous else f"S{start:02d}-S{previous:02d}")
            start = previous = number
        runs.append(f"S{start:02d}" if start == previous else f"S{start:02d}-S{previous:02d}")
        return ", ".join(runs)


@dataclass
class Title:
    """A JustWatch search result."""

    node_id: str
    object_type: str  # MOVIE | SHOW
    name: str
    original_name: str = ""
    year: str = ""
    genres: list[str] = field(default_factory=list)
    synopsis: str = ""
    imdb_id: str = ""
    path: str = ""

    @property
    def is_show(self) -> bool:
        return self.object_type.upper() == "SHOW"

    @property
    def kind_label(self) -> str:
        return "series" if self.is_show else "film"

    def label(self) -> str:
        line = self.name
        if self.original_name and self.original_name != self.name:
            line += f" ({self.original_name})"
        if self.year:
            line += f" [{self.year}]"
        return line

    @property
    def web_url(self) -> str:
        return f"https://www.justwatch.com{self.path}" if self.path else ""


@dataclass
class Availability:
    """Every offer for one title in one region."""

    region: str
    offers: list[Offer] = field(default_factory=list)
    error: str = ""

    @property
    def label(self) -> str:
        return region_label(self.region)

    def by_kind(self) -> list[tuple[str, list[Offer]]]:
        """Grouped and ordered the way they are worth reading."""
        groups: dict[str, list[Offer]] = {}
        for offer in self.offers:
            groups.setdefault(offer.monetization, []).append(offer)
        ordered = [m for m in MONETIZATION_ORDER if m in groups]
        ordered += [m for m in groups if m not in MONETIZATION_ORDER]
        return [(monetization, groups[monetization]) for monetization in ordered]

    def summary(self) -> str:
        if self.error:
            return self.error
        if not self.offers:
            return "nothing here"
        kinds = {offer.kind for offer in self.offers}
        names = {offer.provider for offer in self.offers}
        return f"{len(names)} provider(s) · {', '.join(sorted(kinds)).lower()}"


# ------------------------------------------------------- provider -> service

#: Prefixes that mean "resold by": the content is one provider's, but the player
#: and the DRM you would go through are the reseller's, so that is the service
#: worth opening.
RESELLERS = {
    "amazon": "amazon",
    "appletv": "appletv",
    "rokuchannel": "roku",
    "unext": "unext",
}

#: Tier and locale noise on the end of a package name. Stripped one at a time,
#: longest first, so compound package names reach their base provider.
_SUFFIXES = [
    "basicwithads", "premiumplus", "essential", "withads", "premium",
    "standard", "channel", "online", "korea", "store",
    "free", "plus", "vod", "de", "es", "uk", "us", "fr", "it", "br",
]

#: Where neither the technical name nor the display name gets there on its own.
PROVIDER_MAP = {
    "wuaki": "rakuten",
    "vudu": "vudu",
    "vudufree": "vudu",
    "itunes": "appletv",
    "apple": "appletv",
    "appletvplus": "appletv",
    "disneyplus": "disney",
    "max": "max",
    "hbomax": "max",
    "peacocktv": "peacock",
    "tubitv": "tubi",
    "fubotv": "fubo",
    "plutotvfast": "plutotv",
    "molotovtv": "molotov",
    "tvnowde": "rtlplus",
    "joynde": "joyn",
    "movistarott": "movistar",
    "nowonline": "clarotv",
    "clarovideo": "clarovideo",
    "tvingkorea": "tvingw",
    "entertaintv": "magentatv",
    "magentatvplus": "magentatv",
    "animedigitalnetwork": "adn",
    "youtubefree": "youtube",
    "youtubetv": "ytv",
    # Some catalogues list partner brands under a host service. Keep those
    # aliases explicit so their names cannot fall through to an unrelated
    # provider with a similar display name.
    "ctv": "crave",
    "noovo": "crave",
    # An empty value means "we have nothing for this", stated rather than left
    # to a name comparison that would otherwise find something wrong.
    "justwatchtv": "",  # JustWatch's own FAST channels
    "maxdomestore": "",  # a German transactional store, unrelated to HBO Max
    "skystore": "",  # transactional; our Sky services are the subscription apps
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    return _NON_ALNUM.sub("", str(text or "").lower())


def service_index(services: Iterable[Any]) -> dict[str, str]:
    """Every name a service could be recognised by -> its id.

    Built from the registry rather than written out, so a service added later is
    matchable without touching this file. A service's id, its aliases and its
    display name all point at it, because an availability answer names a provider
    the way a person would and not the way this codebase spells it.
    """
    index: dict[str, str] = {}
    for service in services:
        service_id = getattr(service, "ID", "")
        if not service_id:
            continue
        keys = {service_id, *getattr(service, "ALIASES", ())}
        keys.add(getattr(service, "NAME", ""))
        for key in keys:
            normalized = _normalize(key)
            if normalized:
                index.setdefault(normalized, service_id)
    return index


def resolve_service(
    technical_name: str, clear_name: str, index: dict[str, str]
) -> str | None:
    """Which of our services corresponds to a JustWatch provider, if any.

    Tried in order of how much is known: an explicit mapping, a recognised
    reseller, then the package/display names with tier and locale noise removed.
    Resellers must come before the content brand: a channel add-on plays through
    its host platform, not the content owner's native app. Returns ``None``
    rather than guessing when nothing fits - "we cannot open this one" is a
    useful answer and a wrong service is not.
    """
    mapped = PROVIDER_MAP.get(_normalize(technical_name))
    if mapped is not None:
        if not mapped:
            return None
        found = index.get(_normalize(mapped))
        if found:
            return found

    # Channel add-ons use the content brand in their package name, but login,
    # playback and DRM belong to the prefix platform. Do not fall through to the
    # inner brand when that platform is absent: "no service" is safer than
    # opening a native service that cannot play this particular offer.
    normalized = _normalize(technical_name)
    for prefix, reseller in RESELLERS.items():
        if normalized.startswith(prefix) and normalized != prefix:
            return index.get(_normalize(reseller))

    for candidate in (technical_name, clear_name):
        normalized = _normalize(candidate)
        if not normalized:
            continue
        if normalized in index:
            return index[normalized]
        trimmed = _strip_suffixes(normalized)
        if trimmed and trimmed in index:
            return index[trimmed]

    return None


def _strip_suffixes(normalized: str) -> str:
    """Peel tier and locale noise off the end, longest first."""
    changed = True
    while changed and normalized:
        changed = False
        for suffix in _SUFFIXES:
            if len(normalized) > len(suffix) + 2 and normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                changed = True
                break
    return normalized


# ---------------------------------------------------------------- the client


class JustWatchError(RuntimeError):
    pass


_SEARCH = """
query SearchTitles($country: Country!, $language: Language!, $first: Int!, $filter: TitleFilter) {
  popularTitles(country: $country, first: $first, filter: $filter) {
    edges {
      node {
        id
        objectType
        content(country: $country, language: $language) {
          title
          originalTitle
          originalReleaseYear
          fullPath
          shortDescription
          genres { shortName }
          externalIds { imdbId }
        }
      }
    }
  }
}
"""

_OFFERS = """
query TitleOffers(
  $nodeId: ID!, $country: Country!, $language: Language!,
  $filterFlatrate: OfferFilter!, $filterBuy: OfferFilter!,
  $filterRent: OfferFilter!, $filterFree: OfferFilter!,
  $platform: Platform! = WEB
) {
  node(id: $nodeId) {
    id
    __typename
    ... on MovieOrShowOrSeasonOrEpisode {
      offerCount(country: $country, platform: $platform)
      flatrate: offers(country: $country, platform: $platform, filter: $filterFlatrate) { ...Row }
      buy: offers(country: $country, platform: $platform, filter: $filterBuy) { ...Row }
      rent: offers(country: $country, platform: $platform, filter: $filterRent) { ...Row }
      free: offers(country: $country, platform: $platform, filter: $filterFree) { ...Row }
    }
  }
}

fragment Row on Offer {
  id
  monetizationType
  presentationType
  retailPrice(language: $language)
  standardWebURL
  package { clearName technicalName packageId }
}
"""

_SEASONS = """
query ShowSeasons($nodeId: ID!, $country: Country!, $language: Language!, $platform: Platform! = WEB) {
  node(id: $nodeId) {
    id
    __typename
    ... on Show {
      totalSeasonCount
      seasons(limit: 50) {
        id
        totalEpisodeCount
        content(country: $country, language: $language) { seasonNumber title }
        offers(country: $country, platform: $platform) {
          monetizationType
          package { clearName technicalName packageId }
        }
      }
    }
  }
}
"""

#: One offer per provider per way of paying. Without ``bestOnly`` the same
#: provider comes back once per resolution, which is 35 rows for one film.
_FILTERS = {
    "filterFlatrate": {"monetizationTypes": ["FLATRATE", "FLATRATE_AND_BUY"], "bestOnly": True},
    "filterBuy": {"monetizationTypes": ["BUY"], "bestOnly": True},
    "filterRent": {"monetizationTypes": ["RENT"], "bestOnly": True},
    "filterFree": {"monetizationTypes": ["FREE", "ADS"], "bestOnly": True},
}


class JustWatch:
    """Thin client. One session, three queries, no state worth keeping."""

    def __init__(self, *, proxy: str | None = None, timeout: int = 20):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/json",
            }
        )
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    # ------------------------------------------------------------------ plumbing
    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.session.post(
                GRAPHQL_URL, json={"query": query, "variables": variables}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise JustWatchError(f"could not reach JustWatch: {exc}") from exc
        if response.status_code >= 400:
            # 422 is what a rejected document looks like here, and the body says
            # which variable or field it objected to. Worth keeping.
            detail = ""
            try:
                errors = response.json().get("errors") or []
                detail = "; ".join(str(e.get("message", "")) for e in errors)[:200]
            except ValueError:
                detail = (response.text or "")[:200]
            raise JustWatchError(f"JustWatch returned {response.status_code}: {detail}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise JustWatchError("JustWatch returned something that is not JSON") from exc
        if payload.get("errors"):
            messages = "; ".join(str(e.get("message", "")) for e in payload["errors"])[:200]
            raise JustWatchError(messages or "JustWatch rejected the query")
        return payload.get("data") or {}

    # -------------------------------------------------------------------- search
    def search(
        self, query: str, region: str = "US", language: str = "en", count: int = 12
    ) -> list[Title]:
        if not (query or "").strip():
            return []
        data = self._post(
            _SEARCH,
            {
                "country": (region or "US").upper(),
                "language": language,
                "first": max(1, int(count)),
                "filter": {"searchQuery": query.strip()},
            },
        )
        edges = ((data.get("popularTitles") or {}).get("edges")) or []
        titles: list[Title] = []
        for edge in edges:
            node = edge.get("node") or {}
            content = node.get("content") or {}
            year = content.get("originalReleaseYear")
            titles.append(
                Title(
                    node_id=str(node.get("id") or ""),
                    object_type=str(node.get("objectType") or ""),
                    name=str(content.get("title") or "unknown"),
                    original_name=str(content.get("originalTitle") or ""),
                    year=str(year) if year else "",
                    genres=[
                        str(g.get("shortName") or "")
                        for g in (content.get("genres") or [])
                        if g.get("shortName")
                    ],
                    synopsis=str(content.get("shortDescription") or ""),
                    imdb_id=str((content.get("externalIds") or {}).get("imdbId") or ""),
                    path=str(content.get("fullPath") or ""),
                )
            )
        return [title for title in titles if title.node_id]

    # -------------------------------------------------------------------- offers
    def offers(self, node_id: str, region: str = "US", language: str = "en") -> Availability:
        """Who carries this title in this region.

        A region with nothing in it is a normal answer, not a failure, and a
        region that errors is reported per region: one bad country must not lose
        the sixteen good ones alongside it.
        """
        region = (region or "US").upper()
        try:
            data = self._post(
                _OFFERS,
                {"nodeId": node_id, "country": region, "language": language, **_FILTERS},
            )
        except JustWatchError as exc:
            return Availability(region=region, error=str(exc))
        node = data.get("node") or {}
        offers: list[Offer] = []
        for bucket in ("flatrate", "free", "rent", "buy"):
            for raw in node.get(bucket) or []:
                offer = _offer_from(raw)
                if offer is not None:
                    offers.append(offer)
        offers.sort(key=_offer_sort_key)
        return Availability(region=region, offers=offers)

    # ------------------------------------------------------------------- seasons
    def seasons(self, node_id: str, region: str = "US", language: str = "en") -> dict[int, list[int]]:
        """``package_id -> [season numbers]`` for a show in one region.

        Which seasons a provider has is the difference between "it is on there"
        and "the season you want is on there", and for a show that is usually the
        actual question.
        """
        region = (region or "US").upper()
        try:
            data = self._post(
                _SEASONS, {"nodeId": node_id, "country": region, "language": language}
            )
        except JustWatchError:
            return {}
        node = data.get("node") or {}
        spread: dict[int, list[int]] = {}
        for season in node.get("seasons") or []:
            number = (season.get("content") or {}).get("seasonNumber")
            if number is None:
                continue
            for raw in season.get("offers") or []:
                package_id = (raw.get("package") or {}).get("packageId")
                if package_id is None:
                    continue
                bucket = spread.setdefault(int(package_id), [])
                if int(number) not in bucket:
                    bucket.append(int(number))
        return spread

    # ----------------------------------------------------------------- combined
    def availability(
        self,
        title: Title,
        regions: Iterable[str],
        language: str = "en",
        *,
        on_region: Any = None,
    ) -> list[Availability]:
        """Offers for every region, with season spreads folded in for a show.

        ``on_region`` is called with each code before it is fetched, because this
        is one request per region and the caller is a screen that should say so.
        """
        results: list[Availability] = []
        for region in regions:
            if callable(on_region):
                on_region(region)
            found = self.offers(title.node_id, region, language)
            if title.is_show and found.offers:
                spread = self.seasons(title.node_id, region, language)
                for offer in found.offers:
                    if offer.package_id is not None:
                        offer.seasons = sorted(spread.get(offer.package_id, []))
            results.append(found)
        return results


def _offer_from(raw: dict[str, Any]) -> Offer | None:
    package = raw.get("package") or {}
    name = str(package.get("clearName") or "").strip()
    if not name:
        return None
    package_id = package.get("packageId")
    price = raw.get("retailPrice")
    return Offer(
        provider=name,
        technical_name=str(package.get("technicalName") or ""),
        package_id=int(package_id) if package_id is not None else None,
        monetization=str(raw.get("monetizationType") or ""),
        presentation=str(raw.get("presentationType") or ""),
        price=str(price) if price else "",
        url=str(raw.get("standardWebURL") or ""),
    )


def _offer_sort_key(offer: Offer) -> tuple[int, int, str]:
    kind = (
        MONETIZATION_ORDER.index(offer.monetization)
        if offer.monetization in MONETIZATION_ORDER
        else len(MONETIZATION_ORDER)
    )
    quality = (
        PRESENTATION_RANK.index(offer.presentation)
        if offer.presentation in PRESENTATION_RANK
        else len(PRESENTATION_RANK)
    )
    return (kind, quality, offer.provider.lower())


__all__ = [
    "DEFAULT_REGIONS",
    "MONETIZATION",
    "MONETIZATION_ORDER",
    "MONETIZATION_ROLE",
    "REGIONS",
    "Availability",
    "JustWatch",
    "JustWatchError",
    "Offer",
    "Title",
    "parse_regions",
    "region_label",
    "resolve_service",
    "service_index",
]
