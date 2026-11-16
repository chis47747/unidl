"""Channel 4 - TV-app authentication, catalogue, VOD and live Widevine."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

from ...core.cdm import CdmError
from ...core.credentials import CredentialSlot
from ...core.flow import SCOPE_ROOT, Ask, Back, Choice, FlowContext, Pick
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.titles import Title, TitleKind
from . import api, metadata
from .navigation import FLOW_ERRORS, browse_input, browse_page


def _normalise_selection(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


class Channel4(Service):
    ID = "channel4"
    NAME = "Channel 4"
    TAG = "ALL4"
    ALIASES = ("all4", "c4")
    TITLE_RE = r"(?:www\.)?channel4\.com/(?:programmes|now)/"
    GEOFENCE = ("GB",)
    DESCRIPTION = "Channel 4 streaming and live channels, including start-over."
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    CREDENTIALS = [CredentialSlot("default", "Channel 4 account")]

    TOKEN_FILE = "channel4_user_config.json"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.Channel4Api | None = None

    # ------------------------------------------------------------------- auth
    def auth_status(self) -> AuthStatus:
        state = self.ctx.tokens.read(self.TOKEN_FILE) or {}
        state = state if isinstance(state, dict) else {}
        tokens = state.get("tokens") if isinstance(state.get("tokens"), dict) else {}
        expiry = _int(state.get("tokens_expiry"))
        access = tokens.get("accessToken") or tokens.get("access_token")
        refresh = tokens.get("refreshToken") or tokens.get("refresh_token")
        source = "cache"
        if access and expiry > int(time.time()) + 60:
            hours = max(0, (expiry - int(time.time())) // 3600)
            return AuthStatus(True, f"TV session, {hours}h left", detail=source)
        if refresh:
            return AuthStatus(True, "TV session ready to refresh", detail=source)
        cred = self.ctx.credential("default")
        if cred.complete:
            return AuthStatus(False, "credentials ready", detail="will sign in on demand")
        return AuthStatus(False, "no Channel 4 account configured")

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        cred = self.ctx.credential("default")
        email_page = ctx.text("Channel 4 email", placeholder="name@example.com")
        while True:
            username = cred.username or str((yield email_page) or "").strip()
            if not username:
                ctx.warn("no email entered")
                return
            email_page.default = username
            try:
                password = cred.password or str((yield ctx.text("Channel 4 password", password=True)) or "")
            except Back:
                if cred.username or not ctx.interactive:
                    raise
                continue
            break
        if not password:
            ctx.warn("no password entered")
            return
        client = api.Channel4Api(session=self.ctx.session(user_agent=api.USER_AGENT))
        try:
            ctx.status("Signing in to Channel 4")
            state = client.authenticate(username, password)
        except api.Channel4Error as exc:
            ctx.error(str(exc))
            return
        self.ctx.tokens.write(self.TOKEN_FILE, state)
        self._api = client
        ctx.log("Channel 4 TV session saved", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._api = None

    def client(self, ctx: FlowContext | None = None) -> api.Channel4Api:
        cred = self.ctx.credential("default")
        if self._api is not None:
            previous = self._api.state
            fresh = self._api.authenticate(cred.username, cred.password)
            if fresh is not previous:
                self.ctx.tokens.write(self.TOKEN_FILE, fresh)
                if ctx:
                    ctx.log("Channel 4 session refreshed", "ok")
            return self._api
        state = self.ctx.tokens.read(self.TOKEN_FILE) or {}
        state = state if isinstance(state, dict) else {}
        client = api.Channel4Api(
            session=self.ctx.session(user_agent=api.USER_AGENT),
            state=state,
        )
        try:
            if ctx:
                ctx.status("Opening Channel 4 session")
            fresh = client.authenticate(cred.username, cred.password)
        except api.Channel4Error:
            raise
        if fresh is not state:
            self.ctx.tokens.write(self.TOKEN_FILE, fresh)
            if ctx:
                ctx.log("Channel 4 session refreshed", "ok")
        self._api = client
        return client

    # ---------------------------------------------------------------- browsing
    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        page = ctx.pick("", [], scope=SCOPE_ROOT)
        action = None
        while True:
            status = self._auth_status_quietly()
            options = []
            if not status.usable:
                options.append(Choice("Sign in", "login", detail=status.label))
            options.extend([
                Choice("VOD - open a URL or content ID", "url"),
                Choice("Live TV", "live"),
                Choice("Search", "search"),
                Choice("Settings", "settings"),
            ])
            if status.usable:
                options.append(Choice("Sign in again" if status.logged_in else "Sign in", "login", detail=status.label))
            if status.logged_in or status.logout_available:
                options.append(Choice("Sign out", "logout", detail=status.label))
            if self.saved_login() is not None:
                options.append(Choice("Delete saved credentials", "forget"))
            page.choices = options
            page.cursor = next((i for i, option in enumerate(options) if option.value == action), 0)
            action = yield page
            if action is None:
                return
            try:
                if action == "url":
                    yield from browse_input(ctx, "Enter URL or content ID", self._open_target)
                elif action == "search":
                    yield from browse_input(ctx, "Search", self._search_results)
                elif action == "live":
                    yield from self.live(ctx)
                elif action == "settings":
                    yield ctx.settings_request()
                elif action == "login":
                    yield from self.begin_login(ctx)
                elif action == "logout":
                    self.logout()
                    ctx.log("Channel 4 signed out", "ok")
                elif action == "forget":
                    yield from self.forget_login(ctx)
            except Back:
                pass
            except FLOW_ERRORS as exc:
                ctx.error(str(exc))
            if not ctx.interactive:
                return

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        yield from browse_input(ctx, "Enter URL or content ID", self._open_target, target)

    def _open_target(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                "That does not look like a Channel 4 link or id. "
                "Expected /programmes/<slug>, /on-demand/<id>, /now/<channel>, "
                "or a programme slug."
            )
            return
        try:
            client = self.client(ctx)
            if parsed.kind == "live":
                channel = next(
                    (entry for entry in client.channels() if entry.id == parsed.value),
                    None,
                )
            else:
                channel = None
        except api.Channel4Error as exc:
            ctx.error(str(exc))
            return
        if parsed.kind == "live":
            if channel is None:
                ctx.error("Channel 4 returned no matching live channel")
                return
            yield from self._live_channel(ctx, client, channel)
            return
        try:
            show = client.show(parsed.value, parsed.episode_id)
        except api.Channel4Error as exc:
            ctx.error(str(exc))
            return
        yield from self._show(ctx, client, show)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        yield from browse_input(ctx, "Search", self._search_results, query)

    def _search_results(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status("Searching Channel 4")
            hits = client.search(query)
        except api.Channel4Error as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn("Channel 4 found no search results")
            return
        page = ctx.pick(
            "Channel 4 search results",
            [
                Choice(
                    hit.title,
                    hit,
                    detail=hit.synopsis[:90],
                    tags=(hit.label,) if hit.label else (),
                )
                for hit in hits
            ],
        )

        def visit(chosen: api.SearchHit) -> Iterator[Ask]:
            yield from self._show(ctx, client, client.show(chosen.slug))

        yield from browse_page(ctx, page, visit)

    def _show(self, ctx: FlowContext, client: api.Channel4Api, show: api.Show) -> Iterator[Ask]:
        if show.films:
            if len(show.films) == 1:
                yield from self._emit(ctx, client, show.films[0])
            else:
                yield from self._items(ctx, client, show.title, show.films)
            return
        if not show.seasons:
            ctx.warn(f"Channel 4 returned no episodes for {show.title}")
            return
        if len(show.seasons) == 1:
            season = show.seasons[0]
            yield from self._items(ctx, client, f"{show.title}  ·  {season.label}", season.episodes)
            return
        page = ctx.pick(
            f"{show.title}  ·  series",
            [Choice(entry.label, entry) for entry in show.seasons],
        )
        episode_pages = {}

        def visit(season: api.Season) -> Iterator[Ask]:
            if season.number not in episode_pages:
                episode_pages[season.number] = self._item_picker(ctx, f"{show.title}  ·  {season.label}", season.episodes)
            yield from self._items_page(ctx, client, episode_pages[season.number])

        yield from browse_page(ctx, page, visit)

    def _item_picker(self, ctx: FlowContext, title: str, items: list[api.Episode]) -> Pick:
        return ctx.pick(
            title,
            [Choice(item.label, item, detail=item.synopsis[:90]) for item in items],
            multi=True,
            hint="space to tick, enter to confirm",
        )

    def _items(self, ctx: FlowContext, client: api.Channel4Api, title: str, items: list[api.Episode]) -> Iterator[Ask]:
        yield from self._items_page(ctx, client, self._item_picker(ctx, title, items))

    def _items_page(self, ctx: FlowContext, client: api.Channel4Api, page: Pick) -> Iterator[Ask]:
        yield from browse_page(ctx, page, lambda picked: self._emit_many(ctx, client, _normalise_selection(picked)))

    def _emit_many(
        self,
        ctx: FlowContext,
        client: api.Channel4Api,
        items: list[api.Episode],
    ) -> Iterator[Ask]:
        if len(items) > 1:
            ctx.batch(len(items))
        for item in items:
            yield from self._emit(ctx, client, item)

    # ------------------------------------------------------------------- live
    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status("Loading Channel 4 live channels")
            channels = client.channels()
        except api.Channel4Error as exc:
            ctx.error(str(exc))
            return
        page = ctx.table(
            "Channel 4  ·  live",
            ["Channel", "Now", "Next", "Restart"],
            [
                (channel.name, channel.now, channel.next, "yes" if channel.restart else "")
                for channel in channels
            ],
            channels,
        )
        yield from browse_page(ctx, page, lambda channel: self._live_channel(ctx, client, channel))

    def _live_channel(
        self,
        ctx: FlowContext,
        client: api.Channel4Api,
        channel: api.Channel,
    ) -> Iterator[Ask]:
        if channel.restart:
            page = ctx.pick(
                f"{channel.name}  ·  {channel.now or 'Live'}",
                [
                    Choice("Watch live", "live"),
                    Choice("Restart current programme", "restart"),
                ],
            )
            yield from browse_page(ctx, page, lambda mode: self._emit_live(ctx, client, channel, restart=mode == "restart"))
        else:
            yield from self._emit_live(ctx, client, channel)

    def _emit_live(
        self, ctx: FlowContext, client: api.Channel4Api, channel: api.Channel, *, restart: bool = False,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {channel.name}{' restart' if restart else ''}")
            source = client.live_source(channel, restart=restart)
        except api.Channel4Error as exc:
            ctx.error(str(exc))
            return
        title = Title(
            id=f"{channel.id}-restart" if restart else channel.id,
            kind=TitleKind.EPISODE if restart else TitleKind.CHANNEL,
            name=channel.name,
            channel=None if restart else channel.name,
            episode_name=channel.now or None,
            synopsis=channel.synopsis or None,
            service=self.ID,
        )
        yield ctx.emit(self._playback(title, source, is_live=not restart, artwork=channel.artwork))

    # --------------------------------------------------------------- playback
    def _emit(self, ctx: FlowContext, client: api.Channel4Api, item: api.Episode) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {item.label}")
            source = client.source(item)
        except api.Channel4Error as exc:
            ctx.error(f"{item.label}: {exc}")
            return
        title = Title(
            id=item.id,
            kind=TitleKind.MOVIE if item.film else TitleKind.EPISODE,
            name=item.title if item.film else item.show_title,
            year=item.year or None,
            season=None if item.film else item.season,
            episode=None if item.film else item.number,
            episode_name=None if item.film else item.title,
            duration=item.duration if item.duration is not None else source.duration,
            synopsis=item.synopsis or None,
            service=self.ID,
        )
        yield ctx.emit(self._playback(title, source, artwork=item.artwork))

    def _playback(
        self, title: Title, source: api.Source, *, is_live: bool = False, artwork: dict | None = None,
    ) -> Playback:
        request_id = os.path.basename(source.manifest).split("_", 1)[0] if source.video_type == "ondemand" else ""
        playback = Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.manifest,
            headers={"User-Agent": api.USER_AGENT, "Origin": api.WWW, "Referer": f"{api.WWW}/"},
            proxy=self.ctx.proxy,
            is_live=is_live,
            chapters=(
                list(source.chapters)
                if self.fetch_chapters_enabled() and source.video_type == "ondemand" and not is_live
                else []
            ),
            attachments=metadata.attachments(artwork or {}, source.artwork) if self.fetch_attachments_enabled() else [],
            note=source.line(),
        )
        playback.drm = DrmInfo(
            system="widevine",
            license_url=api.LICENSE,
            service_certificate=source.service_certificate,
            context={
                "token": source.license_token,
                "video_type": source.video_type,
                "video_url": source.video_url,
                "request_id": request_id,
            },
        )
        return playback

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        client = self._api
        if client is None:
            raise CdmError("Channel 4 session is not open")
        try:
            return client.license(
                challenge,
                token=str(drm.context.get("token") or ""),
                video_type=str(drm.context.get("video_type") or "ondemand"),
                video_url=str(drm.context.get("video_url") or ""),
                request_id=str(drm.context.get("request_id") or ""),
            )
        except api.Channel4Error as exc:
            raise CdmError(str(exc)) from exc


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


registry.register(Channel4)

__all__ = ["Channel4", "api"]
