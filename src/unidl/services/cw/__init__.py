"""The CW - free with ads, US only, no sign-in.

"""

from __future__ import annotations

from collections.abc import Iterator

from ...core.flow import Ask, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from . import api


class Cw(Service):
    ID = "cw"
    NAME = "The CW"
    #: The tag unshackle uses, so a name from either tool resolves here.
    TAG = "CWTV"
    ALIASES = ("cwtv", "thecw")
    #: ``/channels`` is here as well as the three catalogue paths: a linear channel
    #: is addressed as ``/channels/?channel=<slug>``, which is what The CW's own
    #: search results hand out, so a pasted one has to route here too.
    TITLE_RE = r"cwtv\.com/(?:series|shows|movies|channels)"
    GEOFENCE = ("US",)
    DESCRIPTION = "Free with ads. Recent episodes only, plus 75 linear channels."
    USES = Capabilities()
    #: Brightcove signs the same DASH manifest for both, so the choice is real and
    #: it changes which licence server is called.
    DRM_SYSTEMS = ("widevine", "playready")
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True

    # -------------------------------------------------------------------- auth
    def auth_status(self) -> AuthStatus:
        """Nothing to sign in to. Not a token, not even an anonymous one."""
        return AuthStatus(logged_in=False, anonymous_ok=True, label="no sign-in needed")

    def client(self) -> api.CwApi:
        return api.CwApi(session=self.ctx.session(user_agent=api.USER_AGENT))

    # ---------------------------------------------------------------- browsing
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
            # Bare /channels/ names no channel, so it means the list. Sending that
            # to the picker is the answer the link asks for; an error would not be.
            if parsed.slug:
                yield from self._channel(ctx, client, parsed.slug)
            else:
                yield from self.live(ctx)
        else:
            # The URL's own word for what it is, which decides only where the feed
            # is silent - see api.catalogue.
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
        # Four kinds come back and they go four different ways. An episode hit is
        # already a guid, so it plays without a listing in between; a channel hit
        # is one of the linear channels; the two slug kinds are a catalogue to
        # browse.
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

    # ---------------------------------------------------------------- listings
    def _catalogue(
        self, ctx: FlowContext, client: api.CwApi, slug: str, hint: str = ""
    ) -> Iterator[Ask]:
        """A slug, which may turn out to be a film or a series.

        The feed does not say which before it is asked, so this decides from what
        came back rather than from the URL - a ``/movies/`` link and a ``/shows/``
        link reach the same endpoint, and The CW is not always consistent about
        which one it hands out. ``hint`` is the URL's word for it, and only breaks
        a tie the feed left open.
        """
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
            # A slug carrying both is rare, and filtering the films out of the
            # season grouping used to drop them without a word - a listing that is
            # quietly short is worse than one that is untidy.
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
            ctx.batch(len(items))
        for item in items:
            yield from self._emit(ctx, client, item)

    # ---------------------------------------------------------------- playback
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
            # Worth saying: the feed and Brightcove disagree about this more often
            # than not, and a clear stream where DRM was expected is good news that
            # would otherwise look like a missed licence step.
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
            # The feed's runtime first, Brightcove's second. Two sources because
            # they are two APIs and either can be silent; Brightcove's is the one
            # the legacy script used, in milliseconds.
            duration=float(item.duration) or source.duration or None,
            genre=item.genre or None,
            synopsis=item.synopsis or None,
            service=self.ID,
        )
        yield ctx.emit(self._playback(title, source))

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
        yield ctx.emit(self._playback(title, source, is_live=True))

    def _playback(self, title: Title, source: api.Source, *, is_live: bool = False) -> Playback:
        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest,
            headers={"User-Agent": api.USER_AGENT, "Origin": api.WWW, "Referer": f"{api.WWW}/"},
            proxy=self.ctx.proxy,
            is_live=is_live,
            note=source.line(),
        )
        if source.encrypted:
            playback.drm = DrmInfo(system=source.system, license_url=source.license_url)
        else:
            playback.drm = DrmInfo(clear=True)
        return playback

    # -------------------------------------------------------------- licensing
    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        """Widevine, against The CW's own licence server.

        Every service makes its own licence request: core has no default for it, so
        that a service which needs one cannot end up relying on somebody else's.
        """
        return self._licence_client().widevine_license(drm.license_url, challenge, drm.headers)

    def get_license_soap(self, challenge: str, drm: DrmInfo) -> str:
        """PlayReady, against the same server, which speaks SOAP."""
        return self._licence_client().playready_license(drm.license_url, challenge, drm.headers)

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


# ---------------------------------------------------------------------- labels


def _hit_tags(hit: api.SearchHit) -> tuple[str, ...]:
    """What kind of result this is, and its certificate.

    The kind has to be on the row: one search answers with shows, films, single
    episodes and linear channels, and picking a channel when a series was wanted
    is a different screen, not a slower one.
    """
    kind = {"movie": "film", "episode": "episode", "channel": "live"}.get(
        hit.kind, "series"
    )
    return (kind,) + ((hit.rating,) if hit.rating else ())


def _item_tags(item: api.Item) -> tuple[str, ...]:
    """When this leaves the free window, if the feed said."""
    return (item.leaves,) if item.leaves else ()


registry.register(Cw)

__all__ = ["Cw", "api"]
