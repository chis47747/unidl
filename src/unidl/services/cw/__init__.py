"""The CW - free with ads, US only, no sign-in.

"""

from __future__ import annotations

from collections.abc import Iterator

from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from . import api


class Cw(Service):
    ID = "cw"
    NAME = "The CW"
    TAG = "CWTV"
    ALIASES = ("cwtv", "thecw")
    TITLE_RE = r"cwtv\.com/(?:series|shows|movies|channels)"
    GEOFENCE = ("US",)
    DESCRIPTION = "Free with ads. Recent episodes only, plus 75 linear channels."
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine", "playready")
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True

    def auth_status(self) -> AuthStatus:
        """Nothing to sign in to. Not a token, not even an anonymous one."""
        return AuthStatus(logged_in=False, anonymous_ok=True, label="no sign-in needed")

    def client(self) -> api.CwApi:
        return api.CwApi(session=self.ctx.session(user_agent=api.USER_AGENT))

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                f"That does not look like a CW link: {target}\n"
                "Expected https://www.cwtv.com/shows/<slug>/, "
                "https://www.cwtv.com/movies/<slug>/, "
                "https://www.cwtv.com/channels/?channel=<slug>, "
                "or a link with ?play=<id>"
            )
            return
        client = self.client()
        if parsed.kind == "video":
            yield from self._one(ctx, client, parsed.play_id)
        elif parsed.kind == "channel":
            if parsed.slug:
                yield from self._channel(ctx, client, parsed.slug)
            else:
                yield from self.live(ctx)
        else:
            yield from self._catalogue(ctx, client, parsed.slug, hint=parsed.kind)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        client = self.client()
        try:
            ctx.status(f"Searching The CW for {query}")
            hits = client.search(query)
        except api.CwError as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"The CW found nothing for {query}")
            return
        chosen = yield ctx.pick(
            f"The CW  ·  {query}  ·  {len(hits)} result(s)",
            [Choice(hit.label, hit, detail=self._hit_detail(hit), tags=_hit_tags(hit))
             for hit in hits],
        )
        if chosen is None:
            return
        if chosen.kind == "episode":
            yield from self._one(ctx, client, chosen.guid)
        elif chosen.kind == "channel":
            yield from self._channel(ctx, client, chosen.slug)
        else:
            yield from self._catalogue(ctx, client, chosen.slug, hint=chosen.kind)

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client()
        try:
            ctx.status("Loading channels")
            channels = client.channels()
        except api.CwError as exc:
            ctx.error(str(exc))
            return
        if not channels:
            ctx.warn("The CW returned no channels")
            return
        chosen = yield ctx.table(
            f"Live TV  ·  {len(channels)} channels",
            ["Channel", "On now", "Genre", "Stream"],
            [(item.name, item.on_now, item.genre, item.line()) for item in channels],
            channels,
        )
        if chosen is not None:
            yield from self._emit_live(ctx, client, chosen)

    def _catalogue(
        self, ctx: FlowContext, client: api.CwApi, slug: str, hint: str = ""
    ) -> Iterator[Ask]:
        try:
            series, items = client.catalogue(slug, hint)
        except api.CwError as exc:
            ctx.error(str(exc))
            return

        films = [item for item in items if item.kind == "movie"]
        episodes = [item for item in items if item.kind != "movie"]
        if films and not episodes:
            ctx.log(f"{series}: {len(films)} film(s)", "ok")
            if len(films) == 1:
                yield from self._emit(ctx, client, films[0])
                return
            picked = yield ctx.pick(
                series,
                [Choice(item.label, item, detail=self._detail(item)) for item in films],
                multi=True,
                hint="space to tick, enter to confirm",
            )
            yield from self._emit_many(ctx, client, list(picked or []))
            return

        seasons = client.group(episodes)
        ctx.log(
            f"{series}: {len(episodes)} free episode(s) across {len(seasons)} season(s)",
            "ok",
        )
        ctx.log(
            "  The CW keeps a rolling window of recent episodes, so this is all "
            "there is - not a paging limit",
            "info",
        )
        if films:
            ctx.log(f"  and {len(films)} film(s) under the same slug", "info")
        choices = [Choice(entry.label, entry) for entry in seasons]
        choices += [
            Choice(item.label, item, detail=self._detail(item), tags=("film",))
            for item in films
        ]
        heading = f"{series}  ·  {len(seasons)} season(s)"
        if films:
            heading += f" and {len(films)} film(s)"
        chosen = choices[0].value if len(choices) == 1 else (yield ctx.pick(heading, choices))
        if chosen is None:
            return
        if isinstance(chosen, api.Item):
            yield from self._emit(ctx, client, chosen)
            return
        picked = yield ctx.pick(
            f"{series}  ·  {chosen.label}",
            [
                Choice(item.label, item, detail=self._detail(item), tags=_item_tags(item))
                for item in chosen.episodes
            ],
            multi=True,
            hint="space to tick, enter to confirm",
        )
        yield from self._emit_many(ctx, client, list(picked or []))

    @staticmethod
    def _detail(item: api.Item) -> str:
        return item.synopsis[:80]

    @staticmethod
    def _hit_detail(hit: api.SearchHit) -> str:
        """A search row's second line: what it is about, and how long it runs."""
        bits = [hit.synopsis or hit.genres, hit.duration]
        return "  ·  ".join(bit for bit in bits if bit)[:90]

    def _one(self, ctx: FlowContext, client: api.CwApi, guid: str) -> Iterator[Ask]:
        try:
            ctx.status("Reading that episode")
            item = client.video(guid)
        except api.CwError as exc:
            ctx.error(str(exc))
            return
        yield from self._emit(ctx, client, item)

    def _channel(self, ctx: FlowContext, client: api.CwApi, slug: str) -> Iterator[Ask]:
        """One named channel, for a link or a search hit that already says which."""
        try:
            ctx.status(f"Looking up {slug}")
            channel = client.channel(slug)
        except api.CwError as exc:
            ctx.error(str(exc))
            return
        yield from self._emit_live(ctx, client, channel)

    def _emit_many(
        self, ctx: FlowContext, client: api.CwApi, items: list[api.Item]
    ) -> Iterator[Ask]:
        if len(items) > 1:
            try:
                ctx.batch(len(items))
            except Back:
                return
        for item in items:
            yield from self._emit(ctx, client, item)

    def _emit(self, ctx: FlowContext, client: api.CwApi, item: api.Item) -> Iterator[Ask]:
        wanted = self.drm_system()
        try:
            ctx.status(f"Resolving {item.name}")
            source = client.source(item, system=wanted)
        except api.CwError as exc:
            ctx.error(f"{item.label}: {exc}")
            return
        ctx.log(f"{item.label}: {source.line()}")
        if item.encrypted and not source.encrypted:
            ctx.log("  the feed said DRM, Brightcove served it clear", "info")

        episode = item.kind == "episode"
        title = Title(
            id=item.guid or item.video_id,
            kind=TitleKind.EPISODE if episode else TitleKind.MOVIE,
            name=item.series or item.name,
            episode_name=item.name if episode else None,
            season=item.season if episode else None,
            episode=item.episode if episode else None,
            year=item.year or None,
            duration=float(item.duration) or source.duration or None,
            genre=item.genre or None,
            synopsis=item.synopsis or None,
            service=self.ID,
        )
        try:
            yield ctx.emit(self._playback(title, source))
        except Back:
            return

    def _emit_live(
        self, ctx: FlowContext, client: api.CwApi, channel: api.Channel
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {channel.name}")
            source = client.live_source(channel, system=self.drm_system())
        except api.CwError as exc:
            ctx.error(f"{channel.name}: {exc}")
            return
        ctx.log(f"{channel.name}: {source.line()}")
        title = Title(
            id=channel.slug,
            kind=TitleKind.CHANNEL,
            name=channel.name,
            channel=channel.name,
            genre=channel.genre or None,
            synopsis=channel.on_now or channel.synopsis or None,
            service=self.ID,
        )
        try:
            yield ctx.emit(self._playback(title, source, is_live=True))
        except Back:
            return

    def _playback(self, title: Title, source: api.Source, *, is_live: bool = False) -> Playback:
        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest,
            headers={"User-Agent": api.USER_AGENT, "Origin": api.WWW, "Referer": f"{api.WWW}/"},
            proxy=self.ctx.proxy,
            is_live=is_live,
            note=source.line(),
            chapters=list(source.chapters) if self.fetch_chapters_enabled() else [],
            attachments=(
                list(source.attachments) if self.fetch_attachments_enabled() else []
            ),
        )
        if source.encrypted:
            playback.drm = DrmInfo(system=source.system, license_url=source.license_url)
        else:
            playback.drm = DrmInfo(clear=True)
        return playback

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        return self._licence_client().widevine_license(drm.license_url, challenge, drm.headers)

    def get_license_soap(self, challenge: str, drm: DrmInfo) -> str:
        return self._licence_client().playready_license(drm.license_url, challenge, drm.headers)

    def _licence_client(self):
        client = getattr(self, "_licence_api", None)
        if client is None:
            client = self.client()
            self._licence_api = client
        return client


def _hit_tags(hit: api.SearchHit) -> tuple[str, ...]:
    kind = {"movie": "film", "episode": "episode", "channel": "live"}.get(
        hit.kind, "series"
    )
    return (kind,) + ((hit.rating,) if hit.rating else ())


def _item_tags(item: api.Item) -> tuple[str, ...]:
    return (item.leaves,) if item.leaves else ()


registry.register(Cw)

__all__ = ["Cw", "api"]
