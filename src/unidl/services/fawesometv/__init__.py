"""Fawesome TV - free ad-supported streaming movies, TV shows, and live FAST channels.

Authorization:  Anonymous session (no credentials needed), auto-refreshing token.
Catalogue:      URL (movie/series/episode/live), native search, live channels, categories.
Playback:       Clear HLS/MP4 or Widevine DRM DASH MPD; live HLS with macro cleanup.
Attachments:    Posters, backdrops, banners, thumbnails, and icons.
Chapters:       Intro and credits navigation markers.
Subtitles:      Multi-language closed captions.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from ...core.cdm import CdmError
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from . import api


@registry.register
class FawesomeTV(Service):
    ID = "fawesometv"
    NAME = "Fawesome TV"
    TAG = "FAWESOME"
    ALIASES = ("fawesome", "fawesometv", "fawesome_tv")
    TITLE_RE = (
        r"(?:https?://)?(?:(?:www\.)?fawesome\.tv|fawesome\.ifood\.tv)/"
        r"|\b(?:fawesometv|fawesome):"
        r"|\bhappykids://play"
    )
    GEOFENCE = ()
    DESCRIPTION = (
        "Fawesome TV movies, TV shows, and live FAST channels over the Android TV API: "
        "anonymous session, native search, Widevine DRM, attachments, and chapters."
    )
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True

    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.FawesomeTVApi | None = None

    # -------------------------------------------------------------------- auth

    def auth_status(self) -> AuthStatus:
        raw = self.ctx.tokens.read(self.TOKEN_FILE) or {}
        state = api.TokenState.from_cache(raw)
        if state.is_valid():
            return AuthStatus(
                logged_in=True,
                label="anonymous session",
                detail=f"Device: {state.device_id[:8]}...",
                anonymous_ok=True,
            )
        if state.is_refreshable():
            return AuthStatus(
                logged_in=True,
                label="session ready to refresh",
                detail="Anonymous session",
                anonymous_ok=True,
            )
        return AuthStatus(
            logged_in=False,
            label="anonymous · initialized on demand",
            detail="No sign-in required",
            anonymous_ok=True,
        )

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        """Bootstrap or refresh an anonymous Fawesome TV device session."""
        ctx.status("Initializing Fawesome TV anonymous session")
        client = self._new_client()
        try:
            state = client.ensure_session()
            self._save_state(state)
            self._api = client
            ctx.log("Fawesome TV anonymous session active", "ok")
        except api.FawesomeTVError as exc:
            ctx.error(f"Failed to initialize anonymous session: {exc}")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._api = None

    def _save_state(self, state: api.TokenState) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())

    def _new_client(self) -> api.FawesomeTVApi:
        state = api.TokenState.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        return api.FawesomeTVApi(
            session=self.ctx.session(user_agent=api.USER_AGENT),
            state=state,
            on_update=self._save_state,
        )

    def client(self, ctx: FlowContext | None = None) -> api.FawesomeTVApi:
        if self._api is not None:
            if self._api.state.is_valid():
                return self._api
            try:
                self._api.ensure_session()
                return self._api
            except api.FawesomeTVError:
                self._api = None

        client = self._new_client()
        if ctx is not None:
            ctx.status("Opening Fawesome TV session")
        client.ensure_session()
        self._api = client
        return client

    # ---------------------------------------------------------------- browsing

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        try:
            kind, content_id = api.parse_fawesometv_reference(target)
        except api.FawesomeTVError as exc:
            ctx.error(str(exc))
            return

        # Extract title slug from target URL if present as hint
        slug_match = re.search(
            r"/(?:movies|movie|shows|tv-shows|fawesome-topics)/[0-9]+(?:[-/]([^/?#]+))?",
            target,
            re.IGNORECASE,
        )
        title_hint = ""
        if slug_match and slug_match.group(1):
            title_hint = slug_match.group(1).replace("-", " ").strip()

        client = self.client(ctx)
        yield from self._open_reference(ctx, client, kind, content_id, title_hint=title_hint)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        """Search using native Fawesome TV search (recipes.php?searchType=search)."""
        client = self.client(ctx)
        ctx.status(f"Searching Fawesome TV for {query}")
        try:
            hits = client.search(query)
        except api.FawesomeTVError as exc:
            ctx.error(str(exc))
            return

        if not hits:
            ctx.warn(f"Fawesome TV found nothing for {query}")
            return

        while True:
            try:
                chosen = yield ctx.pick(
                    f"Fawesome TV · {query} · {len(hits)} results",
                    [Choice(hit.label, hit, detail=hit.detail) for hit in hits],
                )
            except Back:
                return

            if chosen is None:
                return

            if chosen.is_show:
                yield from self._open_show(ctx, client, chosen.id, series_item=chosen)
            else:
                yield from self._emit_playable(ctx, client, chosen)

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        """Browse live FAST channels."""
        client = self.client(ctx)
        ctx.status("Loading Fawesome TV live channels")
        try:
            channels = client.get_live_channels()
        except api.FawesomeTVError as exc:
            ctx.error(str(exc))
            return

        if not channels:
            ctx.warn("No Fawesome TV live channels available")
            return

        while True:
            try:
                chosen = yield ctx.pick(
                    f"Fawesome TV Live · {len(channels)} channels",
                    [Choice(ch.label, ch, detail=ch.detail) for ch in channels],
                )
            except Back:
                return

            if chosen is None:
                return

            yield from self._emit_live(ctx, client, chosen)

    def browse(self, ctx: FlowContext) -> Iterator[Ask]:
        """Browse top-level catalog categories."""
        client = self.client(ctx)
        ctx.status("Loading Fawesome TV categories")
        try:
            cats = client.get_categories()
        except api.FawesomeTVError as exc:
            ctx.error(str(exc))
            return

        if not cats:
            ctx.warn("No Fawesome TV categories available")
            return

        while True:
            try:
                chosen = yield ctx.pick(
                    "Fawesome TV · Categories",
                    [Choice(c.title, c, detail="Live channels" if c.is_live else "Category") for c in cats],
                )
            except Back:
                return

            if chosen is None:
                return

            if chosen.is_live:
                yield from self.live(ctx)
            else:
                ctx.warn(f"Use search to explore {chosen.title} titles")

    # ------------------------------------------------------------- resolutions

    def _open_reference(
        self,
        ctx: FlowContext,
        client: api.FawesomeTVApi,
        kind: str,
        content_id: str,
        title_hint: str = "",
    ) -> Iterator[Ask]:
        trimmed = api.trim_id(content_id)

        # 1. Explicit or likely TV Show
        if kind == "show":
            yield from self._open_show(ctx, client, trimmed)
            return

        # 2. Live channel
        if kind == "live":
            ctx.status(f"Loading live channel {trimmed}")
            channels = client.get_live_channels()
            ch = next((c for c in channels if api.trim_id(c.id) == trimmed), None)
            if ch:
                yield from self._emit_live(ctx, client, ch)
                return
            ctx.error(f"Live channel {content_id} not found")
            return

        # 3. Explicit movie
        if kind == "movie":
            ctx.status(f"Loading movie {trimmed}")
            try:
                item = client.get_video(trimmed, title_hint=title_hint)
                yield from self._emit_playable(ctx, client, item)
                return
            except api.FawesomeTVError as exc:
                ctx.error(str(exc))
                return

        # 4. Auto: probe if it's a TV show first
        try:
            series_item, seasons = client.get_show(trimmed)
            if seasons:
                yield from self._open_show(ctx, client, trimmed, series_item=series_item)
                return
        except Exception:
            pass

        # If not a show, fetch as video
        ctx.status(f"Loading title {trimmed}")
        try:
            item = client.get_video(trimmed, title_hint=title_hint)
            yield from self._emit_playable(ctx, client, item)
        except api.FawesomeTVError as exc:
            ctx.error(str(exc))

    def _open_show(
        self,
        ctx: FlowContext,
        client: api.FawesomeTVApi,
        show_id: str,
        series_item: api.TitleItem | None = None,
    ) -> Iterator[Ask]:
        ctx.status(f"Loading TV show {show_id}")
        try:
            series, seasons = client.get_show(show_id)
        except api.FawesomeTVError as exc:
            ctx.error(str(exc))
            return

        if not seasons:
            ctx.warn(f"No seasons found for show {series.title}")
            return

        if series_item and series_item.title:
            series = series_item

        # Single season: pick episode directly
        if len(seasons) == 1:
            yield from self._pick_episodes(ctx, client, series, seasons[0])
            return

        # Multiple seasons: pick season first
        while True:
            try:
                chosen_season = yield ctx.pick(
                    f"{series.title} · {len(seasons)} seasons",
                    [Choice(s.title, s, detail=s.description or f"Season {s.number}") for s in seasons],
                )
            except Back:
                return

            if chosen_season is None:
                return

            yield from self._pick_episodes(ctx, client, series, chosen_season)

    def _pick_episodes(
        self,
        ctx: FlowContext,
        client: api.FawesomeTVApi,
        series: api.TitleItem,
        season: api.SeasonItem,
    ) -> Iterator[Ask]:
        ctx.status(f"Loading episodes for {season.title}")
        try:
            episodes = client.get_season_episodes(season.feed_url)
        except api.FawesomeTVError as exc:
            ctx.error(str(exc))
            return

        if not episodes:
            ctx.warn(f"No episodes found for {season.title}")
            return

        while True:
            try:
                chosen = yield ctx.pick(
                    f"{series.title} · {season.title} · {len(episodes)} episodes",
                    [Choice(ep.label, ep, detail=ep.detail) for ep in episodes],
                    multi=True,
                    hint="space to tick, enter to confirm",
                )
            except Back:
                return

            if not chosen:
                return

            selected = list(chosen) if isinstance(chosen, (list, tuple)) else [chosen]
            if len(selected) > 1:
                ctx.batch(len(selected))

            for ep in selected:
                yield from self._emit_playable(ctx, client, ep, series=series)
            return

    # ------------------------------------------------------------- emissions

    def _emit_playable(
        self,
        ctx: FlowContext,
        client: api.FawesomeTVApi,
        item: api.TitleItem,
        series: api.TitleItem | None = None,
    ) -> Iterator[Ask]:
        if not item.video_url:
            ctx.error(f"No playback URL found for {item.title}")
            return

        playback = self._playback(client, item, series=series)
        yield ctx.emit(playback)

    def _emit_live(
        self,
        ctx: FlowContext,
        client: api.FawesomeTVApi,
        channel: api.LiveChannel,
    ) -> Iterator[Ask]:
        title = Title(
            id=channel.id,
            kind=TitleKind.PROGRAM,
            name=channel.title,
            service=self.ID,
        )
        clean_url = api.clean_live_macro_url(channel.stream_url)
        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=clean_url,
            headers={"User-Agent": api.USER_AGENT},
            proxy=self.ctx.proxy,
            is_live=True,
            drm=DrmInfo(clear=True),
            note=channel.detail,
        )

        # Attach logo/poster if present
        poster = channel.images.get("poster") or channel.images.get("thumbnail")
        if poster:
            from ...core.attachments import Attachment

            playback.attachments = [
                Attachment(
                    url=poster,
                    name="Channel Logo",
                    kind="artwork",
                    mime_type="image/jpeg" if poster.endswith((".jpg", ".jpeg")) else "image/png",
                )
            ]

        yield ctx.emit(playback)

    def _playback(
        self,
        client: api.FawesomeTVApi,
        item: api.TitleItem,
        series: api.TitleItem | None = None,
    ) -> Playback:
        if item.kind == "episode":
            series_name = series.title if series else (item.series_name or item.title)
            ep_year = item.year or (series.year if series else None)
            title = Title(
                id=item.id,
                kind=TitleKind.EPISODE,
                name=series_name,
                year=str(ep_year) if ep_year else None,
                season=item.season_number,
                episode=item.episode_number,
                episode_name=item.episode_title or item.title,
                service=self.ID,
            )
        else:
            title = Title(
                id=item.id,
                kind=TitleKind.MOVIE,
                name=item.title,
                year=str(item.year) if item.year else None,
                service=self.ID,
            )

        if item.duration:
            title.duration = item.duration

        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=item.video_url,
            headers={"User-Agent": api.USER_AGENT},
            proxy=self.ctx.proxy,
            is_live=False,
            note=item.detail,
        )

        # DRM setup
        if item.is_drm:
            playback.drm = DrmInfo(
                system="widevine",
                license_url=client.get_drm_license_url(),
                headers={"User-Agent": api.USER_AGENT},
            )
        else:
            playback.drm = DrmInfo(clear=True)

        # Subtitles
        playback.subtitle_references = client.extract_subtitles(item)

        # Chapters
        if self.fetch_chapters_enabled():
            chapters = client.parse_chapters(item)
            if chapters:
                playback.chapters = chapters

        # Attachments
        if self.fetch_attachments_enabled():
            attachments = client.extract_attachments(item)
            if attachments:
                playback.attachments = attachments

        return playback

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        """Acquire Widevine DRM license from Fawesome TV license proxy."""
        client = self._api or self._new_client()
        try:
            return client.widevine_license(drm.license_url, challenge)
        except api.FawesomeTVError as exc:
            raise CdmError(str(exc)) from exc


__all__ = ["FawesomeTV", "api"]
