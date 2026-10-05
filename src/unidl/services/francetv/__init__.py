"""France.tv - free public broadcaster (Android TV protocol).

Authorization:  free for most catalogue items; optional TV code at
                france.tv/appstv/connexion for login-flagged titles.
Geofence:       FR (geo-info.ftven.fr).
Playback:       DASH/HLS via K7; Widevine with nv-authorizations when DRM.
Catalogue:      URL (france.tv), search, live hub.
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
        Option("free", "Free · no account (public catalogue)"),
        Option("tv", "TV login · code at france.tv/appstv/connexion"),
    ],
    default="free",
    help=(
        "Free works for most catch-up and live streams. "
        "TV login unlocks catalogue items marked login-required."
    ),
    resets_session=True,
)


def _safe_failure(summary: str, exc: api.FranceTvError) -> str:
    if exc.status_code is not None:
        return f"{summary} (HTTP {exc.status_code})"
    return summary


@registry.register
class FranceTV(Service):
    """France.tv public service broadcaster."""

    ID = "francetv"
    NAME = "France.tv"
    TAG = "FTV"
    ALIASES = ("france.tv", "france-tv", "francetv", "pluzz")
    TITLE_RE = (
        r"^https://(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*"
        r"france\.tv(?::443)?(?:/|$)"
    )
    GEOFENCE = ("FR",)
    DESCRIPTION = (
        "France.tv free catch-up, search and live. Optional TV activation for "
        "login-gated titles. Widevine when the stream is protected; FR IP."
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
        self._api: api.FranceTvApi | None = None

    def login_method(self) -> str:
        value = str(self.settings.get("login_method") or "free").strip().lower()
        return "tv" if value in {"tv", "login", "account"} else "free"

    def auth_status(self) -> AuthStatus:
        state = api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        method = self.login_method()
        if state.is_fresh():
            return AuthStatus(
                logged_in=True,
                anonymous_ok=True,
                label="TV session · signed in",
                detail="token cache",
            )
        if state.refresh_token:
            return AuthStatus(
                logged_in=True,
                anonymous_ok=True,
                label="TV session · refresh needed",
                detail="token cache",
            )
        if method == "tv":
            return AuthStatus(
                logged_in=False,
                anonymous_ok=True,
                label="no TV session",
                detail="free catalogue still works; Sign in for gated titles",
            )
        return AuthStatus(
            logged_in=False,
            anonymous_ok=True,
            label="free · no account",
            detail="public catalogue",
        )

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_client(require_login=True)
        try:
            if client.state.is_fresh():
                self._api = client
                ctx.log("France.tv session is already active", "ok")
                return
            if client.state.refresh_token and client.state.user_id:
                try:
                    client.refresh()
                except api.RefreshRejected:
                    pass
                else:
                    self._api = client
                    ctx.log("France.tv session refreshed", "ok")
                    return
            ctx.status("Requesting a France.tv TV code")
            challenge = client.start_device_code()
        except api.FranceTvError as exc:
            ctx.error(_safe_failure("France.tv sign-in could not start", exc))
            return

        def poll() -> api.Session | api.FranceTvError | None:
            # Await retries an exception because a single failed network poll is
            # normally harmless.  FranceTV's client already absorbs the bounded
            # transient cases; anything it raises here is terminal.  Return that
            # error as the answer so the wait closes instead of polling a code the
            # provider has already exchanged for a token.
            try:
                return client.poll_device_code(challenge)
            except api.FranceTvError as exc:
                return exc

        try:
            state = yield ctx.wait_for(
                "Activate France.tv on another device",
                [
                    ("Open", challenge.url),
                    ("Code", challenge.user_code),
                    "Enter the code on the activation page, then return here.",
                ],
                poll=poll,
                timeout=float(challenge.expires_in),
                interval=float(challenge.interval),
                hint=api.ACTIVATION_URL,
            )
        except api.FranceTvError as exc:
            ctx.error(_safe_failure("France.tv activation failed", exc))
            return
        except Back:
            return
        if isinstance(state, api.FranceTvError):
            ctx.error(_safe_failure("France.tv activation failed", state))
            return
        if not isinstance(state, api.Session) or not state.signed_in:
            ctx.error("France.tv activation finished without a valid session")
            return
        self._api = client
        ctx.log("France.tv TV login confirmed", "ok")

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

    def _runtime_session(self):
        self.ctx.refresh_proxy()
        return self.ctx.session(user_agent=api.USER_AGENT, cookies=False)

    def _new_client(self, *, require_login: bool | None = None) -> api.FranceTvApi:
        need = self.login_method() == "tv" if require_login is None else require_login
        return api.FranceTvApi(
            self._runtime_session(),
            state=api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE)),
            on_save=self._write_state,
            require_login=need,
        )

    def _sync_client(self, client: api.FranceTvApi) -> api.FranceTvApi:
        client.session = self._runtime_session()
        client.state = api.Session.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        client.on_save = self._write_state
        client.require_login = self.login_method() == "tv"
        return client

    def client(self, ctx: FlowContext | None = None) -> api.FranceTvApi:
        if self._api is None:
            self._api = self._new_client()
        else:
            self._sync_client(self._api)
        if ctx is not None:
            ctx.status("Opening France.tv")
            ctx.log("France.tv client ready", "ok")
        return self._api

    # ---------------------------------------------------------------- browsing

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        parsed = api.parse_input(target)
        if parsed is None:
            ctx.error("France.tv input is not a supported secure link or content id")
            return
        try:
            client = self.client(ctx)
            kind, value = parsed
            if kind == "content":
                item = client.content_details(value)
                yield from self._dispatch_item(ctx, client, item)
            elif kind == "live_slug":
                item = client.live_by_slug(value)
                yield from self._play(ctx, client, item)
            elif kind == "program":
                yield from self._browse_list(
                    ctx,
                    client,
                    client.program_page(value),
                    "France.tv program episodes",
                )
            elif kind == "taxonomy":
                item = client.resolve_taxonomy(value)
                yield from self._dispatch_item(ctx, client, item)
            else:
                ctx.error("France.tv route is not supported")
        except api.FranceTvError as exc:
            ctx.error(_safe_failure("France.tv link could not be opened", exc))

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        wanted = str(query or "").strip()
        if not wanted:
            ctx.warn("Enter a search term")
            return
        try:
            client = self.client(ctx)
            ctx.status("Searching France.tv")
            hits = client.search(wanted)
        except api.FranceTvError as exc:
            ctx.error(_safe_failure("France.tv search failed", exc))
            return
        if not hits:
            ctx.warn("France.tv search returned no results")
            return
        yield from self._browse_list(ctx, client, hits, "France.tv search results")

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        try:
            client = self.client(ctx)
            ctx.status("Loading France.tv live channels")
            channels = client.live_channels()
        except api.FranceTvError as exc:
            ctx.error(_safe_failure("France.tv live channels could not be loaded", exc))
            return
        if not channels:
            ctx.warn("France.tv returned no live streams")
            return
        while True:
            try:
                channel = yield ctx.pick(
                    f"France.tv Live  ·  {len(channels)}",
                    [
                        Choice(
                            item.label,
                            item,
                            detail=item.channel_label or item.kind,
                            tags=("live", "login" if item.login_required else ""),
                        )
                        for item in channels
                    ],
                )
            except Back:
                return
            if not isinstance(channel, api.CatalogItem):
                return
            try:
                yield from self._play(ctx, client, channel)
            except Back:
                continue

    def _browse_list(
        self,
        ctx: FlowContext,
        client: api.FranceTvApi,
        items: list[api.CatalogItem],
        title: str,
    ) -> Iterator[Ask]:
        while True:
            try:
                picked = yield ctx.pick(
                    title,
                    [
                        Choice(
                            item.label,
                            item,
                            detail=self._detail(item),
                            tags=tuple(
                                tag
                                for tag in (
                                    item.kind,
                                    "login" if item.login_required else "",
                                )
                                if tag
                            ),
                        )
                        for item in items
                    ],
                )
            except Back:
                return
            if not isinstance(picked, api.CatalogItem):
                return
            try:
                yield from self._dispatch_item(ctx, client, picked)
            except Back:
                continue

    def _dispatch_item(
        self, ctx: FlowContext, client: api.FranceTvApi, item: api.CatalogItem
    ) -> Iterator[Ask]:
        if item.kind == "program" or item.program_path and not item.si_id:
            path = item.program_path or item.raw.get("program_path")
            if not path:
                ctx.error("France.tv program metadata is incomplete")
                return
            try:
                ctx.status("Loading France.tv program")
                episodes = client.program_page(str(path))
            except api.FranceTvError as exc:
                ctx.error(_safe_failure("France.tv program could not be loaded", exc))
                return
            if not episodes:
                ctx.warn("France.tv program has no playable episodes")
                return
            yield from self._browse_list(
                ctx,
                client,
                episodes,
                "France.tv program episodes",
            )
            return
        if item.kind == "collection" or item.collection_path and not item.si_id:
            path = item.collection_path
            if not path:
                ctx.error("France.tv collection metadata is incomplete")
                return
            try:
                ctx.status("Loading France.tv collection")
                children = client.collection_page(path)
            except api.FranceTvError as exc:
                ctx.error(_safe_failure("France.tv collection could not be loaded", exc))
                return
            if not children:
                ctx.warn("France.tv collection is empty")
                return
            yield from self._browse_list(
                ctx,
                client,
                children,
                "France.tv collection",
            )
            return
        if item.kind == "event" or item.event_path and not item.si_id:
            path = item.event_path
            if not path:
                ctx.error("France.tv event metadata is incomplete")
                return
            try:
                ctx.status("Loading France.tv event")
                children = client.event_page(path)
            except api.FranceTvError as exc:
                ctx.error(_safe_failure("France.tv event could not be loaded", exc))
                return
            if not children:
                ctx.warn("France.tv event is empty")
                return
            yield from self._browse_list(
                ctx,
                client,
                children,
                "France.tv event",
            )
            return
        if not item.si_id:
            ctx.error("France.tv item is not a playable video")
            return
        yield from self._play(ctx, client, item)

    def _detail(self, item: api.CatalogItem) -> str:
        bits: list[str] = []
        if item.channel_label:
            bits.append(item.channel_label)
        if item.program_title and item.program_title != item.title:
            bits.append(item.program_title)
        if item.season is not None and item.episode is not None:
            bits.append(f"S{item.season:02d}E{item.episode:02d}")
        elif item.episode is not None:
            bits.append(f"E{item.episode:02d}")
        if item.year:
            bits.append(item.year)
        if item.login_required:
            bits.append("login required")
        return " · ".join(bits)

    def _play(
        self, ctx: FlowContext, client: api.FranceTvApi, item: api.CatalogItem
    ) -> Iterator[Ask]:
        try:
            client = self.client()
            if item.login_required and not client.state.is_fresh():
                client.ensure_session(for_item=item)
            ctx.status("Resolving France.tv playback")
            source = client.resolve(item)
        except api.FranceTvAuthRequired as exc:
            ctx.error(_safe_failure("France.tv TV sign-in is required", exc))
            return
        except api.FranceTvError as exc:
            ctx.error(_safe_failure("France.tv playback could not be resolved", exc))
            return
        self._api = client
        title = self._title(source)
        note = source.note or ("widevine" if source.encrypted else "clear")
        ctx.log(f"France.tv playback ready · {note}", "ok")
        if source.encrypted:
            drm = DrmInfo(
                system="widevine",
                license_url=source.license_url,
                headers={"nv-authorizations": source.drm_token},
                context={
                    "license_url": source.license_url,
                    "drm_token": source.drm_token,
                },
            )
        else:
            drm = DrmInfo(clear=True)
        yield ctx.emit(
            Playback(
                title=title,
                save_name=self.save_name(title),
                manifest_url=source.manifest,
                headers={"User-Agent": api.USER_AGENT},
                proxy=self.ctx.proxy,
                is_live=source.is_live,
                note=note,
                drm=drm,
            )
        )

    def _title(self, source: api.Source) -> Title:
        if source.is_live:
            return Title(
                id=source.content_id or source.media_id,
                kind=TitleKind.CHANNEL,
                name=source.title,
                channel=source.title,
                service=self.ID,
            )
        if source.season is not None or source.episode is not None:
            return Title(
                id=source.content_id or source.media_id,
                kind=TitleKind.EPISODE,
                name=source.program_title or source.title,
                season=source.season,
                episode=source.episode,
                episode_name=source.title if source.program_title else None,
                year=source.year or None,
                service=self.ID,
            )
        return Title(
            id=source.content_id or source.media_id,
            kind=TitleKind.MOVIE,
            name=source.title,
            year=source.year or None,
            service=self.ID,
        )

    def widevine_transport(self, challenge: bytes, drm: DrmInfo) -> bytes:
        context = dict(drm.context or {})
        license_url = str(context.get("license_url") or drm.license_url or "")
        drm_token = str(
            context.get("drm_token")
            or (drm.headers or {}).get("nv-authorizations")
            or ""
        )
        try:
            client = self.client()
            return client.widevine_license(
                challenge,
                license_url=license_url,
                drm_token=drm_token,
            )
        except api.FranceTvError as exc:
            raise CdmError(_safe_failure("France.tv license request failed", exc)) from exc

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        return self.widevine_transport(challenge, drm)


__all__ = ["FranceTV", "api"]
