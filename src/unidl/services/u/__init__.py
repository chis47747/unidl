"""U (formerly UKTV Play) - public UK catalogue and Widevine playback."""

from __future__ import annotations

from collections.abc import Iterator

from ...core.flow import Ask, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from . import api


class U(Service):
    ID = "u"
    NAME = "U"
    TAG = "UKTV"
    ALIASES = ("uktv", "uktvplay", "u&")
    TITLE_RE = r"^(?:https?://)?(?:www\.)?u\.co\.uk(?::\d+)?/shows/"
    GEOFENCE = ("GB",)
    DESCRIPTION = "U and U&Dave programmes. No sign-in; Widevine playback."
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True

    def auth_status(self) -> AuthStatus:
        return AuthStatus(logged_in=False, anonymous_ok=True, label="no sign-in needed")

    def client(self) -> api.UApi:
        return api.UApi(
            session=self.ctx.session(
                user_agent=api.CATALOGUE_USER_AGENT,
                cookies=False,
            )
        )

    # ---------------------------------------------------------------- browsing
    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                f"That does not look like a U link or show slug: {target}\n"
                "Expected https://u.co.uk/shows/<slug>/..."
            )
            return
        client = self.client()
        if parsed.video_id:
            try:
                ctx.status("Finding that episode")
                _show, episode = client.find_episode(parsed.slug, parsed.video_id)
            except api.UError as exc:
                ctx.error(str(exc))
                return
            yield from self._emit(ctx, client, episode)
            return
        yield from self._catalogue(ctx, client, parsed.slug)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        client = self.client()
        try:
            ctx.status(f"Searching U for {query}")
            hits = client.search(query)
        except api.UError as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"U found nothing for {query}")
            return
        chosen = yield ctx.pick(
            f"U  ·  {query}",
            [
                Choice(
                    hit.name,
                    hit,
                    detail=hit.synopsis[:90],
                    tags=tuple(
                        value
                        for value in (
                            hit.channel.upper() if hit.channel else "",
                            f"{hit.episode_count} episodes" if hit.episode_count else "",
                        )
                        if value
                    ),
                )
                for hit in hits
            ],
        )
        if chosen is not None:
            yield from self._catalogue(ctx, client, chosen.slug)

    def _catalogue(self, ctx: FlowContext, client: api.UApi, slug: str) -> Iterator[Ask]:
        try:
            ctx.status(f"Loading {slug}")
            show = client.show(slug)
        except api.UError as exc:
            ctx.error(str(exc))
            return
        if not show.seasons:
            ctx.warn(f"U returned no seasons for {show.name}")
            return

        selected_seasons: list[api.Season]
        if len(show.seasons) == 1:
            selected_seasons = [show.seasons[0]]
        else:
            selected = yield ctx.pick(
                f"{show.name}  ·  seasons",
                [Choice(entry.label, entry) for entry in show.seasons]
                + [Choice("All Seasons", "all")],
            )
            if selected is None:
                return
            selected_seasons = show.seasons if selected == "all" else [selected]

        episodes: list[api.Episode] = []
        try:
            for season_entry in selected_seasons:
                ctx.status(f"Loading {show.name}  ·  {season_entry.label}")
                loaded = client.season(season_entry)
                episodes.extend(loaded.episodes)
        except api.UError as exc:
            ctx.error(str(exc))
            return

        heading = (
            f"{show.name}  ·  All Seasons"
            if len(selected_seasons) > 1
            else f"{show.name}  ·  {selected_seasons[0].label}"
        )
        if not episodes:
            ctx.warn(f"{heading}: no episodes returned")
            return
        ctx.log(heading, "ok")
        picked = yield ctx.pick(
            heading,
            [
                Choice(item.label, item, detail=item.synopsis[:90])
                for item in episodes
            ],
            multi=True,
            hint="space to tick, enter to confirm",
        )
        selected_episodes = list(picked or [])
        if len(selected_episodes) > 1:
            ctx.batch(len(selected_episodes))
        for episode in selected_episodes:
            yield from self._emit(ctx, client, episode)

    # --------------------------------------------------------------- playback
    def _emit(self, ctx: FlowContext, client: api.UApi, item: api.Episode) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {item.label}")
            source = client.source(item.video_id)
        except api.UError as exc:
            ctx.error(f"{item.label}: {exc}")
            return
        ctx.log(f"{item.label}: {source.line()}")

        episodic = not item.feature and item.season is not None and item.number is not None
        title = Title(
            id=item.video_id,
            kind=TitleKind.EPISODE if episodic else TitleKind.MOVIE,
            name=item.brand,
            season=item.season if episodic else None,
            episode=item.number if episodic else None,
            episode_name=item.name if episodic else None,
            duration=float(item.duration) if item.duration else None,
            channel=item.channel or None,
            synopsis=item.synopsis or None,
            service=self.ID,
        )
        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest,
            proxy=self.ctx.proxy,
            note=source.line(),
        )
        playback.drm = (
            DrmInfo(system="widevine", license_url=source.license_url)
            if source.encrypted
            else DrmInfo(clear=True)
        )
        yield ctx.emit(playback)

    # -------------------------------------------------------------- licensing
    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        """Widevine, against U's own licence server.

        Every service makes its own licence request: core has no default for it, so
        that a service which needs one cannot end up relying on somebody else's.
        """
        return self._licence_client().widevine_license(drm.license_url, challenge, drm.headers)

    def _licence_client(self):
        """The client the licence goes through, reused across a run.

        A licence can be asked for long after the playback that produced it - a saved
        command re-run, a batch working through a list - so this builds one on demand
        rather than assuming the browsing client is still around.
        """
        client = getattr(self, "_licence_api", None)
        if client is None:
            client = self.client()
            self._licence_api = client
        return client



registry.register(U)

__all__ = ["U", "api"]
