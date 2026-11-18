"""9Now - Nine device-code auth, catalogue and service-local Widevine/PlayReady."""

from __future__ import annotations

from collections.abc import Iterator

from ...core.cdm import CdmError
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from . import api

_REGION = Setting(
    key="region",
    label="Live region",
    kind="choice",
    options=[
        Option("nsw", "NSW"),
        Option("vic", "VIC"),
        Option("qld", "QLD"),
        Option("sa", "SA"),
        Option("wa", "WA"),
        Option("tas", "TAS"),
        Option("act", "ACT"),
        Option("nt", "NT"),
    ],
    default="nsw",
    help="Australian state/territory used for the live channel list.",
)


@registry.register
class NineNow(Service):
    ID = "ninenow"
    NAME = "9Now"
    TAG = "9NOW"
    ALIASES = ("9now", "nine")
    TITLE_RE = r"(?:www\.)?9now\.com\.au/"
    GEOFENCE = ("AU",)
    DESCRIPTION = (
        "9Now (Nine Australia): searchable shows, movies and clips over tv-api, "
        "app live streams, and Brightcove Widevine/PlayReady. Optional device-code sign-in."
    )
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine", "playready")
    SETTINGS = [_REGION]
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True

    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.NineNowApi | None = None

    def region(self) -> str:
        return api._normalize_region(str(self.settings.get("region") or "nsw"))

    def auth_status(self) -> AuthStatus:
        raw = self.ctx.tokens.read(self.TOKEN_FILE) or {}
        state = api.Session.from_cache(raw)
        if not state.signed_in:
            return AuthStatus(
                logged_in=False,
                anonymous_ok=True,
                label="no token · public VOD/live works anonymously",
                detail="Sign in for SSO-gated streams",
            )
        detail = "token cache"
        if state.is_fresh():
            return AuthStatus(True, "signed in", detail=detail, anonymous_ok=True)
        if state.refresh_token:
            return AuthStatus(True, "signed in · refresh available", detail=detail, anonymous_ok=True)
        return AuthStatus(
            logged_in=False,
            anonymous_ok=True,
            label="token expired",
            detail="open Sign in to activate again",
        )

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client()
        try:
            if client.state.is_fresh():
                self._save_state(client.state)
                self._api = client
                ctx.log("already signed in", "ok")
                return
            if client.state.refresh_token and client.refresh():
                self._save_state(client.state)
                self._api = client
                ctx.log("session refreshed", "ok")
                return
            ctx.status("Requesting a 9Now activation code")
            challenge = client.start_device_code()
        except api.NineNowError as exc:
            ctx.error(str(exc))
            return

        def poll() -> api.Session | None:
            return client.poll_device_code(challenge)

        try:
            state = yield ctx.wait_for(
                "Activate 9Now on another device",
                [
                    ("Code", str(challenge.auth_code)),
                    ("Open", str(challenge.verification_uri)),
                ],
                poll=poll,
                timeout=float(max(60, min(challenge.expires_in or 300, 600))),
                interval=float(max(3, challenge.interval or api.DEVICE_POLL_INTERVAL)),
                hint="Enter the code at 9now.com.au/activate, then return here.",
            )
        except api.NineNowError as exc:
            ctx.error(str(exc))
            return
        if not isinstance(state, api.Session) or not state.access_token:
            ctx.error("9Now activation finished without an access token")
            return
        self._save_state(state)
        self._api = client
        ctx.log("9Now activated", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        if self.ctx.tokens.read(self.TOKEN_FILE):
            self.ctx.tokens.write(
                self.TOKEN_FILE,
                {"access_token": "", "refresh_token": "", "token_expires": 0},
            )
        self._api = None
        super().logout()

    def _new_client(self, state: api.Session | None = None) -> api.NineNowApi:
        return api.NineNowApi(
            session=self.ctx.session(user_agent=api.USER_AGENT, cookies=False),
            state=state or api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE)),
            region=self.region(),
            on_save=self._save_state,
            system=self.drm_system(),
        )

    def _save_state(self, state: api.Session) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())

    def client(self, ctx: FlowContext | None = None, *, require_auth: bool = False) -> api.NineNowApi:
        if self._api is not None:
            self._api.region = self.region()
            self._api.system = self.drm_system()
            try:
                ok = self._api.ensure_auth(required=require_auth)
            except api.NineNowError:
                self._api = None
                if require_auth:
                    raise
            else:
                if self._api.state.signed_in:
                    self._save_state(self._api.state)
                elif not ok and not require_auth:
                    pass
                return self._api

        client = self._new_client()
        if ctx is not None and (require_auth or client.state.signed_in):
            ctx.status("Opening 9Now session")
        try:
            client.ensure_auth(required=require_auth)
        except api.NineNowError:
            if require_auth:
                raise
        if client.state.signed_in:
            self._save_state(client.state)
        self._api = client
        return client

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error(
                f"That does not look like a 9Now link or series slug: {target}\n"
                "Expected https://www.9now.com.au/<series> or …/<season>/clip-<id>."
            )
            return
        try:
            client = self.client(ctx)
        except api.NineNowError as exc:
            ctx.error(str(exc))
            return

        if parsed.kind == "clip" and parsed.season_slug and parsed.clip_id:
            try:
                ctx.status(f"Finding clip {parsed.clip_id}")
                page, season, clip = client.find_clip(parsed.series_slug, parsed.season_slug, parsed.clip_id)
            except api.NineNowError as exc:
                ctx.warn(str(exc))
                try:
                    page = client.series(parsed.series_slug)
                except api.NineNowError as err:
                    ctx.error(str(err))
                    return
                season = next((s for s in page.seasons if s.slug == parsed.season_slug), None)
                if season is None:
                    ctx.error(f"Season {parsed.season_slug!r} not found")
                    return
                yield from self._pick_videos(ctx, client, page, season, kind="clips")
                return
            yield from self._emit_episode(ctx, client, page, season, clip)
            return

        if parsed.kind == "episode" and parsed.season_slug and parsed.episode_slug:
            try:
                ctx.status(f"Finding {parsed.episode_slug}")
                page, season, episode = client.find_episode(parsed.series_slug, parsed.season_slug, parsed.episode_slug)
            except api.NineNowError as exc:
                ctx.warn(str(exc))
                yield from self._series(ctx, client, parsed.series_slug)
                return
            if page.is_movie:
                yield from self._emit_movie(ctx, client, page, episode)
            else:
                yield from self._emit_episode(ctx, client, page, season, episode)
            return

        yield from self._series(ctx, client, parsed.series_slug)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        wanted = str(query or "").strip()
        if not wanted:
            return
        try:
            client = self.client(ctx)
            ctx.status(f"Searching 9Now for {wanted}")
            hits = client.search(wanted)
        except api.NineNowError as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"9Now found nothing for {wanted!r}")
            return

        page = ctx.pick(
            f"9Now  ·  {wanted}  ·  {len(hits)} result(s)",
            [
                Choice(hit.label, hit, detail=hit.detail, tags=("movie" if hit.is_movie else "series",))
                for hit in hits[:80]
            ],
        )
        while True:
            chosen = yield page
            if chosen is None:
                return
            page.cursor = hits.index(chosen)
            try:
                yield from self._series(ctx, client, chosen.slug)
            except Back:
                continue
            if not ctx.interactive:
                return

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx, require_auth=False)
        except api.NineNowError as exc:
            ctx.error(str(exc))
            return
        region = self.region()
        try:
            ctx.status(f"Loading 9Now live · {region.upper()}")
            items = client.live(region)
        except api.NineNowError as exc:
            ctx.error(str(exc))
            return
        ctx.log(f"{len(items)} live item(s) · {region.upper()}", "ok")
        page = ctx.pick(
            f"9Now Live  ·  {region.upper()}",
            [Choice(item.label, item, detail=item.detail) for item in items],
            multi=True,
            hint="space to tick, enter to confirm",
        )
        while True:
            chosen = list((yield page) or [])
            if not chosen:
                return
            page.cursor = items.index(chosen[0])
            try:
                if len(chosen) > 1:
                    ctx.batch(len(chosen))
                for item in chosen:
                    yield from self._emit_live(ctx, client, item)
            except Back:
                continue
            if not ctx.interactive:
                return

    def _series(self, ctx: FlowContext, client: api.NineNowApi, slug: str) -> Iterator[Ask]:
        try:
            ctx.status(f"Loading {slug}")
            page = client.series(slug)
        except api.NineNowError as exc:
            ctx.error(str(exc))
            return
        if page.is_movie:
            yield from self._movie(ctx, client, page)
            return
        if len(page.seasons) == 1:
            yield from self._season(ctx, client, page, page.seasons[0])
            return
        picker = ctx.pick(
            f"{page.name}  ·  seasons",
            [
                Choice(
                    entry.label,
                    entry,
                    detail=f"{entry.episode_count} episode(s)" if entry.episode_count else entry.slug,
                )
                for entry in page.seasons
            ],
        )
        while True:
            season = yield picker
            if season is None:
                return
            picker.cursor = page.seasons.index(season)
            try:
                yield from self._season(ctx, client, page, season)
            except Back:
                continue
            if not ctx.interactive:
                return

    def _season(
        self, ctx: FlowContext, client: api.NineNowApi, page: api.SeriesPage, season: api.Season
    ) -> Iterator[Ask]:
        picker = ctx.pick(
            f"{page.name}  ·  {season.label}  ·  what to list",
            [
                Choice("Episodes", "episodes"),
                Choice("Clips", "clips"),
            ],
        )
        while True:
            kind = yield picker
            if kind not in {"episodes", "clips"}:
                return
            picker.cursor = int(kind == "clips")
            try:
                yield from self._pick_videos(ctx, client, page, season, kind=kind)
            except Back:
                continue
            if not ctx.interactive:
                return

    def _movie(self, ctx: FlowContext, client: api.NineNowApi, page: api.SeriesPage) -> Iterator[Ask]:
        season = page.seasons[0]
        try:
            ctx.status(f"Loading {page.name}")
            items = client.episodes(page.slug, season.slug)
        except api.NineNowError as exc:
            ctx.error(str(exc))
            return
        if not items:
            ctx.warn(f"9Now currently has no playable video for {page.name}")
            return
        if len(items) == 1:
            yield from self._emit_movie(ctx, client, page, items[0])
            return
        picker = ctx.pick(
            f"{page.name}  ·  available versions",
            [Choice(entry.label, entry, detail=entry.detail) for entry in items],
        )
        while True:
            item = yield picker
            if item is None:
                return
            picker.cursor = items.index(item)
            try:
                yield from self._emit_movie(ctx, client, page, item)
            except Back:
                continue
            if not ctx.interactive:
                return

    def _pick_videos(
        self,
        ctx: FlowContext,
        client: api.NineNowApi,
        page: api.SeriesPage,
        season: api.Season,
        *,
        kind: str,
    ) -> Iterator[Ask]:
        load = client.clips if kind == "clips" else client.episodes
        try:
            ctx.status(f"Loading {kind} · {season.label}")
            items = load(page.slug, season.slug)
        except api.NineNowError as exc:
            ctx.error(str(exc))
            return
        if not items:
            ctx.warn(f"No {kind} in {page.name} · {season.label}")
            return
        picker = ctx.pick(
            f"{page.name}  ·  {season.label}  ·  {kind}",
            [Choice(item.label, item, detail=item.detail) for item in items],
            multi=True,
            hint="space to tick, enter to confirm",
        )
        while True:
            chosen = list((yield picker) or [])
            if not chosen:
                return
            picker.cursor = items.index(chosen[0])
            try:
                if len(chosen) > 1:
                    ctx.batch(len(chosen))
                for item in chosen:
                    yield from self._emit_episode(ctx, client, page, season, item)
            except Back:
                continue
            if not ctx.interactive:
                return

    def _emit_movie(
        self,
        ctx: FlowContext,
        client: api.NineNowApi,
        page: api.SeriesPage,
        item: api.Episode,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {page.name}")
            client.system = self.drm_system()
            source = client.source(item.brightcove_id, live=False)
        except api.NineNowError as exc:
            ctx.error(f"{page.name}: {exc}")
            return
        title = Title(
            id=item.brightcove_id,
            kind=TitleKind.MOVIE,
            name=page.name,
            year=page.year or None,
            duration=(item.duration_ms / 1000.0) if item.duration_ms else None,
            synopsis=item.description or page.description or None,
            service=self.ID,
        )
        yield ctx.emit(self._playback(title, source, client=client))

    def _emit_episode(
        self,
        ctx: FlowContext,
        client: api.NineNowApi,
        page: api.SeriesPage,
        season: api.Season,
        item: api.Episode,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {item.label}")
            client.system = self.drm_system()
            source = client.source(item.brightcove_id, live=False)
        except api.NineNowError as exc:
            ctx.error(f"{item.label}: {exc}")
            return
        ctx.log(f"{item.label}: {source.line()}")

        season_num = season.season_number or None
        if item.is_clip:
            title = Title(
                id=item.brightcove_id,
                kind=TitleKind.EPISODE,
                name=page.name,
                season=season_num,
                episode=item.clip_number or item.episode_number or None,
                episode_name=item.title,
                duration=(item.duration_ms / 1000.0) if item.duration_ms else None,
                synopsis=item.description or None,
                service=self.ID,
            )
        else:
            title = Title(
                id=item.brightcove_id,
                kind=TitleKind.EPISODE,
                name=page.name,
                season=season_num,
                episode=item.episode_number or None,
                episode_name=item.title,
                duration=(item.duration_ms / 1000.0) if item.duration_ms else None,
                synopsis=item.description or None,
                service=self.ID,
            )
        yield ctx.emit(self._playback(title, source, client=client))

    def _emit_live(self, ctx: FlowContext, client: api.NineNowApi, item: api.LiveItem) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {item.name}")
            client.system = self.drm_system()
            client.region = self.region()
            source = client.live_source(item)
        except api.NineNowError as exc:
            ctx.error(f"{item.name}: {exc}")
            return
        ctx.log(f"{item.name}: {source.line()}")
        title = Title(
            id=item.id or item.brightcove_id or item.name,
            kind=TitleKind.CHANNEL,
            name=item.name,
            channel=item.name,
            synopsis=item.description or None,
            service=self.ID,
        )
        yield ctx.emit(self._playback(title, source, live=True))

    def _playback(
        self, title: Title, source: api.Source, *, live: bool = False, client: api.NineNowApi | None = None
    ) -> Playback:
        if not live and title.duration is None and source.duration_ms > 0:
            title.duration = source.duration_ms / 1000
        chapters = ()
        if not live and self.fetch_chapters_enabled():
            chapters = client.chapters(source) if client is not None and source.chapter_tracks else source.chapters
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
            is_live=live,
            note=source.line(),
            chapters=list(chapters),
            attachments=list(source.attachments) if self.fetch_attachments_enabled() else [],
        )
        if source.encrypted:
            if not source.license_url:
                raise CdmError(f"9Now returned an encrypted {source.system} source without a licence endpoint")
            playback.drm = DrmInfo(
                system=source.system,
                license_url=source.license_url,
                headers={
                    "Origin": api.WWW,
                    "Referer": f"{api.WWW}/",
                    "User-Agent": api.USER_AGENT,
                    **dict(source.license_headers),
                },
            )
        else:
            playback.drm = DrmInfo(clear=True)
        return playback

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        if drm.system != "widevine":
            raise CdmError("9Now Widevine transport cannot send a PlayReady challenge")
        if not drm.license_url:
            raise CdmError("9Now playback returned no Widevine licence URL")
        try:
            return self._licence_client().widevine_license(drm.license_url, challenge, headers=drm.headers)
        except api.NineNowError as exc:
            raise CdmError(str(exc)) from exc

    def get_license_soap(self, challenge: str, drm: DrmInfo) -> str:
        if drm.system != "playready":
            raise CdmError("9Now SOAP licence transport requires PlayReady")
        if not drm.license_url:
            raise CdmError("9Now playback returned no PlayReady licence URL")
        try:
            return self._licence_client().playready_license(drm.license_url, challenge, headers=drm.headers)
        except api.NineNowError as exc:
            raise CdmError(str(exc)) from exc

    def _licence_client(self) -> api.NineNowApi:
        client = getattr(self, "_api", None)
        if client is not None:
            return client
        client = self._new_client()
        self._api = client
        return client


__all__ = ["NineNow", "api"]
