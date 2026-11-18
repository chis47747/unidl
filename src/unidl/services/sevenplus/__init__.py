"""7plus - Seven Network Australia catch-up, search and live TV."""

from __future__ import annotations

import time
from collections.abc import Iterator

from ...core.attachments import Attachment
from ...core.cdm import CdmError
from ...core.credentials import CredentialSlot
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from . import api

_LIVE_REGION = Setting(
    key="live_region",
    label="Live region",
    kind="choice",
    options=[Option("auto", "Automatic (IP)")] + [
        Option(key, region[0]) for key, region in api.LIVE_REGIONS.items()
    ],
    default="auto",
    help=(
        "Region for the live guide and playback only. State options select the named capital feed; "
        "Automatic keeps the detected/cached IP market. Does not change VOD, account location "
        "or Seven's location-verification requirements. No sign-in is needed when switching."
    ),
)


@registry.register
class SevenPlus(Service):
    ID = "sevenplus"
    NAME = "7plus"
    TAG = "7PLUS"
    ALIASES = ("7plus", "7+")
    TITLE_RE = r"(?:www\.)?7plus\.com\.au/"
    GEOFENCE = ("AU",)
    DESCRIPTION = (
        "Seven Network Australia: free catch-up, search and live channels. "
        "Gigya sign-in with cached auth2 tokens."
    )
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine", "playready")
    SETTINGS = [_LIVE_REGION]
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    CREDENTIALS = [CredentialSlot("default", "7plus account (email + password)")]

    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.SevenPlusApi | None = None

    def live_region(self) -> str:
        value = str(self.settings.get("live_region") or "auto").strip().lower()
        return value if value in api.LIVE_REGIONS else "auto"

    def auth_status(self) -> AuthStatus:
        raw = self.ctx.tokens.read(self.TOKEN_FILE) or {}
        state = api.Session.from_cache(raw)
        source = "cache"
        now = int(time.time())
        if state.access_fresh:
            hours = max(0, (state.token_expires - now) // 3600)
            return AuthStatus(True, f"signed in, {hours}h left", detail=source)
        if state.can_refresh:
            return AuthStatus(True, "session ready to refresh", detail=source)
        cred = self.ctx.credential("default")
        if cred.complete:
            return AuthStatus(False, "credentials ready", detail="will sign in on demand")
        return AuthStatus(False, "no 7plus account configured", detail="slot 'default'")

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        cred = self.ctx.credential("default")
        username = cred.username or ""
        while True:
            if not cred.username:
                username = str(
                    (yield ctx.text("7plus email", placeholder="name@example.com", default=username)) or ""
                ).strip()
            if not username:
                ctx.warn("no email entered")
                return
            try:
                password = cred.password or str((yield ctx.text("7plus password", password=True)) or "")
            except Back:
                if cred.username:
                    raise
                continue
            break
        if not password:
            ctx.warn("no password entered")
            return
        client = api.SevenPlusApi(
            session=self.ctx.session(user_agent=api.USER_AGENT),
            on_save=lambda state: self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache()),
            security_level=self.cdm_level(self.drm_system()),
            system=self.drm_system(),
            live_region=self.live_region(),
        )
        try:
            ctx.status("Signing in to 7plus")
            client.authenticate(username, password)
        except api.SevenPlusError as exc:
            ctx.error(str(exc))
            return
        self._api = client
        ctx.log("7plus session saved", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._api = None

    def client(self, ctx: FlowContext | None = None) -> api.SevenPlusApi:
        if self._api is not None:
            self._api.system = self.drm_system()
            self._api.security_level = self.cdm_level(self.drm_system())
            self._api.live_region = self.live_region()
            return self._api
        cached = self.ctx.tokens.read(self.TOKEN_FILE) or {}
        client = api.SevenPlusApi(
            session=self.ctx.session(user_agent=api.USER_AGENT),
            state=api.Session.from_cache(cached),
            on_save=lambda state: self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache()),
            security_level=self.cdm_level(self.drm_system()),
            system=self.drm_system(),
            live_region=self.live_region(),
        )
        cred = self.ctx.credential("default")
        if ctx:
            ctx.status("Opening 7plus session")
        try:
            client.ensure_session(cred.username or "", cred.password or "")
        except api.SevenPlusError:
            raise
        self._api = client
        return client

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                f"That does not look like a 7plus link or slug: {target}\n"
                "Expected https://7plus.com.au/<show>, ?episode-id=…, live-tv, or a show slug."
            )
            return
        try:
            client = self.client(ctx)
        except api.SevenPlusError as exc:
            ctx.error(str(exc))
            return
        if parsed.kind == "live":
            yield from self._live_pick(ctx, client, prefer=parsed.value or None)
            return
        yield from self._show(ctx, client, parsed.value, episode_id=parsed.episode_id)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status(f"Searching 7plus for {query}")
            hits = client.search(query)
        except api.SevenPlusError as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"7plus found nothing for {query}")
            return
        page = ctx.pick(
            f"7plus  ·  {query}",
            [Choice(hit.title, hit, detail=hit.url) for hit in hits],
        )
        while True:
            chosen = yield page
            if chosen is None:
                return
            page.cursor = hits.index(chosen)
            try:
                yield from self._show(ctx, client, chosen.slug)
            except Back:
                continue
            if not ctx.interactive:
                return

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
        except api.SevenPlusError as exc:
            ctx.error(str(exc))
            return
        yield from self._live_pick(ctx, client)

    def _live_pick(
        self,
        ctx: FlowContext,
        client: api.SevenPlusApi,
        prefer: str | None = None,
    ) -> Iterator[Ask]:
        page = None
        region = ""
        while True:
            current_region = self.live_region()
            if page is None or current_region != region:
                region = current_region
                client.live_region = region
                label = api.LIVE_REGIONS[region][0] if region in api.LIVE_REGIONS else "Automatic (IP)"
                try:
                    ctx.status(f"Loading 7plus live channels · {label}")
                    channels = client.channels()
                except api.SevenPlusError as exc:
                    ctx.error(str(exc))
                    return
                if not channels:
                    ctx.warn("7plus returned no live channels")
                    return
                if prefer:
                    match = next(
                        (
                            c
                            for c in channels
                            if prefer in {c.id, c.channel_name, c.name}
                            or prefer.lower() in {c.id.lower(), c.channel_name.lower(), c.name.lower()}
                        ),
                        None,
                    )
                    if match is not None:
                        yield from self._emit_live(ctx, client, match)
                        return
                    ctx.warn(f"channel {prefer} not in the guide; pick one")
                    prefer = None
                page = ctx.table(
                    f"7plus  ·  {label}  ·  {len(channels)} live channels",
                    ["Channel", "Now"],
                    [(channel.name, channel.current_show) for channel in channels],
                    channels,
                )
            chosen = yield page
            if chosen is None:
                return
            if self.live_region() != region:
                continue
            try:
                yield from self._emit_live(ctx, client, chosen)
            except Back:
                continue
            if not ctx.interactive:
                return

    def _show(
        self,
        ctx: FlowContext,
        client: api.SevenPlusApi,
        slug: str,
        *,
        episode_id: str = "",
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Loading {slug}")
            content = client.resolve(slug)
        except api.SevenPlusError as exc:
            ctx.error(str(exc))
            return
        if isinstance(content, api.Movie):
            yield from self._emit_movie(ctx, client, content)
            return
        series = content
        if episode_id:
            for season in series.seasons:
                for ep in season.episodes:
                    if episode_id in {ep.id, ep.catalogue_number}:
                        yield from self._emit_episode(ctx, client, series, ep)
                        return
            ctx.warn(f"episode-id {episode_id} not in catalogue; pick a season")

        if not series.seasons:
            ctx.warn(f"7plus returned no episodes for {series.title}")
            return
        if len(series.seasons) == 1:
            yield from self._episodes(ctx, client, series, series.seasons[0])
            return
        page = ctx.pick(
            f"{series.title}  ·  seasons",
            [Choice(entry.label, entry) for entry in series.seasons],
        )
        while True:
            season = yield page
            if season is None:
                return
            page.cursor = series.seasons.index(season)
            try:
                yield from self._episodes(ctx, client, series, season)
            except Back:
                continue
            if not ctx.interactive:
                return

    def _episodes(
        self, ctx: FlowContext, client: api.SevenPlusApi, series: api.Series, season: api.Season
    ) -> Iterator[Ask]:
        if not season.episodes:
            ctx.warn(f"no episodes in {season.label}")
            return
        page = ctx.pick(
            f"{series.title}  ·  {season.label}",
            [Choice(item.label, item) for item in season.episodes],
            multi=True,
            hint="space to tick, enter to confirm",
        )
        while True:
            items = list((yield page) or [])
            if not items:
                return
            page.cursor = season.episodes.index(items[0])
            try:
                if len(items) > 1:
                    ctx.batch(len(items))
                for item in items:
                    yield from self._emit_episode(ctx, client, series, item)
            except Back:
                continue
            if not ctx.interactive:
                return

    def _emit_movie(
        self, ctx: FlowContext, client: api.SevenPlusApi, movie: api.Movie
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {movie.title}")
            source = client.source(movie.id)
        except api.SevenPlusError as exc:
            ctx.error(str(exc))
            return
        title = Title(
            id=movie.id,
            kind=TitleKind.MOVIE,
            name=movie.title,
            year=movie.year or None,
            service=self.ID,
        )
        yield ctx.emit(self._playback(title, source, image_url=movie.image_url))

    def _emit_episode(
        self,
        ctx: FlowContext,
        client: api.SevenPlusApi,
        series: api.Series,
        episode: api.Episode,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {episode.label}")
            source = client.source(episode.id)
        except api.SevenPlusError as exc:
            ctx.error(f"{episode.label}: {exc}")
            return
        title = Title(
            id=episode.id,
            kind=TitleKind.EPISODE,
            name=series.title,
            season=episode.season or None,
            episode=episode.number or None,
            episode_name=episode.name or episode.title or None,
            year=episode.year or None,
            service=self.ID,
        )
        yield ctx.emit(self._playback(title, source, image_url=episode.image_url))

    def _emit_live(
        self, ctx: FlowContext, client: api.SevenPlusApi, channel: api.Channel
    ) -> Iterator[Ask]:
        client.live_region = self.live_region()
        try:
            ctx.status(f"Resolving {channel.name}")
            source = client.live_source(channel)
        except api.SevenPlusError as exc:
            ctx.error(f"{channel.name}: {exc}")
            return
        title = Title(
            id=channel.id or channel.channel_name,
            kind=TitleKind.CHANNEL,
            name=channel.name,
            channel=channel.name,
            episode_name=channel.current_show or None,
            service=self.ID,
        )
        yield ctx.emit(self._playback(title, source, is_live=True, image_url=channel.logo_url))

    def _playback(
        self, title: Title, source: api.Source, *, is_live: bool = False, image_url: str = ""
    ) -> Playback:
        is_live = is_live or source.is_live
        if not is_live and source.duration_ms > 0:
            title.duration = source.duration_ms / 1000
        attachments: list[Attachment] = []
        if self.fetch_attachments_enabled():
            attachments.extend(source.attachments)
            if image_url and all(item.url != image_url for item in attachments):
                if is_live:
                    attachments.insert(0, Attachment(image_url, "Channel logo", "image"))
                elif not attachments:
                    attachments.append(Attachment(image_url, "Catalogue artwork", "artwork"))
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
            note=source.line(),
            chapters=list(source.chapters) if not is_live and self.fetch_chapters_enabled() else [],
            attachments=attachments,
        )
        if source.encrypted:
            headers: dict[str, str] = dict(source.drm_headers)
            if source.auth_token:
                header_names = {key.lower() for key in headers}
                if source.drm_provider == "brightcove":
                    if "bcov-auth" not in header_names:
                        headers["BCOV-Auth"] = source.auth_token
                elif "authorization" not in header_names and "bcov-auth" not in header_names:
                    headers["Authorization"] = f"Bearer {source.auth_token}"
            playback.drm = DrmInfo(
                system=source.system,
                license_url=source.license_url,
                pssh=source.pssh or None,
                wrm_header=source.wrm_headers[0] if source.wrm_headers else None,
                headers=headers,
                context={
                    key: value
                    for key, value in {
                        "auth_token": source.auth_token,
                        "drm_provider": source.drm_provider,
                    }.items()
                    if value
                },
            )
            if source.wrm_headers:
                playback.drm.context["wrm_headers"] = list(source.wrm_headers)
        else:
            playback.drm = DrmInfo(clear=True)
        return playback

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        if drm.system == "playready":
            raise CdmError("7plus PlayReady requires its SOAP licence transport")
        client = self._api
        if client is None:
            client = api.SevenPlusApi(session=self.ctx.session(user_agent=api.USER_AGENT))
            self._api = client
        auth_token = str((drm.context or {}).get("auth_token") or "")
        drm_provider = str((drm.context or {}).get("drm_provider") or "")
        try:
            return client.widevine_license(
                drm.license_url or "",
                challenge,
                auth_token=auth_token,
                drm_provider=drm_provider,
                headers=dict(drm.headers or {}),
            )
        except api.SevenPlusError as exc:
            raise CdmError(str(exc)) from exc

    def get_license_soap(self, challenge: str, drm: DrmInfo) -> str:
        if drm.system != "playready":
            raise CdmError("7plus SOAP licence transport requires PlayReady")
        client = self._api
        if client is None:
            client = api.SevenPlusApi(session=self.ctx.session(user_agent=api.USER_AGENT), system="playready")
            self._api = client
        try:
            return client.playready_license(
                drm.license_url or "",
                challenge,
                auth_token=str((drm.context or {}).get("auth_token") or ""),
                drm_provider=str((drm.context or {}).get("drm_provider") or ""),
                headers=dict(drm.headers or {}),
            )
        except api.SevenPlusError as exc:
            raise CdmError(str(exc)) from exc


__all__ = ["SevenPlus", "api"]
