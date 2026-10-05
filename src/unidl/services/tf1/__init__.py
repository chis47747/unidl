"""TF1+ (mytf1) - free French FTA with optional TV account.

Authorization:  smart_tv_anonymous guest JWT (default), or OAuth device-code
                at tf1.fr/tv for full catalogue rights.
Geofence:       FR.
Playback:       DASH via mediainfocombo; Widevine DRM proxy when present.
Catalogue:      URL (tf1.fr), GraphQL search, live channel list.
"""

from __future__ import annotations

from collections.abc import Iterator

from ...core.cdm import CdmError
from ...core.flow import Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from . import api

_LOGIN_METHOD = Setting(
    key="login_method",
    label="Login method",
    kind="choice",
    options=[
        Option("anonymous", "Free · anonymous smart TV token"),
        Option("tv", "TV login · activate at tf1.fr/tv"),
    ],
    default="anonymous",
    help=(
        "Anonymous works for free linear streams such as LCI and limited catalogue. "
        "TV login unlocks full TF1+ rights for JT and most catch-up."
    ),
    resets_session=True,
)


@registry.register
class TF1(Service):
    """TF1+ France (Android TV / mytf1)."""

    ID = "tf1"
    NAME = "TF1+"
    TAG = "TF1"
    ALIASES = ("tf1+", "mytf1", "tf1.fr", "lci")
    TITLE_RE = r"(?:www\.)?(?:tf1\.fr|tf1info\.fr|lci\.fr)/"
    GEOFENCE = ("FR",)
    DESCRIPTION = (
        "TF1+ free anonymous Android TV token, optional TV activation, "
        "search, live channels, DASH Widevine when protected. FR IP required."
    )
    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SETTINGS = [_LOGIN_METHOD]
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = True
    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx):
        super().__init__(ctx)
        self._api: api.Tf1Api | None = None

    def login_method(self) -> str:
        return api.normalize_login_method(str(self.settings.get("login_method") or "anonymous"))

    def auth_status(self) -> AuthStatus:
        state = api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        method = self.login_method()
        if state.is_fresh():
            mode = state.login_method or method
            right = f" · {state.right}" if state.right else ""
            if mode == "tv":
                return AuthStatus(
                    logged_in=True,
                    anonymous_ok=True,
                    label=f"TV session{right}",
                    detail="token cache",
                )
            return AuthStatus(
                logged_in=False,
                anonymous_ok=True,
                label=f"anonymous session ready{right}",
                detail="token cache",
            )
        if method == "tv":
            return AuthStatus(
                logged_in=False,
                anonymous_ok=True,
                label="no TV session",
                detail="anonymous still available; Sign in to activate",
            )
        return AuthStatus(
            logged_in=False,
            anonymous_ok=True,
            label="free · will authenticate on demand",
            detail="no cached anonymous token",
        )

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client()
        method = self.login_method()
        try:
            if method == "anonymous":
                ctx.status("Requesting TF1 anonymous token")
                client.login_anonymous()
                self._api = client
                ctx.log(f"anonymous session ready · right={client.state.right}", "ok")
                return
            if client.state.is_fresh() and client.state.login_method == "tv":
                self._api = client
                ctx.log("already signed in with TV session", "ok")
                return
            if client.state.refresh_token:
                if client.refresh() and client.state.login_method == "tv":
                    self._api = client
                    ctx.log("TF1 TV session refreshed", "ok")
                    return
            ctx.status("Starting TF1 TV activation")
            challenge = client.start_device_code()
        except api.Tf1Error as exc:
            ctx.error(str(exc))
            return

        def poll() -> api.Session | None:
            return client.poll_device_code(challenge)

        try:
            state = yield ctx.wait_for(
                "Activate TF1+ on another device",
                [
                    ("Open", challenge.verification_url),
                    ("Code", challenge.user_code),
                    "Enter the code on the activation page, then return here.",
                ],
                poll=poll,
                timeout=float(challenge.expires_in),
                interval=float(challenge.interval),
                hint=api.ACTIVATION_URL,
            )
        except api.Tf1Error as exc:
            ctx.error(str(exc))
            return
        except Back:
            return
        if not isinstance(state, api.Session) or not state.signed_in:
            ctx.error("TF1 activation finished without a session")
            return
        self._api = client
        ctx.log(f"TV login confirmed · right={state.right}", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        self._api = None
        super().logout()

    def _write_state(self, state: api.Session) -> None:
        path = self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _new_client(self) -> api.Tf1Api:
        return api.Tf1Api(
            self.ctx.session(user_agent=api.USER_AGENT, cookies=False),
            state=api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE)),
            on_save=self._write_state,
            login_method=self.login_method(),
        )

    def client(self, ctx: FlowContext | None = None) -> api.Tf1Api:
        if self._api is not None:
            self._api.login_method = self.login_method()
            try:
                self._api.ensure_auth()
                return self._api
            except api.Tf1Error:
                self._api = None
        client = self._new_client()
        if ctx is not None:
            ctx.status("Opening TF1+ session")
        client.ensure_auth()
        self._api = client
        if ctx is not None:
            mode = client.state.login_method or self.login_method()
            ctx.log(f"TF1+ session ready · {mode} · right={client.state.right}", "ok")
        return client

    # ---------------------------------------------------------------- browsing

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        route = api.parse_input(target)
        if route is None:
            ctx.error(
                f"That does not look like a TF1 link or id: {target}\n"
                "Expected https://www.tf1.fr/…, L_LCI, or a video UUID."
            )
            return
        try:
            client = self.client(ctx)
        except api.Tf1Error as exc:
            ctx.error(str(exc))
            return
        kind = route.get("type") or ""
        try:
            if kind == "Live":
                item = client.live_by_slug(route["id"])
                yield from self._play_item(ctx, client, item)
            elif kind == "LiveSlug":
                item = client.live_by_slug(route["id"])
                yield from self._play_item(ctx, client, item)
            elif kind == "Video":
                yield from self._play_media(
                    ctx,
                    client,
                    route["id"],
                    title=route["id"],
                )
            elif kind == "VideoSlug":
                item = client.video_by_slug(route.get("programSlug") or "", route["id"])
                yield from self._play_item(ctx, client, item)
            elif kind == "ProgramSlug":
                program = client.program_by_slug(route["id"])
                yield from self._program(ctx, client, program)
            else:
                ctx.error(f"Unsupported TF1 route: {kind}")
        except api.Tf1Error as exc:
            ctx.error(str(exc))

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        wanted = str(query or "").strip()
        if not wanted:
            ctx.warn("Enter a search term")
            return
        try:
            client = self.client(ctx)
            ctx.status(f"Searching TF1+ for {wanted}")
            hits = client.search(wanted)
        except api.Tf1Error as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"TF1+ found nothing for {wanted}")
            return
        while True:
            try:
                hit = yield ctx.pick(
                    f"TF1+ · {wanted}",
                    [
                        Choice(
                            item.label,
                            item,
                            detail=self._detail(item),
                            tags=(item.kind,),
                        )
                        for item in hits
                    ],
                )
            except Back:
                return
            if not isinstance(hit, api.CatalogItem):
                return
            try:
                if hit.kind == "Program":
                    yield from self._program(ctx, client, hit)
                else:
                    yield from self._play_item(ctx, client, hit)
            except Back:
                continue

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            channels = client.live_channels()
        except api.Tf1Error as exc:
            ctx.error(str(exc))
            return
        while True:
            try:
                channel = yield ctx.pick(
                    f"TF1+ Live  ·  {len(channels)}",
                    [Choice(item.label, item, detail=item.id) for item in channels],
                )
            except Back:
                return
            if not isinstance(channel, api.CatalogItem):
                return
            try:
                yield from self._play_item(ctx, client, channel)
            except Back:
                continue

    def _program(
        self, ctx: FlowContext, client: api.Tf1Api, program: api.CatalogItem
    ) -> Iterator[Ask]:
        slug = program.program_slug or program.slug
        if not slug:
            ctx.error(f"{program.label}: missing program slug")
            return
        try:
            ctx.status(f"Loading {program.label}")
            videos = client.program_videos(slug)
        except api.Tf1Error as exc:
            ctx.error(str(exc))
            return
        if not videos:
            ctx.warn(f"{program.label}: no replay videos")
            return
        while True:
            try:
                picked = yield ctx.pick(
                    program.label,
                    [
                        Choice(item.label, item, detail=self._detail(item))
                        for item in videos
                    ],
                    multi=True,
                    hint="space to tick, enter to confirm",
                )
            except Back:
                return
            chosen = list(picked or [])
            if not chosen:
                return
            if len(chosen) > 1:
                ctx.batch(len(chosen))
            for item in chosen:
                try:
                    yield from self._play_item(ctx, client, item)
                except api.Tf1Error as exc:
                    ctx.error(f"{item.label}: {exc}")
            if len(videos) == 1:
                return

    def _detail(self, item: api.CatalogItem) -> str:
        bits: list[str] = []
        if item.program_name and item.program_name != item.title:
            bits.append(item.program_name)
        if item.season is not None and item.episode is not None:
            bits.append(f"S{item.season:02d}E{item.episode:02d}")
        elif item.episode is not None:
            bits.append(f"E{item.episode}")
        if item.year:
            bits.append(item.year)
        if item.kind and item.kind not in {"Video", "REPLAY"}:
            bits.append(item.kind)
        return " · ".join(bits)

    def _play_item(
        self, ctx: FlowContext, client: api.Tf1Api, item: api.CatalogItem
    ) -> Iterator[Ask]:
        media_id = item.id
        if not media_id:
            ctx.error(f"{item.label}: missing media id")
            return
        yield from self._play_media(
            ctx,
            client,
            media_id,
            title=item.title,
            series_title=item.program_name,
            season=item.season,
            episode=item.episode,
            year=item.year,
            is_live=item.is_live or item.kind == "Live",
        )

    def _play_media(
        self,
        ctx: FlowContext,
        client: api.Tf1Api,
        media_id: str,
        *,
        title: str,
        series_title: str = "",
        season: int | None = None,
        episode: int | None = None,
        year: str = "",
        is_live: bool = False,
    ) -> Iterator[Ask]:
        try:
            ctx.status(f"Resolving {title or media_id}")
            source = client.resolve(
                media_id,
                title=title,
                series_title=series_title,
                season=season,
                episode=episode,
                year=year,
                is_live=is_live,
            )
        except api.Tf1Error as exc:
            ctx.error(str(exc))
            return
        self._api = client
        title_obj = self._title(source)
        note = source.note or ("widevine" if source.encrypted else "clear")
        ctx.log(f"{title_obj.name}: {note}", "ok")
        if source.encrypted:
            drm = DrmInfo(
                system="widevine",
                license_url=source.license_url,
                headers=dict(source.license_headers or {}),
                context={
                    "license_url": source.license_url,
                    "license_headers": dict(source.license_headers or {}),
                },
            )
        else:
            drm = DrmInfo(clear=True)
        yield ctx.emit(
            Playback(
                title=title_obj,
                save_name=self.save_name(title_obj),
                manifest_url=source.manifest,
                headers={"User-Agent": api.PLAYER_USER_AGENT},
                proxy=self.ctx.proxy,
                is_live=source.is_live,
                note=note,
                drm=drm,
            )
        )

    def _title(self, source: api.Source) -> Title:
        if source.is_live:
            return Title(
                id=source.media_id,
                kind=TitleKind.CHANNEL,
                name=source.title,
                channel=source.title,
                service=self.ID,
            )
        if source.season is not None or source.episode is not None or source.series_title:
            return Title(
                id=source.media_id,
                kind=TitleKind.EPISODE,
                name=source.series_title or source.title,
                season=source.season,
                episode=source.episode,
                episode_name=source.title if source.series_title else None,
                year=source.year or None,
                service=self.ID,
            )
        return Title(
            id=source.media_id,
            kind=TitleKind.MOVIE,
            name=source.title,
            year=source.year or None,
            service=self.ID,
        )

    def widevine_transport(self, challenge: bytes, drm: DrmInfo) -> bytes:
        context = dict(drm.context or {})
        license_url = str(context.get("license_url") or drm.license_url or "")
        headers = dict(context.get("license_headers") or drm.headers or {})
        try:
            client = self._api or self._new_client()
            return client.widevine_license(
                challenge, license_url=license_url, license_headers=headers
            )
        except api.Tf1Error as exc:
            raise CdmError(str(exc)) from exc

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        return self.widevine_transport(challenge, drm)


__all__ = ["TF1", "api"]
