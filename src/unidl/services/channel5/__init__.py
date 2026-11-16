"""Channel 5 / My5 - public catalogue, mTLS Cassie playback, Widevine."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from importlib.resources import as_file, files

from ...core.flow import SCOPE_ROOT, Ask, Back, Choice, FlowContext
from ...core.helpers import Helper, HelperKind
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from . import api, metadata

_CURL = Helper(
    key="curl",
    label="curl",
    kind=HelperKind.BINARY,
    candidates=("curl",),
    install_hint="install curl, or set helpers.curl in unidl.yaml",
)

# These are client protocol materials shipped by Channel 5's Android app, not
# account credentials. Keep the password out of argv by sending curl's config
# over stdin in _cassie().
_CASSIE_CERT = files(__package__).joinpath("cassie.p12")
_CASSIE_P12_PASSWORD = "w78t78bfkl@aweQGzE"


class Channel5(Service):
    ID = "channel5"
    NAME = "Channel 5"
    TAG = "MY5"
    ALIASES = ("my5", "five")
    TITLE_RE = r"(?:channel5\.com|my5\.tv)/(?:show|shows|watch|channels?|live)/"
    GEOFENCE = ("GB",)
    DESCRIPTION = "Channel 5 catalogue and live channels; Widevine via Cassie mTLS."
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    HELPERS = [_CURL]

    def auth_status(self) -> AuthStatus:
        return AuthStatus(logged_in=False, anonymous_ok=True, label="no account needed")

    def client(self, ctx: FlowContext | None = None) -> api.Channel5Api:
        session = self.ctx.session(user_agent=api.USER_AGENT)
        return api.Channel5Api(session=session, cassie=self._cassie(ctx))

    def _cassie(self, ctx: FlowContext | None = None):
        curl = self.ctx.helper("curl")

        def fetch(asset_id: str, is_live: bool) -> dict:
            runner = self.ctx.runner(log=ctx.log if ctx else None)
            with as_file(_CASSIE_CERT) as certificate:
                result = runner.run(
                    [curl, "-sS", "--config", "-", api.cassie_url(asset_id, is_live)],
                    stdin_text=api.curl_config(certificate, _CASSIE_P12_PASSWORD),
                    timeout=40,
                )
            return api.parse_cassie(result.stdout)

        return fetch

    # ---------------------------------------------------------------- browsing
    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        options = [
            Choice("VOD - open a URL or content ID", "url"),
            Choice("Live TV", "live"),
            Choice("Search", "search"),
            Choice("Settings", "settings"),
        ]
        page = ctx.pick("", options, scope=SCOPE_ROOT)
        while True:
            action = yield page
            page.cursor = next((i for i, option in enumerate(options) if option.value == action), 0)
            try:
                if action == "url":
                    yield from self._input(ctx, "Enter URL or content ID", self.open_url)
                elif action == "search":
                    yield from self._input(ctx, "Search", self.search)
                elif action == "live":
                    yield from self.live(ctx)
                elif action == "settings":
                    yield ctx.settings_request()
            except Back:
                continue
            except RuntimeError as exc:
                ctx.error(str(exc))

    def _input(
        self, ctx: FlowContext, label: str, visit: Callable[[FlowContext, str], Iterator[Ask]]
    ) -> Iterator[Ask]:
        page = ctx.text(label)
        while True:
            value = str((yield page) or "").strip()
            if not value:
                return
            page.default = value
            try:
                yield from visit(ctx, value)
            except Back:
                pass
            except RuntimeError as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                f"That does not look like a Channel 5 link or id: {target}\n"
                "Expected /show/<slug>, /watch/<C-id>, /channels/<name>, a show id, or a C-id."
            )
            return
        client = self.client(ctx)
        try:
            if parsed.kind == "watchable":
                yield from self._emit(ctx, client, client.watchable(parsed.value))
            elif parsed.kind == "live":
                yield from self._emit_live(ctx, client, client.channel(parsed.value))
            else:
                hit = client.resolve_show(parsed.value)
                yield from self._show(ctx, client, hit)
        except api.Channel5Error as exc:
            ctx.error(str(exc))

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        client = self.client(ctx)
        try:
            ctx.status(f"Searching Channel 5 for {query}")
            hits = client.search(query)
        except api.Channel5Error as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"Channel 5 found nothing for {query}")
            return
        page = ctx.pick(
            f"Channel 5  ·  {query}",
            [
                Choice(hit.title, hit, detail=hit.synopsis[:90], tags=((hit.genre,) if hit.genre else ()))
                for hit in hits
            ],
        )
        while True:
            chosen = yield page
            if chosen is None:
                return
            page.cursor = hits.index(chosen)
            try:
                yield from self._show(ctx, client, chosen)
            except Back:
                pass
            except RuntimeError as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client(ctx)
        try:
            ctx.status("Loading Channel 5 live channels")
            channels = client.channels()
        except api.Channel5Error as exc:
            ctx.error(str(exc))
            return
        if not channels:
            ctx.warn("Channel 5 returned no live channels")
            return
        page = ctx.table(
            f"Channel 5  ·  {len(channels)} live channels",
            ["Channel", "Now", "Next"],
            [(channel.name, channel.now, channel.next) for channel in channels],
            channels,
        )
        while True:
            chosen = yield page
            if chosen is None:
                return
            try:
                yield from self._emit_live(ctx, client, chosen)
            except Back:
                pass
            except RuntimeError as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def _show(self, ctx: FlowContext, client: api.Channel5Api, hit: api.SearchHit) -> Iterator[Ask]:
        try:
            ctx.status(f"Loading {hit.title}")
            show = client.show(hit.id)
        except api.Channel5Error as exc:
            ctx.error(str(exc))
            return
        if show.films and not show.seasons:
            if len(show.films) == 1:
                yield from self._emit(ctx, client, show.films[0])
            else:
                yield from self._items(ctx, client, show.title, show.films)
            return
        if not show.seasons:
            ctx.warn(f"Channel 5 returned no episodes for {show.title}")
            return
        if len(show.seasons) == 1:
            season = show.seasons[0]
            yield from self._items(ctx, client, f"{show.title}  ·  {season.label}", season.episodes)
            return
        page = ctx.pick(
            f"{show.title}  ·  seasons",
            [Choice(entry.label, entry) for entry in show.seasons],
        )
        while True:
            season = yield page
            if season is None:
                return
            page.cursor = show.seasons.index(season)
            try:
                yield from self._items(ctx, client, f"{show.title}  ·  {season.label}", season.episodes)
            except Back:
                pass
            except RuntimeError as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def _items(
        self, ctx: FlowContext, client: api.Channel5Api, title: str, items: list[api.Episode]
    ) -> Iterator[Ask]:
        page = ctx.pick(
            title,
            [Choice(item.label, item, detail=item.synopsis[:90]) for item in items],
            multi=True,
            hint="space to tick, enter to confirm",
        )
        while True:
            picked = list((yield page) or [])
            if picked:
                page.preselected = [items.index(item) for item in picked]
                page.cursor = page.preselected[0]
                try:
                    yield from self._emit_many(ctx, client, picked)
                except Back:
                    pass
                except RuntimeError as exc:
                    ctx.error(str(exc))
            if not ctx.interactive:
                return

    def _emit_many(self, ctx: FlowContext, client: api.Channel5Api, items: list[api.Episode]) -> Iterator[Ask]:
        if len(items) > 1:
            ctx.batch(len(items))
        for item in items:
            yield from self._emit(ctx, client, item)

    # --------------------------------------------------------------- playback
    def _emit(self, ctx: FlowContext, client: api.Channel5Api, item: api.Episode) -> Iterator[Ask]:
        try:
            yield ctx.emit(self._resolve_vod(ctx, client, item))
        except RuntimeError as exc:
            ctx.error(f"{item.label}: {exc}")

    def _resolve_vod(self, ctx: FlowContext, client: api.Channel5Api, item: api.Episode) -> Playback:
        ctx.status(f"Resolving {item.label}")
        source = client.source(item.id, fetch_chapters=self.fetch_chapters_enabled())
        title = Title(
            id=item.id,
            kind=TitleKind.MOVIE if item.film else TitleKind.EPISODE,
            name=item.show_title or item.title,
            year=item.year or None,
            season=None if item.film else item.season,
            episode=None if item.film else item.number,
            episode_name=None if item.film else item.title,
            duration=float(source.duration or item.duration) if (source.duration or item.duration) else None,
            genre=item.genre or None,
            synopsis=item.synopsis or None,
            service=self.ID,
        )
        return self._playback(title, source, artwork=item.artwork)

    def _emit_live(self, ctx: FlowContext, client: api.Channel5Api, channel: api.Channel) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {channel.name}")
            source = client.source(channel.id, is_live=True, fetch_chapters=False)
        except api.Channel5Error as exc:
            ctx.error(f"{channel.name}: {exc}")
            return
        title = Title(
            id=channel.id,
            kind=TitleKind.CHANNEL,
            name=channel.name,
            channel=channel.name,
            synopsis=channel.now or None,
            service=self.ID,
        )
        yield ctx.emit(self._playback(title, source, is_live=True, artwork=channel.artwork))

    def _playback(
        self, title: Title, source: api.Source, *, is_live: bool = False, artwork: dict | None = None
    ) -> Playback:
        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest,
            headers={
                "User-Agent": api.USER_AGENT,
                "Origin": api.WWW,
                "Referer": f"{api.WWW}/",
            },
            proxy=self.ctx.proxy,
            is_live=is_live,
            chapters=(list(source.chapters) if not is_live and self.fetch_chapters_enabled() else []),
            attachments=(metadata.attachments(artwork or {}, live=is_live) if self.fetch_attachments_enabled() else []),
            note=source.line(),
        )
        playback.drm = (
            DrmInfo(system="widevine", license_url=source.license_url) if source.encrypted else DrmInfo(clear=True)
        )
        return playback

    # -------------------------------------------------------------- licensing
    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        """Widevine, against Channel 5's own licence server.

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


registry.register(Channel5)

__all__ = ["Channel5", "api"]
