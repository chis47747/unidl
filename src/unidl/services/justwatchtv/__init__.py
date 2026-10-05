"""JustWatch TV service implementation.

Provides free streaming movies, TV shows, and FAST catalogue on JustWatch TV
aligned with Android TV APK com.justwatch.justwatch (TV-code login, token refresh,
title metadata, Widevine/Clear playback, search, live FAST catalogue, attachments,
and chapters).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from ...core.credentials import mask_account
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.qr import QrPresentation
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from . import api

logger = logging.getLogger(__name__)

_COUNTRY = Setting(
    key="country",
    label="Country / Region",
    kind="text",
    default="US",
    help="ISO 3166-1 alpha-2 country code (e.g. US, DE, GB).",
)

_LANGUAGE = Setting(
    key="language",
    label="Language",
    kind="text",
    default="en",
    help="ISO 639-1 language code (e.g. en, de).",
)

_LOGIN_METHOD = Setting(
    key="login_method",
    label="Sign-in method",
    kind="choice",
    options=[
        Option("tv", "TV pairing code (justwatch.com/tv)"),
        Option("anonymous", "Anonymous (free streaming only)"),
    ],
    default="tv",
    help="Sign-in method for JustWatch TV.",
)


class JustWatchTV(Service):
    ID = "justwatchtv"
    NAME = "JustWatch TV"
    TAG = "JWT"
    ALIASES = ("jwt", "jwtv", "justwatch_tv")
    DESCRIPTION = (
        "JustWatch TV free streaming movies, TV shows, and FAST catalogue over "
        "Android TV GraphQL API with TV-code login, Widevine DRM, attachments, and chapters."
    )
    TITLE_RE = (
        r"(?:https?://)?(?:www\.)?justwatch\.com/(?:[a-z]{2}/)?(?:movie|tv-show)/"
        r"|^(?:tm|ts|tss|tse)\d+$"
    )
    GEOFENCE = ()
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SETTINGS = [_COUNTRY, _LANGUAGE, _LOGIN_METHOD]
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    SUPPORTS_LIBRARY = False

    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self._api: api.JustWatchTvApi | None = None

    # -------------------------------------------------------------------- auth

    def auth_status(self) -> AuthStatus:
        raw = self.ctx.tokens.read(self.TOKEN_FILE)
        state = api.SessionState.from_cache(raw)
        if state.signed_in and state.valid:
            return AuthStatus(
                logged_in=True,
                label=mask_account(state.email or state.user_id or "JustWatch Account"),
                detail=f"token · {int(state.hours_left)}h left",
                anonymous_ok=True,
                logout_available=True,
            )
        if state.refreshable:
            return AuthStatus(
                logged_in=True,
                label=mask_account(state.email or state.user_id or "JustWatch Account"),
                detail="session ready to refresh",
                anonymous_ok=True,
                logout_available=True,
            )
        return AuthStatus(
            logged_in=False,
            label="Anonymous (TV pairing available)",
            detail="Free content available without sign-in",
            anonymous_ok=True,
            logout_available=False,
        )

    def _new_client(self) -> api.JustWatchTvApi:
        raw = self.ctx.tokens.read(self.TOKEN_FILE)
        state = api.SessionState.from_cache(raw)
        country = str(self.settings.get("country") or "US")
        language = str(self.settings.get("language") or "en")
        return api.JustWatchTvApi(
            session=self.ctx.session(user_agent=api.USER_AGENT),
            state=state,
            country=country,
            language=language,
            on_save=self._save_state,
        )

    def _save_state(self, state: api.SessionState) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())

    def client(self, ctx: FlowContext | None = None) -> api.JustWatchTvApi:
        if self._api is not None:
            return self._api
        client = self._new_client()
        if client.state.signed_in and not client.state.valid and client.state.refreshable:
            if ctx is not None:
                ctx.status("Refreshing JustWatch TV session")
            try:
                client.refresh()
                if ctx is not None:
                    ctx.log("JustWatch TV session refreshed", "ok")
            except Exception as exc:
                logger.warning("Session refresh failed: %s", exc)
        self._api = client
        return client

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client()
        if client.state.signed_in and client.state.valid:
            self._api = client
            ctx.log("already signed in", "ok")
            return

        if client.state.refreshable:
            ctx.status("Refreshing JustWatch TV session")
            try:
                client.refresh()
                self._api = client
                ctx.log("JustWatch TV session refreshed", "ok")
                return
            except Exception:
                pass

        ctx.status("Requesting JustWatch TV pairing code")
        try:
            challenge = client.start_tv_code()
        except api.JustWatchTvError as exc:
            ctx.error(str(exc))
            return

        lines: list[str | tuple[str, str]] = [
            ("Code", challenge.code),
            ("Open", api.TV_LOGIN_URL),
        ]
        qr = QrPresentation(
            payload=challenge.verification_url,
            fallback_url=api.TV_LOGIN_URL,
            alt="JustWatch TV sign-in QR code",
        )

        def poll() -> api.SessionState | None:
            return client.poll_tv_code(challenge)

        try:
            state = yield ctx.wait_for(
                "Confirm JustWatch TV on another device",
                lines,
                poll,
                timeout=600.0,
                interval=2.5,
                hint="Open https://www.justwatch.com/tv/, enter the code, and confirm on your phone or computer.",
                qr=qr,
            )
        except Back:
            return
        except Exception as exc:
            ctx.error(str(exc))
            return

        if not isinstance(state, api.SessionState) or not state.valid:
            ctx.error("JustWatch TV login completed without a valid session")
            return

        self._save_state(state)
        self._api = client
        ctx.log("JustWatch TV signed in successfully", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._api = None

    # ---------------------------------------------------------------- browsing

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status(f"Resolving JustWatch title {target}")
            details = client.resolve_url(target)
        except api.JustWatchTvError as exc:
            ctx.error(str(exc))
            return

        yield from self._open_details(ctx, client, details)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status(f"Searching JustWatch TV for {query}")
            results = client.search(query)
        except api.JustWatchTvError as exc:
            ctx.error(str(exc))
            return

        if not results:
            ctx.warn(f"JustWatch TV found no titles matching {query}")
            return

        while True:
            choices = []
            for item in results:
                year_str = f" ({item.release_year})" if item.release_year else ""
                score_str = f" · IMDb {item.imdb_score:.1f}" if item.imdb_score else ""
                type_str = "Movie" if item.is_movie else "Show"
                label = f"{item.title}{year_str}"
                detail = f"{type_str}{score_str} · {item.description[:60]}..." if item.description else type_str
                choices.append(Choice(label, item, detail=detail))

            try:
                chosen = yield ctx.pick(
                    f"JustWatch TV  ·  {query}  ·  {len(results)} results",
                    choices,
                )
            except Back:
                return

            if chosen is None:
                return

            yield from self._open_details(ctx, client, chosen)

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        """Browse JustWatch TV Free / FAST catalogue."""
        try:
            client = self.client(ctx)
            ctx.status("Loading JustWatch TV Free catalogue")
            titles = client.live_fast_titles()
        except api.JustWatchTvError as exc:
            ctx.error(str(exc))
            return

        if not titles:
            ctx.warn("No free JustWatch TV titles were returned")
            return

        while True:
            choices = []
            for item in titles:
                year_str = f" ({item.release_year})" if item.release_year else ""
                score_str = f" · IMDb {item.imdb_score:.1f}" if item.imdb_score else ""
                type_str = "Movie" if item.is_movie else "Show"
                label = f"{item.title}{year_str}"
                detail = f"{type_str}{score_str}"
                choices.append(Choice(label, item, detail=detail))

            try:
                chosen = yield ctx.pick(
                    f"JustWatch TV Free & FAST  ·  {len(titles)} titles",
                    choices,
                )
            except Back:
                return

            if chosen is None:
                return

            yield from self._open_details(ctx, client, chosen)

    def _open_details(
        self,
        ctx: FlowContext,
        client: api.JustWatchTvApi,
        details: api.TitleDetails,
    ) -> Iterator[Ask]:
        if details.is_movie:
            if not details.jwt_offer:
                ctx.status(f"Loading streaming offer for {details.title}")
                try:
                    details = client.get_node(details.id)
                except api.JustWatchTvError as exc:
                    ctx.error(str(exc))
                    return
            offer = details.jwt_offer
            if not offer:
                ctx.error(f"No JustWatch TV streaming offer available for {details.title}")
                return
            yield from self._emit_playable(ctx, client, details, offer)
            return

        if details.is_show:
            if not details.seasons:
                ctx.status(f"Loading episodes for {details.title}")
                try:
                    details = client.get_node(details.id)
                except api.JustWatchTvError as exc:
                    ctx.error(str(exc))
                    return

            if not details.seasons:
                ctx.error(f"No seasons or episodes available for show {details.title}")
                return

            if len(details.seasons) == 1:
                season = details.seasons[0]
                yield from self._pick_episodes(ctx, client, details, season)
                return

            # Multiple seasons: choose season first
            while True:
                season_choices = [
                    Choice(
                        s.title,
                        s,
                        detail=f"{len(s.episodes)} episodes",
                    )
                    for s in details.seasons
                ]
                try:
                    selected_season = yield ctx.pick(
                        f"{details.title}  ·  Seasons",
                        season_choices,
                    )
                except Back:
                    return

                if selected_season is None:
                    return

                yield from self._pick_episodes(ctx, client, details, selected_season)

        if details.is_episode:
            offer = details.jwt_offer
            if not offer:
                ctx.error(f"No JustWatch TV offer found for {details.title}")
                return
            yield from self._emit_playable(ctx, client, details, offer)

    def _pick_episodes(
        self,
        ctx: FlowContext,
        client: api.JustWatchTvApi,
        show: api.TitleDetails,
        season: api.SeasonItem,
    ) -> Iterator[Ask]:
        if not season.episodes:
            ctx.error(f"No episodes found in {season.title}")
            return

        ep_choices = [
            Choice(
                f"E{ep.episode_number:02d} · {ep.title}",
                ep,
                detail=ep.description[:80] + "..." if len(ep.description) > 80 else ep.description,
            )
            for ep in season.episodes
        ]

        while True:
            try:
                picked = yield ctx.pick(
                    f"{show.title}  ·  {season.title}",
                    ep_choices,
                    multi=True,
                    hint="space to select multiple, enter to confirm",
                )
            except Back:
                return

            if picked is None:
                return

            selected_list = list(picked) if isinstance(picked, (list, tuple)) else [picked]
            if len(selected_list) > 1:
                ctx.batch(len(selected_list))

            for ep in selected_list:
                offer = ep.jwt_offer
                if not offer:
                    ctx.warn(f"No JustWatch TV offer for S{ep.season_number:02d}E{ep.episode_number:02d} ({ep.title})")
                    continue
                yield from self._emit_playable(ctx, client, show, offer, episode=ep)
            return

    def _emit_playable(
        self,
        ctx: FlowContext,
        client: api.JustWatchTvApi,
        details: api.TitleDetails,
        offer: api.TitleOffer,
        episode: api.EpisodeItem | None = None,
    ) -> Iterator[Ask]:
        title_label = (
            f"{details.title} S{episode.season_number:02d}E{episode.episode_number:02d} - {episode.title}"
            if episode
            else details.title
        )
        ctx.status(f"Retrieving stream for {title_label}")
        try:
            play_info = client.get_play_info(
                offer.id,
                offer_external_stream=offer.stream_url_external,
            )
        except api.JustWatchTvError as exc:
            ctx.error(str(exc))
            return

        playback = self._create_playback(client, details, offer, play_info, episode=episode)

        # Attachments
        if self.fetch_attachments_enabled():
            try:
                attachments = client.extract_attachments(details, episode=episode)
                if attachments:
                    playback.attachments = attachments
                    ctx.log(f"attachments: {', '.join(a.name for a in attachments)}", "info")
            except Exception as exc:
                ctx.warn(f"Failed to extract attachments: {exc}")

        # Chapters
        if self.fetch_chapters_enabled() and play_info.stream_url:
            try:
                chapters = client.parse_manifest_chapters(play_info.stream_url)
                if chapters:
                    playback.chapters = chapters
                    ctx.log(f"chapters: {len(chapters)} cues", "info")
            except Exception as exc:
                ctx.warn(f"Failed to extract chapters: {exc}")

        yield ctx.emit(playback)

    def _create_playback(
        self,
        client: api.JustWatchTvApi,
        details: api.TitleDetails,
        offer: api.TitleOffer,
        play_info: api.PlayInfo,
        episode: api.EpisodeItem | None = None,
    ) -> Playback:
        if episode is not None:
            title = Title(
                id=episode.id,
                kind=TitleKind.EPISODE,
                name=details.title,
                season=episode.season_number,
                episode=episode.episode_number,
                episode_name=episode.title,
                year=str(details.release_year) if details.release_year else None,
                synopsis=episode.description or details.description or None,
                service=self.ID,
                data={
                    "offer_id": offer.id,
                    "title_id": details.id,
                    "episode_id": episode.id,
                    "stream_url_external": offer.stream_url_external,
                },
            )
        else:
            title = Title(
                id=details.id,
                kind=TitleKind.MOVIE,
                name=details.title,
                year=str(details.release_year) if details.release_year else None,
                synopsis=details.description or None,
                service=self.ID,
                data={
                    "offer_id": offer.id,
                    "title_id": details.id,
                    "stream_url_external": offer.stream_url_external,
                },
            )

        drm: DrmInfo | None = None
        if not play_info.is_clear and play_info.license_url:
            drm = DrmInfo(
                system="widevine",
                license_url=play_info.license_url,
                headers=client.license_headers(),
            )
        else:
            drm = DrmInfo(clear=True)

        save_name = self.save_name(title)
        return Playback(
            title=title,
            save_name=save_name,
            manifest_url=play_info.stream_url,
            headers=client._headers(),
            drm=drm,
        )

    # --------------------------------------------------------------- playback

    def get_playback(self, title: Title) -> Playback:
        """Resolve a Title into a Playback object directly."""
        client = self.client()
        offer_id = str(title.data.get("offer_id") or "")
        offer_stream_url = str(title.data.get("stream_url_external") or "")
        if not offer_id:
            # Re-fetch title node and acquire first offer
            details = client.get_node(title.id)
            offer = details.jwt_offer
            if not offer:
                raise api.JustWatchTvPlayError(f"No JustWatch TV offer found for {title.name}")
            offer_id = offer.id
            offer_stream_url = offer.stream_url_external

        play_info = client.get_play_info(offer_id, offer_external_stream=offer_stream_url)
        drm: DrmInfo | None = None
        if not play_info.is_clear and play_info.license_url:
            drm = DrmInfo(
                system="widevine",
                license_url=play_info.license_url,
                headers=client.license_headers(),
            )
        else:
            drm = DrmInfo(clear=True)

        return Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=play_info.stream_url,
            headers=client._headers(),
            drm=drm,
        )

    # -------------------------------------------------------------- licensing

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        """Acquire Widevine licence from JustWatch DRM server."""
        return self._client_for_licence().widevine_license(
            challenge,
            license_url=drm.license_url,
        )

    def _client_for_licence(self) -> api.JustWatchTvApi:
        if self._api is None:
            self._api = self.client()
        return self._api


registry.register(JustWatchTV)
