"""ABC iView - Australia's free public broadcaster app.

Authorization:  none. JWT for DRM is minted anonymously from a public client id.
Geofence:       AU.
Playback:       DASH (HEVC CBCS preferred) or HLS; Widevine via KeyOS with
                ``x-keyos-authorization`` customdata when the stream is protected.
"""

from __future__ import annotations

from collections.abc import Iterator

from ...core.cdm import CdmError
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from . import api


class ABCiView(Service):
    ID = "abciview"
    NAME = "ABC iView"
    #: Not "ABC" - that tag is the US network (id ``abc``). Australia's product
    #: tag is ``iview``; the pair is fixed in core/brands.py so neither drifts.
    TAG = "iview"
    #: Never bare ``abc``: that is the US service id. Aliases match before tags.
    ALIASES = ("iview", "abc-iview", "abcau", "abc.net.au")
    TITLE_RE = r"iview\.abc\.net\.au/"
    GEOFENCE = ("AU",)
    DESCRIPTION = (
        "Australian free VOD, search and live TV. Anonymous JWT for KeyOS Widevine; "
        "AU IP required."
    )
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.AbcIviewApi | None = None

    # -------------------------------------------------------------------- auth
    def auth_status(self) -> AuthStatus:
        """Cheap and offline: the free catalogue needs no sign-in."""
        return AuthStatus(
            logged_in=False,
            anonymous_ok=True,
            label="no sign-in needed",
            detail="anonymous JWT for DRM",
        )

    def client(self, ctx: FlowContext | None = None) -> api.AbcIviewApi:
        if self._api is None:
            self._api = api.AbcIviewApi(
                session=self.ctx.session(user_agent=api.USER_AGENT)
            )
            if ctx is not None:
                try:
                    ctx.status("Checking ABC iView profile status")
                    self._api.anonymous_login()
                except api.AbcIviewError as exc:
                    # Profile status is optional; streams only need the JWT.
                    ctx.log(f"profile status unavailable: {exc}", "info")
        return self._api

    # ---------------------------------------------------------------- browsing
    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        try:
            parsed = api.parse_input(target)
        except api.AbcIviewError as exc:
            ctx.error(str(exc))
            return
        if parsed is None:
            ctx.error(
                f"That does not look like an ABC iView link or id: {target}\n"
                "Expected https://iview.abc.net.au/show/<slug>, "
                "https://iview.abc.net.au/video/<id>, or a bare show/video id."
            )
            return
        client = self.client(ctx)
        if parsed.kind == "video":
            yield from self._video(ctx, client, parsed.value)
            return
        yield from self._show(ctx, client, parsed.value)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        wanted = str(query or "").strip()
        if not wanted:
            return
        client = self.client(ctx)
        try:
            ctx.status(f"Searching ABC iView for {wanted}")
            hits = client.search(wanted)
        except api.AbcIviewError as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"ABC iView found nothing for {wanted!r}")
            return
        while True:
            try:
                chosen = yield ctx.pick(
                    f"ABC iView  ·  {wanted}  ·  {len(hits)} results",
                    [
                        Choice(
                            hit.label,
                            hit,
                            detail=(hit.description[:90] if hit.description else hit.type_label),
                            tags=(hit.kind,) if hit.kind and hit.kind != "unknown" else (),
                        )
                        for hit in hits
                    ],
                )
            except Back:
                return
            if chosen is None:
                return
            try:
                if chosen.kind == "show":
                    yield from self._show(ctx, client, chosen.id)
                elif chosen.kind == "video":
                    yield from self._video(ctx, client, chosen.id)
                else:
                    ctx.warn(f"Unsupported search result type: {chosen.type_label or chosen.kind}")
            except Back:
                continue

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client(ctx)
        try:
            ctx.status("Loading ABC iView live channels")
            channels = client.get_live_channels()
        except api.AbcIviewError as exc:
            ctx.error(str(exc))
            return
        if not channels:
            ctx.warn("ABC iView published no live channels")
            return
        while True:
            try:
                chosen = yield ctx.table(
                    f"ABC iView Live  ·  {len(channels)} channels",
                    ["Channel", "Collection", "Type"],
                    [
                        (
                            channel.title,
                            channel.collection or "-",
                            channel.type or "livestream",
                        )
                        for channel in channels
                    ],
                    channels,
                )
            except Back:
                return
            if chosen is None:
                return
            try:
                yield from self._emit_episode(ctx, client, chosen, is_live=True)
            except Back:
                continue

    # ---------------------------------------------------------------- listings
    def _show(self, ctx: FlowContext, client: api.AbcIviewApi, show_id: str) -> Iterator[Ask]:
        try:
            ctx.status(f"Loading show {show_id}")
            show = client.get_show(show_id)
        except api.AbcIviewError as exc:
            ctx.error(str(exc))
            return

        if show.unavailable_message:
            ctx.warn(f"{show.title}: {show.unavailable_message}")
            return

        if show.kind == "movie":
            if not show.video_id:
                ctx.error(f"Could not find a video id for {show.title}")
                return
            yield from self._video(ctx, client, show.video_id, movie=show)
            return

        if show.kind == "video" and show.video_id and not show.episodes:
            yield from self._video(ctx, client, show.video_id)
            return

        if not show.episodes:
            if show.video_id:
                yield from self._video(ctx, client, show.video_id)
                return
            ctx.warn(f"ABC iView returned no episodes for {show.title}")
            return

        yield from self._series(ctx, client, show)

    def _series(self, ctx: FlowContext, client: api.AbcIviewApi, show: api.Show) -> Iterator[Ask]:
        seasons: dict[int, list[api.Episode]] = {}
        for episode in show.episodes:
            seasons.setdefault(episode.season, []).append(episode)
        sorted_seasons = sorted(seasons.keys())
        ctx.log(f"{show.title}: {len(show.episodes)} episode(s) in {len(sorted_seasons)} season(s)", "ok")

        while True:
            try:
                if len(sorted_seasons) == 1:
                    season_num = sorted_seasons[0]
                else:
                    season_num = yield ctx.pick(
                        f"{show.title}  ·  seasons",
                        [
                            Choice(
                                f"Season {num}" if num else "Episodes",
                                num,
                                detail=f"{len(seasons[num])} episode(s)",
                            )
                            for num in sorted_seasons
                        ],
                    )
                    if season_num is None:
                        return
            except Back:
                return

            episodes = sorted(seasons[season_num], key=lambda item: (item.number, item.name))
            if not episodes:
                ctx.warn(f"Season {season_num} has no episodes")
                if len(sorted_seasons) == 1:
                    return
                continue

            heading = (
                f"{show.title}  ·  Season {season_num}"
                if season_num
                else f"{show.title}  ·  episodes"
            )
            try:
                yield from self._pick_episodes(ctx, client, heading, episodes)
            except Back:
                if len(sorted_seasons) == 1:
                    return
                continue

    def _pick_episodes(
        self,
        ctx: FlowContext,
        client: api.AbcIviewApi,
        heading: str,
        episodes: list[api.Episode],
    ) -> Iterator[Ask]:
        while True:
            try:
                picked = yield ctx.pick(
                    f"{heading}  ·  {len(episodes)} episodes",
                    [
                        Choice(
                            item.label,
                            item,
                            detail=item.description[:90] if item.description else "",
                        )
                        for item in episodes
                    ],
                    multi=True,
                    hint="space to tick, enter to confirm",
                )
            except Back:
                raise
            chosen = list(picked or [])
            if not chosen:
                return
            if len(chosen) > 1:
                ctx.batch(len(chosen))
            for item in chosen:
                yield from self._emit_episode(ctx, client, item)

    def _video(
        self,
        ctx: FlowContext,
        client: api.AbcIviewApi,
        video_id: str,
        *,
        movie: api.Show | None = None,
    ) -> Iterator[Ask]:
        if movie is not None:
            episode = api.Episode(
                id=video_id,
                title=movie.title,
                year=movie.year,
                name=movie.title,
                description=movie.synopsis,
                type="movie",
            )
            yield from self._emit_episode(
                ctx, client, episode, year=movie.year, force_movie=True
            )
            return
        try:
            ctx.status(f"Loading video {video_id}")
            episode = client.get_video(video_id)
        except api.AbcIviewError as exc:
            ctx.error(str(exc))
            return
        yield from self._emit_episode(ctx, client, episode)

    # --------------------------------------------------------------- playback
    def _emit_episode(
        self,
        ctx: FlowContext,
        client: api.AbcIviewApi,
        episode: api.Episode,
        *,
        is_live: bool | None = None,
        year: str = "",
        force_movie: bool = False,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {episode.label}")
            source = client.get_source(episode.id)
        except api.AbcIviewError as exc:
            ctx.error(f"{episode.label}: {exc}")
            return
        ctx.log(f"{episode.label}: {source.line()}")
        live = bool(is_live if is_live is not None else (episode.is_live or source.is_live))
        title = self._title(episode, year=year, force_movie=force_movie, is_live=live)
        yield ctx.emit(self._playback(title, source, is_live=live))

    def _title(
        self,
        episode: api.Episode,
        *,
        year: str = "",
        force_movie: bool = False,
        is_live: bool = False,
    ) -> Title:
        if is_live:
            return Title(
                id=episode.id,
                kind=TitleKind.CHANNEL,
                name=episode.title,
                channel=episode.title,
                episode_name=episode.name if episode.name != episode.title else None,
                synopsis=episode.description or None,
                service=self.ID,
            )
        if force_movie or (
            not episode.season and not episode.number and episode.type in {"movie", "feature", "single"}
        ):
            return Title(
                id=episode.id,
                kind=TitleKind.MOVIE,
                name=episode.title,
                year=year or episode.year or None,
                synopsis=episode.description or None,
                service=self.ID,
            )
        if episode.season or episode.number:
            return Title(
                id=episode.id,
                kind=TitleKind.EPISODE,
                name=episode.title,
                season=episode.season or None,
                episode=episode.number or None,
                episode_name=episode.name or None,
                synopsis=episode.description or None,
                service=self.ID,
            )
        return Title(
            id=episode.id,
            kind=TitleKind.MOVIE,
            name=episode.title,
            year=year or episode.year or None,
            episode_name=episode.name if episode.name != episode.title else None,
            synopsis=episode.description or None,
            service=self.ID,
        )

    def _playback(self, title: Title, source: api.Source, *, is_live: bool = False) -> Playback:
        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest,
            headers={"User-Agent": api.USER_AGENT},
            proxy=self.ctx.proxy,
            is_live=is_live,
            note=source.line(),
        )
        if source.protected:
            # PSSH is left for core to read from the MPD. KeyOS needs the
            # customdata on every licence POST as x-keyos-authorization.
            playback.drm = DrmInfo(
                system="widevine",
                license_url=api.LICENSE_URL,
                headers={"x-keyos-authorization": source.customdata},
                context={
                    "customdata": source.customdata,
                    "video_id": source.video_id,
                },
            )
        else:
            playback.drm = DrmInfo(clear=True)
        return playback

    # -------------------------------------------------------------- licensing
    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        """Widevine against KeyOS, authenticated with the stream's customdata."""
        context = dict(drm.context or {})
        customdata = str(
            context.get("customdata")
            or (drm.headers or {}).get("x-keyos-authorization")
            or ""
        )
        try:
            return self._licence_client().widevine_license(
                challenge,
                customdata=customdata,
                license_url=drm.license_url or api.LICENSE_URL,
                headers={
                    key: value
                    for key, value in (drm.headers or {}).items()
                    if key.lower() != "x-keyos-authorization"
                },
            )
        except api.AbcIviewError as exc:
            raise CdmError(str(exc)) from exc

    def _licence_client(self) -> api.AbcIviewApi:
        """Client reused across a run; built on demand for a late licence call."""
        if self._api is None:
            self._api = api.AbcIviewApi(
                session=self.ctx.session(user_agent=api.USER_AGENT)
            )
        return self._api


registry.register(ABCiView)

__all__ = ["ABCiView", "api"]
