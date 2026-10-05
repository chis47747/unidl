"""JustWatch TV API client aligned with the Android TV app.

Reference APK: com.justwatch.justwatch v26.38.5 (code 2638005)
GraphQL Backend: https://apis.justwatch.com/graphql
Firebase Auth: Project justwatch-b2c-apps (AIzaSyAu34MXe6E6WCFfnJueV2yGV536m7nBXxc)
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

from ...core.attachments import Attachment
from ...core.chapters import Chapter

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://apis.justwatch.com/graphql"
DRM_LICENSE_URL = "https://apis.justwatch.com/drm/license?drmType=widevine"
IMAGE_BASE_URL = "https://images.justwatch.com"
TV_LOGIN_URL = "https://www.justwatch.com/tv/"

# Aligned with Android TV APK decompilation (prod: androidtv / firetv)
FIREBASE_API_KEY = "AIzaSyAu34MXe6E6WCFfnJueV2yGV536m7nBXxc"
FIREBASE_FALLBACK_API_KEYS = [
    "AIzaSyAu34MXe6E6WCFfnJueV2yGV536m7nBXxc",  # Android TV / FireTV prod
    "AIzaSyDv6JIzdDvbTBS-JWdR4Kl22UvgWGAyuo8",  # Web / Tizen / WebOS prod
    "AIzaSyBlymyRyt27TKgIoZnN5sb9fAU-Kq6BmPI",  # tvOS prod
    "AIzaSyABuan0Se0gTwaQgBS7bM-h9LbXChpCFqg",  # Android TV stage
]
FIREBASE_SIGNIN_URL = (
    f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={FIREBASE_API_KEY}"
)
FIREBASE_REFRESH_URL = (
    f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"
)

TOKEN_FILE = "session.json"
USER_AGENT = "JustWatch/26.38.5 (Android TV)"
APP_VERSION = "26.38.5"
DEVICE_USER_AGENT = "okhttp/4.9.2"
JWT_PACKAGE_SHORT = "jwt"
JWT_PACKAGE_TECHNICAL = "justwatchtv"


class JustWatchTvError(Exception):
    """Base exception for JustWatch TV API operations."""


class JustWatchTvAuthError(JustWatchTvError):
    """Authentication or token error."""


class JustWatchTvNotFoundError(JustWatchTvError):
    """Title or offer not found."""


class JustWatchTvPlayError(JustWatchTvError):
    """Playback authorization error."""


# ---------------------------------------------------------------- models


@dataclass
class SessionState:
    id_token: str = ""
    refresh_token: str = ""
    device_id: str = ""
    user_id: str = ""
    email: str = ""
    expires_at: float = 0.0

    @property
    def valid(self) -> bool:
        return bool(self.id_token and time.time() < self.expires_at - 60)

    @property
    def refreshable(self) -> bool:
        return bool(self.refresh_token)

    @property
    def hours_left(self) -> float:
        return max(0.0, (self.expires_at - time.time()) / 3600)

    @property
    def signed_in(self) -> bool:
        return bool(self.id_token or self.refresh_token)

    def to_cache(self) -> dict[str, Any]:
        return {
            "id_token": self.id_token,
            "refresh_token": self.refresh_token,
            "device_id": self.device_id,
            "user_id": self.user_id,
            "email": self.email,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_cache(cls, data: dict[str, Any] | None) -> SessionState:
        if not isinstance(data, dict):
            return cls()
        return cls(
            id_token=str(data.get("id_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            device_id=str(data.get("device_id") or ""),
            user_id=str(data.get("user_id") or ""),
            email=str(data.get("email") or ""),
            expires_at=float(data.get("expires_at") or 0.0),
        )


@dataclass(frozen=True)
class TvCodeChallenge:
    code: str
    uuid: str
    verification_url: str = TV_LOGIN_URL


@dataclass
class TitleOffer:
    id: str
    package_id: int | None = None
    short_name: str = ""
    technical_name: str = ""
    clear_name: str = ""
    monetization_type: str = ""
    presentation_type: str = ""
    stream_url_external: str = ""

    @property
    def is_justwatch_tv(self) -> bool:
        return (
            self.short_name.lower() == JWT_PACKAGE_SHORT
            or self.technical_name.lower() == JWT_PACKAGE_TECHNICAL
        )


@dataclass
class EpisodeItem:
    id: str
    object_id: int
    season_number: int
    episode_number: int
    title: str
    description: str = ""
    thumbnail_url: str = ""
    offers: list[TitleOffer] = field(default_factory=list)

    @property
    def jwt_offer(self) -> TitleOffer | None:
        jwt_offers = [o for o in self.offers if o.is_justwatch_tv]
        if not jwt_offers:
            return None
        for pref in ["_4K", "4K", "HD", "SD"]:
            for o in jwt_offers:
                if o.presentation_type.upper() == pref:
                    return o
        return jwt_offers[0]


@dataclass
class SeasonItem:
    id: str
    object_id: int
    season_number: int
    title: str
    episodes: list[EpisodeItem] = field(default_factory=list)


@dataclass
class TitleDetails:
    id: str
    object_id: int
    object_type: str  # "MOVIE", "SHOW", "SHOW_EPISODE"
    title: str
    release_year: int | None = None
    description: str = ""
    poster_url: str = ""
    backdrop_url: str = ""
    runtime: int | None = None
    genres: list[str] = field(default_factory=list)
    imdb_score: float | None = None
    offers: list[TitleOffer] = field(default_factory=list)
    seasons: list[SeasonItem] = field(default_factory=list)

    @property
    def is_movie(self) -> bool:
        return self.object_type.upper() == "MOVIE"

    @property
    def is_show(self) -> bool:
        return self.object_type.upper() == "SHOW"

    @property
    def is_episode(self) -> bool:
        return self.object_type.upper() in {"SHOW_EPISODE", "EPISODE"}

    @property
    def jwt_offer(self) -> TitleOffer | None:
        jwt_offers = [o for o in self.offers if o.is_justwatch_tv]
        if not jwt_offers:
            return None
        for pref in ["_4K", "4K", "HD", "SD"]:
            for o in jwt_offers:
                if o.presentation_type.upper() == pref:
                    return o
        return jwt_offers[0]


@dataclass
class PlayInfo:
    offer_id: str
    manifest_url: str = ""
    external_stream_url: str = ""
    drm_type: str = ""
    license_url: str = ""
    certificate_url: str = ""
    is_clear: bool = False

    @property
    def stream_url(self) -> str:
        return self.manifest_url or self.external_stream_url


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Safely decode JWT payload claims without cryptographic verification."""
    try:
        parts = token.strip().split(".")
        if len(parts) >= 2:
            pad = len(parts[1]) % 4
            b64 = parts[1] + ("=" * (4 - pad) if pad else "")
            return json.loads(base64.urlsafe_b64decode(b64.encode()).decode("utf-8"))
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------- API Client


class JustWatchTvApi:
    """GraphQL and Playback API client matching Android TV APK behavior."""

    def __init__(
        self,
        session: requests.Session | None = None,
        state: SessionState | None = None,
        country: str = "US",
        language: str = "en",
        on_save: Callable[[SessionState], None] | None = None,
    ):
        self.session = session or requests.Session()
        self.state = state or SessionState()
        self.country = country.upper()
        self.language = language.lower()
        self.on_save = on_save

    # -------------------------------------------------------------- HTTP

    def _headers(self, custom: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.state.device_id:
            headers["Device-ID"] = self.state.device_id
        if self.state.valid:
            headers["Authorization"] = f"Bearer {self.state.id_token}"
        if custom:
            headers.update(custom)
        return headers

    def _graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables

        req_headers = self._headers(headers)
        try:
            resp = self.session.post(
                GRAPHQL_URL,
                json=payload,
                headers=req_headers,
                timeout=25,
            )
        except requests.RequestException as exc:
            raise JustWatchTvError(f"JustWatch GraphQL request failed: {exc}") from exc

        if resp.status_code == 401 and "invalid device ID" in resp.text:
            # Refresh device ID and retry once
            self.state.device_id = self.get_or_create_device_id()
            req_headers["Device-ID"] = self.state.device_id
            try:
                resp = self.session.post(
                    GRAPHQL_URL,
                    json=payload,
                    headers=req_headers,
                    timeout=25,
                )
            except requests.RequestException as exc:
                raise JustWatchTvError(f"JustWatch GraphQL retry failed: {exc}") from exc

        if resp.status_code >= 400:
            raise JustWatchTvError(
                f"JustWatch GraphQL HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise JustWatchTvError(f"JustWatch GraphQL non-JSON response: {resp.text[:200]}") from exc

        errors = data.get("errors")
        if errors:
            first_msg = errors[0].get("message") if isinstance(errors, list) and errors else str(errors)
            raise JustWatchTvError(f"JustWatch GraphQL error: {first_msg}")

        return data.get("data") or {}

    # -------------------------------------------------------------- Device ID

    def get_or_create_device_id(self) -> str:
        """Obtain a device ID from backend mutation, falling back to cached or UUID."""
        if self.state.device_id:
            return self.state.device_id

        mutation = """
        mutation {
          getNewDeviceId(input: { doNotTrack: false, ids: [] }) {
            deviceId
          }
        }
        """
        try:
            data = self._graphql(mutation, headers={"User-Agent": DEVICE_USER_AGENT})
            device_id = data.get("getNewDeviceId", {}).get("deviceId")
            if device_id:
                self.state.device_id = str(device_id)
                self._persist()
                return self.state.device_id
        except Exception as exc:
            logger.debug("Failed to acquire remote device ID: %s", exc)

        # Fallback to local UUID
        self.state.device_id = str(uuid.uuid4())
        self._persist()
        return self.state.device_id

    # -------------------------------------------------------------- Auth

    def start_tv_code(self) -> TvCodeChallenge:
        """Generate a 4-letter alphanumeric TV pairing code and UUID."""
        self.get_or_create_device_id()
        mutation = """
        mutation AccountGetTvCodeMutation {
          getTvCode {
            code
            uuid
          }
        }
        """
        data = self._graphql(mutation)
        tv_data = data.get("getTvCode") or {}
        code = str(tv_data.get("code") or "").strip()
        uuid_str = str(tv_data.get("uuid") or "").strip()
        if not code or not uuid_str:
            raise JustWatchTvAuthError("Failed to obtain TV code from JustWatch backend")

        return TvCodeChallenge(
            code=code,
            uuid=uuid_str,
            verification_url=f"{TV_LOGIN_URL}?code={code}",
        )

    def poll_tv_code(self, challenge: TvCodeChallenge) -> SessionState | None:
        """Poll for user authorization of TV code and exchange for Firebase ID token."""
        mutation = """
        mutation PollForTvJwt($input: PollForTvJwtInput!) {
          pollForTvJwt(input: $input) {
            jwt
          }
        }
        """
        variables = {"input": {"code": challenge.code, "uuid": challenge.uuid}}
        headers = {
            "Device-Id": self.state.device_id,
            "App-Version": APP_VERSION,
        }
        data = self._graphql(mutation, variables=variables, headers=headers)
        poll_result = data.get("pollForTvJwt") or {}
        custom_jwt = str(poll_result.get("jwt") or "").strip()

        if not custom_jwt:
            return None

        payload = decode_jwt_payload(custom_jwt)
        iss = str(payload.get("iss") or "")
        user_id = str(payload.get("user_id") or payload.get("sub") or "")
        email = str(payload.get("email") or "")
        exp = float(payload.get("exp") or 0.0)

        # If the JWT is already a Firebase ID token or JustWatch session token
        if iss.startswith("https://securetoken.google.com/") or "justwatch.com" in iss:
            self.state.id_token = custom_jwt
            self.state.user_id = user_id
            self.state.email = email
            self.state.expires_at = exp if exp > 0 else (time.time() + 3600)
            self._persist()
            return self.state

        # Otherwise, exchange Firebase custom token with IdentityToolkit
        fb_data: dict[str, Any] | None = None
        last_exc: Exception | None = None
        for key in FIREBASE_FALLBACK_API_KEYS:
            url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={key}"
            try:
                resp = self.session.post(
                    url,
                    json={"token": custom_jwt, "returnSecureToken": True},
                    headers={"Content-Type": "application/json"},
                    timeout=20,
                )
                if resp.status_code == 200:
                    fb_data = resp.json()
                    break
                elif resp.status_code == 400 and "INVALID_CUSTOM_TOKEN" in resp.text:
                    last_exc = JustWatchTvAuthError(f"Firebase rejected token with key {key[:8]}...")
                    continue
                else:
                    resp.raise_for_status()
            except requests.RequestException as exc:
                last_exc = exc

        if fb_data:
            id_token = str(fb_data.get("idToken") or "")
            refresh_token = str(fb_data.get("refreshToken") or "")
            expires_in = float(fb_data.get("expiresIn") or 3600)
            local_id = str(fb_data.get("localId") or user_id)

            self.state.id_token = id_token
            self.state.refresh_token = refresh_token
            self.state.expires_at = time.time() + expires_in
            self.state.user_id = local_id
            if email and not self.state.email:
                self.state.email = email

            self._persist()
            return self.state

        # Fallback: If Firebase exchange failed, use JWT directly as session token
        logger.warning(
            "Firebase custom token exchange failed (%s); using TV session JWT directly",
            last_exc,
        )
        self.state.id_token = custom_jwt
        self.state.user_id = user_id
        self.state.email = email
        self.state.expires_at = exp if exp > 0 else (time.time() + 3600)
        self._persist()
        return self.state

    def refresh(self) -> SessionState:
        """Refresh Firebase authentication token via SecureToken endpoint."""
        if not self.state.refresh_token:
            raise JustWatchTvAuthError("No refresh token available")

        last_exc: Exception | None = None
        for key in FIREBASE_FALLBACK_API_KEYS:
            url = f"https://securetoken.googleapis.com/v1/token?key={key}"
            payload = {
                "grant_type": "refresh_token",
                "refresh_token": self.state.refresh_token,
            }
            try:
                resp = self.session.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=20,
                )
                if resp.status_code == 200:
                    fb_data = resp.json()
                    id_token = str(
                        fb_data.get("id_token")
                        or fb_data.get("access_token")
                        or ""
                    )
                    refresh_token = str(
                        fb_data.get("refresh_token") or self.state.refresh_token
                    )
                    expires_in = float(fb_data.get("expires_in") or 3600)
                    user_id = str(fb_data.get("user_id") or self.state.user_id)

                    self.state.id_token = id_token
                    self.state.refresh_token = refresh_token
                    self.state.expires_at = time.time() + expires_in
                    self.state.user_id = user_id

                    self._persist()
                    return self.state
                elif resp.status_code == 400:
                    continue
                else:
                    resp.raise_for_status()
            except requests.RequestException as exc:
                last_exc = exc

        raise JustWatchTvAuthError(f"Firebase token refresh failed: {last_exc}")

    def ensure_auth(self) -> None:
        """Ensure session has a valid token, refreshing if necessary."""
        self.get_or_create_device_id()
        if self.state.signed_in and not self.state.valid and self.state.refreshable:
            try:
                self.refresh()
            except Exception as exc:
                logger.warning("Token refresh failed: %s", exc)

    def _persist(self) -> None:
        if callable(self.on_save):
            try:
                self.on_save(self.state)
            except Exception as exc:
                logger.debug("on_save callback failed: %s", exc)

    # -------------------------------------------------------------- Resolving & Metadata

    def resolve_url(
        self,
        target: str,
        country: str | None = None,
        language: str | None = None,
    ) -> TitleDetails:
        """Resolve a full JustWatch web URL or direct ID into TitleDetails."""
        self.ensure_auth()
        c = (country or self.country).upper()
        lang = (language or self.language).lower()

        target = target.strip()
        # Direct entity ID check (e.g. tm940647, ts367130, tse6926495)
        if re.match(r"^(?:tm|ts|tss|tse|se|ep)\d+$", target, re.IGNORECASE):
            return self.get_node(target, country=c, language=lang)

        # Web URL
        parsed = urlparse(target)
        path = parsed.path
        if not path.startswith("/"):
            path = "/" + path

        query = """
        query ResolveUrl($fullPath: String!, $country: Country!, $language: Language!) {
          urlV2(fullPath: $fullPath) {
            node {
              id
              ... on Movie {
                objectId
                objectType
                content(country: $country, language: $language) {
                  title
                  originalReleaseYear
                  shortDescription
                  posterUrl
                  backdrops { backdropUrl }
                  scoring { imdbScore }
                  genres { translation(language: $language) }
                }
                offers(country: $country, platform: ANDROID_TV) {
                  id
                  monetizationType
                  presentationType
                  package {
                    id
                    packageId
                    shortName
                    technicalName
                    clearName
                  }
                  streamUrlExternalPlayer
                }
              }
              ... on Show {
                objectId
                objectType
                content(country: $country, language: $language) {
                  title
                  originalReleaseYear
                  shortDescription
                  posterUrl
                  backdrops { backdropUrl }
                  scoring { imdbScore }
                  genres { translation(language: $language) }
                }
                offers(country: $country, platform: ANDROID_TV) {
                  id
                  monetizationType
                  presentationType
                  package {
                    id
                    packageId
                    shortName
                    technicalName
                    clearName
                  }
                }
                seasons {
                  id
                  objectId
                  content(country: $country, language: $language) {
                    title
                    seasonNumber
                  }
                  episodes {
                    id
                    objectId
                    content(country: $country, language: $language) {
                      title
                      episodeNumber
                      seasonNumber
                      shortDescription
                    }
                    offers(country: $country, platform: ANDROID_TV) {
                      id
                      monetizationType
                      presentationType
                      package {
                        id
                        packageId
                        shortName
                        technicalName
                        clearName
                      }
                      streamUrlExternalPlayer
                    }
                  }
                }
              }
            }
          }
        }
        """
        variables = {"fullPath": path, "country": c, "language": lang}
        data = self._graphql(query, variables=variables)
        node = (data.get("urlV2") or {}).get("node")
        if not node:
            raise JustWatchTvNotFoundError(f"Could not resolve URL: {target}")

        return self._parse_node(node)

    def get_node(
        self,
        node_id: str,
        country: str | None = None,
        language: str | None = None,
    ) -> TitleDetails:
        """Fetch title node by ID."""
        self.ensure_auth()
        c = (country or self.country).upper()
        lang = (language or self.language).lower()

        query = """
        query GetNode($id: ID!, $country: Country!, $language: Language!) {
          node(id: $id) {
            id
            ... on Movie {
              objectId
              objectType
              content(country: $country, language: $language) {
                title
                originalReleaseYear
                shortDescription
                posterUrl
                backdrops { backdropUrl }
                scoring { imdbScore }
                genres { translation(language: $language) }
              }
              offers(country: $country, platform: ANDROID_TV) {
                id
                monetizationType
                presentationType
                package { packageId shortName technicalName clearName }
                streamUrlExternalPlayer
              }
            }
            ... on Show {
              objectId
              objectType
              content(country: $country, language: $language) {
                title
                originalReleaseYear
                shortDescription
                posterUrl
                backdrops { backdropUrl }
                scoring { imdbScore }
                genres { translation(language: $language) }
              }
              seasons {
                id
                objectId
                content(country: $country, language: $language) {
                  title
                  seasonNumber
                }
                episodes {
                  id
                  objectId
                  content(country: $country, language: $language) {
                    title
                    episodeNumber
                    seasonNumber
                    shortDescription
                  }
                  offers(country: $country, platform: ANDROID_TV) {
                    id
                    monetizationType
                    presentationType
                    package { packageId shortName technicalName clearName }
                    streamUrlExternalPlayer
                  }
                }
              }
            }
            ... on Episode {
              objectId
              objectType
              content(country: $country, language: $language) {
                title
                episodeNumber
                seasonNumber
                shortDescription
              }
              offers(country: $country, platform: ANDROID_TV) {
                id
                monetizationType
                presentationType
                package { packageId shortName technicalName clearName }
                streamUrlExternalPlayer
              }
            }
          }
        }
        """
        variables = {"id": node_id, "country": c, "language": lang}
        data = self._graphql(query, variables=variables)
        node = data.get("node")
        if not node:
            raise JustWatchTvNotFoundError(f"Node not found: {node_id}")

        return self._parse_node(node)

    def _parse_node(self, node: dict[str, Any]) -> TitleDetails:
        node_id = str(node.get("id") or "")
        object_id = int(node.get("objectId") or 0)
        object_type = str(node.get("objectType") or "").upper()
        content = node.get("content") or {}

        title_name = str(content.get("title") or "")
        year = content.get("originalReleaseYear")
        release_year = int(year) if year is not None else None
        description = str(content.get("shortDescription") or "")

        poster_template = str(content.get("posterUrl") or "")
        poster_url = self._format_image_url(poster_template, profile="s718", fmt="jpg")

        backdrops = content.get("backdrops") or []
        backdrop_url = ""
        if backdrops and isinstance(backdrops, list):
            first_bd = backdrops[0].get("backdropUrl") or ""
            backdrop_url = self._format_image_url(first_bd, profile="s1920", fmt="jpg")

        scoring = content.get("scoring") or {}
        imdb_score = scoring.get("imdbScore")

        genres_list: list[str] = []
        for g in content.get("genres") or []:
            trans = g.get("translation")
            if trans:
                genres_list.append(str(trans))

        # Parse offers
        offers: list[TitleOffer] = []
        for o in node.get("offers") or []:
            pkg = o.get("package") or {}
            offers.append(
                TitleOffer(
                    id=str(o.get("id") or ""),
                    package_id=pkg.get("packageId"),
                    short_name=str(pkg.get("shortName") or ""),
                    technical_name=str(pkg.get("technicalName") or ""),
                    clear_name=str(pkg.get("clearName") or ""),
                    monetization_type=str(o.get("monetizationType") or ""),
                    presentation_type=str(o.get("presentationType") or ""),
                    stream_url_external=str(o.get("streamUrlExternalPlayer") or ""),
                )
            )

        # Parse seasons & episodes if show
        seasons: list[SeasonItem] = []
        for s in node.get("seasons") or []:
            s_content = s.get("content") or {}
            episodes: list[EpisodeItem] = []
            for ep in s.get("episodes") or []:
                ep_content = ep.get("content") or {}
                ep_offers: list[TitleOffer] = []
                for o in ep.get("offers") or []:
                    pkg = o.get("package") or {}
                    ep_offers.append(
                        TitleOffer(
                            id=str(o.get("id") or ""),
                            package_id=pkg.get("packageId"),
                            short_name=str(pkg.get("shortName") or ""),
                            technical_name=str(pkg.get("technicalName") or ""),
                            clear_name=str(pkg.get("clearName") or ""),
                            monetization_type=str(o.get("monetizationType") or ""),
                            presentation_type=str(o.get("presentationType") or ""),
                            stream_url_external=str(o.get("streamUrlExternalPlayer") or ""),
                        )
                    )
                episodes.append(
                    EpisodeItem(
                        id=str(ep.get("id") or ""),
                        object_id=int(ep.get("objectId") or 0),
                        season_number=int(ep_content.get("seasonNumber") or s_content.get("seasonNumber") or 1),
                        episode_number=int(ep_content.get("episodeNumber") or len(episodes) + 1),
                        title=str(ep_content.get("title") or f"Episode {len(episodes) + 1}"),
                        description=str(ep_content.get("shortDescription") or ""),
                        offers=ep_offers,
                    )
                )

            seasons.append(
                SeasonItem(
                    id=str(s.get("id") or ""),
                    object_id=int(s.get("objectId") or 0),
                    season_number=int(s_content.get("seasonNumber") or len(seasons) + 1),
                    title=str(s_content.get("title") or f"Season {len(seasons) + 1}"),
                    episodes=episodes,
                )
            )

        return TitleDetails(
            id=node_id,
            object_id=object_id,
            object_type=object_type,
            title=title_name,
            release_year=release_year,
            description=description,
            poster_url=poster_url,
            backdrop_url=backdrop_url,
            genres=genres_list,
            imdb_score=float(imdb_score) if imdb_score is not None else None,
            offers=offers,
            seasons=seasons,
        )

    # -------------------------------------------------------------- Playback

    def get_play_info(
        self,
        offer_id: str,
        drm_type: str = "WIDEVINE",
        offer_external_stream: str = "",
    ) -> PlayInfo:
        """Call BitmovinPlayerNativeGetPlayInfoMutation to acquire stream & DRM info."""
        self.ensure_auth()
        mutation = """
        mutation BitmovinPlayerNativeGetPlayInfoMutation($input: GetPlayInformationInput!) {
          getPlayInformation(input: $input) {
            errorCode
            vastTag
            playbackProgress {
              position
              runtime
              completed
            }
            offer {
              mediaDealId
              streamUrlExternalPlayer
              id
            }
            streams {
              manifestUrlSignedVariants
              drmConfigs {
                drmType
                laURL
                certificateURL
              }
            }
          }
        }
        """
        variables = {
            "input": {
                "offerId": offer_id,
                "drmType": drm_type.upper(),
            }
        }
        play_info_data: dict[str, Any] = {}
        try:
            data = self._graphql(mutation, variables=variables)
            play_info_data = data.get("getPlayInformation") or {}
        except Exception as exc:
            if offer_external_stream:
                logger.debug("Playback mutation error (%s); falling back to offer external stream", exc)
            else:
                raise

        error_code = play_info_data.get("errorCode")
        offer_data = play_info_data.get("offer") or {}
        external_stream = str(offer_data.get("streamUrlExternalPlayer") or offer_external_stream or "")

        if error_code:
            if external_stream:
                return PlayInfo(
                    offer_id=offer_id,
                    external_stream_url=external_stream,
                    is_clear=True,
                )
            raise JustWatchTvPlayError(f"Playback returned error code: {error_code}")

        streams = play_info_data.get("streams") or []
        manifest_url = ""
        la_url = ""
        cert_url = ""
        matched_drm_type = ""

        if streams and isinstance(streams, list):
            first_stream = streams[0]
            manifest_url = str(first_stream.get("manifestUrlSignedVariants") or "")
            drm_configs = first_stream.get("drmConfigs") or []
            for cfg in drm_configs:
                cur_type = str(cfg.get("drmType") or "").upper()
                if cur_type == drm_type.upper():
                    matched_drm_type = cur_type
                    la_url = str(cfg.get("laURL") or "")
                    cert_url = str(cfg.get("certificateURL") or "")
                    break
            if not la_url and drm_configs:
                matched_drm_type = str(drm_configs[0].get("drmType") or "").upper()
                la_url = str(drm_configs[0].get("laURL") or "")
                cert_url = str(drm_configs[0].get("certificateURL") or "")

        if not manifest_url and not external_stream:
            raise JustWatchTvPlayError("No playable stream returned by JustWatch TV")

        is_clear = bool(external_stream and not manifest_url and not la_url)

        return PlayInfo(
            offer_id=offer_id,
            manifest_url=manifest_url,
            external_stream_url=external_stream,
            drm_type=matched_drm_type or ("WIDEVINE" if la_url else ""),
            license_url=la_url or (DRM_LICENSE_URL if not is_clear and manifest_url else ""),
            certificate_url=cert_url,
            is_clear=is_clear,
        )

    # -------------------------------------------------------------- Search & Catalog

    def search(
        self,
        query: str,
        country: str | None = None,
        language: str | None = None,
        first: int = 20,
    ) -> list[TitleDetails]:
        """Search titles carrying JustWatch TV (jwt) offers."""
        self.ensure_auth()
        c = (country or self.country).upper()
        lang = (language or self.language).lower()

        gql_query = """
        query SearchTitles($query: String!, $country: Country!, $language: Language!, $first: Int!) {
          popularTitles(
            country: $country
            filter: {
              searchQuery: $query
              packages: ["jwt"]
              monetizationTypes: [FREE, ADS]
            }
            first: $first
          ) {
            edges {
              node {
                id
                objectId
                objectType
                content(country: $country, language: $language) {
                  title
                  originalReleaseYear
                  shortDescription
                  posterUrl
                  backdrops { backdropUrl }
                  scoring { imdbScore }
                }
                offers(country: $country, platform: ANDROID_TV, filter: { packages: ["jwt"] }) {
                  id
                  monetizationType
                  presentationType
                  package { packageId shortName technicalName clearName }
                  streamUrlExternalPlayer
                }
              }
            }
          }
        }
        """
        variables = {"query": query, "country": c, "language": lang, "first": first}
        data = self._graphql(gql_query, variables=variables)
        edges = (data.get("popularTitles") or {}).get("edges") or []

        results: list[TitleDetails] = []
        for edge in edges:
            node = edge.get("node")
            if node:
                results.append(self._parse_node(node))
        return results

    def popular_titles(
        self,
        country: str | None = None,
        language: str | None = None,
        first: int = 30,
    ) -> list[TitleDetails]:
        """Fetch popular free JustWatch TV catalogue items."""
        self.ensure_auth()
        c = (country or self.country).upper()
        lang = (language or self.language).lower()

        gql_query = """
        query PopularJustWatchTitles($country: Country!, $language: Language!, $first: Int!) {
          popularTitles(
            country: $country
            filter: {
              packages: ["jwt"]
              monetizationTypes: [FREE, ADS]
            }
            first: $first
          ) {
            edges {
              node {
                id
                objectId
                objectType
                content(country: $country, language: $language) {
                  title
                  originalReleaseYear
                  shortDescription
                  posterUrl
                  backdrops { backdropUrl }
                  scoring { imdbScore }
                }
                offers(country: $country, platform: ANDROID_TV, filter: { packages: ["jwt"] }) {
                  id
                  monetizationType
                  presentationType
                  package { packageId shortName technicalName clearName }
                  streamUrlExternalPlayer
                }
              }
            }
          }
        }
        """
        variables = {"country": c, "language": lang, "first": first}
        data = self._graphql(gql_query, variables=variables)
        edges = (data.get("popularTitles") or {}).get("edges") or []

        results: list[TitleDetails] = []
        for edge in edges:
            node = edge.get("node")
            if node:
                results.append(self._parse_node(node))
        return results

    def live_fast_titles(
        self,
        country: str | None = None,
        language: str | None = None,
    ) -> list[TitleDetails]:
        """Fetch FAST / AVOD Free titles from home DiscoveryQuery or popular titles."""
        return self.popular_titles(country=country, language=language, first=40)

    # -------------------------------------------------------------- Attachments & Chapters

    @staticmethod
    def _format_image_url(template: str, profile: str = "s718", fmt: str = "jpg") -> str:
        if not template:
            return ""
        path = template.replace("{profile}", profile).replace("{format}", fmt)
        if not path.startswith("http"):
            path = f"{IMAGE_BASE_URL}{path if path.startswith('/') else '/' + path}"
        return path

    def extract_attachments(
        self,
        title: TitleDetails,
        episode: EpisodeItem | None = None,
    ) -> list[Attachment]:
        """Construct Attachment objects for title poster and backdrop."""
        attachments: list[Attachment] = []
        seen_urls: set[str] = set()

        if title.poster_url and title.poster_url not in seen_urls:
            seen_urls.add(title.poster_url)
            attachments.append(
                Attachment(
                    url=title.poster_url,
                    name="Poster",
                    kind="poster",
                    mime_type="image/jpeg",
                )
            )

        if title.backdrop_url and title.backdrop_url not in seen_urls:
            seen_urls.add(title.backdrop_url)
            attachments.append(
                Attachment(
                    url=title.backdrop_url,
                    name="Backdrop",
                    kind="artwork",
                    mime_type="image/jpeg",
                )
            )

        if episode and episode.thumbnail_url and episode.thumbnail_url not in seen_urls:
            seen_urls.add(episode.thumbnail_url)
            attachments.append(
                Attachment(
                    url=episode.thumbnail_url,
                    name="Thumbnail",
                    kind="thumbnail",
                    mime_type="image/jpeg",
                )
            )

        return attachments

    def parse_manifest_chapters(self, manifest_url: str) -> list[Chapter]:
        """Parse HLS manifest for cue markers, ad breaks or segment transitions as chapters."""
        if not manifest_url:
            return []

        try:
            resp = self.session.get(manifest_url, headers=self._headers(), timeout=15)
            if resp.status_code >= 400:
                return []
            text = resp.text
        except Exception:
            return []

        chapters: list[Chapter] = []
        current_time_ms = 0
        chapter_idx = 1

        inf_re = re.compile(r"^#EXTINF:([\d.]+)", re.MULTILINE)
        cue_re = re.compile(r"^#(?:EXT-X-CUE-OUT|EXT-X-DATERANGE|EXT-OATCLS)", re.MULTILINE)

        # Check for inline cue markers
        for line in text.splitlines():
            line = line.strip()
            inf_match = inf_re.match(line)
            if inf_match:
                duration_sec = float(inf_match.group(1))
                current_time_ms += int(round(duration_sec * 1000))
            elif cue_re.match(line):
                chapters.append(
                    Chapter(
                        start_ms=current_time_ms,
                        title=f"Segment {chapter_idx}",
                        kind="ad" if "CUE" in line else "scene",
                    )
                )
                chapter_idx += 1

        return chapters

    # -------------------------------------------------------------- Licensing

    def license_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/octet-stream",
        }
        if self.state.device_id:
            headers["Device-ID"] = self.state.device_id
        if self.state.valid:
            headers["Authorization"] = f"Bearer {self.state.id_token}"
        return headers

    def widevine_license(
        self,
        challenge: bytes,
        license_url: str | None = None,
    ) -> bytes:
        """Post Widevine license challenge to JustWatch DRM endpoint."""
        url = license_url or DRM_LICENSE_URL
        headers = self.license_headers()
        try:
            resp = self.session.post(
                url,
                data=challenge,
                headers=headers,
                timeout=25,
            )
        except requests.RequestException as exc:
            raise JustWatchTvPlayError(f"License server network error: {exc}") from exc

        if resp.status_code >= 400:
            raise JustWatchTvPlayError(
                f"License server returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        if not resp.content:
            raise JustWatchTvPlayError("License server returned empty payload")

        return resp.content
